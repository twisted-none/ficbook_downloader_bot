import asyncio
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from src.bot import app
from src.bot.jobs import JobRegistry
from src.core.download_queue import DownloadQueuePool
from src.core.models import Story
from src.sources.collections import FicbookCollection
from tests.test_download_status_flow import IncomingMessage, NoopAction


URL = 'https://ficbook.net/readfic/123'
COLLECTION = 'https://ficbook.net/collections/123'
WORKS = tuple(f'https://ficbook.net/readfic/{i}' for i in range(3))


def callback(data, user=1, chat=1):
    message = Message(message_id=10, date=datetime.now(timezone.utc), chat={'id': chat, 'type': 'private'})
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), message=message, answer=AsyncMock())


class DownloadControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.real_start_download = app._start_download
        self.message = IncomingMessage()
        self.client = SimpleNamespace(
            download=Mock(side_effect=lambda url, *args, **kwargs: Story('Book', 'Author', url)),
            collection=Mock(return_value=FicbookCollection('Collection', COLLECTION, WORKS)),
        )
        self.queues = DownloadQueuePool()
        self.deps = dict(ficbook_client=self.client, admin_chat_id=None, alert_bot=None,
                         analytics_store=object(), download_queues=self.queues, premium_queue_user_ids=frozenset())
        for name, value in (
            ('_jobs', JobRegistry()), ('_telegram_edit_gate', app.TelegramEditGate(min_interval=0)),
            ('_start_download', AsyncMock(return_value=10)), ('_discard_download', AsyncMock()),
            ('_finish_download', AsyncMock()), ('_notify_admin', AsyncMock()),
        ):
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)
        app._failed_collections.clear()
        self.addCleanup(app._failed_collections.clear)
        p = patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction())
        p.start()
        self.addCleanup(p.stop)

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    async def download(self, **kwargs):
        return await app._process_download(self.message, **self.deps, url=URL, formats=('txt',), **kwargs)

    async def collection(self):
        await app._process_collection(self.message, **self.deps, url=COLLECTION, formats=('txt',))

    async def test_duplicate_is_not_downloaded_or_counted_twice(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
            return True

        with patch.object(app, '_process_download_impl', side_effect=blocked) as process:
            first = asyncio.create_task(self.download())
            await started.wait()
            try:
                self.assertIsNone(await self.download())
                self.assertEqual(process.await_count, 1)
                self.assertIn('уже скачивается', self.message.statuses[-1].text)
            finally:
                release.set()
                await first

    async def test_user_cancellation_keeps_slot_until_worker_exits_and_sends_nothing(self):
        started, release = Event(), Event()

        def download(url, *args, **kwargs):
            started.set()
            release.wait(5)
            return Story('Book', 'Author', url)

        self.client.download.side_effect = download
        first = asyncio.create_task(self.download())
        await self.wait_until(started.is_set)
        token = next(iter(app._jobs._jobs))
        try:
            foreign = callback('download:cancel:' + token, user=2)
            await app.on_cancel_download(foreign)
            self.assertTrue(foreign.answer.call_args.kwargs['show_alert'])
            own = callback('download:cancel:' + token)
            await app.on_cancel_download(own)
            await asyncio.sleep(0.02)
            self.assertFalse(first.done())
            self.assertEqual(await self.queues.total_jobs(), 1)
            self.assertIsNone(await self.download())
        finally:
            release.set()
            await first
        self.assertEqual(await self.queues.total_jobs(), 0)
        self.assertEqual(self.message.documents, [])
        app._finish_download.assert_not_awaited()
        app._discard_download.assert_awaited_once()
        self.assertFalse(app._jobs._jobs)

    async def test_cancelling_queued_work_does_not_touch_the_running_work(self):
        started, release = Event(), Event()

        def download(url, *args, **kwargs):
            started.set()
            release.wait(5)
            return Story('Book', 'Author', url)

        self.client.download.side_effect = download
        first = asyncio.create_task(self.download())
        await self.wait_until(started.is_set)
        second = asyncio.create_task(self.download(cover_enabled=False))
        await self.wait_until(lambda: len(app._jobs._jobs) == 2)
        queued = list(app._jobs._jobs.values())[1]
        try:
            await app.on_cancel_download(callback('download:cancel:' + queued.token))
            await second
            self.assertEqual(self.client.download.call_count, 1)
            self.assertFalse(first.done())
        finally:
            release.set()
            await first
        self.assertEqual(len(self.message.documents), 1)

    async def test_stop_collection_cancels_child_and_never_starts_next_work(self):
        started, release = Event(), Event()

        def download(url, *args, **kwargs):
            started.set()
            release.wait(5)
            return Story('Book', 'Author', url)

        self.client.download.side_effect = download
        task = asyncio.create_task(self.collection())
        await self.wait_until(started.is_set)
        parent = next(job for job in app._jobs._jobs.values() if job.kind == 'collection')
        try:
            await app.on_cancel_download(callback('download:cancel:' + parent.token))
        finally:
            release.set()
            await task
        self.assertEqual(self.client.download.call_count, 1)
        self.assertFalse(app._collection_users)
        self.assertFalse(app._jobs._jobs)
        self.assertEqual(self.message.documents, [])

    async def test_failed_retry_only_contains_errors_and_is_consumed_once(self):
        with patch.object(app, '_process_download', AsyncMock(side_effect=[True, False, None])):
            await self.collection()
        summary = self.message.statuses[-1]
        self.assertIn('Не удалось: 1', summary.text)
        self.assertIn('Отменено: 1', summary.text)
        data = summary.reply_markup.inline_keyboard[0][0].callback_data
        foreign = callback(data, user=2)
        await app.on_retry_collection(foreign, **self.deps)
        self.assertTrue(foreign.answer.call_args.kwargs['show_alert'])
        other_chat = callback(data, chat=2)
        await app.on_retry_collection(other_chat, **self.deps)
        self.assertTrue(other_chat.answer.call_args.kwargs['show_alert'])
        with patch.object(app, '_process_download', AsyncMock(return_value=True)) as process:
            await app.on_retry_collection(callback(data), **self.deps)
            await app.on_retry_collection(callback(data), **self.deps)
        self.assertEqual([c.args[6] for c in process.call_args_list], [WORKS[1]])
        self.assertEqual(self.client.collection.call_count, 1)

    async def test_expired_retry_does_not_download(self):
        with patch.object(app, '_process_download', AsyncMock()) as process:
            await app.on_retry_collection(callback('collection:retry:expired'), **self.deps)
        process.assert_not_awaited()

    async def test_cancel_during_analytics_insert_does_not_leave_an_attempt(self):
        started, release = Event(), Event()

        def insert(*args):
            started.set()
            release.wait(3)
            return 77

        store = SimpleNamespace(start_download=Mock(side_effect=insert))
        task = asyncio.create_task(self.real_start_download(store, self.message, URL))
        await self.wait_until(started.is_set)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        app._discard_download.assert_awaited_once_with(store, 77)

    async def test_retry_survives_callback_acknowledgement_failure(self):
        with patch.object(app, '_process_download', AsyncMock(side_effect=[False, True, True])):
            await self.collection()
        data = self.message.statuses[-1].reply_markup.inline_keyboard[0][0].callback_data
        cb = callback(data)
        cb.answer.side_effect = app.TelegramNetworkError(method=app.BotCommand(command='x', description='x'),
                                                         message='timeout')
        with self.assertRaises(app.TelegramNetworkError):
            await app.on_retry_collection(cb, **self.deps)
        self.assertIn(data.removeprefix('collection:retry:'), app._failed_collections)

    async def test_repeated_retry_presses_cannot_run_two_batches(self):
        with patch.object(app, '_process_download', AsyncMock(side_effect=[False, True, True])):
            await self.collection()
        data = self.message.statuses[-1].reply_markup.inline_keyboard[0][0].callback_data
        entered, release = asyncio.Event(), asyncio.Event()

        async def acknowledge(*args, **kwargs):
            entered.set()
            await release.wait()

        cb = callback(data)
        cb.answer.side_effect = acknowledge
        with patch.object(app, '_process_collection', AsyncMock()) as process:
            first = asyncio.create_task(app.on_retry_collection(cb, **self.deps))
            await entered.wait()
            await app.on_retry_collection(callback(data), **self.deps)
            release.set()
            await first
        process.assert_awaited_once()

    async def test_shared_active_work_is_awaited_without_resending_or_cancelling(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
            return True

        self.client.collection.return_value = FicbookCollection('Collection', COLLECTION, (URL,))
        with patch.object(app, '_process_download_impl', side_effect=blocked) as process:
            first = asyncio.create_task(self.download())
            await started.wait()
            batch = asyncio.create_task(self.collection())
            await self.wait_until(lambda: bool(app._collection_users))
            await asyncio.sleep(0.02)
            self.assertFalse(batch.done())
            release.set()
            await asyncio.gather(first, batch)
        self.assertEqual(process.await_count, 1)
        self.assertIn('Отправлено: 1', self.message.statuses[-1].text)

    async def test_stage_text_is_visible_and_escaped(self):
        state = app.DownloadProgressState()
        state.observe('Скачиваю главы: 12/26 <script>')
        self.assertIn('12/26 &lt;script&gt;', state.status(('epub',)))
        await self.download()
        self.assertTrue(any('Создаю файлы' in edit for status in self.message.statuses for edit in status.edits))
        self.assertTrue(any('Отправляем...' in edit for status in self.message.statuses for edit in status.edits))

    async def test_delivered_files_cannot_be_cancelled_while_statistics_are_finishing(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def finish(*args, **kwargs):
            entered.set()
            await release.wait()

        app._finish_download.side_effect = finish
        task = asyncio.create_task(self.download())
        await entered.wait()
        job = next(iter(app._jobs._jobs.values()))
        try:
            self.assertEqual(len(self.message.documents), 1)
            self.assertFalse(app._jobs.cancel(job.token, 1, 1))
        finally:
            release.set()
            await task
        app._discard_download.assert_not_awaited()
