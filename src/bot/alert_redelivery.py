from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import logging
import json
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
from src.bot.jobs import download_key
from src.storage.download_jobs import DownloadJobs, restoring_job

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
    alert: dict | None = None
    deferred: bool = False


class AlertRedelivery:
    def __init__(self, main_bot: Bot, admin_chat_id: int | None, store: AlertRetryStore,
                 download: Callable[[Message, AlertRetry, Redelivery], Awaitable[bool | None]],
                 *, saved_jobs: DownloadJobs | None = None, schedule: Callable[[], None] | None = None):
        self.main_bot = main_bot
        self.admin_chat_id = admin_chat_id
        self.store = store
        self.download = download
        self.active: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.changed = asyncio.Event()
        self.saved_jobs = saved_jobs
        self.schedule = schedule
        self.enqueuing: set[str] = set()

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

    def _body(self, message: Message, text: str) -> str:
        base = (message.text or '').split(STATUS_HEADER, 1)[0]
        base = base.encode('utf-16-le')[:6400].decode('utf-16-le', 'ignore')
        text = text.encode('utf-16-le')[:1000].decode('utf-16-le', 'ignore')
        length = len(base.encode('utf-16-le')) // 2
        entities = [entity for entity in message.entities or () if entity.offset + entity.length <= length]
        return html_decoration.unparse(base, entities) + '\n\n<b>Повторная отправка</b>\n' + escape(text)

    async def _status(self, message: Message, token: str, text: str, *, busy: bool = False) -> None:
        try:
            await deliver(message, lambda: message.edit_text(
                self._body(message, text), parse_mode='HTML', reply_markup=retry_keyboard(token, busy=busy)))
        except Exception:
            logger.warning('Could not update admin redelivery status')

    async def _delete(self, message: Message, token: str, *, queued: bool = False) -> None:
        try:
            await deliver(message, message.delete)
        except TelegramBadRequest as exc:
            if 'message to delete not found' in exc.message.lower():
                return
            await self._delete_failed(message, token, queued=queued)
        except Exception:
            await self._delete_failed(message, token, queued=queued)

    async def _delete_failed(self, message: Message, token: str, *, queued: bool = False) -> None:
        logger.warning('Telegram did not delete the retry alert')
        await self._status(message, token,
            ('Работа сохранена в очереди. ' if queued else 'Работа отправлена. ') +
            'Telegram не позволил удалить алерт. '
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
        if self.saved_jobs is not None:
            await self.enqueue(message, token, callback)
        else:
            await self.retry(message, token, callback)

    @staticmethod
    def _target(request: AlertRetry, bot: Bot) -> Message:
        return Message(message_id=0, date=datetime.now(timezone.utc),
            from_user=User(id=request.user_id, is_bot=False, username=request.username or None,
                           first_name=request.full_name or f'ID {request.user_id}'),
            chat=Chat(id=request.chat_id, type='private' if request.chat_id > 0 else 'supergroup'),
            text=request.url).as_(bot)

    @staticmethod
    def _snapshot(message: Message) -> dict:
        return message.model_dump(mode='json', exclude_none=True, include={
            'message_id': True, 'date': True, 'chat': {'id', 'type'}, 'text': True, 'entities': True,
        })

    @staticmethod
    async def _close_claim(claim) -> None:
        closing = asyncio.create_task(asyncio.to_thread(claim.close))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise

    async def enqueue(self, message: Message, token: str, callback: CallbackQuery) -> None:
        retry_id = uuid4().hex[:12]
        log_retry(logger, 'alert_retry_clicked', retry_id=retry_id, alert_id=message.message_id)
        if token in self.enqueuing:
            await self._answer(callback, 'Сохраняю заявку в очередь.')
            return
        self.enqueuing.add(token)
        claim = None
        try:
            existing = next((entry for entry in self.saved_jobs.entries.values()
                             if entry.payload.get('alert', {}).get('token') == token), None)
            if existing is not None and existing.checkpoint.get('returned_message_id') == message.message_id:
                await asyncio.to_thread(self.store.replace_message, token,
                                        existing.payload['alert']['message']['message_id'], message.message_id)
                self.saved_jobs.finish(existing)
                existing = None
            if existing is not None:
                alert = existing.payload['alert']
                if alert['bot_id'] == message.bot.id and alert['message']['message_id'] == message.message_id:
                    await self._answer(callback, 'Работа уже в очереди.')
                    await self._delete(message, token, queued=True)
                else:
                    await self._answer(callback, 'Эта заявка уже обрабатывается.')
                return
            acquisition = asyncio.create_task(asyncio.to_thread(
                self.store.claim, token, message.bot.id, message.chat.id, message.message_id))
            try:
                claim = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                claim = await acquisition
                raise
            if claim is None:
                await self._answer(callback, 'Алерт истёк, недоступен или уже обрабатывается.')
                return
            if claim.request.delivered:
                await self._answer(callback, 'Работа уже отправлена.')
                await self._delete(message, token)
                return
            request = claim.request
            target = self._target(request, self.main_bot)
            key = download_key(request.user_id, request.chat_id, request.url, request.formats,
                               request.cover_enabled, request.chapter_numbers, request.opening_page)
            if any(entry.payload.get('key') == json.dumps(key) for entry in self.saved_jobs.entries.values()):
                await self._answer(callback, 'Эта работа уже скачивается или ожидает очереди.')
                return
            self.saved_jobs.add(dict(
                token=uuid4().hex[:24], key=json.dumps(key), kind='work', url=request.url,
                formats=list(request.formats), cover_enabled=request.cover_enabled, opening_page=request.opening_page,
                chapters=list(request.chapter_numbers) if request.chapter_numbers is not None else None,
                message=target.model_dump(mode='json', exclude_none=True),
                alert={'token': token, 'bot_id': message.bot.id, 'message': self._snapshot(message), 'queued': True},
            ))
            admitted, claim = claim, None
            try:
                await self._close_claim(admitted)
            except Exception:
                logger.warning('Could not release retry admission claim; broken session discarded')
            log_retry(logger, 'alert_retry_queued', retry_id=retry_id, alert_id=message.message_id, url=request.url)
            await self._answer(callback, 'Работа добавлена в очередь.')
            await self._delete(message, token, queued=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_retry(logger, 'alert_retry_enqueue_failed', retry_id=retry_id, alert_id=message.message_id, error=error)
            await self._answer(callback, 'Не удалось сохранить заявку. Алерт оставлен для повторной попытки.')
        finally:
            try:
                if claim is not None:
                    try:
                        await self._close_claim(claim)
                    except Exception:
                        logger.warning('Could not release retry admission claim; broken session discarded')
            finally:
                self.enqueuing.discard(token)
                if self.schedule is not None:
                    self.schedule()

    async def _return_alert(self, message: Message, token: str, entry) -> bool:
        message_id = entry.checkpoint.get('returned_message_id')
        if message_id is None:
            sent = await message.bot.send_message(
                message.chat.id, self._body(message, 'Не удалось отправить работу.\n' + entry.checkpoint['retry_failure']),
                parse_mode='HTML', reply_markup=retry_keyboard(token),
            )
            message_id = sent.message_id
            self.saved_jobs.save(entry, returned_message_id=message_id)
        await asyncio.to_thread(self.store.replace_message, token, message.message_id, message_id)
        return True

    async def retry(self, message: Message, token: str, callback: CallbackQuery | None = None) -> bool | None:
        async def answer(text: str) -> None:
            if callback is not None:
                await self._answer(callback, text)

        retry_id = uuid4().hex[:12]
        entry = (self.saved_jobs.entries.get(restoring_job.get()) if self.saved_jobs is not None else None)
        queued = entry is not None and entry.payload.get('alert', {}).get('token') == token
        log_retry(logger, 'alert_retry_clicked', retry_id=retry_id, alert_id=message.message_id)
        while callback is None and (token in self.active or len(self.active) >= MAX_ACTIVE_REDELIVERIES):
            self.changed.clear()
            await self.changed.wait()
        if token in self.active or len(self.active) >= MAX_ACTIVE_REDELIVERIES:
            log_retry(logger, 'alert_retry_rejected', retry_id=retry_id, alert_id=message.message_id, reason='Retry busy')
            await answer('Отправка уже выполняется. Дождись завершения.')
            return
        self.active.add(token)
        task = asyncio.current_task()
        self.tasks.add(task)
        claim = None
        try:
            if queued:
                await self._delete(message, token, queued=True)
                if entry.checkpoint.get('retry_failure'):
                    return await self._return_alert(message, token, entry)
            # Shield acquisition so cancellation cannot leak a pooled session/advisory lock.
            acquisition = asyncio.create_task(asyncio.to_thread(
                self.store.claim, token, message.bot.id, message.chat.id, message.message_id))
            try:
                claim = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                claim = await acquisition
                raise
            if claim is None:
                log_retry(logger, 'alert_retry_rejected', retry_id=retry_id, alert_id=message.message_id,
                          reason='Expired, claimed or mismatched alert')
                await answer('Алерт истёк, недоступен или уже обрабатывается.')
                if queued:
                    self.saved_jobs.save(entry, retry_failure='Не удалось восстановить повторную отправку. Алерт недоступен или истёк.')
                    return await self._return_alert(message, token, entry)
                if callback is None:
                    await self._status(message, token, 'Не удалось восстановить повторную отправку. Алерт недоступен или истёк.')
                return True
            if claim.request.delivered:
                log_retry(logger, 'alert_retry_succeeded', retry_id=retry_id, alert_id=message.message_id,
                          url=claim.request.url, already_delivered=True)
                await answer('Уже отправлено. Удаляю алерт.')
                await self._delete(message, token)
                return True
            await answer('Начинаю повторную загрузку.')
            if not queued:
                await self._status(message, token, 'Повторная отправка выполняется…', busy=True)
            request = claim.request
            log_retry(logger, 'alert_retry_started', retry_id=retry_id, alert_id=message.message_id,
                      url=request.url, formats=list(request.formats), sent_formats=list(request.sent_formats))
            target = self._target(request, self.main_bot)

            async def record_sent(fmt: str) -> None:
                write = asyncio.create_task(asyncio.to_thread(claim.record_sent, fmt))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await write
                    raise
                log_retry(logger, 'alert_retry_file_sent', retry_id=retry_id,
                          alert_id=message.message_id, url=request.url, format=fmt)

            delivery = Redelivery(request.sent_formats, record_sent, alert={
                'token': token, 'bot_id': message.bot.id,
                'message': self._snapshot(message), 'queued': queued,
            })
            await self.download(target, request, delivery)
            if delivery.deferred:
                if not queued:
                    await self._status(message, token, 'Задание сохранено. Повторится после /resume.', busy=True)
            elif claim.request.delivered:
                log_retry(logger, 'alert_retry_succeeded', retry_id=retry_id,
                          alert_id=message.message_id, url=request.url,
                          sent_formats=list(claim.request.sent_formats))
                await self._delete(message, token)
                return True
            else:
                reason = delivery.failure or 'Не все файлы удалось доставить. Можно повторить попытку.'
                log_retry(logger, 'alert_retry_failed', retry_id=retry_id, alert_id=message.message_id,
                          url=request.url, reason=reason, error=delivery.error,
                          sent_formats=list(claim.request.sent_formats))
                if queued:
                    self.saved_jobs.save(entry, retry_failure=reason)
                    return await self._return_alert(message, token, entry)
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
                return True
            elif queued:
                if claim is not None and not entry.checkpoint.get('retry_failure'):
                    self.saved_jobs.save(entry, retry_failure='Временный сбой при повторной загрузке. Можно повторить попытку.')
                return False
            else:
                await self._status(message, token,
                    'Не удалось отправить работу. Временный сбой при повторной загрузке. Можно повторить попытку.')
        finally:
            try:
                if claim is not None:
                    try:
                        await self._close_claim(claim)
                    except Exception:
                        logger.warning('Could not release redelivery connection; broken session discarded')
            finally:
                self.active.discard(token)
                self.tasks.discard(task)
                self.changed.set()


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
        async def stop_and_drain():
            for poller, task in zip(pollers, tasks):
                if not task.done():
                    try:
                        await poller.stop_polling()
                    except Exception as error:
                        logger.warning('Polling stop failed; cancelling poller type=%s', type(error).__name__)
                        task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # aiogram 3.31 stops polling without awaiting its per-update tasks.
            # Drain both dispatchers while the executors, DB and Bot sessions still exist.
            handlers = {handler for poller in pollers
                        for handler in getattr(poller, '_handle_update_tasks', ()) if not handler.done()}
            for handler in handlers:
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

        shutdown = asyncio.create_task(stop_and_drain())
        interrupted = False
        while not shutdown.done():
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                interrupted = True
        shutdown.result()
        if interrupted:
            raise asyncio.CancelledError
