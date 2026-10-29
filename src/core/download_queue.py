from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from threading import Lock
from typing import Any, TypeVar

from src.sources.registry import site_key
from src.core.site_recovery import SiteRecovery

T = TypeVar("T")
JOB_SECONDS = 120.0
REGULAR_PRIORITY = 0
FAILOVER_PRIORITY = 1
PREMIUM_PRIORITY = 2
QueueCallback = Callable[["QueueEstimate"], Awaitable[None]]
StartedCallback = Callable[[], Awaitable[None]]


class QueueFullError(Exception):
    """The configured admission limit has been reached."""


class QueueCapacity:
    def __init__(self, max_jobs: int = 500, max_active: int = 5) -> None:
        self.condition = asyncio.Condition()
        self.max_jobs = max(1, max_jobs)
        self.max_active = max(1, max_active)
        self.jobs = 0
        self.active = 0


@dataclass(eq=False, slots=True)
class _Ticket:
    queue: "DownloadQueue"
    token: object
    user: object
    priority: int
    continuation: bool = False
    ready: bool = False


class SiteScheduler:
    """One fair turn shared by all account queues for a site."""

    def __init__(self, capacity: QueueCapacity | None = None, *, slots: int = 1) -> None:
        self.capacity = capacity or QueueCapacity()
        self.waiting: list[_Ticket] = []
        self.last_user: object | None = None
        self.streak = 0
        self.served: set[object] = set()
        self.round_users: set[object] = set()
        self.round_open = True
        self.slots = slots
        self.active: dict[object, float] = {}

    @staticmethod
    def _pick(waiting: list[_Ticket], last_user: object | None, streak: int,
              served: set[object], round_users: set[object], round_open: bool) -> _Ticket | None:
        if not waiting:
            return None
        users = {ticket.user for ticket in waiting}
        if last_user in users and (streak < 5 or len(users) == 1):
            chosen = last_user
        else:
            if round_open:
                round_users.update(users)
            candidates = users & (round_users - served)
            if not candidates:
                served.clear()
                round_users.clear()
                round_users.update(users)
                candidates = users
            alternatives = candidates - {last_user}
            if alternatives:
                candidates = alternatives
            eligible = [ticket for ticket in waiting if ticket.user in candidates]
            chosen = max(eligible, key=lambda ticket: ticket.priority).user
        return next(ticket for ticket in waiting if ticket.user == chosen)

    def next_ticket(self) -> _Ticket | None:
        ready = [item for item in self.waiting if item.ready]
        ticket = self._pick(ready, self.last_user, self.streak, self.served,
                            self.round_users, self.round_open)
        if ticket is None:
            return None
        available = [item for item in ready if item.queue._active_token is None]
        preferred = next((item for item in available if item.user == ticket.user), None)
        if preferred is not None:
            return preferred
        # A busy account must not idle other accounts. The last user still
        # cannot exceed five starts while a different ready user is waiting.
        if self.streak >= 5 and any(item.user != self.last_user for item in ready):
            available = [item for item in available if item.user != self.last_user]
        if not available:
            return None
        unserved_users = self.round_users - self.served
        unserved = [item for item in available if item.user in unserved_users]
        user = max(unserved or available, key=lambda item: item.priority).user
        return next(item for item in available if item.user == user)

    def start(self, ticket: _Ticket) -> None:
        self.waiting.remove(ticket)
        self.active[ticket.token] = monotonic()
        if ticket.user != self.last_user:
            if self.last_user is not None:
                if self.round_open:
                    self.round_users.update(item.user for item in self.waiting if item.ready)
                self.round_open = False
            self.last_user = ticket.user
            self.streak = 0
        if not ticket.continuation:
            self.streak = min(5, self.streak + 1)
        self.served.add(ticket.user)
        self.round_users.add(ticket.user)
        # Remember the whole round, including users whose pending queue drained.
        # Otherwise a returning premium user can repeatedly jump the same waiter.

    def ordered(self) -> list[object]:
        groups: dict[object, deque[_Ticket]] = {}
        for ticket in self.waiting:
            groups.setdefault(ticket.user, deque()).append(ticket)
        representatives = {
            user: _Ticket(tickets[0].queue, tickets[0].token, user,
                          max(ticket.priority for ticket in tickets))
            for user, tickets in groups.items()
        }
        user, streak, served = self.last_user, self.streak, set(self.served)
        round_users, round_open = set(self.round_users), self.round_open
        result = []
        while groups:
            ticket = self._pick(list(representatives.values()), user, streak, served, round_users, round_open)
            assert ticket is not None
            if user is not None and ticket.user != user:
                round_open = False
            streak = streak if ticket.user == user else 0
            user = ticket.user
            served.add(user)
            round_users.add(user)
            tickets = groups[user]
            while tickets:
                item = tickets.popleft()
                streak = min(5, streak + int(not item.continuation))
                result.append(item.token)
                if streak >= 5 and len(groups) > 1:
                    break
            if tickets:
                representatives[user].priority = max(item.priority for item in tickets)
            else:
                del groups[user]
                del representatives[user]
        return result


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
    def __init__(self, *, min_start_interval: float = 0.0, scheduler: SiteScheduler | None = None) -> None:
        self._scheduler = scheduler or SiteScheduler()
        self._capacity = self._scheduler.capacity
        self._condition = self._capacity.condition
        self._min_start_interval = max(0.0, min_start_interval)
        self._start_lock = asyncio.Lock()
        self._last_started_at = 0.0
        self._waiting: set[object] = set()
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
        user_id: int | None = None,
        continuation: bool = False,
        on_completed: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        token = object()
        level = PREMIUM_PRIORITY if priority else REGULAR_PRIORITY
        if priority_level is not None:
            level = max(REGULAR_PRIORITY, priority_level)
        async with self._condition:
            if self._capacity.jobs >= self._capacity.max_jobs:
                raise QueueFullError("Download queue is full")
            self._capacity.jobs += 1
            self._waiting.add(token)
            ticket = _Ticket(self, token, user_id if user_id is not None else token, level, continuation)
            self._scheduler.waiting.append(ticket)
            estimate = self._queue_estimate_for(token)
            self._condition.notify_all()
        updater: asyncio.Task[None] | None = None
        started = False
        try:
            if on_queued:
                await on_queued(estimate)
                updater = self._queue_updater(token, on_queued, queue_update_interval)
            async with self._condition:
                ticket.ready = True
                self._condition.notify_all()
                while not self._can_start(token):
                    await self._condition.wait()
                self._discard_waiting(token)
                self._scheduler.start(ticket)
                self._capacity.active += 1
                self._active_token = token
                self._active_started_at = monotonic()
                started = True
                self._condition.notify_all()
            if updater:
                updater.cancel()
            if on_started:
                await on_started()
            await self._wait_for_turn()
            result = await job()
            if on_completed:
                await on_completed(result)
            return result
        finally:
            async with self._condition:
                self._capacity.jobs -= 1
                if started and self._active_token is token:
                    self._scheduler.active.pop(token, None)
                    self._capacity.active -= 1
                    self._active_token = None
                    self._active_started_at = 0.0
                elif not started:
                    self._discard_waiting(token)
                    if ticket in self._scheduler.waiting:
                        self._scheduler.waiting.remove(ticket)
                if not self._scheduler.waiting and not self._scheduler.active:
                    self._scheduler.served.clear()
                    self._scheduler.round_users.clear()
                    self._scheduler.round_open = True
                    self._scheduler.last_user = None
                    self._scheduler.streak = 0
                self._condition.notify_all()
            if updater:
                updater.cancel()
                await asyncio.gather(updater, return_exceptions=True)

    async def estimate_for_new(self, *, priority: bool = False, user_id: int | None = None) -> QueueEstimate:
        token = object()
        async with self._condition:
            self._waiting.add(token)
            ticket = _Ticket(self, token, user_id if user_id is not None else token,
                             PREMIUM_PRIORITY if priority else REGULAR_PRIORITY)
            self._scheduler.waiting.append(ticket)
            try:
                return self._queue_estimate_for(token)
            finally:
                self._discard_waiting(token)
                self._scheduler.waiting.remove(ticket)

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
        index = self._scheduler.ordered().index(token)
        active = len(self._scheduler.active)
        position = active + index + 1
        total = active + len(self._scheduler.waiting)
        slots = min(self._scheduler.slots, self._capacity.max_active)
        wait = index * JOB_SECONDS / slots
        if active:
            now = monotonic()
            wait += min(max(0.0, JOB_SECONDS - (now - started_at))
                        for started_at in self._scheduler.active.values())
        return QueueEstimate(position, total, wait)

    def _discard_waiting(self, token: object) -> None:
        self._waiting.discard(token)

    def _can_start(self, token: object) -> bool:
        ticket = self._scheduler.next_ticket()
        return (self._active_token is None and self._capacity.active < self._capacity.max_active
                and ticket is not None and ticket.token is token)


