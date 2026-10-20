from contextlib import nullcontext
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from src.storage.analytics import AnalyticsStore, UserDownloadSettings


class AnalyticsPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.execute.return_value.fetchone.return_value = {
            'formats': ['fb2'], 'chapter_selection_enabled': False, 'cover_enabled': True,
        }
        self.pool = Mock(closed=True)
        self.pool.connection.side_effect = lambda: nullcontext(self.db)
        self.pool.open.side_effect = lambda: setattr(self.pool, 'closed', False)
        factory = patch('src.storage.analytics.ConnectionPool', return_value=self.pool, create=True)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        connect = patch('src.storage.analytics.psycopg.connect', side_effect=lambda *a, **k: nullcontext(self.db))
        self.direct_connect = connect.start()
        self.addCleanup(connect.stop)
        self.store = AnalyticsStore('postgresql://unused-test-database', initialize=False)
        self.addCleanup(lambda: self.store.close() if hasattr(self.store, 'close') else None)

    def test_connections_are_borrowed_and_returned_to_a_bounded_pool(self):
        self.store.get_user_download_settings(1)
        self.factory.assert_called_once()
        self.assertEqual(self.factory.call_args.kwargs['max_size'], 4)
        self.assertFalse(self.factory.call_args.kwargs['open'])
        self.pool.connection.assert_called_once()
        self.direct_connect.assert_not_called()

    def test_settings_are_cached_and_saves_replace_cached_value(self):
        self.assertEqual(self.store.get_user_download_settings(1).formats, ('fb2',))
        self.assertEqual(self.store.get_user_download_settings(1).formats, ('fb2',))
        self.assertEqual(self.db.execute.call_count, 1)
        changed = UserDownloadSettings(('epub',), True, False)
        self.store.save_user_download_settings(1, changed)
        self.assertEqual(self.store.get_user_download_settings(1), changed)
        self.assertEqual(self.db.execute.call_count, 2)

    def test_settings_cache_expires(self):
        with patch('src.storage.analytics.monotonic', return_value=100, create=True):
            self.store.get_user_download_settings(1)
        with patch('src.storage.analytics.monotonic', return_value=200, create=True):
            self.store.get_user_download_settings(1)
        self.assertEqual(self.db.execute.call_count, 2)

    def test_settings_reads_do_not_wait_for_maintenance_lock(self):
        finished = Event()

        def read():
            self.store.get_user_download_settings(1)
            finished.set()

        self.store.lock.acquire()
        thread = Thread(target=read)
        thread.start()
        try:
            self.assertTrue(finished.wait(0.5))
        finally:
            self.store.lock.release()
            thread.join(timeout=2)

    def test_concurrent_saves_do_not_publish_stale_cache_after_newer_db_write(self):
        entered, release, second_done = Event(), Event(), Event()
        db_value = []
        first = UserDownloadSettings(('fb2',), False, True)
        second = UserDownloadSettings(('epub',), True, False)

        def execute(query, params):
            db_value[:] = [UserDownloadSettings(tuple(params[1]), params[2], params[3])]
            if params[1] == ['fb2']:
                entered.set()
                release.wait(2)
            return Mock()

        self.db.execute.side_effect = execute
        a = Thread(target=self.store.save_user_download_settings, args=(1, first))

        def save_second():
            self.store.save_user_download_settings(1, second)
            second_done.set()

        b = Thread(target=save_second)
        a.start()
        try:
            self.assertTrue(entered.wait(1))
            b.start()
            second_done.wait(0.05)
        finally:
            release.set()
            a.join(timeout=2)
            b.join(timeout=2)
        self.assertEqual(self.store.get_user_download_settings(1), db_value[0])

    def test_failed_save_does_not_replace_cached_settings(self):
        original = self.store.get_user_download_settings(1)
        self.db.execute.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            self.store.save_user_download_settings(1, UserDownloadSettings(('epub',), True, False))
        self.assertEqual(self.store.get_user_download_settings(1), original)

    def test_closed_pool_cannot_be_reopened_by_a_late_task(self):
        self.store.close()
        with self.assertRaises(RuntimeError):
            self.store._connect()
        self.pool.open.assert_not_called()
