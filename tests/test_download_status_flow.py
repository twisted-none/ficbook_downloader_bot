from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from src.bot.app import _process_download
from src.core.download_queue import DownloadQueuePool
from src.core.models import Story


class SentMessage:
    def __init__(self, text: str, events: list[str], reject_final: bool = False) -> None:
        self.text = text
        self.events = events
        self.reject_final = reject_final
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        if self.reject_final and "Отправляем..." in text:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text=text),
                message="Bad Request: message to edit not found",
            )
        self.edits.append(text)
        self.events.append(f"edit:{text}")

    async def delete(self) -> None:
        self.deleted = True
        self.events.append("delete")


class IncomingMessage:
    def __init__(self, *, reject_final: bool = False) -> None:
        self.bot = object()
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=1)
        self.reject_final = reject_final
        self.events: list[str] = []
        self.statuses: list[SentMessage] = []

    async def answer(self, text: str, **_: object) -> SentMessage:
        status = SentMessage(text, self.events, self.reject_final and bool(self.statuses))
        self.statuses.append(status)
        self.events.append(f"answer:{text}")
        return status

    async def answer_document(self, *_: object, **__: object) -> None:
        self.events.append("document")


class FakeClient:
    def download(self, url: str, progress, **_: object) -> Story:
        progress("Скачиваю страницу фанфика")
        return Story("Test", "Author", url)


class NoopAction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class DownloadStatusFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run_download(self, message: IncomingMessage) -> None:
        patches = (
            patch("src.bot.app._start_download", AsyncMock(return_value=None)),
            patch("src.bot.app._finish_download", AsyncMock()),
            patch("src.bot.app._build_selected_files", AsyncMock(return_value=[("txt", b"file")])),
            patch("src.bot.app.ChatActionSender.upload_document", return_value=NoopAction()),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await _process_download(
                message, FakeClient(), None, None, object(), DownloadQueuePool(),
                "https://archiveofourown.org/works/1", ("txt",), frozenset(),
            )

    async def test_queue_is_replaced_once_then_active_message_is_only_edited(self) -> None:
        message = IncomingMessage()
        await self._run_download(message)

        queued, active = message.statuses
        self.assertIn("Место в очереди: 1.", queued.text)
        self.assertNotIn("Добавля", queued.text)
        self.assertTrue(queued.deleted)
        self.assertIn("Ваш фанфик скачивается...", active.text)
        self.assertTrue(any("20%" in text for text in active.edits))
        self.assertIn("Отправляем...", active.edits[-1])
        self.assertLess(message.events.index(f"edit:{active.edits[-1]}"), message.events.index("document"))

    async def test_rejected_final_edit_does_not_discard_completed_file(self) -> None:
        message = IncomingMessage(reject_final=True)

        await self._run_download(message)

        self.assertIn("document", message.events)


if __name__ == "__main__":
    unittest.main()
