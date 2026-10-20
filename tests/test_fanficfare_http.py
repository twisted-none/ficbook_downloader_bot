from io import BytesIO
from email.message import Message
import gzip
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fanficfare.exceptions import HTTPErrorFFF

from src.sources.fanficfare_http import SafeFanficFareFetcher
from src.sources.safe_http import UnsafeRequestError
from src.core.limits import ResourceLimitError, read_bounded
from tests.test_formats_cover import PNG_1X1


class Response(BytesIO):
    headers = {}

    def geturl(self):
        return 'https://ficbook.net/readfic/1'


class FanficFareTransportTests(unittest.TestCase):
    def fetcher(self):
        return SafeFanficFareFetcher(lambda name, default='': default, lambda name: [], 'ficbook.net')

    def test_foreign_pages_and_credential_posts_are_blocked_before_open(self):
        fetcher = self.fetcher()
        with patch('src.sources.fanficfare_http.build_safe_opener') as build:
            for method, url in (('GET', 'https://evil.example/book'),
                                ('POST', 'https://evil.example/login'),
                                ('GET', 'http://ficbook.net/readfic/1'),
                                ('GET', 'https://user:secret@ficbook.net/readfic/1')):
                with self.subTest(method=method, url=url), self.assertRaises(UnsafeRequestError):
                    fetcher.request(method, url, parameters={'password': 'dummy'} if method == 'POST' else None)
            build.assert_not_called()

    def test_source_request_uses_pinned_transport_and_its_cookiejar(self):
        fetcher = self.fetcher()
        opener = Mock()
        opener.open.return_value = Response(b'body')
        with patch('src.sources.fanficfare_http.build_safe_opener', return_value=opener) as build:
            response = fetcher.request('GET', 'https://ficbook.net/readfic/1')
        self.assertEqual(response.content, b'body')
        self.assertIs(build.call_args.kwargs['cookie_jar'], fetcher.get_cookiejar())
        self.assertIn('ficbook.net', build.call_args.args[0])

    def test_external_cover_does_not_receive_site_cookies_or_auth_headers(self):
        fetcher = self.fetcher()
        opener = Mock()
        opener.open.return_value = Response(PNG_1X1)
        with patch('src.sources.fanficfare_http.build_safe_opener', return_value=opener) as build:
            fetcher.request('GET', 'https://cdn.example/cover.png', headers={
                'Accept': 'image/*', 'Authorization': 'Bearer dummy', 'Cookie': 'session=dummy',
                'Referer': 'https://ficbook.net/private',
            })
        self.assertIsNone(build.call_args.kwargs.get('cookie_jar'))
        headers = {key.lower() for key in opener.open.call_args.args[0].headers}
        self.assertTrue(headers.isdisjoint({'authorization', 'cookie', 'referer'}))

    def test_legacy_source_http_is_upgraded_but_foreign_http_is_not(self):
        fetcher = self.fetcher()
        self.assertEqual(fetcher.condition_url('http://ficbook.net/readfic/1#note'), 'https://ficbook.net/readfic/1')
        with self.assertRaises(UnsafeRequestError):
            fetcher.condition_url('http://evil.example/a')

    def test_gzip_bomb_is_rejected_after_bounded_decompression(self):
        body = Response(gzip.compress(b'x' * 10000))
        body.headers = {'Content-Encoding': 'gzip'}
        with self.assertRaises(ResourceLimitError):
            read_bounded(body, 200)

    def test_successful_response_is_passed_to_fanficfare_without_challenge_interception(self):
        response = Response(b'<html>source page</html>')
        response.headers = {'CF-Mitigated': 'challenge'}
        opener = Mock()
        opener.open.return_value = response
        with patch('src.sources.fanficfare_http.build_safe_opener', return_value=opener), \
                patch('src.sources.collections.BrowserCollectionClient') as browser:
            result = self.fetcher().request('GET', 'https://ficbook.net/readfic/1')
        self.assertEqual(result.content, b'<html>source page</html>')
        self.assertEqual(result.redirecturl, 'https://ficbook.net/readfic/1')
        self.assertTrue(response.closed)
        browser.assert_not_called()

    def test_http_errors_keep_standard_fanficfare_handling_even_with_challenge_header(self):
        url = 'https://ficbook.net/readfic/1'
        for status in (403, 503):
            with self.subTest(status=status):
                headers = Message()
                headers['cF-MiTiGaTeD'] = 'challenge'
                headers['Set-Cookie'] = 'private=dummy'
                response = HTTPError(url, status, 'Error', headers, Response(b'source error page'))
                opener = Mock()
                opener.open.side_effect = response
                with patch('src.sources.fanficfare_http.build_safe_opener', return_value=opener), \
                        patch('src.sources.collections.BrowserCollectionClient') as browser:
                    with self.assertRaises(HTTPErrorFFF) as raised:
                        self.fetcher().request('GET', url)
                self.assertIs(type(raised.exception), HTTPErrorFFF)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.data, b'source error page')
                self.assertNotIn('private', str(raised.exception))
                self.assertTrue(response.closed)
                browser.assert_not_called()

    def test_plain_forbidden_does_not_become_a_cloudflare_challenge(self):
        url = 'https://ficbook.net/readfic/1'
        response = HTTPError(url, 403, 'Forbidden', {}, Response(b'Access denied'))
        opener = Mock()
        opener.open.side_effect = response
        with patch('src.sources.fanficfare_http.build_safe_opener', return_value=opener):
            with self.assertRaises(HTTPErrorFFF) as raised:
                self.fetcher().request('GET', url)
        self.assertIs(type(raised.exception), HTTPErrorFFF)
        self.assertEqual(raised.exception.data, b'Access denied')
        self.assertTrue(response.closed)


if __name__ == '__main__':
    unittest.main()
