from __future__ import annotations

from collections.abc import Callable
from html import escape
from io import BytesIO
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from bs4 import BeautifulSoup
from PIL import Image

from src.core.limits import (ResourceLimitError, bounded_operation, bounded_sleep, check_chapter_count,
                             check_deadline, current_limits, image_is_safe, read_bounded,
                             validate_preview, validate_story)
from src.core.models import Chapter, ChapterPreview, CoverImage, Story, StoryPreview
from src.core.progress import report_title
from src.sources.registry import ZAHLEB_HOSTS, ZAHLEB_PATH, normalize_zahleb_url
from src.sources.safe_http import build_safe_opener, require_https_url
from src.core.retry_logging import public_url

logger = logging.getLogger(__name__)

_ID = re.compile(r'[A-Za-z0-9]{10}')
_COVER_HOSTS = ZAHLEB_HOSTS | {'files.zahleb.me', 'images.zahleb.me'}


class ZahlebError(Exception):
    pass


class ZahlebNotFoundError(ZahlebError):
    pass


class ZahlebRateLimitError(ZahlebError):
    pass


class ZahlebAccessError(ZahlebError):
    pass


def _string(value: object) -> str:
    if not isinstance(value, str):
        return ''
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ZahlebError('Взахлёб вернул повреждённый текст.') from None
    return value.strip()


def _labels(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(_string(item.get('title')) for item in value
                              if isinstance(item, dict) and _string(item.get('title'))))


def _check_access(data: dict) -> None:
    if (data.get('isFree') is False or any(data.get(key) is True for key in
            ('isLocked', 'requiresSubscription', 'isPremium', 'isPaid', 'isPreview'))):
        raise ZahlebAccessError('Глава или история Взахлёб закрыта либо требует подписку. '
                                'Бот скачивает только общедоступный текст.')


