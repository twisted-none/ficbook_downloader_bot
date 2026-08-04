from __future__ import annotations

from html import unescape
import re

from bs4 import BeautifulSoup, NavigableString, Tag

DEFAULT_BLOCK_TAGS = {"article", "blockquote", "div", "li", "ol", "p", "section", "ul"}
DEFAULT_SKIP_TAGS = {"audio", "iframe", "img", "script", "style", "video"}


def html_to_text(html: str) -> str:
    return "\n\n".join(html_to_text_blocks(html))


def html_to_text_blocks(
    html: str,
    *,
    block_tags: set[str] = DEFAULT_BLOCK_TAGS,
    skip_tags: set[str] = DEFAULT_SKIP_TAGS,
) -> list[str]:
    if not html.strip():
        return []
    soup = BeautifulSoup(html, "lxml")
    return text_blocks_from_container(soup.body or soup, block_tags=block_tags, skip_tags=skip_tags)


def text_blocks_from_container(
    container: Tag,
    *,
    block_tags: set[str] = DEFAULT_BLOCK_TAGS,
    skip_tags: set[str] = DEFAULT_SKIP_TAGS,
) -> list[str]:
    blocks: list[str] = []
    text_nodes: list[str] = []
    for child in container.children:
        if isinstance(child, NavigableString):
            text_nodes.append(str(child))
            continue
        if child.name in skip_tags:
            continue
        if child.name == "br":
            _flush_text(blocks, text_nodes)
            continue
        if child.name in block_tags:
            _flush_text(blocks, text_nodes)
            blocks.extend(
                text_blocks_from_container(child, block_tags=block_tags, skip_tags=skip_tags)
                or [tag_text(child)]
            )
            continue
        text_nodes.append(child.get_text(" ", strip=True))
    _flush_text(blocks, text_nodes)
    return [block for block in blocks if block]


def tag_text(node: Tag) -> str:
    return normalize_text(node.get_text(" ", strip=True))


def normalize_text(text: str) -> str:
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_inline_text(text: str) -> str:
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def _flush_text(blocks: list[str], text_nodes: list[str]) -> None:
    text = normalize_text(" ".join(text_nodes))
    if text:
        blocks.append(text)
    text_nodes.clear()
