"""Requests compatibility for AO3 with bounded, pinned HTTPS connections."""

import socket
import ssl
import re
import logging
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from fanficfare.fetchers import RequestsFetcher
from fanficfare.exceptions import HTTPErrorFFF
from fanficfare.fetchers.base_fetcher import FetcherResponse
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError, ProtocolError, TimeoutError as UrllibTimeoutError

from src.core.limits import (ResponseLimitError, bounded_sleep, check_deadline, current_limits,
                             read_bounded, request_timeout)
from src.sources.fanficfare_http import SafeFanficFareFetcher
from src.sources.ao3 import require_available_work
from src.sources.collection_http import BrowserCollectionClient
from src.sources.safe_http import (DeadlineHTTPResponse, UnsafeRequestError,
                                   require_https_url, resolve_addresses)


HOSTS = frozenset({'archiveofourown.org', 'www.archiveofourown.org', 'download.archiveofourown.org'})
TRANSIENT_CODES = frozenset({408, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525})
logger = logging.getLogger(__name__)


def is_transient_ao3_error(error: Exception) -> bool:
    if (isinstance(error, (UnsafeRequestError, requests.exceptions.SSLError, ssl.SSLError))
            or isinstance(error, URLError) and isinstance(error.reason, ssl.SSLError)):
        return False
    if isinstance(error, (HTTPErrorFFF, HTTPError)):
        return getattr(error, 'status_code', getattr(error, 'code', None)) in TRANSIENT_CODES
    return isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError,
                             requests.exceptions.ContentDecodingError, ProtocolError, UrllibTimeoutError,
                             URLError, TimeoutError, ConnectionError))


def _failure_summary(transport: str, error: Exception) -> str:
    code = getattr(error, 'status_code', getattr(error, 'code', None))
    return f'{transport}:HTTP {code}' if isinstance(code, int) else f'{transport}:{type(error).__name__}'


def _browser_failure(error: Exception) -> bool:
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return not isinstance(error, requests.exceptions.SSLError)
    if not isinstance(error, HTTPErrorFFF):
        return False
    return error.status_code == 525 or (error.status_code == 403 and any(
        marker in repr(error).lower() for marker in ('shields are up', 'not a robot', 'cloudflare', 'challenge-platform')
    ))


class AO3HTTPResponse(DeadlineHTTPResponse):
    max_response_seconds = 20


class PublicAO3Connection(HTTPSConnection):
    response_class = AO3HTTPResponse

    def _new_conn(self):
        addresses = resolve_addresses(self.host, self.port or 443)
        deadline = monotonic() + request_timeout(min(self.timeout or 8, 8))
        for family, kind, protocol, _, address in addresses:
            check_deadline()
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(request_timeout(remaining))
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(address)
                return sock
            except OSError:
                sock.close()
        raise NewConnectionError(self, 'Unable to connect to a validated AO3 address')


class _PublicAO3Pool(HTTPSConnectionPool):
    ConnectionCls = PublicAO3Connection


class _AO3PoolManager(PoolManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Each manager owns its mapping: never modify Ficbook's native pools.
        self.pool_classes_by_scheme = dict(self.pool_classes_by_scheme, https=_PublicAO3Pool)


class _DecodedBody:
    def __init__(self, raw):
        self.raw = raw
        encoding = raw.headers.get('Content-Encoding', '').lower()
        if encoding not in {'', 'identity', 'gzip', 'deflate', 'br'}:
            raise ResponseLimitError('AO3 вернул неподдерживаемое сжатие ответа.')
        declared = raw.headers.get('Content-Length', '')
        if declared.isdecimal() and int(declared) > current_limits().max_response_bytes:
            raise ResponseLimitError('Ответ AO3 превышает безопасный размер.')
        self.headers = {'Content-Length': declared} if not encoding or encoding == 'identity' else {}

    @property
    def _deadline_expired(self):
        return getattr(getattr(self.raw, '_original_response', None), '_deadline_expired', False)

    def read1(self, amount):
        # urllib3 2.8 + Brotli 1.2 cap decoder output at `amount`, even for a bomb.
        chunk = self.raw.read1(amount, decode_content=True)
        if self.raw.tell() > current_limits().max_response_bytes:
            raise ResponseLimitError('Сжатый ответ AO3 превышает безопасный размер.')
        return chunk


class AO3Adapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self.poolmanager = _AO3PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs,
        )

    def send(self, request, **kwargs):
        kwargs['stream'] = True
        response = super().send(request, **kwargs)
        try:
            if response.is_redirect or response.is_permanent_redirect:
                response.raw.close()
                payload = b''
            else:
                payload = read_bounded(_DecodedBody(response.raw))
            response._content = payload
            response._content_consumed = True
            return response
        finally:
            response.close()


