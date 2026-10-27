from urllib.error import URLError
import unittest
from unittest.mock import patch

import requests
from fanficfare.exceptions import HTTPErrorFFF
from fanficfare.fetchers.base_fetcher import FetcherResponse

from src.sources.ficbook import (FicbookAccount, FicbookClient, _friendly_download_error,
                                 _friendly_preview_error, _is_ao3_transient_failure)

URL = 'https://archiveofourown.org/works/14860466?view_adult=true'


class AO3RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        self.browser = self.enterContext(patch('src.sources.ao3_http.BrowserCollectionClient'))
        self.browser.return_value.fetch.side_effect = URLError('public transport failed')
        self.sleep = self.enterContext(patch('src.sources.ao3_http.bounded_sleep'))

    def test_timeout_then_failed_public_fallback_can_recover_with_one_final_get(self):
        with patch('fanficfare.fetchers.RequestsFetcher.request',
                   side_effect=[requests.Timeout('read timeout'), FetcherResponse(b'work', URL)]) as send:
            result = self.fetcher.request('GET', URL)
        self.assertEqual(result.content, b'work')
        self.assertEqual(send.call_count, 2)
        self.browser.return_value.fetch.assert_called_once_with(URL)

    def test_navigation_525_retries_same_page_instead_of_an_access_probe(self):
        navigation = URL.replace('?view_adult=true', '/navigate?view_adult=true')
        with patch('fanficfare.fetchers.RequestsFetcher.request',
                   side_effect=[HTTPErrorFFF(navigation, 525, 'TLS failed'), FetcherResponse(b'chapters', navigation)]) as send:
            self.assertEqual(self.fetcher.request('GET', navigation).content, b'chapters')
        self.assertEqual(send.call_args.args[:2], ('GET', navigation))
        self.assertEqual(send.call_count, 2)

    def test_exhaustion_logs_both_transports_without_private_error_bodies(self):
        error = HTTPErrorFFF(URL, 525, 'SSL handshake failed', b'Cloudflare private-body')
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=error) as send, \
                self.assertLogs('src.sources.ao3_http', level='WARNING') as logs:
            with self.assertRaises(HTTPErrorFFF) as caught:
                self.fetcher.request('GET', URL)
        self.assertEqual(send.call_count, 2)
        self.browser.return_value.fetch.assert_called_once()
        self.assertFalse(_is_ao3_transient_failure(URL, caught.exception))
        output = ' '.join(logs.output)
        self.assertIn('525', output)
        self.assertIn('URLError', output)
        self.assertNotIn('private-body', output)

    def test_525_is_not_reported_as_browser_challenge(self):
        error = HTTPErrorFFF(URL, 525, 'SSL handshake failed', b'Cloudflare')
        for formatter in (_friendly_download_error, _friendly_preview_error):
            text = formatter(URL, error).user_message
            self.assertNotIn('браузер', text)
            self.assertIn('соединени', text)

    def test_transient_server_failure_recovers_but_denials_are_not_retried(self):
        with patch('fanficfare.fetchers.RequestsFetcher.request',
                   side_effect=[HTTPErrorFFF(URL, 503, 'Unavailable'), FetcherResponse(b'work', URL)]) as send:
            self.assertEqual(self.fetcher.request('GET', URL).content, b'work')
        self.assertEqual(send.call_count, 2)
        for code in (401, 403, 404, 429, 526):
            self.browser.reset_mock()
            with self.subTest(code=code), patch('fanficfare.fetchers.RequestsFetcher.request',
                    side_effect=HTTPErrorFFF(URL, code, 'Denied')) as send, self.assertRaises(HTTPErrorFFF):
                self.fetcher.request('GET', URL)
            self.assertEqual(send.call_count, 1)
            self.browser.assert_not_called()

    def test_unsafe_tls_and_posts_are_never_replayed(self):
        for method, error in (('POST', requests.Timeout('timeout')),
                              ('GET', requests.exceptions.SSLError('Invalid certificate'))):
            with self.subTest(method=method), patch('fanficfare.fetchers.RequestsFetcher.request',
                    side_effect=error) as send, self.assertRaises(type(error)):
                self.fetcher.request(method, URL)
            self.assertEqual(send.call_count, 1)
        self.browser.assert_not_called()

    def test_broken_chunk_is_retried_without_restarting_the_download(self):
        from urllib3.exceptions import ProtocolError
        with patch('fanficfare.fetchers.RequestsFetcher.request',
                   side_effect=[ProtocolError('Connection broken'), FetcherResponse(b'work', URL)]) as send:
            self.assertEqual(self.fetcher.request('GET', URL).content, b'work')
        self.assertEqual(send.call_count, 2)

    def test_public_rate_limit_stops_further_requests(self):
        from urllib.error import HTTPError
        self.browser.return_value.fetch.side_effect = HTTPError(URL, 429, 'Limit', {}, None)
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=requests.Timeout('timeout')) as send:
            with self.assertRaises(requests.Timeout):
                self.fetcher.request('GET', URL)
        self.assertEqual(send.call_count, 1)

    def test_chapter_loader_does_not_repeat_exhausted_page_budget(self):
        from types import SimpleNamespace
        client = FicbookClient(retry_attempts=3, retry_base_delay=0)
        adapter = SimpleNamespace(getChapterTextNum=lambda *args: self.fetcher.request('GET', URL))
        error = HTTPErrorFFF(URL, 525, 'Failed')
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=error) as send:
            with self.assertRaises(HTTPErrorFFF):
                client._fanficfare_chapter_html(adapter, URL, URL, 1, 1, 1, None)
        self.assertEqual(send.call_count, 2)
        self.browser.return_value.fetch.assert_called_once()
