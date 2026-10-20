import asyncio
import unittest

from src.core.download_queue import DownloadQueue, DownloadQueuePool, PREMIUM_PRIORITY, QueueFullError


class FairQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_another_user_can_use_idle_account_before_current_turn_is_full(self):
        pool = DownloadQueuePool(ficbook_accounts=('one', 'two'))
        first = pool.assign('https://ficbook.net/readfic/1').queue
        second = pool.assign('https://ficbook.net/readfic/2').queue
        release = asyncio.Event()
        active = asyncio.create_task(first.run(release.wait, user_id=1))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(first.run(lambda: asyncio.sleep(0), user_id=1))
        await asyncio.sleep(0)
        try:
            result = await asyncio.wait_for(second.run(lambda: asyncio.sleep(0, result='B'), user_id=2), 1)
            self.assertEqual(result, 'B')
        finally:
            release.set()
            await asyncio.gather(active, waiting)

    async def test_returning_premium_users_cannot_restart_a_round_ahead_of_waiters(self):
        queue = DownloadQueue()
        release = asyncio.Event()
        order = []
        tasks = []

        async def job(user, hold=False, return_user=False):
            order.append(user)
            if hold:
                await release.wait()
            if return_user:
                tasks.append(asyncio.create_task(queue.run(lambda: job('A'), user_id=1,
                                                           priority_level=PREMIUM_PRIORITY)))
                await asyncio.sleep(0)

        tasks.append(asyncio.create_task(queue.run(lambda: job('A', True), user_id=1)))
        await asyncio.sleep(0)
        for _ in range(4):
            tasks.append(asyncio.create_task(queue.run(lambda: job('A'), user_id=1)))
        for index in range(5):
            tasks.append(asyncio.create_task(queue.run(lambda index=index: job('B', return_user=index == 4),
                                                       user_id=2, priority_level=PREMIUM_PRIORITY)))
        tasks.append(asyncio.create_task(queue.run(lambda: job('C'), user_id=3)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await asyncio.gather(*tasks)
        self.assertEqual(order[10], 'C')

    async def test_full_queue_rejects_without_leaking_capacity(self):
        pool = DownloadQueuePool(max_jobs=2, max_active=1)
        queue = pool.assign('https://archiveofourown.org/works/1').queue
        release = asyncio.Event()
        active = asyncio.create_task(queue.run(release.wait, user_id=1))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(queue.run(lambda: asyncio.sleep(0), user_id=2))
        await asyncio.sleep(0)
        with self.assertRaises(QueueFullError):
            await queue.run(lambda: asyncio.sleep(0), user_id=3)
        release.set()
        await asyncio.gather(active, waiting)
        await queue.run(lambda: asyncio.sleep(0), user_id=3)
        self.assertEqual(await pool.total_jobs(), 0)

    async def test_upload_holds_capacity_until_files_are_sent(self):
        pool = DownloadQueuePool(max_active=1)
        first = pool.assign('https://archiveofourown.org/works/1').queue
        second = pool.assign('https://litnet.com/ru/reader/book-b1').queue
        uploading = asyncio.Event()
        release = asyncio.Event()
        other_started = asyncio.Event()

        async def upload(_):
            uploading.set()
            await release.wait()

        task = asyncio.create_task(first.run(lambda: asyncio.sleep(0), on_completed=upload))
        await uploading.wait()
        other = asyncio.create_task(second.run(lambda: asyncio.sleep(0), on_started=lambda: self.signal(other_started)))
        await asyncio.sleep(0)
        self.assertFalse(other_started.is_set())
        release.set()
        await asyncio.wait_for(asyncio.gather(task, other), 2)
        self.assertTrue(other_started.is_set())

    async def signal(self, event):
        event.set()

    async def test_failover_continuation_does_not_consume_another_place(self):
        queue = DownloadQueue()
        release = asyncio.Event()
        retry_started = asyncio.Event()
        release_retry = asyncio.Event()

        async def retry():
            retry_started.set()
            await release_retry.wait()

        active = asyncio.create_task(queue.run(release.wait, user_id=1))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(queue.run(retry, user_id=1, continuation=True))
        await asyncio.sleep(0)
        release.set()
        try:
            await asyncio.wait_for(retry_started.wait(), 1)
            self.assertEqual(queue._scheduler.streak, 1)
        finally:
            release_retry.set()
            await asyncio.gather(active, waiting)
        self.assertEqual(queue._scheduler.streak, 0)  # Idle queues discard scheduling history.

    async def schedule(self, queue, entries):
        release = asyncio.Event()
        started = asyncio.Event()
        order = []

        async def first():
            order.append('A')
            started.set()
            await release.wait()

        tasks = [asyncio.create_task(queue.run(first, user_id=1))]
        await asyncio.wait_for(started.wait(), 1)
        for user, priority in entries:
            async def job(user=user):
                order.append(user)
            tasks.append(asyncio.create_task(queue.run(
                job, user_id={'A': 1, 'B': 2, 'C': 3}[user], priority_level=priority,
            )))
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        return order

    async def test_five_starts_then_next_waiting_user(self):
        order = await self.schedule(DownloadQueue(), [('A', 0)] * 8 + [('B', 0)] * 6)
        self.assertEqual(order, ['A'] * 5 + ['B'] * 5 + ['A'] * 4 + ['B'])

    async def test_alone_can_finish_more_than_five(self):
        self.assertEqual(await self.schedule(DownloadQueue(), [('A', 0)] * 15), ['A'] * 16)

    async def test_priority_cannot_starve_another_user(self):
        order = await self.schedule(DownloadQueue(), [('A', PREMIUM_PRIORITY)] * 15 + [('B', 0)])
        self.assertEqual(order[5], 'B')

    async def test_users_rotate_not_just_alternate_between_two(self):
        order = await self.schedule(DownloadQueue(), [('A', 0)] * 9 + [('B', 0)] * 10 + [('C', 0)] * 10)
        self.assertEqual(order[:15], ['A'] * 5 + ['B'] * 5 + ['C'] * 5)

    async def test_ficbook_accounts_share_fair_turn(self):
        pool = DownloadQueuePool(ficbook_accounts=('one', 'two'))
        first = pool.assign('https://ficbook.net/readfic/1').queue
        second = pool.assign('https://ficbook.net/readfic/2').queue
        release = asyncio.Event()
        order = []

        async def job(user, hold=False):
            order.append(user)
            if hold:
                await release.wait()

        tasks = [asyncio.create_task(first.run(lambda: job('A', True), user_id=1))]
        await asyncio.sleep(0)
        for _ in range(8):
            tasks.append(asyncio.create_task(first.run(lambda: job('A'), user_id=1)))
        tasks.append(asyncio.create_task(first.run(lambda: job('B'), user_id=2)))
        await asyncio.sleep(0)
        # Starts on the second account consume the same user's site-wide turn.
        for _ in range(4):
            await second.run(lambda: job('A'), user_id=1)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(order[:6], ['A'] * 5 + ['B'])

    async def test_cancelled_waiter_does_not_block_the_turn(self):
        queue = DownloadQueue()
        release = asyncio.Event()
        active = asyncio.create_task(queue.run(release.wait, user_id=1))
        await asyncio.sleep(0)
        cancelled = asyncio.create_task(queue.run(lambda: asyncio.sleep(0), user_id=2))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        await active
        await asyncio.wait_for(queue.run(lambda: asyncio.sleep(0), user_id=3), 1)
        self.assertEqual(await queue.total_jobs(), 0)


if __name__ == '__main__':
    unittest.main()
