from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from src.bot import app
from src.storage.analytics import UserDownloadSettings
from src.bot.types import PendingChapterSelection
from src.core.models import ChapterPreview, StoryPreview
from src.sources.share_links import ShareLinkError


class SupportConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app._support_waiting.clear()
        app._support_pending.clear()
        app._chapter_waiting.clear()
        self.prompt = SimpleNamespace(message_id=50, chat=SimpleNamespace(id=10), edit_reply_markup=AsyncMock(), delete=AsyncMock())
        self.message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=10), text="Жалоба: https://ficbook.net/readfic/123",
            from_user=SimpleNamespace(id=10), answer=AsyncMock(return_value=self.prompt),
        )
        self.dependencies = dict(ficbook_client=object(), admin_chat_id=100, alert_bot=None,
                                 analytics_store=object(), download_queues=object(), premium_queue_user_ids=frozenset())

    def tearDown(self):
        app._support_waiting.clear()
        app._support_pending.clear()
        app._chapter_waiting.clear()

    async def test_both_buttons_restore_main_keyboard_before_dispatch(self):
        for action in ("send", "download"):
            with self.subTest(action=action):
                await self.prepare()
                self.message.answer.reset_mock()
                async def dispatched(*args, **kwargs):
                    keyboard = self.message.answer.call_args.kwargs["reply_markup"]
                    labels = [button.text for row in keyboard.keyboard for button in row]
                    self.assertEqual(labels, ["/settings", "/help"])
                with patch.object(app, "_send_support_message", new=AsyncMock(side_effect=dispatched)), patch.object(
                    app, "on_link", new=AsyncMock(side_effect=dispatched)
                ):
                    await app.on_support_decision(self.callback(action), **self.dependencies)

    async def test_chapter_selection_completion_restores_main_keyboard(self):
        app._chapter_waiting[10] = PendingChapterSelection(
            url="https://ficbook.net/readfic/123", formats=("txt",), cover_enabled=False,
            preview=StoryPreview("Title", "Author", "https://ficbook.net/readfic/123", [ChapterPreview(1, "Chapter")]),
        )
        self.message.text = "1"
        with patch.object(app, "_process_download", new_callable=AsyncMock) as download:
            await app.on_chapter_selection_text(self.message, **self.dependencies)
            download.assert_awaited_once()
        keyboard = self.message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual([button.text for row in keyboard.keyboard for button in row], ["/settings", "/help"])

    def callback(self, action, user_id=10, message=None):
        return SimpleNamespace(data="support:" + action, from_user=SimpleNamespace(id=user_id),
                               message=message or self.prompt, answer=AsyncMock())

    async def prepare(self):
        app._support_waiting.add(10)
        await app.on_support_text(self.message, 100)

    async def test_supported_links_require_confirmation_without_sending(self):
        urls = ["https://share.google/Ab123", "https://ficbook.net/readfic/1", "https://archiveofourown.org/works/1",
                "https://www.wattpad.com/story/123", "http://hogwartsnet.ru/mfanf/ffshowfic.php?fid=1",
                "https://litnet.com/ru/book/title-b123", "https://ranobelib.me/ru/123--title"]
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            for url in urls:
                self.message.text = "Жалоба: " + url
                await self.prepare()
                keyboard = self.message.answer.call_args.kwargs["reply_markup"]
                buttons = [button for row in keyboard.inline_keyboard for button in row]
                self.assertEqual([button.text for button in buttons], ["Скачать фанфик", "Отправить в поддержку"])
                self.assertIn(10, app._support_pending)
            send.assert_not_awaited()

    async def test_google_link_uses_resolved_url_and_download_settings(self):
        self.message.text = "https://share.google/Ab123"
        target = "https://ficbook.net/readfic/123"
        for select_chapters in (False, True):
            settings = UserDownloadSettings(formats=("txt",), cover_enabled=False, chapter_selection_enabled=select_chapters)
            with self.subTest(select_chapters=select_chapters), patch.object(
                app, "resolve_share_url", new_callable=AsyncMock, return_value=target
            ) as resolve, patch.object(
                app, "_get_user_download_settings", new_callable=AsyncMock, return_value=settings
            ), patch.object(app, "_process_download", new_callable=AsyncMock) as download, patch.object(
                app, "_ask_chapter_selection", new_callable=AsyncMock
            ) as select:
                await app.on_link(self.message, **self.dependencies)
                resolve.assert_awaited_once_with(self.message.text)
                if select_chapters:
                    select.assert_awaited_once_with(self.message, self.dependencies["ficbook_client"], 100, None,
                                                   target, ("txt",), False, self.dependencies['download_queues'])
                    download.assert_not_awaited()
                else:
                    self.assertEqual(download.call_args.args[6:8], (target, ("txt",)))
                    self.assertFalse(download.call_args.kwargs["cover_enabled"])

    async def test_google_failure_requests_direct_link_without_downloading(self):
        self.message.text = "https://share.google/Ab123"
        with patch.object(app, "resolve_share_url", new_callable=AsyncMock, side_effect=ShareLinkError), patch.object(
            app, "_process_download", new_callable=AsyncMock
        ) as download:
            await app.on_link(self.message, **self.dependencies)
            download.assert_not_awaited()
        self.assertIn("прямую ссылку", self.message.answer.call_args.args[0])

    async def test_support_sends_original_google_link_without_resolving(self):
        self.message.text = "Жалоба: https://share.google/Ab123"
        await self.prepare()
        with patch.object(app, "resolve_share_url", new_callable=AsyncMock) as resolve, patch.object(
            app, "_send_support_message", new_callable=AsyncMock
        ) as send:
            await app.on_support_decision(self.callback("send"), **self.dependencies)
            send.assert_awaited_once_with(self.message, 100, "Ficbook Downloader", self.message.text)
            resolve.assert_not_awaited()

    async def test_plain_text_and_unrelated_links_are_sent_normally(self):
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            for text in ["Спасибо за бота", "Сайт: https://example.org"]:
                self.message.text = text
                await self.prepare()
                send.assert_awaited_with(self.message, 100, "Ficbook Downloader", text)
                self.assertNotIn(10, app._support_pending)

    async def test_support_button_sends_original_text_once(self):
        await self.prepare()
        callback = self.callback("send")
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            await app.on_support_decision(callback, **self.dependencies)
            await app.on_support_decision(callback, **self.dependencies)
            send.assert_awaited_once_with(self.message, 100, "Ficbook Downloader", self.message.text)
        self.prompt.delete.assert_awaited_once()
        self.prompt.edit_reply_markup.assert_not_awaited()

    async def test_download_button_uses_original_sender_and_existing_flow_once(self):
        await self.prepare()
        callback = self.callback("download")
        with patch.object(app, "on_link", new_callable=AsyncMock) as download, patch.object(
            app, "_send_support_message", new_callable=AsyncMock
        ) as send:
            await app.on_support_decision(callback, **self.dependencies)
            await app.on_support_decision(callback, **self.dependencies)
            download.assert_awaited_once_with(self.message, **self.dependencies)
            send.assert_not_awaited()
        self.assertNotIn(10, app._support_waiting)
        self.prompt.delete.assert_awaited_once()

    async def test_failed_deletion_clears_buttons_and_still_dispatches_both_actions(self):
        self.prompt.delete.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=10, message_id=50), message="message can't be deleted"
        )
        for action in ("send", "download"):
            with self.subTest(action=action):
                await self.prepare()
                with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send, patch.object(
                    app, "on_link", new_callable=AsyncMock
                ) as download:
                    await app.on_support_decision(self.callback(action), **self.dependencies)
                    (send if action == "send" else download).assert_awaited_once()
                self.prompt.edit_reply_markup.assert_awaited_with(reply_markup=None)

    async def test_cancel_invalidates_pending_buttons(self):
        await self.prepare()
        await app.on_cancel(self.message, 100)
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            await app.on_support_decision(self.callback("send"), **self.dependencies)
            send.assert_not_awaited()
        self.assertNotIn(10, app._support_pending)

    async def test_concurrent_clicks_only_dispatch_once(self):
        await self.prepare()
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            await asyncio.gather(
                app.on_support_decision(self.callback("send"), **self.dependencies),
                app.on_support_decision(self.callback("send"), **self.dependencies),
            )
            send.assert_awaited_once()

    async def test_download_keeps_user_settings_and_first_supported_link(self):
        self.message.text += " https://archiveofourown.org/works/456"
        await self.prepare()
        settings = UserDownloadSettings(formats=("txt",), cover_enabled=False)
        with patch.object(app, "_get_user_download_settings", new_callable=AsyncMock, return_value=settings) as get_settings, patch.object(
            app, "_process_download", new_callable=AsyncMock
        ) as download:
            await app.on_support_decision(self.callback("download"), **self.dependencies)
            get_settings.assert_awaited_once_with(self.dependencies["analytics_store"], 10)
            self.assertIs(download.call_args.args[0], self.message)
            self.assertEqual(download.call_args.args[6], "https://ficbook.net/readfic/123")
            self.assertEqual(download.call_args.args[7], ("txt",))
            self.assertFalse(download.call_args.kwargs["cover_enabled"])

    async def test_download_keeps_chapter_selection_enabled(self):
        await self.prepare()
        settings = UserDownloadSettings(formats=("epub",), chapter_selection_enabled=True, cover_enabled=False)
        with patch.object(app, "_get_user_download_settings", new_callable=AsyncMock, return_value=settings), patch.object(
            app, "_ask_chapter_selection", new_callable=AsyncMock
        ) as select, patch.object(app, "_process_download", new_callable=AsyncMock) as download:
            await app.on_support_decision(self.callback("download"), **self.dependencies)
            select.assert_awaited_once_with(self.message, self.dependencies["ficbook_client"], 100, None,
                                          "https://ficbook.net/readfic/123", ("epub",), False,
                                          self.dependencies['download_queues'])
            download.assert_not_awaited()

    async def test_foreign_user_and_old_buttons_cannot_consume_new_request(self):
        await self.prepare()
        old = SimpleNamespace(message_id=49, chat=SimpleNamespace(id=10))
        with patch.object(app, "_send_support_message", new_callable=AsyncMock) as send:
            await app.on_support_decision(self.callback("send", user_id=11), **self.dependencies)
            await app.on_support_decision(self.callback("send", message=old), **self.dependencies)
            send.assert_not_awaited()
        self.assertIn(10, app._support_pending)
