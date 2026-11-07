from base64 import b64decode
from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt
from src.sources.ranobelib import RanobelibChapter, RanobelibClient, RanobelibError


BOOK = 'https://ranobelib.me/ru/book/42--test-novel'
CHAPTER = 'https://ranobelib.me/ru/42--test-novel/v1/c0'
IMAGE_PATH = '/uploads/ranobe/42/chapters/17/attachment.png'
IMAGE_URL = 'https://ranobelib.me' + IMAGE_PATH


class RanobelibIllustrationTests(unittest.TestCase):
    def setUp(self):
        self.client = RanobelibClient(min_request_interval=0)
        self.client._info = Mock(return_value={'id': 42, 'rus_name': 'Book'})
        self.client._chapters = Mock(return_value=[RanobelibChapter(1, '1', '0', 'Illustrated chapter')])
        self.client._chapter = Mock(return_value={
            'content': {'type': 'doc', 'content': [
                {'type': 'image', 'attrs': {'images': [{'image': 'attachment'}]}},
            ]},
            'attachments': [{'name': 'attachment', 'url': IMAGE_PATH}],
        })

    def test_image_only_chapter_is_embedded_in_four_formats(self):
        data = BytesIO()
        with Image.new('RGB', (120, 100), 'blue') as image:
            image.save(data, 'PNG')
        payload = data.getvalue()
        response = BytesIO(payload)
        response.geturl = lambda: IMAGE_URL
        with patch.object(self.client._illustration_opener, 'open', return_value=response) as request:
            book = self.client.download(BOOK)
        self.assertEqual(request.call_args.args[0].get_header('Referer'), 'https://ranobelib.me/')
        [chapter] = book.chapters
        self.assertEqual(chapter.source_url, CHAPTER)
        [name] = chapter.illustrations
        with ZipFile(BytesIO(build_epub(book))) as archive:
            self.assertEqual(archive.read(f'OEBPS/images/{name}'), payload)
            opf = ET.fromstring(archive.read('OEBPS/content.opf'))
            self.assertIsNotNone(opf.find(f'.//{{*}}item[@href="images/{name}"]'))
            page = ET.fromstring(archive.read('OEBPS/chapter-0001.xhtml'))
            self.assertIsNotNone(page.find(f'.//{{*}}img[@src="images/{name}"]'))
        fb2 = ET.fromstring(build_fb2(book))
        self.assertEqual(b64decode(fb2.find(f'{{*}}binary[@id="{name}"]').text), payload)
        self.assertEqual(fb2.find('.//{*}image').get('{http://www.w3.org/1999/xlink}href'), '#' + name)
        with ZipFile(BytesIO(build_docx(book))) as archive:
            page = ET.fromstring(archive.read('word/document.xml'))
            self.assertIsNotNone(page.find('.//{*}drawing'))
        self.assertIn(b'/Subtype /Image', build_pdf(book))
        txt = build_txt(book).decode('utf-8')
        self.assertIn('Illustrated chapter', txt)
        self.assertNotIn(name, txt)

    def test_inaccessible_attachment_keeps_chapter_link(self):
        error = HTTPError(IMAGE_URL, 403, 'Forbidden', {}, BytesIO())
        with patch.object(self.client._illustration_opener, 'open', side_effect=error):
            [chapter] = self.client.download(BOOK).chapters
        self.assertIn(f'href="{CHAPTER}"', chapter.html)
        self.assertFalse(chapter.illustrations)

    def test_empty_chapter_without_attachments_still_fails(self):
        self.client._chapter.return_value['content'] = {'type': 'doc', 'content': []}
        with self.assertRaisesRegex(RanobelibError, 'пустую главу 1'):
            self.client.download(BOOK)

    def test_long_image_is_split_without_losing_pixels(self):
        data = BytesIO()
        with Image.new('RGB', (120, 402), 'blue') as image:
            image.save(data, 'PNG')
        response = BytesIO(data.getvalue())
        response.geturl = lambda: IMAGE_URL
        with patch.object(self.client._illustration_opener, 'open', return_value=response):
            [chapter] = self.client.download(BOOK).chapters
        dimensions = []
        for piece in chapter.illustrations.values():
            with Image.open(BytesIO(piece.data)) as image:
                dimensions.append(image.size)
        self.assertEqual(dimensions, [(120, 160), (120, 160), (120, 82)])
