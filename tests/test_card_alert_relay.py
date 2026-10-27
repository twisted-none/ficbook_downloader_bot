import hashlib
import asyncio
import hmac
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User
from src.bot.alert_relay import relay_card_retry


class CardRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot('222222:fake_shared_token')
        self.addAsyncCleanup(self.bot.session.close)
        self.api = AsyncMock()
        self.enterContext(patch.object(Bot, '__call__', new=self.api))
        self.callback = CallbackQuery(id='fixture', from_user=User(id=9, is_bot=False, first_name='Admin'),
            chat_instance='fixture', data='janny:retry:' + 'a' * 24,
            message=Message(message_id=13, date=datetime.now(timezone.utc),
                            chat=Chat(id=9, type='private'), text='Alert')).as_(self.bot)

    async def test_signed_request_is_forwarded_and_acknowledged(self):
        seen = []

        async def retry(request):
            body = await request.read()
            expected = hmac.new(self.bot.token.encode(), b'janny-alert-v1\n' + body, hashlib.sha256).hexdigest()
            self.assertEqual(request.headers['X-Alert-Signature'], expected)
            seen.append(json.loads(body))
            return web.json_response({'text': 'queued'})

        app = web.Application()
        app.router.add_post('/retry', retry)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        self.addAsyncCleanup(runner.cleanup)
        url = f'http://127.0.0.1:{runner.addresses[0][1]}/retry'
        with patch.dict(os.environ, {'JANITOR_ALERT_RETRY_URL': url}):
            await relay_card_retry(self.callback)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['bot_id'], 222222)
        self.assertEqual(seen[0]['user_id'], 9)
        self.assertEqual(seen[0]['message_id'], 13)
        self.assertNotIn('fake_shared_token', json.dumps(seen))
        self.assertEqual(self.api.await_args.args[0].text, 'queued')

    async def test_invalid_button_does_not_contact_card_bot(self):
        callback = self.callback.model_copy(update={'data': 'janny:retry:bad'}).as_(self.bot)
        with patch('src.bot.alert_relay.ClientSession') as session:
            await relay_card_retry(callback)
        session.assert_not_called()

    async def test_chunked_reply_is_not_mistaken_for_failure(self):
        async def retry(request):
            await request.read()
            response = web.StreamResponse(headers={'Content-Type': 'application/json'})
            await response.prepare(request)
            await response.write(b'{"text":')
            await asyncio.sleep(.03)
            await response.write(b'"queued"}')
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post('/retry', retry)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        self.addAsyncCleanup(runner.cleanup)
        url = f'http://127.0.0.1:{runner.addresses[0][1]}/retry'
        with patch.dict(os.environ, {'JANITOR_ALERT_RETRY_URL': url}):
            await relay_card_retry(self.callback)
        self.assertEqual(self.api.await_args.args[0].text, 'queued')

    async def test_external_endpoint_is_refused_without_contact(self):
        with patch.dict(os.environ, {'JANITOR_ALERT_RETRY_URL': 'http://example.invalid/retry'}), \
                patch('src.bot.alert_relay.ClientSession') as session:
            await relay_card_retry(self.callback)
        session.assert_not_called()
        self.assertIn('Алерт сохранён', self.api.await_args.args[0].text)
