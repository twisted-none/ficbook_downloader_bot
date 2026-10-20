from dataclasses import replace
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from curl_cffi import CurlECode, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException

from src.core.limits import ResourceLimitError, ResourceLimits, ResponseLimitError, resource_scope
from src.sources.collection_http import BrowserCollectionClient
from src.sources.collections import _page_url


URL = 'https://ficbook.net/collections/123'


def address(ip='93.184.216.34'):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))


class BrowserCollectionClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.curl_options = {}
        self.factory = patch('src.sources.collection_http.Session', return_value=self.session).start()
        patch('src.sources.safe_http.socket.getaddrinfo', return_value=[address()]).start()
        patch('src.sources.collection_http.getproxies', return_value={}).start()
        self.addCleanup(patch.stopall)
        self.client = BrowserCollectionClient(lambda target: _page_url(target, URL))
        self.addCleanup(self.client.close)
        self.chunks = [b'<html>page</html>']
        self.status = 200
        self.headers = {}
        self.session.get.side_effect = self.respond

    def respond(self, url, **options):
        for chunk in self.chunks:
            if options['content_callback'](chunk) == CURL_WRITEFUNC_ERROR:
                raise RequestException('callback aborted')
        return SimpleNamespace(status_code=self.status, headers=self.headers, url=url)

    def test_verified_direct_https_pins_dns_and_never_accepts_credentials(self):
        body, url = self.client.fetch(URL)
        self.assertEqual(body, b'<html>page</html>')
        self.assertEqual(url, URL)
        self.factory.assert_called_once_with(impersonate='chrome', trust_env=False)
        options = self.session.get.call_args.kwargs
        self.assertIs(options['verify'], True)
        self.assertIs(options['allow_redirects'], False)
        self.assertNotIn('auth', options)
        self.assertNotIn('cookies', options)
        self.assertFalse({'cookie', 'authorization', 'proxy-authorization'} & {key.lower() for key in options['headers']})
        self.assertEqual(self.session.curl_options[CurlOpt.RESOLVE], [b'ficbook.net:443:93.184.216.34'])
        self.assertEqual(self.session.curl_options[CurlOpt.PROXY], b'')
        self.assertLessEqual(options['timeout'], ResourceLimits().max_response_seconds)

    def test_ao3_allowlist_is_per_client_and_keeps_collection_hosts_unchanged(self):
        url = 'https://archiveofourown.org/works/14454174'
        client = BrowserCollectionClient(lambda target: None, allowed_hosts=frozenset({'archiveofourown.org'}))
        self.addCleanup(client.close)
        self.assertEqual(client.fetch(url)[1], url)
        self.assertEqual(self.session.curl_options[CurlOpt.RESOLVE], [b'archiveofourown.org:443:93.184.216.34'])
        self.session.get.reset_mock()
        with self.assertRaises(URLError):
            self.client.fetch(url)
        self.session.get.assert_not_called()
        self.status, self.headers = 302, {'Location': 'https://ficbook.net/collections/123'}
        with self.assertRaises(URLError):
            client.fetch(url)
        self.session.get.assert_called_once()

    def test_private_or_mixed_dns_is_rejected_before_the_client_is_created(self):
        for ip in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '100.64.0.1'):
            with self.subTest(ip=ip), patch('src.sources.safe_http.socket.getaddrinfo', return_value=[address(), address(ip)]):
                with self.assertRaises(URLError):
                    self.client.fetch(URL)
        self.factory.assert_not_called()

    def test_urls_outside_the_collection_are_rejected_before_connecting(self):
        for url in ('http://ficbook.net/collections/123', 'https://ficbook.net/collections/999',
                    'https://ficbook.net/login', 'https://user@ficbook.net/collections/123',
                    'https://ficbook.net:8443/collections/123', 'https://127.0.0.1/'):
            with self.subTest(url=url), self.assertRaises((URLError, ValueError)):
                self.client.fetch(url)
        self.factory.assert_not_called()

    def test_cross_collection_or_external_redirects_are_not_followed(self):
        self.status = 302
        for target in ('/login', '/collections/999', 'https://127.0.0.1/', 'http://ficbook.net/collections/123'):
            self.headers = {'Location': target}
            self.session.get.reset_mock()
            with self.subTest(target=target), self.assertRaises((URLError, ValueError)):
                self.client.fetch(URL)
            self.session.get.assert_called_once()

    def test_valid_redirect_is_checked_and_followed_manually(self):
        self.session.get.side_effect = [SimpleNamespace(status_code=302, headers={'Location': '?p=2'}, url=URL),
                                        SimpleNamespace(status_code=200, headers={}, url=URL + '?p=2')]
        self.assertEqual(self.client.fetch(URL)[1], URL + '?p=2')
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list], [URL, URL + '?p=2'])
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in self.session.get.call_args_list))

    def test_redirect_with_control_characters_is_rejected_before_normalization(self):
        self.status, self.headers = 302, {'Location': '\n?p=2'}
        with self.assertRaises(URLError):
            self.client.fetch(URL)
        self.session.get.assert_called_once()

    def test_unexpected_effective_url_is_never_accepted_as_the_requested_page(self):
        self.session.get.side_effect = None
        self.session.get.return_value = SimpleNamespace(status_code=200, headers={}, url='https://evil.test/')
        with self.assertRaises(URLError):
            self.client.fetch(URL)

    def test_ipv6_dns_pin_uses_brackets(self):
        entry = (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2606:4700:4700::1111', 443, 0, 0))
        with patch('src.sources.safe_http.socket.getaddrinfo', return_value=[entry]):
            self.client.fetch(URL)
        self.assertEqual(self.session.curl_options[CurlOpt.RESOLVE], [b'ficbook.net:443:[2606:4700:4700::1111]'])

    def test_redirect_loop_is_bounded(self):
        self.status, self.headers = 302, {'Location': URL}
        with self.assertRaises(URLError):
            self.client.fetch(URL)
        self.session.get.assert_called_once()

    def test_body_limit_propagates_through_native_callback_abort(self):
        self.chunks = [b'123', b'45', b'6']
        with resource_scope(replace(ResourceLimits(), max_response_bytes=5)):
            with self.assertRaises(ResponseLimitError):
                self.client.fetch(URL)
        self.session.get.assert_called_once()

    def test_native_declared_size_rejection_is_reported_as_a_resource_limit(self):
        self.session.get.side_effect = RequestException('too large', code=CurlECode.FILESIZE_EXCEEDED)
        with self.assertRaises(ResponseLimitError):
            self.client.fetch(URL)

    def test_truncated_identity_response_is_rejected(self):
        self.headers = {'Content-Length': '100'}
        with self.assertRaises(URLError):
            self.client.fetch(URL)

    def test_decompressed_bytes_are_capped_even_if_wire_length_is_small(self):
        self.headers = {'Content-Encoding': 'gzip', 'Content-Length': '1'}
        self.chunks = [b'123456']
        with resource_scope(replace(ResourceLimits(), max_response_bytes=5)):
            with self.assertRaises(ResponseLimitError):
                self.client.fetch(URL)

    def test_deadline_applies_to_native_timeout_and_body_callback(self):
        with resource_scope(replace(ResourceLimits(), max_response_seconds=2)):
            self.client.fetch(URL)
        self.assertLessEqual(self.session.get.call_args.kwargs['timeout'], 2)
        def expired(url, **options):
            with patch('src.sources.collection_http.check_deadline', side_effect=ResourceLimitError('expired')):
                return self.respond(url, **options)
        self.session.get.side_effect = expired
        with self.assertRaisesRegex(ResourceLimitError, 'expired'):
            self.client.fetch(URL)

    def test_http_failures_keep_status_and_challenge_header(self):
        self.status, self.headers = 403, {'cf-mitigated': 'challenge'}
        with self.assertRaises(HTTPError) as error:
            self.client.fetch(URL)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(error.exception.headers['cf-mitigated'], 'challenge')
        error.exception.close()

    def test_network_error_does_not_expose_library_details(self):
        self.session.get.side_effect = RequestException('sensitive connection details')
        with self.assertRaises(URLError) as error:
            self.client.fetch(URL)
        self.assertNotIn('sensitive', str(error.exception))

    def test_configured_proxy_is_not_silently_bypassed(self):
        with patch('src.sources.collection_http.getproxies', return_value={'https': 'http://name:secret@127.0.0.1:8080'}), patch(
            'src.sources.collection_http.proxy_bypass', return_value=False,
        ):
            with self.assertRaises(URLError) as error:
                self.client.fetch(URL)
        self.assertNotIn('secret', str(error.exception))
        self.factory.assert_not_called()

    def test_session_lives_only_until_its_collection_finishes(self):
        self.client.fetch(URL)
        self.client.fetch(URL + '?p=2')
        self.factory.assert_called_once()
        self.client.close()
        self.session.close.assert_called_once()

    def test_closed_session_cannot_be_reused(self):
        self.client.close()
        with self.assertRaises(URLError):
            self.client.fetch(URL)
        self.factory.assert_not_called()
