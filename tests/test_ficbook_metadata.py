from __future__ import annotations

import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from bs4 import BeautifulSoup

from src.core.models import Story
from src.core.limits import ResourceLimitError
from src.sources.ficbook import (
    FicbookAccount,
    FicbookChapterDownloadError,
    FicbookClient,
    FicbookError,
    FicbookNotFoundError,
    FicbookSiteStatus,
    FicbookSiteUnavailableError,
    classify_ficbook_homepage,
    extract_url,
    normalize_url,
)


class FakeStory:
    def __init__(self, metadata: dict[str, str] | None = None) -> None:
        self.metadata = metadata or {}
        self.lists: dict[str, list[str]] = {}

    def getMetadata(self, key: str) -> str:
        return self.metadata.get(key, "")

    def setMetadata(self, key: str, value: str) -> None:
        self.metadata[key] = value

    def getList(self, key: str) -> list[str]:
        return self.lists.get(key, [])

    def addToList(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def clear_processed_metadata_cache(self) -> None:
        return None


class FakeFicbookAdapter:
    def __init__(self, html: str) -> None:
        self.story = FakeStory({"storyId": "018f3425-37e3-7752-9c98-c6b1b22ca383"})
        self.url = "https://ficbook.net/readfic/018f3425-37e3-7752-9c98-c6b1b22ca383"
        self.metadataDone = False
        self._html = html
        self._chapters: list[dict[str, str]] = []

    def getSiteDomain(self) -> str:
        return "ficbook.net"

    def getStoryMetadataOnly(self, *, get_cover: bool = True) -> object:
        raise AttributeError("'NoneType' object has no attribute 'text'")

    def get_request(self, url: str) -> str:
        return self._html

    def make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def get_chapters(self) -> list[dict[str, str]]:
        return [dict(chapter) for chapter in self._chapters]

    def add_chapter(self, title: str, url: str) -> None:
        self._chapters.append({"title": title, "url": url})

    def setCoverImage(self, url: str, cover_url: str) -> None:
        return None


class FicbookMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FicbookClient()

    def test_status_is_not_defaulted_to_in_progress(self) -> None:
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        self.assertEqual(self.client._status_from_soup(soup), "")

    def test_ficbook_maintenance_page_is_classified_as_unavailable(self) -> None:
        html = "<html><body><h1>Ведутся технические работы</h1><p>Скоро вернёмся</p></body></html>"

        self.assertIs(
            classify_ficbook_homepage(200, html),
            FicbookSiteStatus.UNAVAILABLE,
        )

    def test_normal_ficbook_page_is_classified_as_available(self) -> None:
        html = "<html><head><title>Книга Фанфиков</title></head><body>Фикбук</body></html>"

        self.assertIs(
            classify_ficbook_homepage(200, html),
            FicbookSiteStatus.AVAILABLE,
        )

    def test_gateway_error_is_classified_as_unavailable(self) -> None:
        self.assertIs(
            classify_ficbook_homepage(503, "Service unavailable"),
            FicbookSiteStatus.UNAVAILABLE,
        )

    def test_parser_error_becomes_outage_only_after_homepage_check(self) -> None:
        self.client._build_config = lambda *_: object()  # type: ignore[method-assign]
        self.client.ficbook_site_status = lambda: FicbookSiteStatus.UNAVAILABLE  # type: ignore[method-assign]

        with patch("src.sources.ficbook.adapters.getAdapter", side_effect=ValueError("parser drift")):
            with self.assertRaises(FicbookSiteUnavailableError):
                self.client._download_once(
                    "https://ficbook.net/readfic/1",
                    FicbookAccount(),
                    None,
                    None,
                )

    def test_parser_error_stays_normal_when_ficbook_homepage_is_available(self) -> None:
        self.client._build_config = lambda *_: object()  # type: ignore[method-assign]
        self.client.ficbook_site_status = lambda: FicbookSiteStatus.AVAILABLE  # type: ignore[method-assign]

        with patch("src.sources.ficbook.adapters.getAdapter", side_effect=ValueError("parser drift")):
            with self.assertRaises(FicbookError) as raised:
                self.client._download_once(
                    "https://ficbook.net/readfic/1",
                    FicbookAccount(),
                    None,
                    None,
                )

        self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
        self.assertEqual(
            str(raised.exception),
            "Не удалось скачать фанфик с Ficbook. Возможно, сайт изменил страницу или временно отдает неполный ответ.",
        )

    def test_unknown_homepage_does_not_turn_download_or_preview_errors_into_outage(self):
        for operation in (self.client._download_once, self.client._preview_once):
            with self.subTest(operation=operation.__name__), \
                    patch.object(self.client, '_build_config', return_value=object()), \
                    patch.object(self.client, 'ficbook_site_status', return_value=FicbookSiteStatus.UNKNOWN), \
                    patch('src.sources.ficbook.adapters.getAdapter', side_effect=ValueError('parser drift')):
                args = ('https://ficbook.net/readfic/1', FicbookAccount())
                if operation.__name__ == '_download_once':
                    args += (None, None)
                with self.assertRaises(FicbookError) as raised:
                    operation(*args)
                self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
                self.assertIn('parser drift', raised.exception.technical)

    def test_network_failure_stays_network_error_when_homepage_status_is_unknown(self):
        with patch.object(self.client, '_build_config', return_value=object()), \
                patch.object(self.client, 'ficbook_site_status', return_value=FicbookSiteStatus.UNKNOWN), \
                patch('src.sources.ficbook.adapters.getAdapter', side_effect=URLError('connection timeout')):
            with self.assertRaises(FicbookError) as raised:
                self.client._download_once('https://ficbook.net/readfic/1', FicbookAccount(), None, None)
        self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
        self.assertIn('соединение', raised.exception.user_message)

    def test_plain_access_error_is_not_a_site_outage_or_cloudflare(self):
        from fanficfare.exceptions import HTTPErrorFFF

        for code in (401, 403):
            with self.subTest(code=code), patch.object(self.client, '_build_config', return_value=object()), \
                    patch.object(self.client, 'ficbook_site_status', return_value=FicbookSiteStatus.UNAVAILABLE) as status, \
                    patch('src.sources.ficbook.adapters.getAdapter', side_effect=HTTPErrorFFF(
                        'https://ficbook.net/readfic/1', code, 'Forbidden', b'Access denied',
                    )):
                with self.assertRaises(FicbookError) as raised:
                    self.client._download_once('https://ficbook.net/readfic/1', FicbookAccount(), None, None)
                self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
                self.assertIn('доступ', raised.exception.user_message)
                self.assertNotIn('Cloudflare', raised.exception.user_message)
                status.assert_not_called()

    def test_http_403_uses_standard_error_in_download_and_preview(self):
        from fanficfare.exceptions import HTTPErrorFFF

        for operation in (self.client._download_once, self.client._preview_once):
            with self.subTest(operation=operation.__name__), \
                    patch.object(self.client, '_build_config', return_value=object()), \
                    patch.object(self.client, 'ficbook_site_status') as status, \
                    patch('src.sources.ficbook.adapters.getAdapter', side_effect=HTTPErrorFFF(
                        'https://ficbook.net/readfic/1', 403, 'Source returned an HTTP error',
                    )):
                args = ('https://ficbook.net/readfic/1', FicbookAccount())
                if operation.__name__ == '_download_once':
                    args += (None, None)
                with self.assertRaises(FicbookError) as raised:
                    operation(*args)
                self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
                self.assertIn('доступ', raised.exception.user_message)
                self.assertNotIn('Cloudflare', raised.exception.user_message)
                status.assert_not_called()

    def test_homepage_challenge_is_not_an_outage_even_with_http_503_or_maintenance_text(self):
        for code in (200, 403, 503):
            with self.subTest(code=code):
                headers = Message()
                headers['CF-Mitigated'] = 'challenge'
                response = BytesIO(b'under maintenance')
                response.headers = headers
                response.status = code
                if code != 200:
                    response = HTTPError('https://ficbook.net/', code, 'Challenge', headers, response)
                with patch('src.sources.ficbook.urlopen', **(
                    {'return_value': response} if code == 200 else {'side_effect': response}
                )):
                    result = self.client.ficbook_site_status()
                self.assertEqual(result.value, 'challenged')
                self.assertTrue(response.closed)

    def test_homepage_resource_limits_are_not_silenced_as_unknown_status(self):
        with patch('src.sources.ficbook.urlopen', side_effect=ResourceLimitError('deadline')):
            with self.assertRaises(ResourceLimitError):
                self.client.ficbook_site_status()

    def test_homepage_http_errors_close_response_and_keep_unknown_separate_from_outage(self):
        for code, expected in ((403, FicbookSiteStatus.UNKNOWN), (502, FicbookSiteStatus.UNAVAILABLE)):
            with self.subTest(code=code):
                response = HTTPError('https://ficbook.net/', code, 'Error', {}, BytesIO(b'Error'))
                with patch('src.sources.ficbook.urlopen', side_effect=response):
                    self.assertIs(self.client.ficbook_site_status(), expected)
                self.assertTrue(response.closed)

    def test_challenged_homepage_does_not_replace_the_original_parser_error(self):
        with patch.object(self.client, '_build_config', return_value=object()), \
                patch.object(self.client, 'ficbook_site_status', return_value=FicbookSiteStatus.CHALLENGED), \
                patch('src.sources.ficbook.adapters.getAdapter', side_effect=ValueError('parser drift')):
            with self.assertRaises(FicbookError) as raised:
                self.client._download_once('https://ficbook.net/readfic/1', FicbookAccount(), None, None)
        self.assertNotIsInstance(raised.exception, FicbookSiteUnavailableError)
        self.assertNotIn('Cloudflare', raised.exception.user_message)
        self.assertIn('parser drift', raised.exception.technical)
        self.assertIn('challenged', raised.exception.technical)

    def test_ficbook_metadata_recovers_from_missing_text_element(self) -> None:
        adapter = FakeFicbookAdapter(
            """
            <section class="chapter-info"><h1>Чертовски хорошо<sup>+</sup></h1></section>
            <a href="/authors/42">Автор</a>
            <ul class="list-of-fanfic-parts">
              <li class="part"><a href="/readfic/018f3425-37e3-7752-9c98-c6b1b22ca383/1#part_content">Глава 1</a></li>
              <li class="part"><a href="/readfic/018f3425-37e3-7752-9c98-c6b1b22ca383/2#part_content">Глава 2</a></li>
            </ul>
            <strong>Размер:</strong><div>12 страниц, 3456 слов</div>
            """
        )

        story = self.client._get_story_metadata(adapter, adapter.url)

        self.assertTrue(adapter.metadataDone)
        self.assertEqual(story.getMetadata("title"), "Чертовски хорошо")
        self.assertEqual(story.getMetadata("author"), "Автор")
        self.assertEqual(story.getMetadata("authorUrl"), "https://ficbook.net/authors/42")
        self.assertEqual([chapter["title"] for chapter in adapter.get_chapters()], ["Глава 1", "Глава 2"])
        self.assertEqual(story.getMetadata("pages"), "12")
        self.assertEqual(story.getMetadata("numWords"), "3456")

    def test_primary_parser_with_real_adapter(self) -> None:
        from fanficfare import adapters
        url = "https://ficbook.net/readfic/123"
        adapter = adapters.getAdapter(self.client._build_config(url, FicbookAccount()), url)
        html = """
        <section class="chapter-info"><h1>Title</h1></section>
        <header><a href="/fanfiction/books/test">Fandom</a></header>
        <div class="tags"><a href="/tags/1">Tag</a></div>
        <meta property="og:image" content="https://ficbook.net/cover.jpg">
        <ul class="list-of-fanfic-parts"><li class="part">
          <a href="/readfic/123/1#part_content">Part</a>
          <a href="/readfic/123/1?x=1">Duplicate</a>
          <a href="/readfic/123/reviews">Reviews</a>
          <a href="https://example.org/readfic/123/2">External</a>
          <a href="/readfic/456/2">Other story</a>
          <span title="8 сентября 2026 г. в 12:00"></span>
        </li></ul>
        """
        with patch.object(adapter, "get_request", return_value=html), patch.object(
            adapter, "getStoryMetadataOnly", side_effect=AssertionError("Legacy parser called")
        ), patch.object(adapter, "setCoverImage") as cover:
            story = self.client._get_story_metadata(adapter, url, get_cover=False)
        self.assertEqual(story.getMetadata("title"), "Title")
        self.assertEqual(story.getMetadata("numChapters"), "1")
        self.assertEqual(story.getList("category"), ["Fandom"])
        self.assertEqual(story.getList("genre"), ["Tag"])
        self.assertEqual(story.getMetadataRaw("datePublished").year, 2026)
        self.assertEqual(len(adapter.get_chapters()), 1)
        cover.assert_not_called()

    def test_primary_parser_rejects_missing_chapters(self) -> None:
        adapter = FakeFicbookAdapter('<section class="chapter-info"><h1>Title</h1></section>')
        with self.assertRaises(FicbookError):
            self.client._get_story_metadata(adapter, adapter.url)
        self.assertFalse(adapter.metadataDone)

    def test_primary_parser_accepts_single_chapter_without_optional_metadata(self) -> None:
        adapter = FakeFicbookAdapter(
            '<section class="chapter-info"><h1>Title</h1></section>'
            '<ul class="list-of-fanfic-parts"></ul>'
            '<a href="/readfic/018f3425-37e3-7752-9c98-c6b1b22ca383#part_content">Начать читать</a>'
            '<div id="content" class="part_text">Text</div>'
        )
        self.client._get_story_metadata(adapter, adapter.url)
        self.assertEqual(adapter.get_chapters(), [{"title": "Title", "url": adapter.url}])
        self.assertEqual(self.client._load_chapter_html(adapter, adapter.url), "<p>Text</p>")

    def test_empty_ficbook_reports_missing_text(self) -> None:
        adapter = FakeFicbookAdapter(
            '<section class="chapter-info"><h1>Empty</h1></section>'
            '<ul class="list-of-fanfic-parts"></ul>'
            '<article id="part_content"><h2>Содержание</h2></article>'
            '<strong>Размер:</strong><div>0 страниц, 0 слов, 1 часть</div>'
        )

        with self.assertRaisesRegex(
            FicbookNotFoundError,
            "Ficbook сообщает 0 страниц и 0 слов",
        ):
            self.client._get_story_metadata(adapter, adapter.url)

    def test_primary_parser_preserves_access_and_not_found_errors(self) -> None:
        from fanficfare import adapters, exceptions
        url = "https://ficbook.net/readfic/123"
        adapter = adapters.getAdapter(self.client._build_config(url, FicbookAccount()), url)
        with patch.object(adapter, "get_request", side_effect=exceptions.HTTPErrorFFF(url, 404, "Missing")):
            with self.assertRaises(exceptions.StoryDoesNotExist):
                self.client._get_story_metadata(adapter, url)
        with patch.object(adapter, "get_request", return_value='<div id="adultCoverWarning"></div>'), patch.object(
            adapter, "getConfig", return_value=False
        ):
            adapter.is_adult = False
            with self.assertRaises(exceptions.AdultCheckRequired):
                self.client._get_story_metadata(adapter, url)
        self.assertFalse(adapter.metadataDone)

    def test_broken_contents_is_not_treated_as_single_chapter(self) -> None:
        adapter = FakeFicbookAdapter(
            '<section class="chapter-info"><h1>Title</h1></section>'
            '<ul class="list-of-fanfic-parts"><li class="part">Missing link</li></ul>'
            '<div id="content">Preview text</div>'
        )
        with self.assertRaises(FicbookError):
            self.client._get_story_metadata(adapter, adapter.url)

    def test_other_sites_use_fanficfare_parser(self) -> None:
        adapter = Mock()
        adapter.getSiteDomain.return_value = "archiveofourown.org"
        story = self.client._get_story_metadata(adapter, "https://archiveofourown.org/works/1", get_cover=False)
        self.assertIs(story, adapter.getStoryMetadataOnly.return_value)
        adapter.getStoryMetadataOnly.assert_called_once_with(get_cover=False)

    def test_status_and_dates_are_read_from_ficbook_markup(self) -> None:
        soup = BeautifulSoup(
            """
            <div class="badge-status-finished"><span>Завершён</span></div>
            <div>Дата публикации: 01.02.2024</div>
            <div>Дата завершения: 03.04.2024</div>
            """,
            "lxml",
        )
        story = FakeStory({"status": self.client._status_from_soup(soup)})

        self.client._fill_date_metadata(story, soup)

        self.assertEqual(self.client._status_text(story.getMetadata("status")), "завершён")
        self.assertEqual(story.getMetadata("dateStart"), "01.02.2024")
        self.assertEqual(story.getMetadata("dateFinish"), "03.04.2024")

    def test_annotation_does_not_duplicate_size_block(self) -> None:
        story = FakeStory(
            {
                "title": "Title",
                "author": "Author",
                "classification": "джен",
                "numChapters": "3",
                "status": "Completed",
                "dateStart": "01.02.2024",
                "dateFinish": "03.04.2024",
                "pages": "12",
                "numWords": "3456",
            }
        )
        soup = BeautifulSoup("<html><body></body></html>", "lxml")

        annotation = self.client._build_annotation_html(story, soup, "https://ficbook.net/readfic/1")

        self.assertNotIn("Размер", annotation)
        self.assertIn("Статус", annotation)
        self.assertIn("завершён", annotation)
        self.assertIn("Дата начала", annotation)
        self.assertIn("Дата завершения", annotation)

    def test_pairing_and_characters_are_recovered_from_ficbook_markup(self) -> None:
        story = FakeStory()
        soup = BeautifulSoup(
            """
            <strong>Пэйринг и персонажи:</strong>
            <div>
              <a class="pairing-highlight" href="/pairings/1">Гарри Поттер/Драко Малфой</a>
              <a href="/pairings/2">Гермиона Грейнджер</a>
            </div>
            """,
            "lxml",
        )

        self.client._fill_pairing_metadata(story, soup)

        self.assertEqual(
            story.getList("ships"),
            ["Гарри Поттер/Драко Малфой", "Гермиона Грейнджер"],
        )

    def test_ficbook_characters_extend_existing_pairings_without_duplicates(self) -> None:
        story = FakeStory()
        story.lists["ships"] = ["Гарри Поттер/Драко Малфой"]
        soup = BeautifulSoup(
            """
            <strong>Пейринг и персонажи:</strong>
            <div>
              <a class="pairing-highlight" href="/pairings/1">Гарри Поттер/Драко Малфой</a>
              <a href="/pairings/2">Гермиона Грейнджер</a>
            </div>
            """,
            "lxml",
        )

        self.client._fill_pairing_metadata(story, soup)

        self.assertEqual(
            story.getList("ships"),
            ["Гарри Поттер/Драко Малфой", "Гермиона Грейнджер"],
        )

    def test_interactive_footnotes_are_rendered_after_chapter(self) -> None:
        soup = BeautifulSoup(
            """
            <div id="content"><p>Текст<span class="footnote" id="note-1"></span>.</p></div>
            <script>window.textFootnotes = {"note-1":"<p>Текст примечания <b>автора</b>.</p>"};</script>
            """,
            "lxml",
        )

        html = self.client._chapter_content_html(soup, soup.select_one("#content"))

        self.assertIn("Текст<sup", html)
        self.assertIn("[1]", html)
        self.assertIn("Интерактивные примечания", html)
        self.assertIn("Текст примечания <b>автора</b>", html)

    def test_interactive_footnote_falls_back_to_title_attribute(self) -> None:
        soup = BeautifulSoup(
            '<div id="content">Текст<a class="footnote" id="note-2" title="Подсказка"></a></div>',
            "lxml",
        )

        html = self.client._chapter_content_html(soup, soup.select_one("#content"))

        self.assertIn("[1]", html)
        self.assertIn("Подсказка", html)

    def test_chapter_notes_keep_their_position_around_text(self) -> None:
        source = """
            <div class="part-comment-top"><div class="text-preline">Перед главой</div></div>
            <div id="content"><p>Текст главы</p></div>
            <div class="part-comment-bottom"><div class="text-preline">После главы</div></div>
        """
        adapter = Mock()
        adapter.get_request.return_value = source
        adapter.make_soup.side_effect = lambda html: BeautifulSoup(html, "lxml")

        html = self.client._load_chapter_html(adapter, "https://ficbook.net/readfic/1/1")

        self.assertLess(html.index("Перед главой"), html.index("Текст главы"))
        self.assertLess(html.index("Текст главы"), html.index("После главы"))

    def test_supported_site_urls_are_extracted(self) -> None:
        urls = [
            "https://ficbook.net/readfic/123",
            "https://archiveofourown.org/works/123",
            "https://www.wattpad.com/story/123-title",
            "http://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123",
            "https://litnet.com/ru/book/title-b123",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(extract_url(f"download {url}"), url)

    def test_fanfictionnet_url_is_not_supported(self) -> None:
        self.assertIsNone(extract_url("https://www.fanfiction.net/s/123/1/title"))

    def test_hogwartsnet_url_is_normalized_to_story_root(self) -> None:
        self.assertEqual(
            normalize_url("https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123&chapter=4"),
            "https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123",
        )

    def test_ficbook_accounts_rotate_for_parallel_downloads(self) -> None:
        client = FicbookClient(accounts=(FicbookAccount("one", "p"), FicbookAccount("two", "p")))

        first = client._accounts_for_url("https://ficbook.net/readfic/1", rotate=True)
        second = client._accounts_for_url("https://ficbook.net/readfic/1", rotate=True)

        self.assertEqual(first[0].login, "one")
        self.assertEqual(second[0].login, "two")

    def test_ao3_disables_full_work_request(self) -> None:
        config = self.client._build_config(
            "https://archiveofourown.org/works/10057010",
            FicbookAccount(),
        )

        self.assertEqual(config.get("overrides", "use_view_full_work"), "false")

    def test_ao3_transient_525_is_retried(self) -> None:
        client = FicbookClient(retry_attempts=2, retry_base_delay=0)
        attempts = 0

        def fake_download(normalized, account, progress, chapter_numbers):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FicbookError("AO3 temporary response", technical="HTTP 525")
            return Story(title="Title", author="Author", source_url=normalized)

        client._download_once = fake_download  # type: ignore[method-assign]

        story = client.download("https://archiveofourown.org/works/10057010")

        self.assertEqual(story.title, "Title")
        self.assertEqual(attempts, 2)

    def test_ao3_525_retries_only_current_chapter(self) -> None:
        client = FicbookClient(retry_attempts=2, retry_base_delay=0)
        adapter = Mock()
        adapter.getChapterTextNum.side_effect = [RuntimeError("HTTP 525"), "<p>Chapter</p>"]
        updates: list[str] = []

        html = client._fanficfare_chapter_html(
            adapter, "https://archiveofourown.org/works/1", "chapter-url", 4, 4, 10, updates.append,
        )

        self.assertEqual(html, "<p>Chapter</p>")
        self.assertEqual(adapter.getChapterTextNum.call_count, 2)
        self.assertIn("главу 4/10", updates[0])

    def test_exhausted_ao3_chapter_retry_is_not_restarted_from_beginning(self) -> None:
        client = FicbookClient(retry_attempts=3, retry_base_delay=0)
        client._download_once = Mock(
            side_effect=FicbookChapterDownloadError("chapter failed", technical="HTTP 525")
        )

        with self.assertRaises(FicbookChapterDownloadError):
            client.download("https://archiveofourown.org/works/1")

        self.assertEqual(client._download_once.call_count, 1)

    def test_download_uses_explicit_queue_account_only(self) -> None:
        first = FicbookAccount("one", "p")
        second = FicbookAccount("two", "p")
        client = FicbookClient(accounts=(first, second))
        used: list[FicbookAccount] = []

        def fake_download(normalized, account, progress, chapter_numbers):
            used.append(account)
            return Story(title="Title", author="Author", source_url=normalized)

        client._download_once = fake_download  # type: ignore[method-assign]

        client.download("https://ficbook.net/readfic/1", account=second)

        self.assertEqual(used, [second])

    def test_hogwartsnet_metadata_and_chapter_are_parsed(self) -> None:
        soup = BeautifulSoup(
            """
            <table class="fd_hp_main">
              <tr><td class="fichead">
                <a href="ffshowfic.php?fid=1"><b>Title</b></a>
                автора <a href="member.php?id=1">Author</a> <i>в работе</i>
                <div>Annotation</div>
                <a href="findex.php?fandoms=1">Гарри Поттер</a><br>
                Драма || гет || R || Размер: мини || Глав: 2<br>
                Начало: 01.02.24 || Обновление: 03.02.24
              </td><td class="fichead_chapters">
                <select name="chapter">
                  <option value="1">глава 1</option>
                  <option value="2">глава 2</option>
                </select>
              </td></tr>
              <tr><td colspan="2"><div id="chap_text"><center><b>Глава 1</b></center><br>Body<br><br>Next</div></td></tr>
            </table>
            """,
            "lxml",
        )

        metadata = self.client._hogwartsnet_metadata(soup, "http://hogwartsnet.ru/mfanf/ffshowfic.php?fid=1")
        chapters = self.client._hogwartsnet_chapter_previews(soup, metadata["title"])
        title, html = self.client._hogwartsnet_chapter(soup, chapters[0].title)

        self.assertEqual(metadata["title"], "Title")
        self.assertEqual(metadata["author"], "Author")
        self.assertEqual(metadata["status"], "в процессе")
        self.assertEqual(metadata["start_date"], "01.02.24")
        self.assertEqual(len(chapters), 2)
        self.assertEqual(title, "Глава 1")
        self.assertIn("<p>Body</p>", html)


if __name__ == "__main__":
    unittest.main()
