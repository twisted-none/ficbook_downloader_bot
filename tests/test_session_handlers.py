import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.bot import app
from src.bot.types import SettingsDraft, SettingsSession
from src.core.models import ChapterPreview, StoryPreview


class SessionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_selection_accepts_only_replies_after_new_prompt(self):
        url = 'https://ficbook.net/readfic/1'
        preview = StoryPreview('Работа', 'Автор', url, [ChapterPreview(1, 'Первая'), ChapterPreview(2, 'Вторая')])
        user = SimpleNamespace(id=42)
        chat = SimpleNamespace(id=42)
        loading = SimpleNamespace(delete=AsyncMock())
        preview_started = asyncio.Event()
        preview_ready = asyncio.Event()
        prompt_started = asyncio.Event()
        prompt_sent = asyncio.Event()
        client = SimpleNamespace(preview=unittest.mock.Mock())
        queues = app.DownloadQueuePool()

        def message(message_id, text):
            return SimpleNamespace(message_id=message_id, text=text, from_user=user, chat=chat, answer=AsyncMock())

        async def run_worker(*args):
            preview_started.set()
            await preview_ready.wait()
            return preview

        async def send_status(message, text, **kwargs):
            if 'Всего глав в работе' not in text:
                self.assertEqual(text, app._CHAPTERS_LOADING)
                return loading
            loading.delete.assert_awaited_once()
            self.assertIn('Всего глав в работе: 2.', text)
            self.assertIn('Введите главы', text)
            prompt_started.set()
            await prompt_sent.wait()
            return SimpleNamespace(message_id=20)

        async def select(reply):
            await app.on_chapter_selection_text(reply, client, None, None, object(), queues, frozenset())

        with patch.object(app, '_answer_status_with_retry', side_effect=send_status), \
             patch.object(app, 'run_worker', side_effect=run_worker), \
             patch.object(app, '_process_download', new_callable=AsyncMock) as download:
            preparation = asyncio.create_task(app._ask_chapter_selection(
                message(10, url), client, None, None, url, ('epub',), False, queues, opening_page='annotation',
            ))
            try:
                await asyncio.wait_for(preview_started.wait(), timeout=2)
                early = message(12, '0')
                await select(early)
                early.answer.assert_awaited_once_with(app._CHAPTERS_LOADING)
                preview_ready.set()
                await asyncio.wait_for(prompt_started.wait(), timeout=2)
                while_sending = message(18, '2')
                await select(while_sending)
                while_sending.answer.assert_awaited_once_with(app._CHAPTERS_LOADING)
                prompt_sent.set()
                await asyncio.wait_for(preparation, timeout=2)
                delayed = message(19, '0')
                await select(delayed)
                delayed.answer.assert_awaited_once()
                download.assert_not_awaited()

                await select(message(21, '2'))
                download.assert_awaited_once()
                self.assertEqual(download.await_args.args[6:8], (url, ('epub',)))
                self.assertEqual(download.await_args.kwargs['chapter_numbers'], {2})
                self.assertEqual(download.await_args.kwargs['opening_page'], 'annotation')
                self.assertFalse(download.await_args.kwargs['cover_enabled'])
            finally:
                if not preparation.done():
                    preparation.cancel()
                    await asyncio.gather(preparation, return_exceptions=True)
                app._chapter_waiting.pop(user.id, None)

    async def test_preview_stops_cleanly_when_telegram_status_is_unavailable(self):
        from src.sources.ficbook import FicbookError
        client = SimpleNamespace(preview=unittest.mock.Mock())
        with patch.object(app, '_answer_status_with_retry', new_callable=AsyncMock,
                          side_effect=FicbookError('Telegram is unavailable')):
            await app._ask_chapter_selection(object(), client, None, None,
                                             'https://ficbook.net/readfic/1', ('txt',), False)
        client.preview.assert_not_called()

    async def test_old_settings_message_cannot_modify_a_new_session(self):
        draft = SettingsDraft(('txt',))
        current = SettingsSession(draft, draft, message_id=2, chat_id=42)
        app._settings_drafts[42] = current
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=42), edit_reply_markup=AsyncMock()), answer=AsyncMock())
        self.assertIsNone(await app._settings_session(callback, object()))
        self.assertIs(app._settings_drafts.get(42), current)
        callback.message.edit_reply_markup.assert_not_awaited()

    def tearDown(self):
        app._settings_drafts.clear()
        app._support_waiting.clear()

    async def test_expired_settings_callback_cannot_create_or_save_a_session(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=42), edit_reply_markup=AsyncMock()), answer=AsyncMock())
        with patch('src.bot.sessions.monotonic', return_value=0):
            draft = SettingsDraft(('txt',))
            app._settings_drafts[42] = SettingsSession(draft, draft)
        with patch('src.bot.sessions.monotonic', return_value=1900), patch.object(
            app, '_get_user_download_settings', new_callable=AsyncMock,
        ) as read_settings:
            self.assertIsNone(await app._settings_session(callback, object()))
        read_settings.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_corrupt_user_registry_does_not_block_download_handlers(self):
        from src.storage.users import UserStoreError
        user_store = SimpleNamespace(add=unittest.mock.Mock(side_effect=UserStoreError('invalid registry')))
        message = SimpleNamespace(from_user=SimpleNamespace(id=42))
        handler = AsyncMock(return_value='handled')
        result = await app.RegisterUserMiddleware()(handler, message, {'user_store': user_store})
        self.assertEqual(result, 'handled')


if __name__ == '__main__':
    unittest.main()
