import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Chat, Message, User

from src.bot import app
from src.bot.jobs import current_job
from src.core.download_queue import DownloadQueuePool
from src.core.models import Story
from src.storage.download_jobs import DownloadJobs, restoring_job


class PersistentDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'queue.sqlite3'
        self.saved = DownloadJobs(self.path, 1, 10)
        self.addCleanup(self.saved.db.close)
        self.message = Message(message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=20, type='private'), from_user=User(id=20, is_bot=False, first_name='Reader'))

    def submit(self, url, operation):
        return asyncio.create_task(app._run_tracked(self.message, url, ('fb2', 'epub'), False,
            {2}, 'work', None, operation, saved_jobs=self.saved))

    async def test_restart_keeps_interrupted_job_settings_and_receipts(self):
        entered = asyncio.Event()
        async def download():
            self.saved.save(current_job.get().saved, sent_formats=['fb2'])
            entered.set()
            await asyncio.Event().wait()

        task = self.submit('https://ficbook.net/readfic/123', download)
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.saved.pause()
        reopened = DownloadJobs(self.path, 1, 10)
        self.addCleanup(reopened.db.close)
        [entry] = reopened.entries.values()
        self.assertTrue(reopened.paused)
        self.assertEqual(entry.payload['chapters'], [2])
        self.assertEqual(entry.payload['formats'], ['fb2', 'epub'])
        self.assertFalse(entry.payload['cover_enabled'])
        self.assertEqual(entry.checkpoint['sent_formats'], ['fb2'])
        reopened.resume()
        self.saved = reopened
        async def finish():
            self.assertEqual(current_job.get().token, entry.payload['token'])
            self.assertEqual(current_job.get().saved.checkpoint['sent_formats'], ['fb2'])
            return True

        context = restoring_job.set(entry.id)
        try:
            self.assertTrue(await self.submit(entry.payload['url'], finish))
        finally:
            restoring_job.reset(context)
        self.assertFalse(reopened.entries)

    async def test_stop_drains_available_jobs_and_defers_site_outage_and_new_links(self):
        queues = DownloadQueuePool()
        queues.saved_jobs = self.saved
        ready = asyncio.Event()
        release = asyncio.Event()
        available = asyncio.Event()
        outage = asyncio.Event()
        admitted = asyncio.Event()
        new_job_started = AsyncMock(return_value=True)

        async def healthy_job():
            current_job.get().started = True
            available.set()
            await release.wait()
            return True

        async def unavailable_job():
            current_job.get().started = True
            current_job.get().waiting_for_site = True
            queues.mark_site_unavailable('archiveofourown.org')
            outage.set()
            await asyncio.Event().wait()

        async def answer(text):
            if 'Можно перезапускать' in text:
                ready.set()

        async def notice(*args):
            admitted.set()

        admin = SimpleNamespace(from_user=SimpleNamespace(id=9), answer=AsyncMock(side_effect=answer))
        healthy = self.submit('https://fikbooks.ru/readfic/123', healthy_job)
        blocked = self.submit('https://archiveofourown.org/works/123', unavailable_job)
        tasks = [healthy, blocked]
        try:
            await asyncio.wait_for(asyncio.gather(available.wait(), outage.wait()), 2)
            stop = asyncio.create_task(app.on_stop_queue(admin, 9, queues))
            tasks.append(stop)
            await asyncio.wait_for(asyncio.gather(blocked, return_exceptions=True), 2)
            self.assertTrue(blocked.cancelled())
            with patch.object(app, '_optional_status', side_effect=notice):
                held = self.submit('https://litnet.com/ru/book/title-b123', new_job_started)
                tasks.append(held)
                await asyncio.wait_for(admitted.wait(), 2)
                self.assertFalse(ready.is_set())
                new_job_started.assert_not_awaited()
                release.set()
                await asyncio.wait_for(ready.wait(), 3)
                self.assertTrue(healthy.done())
                self.assertEqual(len(self.saved.entries), 2)
                self.assertIn('Оставлено до восстановления сайтов: 1', admin.answer.await_args.args[0])
                self.assertIsNone(await asyncio.wait_for(held, 2))
                def restore():
                    entry = next(entry for entry in self.saved.entries.values()
                                 if entry.payload['url'].startswith('https://litnet.com/'))
                    context = restoring_job.set(entry.id)
                    try:
                        tasks.append(self.submit(entry.payload['url'], new_job_started))
                    finally:
                        restoring_job.reset(context)
                await app.on_resume_queue(admin, 9, queues, restore)
                self.assertTrue(await asyncio.wait_for(tasks[-1], 2))
                new_job_started.assert_awaited_once()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_restored_download_sends_only_unconfirmed_formats(self):
        from tests.test_download_status_flow import IncomingMessage, NoopAction
        url = 'https://fikbooks.ru/readfic/123'
        entry = self.saved.add({'token': 'saved-test-job', 'url': url})
        self.saved.save(entry, sent_formats=['fb2'])
        queues = DownloadQueuePool()
        queues.saved_jobs = self.saved
        message = IncomingMessage()
        client = SimpleNamespace(download=Mock(return_value=Story('Book', 'Author', url)))
        context = restoring_job.set(entry.id)
        try:
            with patch.object(app, '_start_download', AsyncMock()), \
                    patch.object(app, '_finish_download', AsyncMock()), \
                    patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                    patch.object(app, '_build_selected_files', AsyncMock(
                        return_value=[('fb2', b'fb2'), ('epub', b'epub')])):
                self.assertTrue(await app._process_download(message, client, None, None, object(), queues,
                                                           url, ('fb2', 'epub'), frozenset()))
        finally:
            restoring_job.reset(context)
        self.assertEqual(len(message.documents), 1)
        self.assertEqual(message.documents[0]['caption'], 'Book (EPUB)')
        self.assertFalse(self.saved.entries)

    async def test_collection_resumes_after_last_completed_work(self):
        from tests.test_download_status_flow import IncomingMessage
        url = 'https://ficbook.net/collections/123'
        first, second = ('https://ficbook.net/readfic/1', 'https://ficbook.net/readfic/2')
        entry = self.saved.add({'token': 'saved-collection', 'url': url})
        self.saved.save(entry, collection_urls=[first, second], collection_title='Saved collection',
                       processed=1, sent=1, sent_formats=[])
        queues = DownloadQueuePool()
        queues.saved_jobs = self.saved
        message = IncomingMessage()
        client = SimpleNamespace(collection=Mock())
        context = restoring_job.set(entry.id)
        try:
            with patch.object(app, '_process_download', AsyncMock(return_value=True)) as download:
                await app._process_collection(message, client, None, None, object(), queues,
                                              url, ('epub',), frozenset())
        finally:
            restoring_job.reset(context)
        download.assert_awaited_once()
        self.assertEqual(download.call_args.args[6], second)
        client.collection.assert_not_called()
        self.assertFalse(self.saved.entries)
