from io import BytesIO
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from src.sources.safe_http import (
    PublicHTTPSConnection, SafeOpener, SafeProxyHandler, SafeRedirectHandler, UnsafeRequestError,
    build_safe_opener, require_https_url, resolve_addresses,
)


def answer(ip, family=socket.AF_INET):
    address = (ip, 443) if family == socket.AF_INET else (ip, 443, 0, 0)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


class SafeURLTests(unittest.TestCase):
    def test_unsafe_urls_are_rejected_without_network_access(self):
        urls = (
            "http://cdn.example/cover.jpg", "file:///etc/passwd", "data:image/png;base64,x",
            "https://127.0.0.1/a", "https://10.0.0.1/a", "https://169.254.169.254/a",
            "https://[::1]/a", "https://[::ffff:127.0.0.1]/a", "https://[fe80::1%25eth0]/a",
            "https://user:secret@cdn.example/a", "https://cdn.example:444/a", "https://[invalid/a",
            "https://cdn.example\\@localhost/a", "https://cdn.example/\na", " https://cdn.example/a",
        )
        with patch("src.sources.safe_http.socket.getaddrinfo") as dns:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                    require_https_url(url)
            dns.assert_not_called()

    def test_allowed_hosts_are_exact_and_https_only(self):
        hosts = frozenset({"litnet.com"})
        self.assertEqual(require_https_url("https://litnet.com:443/auth/login", hosts).hostname, "litnet.com")
        for url in ("https://litnet.com.evil.example/auth/login", "https://evil.example/?litnet.com",
                    "https://www.litnet.com/auth/login", "http://litnet.com/auth/login"):
            with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                require_https_url(url, hosts)

    def test_dns_rejects_every_non_public_address_before_connecting(self):
        ips = ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "100.64.0.1",
               "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1",
               "64:ff9b::7f00:1", "2002:7f00:1::", "192.0.0.8")
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            with self.subTest(ip=ip), patch("src.sources.safe_http.socket.getaddrinfo", return_value=[
                answer("93.184.216.34"), answer(ip, family)
            ]), self.assertRaises(UnsafeRequestError):
                resolve_addresses("cdn.example", 443)

    def test_legacy_numeric_ip_spellings_are_checked_after_resolution(self):
        with patch("src.sources.safe_http.socket.getaddrinfo", return_value=[answer("127.0.0.1")]):
            for host in ("2130706433", "0x7f000001", "127.1", "0177.0.0.1"):
                with self.subTest(host=host), self.assertRaises(UnsafeRequestError):
                    resolve_addresses(host, 443)


class PinnedConnectionTests(unittest.TestCase):
    def context(self):
        return Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)

    def test_direct_connection_uses_validated_ip_once_and_preserves_tls_hostname(self):
        context = self.context()
        sock = Mock()
        with patch("src.sources.safe_http.socket.getaddrinfo", side_effect=[
            [answer("93.184.216.34")], [answer("127.0.0.1")]
        ]) as dns, patch("src.sources.safe_http.socket.socket", return_value=sock):
            connection = PublicHTTPSConnection("cdn.example", context=context, timeout=3)
            connection.connect()
            dns.assert_called_once()
            sock.connect.assert_called_once_with(("93.184.216.34", 443))
            context.wrap_socket.assert_called_once_with(sock, server_hostname="cdn.example")

    def test_private_dns_answer_never_reaches_socket(self):
        with patch("src.sources.safe_http.socket.getaddrinfo", return_value=[answer("10.0.0.1")]), patch(
            "src.sources.safe_http.socket.socket"
        ) as factory, self.assertRaises(UnsafeRequestError):
            PublicHTTPSConnection("cdn.example", context=self.context()).connect()
        factory.assert_not_called()

    def test_http_proxy_tunnels_to_pinned_ip_with_original_tls_hostname(self):
        context, sock = self.context(), Mock()
        sock.makefile.return_value = BytesIO(b"HTTP/1.0 200 Connection established\r\n\r\n")

        def dns(host, port, *args):
            if host == "cdn.example":
                return [answer("93.184.216.34")]
            self.assertEqual((host, port), ("proxy.example", 3128))
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.2", port))]

        with patch("src.sources.safe_http.socket.getaddrinfo", side_effect=dns), patch(
            "src.sources.safe_http.socket.socket", return_value=sock
        ):
            connection = PublicHTTPSConnection("proxy.example:3128", via_proxy=True, context=context, timeout=3)
            connection.set_tunnel("cdn.example", 443, headers={"Proxy-Authorization": "Basic test-only"})
            connection.connect()
        sock.connect.assert_called_once_with(("10.0.0.2", 3128))
        sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        self.assertIn(b"CONNECT 93.184.216.34:443", sent)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="cdn.example")

    def test_proxy_never_receives_connect_for_private_target(self):
        with patch("src.sources.safe_http.socket.getaddrinfo", return_value=[answer("127.0.0.1")]), patch(
            "src.sources.safe_http.socket.socket"
        ) as factory, self.assertRaises(UnsafeRequestError):
            connection = PublicHTTPSConnection("proxy.example:3128", via_proxy=True, context=self.context())
            connection.set_tunnel("cdn.example", 443)
        factory.assert_not_called()


