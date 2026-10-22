import asyncio
import unittest

from src.bot.jobs import JobRegistry, download_key


URL = 'https://ficbook.net/readfic/123'


class DownloadJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_ao3_chapter_urls_share_a_work_key_unless_selection_differs(self):
        root = 'https://archiveofourown.org/works/123'
        self.assertEqual(download_key(1, 1, root, ('txt',), True, None),
                         download_key(1, 1, root + '/chapters/456?view_adult=true', ('txt',), True, None))
        self.assertNotEqual(download_key(1, 1, root, ('txt',), True, {1}),
                            download_key(1, 1, root, ('txt',), True, {2}))

    async def test_keys_normalize_links_but_preserve_download_options_and_owners(self):
        key = download_key(1, 10, URL, ('epub', 'fb2'), True, None)
        self.assertEqual(key, download_key(1, 10, URL + '?utm_source=test#part', ('fb2', 'epub'), True, None))
        for user, chat, formats, cover, chapters in (
            (2, 10, ('epub', 'fb2'), True, None),
            (1, 20, ('epub', 'fb2'), True, None),
            (1, 10, ('txt',), True, None),
            (1, 10, ('epub', 'fb2'), False, None),
            (1, 10, ('epub', 'fb2'), True, {1}),
        ):
            self.assertNotEqual(key, download_key(user, chat, URL, formats, cover, chapters))

    async def test_cancellation_is_owner_and_chat_scoped_and_only_requested_once(self):
        registry = JobRegistry()
        job, created = registry.reserve(('work',), 1, 10)
        self.assertTrue(created)
        self.assertIs(registry.reserve(('work',), 1, 10)[0], job)
        self.assertFalse(registry.reserve(('work',), 1, 10)[1])
        job.task = asyncio.create_task(asyncio.sleep(100))
        self.assertFalse(registry.cancel(job.token, 2, 10))
        self.assertFalse(registry.cancel(job.token, 1, 20))
        self.assertTrue(registry.cancel(job.token, 1, 10))
        self.assertTrue(registry.cancel(job.token, 1, 10))
        self.assertEqual(job.task.cancelling(), 1)
        with self.assertRaises(asyncio.CancelledError):
            await job.task
        self.assertIs(registry.reserve(('work',), 1, 10)[0], job)
        registry.release(job)
        self.assertFalse(registry.cancel(job.token, 1, 10))
        self.assertTrue(registry.reserve(('work',), 1, 10)[1])

    async def test_active_jobs_are_not_evicted_at_capacity(self):
        registry = JobRegistry(max_entries=1)
        job, _ = registry.reserve(('one',), 1, 10)
        with self.assertRaises(OverflowError):
            registry.reserve(('two',), 2, 20)
        self.assertIs(registry.get(job.token), job)
