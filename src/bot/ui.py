from __future__ import annotations

import re
from html import escape
from math import ceil

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from src.bot.types import SettingsDraft, SettingsSession
from src.exporters.formats import ALLOWED_FORMATS

CHAPTER_DOWNLOAD_RE = re.compile(r"Скачиваю главы: (?P<current>\d+)/(?P<total>\d+)")
WELCOME_TEXT = "\n".join(
    [
        "✨ <b>Привет! Я помогу сохранить фанфик в удобный файл.</b>",
        "",
        "📚 Сейчас доступны Ficbook, AO3, Wattpad, Hogwartsnet, RanobeLIB и бесплатные книги Litnet.",
        "💳 Платные книги Litnet бот не скачивает.",
        "Я скачаю фанфик и отправлю его в выбранных форматах.",
        "🖼 Если включены обложки, добавлю их в поддерживаемые форматы.",
        "✂️ Если включен выбор глав, сначала спрошу, какие главы скачать.",
        "",
        "⚙️ Форматы и обложки можно настроить через /settings.",
        "🆘 Если что-то пошло не так, напиши в поддержку через /help.",
        "",
        "Пришли ссылку, и начнем.",
    ]
)


def main_keyboard(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text="/settings"), KeyboardButton(text="/help")]]
    if is_admin:
        keyboard.append([KeyboardButton(text="/broadcast"), KeyboardButton(text="/queue")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def main_keyboard_for_message(message: Message, admin_chat_id: int | None) -> ReplyKeyboardMarkup:
    return main_keyboard(is_admin=_is_admin_user(message.from_user.id if message.from_user else None, admin_chat_id))


def main_keyboard_for_user(user_id: int, admin_chat_id: int | None) -> ReplyKeyboardMarkup:
    return main_keyboard(is_admin=_is_admin_user(user_id, admin_chat_id))


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="/cancel")]], resize_keyboard=True)


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Написать админу", callback_data="support:write")]]
    )


def reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"support:reply:{user_id}")]]
    )


def developer_error_text() -> str:
    return "Не получилось скачать фанфик из-за внутренней ошибки. Я уже отправил детали разработчику."


def download_error_text(error: str, *, admin_notified: bool = True) -> str:
    lines = [
        f"Ошибка при скачивании: {escape(error.strip())}",
        "",
        "Админ уже получил уведомление и занимается проблемой." if admin_notified else "Попробуй повторить позже.",
    ]
    return "\n".join(lines)


def settings_menu_keyboard(draft: SettingsDraft) -> InlineKeyboardMarkup:
    chapter_text = "✅ Выбор глав" if draft.chapter_selection_enabled else "Выбор глав"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Форматы файлов", callback_data="settings:section:formats")],
            [InlineKeyboardButton(text="Обложка", callback_data="settings:section:cover")],
            [InlineKeyboardButton(text=chapter_text, callback_data="settings:toggle_chapters")],
            [InlineKeyboardButton(text="Сохранить настройки", callback_data="settings:save:menu")],
        ]
    )


def settings_formats_keyboard(draft: SettingsDraft) -> InlineKeyboardMarkup:
    selected = set(draft.formats)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if fmt in selected else ''}{fmt}",
                    callback_data=f"settings:toggle:{fmt}",
                )
                for fmt in ALLOWED_FORMATS
            ],
            [InlineKeyboardButton(text="Выбрать все", callback_data="settings:select_all")],
            [InlineKeyboardButton(text="Сохранить настройки", callback_data="settings:save:formats")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:back")],
        ]
    )


def settings_cover_keyboard(draft: SettingsDraft) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ С обложкой" if draft.cover_enabled else "С обложкой",
                    callback_data="settings:cover:on",
                ),
                InlineKeyboardButton(
                    text="Без обложки" if draft.cover_enabled else "✅ Без обложки",
                    callback_data="settings:cover:off",
                ),
            ],
            [InlineKeyboardButton(text="Сохранить настройки", callback_data="settings:save:cover")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:back")],
        ]
    )


def settings_unsaved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выйти и сохранить", callback_data="settings:exit_save")],
            [InlineKeyboardButton(text="Выйти без сохранения", callback_data="settings:exit_discard")],
        ]
    )


def settings_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="settings:back")]]
    )


