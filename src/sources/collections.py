from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import heapq
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import Request

from bs4 import BeautifulSoup

from src.core.limits import (ResourceLimitError, bounded_operation, bounded_sleep,
                             check_deadline, current_limits, read_bounded, request_timeout)
from src.sources.registry import FICBOOK_ID_PATTERN, normalize_collection_url
from src.sources.collection_http import BrowserCollectionClient
from src.sources.safe_http import (SafeRedirectHandler, build_safe_opener,
                                   is_cloudflare_challenge, require_https_url)


class CollectionError(Exception):
    """A public collection could not be read completely."""


@dataclass(frozen=True, slots=True)
class FicbookCollection:
    title: str
    source_url: str
    urls: tuple[str, ...]


_HOSTS = frozenset({'ficbook.net', 'www.ficbook.net'})
logger = logging.getLogger(__name__)
_PAGE_KEYS = {'p', 'page', 'page_num', 'page_number', 'offset'}
_WORK_PATH = re.compile(rf'/readfic/({FICBOOK_ID_PATTERN})(?:/[0-9]+)?/?', re.I)
_PAGINATION = ('.pagination a[href], .page-pagination a[href], .paginator a[href], '
               '.pagination-holder a[href], .pagination-wrapper a[href], '
               '.pager a[href], a[rel~="next"][href], link[rel~="next"][href]')


def _page_url(url: str, base: str) -> tuple[int, str]:
    require_https_url(url, _HOSTS)
    if normalize_collection_url(url) != base:
        raise ValueError('Pagination left the collection')
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    path_page = re.search(r'/(?:page|p)/([0-9]{1,9})/?$', parts.path)
    if path_page:
        if query:
            raise ValueError('Ambiguous pagination')
        number = int(path_page.group(1))
        target = base + parts.path[len(urlsplit(base).path):].rstrip('/')
    elif query:
        if len(query) != 1 or query[0][0] not in _PAGE_KEYS or not re.fullmatch('[0-9]{1,9}', query[0][1]):
            raise ValueError('Unknown pagination')
        key, value = query[0]
        number = int(value) + (1 if key == 'offset' else 0)
        target = base + '?' + urlencode({key: int(value)})
    else:
        number, target = 1, base
    if number < 1:
        raise ValueError('Invalid page number')
    return number, base if number == 1 else target


class _CollectionRedirectHandler(SafeRedirectHandler):
    def __init__(self, base: str) -> None:
        super().__init__(_HOSTS)
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _page_url(newurl, self.base)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextmanager
def _collection_reader(base: str):
    opener = build_safe_opener(_HOSTS, redirect_handler=_CollectionRedirectHandler(base))
    browser: BrowserCollectionClient | None = None

    def fetch(url: str) -> tuple[bytes, str]:
        nonlocal browser
        if browser is not None:
            return browser.fetch(url)
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        })
        try:
            with opener.open(request, timeout=request_timeout()) as response:
                if not is_cloudflare_challenge(response.headers):
                    return read_bounded(response), response.geturl()
        except HTTPError as exc:
            if not is_cloudflare_challenge(exc.headers):
                raise
            exc.close()
        logger.info('Cloudflare challenged a public collection; switching to anonymous browser transport')
        browser = BrowserCollectionClient(lambda target: _page_url(target, base))
        return browser.fetch(url)

    try:
        yield fetch
    finally:
        if browser is not None:
            browser.close()


def _parse_page(html: str, url: str, base: str) -> tuple[str, list[str], list[tuple[int, str]]]:
    if not re.search(r'</(?:html|body)\s*>', html, re.I):
        raise CollectionError('Ficbook вернул неполную страницу сборника. Попробуй позже.')
    soup = BeautifulSoup(html, 'lxml')
    pages = []
    for anchor in soup.select(_PAGINATION):
        try:
            target = urljoin(url, anchor['href'])
            require_https_url(target, _HOSTS)
            if normalize_collection_url(target) != base:
                continue
        except (ValueError, URLError):
            continue
        try:
            pages.append(_page_url(target, base))
        except ValueError as exc:
            params = dict(parse_qsl(urlsplit(target).query))
            if _PAGE_KEYS.intersection(params) or 'next' in anchor.get('rel', []):
                raise CollectionError('Не удалось разобрать все страницы сборника. Сайт изменил пагинацию.') from exc
    root = soup.select_one('main') or soup
    for unwanted in root.select('aside, header, footer, .recommendations, .recommended-fanfics'):
        unwanted.decompose()
    heading = root.select_one('h1')
    title = heading.get_text(' ', strip=True)[:200] if heading else 'Сборник Ficbook'
    text = root.get_text(' ', strip=True).lower()
    if re.search(r'закрытый сборник|сборник закрыт|сборник (?:является )?закрытым|доступ к сборнику ограничен', text):
        raise CollectionError('Этот сборник закрыт. Бот скачивает только публичные сборники Ficbook.')
    works = []
    for anchor in root.select('.fanfic-inline-title a[href], a.fanfic-inline-title[href], '
                              '.fanfic-block h3 a[href], article h3 a[href]'):
        try:
            target = urlsplit(urljoin(url, anchor['href']))._replace(fragment='').geturl()
            parts = require_https_url(target, _HOSTS)
            match = _WORK_PATH.fullmatch(parts.path)
            if match:
                works.append(f'https://ficbook.net/readfic/{match.group(1).lower()}')
        except (ValueError, URLError):
            continue
    if not works and not re.search(r'сборник пуст|в сборнике.{0,40}нет (?:фанфиков|работ|произведений)', text):
        raise CollectionError('Не удалось прочитать список работ сборника. Он закрыт, недоступен или сайт изменился.')
    return title, works, pages


