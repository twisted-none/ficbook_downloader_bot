from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from bs4 import BeautifulSoup

from src.core.limits import ResourceLimitError, ResourceLimits, resource_scope
from src.exporters.navigation import prepare_chapters
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt
from src.core.models import CoverImage
from src.core.download_queue import DownloadQueuePool
from src.sources.registry import extract_url, is_supported_story_url, normalize_url, site_key
from src.sources.share_links import extract_download_url
from src.sources.fikbooks import FikbooksClient, FikbooksError, FikbooksNotFoundError, FikbooksRateLimitError
from src.sources.ficbook import FicbookClient, FicbookNotFoundError

ROOT = 'https://fikbooks.ru/readfic/336966'
BOOK = '''<h1 class="article-header__name">Book &amp; title</h1>
<div class="article-categories"><a>Fandom</a></div>
<div class="article-header"><div class="card-item__badge">Завершён</div>
<div class="card-item__badge">R</div></div>
<div class="author-item"><a class="author-item__name">Author</a><div class="author-item__info">автор</div></div>
<div class="author-item"><a class="author-item__name">Translator</a><div class="author-item__info">переводчик</div></div>
<div class="card-item__field"><div class="card-item__field-title">Описание</div>
<div class="card-item__field-text" itemprop="description">Description &amp; text</div></div>
<img class="article-top__image" src="/img/nofanfic.jpg">
<section id="parts"><a class="part-item__link" href="/readfic/336966/11#part_content">First</a>
<a class="part-item__link" href="/readfic/336966/12#part_content">Second</a>
<a class="part-item__link" href="/readfic/336966/11#part_content">First duplicate</a></section>'''
PART = '''<section id="part_content" class="article-nav">Navigation, not chapter</section>
<section class="article-parts"><h2 class="part-header__title">First</h2>
<div class="article-parts__description"><p>Chapter text <a href="#note">[1]</a></p>
<p id="note">Note</p><script>evil()</script></div></section>
<section id="comments">Not chapter text</section>'''


class FakeClient(FikbooksClient):
    def __init__(self):
        super().__init__(request_delay=0)
        self.requested = []

    def _soup(self, url):
        self.requested.append(url)
        return BeautifulSoup(BOOK if url == ROOT else PART, 'lxml')