class SafeRedirectTests(unittest.TestCase):
    def test_cross_origin_redirect_never_forwards_secrets(self):
        handler = SafeRedirectHandler(frozenset({"api.cdnlibs.org"}))
        request = Request("https://api.cdnlibs.org/api/manga/1", headers={"Authorization": "Bearer test-only"})
        for url in ("https://evil.example/a", "http://api.cdnlibs.org/a", "https://127.0.0.1/a"):
            with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_public_cdn_redirect_strips_origin_and_proxy_credentials(self):
        handler = SafeRedirectHandler()
        request = Request("https://cdn.example/a", headers={
            "Authorization": "Bearer test-only", "Cookie": "session=test-only",
            "Proxy-Authorization": "Basic test-only", "Referer": "https://cdn.example/?token=test-only",
        })
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://other.example/a")
        self.assertEqual(redirected.header_items(), [])

    def test_post_is_not_forwarded_to_another_allowed_host(self):
        handler = SafeRedirectHandler(frozenset({"litnet.com", "www.litnet.com"}))
        request = Request("https://litnet.com/auth/login", data=b"password=test-only")
        with self.assertRaises(UnsafeRequestError):
            handler.redirect_request(request, None, 307, "Temporary", {}, "https://www.litnet.com/auth/login")

    def test_same_origin_post_redirect_preserves_body_and_method(self):
        handler = SafeRedirectHandler(frozenset({"litnet.com"}))
        request = Request("https://litnet.com/auth/login", data=b"password=test-only")
        redirected = handler.redirect_request(request, None, 307, "Temporary", {}, "https://litnet.com/auth/login/")
        self.assertEqual(redirected.get_method(), "POST")
        self.assertEqual(redirected.data, request.data)

    def test_redirect_does_not_read_arbitrary_response_body(self):
        handler = SafeRedirectHandler()
        handler.parent = Mock()
        request, response = Request("https://cdn.example/a"), Mock()
        request.timeout = 3
        handler.http_error_302(request, response, 302, "Found", {"Location": "/b"})
        response.read.assert_not_called()
        response.close.assert_called_once()

    def test_redirect_chain_is_bounded(self):
        handler = SafeRedirectHandler()
        handler.parent = Mock()
        request = Request("https://cdn.example/a")
        request.redirect_dict = {str(index): 1 for index in range(5)}
        with self.assertRaises(UnsafeRequestError):
            handler.http_error_302(request, Mock(), 302, "Found", {"Location": "/b"})
        handler.parent.open.assert_not_called()

    def test_initial_non_https_request_is_blocked_before_default_handlers(self):
        backend = Mock()
        opener = SafeOpener(backend)
        with self.assertRaises(UnsafeRequestError):
            opener.open("file:///etc/passwd")
        backend.open.assert_not_called()

    def test_environment_proxy_handler_is_kept_and_covers_have_no_cookie_handler(self):
        with patch("urllib.request.getproxies", return_value={"https": "http://proxy.example:3128"}):
            opener = build_safe_opener()
        handlers = opener.backend.handlers
        self.assertTrue(any(type(h).__name__ == "SafeProxyHandler" for h in handlers))
        self.assertFalse(any(type(h).__name__ == "HTTPCookieProcessor" for h in handlers))

    def test_unsupported_proxy_fails_closed_instead_of_falling_back_to_direct(self):
        handler = SafeProxyHandler({})
        with patch("src.sources.safe_http.proxy_bypass", return_value=False):
            for proxy in ("socks5://proxy.example:1080", "https://proxy.example:443", "http://proxy.example:0"):
                with self.subTest(proxy=proxy), self.assertRaises(UnsafeRequestError):
                    handler.proxy_open(Request("https://cdn.example/a"), proxy, "https")


