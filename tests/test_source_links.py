from __future__ import annotations

import unittest

from bs4 import BeautifulSoup
from fanficfare import adapters

from src.core.models import Chapter, Story
from src.exporters.navigation import prepare_chapters
from src.sources.ficbook import FicbookAccount, FicbookClient
from src.sources.litnet import LitnetClient
from src.sources.ranobelib import RanobelibClient


class SourceLinkTests(unittest.TestCase):
    def assert_note_link(self, html: str, source_url: str) -> None:
        story = Story('Book', 'Author', source_url, chapters=[Chapter('Chapter', html, source_url)])
        soup = BeautifulSoup(prepare_chapters(story)[0].html, 'lxml')
        reference = soup.find('a', string='[1]')
        self.assertIsNotNone(reference)
        self.assertTrue(reference['href'].startswith('#'))
        target = soup.find(id=reference['href'][1:])
        self.assertIsNotNone(target)
        self.assertIn('Note body', target.get_text(' ', strip=True))
        self.assertIsNotNone(target.find('a', href='#' + reference['id']))

    def test_ficbook_popup_notes_become_linkable_text(self) -> None:
        soup = BeautifulSoup('<div id="content"><p>Text<span class="footnote" id="fn"></span>.</p></div>'
                             '<script>window.textFootnotes = {"fn":"<p>Note body</p>"};</script>', 'lxml')
        html = FicbookClient()._chapter_content_html(soup, soup.select_one('#content'))
        self.assert_note_link(html, 'https://ficbook.net/readfic/1')

    def test_hogwartsnet_retains_note_anchors(self) -> None:
        soup = BeautifulSoup('<div id="chap_text"><p>Text<a href="#fn">[1]</a></p>'
                             '<p id="fn">Note body</p></div>', 'lxml')
        _, html = FicbookClient()._hogwartsnet_chapter(soup, 'Chapter')
        self.assert_note_link(html, 'https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=1')

    def test_litnet_retains_note_anchors(self) -> None:
        soup = BeautifulSoup('<div class="reader-text"><p>Text<a href="#fn">[1]</a></p>'
                             '<p id="fn">Note body</p></div>', 'lxml')
        self.assert_note_link(LitnetClient()._chapter_html(soup), 'https://litnet.com/ru/reader/book-b1?c=1')

    def test_ranobelib_structured_content_retains_link_destinations(self) -> None:
        content = {'type': 'doc', 'content': [
            {'type': 'paragraph', 'content': [
                {'type': 'text', 'text': '[1]', 'marks': [{'type': 'link', 'attrs': {'href': '#fn'}}]},
            ]},
            {'type': 'paragraph', 'attrs': {'id': 'fn'}, 'content': [
                {'type': 'text', 'text': 'Note body'},
            ]},
        ]}
        self.assert_note_link(RanobelibClient()._content_html(content), 'https://ranobelib.me/1--book/v1/c1')

    def test_fanficfare_retains_ao3_and_wattpad_note_anchors(self) -> None:
        for url in ('https://archiveofourown.org/works/1', 'https://www.wattpad.com/story/123-test'):
            with self.subTest(site=url):
                client = FicbookClient()
                config = client._build_config(url, FicbookAccount())
                adapter = adapters.getAdapter(config, url)
                soup = BeautifulSoup('<div><p>Text<a href="#fn">[1]</a></p>'
                                     '<p id="fn">Note body</p></div>', 'html5lib')
                html = adapter.utf8FromSoup(url, soup.div)
                self.assert_note_link(html, url)

    def test_ranobelib_notes_in_lists_keep_their_targets(self) -> None:
        content = {'type': 'doc', 'content': [
            {'type': 'paragraph', 'content': [
                {'type': 'text', 'text': '[1]', 'marks': [{'type': 'link', 'attrs': {'href': '#fn'}}]},
            ]},
            {'type': 'ordered_list', 'content': [
                {'type': 'list_item', 'attrs': {'id': 'fn'}, 'content': [
                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Note body'}]},
                ]},
            ]},
        ]}
        self.assert_note_link(RanobelibClient()._content_html(content), 'https://ranobelib.me/1--book/v1/c1')


if __name__ == '__main__':
    unittest.main()
