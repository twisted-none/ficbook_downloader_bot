import asyncio
from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import history_retention_days
from src.storage import retention
from src.storage.analytics import RetentionPreview, _public_story_url


class RetentionConfigurationTests(unittest.TestCase):
    def test_default_is_thirty_days(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(history_retention_days(), 30)

    def test_invalid_or_shorter_retention_is_rejected(self):
        for value in ("0", "-1", "1", "29", "abc"):
            with self.subTest(value=value), patch.dict(os.environ, {"HISTORY_RETENTION_DAYS": value}, clear=True):
                with self.assertRaises(ValueError):
                    history_retention_days()

    def test_public_urls_keep_work_identity_and_drop_private_query_fields(self):
        cases = {
            "https://ficbook.net/readfic/123?token=secret#private": "https://ficbook.net/readfic/123",
            "https://archiveofourown.org/works/123?view_adult=true&key=secret": "https://archiveofourown.org/works/123",
            "https://www.wattpad.com/story/123-book?user=private": "https://www.wattpad.com/story/123-book",
            "http://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123&password=secret":
                "https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123",
            "https://litnet.com/ru/book/name-b123?key=secret": "https://litnet.com/ru/reader/name-b123",
            "https://ranobelib.me/ru/123--name?token=secret": "https://ranobelib.me/ru/123--name",
            "https://user:secret@ficbook.net/readfic/123": "",
            "https://evil.example/readfic/123?token=secret": "",
            "file:///etc/passwd": "",
            "https://[invalid": "",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(_public_story_url(url), expected)


class RetentionCommandTests(unittest.TestCase):
    def command(self, args, store):
        output = StringIO()
        with patch("sys.argv", ["retention", *args]), patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://unused-test-database"}, clear=True
        ), patch.object(retention, "load_dotenv"), patch.object(
            retention, "AnalyticsStore", return_value=store
        ) as constructor, redirect_stdout(output):
            retention.main()
        constructor.assert_called_once_with("postgresql://unused-test-database", history_retention_days=30,
                                            initialize=False)
        return output.getvalue()

    def test_existing_data_is_previewed_but_not_changed_without_apply(self):
        store = Mock()
        store.retention_preview.return_value = RetentionPreview(7, 2)
        output = self.command(["--enroll-existing"], store)
        store.retention_preview.assert_called_once_with(include_legacy=True)
        store.enroll_legacy_history.assert_not_called()
        store.cleanup_expired_history.assert_not_called()
        self.assertIn("Preview only", output)

    def test_initial_enrollment_requires_both_explicit_flags(self):
        store = Mock()
        store.retention_preview.return_value = RetentionPreview(7, 2)
        store.enroll_legacy_history.return_value = 9
        store.cleanup_expired_history.side_effect = [1000, 7]
        output = self.command(["--enroll-existing", "--apply"], store)
        store.enroll_legacy_history.assert_called_once_with()
        self.assertEqual(store.cleanup_expired_history.call_count, 2)
        self.assertIn("1007", output)

    def test_apply_without_enrollment_does_not_enroll_old_history(self):
        store = Mock()
        store.retention_preview.return_value = RetentionPreview(0, 0)
        store.cleanup_expired_history.return_value = 0
        self.command(["--apply"], store)
        store.enroll_legacy_history.assert_not_called()


class RetentionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_processes_batches_and_never_enrolls_old_data(self):
        store = Mock(cleanup_expired_history=Mock(side_effect=[1000, 7]))

        async def sleep(delay):
            if delay:
                raise asyncio.CancelledError

        with patch.object(retention.asyncio, "sleep", new=AsyncMock(side_effect=sleep)) as sleeper:
            with self.assertRaises(asyncio.CancelledError):
                await retention.run_history_retention(store)
        self.assertEqual(store.cleanup_expired_history.call_count, 2)
        store.enroll_legacy_history.assert_not_called()
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [0, 86400.0])

    async def test_scheduler_retries_after_error_without_logging_private_data(self):
        store = Mock(cleanup_expired_history=Mock(side_effect=RuntimeError("password=private")))
        with patch.object(retention.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)), self.assertLogs(
            retention.logger, level="WARNING"
        ) as logs:
            with self.assertRaises(asyncio.CancelledError):
                await retention.run_history_retention(store)
        self.assertNotIn("password=private", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
