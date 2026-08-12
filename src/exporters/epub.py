from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from io import BytesIO
import re
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from bs4 import BeautifulSoup, NavigableString, Tag

from src.core.models import Chapter, Story

SKIP_TAGS = {"audio", "iframe", "img", "script", "style", "video"}
CONTAINER_TAGS = {"article", "div", "section"}
INLINE_TAGS = {"b": "strong", "strong": "strong", "i": "em", "em": "em"}

CSS = """
body { font-family: serif; line-height: 1.45; margin: 5%; }
p { margin: 0 0 0.8em; text-indent: 1.2em; }
h1, h2 { line-height: 1.2; text-indent: 0; }
blockquote { margin: 1em 1.5em; font-style: italic; }
ul, ol { margin: 0.8em 0 0.8em 1.5em; padding: 0; }
li { margin: 0.25em 0; }
.meta p, .note-title { text-indent: 0; }
.note-title { font-weight: bold; margin-top: 1.2em; }
.cover { margin: 0; text-align: center; }
.cover img { max-width: 100%; max-height: 95vh; }
""".strip()


def build_epub(story: Story) -> bytes:
    book_id = str(uuid5(NAMESPACE_URL, story.source_url))
    chapters = [_chapter_page(index, chapter) for index, chapter in enumerate(story.chapters, 1)]
    intro = _page("intro.xhtml", "Аннотация", _intro_body(story))
    files = [intro, *chapters]
    if story.cover:
        files.insert(0, _cover_page(story))
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", _container_xml(), compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/styles/book.css", CSS, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", _opf(story, book_id, files), compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/toc.ncx", _ncx(story, book_id, files), compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/nav.xhtml", _nav(story, files), compress_type=ZIP_DEFLATED)
        if story.cover:
            archive.writestr(_cover_archive_path(story), story.cover.data, compress_type=ZIP_DEFLATED)
        for page in files:
            archive.writestr(f"OEBPS/{page['href']}", page["content"], compress_type=ZIP_DEFLATED)
    return buffer.getvalue()


def _chapter_page(index: int, chapter: Chapter) -> dict[str, str]:
    body = _clean_blocks(chapter.html) or "<p></p>"
    return _page(f"chapter-{index:04}.xhtml", chapter.title or f"Глава {index}", body)


def _cover_page(story: Story) -> dict[str, str]:
    href = escape(_cover_href(story), quote=True)
    body = f'<section class="cover"><img src="{href}" alt="{escape(story.title, quote=True)}"/></section>'
    return _page("cover.xhtml", "Обложка", body)


def _intro_body(story: Story) -> str:
    meta = []
    if story.page_count:
        meta.append(f"<p>Страниц: {escape(story.page_count)}</p>")
    if story.word_count:
        meta.append(f"<p>Слов: {escape(story.word_count)}</p>")
    if story.pairings:
        meta.append(f"<p>Пейринг и персонажи: {escape(', '.join(story.pairings))}</p>")
    annotation = _clean_blocks(story.annotation_html or story.description, "meta")
    return f'<section class="meta">{"".join(meta)}</section>{annotation}' if meta else annotation


def _page(href: str, title: str, body: str) -> dict[str, str]:
    safe_title = escape(title)
    content = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ru">