class AO3Session(requests.Session):
    def __init__(self):
        super().__init__()
        self.max_redirects = 5
        self.headers['Accept-Encoding'] = 'gzip, deflate, br'

    def send(self, request, **kwargs):
        require_https_url(request.url, HOSTS)
        if request.method not in {'GET', 'POST'} or kwargs.get('verify', self.verify) is not True:
            raise UnsafeRequestError('Verified AO3 HTTPS requests are required')
        if kwargs.get('proxies'):
            raise UnsafeRequestError('Configured proxy cannot be used with pinned AO3 connections')
        timeout = kwargs.get('timeout', 60)
        connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
        kwargs['timeout'] = (request_timeout(min(connect or 8, 8)), request_timeout(min(read or 20, 20)))
        return super().send(request, **kwargs)

    def get_redirect_target(self, response):
        location = super().get_redirect_target(response)
        if location:
            if '\\' in location or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location):
                raise UnsafeRequestError()
            origin = require_https_url(response.request.url, HOSTS)
            target = require_https_url(urljoin(response.url, location), HOSTS)
            if origin.hostname != target.hostname and (
                response.request.method != 'GET' or response.request.body
            ):
                raise UnsafeRequestError('AO3 credential requests cannot change origin')
        return location

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if require_https_url(response.request.url).hostname != require_https_url(prepared_request.url).hostname:
            for name in ('Authorization', 'Cookie', 'Referer', 'Origin', 'Proxy-Authorization', 'Host'):
                prepared_request.headers.pop(name, None)


class AO3RequestsFetcher(RequestsFetcher):
    allowed_hosts = HOSTS

    def condition_url(self, url):
        return SafeFanficFareFetcher.condition_url(self, url)

    def make_sesssion(self):
        return AO3Session()

    def do_mounts(self, session):
        # Recovery belongs to the per-page fallback, not urllib3's retry loop.
        session.mount('https://', AO3Adapter(max_retries=0))

    def use_verify(self):
        return True

    def request(self, method, url, headers=None, parameters=None, json=None):
        if headers and headers.get('Accept') == 'image/*':
            return SafeFanficFareFetcher(self.getConfig, self.getConfigList, 'archiveofourown.org').request(
                method, url, headers=headers, parameters=parameters, json=json,
            )
        require_https_url(url, HOSTS)
        parts = urlsplit(url)
        probe = (urlunsplit(parts._replace(path=parts.path.removesuffix('/navigate'), query='view_adult=true'))
                 if (method == 'GET' and parameters is None and json is None
                     and re.fullmatch(r'/works/\d+/navigate', parts.path)) else None)
        result = self._request_page(method, url, headers=headers, parameters=parameters, json=json, probe=probe)
        if probe and result.redirecturl and urlsplit(result.redirecturl).path in {'', '/'}:
            # AO3 sends unrevealed work indexes home; FFF mistakes home for a login wall.
            response = self._request_page('GET', probe, headers=headers, attempts=1)
            require_available_work(response.content)
        if method == 'GET':
            require_available_work(result.content)
        return result

    def _request_page(self, method, url, headers=None, parameters=None, json=None, *, probe=None, attempts=3):
        replay = (method == 'GET' and parameters is None and json is None
                  and re.match(r'^/(?:works|downloads)/', urlsplit(url).path))
        if not replay:
            return super().request(method, url, headers=headers, parameters=parameters, json=json)
        first_error = None
        failures = []
        browser = None
        try:
            for attempt in range(attempts):
                check_deadline()
                if attempt:
                    bounded_sleep(attempt)
                # All transports and the optional access probe share three attempts.
                public = attempt == 1 and _browser_failure(first_error)
                access_probe = (attempt == 2 and probe and isinstance(first_error, HTTPErrorFFF)
                                and first_error.status_code == 403)
                transport = 'public' if public else 'access-probe' if access_probe else 'requests'
                try:
                    if public:
                        # Do not forward account headers, cookies or request bodies.
                        browser = BrowserCollectionClient(
                            lambda target: require_https_url(target, HOSTS), allowed_hosts=HOSTS, timeout_seconds=25)
                        content, target = browser.fetch(url)
                        return FetcherResponse(content, target)
                    response = super().request('GET', probe if access_probe else url, headers=headers)
                    if not access_probe:
                        return response
                    require_available_work(response.content)
                    break
                except (HTTPErrorFFF, requests.RequestException, URLError, TimeoutError, ConnectionError,
                        ProtocolError, UrllibTimeoutError) as error:
                    failures.append(_failure_summary(transport, error))
                    if isinstance(error, HTTPError):
                        error.close()
                    if first_error is None:
                        first_error = error
                    if not is_transient_ao3_error(error) and not _browser_failure(error):
                        if attempt == 0:
                            raise
                        break
            first_error.ao3_retry_exhausted = True
            first_error.ao3_failures = tuple(failures)
            identity = re.search(r'/(?:works|downloads)/(\d+)', urlsplit(url).path)
            logger.warning('AO3 recovery exhausted for work %s: %s',
                           identity.group(1) if identity else 'unknown', '; '.join(failures))
            raise first_error from None
        finally:
            if browser is not None:
                browser.close()
