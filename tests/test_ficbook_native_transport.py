import unittest
from unittest.mock import Mock, patch

from fanficfare.configurable import Configuration
from fanficfare.fetchers import RequestsFetcher

from src.sources.ficbook import FicbookAccount, FicbookClient


URL = 'https://ficbook.net/readfic/12531381'


class FicbookNativeTransportTests(unittest.TestCase):
    def test_ficbook_uses_original_configuration_and_requests_fetcher(self):
        config = FicbookClient()._build_config(URL, FicbookAccount())
        self.assertIs(type(config), Configuration)
        self.assertIs(type(config.get_fetcher()), RequestsFetcher)

    def test_native_request_does_not_call_urllib_or_collection_browser(self):
        config = FicbookClient()._build_config(URL, FicbookAccount())
        response = Mock(status_code=200, url=URL, content=b'<html>story</html>')
        with patch('requests.sessions.Session.request', return_value=response) as request, \
                patch('src.sources.fanficfare_http.build_safe_opener') as urllib_open, \
                patch('src.sources.collections.BrowserCollectionClient') as browser:
            body, final_url = config.get_fetcher().get_request_redirected(URL, usecache=False)
        self.assertEqual(body, b'<html>story</html>')
        self.assertEqual(final_url, URL)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ('GET', URL))
        self.assertTrue(request.call_args.kwargs['verify'])
        urllib_open.assert_not_called()
        browser.assert_not_called()

    def test_original_login_settings_and_cookiejar_survive_fetcher_refresh(self):
        account = FicbookAccount('test-user', 'dummy-password')
        config = FicbookClient()._build_config(URL, account)
        self.assertEqual(config.getConfig('username'), account.login)
        self.assertEqual(config.getConfig('password'), account.password)
        self.assertTrue(config.getConfig('is_adult'))
        original = config.get_fetcher()
        jar = original.get_cookiejar()
        refreshed = config.get_fetcher(make_new=True)
        self.assertIs(type(refreshed), RequestsFetcher)
        self.assertIs(refreshed.get_cookiejar(), jar)
        self.assertIsNot(original, refreshed)
