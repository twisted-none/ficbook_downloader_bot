from __future__ import annotations

from collections.abc import Callable
from configparser import DuplicateSectionError
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from html import escape
from http.cookiejar import CookieJar
import json
import logging
import mimetypes
from pathlib import Path
import re
import ssl
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

import fanficfare
from bs4 import BeautifulSoup, NavigableString, Tag
from fanficfare import adapters, exceptions
from fanficfare.configurable import Configuration
from fanficfare.exceptions import HTTPErrorFFF

from src.sources.ao3 import AO3WorkUnavailableError, download_html_chapters
from src.sources.ao3_status import AO3MaintenanceError
from src.sources.collections import FicbookCollection, load_collection
from src.sources.fikbooks import FikbooksClient, FikbooksError, FikbooksNotFoundError, FikbooksRateLimitError
from src.sources.zahleb import ZahlebClient, ZahlebError, ZahlebNotFoundError, ZahlebRateLimitError
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
from src.core.progress import report_title
from src.core.covers import is_ficbook_placeholder
from src.core.limits import (ResourceLimitError, bounded_operation, check_chapter_count,
                             bounded_sleep as sleep, check_deadline, read_bounded,
                             request_timeout, validate_preview, validate_story)
from src.sources.fanficfare_http import SafeSourceConfiguration
from src.sources.safe_http import (SafeHTTPSHandler, SafeProxyHandler, SafeRedirectHandler,
                                   build_safe_opener, is_cloudflare_challenge)
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


def _require_hogwartsnet_https(url: str) -> None:
    try:
        parts = urlsplit(url)
        safe = (
            parts.scheme == "https"
            and parts.hostname in {"hogwartsnet.ru", "www.hogwartsnet.ru"}
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and "\\" not in url
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        safe = False
    if not safe:
        raise FicbookError(
            "Не удалось установить безопасное соединение с Hogwartsnet. Попробуй повторить позже.",
            technical="Hogwartsnet rejected an insecure URL or redirect.",
        )


class _HogwartsnetHTTPSRedirectHandler(SafeRedirectHandler):
    def __init__(self) -> None:
        super().__init__(frozenset({'hogwartsnet.ru', 'www.hogwartsnet.ru'}))

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str,
    ) -> Any:
        _require_hogwartsnet_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class FicbookPaidContentError(FicbookError):
    """The requested work requires a purchase that the bot does not perform."""


class FicbookSiteUnavailableError(FicbookError):
    """Ficbook is serving a maintenance page instead of the normal site."""


class AO3SiteUnavailableError(FicbookError):
    """AO3 explicitly reports maintenance, rather than a generic HTTP failure."""


class FicbookChapterDownloadError(FicbookError):
    """A chapter stayed unavailable after source-specific retries."""


