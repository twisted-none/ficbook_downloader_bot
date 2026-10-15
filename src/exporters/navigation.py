from __future__ import annotations

from collections import defaultdict
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from src.core.models import Chapter, Story


def chapter_id(index: int) -> str:
    return f"chapter-{index:04d}"


def _page_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ''))


def prepare_chapters(story: Story) -> list[Chapter]:
    """Resolve source anchors into unique, format-independent book destinations."""
    soups = [BeautifulSoup(chapter.html, 'lxml') for chapter in story.chapters]
    local: list[dict[str, str]] = []
    global_ids: dict[str, list[str]] = defaultdict(list)
    pages: dict[str, int] = {}
    targets: dict[str, Tag] = {}
    for index, (chapter, soup) in enumerate(zip(story.chapters, soups), 1):
        root = soup.body or soup
        for node in root.select('script, style, iframe, audio, video, img'):
            node.decompose()
        mapping: dict[str, str] = {}
        for node in (soup.html, soup.body):
            if node and node.get('id'):
                alias = str(node['id'])
                mapping[alias] = chapter_id(index)
                global_ids[alias].append(chapter_id(index))
        for number, node in enumerate(root.select('[id], a[name]'), 1):
            aliases = [str(node.get(attr) or '') for attr in ('id', 'name')]
            destination = f"c{index:04d}-a{number:04d}"
            node['id'] = destination
            node.attrs.pop('name', None)
            targets[destination] = node
            for alias in set(filter(None, aliases)):
                mapping.setdefault(alias, destination)
                global_ids[alias].append(destination)
        local.append(mapping)
        if chapter.source_url:
            pages[_page_url(chapter.source_url)] = index

    resolved: list[tuple[Tag, Tag, str]] = []
    for index, (chapter, soup) in enumerate(zip(story.chapters, soups), 1):
        base = chapter.source_url or story.source_url
        for number, link in enumerate(soup.select('a[href]'), 1):
            href = str(link['href']).strip()
            link.attrs.pop('target', None)
            try:
                parsed = urlsplit(href)
            except ValueError:
                link.attrs.pop('href', None)
                continue
            fragment = unquote(parsed.fragment)
            destination = ''
            if href.startswith('#'):
                destination = local[index - 1].get(fragment, '')
                if not destination and len(global_ids.get(fragment, [])) == 1:
                    destination = global_ids[fragment][0]
            elif not parsed.scheme or parsed.scheme.lower() in {'http', 'https'}:
                absolute = urljoin(base, href)
                page = _page_url(absolute)
                target_index = pages.get(page)
                if target_index:
                    destination = (local[target_index - 1].get(fragment, '')
                                   if fragment else chapter_id(target_index))
                elif fragment:
                    parsed_absolute = urlsplit(absolute)
                    same_site = parsed_absolute.netloc.lower() == urlsplit(base).netloc.lower()
                    if same_site and (parsed_absolute.path in {'', '/'} or page == _page_url(story.source_url)):
                        destination = local[index - 1].get(fragment, '')
                        if not destination and len(global_ids.get(fragment, [])) == 1:
                            destination = global_ids[fragment][0]
                if not destination:
                    link['href'] = absolute
            else:
                link.attrs.pop('href', None)
                continue
            if destination:
                link['href'] = '#' + destination
                if destination in targets:
                    reference_id = str(link.get('id') or f"c{index:04d}-r{number:04d}")
                    link['id'] = reference_id
                    resolved.append((link, targets[destination], reference_id))
            elif href.startswith('#') or not href:
                link.attrs.pop('href', None)

    # Keep existing return links; add ordinary links for notes without one.
    backlinks: dict[str, list[Tag]] = defaultdict(list)
    for link, target, reference_id in resolved:
        if target.name == 'a' and target.has_attr('href'):
            continue
        container = target
        if target.name == 'a' and not target.get_text(strip=True) and isinstance(target.parent, Tag):
            container = target.parent
        if container.find('a', href='#' + reference_id):
            continue
        source_ids = [str(node.get('id')) for node in [link, *link.parents]
                      if isinstance(node, Tag) and node.get('id')]
        if any(container.find('a', href='#' + value) for value in source_ids):
            continue
        backlink = soups[0].new_tag('a', href='#' + reference_id)
        backlink['class'] = 'note-backlink'
        backlink.string = ' [Назад]'
        container.append(backlink)
        backlinks[str(target['id'])].append(backlink)

    for links in backlinks.values():
        if len(links) > 1:
            for index, backlink in enumerate(links, 1):
                backlink.string = f' [Назад {index}]'

    return [
        Chapter(chapter.title, (soup.body or soup).decode_contents(), chapter.source_url)
        for chapter, soup in zip(story.chapters, soups)
    ]
