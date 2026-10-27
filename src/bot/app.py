from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from html import escape
import logging
import re
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

from src.storage.analytics import AnalyticsStore, UserDownloadSettings, _public_story_url
from src.storage.alert_retries import AlertRetry, AlertRetryStore
from src.bot.alert_redelivery import AlertRedelivery, Redelivery, RETRY_PREFIX, poll_bots, retry_keyboard
from src.bot.alert_relay import PREFIX as CARD_RETRY_PREFIX, relay_card_retry
from src.bot.types import PendingChapterSelection, PendingSupportMessage, SettingsDraft, SettingsSession
from src.bot.sessions import ExpiringDict, ExpiringSet
from src.bot.jobs import JobRegistry, current_job, download_key
from src.bot.telegram_delivery import DeliveryDeferred, TelegramDeliveryError, deliver
from src.bot.ui import (
    active_download_status as _active_download_status,
    cancel_keyboard as _cancel_keyboard,
    chapter_selection_input_text as _chapter_selection_input_text,
    developer_error_text as _developer_error_text,
    download_error_text as _download_error_text,
    download_progress_percent as _download_progress_percent,
    download_identity as _download_identity,
    format_list as _format_list,
    main_keyboard as _main_keyboard,
    main_keyboard_for_message as _main_keyboard_for_message,
    main_keyboard_for_user as _main_keyboard_for_user,
    progress_text as _progress_text,
    queue_status as _queue_status,
    reply_keyboard as _reply_keyboard,
    sending_status as _sending_status,
    settings_back_keyboard as _settings_back_keyboard,
    settings_changed as _settings_changed,
    settings_cover_keyboard as _settings_cover_keyboard,
    settings_cover_text as _settings_cover_text,
    settings_formats_keyboard as _settings_formats_keyboard,
    settings_formats_text as _settings_formats_text,
    settings_menu_keyboard as _settings_menu_keyboard,
    settings_menu_text as _settings_menu_text,
    settings_save_error_text as _settings_save_error_text,
    settings_unsaved_keyboard as _settings_unsaved_keyboard,
    settings_unsaved_text as _settings_unsaved_text,
    support_keyboard as _support_keyboard,
    support_confirmation_keyboard as _support_confirmation_keyboard,
    WELCOME_TEXT,
)
from src.core.chapter_selection import ChapterSelectionError, parse_chapter_selection
from src.core.config import Settings
from src.core.download_queue import (
    FAILOVER_PRIORITY,
    PREMIUM_PRIORITY,
    DownloadQueuePool,
    QueueAssignment,
    QueueFullError,
)
from src.core.limits import (ResourceLimitError, ResourceLimits, bounded_async_operation,
                             check_deadline, close_download_workers, current_limits, run_worker, validate_story)
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.sources.ficbook import (
    FicbookAccount,
    FicbookClient,
    FicbookError,
    FicbookLoginError,
    FicbookNotFoundError,
    FicbookPaidContentError,
    FicbookRateLimitError,
    FicbookSiteStatus,
    FicbookSiteUnavailableError,
)
from src.exporters.formats import ALLOWED_FORMATS, build_docx, build_pdf, build_txt, normalize_formats
from src.sources.share_links import ShareLinkError, extract_download_url, is_google_share_url, resolve_share_url
from src.sources.collections import CollectionError, FicbookCollection
from src.sources.registry import is_ficbook_collection_url
from src.monitoring.metrics import run_metrics_server
from src.storage.retention import run_history_retention
from src.core.models import Story, StoryPreview
from src.core.progress import ProgressReporter
from src.core.covers import cover_for_story
from src.storage.users import UserStore, UserStoreError

router = Router()
URL_RE = re.compile(r"https?://[^\s]+")
logger = logging.getLogger(__name__)
_broadcast_waiting: ExpiringSet[int] = ExpiringSet()
_support_waiting: ExpiringSet[int] = ExpiringSet()
_support_pending: ExpiringDict[int, PendingSupportMessage] = ExpiringDict()
_reply_waiting: ExpiringDict[int, int] = ExpiringDict()
_settings_drafts: ExpiringDict[int, SettingsSession] = ExpiringDict()
_chapter_waiting: ExpiringDict[int, PendingChapterSelection] = ExpiringDict()
_collection_users: set[int] = set()
_jobs = JobRegistry()


@dataclass(frozen=True)
class FailedCollection:
    message: Message
    collection: FicbookCollection
    formats: tuple[str, ...]
    cover_enabled: bool
    user_id: int
    chat_id: int


_failed_collections: ExpiringDict[str, FailedCollection] = ExpiringDict(max_entries=1000)
_session_stores = (_broadcast_waiting, _support_waiting, _support_pending,
                   _reply_waiting, _settings_drafts, _chapter_waiting, _failed_collections)
FICBOOK_SITE_RECHECK_SECONDS = 60.0
ACTIVE_PROGRESS_UPDATE_SECONDS = 5.0
TELEGRAM_SEND_ATTEMPTS = 3
TELEGRAM_SEND_RETRY_DELAY_SECONDS = 3.0