class _RichText:
    """Render the site's JSON text, never treating source text or attributes as HTML."""

    def __init__(self):
        self.nodes = 0
        self.text_units = 0
        self.json_text_units = 0
        self.text_bytes = 0
        self.has_text = False

    def node(self, value: object, depth: int = 0, *, block: bool = False) -> str:
        check_deadline()
        self.nodes += 1
        if depth > 32 or self.nodes > 50000:
            raise ResourceLimitError('Структура текста Взахлёб слишком сложная.')
        if not isinstance(value, dict):
            raise ZahlebError('Взахлёб вернул неизвестный формат текста главы.')
        text = value.get('text', '')
        if not isinstance(text, str):
            raise ZahlebError('Взахлёб вернул повреждённый текст главы.')
        try:
            self.text_units += len(text.encode('utf-16-le')) // 2
            self.json_text_units += len(json.dumps(text, ensure_ascii=False).encode('utf-16-le')) // 2
        except UnicodeError:
            raise ZahlebError('Взахлёб вернул повреждённый текст главы.') from None
        self.has_text = self.has_text or bool(text.strip())
        safe = escape(text)
        self.text_bytes += len(safe.encode('utf-8'))
        if self.text_bytes > current_limits().max_story_bytes:
            raise ResourceLimitError()
        html = safe.replace('\n', '<br/>')
        children = value.get('children', [])
        if not isinstance(children, list):
            raise ZahlebError('Взахлёб вернул повреждённую структуру главы.')
        html += ''.join(self.node(child, depth + 1) for child in children)
        for flag, tag in (('bold', 'strong'), ('italic', 'em'), ('underline', 'u'),
                          ('strikethrough', 's'), ('superscript', 'sup'), ('subscript', 'sub'), ('code', 'code')):
            if value.get(flag) is True:
                html = f'<{tag}>{html}</{tag}>'
        kind = value.get('type')
        if kind is not None and not isinstance(kind, str):
            raise ZahlebError('Взахлёб вернул неизвестный тип текстового блока.')
        if kind in {'link', 'a'}:
            href = _string(value.get('url') or value.get('href'))
            try:
                parts = urlsplit(href)
                safe_link = (parts.scheme.lower() in {'', 'https', 'http'} and not parts.username
                             and not parts.password and not any(ord(c) < 32 for c in href))
            except ValueError:
                safe_link = False
            if href and safe_link:
                html = f'<a href="{escape(href, quote=True)}">{html}</a>'
        if block or kind in {'paragraph', 'block-quote', 'blockquote', 'ul', 'ol', 'li',
                             'bulleted-list', 'numbered-list', 'list-item', 'h1', 'h2', 'h3', 'h4'}:
            tags = {'block-quote': 'blockquote', 'bulleted-list': 'ul', 'numbered-list': 'ol', 'list-item': 'li'}
            tag = tags.get(kind, kind) if kind else 'p'
            if tag not in {'blockquote', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4'}:
                tag = 'p'
            name = _string(value.get('name'))
            if name:
                html = f'<strong>{escape(name)}:</strong> ' + html
            anchor = _string(value.get('id'))
            attr = f' id="{escape(anchor, quote=True)}"' if re.fullmatch(r'[\w.-]{1,200}', anchor) else ''
            html = f'<{tag}{attr}>{html or "<br/>"}</{tag}>'
        return html


class ZahlebClient:
    def __init__(self, request_delay: float = 1.0):
        self.request_delay = max(0.0, request_delay)
        self._opener = build_safe_opener(ZAHLEB_HOSTS)
        self._cover_opener = build_safe_opener(frozenset(_COVER_HOSTS))

    @bounded_operation
    def preview(self, url: str) -> StoryPreview:
        root = normalize_zahleb_url(url)
        book, chapters = self._book(self._page(root), root)
        preview = StoryPreview(book['title'], book['shortAuthor'], root,
                               [ChapterPreview(i, item['title']) for i, item in enumerate(chapters, 1)])
        validate_preview(preview)
        return preview

    @bounded_operation
    def download(self, url: str, progress: Callable[[str], None] | None = None,
                 chapter_numbers: set[int] | frozenset[int] | None = None) -> Story:
        root = normalize_zahleb_url(url)
        book, chapters = self._book(self._page(root), root)
        report_title(progress, book['title'])
        if chapter_numbers is not None and (not chapter_numbers or any(
                n < 1 or n > len(chapters) for n in chapter_numbers)):
            raise ZahlebError(f'В истории {len(chapters)} глав. Проверь выбранные номера.')
        selected = [chapter for i, chapter in enumerate(chapters, 1)
                    if chapter_numbers is None or i in chapter_numbers]
        check_chapter_count(len(selected))
        bodies = []
        for index, chapter in enumerate(selected, 1):
            check_deadline()
            _check_access(chapter)
            if progress:
                progress(f'Скачиваю главы: {index}/{len(selected)}')
            if self.request_delay:
                bounded_sleep(self.request_delay)
            link = root + '/' + chapter['objectId']
            page = self._page(link)
            if not isinstance(page.get('story'), dict) or page['story'].get('objectId') != book['objectId']:
                raise ZahlebError('Взахлёб вернул другую историю вместо запрошенной главы.')
            ep = page.get('episode')
            if not isinstance(ep, dict):
                raise ZahlebAccessError('Взахлёб не отдал главу. Неполная книга не будет отправлена.')
            _check_access(ep)
            owner = ep.get('story')
            if (ep.get('objectId') != chapter['objectId'] or ep.get('order') != chapter['order']
                    or not isinstance(owner, dict) or owner.get('objectId') != book['objectId']
                    or ep.get('status') != 'published'):
                raise ZahlebError('Взахлёб вернул неверную или неопубликованную главу.')
            content = ep.get('content')
            if not isinstance(content, list) or not content:
                raise ZahlebAccessError('Взахлёб не отдал текст главы. Неполная книга не будет отправлена.')
            renderer = _RichText()
            html = ''.join(renderer.node(node, block=True) for node in content)
            length = ep.get('contentLength')
            if length is not None and (type(length) is not int or length < 0):
                raise ZahlebError('Взахлёб вернул неверный размер текста главы.')
            # Public episodes use both raw UTF-16 and JSON-encoded text lengths.
            # Accept the alternate representation only on an exact match, never a tolerance.
            if not renderer.has_text or (length is not None and renderer.text_units < length
                                         and renderer.json_text_units != length):
                logger.warning('zahleb_content_length_rejected chapter_id=%s declared=%s utf16=%s json_utf16=%s',
                               chapter['objectId'], length, renderer.text_units, renderer.json_text_units)
                raise ZahlebError('Взахлёб вернул пустой или неполный текст главы. Книга не будет отправлена.')
            bodies.append(Chapter.from_download(chapter['title'], html, link))
        age = book.get('ageRating')
        rating = f'{age}+' if type(age) is int and age in {0, 6, 12, 16, 18} else ''
        genres = list(dict.fromkeys(_labels(book.get('genres')) + _labels(book.get('marks'))))
        story = Story(book['title'], book['shortAuthor'], root,
            description=_string(book.get('subtitle')), chapters=bodies,
            fandoms=_labels(book.get('fanbase')), genres=genres,
            pairings=_labels(book.get('pairs')), rating=rating,
            status='В процессе' if book.get('continued') is True else 'Завершён' if book.get('continued') is False else '',
            cover=self._cover(book.get('coverImage')))
        validate_story(story)
        return story

    def _book(self, page: dict, root: str) -> tuple[dict, list[dict]]:
        raw = page.get('story')
        if not isinstance(raw, dict):
            raise ZahlebNotFoundError('История не найдена или недоступна на Взахлёб.')
        book = dict(raw)
        match = ZAHLEB_PATH.fullmatch(urlsplit(root).path)
        if match is None or book.get('objectId') != match.group(2):
            raise ZahlebError('Взахлёб вернул другую историю.')
        _check_access(book)
        if book.get('isComics') or book.get('isVideo') or _string(book.get('contentType')) in {'comics', 'comic', 'video', 'audio'}:
            raise ZahlebError('Бот скачивает текстовые истории Взахлёб, но не комиксы, видео или аудио.')
        book['title'] = _string(book.get('title'))
        book['shortAuthor'] = _string(book.get('shortAuthor')) or 'Автор не указан'
        if not book['title']:
            raise ZahlebError('Взахлёб не отдал название истории.')
        chapters = book.get('chapters')
        try:
            if not isinstance(book.get('episodesCount'), (str, int)) or isinstance(book.get('episodesCount'), bool):
                raise ValueError('Invalid chapter count')
            count = int(book['episodesCount'])
        except (KeyError, TypeError, ValueError):
            raise ZahlebError('Взахлёб не отдал число глав.') from None
        if count > current_limits().max_preview_chapters:
            raise ResourceLimitError('В истории Взахлёб слишком много глав.')
        if not isinstance(chapters, list) or not chapters or count != len(chapters):
            raise ZahlebError('Взахлёб вернул неполное оглавление. Книга не будет отправлена.')
        if any(not isinstance(c, dict) or type(c.get('order')) is not int
               or not _ID.fullmatch(_string(c.get('objectId'))) for c in chapters):
            raise ZahlebError('Взахлёб вернул повреждённое оглавление.')
        chapters = sorted(chapters, key=lambda c: c['order'])
        if ([c['order'] for c in chapters] != list(range(1, count + 1))
                or len({c['objectId'] for c in chapters}) != count):
            raise ZahlebError('В оглавлении Взахлёб пропущены или повторяются главы.')
        return book, [{**chapter, 'title': _string(chapter.get('title')) or f'Глава {i}'}
                      for i, chapter in enumerate(chapters, 1)]

    def _page(self, url: str) -> dict:
        try:
            parts = require_https_url(url, ZAHLEB_HOSTS)
            expected = ZAHLEB_PATH.fullmatch(parts.path)
            if expected is None:
                raise ZahlebError('Некорректная ссылка на историю Взахлёб.')
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)',
                                           'Accept-Language': 'ru-RU,ru;q=0.9'})
            with self._opener.open(request, timeout=30) as response:
                final = require_https_url(response.geturl(), ZAHLEB_HOSTS)
                actual = ZAHLEB_PATH.fullmatch(final.path)
                if (actual is None or actual.group(2) != expected.group(2)
                        or expected.group(3) is not None and actual.group(3) != expected.group(3)):
                    logger.warning('zahleb_redirect_rejected requested=%s final=%s', public_url(url), public_url(response.geturl()))
                    raise ZahlebError('Взахлёб перенаправил запрос на другую страницу.')
                data = read_bounded(response)
        except HTTPError as exc:
            with exc:
                code = exc.code
            if code in {404, 410}:
                raise ZahlebNotFoundError('История или глава не найдены на Взахлёб.') from None
            if code == 429:
                raise ZahlebRateLimitError('Взахлёб временно ограничил запросы. Попробуй позже.') from None
            if code in {401, 403}:
                raise ZahlebAccessError('Взахлёб отказал в доступе к странице. Попробуй позже.') from None
            raise ZahlebError(f'Взахлёб вернул HTTP {code}. Попробуй позже.') from None
        except (URLError, OSError):
            raise ZahlebError('Не удалось безопасно загрузить страницу Взахлёб. Попробуй позже.') from None
        try:
            soup = BeautifulSoup(data.decode('utf-8'), 'lxml')
            scripts = soup.find_all('script', id='__NEXT_DATA__')
            if len(scripts) != 1:
                raise ValueError('Missing page data')
            state = json.loads(scripts[0].string or '')
            page = state['props']['pageProps']
            if not isinstance(page, dict):
                raise ValueError('Invalid page data')
            if expected.group(3) is None and actual.group(3) is not None:
                _, chapters = self._book(page, url)
                ep = page.get('episode')
                if (actual.group(3) != chapters[0]['objectId'] or not isinstance(ep, dict)
                        or ep.get('objectId') != actual.group(3)):
                    logger.warning('zahleb_redirect_rejected requested=%s final=%s', public_url(url), public_url(response.geturl()))
                    raise ZahlebError('Взахлёб перенаправил запрос не на первую главу запрошенной истории.')
                logger.info('zahleb_redirect_accepted requested=%s final=%s', public_url(url), public_url(response.geturl()))
            return page
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ZahlebError('Взахлёб изменил страницу или не отдал данные истории. Попробуй позже.') from None

    def _cover(self, value: object) -> CoverImage | None:
        url = _string(value)
        if not url:
            return None
        try:
            require_https_url(url, frozenset(_COVER_HOSTS))
            with self._cover_opener.open(Request(url), timeout=20) as response:
                data = read_bounded(response, 8 * 1024 * 1024)
            if not image_is_safe(data):
                return None
            with Image.open(BytesIO(data)) as image:
                fmt = image.format
            if fmt not in {'JPEG', 'PNG'}:
                return None
            return CoverImage(data, 'image/jpeg' if fmt == 'JPEG' else 'image/png', 'jpg' if fmt == 'JPEG' else 'png')
        except (URLError, OSError, ValueError, ResourceLimitError):
            check_deadline()
            return None
