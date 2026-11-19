import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot import app
from src.core.download_queue import DownloadQueuePool
from src.core.models import Story
from src.sources.ao3_status import AO3RateLimitError
from src.sources.ficbook import FicbookClient, FicbookRateLimitError


URL = 'https://archiveofourown.org/works/95108896'


class AO3RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_http_adapter_keeps_retry_after(self):
        from src.sources.ao3_http import AO3Adapter
        response = SimpleNamespace(
            is_redirect=False, is_permanent_redirect=False, status_code=429,
            raw=Mock(headers={}), headers={'Retry-After': '720'}, close=Mock(),
        )
        with patch('requests.adapters.HTTPAdapter.send', return_value=response), \
                patch('src.sources.ao3_http.read_bounded', return_value=b'Limited'), \
                self.assertRaises(AO3RateLimitError) as caught:
            AO3Adapter().send(Mock())
        self.assertEqual(caught.exception.retry_after, 720)
        response.close.assert_called_once()

    def test_browser_server_forwards_rate_limit(self):
        import json
        from browser.server import Handler
        handler = object.__new__(Handler)
        handler.path = '/works/95108896'
        handler.respond = Mock()
        payload = json.dumps({'work_id': '95108896', 'rate_limited': True, 'retry_after': '720'}).encode()
        with patch('browser.server.download', return_value=payload):
            handler.do_GET()
        handler.respond.assert_called_once_with(429, payload)

    def test_retry_after_seconds_date_and_invalid_value(self):
        self.assertEqual(AO3RateLimitError({'Retry-After': '720'}).retry_after, 720)
        later = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10))
        self.assertGreater(AO3RateLimitError({'Retry-After': later}).retry_after, 590)
        self.assertIsNone(AO3RateLimitError({'Retry-After': 'invalid'}).retry_after)

    def test_download_and_preview_preserve_rate_limit(self):
        for method, internal in (('download', '_download_once'), ('preview', '_preview_once')):
            client = FicbookClient()
            error = FicbookRateLimitError('Limited', retry_after=720)
            with self.subTest(method=method), patch.object(client, internal, side_effect=error), \
                    self.assertRaises(FicbookRateLimitError) as caught:
                getattr(client, method)(URL)
            self.assertIs(caught.exception, error)

    async def test_shared_pause_retries_one_request_before_next_job(self):
        queues = DownloadQueuePool()
        progress = SimpleNamespace(start_phase=AsyncMock())
        order = []
        entered = asyncio.Event()
        release = asyncio.Event()
        clock = [0.0]

        async def sleep(delay):
            self.assertEqual(delay, 720)
            entered.set()
            await release.wait()
            clock[0] += delay

        async def first():
            order.append('first')
            if len(order) == 1:
                raise FicbookRateLimitError('Limited', retry_after=720)
            return 'one'

        async def second():
            order.append('second')
            return 'two'

        # Patch the module reference, keeping asyncio's own scheduling untouched.
        fake_asyncio = SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for)
        with patch.object(app, 'monotonic', side_effect=lambda: clock[0]), patch.object(app, 'asyncio', fake_asyncio):
            task = asyncio.create_task(app._run_ao3_request(queues, first, progress, ('epub',)))
            other = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                other = asyncio.create_task(app._run_ao3_request(queues, second, progress, ('fb2',)))
                await asyncio.sleep(0)
                self.assertEqual(order, ['first'])
                release.set()
                self.assertEqual(await asyncio.wait_for(asyncio.gather(task, other), 2), ['one', 'two'])
                self.assertEqual(order, ['first', 'first', 'second'])
                self.assertEqual(queues.ao3_retry_at, 0)
            finally:
                for pending in (task, other):
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.gather(*(t for t in (task, other) if t is not None), return_exceptions=True)

    async def test_default_pause_and_cancellation_keep_deadline_and_release_lock(self):
        queues = DownloadQueuePool()
        entered = asyncio.Event()
        async def sleep(delay):
            self.assertGreater(delay, 299)
            entered.set()
            await asyncio.Event().wait()
        with patch.object(app, 'asyncio', SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for)):
            task = asyncio.create_task(app._run_ao3_request(
                queues, AsyncMock(side_effect=FicbookRateLimitError('Limited')),
                SimpleNamespace(start_phase=AsyncMock()), ('epub',),
            ))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(queues.ao3_request_lock.locked())
        self.assertGreater(queues.ao3_retry_at, 0)

    async def test_download_waits_and_delivers_selected_formats_without_alert(self):
        from tests.test_download_status_flow import IncomingMessage, NoopAction
        queues = DownloadQueuePool()
        client = SimpleNamespace(download=Mock(side_effect=[
            FicbookRateLimitError('Limited', retry_after=1), Story('Book', 'Writer', URL),
        ]))
        message = IncomingMessage()
        with patch.object(app, '_start_download', AsyncMock()), \
                patch.object(app, '_finish_download', AsyncMock()), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('epub', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()):
            await app._process_download(message, client, None, None, object(), queues,
                                        URL, ('epub',), frozenset(), chapter_numbers={2})
        self.assertEqual(client.download.call_count, 2)
        self.assertEqual(client.download.call_args.kwargs['chapter_numbers'], {2})
        self.assertEqual(len(message.documents), 1)
        notify.assert_not_awaited()