def settings_menu_text(draft: SettingsDraft, *, saved: bool = False, discarded: bool = False) -> str:
    lines = [
        "Настройки",
        "",
        f"Форматы: {format_list(draft.formats)}",
        f"Обложка: {'включена' if draft.cover_enabled else 'выключена'}",
        f"Выбор отдельных глав: {'включен' if draft.chapter_selection_enabled else 'выключен'}",
    ]
    if saved:
        lines.append("\nНастройки сохранены.")
    if discarded:
        lines.append("\nВы вышли без сохранения настроек.")
    return "\n".join(lines)


def settings_formats_text(draft: SettingsDraft, *, saved: bool = False) -> str:
    selected = set(draft.formats)
    lines = ["Форматы файлов", ""]
    lines.extend(f"{index}. {'✅ ' if fmt in selected else ''}{fmt}" for index, fmt in enumerate(ALLOWED_FORMATS, 1))
    if saved:
        lines.append("\nНастройки сохранены.")
    return "\n".join(lines)


def settings_cover_text(draft: SettingsDraft, *, saved: bool = False) -> str:
    lines = [
        "Обложка",
        "",
        "Обложка будет добавляться в поддерживаемые форматы." if draft.cover_enabled else "Файлы будут собираться без обложки.",
    ]
    if saved:
        lines.append("\nНастройки сохранены.")
    return "\n".join(lines)


def settings_unsaved_text() -> str:
    return "Вы вышли не сохранив настройки."


def settings_save_error_text(draft: SettingsDraft) -> str:
    return "Не удалось сохранить настройки. Попробуйте позже.\n\n" + settings_menu_text(draft)


def settings_changed(session: SettingsSession) -> bool:
    return session.draft != session.saved


def chapter_selection_input_text(status: str | None = None) -> str:
    lines = [
        "Введите главы, которые хотите скачать, или 0 для всех.",
        "Формат: 1,2,5-10,17.",
        "/cancel — отменить.",
    ]
    if status:
        lines.extend(["", status])
    return "\n".join(lines)


def format_list(formats: tuple[str, ...]) -> str:
    return ", ".join(fmt.upper() for fmt in formats)


def progress_text(text: str, percent: int) -> str:
    clamped = max(0, min(100, percent))
    return f"{_progress_bar(clamped)} {clamped}%\n{text}"


def download_progress_text(text: str) -> str:
    return progress_text(text, download_progress_percent(text))


def queue_status(
    formats: tuple[str, ...],
    position: int,
    estimated_wait: float,
    *,
    ficbook_unavailable: bool = False,
) -> str:
    wait_line = (
        "Примерное ожидание: до восстановления Ficbook."
        if ficbook_unavailable
        else f"Примерное ожидание: {duration_text(estimated_wait)}."
    )
    outage_lines = (
        ["", "Ficbook сейчас не работает.", "Фанфик скачается, как только сайт снова заработает."]
        if ficbook_unavailable
        else []
    )
    return progress_text(
        "\n".join(
            [
                f"Место в очереди: {position}.",
                wait_line,
                *outage_lines,
                "",
                f"Форматы: {format_list(formats)}.",
            ]
        ),
        0,
    )


def active_download_status(formats: tuple[str, ...], percent: int) -> str:
    return progress_text(
        f"Форматы: {format_list(formats)}.\nВаш фанфик скачивается...",
        percent,
    )


def sending_status(formats: tuple[str, ...]) -> str:
    return progress_text(f"Форматы: {format_list(formats)}.\nОтправляем...", 100)


def _is_admin_user(user_id: int | None, admin_chat_id: int | None) -> bool:
    return bool(admin_chat_id and user_id == admin_chat_id)


def _progress_bar(percent: int) -> str:
    filled = percent // 20
    return "🟢" * filled + "⚪️" * (5 - filled)


def download_progress_percent(text: str) -> int:
    if "Скачиваю описание фанфика" in text:
        return 10
    if "Скачиваю страницу фанфика" in text:
        return 20
    match = CHAPTER_DOWNLOAD_RE.search(text)
    if match:
        current = int(match.group("current"))
        total = max(1, int(match.group("total")))
        return min(70, 20 + round(current / total * 50))
    if "429" in text or "ограничил запросы" in text:
        return 10
    return 10


def duration_text(seconds: float) -> str:
    minutes = max(1, ceil(max(0.0, seconds) / 60))
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}"
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"]
    if minutes:
        parts.append(f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


def _plural(value: int, one: str, few: str, many: str) -> str:
    if value % 100 in range(11, 15):
        return many
    if value % 10 == 1:
        return one
    if value % 10 in range(2, 5):
        return few
    return many
