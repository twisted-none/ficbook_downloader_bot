from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from src.storage.analytics import AnalyticsStore, render_prometheus

logger = logging.getLogger(__name__)


async def start_metrics_server(store: AnalyticsStore, host: str, port: int) -> web.AppRunner:
    app = web.Application()
    app["analytics_store"] = store
    app.router.add_get("/metrics", _metrics)
    app.router.add_get("/health", _health)
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
    store: AnalyticsStore = request.app["analytics_store"]
    payload = await asyncio.to_thread(render_prometheus, store.snapshot())
    return web.Response(text=payload, content_type="text/plain; version=0.0.4")


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")
