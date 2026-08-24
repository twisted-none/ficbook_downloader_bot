from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import replace
from html import escape
import logging
import re
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
)
from aiogram.utils.chat_action import ChatActionSender

from src.storage.analytics import AnalyticsStore, UserDownloadSettings
from src.bot.types import PendingChapterSelection, SettingsDraft, SettingsSession
from src.bot.ui import (
    active_download_status as _active_download_status,
    cancel_keyboard as _cancel_keyboard,
    chapter_selection_input_text as _chapter_selection_input_text,
    developer_error_text as _developer_error_text,
    download_error_text as _download_error_text,
    download_progress_percent as _download_progress_percent,
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
    WELCOME_TEXT,
)
from src.core.chapter_selection import ChapterSelectionError, parse_chapter_selection
from src.core.config import Settings
from src.core.download_queue import (
    FAILOVER_PRIORITY,
    PREMIUM_PRIORITY,
    DownloadQueuePool,
    QueueAssignment,
)
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
    extract_url,
)
from src.exporters.formats import ALLOWED_FORMATS, build_docx, build_pdf, build_txt, normalize_formats
from src.monitoring.metrics import run_metrics_server
from src.core.models import Story, StoryPreview
from src.storage.users import UserStore

router = Router()
URL_RE = re.compile(r"https?://[^\s]+")
logger = logging.getLogger(__name__)
_broadcast_waiting: set[int] = set()
_support_waiting: set[int] = set()
_reply_waiting: dict[int, int] = {}
_settings_drafts: dict[int, "SettingsSession"] = {}
_chapter_waiting: dict[int, "PendingChapterSelection"] = {}
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
            await message.edit_text(text)
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
                await message.edit_text(text)
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
    def __init__(self, message: Message, *, min_interval: float = 1.0, initial_text: str = "") -> None:
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

    async def set_required(self, text: str) -> bool:
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
                    delay = self._retry_after_until - now
                else:
                    try:
                        await _telegram_edit_gate.edit(self._message, text)
                    except TelegramRetryAfter as exc:
                        retry_after = float(getattr(exc, "retry_after", 60))
                        self._retry_after_until = monotonic() + retry_after + 1.0
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

    def from_thread(self, text: str) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.set(text), self._loop)

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
    def __init__(self) -> None:
        self._lock = Lock()
        self._attempt_started_at = monotonic()
        self._actual_percent = 0
        self._shown_percent = 0

    def start_attempt(self) -> None:
        with self._lock:
            self._attempt_started_at = monotonic()

    def observe(self, text: str) -> None:
        self.set_actual(_download_progress_percent(text))

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
        user_store: UserStore | None = data.get("user_store")
        if user_store and event.from_user:
            user_store.add(event.from_user.id)
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
    _settings_drafts[message.from_user.id] = SettingsSession(saved=draft, draft=draft)
    await message.answer(_settings_menu_text(draft), reply_markup=_settings_menu_keyboard(draft))


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
    if message.from_user.id in _support_waiting:
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
    await _send_support_message(message, admin_chat_id, "Ficbook Downloader", text)


@router.message(lambda message: message.from_user and message.from_user.id in _broadcast_waiting)
async def on_broadcast_text(message: Message, admin_chat_id: int | None, user_store: UserStore) -> None:
    if not _is_admin(message, admin_chat_id):
        return
    _broadcast_waiting.discard(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст для рассылки.", reply_markup=_main_keyboard_for_message(message, admin_chat_id))
        return
    sent, failed = await _broadcast(message.bot, user_store, text)
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
) -> None:
    url = extract_url(message.text or "")
    if not url:
        await message.answer(
            "Нужна ссылка на Ficbook, AO3, Wattpad, Hogwartsnet или Litnet.",
            reply_markup=_main_keyboard_for_message(message, admin_chat_id),
        )
        return
    settings = (
        await _get_user_download_settings(analytics_store, message.from_user.id)
        if message.from_user
        else UserDownloadSettings()
    )
    formats = settings.formats
    cover_enabled = settings.cover_enabled
    chapter_selection_enabled = settings.chapter_selection_enabled
    if chapter_selection_enabled and message.from_user:
        await _ask_chapter_selection(message, ficbook_client, admin_chat_id, alert_bot, url, formats, cover_enabled)
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
    )


async def _ask_chapter_selection(
    message: Message,
    ficbook_client: FicbookClient,
    admin_chat_id: int | None,
    alert_bot: Bot | None,
    url: str,
    formats: tuple[str, ...],
    cover_enabled: bool,
) -> None:
    status = await message.answer(
        _chapter_selection_input_text("Подготавливаю список глав..."),
        reply_markup=_cancel_keyboard(),
    )
    progress = ProgressMessage(status)
    try:
        preview = await asyncio.to_thread(ficbook_client.preview, url, progress.from_thread)
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
    if not preview.chapters:
        await _replace_status_with_error(message, status, admin_chat_id, "Не удалось получить список глав.")
        return
    _chapter_waiting[message.from_user.id] = PendingChapterSelection(preview.source_url, formats, cover_enabled, preview)
    await status.edit_text(_chapter_selection_prompt(preview))