class UrllibPipelineTests(unittest.TestCase):
    def socket(self, response):
        sock = Mock()
        sock.makefile.return_value = BytesIO(response)
        return sock

    def context(self):
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        context.wrap_socket.side_effect = lambda sock, **kwargs: sock
        return context

    def test_malicious_redirect_never_opens_second_connection(self):
        sock = self.socket(b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/\r\nContent-Length: 0\r\n\r\n")
        with patch("urllib.request.getproxies", return_value={}), patch(
            "src.sources.safe_http._tls_context", return_value=self.context()
        ), patch("src.sources.safe_http.socket.getaddrinfo", return_value=[answer("93.184.216.34")]), patch(
            "src.sources.safe_http.socket.socket", return_value=sock
        ) as factory, self.assertRaises(UnsafeRequestError):
            build_safe_opener().open("https://cdn.example/a")
        self.assertEqual(factory.call_count, 1)
        sock.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_bearer_redirect_is_blocked_before_contacting_foreign_origin(self):
        sock = self.socket(b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/collect\r\nContent-Length: 0\r\n\r\n")
        request = Request("https://api.cdnlibs.org/api/manga/1", headers={"Authorization": "Bearer fake-token"})
        with patch("urllib.request.getproxies", return_value={}), patch(
            "src.sources.safe_http._tls_context", return_value=self.context()
        ), patch("src.sources.safe_http.socket.getaddrinfo", return_value=[answer("93.184.216.34")]) as dns, patch(
            "src.sources.safe_http.socket.socket", return_value=sock
        ) as factory, self.assertRaises(UnsafeRequestError):
            build_safe_opener(frozenset({"api.cdnlibs.org"})).open(request)
        dns.assert_called_once()
        self.assertEqual(factory.call_count, 1)

    def test_ipv6_direct_connection_keeps_original_sni(self):
        sock, context = Mock(), self.context()
        with patch("src.sources.safe_http.socket.getaddrinfo", return_value=[
            answer("2606:4700:4700::1111", socket.AF_INET6)
        ]), patch("src.sources.safe_http.socket.socket", return_value=sock):
            PublicHTTPSConnection("cdn.example", context=context).connect()
        sock.connect.assert_called_once_with(("2606:4700:4700::1111", 443, 0, 0))
        context.wrap_socket.assert_called_once_with(sock, server_hostname="cdn.example")

    def test_public_cdn_redirect_uses_new_checked_ip_and_no_session_headers(self):
        first = self.socket(b"HTTP/1.1 302 Found\r\nLocation: https://other.example/a.jpg\r\n"
                            b"Set-Cookie: session=fake-secret\r\nContent-Length: 0\r\n\r\n")
        second = self.socket(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\nimg")
        context = self.context()
        with patch("urllib.request.getproxies", return_value={}), patch(
            "src.sources.safe_http._tls_context", return_value=context
        ), patch("src.sources.safe_http.socket.getaddrinfo", side_effect=[
            [answer("93.184.216.34")], [answer("93.184.216.35")]
        ]) as dns, patch("src.sources.safe_http.socket.socket", side_effect=[first, second]):
            with build_safe_opener().open("https://cdn.example/a.jpg") as response:
                self.assertEqual(response.read(), b"img")
        self.assertEqual(dns.call_count, 2)
        second.connect.assert_called_once_with(("93.184.216.35", 443))
        sent = b"".join(call.args[0] for call in second.sendall.call_args_list)
        self.assertNotIn(b"Cookie:", sent)
        self.assertNotIn(b"Authorization:", sent)
        self.assertEqual([call.kwargs["server_hostname"] for call in context.wrap_socket.call_args_list],
                         ["cdn.example", "other.example"])

    def test_proxy_authentication_is_only_sent_to_connect_not_the_origin(self):
        sock = Mock()
        sock.makefile.side_effect = [BytesIO(b"HTTP/1.0 200 Connection established\r\n\r\n"),
                                    BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")]

        def dns(host, port, *args):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     ("10.0.0.2" if host == "proxy.example" else "93.184.216.34", port))]

        with patch("urllib.request.getproxies", return_value={"https": "http://user:test-only@proxy.example:3128"}), patch(
            "src.sources.safe_http.proxy_bypass", return_value=False
        ), patch("src.sources.safe_http._tls_context", return_value=self.context()), patch(
            "src.sources.safe_http.socket.getaddrinfo", side_effect=dns
        ), patch("src.sources.safe_http.socket.socket", return_value=sock):
            with build_safe_opener().open("https://cdn.example/a") as response:
                self.assertEqual(response.read(), b"ok")
        sent = [call.args[0] for call in sock.sendall.call_args_list]
        self.assertIn(b"CONNECT 93.184.216.34:443", sent[0])
        self.assertIn(b"Proxy-Authorization:", sent[0])
        self.assertNotIn(b"Proxy-Authorization:", b"".join(sent[1:]))

    def test_pinned_ipv6_proxy_target_is_bracketed(self):
        sock = Mock()
        sock.makefile.return_value = BytesIO(b"HTTP/1.0 200 Connection established\r\n\r\n")

        def dns(host, port, *args):
            if host == "cdn.example":
                return [answer("2606:4700:4700::1111", socket.AF_INET6)]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.2", port))]

        with patch("src.sources.safe_http.socket.getaddrinfo", side_effect=dns), patch(
            "src.sources.safe_http.socket.socket", return_value=sock
        ):
            connection = PublicHTTPSConnection("proxy.example:3128", via_proxy=True, context=self.context())
            connection.set_tunnel("cdn.example", 443)
            connection.connect()
        sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        self.assertIn(b"CONNECT [2606:4700:4700::1111]:443", sent)


if __name__ == "__main__":
    unittest.main()
