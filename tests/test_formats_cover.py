from __future__ import annotations

from base64 import b64decode
from zipfile import ZipFile
from io import BytesIO
import unittest

from src.core.models import Chapter, CoverImage, Story
from src.exporters.epub import build_epub
from src.exporters.fb2 import build_fb2
from src.exporters.formats import build_docx, build_pdf, build_txt


PNG_1X1 = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _story(cover: bool = True) -> Story:
    return Story(
        title="Title",
        author="Author",
        source_url="https://ficbook.net/readfic/1",
        annotation_html="<p>Annotation</p>",
        page_count="12",
        word_count="3 456",
        pairings=["Гарри Поттер/Драко Малфой", "Гермиона Грейнджер"],
        cover=CoverImage(PNG_1X1, "image/png", "png") if cover else None,
        chapters=[Chapter("Chapter 1", "<p>Body</p>")],
    )


class FormatCoverTests(unittest.TestCase):
    def test_chapter_notes_keep_order_in_text_and_xml_formats(self) -> None:
        story = _story(cover=False)
        story.chapters = [
            Chapter(
                "Chapter 1",
                "<p><b>Примечания:</b></p><p>Перед главой</p>"
                "<p>Текст главы</p><p><b>Примечания:</b></p><p>После главы</p>",
            )
        ]
        payloads = [build_txt(story).decode("utf-8"), build_fb2(story).decode("utf-8")]
        with ZipFile(BytesIO(build_epub(story))) as archive:
            payloads.append(archive.read("OEBPS/chapter-0001.xhtml").decode("utf-8"))
        with ZipFile(BytesIO(build_docx(story))) as archive:
            payloads.append(archive.read("word/document.xml").decode("utf-8"))

        for payload in payloads:
            with self.subTest(format=payload[:5]):
                self.assertLess(payload.index("Перед главой"), payload.index("Текст главы"))
                self.assertLess(payload.index("Текст главы"), payload.index("После главы"))

    def test_txt_contains_ficbook_pages(self) -> None:
        text = build_txt(_story()).decode("utf-8")
        self.assertIn("Страниц: 12", text)
        self.assertIn(
            "Пейринг и персонажи: Гарри Поттер/Драко Малфой, Гермиона Грейнджер",
            text,
        )

    def test_epub_contains_cover_manifest_and_page_count(self) -> None:
        payload = build_epub(_story())
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIn("OEBPS/images/cover.png", archive.namelist())
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
            intro = archive.read("OEBPS/intro.xhtml").decode("utf-8")
        self.assertIn("cover-image", opf)
        self.assertIn("Страниц: 12", intro)
        self.assertIn(
            "Пейринг и персонажи: Гарри Поттер/Драко Малфой, Гермиона Грейнджер",
            intro,
        )
        self.assertIn("<dc:subject>Гарри Поттер/Драко Малфой</dc:subject>", opf)

    def test_fb2_contains_cover_and_page_count(self) -> None:
        text = build_fb2(_story()).decode("utf-8")
        self.assertIn("<coverpage>", text)
        self.assertIn("<binary id=\"cover.png\" content-type=\"image/png\">", text)
        self.assertIn("ficbook-pages", text)
        self.assertIn("Страниц: 12", text)
        self.assertIn(
            "Пейринг и персонажи: Гарри Поттер/Драко Малфой, Гермиона Грейнджер",
            text,
        )
        self.assertIn(
            "<keywords>Гарри Поттер/Драко Малфой, Гермиона Грейнджер</keywords>",
            text,
        )
        self.assertLess(text.index("</annotation>"), text.index("<keywords>"))
        self.assertLess(text.index("</keywords>"), text.index("<coverpage>"))
        self.assertIn("<body><section><title><p>Информация</p></title>", text)

    def test_docx_builds_with_cover(self) -> None:
        payload = build_docx(_story())
        self.assertTrue(payload.startswith(b"PK"))
        with ZipFile(BytesIO(payload)) as archive:
            footer_names = [name for name in archive.namelist() if name.startswith("word/footer")]
            footer_xml = "\n".join(archive.read(name).decode("utf-8") for name in footer_names)
            document_xml = archive.read("word/document.xml").decode("utf-8")
            properties_xml = archive.read("docProps/core.xml").decode("utf-8")
        self.assertIn("PAGE", footer_xml)
        self.assertIn("NUMPAGES", footer_xml)
        self.assertIn(
            "Пейринг и персонажи: Гарри Поттер/Драко Малфой, Гермиона Грейнджер",
            document_xml,
        )
        self.assertIn("Гарри Поттер/Драко Малфой", properties_xml)

    def test_pdf_builds_with_page_numbers(self) -> None:
        payload = build_pdf(_story())
        self.assertTrue(payload.startswith(b"%PDF"))

    def test_formats_build_without_cover(self) -> None:
        story = _story(cover=False)
        self.assertTrue(build_epub(story))
        self.assertNotIn("<coverpage>", build_fb2(story).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
