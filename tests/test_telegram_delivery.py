import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from src.bot.telegram_delivery import DeliveryDeferred, TelegramDeliveryGate, TelegramDeliveryError
from src.bot import app
from src.core.download_queue import DownloadQueuePool
from src.storage.analytics import UserDownloadSettings
from tests.test_download_status_flow import IncomingMessage, FastClient, NoopAction


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_400_links_survive_status_flood_and_download_once_each(self):
        loop = asyncio.get_running_loop()
        was_debug = loop.get_debug()
        loop.set_debug(False)
        self.addCleanup(loop.set_debug, was_debug)
        now = [100.0]

        async def sleep(delay):
            now[0] += delay

        gate = TelegramDeliveryGate(clock=lambda: now[0], sleep=sleep)
        bot = object()
        status_send = AsyncMock(side_effect=TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='status'), message='limited', retry_after=81,
        ))
        messages = [IncomingMessage() for _ in range(400)]
        for index, message in enumerate(messages):
            message.bot = bot
            message.text = f'https://archiveofourown.org/works/{index + 1}'
            message.answer = status_send
        client = FastClient()
        client.download = Mock(wraps=client.download)
        queues = DownloadQueuePool()

        async def handle(message):
            async def handler(event, data):
                await app.on_link(event, client, 999, None, object(), queues, frozenset(), **data)
            await app.RegisterUserMiddleware()(handler, message, {})

        with patch('src.bot.telegram_delivery.telegram_delivery', gate), \
                patch.object(app, '_get_user_download_settings', AsyncMock(return_value=UserDownloadSettings(formats=('txt',)))), \
                patch.object(app, '_start_download', AsyncMock(return_value=None)), \
                patch.object(app, '_finish_download', AsyncMock()) as finish, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify:
            await asyncio.gather(*(handle(message) for message in messages))
        self.assertEqual(client.download.call_count, 400)
        self.assertEqual(sum(len(message.documents) for message in messages), 400)
        self.assertEqual(finish.await_count, 400)
        self.assertTrue(all(call.kwargs['success'] for call in finish.call_args_list))
        notify.assert_not_awaited()
        self.assertLess(status_send.await_count, 10)

    async def test_document_retry_after_retries_file_without_source_download(self):
        now = [100.0]

        async def sleep(delay):
            now[0] += delay

        gate = TelegramDeliveryGate(clock=lambda: now[0], sleep=sleep)
        message = IncomingMessage()
        message.answer_document = AsyncMock(side_effect=[TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='file'), message='limited', retry_after=81,
        ), None])
        with patch('src.bot.telegram_delivery.telegram_delivery', gate):
            await app._answer_document_with_retry(message, b'payload', filename='test.txt',
                                                   caption='test', reply_markup=None)
        self.assertEqual(message.answer_document.await_count, 2)
        files = [call.args[0] for call in message.answer_document.call_args_list]
        self.assertTrue(all(file.data == b'payload' for file in files))
        self.assertGreaterEqual(now[0], 182)

    async def test_delivery_failure_is_not_reported_as_ficbook_error(self):
        message = IncomingMessage()
        client = FastClient()
        client.download = Mock(wraps=client.download)
        with patch.object(app, '_start_download', AsyncMock(return_value=None)), \
                patch.object(app, '_finish_download', AsyncMock()) as finish, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app, '_answer_document_with_retry', AsyncMock(side_effect=TelegramDeliveryError('timeout'))), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify:
            result = await app._process_download(message, client, 999, None, object(), DownloadQueuePool(),
                                                  'https://archiveofourown.org/works/1', ('txt',), frozenset())
        self.assertFalse(result)
        client.download.assert_called_once()
        self.assertFalse(finish.call_args.kwargs['success'])
        notify.assert_not_awaited()

    async def test_400_optional_statuses_do_not_queue_400_requests(self):
        gate = TelegramDeliveryGate()
        send = AsyncMock(return_value='sent')

        async def attempt():
            try:
                return await gate.call(1, 2, send, optional=True)
            except DeliveryDeferred:
                return None

        results = await asyncio.gather(*(attempt() for _ in range(400)))
        self.assertEqual(results.count('sent'), 1)
        send.assert_awaited_once()

    async def test_retry_after_is_shared_by_other_statuses_and_file(self):
        now = [100.0]

        async def sleep(delay):
            now[0] += delay

        gate = TelegramDeliveryGate(clock=lambda: now[0], sleep=sleep)
        blocked = AsyncMock(side_effect=TelegramRetryAfter(
            method=SendMessage(chat_id=2, text='status'), message='limited', retry_after=81,
        ))
        with self.assertRaises(TelegramRetryAfter):
            await gate.call(1, 2, blocked, optional=True)
        status = AsyncMock()
        with self.assertRaises(DeliveryDeferred):
            await gate.call(1, 2, status, optional=True)
        status.assert_not_awaited()
        send_file = AsyncMock(return_value='file')
        self.assertEqual(await gate.call(1, 2, send_file), 'file')
        self.assertGreaterEqual(now[0], 182)

    async def test_excessive_backoff_has_bounded_wait(self):
        gate = TelegramDeliveryGate(max_wait=0)
        send = AsyncMock()
        await gate.call(1, 2, send)
        with self.assertRaises(TelegramDeliveryError):
            await gate.call(1, 2, send)

    async def test_cancelled_waiter_releases_chat(self):
        gate = TelegramDeliveryGate(chat_interval=60)
        send = AsyncMock()
        await gate.call(1, 2, send)
        waiter = asyncio.create_task(gate.call(1, 2, send))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(gate.tracked_chats, 1)

    async def test_capacity_does_not_evict_an_active_chat_lock(self):
        gate = TelegramDeliveryGate(max_chats=1)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            entered.set()
            await release.wait()

        task = asyncio.create_task(gate.call(1, 2, operation))
        await entered.wait()
        try:
            with self.assertRaises(DeliveryDeferred):
                await gate.call(1, 3, AsyncMock(), optional=True)
            self.assertEqual(gate.tracked_chats, 1)
        finally:
            release.set()
            await task

    async def test_bot_pacing_is_shared_between_chats(self):
        gate = TelegramDeliveryGate(bot_interval=60)
        await gate.call(1, 2, AsyncMock(), optional=True)
        send = AsyncMock()
        with self.assertRaises(DeliveryDeferred):
            await gate.call(1, 3, send, optional=True)
        send.assert_not_awaited()

    async def test_file_success_is_preserved_when_status_cleanup_times_out(self):
        message = IncomingMessage()
        status = await message.answer('queued')
        status.chat = message.chat
        status.bot = message.bot
        status.delete = AsyncMock()
        with patch.object(app, '_start_download', AsyncMock(return_value=None)), \
                patch.object(app, '_finish_download', AsyncMock()) as finish, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app, '_answer_document_with_retry', AsyncMock()), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify, \
                patch.object(app, 'deliver', AsyncMock(side_effect=TelegramDeliveryError('cleanup timeout'))):
            # Isolate cleanup: progress updates are optional and independently tested.
            with patch.object(app.ProgressMessage, 'set', AsyncMock()), \
                    patch.object(app.ProgressMessage, 'set_required', AsyncMock(return_value=False)):
                result = await app._process_download(message, FastClient(), 999, None, object(),
                    DownloadQueuePool(), 'https://archiveofourown.org/works/1', ('txt',), frozenset(),
                    initial_status=status)
        self.assertTrue(result)
        finish.assert_awaited_once()
        self.assertTrue(finish.call_args.kwargs['success'])
        notify.assert_not_awaited()

