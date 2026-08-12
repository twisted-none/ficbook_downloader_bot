from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http.cookiejar import CookieJar
import mimetypes
import re
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from bs4 import BeautifulSoup, Tag

from src.core.models import Chapter, ChapterPreview, CoverImage, Story, StoryPreview

MAX_COVER_BYTES = 8 * 1024 * 1024
LOGIN_URL = (
    "https://litnet.com/auth/login?classic=1&"
    "link=https%3A%2F%2Flitnet.com%2Fru%2Fsite%2Flibrary"
)


class LitnetError(Exception):
    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class LitnetLoginError(LitnetError):
    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(
            message,
            user_message=user_message or "Авторизация через Litnet не сработала.",
        )


class LitnetAccessError(LitnetError):
    pass


class LitnetPaidBookError(LitnetAccessError):
    def __init__(self, message: str = "Litnet book requires purchase") -> None:
        super().__init__(
            message,
            user_message="Эта книга платная. Бот скачивает только бесплатные книги Litnet.",
        )


class LitnetRateLimitError(LitnetError):
    pass


@dataclass(frozen=True, slots=True)
class LitnetChapter:
    index: int
    title: str
    chapter_id: str


class LitnetClient:
    def __init__(self, login: str = "", password: str = "") -> None:
        self.login = login
        self.password = password
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._authenticated = False
        self._lock = Lock()

    def download(
        self,
        url: str,
        progress: Callable[[str], None] | None = None,
        chapter_numbers: set[int] | frozenset[int] | None = None,
    ) -> Story:
        with self._lock:
            reader = self._soup(url)
            book = self._soup(url.replace("/reader/", "/book/"))
            self._ensure_free_book(book)
            try:
                chapters = self._chapters(reader)
            except LitnetAccessError:
                self._login_for_protected_content()
                reader = self._soup(url)
                book = self._soup(url.replace("/reader/", "/book/"))
                chapters = self._chapters(reader)
            selected = self._select_chapters(chapters, chapter_numbers)
            bodies: list[Chapter] = []
            for position, chapter in enumerate(selected, 1):
                if progress:
                    progress(f"Скачиваю главы: {position}/{len(selected)}")
                page = self._soup(f"{url}?c={chapter.chapter_id}")
                try:
                    html = self._chapter_html(page)
                except LitnetAccessError:
                    try:
                        self._login_for_protected_content()
                    except LitnetLoginError as exc:
                        raise self._protected_chapter_login_error(chapter.index, exc) from exc
                    html = self._chapter_html(self._soup(f"{url}?c={chapter.chapter_id}"))
                bodies.append(Chapter(chapter.title, html))
            return self._story(url, reader, book, bodies)

    def preview(self, url: str) -> StoryPreview:
        with self._lock:
            reader = self._soup(url)
            book = self._soup(url.replace("/reader/", "/book/"))
            self._ensure_free_book(book)
            try:
                chapters = self._chapters(reader)
            except LitnetAccessError:
                self._login_for_protected_content()
                reader = self._soup(url)
                chapters = self._chapters(reader)
            title = self._metadata(reader, "og:title") or self._heading(reader, "h1") or "litnet"
            author_node = reader.select_one("a.sa-name")
            author = self._text(author_node) or "unknown"
            return StoryPreview(
                title=title,
                author=author,
                source_url=url,
                chapters=[ChapterPreview(chapter.index, chapter.title) for chapter in chapters],
            )

    def _login_if_configured(self) -> None:
        if self._authenticated or not (self.login and self.password):
            return
        page = self._soup(LOGIN_URL)
        form = page.find("form", action=re.compile(r"/auth/login"))
        if not isinstance(form, Tag):
            raise LitnetLoginError("Litnet изменил форму входа. Авторизация сейчас недоступна.")
        payload = {
            node.get("name"): node.get("value", "")
            for node in form.select("input[name]")
            if node.get("name")
        }
        payload["LoginForm[login]"] = self.login
        payload["LoginForm[password]"] = self.password
        action = urljoin(LOGIN_URL, str(form.get("action") or LOGIN_URL))
        response = self._soup(action, urlencode(payload).encode("utf-8"))
        if response.select_one("input[name='LoginForm[login]']"):
            if self._requires_captcha(response):
                raise LitnetLoginError(
                    "Litnet запросил CAPTCHA. Бот не обходит проверку; войти сейчас не удалось."
                )
            raise LitnetLoginError("Не удалось войти в Litnet. Проверь LITNET_LOGIN и LITNET_PASSWORD.")
        self._authenticated = True

    def _login_for_protected_content(self) -> None:
        if not (self.login and self.password):
            raise LitnetAccessError(
                "Для доступа к этой книге или главе требуется авторизация Litnet.",
                user_message="Эта книга или глава недоступна для скачивания через Litnet.",
            )
        self._login_if_configured()

    def _protected_chapter_login_error(
        self,
        chapter_index: int,
        error: LitnetLoginError,
    ) -> LitnetLoginError:
        if "captcha" in str(error).lower():
            user_message = (
                f"Глава {chapter_index} доступна только после входа, но Litnet запросил "
                "проверку «Я не робот». Выбери доступные главы через режим выбора глав."
            )
        else:
            user_message = (
                f"Глава {chapter_index} доступна только после входа в Litnet, "
                "но авторизация не сработала."
            )
        return LitnetLoginError(str(error), user_message=user_message)

    def _story(
        self,
        url: str,
        reader: BeautifulSoup,
        book: BeautifulSoup,
        chapters: list[Chapter],
    ) -> Story:
        title = self._metadata(reader, "og:title") or self._heading(reader, "h1") or "litnet"
        description = self._metadata(reader, "og:description")
        author = self._text(reader.select_one("a.sa-name")) or "unknown"
        page_text = book.get_text(" ", strip=True)
        status = (
            "завершён"
            if re.search(r"\b(?:Закончена|Завершена|Полный текст)\b", page_text, re.I)
            else ""
        )
        if not status and re.search(r"\b(?:В процессе|Пишется)\b", page_text, re.I):
            status = "в процессе"
        page_count_match = re.search(r"(\d+)\s*стр\.?", page_text, re.I)
        updated_match = re.search(r"Отредактировано:\s*([0-9.]+)", reader.get_text(" ", strip=True), re.I)
        rating_match = re.search(r"(?<!\d)(18\+|16\+|12\+)(?!\d)", reader.get_text(" ", strip=True))
        genres = self._genres(reader)
        updated = updated_match.group(1) if updated_match else ""
        return Story(
            title=title,
            author=author,
            source_url=url,
            description=description,
            annotation_html=self._annotation(description, status, updated),
            language="ru",
            updated=updated,
            finish_date=updated if status == "завершён" else "",
            status=status,
            genres=genres,
            rating=rating_match.group(1) if rating_match else "",
            page_count=page_count_match.group(1) if page_count_match else "",
            cover=self._cover(reader),
            chapters=chapters,
        )

    def _chapters(self, soup: BeautifulSoup) -> list[LitnetChapter]:
        options = soup.select("select option[value]")
        chapters = [
            LitnetChapter(index, self._text(option) or f"Глава {index}", str(option.get("value")))
            for index, option in enumerate(options, 1)
            if str(option.get("value") or "").isdigit()
        ]
        if not chapters:
            text = soup.get_text(" ", strip=True)
            if re.search(r"доступн\w*\s+только\s+зарегистрирован|необходимо\s+авториз", text, re.I):
                raise LitnetAccessError(
                    "Litnet требует авторизацию для получения списка глав.",
                    user_message="Эта книга недоступна для скачивания через Litnet.",
                )
            raise LitnetError("Не удалось получить список глав Litnet.")
        return chapters

    def _chapter_html(self, soup: BeautifulSoup) -> str:
        container = soup.select_one(".reader-text")
        if not isinstance(container, Tag):
            text = soup.get_text(" ", strip=True)
            if re.search(r"доступн\w*\s+только|купить|авториз", text, re.I):
                raise LitnetAccessError(
                    "У аккаунта Litnet нет доступа к этой главе. Проверь покупку или подписку.",
                    user_message="Эта глава недоступна для скачивания через Litnet.",
                )
            raise LitnetError("Litnet не вернул текст главы.")
        for node in container.select("script, style, button, .advertising, h2"):
            node.decompose()
        html = container.decode_contents().strip()
        if not BeautifulSoup(html, "lxml").get_text(" ", strip=True):
            raise LitnetError("Litnet вернул пустой текст главы.")
        return html

    def _ensure_free_book(self, soup: BeautifulSoup) -> None:
        purchase = soup.select_one(".buy-button-container .js-start-buy-metrics")
        if not isinstance(purchase, Tag):
            return
        text = self._text(purchase)
        if re.search(r"\b(?:купить|подписка)\b", text, re.I):
            raise LitnetPaidBookError(f"Litnet purchase button found: {text}")

    def _select_chapters(
        self,
        chapters: list[LitnetChapter],
        numbers: set[int] | frozenset[int] | None,
    ) -> list[LitnetChapter]:
        if numbers is None:
            return chapters
        missing = sorted(number for number in numbers if number < 1 or number > len(chapters))
        if missing:
            values = ", ".join(map(str, missing))
            raise LitnetError(f"В книге {len(chapters)} глав, нельзя скачать: {values}.")
        return [chapter for chapter in chapters if chapter.index in numbers]

    def _cover(self, soup: BeautifulSoup) -> CoverImage | None:
        url = self._metadata(soup, "og:image")
        if not url:
            return None
        try:
            data, content_type = self._open_bytes(url)
        except LitnetError:
            return None
        if not data or len(data) > MAX_COVER_BYTES:
            return None
        mime = content_type.split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            return None
        return CoverImage(data, mime, self._cover_extension(mime, url))

    def _soup(self, url: str, data: bytes | None = None) -> BeautifulSoup:
        payload, _ = self._open_bytes(url, data)
        return BeautifulSoup(payload.decode("utf-8", "ignore"), "lxml")

    def _open_bytes(self, url: str, data: bytes | None = None) -> tuple[bytes, str]:
        request = Request(
            url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.read(), response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code == 429:
                raise LitnetRateLimitError("Litnet временно ограничил запросы.") from exc
            if exc.code in {401, 403}:
                raise LitnetAccessError(
                    "У аккаунта Litnet нет доступа к этой книге или главе.",
                    user_message="Эта книга или глава недоступна для скачивания через Litnet.",
                ) from exc
            raise LitnetError(
                f"Litnet вернул ошибку HTTP {exc.code}.",
                user_message="Litnet вернул ошибку при загрузке. Попробуй повторить позже.",
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise LitnetError("Litnet сейчас не отвечает. Попробуй повторить позже.") from exc

    def _cover_extension(self, mime: str, url: str) -> str:
        extension = mimetypes.guess_extension(mime) or ""
        extension = extension.lstrip(".") or url.rsplit(".", 1)[-1].split("?", 1)[0]
        return re.sub(r"[^a-z0-9]", "", extension.lower()) or "jpg"

    def _metadata(self, soup: BeautifulSoup, property_name: str) -> str:
        node = soup.find("meta", attrs={"property": property_name})
        return str(node.get("content") or "").strip() if isinstance(node, Tag) else ""

    def _heading(self, soup: BeautifulSoup, selector: str) -> str:
        return self._text(soup.select_one(selector))

    def _genres(self, soup: BeautifulSoup) -> list[str]:
        values: list[str] = []
        for node in soup.select("a[href*='/top/'], a[href*='/tag/']"):
            text = self._text(node)
            if text and text != "Литнет" and text not in values:
                values.append(text)
        return values

    def _annotation(self, description: str, status: str, updated: str) -> str:
        parts = [f"<p>{escape(description)}</p>" if description else ""]
        if status:
            parts.append(f"<p><strong>Статус:</strong> {escape(status)}</p>")
        if updated:
            parts.append(f"<p><strong>Обновлено:</strong> {escape(updated)}</p>")
        return "".join(parts)

    def _requires_captcha(self, soup: BeautifulSoup) -> bool:
        captcha = soup.select_one("input[name='LoginForm[captcha]']")
        return bool(
            captcha
            and str(captcha.get("type") or "").lower() != "hidden"
            or soup.select_one(".g-recaptcha, [data-sitekey], iframe[src*='recaptcha']")
            or re.search(
                r"\bCAPTCHA\b|(?:провер|подтверд)\w*,?\s+что\s+вы\s+не\s+робот",
                soup.get_text(" ", strip=True),
                re.I,
            )
        )

    def _text(self, node: Any) -> str:
        return node.get_text(" ", strip=True) if isinstance(node, Tag) else ""
