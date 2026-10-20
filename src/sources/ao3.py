from __future__ import annotations

from collections.abc import Callable
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from src.core.models import Chapter
from src.core.limits import ResourceLimitError, ResponseLimitError


logger = logging.getLogger(__name__)


class AO3WorkUnavailableError(Exception):
    pass


def require_available_work(html: bytes) -> None:
    marker = 'This work is part of an ongoing challenge and will be revealed soon!'
    if marker.encode() not in html:
        return
    soup = BeautifulSoup(html, 'lxml')
    for notice in soup.select('#main .notice'):
        if (notice.find_parent(id=re.compile(r'^(?:workskin|chapters)$')) is not None
                or notice.find_parent(class_='userstuff') is not None):
            continue
        if marker in notice.get_text(' ', strip=True):
            raise AO3WorkUnavailableError(
                'Эта работа AO3 скрыта в нераскрытой коллекции (Mystery Work). '
                'Сайт не предоставляет её текст. Скачать можно после раскрытия работы.'
            )


def _download_link(page: str, work_url: str) -> str | None:
    work = re.search(r'/works/(\d+)', work_url)
    if not work:
        return None
    soup = BeautifulSoup(page, 'lxml')
    for link in soup.select('li.download a[href]'):
        href = str(link['href']).strip()
        try:
            absolute = urljoin(work_url, href)
            parsed = urlsplit(absolute)
        except ValueError:
            continue
        if (parsed.scheme == 'https'
                and parsed.netloc.lower() in {'archiveofourown.org', 'download.archiveofourown.org'}
                and parsed.path.startswith(f'/downloads/{work.group(1)}/')
                and parsed.path.lower().endswith('.html')):
            return absolute
    return None


def _text(html: str) -> str:
    return ' '.join(BeautifulSoup(html, 'lxml').get_text(' ', strip=True).split())


def parse_download(
    html: str,
    chapter_links: list[dict[str, Any]],
    selected: list[tuple[int, dict[str, Any]]],
    description: str = '',
) -> list[Chapter]:
    soup = BeautifulSoup(html, 'lxml')
    root = soup.select_one('div#chapters')
    if root is None:
        raise ValueError('AO3 HTML download has no chapter container')
    groups: list[list[Tag]] = []
    for node in root.children:
        if not isinstance(node, Tag):
            continue
        classes = set(node.get('class', []))
        if {'meta', 'group'} <= classes and node.select_one('h2.heading'):
            groups.append([])
        if groups:
            groups[-1].append(node)
    if not groups and len(chapter_links) == 1:
        groups = [[node for node in root.children
                   if isinstance(node, Tag) and 'toc-heading' not in node.get('class', [])]]
    if len(groups) != len(chapter_links):
        raise ValueError(f'AO3 HTML chapter count mismatch: {len(groups)} / {len(chapter_links)}')
    for group in groups:
        bodies = [node for node in group if node.name == 'div' and 'userstuff' in node.get('class', [])]
        if len(bodies) != 1:
            raise ValueError('AO3 HTML download has missing or ambiguous chapter text')

    chapters = []
    for index, link in selected:
        group = groups[index - 1]
        parts = []
        for node in group:
            # Titles come from the metadata; keep summaries and notes in place.
            heading = node.select_one('h2.heading') if 'meta' in node.get('class', []) else None
            if heading:
                heading.decompose()
            parts.append(str(node))
        chapters.append(Chapter.from_download(str(link.get('title') or f'Глава {index}'), ''.join(parts), str(link['url'])))

    if chapters:
        notes = soup.select('#preface > .meta > blockquote.userstuff')
        if notes and _text(str(notes[0])) == _text(description):
            notes = notes[1:]
        if notes:
            opening = '<div class="work-notes"><p><b>Примечания:</b></p>' + ''.join(map(str, notes)) + '</div>'
            chapters[0].html = opening + chapters[0].html
        endnotes = soup.select_one('#afterword #endnotes')
        if endnotes:
            chapters[-1].html += str(endnotes)
    return chapters


def download_html_chapters(
    adapter: Any,
    work_url: str,
    chapter_links: list[dict[str, Any]],
    selected: list[tuple[int, dict[str, Any]]],
    description: str,
    progress: Callable[[str], None] | None,
    pause: Callable[[], None],
) -> list[Chapter] | None:
    try:
        page = adapter.get_request(adapter.url + adapter.addurl)
    except ResourceLimitError:
        raise
    except Exception as exc:
        logger.info('AO3 download menu unavailable (%s); using chapter loader', type(exc).__name__)
        return None
    link = _download_link(page, work_url)
    if link is None:
        logger.info('AO3 has no supported HTML download link; using chapter loader')
        return None
    if progress:
        progress('Скачиваю страницу фанфика: HTML-файл AO3 целиком')
    pause()
    try:
        html = adapter.get_request(link, usecache=False)
    except ResponseLimitError:
        logger.info('AO3 HTML export exceeds response limit; using chapter loader')
        return None
    except ResourceLimitError:
        raise
    except Exception as exc:
        logger.info('AO3 HTML download unavailable (%s); using chapter loader', type(exc).__name__)
        return None
    try:
        chapters = parse_download(html, chapter_links, selected, description)
    except ValueError as exc:
        logger.warning('%s; using AO3 chapter loader', exc)
        return None
    if progress:
        progress(f'Скачиваю главы: {len(chapters)}/{len(chapters)}')
    return chapters
