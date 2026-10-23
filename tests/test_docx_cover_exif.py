from dataclasses import replace
from io import BytesIO
from struct import error as StructError, pack
from unittest.mock import patch
from zipfile import ZipFile
import unittest

from PIL import Image
from docx.image.image import Image as DocxImage

from src.bot.app import DownloadProgressState, _build_selected_files
from src.core.limits import ResourceLimitError, ResourceLimits, resource_scope
from src.core.models import CoverImage
from src.exporters import formats
from tests.test_formats_cover import _story


def jpeg_with_empty_exif_string() -> bytes:
    # A valid JPEG with a TIFF ASCII entry whose count is zero: python-docx reads -1 chars.
    exif = (b'Exif\x00\x00' + pack('<2sHIH', b'II', 42, 8, 1)
            + pack('<HHI4s', 270, 2, 0, b'\x00' * 4) + pack('<I', 0))
    buffer = BytesIO()
    Image.new('RGB', (80, 120), (20, 130, 150)).save(buffer, format='JPEG', exif=exif)
    return buffer.getvalue()


class DocxCoverExifTests(unittest.TestCase):
    def test_fixture_reproduces_production_parser_error(self):
        data = jpeg_with_empty_exif_string()
        with Image.open(BytesIO(data)) as image:
            image.load()
            self.assertEqual(image.size, (80, 120))
        with self.assertRaisesRegex(StructError, 'bad char in struct format'):
            DocxImage.from_blob(data)

    def test_bad_exif_cover_is_preserved_as_first_page(self):
        original = jpeg_with_empty_exif_string()
        story = replace(_story(), cover=CoverImage(original, 'image/jpeg', 'jpg'))
        with ZipFile(BytesIO(formats.build_docx(story))) as archive:
            images = [name for name in archive.namelist() if name.startswith('word/media/')]
            self.assertEqual(len(images), 1)
            data = archive.read(images[0])
            self.assertEqual(DocxImage.from_blob(data).px_width, 80)
            with Image.open(BytesIO(data)) as image:
                image.load()
                self.assertEqual(image.size, (80, 120))
                self.assertNotIn('exif', image.info)
            xml = archive.read('word/document.xml').decode()
            self.assertLess(xml.index('w:drawing'), xml.index('Title'))
            self.assertIn('w:type="page"', xml)
            self.assertIn('Body', xml)
        self.assertEqual(story.cover.data, original)

    def test_unreadable_cover_does_not_abort_or_add_blank_page(self):
        story = replace(_story(), cover=CoverImage(b'broken image', 'image/jpeg', 'jpg'))
        with ZipFile(BytesIO(formats.build_docx(story))) as archive:
            self.assertFalse(any(name.startswith('word/media/') for name in archive.namelist()))
            xml = archive.read('word/document.xml').decode()
            self.assertNotIn('w:drawing', xml)
            self.assertNotIn('w:type="page"', xml)
            self.assertIn('Title', xml)
            self.assertIn('Body', xml)

    def test_pixel_limit_skips_cover_before_decoding(self):
        story = replace(_story(), cover=CoverImage(jpeg_with_empty_exif_string(), 'image/jpeg', 'jpg'))
        with resource_scope(ResourceLimits(max_cover_pixels=100)), \
                patch.object(Image.Image, 'load', side_effect=AssertionError('Oversized cover decoded')):
            with ZipFile(BytesIO(formats.build_docx(story))) as archive:
                self.assertFalse(any(name.startswith('word/media/') for name in archive.namelist()))

    def test_valid_exif_orientation_is_preserved_without_metadata(self):
        exif = Image.Exif()
        exif[274] = 6
        buffer = BytesIO()
        Image.new('RGB', (80, 120), 'teal').save(buffer, format='JPEG', exif=exif)
        story = replace(_story(), cover=CoverImage(buffer.getvalue(), 'image/jpeg', 'jpg'))
        with ZipFile(BytesIO(formats.build_docx(story))) as archive:
            name = next(n for n in archive.namelist() if n.startswith('word/media/'))
            with Image.open(BytesIO(archive.read(name))) as image:
                self.assertEqual(image.size, (120, 80))
                self.assertNotIn('exif', image.info)

    def test_palette_transparency_is_preserved(self):
        buffer = BytesIO()
        image = Image.new('P', (40, 60), 0)
        image.putpalette([0, 0, 0] * 256)
        image.save(buffer, format='PNG', transparency=0)
        story = replace(_story(), cover=CoverImage(buffer.getvalue(), 'image/png', 'png'))
        with ZipFile(BytesIO(formats.build_docx(story))) as archive:
            name = next(n for n in archive.namelist() if n.startswith('word/media/'))
            with Image.open(BytesIO(archive.read(name))) as normalized:
                self.assertEqual(normalized.mode, 'RGBA')
                self.assertEqual(normalized.getpixel((0, 0))[3], 0)

    def test_failed_exif_rotation_still_keeps_readable_picture(self):
        story = replace(_story(), cover=CoverImage(jpeg_with_empty_exif_string(), 'image/jpeg', 'jpg'))
        with patch.object(formats.ImageOps, 'exif_transpose', side_effect=StructError('bad EXIF')):
            with ZipFile(BytesIO(formats.build_docx(story))) as archive:
                self.assertTrue(any(name.startswith('word/media/') for name in archive.namelist()))

    def test_decoding_error_skips_cover_and_keeps_text(self):
        story = replace(_story(), cover=CoverImage(jpeg_with_empty_exif_string(), 'image/jpeg', 'jpg'))
        with patch.object(Image.Image, 'load', side_effect=OSError('invalid pixels')):
            with ZipFile(BytesIO(formats.build_docx(story))) as archive:
                self.assertFalse(any(name.startswith('word/media/') for name in archive.namelist()))
                self.assertIn(b'Body', archive.read('word/document.xml'))

    def test_cover_encoding_limit_does_not_abort_document(self):
        story = replace(_story(), cover=CoverImage(jpeg_with_empty_exif_string(), 'image/jpeg', 'jpg'))
        with patch.object(Image.Image, 'save', side_effect=ResourceLimitError('image byte limit')):
            with ZipFile(BytesIO(formats.build_docx(story))) as archive:
                self.assertFalse(any(name.startswith('word/media/') for name in archive.namelist()))
                self.assertIn(b'Body', archive.read('word/document.xml'))

    def test_global_deadline_is_not_swallowed_as_bad_cover(self):
        data = jpeg_with_empty_exif_string()
        with patch.object(Image.Image, 'save', side_effect=ResourceLimitError('image byte limit')), \
                patch.object(formats, 'check_deadline', side_effect=[None, ResourceLimitError('deadline')]):
            with self.assertRaisesRegex(ResourceLimitError, 'deadline'):
                formats._docx_cover_bytes(data)


class DocxCoverExifPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_bad_exif_does_not_abort_all_selected_formats(self):
        story = replace(_story(), cover=CoverImage(jpeg_with_empty_exif_string(), 'image/jpeg', 'jpg'))
        files = dict(await _build_selected_files(story, ('fb2', 'epub', 'txt', 'docx', 'pdf'), DownloadProgressState()))
        self.assertEqual(set(files), {'fb2', 'epub', 'txt', 'docx', 'pdf'})
        self.assertTrue(all(files.values()))
        with ZipFile(BytesIO(files['epub'])) as archive:
            self.assertEqual(archive.read('OEBPS/images/cover.jpg'), story.cover.data)

