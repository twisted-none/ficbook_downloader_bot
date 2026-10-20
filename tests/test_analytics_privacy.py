import asyncio
import os
from threading import Event
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from aiogram.types import User
from aiohttp.test_utils import TestClient, TestServer
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.monitoring import metrics
from src.storage.analytics import AnalyticsSnapshot, AnalyticsStore, FanficStats, UserStats, render_prometheus


def sample_snapshot():
    return AnalyticsSnapshot(
        12, 3, 20, 15, 0.75,
        [UserStats(987654321, "private_username", "Private Name", 7, "https://example.org/private")],
        [FanficStats("https://example.org/book?token=secret", "Private Title", 9)],
    )


class MetricsPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def test_metrics_contain_only_aggregate_counts(self):
        payload = render_prometheus(sample_snapshot())
        for value in ("987654321", "private_username", "Private Name", "https://", "Private Title",
                      "user_downloads_total", "last_download_url", "secret"):
            self.assertNotIn(value, payload)
        self.assertIn("ficbook_bot_successful_downloads_total 15\n", payload)
        self.assertIn('ficbook_bot_top_fanfic_downloads{rank="1"} 9\n', payload)

    async def test_parallel_scrapes_share_cache_and_health_does_not_wait_for_database(self):
        entered, release = Event(), Event()

        def snapshot(*, include_users):
            self.assertFalse(include_users)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test database was not released")
            return sample_snapshot()

        store = Mock(snapshot=Mock(side_effect=snapshot))
        client = TestClient(TestServer(metrics.create_metrics_app(store)))
        await client.start_server()
        tasks = []
        try:
            tasks = [asyncio.create_task(client.get("/metrics")) for _ in range(5)]
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            response = await asyncio.wait_for(client.get("/health"), timeout=1)
            self.assertEqual(await response.text(), "ok\n")
            release.set()
            responses = await asyncio.gather(*tasks)
            self.assertTrue(all(response.status == 200 for response in responses))
            self.assertEqual((await client.get("/metrics")).status, 200)
            store.snapshot.assert_called_once_with(include_users=False)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()

    async def test_database_failure_is_generic_and_next_scrape_can_retry(self):
        store = Mock(snapshot=Mock(side_effect=[RuntimeError("password=secret"), sample_snapshot()]))
        client = TestClient(TestServer(metrics.create_metrics_app(store)))
        now = [100.0]
        with patch.object(metrics, "monotonic", side_effect=lambda: now[0]):
            await client.start_server()
            try:
                response = await client.get("/metrics")
                self.assertEqual(response.status, 503)
                self.assertNotIn("secret", await response.text())
                self.assertEqual((await client.get("/metrics")).status, 503)
                self.assertEqual(store.snapshot.call_count, 1)
                now[0] += 6.0
                self.assertEqual((await client.get("/metrics")).status, 200)
                self.assertEqual(store.snapshot.call_count, 2)
            finally:
                await client.close()

    async def test_cache_expires_and_refreshes_aggregate_snapshot(self):
        store = Mock(snapshot=Mock(return_value=sample_snapshot()))
        cache = metrics.MetricsCache(store)
        now = [100.0]
        with patch.object(metrics, "monotonic", side_effect=lambda: now[0]):
            await cache.get()
            now[0] += 29.0
            await cache.get()
            self.assertEqual(store.snapshot.call_count, 1)
            now[0] += 2.0
            await cache.get()
            self.assertEqual(store.snapshot.call_count, 2)

    async def test_cancelled_scrape_does_not_start_duplicate_database_query(self):
        entered, release = Event(), Event()

        def snapshot(*, include_users):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test database was not released")
            return sample_snapshot()

        store = Mock(snapshot=Mock(side_effect=snapshot))
        cache = metrics.MetricsCache(store)
        first = asyncio.create_task(cache.get())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(cache.get())
            release.set()
            self.assertIn("successful_downloads_total 15", await second)
            store.snapshot.assert_called_once_with(include_users=False)
        finally:
            release.set()
            await cache.close()


