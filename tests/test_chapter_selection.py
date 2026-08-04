from __future__ import annotations

import unittest

from src.core.chapter_selection import ChapterSelectionError, parse_chapter_numbers, parse_chapter_selection
from src.sources.ficbook import select_chapter_links


class ChapterSelectionTests(unittest.TestCase):
    def test_parse_single_numbers_and_ranges(self) -> None:
        self.assertEqual(parse_chapter_numbers("1,2,5-7,7", 10), (1, 2, 5, 6, 7))

    def test_parse_selection_accepts_zero_for_all_chapters(self) -> None:
        self.assertIsNone(parse_chapter_selection("0", 10))

    def test_parse_selection_keeps_explicit_chapter_numbers(self) -> None:
        self.assertEqual(parse_chapter_selection("1,3-4", 10), (1, 3, 4))

    def test_parse_rejects_invalid_tokens(self) -> None:
        with self.assertRaises(ChapterSelectionError):
            parse_chapter_numbers("1, two", 10)

    def test_parse_rejects_reverse_ranges(self) -> None:
        with self.assertRaises(ChapterSelectionError):
            parse_chapter_numbers("5-3", 10)

    def test_parse_rejects_out_of_range_numbers(self) -> None:
        with self.assertRaises(ChapterSelectionError):
            parse_chapter_numbers("1,11", 10)

    def test_select_chapter_links_preserves_story_order(self) -> None:
        chapters = [{"title": "1"}, {"title": "2"}, {"title": "3"}]
        selected = select_chapter_links(chapters, {3, 1})
        self.assertEqual([index for index, _ in selected], [1, 3])

    def test_select_chapter_links_keeps_all_without_filter(self) -> None:
        chapters = [{"title": "1"}, {"title": "2"}]
        selected = select_chapter_links(chapters, None)
        self.assertEqual([index for index, _ in selected], [1, 2])


if __name__ == "__main__":
    unittest.main()
