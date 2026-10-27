"""Anonymous AO3-only recovery through the isolated browser worker."""

import json
import logging
import os
import re
from html import escape
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bs4 import BeautifulSoup
from fanficfare.story import langs

from src.core.limits import (ResourceLimitError, check_chapter_count, check_deadline,
                             current_limits, read_bounded, request_timeout)
from src.core.models import Chapter, ChapterPreview, Story, StoryPreview
from src.core.progress import report_title
from src.sources.ao3 import parse_download, require_available_work
from src.sources.safe_http import require_https_url

logger = logging.getLogger(__name__)
HOSTS = frozenset({'archiveofourown.org', 'www.archiveofourown.org', 'download.archiveofourown.org'})


class BrowserDownloadError(Exception):
    pass


def work_id(url: str) -> str:
    parts = require_https_url(url.split('#', 1)[0], HOSTS)
    match = re.fullmatch(r'/works/([1-9][0-9]{0,15})(?:/chapters/[1-9][0-9]*)?/?', parts.path)
    if match is None:
        raise BrowserDownloadError('Некорректная ссылка AO3.')
    return match[1]


def eligible(error: Exception) -> bool:
    from src.sources.ao3_http import _browser_failure, is_transient_ao3_error
    # Inspect the original cause, not friendly text or user-provided story text.
    seen = set()
    while error.__cause__ is not None and id(error) not in seen:
        seen.add(id(error))
        error = error.__cause__
    if any('HTTP ' + str(code) in failure for failure in getattr(error, 'ao3_failures', ())
           for code in (401, 404, 429, 526)):
        return False
    return not isinstance(error, ResourceLimitError) and (is_transient_ao3_error(error) or _browser_failure(error))


def enabled_for(url: str, error: Exception) -> bool:
    if not os.getenv('AO3_BROWSER_URL') or not eligible(error):
        return False
    try:
        work_id(url)
        return True
    except Exception:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BrowserDownloadError('Браузерный сервис вернул неожиданный ответ.')


def fetch_bundle(url: str) -> dict:
    endpoint = os.getenv('AO3_BROWSER_URL', '').rstrip('/')
    # This is a trusted internal service, never an arbitrary user URL/proxy.
    if endpoint not in {'http://ao3-browser:8000', 'http://127.0.0.1:18080'}:
        raise BrowserDownloadError('Резервный способ AO3 не настроен.')
    request = Request(f'{endpoint}/works/{work_id(url)}', headers={'Accept': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=request_timeout(175)) as response:
            data = json.loads(read_bounded(response))
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 503:
            raise BrowserDownloadError('Резервный загрузчик AO3 занят. Попробуй немного позже.') from None
        raise BrowserDownloadError('AO3 не отдал работу через резервный загрузчик. Попробуй позже.') from None
    if (not isinstance(data, dict) or data.get('work_id') != work_id(url)
            or not isinstance(data.get('page'), str) or not isinstance(data.get('download'), str)):
        raise BrowserDownloadError('Некорректный ответ резервного загрузчика AO3.')
    check_deadline()
    return data


def _clean(fragment: str, origin: str) -> str:
    soup = BeautifulSoup(fragment, 'lxml')
    for node in list(soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'form', 'input',
                                   'button', 'link', 'meta', 'base', 'img', 'video', 'audio', 'svg'])):
        node.decompose()
    for node in soup.find_all(True):
        node.attrs = {key: value for key, value in node.attrs.items() if key in {'id', 'name', 'href', 'title', 'class'}}
        if node.has_attr('href'):
            href = str(node['href']).strip()
            if href.startswith('#'):
                continue
            try:
                target = urljoin(origin, href)
                parts = urlsplit(target)
                if (parts.scheme not in {'http', 'https'} or parts.username is not None or parts.password is not None
                        or any(ord(char) < 33 for char in href)):
                    raise ValueError()
                node['href'] = target
            except ValueError:
                del node['href']
    return ''.join(str(node) for node in (soup.body or soup).contents)


