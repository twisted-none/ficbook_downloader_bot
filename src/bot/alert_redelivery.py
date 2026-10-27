from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import logging
import re
from uuid import uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from aiogram.utils.text_decorations import html_decoration

from src.bot.telegram_delivery import deliver
from src.storage.alert_retries import AlertRetry, AlertRetryStore
from src.bot.alert_relay import PREFIX as CARD_RETRY_PREFIX, relay_card_retry
from src.core.retry_logging import log_retry

logger = logging.getLogger(__name__)
RETRY_NOTICE = 'Проблема со скачиванием исправлена. Вот ваша работа:'
RETRY_PREFIX = 'alert:retry:'
STATUS_HEADER = '\n\nПовторная отправка\n'
# A claim holds one pool connection; leave two of the four for normal downloads.
MAX_ACTIVE_REDELIVERIES = 2


def retry_keyboard(token: str, *, busy: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text='Отправляется...' if busy else 'Отправить пользователю еще раз', callback_data=RETRY_PREFIX + token)]])


@dataclass(slots=True)
class Redelivery:
    sent_formats: tuple[str, ...]
    record_sent: Callable[[str], Awaitable[None]]
    notice: str = RETRY_NOTICE
    failure: str = ''
    error: Exception | None = None


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

    async def _status(self, message: Message, token: str, text: str, *, busy: bool = False) -> None:
        base = (message.text or '').split(STATUS_HEADER, 1)[0]
        base = base.encode('utf-16-le')[:6400].decode('utf-16-le', 'ignore')
        text = text.encode('utf-16-le')[:1000].decode('utf-16-le', 'ignore')
        length = len(base.encode('utf-16-le')) // 2
        entities = [entity for entity in message.entities or () if entity.offset + entity.length <= length]
        body = html_decoration.unparse(base, entities) + '\n\n<b>Повторная отправка</b>\n' + escape(text)
        try:
            await deliver(message, lambda: message.edit_text(
                body, parse_mode='HTML', reply_markup=retry_keyboard(token, busy=busy)))
        except Exception:
            logger.warning('Could not update admin redelivery status')

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
        await self._status(message, token,
            'Работа отправлена. Telegram не позволил удалить алерт. '
            'Повторное нажатие повторит только удаление, без отправки файлов.')

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
        retry_id = uuid4().hex[:12]
        log_retry(logger, 'alert_retry_clicked', retry_id=retry_id, alert_id=message.message_id)
        if token in self.active or len(self.active) >= MAX_ACTIVE_REDELIVERIES:
            log_retry(logger, 'alert_retry_rejected', retry_id=retry_id, alert_id=message.message_id, reason='Retry busy')
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
                log_retry(logger, 'alert_retry_rejected', retry_id=retry_id, alert_id=message.message_id,
                          reason='Expired, claimed or mismatched alert')
                await self._answer(callback, 'Алерт истёк, недоступен или уже обрабатывается.')
                return
            if claim.request.delivered:
                log_retry(logger, 'alert_retry_succeeded', retry_id=retry_id, alert_id=message.message_id,
                          url=claim.request.url, already_delivered=True)
                await self._answer(callback, 'Уже отправлено. Удаляю алерт.')
                await self._delete(message, token)
                return
            await self._answer(callback, 'Начинаю повторную загрузку.')
            await self._status(message, token, 'Повторная отправка выполняется…', busy=True)
            request = claim.request
            log_retry(logger, 'alert_retry_started', retry_id=retry_id, alert_id=message.message_id,
                      url=request.url, formats=list(request.formats), sent_formats=list(request.sent_formats))
            target = Message(message_id=0, date=datetime.now(timezone.utc),
                from_user=User(id=request.user_id, is_bot=False, username=request.username or None,
                               first_name=request.full_name or f'ID {request.user_id}'),
                chat=Chat(id=request.chat_id, type='private' if request.chat_id > 0 else 'supergroup'),
                text=request.url).as_(self.main_bot)

            async def record_sent(fmt: str) -> None:
                write = asyncio.create_task(asyncio.to_thread(claim.record_sent, fmt))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await write
                    raise
                log_retry(logger, 'alert_retry_file_sent', retry_id=retry_id,
                          alert_id=message.message_id, url=request.url, format=fmt)

            delivery = Redelivery(request.sent_formats, record_sent)
            await self.download(target, request, delivery)
            if claim.request.delivered:
                log_retry(logger, 'alert_retry_succeeded', retry_id=retry_id,
                          alert_id=message.message_id, url=request.url,
                          sent_formats=list(claim.request.sent_formats))
                await self._delete(message, token)
            else:
                reason = delivery.failure or 'Не все файлы удалось доставить. Можно повторить попытку.'
                log_retry(logger, 'alert_retry_failed', retry_id=retry_id, alert_id=message.message_id,
                          url=request.url, reason=reason, error=delivery.error,
                          sent_formats=list(claim.request.sent_formats))
                await self._status(message, token, 'Не удалось отправить работу.\n' + reason)
        except asyncio.CancelledError:
            log_retry(logger, 'alert_retry_cancelled', retry_id=retry_id, alert_id=message.message_id,
                      url=claim.request.url if claim else '')
            raise
        except Exception as error:
            log_retry(logger, 'alert_retry_failed', retry_id=retry_id, alert_id=message.message_id,
                      url=claim.request.url if claim else '', reason='Unexpected retry failure', error=error)
            if claim is not None and claim.request.delivered:
                await self._delete(message, token)
            else:
                await self._status(message, token,
                    'Не удалось отправить работу. Временный сбой при повторной загрузке. Можно повторить попытку.')
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