class FicbookSiteStatus(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CHALLENGED = "challenged"
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


def urlopen(request: Request, timeout: float = 30) -> Any:
    return build_safe_opener(frozenset({'ficbook.net', 'www.ficbook.net'})).open(
        request, timeout=request_timeout(timeout),
    )


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
        self.fikbooks = FikbooksClient(request_delay=request_delay)
        self.zahleb = ZahlebClient(request_delay=request_delay)
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
                if is_cloudflare_challenge(response.headers):
                    return FicbookSiteStatus.CHALLENGED
                status = int(getattr(response, "status", 200))
                html = read_bounded(response, FICBOOK_HOME_MAX_BYTES).decode("utf-8", "ignore")
        except HTTPError as exc:
            with exc:
                if is_cloudflare_challenge(exc.headers):
                    return FicbookSiteStatus.CHALLENGED
                try:
                    html = read_bounded(exc, FICBOOK_HOME_MAX_BYTES).decode("utf-8", "ignore")
                except OSError:
                    html = ""
            return classify_ficbook_homepage(exc.code, html)
        except ResourceLimitError:
            raise
        except Exception:
            logger.warning("Could not check Ficbook availability", exc_info=True)
            return FicbookSiteStatus.UNKNOWN
        return classify_ficbook_homepage(status, html)

    def _ficbook_failure(self, error: Exception, *, preview: bool = False) -> FicbookError:
        if _is_rate_limited(error):
            return FicbookRateLimitError("Ficbook временно ограничил запросы.", technical=repr(error))
        code = error.status_code if isinstance(error, HTTPErrorFFF) else getattr(error, 'code', None)
        if code in {401, 403}:
            return FicbookError(
                "Ficbook отказал в доступе к запрошенной странице. "
                "Это может быть ограничение доступа или блокировка запроса, а не сбой сайта.",
                technical=repr(error),
            )
        status = self.ficbook_site_status()
        if status is FicbookSiteStatus.UNAVAILABLE:
            return _ficbook_site_unavailable_error(error)
        friendly = _friendly_preview_error if preview else _friendly_download_error
        result = friendly('https://ficbook.net/', error)
        result.technical = f'{error!r}; Ficbook availability: {status.value}'
        return result

    def collection(self, url: str, progress: Callable[[str], None] | None = None) -> FicbookCollection:
        return load_collection(
            url, progress, request_delay=self.request_delay,
            retry_attempts=self.retry_attempts, retry_base_delay=self.retry_base_delay,
        )

    @bounded_operation
    def download(
        self,
        url: str,
        progress: Callable[[str], None] | None = None,
        chapter_numbers: set[int] | frozenset[int] | None = None,
        account: FicbookAccount | None = None,
    ) -> Story:
        normalized = normalize_url(url)
        site_name = _site_display_name(normalized)
        attempts = 1 if _site_key(normalized) == 'archiveofourown.org' else self.retry_attempts
        last_rate_limit: FicbookRateLimitError | None = None
        accounts = (
            (account,)
            if account is not None and _is_ficbook_url(normalized)
            else self._accounts_for_url(normalized, rotate=True)
        )
        for index, account in enumerate(accounts, 1):
            for attempt in range(1, attempts + 1):
                try:
                    _report(progress, f"Скачиваю описание фанфика: аккаунт {index}/{len(accounts)}")
                    result = self._download_once(normalized, account, progress, chapter_numbers)
                    validate_story(result)
                    return result
                except FicbookRateLimitError as exc:
                    last_rate_limit = exc
                    logger.warning(
                        "%s rate limit for account %s/%s, attempt %s/%s",
                        site_name,
                        index,
                        len(accounts),
                        attempt,
                        self.retry_attempts,
                    )
                    if attempt >= attempts:
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
                    from src.sources.ao3_browser import enabled_for
                    if enabled_for(normalized, exc):
                        result = self._ao3_browser_recovery(normalized, progress, chapter_numbers)
                        validate_story(result)
                        return result
                    raise
        if last_rate_limit:
            raise FicbookRateLimitError(
                f"{site_name} временно ограничил запросы. Попробуй повторить позже.",
                technical=getattr(last_rate_limit, "technical", "") or str(last_rate_limit),
                retry_after=self.retry_max_delay or None,
            ) from last_rate_limit
        raise FicbookError("Не удалось загрузить фанфик.")

    @bounded_operation
    def preview(self, url: str, progress: Callable[[str], None] | None = None) -> StoryPreview:
        normalized = normalize_url(url)
        site_name = _site_display_name(normalized)
        attempts = 1 if _site_key(normalized) == 'archiveofourown.org' else self.retry_attempts
        last_rate_limit: FicbookRateLimitError | None = None
        accounts = self._accounts_for_url(normalized)
        for index, account in enumerate(accounts, 1):
            for attempt in range(1, attempts + 1):
                try:
                    _report(
                        progress,
                        "Введите главы, которые хотите скачать, или 0 для всех.\n"
                        "Формат: 1,2,5-10,17.\n"
                        f"Аккаунт: {index}/{len(accounts)}",
                    )
                    result = self._preview_once(normalized, account)
                    validate_preview(result)
                    return result
                except FicbookRateLimitError as exc:
                    last_rate_limit = exc
                    if attempt >= attempts:
                        break
                    delay = self._retry_delay(attempt)
                    _report(progress, f"{site_name} временно ограничил запросы. Повторю через {delay:.0f} сек.")
                    sleep(delay)
                except FicbookError as exc:
                    from src.sources.ao3_browser import enabled_for
                    if enabled_for(normalized, exc):
                        result = self._ao3_browser_recovery(normalized, progress, preview=True)
                        validate_preview(result)
                        return result
                    raise
        if last_rate_limit:
            raise FicbookError(
                f"{site_name} временно ограничил запросы. Попробуй повторить скачивание позже.",
                technical=getattr(last_rate_limit, "technical", "") or str(last_rate_limit),
            ) from last_rate_limit
        raise FicbookError("Не удалось получить список глав.")

    def _ao3_browser_recovery(self, url, progress=None, chapter_numbers=None, *, preview=False):
        from src.sources.ao3_browser import BrowserDownloadError, download_browser_work
        try:
            return download_browser_work(url, progress, chapter_numbers, preview=preview)
        except ResourceLimitError:
            raise
        except AO3MaintenanceError as exc:
            raise AO3SiteUnavailableError(str(exc)) from exc
        except AO3WorkUnavailableError as exc:
            raise FicbookError(str(exc)) from exc
        except Exception as exc:
            logger.warning('AO3 browser recovery failed (%s)', type(exc).__name__)
            text = str(exc) if isinstance(exc, BrowserDownloadError) else 'Резервное скачивание AO3 не удалось. Попробуй позже.'
            raise FicbookError(text) from exc

    def _download_once(
        self,
        normalized: str,
        account: FicbookAccount,
        progress: Callable[[str], None] | None,
        chapter_numbers: set[int] | frozenset[int] | None,
    ) -> Story:
        if _site_key(normalized) == 'zahleb.me':
            return self._zahleb_request(normalized, progress=progress, chapter_numbers=chapter_numbers)
        if _site_key(normalized) == 'fikbooks.ru':
            return self._fikbooks_request(normalized, progress=progress, chapter_numbers=chapter_numbers)
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
            report_title(progress, story.getMetadata('title'))
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
                chapters.append(Chapter.from_download(title=chapter["title"], html=self._load_chapter_html(adapter, chapter["url"]), source_url=chapter["url"]))
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
        except (FicbookError, ResourceLimitError):
            raise
        except Exception as exc:
            raise self._ficbook_failure(exc) from exc
        return Story(
            title=story.getMetadata("title") or "ficbook",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            description=story.getMetadata("description") or "",
            annotation_html=self._build_annotation_html(story, page_soup, normalized),
            annotation_complete=True,
            translator=self._safe_text(story.getMetadata("translator")),
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
            cover=self._extract_cover_image(story, source_url=normalized),
            chapters=chapters,
        )

    def _preview_once(self, normalized: str, account: FicbookAccount) -> StoryPreview:
        if _site_key(normalized) == 'zahleb.me':
            return self._zahleb_request(normalized, preview=True)
        if _site_key(normalized) == 'fikbooks.ru':
            return self._fikbooks_request(normalized, preview=True)
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
        except (FicbookError, ResourceLimitError):
            raise
        except Exception as exc:
            raise self._ficbook_failure(exc, preview=True) from exc
        return StoryPreview(
            title=story.getMetadata("title") or "ficbook",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            chapters=chapters,
        )

    def _zahleb_request(self, url: str, *, preview: bool = False,
                        progress: Callable[[str], None] | None = None,
                        chapter_numbers: set[int] | frozenset[int] | None = None) -> Story | StoryPreview:
        try:
            if preview:
                return self.zahleb.preview(url)
            return self.zahleb.download(url, progress, chapter_numbers)
        except ZahlebNotFoundError as exc:
            raise FicbookNotFoundError(str(exc)) from exc
        except ZahlebRateLimitError as exc:
            raise FicbookRateLimitError(str(exc)) from exc
        except ZahlebError as exc:
            raise FicbookError(str(exc)) from exc

    def _fikbooks_request(self, url: str, *, preview: bool = False,
                          progress: Callable[[str], None] | None = None,
                          chapter_numbers: set[int] | frozenset[int] | None = None) -> Story | StoryPreview:
        try:
            if preview:
                return self.fikbooks.preview(url)
            return self.fikbooks.download(url, progress, chapter_numbers)
        except FikbooksNotFoundError as exc:
            raise FicbookNotFoundError(str(exc)) from exc
        except FikbooksRateLimitError as exc:
            raise FicbookRateLimitError(str(exc)) from exc
        except FikbooksError as exc:
            raise FicbookError(str(exc)) from exc

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
            report_title(progress, story.getMetadata('title'))
            chapter_links = adapter.get_chapters()
            selected_chapters = select_chapter_links(chapter_links, chapter_numbers)
            if _site_key(normalized) == 'archiveofourown.org':
                downloaded = download_html_chapters(
                    adapter, normalized, chapter_links, selected_chapters,
                    story.getMetadata('description') or '', progress, self._pause_between_requests,
                )
                if downloaded is not None:
                    return self._story_from_fanficfare_metadata(story, normalized, downloaded)
            chapters: list[Chapter] = []
            for position, (chapter_index, chapter) in enumerate(selected_chapters, 1):
                _report(progress, f"Скачиваю главы: {position}/{len(selected_chapters)}")
                self._pause_between_requests()
                chapters.append(
                    Chapter.from_download(
                        title=chapter.get("title") or f"Глава {chapter_index}",
                        source_url=chapter["url"],
                        html=self._fanficfare_chapter_html(
                            adapter, normalized, chapter["url"], chapter_index, position,
                            len(selected_chapters), progress,
                        ),
                    )
                )
        except (FicbookChapterDownloadError, ResourceLimitError):
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
        check_deadline()
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
        report_title(progress, metadata['title'])
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
            chapters.append(Chapter.from_download(title=title, html=html, source_url=chapter["url"]))
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
        _require_hogwartsnet_https(url)
        opener = build_opener(
            HTTPCookieProcessor(CookieJar()),
            SafeHTTPSHandler(context=ssl.create_default_context()),
            SafeProxyHandler(),
            _HogwartsnetHTTPSRedirectHandler(),
        )
        if account.login and account.password:
            payload = urlencode({"id": account.login, "pwd": account.password}).encode("cp1251")
            self._hogwartsnet_open(opener, url, data=payload)
        return opener

    def _hogwartsnet_soup(self, url: str, opener: Any) -> BeautifulSoup:
        return BeautifulSoup(self._hogwartsnet_open(opener, url), "lxml")

    def _hogwartsnet_open(self, opener: Any, url: str, data: bytes | None = None) -> str:
        _require_hogwartsnet_https(url)
        request = Request(
            url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 ficbook-downloader-bot",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with opener.open(request, timeout=request_timeout()) as response:
                return read_bounded(response).decode("cp1251", "replace")
        except (HTTPError, URLError) as exc:
            if _is_rate_limited(exc):
                raise FicbookRateLimitError("Hogwartsnet временно ограничил запросы.", technical=repr(exc)) from exc
            if isinstance(exc, URLError) and isinstance(exc.reason, ssl.SSLError):
                raise FicbookError(
                    "Не удалось проверить защищённое соединение с Hogwartsnet. Попробуй повторить позже.",
                    technical="Hogwartsnet TLS verification failed.",
                ) from exc
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
            check_chapter_count(int(total))
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
        sections = adapters.getConfigSectionsFor(url)
        if _site_key(url) == "ficbook.net":
            config = Configuration(sections, "epub")
        else:
            config = SafeSourceConfiguration(sections, "epub", site=_site_key(url))
        config.read([str(self.defaults_ini)])
        if urlsplit(url).hostname == "ficbook.net":
            config.set("ficbook.net", "add_to_extra_valid_entries", ",translator,translatorUrl")
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
        if getattr(adapter, "getSiteDomain", lambda: "")() != "ficbook.net":
            return adapter.getStoryMetadataOnly(get_cover=get_cover)
        if not adapter.metadataDone:
            try:
                self._parse_ficbook_metadata(adapter, url, get_cover=get_cover)
            except HTTPErrorFFF as exc:
                if exc.status_code in (404, 410):
                    raise exceptions.StoryDoesNotExist(url) from exc
                raise
            adapter.metadataDone = True
        return adapter.story

    def _parse_ficbook_metadata(self, adapter: Any, url: str, *, get_cover: bool = True) -> None:
        soup = adapter.make_soup(adapter.get_request(url))
        if soup.find(id="adultCoverWarning") and not (adapter.is_adult or adapter.getConfig("is_adult")):
            raise exceptions.AdultCheckRequired(url)
        story = adapter.story
        if not story.getMetadata("title"):
            title = self._ficbook_title_from_soup(soup)
            if title:
                story.setMetadata("title", title)
        if not story.getMetadata("title"):
            raise FicbookError(
                "Не удалось прочитать название фанфика Ficbook.",
                technical=f"Failed to recover Ficbook title: {url}",
            )
        self._fill_ficbook_author_metadata(story, soup)
        self._fill_size_metadata(story, soup)
        if not adapter.get_chapters():
            self._fill_ficbook_chapter_links(adapter, soup, url)
        if not adapter.get_chapters():
            if story.getMetadata("pages") == "0" and story.getMetadata("numWords") == "0":
                raise FicbookNotFoundError(
                    "В фанфике нет доступного текста: Ficbook сообщает 0 страниц и 0 слов."
                )
            raise FicbookError(
                "Не удалось прочитать список глав Ficbook.",
                technical=f"Failed to recover Ficbook chapters: {url}",
            )
        story.setMetadata("language", "Russian")
        for key, selector in (("category", 'header a[href^="/fanfiction/"]'), ("genre", '.tags a[href^="/tags/"]')):
            for link in soup.select(selector):
                value = self._tag_text(link)
                if value and value not in story.getList(key):
                    story.addToList(key, value)
        if len(story.getList("category")) > 1:
            story.addToList("genre", "Кроссовер")
        self._fill_ficbook_part_dates(story, soup)
        self._fill_optional_ficbook_metadata(adapter, soup, get_cover=get_cover)
        story.clear_processed_metadata_cache()

    def _fill_ficbook_part_dates(self, story: Any, soup: BeautifulSoup) -> None:
        months = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
        dates: list[datetime] = []
        for node in soup.select("ul.list-of-fanfic-parts li.part span[title], .part-date span[title]"):
            value = self._safe_text(node.get("title")).lower()
            for number, month in enumerate(months, 1):
                value = value.replace(month, str(number))
            match = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{4})", value)
            if match:
                day, month, year = map(int, match.groups())
                try:
                    dates.append(datetime(year, month, day))
                except ValueError:
                    continue
        if dates:
            story.setMetadata("datePublished", min(dates))
            story.setMetadata("dateUpdated", max(dates))

    def _ficbook_title_from_soup(self, soup: BeautifulSoup) -> str:
        title = soup.select_one("section.chapter-info h1")
        if isinstance(title, Tag):
            for marker in title.find_all("sup"):
                marker.decompose()
            return self._tag_text(title)
        meta = soup.find("meta", property="og:title")
        if isinstance(meta, Tag):
            return self._safe_text(meta.get("content"))
        return ""

    def _fill_ficbook_author_metadata(self, story: Any, soup: BeautifulSoup) -> None:
        credits: dict[str, list[tuple[str, str]]] = {"author": [], "translator": []}
        root = soup.select_one('section.chapter-info') or soup
        links = soup.select('.hat-creator-container .creator-username, .creator-info .creator-username')
        links.extend(root.select(
            '.fanfic-hat a.word-break[href^="/authors/"], .fanfic-hat a[itemprop="author"][href]'
        ))
        if not links:
            links = soup.select("a.creator-username")
        for link in links:
            container = link.find_parent(class_=re.compile(r"^(hat-creator-container|creator-info)$"))
            role_node = container.select_one(".small-text.text-muted") if container else None
            if role_node is None:
                for parent in link.parents:
                    if not isinstance(parent, Tag) or parent.name in {'section', 'header', 'body', 'html'}:
                        break
                    role_node = parent.find(class_='ds-badge', recursive=False)
                    if role_node is not None:
                        break
            role = self._tag_text(role_node).lower() if role_node else ""
            key = "translator" if "переводчик" in role else "author"
            if role and not any(word in role for word in ("автор", "переводчик")):
                continue
            name = self._tag_text(link)
            href = self._safe_text(link.get("href"))
            url = f"https://ficbook.net{href}" if href.startswith("/") else href
            if name and (name, url) not in credits[key]:
                credits[key].append((name, url))
        for label in soup.find_all(["strong", "b", "dt"]):
            if not re.fullmatch(r"Автор(?:ы)? оригинала\s*:?", self._tag_text(label), re.I):
                continue
            value = label.find_next_sibling()
            if not isinstance(value, Tag):
                continue
            name = self._tag_text(value)
            if not name:
                continue
            link = value if value.name == "a" else value.find("a", href=True)
            href = self._safe_text(link.get('href')) if link else ''
            credits["author"] = [(name, urljoin('https://ficbook.net', href) if href else '')]
            break
        # Legacy pages may have a bare author link, but role-labelled pages must
        # never substitute a translator or beta for a missing original author.
        if not links and not credits["author"]:
            for link in soup.find_all("a", href=re.compile(r"^/authors/")):
                if link.find_parent(id=re.compile(r'^(?:comments?|reviews?)(?:$|[-_\d])')):
                    continue
                if self._tag_text(link):
                    credits["author"].append((self._tag_text(link), f"https://ficbook.net{link['href']}"))
                    break
        for key, people in credits.items():
            if people:
                story.setMetadata(key, ", ".join(name for name, _ in people))
                story.setMetadata(key + "Url", people[0][1] if len(people) == 1 else "")
        if credits["author"]:
            story.setMetadata("authorId", story.getMetadata("author"))
        elif credits["translator"]:
            story.setMetadata("author", "")
            story.setMetadata("authorUrl", "")

    def _fill_ficbook_chapter_links(self, adapter: Any, soup: BeautifulSoup, url: str) -> None:
        story_id = self._safe_text(adapter.story.getMetadata("storyId"))
        pattern = re.compile(rf"/readfic/{re.escape(story_id)}/\d+/?$")
        seen: set[str] = set()
        for link in soup.select("ul.list-of-fanfic-parts li.part a[href]"):
            if not isinstance(link, Tag):
                continue
            href = self._safe_text(link.get("href"))
            parsed = urlsplit(href)
            if parsed.netloc not in ("", "ficbook.net") or parsed.scheme not in ("", "https", "http"):
                continue
            if not story_id or not pattern.fullmatch(parsed.path) or parsed.path.rstrip("/") in seen:
                continue
            seen.add(parsed.path.rstrip("/"))
            chapter_url = f"https://ficbook.net{parsed.path.rstrip('/')}#part_content"
            adapter.add_chapter(self._tag_text(link) or f"Глава {len(seen)}", chapter_url)
        chapter = soup.select_one("div#content, div.public_beta_disabled")
        if not seen and not soup.select_one("ul.list-of-fanfic-parts li.part") and chapter:
            adapter.add_chapter(adapter.story.getMetadata("title"), url)

    def _fill_optional_ficbook_metadata(self, adapter: Any, soup: BeautifulSoup, *, get_cover: bool = True) -> None:
        story = adapter.story
        rating = (soup.select_one('section.chapter-info header .fanfic-badges [class*="ds-label-rating-"]')
                  or soup.select_one('header div[class*="badge-rating-"] span'))
        if rating:
            story.setMetadata("rating", self._tag_text(rating))
        if not story.getMetadata("classification"):
            direction = (soup.select_one('section.chapter-info header .fanfic-badges .direction')
                         or soup.select_one('div[class^="badge-with-icon direction"] span.badge-text'))
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
        if get_cover:
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
        nodes = soup.select('section.chapter-info header .fanfic-badges [class*="ds-label-status-"]')
        if not nodes:
            nodes = soup.select('[class*="badge-status"]')
        for node in nodes:
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
        chapter = soup.select_one("div#content, div.public_beta_disabled")
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
        parts: list[str] = []
        def row(label: str, value: Any) -> None:
            text = self._safe_text(value)
            if text:
                parts.append(f"<p>{escape(label)}: {escape(text)}</p>")
        parts.append(f"<p>{escape(self._safe_text(story.getMetadata('title')))}</p>")
        row("Ссылка", source_url)
        row("Автор", self._text_line(story.getMetadata("author"), story.getMetadata("authorUrl")))
        row("Переводчик", self._text_line(story.getMetadata("translator"), story.getMetadata("translatorUrl")))
        row("Направленность", story.getMetadata("classification"))
        row("Фэндом", ", ".join(story.getList("category")))
        row("Пейринг и персонажи", ", ".join(story.getList("ships")))
        row("Рейтинг", story.getMetadata("rating"))
        row("Страниц", story.getMetadata("pages"))
        row("Слов", self._safe_text(story.getMetadata("numWords")).replace(",", " "))
        row("Кол-во частей", story.getMetadata("numChapters"))
        status = self._status_text(story.getMetadata("status"))
        row("Статус", status)
        start_date = self._safe_text(story.getMetadata("dateStart") or story.getMetadata("datePublished"))
        row("Дата начала", start_date)
        finish_date = self._safe_text(story.getMetadata("dateFinish"))
        row("Дата завершения", finish_date)
        row("Метки", ", ".join(story.getList("genre")))
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
            parts.append(f"<p>{escape(publication)}</p>")
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
            link = soup.new_tag("a", href=f"#footnote-{number}")
            link.string = f"[{number}]"
            marker.append(link)
            reference.replace_with(marker)
        parts = [self._render_preline_container(chapter)]
        if rendered_notes:
            parts.append('<div class="interactive-footnotes"><p><b>Интерактивные примечания:</b></p>')
            for number, note_html in rendered_notes:
                parts.extend((f'<div id="footnote-{number}"><p><b>[{number}]</b></p>', self._render_preline_html(note_html), '</div>'))
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

    def _extract_cover_image(self, story: Any, *, source_url: str = '') -> CoverImage | None:
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
            cover = CoverImage(data=data, mime=mime, extension=extension)
            if _is_ficbook_url(source_url) and is_ficbook_placeholder(cover):
                continue
            return cover
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
    if isinstance(error, ResourceLimitError):
        raise error
    site_name = _site_display_name(url)
    if _site_key(url) == 'archiveofourown.org' and isinstance(error, AO3MaintenanceError):
        return AO3SiteUnavailableError(str(error))
    if _site_key(url) == 'archiveofourown.org' and isinstance(error, AO3WorkUnavailableError):
        return FicbookError(str(error))
    if _site_key(url) == 'archiveofourown.org' and getattr(error, 'status_code', None) == 525:
        return FicbookError('AO3 временно недоступен: сбой защищённого соединения на стороне сайта. '
                            'Попробуй повторить позже.', technical='AO3 HTTP 525')
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


def _ficbook_site_unavailable_error(error: Exception) -> FicbookSiteUnavailableError:
    return FicbookSiteUnavailableError(
        "Проверка доступности подтвердила временную недоступность Ficbook.",
        technical=repr(error),
        user_message=(
            "Ficbook сейчас не работает. Фанфик останется в очереди и скачается, "
            "как только сайт снова заработает."
        ),
    )


def _friendly_preview_error(url: str, error: Exception) -> FicbookError:
    if _site_key(url) == 'archiveofourown.org' and isinstance(error, AO3MaintenanceError):
        return AO3SiteUnavailableError(str(error))
    site_name = _site_display_name(url)
    if _site_key(url) == 'archiveofourown.org' and isinstance(error, AO3WorkUnavailableError):
        return FicbookError(str(error))
    if _site_key(url) == 'archiveofourown.org' and getattr(error, 'status_code', None) == 525:
        return FicbookError('AO3 временно недоступен: сбой защищённого соединения на стороне сайта. '
                            'Список глав пока получить не удалось. Попробуй позже.', technical='AO3 HTTP 525')
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
    code = getattr(error, 'status_code', getattr(error, 'code', None))
    if code is not None and code != 403:
        return False
    text = repr(error).lower()
    return any(marker in text for marker in ("shields are up", "not a robot", "cloudflare", "challenge-platform"))


def _is_ao3_transient_failure(url: str, error: Exception) -> bool:
    if _site_key(url) != "archiveofourown.org":
        return False
    if getattr(error, 'ao3_retry_exhausted', False):
        return False
    from src.sources.ao3_http import is_transient_ao3_error
    if is_transient_ao3_error(error):
        return True
    code = getattr(error, 'status_code', getattr(error, 'code', None))
    if code is not None:
        return code == 403 and _is_ao3_shields_error(url, error)
    text = f"{error!r} {getattr(error, 'technical', '')}".lower()
    return any(
        marker in text
        for marker in ("525", "shields are up", "not a robot", "cloudflare", "challenge-platform")
    )


def select_chapter_links(
    chapter_links: list[dict[str, Any]],
    chapter_numbers: set[int] | frozenset[int] | None,
) -> list[tuple[int, dict[str, Any]]]:
    check_chapter_count(len(chapter_numbers) if chapter_numbers else len(chapter_links))
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
