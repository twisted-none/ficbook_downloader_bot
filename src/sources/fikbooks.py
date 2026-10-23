from __future__ import annotations

from collections.abc import Callable
from html import escape
from io import BytesIO
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request

from bs4 import BeautifulSoup, Tag
from PIL import Image

from src.core.limits import (ResourceLimitError, bounded_operation, bounded_sleep,
                             check_chapter_count, check_deadline, current_limits,
                             image_is_safe, read_bounded, validate_story)
from src.core.models import Chapter, ChapterPreview, CoverImage, Story, StoryPreview
from src.sources.registry import FIKBOOKS_HOSTS, FIKBOOKS_PATH, normalize_fikbooks_url
from src.sources.safe_http import UnsafeRequestError, build_safe_opener, require_https_url


class FikbooksError(Exception):
    pass


class FikbooksNotFoundError(FikbooksError):
    pass


class FikbooksRateLimitError(FikbooksError):
    pass


def _text(node: Tag | None) -> str:
    return node.get_text(' ', strip=True) if node is not None else ''


class FikbooksClient:
    """Public pages only; independent of Ficbook accounts and its native transport."""

    def __init__(self, request_delay: float = 1.0):
        self.request_delay = max(0.0, request_delay)
        self._opener = build_safe_opener(FIKBOOKS_HOSTS)
        self._cover_opener = build_safe_opener()

    @bounded_operation
    def preview(self, url: str) -> StoryPreview:
        root = normalize_fikbooks_url(url)
        soup = self._soup(root)
        title, author, _ = self._credits(soup)
        chapters = self._chapters(soup, root)
        return StoryPreview(title, author, root, [ChapterPreview(i, title)
                                                 for i, (title, _) in enumerate(chapters, 1)])

    @bounded_operation
    def download(self, url: str, progress: Callable[[str], None] | None = None,
                 chapter_numbers: set[int] | frozenset[int] | None = None) -> Story:
        root = normalize_fikbooks_url(url)
        soup = self._soup(root)
        title, author, translator = self._credits(soup)
        chapters = self._chapters(soup, root)
        if chapter_numbers is not None and (not chapter_numbers or any(
                number < 1 or number > len(chapters) for number in chapter_numbers)):
            raise FikbooksError(f'В фанфике {len(chapters)} глав. Проверь выбранные номера.')
        selected = [(title, link) for i, (title, link) in enumerate(chapters, 1)
                    if chapter_numbers is None or i in chapter_numbers]
        check_chapter_count(len(selected))
        bodies = []
        for position, (chapter_title, link) in enumerate(selected, 1):
            check_deadline()
            if progress:
                progress(f'Скачиваю главы: {position}/{len(selected)}')
            if self.request_delay:
                bounded_sleep(self.request_delay)
            page = soup if link == root else self._soup(link)
            bodies.append(Chapter.from_download(chapter_title, self._chapter_html(page), link))
        description = _text(soup.select_one('.article-top [itemprop="description"]')
                            or soup.select_one('[itemprop="description"]'))
        annotation = []
        for field in soup.select('.article-top .card-item__field'):
            label = _text(field.select_one('.card-item__field-title'))
            value = _text(field.select_one('.card-item__field-text'))
            if label and value:
                annotation.append(f'<p><strong>{escape(label)}:</strong> {escape(value)}</p>')
        badges = [_text(node) for node in soup.select('.article-header .card-item__badge')]
        story = Story(title, author, root, description=description, annotation_html=''.join(annotation),
                      translator=translator, chapters=bodies,
                      fandoms=[_text(node) for node in soup.select('.article-categories a')],
                      genres=[_text(node) for node in soup.select('.article-top .tag-field__item')],
                      pairings=[_text(node) for node in soup.select('.article-top .pairaing-field__item')],
                      status=next((b for b in badges if b in {'Завершён', 'Завершен', 'В процессе', 'Заморожен'}), ''),
                      rating=next((b for b in badges if re.fullmatch(r'(?:G|PG-13|R|NC-17|NC-21)', b)), ''),
                      cover=self._cover(soup, root))
        validate_story(story)
        return story

    def _credits(self, soup: BeautifulSoup) -> tuple[str, str, str]:
        title = _text(soup.select_one('h1.article-header__name'))
        if not title:
            raise FikbooksError('Fikbooks не вернул страницу фанфика или изменил её разметку.')
        authors, translators = [], []
        for item in soup.select('.author-item'):
            name = _text(item.select_one('.author-item__name'))
            info = item.select('.author-item__info')
            role = _text(info[-1]).lower() if info else ''
            names = translators if 'переводчик' in role else authors
            if name and name not in names:
                names.append(name)
        return title, ', '.join(authors) or 'Автор не указан', ', '.join(translators)

    def _chapters(self, soup: BeautifulSoup, root: str) -> list[tuple[str, str]]:
        result, seen = [], set()
        for link in soup.select('#parts a.part-item__link'):
            try:
                raw = urljoin(root, str(link.get('href') or ''))
                parts = urlsplit(raw)
                clean = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
                require_https_url(clean, FIKBOOKS_HOSTS)
                match = FIKBOOKS_PATH.fullmatch(parts.path)
                if normalize_fikbooks_url(clean) != root or not match or not match.group(2):
                    raise ValueError('Unexpected chapter URL')
            except (ValueError, UnsafeRequestError):
                raise FikbooksError('Fikbooks вернул небезопасную ссылку на главу.') from None
            # Canonical host also makes duplicate detection and in-book navigation consistent.
            clean = f'{root}/{match.group(2)}'
            if clean not in seen:
                seen.add(clean)
                result.append((_text(link) or f'Глава {len(result) + 1}', clean))
                if len(result) > current_limits().max_preview_chapters:
                    raise ResourceLimitError('В произведении слишком много глав.')
        if not result:
            if soup.select_one('.article-parts__description') is not None:
                result.append((_text(soup.select_one('.part-header__title')) or 'Текст', root))
            else:
                raise FikbooksError('Не удалось получить оглавление Fikbooks. Скачивание остановлено.')
        return result

    def _chapter_html(self, soup: BeautifulSoup) -> str:
        content = soup.select_one('.article-parts .article-parts__description')
        if content is None or not content.get_text(strip=True):
            raise FikbooksError('Fikbooks не отдал текст главы. Неполный файл не будет отправлен.')
        content = BeautifulSoup(str(content), 'lxml')
        for node in content.select('script,style,iframe,object,embed,form,input,button,img,video,audio'):
            node.decompose()
        allowed_tags = {'div', 'span', 'p', 'br', 'hr', 'a', 'strong', 'b', 'em', 'i', 'u', 's',
                        'sup', 'sub', 'blockquote', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4',
                        'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'pre', 'code'}
        for node in content.find_all(True):
            if node.name not in allowed_tags:
                node.unwrap()
                continue
            node.attrs = {key: value for key, value in node.attrs.items()
                          if key in {'id', 'name', 'href', 'title'}}
            if node.has_attr('href'):
                try:
                    parts = urlsplit(str(node['href']))
                    safe_scheme = parts.scheme.lower() in {'', 'https', 'http'}
                except ValueError:
                    safe_scheme = False
                if not safe_scheme:
                    node.attrs.pop('href', None)
        return '<div id="part_content">' + (content.body or content).decode_contents() + '</div>'

    def _soup(self, url: str) -> BeautifulSoup:
        try:
            require_https_url(url, FIKBOOKS_HOSTS)
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)',
                                            'Accept-Language': 'ru-RU,ru;q=0.9'})
            with self._opener.open(request, timeout=30) as response:
                data = read_bounded(response)
            return BeautifulSoup(data.decode('utf-8', 'replace'), 'lxml')
        except HTTPError as exc:
            with exc:
                code = exc.code
            if code in {404, 410}:
                raise FikbooksNotFoundError('Фанфик или глава не найдены на Fikbooks.') from None
            if code == 429:
                raise FikbooksRateLimitError('Fikbooks временно ограничил запросы. Попробуй позже.') from None
            raise FikbooksError(f'Fikbooks вернул HTTP {code}. Страница сейчас недоступна.') from None
        except (URLError, OSError):
            raise FikbooksError('Не удалось безопасно загрузить страницу Fikbooks. Попробуй позже.') from None

    def _cover(self, soup: BeautifulSoup, root: str) -> CoverImage | None:
        image = soup.select_one('img.article-top__image')
        if image is None or not image.get('src'):
            return None
        url = urljoin(root, str(image['src']))
        if urlsplit(url).path == '/img/nofanfic.jpg':
            return None
        try:
            require_https_url(url)
            with self._cover_opener.open(Request(url), timeout=20) as response:
                data = read_bounded(response, 8 * 1024 * 1024)
            if not image_is_safe(data):
                return None
            with Image.open(BytesIO(data)) as image:
                fmt = image.format
            if fmt not in {'JPEG', 'PNG'}:
                return None
            return CoverImage(data, 'image/jpeg' if fmt == 'JPEG' else 'image/png',
                              'jpg' if fmt == 'JPEG' else 'png')
        except (URLError, OSError, ValueError, ResourceLimitError):
            return None
