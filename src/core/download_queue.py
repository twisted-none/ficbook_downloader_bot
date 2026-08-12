from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from threading import Lock
from typing import Any, TypeVar

from src.sources.registry import site_key

T = TypeVar("T")
JOB_SECONDS = 120.0
REGULAR_PRIORITY = 0
FAILOVER_PRIORITY = 1
PREMIUM_PRIORITY = 2
QueueCallback = Callable[["QueueEstimate"], Awaitable[None]]
StartedCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    position: int
    total: int
    estimated_wait_seconds: float
    estimated_download_seconds: float = JOB_SECONDS


@dataclass(frozen=True, slots=True)
class QueueAssignment:
    key: str
    queue: "DownloadQueue"
    account: Any | None = None
    ficbook_slot: int | None = None


class DownloadQueue:
    def __init__(self, *, min_start_interval: float = 0.0) -> None:
        self._condition = asyncio.Condition()
        self._min_start_interval = max(0.0, min_start_interval)
        self._start_lock = asyncio.Lock()
        self._last_started_at = 0.0
        self._waiting: deque[object] = deque()
        self._priorities: dict[object, int] = {}
        self._active_token: object | None = None
        self._active_started_at = 0.0

    async def run(
        self,
        job: Callable[[], Awaitable[T]],
        on_queued: QueueCallback | None = None,
        on_started: StartedCallback | None = None,
        queue_update_interval: float = 60.0,
        priority: bool = False,
        priority_level: int | None = None,
    ) -> T:
        token = object()
        level = PREMIUM_PRIORITY if priority else REGULAR_PRIORITY
        if priority_level is not None:
            level = max(REGULAR_PRIORITY, priority_level)
        async with self._condition:
            self._add_waiting(token, level)
            estimate = self._queue_estimate_for(token)
            self._condition.notify_all()
        updater: asyncio.Task[None] | None = None
        started = False
        try:
            if on_queued:
                await on_queued(estimate)
                updater = self._queue_updater(token, on_queued, queue_update_interval)
            async with self._condition:
                while not self._can_start(token):
                    await self._condition.wait()
                self._discard_waiting(token)
                self._active_token = token
                self._active_started_at = monotonic()
                started = True
                self._condition.notify_all()
            if updater:
                updater.cancel()
            if on_started:
                await on_started()
            await self._wait_for_turn()
            return await job()
        finally:
            async with self._condition:
                if started and self._active_token is token:
                    self._active_token = None
                    self._active_started_at = 0.0
                elif not started:
                    self._discard_waiting(token)
                self._condition.notify_all()
            if updater:
                updater.cancel()
                await asyncio.gather(updater, return_exceptions=True)

    async def estimate_for_new(self, *, priority: bool = False) -> QueueEstimate:
        token = object()
        async with self._condition:
            self._add_waiting(token, PREMIUM_PRIORITY if priority else REGULAR_PRIORITY)
            try:
                return self._queue_estimate_for(token)
            finally:
                self._discard_waiting(token)

    async def total_jobs(self) -> int:
        async with self._condition:
            return int(self._active_token is not None) + len(self._waiting)

    def _queue_updater(
        self,
        token: object,
        callback: QueueCallback,
        interval: float,
    ) -> asyncio.Task[None]:
        async def update() -> None:
            while True:
                await asyncio.sleep(max(0.05, interval))
                async with self._condition:
                    if token not in self._waiting:
                        return
                    estimate = self._queue_estimate_for(token)
                if estimate.position > 1:
                    await callback(estimate)

        return asyncio.create_task(update())

    async def _wait_for_turn(self) -> None:
        async with self._start_lock:
            remaining = self._min_start_interval - (monotonic() - self._last_started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_started_at = monotonic()

    def _queue_estimate_for(self, token: object) -> QueueEstimate:
        index = self._waiting.index(token)
        active = self._active_token is not None
        position = int(active) + index + 1
        total = int(active) + len(self._waiting)
        wait = index * JOB_SECONDS
        if active:
            elapsed = max(0.0, monotonic() - self._active_started_at)
            wait += max(0.0, JOB_SECONDS - elapsed)
        return QueueEstimate(position, total, wait)

    def _add_waiting(self, token: object, priority: int) -> None:
        self._priorities[token] = priority
        for index, waiting_token in enumerate(self._waiting):
            if self._priorities[waiting_token] < priority:
                self._waiting.insert(index, token)
                return
        self._waiting.append(token)

    def _discard_waiting(self, token: object) -> None:
        self._priorities.pop(token, None)
        try:
            self._waiting.remove(token)
        except ValueError:
            pass

    def _can_start(self, token: object) -> bool:
        return self._active_token is None and bool(self._waiting) and self._waiting[0] is token


class DownloadQueuePool:
    def __init__(
        self,
        *,
        ficbook_accounts: tuple[Any, ...] = (),
        min_start_interval: float = 0.0,
        account_cooldown_seconds: float = 60.0,
        default_max_concurrent: int = 1,
        site_max_concurrent: dict[str, int] | None = None,
    ) -> None:
        del default_max_concurrent, site_max_concurrent
        self._min_start_interval = max(0.0, min_start_interval)
        self._account_cooldown_seconds = max(1.0, account_cooldown_seconds)
        self._accounts = ficbook_accounts or (None,)
        self._ficbook_queues = tuple(
            DownloadQueue(min_start_interval=self._min_start_interval)
            for _ in self._accounts
        )
        self._site_queues: dict[str, DownloadQueue] = {}
        self._next_ficbook_slot = 0
        self._disabled_slots: set[int] = set()
        self._cooldown_until: dict[int, float] = {}
        self._assignment_lock = Lock()
        self._site_state_lock = Lock()
        self._unavailable_sites: set[str] = set()

    def assign(self, url: str) -> QueueAssignment:
        key = site_key(url)
        if key != "ficbook.net":
            queue = self._site_queues.setdefault(
                key,
                DownloadQueue(min_start_interval=self._min_start_interval),
            )
            return QueueAssignment(key, queue)
        with self._assignment_lock:
            slot = self._next_available_slot(self._next_ficbook_slot, frozenset())
            if slot is None:
                slot = self._next_ficbook_slot % len(self._accounts)
            self._next_ficbook_slot = (slot + 1) % len(self._accounts)
        return self._ficbook_assignment(slot)

    def failover(
        self,
        assignment: QueueAssignment,
        attempted_slots: frozenset[int],
        *,
        permanent: bool,
        retry_after: float | None = None,
    ) -> QueueAssignment | None:
        current = assignment.ficbook_slot
        if current is None:
            return None
        with self._assignment_lock:
            if permanent:
                self._disabled_slots.add(current)
            else:
                cooldown = retry_after or self._account_cooldown_seconds
                self._cooldown_until[current] = monotonic() + max(1.0, cooldown)
            slot = self._next_available_slot((current + 1) % len(self._accounts), attempted_slots)
        return self._ficbook_assignment(slot) if slot is not None else None

    async def total_jobs(self) -> int:
        queues = [*self._ficbook_queues, *self._site_queues.values()]
        counts = await asyncio.gather(*(queue.total_jobs() for queue in queues))
        return sum(counts)

    def mark_site_unavailable(self, key: str) -> None:
        with self._site_state_lock:
            self._unavailable_sites.add(key)

    def mark_site_available(self, key: str) -> None:
        with self._site_state_lock:
            self._unavailable_sites.discard(key)

    def is_site_unavailable(self, key: str) -> bool:
        with self._site_state_lock:
            return key in self._unavailable_sites

    def for_url(self, url: str) -> DownloadQueue:
        return self.assign(url).queue

    def _next_available_slot(self, start: int, excluded: frozenset[int]) -> int | None:
        now = monotonic()
        for offset in range(len(self._accounts)):
            slot = (start + offset) % len(self._accounts)
            if slot in excluded or slot in self._disabled_slots:
                continue
            if self._cooldown_until.get(slot, 0.0) > now:
                continue
            return slot
        return None

    def _ficbook_assignment(self, slot: int) -> QueueAssignment:
        return QueueAssignment(
            key=f"ficbook.net:{slot + 1}",
            queue=self._ficbook_queues[slot],
            account=self._accounts[slot],
            ficbook_slot=slot,
        )
