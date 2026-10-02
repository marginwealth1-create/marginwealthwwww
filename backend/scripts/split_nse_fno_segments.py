"""Migrate the retired NSE_FUT / NSE_OPT admin settings rows into the four
granular rows (stock vs index):

    NSE_FUT  ->  NSE_STK_FUT (Stock Future)  +  NSE_IDX_FUT (Index Future)
    NSE_OPT  ->  NSE_STK_OPT (Stock Option)  +  NSE_IDX_OPT (Index Option)

Why this script exists: bumping SEGMENT_DEFAULTS only SEEDS the four new rows
with DEFAULT values on the next boot. The live DB still holds the admin's real
NSE_FUT / NSE_OPT values on the OLD rows. This script COPIES those values onto
both new rows (over-writing the freshly-seeded defaults), re-points per-symbol
script overrides to the correct stock/index row by symbol prefix, then DELETES
the old NSE_FUT / NSE_OPT documents.

Collections handled:
  segment-level (copy old → BOTH new rows, then delete old):
    - netting_segments                 (key: name)
    - user_segment_overrides           (key: user_id + segment_name + symbol)
    - sub_admin_segment_overrides      (key: sub_admin_id + segment_name)
    - super_admin_segment_overrides    (key: super_admin_id + segment_name)
    - broker_segment_overrides         (key: broker_id + segment_name)
  per-symbol (re-point to ONE row by symbol prefix, then update segment_id):
    - netting_script_overrides         (key: segment_name + symbol + scope)

Idempotent — safe to run repeatedly. Once the old rows are gone it is a no-op.

Run from the backend folder:

    cd ~/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.split_nse_fno_segments

Dry-run (no writes):

    python -m scripts.split_nse_fno_segments --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.database import close_database, init_database
from app.models.netting import NettingSegment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("split_nse_fno")

# old row -> (stock row, index row) + display names for the two new rows
SPLIT_MAP: dict[str, tuple[str, str]] = {
    "NSE_FUT": ("NSE_STK_FUT", "NSE_IDX_FUT"),
    "NSE_OPT": ("NSE_STK_OPT", "NSE_IDX_OPT"),
}
DISPLAY_NAMES: dict[str, str] = {
    "NSE_STK_FUT": "Stock Future",
    "NSE_IDX_FUT": "Index Future",
    "NSE_STK_OPT": "Stock Option",
    "NSE_IDX_OPT": "Index Option",
}

# Index underlyings — a script override whose symbol starts with one of these
# belongs to the INDEX row; everything else is a stock derivative.
INDEX_PREFIXES: tuple[str, ...] = (
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAPNIFTY",
    "SENSEX", "BANKEX",
)

# Field names on the segment/override docs that are NOT copyable settings —
# they identify the doc or the segment and must be set explicitly per new row,
# never blindly copied.
_ID_FIELDS = {"_id", "name", "displayName", "segment_name", "segment_id"}


def _index_row_for_symbol(symbol: str, stock_row: str, index_row: str) -> str:
    sym = (symbol or "").upper().lstrip()
    return index_row if sym.startswith(INDEX_PREFIXES) else stock_row


def _copyable(doc: dict) -> dict:
    """Everything on the source doc except identity / segment-name fields."""
    return {k: v for k, v in doc.items() if k not in _ID_FIELDS}


async def _migrate_segment_collection(
    coll,
    *,
    label: str,
    owner_key_fields: list[str],
    has_symbol: bool,
    dry_run: bool,
) -> tuple[int, int]:
    """Copy each old-row doc onto BOTH new rows (upsert by owner-ids + new
    segment name [+ symbol]) then delete the old doc.

    `owner_key_fields` are the doc's identity fields OTHER than the segment
    name (e.g. ["user_id", "symbol"] for user overrides). For netting_segments
    it is empty (the row is global, keyed only by `name`).
    Returns (rows_written, rows_deleted).
    """
    written = 0
    deleted = 0
    is_segments = label == "netting_segments"
    name_field = "name" if is_segments else "segment_name"

    for old_name, (stock_row, index_row) in SPLIT_MAP.items():
        cursor = coll.find({name_field: old_name})
        old_docs = [d async for d in cursor]
        for old in old_docs:
            settings = _copyable(old)
            for new_name in (stock_row, index_row):
                # Build the upsert key: owner ids (+ symbol) + the new name.
                key: dict = {name_field: new_name}
                for f in owner_key_fields:
                    key[f] = old.get(f)
                if has_symbol:
                    key["symbol"] = old.get("symbol")

                update = {"$set": {**settings, name_field: new_name}}
                if is_segments:
                    update["$set"]["displayName"] = DISPLAY_NAMES[new_name]
                if not dry_run:
                    await coll.update_one(key, update, upsert=True)
                written += 1
                logger.info(
                    "%s: %s -> %s%s",
                    label,
                    old_name,
                    new_name,
                    f" (symbol={old.get('symbol')})" if has_symbol and old.get("symbol") else "",
                )
            # Delete the old doc once both new rows are written.
            if not dry_run:
                await coll.delete_one({"_id": old["_id"]})
            deleted += 1
    return written, deleted


async def _migrate_script_overrides(coll, seg_ids: dict[str, object], *, dry_run: bool) -> int:
    """Re-point per-symbol script overrides from NSE_FUT/NSE_OPT to the correct
    granular row by symbol prefix (index prefix → IDX row, else STK row),
    updating both `segment_name` and `segment_id`. In-place update; no delete.
    """
    repointed = 0
    for old_name, (stock_row, index_row) in SPLIT_MAP.items():
        cursor = coll.find({"segment_name": old_name})
        docs = [d async for d in cursor]
        for d in docs:
            new_name = _index_row_for_symbol(d.get("symbol", ""), stock_row, index_row)
            new_id = seg_ids.get(new_name)
            if new_id is None:
                logger.warning(
                    "script override %s: new segment row %s not found — skipping",
                    d.get("symbol"),
                    new_name,
                )
                continue
            if not dry_run:
                await coll.update_one(
                    {"_id": d["_id"]},
                    {"$set": {"segment_name": new_name, "segment_id": new_id}},
                )
            repointed += 1
            logger.info(
                "netting_script_overrides: %s (%s) -> %s",
                d.get("symbol"),
                old_name,
                new_name,
            )
    return repointed


async def main(dry_run: bool) -> None:
    await init_database()
    if dry_run:
        logger.info("DRY-RUN — no writes will be performed")

    db = NettingSegment.get_motor_collection().database

    # Segment rows first so the new NettingSegment _ids exist before we
    # re-point script overrides at them.
    seg_written, seg_deleted = await _migrate_segment_collection(
        db["netting_segments"],
        label="netting_segments",
        owner_key_fields=[],
        has_symbol=False,
        dry_run=dry_run,
    )

    # Look up the new segment _ids (post-write) for the script-override re-point.
    seg_ids: dict[str, object] = {}
    for new_name in DISPLAY_NAMES:
        doc = await db["netting_segments"].find_one({"name": new_name})
        if doc is not None:
            seg_ids[new_name] = doc["_id"]

    # Per-tier segment-level overrides.
    u_written, u_deleted = await _migrate_segment_collection(
        db["user_segment_overrides"],
        label="user_segment_overrides",
        owner_key_fields=["user_id"],
        has_symbol=True,  # user overrides can be symbol-scoped
        dry_run=dry_run,
    )
    sa_written, sa_deleted = await _migrate_segment_collection(
        db["sub_admin_segment_overrides"],
        label="sub_admin_segment_overrides",
        owner_key_fields=["sub_admin_id"],
        has_symbol=False,
        dry_run=dry_run,
    )
    supa_written, supa_deleted = await _migrate_segment_collection(
        db["super_admin_segment_overrides"],
        label="super_admin_segment_overrides",
        owner_key_fields=["super_admin_id"],
        has_symbol=False,
        dry_run=dry_run,
    )
    b_written, b_deleted = await _migrate_segment_collection(
        db["broker_segment_overrides"],
        label="broker_segment_overrides",
        owner_key_fields=["broker_id"],
        has_symbol=False,
        dry_run=dry_run,
    )

    # Per-symbol script overrides — re-point by symbol prefix.
    scripts_repointed = await _migrate_script_overrides(
        db["netting_script_overrides"], seg_ids, dry_run=dry_run
    )

    logger.info(
        "DONE — segments: %d written / %d old deleted; "
        "user_ov: %d/%d; sub_admin_ov: %d/%d; super_admin_ov: %d/%d; "
        "broker_ov: %d/%d; script_ov re-pointed: %d",
        seg_written, seg_deleted,
        u_written, u_deleted,
        sa_written, sa_deleted,
        supa_written, supa_deleted,
        b_written, b_deleted,
        scripts_repointed,
    )
    await close_database()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
