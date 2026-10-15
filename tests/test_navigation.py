from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from src.core.models import Chapter, Story
from src.exporters.navigation import prepare_chapters


class NavigationTests(unittest.TestCase):
    def test_repeated_ids_are_scoped_and_cross_chapter_links_are_local(self) -> None:
        story = Story('Book', 'Author', 'https://example.org/book', chapters=[
            Chapter('First', '<p>Text<a href="#note">[1]</a></p><p id="note">First note</p>',
                    'https://example.org/book/1'),
            Chapter('Second', '<p><a href="#note">[1]</a><a href="/book/1#note">Earlier</a></p>'
                    '<p id="note">Second note</p>', 'https://example.org/book/2'),
        ])
        chapters = prepare_chapters(story)
        first, second = [BeautifulSoup(chapter.html, 'lxml') for chapter in chapters]
        first_target = first.find(id='c0001-a0001')
        self.assertIsNotNone(first_target)
        self.assertEqual(first.find('a', string='[1]')['href'], '#c0001-a0001')
        self.assertEqual(second.find('a', string='[1]')['href'], '#c0002-a0001')
        self.assertEqual(second.find('a', string='Earlier')['href'], '#c0001-a0001')
        self.assertTrue(first_target.select('a[href^="#"]'))
        self.assertEqual(story.chapters[0].html.count('id="note"'), 1)

    def test_unsafe_and_missing_links_keep_text_without_broken_targets(self) -> None:
        story = Story('Book', 'Author', 'https://example.org/book', chapters=[
            Chapter('First', '<p><a href="javascript:alert(1)">Bad</a>'
                    '<a href="#absent">Missing</a><a name="old"></a>'
                    '<a href="#old">Legacy</a></p>'),
        ])
        soup = BeautifulSoup(prepare_chapters(story)[0].html, 'lxml')
        self.assertFalse(soup.find('a', string='Bad').has_attr('href'))
        self.assertFalse(soup.find('a', string='Missing').has_attr('href'))
        self.assertIsNotNone(soup.find(id=soup.find('a', string='Legacy')['href'][1:]))

    def test_ao3_absolute_root_fragments_and_existing_backlinks_stay_internal(self) -> None:
        story = Story('Book', 'Author', 'https://archiveofourown.org/works/1', chapters=[
            Chapter('First', '<p><a id="ref" href="#note">[1]</a></p>'
                    '<ol><li id="note">Note <a href="https://archiveofourown.org#ref">Return</a></li></ol>',
                    'https://archiveofourown.org/works/1/chapters/2'),
        ])
        soup = BeautifulSoup(prepare_chapters(story)[0].html, 'lxml')
        backlink = soup.find('a', string='Return')
        self.assertTrue(backlink['href'].startswith('#'))
        self.assertFalse(soup.select('.note-backlink'))

    def test_bad_url_does_not_prevent_book_generation(self) -> None:
        story = Story('Book', 'Author', 'https://example.org/book', chapters=[
            Chapter('First', '<p><a href="https://[invalid">Broken</a></p>'),
        ])
        soup = BeautifulSoup(prepare_chapters(story)[0].html, 'lxml')
        self.assertFalse(soup.find('a').has_attr('href'))

    def test_document_root_anchor_resolves_to_chapter_start(self) -> None:
        story = Story('Book', 'Author', 'https://example.org/book', chapters=[
            Chapter('First', '<html><body id="top"><p><a href="#top">Start</a></p></body></html>'),
        ])
        soup = BeautifulSoup(prepare_chapters(story)[0].html, 'lxml')
        self.assertEqual(soup.find('a', string='Start')['href'], '#chapter-0001')


if __name__ == '__main__':
    unittest.main()
