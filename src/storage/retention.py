from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
import psycopg

from src.core.config import history_retention_days
from src.storage.analytics import AnalyticsStore

logger = logging.getLogger(__name__)


async def run_history_retention(store: AnalyticsStore, *, interval_seconds: float = 86400.0) -> None:
    if interval_seconds <= 0:
        raise ValueError("Retention interval must be positive")
    while True:
        try:
            total = 0
            while True:
                count = await asyncio.to_thread(store.cleanup_expired_history)
                total += count
                if count < 1000:
                    break
                await asyncio.sleep(0)
            if total:
                logger.info("Removed personal associations from %d expired download records", total)
        except Exception:
            logger.warning("History retention failed; will retry on the next scheduled run")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or apply personal history retention without deleting statistics.")
    parser.add_argument("--enroll-existing", action="store_true", help="Include pre-migration history in retention.")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag only counts are displayed.")
    args = parser.parse_args()
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL is required")
    store = None
    try:
        store = AnalyticsStore(database_url, history_retention_days=history_retention_days(), initialize=False)
        preview = store.retention_preview(include_legacy=args.enroll_existing)
        print(f"Eligible download records: {preview.download_records}")
        print(f"Eligible inactive profiles: {preview.profile_records}")
        if not args.apply:
            print("Preview only. No personal data was changed.")
            return
        if args.enroll_existing:
            print(f"Existing records enrolled: {store.enroll_legacy_history()}")
        total = 0
        while True:
            count = store.cleanup_expired_history()
            total += count
            if count < 1000:
                break
        print(f"Personal associations removed: {total}. Download totals are preserved.")
    except (psycopg.Error, ValueError):
        parser.exit(1, "Retention maintenance failed. Check database access and retention settings.\n")
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
