from __future__ import annotations

import base64
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from src.sources.ficbook import FicbookClient, FicbookError, FicbookLoginError
from src.sources.ranobelib import RanobelibAccessError, RanobelibClient, RanobelibLoginError
from src.sources.registry import extract_url, normalize_url, site_display_name, site_key


class FakeRanobelibClient(RanobelibClient):
    def __init__(self) -> None:
        self.requests: list[str] = []

    def _request_json(self, url: str):
        self.requests.append(url)
        if "fields[]=" in url:
            return {
                "id": 42,
                "rus_name": "Тестовое ранобэ",
                "name": "Test novel",
                "authors": [{"name": "Автор"}],
                "summary": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Описание"}]}],
                },
                "genres": [{"name": "Фэнтези"}],
                "tags": [{"name": "Приключения"}],
                "status": {"label": "Онгоинг"},
                "ageRestriction": {"label": "16+"},
            }
        if url.endswith("/chapters"):
            return [
                {"volume": "1", "number": "1", "name": "Начало", "branches": [{"branch_id": None}]},
                {
                    "volume": "1",
                    "number": "2",
                    "name": "Закрытая",
                    "branches": [{"branch_id": 8, "restricted_view": {"is_open": False}}],
                },
                {"volume": "2", "number": "3", "name": "Продолжение", "branches": [{"branch_id": 9}]},
            ]
        if "/chapter?" in url:
            return {
                "content": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Текст ", "marks": [{"type": "bold"}]},
                                {"type": "text", "text": "главы"},
                            ],
                        }
                    ],
                },
                "attachments": [],
            }
        raise AssertionError(f"Unexpected URL: {url}")

    def _cover(self, value):
        return None


