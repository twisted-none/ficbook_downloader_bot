from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.bot import app
from src.bot.types import SettingsDraft, SettingsSession


class SessionHandlerTests(unittest.IsolatedAsyncioTestCase):
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
