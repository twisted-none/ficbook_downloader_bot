from __future__ import annotations

from collections.abc import Callable
from configparser import DuplicateSectionError
from dataclasses import dataclass
from enum import Enum
from http.cookiejar import CookieJar
import json
import logging
import mimetypes
from pathlib import Path
import re
from threading import Lock
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import fanficfare
from bs4 import BeautifulSoup, NavigableString, Tag
from fanficfare import adapters, exceptions
from fanficfare.configurable import Configuration

from src.sources.litnet import (
    LitnetClient,
    LitnetError,
    LitnetLoginError,
    LitnetPaidBookError,
    LitnetRateLimitError,
)
from src.sources.ranobelib import (
    RanobelibAccessError,
    RanobelibClient,
    RanobelibError,
    RanobelibLoginError,
    RanobelibNotFoundError,
    RanobelibRateLimitError,
)
from src.core.models import Chapter, ChapterPreview, CoverImage, Story, StoryPreview
from src.sources.registry import (
    SUPPORTED_HOST_MARKERS,
    extract_url,
    is_ficbook_url as _is_ficbook_url,
    is_hogwartsnet_url as _is_hogwartsnet_url,
    is_litnet_url as _is_litnet_url,
    is_ranobelib_url as _is_ranobelib_url,
    is_supported_story_url,
    normalize_url,
    site_display_name as _site_display_name,
    site_key as _site_key,
)


logger = logging.getLogger(__name__)


