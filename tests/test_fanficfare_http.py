from io import BytesIO
import gzip
import unittest
from unittest.mock import Mock, patch

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


if __name__ == '__main__':
    unittest.main()