TEST_DATABASE_URL = os.getenv("FICBOOK_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DATABASE_URL, "Set FICBOOK_TEST_DATABASE_URL to an isolated local test database")
class RetentionDatabaseTests(unittest.TestCase):
    def setUp(self):
        params = conninfo_to_dict(TEST_DATABASE_URL)
        if params.get("host") != "127.0.0.1" or params.get("dbname") != "ficbook_security_test":
            self.fail("Integration tests require 127.0.0.1 and database ficbook_security_test")
        self.schema = "privacy_test_" + uuid4().hex
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.database_url = make_conninfo(TEST_DATABASE_URL, options="-c search_path=" + self.schema)
        self.store = AnalyticsStore(self.database_url, history_retention_days=30)
        self.addCleanup(self.store.close)
        self.store.record_user(User(id=101, is_bot=False, first_name="First"), started=True)
        self.store.record_user(User(id=102, is_bot=False, first_name="Second"), started=True)

    def drop_schema(self):
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def row(self, download_id):
        with self.store._connect() as db:
            return db.execute("SELECT * FROM downloads WHERE id = %s", (download_id,)).fetchone()

    def download(self, user=101, *, success=True):
        download_id = self.store.start_download(user, "https://ficbook.net/readfic/123")
        self.store.finish_download(download_id, success=success, url="https://ficbook.net/readfic/123",
                                   title="Test Book", error="Personal error" if not success else "")
        return download_id

    def expire(self, download_id):
        with self.store._connect() as db:
            db.execute("UPDATE downloads SET attempted_at = now() - interval '31 days', "
                       "finished_at = now() - interval '31 days', "
                       "privacy_expires_at = now() - interval '1 day' WHERE id = %s", (download_id,))

    def test_new_history_expires_after_thirty_days(self):
        row = self.row(self.download())
        days = (row["privacy_expires_at"] - row["attempted_at"]).total_seconds() / 86400
        self.assertAlmostEqual(days, 30, places=6)

    def test_cleanup_preserves_global_user_and_fanfic_counts_and_is_idempotent(self):
        old_success = self.download()
        old_error = self.download(102, success=False)
        recent = self.download()
        for download_id in (old_success, old_error):
            self.expire(download_id)
        before = self.store.snapshot()
        self.assertEqual(self.store.cleanup_expired_history(), 2)
        self.assertEqual(self.store.cleanup_expired_history(), 0)
        after = self.store.snapshot()
        for name in ("started_users", "active_users_30d", "download_attempts", "successful_downloads",
                     "success_ratio", "top_fanfics"):
            self.assertEqual(getattr(before, name), getattr(after, name), name)
        self.assertEqual([(u.user_id, u.downloads) for u in before.users],
                         [(u.user_id, u.downloads) for u in after.users])
        for download_id in (old_success, old_error):
            row = self.row(download_id)
            self.assertIsNone(row["user_id"])
            self.assertEqual(row["error"], "")
        self.assertEqual(self.row(recent)["user_id"], 101)
        with self.store._connect() as db:
            users = list(db.execute("SELECT * FROM analytics_users ORDER BY user_id"))
            self.assertEqual([u["downloaded_fanfics"] for u in users], [2, 0])

    def test_legacy_history_requires_explicit_enrollment(self):
        old = self.download()
        self.expire(old)
        with self.store._connect() as db:
            db.execute("UPDATE downloads SET privacy_expires_at = NULL WHERE id = %s", (old,))
        AnalyticsStore(self.database_url, history_retention_days=30).close()
        self.assertEqual(self.store.cleanup_expired_history(), 0)
        self.assertEqual(self.row(old)["user_id"], 101)
        self.assertEqual(self.store.enroll_legacy_history(), 1)
        self.assertEqual(self.store.enroll_legacy_history(), 0)
        self.assertEqual(self.store.cleanup_expired_history(), 1)
        self.assertEqual(self.store.snapshot().successful_downloads, 1)

    def test_cleanup_rolls_back_counters_when_anonymization_fails(self):
        old = self.download()
        self.expire(old)
        with self.store._connect() as db:
            db.execute("ALTER TABLE downloads ADD CONSTRAINT test_fail_cleanup CHECK (user_id IS NOT NULL)")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.cleanup_expired_history()
        self.assertEqual(self.row(old)["user_id"], 101)
        self.assertEqual(self.store.snapshot().users[0].downloads, 1)
        with self.store._connect() as db:
            db.execute("ALTER TABLE downloads DROP CONSTRAINT test_fail_cleanup")
        self.assertEqual(self.store.cleanup_expired_history(), 1)
        self.assertEqual(self.store.snapshot().users[0].downloads, 1)

    def test_errors_after_expiry_are_not_reintroduced(self):
        old = self.download(success=False)
        self.expire(old)
        self.store.cleanup_expired_history()
        self.store.finish_download(old, success=False, url="https://ficbook.net/readfic/123", error="Private again")
        self.assertEqual(self.row(old)["error"], "")

    def test_old_urls_are_sanitized_without_losing_story_statistics(self):
        old = self.download()
        self.expire(old)
        with self.store._connect() as db:
            db.execute("UPDATE downloads SET url = %s WHERE id = %s",
                       ("https://ficbook.net/readfic/123?token=private#personal", old))
        self.store.cleanup_expired_history()
        self.assertEqual(self.row(old)["url"], "https://ficbook.net/readfic/123")
        self.assertEqual(self.store.snapshot().top_fanfics[0].downloads, 1)

    def test_inactive_names_expire_but_user_count_and_settings_remain(self):
        with self.store._connect() as db:
            db.execute("UPDATE bot_users SET profile_expires_at = now() - interval '1 day' WHERE user_id = 101")
        self.store.save_user_formats(101, ("txt",))
        self.store.cleanup_expired_history()
        after = self.store.snapshot()
        self.assertEqual(after.started_users, 2)
        inactive = next(user for user in after.users if user.user_id == 101)
        self.assertEqual((inactive.username, inactive.full_name), ("", ""))
        self.assertEqual(self.store.get_user_formats(101), ("txt",))

    def test_preview_does_not_modify_existing_history(self):
        old = self.download()
        self.expire(old)
        with self.store._connect() as db:
            db.execute("UPDATE downloads SET privacy_expires_at = NULL WHERE id = %s", (old,))
        self.assertEqual(self.store.retention_preview(include_legacy=False).download_records, 0)
        self.assertEqual(self.store.retention_preview(include_legacy=True).download_records, 1)
        self.assertEqual(self.row(old)["user_id"], 101)
        self.assertIsNone(self.row(old)["privacy_expires_at"])

    def test_batched_and_concurrent_cleanup_never_double_counts(self):
        from concurrent.futures import ThreadPoolExecutor
        other = AnalyticsStore(self.database_url)
        self.addCleanup(other.close)
        for _ in range(7):
            self.expire(self.download())
        with ThreadPoolExecutor(max_workers=2) as executor:
            counts = list(executor.map(lambda store: store.cleanup_expired_history(batch_size=4),
                                       (self.store, other)))
        self.assertEqual(sum(counts), 7)
        self.assertEqual(self.store.snapshot().users[0].downloads, 7)
        self.assertEqual(self.store.cleanup_expired_history(), 0)

    def test_migration_preserves_old_rows_views_and_foreign_key(self):
        old = self.download()
        self.expire(old)
        with self.store._connect() as db:
            db.execute("DROP VIEW analytics_users")
            db.execute("ALTER TABLE downloads DROP COLUMN privacy_expires_at")
            db.execute("ALTER TABLE downloads ALTER COLUMN user_id SET NOT NULL")
            db.execute("ALTER TABLE bot_users DROP COLUMN archived_successful_downloads")
            db.execute("ALTER TABLE bot_users DROP COLUMN profile_expires_at")
            db.execute("CREATE VIEW analytics_users AS SELECT u.user_id, u.username, u.full_name, "
                       "COUNT(d.id) AS downloaded_fanfics, NULL::text AS last_download_url, "
                       "u.first_seen_at, u.last_seen_at, u.started_at FROM bot_users u "
                       "LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success' "
                       "GROUP BY u.user_id")
        migrated = AnalyticsStore(self.database_url)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.snapshot().successful_downloads, 1)
        self.assertEqual(migrated.cleanup_expired_history(), 0)
        migrated.enroll_legacy_history()
        self.assertEqual(migrated.cleanup_expired_history(), 1)
        self.assertEqual(migrated.snapshot().users[0].downloads, 1)
        with migrated._connect() as db:
            self.assertEqual(db.execute("SELECT downloaded_fanfics FROM analytics_users WHERE user_id = 101")
                             .fetchone()["downloaded_fanfics"], 1)


if __name__ == "__main__":
    unittest.main()