async def _process_download(
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
) -> None:
    priority = _is_priority_download_user(message, premium_queue_user_ids)
    assignment = download_queues.assign(url)
    status: Message | None = None
    progress: ProgressMessage | None = None
    progress_state = DownloadProgressState()
    download_id = await _start_download(analytics_store, message, url)
    try:
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
                if status is None or progress is None:
                    status = await message.answer(
                        text,
                        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
                    )
                    progress = ProgressMessage(status, min_interval=5.0, initial_text=text)
                    return
                await progress.set(text, force=True)

            async def on_started() -> None:
                nonlocal status, progress
                progress_state.start_attempt()
                if progress is not None:
                    await progress.close()
                if status is not None:
                    try:
                        await status.delete()
                    except TelegramBadRequest:
                        logger.debug("Failed to delete queued status message", exc_info=True)
                active_text = _active_download_status(formats, progress_state.percent())
                status = await message.answer(
                    active_text,
                    reply_markup=_main_keyboard_for_message(message, admin_chat_id),
                )
                progress = ProgressMessage(status, min_interval=5.0, initial_text=active_text)

            async def download_job() -> tuple[Story, list[tuple[str, bytes]]]:
                if progress is None:
                    raise RuntimeError("Active download status was not created")
                active_progress = progress
                while True:
                    site_unavailable = False

                    def observe_progress(text: str) -> None:
                        progress_state.observe(text)

                    updater = asyncio.create_task(
                        _active_progress_updater(active_progress, progress_state, formats)
                    )
                    try:
                        async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
                            story = await asyncio.to_thread(
                                ficbook_client.download,
                                url,
                                observe_progress,
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
                    )
                    progress_state.start_attempt()
                if not cover_enabled:
                    story.cover = None
                files = await _build_selected_files(story, formats, progress_state)
                return story, files

            level = PREMIUM_PRIORITY if priority else FAILOVER_PRIORITY if failover else 0
            try:
                story, files = await current.queue.run(
                    download_job,
                    on_queued=on_queued,
                    on_started=on_started,
                    queue_update_interval=60.0,
                    priority_level=level,
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

        await _finish_download(analytics_store, download_id, success=True, url=story.source_url, title=story.title)
        file_stem = _safe_name(story.title)
        final_text = _sending_status(formats)
        if progress is None or status is None:
            raise RuntimeError("Download status message is missing")
        final_status_updated = await progress.set_required(final_text)
        if not final_status_updated:
            logger.warning(
                "Sending completed download after Telegram did not update final status: chat_id=%s",
                message.chat.id,
            )
        for index, (fmt, payload) in enumerate(files, 1):
            await _answer_document_with_retry(
                message,
                payload,
                filename=f"{file_stem}.{fmt}",
                caption=f"{story.title} ({fmt.upper()})",
                reply_markup=(
                    _main_keyboard_for_message(message, admin_chat_id)
                    if index == len(files)
                    else None
                ),
            )
        try:
            await status.delete()
        except (TelegramNetworkError, TelegramServerError):
            logger.warning("Telegram transient error while deleting completed progress message", exc_info=True)
        except TelegramBadRequest:
            logger.debug("Failed to delete completed progress message", exc_info=True)
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
        await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=True)
        await _replace_status_with_error(
            message,
            status,
            admin_chat_id,
            _user_error_message(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error while processing Ficbook link")
        await _finish_download(analytics_store, download_id, success=False, url=url, error=str(exc))
        await _notify_admin(message, admin_chat_id, alert_bot, url, exc, expected=False)
        await _replace_status_with_error(message, status, admin_chat_id, _developer_error_text())


async def _active_progress_updater(
    progress: ProgressMessage,
    state: DownloadProgressState,
    formats: tuple[str, ...],
) -> None:
    while True:
        await asyncio.sleep(ACTIVE_PROGRESS_UPDATE_SECONDS)
        await progress.set(_active_download_status(formats, state.percent()))


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
            await message.answer_document(
                BufferedInputFile(payload, filename=filename),
                caption=caption,
                reply_markup=reply_markup,
            )
            return
        except (TelegramNetworkError, TelegramServerError) as exc:
            if attempt >= TELEGRAM_SEND_ATTEMPTS:
                raise FicbookError(
                    "Telegram API не принял файл после нескольких попыток.",
                    technical=repr(exc),
                    user_message=(
                        "Не удалось отправить готовый файл из-за временного сбоя Telegram. "
                        "Попробуй повторить позже."
                    ),
                ) from exc
            logger.warning(
                "Telegram transient error while sending %s, retrying attempt %s/%s",
                filename,
                attempt + 1,
                TELEGRAM_SEND_ATTEMPTS,
                exc_info=True,
            )
            await asyncio.sleep(TELEGRAM_SEND_RETRY_DELAY_SECONDS)


async def _wait_for_ficbook_recovery(
    client: FicbookClient,
    queues: DownloadQueuePool,
    progress: ProgressMessage,
    formats: tuple[str, ...],
) -> None:
    await progress.set(
        _queue_status(formats, 1, 0, ficbook_unavailable=True),
        force=True,
    )
    while True:
        await asyncio.sleep(FICBOOK_SITE_RECHECK_SECONDS)
        status = await asyncio.to_thread(client.ficbook_site_status)
        if status is not FicbookSiteStatus.AVAILABLE:
            continue
        queues.mark_site_available("ficbook.net")
        await progress.set(_active_download_status(formats, 0), force=True)
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
        except (TelegramNetworkError, TelegramServerError):
            logger.warning(
                "Telegram transient error while deleting progress message before error response",
                exc_info=True,
            )
        except TelegramBadRequest:
            logger.debug("Failed to delete progress message before error response", exc_info=True)
    reply_markup = _support_keyboard() if support else _main_keyboard_for_message(message, admin_chat_id)
    await message.answer(
        _download_error_text(error, admin_notified=admin_notified),
        reply_markup=reply_markup,
    )


async def _replace_status_with_notice(
    message: Message,
    status: Message | None,
    admin_chat_id: int | None,
    text: str,
) -> None:
    if status is not None:
        try:
            await status.delete()
        except (TelegramNetworkError, TelegramServerError):
            logger.warning(
                "Telegram transient error while deleting progress message before notice",
                exc_info=True,
            )
        except TelegramBadRequest:
            logger.debug("Failed to delete progress message before notice", exc_info=True)
    await message.answer(
        text,
        reply_markup=_main_keyboard_for_message(message, admin_chat_id),
    )


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
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    alert_bot = (
        Bot(settings.alert_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        if settings.alert_bot_token
        else None
    )
    analytics_store = AnalyticsStore(settings.database_url)
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
        ),
        settings.premium_queue_user_ids,
    )
    metrics_task = asyncio.create_task(
        run_metrics_server(analytics_store, settings.metrics_host, settings.metrics_port)
    )
    try:
        await _set_bot_commands(bot, settings.admin_chat_id)
        await dispatcher.start_polling(bot)
    finally:
        metrics_task.cancel()
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
    if session:
        return session
    settings = await _get_user_download_settings(analytics_store, callback.from_user.id)
    draft = SettingsDraft(settings.formats, settings.chapter_selection_enabled, settings.cover_enabled)
    session = SettingsSession(saved=draft, draft=draft)
    _settings_drafts[callback.from_user.id] = session
    return session


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


async def _build_selected_files(
    story: Story,
    formats: tuple[str, ...],
    progress: DownloadProgressState,
) -> list[tuple[str, bytes]]:
    builders = {
        "fb2": build_fb2,
        "epub": build_epub,
        "txt": build_txt,
        "docx": build_docx,
        "pdf": build_pdf,
    }
    files: list[tuple[str, bytes]] = []
    total = max(1, len(formats))
    for index, fmt in enumerate(formats, 1):
        progress.set_actual(80 + round(index / total * 15))
        files.append((fmt, await asyncio.to_thread(builders[fmt], story)))
    return files


async def _broadcast(bot: Bot, user_store: UserStore, text: str) -> tuple[int, int]:
    sent = 0
    failed = 0
    payload = escape(text)
    for user_id in user_store.list_user_ids():
        try:
            await bot.send_message(user_id, payload)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            user_store.remove(user_id)
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
    try:
        return await asyncio.to_thread(analytics_store.start_download, message.from_user.id, url)
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
) -> None:
    if admin_chat_id is None:
        return
    user = message.from_user
    user_name = (user.username or user.full_name) if user else "unknown"
    error_kind = "Handled Ficbook error" if expected else "Unhandled bot error"
    technical = getattr(error, "technical", "")
    if technical:
        logger.warning("Handled user error details: %s", technical)
    text = (
        f"<b>{error_kind}</b>\n"
        f"<b>User:</b> {escape(user_name)}\n"
        f"<b>User ID:</b> {user.id if user else 'unknown'}\n"
        f"<b>Chat ID:</b> {message.chat.id}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Error:</b> {escape(str(error))}"
    )
    try:
        await (alert_bot or message.bot).send_message(admin_chat_id, text)
    except Exception:
        logger.exception("Failed to notify admin about user error")