class TelegramEditGate:
    _MAX_TRACKED_CHATS = 1024
    _STALE_CHAT_SECONDS = 600.0

    def __init__(self, min_interval: float = 1.1) -> None:
        self._min_interval = min_interval
        self._guard = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_edit: dict[int, float] = {}

    async def edit(self, message: Message, text: str) -> None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if chat_id is None:
            await message.edit_text(text, **_job_markup())
            return
        async with self._guard:
            if chat_id not in self._locks and len(self._locks) >= self._MAX_TRACKED_CHATS:
                self._prune_stale_chats()
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            delay = self._min_interval - (monotonic() - self._last_edit.get(chat_id, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await deliver(message, lambda: message.edit_text(text, **_job_markup()), optional=True)
            finally:
                self._last_edit[chat_id] = monotonic()

    def _prune_stale_chats(self) -> None:
        cutoff = monotonic() - self._STALE_CHAT_SECONDS
        stale = [
            chat_id
            for chat_id, edited_at in self._last_edit.items()
            if edited_at < cutoff and not self._locks[chat_id].locked()
        ]
        for chat_id in stale:
            self._locks.pop(chat_id, None)
            self._last_edit.pop(chat_id, None)


_telegram_edit_gate = TelegramEditGate()


class ProgressMessage:
    def __init__(self, message: Message | None, *, min_interval: float = 1.0, initial_text: str = "") -> None:
        self._message = message
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._last_text = initial_text
        self._last_edited_at = 0.0
        self._min_interval = min_interval
        self._retry_after_until = 0.0
        self._pending_text = ""
        self._pending_force = False
        self._pending_task: asyncio.Task[None] | None = None
        self._closed = False

    async def set(self, text: str, *, force: bool = False) -> None:
        async with self._lock:
            if self._closed:
                return
            job = current_job.get()
            if job is not None:
                text = 'Отменяю загрузку. Ожидаю завершения текущего запроса...' if job.cancel_requested else text
                job.text = text
            if self._message is None:
                self._last_text = text
                return
            now = monotonic()
            if text == self._last_text:
                return
            if force:
                self._clear_pending_locked()
            if text == self._pending_text:
                self._pending_force = self._pending_force or force
                return
            if now < self._retry_after_until:
                self._schedule_pending_locked(text, force=force, delay=self._retry_after_until - now)
                return
            if not force and now - self._last_edited_at < self._min_interval:
                self._schedule_pending_locked(text, force=force, delay=self._min_interval - (now - self._last_edited_at))
                return
            try:
                await _telegram_edit_gate.edit(self._message, text)
            except DeliveryDeferred as exc:
                self._schedule_pending_locked(text, force=force, delay=exc.delay)
                return
            except TelegramRetryAfter as exc:
                retry_after = float(getattr(exc, "retry_after", 60))
                self._retry_after_until = monotonic() + retry_after + 1.0
                self._schedule_pending_locked(text, force=force, delay=retry_after + 1.0)
                logger.warning("Telegram throttled progress edit for %.0f seconds", retry_after)
                return
            except (TelegramNetworkError, TelegramServerError) as exc:
                logger.warning("Telegram transient error while editing progress: %s", exc)
                self._schedule_pending_locked(text, force=force, delay=TELEGRAM_SEND_RETRY_DELAY_SECONDS)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    self._last_text = text
                    self._last_edited_at = monotonic()
                    return
                logger.warning("Failed to edit progress message: %s", exc)
                return
            self._last_text = text
            self._last_edited_at = monotonic()

    async def set_required(self, text: str, *, wait_for_throttle: bool = True) -> bool:
        job = current_job.get()
        if job is not None:
            job.text = text
        if self._message is None:
            return False
        while True:
            delay = 0.0
            async with self._lock:
                self._clear_pending_locked()
                if self._closed:
                    return text == self._last_text
                if text == self._last_text:
                    self._closed = True
                    return True
                now = monotonic()
                if now < self._retry_after_until:
                    if not wait_for_throttle:
                        return False
                    delay = self._retry_after_until - now
                else:
                    try:
                        await _telegram_edit_gate.edit(self._message, text)
                    except DeliveryDeferred:
                        return False
                    except TelegramRetryAfter as exc:
                        retry_after = float(getattr(exc, "retry_after", 60))
                        self._retry_after_until = monotonic() + retry_after + 1.0
                        if not wait_for_throttle:
                            return False
                        delay = retry_after + 1.0
                    except (TelegramNetworkError, TelegramServerError) as exc:
                        logger.warning("Telegram transient error while setting required progress: %s", exc)
                        return False
                    except TelegramBadRequest as exc:
                        if "message is not modified" in str(exc).lower():
                            self._last_text = text
                            self._last_edited_at = monotonic()
                            self._closed = True
                            return True
                        logger.warning("Failed to set required progress text: %s", exc)
                        return False
                    else:
                        self._last_text = text
                        self._last_edited_at = monotonic()
                        self._closed = True
                        return True
            await asyncio.sleep(max(0.05, delay))

    def from_thread(self, text: str, *, force: bool = False) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.set(text, force=force), self._loop)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._clear_pending_locked()

    def _schedule_pending_locked(self, text: str, *, force: bool, delay: float) -> None:
        self._pending_text = text
        self._pending_force = self._pending_force or force
        if self._pending_task and not self._pending_task.done():
            return
        self._pending_task = asyncio.create_task(self._flush_pending_after(max(0.05, delay)))

    def _clear_pending_locked(self) -> None:
        self._pending_text = ""
        self._pending_force = False
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()
        self._pending_task = None

    async def _flush_pending_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            text = self._pending_text
            force = self._pending_force
            self._pending_text = ""
            self._pending_force = False
            self._pending_task = None
        if text:
            await self.set(text, force=force)


class DownloadProgressState:
    def __init__(self, *, source_url: str = '') -> None:
        self._lock = Lock()
        self._attempt_started_at = monotonic()
        self._actual_percent = 0
        self._shown_percent = 0
        self._stage = 'Получаю информацию о фанфике'
        self._identity = _download_identity(url=source_url)

    def start_attempt(self) -> None:
        with self._lock:
            self._attempt_started_at = monotonic()

    def observe(self, text: str) -> None:
        self.set_stage(text)
        self.set_actual(_download_progress_percent(text))

    def set_stage(self, text: str) -> None:
        with self._lock:
            self._stage = text[:400]

    def set_title(self, title: str) -> None:
        if title.strip():
            with self._lock:
                self._identity = _download_identity(title=title)

    def with_identity(self, text: str) -> str:
        with self._lock:
            identity = self._identity
        return identity + '\n' + text if identity else text

    def status(self, formats: tuple[str, ...]) -> str:
        with self._lock:
            stage = self._stage
        return self.with_identity(_active_download_status(formats, self.percent()) + '\n' + escape(stage))

    def set_actual(self, percent: int) -> None:
        with self._lock:
            self._actual_percent = max(self._actual_percent, min(95, percent))

    def percent(self) -> int:
        with self._lock:
            elapsed = max(0.0, monotonic() - self._attempt_started_at)
            timed = min(95, int(elapsed / 120.0 * 90))
            self._shown_percent = max(self._shown_percent, self._actual_percent, timed)
            return self._shown_percent


class RegisterUserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else None
        waiting = any(user_id in state for state in (
            _support_waiting, _broadcast_waiting, _reply_waiting, _chapter_waiting,
        ))
        text = getattr(event, 'text', None) or ''
        if not waiting and not text.lstrip().startswith('/') and extract_download_url(text):
            status = await _acknowledge_link(event)
            data['initial_status'] = status
            data['acknowledgement_attempted'] = True
        user_store: UserStore | None = data.get("user_store")
        if user_store and event.from_user:
            try:
                await asyncio.to_thread(user_store.add, event.from_user.id)
            except (UserStoreError, OSError):
                logger.error('User registry could not be updated; existing data left intact')
        analytics_store: AnalyticsStore | None = data.get("analytics_store")
        if analytics_store and event.from_user:
            await _record_user(analytics_store, event.from_user)
        return await handler(event, data)


def create_dispatcher(
    client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    user_store: UserStore,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    premium_queue_user_ids: frozenset[int],
) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["ficbook_client"] = client
    dispatcher["admin_chat_id"] = admin_chat_id
    dispatcher["alert_bot"] = alert_bot
    dispatcher["user_store"] = user_store
    dispatcher["analytics_store"] = analytics_store
    dispatcher["download_queues"] = download_queues
    dispatcher["premium_queue_user_ids"] = premium_queue_user_ids
    router.message.middleware(RegisterUserMiddleware())
    dispatcher.include_router(router)
    return dispatcher


async def _expire_sessions() -> None:
    while True:
        await asyncio.sleep(60)
        for store in _session_stores:
            store.expire()


@router.callback_query(F.data.startswith(RETRY_PREFIX))
async def on_alert_retry(callback: CallbackQuery, alert_redelivery: AlertRedelivery | None = None) -> None:
    if alert_redelivery is not None:
        await alert_redelivery.handle(callback)
    else:
        await callback.answer('Повторная отправка недоступна.', show_alert=True)


@router.callback_query(F.data.startswith(CARD_RETRY_PREFIX))
async def on_card_alert_retry(callback: CallbackQuery) -> None:
    await relay_card_retry(callback)


@router.message(CommandStart())
async def on_start(message: Message, analytics_store: AnalyticsStore, admin_chat_id: int | None) -> None:
    if message.from_user:
        await _record_user(analytics_store, message.from_user, started=True)
    await message.answer(WELCOME_TEXT, reply_markup=_main_keyboard_for_message(message, admin_chat_id))


@router.message(Command("help"))
async def on_help(message: Message, admin_chat_id: int | None) -> None:
    if not message.from_user:
        return
    if admin_chat_id is None:
        await message.answer("Поддержка сейчас недоступна.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        return
    _support_pending.pop(message.from_user.id, None)
    _support_waiting.add(message.from_user.id)
    await message.answer(
        "Напиши сообщение для поддержки следующим сообщением. /cancel — отменить.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(Command("settings"))
async def on_settings(message: Message, analytics_store: AnalyticsStore, admin_chat_id: int | None) -> None:
    if not message.from_user:
        return
    settings = await _get_user_download_settings(analytics_store, message.from_user.id)
    draft = SettingsDraft(settings.formats, settings.chapter_selection_enabled, settings.cover_enabled)
    sent = await message.answer(_settings_menu_text(draft), reply_markup=_settings_menu_keyboard(draft))
    _settings_drafts[message.from_user.id] = SettingsSession(
        saved=draft, draft=draft, message_id=sent.message_id, chat_id=sent.chat.id,
    )


@router.message(Command("broadcast"))
async def on_broadcast_command(message: Message, admin_chat_id: int | None) -> None:
    if not _is_admin(message, admin_chat_id):
        await message.answer("Команда доступна только администратору.")
        return
    _broadcast_waiting.add(message.from_user.id)
    await message.answer(
        "Пришли следующим сообщением текст рассылки. /cancel — отменить.",
        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
    )


@router.message(Command("queue"))
async def on_queue_command(
    message: Message,
    admin_chat_id: int | None,
    download_queues: DownloadQueuePool,
) -> None:
    if not _is_admin(message, admin_chat_id):
        await message.answer("Команда доступна только администратору.")
        return
    total = await download_queues.total_jobs()
    await message.answer(f"Количество фанфиков в очереди: {total}")


@router.message(Command("cancel"))
async def on_cancel(message: Message, admin_chat_id: int | None) -> None:
    if not message.from_user:
        return
    cancelled = False
    if _is_admin(message, admin_chat_id) and message.from_user.id in _broadcast_waiting:
        _broadcast_waiting.discard(message.from_user.id)
        await message.answer("Рассылка отменена.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        cancelled = True
    if _chapter_waiting.pop(message.from_user.id, None):
        await message.answer("Выбор глав отменен.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        cancelled = True
    pending_support = _support_pending.pop(message.from_user.id, None)
    if pending_support:
        await _clear_support_confirmation(pending_support.confirmation)
    if message.from_user.id in _support_waiting or pending_support:
        _support_waiting.discard(message.from_user.id)
        await message.answer("Сообщение в поддержку отменено.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        cancelled = True
    if _reply_waiting.pop(message.from_user.id, None) is not None:
        await message.answer("Ответ пользователю отменен.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        cancelled = True
    if not cancelled:
        await message.answer("Сейчас нечего отменять.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))


@router.callback_query(F.data == "support:write")
async def on_support_click(callback: CallbackQuery, admin_chat_id: int | None) -> None:
    if not callback.from_user:
        return
    await callback.answer()
    if admin_chat_id is None:
        await callback.message.answer("Поддержка сейчас недоступна.", reply_markup=_main_keyboard())  # type: ignore[union-attr]
        return
    _support_pending.pop(callback.from_user.id, None)
    _support_waiting.add(callback.from_user.id)
    await callback.message.answer(  # type: ignore[union-attr]
        "Напиши сообщение для поддержки следующим сообщением. /cancel — отменить.",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "settings:section:formats")
async def on_settings_formats_section(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    session.view = "formats"
    await callback.message.edit_text(_settings_formats_text(session.draft), reply_markup=_settings_formats_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data == "settings:section:cover")
async def on_settings_cover_section(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    session.view = "cover"
    await callback.message.edit_text(_settings_cover_text(session.draft), reply_markup=_settings_cover_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data.startswith("settings:toggle:"))
async def on_settings_toggle_format(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    fmt = (callback.data or "").rsplit(":", 1)[-1]
    if fmt not in ALLOWED_FORMATS:
        await callback.answer("Неизвестный формат", show_alert=True)
        return
    selected = set(session.draft.formats)
    if fmt in selected:
        if len(selected) == 1:
            await callback.answer("Нельзя убрать все форматы.", show_alert=True)
            return
        selected.remove(fmt)
    else:
        selected.add(fmt)
    session.draft = replace(session.draft, formats=normalize_formats(tuple(selected)))
    session.view = "formats"
    await callback.message.edit_text(_settings_formats_text(session.draft), reply_markup=_settings_formats_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data == "settings:select_all")
async def on_settings_select_all(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    session.draft = replace(session.draft, formats=ALLOWED_FORMATS)
    session.view = "formats"
    await callback.message.edit_text(_settings_formats_text(session.draft), reply_markup=_settings_formats_keyboard(session.draft))
    await callback.answer("Выбраны все форматы")


@router.callback_query(F.data.startswith("settings:cover:"))
async def on_settings_toggle_cover(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    enabled = (callback.data or "").rsplit(":", 1)[-1] == "on"
    session.draft = replace(session.draft, cover_enabled=enabled)
    session.view = "cover"
    await callback.message.edit_text(_settings_cover_text(session.draft), reply_markup=_settings_cover_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data == "settings:toggle_chapters")
async def on_settings_toggle_chapters(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    session.draft = replace(session.draft, chapter_selection_enabled=not session.draft.chapter_selection_enabled)
    await callback.message.edit_text(_settings_menu_text(session.draft), reply_markup=_settings_menu_keyboard(session.draft))
    await callback.answer("Настройка обновлена")


@router.callback_query(F.data.startswith("settings:save:"))
async def on_settings_save(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.from_user or not callback.message:
        return
    await callback.answer("Сохраняю...")
    if not await _save_settings_session(callback.from_user.id, session, analytics_store):
        await callback.message.edit_text(_settings_save_error_text(session.draft), reply_markup=_settings_back_keyboard())
        return
    if session.view == "cover":
        await callback.message.edit_text(
            _settings_cover_text(session.draft, saved=True),
            reply_markup=_settings_cover_keyboard(session.draft),
        )
    elif session.view == "formats":
        await callback.message.edit_text(
            _settings_formats_text(session.draft, saved=True),
            reply_markup=_settings_formats_keyboard(session.draft),
        )
    else:
        await callback.message.edit_text(_settings_menu_text(session.draft, saved=True), reply_markup=_settings_menu_keyboard(session.draft))


@router.callback_query(F.data == "settings:back")
async def on_settings_back(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    if _settings_changed(session):
        await callback.message.edit_text(_settings_unsaved_text(), reply_markup=_settings_unsaved_keyboard())
        await callback.answer()
        return
    session.view = "menu"
    await callback.message.edit_text(_settings_menu_text(session.draft), reply_markup=_settings_menu_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data == "settings:exit_save")
async def on_settings_exit_save(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.from_user or not callback.message:
        return
    await callback.answer("Сохраняю...")
    if not await _save_settings_session(callback.from_user.id, session, analytics_store):
        await callback.message.edit_text(_settings_save_error_text(session.draft), reply_markup=_settings_unsaved_keyboard())
        return
    session.view = "menu"
    await callback.message.edit_text(_settings_menu_text(session.draft, saved=True), reply_markup=_settings_menu_keyboard(session.draft))


@router.callback_query(F.data == "settings:exit_discard")
async def on_settings_exit_discard(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    session = await _settings_session(callback, analytics_store)
    if not session or not callback.message:
        return
    session.draft = session.saved
    session.view = "menu"
    await callback.message.edit_text(_settings_menu_text(session.draft, discarded=True), reply_markup=_settings_menu_keyboard(session.draft))
    await callback.answer()


@router.callback_query(F.data.startswith("support:reply:"))
async def on_support_reply_click(callback: CallbackQuery, admin_chat_id: int | None) -> None:
    if not _is_admin_callback(callback, admin_chat_id):
        await callback.answer("Недоступно", show_alert=True)
        return
    user_id = int((callback.data or "").rsplit(":", 1)[-1])
    _reply_waiting[callback.from_user.id] = user_id
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        "Напиши ответ пользователю следующим сообщением. /cancel — отменить.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(lambda message: message.from_user and message.from_user.id in _reply_waiting)
async def on_admin_reply(message: Message, admin_chat_id: int | None) -> None:
    if not _is_admin(message, admin_chat_id):
        return
    user_id = _reply_waiting.pop(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст ответа.")
        return
    await message.bot.send_message(user_id, f"Ответ поддержки:\n\n{escape(text)}", reply_markup=_main_keyboard())
    await message.answer("Ответ отправлен.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))


@router.message(lambda message: message.from_user and message.from_user.id in _support_waiting)
async def on_support_text(message: Message, admin_chat_id: int | None) -> None:
    _support_waiting.discard(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст сообщения.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        return
    if extract_download_url(text):
        confirmation = await message.answer(
            "Вы уверены, что хотите отправить это сообщение в поддержку, "
            "или вы хотели скачать фанфик?",
            reply_markup=_support_confirmation_keyboard(),
        )
        _support_pending[message.from_user.id] = PendingSupportMessage(message, confirmation)
        return
    await _send_support_message(message, admin_chat_id, "Ficbook Downloader", text)


async def _clear_support_confirmation(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, TelegramNetworkError, TelegramForbiddenError, TelegramRetryAfter, TelegramServerError):
        logger.warning("Could not clear support confirmation buttons")


async def _delete_support_confirmation(message: Message) -> None:
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramNetworkError, TelegramForbiddenError, TelegramRetryAfter, TelegramServerError):
        logger.warning("Could not delete support confirmation; clearing buttons")
        await _clear_support_confirmation(message)


@router.callback_query(F.data.in_({"support:download", "support:send"}))
async def on_support_decision(
    callback: CallbackQuery,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    premium_queue_user_ids: frozenset[int],
) -> None:
    pending = _support_pending.get(callback.from_user.id)
    if (
        pending is None or callback.message is None
        or callback.message.chat.id != pending.confirmation.chat.id
        or callback.message.message_id != pending.confirmation.message_id
    ):
        await callback.answer("Этот запрос уже обработан или отменён.", show_alert=True)
        return
    # Consume before any await so concurrent clicks cannot repeat the action.
    _support_pending.pop(callback.from_user.id)
    _support_waiting.discard(callback.from_user.id)
    try:
        await callback.answer()
    except (TelegramBadRequest, TelegramNetworkError, TelegramForbiddenError, TelegramRetryAfter, TelegramServerError):
        logger.warning("Could not acknowledge support confirmation")
    await _delete_support_confirmation(pending.confirmation)
    await pending.message.answer(
        "Отправляю сообщение в поддержку." if callback.data == "support:send" else "Готовлю скачивание.",
        reply_markup=_main_keyboard_for_message(pending.message, admin_chat_id),
    )
    if callback.data == "support:send":
        await _send_support_message(pending.message, admin_chat_id, "Ficbook Downloader", (pending.message.text or "").strip())
    else:
        await on_link(
            pending.message,
            ficbook_client=ficbook_client,
            admin_chat_id=admin_chat_id,
            alert_bot=alert_bot,
            analytics_store=analytics_store,
            download_queues=download_queues,
            premium_queue_user_ids=premium_queue_user_ids,
        )


@router.message(lambda message: message.from_user and message.from_user.id in _broadcast_waiting)
async def on_broadcast_text(message: Message, admin_chat_id: int | None, user_store: UserStore) -> None:
    if not _is_admin(message, admin_chat_id):
        return
    _broadcast_waiting.discard(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст для рассылки.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        return
    try:
        sent, failed = await _broadcast(message.bot, user_store, text)
    except (UserStoreError, OSError):
        logger.error('Broadcast registry is unreadable; existing data left intact')
        await message.answer('Список получателей недоступен. Рассылка остановлена, файл пользователей не сброшен.',
                             reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        return
    await message.answer(
        f"Рассылка завершена. Отправлено: {sent}. Ошибок: {failed}.",
        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
    )


@router.message(lambda message: message.from_user and message.from_user.id in _chapter_waiting)
async def on_chapter_selection_text(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    premium_queue_user_ids: frozenset[int],
) -> None:
    pending = _chapter_waiting.get(message.from_user.id)
    if pending is None:
        return
    text = (message.text or "").strip()
    try:
        parsed_chapters = parse_chapter_selection(text, len(pending.preview.chapters))
    except ChapterSelectionError as exc:
        await message.answer(
            f"{escape(str(exc))}\nФормат: 1,2,5-10,17. Для всех глав отправь 0. /cancel — отменить.",
            reply_markup=_cancel_keyboard(),
        )
        return
    chapter_numbers = set(parsed_chapters) if parsed_chapters is not None else None
    _chapter_waiting.pop(message.from_user.id, None)
    await message.answer(
        "Главы выбраны.",
        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
    )
    await _process_download(
        message,
        ficbook_client,
        admin_chat_id,
        alert_bot,
        analytics_store,
        download_queues,
        pending.url,
        pending.formats,
        premium_queue_user_ids,
        cover_enabled=pending.cover_enabled,
        chapter_numbers=chapter_numbers,
        known_title=pending.preview.title,
    )


@router.message(F.text.regexp(URL_RE.pattern))
async def on_link(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    premium_queue_user_ids: frozenset[int],
    initial_status: Message | None = None,
    acknowledgement_attempted: bool = False,
) -> None:
    url = extract_download_url(message.text or "")
    if not url:
        await message.answer(
            "Нужна ссылка на Ficbook, Fikbooks, AO3, Wattpad, Hogwartsnet, RanobeLIB, Взахлёб (zahleb.me) или Litnet.",
            reply_markup=_main_keyboard_for_message(message, admin_chat_id),
        )
        return
    if initial_status is None and not acknowledgement_attempted:
        initial_status = await _acknowledge_link(message)
    if is_google_share_url(url):
        try:
            url = await resolve_share_url(url)
        except ShareLinkError as exc:
            await _replace_status_with_notice(message, initial_status, admin_chat_id, escape(str(exc)))
            return
    settings = (
        await _get_user_download_settings(analytics_store, message.from_user.id)
        if message.from_user
        else UserDownloadSettings()
    )
    formats = settings.formats
    cover_enabled = settings.cover_enabled
    chapter_selection_enabled = settings.chapter_selection_enabled
    if is_ficbook_collection_url(url):
        await _process_collection(
            message, ficbook_client, admin_chat_id, alert_bot, analytics_store,
            download_queues, url=url, formats=formats,
            premium_queue_user_ids=premium_queue_user_ids, cover_enabled=cover_enabled,
            initial_status=initial_status,
        )
        return
    if chapter_selection_enabled and message.from_user:
        await _ask_chapter_selection(
            message,
            ficbook_client,
            admin_chat_id,
            alert_bot,
            url,
            formats,
            cover_enabled,
            download_queues,
            initial_status=initial_status,
        )
        return
    await _process_download(
        message,
        ficbook_client,
        admin_chat_id,
        alert_bot,
        analytics_store,
        download_queues,
        url,
        formats,
        premium_queue_user_ids,
        cover_enabled=cover_enabled,
        initial_status=initial_status,
    )


@router.message(F.text)
async def on_unmatched_text(message: Message, admin_chat_id: int | None) -> None:
    await message.answer('Пришли ссылку на произведение. Для обращения в поддержку открой /help.',
                         reply_markup=_main_keyboard_for_message(message, admin_chat_id))


async def _ask_chapter_selection(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    url: str,
    formats: tuple[str, ...],
    cover_enabled: bool,
    download_queues: DownloadQueuePool | None = None,
    *,
    initial_status: Message | None = None,
) -> None:
    if initial_status is not None:
        try:
            await initial_status.delete()
        except (TelegramBadRequest, TelegramNetworkError, TelegramServerError,
                TelegramForbiddenError, TelegramRetryAfter):
            logger.debug('Could not delete link acknowledgement')
    try:
        status = await _answer_status_with_retry(message,
            _chapter_selection_input_text("Подготавливаю список глав..."),
            reply_markup=_cancel_keyboard(),
        )
    except (FicbookError, TelegramDeliveryError, TelegramForbiddenError):
        logger.warning('Telegram preview status could not be created; preview not started')
        return
    progress = ProgressMessage(status)
    try:
        queues = download_queues or DownloadQueuePool()
        preview = await queues.run_preview(
            lambda: run_worker(ficbook_client.preview, url, progress.from_thread),
            user_id=message.from_user.id if message.from_user else None,
        )
    except (ResourceLimitError, QueueFullError) as exc:
        await _replace_status_with_notice(message, status, admin_chat_id, _resource_error_text(exc))
        return
    except FicbookSiteUnavailableError:
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            "Ficbook сейчас не работает, поэтому список глав получить не удалось.",
            admin_notified=False,
            support=False,
        )
        return
    except FicbookPaidContentError as exc:
        await _replace_status_with_notice(message, status, admin_chat_id, _user_error_message(exc))
        return
    except FicbookNotFoundError as exc:
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            _user_error_message(exc),
            admin_notified=False,
            support=False,
        )
        return
    except FicbookError as exc:
        await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=True)
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            _user_error_message(exc),
        )
        return
    except Exception as exc:
        logger.exception("Unexpected error while previewing Ficbook chapters")
        await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=False)
        await _replace_status_with_error(message, status, admin_chat_id, _developer_error_text())
        return
    finally:
        await progress.close()
    if not preview.chapters:
        await _replace_status_with_error(message, status, admin_chat_id, "Не удалось получить список глав.")
        return
    _chapter_waiting[message.from_user.id] = PendingChapterSelection(preview.source_url, formats, cover_enabled, preview)
    await status.edit_text(_chapter_selection_prompt(preview))


def _job_markup() -> dict[str, Any]:
    job = current_job.get()
    if job is None:
        return {}
    label = 'Остановить сборник' if job.kind == 'collection' else 'Отменить загрузку'
    return {'reply_markup': InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f'download:cancel:{job.token}'),
    ]])}


async def _run_tracked(
    message: Message, url: str, formats: tuple[str, ...], cover_enabled: bool,
    chapter_numbers: set[int] | None, kind: str, initial_status: Message | None,
    operation: Callable[[], Awaitable[bool | None]],
) -> bool | None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    key = download_key(user_id, message.chat.id, url, formats, cover_enabled, chapter_numbers)
    try:
        job, created = _jobs.reserve(key, user_id, message.chat.id, kind)
    except OverflowError:
        await _replace_status_with_notice(message, initial_status, None, 'Сейчас очередь заполнена. Попробуй позже.')
        return None
    if not created:
        await _replace_status_with_notice(
            message, initial_status, None, 'Эта работа уже скачивается или ожидает очереди.\n' + job.text,
        )
        parent = current_job.get()
        if kind == 'work' and parent is not None and parent.kind == 'collection' and job.task is not None:
            try:
                return await asyncio.shield(job.task)
            except asyncio.CancelledError:
                if not asyncio.current_task().cancelling() and job.cancel_requested:
                    return None
                raise
        return None

    async def execute():
        context = current_job.set(job)
        try:
            return await operation()
        finally:
            current_job.reset(context)

    job.task = asyncio.create_task(execute())
    try:
        return await job.task
    except asyncio.CancelledError:
        if not job.cancel_requested:
            raise
        text = ('Сборник остановлен.\n' + job.text if kind == 'collection' else 'Загрузка отменена.')
        await message.answer(text)
        return None
    finally:
        _jobs.release(job)


@router.callback_query(F.data.startswith('download:cancel:'))
async def on_cancel_download(callback: CallbackQuery) -> None:
    if callback.message is None or not isinstance(callback.message, Message):
        await callback.answer('Сообщение недоступно.', show_alert=True)
        return
    token = (callback.data or '').removeprefix('download:cancel:')
    if not _jobs.cancel(token, callback.from_user.id, callback.message.chat.id):
        await callback.answer('Задача уже завершена или недоступна.', show_alert=True)
        return
    await callback.answer('Отмена запрошена. Текущий запрос может завершиться по таймауту.')


@router.callback_query(F.data.startswith('collection:retry:'))
async def on_retry_collection(
    callback: CallbackQuery, ficbook_client: FicbookClient, admin_chat_id: int | None,
    alert_bot: Bot | None, analytics_store: AnalyticsStore, download_queues: DownloadQueuePool,
    premium_queue_user_ids: frozenset[int],
) -> None:
    token = (callback.data or '').removeprefix('collection:retry:')
    pending = _failed_collections.get(token)
    if (pending is None or callback.message is None or not isinstance(callback.message, Message)
            or pending.user_id != callback.from_user.id or pending.chat_id != callback.message.chat.id):
        await callback.answer('Повтор недоступен или срок действия кнопки истёк.', show_alert=True)
        return
    if pending.user_id in _collection_users:
        await callback.answer('Сначала дождись завершения текущего сборника.', show_alert=True)
        return
    # Consume before the first await: repeated presses must not start another batch.
    _failed_collections.pop(token, None)
    try:
        await callback.answer('Повторяю только неудачные работы.')
    except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter):
        _failed_collections[token] = pending
        raise
    await _process_collection(
        pending.message, ficbook_client, admin_chat_id, alert_bot, analytics_store, download_queues,
        pending.collection.source_url, pending.formats, premium_queue_user_ids,
        cover_enabled=pending.cover_enabled, retry_collection=pending.collection,
    )


async def _process_collection(
    message: Message, ficbook_client: FicbookClient, admin_chat_id: int | None, alert_bot: Bot | None,
    analytics_store: AnalyticsStore, download_queues: DownloadQueuePool, url: str,
    formats: tuple[str, ...], premium_queue_user_ids: frozenset[int], *, cover_enabled: bool = True,
    initial_status: Message | None = None, retry_collection: FicbookCollection | None = None,
) -> None:
    await _run_tracked(
        message, url, formats, cover_enabled, None, 'collection', initial_status,
        lambda: _process_collection_impl(
            message, ficbook_client, admin_chat_id, alert_bot, analytics_store, download_queues,
            url, formats, premium_queue_user_ids, cover_enabled=cover_enabled,
            initial_status=initial_status, retry_collection=retry_collection,
        ),
    )


async def _process_collection_impl(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    url: str,
    formats: tuple[str, ...],
    premium_queue_user_ids: frozenset[int],
    *,
    cover_enabled: bool = True,
    initial_status: Message | None = None,
    retry_collection: FicbookCollection | None = None,
) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in _collection_users:
        await _replace_status_with_notice(
            message, initial_status, admin_chat_id, 'У тебя уже скачивается сборник. Дождись его завершения.',
        )
        return
    if len(_collection_users) >= current_limits().max_collection_batches:
        await _replace_status_with_notice(
            message, initial_status, admin_chat_id, 'Сейчас очередь сборников заполнена. Попробуй позже.',
        )
        return
    _collection_users.add(user_id)
    status = initial_status
    progress = None
    try:
        if status is None:
            status = await _answer_status_with_retry(message, 'Получаю список фанфиков из сборника...')
        progress = ProgressMessage(status, min_interval=5.0)
        await progress.set('Получаю список фанфиков из сборника...', force=True)

        def report_discovery(text: str) -> None:
            # Do not let a quick next page overwrite an unread milestone.
            future = progress.from_thread(text, force=True)
            try:
                future.result(timeout=10)
            except Exception:
                future.cancel()
                logger.warning('Could not deliver collection discovery progress')

        collection = retry_collection or await download_queues.run_preview(
            lambda: run_worker(ficbook_client.collection, url, report_discovery), user_id=user_id,
        )
        total = len(collection.urls)
        if not total:
            await progress.close()
            await _replace_status_with_notice(message, status, admin_chat_id, 'Этот сборник пуст.')
            return
        sent = 0
        cancelled = 0
        failed = []
        title = escape(collection.title)
        await progress.set(f'Сборник: {title}\nОбработано 0 из {total}.\nОтправлено: 0.', force=True)
        for index, work_url in enumerate(collection.urls, 1):
            success = await _process_download(
                message, ficbook_client, admin_chat_id, alert_bot, analytics_store,
                download_queues, work_url, formats, premium_queue_user_ids,
                cover_enabled=cover_enabled, raise_forbidden=True,
            )
            if success is None:
                cancelled += 1
            elif success:
                sent += 1
            else:
                failed.append(work_url)
            await progress.set(
                f'Сборник: {title}\nОбработано {index} из {total}.\nОтправлено: {sent}.', force=True,
            )
        await progress.close()
        summary = (f'Сборник: {title}\nОбработано {total} из {total}.\nОтправлено: {sent}.'
                   f'\nНе удалось: {len(failed)}.\nОтменено: {cancelled}.')
        job = current_job.get()
        if job is not None:
            job.cancel_allowed = False
        if failed:
            token = token_urlsafe(9)
            _failed_collections[token] = FailedCollection(
                message, FicbookCollection(collection.title, url, tuple(failed)), formats,
                cover_enabled, user_id, message.chat.id,
            )
            try:
                await status.delete()
            except (TelegramBadRequest, TelegramNetworkError, TelegramServerError, TelegramRetryAfter):
                logger.debug('Could not remove completed collection status')
            await message.answer(summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Повторить неудачные', callback_data=f'collection:retry:{token}'),
            ]]))
        else:
            await _replace_status_with_notice(message, status, admin_chat_id, summary)
    except asyncio.CancelledError:
        if progress is not None:
            await progress.close()
        if status is not None:
            try:
                await status.delete()
            except (TelegramBadRequest, TelegramNetworkError, TelegramServerError, TelegramRetryAfter,
                    TelegramForbiddenError):
                logger.debug('Could not remove cancelled collection status')
        raise
    except TelegramForbiddenError:
        logger.info('Collection download stopped because Telegram access was revoked')
    except (CollectionError, ResourceLimitError, QueueFullError, FicbookError, TelegramDeliveryError) as exc:
        if progress is not None:
            await progress.close()
        await _replace_status_with_notice(message, status, admin_chat_id, _resource_error_text(exc))
    except Exception as exc:
        logger.exception('Unexpected error while processing Ficbook collection')
        await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=False)
        await _replace_status_with_error(message, status, admin_chat_id, _developer_error_text())
    finally:
        if progress is not None:
            await progress.close()
        _collection_users.discard(user_id)


async def _process_download(
    message: Message, ficbook_client: FicbookClient, admin_chat_id: int | None, alert_bot: Bot | None,
    analytics_store: AnalyticsStore, download_queues: DownloadQueuePool, url: str,
    formats: tuple[str, ...], premium_queue_user_ids: frozenset[int], *, cover_enabled: bool = True,
    chapter_numbers: set[int] | None = None, raise_forbidden: bool = False,
    initial_status: Message | None = None,
    known_title: str = '',
    redelivery: Redelivery | None = None,
) -> bool | None:
    return await _run_tracked(
        message, url, formats, cover_enabled, chapter_numbers, 'work', initial_status,
        lambda: _process_download_impl(
            message, ficbook_client, admin_chat_id, alert_bot, analytics_store, download_queues,
            url, formats, premium_queue_user_ids, cover_enabled=cover_enabled, chapter_numbers=chapter_numbers,
            raise_forbidden=raise_forbidden, initial_status=initial_status, known_title=known_title,
            redelivery=redelivery,
        ),
    )


async def _process_download_impl(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    analytics_store: AnalyticsStore,
    download_queues: DownloadQueuePool,
    url: str,
    formats: tuple[str, ...],
    premium_queue_user_ids: frozenset[int],
    *,
    cover_enabled: bool = True,
    chapter_numbers: set[int] | None = None,
    raise_forbidden: bool = False,
    initial_status: Message | None = None,
    known_title: str = '',
    redelivery: Redelivery | None = None,
) -> bool:
    priority = _is_priority_download_user(message, premium_queue_user_ids)
    assignment = download_queues.assign(url)
    status: Message | None = initial_status
    progress = ProgressMessage(status, min_interval=5.0)
    progress_state = DownloadProgressState(source_url=url)
    progress_state.set_title(known_title)
    download_id = None
    delivered = False
    try:
        download_id = await _start_download(analytics_store, message, url)
        attempted_slots: set[int] = set()
        failover = False
        while True:
            current = assignment
            if current.ficbook_slot is not None:
                attempted_slots.add(current.ficbook_slot)

            async def on_queued(estimate: Any) -> None:
                nonlocal status, progress
                text = _queue_status(
                    formats,
                    estimate.position,
                    estimate.estimated_wait_seconds,
                    ficbook_unavailable=(
                        current.ficbook_slot is not None
                        and download_queues.is_site_unavailable("ficbook.net")
                    ),
                )
                text = progress_state.with_identity(text)
                if status is None or progress is None:
                    status = await _optional_status(message, text)
                    progress = ProgressMessage(status, min_interval=5.0, initial_text=text)
                    return
                await progress.set(text, force=True)

            async def on_started() -> None:
                nonlocal status, progress
                progress_state.start_attempt()
                active_text = progress_state.status(formats)
                if status is None:
                    await progress.close()
                    status = await _optional_status(message, active_text)
                    progress = ProgressMessage(status, min_interval=5.0, initial_text=active_text)
                else:
                    await progress.set(active_text, force=True)

            @bounded_async_operation
            async def download_job() -> tuple[Story, list[tuple[str, bytes]]]:
                if progress is None:
                    raise RuntimeError("Active download status was not created")
                active_progress = progress
                while True:
                    site_unavailable = False

                    def observe_progress(text: str) -> None:
                        progress_state.observe(text)
                        active_progress.from_thread(
                            progress_state.status(formats)
                        )

                    def observe_title(title: str) -> None:
                        progress_state.set_title(title)
                        active_progress.from_thread(progress_state.status(formats), force=True)

                    reporter = ProgressReporter(observe_progress, observe_title)

                    updater = asyncio.create_task(
                        _active_progress_updater(active_progress, progress_state, formats)
                    )
                    try:
                        async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
                            story = await run_worker(
                                ficbook_client.download,
                                url,
                                reporter,
                                chapter_numbers=chapter_numbers,
                                account=current.account,
                            )
                    except FicbookSiteUnavailableError:
                        if current.ficbook_slot is None:
                            raise
                        site_unavailable = True
                    finally:
                        updater.cancel()
                        await asyncio.gather(updater, return_exceptions=True)
                    if not site_unavailable:
                        if current.ficbook_slot is not None:
                            download_queues.mark_site_available("ficbook.net")
                        break
                    download_queues.mark_site_unavailable("ficbook.net")
                    await _wait_for_ficbook_recovery(
                        ficbook_client,
                        download_queues,
                        active_progress,
                        formats,
                        state=progress_state,
                    )
                    progress_state.start_attempt()
                if not cover_enabled:
                    story.cover = None
                progress_state.set_title(story.title)
                progress_state.set_stage('Создаю файлы')
                await active_progress.set(progress_state.status(formats), force=True)
                updater = asyncio.create_task(_active_progress_updater(active_progress, progress_state, formats))
                try:
                    files = await _build_selected_files(story, formats, progress_state, status=active_progress,
                                                        cover_enabled=cover_enabled)
                finally:
                    updater.cancel()
                    await asyncio.gather(updater, return_exceptions=True)
                return story, files

            async def send_completed(result: tuple[Story, list[tuple[str, bytes]]]) -> None:
                nonlocal delivered
                story, files = result
                file_stem = _safe_name(story.title)
                if progress is None:
                    raise RuntimeError('Download status message is missing')
                if not await progress.set_required(progress_state.with_identity(_sending_status(formats)), wait_for_throttle=False):
                    logger.debug('Sending completed download without a final status edit')
                await progress.close()
                pending_files = [(fmt, payload) for fmt, payload in files
                                 if redelivery is None or fmt not in redelivery.sent_formats]
                for index, (fmt, payload) in enumerate(pending_files, 1):
                    caption = f'{escape(story.title)} ({fmt.upper()})'
                    if redelivery is not None and index == len(pending_files):
                        caption = redelivery.notice
                    await _answer_document_with_retry(
                        message, payload, filename=f'{file_stem}.{fmt}',
                        caption=caption,
                        reply_markup=(_main_keyboard_for_message(message, admin_chat_id)
                                      if index == len(pending_files) else None),
                    )
                    if redelivery is not None:
                        await redelivery.record_sent(fmt)
                delivered = True
                job = current_job.get()
                if job is not None:
                    job.cancel_allowed = False
                finish = asyncio.create_task(_finish_download(
                    analytics_store, download_id, success=True, url=story.source_url, title=story.title,
                ))
                try:
                    await asyncio.shield(finish)
                except asyncio.CancelledError:
                    await finish
                    raise
                try:
                    if status is not None:
                        await deliver(status, status.delete)
                except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, TelegramForbiddenError,
                        TelegramDeliveryError):
                    logger.warning('Telegram transient error while deleting completed progress message')
                except TelegramBadRequest:
                    logger.debug('Failed to delete completed progress message')

            level = PREMIUM_PRIORITY if priority else FAILOVER_PRIORITY if failover else 0
            try:
                story, files = await current.queue.run(
                    download_job,
                    on_queued=on_queued,
                    on_started=on_started,
                    queue_update_interval=60.0,
                    priority_level=level,
                    user_id=message.from_user.id if message.from_user else None,
                    continuation=failover,
                    on_completed=send_completed,
                )
                break
            except (FicbookRateLimitError, FicbookLoginError) as exc:
                if current.ficbook_slot is None:
                    raise
                next_assignment = download_queues.failover(
                    current,
                    frozenset(attempted_slots),
                    permanent=isinstance(exc, FicbookLoginError),
                    retry_after=getattr(exc, "retry_after", None),
                )
                if next_assignment is None:
                    raise FicbookError(
                        "Все аккаунты Ficbook сейчас недоступны. Попробуй повторить позже.",
                        technical=getattr(exc, "technical", "") or repr(exc),
                        user_message=(
                            "Авторизация через Ficbook временно недоступна. "
                            "Попробуй повторить позже."
                        ),
                    ) from exc
                assignment = next_assignment
                failover = True
        return True

    except asyncio.CancelledError:
        if not delivered:
            await _discard_download(analytics_store, download_id)
        if status is not None:
            try:
                await status.delete()
            except (TelegramBadRequest, TelegramNetworkError, TelegramServerError, TelegramRetryAfter,
                    TelegramForbiddenError):
                logger.debug('Could not remove cancelled download status')
        raise
    except TelegramForbiddenError:
        await _finish_download(analytics_store, download_id, success=False, url=url, error='Telegram access revoked')
        if raise_forbidden:
            raise
    except TelegramDeliveryError as exc:
        logger.warning('Telegram file delivery failed: %s', exc)
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        await _replace_status_with_notice(message, status, admin_chat_id,
                                         'Не удалось отправить готовый файл из-за сбоя Telegram. Попробуй позже.')
    except (ResourceLimitError, QueueFullError) as exc:
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        await _replace_status_with_notice(message, status, admin_chat_id, _resource_error_text(exc))
    except FicbookPaidContentError as exc:
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        await _replace_status_with_notice(message, status, admin_chat_id, _user_error_message(exc))
    except FicbookNotFoundError as exc:
        await _discard_download(analytics_store, download_id)
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            _user_error_message(exc),
            admin_notified=False,
            support=False,
        )
    except FicbookError as exc:
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        if redelivery is None:
            await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=True,
                                retry_store=getattr(analytics_store, 'alert_retries', None),
                                formats=formats, cover_enabled=cover_enabled, chapter_numbers=chapter_numbers)
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            _user_error_message(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error while processing Ficbook link")
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        if redelivery is None:
            await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=False,
                                retry_store=getattr(analytics_store, 'alert_retries', None),
                                formats=formats, cover_enabled=cover_enabled, chapter_numbers=chapter_numbers)
        await _replace_status_with_error(message, status, admin_chat_id, _developer_error_text())
    finally:
        if progress is not None:
            await progress.close()
    return False


def _resource_error_text(error: Exception) -> str:
    return ('Сейчас очередь заполнена. Попробуй отправить ссылку немного позже.'
            if isinstance(error, QueueFullError) else escape(str(error)))


async def _answer_status_with_retry(message: Message, text: str, **kwargs: Any) -> Message:
    job = current_job.get()
    if job is not None:
        job.text = text
        if 'reply_markup' not in kwargs:
            kwargs.update(_job_markup())
    for attempt in range(TELEGRAM_SEND_ATTEMPTS):
        try:
            return await deliver(message, lambda: message.answer(text, **kwargs))
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            delay = exc.retry_after + 1 if isinstance(exc, TelegramRetryAfter) else TELEGRAM_SEND_RETRY_DELAY_SECONDS
            if attempt + 1 == TELEGRAM_SEND_ATTEMPTS:
                raise TelegramDeliveryError('Telegram status send attempts exhausted') from None
            logger.warning('Telegram status send failed; retrying (%s/%s)', attempt + 2, TELEGRAM_SEND_ATTEMPTS)
            if not isinstance(exc, TelegramRetryAfter):
                await asyncio.sleep(delay)
    raise RuntimeError('Unreachable Telegram retry state')


async def _acknowledge_link(message: Message) -> Message | None:
    identity = _download_identity(url=extract_download_url(message.text or '') or '')
    text = 'Ссылка получена. Подготавливаю загрузку...'
    return await _optional_status(message, text + '\n' + identity if identity else text)


async def _optional_status(message: Message, text: str) -> Message | None:
    job = current_job.get()
    if job is not None:
        job.text = text
    try:
        return await deliver(message, lambda: message.answer(text, **_job_markup()), optional=True)
    except (DeliveryDeferred, TelegramNetworkError, TelegramServerError, TelegramRetryAfter,
            TelegramForbiddenError, TelegramBadRequest, TelegramDeliveryError):
        return None


async def _active_progress_updater(
    progress: ProgressMessage,
    state: DownloadProgressState,
    formats: tuple[str, ...],
) -> None:
    while True:
        await asyncio.sleep(ACTIVE_PROGRESS_UPDATE_SECONDS)
        await progress.set(state.status(formats))


async def _answer_document_with_retry(
    message: Message,
    payload: bytes,
    *,
    filename: str,
    caption: str,
    reply_markup: Any,
) -> None:
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        try:
            await deliver(message, lambda: message.answer_document(
                BufferedInputFile(payload, filename=filename),
                caption=caption,
                reply_markup=reply_markup,
            ))
            return
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            if attempt >= TELEGRAM_SEND_ATTEMPTS:
                raise TelegramDeliveryError('Telegram file send attempts exhausted: ' + type(exc).__name__) from None
            logger.warning(
                "Telegram %s while sending file, retrying attempt %s/%s",
                type(exc).__name__,
                attempt + 1,
                TELEGRAM_SEND_ATTEMPTS,
            )
            if not isinstance(exc, TelegramRetryAfter):
                await asyncio.sleep(TELEGRAM_SEND_RETRY_DELAY_SECONDS)


async def _wait_for_ficbook_recovery(
    client: FicbookClient,
    queues: DownloadQueuePool,
    progress: ProgressMessage,
    formats: tuple[str, ...],
    *, state: DownloadProgressState | None = None,
) -> None:
    waiting_text = _queue_status(formats, 1, 0, ficbook_unavailable=True)
    await progress.set(
        state.with_identity(waiting_text) if state is not None else waiting_text,
        force=True,
    )
    while True:
        await asyncio.sleep(FICBOOK_SITE_RECHECK_SECONDS)
        check_deadline()
        status = await run_worker(client.ficbook_site_status)
        if status is FicbookSiteStatus.UNAVAILABLE:
            continue
        queues.mark_site_available("ficbook.net")
        if status is FicbookSiteStatus.CHALLENGED:
            raise FicbookError(
                "Cloudflare заблокировал проверку доступности Ficbook. "
                "Не удалось подтвердить восстановление сайта. Попробуй повторить позже.",
                technical="Ficbook recovery probe received a Cloudflare challenge",
            )
        if status is not FicbookSiteStatus.AVAILABLE:
            raise FicbookError(
                "Не удалось проверить доступность Ficbook. Это не подтверждает сбой сайта. "
                "Попробуй повторить скачивание позже.",
                technical="Ficbook recovery probe returned an unknown status",
            )
        active_text = _active_download_status(formats, 0)
        await progress.set(state.with_identity(active_text) if state is not None else active_text, force=True)
        return


async def _replace_status_with_error(
    message: Message,
    status: Message | None,
    admin_chat_id: int | None,
    error: str,
    *,
    admin_notified: bool = True,
    support: bool = True,
) -> None:
    if status is not None:
        try:
            await status.delete()
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, TelegramForbiddenError):
            logger.warning(
                "Telegram transient error while deleting progress message before error response",
                exc_info=True,
            )
        except TelegramBadRequest:
            logger.debug("Failed to delete progress message before error response", exc_info=True)
    reply_markup = _support_keyboard() if support else _main_keyboard_for_message(message, admin_chat_id)
    await _answer_notice_safely(message, _download_error_text(error, admin_notified=admin_notified),
                                reply_markup=reply_markup)


async def _replace_status_with_notice(
    message: Message,
    status: Message | None,
    admin_chat_id: int | None,
    text: str,
) -> None:
    if status is not None:
        try:
            await status.delete()
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, TelegramForbiddenError):
            logger.warning(
                "Telegram transient error while deleting progress message before notice",
                exc_info=True,
            )
        except TelegramBadRequest:
            logger.debug("Failed to delete progress message before notice", exc_info=True)
    await _answer_notice_safely(message, text, reply_markup=_main_keyboard_for_message(message, admin_chat_id))


async def _answer_notice_safely(message: Message, text: str, **kwargs: Any) -> None:
    try:
        await _answer_status_with_retry(message, text, **kwargs)
    except (FicbookError, TelegramDeliveryError, TelegramForbiddenError, TelegramBadRequest):
        logger.warning('Telegram error notice could not be delivered')


def _user_error_message(error: Exception) -> str:
    message = str(getattr(error, "user_message", "") or error)
    lowered = message.lower()
    private_markers = (".env", "_login", "_password", "логин и пароль", "login and password")
    if any(marker in lowered for marker in private_markers):
        return "Авторизация на сайте не сработала."
    return message


def _safe_name(title: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._- " else "_" for char in title).strip()
    return cleaned[:96] or "ficbook"


def _ficbook_accounts(settings: Settings) -> tuple[FicbookAccount, ...]:
    pairs = (
        (settings.ficbook_login, settings.ficbook_password, "FICBOOK_LOGIN"),
        (settings.ficbook_backup_login, settings.ficbook_backup_password, "FICBOOK_BACKUP_LOGIN"),
        (settings.ficbook_account_3_login, settings.ficbook_account_3_password, "FICBOOK_ACCOUNT_3_LOGIN"),
        (settings.ficbook_account_4_login, settings.ficbook_account_4_password, "FICBOOK_ACCOUNT_4_LOGIN"),
        (settings.ficbook_account_5_login, settings.ficbook_account_5_password, "FICBOOK_ACCOUNT_5_LOGIN"),
    )
    accounts = [
        account
        for login, password, label in pairs
        if (account := _account_pair(login, password, label)) is not None
    ]
    return tuple(accounts) or (FicbookAccount(),)


def _account_pair(login: str, password: str, label: str) -> FicbookAccount | None:
    if bool(login) != bool(password):
        raise RuntimeError(f"{label} and its password must be filled together")
    return FicbookAccount(login, password) if login else None


def _optional_account(login: str, password: str, label: str) -> tuple[FicbookAccount, ...]:
    account = _account_pair(login, password, label)
    return (account,) if account else (FicbookAccount(),)


def _site_accounts(settings: Settings) -> dict[str, tuple[FicbookAccount, ...]]:
    return {
        "archiveofourown.org": _optional_account(settings.ao3_login, settings.ao3_password, "AO3_LOGIN"),
        "www.wattpad.com": _optional_account(settings.wattpad_login, settings.wattpad_password, "WATTPAD_LOGIN"),
        "hogwartsnet.ru": _optional_account(
            settings.hogwartsnet_login,
            settings.hogwartsnet_password,
            "HOGWARTSNET_LOGIN",
        ),
    }


async def run_bot(settings: Settings) -> None:
    limits = ResourceLimits.from_environment()
    for store in _session_stores:
        values = store._values if isinstance(store, ExpiringSet) else store
        values.ttl_seconds = limits.session_ttl_seconds
        values.max_entries = limits.max_sessions
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    alert_bot = (
        Bot(settings.alert_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        if settings.alert_bot_token
        else None
    )
    analytics_store = await asyncio.to_thread(
        AnalyticsStore, settings.database_url, history_retention_days=settings.history_retention_days
    )
    ficbook_accounts = _ficbook_accounts(settings)
    _account_pair(settings.litnet_login, settings.litnet_password, "LITNET_LOGIN")
    _account_pair(settings.ranobelib_login, settings.ranobelib_password, "RANOBELIB_LOGIN")
    dispatcher = create_dispatcher(
        FicbookClient(
            accounts=ficbook_accounts,
            site_accounts=_site_accounts(settings),
            request_delay=settings.ficbook_request_delay_seconds,
            retry_attempts=settings.ficbook_retry_attempts,
            retry_base_delay=settings.ficbook_retry_base_delay_seconds,
            retry_max_delay=settings.ficbook_retry_max_delay_seconds,
            litnet_login=settings.litnet_login,
            litnet_password=settings.litnet_password,
            ranobelib_login=settings.ranobelib_login,
            ranobelib_password=settings.ranobelib_password,
        ),
        settings.admin_chat_id,
        alert_bot,
        UserStore(settings.data_dir / "users.json"),
        analytics_store,
        DownloadQueuePool(
            ficbook_accounts=ficbook_accounts,
            min_start_interval=settings.ficbook_download_interval_seconds,
            account_cooldown_seconds=settings.ficbook_retry_max_delay_seconds or 60.0,
            max_jobs=limits.max_queue_jobs,
            max_active=limits.max_active_jobs,
        ),
        settings.premium_queue_user_ids,
    )
    metrics_task = asyncio.create_task(
        run_metrics_server(analytics_store, settings.metrics_host, settings.metrics_port)
    )
    retention_task = asyncio.create_task(run_history_retention(analytics_store))
    sessions_task = asyncio.create_task(_expire_sessions())

    async def redownload(message: Message, request: AlertRetry, delivery: Redelivery) -> bool | None:
        return await _process_download(
            message, dispatcher['ficbook_client'], settings.admin_chat_id, alert_bot,
            analytics_store, dispatcher['download_queues'], request.url, request.formats,
            settings.premium_queue_user_ids, cover_enabled=request.cover_enabled,
            chapter_numbers=set(request.chapter_numbers) if request.chapter_numbers is not None else None,
            redelivery=delivery,
        )

    redelivery = AlertRedelivery(bot, settings.admin_chat_id, analytics_store.alert_retries, redownload)
    dispatcher['alert_redelivery'] = redelivery
    try:
        await _set_bot_commands(bot, settings.admin_chat_id)
        await poll_bots(dispatcher, bot, redelivery, alert_bot,
                        tasks_concurrency_limit=limits.max_queue_jobs + 64)
    finally:
        await redelivery.close()
        metrics_task.cancel()
        retention_task.cancel()
        sessions_task.cancel()
        await asyncio.gather(metrics_task, retention_task, sessions_task, return_exceptions=True)
        await asyncio.to_thread(close_download_workers)
        await asyncio.to_thread(analytics_store.close)
        await bot.session.close()
        if alert_bot:
            await alert_bot.session.close()


def _is_admin(message: Message, admin_chat_id: int | None) -> bool:
    return bool(admin_chat_id and message.from_user and message.from_user.id == admin_chat_id)


async def _set_bot_commands(bot: Bot, admin_chat_id: int | None) -> None:
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="settings", description="Настройки скачивания"),
        BotCommand(command="help", description="Написать в поддержку"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    await bot.set_my_commands(commands)
    if admin_chat_id:
        try:
            await bot.set_my_commands(
                [
                    *commands,
                    BotCommand(command="broadcast", description="Рассылка пользователям"),
                    BotCommand(command="queue", description="Количество фанфиков в очереди"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_chat_id),
            )
        except TelegramBadRequest as exc:
            logger.warning("Failed to set admin bot commands for chat %s: %s", admin_chat_id, exc)


def _is_admin_callback(callback: CallbackQuery, admin_chat_id: int | None) -> bool:
    return bool(admin_chat_id and callback.from_user and callback.from_user.id == admin_chat_id)


def _is_priority_download_user(message: Message, premium_queue_user_ids: frozenset[int]) -> bool:
    return bool(message.from_user and message.from_user.id in premium_queue_user_ids)


async def _send_support_message(message: Message, admin_chat_id: int | None, bot_name: str, text: str) -> None:
    if admin_chat_id is None or not message.from_user:
        await message.answer(
            "Поддержка сейчас недоступна.",
            reply_markup=_main_keyboard_for_message(message, admin_chat_id),
        )
        return
    user = message.from_user
    username = f"@{user.username}" if user.username else user.full_name
    payload = (
        f"<b>Сообщение в поддержку: {escape(bot_name)}</b>\n"
        f"<b>User:</b> {escape(username)}\n"
        f"<b>User ID:</b> {user.id}\n\n"
        f"{escape(text)}"
    )
    await message.bot.send_message(admin_chat_id, payload, reply_markup=_reply_keyboard(user.id))
    await message.answer(
        "Сообщение отправлено. Я пришлю ответ здесь.",
        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
    )


async def _get_user_download_settings(analytics_store: AnalyticsStore, user_id: int) -> UserDownloadSettings:
    try:
        return await asyncio.to_thread(analytics_store.get_user_download_settings, user_id)
    except Exception:
        logger.exception("Failed to load user download settings")
        return UserDownloadSettings()


async def _settings_session(callback: CallbackQuery, analytics_store: AnalyticsStore) -> SettingsSession | None:
    if not callback.from_user:
        return None
    session = _settings_drafts.get(callback.from_user.id)
    if session and (session.message_id is None or (callback.message is not None
            and callback.message.message_id == session.message_id
            and callback.message.chat.id == session.chat_id)):
        return session
    await callback.answer('Эти настройки устарели. Открой /settings заново.', show_alert=True)
    return None


async def _save_settings_session(user_id: int, session: SettingsSession, analytics_store: AnalyticsStore) -> bool:
    try:
        await asyncio.to_thread(
            analytics_store.save_user_download_settings,
            user_id,
            UserDownloadSettings(
                session.draft.formats,
                session.draft.chapter_selection_enabled,
                session.draft.cover_enabled,
            ),
        )
    except Exception:
        logger.exception("Failed to save user settings")
        return False
    session.saved = session.draft
    return True


def _chapter_selection_prompt(preview: StoryPreview) -> str:
    lines = [
        f"<b>{escape(preview.title)}</b>",
        f"Глав: {len(preview.chapters)}.",
        _chapter_selection_input_text(),
        "",
    ]
    visible = preview.chapters[:40]
    lines.extend(f"{chapter.index}. {escape(chapter.title)}" for chapter in visible)
    if len(preview.chapters) > len(visible):
        lines.append(f"...и еще {len(preview.chapters) - len(visible)} глав.")
    return "\n".join(lines)


@bounded_async_operation
async def _build_selected_files(
    story: Story,
    formats: tuple[str, ...],
    progress: DownloadProgressState,
    *, status: ProgressMessage | None = None, cover_enabled: bool = True,
) -> list[tuple[str, bytes]]:
    builders = {
        "fb2": build_fb2,
        "epub": build_epub,
        "txt": build_txt,
        "docx": build_docx,
        "pdf": build_pdf,
    }
    files: list[tuple[str, bytes]] = []
    validate_story(story)
    if any(fmt != 'txt' for fmt in formats):
        cover = await run_worker(cover_for_story, story, enabled=cover_enabled)
        story = replace(story, cover=cover)
        validate_story(story)
    limits = current_limits()
    output_size = 0
    total = max(1, len(formats))
    for index, fmt in enumerate(formats, 1):
        progress.set_stage(f'Создаю {fmt.upper()} ({index}/{len(formats)})')
        if status is not None:
            await status.set(progress.status(formats), force=True)
        progress.set_actual(80 + round(index / total * 15))
        check_deadline()
        payload = await run_worker(builders[fmt], story)
        check_deadline()
        output_size += len(payload)
        if len(payload) > limits.max_file_bytes or output_size > limits.max_output_bytes:
            raise ResourceLimitError('Готовые файлы слишком большие. Выбери меньше глав или форматов.')
        files.append((fmt, payload))
    return files


async def _broadcast(bot: Bot, user_store: UserStore, text: str) -> tuple[int, int]:
    sent = 0
    failed = 0
    payload = escape(text)
    for user_id in await asyncio.to_thread(user_store.list_user_ids):
        try:
            await bot.send_message(user_id, payload)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            try:
                await asyncio.to_thread(user_store.remove, user_id)
            except (UserStoreError, OSError):
                logger.error('Could not update broadcast registry; existing data left intact')
            failed += 1
        except Exception:
            logger.exception("Failed to send broadcast to user %s", user_id)
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


async def _record_user(analytics_store: AnalyticsStore, user: Any, *, started: bool = False) -> None:
    try:
        await asyncio.to_thread(analytics_store.record_user, user, started=started)
    except Exception:
        logger.exception("Failed to record user analytics")


async def _start_download(analytics_store: AnalyticsStore, message: Message, url: str) -> int | None:
    if not message.from_user:
        return None
    insert = asyncio.create_task(asyncio.to_thread(analytics_store.start_download, message.from_user.id, url))
    try:
        return await asyncio.shield(insert)
    except asyncio.CancelledError:
        # The INSERT can commit after its awaiting task is cancelled.
        try:
            download_id = await insert
        except Exception:
            logger.exception('Failed to settle cancelled download attempt')
        else:
            await _discard_download(analytics_store, download_id)
        raise
    except Exception:
        logger.exception("Failed to record download attempt")
        return None


async def _finish_download(
    analytics_store: AnalyticsStore,
    download_id: int | None,
    *,
    success: bool,
    url: str,
    title: str = "",
    error: str = "",
) -> None:
    if download_id is None:
        return
    try:
        await asyncio.to_thread(
            analytics_store.finish_download,
            download_id,
            success=success,
            url=url,
            title=title,
            error=error,
        )
    except Exception:
        logger.exception("Failed to finish download analytics")


async def _discard_download(analytics_store: AnalyticsStore, download_id: int | None) -> None:
    if download_id is None:
        return
    try:
        await asyncio.to_thread(analytics_store.discard_download, download_id)
    except Exception:
        logger.exception("Failed to discard download analytics")


async def _notify_admin(
    message: Message,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    url: str,
    error: Exception,
    *,
    expected: bool,
    retry_store: AlertRetryStore | None = None,
    formats: tuple[str, ...] | None = None,
    cover_enabled: bool = True,
    chapter_numbers: set[int] | None = None,
) -> None:
    if admin_chat_id is None:
        return
    user = message.from_user
    user_name = (user.username or user.full_name) if user else "unknown"
    error_kind = "Handled Ficbook error" if expected else "Unhandled bot error"
    technical = getattr(error, "technical", "")
    if technical:
        cause = error.__cause__
        status_code = getattr(cause, 'status_code', None)
        if isinstance(status_code, int):
            technical = f'{type(cause).__name__}: HTTP {status_code}'
        logger.warning("Handled user error details: %s", technical[:500])
    text = (
        f"<b>{error_kind}</b>\n"
        f"<b>User:</b> {escape(user_name)}\n"
        f"<b>User ID:</b> {user.id if user else 'unknown'}\n"
        f"<b>Chat ID:</b> {message.chat.id}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Error:</b> {escape(str(error))}"
    )
    sender = alert_bot or message.bot
    token = None
    if retry_store is not None and formats and user:
        try:
            token = await asyncio.to_thread(
                retry_store.create, user_id=user.id, chat_id=message.chat.id, url=_public_story_url(url),
                formats=formats, cover_enabled=cover_enabled, chapter_numbers=chapter_numbers,
                bot_id=sender.id, admin_chat_id=admin_chat_id,
            )
        except Exception:
            logger.warning('Could not save admin redelivery request; sending alert without button')
    try:
        kwargs = {'reply_markup': retry_keyboard(token)} if token else {}
        alert = await sender.send_message(admin_chat_id, text, **kwargs)
        if token is not None:
            await asyncio.to_thread(retry_store.bind_message, token, alert.message_id)
    except Exception:
        logger.exception("Failed to notify admin about user error")