class RanobelibTests(unittest.TestCase):
    URL = "https://ranobelib.me/ru/42--test-novel"

    def test_urls_are_supported_and_normalized_to_book_root(self) -> None:
        reader = "https://www.ranobelib.me/ru/42--test-novel/read/v2/c3?bid=9"
        book = "https://ranobelib.me/ru/book/42--test-novel"

        self.assertEqual(extract_url(f"скачай {reader}"), reader)
        self.assertEqual(normalize_url(reader), self.URL)
        self.assertEqual(normalize_url(book), self.URL)
        self.assertEqual(site_key(self.URL), "ranobelib.me")
        self.assertEqual(site_display_name(self.URL), "RanobeLIB")

    def test_preview_skips_restricted_branches(self) -> None:
        preview = FakeRanobelibClient().preview(self.URL)

        self.assertEqual(preview.title, "Тестовое ранобэ")
        self.assertEqual(preview.author, "Автор")
        self.assertEqual([chapter.index for chapter in preview.chapters], [1, 2])
        self.assertIn("Том 2, глава 3", preview.chapters[1].title)

    def test_download_selects_available_chapter_and_builds_metadata(self) -> None:
        client = FakeRanobelibClient()
        progress: list[str] = []

        story = client.download(self.URL, progress.append, frozenset({2}))

        self.assertEqual(story.title, "Тестовое ранобэ")
        self.assertEqual(story.author, "Автор")
        self.assertEqual(story.status, "в процессе")
        self.assertEqual(story.rating, "16+")
        self.assertEqual(story.genres, ["Фэнтези", "Приключения"])
        self.assertEqual(story.description, "Описание")
        self.assertEqual(len(story.chapters), 1)
        self.assertIn("<strong>Текст </strong>главы", story.chapters[0].html)
        self.assertTrue(any("volume=2" in url and "number=3" in url and "branch_id=9" in url for url in client.requests))
        self.assertEqual(progress, ["Скачиваю главы: 1/1"])

    def test_all_restricted_branches_return_clear_error(self) -> None:
        client = FakeRanobelibClient()
        client._request_json = lambda _: [
            {"volume": "1", "number": "1", "branches": [{"restricted_view": {"is_open": False}}]}
        ]

        with self.assertRaises(RanobelibAccessError) as raised:
            client._chapters("42--test-novel")

        self.assertIn("нет глав", raised.exception.user_message)

    def test_structured_image_uses_attachment_url(self) -> None:
        client = RanobelibClient(min_request_interval=0)
        content = {
            "type": "doc",
            "content": [{"type": "image", "attrs": {"src": "7", "alt": "Карта"}}],
        }

        html = client._content_html(content, [{"id": 7, "url": "https://img.example/map.jpg"}])

        self.assertIn('src="https://img.example/map.jpg"', html)
        self.assertIn('alt="Карта"', html)

    def test_access_error_is_mapped_without_http_details_for_user(self) -> None:
        client = FicbookClient(retry_attempts=1)
        client.ranobelib.download = Mock(
            side_effect=RanobelibAccessError(
                "HTTP 403 from chapter endpoint",
                user_message="Эта глава недоступна для скачивания через RanobeLIB.",
            )
        )

        with self.assertRaises(FicbookError) as raised:
            client.download(self.URL)

        self.assertEqual(
            raised.exception.user_message,
            "Эта глава недоступна для скачивания через RanobeLIB.",
        )
        self.assertNotIn("403", raised.exception.user_message)

    def test_login_flow_posts_credentials_and_stores_oauth_tokens(self) -> None:
        client = RanobelibClient("reader@example.com", "secret", min_request_interval=0)
        login_page = b"""
            <form action="https://auth.lib.social/auth/login" method="post">
              <input type="hidden" name="_token" value="csrf">
              <input name="login"><input name="password">
            </form>
        """
        submitted: dict[str, str] = {}

        def submit(url: str, payload: dict[str, str], referer: str):
            del url, referer
            submitted.update(payload)
            callback = "https://ranobelib.me/ru/front/auth/oauth/callback?code=code-1&state=oauth-state"
            return callback, callback, b""

        with (
            patch("src.sources.ranobelib.secrets.token_urlsafe", side_effect=["verifier", "oauth-state"]),
            patch.object(client, "_auth_open", return_value=("https://auth.lib.social/auth/login", login_page)),
            patch.object(client, "_altcha_payload", return_value="proof"),
            patch.object(client, "_auth_submit", side_effect=submit),
            patch.object(
                client,
                "_token_request",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ) as token_request,
        ):
            client._login_locked()

        self.assertEqual(submitted["_token"], "csrf")
        self.assertEqual(submitted["login"], "reader@example.com")
        self.assertEqual(submitted["password"], "secret")
        self.assertEqual(submitted["altcha"], "proof")
        self.assertEqual(client._access_token, "access")
        self.assertEqual(client._refresh_token, "refresh")
        token_payload = token_request.call_args.args[0]
        self.assertEqual(token_payload["code"], "code-1")
        self.assertEqual(token_payload["code_verifier"], "verifier")

    def test_altcha_payload_contains_matching_solution(self) -> None:
        client = RanobelibClient(min_request_interval=0)
        salt = "salt:123"
        number = 7
        target = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
        challenge = {
            "algorithm": "SHA-256",
            "challenge": target,
            "salt": salt,
            "signature": "signed",
            "maxnumber": 10,
        }

        with patch.object(client, "_auth_json", return_value=challenge):
            encoded = client._altcha_payload()

        payload = json.loads(base64.b64decode(encoded))
        self.assertEqual(payload["number"], number)
        self.assertEqual(payload["signature"], "signed")

    def test_api_request_uses_bearer_token_without_password(self) -> None:
        client = RanobelibClient("reader", "secret", min_request_interval=0)
        client._access_token = "access-token"

        request = client._api_request("https://api.cdnlibs.org/api/manga/test")

        self.assertEqual(request.get_header("Authorization"), "Bearer access-token")
        self.assertNotIn("secret", str(request.header_items()))

    def test_credentials_are_never_submitted_to_untrusted_host(self) -> None:
        client = RanobelibClient("reader", "secret", min_request_interval=0)

        with self.assertRaises(RanobelibLoginError):
            client._auth_submit("http://example.com/login", {}, "https://auth.lib.social/auth/login")

    def test_login_error_is_mapped_to_common_bot_error(self) -> None:
        client = FicbookClient("", "", ranobelib_login="reader", ranobelib_password="secret")
        client.ranobelib.download = Mock(side_effect=RanobelibLoginError("Login failed"))

        with self.assertRaises(FicbookLoginError) as raised:
            client.download(self.URL)

        self.assertEqual(raised.exception.user_message, "Авторизация через RanobeLIB не сработала.")

    def test_oauth_consent_selects_allow_instead_of_deny(self) -> None:
        client = RanobelibClient("reader", "secret", min_request_interval=0)
        body = """
            <form action="https://auth.lib.social/auth/oauth/authorize" method="post">
              <input type="hidden" name="_token" value="csrf">
              <button type="submit" name="decision" value="deny">Отказать</button>
              <button type="submit" name="decision" value="approve">Разрешить</button>
            </form>
        """.encode("utf-8")
        submitted: dict[str, str] = {}

        def submit(url: str, payload: dict[str, str], referer: str):
            del url, referer
            submitted.update(payload)
            callback = "https://ranobelib.me/ru/front/auth/oauth/callback?code=ok&state=state"
            return callback, callback, b""

        with patch.object(client, "_auth_submit", side_effect=submit):
            callback = client._submit_consent_if_present(body, "https://auth.lib.social/auth/oauth/authorize")

        self.assertIn("code=ok", callback)
        self.assertEqual(submitted["decision"], "approve")


if __name__ == "__main__":
    unittest.main()
