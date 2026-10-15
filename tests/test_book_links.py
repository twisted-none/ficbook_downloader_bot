from __future__ import annotations

from io import BytesIO
import re
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from src.core.models import Chapter, Story
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt


def linked_story(host: str = 'ficbook.net') -> Story:
    return Story('Book', 'Author', f'https://{host}/book', chapters=[
        Chapter('Chapter 2', '<p>Text<sup><a href="#fn">[1]</a></sup>.</p>'
                '<div id="fn"><p>A <b>bold</b> note.</p></div>', f'https://{host}/book/2'),
        Chapter('Chapter 5', '<p><a href="#fn">[1]</a> '
                '<a href="/book/2#fn">Earlier note</a></p>'
                '<p id="fn">Second note.</p>', f'https://{host}/book/5'),
    ])


class BookLinkTests(unittest.TestCase):
    def test_epub_links_resolve_across_chapters_and_toc_is_in_spine(self) -> None:
        for host in ('ficbook.net', 'archiveofourown.org', 'www.wattpad.com',
                     'hogwartsnet.ru', 'ranobelib.me', 'litnet.com'):
            with self.subTest(site=host), ZipFile(BytesIO(build_epub(linked_story(host)))) as archive:
                pages = {name.removeprefix('OEBPS/'): ET.fromstring(archive.read(name))
                         for name in archive.namelist() if name.endswith('.xhtml')}
                for name, page in pages.items():
                    for link in page.findall('.//{*}a'):
                        href = link.get('href', '')
                        if href.startswith(('http:', 'https:')):
                            continue
                        target_file, _, anchor = href.partition('#')
                        target = pages[target_file or name]
                        if anchor:
                            self.assertTrue(any(node.get('id') == anchor for node in target.iter()), href)
                chapter = pages['chapter-0002.xhtml']
                earlier = next(node for node in chapter.findall('.//{*}a') if node.text == 'Earlier note')
                self.assertTrue(earlier.get('href').startswith('chapter-0001.xhtml#'))
                opf = ET.fromstring(archive.read('OEBPS/content.opf'))
                self.assertTrue(any(node.get('idref') == 'nav' for node in opf.findall('.//{*}itemref')))
                self.assertNotIn('epub:type="footnote"', archive.read('OEBPS/chapter-0001.xhtml').decode())

    def test_fb2_toc_footnotes_and_backlinks_resolve(self) -> None:
        root = ET.fromstring(build_fb2(linked_story()))
        ids = [node.get('id') for node in root.iter() if node.get('id')]
        self.assertEqual(len(ids), len(set(ids)))
        links = root.findall('.//{*}a')
        self.assertGreaterEqual(len(links), 8)
        for link in links:
            href = link.get('{http://www.w3.org/1999/xlink}href', '')
            if href.startswith('#'):
                self.assertIn(href[1:], ids)
            self.assertNotEqual(link.get('type'), 'note')
        self.assertIn('Оглавление', ''.join(root.itertext()))

    def test_docx_uses_bookmarks_and_internal_hyperlinks(self) -> None:
        with ZipFile(BytesIO(build_docx(linked_story()))) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        bookmarks = [node.get(ns + 'name') for node in root.iter(ns + 'bookmarkStart')]
        self.assertEqual(len(bookmarks), len(set(bookmarks)))
        links = list(root.iter(ns + 'hyperlink'))
        self.assertGreaterEqual(len(links), 8)
        for link in links:
            anchor = link.get(ns + 'anchor')
            if anchor:
                self.assertIn(anchor, bookmarks)
        self.assertEqual(sum(link.get(ns + 'anchor', '').startswith('chapter_') for link in links), 2)

    def test_pdf_contains_internal_link_annotations_and_toc(self) -> None:
        payload = build_pdf(linked_story())
        links = re.findall(rb'/Subtype /Link', payload)
        self.assertGreaterEqual(len(links), 8)
        self.assertGreaterEqual(len(re.findall(rb'/Dest\s*\[', payload)), 8)

    def test_docx_does_not_link_to_an_empty_annotation(self) -> None:
        story = linked_story()
        story.annotation_html = '<p><br/></p>'
        with ZipFile(BytesIO(build_docx(story))) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        self.assertFalse(any(link.get(ns + 'anchor') == 'annotation' for link in root.iter(ns + 'hyperlink')))

    def test_txt_keeps_notes_and_a_plain_toc(self) -> None:
        text = build_txt(linked_story()).decode('utf-8')
        self.assertIn('Оглавление', text)
        self.assertIn('[1]', text)
        self.assertIn('bold', text)
        self.assertNotIn('href=', text)


if __name__ == '__main__':
    unittest.main()
