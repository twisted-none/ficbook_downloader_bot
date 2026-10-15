from email.message import Message
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from bs4 import BeautifulSoup

from src.sources.litnet import LitnetClient, LitnetLoginError
from src.sources.ranobelib import (
    AUTH_CALLBACK_URL, RanobelibClient, RanobelibLoginError, _OAuthCallback, _OAuthRedirectHandler,
)
from src.sources.safe_http import UnsafeRequestError


class SourceHTTPSecurityTests(unittest.TestCase):
    def test_litnet_never_posts_login_to_foreign_http_or_nonstandard_port(self):
        actions = ("https://evil.example/auth/login", "http://litnet.com/auth/login",
                   "https://litnet.com:444/auth/login", "https://user:secret@litnet.com/auth/login")
        for action in actions:
            with self.subTest(action=action):
                client = LitnetClient("fake-user", "fake-password")
                page = BeautifulSoup(f'<form action="{action}"><input name="_csrf" value="fake"></form>', "lxml")
                client._soup = Mock(return_value=page)
                with self.assertRaises(LitnetLoginError):
                    client._login_if_configured()
                self.assertEqual(client._soup.call_count, 1)

    def test_ranobelib_does_not_construct_bearer_request_for_foreign_origin(self):
        client = RanobelibClient(min_request_interval=0)
        client._access_token = "fake-token"
        for url in ("https://evil.example/api/manga/1", "http://api.cdnlibs.org/api/manga/1",
                    "https://api.cdnlibs.org:444/api/manga/1"):
            with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                client._api_request(url)

    def test_auth_url_validation_rejects_userinfo_and_nonstandard_port(self):
        client = RanobelibClient()
        for url in ("https://user:secret@auth.lib.social/auth/login", "https://auth.lib.social:444/auth/login"):
            with self.subTest(url=url), self.assertRaises(RanobelibLoginError):
                client._require_auth_host(url)

    def test_oauth_callback_must_match_exact_origin_and_path(self):
        client = RanobelibClient()
        bad = ("https://evil.example/ru/front/auth/oauth/callback", AUTH_CALLBACK_URL + "evil",
               AUTH_CALLBACK_URL.replace("https:", "http:"), AUTH_CALLBACK_URL.replace("ranobelib.me", "ranobelib.me.evil.example"))
        for url in bad:
            with self.subTest(url=url), self.assertRaises(RanobelibLoginError):
                client._oauth_code(url + "?code=fake-code&state=fake-state", "fake-state")
        self.assertEqual(client._oauth_code(AUTH_CALLBACK_URL + "?code=fake-code&state=fake-state", "fake-state"), "fake-code")
        with self.assertRaises(RanobelibLoginError):
            client._oauth_code(AUTH_CALLBACK_URL + "?code=a&code=b&state=fake-state", "fake-state")

    def test_auth_redirects_only_allow_auth_origin_or_exact_callback(self):
        handler = _OAuthRedirectHandler()
        request = Request("https://auth.lib.social/auth/login", data=b"password=fake-password")
        for url in ("https://evil.example/", "http://auth.lib.social/auth/login", AUTH_CALLBACK_URL + "evil?code=fake"):
            with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        callback = AUTH_CALLBACK_URL + "?code=fake-code&state=fake-state"
        with self.assertRaises(_OAuthCallback) as raised:
            handler.redirect_request(request, None, 302, "Found", {}, callback)
        self.assertNotIn("fake-code", str(raised.exception))

    def test_covers_use_anonymous_client_and_skip_unsafe_targets(self):
        litnet, ranobelib = LitnetClient(), RanobelibClient(min_request_interval=0)
        for client in (litnet, ranobelib):
            self.assertIsNot(client._cover_opener, getattr(client, "_opener", getattr(client, "_auth_opener", None)))
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://[::1]/a", "https://10.0.0.1/a"):
            with self.subTest(url=url):
                soup = BeautifulSoup(f'<meta property="og:image" content="{url}">', "lxml")
                self.assertIsNone(litnet._cover(soup))
                self.assertIsNone(ranobelib._cover({"default": url}))

    def test_litnet_cover_is_bounded_before_read_and_contains_no_login_headers(self):
        client = LitnetClient("fake-user", "fake-password")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = Message()
        response.headers["Content-Type"] = "image/jpeg"
        response.read.return_value = b"fake-jpeg"
        client._cover_opener = Mock(open=Mock(return_value=response))
        client._opener = Mock()
        soup = BeautifulSoup('<meta property="og:image" content="https://cdn.example/a.jpg">', "lxml")
        self.assertIsNotNone(client._cover(soup))
        request = client._cover_opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.data)
        response.read.assert_called_once_with(8 * 1024 * 1024 + 1)
        client._opener.open.assert_not_called()

    def test_malformed_cover_does_not_abort_either_source(self):
        for client, value in (
            (LitnetClient(), BeautifulSoup('<meta property="og:image" content="https://cdn.example/a.jpg">', "lxml")),
            (RanobelibClient(min_request_interval=0), {"default": "https://cdn.example/a.jpg"}),
        ):
            with self.subTest(source=type(client).__name__):
                client._cover_opener = Mock(open=Mock(side_effect=ValueError("malformed remote image")))
                self.assertIsNone(client._cover(value))


if __name__ == "__main__":
    unittest.main()
