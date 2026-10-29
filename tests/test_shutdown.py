import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Dispatcher


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_shutdown_signal_does_not_interrupt_handler_cleanup(self):
        from src.bot.alert_redelivery import poll_bots
        cleaning, release = asyncio.Event(), asyncio.Event()
        async def handler():
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        child = asyncio.create_task(handler())
        await asyncio.sleep(0)
        dispatcher = Dispatcher()
        dispatcher._handle_update_tasks.add(child)
        dispatcher.start_polling = AsyncMock()
        service = SimpleNamespace(dispatcher=Mock())
        task = asyncio.create_task(poll_bots(dispatcher, SimpleNamespace(id=1), service, None,
                                              tasks_concurrency_limit=10))
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(child.done())

    async def test_running_handlers_finish_cleanup_before_pool_database_and_sessions_close(self):
        from src.bot import app
        events = []
        entered = asyncio.Event()
        async def handler():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                await asyncio.to_thread(events.append, 'handler_cleanup')
        task = asyncio.create_task(handler())
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        self.addCleanup(task.cancel)
        await entered.wait()
        dispatcher = Dispatcher()
        dispatcher._handle_update_tasks.add(task)
        dispatcher.start_polling = AsyncMock(side_effect=lambda *args, **kwargs: None)
        dispatcher.stop_polling = AsyncMock()
        bot = SimpleNamespace(id=1, session=SimpleNamespace(close=AsyncMock(side_effect=lambda: events.append('session_close'))))
        analytics = SimpleNamespace(alert_retries=object(), close=Mock(side_effect=lambda: events.append('database_close')))
        settings = SimpleNamespace(bot_token='111111:fake_test_token', alert_bot_token='',
            database_url='unused', history_retention_days=30, data_dir=Path('unused'),
            admin_chat_id=9, premium_queue_user_ids=frozenset(), metrics_host='127.0.0.1', metrics_port=8000,
            litnet_login='', litnet_password='', ranobelib_login='', ranobelib_password='',
            ficbook_request_delay_seconds=0, ficbook_retry_attempts=1, ficbook_retry_base_delay_seconds=0,
            ficbook_retry_max_delay_seconds=0, ficbook_download_interval_seconds=0)
        async def background(*args):
            await asyncio.Event().wait()
        with patch.object(app, 'Bot', return_value=bot), patch.object(app, 'AnalyticsStore', return_value=analytics), \
                patch.object(app, 'FicbookClient', return_value=object()), \
                patch.object(app, 'UserStore', return_value=object()), \
                patch.object(app, '_ficbook_accounts', return_value=()), \
                patch.object(app, '_site_accounts', return_value={}), \
                patch.object(app, 'create_dispatcher', return_value=dispatcher), \
                patch.object(app, '_set_bot_commands', AsyncMock()), \
                patch.object(app, 'run_metrics_server', side_effect=background), \
                patch.object(app, 'run_history_retention', side_effect=background), \
                patch.object(app, '_expire_sessions', side_effect=background), \
                patch.object(app, 'close_download_workers', side_effect=lambda: events.append('pool_close')):
            await app.run_bot(settings)
        self.assertTrue(task.done())
        self.assertEqual(events, ['handler_cleanup', 'pool_close', 'database_close', 'session_close'])

    async def test_preview_failure_creates_retry_with_formats_and_cover_settings(self):
        from src.bot import app
        from src.bot.alert_redelivery import retry_keyboard
        message = SimpleNamespace(from_user=SimpleNamespace(id=101, username='reader', full_name='Reader'),
                                  chat=SimpleNamespace(id=101), bot=object())
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        store = SimpleNamespace(create=Mock(return_value='test_token'), bind_message=Mock())
        sender = SimpleNamespace(id=123456, send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
        client = SimpleNamespace(preview=Mock(side_effect=RuntimeError('cannot schedule new futures after shutdown')))
        with patch.object(app, '_answer_status_with_retry', AsyncMock(return_value=status)), \
                patch.object(app, '_replace_status_with_error', AsyncMock()):
            await app._ask_chapter_selection(message, client, 9, sender, 'https://ficbook.net/readfic/123',
                                            ('epub', 'fb2'), False, retry_store=store)
        self.assertEqual(store.create.call_args.kwargs['formats'], ('epub', 'fb2'))
        self.assertFalse(store.create.call_args.kwargs['cover_enabled'])
        self.assertIsNone(store.create.call_args.kwargs['chapter_numbers'])
        self.assertEqual(sender.send_message.await_args.kwargs['reply_markup'], retry_keyboard('test_token'))
        store.bind_message.assert_called_once_with('test_token', 12)
