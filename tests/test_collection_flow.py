import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from src.bot import app
from src.core.download_queue import DownloadQueuePool
from src.core.limits import ResourceLimits
from src.sources.collections import CollectionError, FicbookCollection
from src.sources.fanficfare_http import SourceChallengeError
from src.sources.ficbook import FicbookClient, FicbookSiteStatus
from src.storage.analytics import UserDownloadSettings
from tests.test_download_status_flow import IncomingMessage, NoopAction


URL = 'https://ficbook.net/collections/123'
WORKS = tuple(f'https://ficbook.net/readfic/{i}' for i in (1, 2, 3))


class CollectionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = IncomingMessage()
        self.message.text = URL
        self.client = SimpleNamespace(collection=Mock(return_value=FicbookCollection('Books <&>', URL, WORKS)))
        self.dependencies = dict(ficbook_client=self.client, admin_chat_id=None, alert_bot=None,
                                 analytics_store=object(), download_queues=DownloadQueuePool(),
                                 premium_queue_user_ids=frozenset())

    async def run_collection(self, **kwargs):
        await app._process_collection(self.message, **self.dependencies, url=URL,
                                      formats=('txt', 'epub'), cover_enabled=False, **kwargs)

    async def test_collection_bypasses_chapter_selection_and_uses_saved_settings(self):
        settings = UserDownloadSettings(formats=('txt',), cover_enabled=False, chapter_selection_enabled=True)
        with patch.object(app, '_get_user_download_settings', AsyncMock(return_value=settings)), \
                patch.object(app, '_process_collection', AsyncMock()) as collection, \
                patch.object(app, '_ask_chapter_selection', AsyncMock()) as chapters, \
                patch.object(app, '_process_download', AsyncMock()) as download:
            await app.on_link(self.message, **self.dependencies)
        collection.assert_awaited_once()
        self.assertEqual(collection.call_args.kwargs['formats'], ('txt',))
        self.assertFalse(collection.call_args.kwargs['cover_enabled'])
        chapters.assert_not_awaited()
        download.assert_not_awaited()

    async def test_works_are_sequential_and_failure_does_not_stop_the_rest(self):
        with patch.object(app, '_process_download', AsyncMock(side_effect=[True, False, True])) as download:
            await self.run_collection()
        self.assertEqual([call.args[6] for call in download.call_args_list], list(WORKS))
        for call in download.call_args_list:
            self.assertEqual(call.args[7], ('txt', 'epub'))
            self.assertFalse(call.kwargs['cover_enabled'])
        self.assertIn('Отправлено: 2', self.message.statuses[-1].text)
        self.assertIn('Не удалось: 1', self.message.statuses[-1].text)
        self.assertIn('Books &lt;&amp;&gt;', self.message.statuses[-1].text)
        self.assertTrue(self.message.statuses[0].deleted)

    async def test_collection_failure_or_empty_list_does_not_start_downloads(self):
        for value in (CollectionError('closed'), FicbookCollection('Empty', URL, ())):
            with self.subTest(value=value), patch.object(app, '_process_download', AsyncMock()) as download:
                self.client.collection.side_effect = value if isinstance(value, Exception) else None
                self.client.collection.return_value = value
                await self.run_collection()
                download.assert_not_awaited()

    async def test_duplicate_batch_is_rejected_and_guard_cleared_after_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
            return True
        with patch.object(app, '_process_download', AsyncMock(side_effect=blocked)) as download:
            task = asyncio.create_task(self.run_collection())
            await asyncio.wait_for(started.wait(), 2)
            try:
                await self.run_collection()
                self.assertEqual(download.await_count, 1)
                self.assertIn('уже скачивается', self.message.statuses[-1].text)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        with patch.object(app, '_process_download', AsyncMock(return_value=True)) as download:
            await self.run_collection()
            self.assertEqual(download.await_count, 3)

    async def test_global_batch_limit_rejects_before_source_requests(self):
        with patch.object(app, '_collection_users', {999}), patch.object(
            app, 'current_limits', return_value=replace(ResourceLimits(), max_collection_batches=1),
        ):
            await self.run_collection()
        self.client.collection.assert_not_called()

    async def test_forbidden_telegram_stops_the_batch_and_releases_guard(self):
        error = TelegramForbiddenError(method=SendMessage(chat_id=1, text='test'), message='blocked')
        with patch.object(app, '_process_download', AsyncMock(side_effect=error)) as download:
            await self.run_collection()
        self.assertEqual(download.await_count, 1)
        self.assertFalse(app._collection_users)

    async def test_initial_telegram_failure_does_not_fetch_the_collection(self):
        with patch.object(app, '_answer_status_with_retry', AsyncMock(side_effect=app.FicbookError('offline'))):
            await self.run_collection()
        self.client.collection.assert_not_called()
        self.assertFalse(app._collection_users)

    async def test_each_actual_work_reuses_queue_and_records_its_own_statistics(self):
        self.client.download = Mock(side_effect=lambda url, *args, **kwargs: app.Story('Book', 'Author', url))
        with patch.object(app, '_start_download', AsyncMock(return_value=None)) as start, \
                patch.object(app, '_finish_download', AsyncMock()) as finish, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app._telegram_edit_gate, 'edit', AsyncMock()):
            await self.run_collection()
        self.assertEqual(start.await_count, 3)
        self.assertEqual(finish.await_count, 3)
        self.assertEqual(len(self.message.documents), 3)
        self.assertEqual(await self.dependencies['download_queues'].total_jobs(), 0)

    async def test_actual_paid_work_is_skipped_and_next_work_is_sent(self):
        def download(url, *args, **kwargs):
            if url == WORKS[1]:
                raise app.FicbookPaidContentError('Paid work')
            return app.Story('Book', 'Author', url)
        self.client.download = Mock(side_effect=download)
        with patch.object(app, '_start_download', AsyncMock(return_value=None)) as start, \
                patch.object(app, '_finish_download', AsyncMock()), \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app._telegram_edit_gate, 'edit', AsyncMock()):
            await self.run_collection()
        self.assertEqual(start.await_count, 3)
        self.assertEqual(self.client.download.call_count, 3)
        self.assertEqual(len(self.message.documents), 2)
        self.assertIn('Отправлено: 2', self.message.statuses[-1].text)
        self.assertIn('Не удалось: 1', self.message.statuses[-1].text)

    async def test_source_errors_do_not_pause_the_collection_or_poison_site_state(self):
        errors = (
            SourceChallengeError(WORKS[0], 403),
            ValueError('parser drift'),
            URLError('connection timeout'),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.message = IncomingMessage()
                source = FicbookClient(retry_attempts=1)
                original = source._download_once

                def download_once(url, account, progress, chapter_numbers):
                    if url == WORKS[0]:
                        return original(url, account, progress, chapter_numbers)
                    return app.Story('Book', 'Author', url)

                source.collection = self.client.collection
                self.dependencies['ficbook_client'] = source
                queues = DownloadQueuePool()
                self.dependencies['download_queues'] = queues
                with patch.object(source, '_download_once', side_effect=download_once), \
                        patch.object(source, '_build_config', return_value=object()), \
                        patch.object(source, 'ficbook_site_status', return_value=FicbookSiteStatus.UNKNOWN), \
                        patch('src.sources.ficbook.adapters.getAdapter', side_effect=error), \
                        patch.object(app, '_wait_for_ficbook_recovery', AsyncMock(
                            side_effect=app.FicbookError('Unexpected recovery wait'),
                        )) as wait, \
                        patch.object(app, '_start_download', AsyncMock(return_value=None)), \
                        patch.object(app, '_finish_download', AsyncMock()) as finish, \
                        patch.object(app, '_notify_admin', AsyncMock()), \
                        patch.object(app, '_build_selected_files', AsyncMock(return_value=[('txt', b'file')])), \
                        patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                        patch.object(app._telegram_edit_gate, 'edit', AsyncMock()):
                    await self.run_collection()
                wait.assert_not_awaited()
                self.assertFalse(queues.is_site_unavailable('ficbook.net'))
                self.assertEqual(await queues.total_jobs(), 0)
                self.assertEqual(len(self.message.documents), 2)
                self.assertEqual([call.kwargs['success'] for call in finish.call_args_list], [False, True, True])
                self.assertIn('Отправлено: 2', self.message.statuses[-1].text)
                self.assertIn('Не удалось: 1', self.message.statuses[-1].text)
                if isinstance(error, SourceChallengeError):
                    self.assertTrue(any('Cloudflare' in status.text for status in self.message.statuses))
