from __future__ import annotations

import ssl
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request

from src.sources.ficbook import FicbookAccount, FicbookClient, FicbookError
from src.sources.registry import extract_url, normalize_url


STORY_URL = "https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123"
PUBLIC_PAGE = """
<table><tr><td class="fichead">
<a href="ffshowfic.php?fid=123"><b>Public book</b></a>
<a href="member.php?id=1">Author</a><div>Summary</div>
</td></tr><tr><td class="fichead_chapters">
<select name="chapter"><option value="1">Chapter one</option></select>
</td></tr></table><div id="chap_text"><p>Chapter body.</p></div>
"""


def fake_opener() -> Mock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = PUBLIC_PAGE.encode("cp1251")
    response.geturl.return_value = STORY_URL
    opener = Mock()
    opener.open.return_value = response
    return opener


class HogwartsnetSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FicbookClient()

    def test_legacy_http_links_are_supported_and_upgraded(self) -> None:
        for host in ("hogwartsnet.ru", "www.hogwartsnet.ru"):
            for scheme in ("http", "https"):
                url = f"{scheme}://{host}/mfanf/ffshowfic.php?fid=123&chapter=4#note"
                with self.subTest(url=url):
                    self.assertEqual(extract_url(url), url)
                    self.assertEqual(normalize_url(url), STORY_URL)

    def test_public_preview_and_download_use_https_without_credentials(self) -> None:
        opener = fake_opener()
        with patch("src.sources.ficbook.build_opener", return_value=opener):
            preview = self.client.preview(STORY_URL.replace("https:", "http:"))
            story = self.client.download(STORY_URL.replace("https:", "http:"), chapter_numbers={1})

        self.assertEqual(preview.title, "Public book")
        self.assertEqual(story.source_url, STORY_URL)
        self.assertEqual(len(story.chapters), 1)
        self.assertIn("Chapter body.", story.chapters[0].html)
        self.assertTrue(story.chapters[0].source_url.startswith("https://"))
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.startswith("https://hogwartsnet.ru/"))
            self.assertIsNone(request.data)

    def test_credentials_are_posted_only_to_https(self) -> None:
        opener = fake_opener()
        with patch("src.sources.ficbook.build_opener", return_value=opener):
            self.client._hogwartsnet_opener(STORY_URL, FicbookAccount("test-reader", "dummy-password"))

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, STORY_URL)
        self.assertEqual(request.get_method(), "POST")
        payload = parse_qs(request.data.decode("cp1251"))
        self.assertEqual(payload, {"id": ["test-reader"], "pwd": ["dummy-password"]})

    def test_unsafe_requests_are_rejected_before_network_io(self) -> None:
        urls = (
            STORY_URL.replace("https:", "http:"),
            "https://evil.example/mfanf/ffshowfic.php?fid=123",
            "https://hogwartsnet.ru.evil.example/mfanf/ffshowfic.php?fid=123",
            "https://test-reader:dummy-password@hogwartsnet.ru/mfanf/ffshowfic.php?fid=123",
            "https://hogwartsnet.ru:8443/mfanf/ffshowfic.php?fid=123",
            "https://hogwartsnet.ru:invalid/mfanf/ffshowfic.php?fid=123",
            "https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123\n",
            "https://hogwartsnet.ru\\evil.example/mfanf/ffshowfic.php?fid=123",
            "file:///tmp/story",
        )
        for url in urls:
            with self.subTest(url=url):
                opener = fake_opener()
                with self.assertRaises(FicbookError):
                    self.client._hogwartsnet_open(opener, url, data=b"dummy-credentials")
                opener.open.assert_not_called()

    def test_login_rejects_http_before_sending_credentials(self) -> None:
        opener = fake_opener()
        with patch("src.sources.ficbook.build_opener", return_value=opener):
            with self.assertRaises(FicbookError):
                self.client._hogwartsnet_opener(
                    STORY_URL.replace("https:", "http:"), FicbookAccount("test-reader", "dummy-password")
                )
        opener.open.assert_not_called()

    def test_redirects_cannot_downgrade_https_or_change_host(self) -> None:
        opener = self.client._hogwartsnet_opener(STORY_URL, FicbookAccount())
        handler = next(item for item in opener.handlers if isinstance(item, HTTPRedirectHandler))
        targets = (
            STORY_URL.replace("https:", "http:"),
            "https://evil.example/next",
            "http://127.0.0.1/private",
            "https://hogwartsnet.ru:8443/next",
            "https://test-reader@hogwartsnet.ru/next",
        )
        for code in (301, 302, 303, 307, 308):
            for target in targets:
                with self.subTest(code=code, target=target):
                    with self.assertRaises(FicbookError):
                        handler.redirect_request(Request(STORY_URL), None, code, "Redirect", {}, target)

    def test_safe_https_redirect_remains_supported(self) -> None:
        opener = self.client._hogwartsnet_opener(STORY_URL, FicbookAccount())
        handler = next(item for item in opener.handlers if isinstance(item, HTTPRedirectHandler))
        target = STORY_URL + "&chapter=2"
        redirected = handler.redirect_request(Request(STORY_URL), None, 302, "Redirect", {}, target)
        self.assertEqual(redirected.full_url, target)

    def test_https_context_verifies_certificate_and_hostname(self) -> None:
        opener = self.client._hogwartsnet_opener(STORY_URL, FicbookAccount())
        handler = next(item for item in opener.handlers if isinstance(item, HTTPSHandler))
        self.assertIsNotNone(handler._context)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)

    def test_certificate_failure_never_retries_over_http(self) -> None:
        opener = fake_opener()
        opener.open.side_effect = URLError(ssl.SSLCertVerificationError("certificate verification failed"))
        with self.assertRaises(FicbookError):
            self.client._hogwartsnet_open(opener, STORY_URL, data=b"dummy-credentials")
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.args[0].full_url, STORY_URL)


if __name__ == "__main__":
    unittest.main()
