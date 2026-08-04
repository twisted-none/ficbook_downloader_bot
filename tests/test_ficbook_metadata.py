from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from src.core.models import Story
from src.sources.ficbook import FicbookAccount, FicbookClient, extract_url, normalize_url


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


class FicbookMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FicbookClient()

    def test_status_is_not_defaulted_to_in_progress(self) -> None:
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        self.assertEqual(self.client._status_from_soup(soup), "")

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

    def test_pairing_is_recovered_from_ficbook_markup(self) -> None:
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

        self.assertEqual(story.getList("ships"), ["Гарри Поттер/Драко Малфой"])

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
            "http://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123",
        )

    def test_ficbook_accounts_rotate_for_parallel_downloads(self) -> None:
        client = FicbookClient(accounts=(FicbookAccount("one", "p"), FicbookAccount("two", "p")))

        first = client._accounts_for_url("https://ficbook.net/readfic/1", rotate=True)
        second = client._accounts_for_url("https://ficbook.net/readfic/1", rotate=True)

        self.assertEqual(first[0].login, "one")
        self.assertEqual(second[0].login, "two")

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
