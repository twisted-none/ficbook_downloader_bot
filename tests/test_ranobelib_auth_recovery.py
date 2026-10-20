import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from src.sources.ranobelib import RanobelibAccessError, RanobelibClient, RanobelibLoginError
from src.sources.safe_http import UnsafeRequestError


URL = 'https://api.cdnlibs.org/api/manga/test'


def response(data):
    result = Mock(headers={}, read=Mock(return_value=json.dumps({'data': data}).encode()))
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class RanobelibAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = RanobelibClient('fake-reader', 'fake-password', min_request_interval=0, retry_attempts=1)

    def test_public_book_is_available_even_if_configured_login_is_broken(self):
        self.client._api_opener = Mock(open=Mock(return_value=response({'id': 31524})))
        with patch.object(self.client, '_login_locked', side_effect=RanobelibLoginError('blocked')) as login:
            self.assertEqual(self.client._request_json(URL), {'id': 31524})
        login.assert_not_called()
        request = self.client._api_opener.open.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))

    def test_authentication_is_only_attempted_after_api_requires_it(self):
        unauthorized = HTTPError(URL, 401, 'Unauthorized', {}, None)
        self.client._api_opener = Mock(open=Mock(side_effect=[unauthorized, response({'id': 1})]))

        def login():
            self.assertEqual(self.client._api_opener.open.call_count, 1)
            self.client._access_token = 'fake-access'

        with patch.object(self.client, '_login_locked', side_effect=login) as authenticate:
            self.assertEqual(self.client._request_json(URL), {'id': 1})
        authenticate.assert_called_once()
        second = self.client._api_opener.open.call_args.args[0]
        self.assertEqual(second.get_header('Authorization'), 'Bearer fake-access')

    def test_expired_token_is_refreshed_after_401(self):
        self.client._access_token, self.client._refresh_token = 'expired', 'fake-refresh'
        self.client._api_opener = Mock(open=Mock(side_effect=[
            HTTPError(URL, 401, 'Unauthorized', {}, None), response({'id': 1}),
        ]))

        def refresh():
            self.client._access_token = 'renewed'
            return True

        with patch.object(self.client, '_refresh_locked', side_effect=refresh) as renew, \
                patch.object(self.client, '_login_locked') as login:
            self.assertEqual(self.client._request_json(URL), {'id': 1})
        renew.assert_called_once()
        login.assert_not_called()

    def test_late_401_does_not_repeat_login_after_another_request_updated_token(self):
        self.client._access_token = 'expired'

        def send(request, **kwargs):
            if request.get_header('Authorization') == 'Bearer expired':
                self.client._access_token = 'renewed-by-another-request'
                raise HTTPError(URL, 401, 'Unauthorized', {}, None)
            return response({'id': 1})

        self.client._api_opener = Mock(open=Mock(side_effect=send))
        with patch.object(self.client, '_login_locked') as login:
            self.assertEqual(self.client._request_json(URL), {'id': 1})
        login.assert_not_called()
        self.assertEqual(self.client._api_opener.open.call_count, 2)

    def test_second_401_does_not_start_an_authentication_loop(self):
        self.client._api_opener = Mock(open=Mock(side_effect=[
            HTTPError(URL, 401, 'Unauthorized', {}, None), HTTPError(URL, 401, 'Unauthorized', {}, None),
        ]))
        with patch.object(self.client, '_login_locked') as login, self.assertRaises(RanobelibLoginError):
            self.client._request_json(URL)
        login.assert_called_once()
        self.assertEqual(self.client._api_opener.open.call_count, 2)

    def test_forbidden_content_does_not_trigger_login_or_bypass(self):
        self.client._api_opener = Mock(open=Mock(side_effect=HTTPError(URL, 403, 'Forbidden', {}, None)))
        with patch.object(self.client, '_login_locked') as login, self.assertRaises(RanobelibAccessError):
            self.client._request_json(URL)
        login.assert_not_called()

    def test_auth_error_diagnostics_distinguish_http_timeout_and_policy_without_secrets(self):
        errors = (
            (HTTPError('https://auth.lib.social/auth/login?token=fake-secret', 422, 'fake-secret', {}, None), 'HTTP 422'),
            (TimeoutError('fake-secret'), 'timeout'),
            (UnsafeRequestError('fake-secret'), 'security policy'),
            (URLError('fake-secret'), 'network'),
        )
        for error, marker in errors:
            with self.subTest(marker=marker):
                self.client._auth_opener = Mock(open=Mock(side_effect=error))
                with self.assertRaises(RanobelibLoginError) as caught:
                    self.client._auth_submit('https://auth.lib.social/auth/login', {}, 'https://auth.lib.social/auth/login')
                self.assertIn(marker, str(caught.exception))
                self.assertNotIn('fake-secret', str(caught.exception))
                if marker != 'security policy':
                    self.assertNotIn('security policy', str(caught.exception))