def parse_browser_work(url: str, bundle: dict, selected: set[int] | frozenset[int] | None) -> Story:
    origin = f'https://archiveofourown.org/works/{work_id(url)}'
    page = bundle['page']
    if len(page.encode()) + len(bundle['download'].encode()) > current_limits().max_response_bytes:
        raise ResourceLimitError()
    require_available_work(page.encode())
    soup = BeautifulSoup(page, 'lxml')
    root = soup.select_one('#workskin')
    title_node = root.select_one('.preface h2.title') if root else None
    count = soup.select_one('dd.chapters')
    match = re.fullmatch(r'([\d,]+)/([\d,]+|\?)', count.get_text(strip=True)) if count else None
    if not root or not title_node or not match:
        raise BrowserDownloadError('AO3 не предоставил открытую страницу работы с числом глав.')
    expected = int(match[1].replace(',', ''))
    check_chapter_count(expected)
    if expected < 1 or selected is not None and (not selected or any(n < 1 or n > expected for n in selected)):
        raise BrowserDownloadError('Выбранные главы отсутствуют в работе AO3.')
    title = title_node.get_text(' ', strip=True)
    authors = [a.get_text(' ', strip=True) for a in root.select('.preface h3.byline a[rel="author"]')]
    author = ', '.join(dict.fromkeys(authors)) or 'Anonymous'
    summary = root.select_one('.preface .summary .userstuff')
    description = _clean(str(summary), origin) if summary else ''
    chapters = []
    if bundle['download']:
        downloaded = BeautifulSoup(bundle['download'], 'lxml')
        bodies = downloaded.select('#chapters > div.userstuff')
        if len(bodies) != expected or any(not body.get_text(' ', strip=True) for body in bodies):
            raise BrowserDownloadError('AO3 вернул HTML-файл с отсутствующим текстом глав.')
        headings = downloaded.select('#chapters > .meta.group h2.heading')
        links = [{'title': heading.get_text(' ', strip=True), 'url': f'{origin}#chapter-{index}'}
                 for index, heading in enumerate(headings, 1)]
        if not links and expected == 1:
            links = [{'title': title, 'url': origin}]
        if len(links) != expected:
            raise BrowserDownloadError('AO3 вернул неполный HTML-файл: число глав не совпадает.')
        wanted = [(i, link) for i, link in enumerate(links, 1) if selected is None or i in selected]
        try:
            chapters = parse_download(_clean(bundle['download'], origin), links, wanted, description)
        except ValueError as error:
            raise BrowserDownloadError('AO3 вернул неполный HTML-файл работы.') from error
    else:
        contents = root.select_one('#chapters')
        groups = contents.select(':scope > .chapter') if contents else []
        if not groups and contents and expected == 1:
            groups = [contents]
        if len(groups) != expected:
            raise BrowserDownloadError('AO3 вернул неполную работу: число глав не совпадает.')
        for index, group in enumerate(groups, 1):
            bodies = group.select(':scope > .userstuff')
            if len(bodies) != 1 or not bodies[0].get_text(' ', strip=True):
                raise BrowserDownloadError(f'AO3 не предоставил текст главы {index}.')
            if selected is not None and index not in selected:
                continue
            heading = group.select_one('h3.title')
            fragment = BeautifulSoup(str(group), 'lxml')
            for node in fragment.select('.preface h3.title, h3.landmark'):
                node.decompose()
            chapters.append(Chapter.from_download(heading.get_text(' ', strip=True) if heading else title,
                                                  _clean(str(fragment), origin), f'{origin}#chapter-{index}'))
        notes = root.select_one(':scope > .preface > .notes > .userstuff')
        if notes and chapters:
            chapters[0].html = _clean(str(notes), origin) + chapters[0].html
        endnotes = root.select_one('#work_endnotes')
        if endnotes and chapters:
            chapters[-1].html += _clean(str(endnotes), origin)
    if not chapters or any(not BeautifulSoup(c.html, 'lxml').get_text(' ', strip=True) for c in chapters):
        raise BrowserDownloadError('AO3 вернул пустой текст работы.')
    def value(selector):
        node = soup.select_one(selector)
        return node.get_text(' ', strip=True) if node else ''
    def tags(selector):
        return [node.get_text(' ', strip=True) for node in soup.select(selector + ' a.tag')]
    published, updated = value('dd.published'), value('dd.status')
    language = langs.get(value('dd.language'), 'en')
    return Story(title=title, author=author, source_url=origin, chapters=chapters, description=description,
                 annotation_html=f'<p><b>{escape(title)}</b></p><p>Автор: {escape(author)}</p>' + description,
                 language=language, published=published, updated=updated, start_date=published,
                 status='завершён' if match[2] != '?' and match[1] == match[2] else 'в процессе',
                 fandoms=tags('dd.fandom'), genres=tags('dd.freeform'), pairings=tags('dd.relationship'),
                 rating=value('dd.rating'), word_count=value('dd.words'))


def download_browser_work(url, progress=None, chapter_numbers=None, *, preview=False):
    if progress:
        progress('Пробую резервный способ скачивания AO3 через браузер')
    bundle = fetch_bundle(url)
    story = parse_browser_work(url, bundle, chapter_numbers if not preview else None)
    report_title(progress, story.title)
    logger.info('AO3 browser recovered work %s with %s chapters via %s', work_id(url), len(story.chapters),
                'Download' if bundle['download'] else 'full-work page')
    if preview:
        return StoryPreview(story.title, story.author, story.source_url,
                            [ChapterPreview(i, chapter.title) for i, chapter in enumerate(story.chapters, 1)])
    if progress:
        progress(f'Скачиваю главы: {len(story.chapters)}/{len(story.chapters)}')
    return story
