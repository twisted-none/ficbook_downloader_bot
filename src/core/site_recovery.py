"""One demand-driven recovery probe shared by all waiting jobs."""

import asyncio
import logging

from src.core.limits import suspend_deadline

logger = logging.getLogger(__name__)


class SiteRecovery:
    def __init__(self, *, interval=300, sleep=None):
        self.interval = interval
        self.sleep = sleep or asyncio.sleep
        self.task = None
        self.waiters = 0

    async def _poll(self, probe):
        while True:
            await self.sleep(self.interval)
            try:
                if await probe():
                    return
            except Exception as error:
                logger.warning('site_recovery_probe_failed type=%s', type(error).__name__)

    async def wait(self, probe):
        # Waiting for a site outage is not CPU/download processing time.
        with suspend_deadline():
            self.waiters += 1
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._poll(probe))
            task = self.task
            try:
                await asyncio.shield(task)
            finally:
                self.waiters -= 1
                if self.waiters == 0 and self.task is task:
                    self.task = None
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
