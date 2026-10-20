import asyncio
from dataclasses import replace
from io import BytesIO
from threading import Event
import unittest
from unittest.mock import Mock, patch

from fanficfare.fetchers import RequestsFetcher

from src.bot.app import DownloadProgressState, _build_selected_files
from src.core.download_queue import DownloadQueue
from src.core.limits import ResourceLimitError, ResourceLimits, read_bounded, resource_scope, run_worker
from src.core.models import Chapter, Story
from src.sources.fanficfare_http import SafeFanficFareFetcher
from src.sources.ao3_http import AO3RequestsFetcher
from src.sources.ficbook import FicbookAccount, FicbookClient
from src.sources.safe_http import DeadlineHTTPResponse


class HTTPDeadlineTests(unittest.TestCase):
    def test_complete_response_cancels_absolute_timeout(self):
        sock = Mock()
        sock.makefile.return_value = BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody')
        with patch('src.sources.safe_http.Timer') as timer:
            response = DeadlineHTTPResponse(sock)
            response.begin()
            self.assertEqual(read_bounded(response), b'body')
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called()
        sock.shutdown.assert_not_called()

    def test_timeout_closes_slow_socket_and_is_not_treated_as_a_complete_body(self):
        sock = Mock()
        sock.makefile.return_value = BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody')
        with patch('src.sources.safe_http.Timer'):
            response = DeadlineHTTPResponse(sock)
            response.begin()
            response._expire()
            with self.assertRaises(ResourceLimitError):
                read_bounded(response)
            response.close()
        sock.shutdown.assert_called_once()

    def test_source_configurations_keep_selected_transport_after_refresh(self):
        client = FicbookClient()
        urls = ('https://ficbook.net/readfic/1', 'https://archiveofourown.org/works/1',
                'https://www.wattpad.com/story/1')
        for url in urls:
            with self.subTest(url=url):
                config = client._build_config(url, FicbookAccount())
                fetcher = config.get_fetcher()
                expected = (RequestsFetcher if 'ficbook.net' in url else
                            AO3RequestsFetcher if 'archiveofourown.org' in url else SafeFanficFareFetcher)
                self.assertIs(type(fetcher), expected)
                jar = fetcher.get_cookiejar()
                refreshed = config.get_fetcher(make_new=True)
                self.assertIs(type(refreshed), expected)
                self.assertIs(refreshed.get_cookiejar(), jar)


class RuntimeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_building_multiple_formats_does_not_recount_source_chapters(self):
        story = Story('Book', 'Author', 'https://ficbook.net/readfic/1', chapters=[Chapter('One', '<p>Body</p>')])
        with resource_scope(replace(ResourceLimits(), max_chapters=1)):
            files = await _build_selected_files(story, ('fb2', 'epub', 'docx', 'pdf'), DownloadProgressState())
        self.assertEqual([fmt for fmt, _ in files], ['fb2', 'epub', 'docx', 'pdf'])
        self.assertTrue(all(payload for _, payload in files))

    async def test_cancellation_keeps_queue_slot_until_worker_really_stops(self):
        release = Event()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        queue = DownloadQueue()

        def work():
            loop.call_soon_threadsafe(started.set)
            release.wait(2)

        task = asyncio.create_task(queue.run(lambda: run_worker(work), user_id=1))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertEqual(await queue.total_jobs(), 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(await queue.total_jobs(), 0)

    async def test_slow_initial_notification_does_not_block_other_ready_users(self):
        queue = DownloadQueue()
        release = asyncio.Event()
        waiting = asyncio.Event()

        async def notification(_):
            waiting.set()
            await release.wait()

        first = asyncio.create_task(queue.run(lambda: asyncio.sleep(0), on_queued=notification, user_id=1))
        try:
            await waiting.wait()
            self.assertEqual(await asyncio.wait_for(
                queue.run(lambda: asyncio.sleep(0, result='other'), user_id=2), 1,
            ), 'other')
        finally:
            release.set()
            await first

    async def test_large_file_is_not_returned_for_sending(self):
        story = Story('Book', 'Author', 'https://ficbook.net/readfic/1', chapters=[Chapter('One', 'a')])
        with resource_scope(replace(ResourceLimits(), max_file_bytes=5)), patch(
            'src.bot.app.build_txt', return_value=b'123456',
        ), self.assertRaises(ResourceLimitError):
            await _build_selected_files(story, ('txt',), DownloadProgressState())

    async def test_idle_queue_drops_user_history(self):
        queue = DownloadQueue()
        for user in range(25):
            await queue.run(lambda: asyncio.sleep(0), user_id=user)
        self.assertEqual(queue._scheduler.served, set())
        self.assertEqual(queue._scheduler.round_users, set())
        self.assertEqual(queue._scheduler.active, {})


if __name__ == '__main__':
    unittest.main()
