from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from src.bot.ui import WELCOME_TEXT
from src.core.progress import ProgressReporter
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt
from src.sources.ficbook import FicbookClient, FicbookError, FicbookNotFoundError, FicbookRateLimitError
from src.sources.zahleb import ZahlebAccessError, ZahlebNotFoundError, ZahlebRateLimitError
from src.storage.analytics import _public_story_url
from tests.test_zahleb import FakeClient, ROOT


class ZahlebIntegrationTests(unittest.TestCase):
    def client(self):
        client = FicbookClient(retry_attempts=1)
        client.zahleb = FakeClient()
        return client

    def test_facade_uses_zahleb_without_touching_ficbook_transport(self):
        client = self.client()
        with patch.object(client, '_build_config', side_effect=AssertionError('Native transport used')):
            story = client.download(ROOT, chapter_numbers={2})
            preview = client.preview(ROOT)
        self.assertEqual(story.author, 'Author')
        self.assertEqual(story.chapters[0].title, 'Second')
        self.assertEqual(len(preview.chapters), 2)

    def test_errors_use_existing_bot_error_types(self):
        for error, expected in ((ZahlebNotFoundError('Not found'), FicbookNotFoundError),
                                (ZahlebRateLimitError('Limit'), FicbookRateLimitError),
                                (ZahlebAccessError('Closed'), FicbookError)):
            client = self.client()
            client.zahleb.preview = Mock(side_effect=error)
            client.zahleb.download = Mock(side_effect=error)
            for operation in (client.preview, client.download):
                public_error = FicbookError if operation == client.preview and expected is FicbookRateLimitError else expected
                with self.subTest(error=type(error).__name__, operation=operation.__name__), self.assertRaises(public_error):
                    operation(ROOT)

    def test_all_five_formats_keep_title_and_author(self):
        story = self.client().download(ROOT)
        self.assertIn(b'Chapter text', build_txt(story))
        self.assertTrue(build_pdf(story).startswith(b'%PDF-'))
        fb2 = ET.fromstring(build_fb2(story))
        ns = {'f': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
        self.assertEqual(fb2.find('.//f:book-title', ns).text, 'Book <&>')
        with ZipFile(BytesIO(build_epub(story))) as archive:
            opf = ET.fromstring(archive.read('OEBPS/content.opf'))
            dc = {'dc': 'http://purl.org/dc/elements/1.1/'}
            self.assertEqual(opf.find('.//dc:title', dc).text, 'Book <&>')
            self.assertEqual(opf.find('.//dc:creator', dc).text, 'Author')
        with ZipFile(BytesIO(build_docx(story))) as archive:
            self.assertIn(b'Author', archive.read('docProps/core.xml'))

    def test_title_event_is_available_to_download_status(self):
        events = []
        progress = ProgressReporter(lambda text: events.append(('progress', text)),
                                    lambda title: events.append(('title', title)))
        self.client().download(ROOT, progress)
        title_index = events.index(('title', 'Book <&>'))
        chapter_index = next(i for i, (_, text) in enumerate(events) if 'главы:' in text)
        self.assertLess(title_index, chapter_index)

    def test_history_and_admin_retry_url_keep_work_identity(self):
        self.assertEqual(_public_story_url(ROOT + '/nEZoFrmTKQ?token=private'), ROOT)
        self.assertIn('Взахлёб (zahleb.me)', WELCOME_TEXT)


if __name__ == '__main__':
    unittest.main()
