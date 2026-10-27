import os
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.storage.analytics import AnalyticsStore

TEST_DATABASE_URL = os.getenv('FICBOOK_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_DATABASE_URL, 'Requires an isolated local test database')
class AlertRetryStorageTests(unittest.TestCase):
    def setUp(self):
        params = conninfo_to_dict(TEST_DATABASE_URL)
        if params.get('host') != '127.0.0.1' or params.get('dbname') != 'ficbook_security_test':
            self.fail('Integration tests require 127.0.0.1 and database ficbook_security_test')
        self.schema = 'alert_test_' + uuid4().hex
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.dsn = make_conninfo(TEST_DATABASE_URL, options='-c search_path=' + self.schema)
        self.analytics = AnalyticsStore(self.dsn)
        self.addCleanup(self.analytics.close)
        self.store = self.analytics.alert_retries

    def drop_schema(self):
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def create(self, **kwargs):
        values = dict(user_id=101, chat_id=102, url='https://ficbook.net/readfic/123',
                      formats=('epub', 'fb2'), cover_enabled=False, chapter_numbers={2, 4},
                      bot_id=123456, admin_chat_id=9)
        values.update(kwargs)
        token = self.store.create(**values)
        self.store.bind_message(token, 12)
        return token

    def claim(self, token):
        claim = self.store.claim(token, 123456, 9, 12)
        if claim:
            self.addCleanup(claim.close)
        return claim

    def test_snapshot_survives_store_restart_and_keeps_original_settings(self):
        token = self.create()
        restarted = AnalyticsStore(self.dsn)
        self.addCleanup(restarted.close)
        claim = restarted.alert_retries.claim(token, 123456, 9, 12)
        self.addCleanup(claim.close)
        self.assertEqual(claim.request.user_id, 101)
        self.assertEqual(claim.request.chat_id, 102)
        self.assertEqual(claim.request.formats, ('epub', 'fb2'))
        self.assertEqual(claim.request.chapter_numbers, (2, 4))
        self.assertFalse(claim.request.cover_enabled)

    def test_claim_is_exclusive_and_released_when_connection_closes(self):
        token = self.create()
        claim = self.store.claim(token, 123456, 9, 12)
        try:
            self.assertIsNone(self.store.claim(token, 123456, 9, 12))
        finally:
            claim.close()
        self.assertIsNotNone(self.claim(token))

    def test_server_connection_loss_releases_lock(self):
        token = self.create()
        claim = self.store.claim(token, 123456, 9, 12)
        claim.db.close()
        claim.connection.__exit__(None, None, None)
        self.assertIsNotNone(self.claim(token))

    def test_callback_is_bound_to_bot_admin_chat_and_message(self):
        token = self.create()
        for bot_id, chat_id, message_id in ((1, 9, 12), (123456, 8, 12), (123456, 9, 13)):
            with self.subTest(bot=bot_id, chat=chat_id, message=message_id):
                self.assertIsNone(self.store.claim(token, bot_id, chat_id, message_id))
        self.assertIsNotNone(self.claim(token))

    def test_delivered_formats_persist_and_final_format_marks_delivery(self):
        token = self.create()
        claim = self.store.claim(token, 123456, 9, 12)
        try:
            claim.record_sent('epub')
            self.assertFalse(claim.request.delivered)
        finally:
            claim.close()
        resumed = self.claim(token)
        self.assertEqual(resumed.request.sent_formats, ('epub',))
        resumed.record_sent('fb2')
        self.assertTrue(resumed.request.delivered)
        with self.assertRaises(ValueError):
            resumed.record_sent('fb2')

    def test_all_chapters_remain_none(self):
        claim = self.claim(self.create(chapter_numbers=None))
        self.assertIsNone(claim.request.chapter_numbers)

    def test_identity_survives_restart(self):
        token = self.create(username='reader', full_name='Имя Читателя')
        restarted = AnalyticsStore(self.dsn)
        self.addCleanup(restarted.close)
        claim = restarted.alert_retries.claim(token, 123456, 9, 12)
        self.addCleanup(claim.close)
        self.assertEqual(claim.request.username, 'reader')
        self.assertEqual(claim.request.full_name, 'Имя Читателя')

    def test_old_snapshot_uses_only_unexpired_profile(self):
        from aiogram.types import User
        self.analytics.record_user(User(id=101, is_bot=False, first_name='Reader', username='reader'))
        token = self.create()
        with self.analytics._connect() as db:
            db.execute('UPDATE alert_retries SET username = NULL, full_name = NULL WHERE token = %s', (token,))
        claim = self.store.claim(token, 123456, 9, 12)
        self.assertEqual(claim.request.username, 'reader')
        claim.close()
        with self.analytics._connect() as db:
            db.execute("UPDATE bot_users SET profile_expires_at = now() - interval '1 second' WHERE user_id = 101")
        claim = self.claim(token)
        self.assertFalse(claim.request.username)

    def test_expiry_is_thirty_days_and_cleanup_does_not_change_statistics(self):
        token = self.create()
        before = self.analytics.snapshot()
        with self.analytics._connect() as db:
            seconds = db.execute("SELECT extract(epoch FROM expires_at - now()) AS seconds FROM alert_retries"
                                 " WHERE token = %s", (token,)).fetchone()['seconds']
            self.assertAlmostEqual(float(seconds) / 86400, 30, places=4)
            db.execute("UPDATE alert_retries SET expires_at = now() - interval '1 second' WHERE token = %s", (token,))
        self.assertIsNone(self.store.claim(token, 123456, 9, 12))
        self.analytics.cleanup_expired_history()
        with self.analytics._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS count FROM alert_retries').fetchone()['count'], 0)
        self.assertEqual(before, self.analytics.snapshot())

    def test_invalid_requests_are_not_saved(self):
        for kwargs in ({'formats': ('exe',)}, {'formats': ()}, {'url': 'https://127.0.0.1/work'},
                       {'chapter_numbers': {0}}, {'chapter_numbers': set()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.create(**kwargs)

    def test_token_is_parameterized_and_cannot_read_another_request(self):
        self.create()
        self.assertIsNone(self.store.claim("' OR true --", 123456, 9, 12))

    def test_button_download_delivery_and_alert_deletion_end_to_end(self):
        from aiogram import Bot
        from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup,
                                    EditMessageText, SendDocument, SendMessage)
        from aiogram.types import Chat, Message, User
        from src.bot import app
        from src.bot.alert_redelivery import AlertRedelivery, RETRY_NOTICE
        from src.bot.telegram_delivery import TelegramDeliveryGate
        from src.core.download_queue import DownloadQueuePool
        from src.core.models import Story
        from tests.test_alert_redelivery import callback
        from tests.test_download_status_flow import NoopAction

        self.analytics.record_user(User(id=101, is_bot=False, first_name='Test'), started=True)
        token = self.create()
        events = []
        async def run():
            main_bot, alert_bot = Bot('654321:fake_token'), Bot('123456:fake_token')
            client = SimpleNamespace(download=Mock(return_value=Story('Book', 'Author', 'https://ficbook.net/readfic/123')))
            queues = DownloadQueuePool()
            async def download(message, request, delivery):
                return await app._process_download(message, client, 9, alert_bot, self.analytics, queues,
                    request.url, request.formats, frozenset(), cover_enabled=request.cover_enabled,
                    chapter_numbers=set(request.chapter_numbers), redelivery=delivery)
            async def api(bot, method, **kwargs):
                if isinstance(method, SendDocument):
                    events.append(('file', bot.id, method.chat_id, method.caption))
                elif isinstance(method, DeleteMessage):
                    events.append(('delete', bot.id, method.message_id))
                elif not isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage)):
                    raise AssertionError(type(method))
                if isinstance(method, (SendMessage, SendDocument)):
                    return Message(message_id=100, date=datetime.now(timezone.utc),
                                   chat=Chat(id=method.chat_id, type='private'), text='Status').as_(bot)
                return True
            service = AlertRedelivery(main_bot, 9, self.store, download)
            query = callback(token=token).as_(alert_bot)
            query.message.as_(alert_bot)
            try:
                with patch.object(Bot, '__call__', new=api), \
                        patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                        patch.object(app, '_build_selected_files', AsyncMock(return_value=[('epub', b'file'), ('fb2', b'file')])), \
                        patch('src.bot.telegram_delivery.telegram_delivery', TelegramDeliveryGate(chat_interval=0, bot_interval=0)):
                    await service.handle(query)
            finally:
                await service.close()
                await main_bot.session.close()
                await alert_bot.session.close()
        asyncio.run(run())
        files = [event for event in events if event[0] == 'file']
        self.assertEqual([(event[1], event[2]) for event in files], [(654321, 102), (654321, 102)])
        self.assertEqual(files[-1][3], RETRY_NOTICE)
        self.assertEqual(events[-1], ('delete', 123456, 12))
        self.assertTrue(self.claim(token).request.delivered)
        self.assertEqual(self.analytics.snapshot().successful_downloads, 1)


if __name__ == '__main__':
    unittest.main()
