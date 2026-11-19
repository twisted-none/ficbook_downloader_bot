import asyncio
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot.alert_redelivery import retry_keyboard
from src.storage.download_jobs import DownloadJobs, restoring_job
from tests import test_alert_redelivery as helpers


class QueuedAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await helpers.AlertRedeliveryTests.asyncSetUp(self)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'queue.sqlite3'
        self.saved = DownloadJobs(self.path, self.bot.id, 20)
        self.addCleanup(self.saved.db.close)
        self.service.saved_jobs = self.saved
        self.service.schedule = Mock()
        self.store.replace_message = Mock()
        self.notice.return_value = Mock(message_id=55)

        def receipt(fmt):
            sent = (*self.claim.request.sent_formats, fmt)
            self.claim.request = replace(self.claim.request, sent_formats=sent,
                                         delivered=set(sent) == set(self.claim.request.formats))
        self.claim.record_sent.side_effect = receipt

    async def click(self, **kwargs):
        await helpers.AlertRedeliveryTests.call(self, **kwargs)

    async def process(self, entry):
        query = helpers.callback().as_(self.bot)
        query.message.as_(self.bot)
        context = restoring_job.set(entry.id)
        try:
            result = await self.service.retry(query.message, 'test_token')
            if result:
                self.saved.finish(entry)
            return result
        finally:
            restoring_job.reset(context)

    async def test_click_saves_before_deletion_and_does_not_wait_for_download(self):
        async def deleted(**kwargs):
            self.assertEqual(len(self.saved.entries), 1)
        self.delete.side_effect = deleted
        await self.click()
        [entry] = self.saved.entries.values()
        self.assertEqual(entry.payload['formats'], ['epub', 'fb2'])
        self.assertEqual(entry.payload['chapters'], [2, 4])
        self.assertFalse(entry.payload['cover_enabled'])
        self.assertTrue(entry.payload['alert']['queued'])
        self.download.assert_not_awaited()
        self.claim.close.assert_called_once()
        self.service.schedule.assert_called_once()
        self.assertIn('добавлена в очередь', self.answer.await_args.kwargs['text'])

    async def test_more_than_two_clicks_are_queued_and_double_click_is_deduplicated(self):
        await self.click()
        await self.click()
        self.assertEqual(len(self.saved.entries), 1)
        self.assertEqual(self.store.claim.call_count, 1)
        for number in (2, 3):
            self.claim.request = replace(self.retry, token=f'token_{number}',
                                         url=f'https://ficbook.net/readfic/{number}')
            await self.click(token=f'token_{number}', message_id=number)
        self.assertEqual(len(self.saved.entries), 3)
        self.download.assert_not_awaited()

    async def test_save_failure_keeps_original_alert(self):
        with patch.object(self.saved, 'add', side_effect=RuntimeError('disk full')):
            await self.click()
        self.delete.assert_not_awaited()
        self.download.assert_not_awaited()
        self.assertFalse(self.saved.entries)
        self.claim.close.assert_called_once()

    async def test_delete_failure_keeps_one_saved_job_and_second_click_only_retries_delete(self):
        self.delete.side_effect = RuntimeError('Telegram unavailable')
        await self.click()
        await self.click()
        self.assertEqual(len(self.saved.entries), 1)
        self.assertEqual(self.store.claim.call_count, 1)
        self.assertIn('сохранена в очереди', self.edit.await_args.kwargs['text'])
        self.download.assert_not_awaited()

    async def test_success_does_not_recreate_alert_and_preserves_delivery_receipts(self):
        await self.click()
        [entry] = self.saved.entries.values()
        async def download(message, request, delivery):
            self.assertEqual(request.chapter_numbers, (2, 4))
            await delivery.record_sent('epub')
            await delivery.record_sent('fb2')
            return True
        self.download.side_effect = download
        self.assertTrue(await self.process(entry))
        self.assertFalse(self.saved.entries)
        self.notice.assert_not_awaited()
        self.assertEqual(self.claim.request.sent_formats, ('epub', 'fb2'))

    async def test_failure_returns_new_alert_with_same_retry_token(self):
        await self.click()
        [entry] = self.saved.entries.values()
        async def download(message, request, delivery):
            delivery.failure = '<source> & unavailable'
            return False
        self.download.side_effect = download
        self.assertTrue(await self.process(entry))
        self.assertFalse(self.saved.entries)
        self.assertIn('&lt;source&gt; &amp; unavailable', self.notice.await_args.kwargs['text'])
        self.assertEqual(self.notice.await_args.kwargs['reply_markup'],
                         retry_keyboard('test_token').model_dump(exclude_none=True))
        self.store.replace_message.assert_called_once_with('test_token', 12, 55)

    async def test_failed_alert_send_is_retried_without_downloading_again(self):
        await self.click()
        [entry] = self.saved.entries.values()
        self.notice.side_effect = RuntimeError('Telegram unavailable')
        self.assertFalse(await self.process(entry))
        self.assertTrue(entry.checkpoint.get('retry_failure'))
        self.notice.side_effect = None
        self.assertTrue(await self.process(entry))
        self.download.assert_awaited_once()

    async def test_failed_rebinding_uses_saved_message_after_restart(self):
        await self.click()
        [entry] = self.saved.entries.values()
        self.store.replace_message.side_effect = RuntimeError('Database unavailable')
        self.assertFalse(await self.process(entry))
        reopened = DownloadJobs(self.path, self.bot.id, 20)
        self.addCleanup(reopened.db.close)
        self.saved = reopened
        self.service.saved_jobs = reopened
        [restored] = reopened.entries.values()
        self.assertEqual(restored.checkpoint['returned_message_id'], 55)
        self.store.replace_message.side_effect = None
        self.assertTrue(await self.process(restored))
        self.notice.assert_awaited_once()
        self.download.assert_awaited_once()

    async def test_real_download_failure_keeps_queue_entry_until_alert_is_returned(self):
        from types import SimpleNamespace
        from src.bot import app
        from src.core.download_queue import DownloadQueuePool
        from src.sources.ficbook import FicbookError
        await self.click()
        [entry] = self.saved.entries.values()
        queues = DownloadQueuePool()
        queues.saved_jobs = self.saved
        client = SimpleNamespace(download=Mock(side_effect=FicbookError('Source failed')))
        async def download(message, request, delivery):
            result = await app._process_download(
                message, client, 9, None, object(), queues, request.url, request.formats,
                frozenset(), cover_enabled=request.cover_enabled,
                chapter_numbers=set(request.chapter_numbers), redelivery=delivery,
            )
            self.assertIn(entry.id, self.saved.entries)
            return result
        self.download.side_effect = download
        self.notice.side_effect = RuntimeError('Telegram unavailable')
        with patch.object(app, '_start_download', AsyncMock()), \
                patch.object(app, '_finish_download', AsyncMock()), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify:
            self.assertFalse(await self.process(entry))
        self.assertIn(entry.id, self.saved.entries)
        self.assertEqual(entry.checkpoint['retry_failure'], 'Source failed')
        self.notice.side_effect = None
        self.assertTrue(await self.process(entry))
        self.download.assert_awaited_once()
        notify.assert_not_awaited()

    async def test_old_saved_retry_uses_new_alert_flow(self):
        await self.click()
        [entry] = self.saved.entries.values()
        entry.payload['alert'].pop('queued')
        self.assertTrue(await self.process(entry))
        self.notice.assert_awaited_once()
        self.edit.assert_not_awaited()

    async def test_shutdown_keeps_job_and_partially_sent_formats(self):
        await self.click()
        [entry] = self.saved.entries.values()
        sent = asyncio.Event()
        async def download(message, request, delivery):
            await delivery.record_sent('epub')
            sent.set()
            await asyncio.Event().wait()
        self.download.side_effect = download
        task = asyncio.create_task(self.process(entry))
        await asyncio.wait_for(sent.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(entry.id, self.saved.entries)
        self.assertEqual(self.claim.request.sent_formats, ('epub',))
        self.claim.close.assert_called()
