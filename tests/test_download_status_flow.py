from __future__ import annotations

import asyncio
from types import SimpleNamespace
from time import sleep
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import ReplyKeyboardMarkup

from src.bot.app import _process_download
from src.core.download_queue import DownloadQueuePool
from src.core.models import Story
from src.sources.ficbook import FicbookPaidContentError, FicbookSiteStatus, FicbookSiteUnavailableError


class SentMessage:
    def __init__(
        self,
        text: str,
        events: list[str],
        reject_final: bool = False,
        *,
        reply_markup: object = None,
    ) -> None:
        self.text = text
        self.events = events
        self.reject_final = reject_final
        self.reply_markup = reply_markup
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        if isinstance(self.reply_markup, ReplyKeyboardMarkup):
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text=text),
                message="Bad Request: message can't be edited",
            )
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
        self.documents: list[dict[str, object]] = []

    async def answer(self, text: str, **kwargs: object) -> SentMessage:
        status = SentMessage(
            text,
            self.events,
            self.reject_final and bool(self.statuses),
            reply_markup=kwargs.get("reply_markup"),
        )
        self.statuses.append(status)
        self.events.append(f"answer:{text}")
        return status

    async def answer_document(self, *_: object, **kwargs: object) -> None:
        self.documents.append(kwargs)
        self.events.append("document")


class FakeClient:
    def download(self, url: str, progress, **_: object) -> Story:
        progress("Скачиваю страницу фанфика")
        sleep(0.03)
        return Story("Test", "Author", url)


class FastClient(FakeClient):
    def download(self, url: str, progress, **_: object) -> Story:
        progress("Скачиваю страницу фанфика")
        return Story("Test", "Author", url)


class HtmlTitleClient(FakeClient):
    def download(self, url: str, progress, **_: object) -> Story:
        progress("Скачиваю страницу фанфика")
        return Story('<hacker kook="taehyung pawn">', "Author", url)


class OutageThenSuccessClient(FakeClient):
    def __init__(self) -> None:
        self.calls = 0

    def download(self, url: str, progress, **_: object) -> Story:
        self.calls += 1
        if self.calls == 1:
            raise FicbookSiteUnavailableError("maintenance")
        return super().download(url, progress)

    def ficbook_site_status(self) -> FicbookSiteStatus:
        return FicbookSiteStatus.AVAILABLE


class PaidLitnetClient(FakeClient):
    def download(self, url: str, progress, **_: object) -> Story:
        raise FicbookPaidContentError(
            "purchase required",
            user_message="Эта книга платная. Бот скачивает только бесплатные книги Litnet.",
        )


class NoopAction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class DownloadStatusFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_telegram_outage_does_not_start_or_repeat_download(self) -> None:
        message = IncomingMessage()
        message.answer = AsyncMock(side_effect=TelegramNetworkError(
            method=EditMessageText(chat_id=1, message_id=1, text='status'), message='timeout',
        ))
        client = FastClient()
        client.download = unittest.mock.Mock(wraps=client.download)
        with patch('src.bot.app.TELEGRAM_SEND_RETRY_DELAY_SECONDS', 0):
            await self._run_download(message, client)
        client.download.assert_not_called()
        self.assertEqual(message.answer.await_count, 6)  # Three status attempts and three error-notice attempts.
        self.assertEqual(message.documents, [])

    async def test_initial_status_timeout_is_retried_without_repeating_download(self) -> None:
        message = IncomingMessage()
        original = message.answer
        client = FastClient()
        client.download = unittest.mock.Mock(wraps=client.download)
        calls = 0

        async def flaky_answer(text, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramNetworkError(method=EditMessageText(chat_id=1, message_id=1, text=text),
                                           message='Request timeout error')
            return await original(text, **kwargs)

        message.answer = flaky_answer
        with patch('src.bot.app.TELEGRAM_SEND_RETRY_DELAY_SECONDS', 0):
            await self._run_download(message, client)
        client.download.assert_called_once()
        self.assertEqual(len(message.documents), 1)

    async def _run_download(
        self,
        message: IncomingMessage,
        client: FakeClient | None = None,
        url: str = "https://archiveofourown.org/works/1",
        *,
        update_interval: float = 0.005,
    ) -> None:
        patches = (
            patch("src.bot.app._start_download", AsyncMock(return_value=None)),
            patch("src.bot.app._finish_download", AsyncMock()),
            patch("src.bot.app._build_selected_files", AsyncMock(return_value=[("txt", b"file")])),
            patch("src.bot.app.ChatActionSender.upload_document", return_value=NoopAction()),
            patch("src.bot.app.ACTIVE_PROGRESS_UPDATE_SECONDS", update_interval),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await _process_download(
                message, client or FakeClient(), None, None, object(), DownloadQueuePool(),
                url, ("txt",), frozenset(),
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

    async def test_fast_download_updates_progress_before_timer_and_sending(self) -> None:
        message = IncomingMessage()

        await self._run_download(message, FastClient(), update_interval=60.0)

        active = message.statuses[1]
        progress_edit = next((text for text in active.edits if "20%" in text), None)
        self.assertIsNotNone(progress_edit)
        final_edit = active.edits[-1]
        self.assertIn("Отправляем...", final_edit)
        self.assertLess(
            message.events.index(f"edit:{progress_edit}"),
            message.events.index(f"edit:{final_edit}"),
        )
        self.assertLess(message.events.index(f"edit:{final_edit}"), message.events.index("document"))

    async def test_status_messages_allow_edits_and_document_restores_keyboard(self) -> None:
        message = IncomingMessage()

        await self._run_download(message)

        queued, active = message.statuses
        self.assertIsNone(queued.reply_markup)
        self.assertIsNone(active.reply_markup)
        self.assertIsInstance(message.documents[-1]["reply_markup"], ReplyKeyboardMarkup)

    async def test_rejected_final_edit_does_not_discard_completed_file(self) -> None:
        message = IncomingMessage(reject_final=True)

        await self._run_download(message)

        self.assertIn("document", message.events)

    async def test_document_caption_escapes_html_title(self) -> None:
        message = IncomingMessage()

        await self._run_download(message, HtmlTitleClient())

        self.assertEqual(
            message.documents[0]["caption"],
            "&lt;hacker kook=&quot;taehyung pawn&quot;&gt; (TXT)",
        )

    async def test_ficbook_outage_keeps_active_job_and_retries_after_recovery(self) -> None:
        message = IncomingMessage()
        client = OutageThenSuccessClient()

        with patch("src.bot.app.FICBOOK_SITE_RECHECK_SECONDS", 0):
            await self._run_download(message, client, "https://ficbook.net/readfic/1")

        self.assertEqual(client.calls, 2)
        self.assertEqual(len(message.statuses), 2)
        active = message.statuses[1]
        self.assertTrue(any("Ficbook сейчас не работает" in text for text in active.edits))
        self.assertTrue(any("Ваш фанфик скачивается" in text for text in active.edits))
        self.assertIn("document", message.events)

    async def test_paid_litnet_book_is_a_notice_without_admin_alert(self) -> None:
        message = IncomingMessage()

        with patch("src.bot.app._notify_admin", AsyncMock()) as notify:
            await self._run_download(
                message,
                PaidLitnetClient(),
                "https://litnet.com/ru/book/paid-b1",
            )

        notify.assert_not_awaited()
        self.assertEqual(
            message.statuses[-1].text,
            "Эта книга платная. Бот скачивает только бесплатные книги Litnet.",
        )
        self.assertNotIn("Админ", message.statuses[-1].text)


if __name__ == "__main__":
    unittest.main()
