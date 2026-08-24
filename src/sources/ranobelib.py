from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http.cookiejar import CookieJar
import base64
import hashlib
import hmac
import json
import mimetypes
import re
import secrets
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener, urlopen

from bs4 import BeautifulSoup

from src.core.models import Chapter, ChapterPreview, CoverImage, Story, StoryPreview

API_ROOT = "https://api.cdnlibs.org/api/manga"
AUTH_AUTHORIZE_URL = "https://auth.lib.social/auth/oauth/authorize"
AUTH_LOGIN_URL = "https://auth.lib.social/auth/login"
AUTH_TOKEN_URL = "https://api.cdnlibs.org/api/auth/oauth/token"
AUTH_CALLBACK_URL = "https://ranobelib.me/ru/front/auth/oauth/callback"
ALTCHA_CHALLENGE_URL = "https://auth.lib.social/altcha/challenge"
MAX_COVER_BYTES = 8 * 1024 * 1024


class RanobelibError(Exception):
    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class RanobelibNotFoundError(RanobelibError):
    pass


class RanobelibAccessError(RanobelibError):
    pass


class RanobelibRateLimitError(RanobelibError):
    pass


class RanobelibLoginError(RanobelibError):
    def __init__(self, message: str) -> None:
        super().__init__(message, user_message="Авторизация через RanobeLIB не сработала.")


