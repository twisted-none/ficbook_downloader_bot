from __future__ import annotations

import asyncio
import logging
from time import monotonic

from aiohttp import web

from src.storage.analytics import AnalyticsStore, render_prometheus

logger = logging.getLogger(__name__)


class MetricsCache:
    def __init__(self, store: AnalyticsStore, ttl_seconds: float = 30.0) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.payload = ""
        self.expires_at = 0.0
        self.retry_after = 0.0
        self.refresh_task: asyncio.Task[str] | None = None

    async def get(self) -> str:
        if self.payload and monotonic() < self.expires_at:
            return self.payload
        if monotonic() < self.retry_after:
            raise RuntimeError("Metrics temporarily unavailable")
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self._refresh())
            self.refresh_task.add_done_callback(self._consume_exception)
        return await asyncio.shield(self.refresh_task)

    async def _refresh(self) -> str:
        try:
            payload = await asyncio.to_thread(self._render)
        except Exception:
            self.retry_after = monotonic() + 5.0
            logger.warning("Aggregate metrics refresh failed; retry deferred for 5 seconds")
            raise
        self.payload = payload
        self.expires_at = monotonic() + self.ttl_seconds
        return payload

    def _render(self) -> str:
        return render_prometheus(self.store.snapshot(include_users=False))

    @staticmethod
    def _consume_exception(task: asyncio.Task[str]) -> None:
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        if self.refresh_task is not None:
            await asyncio.gather(self.refresh_task, return_exceptions=True)


METRICS_CACHE = web.AppKey("metrics_cache", MetricsCache)


def create_metrics_app(store: AnalyticsStore) -> web.Application:
    app = web.Application()
    app[METRICS_CACHE] = MetricsCache(store)
    app.router.add_get("/metrics", _metrics)
    app.router.add_get("/health", _health)
    app.on_cleanup.append(_cleanup)
    return app


async def start_metrics_server(store: AnalyticsStore, host: str, port: int) -> web.AppRunner:
    app = create_metrics_app(store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Metrics server started on %s:%s", host, port)
    return runner


async def run_metrics_server(store: AnalyticsStore, host: str, port: int) -> None:
    runner = await start_metrics_server(store, host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def _metrics(request: web.Request) -> web.Response:
    try:
        payload = await request.app[METRICS_CACHE].get()
    except Exception:
        return web.Response(status=503, text="Metrics temporarily unavailable\n", content_type="text/plain")
    return web.Response(text=payload, headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def _cleanup(app: web.Application) -> None:
    await app[METRICS_CACHE].close()
