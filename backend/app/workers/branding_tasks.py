"""Celery tasks for the white-label branding subsystem.

Single task: ``provision_ssl(admin_id)`` orchestrates the full custom
domain provisioning pipeline by shelling out to the deploy helper
script ``marginwealth-add-branded-domain.sh`` which:

  1. Writes a per-tenant nginx server block proxying to the user
     frontend (port 3000).
  2. Reloads nginx so the block is reachable for ACME HTTP-01
     validation.
  3. Runs ``certbot --nginx`` to obtain a Let's Encrypt cert and
     inject SSL listeners into that same block.
  4. On failure, removes the half-provisioned config so the domain
     never serves the app over plaintext.

Final step: flip the User row's ``custom_domain_status`` to READY /
FAILED with a human-readable error.

Server prerequisites (see ``deploy/README.md``):

* ``certbot`` and ``python3-certbot-nginx`` installed.
* The provisioning helper scripts deployed to ``/usr/local/bin/``:

    /usr/local/bin/marginwealth-add-branded-domain
    /usr/local/bin/marginwealth-remove-branded-domain

* The backend's OS user has passwordless sudo for those two scripts
  via ``/etc/sudoers.d/marginwealth-branding``.
* ``settings.PLATFORM_PUBLIC_IP`` set in ``.env`` — admins point
  their A records here.

If ANY of the above is missing, the task gracefully marks the row
FAILED with a human-readable error instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Final

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


CERTBOT_TIMEOUT_SEC: Final[int] = 180
CERTBOT_EMAIL_DEFAULT: Final[str] = "ops@marginwealth.com"
PROVISION_SCRIPT: Final[str] = "/usr/local/bin/marginwealth-add-branded-domain"


def _script_exists(path: str) -> bool:
    """Cheap check that the provisioning helper is deployed and
    executable. We don't try to invoke it here — the actual sudo run
    will surface any permission errors with full context."""
    import os

    return os.path.isfile(path) and os.access(path, os.X_OK)


@celery_app.task(
    name="app.workers.branding_tasks.provision_ssl",
    bind=True,
    autoretry_for=(),  # explicit no-auto-retry — we manage status ourselves
    max_retries=0,
)
def provision_ssl(self, admin_id: str) -> dict:
    """Run certbot for an admin's custom_domain and flip status.

    Returns a small dict the result backend can serialize so an
    operator inspecting `celery events` sees what happened.
    """
    from app.core.database import close_database, init_database
    from app.models.user import User
    from app.services import branding_service
    from beanie import PydanticObjectId

    async def _run() -> dict:
        await init_database()
        try:
            user = await User.get(PydanticObjectId(admin_id))
            if user is None:
                return {"ok": False, "error": "admin_not_found"}

            # Idempotency: if status is already READY / not PROVISIONING,
            # bail without touching certbot.
            if user.custom_domain_status != branding_service.STATUS_PROVISIONING:
                return {
                    "ok": False,
                    "error": f"unexpected_status={user.custom_domain_status}",
                }

            domain = user.custom_domain
            if not domain:
                await branding_service.mark_domain_failed(
                    user.id, "custom_domain unset on row"
                )
                return {"ok": False, "error": "no_domain"}

            # Sanity: provisioning helper deployed?
            # We check the script presence rather than just the certbot
            # binary because the helper is the actual contract — a host
            # with certbot but no helper script is misconfigured for
            # Phase 4 and the human-readable error helps the operator.
            if not _script_exists(PROVISION_SCRIPT):
                await branding_service.mark_domain_failed(
                    user.id,
                    f"Provisioning helper not found at {PROVISION_SCRIPT}. "
                    "Deploy `deploy/scripts/marginwealth-add-branded-domain.sh` "
                    "and update sudoers (see deploy/README.md).",
                )
                return {"ok": False, "error": "helper_missing"}
            if shutil.which("certbot") is None:
                await branding_service.mark_domain_failed(
                    user.id,
                    "certbot is not installed on this host. "
                    "Run `apt install certbot python3-certbot-nginx`.",
                )
                return {"ok": False, "error": "certbot_missing"}

            # Use the admin's email when available so they receive the
            # Let's Encrypt expiry notices for THEIR cert; otherwise
            # fall back to the platform ops mailbox.
            email = (user.email or "").strip() or CERTBOT_EMAIL_DEFAULT

            cmd = [
                "sudo",
                "-n",  # never prompt for a password — fail fast if sudoers is wrong
                PROVISION_SCRIPT,
                domain,
                email,
            ]

            logger.info(
                "branding_provision_ssl_start admin_id=%s domain=%s", admin_id, domain
            )
            try:
                result = subprocess.run(  # noqa: S603 — controlled args
                    cmd,
                    timeout=CERTBOT_TIMEOUT_SEC,
                    capture_output=True,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                await branding_service.mark_domain_failed(
                    user.id,
                    f"Provisioning timed out after {CERTBOT_TIMEOUT_SEC}s. "
                    "Check the worker can reach Let's Encrypt.",
                )
                return {"ok": False, "error": "timeout"}
            except FileNotFoundError:
                await branding_service.mark_domain_failed(
                    user.id, "sudo or provisioning helper not found in PATH"
                )
                return {"ok": False, "error": "binary_missing"}

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                err = (stderr or stdout or "provisioning failed without output")[:500]
                logger.warning(
                    "branding_provision_ssl_failed admin_id=%s rc=%d err=%s",
                    admin_id,
                    result.returncode,
                    err,
                )
                await branding_service.mark_domain_failed(user.id, err)
                return {
                    "ok": False,
                    "error": "provision_failed",
                    "rc": result.returncode,
                }

            await branding_service.mark_domain_ready(user.id)

            # Best-effort admin-events ping so an open admin UI sees
            # the live status flip without polling.
            try:
                from app.services.admin_events import publish_admin_event

                await publish_admin_event(
                    "branding_domain_ready",
                    {"admin_id": str(user.id), "domain": domain},
                )
            except Exception:  # pragma: no cover
                pass

            logger.info(
                "branding_provision_ssl_ready admin_id=%s domain=%s", admin_id, domain
            )
            return {"ok": True, "domain": domain}
        finally:
            await close_database()

    return asyncio.run(_run())
