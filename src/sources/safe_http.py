from __future__ import annotations

from collections.abc import Mapping
from email.message import Message
from http.client import HTTPConnection, HTTPSConnection, HTTPResponse
from http.cookiejar import CookieJar
from functools import lru_cache
from ipaddress import ip_address, ip_network
import socket
import ssl
from threading import Timer
from typing import Any
from urllib.error import URLError
from urllib.parse import SplitResult, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request,
    build_opener, proxy_bypass,
)

from src.core.limits import check_deadline, current_limits, request_timeout

_TRANSITION_NETWORKS = tuple(ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16", "192.0.0.0/24",
))


class UnsafeRequestError(URLError):
    def __init__(self, reason: str = "External request blocked by security policy") -> None:
        super().__init__(reason)


def is_cloudflare_challenge(headers: Message | Mapping[str, str]) -> bool:
    return any(name.lower() == 'cf-mitigated' and str(value).strip().lower() == 'challenge'
               for name, value in headers.items())


def is_public_address(value: str) -> bool:
    try:
        ip = ip_address(value)
    except ValueError:
        return False
    return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved
                and not getattr(ip, "is_site_local", False)
                and not any(ip.version == network.version and ip in network for network in _TRANSITION_NETWORKS))


def require_https_url(url: str, allowed_hosts: frozenset[str] | None = None) -> SplitResult:
    if (len(url) > 8192 or "\\" in url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
        raise UnsafeRequestError()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme != "https" or not host or parts.port not in {None, 443}
                or parts.username is not None or parts.password is not None or "%" in host
                or parts.fragment or (allowed_hosts is not None and host not in allowed_hosts)):
            raise UnsafeRequestError()
        try:
            ip_address(host)
        except ValueError:
            pass
        else:
            if not is_public_address(host):
                raise UnsafeRequestError()
        return parts
    except ValueError:
        raise UnsafeRequestError() from None


def resolve_addresses(host: str, port: int, *, public_only: bool = True) -> list[tuple]:
    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        raise UnsafeRequestError("Unable to resolve external host safely") from None
    if not addresses:
        raise UnsafeRequestError("External host has no usable addresses")
    for family, kind, _, _, address in addresses:
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or address[1] != port or (public_only and not is_public_address(address[0]))
                or (public_only and family == socket.AF_INET6 and address[3] != 0)):
            raise UnsafeRequestError()
    return addresses


