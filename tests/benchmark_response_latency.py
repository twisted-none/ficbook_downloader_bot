"""Synthetic first-reply benchmark; no Telegram, database or source requests."""

import asyncio
import json
from threading import Lock
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.app import RegisterUserMiddleware
from tests.test_download_status_flow import IncomingMessage


async def measure(*, optimized: bool, count: int = 100):
    lock = Lock()
    latencies = []

    def add(user_id):
        with lock:
            sleep(0.005)

    async def handle(index):
        message = IncomingMessage()
        message.from_user = SimpleNamespace(id=100000 + index)
        message.text = 'https://ficbook.net/readfic/12531381'
        original = message.answer
        started = monotonic()

        async def answer(text, **kwargs):
            latencies.append((monotonic() - started) * 1000)
            return await original(text, **kwargs)

        message.answer = answer
        if optimized:
            await RegisterUserMiddleware()(AsyncMock(), message, {'user_store': SimpleNamespace(add=add)})
        else:
            await asyncio.to_thread(add, message.from_user.id)
            await message.answer('Link received')

    await asyncio.gather(*(handle(i) for i in range(count)))
    latencies.sort()
    return {'messages': count, 'p50_ms': round(latencies[count // 2], 2),
            'p95_ms': round(latencies[int(count * 0.95) - 1], 2), 'max_ms': round(latencies[-1], 2)}


async def main():
    print(json.dumps({'before': await measure(optimized=False), 'after': await measure(optimized=True)}))


if __name__ == '__main__':
    asyncio.run(main())
