from __future__ import annotations

from base64 import b64encode
from html import escape
import re
from uuid import uuid5, NAMESPACE_URL

from bs4 import BeautifulSoup, NavigableString, Tag

from src.core.models import Chapter, Story
from src.exporters.navigation import chapter_id, prepare_chapters
from src.exporters.xml_text import xml_safe_story

INLINE_TAGS = {"strong": "strong", "b": "strong", "em": "emphasis", "i": "emphasis"}
BLOCK_TAGS = {"article", "blockquote", "div", "li", "ol", "p", "section", "ul"}
SKIP_TAGS = {"audio", "iframe", "img", "script", "style", "video"}


def build_fb2(story: Story) -> bytes:
    story = xml_safe_story(story)
    annotation = (
        _annotation_xml(story.description)
        if story.annotation_complete else
        _annotation_xml(story.annotation_html or story.description, story.page_count, story.word_count)
    )
    translator = f"<translator><nickname>{escape(story.translator)}</nickname></translator>" if story.translator else ""
    info = _info_section_xml(story)
    prepared = prepare_chapters(story)
    notes: list[str] = []
    chapters = "".join(_chapter_to_xml(chapter, index, notes) for index, chapter in enumerate(prepared, 1))
    notes_body = '<body name="notes">' + ''.join(notes) + '</body>' if notes else ''
    toc = '<section><title><p>Оглавление</p></title>' + ''.join(
        f'<p><a l:href="#{chapter_id(index)}">{escape(chapter.title or f"Глава {index}")}</a></p>'
        for index, chapter in enumerate(prepared, 1)
    ) + '</section>' if prepared else ''
    frontmatter = toc + info if story.opening_page == 'contents' else info + toc
    genres = "".join(f"<genre>{escape(item)}</genre>" for item in story.genres)
    keywords = _keywords_xml(story)
    language = _normalize_language(story.language)
    coverpage = _coverpage_xml(story)
    binary = _cover_binary_xml(story)
    custom_pages = (
        f'<custom-info info-type="ficbook-pages">{escape(story.page_count)}</custom-info>'
        if story.page_count
        else ""
    )
    fb2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
  <description><title-info>{genres}<author><nickname>{escape(story.author)}</nickname></author>
  <book-title>{escape(story.title)}</book-title>{annotation}{keywords}{coverpage}<lang>{escape(language)}</lang>{translator}</title-info>
  <document-info><author><nickname>ficbook_bot</nickname></author><id>{uuid5(NAMESPACE_URL, story.source_url)}</id>
  <program-used>ficbook_bot</program-used><src-url>{escape(story.source_url)}</src-url></document-info>{custom_pages}</description>
  <body>{frontmatter}{chapters}</body>{notes_body}{binary}</FictionBook>"""
    return fb2.encode("utf-8")


def _chapter_to_xml(chapter: Chapter, index: int, notes: list[str]) -> str:
    soup = BeautifulSoup(chapter.html, 'lxml')
    references = {str(link['href'])[1:] for link in soup.select('a[href^="#"]')}
    note_ids: set[str] = set()
    for container in soup.select('.interactive-footnotes'):
        for node in container.find_all('div', id=True, recursive=False):
            note_id = str(node['id'])
            if note_id not in references or not node.get_text(strip=True):
                continue
            # The section itself is the link target and contains the note text.
            blocks = ''.join(_fragment_to_blocks(node.decode_contents()))
            if blocks:
                notes.append(f'<section id="{escape(note_id, quote=True)}">{blocks}</section>')
                note_ids.add(note_id)
                node.decompose()
        remaining = container.find_all(recursive=False)
        if (note_ids and len(remaining) == 1
                and container.get_text(strip=True) == 'Интерактивные примечания:'):
            container.decompose()
    paragraphs = "".join(_soup_to_blocks(soup, note_ids))
    return f'<section id="{chapter_id(index)}"><title><p>{escape(chapter.title or f"Глава {index}")}</p></title>{paragraphs or "<empty-line/>"}</section>'


def _info_section_xml(story: Story) -> str:
    heading = 'Информация' if story.opening_page == 'default' else 'Аннотация'
    if story.annotation_complete:
        paragraphs = "".join(_fragment_to_blocks(story.annotation_html or story.description))
        return f"<section><title><p>{heading}</p></title>{paragraphs}</section>"
    rows = [
        ("Автор", story.author),
        ("Страниц", story.page_count),
        ("Слов", story.word_count),
        ("Пейринг и персонажи", ", ".join(story.pairings)),
        ("Ссылка", story.source_url),
    ]
    paragraphs = "".join(f"<p>{escape(label)}: {escape(value)}</p>" for label, value in rows if value)
    if story.opening_page != 'default':
        paragraphs += ''.join(_fragment_to_blocks(story.annotation_html or story.description))
    if not paragraphs:
        return ""
    return f"<section><title><p>{heading}</p></title>{paragraphs}</section>"


def _keywords_xml(story: Story) -> str:
    values = [*story.fandoms, *story.genres, *story.pairings]
    return f"<keywords>{escape(', '.join(values))}</keywords>" if values else ""


def _annotation_xml(html: str, page_count: str = "", word_count: str = "") -> str:
    meta = []
    if page_count:
        meta.append(f"<p>Страниц: {escape(page_count)}</p>")
    if word_count:
        meta.append(f"<p>Слов: {escape(word_count)}</p>")
    blocks = "".join(_fragment_to_blocks(html))
    blocks = "".join(meta) + blocks
    return f"<annotation>{blocks}</annotation>" if blocks else ""


def _coverpage_xml(story: Story) -> str:
    if not story.cover:
        return ""
    return f'<coverpage><image l:href="#{_cover_id(story)}"/></coverpage>'


def _cover_binary_xml(story: Story) -> str:
    if not story.cover:
        return ""
    payload = b64encode(story.cover.data).decode("ascii")
    return f'<binary id="{_cover_id(story)}" content-type="{escape(story.cover.mime)}">{payload}</binary>'


def _cover_id(story: Story) -> str:
    extension = story.cover.extension if story.cover else "jpg"
    return f"cover.{extension}"


def _fragment_to_blocks(html: str) -> list[str]:
    if not html.strip():
        return []
    return _soup_to_blocks(BeautifulSoup(html, "lxml"), set())


def _soup_to_blocks(soup: BeautifulSoup, note_ids: set[str]) -> list[str]:
    root = soup.body or soup
    for node in list(root.select('[id]')):
        anchor = soup.new_tag('fb2-anchor', id=str(node['id']))
        block = node if node.name in BLOCK_TAGS else node.find_parent(list(BLOCK_TAGS))
        (block or node).insert_before(anchor)
    return _blocks_from_container(root, note_ids)


def _blocks_from_container(container: Tag, note_ids: set[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for child in container.children:
        _consume_node(child, blocks, current, note_ids)
    _flush_paragraph(blocks, current)
    return blocks


def _consume_node(node: Tag | NavigableString, blocks: list[str], current: list[str], note_ids: set[str]) -> None:
    if isinstance(node, NavigableString):
        _consume_text(str(node), blocks, current)
        return
    if node.name in SKIP_TAGS:
        return
    if node.name == 'fb2-anchor':
        _flush_paragraph(blocks, current)
        blocks.append(f'<p id="{escape(str(node["id"]), quote=True)}"></p>')
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
        blocks.extend(_blocks_from_container(node, note_ids))
        return
    inline = _render_inline(node, note_ids)
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


def _render_inline(node: Tag | NavigableString, note_ids: set[str]) -> str:
    if isinstance(node, NavigableString):
        return escape(_normalize_text(str(node)))
    if node.name in SKIP_TAGS or node.name == "br":
        return ""
    content = "".join(_render_inline(child, note_ids) for child in node.children)
    if node.name == 'a' and content.strip():
        href = str(node.get('href') or '')
        if href.startswith(('#', 'http://', 'https://')):
            note_type = ' type="note"' if href.startswith('#') and href[1:] in note_ids else ''
            return f'<a l:href="{escape(href, quote=True)}"{note_type}>{content}</a>'
    if node.name in {'sup', 'sub'} and content:
        return f'<{node.name}>{content}</{node.name}>'
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