class DeadlineHTTPResponse(HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self._response_socket = sock
        self._deadline_expired = False
        self._deadline_timer = Timer(request_timeout(current_limits().max_response_seconds), self._expire)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def _expire(self):
        self._deadline_expired = True
        try:
            self._response_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _close_conn(self):
        timer = getattr(self, '_deadline_timer', None)
        if timer is not None:
            timer.cancel()
        super()._close_conn()


class PublicHTTPSConnection(HTTPSConnection):
    response_class = DeadlineHTTPResponse

    def __init__(self, host: str, *, via_proxy: bool = False, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        if not self._context.check_hostname or self._context.verify_mode != ssl.CERT_REQUIRED:
            raise UnsafeRequestError("Verified TLS is required")
        self._via_proxy = via_proxy
        self._tls_hostname = self.host
        self._create_connection = self._connect_socket

    def _connect_socket(self, address: tuple, timeout: float, source_address: tuple | None = None) -> socket.socket:
        # Only the explicitly configured proxy may have a private address; the target is always public.
        addresses = resolve_addresses(*address, public_only=not self._via_proxy)
        for family, kind, protocol, _, numeric_address in addresses:
            check_deadline()
            sock = socket.socket(family, kind, protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(request_timeout(timeout))
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(numeric_address)
                return sock
            except OSError:
                sock.close()
        raise UnsafeRequestError("Unable to connect to external host") from None

    def set_tunnel(self, host: str, port: int | None = None, headers: dict | None = None) -> None:
        host, port = self._get_hostport(host, port)
        if port != 443:
            raise UnsafeRequestError()
        addresses = resolve_addresses(host, port)
        # CONNECT uses a numeric target, so the proxy cannot perform a second DNS lookup.
        selected = next((entry for entry in addresses if entry[0] == socket.AF_INET), addresses[0])
        self._tls_hostname = host
        super().set_tunnel(selected[4][0], port, headers)

    def _tunnel(self) -> None:
        original = self._tunnel_host
        if original and ":" in original:
            self._tunnel_host = f"[{original}]"
        try:
            super()._tunnel()
        finally:
            self._tunnel_host = original

    def connect(self) -> None:
        if self._via_proxy and not self._tunnel_host:
            raise UnsafeRequestError("Proxy requests require a validated tunnel target")
        HTTPConnection.connect(self)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tls_hostname)
        except Exception:
            self.close()
            raise


class SafeProxyHandler(ProxyHandler):
    def proxy_open(self, req: Request, proxy: str, type: str) -> Any:
        if req.host and proxy_bypass(req.host):
            return None
        try:
            parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            port = 80 if parts.port is None else parts.port
            if parts.scheme != "http" or not parts.hostname or not 1 <= port <= 65535:
                raise UnsafeRequestError("Configured proxy protocol cannot be used safely")
        except ValueError:
            raise UnsafeRequestError("Invalid configured proxy") from None
        return super().proxy_open(req, proxy, type)


class SafeHTTPSHandler(HTTPSHandler):
    def https_open(self, req: Request) -> Any:
        require_https_url(req.full_url)

        def connection(host: str, **kwargs: Any) -> PublicHTTPSConnection:
            return PublicHTTPSConnection(host, via_proxy=bool(req._tunnel_host), **kwargs)

        return self.do_open(connection, req, context=self._context)


class SafeRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        target = require_https_url(newurl, self.allowed_hosts)
        origin = require_https_url(req.full_url, self.allowed_hosts)
        cross_origin = origin.hostname != target.hostname
        if cross_origin and (req.data is not None or req.get_method() not in {"GET", "HEAD"}):
            raise UnsafeRequestError("Credential requests cannot change origin")
        if code in {307, 308}:
            redirected = Request(newurl, data=req.data, headers=dict(req.headers), method=req.get_method())
        else:
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        for name in list(redirected.headers):
            if name.lower() == "proxy-authorization" or (cross_origin and name.lower() in {
                "authorization", "cookie", "referer", "origin", "host",
            }):
                redirected.remove_header(name)
        return redirected

    def http_error_302(self, req: Request, fp: Any, code: int, msg: str, headers: Any) -> Any:
        location = headers.get("Location") or headers.get("URI")
        if location is None:
            return None
        try:
            if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location) or "\\" in location:
                raise UnsafeRequestError()
            redirected = self.redirect_request(req, fp, code, msg, headers, urljoin(req.full_url, location))
            visited = getattr(req, "redirect_dict", {})
            if sum(visited.values()) >= self.max_redirections:
                raise UnsafeRequestError("Too many external redirects")
            visited[redirected.full_url] = visited.get(redirected.full_url, 0) + 1
            redirected.redirect_dict = visited
        finally:
            # urllib otherwise reads the whole redirect body before following Location.
            fp.close()
        return self.parent.open(redirected, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class SafeOpener:
    def __init__(self, backend: Any, allowed_hosts: frozenset[str] | None = None) -> None:
        self.backend = backend
        self.allowed_hosts = allowed_hosts

    def open(self, url: str | Request, data: bytes | None = None, timeout: float = 30) -> Any:
        require_https_url(url.full_url if isinstance(url, Request) else url, self.allowed_hosts)
        return self.backend.open(url, data=data, timeout=request_timeout(timeout))


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    return context


def build_safe_opener(
    allowed_hosts: frozenset[str] | None = None, *, cookie_jar: CookieJar | None = None,
    redirect_handler: SafeRedirectHandler | None = None,
) -> SafeOpener:
    handlers = [SafeProxyHandler(), SafeHTTPSHandler(context=_tls_context()),
                redirect_handler or SafeRedirectHandler(allowed_hosts)]
    if cookie_jar is not None:
        handlers.append(HTTPCookieProcessor(cookie_jar))
    return SafeOpener(build_opener(*handlers), allowed_hosts)
