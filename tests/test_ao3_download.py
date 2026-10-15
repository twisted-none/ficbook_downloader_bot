from __future__ import annotations

import unittest
from io import BytesIO
from xml.etree import ElementTree as ET
from unittest.mock import Mock
from zipfile import ZipFile

from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt
from src.sources.ficbook import FicbookAccount, FicbookClient
from tests.test_ficbook_metadata import FakeStory


WORK_URL = 'https://archiveofourown.org/works/1'
DOWNLOAD_URL = 'https://archiveofourown.org/downloads/1/Book.html?updated_at=123'
MENU = '<ul><li class="download"><ul><li><a href="/downloads/1/Book.html?updated_at=123">HTML</a></li></ul></li></ul>'


def export_html(count: int, *, single: bool = False) -> str:
    parts = ['<html><body><div id="preface"><div class="meta"><h1>Book</h1>'
             '<blockquote class="userstuff">Summary</blockquote>'
             '<blockquote class="userstuff">Work opening note</blockquote></div></div><div id="chapters" class="userstuff">']
    for index in range(1, count + 1):
        if not single:
            parts.append(f'<div class="meta group"><h2 class="heading">Chapter {index}</h2>'
                         f'<blockquote class="userstuff">Before {index}</blockquote></div>')
        else:
            parts.append('<h2 class="toc-heading">Book</h2>')
        parts.append(f'<div class="userstuff"><p>Body {index}<a href="#fn{index}">[1]</a></p></div>')
        if not single:
            parts.append(f'<div class="meta" id="endnotes{index}"><p id="fn{index}">After {index}</p></div>')
    parts.append('</div><div id="afterword"><div class="meta"><div id="endnotes">Work end note</div></div></div></body></html>')
    return ''.join(parts)


class AO3DownloadTests(unittest.TestCase):
    def setup_client(self, count: int = 2, *, menu: str = MENU, html: str | Exception | None = None):
        client = FicbookClient()
        links = [{'title': f'Title {i}', 'url': f'{WORK_URL}/chapters/{i}'} for i in range(1, count + 1)]
        adapter = Mock()
        adapter.url = WORK_URL
        adapter.addurl = '?view_adult=true'
        adapter.get_chapters.return_value = links
        adapter.get_request.side_effect = [menu, html if html is not None else export_html(count)]
        adapter.getChapterTextNum.return_value = '<p>Fallback body</p>'
        client._adapter_for = Mock(return_value=adapter)
        client._get_story_metadata = Mock(return_value=FakeStory({'title': 'Book', 'author': 'Author', 'description': 'Summary'}))
        client._extract_cover_image = Mock(return_value=None)
        return client, adapter

    def test_downloads_one_html_file_instead_of_fourteen_chapters(self) -> None:
        client, adapter = self.setup_client(14)
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
        self.assertEqual(len(story.chapters), 14)
        adapter.getChapterTextNum.assert_not_called()
        self.assertEqual(adapter.get_request.call_count, 2)
        self.assertEqual(adapter.get_request.call_args.args[0], DOWNLOAD_URL)
        self.assertIn('Body 14', story.chapters[-1].html)

    def test_selection_keeps_notes_links_and_original_chapter_url(self) -> None:
        client, adapter = self.setup_client(3)
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, {2})
        self.assertEqual(len(story.chapters), 1)
        chapter = story.chapters[0]
        self.assertEqual(chapter.title, 'Title 2')
        self.assertEqual(chapter.source_url, WORK_URL + '/chapters/2')
        for text in ('Work opening note', 'Before 2', 'Body 2', 'After 2', 'Work end note', 'href="#fn2"', 'id="fn2"'):
            self.assertIn(text, chapter.html)
        self.assertNotIn('Body 1', chapter.html)
        self.assertNotIn('Summary', chapter.html)
        adapter.getChapterTextNum.assert_not_called()

    def test_single_chapter_export_without_chapter_metadata(self) -> None:
        client, adapter = self.setup_client(1, html=export_html(1, single=True))
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
        self.assertIn('Body 1', story.chapters[0].html)
        adapter.getChapterTextNum.assert_not_called()

    def test_missing_menu_uses_existing_chapter_loader(self) -> None:
        client, adapter = self.setup_client(menu='<html>No download menu</html>')
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, {2})
        self.assertIn('Fallback body', story.chapters[0].html)
        adapter.getChapterTextNum.assert_called_once_with(WORK_URL + '/chapters/2', 1)

    def test_failed_download_uses_existing_chapter_loader(self) -> None:
        client, adapter = self.setup_client(html=RuntimeError('HTTP 525'))
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
        self.assertEqual(len(story.chapters), 2)
        self.assertEqual(adapter.getChapterTextNum.call_count, 2)

    def test_incomplete_or_non_export_html_is_not_returned_as_a_book(self) -> None:
        for html in (export_html(1), '<html>Shields are up!</html>'):
            with self.subTest(html=html[:30]):
                client, adapter = self.setup_client(2, html=html)
                story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
                self.assertTrue(all('Fallback body' in chapter.html for chapter in story.chapters))
                self.assertEqual(adapter.getChapterTextNum.call_count, 2)

    def test_foreign_and_wrong_work_download_links_are_rejected(self) -> None:
        for href in ('https://evil.example/downloads/1/Book.html', '/downloads/2/Book.html'):
            with self.subTest(href=href):
                menu = f'<li class="download"><a href="{href}">HTML</a></li>'
                client, adapter = self.setup_client(menu=menu)
                client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
                self.assertEqual(adapter.get_request.call_count, 1)
                self.assertEqual(adapter.getChapterTextNum.call_count, 2)

    def test_wattpad_keeps_existing_loader(self) -> None:
        client, adapter = self.setup_client()
        client._download_fanficfare_once('https://www.wattpad.com/story/1', FicbookAccount(), None, None)
        adapter.get_request.assert_not_called()
        self.assertEqual(adapter.getChapterTextNum.call_count, 2)

    def test_downloaded_html_builds_all_formats_with_resolved_footnotes(self) -> None:
        client, _ = self.setup_client()
        story = client._download_fanficfare_once(WORK_URL, FicbookAccount(), None, None)
        with ZipFile(BytesIO(build_epub(story))) as archive:
            pages = {name.removeprefix('OEBPS/'): ET.fromstring(archive.read(name))
                     for name in archive.namelist() if name.endswith('.xhtml')}
            for name, page in pages.items():
                for link in page.findall('.//{*}a'):
                    href = link.get('href', '')
                    if not href or href.startswith(('http:', 'https:')):
                        continue
                    filename, _, anchor = href.partition('#')
                    target = pages[filename or name]
                    if anchor:
                        self.assertTrue(any(node.get('id') == anchor for node in target.iter()), href)
        with ZipFile(BytesIO(build_docx(story))) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        names = {node.get(ns + 'name') for node in root.iter(ns + 'bookmarkStart')}
        for link in root.iter(ns + 'hyperlink'):
            anchor = link.get(ns + 'anchor')
            if anchor:
                self.assertIn(anchor, names)
        root = ET.fromstring(build_fb2(story))
        names = {node.get('id') for node in root.iter()}
        for link in root.findall('.//{*}a'):
            href = link.get('{http://www.w3.org/1999/xlink}href', '')
            if href.startswith('#'):
                self.assertIn(href[1:], names)
        self.assertGreaterEqual(build_pdf(story).count(b'/Subtype /Link'), 6)
        text = build_txt(story).decode('utf-8')
        for note in ('Before 1', 'After 1', 'Work opening note', 'Work end note'):
            self.assertIn(note, text)


if __name__ == '__main__':
    unittest.main()
