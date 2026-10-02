"""Celery application — workers for squareoff, risk, EOD, reports, notifications, backups.

Phase 1 wires the app and beat schedule; tasks themselves are stubbed and
filled in by later phases. Running `celery -A app.workers.celery_app worker`
should boot cleanly.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "setupfx",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.branding_tasks",  # white-label SSL provisioning
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.DEFAULT_TIMEZONE,
    enable_utc=False,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=4,
    task_default_queue="default",
    task_routes={
        "app.workers.squareoff_tasks.*": {"queue": "squareoff"},
        "app.workers.risk_tasks.*": {"queue": "risk"},
        "app.workers.eod_tasks.*": {"queue": "eod"},
        "app.workers.report_tasks.*": {"queue": "reports"},
        "app.workers.notification_tasks.*": {"queue": "notifications"},
        "app.workers.backup_tasks.*": {"queue": "backups"},
    },
    beat_schedule={
        # Filled in by later phases. Concrete tasks are added here once written.
        # "auto-squareoff-every-min": {
        #     "task": "app.workers.squareoff_tasks.run_auto_squareoff",
        #     "schedule": 60.0,
        # },
        # "risk-check-every-30s": {
        #     "task": "app.workers.risk_tasks.run_risk_check",
        #     "schedule": 30.0,
        # },
        # "eod-reset-1545": {
        #     "task": "app.workers.eod_tasks.run_eod",
        #     "schedule": crontab(hour=15, minute=45),
        # },
        # "daily-backup-23": {
        #     "task": "app.workers.backup_tasks.run_daily_backup",
        #     "schedule": crontab(hour=23, minute=0),
        # },
    },
)
