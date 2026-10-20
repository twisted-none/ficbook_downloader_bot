import unittest
from unittest.mock import patch
from urllib.error import URLError
import requests

from fanficfare.exceptions import HTTPErrorFFF
from fanficfare.fetchers.base_fetcher import FetcherResponse

from src.sources.ao3 import AO3WorkUnavailableError, require_available_work
from src.sources.ficbook import FicbookAccount, FicbookClient, _friendly_download_error, _friendly_preview_error


URL = 'https://archiveofourown.org/works/14454174/navigate?view_adult=true'
NOTICE = b'This work is part of an ongoing challenge and will be revealed soon!'
HIDDEN = b'<html><title>Mystery Work</title><div id="main"><p class="notice">' + NOTICE + b'</p></div></html>'


class AO3AvailabilityTests(unittest.TestCase):
    def setUp(self):
        browser = patch('src.sources.ao3_http.BrowserCollectionClient')
        self.browser = browser.start()
        self.addCleanup(browser.stop)
        self.browser.return_value.fetch.side_effect = URLError('fallback unavailable')

    def test_unrevealed_collection_is_an_access_error_not_a_browser_challenge(self):
        with self.assertRaises(AO3WorkUnavailableError) as caught:
            require_available_work(HIDDEN)
        for format_error in (_friendly_download_error, _friendly_preview_error):
            error = format_error(URL, caught.exception)
            self.assertIn('скрыт', error.user_message)
            self.assertNotIn('браузер', error.user_message)

    def test_same_sentence_inside_story_text_is_not_an_access_error(self):
        require_available_work(b'<div id="main"><div id="workskin"><p class="notice">' + NOTICE + b'</p></div></div>')
        require_available_work(b'<div id="main"><div class="userstuff">' + NOTICE + b'</div></div>')
        require_available_work(b'<div id="main"><div class="userstuff"><p class="notice">' + NOTICE + b'</p></div></div>')

    def test_hidden_work_is_detected_on_successful_response(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        with patch('fanficfare.fetchers.RequestsFetcher.request', return_value=FetcherResponse(HIDDEN, URL)), \
                self.assertRaises(AO3WorkUnavailableError):
            fetcher.request('GET', URL)

    def test_failed_navigation_probes_public_work_for_real_access_reason(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        blocked = HTTPErrorFFF(URL, 403, 'Forbidden', b'<title>Shields are up!</title>')
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=[blocked, FetcherResponse(HIDDEN, URL)]) as send, \
                self.assertRaises(AO3WorkUnavailableError):
            fetcher.request('GET', URL)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[:2], ('GET', 'https://archiveofourown.org/works/14454174?view_adult=true'))

    def test_navigation_redirect_to_home_checks_work_before_accidental_login(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        home = FetcherResponse(b'<h4 class="heading">With an AO3 account, you can:</h4>', 'https://archiveofourown.org/')
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=[home, FetcherResponse(HIDDEN, URL)]) as send:
            with self.assertRaises(AO3WorkUnavailableError):
                fetcher.request('GET', URL)
        self.assertEqual(send.call_count, 2)

    def test_unavailable_probe_preserves_original_error_and_does_not_loop(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        blocked = HTTPErrorFFF(URL, 525, 'TLS handshake failed')
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=[blocked, HTTPErrorFFF(URL, 503, 'Unavailable')]) as send:
            with self.assertRaises(HTTPErrorFFF) as caught:
                fetcher.request('GET', URL)
        self.assertIs(caught.exception, blocked)
        self.assertEqual(send.call_count, 2)

    def test_ordinary_denial_and_login_post_do_not_trigger_probe(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        for method, code, body in (('GET', 403, b'Forbidden'), ('POST', 525, b''), ('GET', 429, b'')):
            with self.subTest(method=method, code=code):
                with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=HTTPErrorFFF(URL, code, 'Denied', body)) as send:
                    with self.assertRaises(HTTPErrorFFF):
                        fetcher.request(method, URL)
                self.assertEqual(send.call_count, 1)

    def test_challenge_uses_anonymous_bounded_public_fallback(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        blocked = HTTPErrorFFF(URL, 403, 'Forbidden', b'<title>Shields are up!</title>')
        self.browser.return_value.fetch.side_effect = None
        self.browser.return_value.fetch.return_value = (b'<html>Public navigation</html>', URL)
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=blocked):
            result = fetcher.request('GET', URL, headers={'Cookie': 'fake-secret', 'Authorization': 'fake-secret'})
        self.assertEqual(result.content, b'<html>Public navigation</html>')
        self.browser.return_value.fetch.assert_called_once_with(URL)
        self.browser.return_value.close.assert_called_once()
        self.assertNotIn('fake-secret', repr(self.browser.call_args))

    def test_anonymous_fallback_never_handles_login_posts_or_rate_limits(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        for method, code in (('POST', 525), ('GET', 429)):
            with self.subTest(method=method, code=code), \
                    patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=HTTPErrorFFF(URL, code, 'Denied')):
                with self.assertRaises(HTTPErrorFFF):
                    fetcher.request(method, URL)
        self.browser.assert_not_called()

    def test_public_read_timeout_can_use_fallback_but_tls_errors_cannot(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        self.browser.return_value.fetch.side_effect = None
        self.browser.return_value.fetch.return_value = (b'<html>Public navigation</html>', URL)
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=requests.Timeout('read timeout')):
            self.assertEqual(fetcher.request('GET', URL).content, b'<html>Public navigation</html>')
        self.browser.reset_mock()
        with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=requests.exceptions.SSLError('bad certificate')):
            with self.assertRaises(requests.exceptions.SSLError):
                fetcher.request('GET', URL)
        self.browser.assert_not_called()

    def test_get_with_explicit_body_is_not_replayed_anonymously(self):
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        for body in ({'parameters': b''}, {'json': {}}):
            with self.subTest(body=body), \
                    patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=HTTPErrorFFF(URL, 525, 'Failed')):
                with self.assertRaises(HTTPErrorFFF):
                    fetcher.request('GET', 'https://archiveofourown.org/works/14454174', **body)
        self.browser.assert_not_called()
