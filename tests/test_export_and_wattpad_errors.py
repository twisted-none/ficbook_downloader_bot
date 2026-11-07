from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fanficfare.exceptions import HTTPErrorFFF

from src.core.models import Chapter, Story
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_txt
from src.sources.ficbook import FicbookClient, FicbookError, FicbookNotFoundError


class ExportErrorTests(unittest.TestCase):
    def test_xml_exports_clean_illegal_metadata_and_chapter_text(self):
        book = Story('Na\x01me', 'Author\x00', 'https://ficbook.net/readfic/1',
                     description='Info\x0b here', genres=['Fa\uffffntasy'],
                     chapters=[Chapter('Chapter\x08', '<p>One\x03 <em>two</em>\ufffe three.</p>')])
        fb2 = ET.fromstring(build_fb2(book))
        self.assertIn('One two three.', ''.join(fb2.itertext()))
        for build, filenames in ((build_epub, ('OEBPS/content.opf', 'OEBPS/chapter-0001.xhtml')),
                                 (build_docx, ('docProps/core.xml', 'word/document.xml'))):
            with self.subTest(exporter=build.__name__), ZipFile(BytesIO(build(book))) as archive:
                meta, chapter = (ET.fromstring(archive.read(name)) for name in filenames)
                self.assertIn('Name', ''.join(meta.itertext()))
                self.assertIn('One two three.', ''.join(chapter.itertext()))
        self.assertEqual(book.title, 'Na\x01me')

    def test_opening_choice_reorders_frontmatter(self):
        book = Story('Book', 'Author', 'https://ficbook.net/readfic/1', description='Summary',
                     chapters=[Chapter('Chapter', '<p>Text</p>')])
        for opening in ('annotation', 'contents'):
            with self.subTest(opening=opening):
                book.opening_page = opening
                annotation_first = opening == 'annotation'
                with ZipFile(BytesIO(build_epub(book))) as archive:
                    opf = ET.fromstring(archive.read('OEBPS/content.opf'))
                self.assertEqual(opf.find('.//{*}itemref').get('idref'), 'p0' if annotation_first else 'nav')
                fb2 = ET.fromstring(build_fb2(book)).find('{*}body')
                self.assertEqual(fb2.find('.//{*}title/{*}p').text,
                                 'Аннотация' if annotation_first else 'Оглавление')
                with ZipFile(BytesIO(build_docx(book))) as archive:
                    docx = ''.join(ET.fromstring(archive.read('word/document.xml')).itertext())
                for text in (docx, build_txt(book).decode('utf-8')):
                    self.assertEqual(text.index('Аннотация') < text.index('Оглавление'), annotation_first)


class WattpadErrorTests(unittest.TestCase):
    def test_missing_part_is_not_found_but_block_and_generic_bad_request_are_errors(self):
        url = 'https://www.wattpad.com/1370376413'
        endpoint = 'https://www.wattpad.com/v4/parts/1370376413?fields=group(id)'
        cases = ((400, b'{"message":"Story part not found"}', True), (404, b'', True),
                 (400, b'{"message":"Bad request"}', False), (403, b'Blocked', False),
                 (503, b'Temporary outage', False))
        for method in ('download', 'preview'):
            for code, body, missing in cases:
                error = HTTPErrorFFF(endpoint, code, 'Source returned an HTTP error', body)
                with self.subTest(method=method, code=code, body=body), \
                        patch('src.sources.ficbook.adapters.getAdapter', side_effect=error), \
                        self.assertRaises(FicbookError) as caught:
                    getattr(FicbookClient(), method)(url)
                self.assertEqual(isinstance(caught.exception, FicbookNotFoundError), missing)


class AlertFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_first_alert_is_sent_and_missing_works_are_silent(self):
        from src.bot.app import _notify_admin
        bot = SimpleNamespace(id=1, send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
        user = SimpleNamespace(id=3, username='reader', full_name='Reader')
        message = SimpleNamespace(from_user=user, chat=SimpleNamespace(id=3), bot=bot)
        store = SimpleNamespace(create=Mock(side_effect=['token', None]), bind_message=Mock())
        url = 'https://www.wattpad.com/1370376413'
        for error in (FicbookNotFoundError('Missing'), FicbookError('Failure'), FicbookError('Failure again')):
            await _notify_admin(message, 9, None, url, error, expected=True, retry_store=store,
                                formats=('epub',), opening_page='annotation')
        bot.send_message.assert_awaited_once()
        store.bind_message.assert_called_once_with('token', 2)
        self.assertEqual(store.create.call_count, 2)
        self.assertEqual(store.create.call_args.kwargs['opening_page'], 'annotation')
