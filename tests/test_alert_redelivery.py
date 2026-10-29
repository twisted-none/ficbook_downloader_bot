import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from src.bot.alert_redelivery import AlertRedelivery, RETRY_NOTICE, poll_bots, retry_keyboard
from src.storage.alert_retries import AlertRetry


def callback(*, user_id=9, chat_id=9, message_id=12, token='test_token'):
    return CallbackQuery(id='query', from_user=User(id=user_id, is_bot=False, first_name='Admin'),
        chat_instance='instance', data='alert:retry:' + token,
        message=Message(message_id=message_id, date=datetime.now(timezone.utc),
                        chat=Chat(id=chat_id, type='private'), text='Error'))


class AlertRedeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_handled_failure_and_success_are_logged_with_original_url(self):
        async def failed(message, request, delivery):
            delivery.failure = 'Взахлёб перенаправил запрос на другую страницу.'
            return False
        self.download.side_effect = failed
        with self.assertLogs('src.bot.alert_redelivery', level='INFO') as output:
            await self.call()
        text = '\n'.join(output.output)
        self.assertIn('alert_retry_started', text)
        self.assertIn('alert_retry_failed', text)
        self.assertIn(self.retry.url, text)
        self.assertIn('Взахлёб перенаправил', text)
        self.assertNotIn('test_token', text)
        self.claim.request = replace(self.retry, delivered=True)
        with self.assertLogs('src.bot.alert_redelivery', level='INFO') as output:
            await self.call()
        self.assertIn('alert_retry_succeeded', '\n'.join(output.output))

    async def test_retry_preserves_telegram_identity(self):
        self.claim.request = replace(self.retry, username='reader', full_name='Имя Читателя')
        await self.call()
        target = self.download.await_args.args[0]
        self.assertEqual(target.from_user.username, 'reader')
        self.assertEqual(target.from_user.full_name, 'Имя Читателя')

    async def asyncSetUp(self):
        self.bot = Bot('123456:fake_test_token')
        self.addAsyncCleanup(self.bot.session.close)
        self.retry = AlertRetry('test_token', 101, 102, 'https://ficbook.net/readfic/123',
                                ('epub', 'fb2'), False, (2, 4), (), False)
        self.claim = SimpleNamespace(request=self.retry, record_sent=Mock(), close=Mock())
        self.store = SimpleNamespace(claim=Mock(return_value=self.claim))
        self.download = AsyncMock(return_value=True)
        self.service = AlertRedelivery(self.bot, 9, self.store, self.download)
        self.answer, self.delete, self.edit, self.notice = (AsyncMock() for _ in range(4))
        async def api(method, **kwargs):
            handler = {AnswerCallbackQuery: self.answer, DeleteMessage: self.delete,
                       EditMessageReplyMarkup: self.edit, EditMessageText: self.edit, SendMessage: self.notice}[type(method)]
            return await handler(**method.model_dump(exclude_none=True))
        self.enterContext(patch.object(Bot, '__call__', new=AsyncMock(side_effect=api)))
        from src.bot.telegram_delivery import TelegramDeliveryGate
        self.enterContext(patch('src.bot.telegram_delivery.telegram_delivery',
                               TelegramDeliveryGate(chat_interval=0, bot_interval=0)))

    async def call(self, **kwargs):
        query = callback(**kwargs).as_(self.bot)
        query.message.as_(self.bot)
        await self.service.handle(query)

    async def test_success_uses_original_recipient_and_deletes_alert_after_download(self):
        async def download(message, request, delivery):
            self.assertIs(message.bot, self.bot)
            self.assertEqual(message.from_user.id, 101)
            self.assertEqual(message.chat.id, 102)
            self.assertEqual(request.formats, ('epub', 'fb2'))
            self.assertEqual(request.chapter_numbers, (2, 4))
            self.assertFalse(request.cover_enabled)
            self.delete.assert_not_awaited()
            self.assertEqual(delivery.notice, RETRY_NOTICE)
            self.assertIn('Повторная отправка выполняется', self.edit.await_args.kwargs['text'])
            self.assertIn('Error', self.edit.await_args.kwargs['text'])
            self.notice.assert_not_awaited()
            self.claim.request = replace(request, delivered=True)
            return True
        self.download.side_effect = download
        await self.call()
        self.download.assert_awaited_once()
        self.delete.assert_awaited_once()
        self.claim.close.assert_called_once()

    async def test_non_admin_and_wrong_chat_cannot_start_download(self):
        await self.call(user_id=8)
        await self.call(chat_id=8)
        self.store.claim.assert_not_called()
        self.download.assert_not_awaited()

    async def test_expired_or_wrong_message_cannot_start_download(self):
        self.store.claim.return_value = None
        await self.call(message_id=500)
        self.download.assert_not_awaited()
        self.delete.assert_not_awaited()

    async def test_double_click_does_not_duplicate_download(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def download(*args):
            started.set()
            await release.wait()
            return False
        self.download.side_effect = download
        first = asyncio.create_task(self.call())
        await started.wait()
        try:
            await self.call()
            self.download.assert_awaited_once()
        finally:
            release.set()
            await first

    async def test_failed_download_keeps_alert_and_restores_button(self):
        self.download.return_value = False
        await self.call()
        self.delete.assert_not_awaited()
        self.assertEqual(self.edit.await_args.kwargs['reply_markup'],
                         retry_keyboard('test_token').model_dump(exclude_none=True))
        self.claim.close.assert_called_once()
        self.notice.assert_not_awaited()
        self.assertIn('Не удалось отправить', self.edit.await_args.kwargs['text'])

    async def test_already_delivered_only_retries_alert_deletion(self):
        self.claim.request = replace(self.retry, delivered=True)
        await self.call()
        self.download.assert_not_awaited()
        self.delete.assert_awaited_once()

    async def test_download_exception_keeps_retry_available(self):
        self.download.side_effect = RuntimeError('download failed')
        with self.assertLogs('src.bot.alert_redelivery', level='WARNING'):
            await self.call()
        self.delete.assert_not_awaited()
        self.claim.close.assert_called_once()

    async def test_malformed_callback_is_rejected(self):
        await self.call(token='x' * 200)
        self.store.claim.assert_not_called()

    async def test_failure_reason_is_in_original_alert_and_html_escaped(self):
        async def download(message, request, delivery):
            delivery.failure = '<source> & unavailable'
            return False
        self.download.side_effect = download
        await self.call()
        self.assertIn('&lt;source&gt; &amp; unavailable', self.edit.await_args.kwargs['text'])
        self.assertEqual(self.edit.await_args.kwargs['message_id'], 12)
        self.assertEqual(self.edit.await_args.kwargs['chat_id'], 9)
        self.notice.assert_not_awaited()

    async def test_status_replaces_old_footer_and_preserves_original_formatting(self):
        from aiogram.types import MessageEntity
        from src.bot.alert_redelivery import STATUS_HEADER
        query = callback().as_(self.bot)
        updated = query.message.model_copy(update={
            'text': 'Error' + STATUS_HEADER + 'Previous failure',
            'entities': [MessageEntity(type='bold', offset=0, length=5)],
        }).as_(self.bot)
        query = query.model_copy(update={'message': updated}).as_(self.bot)
        self.download.return_value = False
        await self.service.handle(query)
        body = self.edit.await_args.kwargs['text']
        self.assertIn('<b>Error</b>', body)
        self.assertNotIn('Previous failure', body)
        self.assertEqual(body.count('<b>Повторная отправка</b>'), 1)

    async def test_delete_failure_never_repeats_successful_delivery(self):
        self.claim.request = replace(self.retry, delivered=True)
        self.delete.side_effect = RuntimeError('Telegram unavailable')
        with self.assertLogs('src.bot.alert_redelivery', level='WARNING'):
            await self.call()
            await self.call()
        self.download.assert_not_awaited()
        self.assertEqual(self.delete.await_count, 2)

    async def test_cancellation_releases_claim_and_active_slot(self):
        started = asyncio.Event()
        async def download(*args):
            started.set()
            await asyncio.Event().wait()
        self.download.side_effect = download
        task = asyncio.create_task(self.call())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.claim.close.assert_called_once()
        self.assertFalse(self.service.active)

    async def test_alert_dispatcher_consumes_callback_updates_only(self):
        dispatcher = self.service.dispatcher()
        self.assertEqual(dispatcher.resolve_used_update_types(), ['callback_query'])
        self.assertEqual(dispatcher.callback_query.handlers[0].callback, self.service.handle)

    async def test_close_waits_for_active_handler_cleanup(self):
        started = asyncio.Event()
        async def download(*args):
            started.set()
            await asyncio.Event().wait()
        self.download.side_effect = download
        task = asyncio.create_task(self.call())
        await started.wait()
        await self.service.close()
        self.assertTrue(task.cancelled())
        self.claim.close.assert_called_once()
        self.assertFalse(self.service.tasks)

    async def test_cleanup_failure_does_not_escape_successful_handler(self):
        self.claim.request = replace(self.retry, delivered=True)
        self.claim.close.side_effect = RuntimeError('Connection lost')
        with self.assertLogs('src.bot.alert_redelivery', level='WARNING'):
            await self.call()
        self.delete.assert_awaited_once()
        self.download.assert_not_awaited()
        self.assertFalse(self.service.active)

    async def test_cancellation_during_database_claim_does_not_leak_lock(self):
        from threading import Event
        entered, release = Event(), Event()
        def claim(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Claim not released')
            return self.claim
        self.store.claim.side_effect = claim
        task = asyncio.create_task(self.call())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.claim.close.assert_called_once()
        self.assertFalse(self.service.active)


class RedeliveryDownloadFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.bot import app
        from src.bot.telegram_delivery import TelegramDeliveryGate
        from tests.test_download_status_flow import NoopAction
        self.app = app
        for name, value in (
            ('_start_download', AsyncMock(return_value=1)),
            ('_finish_download', AsyncMock()),
            ('_discard_download', AsyncMock()),
            ('_notify_admin', AsyncMock()),
            ('_build_selected_files', AsyncMock(return_value=[('epub', b'epub'), ('fb2', b'fb2')])),
            ('_telegram_edit_gate', app.TelegramEditGate(min_interval=0)),
        ):
            self.enterContext(patch.object(app, name, value))
        self.enterContext(patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()))
        self.enterContext(patch('src.bot.telegram_delivery.telegram_delivery',
                               TelegramDeliveryGate(chat_interval=0, bot_interval=0)))

    async def run_download(self, *, sent=(), fail_second=False, source_error=None):
        from src.bot.alert_redelivery import Redelivery
        from src.bot.telegram_delivery import TelegramDeliveryError
        from src.core.download_queue import DownloadQueuePool
        from src.core.models import Story
        from tests.test_download_status_flow import IncomingMessage
        message = IncomingMessage()
        send = message.answer_document
        async def send_document(*args, **kwargs):
            if fail_second and len(message.documents) == 1:
                raise TelegramDeliveryError('Failed second format')
            await send(*args, **kwargs)
        message.answer_document = send_document
        client = SimpleNamespace(download=Mock(return_value=Story('Book', 'Author', 'https://ficbook.net/readfic/123')))
        if source_error is not None:
            client.download.side_effect = source_error
        receipt = AsyncMock()
        result = await self.app._process_download(message, client, None, None, object(), DownloadQueuePool(),
            'https://ficbook.net/readfic/123', ('epub', 'fb2'), frozenset(),
            cover_enabled=False, chapter_numbers={2, 4}, redelivery=Redelivery(sent, receipt))
        return result, message, client, receipt

    async def test_standard_download_path_and_final_caption(self):
        result, message, client, receipt = await self.run_download()
        self.assertTrue(result)
        client.download.assert_called_once()
        self.assertEqual(client.download.call_args.kwargs['chapter_numbers'], {2, 4})
        self.assertEqual(message.documents[-1]['caption'], RETRY_NOTICE)
        self.assertNotEqual(message.documents[0]['caption'], RETRY_NOTICE)
        self.assertEqual([call.args[0] for call in receipt.await_args_list], ['epub', 'fb2'])
        self.app._notify_admin.assert_not_awaited()

    async def test_resume_skips_already_sent_formats(self):
        result, message, _, receipt = await self.run_download(sent=('epub',))
        self.assertTrue(result)
        self.assertEqual(len(message.documents), 1)
        self.assertEqual(message.documents[0]['caption'], RETRY_NOTICE)
        receipt.assert_awaited_once_with('fb2')

    async def test_partial_failure_only_records_confirmed_files(self):
        with self.assertLogs('src.bot.app', level='WARNING'):
            result, message, _, receipt = await self.run_download(fail_second=True)
        self.assertFalse(result)
        self.assertEqual(len(message.documents), 1)
        receipt.assert_awaited_once_with('epub')
        self.app._notify_admin.assert_not_awaited()
        self.assertEqual(message.statuses, [])

    async def test_successful_admin_download_sends_no_progress_messages(self):
        result, message, _, _ = await self.run_download()
        self.assertTrue(result)
        self.assertEqual(message.statuses, [])
        self.app.ChatActionSender.upload_document.assert_not_called()

    async def test_source_failures_never_send_user_error_messages(self):
        from src.sources.ficbook import FicbookError, FicbookNotFoundError, FicbookPaidContentError
        from src.core.limits import ResourceLimitError
        from src.core.download_queue import QueueFullError
        for error in (FicbookError('Temporary source error'), FicbookNotFoundError('Missing'),
                      FicbookPaidContentError('Paid'), ResourceLimitError('Too big'), QueueFullError('Queue full')):
            with self.subTest(error=type(error).__name__):
                result, message, _, receipt = await self.run_download(source_error=error)
                self.assertFalse(result)
                self.assertEqual(message.statuses, [])
                self.assertEqual(message.documents, [])
                receipt.assert_not_awaited()
        self.app._notify_admin.assert_not_awaited()

    async def test_full_registry_does_not_notify_user(self):
        from src.bot.alert_redelivery import Redelivery
        from src.core.download_queue import DownloadQueuePool
        from tests.test_download_status_flow import IncomingMessage
        message, delivery = IncomingMessage(), Redelivery((), AsyncMock())
        with patch.object(self.app._jobs, 'reserve', side_effect=OverflowError):
            result = await self.app._process_download(message, object(), None, None, object(), DownloadQueuePool(),
                'https://ficbook.net/readfic/123', ('epub',), frozenset(), redelivery=delivery)
        self.assertIsNone(result)
        self.assertEqual(message.statuses, [])
        self.assertIn('заполнена', delivery.failure)

    async def test_duplicate_job_does_not_notify_user(self):
        from src.bot.alert_redelivery import Redelivery
        from src.core.download_queue import DownloadQueuePool
        from tests.test_download_status_flow import IncomingMessage
        message, delivery = IncomingMessage(), Redelivery((), AsyncMock())
        with patch.object(self.app._jobs, 'reserve', return_value=(SimpleNamespace(text='Busy', task=None), False)):
            result = await self.app._process_download(message, object(), None, None, object(), DownloadQueuePool(),
                'https://ficbook.net/readfic/123', ('epub',), frozenset(), redelivery=delivery)
        self.assertIsNone(result)
        self.assertEqual(message.statuses, [])
        self.assertIn('уже скачивается', delivery.failure)


class AlertSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_alert_stores_settings_and_binds_button_to_sent_message(self):
        from src.bot.app import _notify_admin
        sender = SimpleNamespace(id=123456, send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
        store = SimpleNamespace(create=Mock(return_value='test_token'), bind_message=Mock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=101, username='user', full_name='User'),
                                  chat=SimpleNamespace(id=102), bot=object())
        await _notify_admin(message, 9, sender, 'https://ficbook.net/readfic/123?token=secret',
                            RuntimeError('Error'), expected=False, retry_store=store,
                            formats=('epub', 'fb2'), cover_enabled=False, chapter_numbers={2, 4})
        self.assertEqual(store.create.call_args.kwargs['url'], 'https://ficbook.net/readfic/123')
        self.assertEqual(store.create.call_args.kwargs['user_id'], 101)
        self.assertEqual(store.create.call_args.kwargs['username'], 'user')
        self.assertEqual(store.create.call_args.kwargs['full_name'], 'User')
        self.assertIn('<b>User:</b> @user', sender.send_message.await_args.args[1])
        self.assertEqual(retry_keyboard('test_token').inline_keyboard[0][0].text,
                         'Отправить пользователю еще раз')
        self.assertEqual(store.create.call_args.kwargs['chapter_numbers'], {2, 4})
        self.assertFalse(store.create.call_args.kwargs['cover_enabled'])
        store.bind_message.assert_called_once_with('test_token', 12)
        self.assertEqual(sender.send_message.await_args.kwargs['reply_markup'], retry_keyboard('test_token'))

    async def test_storage_failure_does_not_lose_original_alert(self):
        from src.bot.app import _notify_admin
        sender = SimpleNamespace(id=123456, send_message=AsyncMock())
        store = SimpleNamespace(create=Mock(side_effect=RuntimeError('Database offline')))
        message = SimpleNamespace(from_user=SimpleNamespace(id=101, username='user', full_name='User'),
                                  chat=SimpleNamespace(id=102), bot=object())
        with self.assertLogs('src.bot.app', level='WARNING'):
            await _notify_admin(message, 9, sender, 'https://ficbook.net/readfic/123',
                                RuntimeError('Error'), expected=False, retry_store=store, formats=('epub',))
        sender.send_message.assert_awaited_once()
        self.assertNotIn('reply_markup', sender.send_message.await_args.kwargs)
        self.assertIn('не удалось сохранить задание', sender.send_message.await_args.args[1])


class DualPollingTests(unittest.IsolatedAsyncioTestCase):
    def poller(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def start(*args, **kwargs):
            started.set()
            await stopped.wait()
        poller = SimpleNamespace(start_polling=AsyncMock(side_effect=start),
                                 stop_polling=AsyncMock(side_effect=stopped.set))
        return poller, started, stopped

    async def test_both_bots_are_polled_and_other_poller_is_stopped_gracefully(self):
        main, main_started, main_stop = self.poller()
        alert, alert_started, _ = self.poller()
        service = SimpleNamespace(dispatcher=Mock(return_value=alert))
        task = asyncio.create_task(poll_bots(main, SimpleNamespace(id=1), service,
                                            SimpleNamespace(id=2), tasks_concurrency_limit=500))
        await asyncio.wait_for(asyncio.gather(main_started.wait(), alert_started.wait()), 2)
        main_stop.set()
        await asyncio.wait_for(task, 2)
        alert.stop_polling.assert_awaited_once()
        self.assertFalse(alert.start_polling.await_args.kwargs['handle_signals'])
        self.assertEqual(alert.start_polling.await_args.kwargs['allowed_updates'], ['callback_query'])

    async def test_same_bot_token_is_never_polled_twice(self):
        main, started, stop = self.poller()
        service = SimpleNamespace(dispatcher=Mock())
        task = asyncio.create_task(poll_bots(main, SimpleNamespace(id=1), service,
                                            SimpleNamespace(id=1), tasks_concurrency_limit=500))
        await asyncio.wait_for(started.wait(), 2)
        stop.set()
        await asyncio.wait_for(task, 2)
        service.dispatcher.assert_not_called()

    async def test_cancellation_stops_both_pollers(self):
        main, main_started, _ = self.poller()
        alert, alert_started, _ = self.poller()
        service = SimpleNamespace(dispatcher=Mock(return_value=alert))
        task = asyncio.create_task(poll_bots(main, SimpleNamespace(id=1), service,
                                            SimpleNamespace(id=2), tasks_concurrency_limit=500))
        await asyncio.wait_for(asyncio.gather(main_started.wait(), alert_started.wait()), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        main.stop_polling.assert_awaited_once()
        alert.stop_polling.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