<head><title>{safe_title}</title><link rel="stylesheet" href="styles/book.css" type="text/css"/></head>
<body><h1>{safe_title}</h1>{body}</body></html>"""
    return {"href": href, "title": title, "content": content}


def _clean_blocks(html: str, css_class: str = "") -> str:
    if not html.strip():
        return ""
    soup = BeautifulSoup(html, "lxml")
    blocks = "".join(_render_block(child) for child in (soup.body or soup).children)
    if css_class and blocks:
        return f'<section class="{css_class}">{blocks}</section>'
    return blocks


def _render_block(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        text = _normalize_text(str(node))
        return f"<p>{escape(text)}</p>" if text else ""
    if node.name in SKIP_TAGS:
        return ""
    if node.name in CONTAINER_TAGS:
        return "".join(_render_block(child) for child in node.children)
    if node.name in {"ul", "ol"}:
        items = "".join(_render_list_item(child) for child in node.children)
        return f"<{node.name}>{items}</{node.name}>" if items else ""
    if node.name == "blockquote":
        content = "".join(_render_block(child) for child in node.children) or _render_inline_children(node)
        return f"<blockquote>{content}</blockquote>" if content else ""
    if node.name == "p":
        content = _render_inline_children(node)
        css_class = ' class="note-title"' if _is_note_heading(node) else ""
        return f"<p{css_class}>{content}</p>" if content else ""
    if node.name == "br":
        return ""
    content = _render_inline(node)
    return f"<p>{content}</p>" if content else ""


def _render_list_item(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        text = escape(_normalize_text(str(node)))
        return f"<li>{text}</li>" if text else ""
    if node.name != "li":
        return _render_block(node)
    content = _render_inline_children(node) or "".join(_render_block(child) for child in node.children)
    return f"<li>{content}</li>" if content else ""


def _render_inline_children(node: Tag) -> str:
    return _normalize_inline("".join(_render_inline(child) for child in node.children))


def _render_inline(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return escape(_normalize_text(str(node)))
    if node.name in SKIP_TAGS:
        return ""
    if node.name == "br":
        return "<br />"
    content = _render_inline_children(node)
    mapped = INLINE_TAGS.get(node.name)
    if mapped and content:
        return f"<{mapped}>{content}</{mapped}>"
    if node.name == "a" and content:
        href = escape(str(node.get("href", "")).strip(), quote=True)
        return f'<a href="{href}">{content}</a>' if href.startswith(("http://", "https://")) else content
    return content


def _normalize_inline(text: str) -> str:
    text = re.sub(r"[ \t\r\n]+", " ", text.replace("\xa0", " "))
    return re.sub(r">\s+<", "><", text).strip()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _container_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _opf(story: Story, book_id: str, pages: list[dict[str, str]]) -> str:
    items = "".join(f'<item id="p{i}" href="{p["href"]}" media-type="application/xhtml+xml"/>' for i, p in enumerate(pages))
    spine = "".join(f'<itemref idref="p{i}"/>' for i, _ in enumerate(pages))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subjects = "".join(
        f"<dc:subject>{escape(item)}</dc:subject>"
        for item in [*story.fandoms, *story.genres, *story.pairings]
    )
    description = _description_xml(story.annotation_html or story.description)
    dates = _date_xml("created", story.published) + _date_xml("modified", story.updated)
    cover_item = ""
    cover_meta = ""
    if story.cover:
        cover_item = (
            f'<item id="cover-image" href="{escape(_cover_href(story), quote=True)}" '
            f'media-type="{escape(story.cover.mime, quote=True)}" properties="cover-image"/>'
        )
        cover_meta = '<meta name="cover" content="cover-image"/>'
    pages_meta = f'<meta name="ficbook:pages" content="{escape(story.page_count, quote=True)}"/>' if story.page_count else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package version="3.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{escape(story.title)}</dc:title><dc:creator>{escape(story.author)}</dc:creator>
<dc:language>{escape(_normalize_language(story.language))}</dc:language><dc:source>{escape(story.source_url)}</dc:source>
{subjects}{description}{dates}{cover_meta}{pages_meta}<meta property="dcterms:modified">{modified}</meta></metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="styles/book.css" media-type="text/css"/>{cover_item}{items}</manifest>
<spine toc="ncx">{spine}</spine></package>"""


def _ncx(story: Story, book_id: str, pages: list[dict[str, str]]) -> str:
    nav = "".join(
        f'<navPoint id="nav-{i}" playOrder="{i}"><navLabel><text>{escape(p["title"])}</text></navLabel>'
        f'<content src="{p["href"]}"/></navPoint>'
        for i, p in enumerate(pages, 1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/></head><docTitle><text>{escape(story.title)}</text></docTitle>
<navMap>{nav}</navMap></ncx>"""


def _nav(story: Story, pages: list[dict[str, str]]) -> str:
    links = "".join(f'<li><a href="{p["href"]}">{escape(p["title"])}</a></li>' for p in pages)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ru"><head><title>Оглавление</title></head>
<body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>{escape(story.title)}</h1><ol>{links}</ol></nav></body></html>"""


def _normalize_language(language: str) -> str:
    value = language.strip().lower()
    return "ru" if value in {"russian", "ru", "русский"} else value or "ru"


def _is_note_heading(node: Tag) -> bool:
    text = _normalize_text(node.get_text(" ", strip=True)).rstrip(":").lower()
    return text in {"примечания", "примечание автора", "примечания автора"}


def _description_xml(html: str) -> str:
    if not html.strip():
        return ""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return f"<dc:description>{escape(_normalize_text(text))}</dc:description>" if text else ""


def _date_xml(kind: str, value: str) -> str:
    text = value.strip()
    return f"<dc:date>{escape(text)}</dc:date>" if text else ""


def _cover_href(story: Story) -> str:
    extension = story.cover.extension if story.cover else "jpg"
    return f"images/cover.{extension}"


def _cover_archive_path(story: Story) -> str:
    return f"OEBPS/{_cover_href(story)}"
