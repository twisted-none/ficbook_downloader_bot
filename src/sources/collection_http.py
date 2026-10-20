from collections.abc import Callable
from email.message import Message
from io import BytesIO
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import getproxies, proxy_bypass

from curl_cffi import CurlECode, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import RequestException

from src.core.limits import (ResourceLimitError, ResponseLimitError, check_deadline,
                             current_limits, request_timeout)
from src.sources.safe_http import is_cloudflare_challenge, require_https_url, resolve_addresses


_HOSTS = frozenset({'ficbook.net', 'www.ficbook.net'})


class BrowserCollectionClient:
    """One anonymous, memory-only browser session for a public collection."""

    def __init__(self, validate_url: Callable[[str], object]) -> None:
        self._validate_url = validate_url
        self._session: Session | None = None
        self._closed = False

    def fetch(self, url: str) -> tuple[bytes, str]:
        if self._closed:
            raise URLError('Collection session is closed')
        visited: set[str] = set()
        for hop in range(6):
            check_deadline()
            parts = require_https_url(url, _HOSTS)
            self._validate_url(url)
            if url in visited:
                raise URLError('Collection redirect loop')
            visited.add(url)
            # RESOLVE cannot pin the CONNECT destination of an arbitrary proxy.
            # Refuse instead of leaking a configured proxy's traffic onto a direct route.
            if getproxies().get('https') and not proxy_bypass(parts.hostname):
                raise URLError('Browser collection transport cannot use the configured proxy safely')
            addresses = resolve_addresses(parts.hostname, 443)
            selected = next((entry for entry in addresses if entry[0] == socket.AF_INET), addresses[0])
            address = selected[4][0]
            pin = f'[{address}]' if ':' in address else address
            limit = current_limits().max_response_bytes
            if self._session is None:
                self._session = Session(impersonate='chrome', trust_env=False)
            self._session.curl_options = {
                CurlOpt.RESOLVE: [f'{parts.hostname}:443:{pin}'.encode('ascii')],
                CurlOpt.PROXY: b'',
                CurlOpt.NETRC: 0,
                CurlOpt.FRESH_CONNECT: 1,
                CurlOpt.FORBID_REUSE: 1,
                CurlOpt.MAXFILESIZE_LARGE: limit,
            }
            body = bytearray()
            callback_error: ResourceLimitError | None = None

            def receive(chunk: bytes) -> int:
                nonlocal callback_error
                try:
                    check_deadline()
                    if len(body) + len(chunk) > limit:
                        raise ResponseLimitError('Ответ сайта превышает безопасный размер загрузки.')
                except ResourceLimitError as exc:
                    callback_error = exc
                    return CURL_WRITEFUNC_ERROR
                body.extend(chunk)
                return len(chunk)

            try:
                response = self._session.get(
                    url, verify=True, allow_redirects=False,
                    timeout=request_timeout(current_limits().max_response_seconds),
                    accept_encoding='identity', headers={'Accept-Language': 'ru-RU,ru;q=0.9'},
                    content_callback=receive,
                )
            except RequestException as exc:
                if callback_error is not None:
                    raise callback_error from None
                if exc.code == CurlECode.FILESIZE_EXCEEDED:
                    raise ResponseLimitError('Ответ сайта превышает безопасный размер загрузки.') from None
                raise URLError('Unable to fetch public collection safely') from None
            if callback_error is not None:
                raise callback_error
            check_deadline()
            if response.url != url:
                raise URLError('Unexpected effective collection URL')
            headers = Message()
            for name, value in response.headers.items():
                headers[name] = value
            if response.status_code in {301, 302, 303, 307, 308}:
                location = headers.get('Location')
                if (not location or hop == 5 or '\\' in location
                        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location)):
                    raise URLError('Invalid or excessive collection redirects')
                url = urljoin(url, location)
                continue
            if response.status_code != 200 or is_cloudflare_challenge(headers):
                raise HTTPError(url, response.status_code, 'Public collection request failed', headers, BytesIO(body))
            declared = headers.get('Content-Length', '')
            if (declared.isdecimal() and headers.get('Content-Encoding', 'identity').lower() == 'identity'
                    and len(body) != int(declared)):
                raise URLError('Incomplete collection response')
            return bytes(body), url
        raise URLError('Too many collection redirects')

    def close(self) -> None:
        self._closed = True
        if self._session is not None:
            self._session.close()
            self._session = None
