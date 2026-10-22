from dataclasses import replace
from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from src.core.models import CoverImage
from src.core import covers
from src.core.covers import cover_for_story, is_ficbook_placeholder, text_cover, _fit_text
from src.bot.app import DownloadProgressState, _build_selected_files
from src.sources.ficbook import FicbookClient
from tests.test_formats_cover import _story


FINGERPRINT = 0xd36191475cb996b6a5e6a48888f88d6d3d48


def placeholder():
    # Synthetic greyscale gradients reproduce the known banner's comparison bits.
    bits = f'{FINGERPRINT:0144b}'
    pixels = []
    for row in range(9):
        value = 128
        pixels.append(value)
        for bit in bits[row * 16:(row + 1) * 16]:
            value += -6 if bit == '1' else 6
            pixels.append(value)
    image = Image.new('L', (17, 9))
    image.putdata(pixels)
    image = image.resize((510, 228), Image.Resampling.NEAREST)
    buffer = BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=90)
    return CoverImage(buffer.getvalue(), 'image/jpeg', 'jpg')


class TextCoverTests(unittest.TestCase):
    def test_known_placeholder_is_recognized_but_another_landscape_is_not(self):
        self.assertTrue(is_ficbook_placeholder(placeholder()))
        image = Image.new('RGB', (510, 228), 'purple')
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        self.assertFalse(is_ficbook_placeholder(CoverImage(buffer.getvalue(), 'image/png', 'png')))
        self.assertFalse(is_ficbook_placeholder(CoverImage(b'invalid image', 'image/jpeg', 'jpg')))

    def test_real_cover_is_preserved_and_disabled_cover_is_absent(self):
        story = _story()
        self.assertIs(cover_for_story(story), story.cover)
        self.assertIsNone(cover_for_story(story, enabled=False))
        self.assertIsNone(cover_for_story(_story(False), enabled=False))

    def test_missing_cover_and_ficbook_banner_get_a_text_cover(self):
        for original in (None, placeholder()):
            story = replace(_story(), title='Необъявленная война', author='Фрейфея', cover=original)
            cover = cover_for_story(story)
            self.assertEqual(cover.mime, 'image/png')
            with Image.open(BytesIO(cover.data)) as image:
                self.assertEqual(image.size, (1200, 1800))
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                self.assertIsNotNone(image.convert('L').point(lambda p: 255 - p).getbbox())
            self.assertIs(story.cover, original)

    def test_other_sites_real_cover_is_not_classified_as_a_ficbook_placeholder(self):
        story = replace(_story(), source_url='https://archiveofourown.org/works/1', cover=placeholder())
        self.assertIs(cover_for_story(story), story.cover)

    def test_text_cover_uses_the_actual_title_author_and_translator(self):
        with patch('src.core.covers._draw_text', wraps=covers._draw_text) as draw:
            text_cover('Необъявленная война', 'Фрейфея', 'Переводчик')
        text = ' '.join(str(call.args[1]) for call in draw.call_args_list)
        self.assertIn('Необъявленная война', text)
        self.assertIn('Фрейфея', text)
        self.assertIn('Переводчик', text)

    def test_long_title_and_unbroken_words_fit_the_assigned_rectangle(self):
        from PIL import ImageDraw
        draw = ImageDraw.Draw(Image.new('RGB', (1200, 1800), 'white'))
        for text in ('Очень длинное название ' * 100, 'Неразрывно' * 200):
            font, lines = _fit_text(draw, text, 1000, 850, 108)
            self.assertTrue(lines)
            self.assertTrue(all(draw.textbbox((0, 0), line, font=font)[2] <= 1000 for line in lines))
            heights = [draw.textbbox((0, 0), line, font=font)[3] - draw.textbbox((0, 0), line, font=font)[1] for line in lines]
            self.assertLessEqual(sum(heights) + (len(lines) - 1) * max(4, font.size // 4), 850)

    def test_normal_title_words_are_not_split_to_keep_a_larger_font(self):
        from PIL import ImageDraw
        draw = ImageDraw.Draw(Image.new('RGB', (1200, 1800), 'white'))
        _, lines = _fit_text(draw, 'Необъявленная война', 1000, 850, 108, bold=True)
        self.assertEqual(' '.join(lines), 'Необъявленная война')

    def test_source_discards_preloaded_ficbook_banner_but_keeps_real_images(self):
        client = FicbookClient()
        banner = placeholder()
        for cover, expected in ((banner, None), (_story().cover, 'image/png')):
            story = Mock()
            story.getImgUrls.return_value = [dict(newsrc='images/cover.' + cover.extension,
                                                data=cover.data, mime=cover.mime, ext=cover.extension)]
            result = client._extract_cover_image(story, source_url='https://ficbook.net/readfic/554903')
            self.assertEqual(result.mime if result else None, expected)


class TextCoverExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_cover_is_embedded_in_all_image_formats_and_metadata_is_unchanged(self):
        story = replace(_story(False), title='Необъявленная война', author='Фрейфея')
        files = dict(await _build_selected_files(story, ('epub', 'fb2', 'docx', 'pdf', 'txt'), DownloadProgressState()))
        with ZipFile(BytesIO(files['epub'])) as archive:
            opf = ET.fromstring(archive.read('OEBPS/content.opf'))
            self.assertEqual(opf.findtext('.//{*}title'), story.title)
            self.assertEqual(opf.findtext('.//{*}creator'), story.author)
            image = opf.find('.//{*}item[@properties="cover-image"]')
            self.assertIsNotNone(image)
            self.assertIn('OEBPS/' + image.get('href'), archive.namelist())
        self.assertIsNotNone(ET.fromstring(files['fb2']).find('.//{*}coverpage'))
        with ZipFile(BytesIO(files['docx'])) as archive:
            self.assertTrue(any(name.startswith('word/media/') for name in archive.namelist()))
        self.assertTrue(files['pdf'].startswith(b'%PDF'))
        self.assertIn(story.title, files['txt'].decode('utf-8'))
        self.assertIsNone(story.cover)

    async def test_disabled_cover_is_not_regenerated(self):
        files = dict(await _build_selected_files(_story(), ('epub', 'fb2'), DownloadProgressState(), cover_enabled=False))
        with ZipFile(BytesIO(files['epub'])) as archive:
            self.assertNotIn('OEBPS/cover.xhtml', archive.namelist())
        self.assertIsNone(ET.fromstring(files['fb2']).find('.//{*}coverpage'))

    async def test_txt_does_not_spend_time_generating_an_image(self):
        with patch('src.bot.app.cover_for_story', side_effect=AssertionError('TXT has no cover')):
            await _build_selected_files(_story(False), ('txt',), DownloadProgressState())