class FicbookError(Exception):
    """Domain error with separate admin and user-facing messages."""

    def __init__(
        self,
        message: str,
        *,
        technical: str = "",
        user_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.user_message = user_message or message
        self.technical = technical


class FicbookNotFoundError(FicbookError):
    """Ficbook story does not exist or is not available by this URL."""


class FicbookRateLimitError(FicbookError):
    """Ficbook temporarily rejected requests with HTTP 429."""

    def __init__(
        self,
        message: str,
        *,
        technical: str = "",
        retry_after: float | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, technical=technical, user_message=user_message)
        self.retry_after = retry_after


class FicbookLoginError(FicbookError):
    """The account assigned to a queue could not authenticate."""


class FicbookPaidContentError(FicbookError):
    """The requested work requires a purchase that the bot does not perform."""


class FicbookSiteUnavailableError(FicbookError):
    """Ficbook is serving a maintenance page instead of the normal site."""


class FicbookChapterDownloadError(FicbookError):
    """A chapter stayed unavailable after source-specific retries."""


class FicbookSiteStatus(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FicbookAccount:
    login: str = ""
    password: str = ""

    @property
    def label(self) -> str:
        return self.login or "anonymous"


BLOCK_HTML_TAGS = {"article", "blockquote", "div", "hr", "li", "ol", "p", "section", "ul"}
MAX_COVER_BYTES = 8 * 1024 * 1024
FICBOOK_HOME_URL = "https://ficbook.net/"
FICBOOK_HOME_MAX_BYTES = 512 * 1024
DATE_VALUE_PATTERN = r"(?:[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2,4}|[0-9]{1,2}\s+[А-Яа-яЁё]+\s+[0-9]{4})"


class FicbookClient:
    def __init__(
        self,
        login: str = "",
        password: str = "",
        accounts: tuple[FicbookAccount, ...] = (),
        site_accounts: dict[str, tuple[FicbookAccount, ...]] | None = None,
        request_delay: float = 0.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 8.0,
        retry_max_delay: float = 45.0,
        litnet_login: str = "",
        litnet_password: str = "",
        ranobelib_login: str = "",
        ranobelib_password: str = "",
    ) -> None:
        self.accounts = accounts or (FicbookAccount(login, password),)
        self.site_accounts = site_accounts or {}
        self.request_delay = max(0.0, request_delay)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self.retry_max_delay = max(0.0, retry_max_delay)
        self.litnet = LitnetClient(litnet_login, litnet_password)
        self.ranobelib = RanobelibClient(ranobelib_login, ranobelib_password)
        self.defaults_ini = Path(fanficfare.__file__).with_name("defaults.ini")
        self._account_lock = Lock()
        self._next_account_index: dict[str, int] = {}

    def ficbook_site_status(self) -> FicbookSiteStatus:
        request = Request(
            FICBOOK_HOME_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                status = int(getattr(response, "status", 200))
                html = response.read(FICBOOK_HOME_MAX_BYTES).decode("utf-8", "ignore")
        except HTTPError as exc:
            try:
                html = exc.read(FICBOOK_HOME_MAX_BYTES).decode("utf-8", "ignore")
            except OSError:
                html = ""
            return classify_ficbook_homepage(exc.code, html)
        except Exception:
            logger.warning("Could not check Ficbook availability", exc_info=True)
            return FicbookSiteStatus.UNKNOWN
        return classify_ficbook_homepage(status, html)

    def download(
        self,
        url: str,
        progress: Callable[[str], None] | None = None,
        chapter_numbers: set[int] | frozenset[int] | None = None,
        account: FicbookAccount | None = None,
    ) -> Story:
        normalized = normalize_url(url)
        site_name = _site_display_name(normalized)
        last_rate_limit: FicbookRateLimitError | None = None
        last_transient_error: FicbookError | None = None
        accounts = (
            (account,)
            if account is not None and _is_ficbook_url(normalized)
            else self._accounts_for_url(normalized, rotate=True)
        )
        for index, account in enumerate(accounts, 1):
            for attempt in range(1, self.retry_attempts + 1):
                try:
                    _report(progress, f"Скачиваю описание фанфика: аккаунт {index}/{len(accounts)}")
                    return self._download_once(normalized, account, progress, chapter_numbers)
                except FicbookRateLimitError as exc:
                    last_rate_limit = exc
                    last_transient_error = None
                    logger.warning(
                        "%s rate limit for account %s/%s, attempt %s/%s",
                        site_name,
                        index,
                        len(accounts),
                        attempt,
                        self.retry_attempts,
                    )
                    if attempt >= self.retry_attempts:
                        break
                    delay = self._retry_delay(attempt)
                    _report(
                        progress,
                        (
                            f"{site_name} временно ограничил запросы.\n"
                            f"Повторю попытку через {delay:.0f} сек.; пока ничего делать не нужно."
                        ),
                    )
                    sleep(delay)
                except FicbookError as exc:
                    if not _is_ao3_transient_error(normalized, exc):
                        raise
                    last_transient_error = exc
                    last_rate_limit = None
                    if attempt >= self.retry_attempts:
                        break
                    delay = self._retry_delay(attempt)
                    _report(
                        progress,
                        f"AO3 временно отклонил запрос. Повторю попытку через {delay:.0f} сек.",
                    )
                    sleep(delay)
        if last_rate_limit:
            raise FicbookRateLimitError(
                f"{site_name} временно ограничил запросы. Попробуй повторить позже.",
                technical=getattr(last_rate_limit, "technical", "") or str(last_rate_limit),
                retry_after=self.retry_max_delay or None,
            ) from last_rate_limit
        if last_transient_error:
            raise last_transient_error
        raise FicbookError("Не удалось загрузить фанфик.")

    def preview(self, url: str, progress: Callable[[str], None] | None = None) -> StoryPreview:
        normalized = normalize_url(url)
        site_name = _site_display_name(normalized)
        last_rate_limit: FicbookRateLimitError | None = None
        last_transient_error: FicbookError | None = None
        accounts = self._accounts_for_url(normalized)
        for index, account in enumerate(accounts, 1):
            for attempt in range(1, self.retry_attempts + 1):
                try:
                    _report(
                        progress,
                        "Введите главы, которые хотите скачать, или 0 для всех.\n"
                        "Формат: 1,2,5-10,17.\n"
                        f"Аккаунт: {index}/{len(accounts)}",
                    )
                    return self._preview_once(normalized, account)
                except FicbookRateLimitError as exc:
                    last_rate_limit = exc
                    last_transient_error = None
                    if attempt >= self.retry_attempts:
                        break
                    delay = self._retry_delay(attempt)
                    _report(progress, f"{site_name} временно ограничил запросы. Повторю через {delay:.0f} сек.")
                    sleep(delay)
                except FicbookError as exc:
                    if not _is_ao3_transient_error(normalized, exc):
                        raise
                    last_transient_error = exc
                    last_rate_limit = None
                    if attempt >= self.retry_attempts:
                        break
                    delay = self._retry_delay(attempt)
                    _report(progress, f"AO3 временно отклонил запрос. Повторю через {delay:.0f} сек.")
                    sleep(delay)
        if last_rate_limit:
            raise FicbookError(
                f"{site_name} временно ограничил запросы. Попробуй повторить скачивание позже.",
                technical=getattr(last_rate_limit, "technical", "") or str(last_rate_limit),
            ) from last_rate_limit
        if last_transient_error:
            raise last_transient_error
        raise FicbookError("Не удалось получить список глав.")

    def _download_once(
        self,
        normalized: str,
        account: FicbookAccount,
        progress: Callable[[str], None] | None,
        chapter_numbers: set[int] | frozenset[int] | None,
    ) -> Story:
        if _is_ranobelib_url(normalized):
            try:
                return self.ranobelib.download(normalized, progress, chapter_numbers)
            except RanobelibNotFoundError as exc:
                raise FicbookNotFoundError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except RanobelibRateLimitError as exc:
                raise FicbookRateLimitError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except RanobelibLoginError as exc:
                raise FicbookLoginError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except (RanobelibAccessError, RanobelibError) as exc:
                raise FicbookError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
        if _is_litnet_url(normalized):
            try:
                return self.litnet.download(normalized, progress, chapter_numbers)
            except LitnetPaidBookError as exc:
                raise FicbookPaidContentError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetLoginError as exc:
                raise FicbookLoginError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetRateLimitError as exc:
                raise FicbookRateLimitError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetError as exc:
                raise FicbookError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
        if _is_hogwartsnet_url(normalized):
            return self._download_hogwartsnet_once(normalized, account, progress, chapter_numbers)
        if not _is_ficbook_url(normalized):
            return self._download_fanficfare_once(normalized, account, progress, chapter_numbers)
        try:
            config = self._build_config(normalized, account)
            adapter = adapters.getAdapter(config, normalized)
            story = self._get_story_metadata(adapter, normalized)
            _report(progress, "Скачиваю страницу фанфика")
            self._pause_between_requests()
            page_soup = adapter.make_soup(adapter.get_request(normalized))
            self._fill_optional_ficbook_metadata(adapter, page_soup)
            chapter_links = adapter.get_chapters()
            selected_chapters = select_chapter_links(chapter_links, chapter_numbers)
            chapters: list[Chapter] = []
            for position, (chapter_index, chapter) in enumerate(selected_chapters, 1):
                _report(progress, f"Скачиваю главы: {position}/{len(selected_chapters)}")
                self._pause_between_requests()
                chapters.append(Chapter(title=chapter["title"], html=self._load_chapter_html(adapter, chapter["url"])))
        except exceptions.AdultCheckRequired as exc:
            raise FicbookError(
                self._adult_message(account),
                user_message=self._adult_user_message(normalized),
            ) from exc
        except exceptions.FailedToLogin as exc:
            raise FicbookLoginError(
                "Не удалось войти в Ficbook. Проверь логин и пароль назначенного аккаунта.",
                user_message="Авторизация через Ficbook не сработала.",
            ) from exc
        except (exceptions.UnknownSite, exceptions.StoryDoesNotExist) as exc:
            raise FicbookNotFoundError("Не удалось найти фанфик по этой ссылке.") from exc
        except Exception as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Ficbook временно ограничил запросы.", technical=repr(exc)) from exc
            if self.ficbook_site_status() is FicbookSiteStatus.UNAVAILABLE:
                raise FicbookSiteUnavailableError(
                    "Ficbook сейчас не работает и вместо обычной страницы показывает сообщение о недоступности.",
                    technical=repr(exc),
                    user_message=(
                        "Ficbook сейчас не работает. Фанфик останется в очереди и скачается, "
                        "как только сайт снова заработает."
                    ),
                ) from exc
            raise _friendly_download_error(normalized, exc) from exc
        return Story(
            title=story.getMetadata("title") or "ficbook",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            description=story.getMetadata("description") or "",
            annotation_html=self._build_annotation_html(story, page_soup, normalized),
            language=story.getMetadata("language") or "ru",
            published=story.getMetadata("datePublished") or "",
            updated=story.getMetadata("dateUpdated") or "",
            start_date=self._safe_text(story.getMetadata("dateStart") or story.getMetadata("datePublished")),
            finish_date=self._safe_text(story.getMetadata("dateFinish")),
            status=self._status_text(story.getMetadata("status")),
            fandoms=story.getList("category"),
            genres=story.getList("genre"),
            pairings=story.getList("ships"),
            rating=story.getMetadata("rating") or "",
            page_count=self._safe_text(story.getMetadata("pages")),
            word_count=self._safe_text(story.getMetadata("numWords")).replace(",", " "),
            cover=self._extract_cover_image(story),
            chapters=chapters,
        )

    def _preview_once(self, normalized: str, account: FicbookAccount) -> StoryPreview:
        if _is_ranobelib_url(normalized):
            try:
                return self.ranobelib.preview(normalized)
            except RanobelibNotFoundError as exc:
                raise FicbookNotFoundError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except RanobelibRateLimitError as exc:
                raise FicbookRateLimitError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except RanobelibLoginError as exc:
                raise FicbookLoginError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
            except (RanobelibAccessError, RanobelibError) as exc:
                raise FicbookError(str(exc), technical=repr(exc), user_message=exc.user_message) from exc
        if _is_litnet_url(normalized):
            try:
                return self.litnet.preview(normalized)
            except LitnetPaidBookError as exc:
                raise FicbookPaidContentError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetLoginError as exc:
                raise FicbookLoginError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetRateLimitError as exc:
                raise FicbookRateLimitError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
            except LitnetError as exc:
                raise FicbookError(
                    str(exc),
                    technical=repr(exc),
                    user_message=exc.user_message,
                ) from exc
        if _is_hogwartsnet_url(normalized):
            return self._preview_hogwartsnet_once(normalized, account)
        if not _is_ficbook_url(normalized):
            return self._preview_fanficfare_once(normalized, account)
        try:
            config = self._build_config(normalized, account)
            adapter = adapters.getAdapter(config, normalized)
            story = self._get_story_metadata(adapter, normalized, get_cover=False)
            chapters = [
                ChapterPreview(index=index, title=chapter["title"])
                for index, chapter in enumerate(adapter.get_chapters(), 1)
            ]
        except exceptions.AdultCheckRequired as exc:
            raise FicbookError(
                self._adult_message(account),
                user_message=self._adult_user_message(normalized),
            ) from exc
        except exceptions.FailedToLogin as exc:
            raise FicbookLoginError(
                "Не удалось войти в Ficbook. Проверь логин и пароль назначенного аккаунта.",
                user_message="Авторизация через Ficbook не сработала.",
            ) from exc
        except (exceptions.UnknownSite, exceptions.StoryDoesNotExist) as exc:
            raise FicbookNotFoundError("Не удалось найти фанфик по этой ссылке.") from exc
        except Exception as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Ficbook временно ограничил запросы.", technical=repr(exc)) from exc
            raise _friendly_preview_error(normalized, exc) from exc
        return StoryPreview(
            title=story.getMetadata("title") or "ficbook",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            chapters=chapters,
        )

    def _accounts_for_url(self, url: str, *, rotate: bool = False) -> tuple[FicbookAccount, ...]:
        site = _site_key(url)
        if site == "ficbook.net":
            accounts = self.accounts
        else:
            accounts = self.site_accounts.get(site, (FicbookAccount(),))
        if not rotate or len(accounts) <= 1:
            return accounts
        with self._account_lock:
            start = self._next_account_index.get(site, 0) % len(accounts)
            self._next_account_index[site] = (start + 1) % len(accounts)
        return accounts[start:] + accounts[:start]

    def _download_fanficfare_once(
        self,
        normalized: str,
        account: FicbookAccount,
        progress: Callable[[str], None] | None,
        chapter_numbers: set[int] | frozenset[int] | None,
    ) -> Story:
        try:
            adapter = self._adapter_for(normalized, account)
            story = self._get_story_metadata(adapter, normalized)
            chapter_links = adapter.get_chapters()
            selected_chapters = select_chapter_links(chapter_links, chapter_numbers)
            chapters: list[Chapter] = []
            for position, (chapter_index, chapter) in enumerate(selected_chapters, 1):
                _report(progress, f"Скачиваю главы: {position}/{len(selected_chapters)}")
                self._pause_between_requests()
                chapters.append(
                    Chapter(
                        title=chapter.get("title") or f"Глава {chapter_index}",
                        html=self._fanficfare_chapter_html(
                            adapter, normalized, chapter["url"], chapter_index, position,
                            len(selected_chapters), progress,
                        ),
                    )
                )
        except FicbookChapterDownloadError:
            raise
        except exceptions.AdultCheckRequired as exc:
            raise FicbookError(
                self._adult_message(account),
                user_message=self._adult_user_message(normalized),
            ) from exc
        except exceptions.FailedToLogin as exc:
            site_name = _site_display_name(normalized)
            raise FicbookLoginError(
                "Не удалось войти на сайт. Проверь логин и пароль в .env.",
                user_message=f"Авторизация через {site_name} не сработала.",
            ) from exc
        except (exceptions.UnknownSite, exceptions.StoryDoesNotExist) as exc:
            raise FicbookNotFoundError("Не удалось найти фанфик по этой ссылке.") from exc
        except Exception as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Сайт временно ограничил запросы.", technical=repr(exc)) from exc
            raise _friendly_download_error(normalized, exc) from exc
        return self._story_from_fanficfare_metadata(story, normalized, chapters)

    def _fanficfare_chapter_html(
        self, adapter: Any, url: str, chapter_url: str, chapter_index: int,
        position: int, total: int, progress: Callable[[str], None] | None,
    ) -> str:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return adapter.getChapterTextNum(chapter_url, chapter_index - 1)
            except Exception as exc:
                if not _is_ao3_transient_failure(url, exc):
                    raise
                if attempt >= self.retry_attempts:
                    raise FicbookChapterDownloadError(
                        f"AO3 не отдал главу {chapter_index} после {attempt} попыток.",
                        technical=repr(exc),
                        user_message=f"AO3 временно не отдал главу {chapter_index}. Попробуй повторить позже.",
                    ) from exc
                delay = self._retry_delay(attempt)
                _report(progress, f"AO3 не отдал главу {position}/{total}. Повторю через {delay:.0f} сек.")
                sleep(delay)
        raise RuntimeError("Unreachable AO3 chapter retry state")

    def _preview_fanficfare_once(self, normalized: str, account: FicbookAccount) -> StoryPreview:
        try:
            adapter = self._adapter_for(normalized, account)
            story = self._get_story_metadata(adapter, normalized, get_cover=False)
            chapters = [
                ChapterPreview(index=index, title=chapter.get("title") or f"Глава {index}")
                for index, chapter in enumerate(adapter.get_chapters(), 1)
            ]
        except exceptions.AdultCheckRequired as exc:
            raise FicbookError(
                self._adult_message(account),
                user_message=self._adult_user_message(normalized),
            ) from exc
        except exceptions.FailedToLogin as exc:
            site_name = _site_display_name(normalized)
            raise FicbookLoginError(
                "Не удалось войти на сайт. Проверь логин и пароль в .env.",
                user_message=f"Авторизация через {site_name} не сработала.",
            ) from exc
        except (exceptions.UnknownSite, exceptions.StoryDoesNotExist) as exc:
            raise FicbookNotFoundError("Не удалось найти фанфик по этой ссылке.") from exc
        except Exception as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Сайт временно ограничил запросы.", technical=repr(exc)) from exc
            raise _friendly_preview_error(normalized, exc) from exc
        return StoryPreview(
            title=story.getMetadata("title") or "fanfic",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            chapters=chapters,
        )

    def _adapter_for(self, url: str, account: FicbookAccount) -> Any:
        config = self._build_config(url, account)
        return adapters.getAdapter(config, url)

    def _story_from_fanficfare_metadata(self, story: Any, source_url: str, chapters: list[Chapter]) -> Story:
        status = self._status_text(story.getMetadata("status"))
        published = self._safe_text(story.getMetadata("datePublished"))
        updated = self._safe_text(story.getMetadata("dateUpdated"))
        finish_date = updated if status == "завершён" else ""
        return Story(
            title=story.getMetadata("title") or "fanfic",
            author=story.getMetadata("author") or "unknown",
            source_url=source_url,
            description=story.getMetadata("description") or "",
            annotation_html=self._build_generic_annotation_html(story, source_url, status, published, finish_date),
            language=story.getMetadata("language") or "ru",
            published=published,
            updated=updated,
            start_date=published,
            finish_date=finish_date,
            status=status,
            fandoms=story.getList("category"),
            genres=story.getList("genre"),
            pairings=story.getList("ships"),
            rating=story.getMetadata("rating") or "",
            word_count=self._safe_text(story.getMetadata("numWords")).replace(",", " "),
            cover=self._extract_cover_image(story),
            chapters=chapters,
        )

    def _retry_delay(self, attempt: int) -> float:
        delay = self.retry_base_delay * (2 ** (attempt - 1))
        return min(delay, self.retry_max_delay) if self.retry_max_delay else delay

    def _pause_between_requests(self) -> None:
        if self.request_delay:
            sleep(self.request_delay)

    def _download_hogwartsnet_once(
        self,
        normalized: str,
        account: FicbookAccount,
        progress: Callable[[str], None] | None,
        chapter_numbers: set[int] | frozenset[int] | None,
    ) -> Story:
        opener = self._hogwartsnet_opener(normalized, account)
        soup = self._hogwartsnet_soup(normalized, opener)
        if self._hogwartsnet_is_locked(soup):
            raise FicbookError(
                self._hogwartsnet_adult_message(account),
                user_message=self._adult_user_message(normalized),
            )
        metadata = self._hogwartsnet_metadata(soup, normalized)
        previews = self._hogwartsnet_chapter_previews(soup, normalized)
        selected_chapters = select_chapter_links(
            [{"title": chapter.title, "url": self._hogwartsnet_chapter_url(normalized, chapter.index)} for chapter in previews],
            chapter_numbers,
        )
        chapters: list[Chapter] = []
        for position, (chapter_index, chapter) in enumerate(selected_chapters, 1):
            _report(progress, f"Скачиваю главы: {position}/{len(selected_chapters)}")
            self._pause_between_requests()
            chapter_soup = self._hogwartsnet_soup(chapter["url"], opener)
            if self._hogwartsnet_is_locked(chapter_soup):
                raise FicbookError(
                    self._hogwartsnet_adult_message(account),
                    user_message=self._adult_user_message(normalized),
                )
            title, html = self._hogwartsnet_chapter(chapter_soup, chapter.get("title") or f"Глава {chapter_index}")
            chapters.append(Chapter(title=title, html=html))
        return Story(
            title=metadata["title"],
            author=metadata["author"],
            source_url=normalized,
            description=metadata["description"],
            annotation_html=metadata["annotation_html"],
            language="ru",
            start_date=metadata["start_date"],
            finish_date=metadata["finish_date"],
            status=metadata["status"],
            fandoms=metadata["fandoms"],
            genres=metadata["genres"],
            rating=metadata["rating"],
            chapters=chapters,
        )

    def _preview_hogwartsnet_once(self, normalized: str, account: FicbookAccount) -> StoryPreview:
        opener = self._hogwartsnet_opener(normalized, account)
        soup = self._hogwartsnet_soup(normalized, opener)
        if self._hogwartsnet_is_locked(soup):
            raise FicbookError(
                self._hogwartsnet_adult_message(account),
                user_message=self._adult_user_message(normalized),
            )
        metadata = self._hogwartsnet_metadata(soup, normalized)
        return StoryPreview(
            title=metadata["title"],
            author=metadata["author"],
            source_url=normalized,
            chapters=self._hogwartsnet_chapter_previews(soup, normalized),
        )

    def _hogwartsnet_opener(self, url: str, account: FicbookAccount) -> Any:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        if account.login and account.password:
            payload = urlencode({"id": account.login, "pwd": account.password}).encode("cp1251")
            self._hogwartsnet_open(opener, url, data=payload)
        return opener

    def _hogwartsnet_soup(self, url: str, opener: Any) -> BeautifulSoup:
        return BeautifulSoup(self._hogwartsnet_open(opener, url), "lxml")

    def _hogwartsnet_open(self, opener: Any, url: str, data: bytes | None = None) -> str:
        request = Request(
            url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 ficbook-downloader-bot",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                return response.read().decode("cp1251", "replace")
        except (HTTPError, URLError) as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Hogwartsnet временно ограничил запросы.", technical=repr(exc)) from exc
            raise _friendly_download_error(url, exc) from exc

    def _hogwartsnet_metadata(self, soup: BeautifulSoup, source_url: str) -> dict[str, Any]:
        head = soup.select_one("td.fichead")
        if head is None:
            raise FicbookNotFoundError("Не удалось найти фанфик по этой ссылке.")
        title_node = head.select_one('a[href*="ffshowfic"] b') or head.select_one('a[href*="ffshowfic"]')
        author_node = head.select_one('a[href*="member.php"]')
        status_node = head.find("i")
        description_node = head.find("div")
        raw_text = self._safe_text(head.get_text(" ", strip=True))
        title = self._tag_text(title_node) if isinstance(title_node, Tag) else "hogwartsnet"
        author = self._tag_text(author_node) if isinstance(author_node, Tag) else "unknown"
        status = self._status_text(self._tag_text(status_node) if isinstance(status_node, Tag) else "")
        start_date = self._metadata_value(raw_text, "Начало")
        finish_date = self._metadata_value(raw_text, "Окончание")
        updated = self._metadata_value(raw_text, "Обновление")
        if status == "завершён" and not finish_date:
            finish_date = updated
        fandoms = self._hogwartsnet_fandoms(head)
        genres = self._hogwartsnet_genres(raw_text)
        rating = self._hogwartsnet_rating(raw_text)
        description = self._render_preline_container(description_node) if isinstance(description_node, Tag) else ""
        annotation_html = self._hogwartsnet_annotation_html(
            title,
            source_url,
            author,
            status,
            start_date,
            finish_date,
            fandoms,
            genres,
            rating,
            description,
        )
        return {
            "title": title,
            "author": author,
            "description": description,
            "annotation_html": annotation_html,
            "status": status,
            "start_date": start_date,
            "finish_date": finish_date,
            "fandoms": fandoms,
            "genres": genres,
            "rating": rating,
        }

    def _hogwartsnet_chapter_previews(self, soup: BeautifulSoup, source_url: str) -> list[ChapterPreview]:
        selector = soup.select_one("td.fichead_chapters select[name='chapter']")
        if selector:
            chapters = [
                ChapterPreview(index=int(option.get("value")), title=self._tag_text(option))
                for option in selector.find_all("option")
                if self._safe_text(option.get("value")).isdigit()
            ]
            if chapters:
                return chapters
        total = self._metadata_value(soup.get_text(" ", strip=True), "Глав")
        if total.isdigit():
            return [ChapterPreview(index=index, title=f"глава {index}") for index in range(1, int(total) + 1)]
        return [ChapterPreview(index=1, title="глава 1")]

    def _hogwartsnet_chapter_url(self, source_url: str, chapter_index: int) -> str:
        parts = urlsplit(source_url)
        query = parse_qs(parts.query)
        query["chapter"] = [str(chapter_index)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))

    def _hogwartsnet_chapter(self, soup: BeautifulSoup, fallback_title: str) -> tuple[str, str]:
        node = soup.select_one("#chap_text")
        if node is None:
            raise FicbookError("Не удалось извлечь главу Hogwartsnet.")
        title = fallback_title
        title_node = node.find(["center", "h1", "h2"])
        if isinstance(title_node, Tag) and self._tag_text(title_node):
            title = self._tag_text(title_node)
            title_node.decompose()
        return title, self._render_preline_container(node)

    def _hogwartsnet_is_locked(self, soup: BeautifulSoup) -> bool:
        text = self._safe_text(soup.get_text(" ", strip=True)).lower()
        return "текст фанфика доступен только зарегистрированным пользователям старше 18 лет" in text

    def _hogwartsnet_adult_message(self, account: FicbookAccount) -> str:
        if account.login and account.password:
            return "Hogwartsnet не отдал 18+ текст после авторизации. Проверь HOGWARTSNET_LOGIN и HOGWARTSNET_PASSWORD."
        return "Для 18+ фанфиков Hogwartsnet укажи HOGWARTSNET_LOGIN и HOGWARTSNET_PASSWORD в .env."

    def _build_config(self, url: str, account: FicbookAccount) -> Configuration:
        config = Configuration(adapters.getConfigSectionsFor(url), "epub")
        config.read([str(self.defaults_ini)])
        try:
            config.add_section("overrides")
        except DuplicateSectionError:
            pass
        for key, value in {
            "is_adult": "true",
            "include_images": "true",
            "replace_text_formatting": "true",
            "username": account.login,
            "password": account.password,
        }.items():
            if value:
                config.set("overrides", key, value)
        if _site_key(url) == "archiveofourown.org":
            config.set("overrides", "use_view_full_work", "false")
        return config

    def _get_story_metadata(self, adapter: Any, url: str, *, get_cover: bool = True) -> Any:
        try:
            return adapter.getStoryMetadataOnly(get_cover=get_cover)
        except AttributeError as exc:
            if "NoneType" not in str(exc) or "find" not in str(exc):
                raise
            if getattr(adapter, "getSiteDomain", lambda: "")() != "ficbook.net":
                raise
            if not adapter.get_chapters() or not adapter.story.getMetadata("title"):
                raise
            logger.warning("Recovering Ficbook metadata after FanFicFare parser error: %s", exc)
            soup = adapter.make_soup(adapter.get_request(url))
            self._fill_optional_ficbook_metadata(adapter, soup)
            adapter.story.clear_processed_metadata_cache()
            adapter.metadataDone = True
            return adapter.story

    def _fill_optional_ficbook_metadata(self, adapter: Any, soup: BeautifulSoup) -> None:
        story = adapter.story
        rating = soup.select_one('header div[class*="badge-rating-"] span')
        if rating:
            story.setMetadata("rating", self._tag_text(rating))
        if not story.getMetadata("classification"):
            direction = soup.select_one('div[class^="badge-with-icon direction"] span.badge-text')
            if direction:
                story.setMetadata("classification", self._tag_text(direction))
        status = self._status_from_soup(soup)
        if status:
            story.setMetadata("status", status)
        self._fill_date_metadata(story, soup)
        summary = soup.find("div", itemprop="description")
        if isinstance(summary, Tag) and not story.getMetadata("description"):
            summary["class"] = [*summary.get("class", []), "part_text"]
            adapter.setDescription(adapter.url, summary)
        comment = soup.find("div", {"class": "js-public-beta-author-comment"})
        if isinstance(comment, Tag) and not story.getMetadata("authorcomment"):
            comment["class"] = [*comment.get("class", []), "part_text"]
            story.setMetadata("authorcomment", comment)
        self._fill_pairing_metadata(story, soup)
        self._fill_size_metadata(story, soup)
        self._fill_cover_image(adapter, soup)
        story.clear_processed_metadata_cache()

    def _fill_pairing_metadata(self, story: Any, soup: BeautifulSoup) -> None:
        label = soup.find(string=re.compile(r"П[эе]йринг\s+и\s+персонажи", re.I))
        container = label.parent.find_next("div") if label and isinstance(label.parent, Tag) else None
        if not isinstance(container, Tag):
            return
        existing = set(story.getList("ships"))
        for link in container.find_all("a", href=re.compile(r"/pairings/")):
            value = self._tag_text(link)
            if value and value not in existing:
                story.addToList("ships", value)
                existing.add(value)

    def _fill_size_metadata(self, story: Any, soup: BeautifulSoup) -> None:
        if story.getMetadata("pages") and story.getMetadata("numWords"):
            return
        size_text = self._size_source_text(soup)
        compact = re.sub(r"[\s\xa0]+", "", size_text.lower())
        if not story.getMetadata("pages"):
            pages = re.search(r"(\d+)(?:страниц|страницы|страница)", compact)
            if pages:
                story.setMetadata("pages", pages.group(1))
        if not story.getMetadata("numWords"):
            words = re.search(r"(\d+)(?:слов|слова|слово)", compact)
            if words:
                story.setMetadata("numWords", words.group(1))

    def _fill_date_metadata(self, story: Any, soup: BeautifulSoup) -> None:
        start_date = self._date_by_label(
            soup,
            (
                "Дата публикации",
                "Начало публикации",
                "Опубликовано",
                "Опубликован",
                "Размещено",
                "Размещён",
                "Размещен",
            ),
        )
        finish_date = self._date_by_label(
            soup,
            (
                "Дата завершения",
                "Дата окончания",
                "Окончание публикации",
                "Завершено",
                "Завершён",
                "Завершен",
            ),
        )
        if start_date:
            story.setMetadata("dateStart", start_date)
            if not story.getMetadata("datePublished"):
                story.setMetadata("datePublished", start_date)
        if not finish_date and self._status_text(story.getMetadata("status")) == "завершён":
            finish_date = self._safe_text(story.getMetadata("dateUpdated")) or self._date_by_label(
                soup,
                ("Дата обновления", "Обновлено", "Обновлён", "Обновлен"),
            )
        if finish_date:
            story.setMetadata("dateFinish", finish_date)

    def _status_from_soup(self, soup: BeautifulSoup) -> str:
        for node in soup.select('[class*="badge-status"]'):
            class_text = " ".join(str(item) for item in node.get("class", []))
            text = self._tag_text(node)
            normalized = f"{class_text} {text}".lower().replace("_", "-")
            if any(marker in normalized for marker in ("finished", "completed", "заверш", "закончен")):
                return "Completed"
            if any(marker in normalized for marker in ("in-progress", "process", "в процессе", "пишется")):
                return "In-Progress"
            if any(marker in normalized for marker in ("frozen", "заморож")):
                return text or "Заморожен"
        return ""

    def _date_by_label(self, soup: BeautifulSoup, labels: tuple[str, ...]) -> str:
        label_pattern = "|".join(re.escape(label) for label in labels)
        pattern = re.compile(rf"(?:{label_pattern})\s*:?\s*({DATE_VALUE_PATTERN})", re.I)
        match = pattern.search(soup.get_text(" ", strip=True).replace("\xa0", " "))
        if match:
            return match.group(1)
        label = soup.find(string=re.compile(label_pattern, re.I))
        if label is None:
            return ""
        candidates = [getattr(label, "parent", None)]
        parent = candidates[0]
        if isinstance(parent, Tag):
            candidates.extend([parent.find_next("span"), parent.find_next("div"), parent.parent])
        for node in candidates:
            if not isinstance(node, Tag):
                continue
            text = self._safe_text(node.get_text(" ", strip=True))
            match = re.search(DATE_VALUE_PATTERN, text)
            if match:
                return match.group(0)
        return ""

    def _size_source_text(self, soup: BeautifulSoup) -> str:
        label = soup.find(string=re.compile(r"Размер:", re.I))
        if label:
            nodes = [getattr(label, "parent", None)]
            parent = nodes[0]
            if isinstance(parent, Tag):
                nodes.extend([parent.find_next("div"), parent.parent])
            text = " ".join(
                node.get_text(" ", strip=True)
                for node in nodes
                if isinstance(node, Tag)
            )
            if text:
                return text
        return soup.get_text(" ", strip=True)

    def _fill_cover_image(self, adapter: Any, soup: BeautifulSoup) -> None:
        if getattr(adapter.story, "cover", None):
            return
        cover_url = self._cover_url_from_soup(soup)
        if not cover_url:
            return
        try:
            adapter.setCoverImage(adapter.url, cover_url)
        except Exception:
            logger.debug("Failed to fetch Ficbook cover image", exc_info=True)

    def _cover_url_from_soup(self, soup: BeautifulSoup) -> str:
        cover = soup.find("fanfic-cover", {"class": "jsVueComponent"})
        if isinstance(cover, Tag):
            for attr in ("src-original", "src", ":src"):
                value = self._safe_text(cover.get(attr))
                if value:
                    return value
        meta = soup.find("meta", property="og:image")
        if isinstance(meta, Tag):
            return self._safe_text(meta.get("content"))
        return ""

    def _tag_text(self, node: Tag) -> str:
        return node.get_text(" ", strip=True).replace("\xa0", " ")

    def _adult_message(self, account: FicbookAccount) -> str:
        if account.login and account.password:
            return "Сайт запросил подтверждение 18+, но доступ получить не удалось. Проверь логин и пароль в .env."
        return "Для 18+ или закрытых фанфиков укажи логин и пароль нужного сайта в .env."

    def _adult_user_message(self, url: str) -> str:
        return f"Не удалось получить доступ к фанфику 18+ через {_site_display_name(url)}."

    def _load_chapter_html(self, adapter: Any, url: str) -> str:
        soup = adapter.make_soup(adapter.get_request(url))
        chapter = soup.find("div", {"id": "content"}) or soup.find("div", {"class": "public_beta_disabled"})
        if chapter is None:
            raise FicbookError(
                "Не удалось скачать одну из глав. Попробуй повторить позже.",
                technical=f"Failed to extract chapter: {url}",
            )
        note_before = soup.select_one("div.part-comment-top div.text-preline")
        note_after = soup.select_one("div.part-comment-bottom div.text-preline")
        parts: list[str] = []
        if note_before:
            parts.append(self._chapter_note_html(note_before))
        parts.append(self._chapter_content_html(soup, chapter))
        if note_after:
            parts.append(self._chapter_note_html(note_after))
        return "".join(parts)

    def _chapter_note_html(self, note: Tag) -> str:
        return (
            "<p><b>Примечания:</b></p>"
            f"{self._render_preline_container(note)}"
        )

    def _build_annotation_html(self, story: Any, page_soup: BeautifulSoup, source_url: str) -> str:
        parts: list[str] = [
            f"<p>{self._text_line(story.getMetadata('title'), source_url)}</p>",
            f"<p>Направленность: {self._safe_text(story.getMetadata('classification'))}</p>",
            f"<p>{self._text_line(story.getMetadata('author'), story.getMetadata('authorUrl'), 'Автор')}</p>",
        ]
        self._append_label_value(parts, "Фэндом", story.getList("category"))
        if story.getMetadata("rating"):
            parts.append(f"<p>Рейтинг: {self._safe_text(story.getMetadata('rating'))}</p>")
        if story.getMetadata("numChapters"):
            parts.append(f"<p>Кол-во частей:{self._safe_text(story.getMetadata('numChapters'))}</p>")
        status = self._status_text(story.getMetadata("status"))
        if status:
            parts.append("<p>Статус:</p>")
            parts.append(f"<p>{status}</p>")
        start_date = self._safe_text(story.getMetadata("dateStart") or story.getMetadata("datePublished"))
        if start_date:
            parts.append("<p>Дата начала:</p>")
            parts.append(f"<p>{start_date}</p>")
        finish_date = self._safe_text(story.getMetadata("dateFinish"))
        if finish_date:
            parts.append("<p>Дата завершения:</p>")
            parts.append(f"<p>{finish_date}</p>")
        self._append_label_value(parts, "Метки", story.getList("genre"))
        description = story.getMetadata("description")
        if description:
            parts.append("<p>Описание:</p>")
            parts.append(self._render_preline_html(description))
        author_comment = story.getMetadata("authorcomment")
        if author_comment:
            parts.append("<p>Примечания:</p>")
            parts.append(self._render_preline_html(author_comment))
        publication = self._publication_notice(page_soup)
        if publication:
            parts.append("<p>Публикация на других ресурсах:</p>")
            parts.append(f"<p>{publication}</p>")
        return "".join(part for part in parts if part)

    def _build_generic_annotation_html(
        self,
        story: Any,
        source_url: str,
        status: str,
        start_date: str,
        finish_date: str,
    ) -> str:
        parts: list[str] = [
            f"<p>{self._text_line(story.getMetadata('title'), source_url)}</p>",
            f"<p>{self._text_line(story.getMetadata('author'), story.getMetadata('authorUrl'), 'Автор')}</p>",
        ]
        if status:
            parts.append("<p>Статус:</p>")
            parts.append(f"<p>{status}</p>")
        if start_date:
            parts.append("<p>Дата начала:</p>")
            parts.append(f"<p>{start_date}</p>")
        if finish_date:
            parts.append("<p>Дата завершения:</p>")
            parts.append(f"<p>{finish_date}</p>")
        self._append_label_value(parts, "Фэндом", story.getList("category"))
        self._append_label_value(parts, "Метки", story.getList("genre"))
        description = story.getMetadata("description")
        if description:
            parts.append("<p>Описание:</p>")
            parts.append(self._render_preline_html(description))
        return "".join(part for part in parts if part)

    def _hogwartsnet_annotation_html(
        self,
        title: str,
        source_url: str,
        author: str,
        status: str,
        start_date: str,
        finish_date: str,
        fandoms: list[str],
        genres: list[str],
        rating: str,
        description: str,
    ) -> str:
        parts: list[str] = [
            f"<p>{self._text_line(title, source_url)}</p>",
            f"<p>Автор: {author}</p>",
        ]
        if status:
            parts.extend(["<p>Статус:</p>", f"<p>{status}</p>"])
        if start_date:
            parts.extend(["<p>Дата начала:</p>", f"<p>{start_date}</p>"])
        if finish_date:
            parts.extend(["<p>Дата завершения:</p>", f"<p>{finish_date}</p>"])
        if fandoms:
            self._append_label_value(parts, "Фэндом", fandoms)
        if genres:
            self._append_label_value(parts, "Метки", genres)
        if rating:
            parts.append(f"<p>Рейтинг: {rating}</p>")
        if description:
            parts.append("<p>Описание:</p>")
            parts.append(description)
        return "".join(parts)

    def _chapter_content_html(self, soup: BeautifulSoup, chapter: Tag) -> str:
        footnotes = self._text_footnotes(soup)
        rendered_notes: list[tuple[int, str]] = []
        assigned: dict[str, int] = {}
        for reference in chapter.select("span.footnote[id], a.footnote[id]"):
            note_id = self._safe_text(reference.get("id"))
            raw_note = footnotes.get(note_id) or self._safe_text(
                reference.get("data-original-title") or reference.get("title")
            )
            if not raw_note:
                continue
            number = assigned.get(note_id)
            if number is None:
                number = len(rendered_notes) + 1
                assigned[note_id] = number
                rendered_notes.append((number, self._sanitize_note_html(raw_note)))
            marker = soup.new_tag("sup", attrs={"class": "footnote-ref"})
            marker.string = f"[{number}]"
            reference.replace_with(marker)
        parts = [self._render_preline_container(chapter)]
        if rendered_notes:
            parts.append('<div class="interactive-footnotes"><p><b>Интерактивные примечания:</b></p>')
            for number, note_html in rendered_notes:
                parts.extend((f"<p><b>[{number}]</b></p>", self._render_preline_html(note_html)))
            parts.append("</div>")
        return "".join(parts)

    def _text_footnotes(self, soup: BeautifulSoup) -> dict[str, str]:
        decoder = json.JSONDecoder()
        for script in soup.find_all("script"):
            source = script.string or script.get_text()
            marker = source.find("textFootnotes")
            start = source.find("{", marker) if marker >= 0 else -1
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(source[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return {str(key): str(item) for key, item in value.items() if item is not None}
        return {}

    def _sanitize_note_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        for node in soup.select("audio, iframe, img, script, style, video"):
            node.decompose()
        root = soup.body or soup
        return "".join(str(node) for node in root.contents).strip()

    def _metadata_value(self, text: str, label: str) -> str:
        pattern = re.compile(rf"{re.escape(label)}:\s*([^|]+)", re.I)
        match = pattern.search(text.replace("\xa0", " "))
        return self._safe_text(match.group(1)) if match else ""

    def _hogwartsnet_fandoms(self, head: Tag) -> list[str]:
        values = []
        for link in head.select('a[href*="fandoms="]'):
            text = self._tag_text(link)
            if text and text not in values:
                values.append(text)
        return values

    def _hogwartsnet_genres(self, text: str) -> list[str]:
        cleaned = text.replace("\xa0", " ")
        before_size = cleaned.split("Размер:", 1)[0]
        if "<" in before_size:
            before_size = BeautifulSoup(before_size, "lxml").get_text(" ", strip=True)
        chunks = [self._safe_text(chunk) for chunk in before_size.split("||")]
        return [chunk for chunk in chunks[-3:] if chunk and not chunk.startswith(("Mир ", "автора "))]

    def _hogwartsnet_rating(self, text: str) -> str:
        for token in ("NC-21", "NC-17", "R", "PG-13", "PG", "G"):
            if re.search(rf"(?:^|\s|\|){re.escape(token)}(?:\s|\||$)", text):
                return token
        return ""

    def _append_label_value(self, parts: list[str], label: str, values: list[str]) -> None:
        text = self._join_values(values)
        if text:
            parts.append(f"<p>{label}:</p>")
            parts.append(f"<p>{text}</p>")

    def _join_values(self, *groups: list[str]) -> str:
        values = [self._safe_text(item) for group in groups for item in group if self._safe_text(item)]
        return ", ".join(values)

    def _size_text(self, story: Any) -> str:
        pages = self._safe_text(story.getMetadata("pages"))
        words = self._safe_text(story.getMetadata("numWords")).replace(",", " ")
        if pages and words:
            return f"{pages} страница, {words} слов"
        return pages or words

    def _extract_cover_image(self, story: Any) -> CoverImage | None:
        for image in story.getImgUrls():
            newsrc = self._safe_text(image.get("newsrc"))
            if not newsrc.startswith("images/cover."):
                continue
            data = image.get("data")
            if not isinstance(data, bytes) or not data or len(data) > MAX_COVER_BYTES:
                continue
            mime = self._safe_text(image.get("mime")) or mimetypes.guess_type(newsrc)[0] or ""
            if not mime.startswith("image/"):
                continue
            extension = self._safe_text(image.get("ext")) or Path(newsrc).suffix.lstrip(".") or "jpg"
            extension = re.sub(r"[^a-zA-Z0-9]", "", extension.lower()) or "jpg"
            return CoverImage(data=data, mime=mime, extension=extension)
        return None

    def _publication_notice(self, page_soup: BeautifulSoup) -> str:
        label = page_soup.find(string=re.compile(r"Публикация на других ресурсах", re.I))
        if label is None:
            return ""
        for sibling in getattr(label.parent, "next_siblings", []):
            text = self._safe_text(getattr(sibling, "get_text", lambda **_: str(sibling))(separator=" ", strip=True))
            if text:
                return text
        return ""

    def _render_preline_container(self, node: Tag) -> str:
        return self._render_preline_html(node.decode_contents())

    def _render_preline_html(self, html: str) -> str:
        raw = re.sub(r"(?i)<br\s*/?>", "\n", html).replace("\r\n", "\n").replace("\r", "\n")
        blocks: list[str] = []
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            blocks.append(stripped if self._is_block_html(stripped) else f"<p>{stripped}</p>")
        return "".join(blocks)

    def _is_block_html(self, html: str) -> bool:
        if "<" not in html or ">" not in html:
            return False
        soup = BeautifulSoup(html, "lxml")
        root = soup.body or soup
        nodes = [node for node in root.contents if not isinstance(node, NavigableString) or node.strip()]
        return bool(nodes) and all(isinstance(node, Tag) and node.name in BLOCK_HTML_TAGS for node in nodes)

    def _status_text(self, value: Any) -> str:
        raw = self._safe_text(value)
        status = raw.lower().replace("_", "-")
        if any(marker in status for marker in ("completed", "complete", "finished", "заверш", "закончен")):
            return "завершён"
        if any(marker in status for marker in ("in progress", "in-progress", "process", "в процессе", "в работе", "пишется")):
            return "в процессе"
        if any(marker in status for marker in ("frozen", "заморож")):
            return "заморожен"
        return raw

    def _text_line(self, text: str, url: str = "", label: str = "") -> str:
        value = self._safe_text(text)
        link = self._safe_text(url)
        prefix = f"{label}: " if label else ""
        if value and link:
            return f"{prefix}{value} ({link})"
        return f"{prefix}{value or link}"

    def _safe_text(self, value: Any) -> str:
        return str(value or "").replace("\xa0", " ").strip()


def _friendly_download_error(url: str, error: Exception) -> FicbookError:
    site_name = _site_display_name(url)
    if _is_ao3_shields_error(url, error):
        return FicbookError(
            "AO3 включил браузерную проверку защиты. Бот не может пройти ее как обычный браузер, "
            "поэтому этот фанфик сейчас не скачать. Попробуй повторить позже.",
            technical=repr(error),
        )
    if _is_network_error(error):
        return FicbookError(
            f"{site_name} сейчас не отвечает или соединение оборвалось. Попробуй повторить позже.",
            technical=repr(error),
        )
    return FicbookError(
        f"Не удалось скачать фанфик с {site_name}. Возможно, сайт изменил страницу или временно отдает неполный ответ.",
        technical=repr(error),
    )


def _friendly_preview_error(url: str, error: Exception) -> FicbookError:
    site_name = _site_display_name(url)
    if _is_ao3_shields_error(url, error):
        return FicbookError(
            "AO3 включил браузерную проверку защиты, поэтому список глав сейчас получить не удалось. "
            "Попробуй повторить позже.",
            technical=repr(error),
        )
    if _is_network_error(error):
        return FicbookError(
            f"{site_name} сейчас не отвечает, поэтому список глав получить не удалось. Попробуй повторить позже.",
            technical=repr(error),
        )
    return FicbookError(
        f"Не удалось получить список глав с {site_name}. Возможно, сайт изменил страницу или временно отдает неполный ответ.",
        technical=repr(error),
    )


def classify_ficbook_homepage(status: int, html: str) -> FicbookSiteStatus:
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True)).lower()
    unavailable_markers = (
        "сайт временно недоступен",
        "сайт сейчас не работает",
        "сайт в данный момент не работает",
        "фикбук временно недоступен",
        "ведутся технические работы",
        "проводим технические работы",
        "скоро всё починим",
        "скоро все починим",
        "скоро вернёмся",
        "скоро вернемся",
        "service unavailable",
        "temporarily unavailable",
        "under maintenance",
    )
    if 500 <= status < 600 or any(marker in text for marker in unavailable_markers):
        return FicbookSiteStatus.UNAVAILABLE
    if 200 <= status < 400:
        return FicbookSiteStatus.AVAILABLE
    return FicbookSiteStatus.UNKNOWN


def _is_network_error(error: Exception) -> bool:
    if isinstance(error, (HTTPError, URLError, TimeoutError, ConnectionError)):
        return True
    text = str(error).lower()
    return any(marker in text for marker in ("timed out", "timeout", "connection", "temporarily unavailable"))


def _is_ao3_shields_error(url: str, error: Exception) -> bool:
    if _site_key(url) != "archiveofourown.org":
        return False
    text = repr(error).lower()
    return any(marker in text for marker in ("shields are up", "not a robot", "cloudflare", "challenge-platform"))


def _is_ao3_transient_error(url: str, error: FicbookError) -> bool:
    return not isinstance(error, FicbookChapterDownloadError) and _is_ao3_transient_failure(url, error)


def _is_ao3_transient_failure(url: str, error: Exception) -> bool:
    if _site_key(url) != "archiveofourown.org":
        return False
    text = f"{error!r} {getattr(error, 'technical', '')}".lower()
    return any(
        marker in text
        for marker in ("525", "shields are up", "not a robot", "cloudflare", "challenge-platform")
    )


def select_chapter_links(
    chapter_links: list[dict[str, Any]],
    chapter_numbers: set[int] | frozenset[int] | None,
) -> list[tuple[int, dict[str, Any]]]:
    indexed = list(enumerate(chapter_links, 1))
    if chapter_numbers is None:
        return indexed
    if not chapter_numbers:
        raise FicbookError("Не выбрано ни одной главы.")
    missing = sorted(number for number in chapter_numbers if number < 1 or number > len(chapter_links))
    if missing:
        raise FicbookError(f"В фанфике {len(chapter_links)} глав, нельзя скачать: {', '.join(map(str, missing))}.")
    selected = set(chapter_numbers)
    return [(index, chapter) for index, chapter in indexed if index in selected]


def _is_rate_limited(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "too many requests" in text


def _report(progress: Callable[[str], None] | None, text: str) -> None:
    if progress:
        progress(text)