class DownloadQueuePool:
    def __init__(
        self,
        *,
        ficbook_accounts: tuple[Any, ...] = (),
        min_start_interval: float = 0.0,
        account_cooldown_seconds: float = 60.0,
        default_max_concurrent: int = 1,
        site_max_concurrent: dict[str, int] | None = None,
        max_jobs: int = 500,
        max_active: int = 5,
    ) -> None:
        del default_max_concurrent, site_max_concurrent
        self._min_start_interval = max(0.0, min_start_interval)
        self._account_cooldown_seconds = max(1.0, account_cooldown_seconds)
        self._accounts = ficbook_accounts or (None,)
        self._capacity = QueueCapacity(max_jobs, max_active)
        self._preview_scheduler = SiteScheduler(QueueCapacity(min(max_jobs, 50), min(max_active, 2)), slots=2)
        ficbook_scheduler = SiteScheduler(self._capacity, slots=len(self._accounts))
        self._ficbook_queues = tuple(
            DownloadQueue(min_start_interval=self._min_start_interval, scheduler=ficbook_scheduler)
            for _ in self._accounts
        )
        self._site_queues: dict[str, DownloadQueue] = {}
        self._next_ficbook_slot = 0
        self._disabled_slots: set[int] = set()
        self._cooldown_until: dict[int, float] = {}
        self._assignment_lock = Lock()
        self._site_state_lock = Lock()
        self._unavailable_sites: set[str] = set()
        self.ao3_recovery = SiteRecovery(interval=300)

    def assign(self, url: str) -> QueueAssignment:
        key = site_key(url)
        if key != "ficbook.net":
            queue = self._site_queues.setdefault(
                key,
                DownloadQueue(min_start_interval=self._min_start_interval,
                              scheduler=SiteScheduler(self._capacity)),
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

    async def run_preview(self, job: Callable[[], Awaitable[T]], *, user_id: int | None = None) -> T:
        queue = DownloadQueue(scheduler=self._preview_scheduler)
        return await queue.run(job, user_id=user_id)

    def mark_site_unavailable(self, key: str) -> None:
        with self._site_state_lock:
            self._unavailable_sites.add(key)

    async def clear_idle_ao3_outage(self) -> None:
        queue = self._site_queues.get('archiveofourown.org')
        jobs = await queue.total_jobs() if queue else 0
        if jobs == 0 and self.ao3_recovery.waiters == 0:
            # A later, unrelated submission must check its page immediately.
            self.mark_site_available('archiveofourown.org')

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
