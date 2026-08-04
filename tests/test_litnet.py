from __future__ import annotations

import unittest
from urllib.parse import parse_qs

from bs4 import BeautifulSoup

from src.core.models import Chapter
from src.sources.litnet import LitnetClient, LitnetError, LitnetLoginError
from src.sources.registry import extract_url, normalize_url


class FakeLoginLitnetClient(LitnetClient):
    def __init__(self) -> None:
        super().__init__("mail@example.com", "secret")
        self.requests: list[tuple[str, bytes | None]] = []

    def _soup(self, url: str, data: bytes | None = None) -> BeautifulSoup:
        self.requests.append((url, data))
        if data is None:
            return BeautifulSoup(
                """
                <form action="/auth/login?classic=1" method="post">
                  <input name="_csrf" value="token">
                  <input name="LoginForm[login]">
                  <input name="LoginForm[password]">
                  <input name="LoginForm[type]" value="v3">
                </form>
                """,
                "lxml",
            )
        return BeautifulSoup("<html><a href='/ru/site/library'>Моя библиотека</a></html>", "lxml")


class LitnetTests(unittest.TestCase):
    def test_litnet_urls_are_supported_and_normalized(self) -> None:
        book = "https://www.litnet.com/ru/book/test-b123"
        reader = "https://litnet.com/ru/reader/test-b123?c=456"

        self.assertEqual(extract_url(f"скачай {book}"), book)
        self.assertEqual(normalize_url(book), "https://litnet.com/ru/reader/test-b123")
        self.assertEqual(normalize_url(reader), "https://litnet.com/ru/reader/test-b123")

    def test_login_posts_csrf_and_credentials(self) -> None:
        client = FakeLoginLitnetClient()

        client._login_if_configured()

        self.assertTrue(client._authenticated)
        payload = parse_qs((client.requests[-1][1] or b"").decode())
        self.assertEqual(payload["_csrf"], ["token"])
        self.assertEqual(payload["LoginForm[login]"], ["mail@example.com"])
        self.assertEqual(payload["LoginForm[password]"], ["secret"])

    def test_metadata_and_chapters_are_parsed(self) -> None:
        reader = BeautifulSoup(
            """
            <meta property="og:title" content="Книга">
            <meta property="og:description" content="Описание">
            <h1>Книга</h1>
            <select>
              <option value="11">Пролог</option>
              <option value="12">Глава 1</option>
            </select>
            <a class="sa-name" href="/ru/author-u1">Автор</a>
            <a href="/ru/top/fentezi">Фэнтези</a>
            <p>16+</p><p>Отредактировано: 13.07.2023</p>
            """,
            "lxml",
        )
        book = BeautifulSoup("<main>Закончена 157 стр.</main>", "lxml")
        client = LitnetClient()

        chapters = client._chapters(reader)
        story = client._story(
            "https://litnet.com/ru/reader/test-b1",
            reader,
            book,
            [Chapter("Пролог", "<p>Текст</p>")],
        )

        self.assertEqual([chapter.title for chapter in chapters], ["Пролог", "Глава 1"])
        self.assertEqual(story.title, "Книга")
        self.assertEqual(story.author, "Автор")
        self.assertEqual(story.status, "завершён")
        self.assertEqual(story.page_count, "157")
        self.assertEqual(story.rating, "16+")

    def test_locked_chapter_returns_clear_error(self) -> None:
        client = LitnetClient()
        soup = BeautifulSoup("<main>Эта глава доступна только после покупки</main>", "lxml")

        with self.assertRaisesRegex(LitnetError, "нет доступа"):
            client._chapter_html(soup)

    def test_public_preview_does_not_require_configured_login(self) -> None:
        client = LitnetClient("configured", "configured")
        reader = BeautifulSoup(
            "<meta property='og:title' content='Public'><select><option value='1'>Глава 1</option></select>",
            "lxml",
        )
        client._soup = lambda *_: reader  # type: ignore[method-assign]
        client._login_if_configured = lambda: self.fail("Public preview attempted login")  # type: ignore[method-assign]

        preview = client.preview("https://litnet.com/ru/reader/public-b1")

        self.assertEqual(preview.title, "Public")
        self.assertEqual(len(preview.chapters), 1)

    def test_recaptcha_markup_is_detected(self) -> None:
        client = LitnetClient()
        soup = BeautifulSoup(
            "<div class='g-recaptcha'></div><div>Подтвердите, что вы не робот</div>",
            "lxml",
        )

        self.assertTrue(client._requires_captcha(soup))

    def test_protected_chapter_error_explains_captcha_without_admin_details(self) -> None:
        client = LitnetClient()
        detailed = "Litnet запросил CAPTCHA. Бот не обходит проверку."

        error = client._protected_chapter_login_error(14, LitnetLoginError(detailed))

        self.assertEqual(str(error), detailed)
        self.assertIn("Глава 14", error.user_message)
        self.assertIn("Я не робот", error.user_message)


if __name__ == "__main__":
    unittest.main()
