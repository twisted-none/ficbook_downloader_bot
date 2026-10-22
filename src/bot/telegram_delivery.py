from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from time import monotonic
from typing import TypeVar
from weakref import WeakKeyDictionary

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError

T = TypeVar('T')
logger = logging.getLogger(__name__)


class TelegramDeliveryError(Exception):
    """A Telegram delivery failure, not a source-site failure."""


class DeliveryDeferred(Exception):
    def __init__(self, delay: float):
        self.delay = max(0.05, delay)


@dataclass
class ChatState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_send: float = 0.0
    users: int = 0


@dataclass
class LoopState:
    chats: OrderedDict[tuple[int, int], ChatState] = field(default_factory=OrderedDict)
    bot_next: dict[int, float] = field(default_factory=dict)
    bot_pause: dict[int, float] = field(default_factory=dict)
    last_warning: dict[int, float] = field(default_factory=dict)


class TelegramDeliveryGate:
    """Drop redundant status requests; pace required deliveries without dropping files."""

    def __init__(self, *, chat_interval: float = 1.1, bot_interval: float = 0.04,
                 max_wait: float = 300.0, max_chats: int = 1024,
                 clock: Callable[[], float] = monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.chat_interval = chat_interval
        self.bot_interval = bot_interval
        self.max_wait = max_wait
        self.max_chats = max_chats
        self.clock = clock
        self.sleep = sleep
        self._loops: WeakKeyDictionary = WeakKeyDictionary()

    def _state(self) -> LoopState:
        return self._loops.setdefault(asyncio.get_running_loop(), LoopState())

    @property
    def tracked_chats(self) -> int:
        return len(self._state().chats)

    async def call(self, bot_id: int, chat_id: int, operation: Callable[[], Awaitable[T]],
                   *, optional: bool = False, group: bool = False) -> T:
        state = self._state()
        key = (bot_id, chat_id)
        now = self.clock()
        if key not in state.chats:
            if len(state.chats) >= self.max_chats:
                # Never evict a lock with queued or active users, or lose a cooldown.
                expired = next((k for k, v in state.chats.items()
                                if not v.users and v.next_send <= now), None)
                if expired is None:
                    if optional:
                        raise DeliveryDeferred(1.1)
                    raise TelegramDeliveryError('Telegram delivery capacity exhausted')
                del state.chats[expired]
                live_bots = {k[0] for k in state.chats}
                state.bot_next = {k: v for k, v in state.bot_next.items()
                                  if k in live_bots or v > now}
                state.bot_pause = {k: v for k, v in state.bot_pause.items()
                                   if k in live_bots or v > now}
                state.last_warning = {k: v for k, v in state.last_warning.items()
                                      if k in live_bots or v + 60 > now}
            state.chats[key] = ChatState()
        chat = state.chats[key]
        state.chats.move_to_end(key)
        deadline = now + self.max_wait
        if optional and chat.users:
            raise DeliveryDeferred(self.chat_interval)
        chat.users += 1
        try:
            async with asyncio.timeout(self.max_wait if not optional else None):
                async with chat.lock:
                    while True:
                        now = self.clock()
                        delay = max(chat.next_send, state.bot_next.get(bot_id, 0),
                                    state.bot_pause.get(bot_id, 0)) - now
                        if delay <= 0:
                            break
                        if optional:
                            raise DeliveryDeferred(delay)
                        if now + delay > deadline:
                            raise TelegramDeliveryError('Telegram delivery cooldown exceeds wait budget')
                        await self.sleep(delay)
                    interval = max(self.chat_interval, 3.1 if group else 0)
                    chat.next_send = now + interval
                    state.bot_next[bot_id] = now + self.bot_interval
                    try:
                        return await operation()
                    except TelegramRetryAfter as exc:
                        # Telegram does not identify the scope of a 429; pause this bot conservatively.
                        state.bot_pause[bot_id] = max(state.bot_pause.get(bot_id, 0),
                                                     self.clock() + float(exc.retry_after) + 1)
                        self._warn(state, bot_id, type(exc).__name__)
                        raise
                    except (TelegramNetworkError, TelegramServerError) as exc:
                        chat.next_send = max(chat.next_send, self.clock() + 3)
                        self._warn(state, bot_id, type(exc).__name__)
                        raise
        except TimeoutError:
            raise TelegramDeliveryError('Telegram delivery wait budget exhausted') from None
        finally:
            chat.users -= 1

    def _warn(self, state: LoopState, bot_id: int, error_type: str) -> None:
        now = self.clock()
        if now - state.last_warning.get(bot_id, float('-inf')) >= 60:
            state.last_warning[bot_id] = now
            logger.warning('Telegram delivery paused after %s; queued downloads are retained', error_type)


telegram_delivery = TelegramDeliveryGate()


async def deliver(message, operation: Callable[[], Awaitable[T]], *, optional: bool = False) -> T:
    chat = getattr(message, 'chat', None)
    bot = getattr(message, 'bot', None)
    if chat is None or bot is None:
        return await operation()
    bot_id = getattr(bot, 'id', None)
    if not isinstance(bot_id, int):
        bot_id = id(bot)
    return await telegram_delivery.call(bot_id, chat.id, operation, optional=optional,
                                        group=getattr(chat, 'type', 'private') in ('group', 'supergroup'))
