from __future__ import annotations

from html import escape
import re
from uuid import uuid5, NAMESPACE_URL

from bs4 import BeautifulSoup, NavigableString, Tag

from src.models import Chapter, Story

INLINE_TAGS = {"strong": "strong", "b": "strong", "em": "emphasis", "i": "emphasis"}
BLOCK_TAGS = {"article", "blockquote", "div", "li", "ol", "p", "section", "ul"}
SKIP_TAGS = {"audio", "iframe", "img", "script", "style", "video"}


def build_fb2(story: Story) -> bytes:
    annotation = _annotation_xml(story.annotation_html or story.description)
    chapters = "".join(_chapter_to_xml(chapter) for chapter in story.chapters)
    genres = "".join(f"<genre>{escape(item)}</genre>" for item in story.genres)
    language = _normalize_language(story.language)
    fb2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info>{genres}<author><nickname>{escape(story.author)}</nickname></author>
  <book-title>{escape(story.title)}</book-title>{annotation}<lang>{escape(language)}</lang></title-info>
  <document-info><author><nickname>ficbook_bot</nickname></author><id>{uuid5(NAMESPACE_URL, story.source_url)}</id>
  <program-used>ficbook_bot</program-used><src-url>{escape(story.source_url)}</src-url></document-info></description>
  <body>{chapters}</body></FictionBook>"""
    return fb2.encode("utf-8")


def _chapter_to_xml(chapter: Chapter) -> str:
    paragraphs = "".join(_fragment_to_blocks(chapter.html))
    return f"<section><title><p>{escape(chapter.title)}</p></title>{paragraphs or '<empty-line/>'}</section>"


def _annotation_xml(html: str) -> str:
    blocks = "".join(_fragment_to_blocks(html))
    return f"<annotation>{blocks}</annotation>" if blocks else ""


def _fragment_to_blocks(html: str) -> list[str]:
    if not html.strip():
        return []
    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup
    return _blocks_from_container(root)


def _blocks_from_container(container: Tag) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for child in container.children:
        _consume_node(child, blocks, current)
    _flush_paragraph(blocks, current)
    return blocks


def _consume_node(node: Tag | NavigableString, blocks: list[str], current: list[str]) -> None:
    if isinstance(node, NavigableString):
        _consume_text(str(node), blocks, current)
        return
    if node.name in SKIP_TAGS:
        return
    if node.name == "empty-line-marker":
        _flush_paragraph(blocks, current)
        if not blocks or blocks[-1] != "<empty-line/>":
            blocks.append("<empty-line/>")
        return
    if node.name == "br":
        _flush_paragraph(blocks, current)
        return
    if node.name in BLOCK_TAGS:
        _flush_paragraph(blocks, current)
        blocks.extend(_blocks_from_container(node))
        return
    inline = _render_inline(node)
    if inline:
        current.append(inline)


def _consume_text(text: str, blocks: list[str], current: list[str]) -> None:
    parts = re.split(r"(?:\r?\n\s*){2,}", text.replace("\xa0", " "))
    for index, part in enumerate(parts):
        normalized = _normalize_text(part)
        if normalized:
            current.append(escape(normalized))
        if index < len(parts) - 1:
            _flush_paragraph(blocks, current)


def _flush_paragraph(blocks: list[str], current: list[str]) -> None:
    text = _normalize_inline("".join(current))
    if text:
        blocks.append(f"<p>{text}</p>")
    current.clear()


def _render_inline(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return escape(_normalize_text(str(node)))
    if node.name in SKIP_TAGS or node.name == "br":
        return ""
    content = "".join(_render_inline(child) for child in node.children)
    if node.name == "a" and not content.strip():
        return escape(node.get("href", ""))
    mapped = INLINE_TAGS.get(node.name)
    return f"<{mapped}>{content}</{mapped}>" if mapped and content.strip() else content


def _normalize_inline(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("\xa0", " "))
    text = re.sub(r">\s+<", "><", text)
    return text.strip()


def _normalize_language(language: str) -> str:
    value = language.strip().lower()
    return "ru" if value in {"russian", "ru", "русский"} else language


def _normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", text)