class FikbooksTests(unittest.TestCase):
    def test_links_are_recognized_without_changing_site(self):
        link = ROOT + '/1501899#part_content'
        self.assertEqual(extract_url('Book ' + link), link)
        self.assertEqual(extract_download_url(link), link)
        self.assertEqual(normalize_url(link), ROOT)
        self.assertEqual(normalize_url('http://www.fikbooks.ru/readfic/336966/1501899'), ROOT)
        self.assertEqual(site_key(link), 'fikbooks.ru')

    def test_unsafe_and_malformed_links_are_rejected(self):
        for url in ('https://fikbooks.ru.evil/readfic/1', 'https://fikbooks.ru@evil/readfic/1',
                    'https://user@fikbooks.ru/readfic/1', 'https://fikbooks.ru:444/readfic/1',
                    'https://fikbooks.ru/readfic/0', 'https://fikbooks.ru/readfic/1/2/3',
                    'https://fikbooks.ru/readfic/../1', 'https://fikbooks.ru/readfic/1?url=http://127.0.0.1',
                    'https://fikbooks.ru/readfic/1\\2'):
            with self.subTest(url=url):
                self.assertFalse(is_supported_story_url(url))

    def test_chapter_link_downloads_whole_book_and_metadata(self):
        client = FakeClient()
        progress = []
        story = client.download(ROOT + '/1501899#part_content', progress.append)
        self.assertEqual(story.title, 'Book & title')
        self.assertEqual(story.author, 'Author')
        self.assertEqual(story.translator, 'Translator')
        self.assertEqual(story.description, 'Description & text')
        self.assertEqual(story.fandoms, ['Fandom'])
        self.assertEqual(story.status, 'Завершён')
        self.assertEqual(story.rating, 'R')
        self.assertIsNone(story.cover)
        self.assertEqual(len(story.chapters), 2)
        self.assertEqual(client.requested, [ROOT, ROOT + '/11', ROOT + '/12'])
        self.assertTrue(any('2/2' in text for text in progress))
        self.assertNotIn('Navigation', story.chapters[0].html)
        self.assertNotIn('evil()', story.chapters[0].html)
        self.assertNotIn('Not chapter', story.chapters[0].html)
        html = prepare_chapters(story)[0].html
        self.assertIn('c0001-', html)

    def test_preview_and_selected_chapters(self):
        client = FakeClient()
        preview = client.preview(ROOT + '/1501899#part_content')
        self.assertEqual([c.index for c in preview.chapters], [1, 2])
        self.assertEqual(preview.author, 'Author')
        self.assertEqual(client.requested, [ROOT])
        client.requested.clear()
        story = client.download(ROOT, chapter_numbers={2})
        self.assertEqual(client.requested, [ROOT, ROOT + '/12'])
        self.assertEqual(story.chapters[0].title, 'Second')

    def test_same_work_and_chapter_share_queue_separate_from_ficbook(self):
        queues = DownloadQueuePool()
        self.assertIs(queues.assign(ROOT).queue, queues.assign(ROOT + '/11#part_content').queue)
        self.assertIsNone(queues.assign(ROOT).account)
        self.assertIsNot(queues.assign(ROOT).queue, queues.assign('https://ficbook.net/readfic/336966').queue)

    def test_export_to_every_supported_format(self):
        story = FakeClient().download(ROOT)
        self.assertIn(b'Chapter text', build_txt(story))
        self.assertEqual(ET.fromstring(build_fb2(story)).find('.//{*}book-title').text, 'Book & title')
        with ZipFile(BytesIO(build_epub(story))) as archive:
            opf = ET.fromstring(archive.read('OEBPS/content.opf'))
            self.assertEqual(opf.find('.//{*}title').text, 'Book & title')
            self.assertIn('OEBPS/chapter-0002.xhtml', archive.namelist())
        with ZipFile(BytesIO(build_docx(story))) as archive:
            self.assertIn(b'Chapter text', archive.read('word/document.xml'))
        self.assertTrue(build_pdf(story).startswith(b'%PDF'))

    def test_real_cover_is_loaded_but_placeholder_never_requested(self):
        from PIL import Image
        data = BytesIO()
        Image.new('RGB', (40, 60), 'white').save(data, 'PNG')
        response = BytesIO(data.getvalue())
        client = FikbooksClient()
        with patch.object(client._cover_opener, 'open', return_value=response) as fetch:
            self.assertIsNone(client._cover(BeautifulSoup(BOOK, 'lxml'), ROOT))
            fetch.assert_not_called()
            soup = BeautifulSoup(BOOK.replace('/img/nofanfic.jpg', '/media/cover.png'), 'lxml')
            cover = client._cover(soup, ROOT)
            self.assertIsInstance(cover, CoverImage)
            self.assertEqual(cover.mime, 'image/png')

    def test_one_chapter_work_without_table_of_contents(self):
        client = FakeClient()
        single = BOOK[:BOOK.index('<section id="parts">')] + PART
        with patch.object(client, '_soup', return_value=BeautifulSoup(single, 'lxml')) as fetch:
            story = client.download(ROOT)
        fetch.assert_called_once_with(ROOT)
        self.assertEqual(len(story.chapters), 1)

    def test_scripts_event_handlers_and_unsafe_links_are_not_exported(self):
        client = FakeClient()
        html = PART.replace('Chapter text', '<a href="javascript:evil()" onclick="evil()">Link</a>')
        html = client._chapter_html(BeautifulSoup(html, 'lxml'))
        self.assertNotIn('javascript:', html)
        self.assertNotIn('onclick', html)
        self.assertNotIn('<script', html)

    def test_active_html_and_svg_are_not_exported(self):
        raw = PART.replace('Chapter text', '<svg><use href="https://evil/image"/></svg>'
                           '<link href="https://evil/style" rel="stylesheet"><p>Chapter text</p>')
        result = FakeClient()._chapter_html(BeautifulSoup(raw, 'lxml'))
        self.assertIn('Chapter text', result)
        self.assertNotIn('<svg', result)
        self.assertNotIn('<use', result)
        self.assertNotIn('<link', result)
        self.assertNotIn('https://evil', result)

    def test_malformed_content_link_does_not_abort_chapter_download(self):
        html = PART.replace('href="#note"', 'href="https://[invalid"')
        result = FakeClient()._chapter_html(BeautifulSoup(html, 'lxml'))
        self.assertIn('Chapter text', result)
        self.assertNotIn('https://[invalid', result)

    def test_author_role_is_not_guessed_from_their_name(self):
        soup = BeautifulSoup(BOOK.replace('>Author<', '>Переводчик Иван<'), 'lxml')
        _, author, translator = FakeClient()._credits(soup)
        self.assertEqual(author, 'Переводчик Иван')
        self.assertEqual(translator, 'Translator')

    def test_invalid_selection_is_not_silently_ignored(self):
        for selected in ({0}, {3}, set()):
            with self.subTest(selected=selected), self.assertRaises(FikbooksError):
                FakeClient().download(ROOT, chapter_numbers=selected)

    def test_foreign_or_other_book_chapters_are_rejected_before_fetch(self):
        for url in ('https://evil/readfic/336966/11', '/readfic/999/11', 'https://[invalid/readfic/336966/11'):
            client = FakeClient()
            soup = BeautifulSoup(BOOK.replace('/readfic/336966/11#part_content', url), 'lxml')
            with patch.object(client, '_soup', return_value=soup) as fetch:
                with self.assertRaises(FikbooksError):
                    client.download(ROOT)
            fetch.assert_called_once_with(ROOT)

    def test_missing_chapter_is_not_successful_partial_book(self):
        client = FakeClient()
        with patch.object(client, '_soup', side_effect=[BeautifulSoup(BOOK, 'lxml'), BeautifulSoup('<p>Login</p>', 'lxml')]):
            with self.assertRaises(FikbooksError):
                client.download(ROOT)

    def test_limits_apply_before_downloading_chapters(self):
        client = FakeClient()
        with resource_scope(ResourceLimits(max_chapters=1)), self.assertRaises(ResourceLimitError):
            client.download(ROOT)
        self.assertEqual(client.requested, [ROOT])

    def test_http_error_classification_and_response_closed(self):
        for code, expected in ((404, FikbooksNotFoundError), (429, FikbooksRateLimitError), (403, FikbooksError)):
            client = FikbooksClient()
            body = BytesIO(b'error')
            error = HTTPError(ROOT, code, 'error', {}, body)
            with patch.object(client._opener, 'open', side_effect=error), self.assertRaises(expected):
                client.preview(ROOT)
            self.assertTrue(body.closed)

    def test_main_client_routes_to_separate_source_without_ficbook_accounts(self):
        client = FicbookClient(login='private-user', password='private-password')
        client.fikbooks = FakeClient()
        with patch('src.sources.ficbook.adapters.getAdapter', side_effect=AssertionError('native adapter used')):
            self.assertEqual(client.download(ROOT).author, 'Author')
            self.assertEqual(len(client.preview(ROOT).chapters), 2)
        client.fikbooks.preview = Mock(side_effect=FikbooksNotFoundError('not found'))
        with self.assertRaises(FicbookNotFoundError):
            client.preview(ROOT)

