from __future__ import annotations

import asyncio
import re
import socket
from urllib.parse import SplitResult, parse_qs, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

from src.sources.registry import SUPPORTED_HOST_MARKERS, is_supported_story_url
from src.sources.safe_http import is_public_address

MAX_RESPONSE_BYTES = 64 * 1024
MAX_HOPS = 4
TIMEOUT_SECONDS = 10
ERROR_TEXT = "Не удалось безопасно раскрыть ссылку Google. Пришли прямую ссылку на фанфик."
_lookups = asyncio.Semaphore(4)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ShareLinkError(Exception):
    def __init__(self) -> None:
        super().__init__(ERROR_TEXT)


def _parts(url: str) -> SplitResult:
    if len(url) > 4096 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise ShareLinkError()
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None or parts.scheme not in {"http", "https"}:
            raise ShareLinkError()
        if parts.port not in {None, 443 if parts.scheme == "https" else 80}:
            raise ShareLinkError()
        return parts
    except ValueError as exc:
        raise ShareLinkError() from exc


def is_google_share_url(url: str) -> bool:
    try:
        parts = _parts(url)
        return bool(parts.scheme == "https" and parts.hostname == "share.google"
                    and _TOKEN.fullmatch(parts.path.removeprefix("/"))
                    and parts.path.startswith("/") and not parts.query and not parts.fragment)
    except ShareLinkError:
        return False


def extract_download_url(text: str) -> str | None:
    for chunk in text.split():
        candidate = chunk.strip("()[]<>,.!?\"'")
        try:
            if is_google_share_url(candidate) or is_supported_story_url(candidate):
                return candidate
        except ValueError:
            continue
    return None


def _kind(url: str) -> str:
    parts = _parts(url)
    if is_google_share_url(url):
        return "google"
    if parts.scheme == "https" and parts.hostname in {"google.com", "www.google.com"} and parts.path == "/share.google":
        query = parse_qs(parts.query, keep_blank_values=True)
        if set(query) == {"q"} and len(query["q"]) == 1 and _TOKEN.fullmatch(query["q"][0]):
            return "google"
    host = parts.hostname or ""
    host = host.removeprefix("www.")
    if host in SUPPORTED_HOST_MARKERS and is_supported_story_url(url):
        if parts.scheme == "https" or host == "hogwartsnet.ru":
            return "story"
    raise ShareLinkError()


class PublicResolver(AbstractResolver):
    def __init__(self) -> None:
        self._resolver = ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        allowed = host in {"share.google", "google.com", "www.google.com"} or host.removeprefix("www.") in SUPPORTED_HOST_MARKERS
        if not allowed:
            raise ShareLinkError()
        addresses = await self._resolver.resolve(host, port, family)
        if not addresses:
            raise ShareLinkError()
        for address in addresses:
            if not is_public_address(address['host']):
                raise ShareLinkError()
        # The connector receives these numeric addresses, not a second DNS lookup.
        return addresses

    async def close(self) -> None:
        await self._resolver.close()


async def _fetch_redirect(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, allow_redirects=False) as response:
        if response.status not in {301, 302, 303, 307, 308}:
            raise ShareLinkError()
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise ShareLinkError()
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ShareLinkError()
        size = 0
        async for chunk in response.content.iter_chunked(8192):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ShareLinkError()
        location = response.headers.get("Location")
        if not location:
            raise ShareLinkError()
        return urljoin(url, location)


async def resolve_share_url(url: str) -> str:
    if not is_google_share_url(url):
        raise ShareLinkError()
    resolver = PublicResolver()
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS), _lookups:
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=1)
            async with aiohttp.ClientSession(
                connector=connector, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                auto_decompress=False, headers={"Accept-Encoding": "identity"},
            ) as session:
                seen: set[str] = set()
                for _ in range(MAX_HOPS):
                    if url in seen:
                        raise ShareLinkError()
                    seen.add(url)
                    kind = _kind(url)
                    if kind == "story":
                        parts = _parts(url)
                        await resolver.resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
                        return url
                    url = await _fetch_redirect(session, url)
                raise ShareLinkError()
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
        raise ShareLinkError() from exc
    finally:
        await resolver.close()
