from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from bs4 import BeautifulSoup
from docx import Document
from reportlab.platypus import Paragraph

from src.core.models import Chapter, Story
from src.sources.ficbook import FicbookClient
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt
from tests.test_ficbook_metadata import FakeStory


class FicbookCreditsTests(unittest.TestCase):
    def test_download_passes_credits_and_complete_annotation_to_exporters(self):
        from fanficfare import adapters
        from src.sources.ficbook import FicbookAccount
        client = FicbookClient()
        url = "https://ficbook.net/readfic/123"
        adapter = adapters.getAdapter(client._build_config(url, FicbookAccount()), url)
        html = '''<section class="chapter-info"><h1>Title</h1></section>
        <div class="hat-creator-container"><a class="creator-username" href="/authors/1">Writer</a><span class="small-text text-muted">Автор</span></div>
        <div class="hat-creator-container"><a class="creator-username" href="/authors/2">Translator</a><span class="small-text text-muted">Переводчик</span></div>
        <ul class="list-of-fanfic-parts"><li class="part"><a href="/readfic/123/1">Chapter</a></li></ul>
        <strong>Размер:</strong><div>12 страниц, 3456 слов</div>'''
        with patch("src.sources.ficbook.adapters.getAdapter", return_value=adapter), patch.object(
            adapter, "get_request", return_value=html
        ), patch.object(client, "_pause_between_requests"), patch.object(client, "_load_chapter_html", return_value="<p>Body</p>"):
            story = client._download_once(url, FicbookAccount(), None, None)
        self.assertEqual(story.author, "Writer")
        self.assertEqual(story.translator, "Translator")
        self.assertTrue(story.annotation_complete)
        self.assertIn("Переводчик: Translator", story.annotation_html)
        self.assertIn("Страниц: 12", story.annotation_html)

    def test_translator_survives_real_fanficfare_metadata(self):
        from fanficfare import adapters
        from src.sources.ficbook import FicbookAccount
        client = FicbookClient()
        url = "https://ficbook.net/readfic/123"
        adapter = adapters.getAdapter(client._build_config(url, FicbookAccount()), url)
        soup = BeautifulSoup('<div class="hat-creator-container"><a class="creator-username" href="/authors/2">Translator</a><span class="small-text text-muted">Переводчик</span></div>', 'lxml')
        client._fill_ficbook_author_metadata(adapter.story, soup)
        self.assertEqual(adapter.story.getMetadata("translator"), "Translator")

    def test_roles_and_multiple_authors(self):
        soup = BeautifulSoup('''<div class="hat-creator-container"><a class="creator-username" href="/authors/1">Beta</a><span class="small-text text-muted">Бета</span></div>
        <div class="hat-creator-container"><a class="creator-username" href="/authors/2">Translator</a><span class="small-text text-muted">Переводчик</span></div>
        <div class="hat-creator-container"><a class="creator-username" href="/authors/3">Writer</a><span class="small-text text-muted">Автор</span></div>
        <div class="hat-creator-container"><a class="creator-username" href="/authors/4">Coauthor</a><span class="small-text text-muted">Соавтор</span></div>''', 'lxml')
        metadata = FakeStory({'author': 'unknown'})
        FicbookClient()._fill_ficbook_author_metadata(metadata, soup)
        self.assertEqual(metadata.getMetadata('author'), 'Writer, Coauthor')
        self.assertEqual(metadata.getMetadata('translator'), 'Translator')

    def test_external_original_author(self):
        soup = BeautifulSoup('''<div class="hat-creator-container"><a class="creator-username" href="/authors/2">Translator</a><span class="small-text text-muted">Переводчик</span></div>
        <strong>Автор оригинала:</strong><div><a href="https://example.org/writer">Original</a></div>''', 'lxml')
        metadata = FakeStory()
        FicbookClient()._fill_ficbook_author_metadata(metadata, soup)
        self.assertEqual(metadata.getMetadata('author'), 'Original')
        self.assertEqual(metadata.getMetadata('translator'), 'Translator')

    def test_missing_original_author_is_not_replaced_by_translator(self):
        soup = BeautifulSoup('<div class="hat-creator-container"><a class="creator-username" href="/authors/2">Translator</a><span class="small-text text-muted">Переводчик</span></div>', 'lxml')
        metadata = FakeStory({'author': 'Translator'})
        FicbookClient()._fill_ficbook_author_metadata(metadata, soup)
        self.assertEqual(metadata.getMetadata('author'), '')
        self.assertEqual(metadata.getMetadata('translator'), 'Translator')

    def test_annotation_escapes_metadata_and_omits_empty_fields(self):
        metadata = FakeStory({'title': 'A < B & C', 'author': '<Writer>', 'pages': '12'})
        html = FicbookClient()._build_annotation_html(metadata, BeautifulSoup('', 'lxml'), 'https://ficbook.net/readfic/1')
        self.assertIn('A &lt; B &amp; C', html)
        self.assertIn('&lt;Writer&gt;', html)
        self.assertNotIn('Переводчик:', html)
        self.assertEqual(html.count('Страниц: 12'), 1)

    def test_all_formats_have_one_ordered_frontmatter(self):
        metadata = FakeStory({'title': 'UniqueTitle', 'author': 'UniqueAuthor', 'translator': 'UniqueTranslator',
                              'pages': '12', 'numWords': '3456', 'description': '<p>UniqueSummary</p>'})
        html = FicbookClient()._build_annotation_html(metadata, BeautifulSoup('', 'lxml'), 'https://ficbook.net/readfic/1')
        story = Story(title='UniqueTitle', author='UniqueAuthor', source_url='https://ficbook.net/readfic/1',
                      translator='UniqueTranslator', annotation_html=html, annotation_complete=True,
                      description='<p>UniqueSummary</p>', page_count='12', word_count='3456', chapters=[Chapter('Chapter', '<p>Body</p>')])
        payloads = {'txt': build_txt(story).decode(), 'docx': '\n'.join(p.text for p in Document(BytesIO(build_docx(story))).paragraphs)}
        payloads['fb2'] = BeautifulSoup(build_fb2(story), 'xml').find('body').get_text(' ')
        with ZipFile(BytesIO(build_epub(story))) as archive:
            payloads['epub'] = BeautifulSoup(archive.read('OEBPS/intro.xhtml'), 'xml').find('body').get_text(' ')
        flow = []
        with patch('src.exporters.formats.SimpleDocTemplate.build', side_effect=lambda items, **kwargs: flow.extend(items)):
            build_pdf(story)
        payloads['pdf'] = '\n'.join(item.getPlainText() for item in flow if isinstance(item, Paragraph))
        for fmt, text in payloads.items():
            with self.subTest(format=fmt):
                for value in ['UniqueTitle', 'UniqueAuthor', 'UniqueTranslator', 'UniqueSummary', 'Страниц: 12', 'Слов: 3456']:
                    self.assertEqual(text.count(value), 1, text)
                self.assertLess(text.index('UniqueTitle'), text.index('UniqueAuthor'))
                self.assertLess(text.index('UniqueAuthor'), text.index('UniqueTranslator'))
                self.assertLess(text.index('UniqueTranslator'), text.index('Страниц: 12'))
                self.assertLess(text.index('Страниц: 12'), text.index('UniqueSummary'))
