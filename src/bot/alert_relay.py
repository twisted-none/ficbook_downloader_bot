"""Forward card-bot callbacks through the single shared alert-bot receiver."""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout
from aiogram.types import CallbackQuery, Message

PREFIX = 'janny:retry:'
DEFAULT_URL = 'http://janitor_characters_bot:8001/retry'


async def relay_card_retry(callback: CallbackQuery) -> None:
    token = (callback.data or '').removeprefix(PREFIX)
    if not isinstance(callback.message, Message) or re.fullmatch(r'[A-Za-z0-9_-]{24}', token) is None:
        await callback.answer('Некорректная кнопка.', show_alert=True)
        return
    text = 'Не удалось связаться с ботом карточек. Алерт сохранён, попробуй позже.'
    try:
        url = os.getenv('JANITOR_ALERT_RETRY_URL', DEFAULT_URL)
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'janitor_characters_bot', 'localhost', '127.0.0.1'}
                or parsed.username or parsed.password or parsed.path != '/retry' or parsed.query or parsed.fragment):
            raise ValueError('Invalid internal card relay address')
        payload = dict(token=token, user_id=callback.from_user.id, chat_id=callback.message.chat.id,
                       message_id=callback.message.message_id, bot_id=callback.bot.id,
                       timestamp=int(time.time()))
        body = json.dumps(payload, separators=(',', ':')).encode()
        signature = hmac.new(callback.bot.token.encode(), b'janny-alert-v1\n' + body, hashlib.sha256).hexdigest()
        async with ClientSession(timeout=ClientTimeout(total=12), trust_env=False) as session:
            async with session.post(url, data=body, allow_redirects=False,
                    headers={'Content-Type': 'application/json', 'X-Alert-Signature': signature}) as response:
                if response.status != 200:
                    raise ValueError('Card relay refused callback')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(512):
                    raw.extend(chunk)
                    if len(raw) > 2048:
                        raise ValueError('Card relay response too large')
                result = json.loads(raw)
                if not isinstance(result, dict) or not isinstance(result.get('text'), str):
                    raise ValueError('Invalid card relay response')
                text = result['text'][:200]
    except Exception as error:
        logging.warning('Card alert relay failed: %s', type(error).__name__)
    await callback.answer(text, show_alert=True)