@bounded_operation
def load_collection(
    url: str, progress: Callable[[str], None] | None = None, *,
    request_delay: float = 0.0, retry_attempts: int = 3, retry_base_delay: float = 8.0,
) -> FicbookCollection:
    try:
        base = normalize_collection_url(url)
    except ValueError as exc:
        raise CollectionError('Нужна ссылка на публичный сборник Ficbook.') from exc
    # Neither transport receives source accounts or authenticated cookies.
    with _collection_reader(base) as fetch:
        return _collect_pages(base, fetch, progress, request_delay, retry_attempts, retry_base_delay)


def _collect_pages(
    base: str, fetch: Callable[[str], tuple[bytes, str]], progress: Callable[[str], None] | None,
    request_delay: float, retry_attempts: int, retry_base_delay: float,
) -> FicbookCollection:
    limits = current_limits()
    pending = [(1, base)]
    discovered = {1}
    works: dict[str, tuple[int, int]] = {}
    title = 'Сборник Ficbook'
    while pending:
        check_deadline()
        number, page_url = heapq.heappop(pending)
        if progress:
            progress(f'Получаю сборник: страница {number}, найдено работ: {len(works)}')
        for attempt in range(max(1, retry_attempts)):
            try:
                if request_delay:
                    bounded_sleep(request_delay)
                payload, final_url = fetch(page_url)
                final_number, _ = _page_url(final_url, base)
                if final_number != number:
                    raise CollectionError('Ficbook перенаправил на другую страницу сборника. Попробуй позже.')
                html = payload.decode('utf-8', 'replace')
                break
            except HTTPError as exc:
                exc.close()
                if is_cloudflare_challenge(exc.headers):
                    raise CollectionError('Cloudflare не пропустил запрос к сборнику даже через резервный клиент. Попробуй позже.') from None
                if exc.code == 404:
                    raise CollectionError('Сборник не найден. Проверь ссылку на сборник Ficbook.') from None
                if exc.code == 401:
                    raise CollectionError('Для доступа к сборнику требуется вход. Бот скачивает только публичные сборники.') from None
                if exc.code == 403:
                    raise CollectionError('Ficbook запретил запрос к сборнику. Это может быть ограничение доступа или временная блокировка.') from None
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= retry_attempts:
                    raise CollectionError('Ficbook не отдал все страницы сборника. Попробуй позже.') from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 >= retry_attempts:
                    raise CollectionError('Не удалось получить все страницы сборника из-за ошибки соединения.') from exc
            except ValueError as exc:
                raise CollectionError('Ficbook перенаправил за пределы сборника. Нужна публичная ссылка.') from exc
            if attempt + 1 < retry_attempts:
                bounded_sleep(max(0.0, retry_base_delay) * (attempt + 1))
        page_title, page_works, next_pages = _parse_page(html, page_url, base)
        if number == 1:
            title = page_title
        for position, work in enumerate(page_works):
            order = (number, position)
            new_work = work not in works
            if work not in works or order < works[work]:
                works[work] = order
            if len(works) > limits.max_collection_works:
                raise ResourceLimitError(f'В сборнике больше {limits.max_collection_works} работ. Он превышает лимит пакетной загрузки.')
            if progress and new_work and len(works) % 5 == 0:
                progress(f'Получаю сборник: страница {number}, найдено работ: {len(works)}')
        if progress:
            progress(f'Получаю сборник: страница {number}, найдено работ: {len(works)}')
        for next_number, next_url in next_pages:
            if next_number not in discovered:
                if len(discovered) >= limits.max_collection_pages:
                    raise ResourceLimitError(f'В сборнике больше {limits.max_collection_pages} страниц. Он превышает лимит пакетной загрузки.')
                discovered.add(next_number)
                heapq.heappush(pending, (next_number, next_url))
    return FicbookCollection(title, base, tuple(sorted(works, key=works.__getitem__)))
