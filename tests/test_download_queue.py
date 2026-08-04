from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest

from src.core.download_queue import (
    FAILOVER_PRIORITY,
    PREMIUM_PRIORITY,
    DownloadQueue,
    DownloadQueuePool,
)


@dataclass(frozen=True)
class FakeAccount:
    login: str


class DownloadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_initial_status_does_not_block_queue(self) -> None:
        queue = DownloadQueue()

        async def failed_status(_) -> None:
            raise ConnectionError("Telegram is unavailable")

        with self.assertRaises(ConnectionError):
            await queue.run(
                lambda: asyncio.sleep(0, result="never"),
                on_queued=failed_status,
            )

        self.assertEqual(await queue.total_jobs(), 0)
        result = await asyncio.wait_for(
            queue.run(lambda: asyncio.sleep(0, result="next")),
            timeout=1,
        )
        self.assertEqual(result, "next")

    async def test_first_position_is_reported_before_download_starts(self) -> None:
        queue = DownloadQueue()
        events: list[str] = []

        async def on_queued(estimate) -> None:
            events.append(f"queued:{estimate.position}")

        async def on_started() -> None:
            events.append("started")

        await queue.run(
            lambda: asyncio.sleep(0, result="done"),
            on_queued=on_queued,
            on_started=on_started,
        )

        self.assertEqual(events, ["queued:1", "started"])

    async def test_ficbook_assignments_use_round_robin(self) -> None:
        accounts = tuple(FakeAccount(str(index)) for index in range(1, 6))
        pool = DownloadQueuePool(ficbook_accounts=accounts)

        assignments = [pool.assign("https://ficbook.net/readfic/1") for _ in range(6)]

        self.assertEqual([item.account.login for item in assignments], ["1", "2", "3", "4", "5", "1"])
        self.assertIs(assignments[0].queue, assignments[5].queue)

    async def test_pool_keeps_other_sites_separate(self) -> None:
        pool = DownloadQueuePool()

        ao3_first = pool.assign("https://archiveofourown.org/works/1")
        ao3_second = pool.assign("https://archiveofourown.org/works/2")
        litnet = pool.assign("https://litnet.com/ru/reader/book-b1")

        self.assertIs(ao3_first.queue, ao3_second.queue)
        self.assertIsNot(ao3_first.queue, litnet.queue)

    async def test_priorities_are_ordered_without_preempting_active_job(self) -> None:
        queue = DownloadQueue()
        release = asyncio.Event()
        started = asyncio.Event()
        order: list[str] = []

        async def active_job() -> str:
            order.append("active")
            started.set()
            await release.wait()
            return "active"

        async def named_job(name: str) -> str:
            order.append(name)
            return name

        active = asyncio.create_task(queue.run(active_job))
        await started.wait()
        regular = asyncio.create_task(queue.run(lambda: named_job("regular")))
        await asyncio.sleep(0)
        failover = asyncio.create_task(
            queue.run(lambda: named_job("failover"), priority_level=FAILOVER_PRIORITY)
        )
        await asyncio.sleep(0)
        premium = asyncio.create_task(
            queue.run(lambda: named_job("premium"), priority_level=PREMIUM_PRIORITY)
        )
        await asyncio.sleep(0)

        release.set()
        await asyncio.wait_for(asyncio.gather(active, regular, failover, premium), timeout=2)

        self.assertEqual(order, ["active", "premium", "failover", "regular"])

    async def test_wait_estimate_decreases_while_active_job_runs(self) -> None:
        queue = DownloadQueue()
        release = asyncio.Event()
        started = asyncio.Event()
        estimates: list[float] = []
        updated = asyncio.Event()

        async def active_job() -> str:
            started.set()
            await release.wait()
            return "active"

        async def on_queued(estimate) -> None:
            estimates.append(estimate.estimated_wait_seconds)
            if len(estimates) >= 2:
                updated.set()

        active = asyncio.create_task(queue.run(active_job))
        await started.wait()
        waiting = asyncio.create_task(
            queue.run(
                lambda: asyncio.sleep(0, result="waiting"),
                on_queued=on_queued,
                queue_update_interval=0.05,
            )
        )

        await asyncio.wait_for(updated.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(asyncio.gather(active, waiting), timeout=2)

        self.assertLess(estimates[-1], estimates[0])

    async def test_total_jobs_counts_active_and_waiting_across_queues(self) -> None:
        pool = DownloadQueuePool()
        queue = pool.assign("https://archiveofourown.org/works/1").queue
        release = asyncio.Event()
        started = asyncio.Event()

        async def active_job() -> str:
            started.set()
            await release.wait()
            return "active"

        active = asyncio.create_task(queue.run(active_job))
        await started.wait()
        waiting = asyncio.create_task(queue.run(lambda: asyncio.sleep(0, result="waiting")))
        await asyncio.sleep(0)

        self.assertEqual(await pool.total_jobs(), 2)

        release.set()
        await asyncio.wait_for(asyncio.gather(active, waiting), timeout=2)

    async def test_failover_moves_to_next_account(self) -> None:
        accounts = tuple(FakeAccount(str(index)) for index in range(1, 4))
        pool = DownloadQueuePool(ficbook_accounts=accounts)
        first = pool.assign("https://ficbook.net/readfic/1")

        second = pool.failover(first, frozenset({0}), permanent=True)

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.ficbook_slot, 1)
        self.assertEqual(second.account.login if second.account else "", "2")


if __name__ == "__main__":
    unittest.main()
