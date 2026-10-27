from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from src.bot.telegram_delivery import deliver
from src.storage.alert_retries import AlertRetry, AlertRetryStore
from src.bot.alert_relay import PREFIX as CARD_RETRY_PREFIX, relay_card_retry

logger = logging.getLogger(__name__)
RETRY_NOTICE = 'админ исправил вашу ошибку, вот ваша работа'
RETRY_PREFIX = 'alert:retry:'
# A claim holds one pool connection; leave two of the four for normal downloads.
MAX_ACTIVE_REDELIVERIES = 2


def retry_keyboard(token: str, *, busy: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text='Отправляется...' if busy else 'Отправить пользователю', callback_data=RETRY_PREFIX + token)]])


@dataclass(frozen=True, slots=True)
class Redelivery:
    sent_formats: tuple[str, ...]
    record_sent: Callable[[str], Awaitable[None]]
    notice: str = RETRY_NOTICE


class AlertRedelivery:
    def __init__(self, main_bot: Bot, admin_chat_id: int | None, store: AlertRetryStore,
                 download: Callable[[Message, AlertRetry, Redelivery], Awaitable[bool | None]]):
        self.main_bot = main_bot
        self.admin_chat_id = admin_chat_id
        self.store = store
        self.download = download
        self.active: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def dispatcher(self) -> Dispatcher:
        dispatcher = Dispatcher()
        dispatcher.callback_query.register(self.handle, F.data.startswith(RETRY_PREFIX))
        dispatcher.callback_query.register(relay_card_retry, F.data.startswith(CARD_RETRY_PREFIX))
        return dispatcher

    async def _answer(self, callback: CallbackQuery, text: str) -> None:
        try:
            await callback.answer(text, show_alert=True)
        except Exception:
            logger.warning('Could not acknowledge admin redelivery button')

    async def _keyboard(self, message: Message, token: str, *, busy: bool = False) -> None:
        try:
            await deliver(message, lambda: message.edit_reply_markup(reply_markup=retry_keyboard(token, busy=busy)))
        except Exception:
            logger.warning('Could not update admin redelivery button')

    async def _delete(self, message: Message, token: str) -> None:
        try:
            await deliver(message, message.delete)
        except TelegramBadRequest as exc:
            if 'message to delete not found' in exc.message.lower():
                return
            await self._delete_failed(message, token)
        except Exception:
            await self._delete_failed(message, token)

    async def _delete_failed(self, message: Message, token: str) -> None:
        logger.warning('Files delivered but Telegram did not delete the alert')
        await self._keyboard(message, token)
        await self._notice(message, 'Файлы отправлены. Не удалось удалить алерт; повторное нажатие повторит только удаление.')

    async def _notice(self, message: Message, text: str) -> None:
        try:
            await deliver(message, lambda: message.answer(text))
        except Exception:
            logger.warning('Could not send admin redelivery outcome')

    async def handle(self, callback: CallbackQuery) -> None:
        message = callback.message
        token = (callback.data or '').removeprefix(RETRY_PREFIX)
        if (not self.admin_chat_id or callback.from_user.id != self.admin_chat_id
                or not isinstance(message, Message) or message.chat.id != self.admin_chat_id):
            await self._answer(callback, 'Нет доступа.')
            return
        if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', token) is None:
            await self._answer(callback, 'Некорректная кнопка.')
            return
        if token in self.active or len(self.active) >= MAX_ACTIVE_REDELIVERIES:
            await self._answer(callback, 'Отправка уже выполняется. Дождись завершения.')
            return
        self.active.add(token)
        task = asyncio.current_task()
        self.tasks.add(task)
        claim = None
        try:
            # Shield acquisition so cancellation cannot leak a pooled session/advisory lock.
            acquisition = asyncio.create_task(asyncio.to_thread(
                self.store.claim, token, callback.bot.id, message.chat.id, message.message_id))
            try:
                claim = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                claim = await acquisition
                raise
            if claim is None:
                await self._answer(callback, 'Алерт истёк, недоступен или уже обрабатывается.')
                return
            if claim.request.delivered:
                await self._answer(callback, 'Уже отправлено. Удаляю алерт.')
                await self._delete(message, token)
                return
            await self._answer(callback, 'Начинаю повторную загрузку.')
            await self._keyboard(message, token, busy=True)
            request = claim.request
            target = Message(message_id=0, date=datetime.now(timezone.utc),
                from_user=User(id=request.user_id, is_bot=False, first_name='User'),
                chat=Chat(id=request.chat_id, type='private' if request.chat_id > 0 else 'supergroup'),
                text=request.url).as_(self.main_bot)

            async def record_sent(fmt: str) -> None:
                write = asyncio.create_task(asyncio.to_thread(claim.record_sent, fmt))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await write
                    raise

            await self.download(target, request, Redelivery(request.sent_formats, record_sent))
            if claim.request.delivered:
                await self._delete(message, token)
            else:
                await self._keyboard(message, token)
                await self._notice(message, 'Не удалось отправить все файлы. Алерт сохранён; можно повторить попытку.')
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Admin redelivery failed; alert retained')
            if claim is not None and claim.request.delivered:
                await self._delete(message, token)
            else:
                await self._keyboard(message, token)
                await self._notice(message, 'Повторная отправка не удалась. Алерт сохранён.')
        finally:
            try:
                if claim is not None:
                    closing = asyncio.create_task(asyncio.to_thread(claim.close))
                    try:
                        try:
                            await asyncio.shield(closing)
                        except asyncio.CancelledError:
                            await closing
                            raise
                    except Exception:
                        logger.warning('Could not release redelivery connection; broken session discarded')
            finally:
                self.active.discard(token)
                self.tasks.discard(task)


async def poll_bots(dispatcher: Dispatcher, bot: Bot, redelivery: AlertRedelivery, alert_bot: Bot | None,
                    *, tasks_concurrency_limit: int) -> None:
    pollers = [dispatcher]
    tasks = [asyncio.create_task(dispatcher.start_polling(
        bot, tasks_concurrency_limit=tasks_concurrency_limit, close_bot_session=False))]
    if alert_bot is not None and alert_bot.id != bot.id:
        alert_dispatcher = redelivery.dispatcher()
        pollers.append(alert_dispatcher)
        tasks.append(asyncio.create_task(alert_dispatcher.start_polling(
            alert_bot, allowed_updates=['callback_query'], handle_signals=False,
            close_bot_session=False, tasks_concurrency_limit=16)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for poller, task in zip(pollers, tasks):
            if not task.done():
                try:
                    await poller.stop_polling()
                except RuntimeError:
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
