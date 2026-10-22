import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot import app
from src.storage.analytics import UserDownloadSettings
from src.core.limits import run_worker
from tests.test_download_status_flow import IncomingMessage


class ResponseLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_acknowledgement_does_not_drop_link(self):
        message = IncomingMessage()
        message.text = 'https://ficbook.net/readfic/12531381'
        handler = AsyncMock()
        with patch.object(app, '_acknowledge_link', AsyncMock(return_value=None)) as acknowledge:
            data = {}
            await app.RegisterUserMiddleware()(handler, message, data)
            handler.assert_awaited_once()
            with patch.object(app, '_get_user_download_settings', AsyncMock(return_value=UserDownloadSettings())), \
                    patch.object(app, '_process_download', AsyncMock()) as download:
                await app.on_link(message, object(), None, None, object(), object(), frozenset(), **data)
            download.assert_awaited_once()
            acknowledge.assert_awaited_once()

    async def test_heavy_download_does_not_occupy_fast_service_executor(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered, release = Event(), Event()

        def slow_download():
            entered.set()
            return release.wait(3)

        job = asyncio.create_task(run_worker(slow_download))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual(await asyncio.wait_for(asyncio.to_thread(lambda: 'fast'), 0.5), 'fast')
        finally:
            release.set()
            await job

    async def test_link_is_acknowledged_before_blocked_registration(self):
        entered, release = Event(), Event()
        message = IncomingMessage()
        message.text = 'https://ficbook.net/readfic/12531381'

        def register(user_id):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Registration was not released')

        handler = AsyncMock(return_value='done')
        data = {'user_store': Mock(add=register)}
        task = asyncio.create_task(app.RegisterUserMiddleware()(handler, message, data))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual(len(message.statuses), 1)
            self.assertIn('Ссылка получена', message.statuses[0].text)
            handler.assert_not_awaited()
        finally:
            release.set()
            await task
        self.assertIs(data['initial_status'], message.statuses[0])

    async def test_direct_handler_acknowledges_before_loading_settings(self):
        message = IncomingMessage()
        message.text = 'https://ficbook.net/readfic/12531381'

        async def settings(*args):
            self.assertEqual(len(message.statuses), 1)
            return UserDownloadSettings()

        with patch.object(app, '_get_user_download_settings', side_effect=settings), \
                patch.object(app, '_process_download', AsyncMock()) as download:
            await app.on_link(message, object(), None, None, object(), object(), frozenset())
        self.assertIs(download.call_args.kwargs['initial_status'], message.statuses[0])

    async def test_support_link_does_not_get_download_acknowledgement(self):
        message = IncomingMessage()
        message.text = 'https://ficbook.net/readfic/12531381'
        app._support_waiting.add(message.from_user.id)
        try:
            await app.RegisterUserMiddleware()(AsyncMock(), message, {})
        finally:
            app._support_waiting.discard(message.from_user.id)
        self.assertEqual(message.statuses, [])

    async def test_command_with_url_argument_does_not_get_download_acknowledgement(self):
        message = IncomingMessage()
        message.text = '/help https://ficbook.net/readfic/12531381'
        await app.RegisterUserMiddleware()(AsyncMock(), message, {})
        self.assertEqual(message.statuses, [])
