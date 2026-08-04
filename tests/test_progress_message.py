from __future__ import annotations

import unittest
import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText

from src.bot.app import DownloadProgressState, ProgressMessage


class FakeProgressMessage:
    def __init__(self) -> None:
        self.calls = 0

    async def edit_text(self, text: str) -> None:
        self.calls += 1
        raise TelegramRetryAfter(
            method=EditMessageText(chat_id=1, message_id=1, text=text),
            message="Too Many Requests: retry after 81",
            retry_after=81,
        )


class SuccessfulProgressMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)


class RetryOnceProgressMessage:
    def __init__(self) -> None:
        self.calls = 0

    async def edit_text(self, text: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise TelegramRetryAfter(
                method=EditMessageText(chat_id=1, message_id=1, text=text),
                message="Too Many Requests: retry after 0",
                retry_after=0,
            )


class NotModifiedProgressMessage:
    async def edit_text(self, text: str) -> None:
        raise TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=1, text=text),
            message="Bad Request: message is not modified",
        )


class ProgressMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_is_swallowed_and_throttles_next_edits(self) -> None:
        message = FakeProgressMessage()
        progress = ProgressMessage(message, min_interval=0)

        await progress.set("first", force=True)
        await progress.set("second", force=True)

        self.assertEqual(message.calls, 1)

    async def test_fast_updates_are_delayed_instead_of_dropped(self) -> None:
        message = SuccessfulProgressMessage()
        progress = ProgressMessage(message, min_interval=0.01)

        await progress.set("first", force=True)
        await progress.set("second")
        await asyncio.sleep(0.08)

        self.assertEqual(message.texts, ["first", "second"])

    async def test_forced_update_cancels_stale_pending_text(self) -> None:
        message = SuccessfulProgressMessage()
        progress = ProgressMessage(message, min_interval=0.1)

        await progress.set("queue 1", force=True)
        await progress.set("queue 2")
        await progress.set("download", force=True)
        await asyncio.sleep(0.12)

        self.assertEqual(message.texts, ["queue 1", "download"])

    async def test_required_update_waits_for_retry(self) -> None:
        message = RetryOnceProgressMessage()
        progress = ProgressMessage(message, min_interval=0)

        updated = await progress.set_required("100%")
        await progress.set("stale 40%", force=True)

        self.assertTrue(updated)
        self.assertEqual(message.calls, 2)

    async def test_download_progress_never_goes_backwards(self) -> None:
        progress = DownloadProgressState()
        progress.set_actual(80)
        progress.set_actual(20)

        self.assertGreaterEqual(progress.percent(), 80)

    async def test_required_update_accepts_already_applied_text(self) -> None:
        progress = ProgressMessage(NotModifiedProgressMessage(), min_interval=0)

        self.assertTrue(await progress.set_required("100%"))


if __name__ == "__main__":
    unittest.main()