class _OAuthCallback(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class _OAuthRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        if newurl.startswith(AUTH_CALLBACK_URL):
            raise _OAuthCallback(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True, slots=True)
class RanobelibChapter:
    index: int
    volume: str
    number: str
    title: str
    branch_id: str = ""


class RanobelibClient:
    def __init__(
        self,
        login: str = "",
        password: str = "",
        *,
        min_request_interval: float = 0.7,
        retry_attempts: int = 3,
    ) -> None:
        self.login = login
        self.password = password
        self._min_request_interval = max(0.0, min_request_interval)
        self._retry_attempts = max(1, retry_attempts)
        self._request_lock = Lock()
        self._last_request_at = 0.0
        self._auth_lock = Lock()
        self._access_token = ""
        self._refresh_token = ""
        self._auth_opener = build_opener(HTTPCookieProcessor(CookieJar()), _OAuthRedirectHandler())

    def download(
        self,
        url: str,
        progress: Callable[[str], None] | None = None,
        chapter_numbers: set[int] | frozenset[int] | None = None,
    ) -> Story:
        slug = self._slug(url)
        info = self._info(slug)
        selected = self._select_chapters(self._chapters(slug), chapter_numbers)
        chapters: list[Chapter] = []
        for position, chapter in enumerate(selected, 1):
            if progress:
                progress(f"Скачиваю главы: {position}/{len(selected)}")
            data = self._chapter(slug, chapter)
            html = self._content_html(data.get("content"), data.get("attachments"))
            if not BeautifulSoup(html, "lxml").get_text(" ", strip=True) and "<img" not in html:
                raise RanobelibError(
                    f"RanobeLIB вернул пустую главу {chapter.index}.",
                    user_message=f"Глава {chapter.index} сейчас недоступна на RanobeLIB.",
                )
            chapters.append(Chapter(chapter.title, html))
        return self._story(url, info, chapters)

    def preview(self, url: str) -> StoryPreview:
        slug = self._slug(url)
        info = self._info(slug)
        chapters = self._chapters(slug)
        return StoryPreview(
            title=self._title(info),
            author=self._authors(info),
            source_url=url,
            chapters=[ChapterPreview(chapter.index, chapter.title) for chapter in chapters],
        )

    def _info(self, slug: str) -> dict[str, Any]:
        fields = ("summary", "genres", "tags", "authors", "status_id")
        query = "&".join(f"fields[]={field}" for field in fields)
        data = self._request_json(f"{API_ROOT}/{slug}?{query}")
        if not isinstance(data, dict) or not data.get("id"):
            raise RanobelibNotFoundError("Произведение RanobeLIB не найдено.")
        return data

    def _chapters(self, slug: str) -> list[RanobelibChapter]:
        raw = self._request_json(f"{API_ROOT}/{slug}/chapters")
        if not isinstance(raw, list):
            raise RanobelibError("RanobeLIB вернул некорректный список глав.")
        chapters: list[RanobelibChapter] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            branch = self._available_branch(item.get("branches"))
            if branch is None:
                continue
            volume = str(item.get("volume") or "1")
            number = str(item.get("number") or len(chapters) + 1)
            name = str(item.get("name") or "").strip()
            title = f"Том {volume}, глава {number}" + (f" — {name}" if name else "")
            branch_id = str(branch.get("branch_id") or "")
            chapters.append(RanobelibChapter(len(chapters) + 1, volume, number, title, branch_id))
        if not chapters:
            raise RanobelibAccessError(
                "RanobeLIB не вернул доступных глав.",
                user_message="У этой книги нет глав, доступных для скачивания без ограничений.",
            )
        return chapters

    def _chapter(self, slug: str, chapter: RanobelibChapter) -> dict[str, Any]:
        params = {"volume": chapter.volume, "number": chapter.number}
        if chapter.branch_id:
            params["branch_id"] = chapter.branch_id
        data = self._request_json(f"{API_ROOT}/{slug}/chapter?{urlencode(params)}")
        if not isinstance(data, dict) or not data.get("content"):
            raise RanobelibAccessError(
                f"RanobeLIB did not return chapter {chapter.index} content.",
                user_message=f"Глава {chapter.index} недоступна для скачивания через RanobeLIB.",
            )
        return data

    def _story(self, url: str, info: dict[str, Any], chapters: list[Chapter]) -> Story:
        description_html = self._content_html(info.get("summary"))
        description = BeautifulSoup(description_html, "lxml").get_text(" ", strip=True)
        status = self._status(info.get("status"))
        genres = self._names(info.get("genres"))
        tags = self._names(info.get("tags"))
        annotation = description_html
        if status:
            annotation += f"<p><strong>Статус:</strong> {escape(status)}</p>"
        return Story(
            title=self._title(info),
            author=self._authors(info),
            source_url=url,
            description=description,
            annotation_html=annotation,
            language="ru",
            status=status,
            genres=[*genres, *(tag for tag in tags if tag not in genres)],
            rating=self._nested_text(info.get("ageRestriction"), "label"),
            cover=self._cover(info.get("cover")),
            chapters=chapters,
        )

    def _request_json(self, url: str) -> Any:
        self._ensure_authenticated()
        auth_retried = False
        total_attempts = self._retry_attempts + int(bool(self.login))
        for attempt in range(1, total_attempts + 1):
            self._wait_for_request_slot()
            try:
                with urlopen(self._api_request(url), timeout=30) as response:
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code == 401 and self.login and not auth_retried:
                    self._renew_authentication()
                    auth_retried = True
                    continue
                if exc.code == 401 and self.login:
                    raise RanobelibLoginError("RanobeLIB rejected the refreshed access token.") from exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self._retry_attempts:
                    sleep(self._retry_delay(exc, attempt))
                    continue
                self._raise_http_error(exc)
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self._retry_attempts:
                    sleep(float(attempt))
                    continue
                raise RanobelibError(
                    f"RanobeLIB request failed: {exc!r}",
                    user_message="RanobeLIB сейчас не отвечает. Попробуй повторить позже.",
                ) from exc
        else:
            raise RanobelibError("RanobeLIB request retry state exhausted.")
        return payload.get("data") if isinstance(payload, dict) else None

    def _api_request(self, url: str) -> Request:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FicbookDownloaderBot/1.0)",
            "Accept": "application/json",
            "Origin": "https://ranobelib.me",
            "Referer": "https://ranobelib.me/",
            "Site-Id": "3",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return Request(url, headers=headers)

    def _ensure_authenticated(self) -> None:
        if not (self.login and self.password) or self._access_token:
            return
        with self._auth_lock:
            if not self._access_token:
                self._login_locked()

    def _renew_authentication(self) -> None:
        with self._auth_lock:
            if self._refresh_token and self._refresh_locked():
                return
            self._access_token = ""
            self._refresh_token = ""
            self._login_locked()

    def _login_locked(self) -> None:
        if not (self.login and self.password):
            raise RanobelibLoginError("RanobeLIB credentials are not configured.")
        self._auth_opener = build_opener(HTTPCookieProcessor(CookieJar()), _OAuthRedirectHandler())
        verifier = secrets.token_urlsafe(96)
        state = secrets.token_urlsafe(30)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = urlencode(
            {
                "scope": "",
                "client_id": "1",
                "response_type": "code",
                "redirect_uri": AUTH_CALLBACK_URL,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        page_url, body = self._auth_open(f"{AUTH_AUTHORIZE_URL}?{query}")
        soup = BeautifulSoup(body, "lxml")
        form = soup.find("form", action=re.compile(r"/auth/login(?:$|\?)"))
        if not form:
            raise RanobelibLoginError("RanobeLIB login form was not found.")
        action = urljoin(page_url, str(form.get("action") or AUTH_LOGIN_URL))
        self._require_auth_host(action)
        payload = self._hidden_form_values(form)
        payload.update(
            {
                "login": self.login,
                "password": self.password,
                "altcha": self._altcha_payload(),
            }
        )
        callback_url, response_url, response_body = self._auth_submit(action, payload, page_url)
        if not callback_url:
            callback_url = self._submit_consent_if_present(response_body, response_url)
        code = self._oauth_code(callback_url, state)
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": 1,
                "redirect_uri": AUTH_CALLBACK_URL,
                "code_verifier": verifier,
                "code": code,
            }
        )
        self._store_token(token)

    def _auth_open(self, url: str) -> tuple[str, bytes]:
        self._require_auth_host(url)
        request = Request(url, headers=self._browser_headers(AUTH_LOGIN_URL))
        try:
            with self._auth_opener.open(request, timeout=30) as response:
                return response.geturl(), response.read()
        except _OAuthCallback as exc:
            return exc.url, b""
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RanobelibLoginError(f"RanobeLIB authorization page failed: {exc!r}") from exc

    def _auth_submit(
        self,
        url: str,
        payload: dict[str, str],
        referer: str,
    ) -> tuple[str, str, bytes]:
        self._require_auth_host(url)
        request = Request(
            url,
            data=urlencode(payload).encode("utf-8"),
            headers={
                **self._browser_headers(referer),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with self._auth_opener.open(request, timeout=30) as response:
                result_url = response.geturl()
                callback = result_url if result_url.startswith(AUTH_CALLBACK_URL) else ""
                return callback, result_url, response.read()
        except _OAuthCallback as exc:
            return exc.url, exc.url, b""
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RanobelibLoginError(f"RanobeLIB login request failed: {exc!r}") from exc

    def _submit_consent_if_present(self, body: bytes, referer: str) -> str:
        soup = BeautifulSoup(body, "lxml")
        if soup.find("form", action=re.compile(r"/auth/login(?:$|\?)")):
            raise RanobelibLoginError("RanobeLIB rejected the configured login or password.")
        form = soup.find("form", action=re.compile(r"/auth/oauth/authorize"))
        if not form:
            form = soup.find("form", method=re.compile(r"post", re.I))
        if not form:
            raise RanobelibLoginError("RanobeLIB did not return an OAuth authorization code.")
        action = urljoin(referer, str(form.get("action") or AUTH_AUTHORIZE_URL))
        payload = self._hidden_form_values(form)
        buttons = form.select("button[type='submit'], input[type='submit']")
        def button_text(button: Any) -> str:
            return " ".join(
                (
                    button.get_text(" ", strip=True),
                    str(button.get("name") or ""),
                    str(button.get("value") or ""),
                )
            )

        preferred = next(
            (
                button
                for button in buttons
                if re.search(r"разреш|подтверд|продолж|approve|allow|accept", button_text(button), re.I)
            ),
            next(
                (
                    button
                    for button in buttons
                    if not re.search(r"отказ|отмен|deny|reject|cancel", button_text(button), re.I)
                ),
                None,
            ),
        )
        if preferred and preferred.get("name"):
            payload[str(preferred.get("name"))] = str(preferred.get("value") or "1")
        callback_url, _, _ = self._auth_submit(action, payload, referer)
        if not callback_url:
            raise RanobelibLoginError("RanobeLIB OAuth consent did not return a code.")
        return callback_url

    def _altcha_payload(self) -> str:
        challenge = self._auth_json(ALTCHA_CHALLENGE_URL, AUTH_LOGIN_URL)
        algorithm = str(challenge.get("algorithm") or "")
        hash_name = {"SHA-1": "sha1", "SHA-256": "sha256", "SHA-512": "sha512"}.get(algorithm)
        salt = str(challenge.get("salt") or "")
        target = str(challenge.get("challenge") or "")
        try:
            max_number = int(challenge.get("maxnumber"))
        except (TypeError, ValueError) as exc:
            raise RanobelibLoginError("RanobeLIB returned an invalid ALTCHA challenge.") from exc
        if not hash_name or not salt or not target or not 0 <= max_number <= 2_000_000:
            raise RanobelibLoginError("RanobeLIB returned an unsupported ALTCHA challenge.")
        for number in range(max_number + 1):
            digest = hashlib.new(hash_name, f"{salt}{number}".encode()).hexdigest()
            if hmac.compare_digest(digest, target):
                result = {
                    "algorithm": algorithm,
                    "challenge": target,
                    "number": number,
                    "salt": salt,
                    "signature": str(challenge.get("signature") or ""),
                }
                raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
                return base64.b64encode(raw).decode("ascii")
        raise RanobelibLoginError("RanobeLIB ALTCHA challenge could not be solved.")

    def _auth_json(self, url: str, referer: str) -> dict[str, Any]:
        self._require_auth_host(url)
        request = Request(url, headers={**self._browser_headers(referer), "Accept": "application/json"})
        try:
            with self._auth_opener.open(request, timeout=30) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RanobelibLoginError(f"RanobeLIB authentication challenge failed: {exc!r}") from exc
        if not isinstance(payload, dict):
            raise RanobelibLoginError("RanobeLIB returned an invalid authentication challenge.")
        return payload

    def _token_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            AUTH_TOKEN_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "User-Agent": self._browser_headers(AUTH_CALLBACK_URL)["User-Agent"],
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://ranobelib.me",
                "Referer": AUTH_CALLBACK_URL,
                "Site-Id": "3",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                token = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RanobelibLoginError(f"RanobeLIB token request failed: {exc!r}") from exc
        if not isinstance(token, dict):
            raise RanobelibLoginError("RanobeLIB returned an invalid OAuth token.")
        return token

    def _refresh_locked(self) -> bool:
        try:
            token = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": 1,
                    "refresh_token": self._refresh_token,
                }
            )
            self._store_token(token)
        except RanobelibLoginError:
            return False
        return True

    def _store_token(self, token: dict[str, Any]) -> None:
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise RanobelibLoginError("RanobeLIB OAuth response did not include an access token.")
        self._access_token = access_token
        self._refresh_token = str(token.get("refresh_token") or self._refresh_token).strip()

    def _oauth_code(self, callback_url: str, expected_state: str) -> str:
        query = parse_qs(urlsplit(callback_url).query)
        code = str(query.get("code", [""])[0])
        state = str(query.get("state", [""])[0])
        if not code or not state or not hmac.compare_digest(state, expected_state):
            raise RanobelibLoginError("RanobeLIB returned an invalid OAuth callback.")
        return code

    def _hidden_form_values(self, form: Any) -> dict[str, str]:
        return {
            str(node.get("name")): str(node.get("value") or "")
            for node in form.select("input[type='hidden'][name]")
        }

    def _browser_headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Origin": "https://auth.lib.social",
            "Referer": referer,
        }

    def _require_auth_host(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "auth.lib.social":
            raise RanobelibLoginError("RanobeLIB attempted to send credentials to an unexpected host.")

    def _wait_for_request_slot(self) -> None:
        with self._request_lock:
            delay = self._min_request_interval - (monotonic() - self._last_request_at)
            if delay > 0:
                sleep(delay)
            self._last_request_at = monotonic()

    def _retry_delay(self, error: HTTPError, attempt: int) -> float:
        value = error.headers.get("Retry-After", "") if error.headers else ""
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return float(2**attempt)

    def _raise_http_error(self, error: HTTPError) -> None:
        if error.code == 404:
            raise RanobelibNotFoundError("Произведение RanobeLIB не найдено.") from error
        if error.code == 429:
            raise RanobelibRateLimitError("RanobeLIB временно ограничил запросы.") from error
        if error.code in {401, 403}:
            raise RanobelibAccessError(
                f"RanobeLIB rejected content access with HTTP {error.code}.",
                user_message="Эта книга или глава недоступна для скачивания через RanobeLIB.",
            ) from error
        raise RanobelibError(
            f"RanobeLIB HTTP {error.code}.",
            user_message="RanobeLIB вернул ошибку. Попробуй повторить позже.",
        ) from error

    def _cover(self, value: Any) -> CoverImage | None:
        if not isinstance(value, dict):
            return None
        url = str(value.get("default") or value.get("md") or "").strip()
        if not url:
            return None
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://ranobelib.me/"})
            with urlopen(request, timeout=30) as response:
                data = response.read(MAX_COVER_BYTES + 1)
                mime = response.headers.get_content_type().lower()
        except (HTTPError, URLError, TimeoutError):
            return None
        if not data or len(data) > MAX_COVER_BYTES or not mime.startswith("image/"):
            return None
        extension = (mimetypes.guess_extension(mime) or ".jpg").lstrip(".")
        return CoverImage(data, mime, extension)

    def _content_html(self, value: Any, attachments: Any = None) -> str:
        if isinstance(value, str):
            soup = BeautifulSoup(value, "lxml")
            for node in soup.select("script, style"):
                node.decompose()
            body = soup.body
            return body.decode_contents().strip() if body else str(soup)
        attachment_urls = self._attachment_urls(attachments)
        return self._render_node(value, attachment_urls) if isinstance(value, dict) else ""

    def _render_node(self, node: Any, attachments: dict[str, str]) -> str:
        if not isinstance(node, dict):
            return ""
        kind = str(node.get("type") or "")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        children = "".join(self._render_node(child, attachments) for child in node.get("content") or [])
        if kind == "text":
            return self._render_text(str(node.get("text") or ""), node.get("marks"))
        if kind in {"doc", "fragment"}:
            return children
        if kind == "paragraph":
            return f"<p>{children}</p>"
        if kind == "heading":
            level = min(6, max(1, int(attrs.get("level") or 2)))
            return f"<h{level}>{children}</h{level}>"
        if kind == "blockquote":
            return f"<blockquote>{children}</blockquote>"
        if kind == "bullet_list":
            return f"<ul>{children}</ul>"
        if kind == "ordered_list":
            start = int(attrs.get("start") or 1)
            return f'<ol start="{start}">{children}</ol>'
        if kind == "list_item":
            return f"<li>{children}</li>"
        if kind == "hard_break":
            return "<br/>"
        if kind == "horizontal_rule":
            return "<hr/>"
        if kind in {"image", "image_block"}:
            src = str(attrs.get("src") or attrs.get("url") or "")
            src = attachments.get(src, src)
            alt = escape(str(attrs.get("alt") or attrs.get("title") or ""), quote=True)
            return f'<p><img src="{escape(src, quote=True)}" alt="{alt}"/></p>' if src else ""
        return children

    def _render_text(self, value: str, marks: Any) -> str:
        text = escape(value)
        wrappers = {"bold": "strong", "italic": "em", "underline": "u", "strike": "s", "code": "code"}
        for mark in marks if isinstance(marks, list) else []:
            if not isinstance(mark, dict):
                continue
            kind = str(mark.get("type") or "")
            if kind == "link":
                attrs = mark.get("attrs") if isinstance(mark.get("attrs"), dict) else {}
                href = escape(str(attrs.get("href") or ""), quote=True)
                text = f'<a href="{href}">{text}</a>' if href else text
            elif kind in wrappers:
                tag = wrappers[kind]
                text = f"<{tag}>{text}</{tag}>"
        return text

    def _select_chapters(
        self,
        chapters: list[RanobelibChapter],
        numbers: set[int] | frozenset[int] | None,
    ) -> list[RanobelibChapter]:
        if numbers is None:
            return chapters
        if not numbers:
            raise RanobelibError("Не выбрано ни одной главы.")
        missing = sorted(number for number in numbers if number < 1 or number > len(chapters))
        if missing:
            raise RanobelibError(f"В книге {len(chapters)} глав, нельзя скачать: {', '.join(map(str, missing))}.")
        return [chapter for chapter in chapters if chapter.index in numbers]

    def _slug(self, url: str) -> str:
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
        candidates = [segment for segment in segments if re.fullmatch(r"\d+--[A-Za-z0-9_-]+", segment)]
        if not candidates:
            raise RanobelibNotFoundError("Некорректная ссылка RanobeLIB.")
        return candidates[0]

    def _available_branch(self, branches: Any) -> dict[str, Any] | None:
        if not isinstance(branches, list):
            return None
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            moderation = branch.get("moderation")
            if isinstance(moderation, dict) and moderation.get("id") == 0:
                continue
            restricted = branch.get("restricted_view")
            if not isinstance(restricted, dict) or restricted.get("is_open") is not False:
                return branch
        return None

    def _attachment_urls(self, attachments: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in attachments if isinstance(attachments, list) else []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            url = str(item["url"])
            for key in (item.get("id"), item.get("name"), item.get("filename"), url):
                if key is not None:
                    result[str(key)] = url
        return result

    def _title(self, info: dict[str, Any]) -> str:
        return str(info.get("rus_name") or info.get("name") or "RanobeLIB").strip()

    def _authors(self, info: dict[str, Any]) -> str:
        names = self._names(info.get("authors"), prefer_russian=True)
        return ", ".join(names) or "Неизвестный автор"

    def _names(self, values: Any, *, prefer_russian: bool = False) -> list[str]:
        result: list[str] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("rus_name") or item.get("name") or "") if prefer_russian else str(item.get("name") or "")
            if name and name not in result:
                result.append(name)
        return result

    def _status(self, value: Any) -> str:
        label = self._nested_text(value, "label")
        if re.search(r"заверш|закончен", label, re.I):
            return "завершён"
        if re.search(r"онгоинг|продолжа", label, re.I):
            return "в процессе"
        return label

    def _nested_text(self, value: Any, key: str) -> str:
        return str(value.get(key) or "").strip() if isinstance(value, dict) else ""
