from __future__ import annotations

import asyncio
from html import escape
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

from src.config import Settings
from src.fb2 import build_fb2
from src.ficbook import FicbookClient, FicbookError, extract_url

router = Router()
URL_RE = re.compile(r"https?://[^\s]+")
logger = logging.getLogger(__name__)


def create_dispatcher(client: FicbookClient, admin_chat_id: int | None) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["ficbook_client"] = client
    dispatcher["admin_chat_id"] = admin_chat_id
    dispatcher.include_router(router)
    return dispatcher


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer("Пришли ссылку на фанфик с Ficbook, и я верну его в формате FB2.")


@router.message(F.text.regexp(URL_RE.pattern))
async def on_link(message: Message, ficbook_client: FicbookClient, admin_chat_id: int | None) -> None:
    url = extract_url(message.text or "")
    if not url:
        await message.answer("Нужна ссылка вида https://ficbook.net/readfic/...")
        return
    status = await message.answer("Скачиваю фанфик и собираю FB2...")
    try:
        async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
            story = await asyncio.to_thread(ficbook_client.download, url)
            payload = await asyncio.to_thread(build_fb2, story)
        file_name = _safe_name(story.title)
        await message.answer_document(BufferedInputFile(payload, filename=file_name), caption=story.title)
        await status.delete()
    except FicbookError as exc:
        await _notify_admin(message, admin_chat_id, url, exc, expected=True)
        await status.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error while processing Ficbook link")
        await _notify_admin(message, admin_chat_id, url, exc, expected=False)
        await status.edit_text("Внутренняя ошибка при обработке ссылки.")


def _safe_name(title: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._- " else "_" for char in title).strip()
    return f"{cleaned[:96] or 'ficbook'}.fb2"


async def run_bot(settings: Settings) -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = create_dispatcher(
        FicbookClient(settings.ficbook_login, settings.ficbook_password),
        settings.admin_chat_id,
    )
    await dispatcher.start_polling(bot)


async def _notify_admin(
    message: Message,
    admin_chat_id: int | None,
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
    text = (
        f"<b>{error_kind}</b>\n"
        f"<b>User:</b> {escape(user_name)}\n"
        f"<b>User ID:</b> {user.id if user else 'unknown'}\n"
        f"<b>Chat ID:</b> {message.chat.id}\n"
        f"<b>URL:</b> {escape(url)}\n"
        f"<b>Error:</b> <code>{escape(str(error))}</code>"
    )
    try:
        await message.bot.send_message(admin_chat_id, text)
    except Exception:
        logger.exception("Failed to notify admin about user error")
