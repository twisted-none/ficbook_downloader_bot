from __future__ import annotations

from configparser import DuplicateSectionError
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import fanficfare
from bs4 import BeautifulSoup, NavigableString, Tag
from fanficfare import adapters, exceptions
from fanficfare.configurable import Configuration

from src.models import Chapter, Story


class FicbookError(Exception):
    """Domain error for user-facing bot responses."""


BLOCK_HTML_TAGS = {"article", "blockquote", "div", "hr", "li", "ol", "p", "section", "ul"}


class FicbookClient:
    def __init__(self, login: str = "", password: str = "") -> None:
        self.login = login
        self.password = password
        self.defaults_ini = Path(fanficfare.__file__).with_name("defaults.ini")

    def download(self, url: str) -> Story:
        normalized = normalize_url(url)
        try:
            config = self._build_config(normalized)
            adapter = adapters.getAdapter(config, normalized)
            story = adapter.getStoryMetadataOnly()
            page_soup = adapter.make_soup(adapter.get_request(normalized))
            chapters = [
                Chapter(title=chapter["title"], html=self._load_chapter_html(adapter, chapter["url"]))
                for chapter in adapter.get_chapters()
            ]
        except exceptions.AdultCheckRequired as exc:
            raise FicbookError(self._adult_message()) from exc
        except exceptions.FailedToLogin as exc:
            raise FicbookError("Не удалось войти в Ficbook. Проверь FICBOOK_LOGIN и FICBOOK_PASSWORD.") from exc
        except (exceptions.UnknownSite, exceptions.StoryDoesNotExist) as exc:
            raise FicbookError("Не удалось найти фанфик по этой ссылке.") from exc
        except Exception as exc:
            raise FicbookError(f"Ошибка загрузки Ficbook: {exc}") from exc
        return Story(
            title=story.getMetadata("title") or "ficbook",
            author=story.getMetadata("author") or "unknown",
            source_url=normalized,
            description=story.getMetadata("description") or "",
            annotation_html=self._build_annotation_html(story, page_soup, normalized),
            language=story.getMetadata("language") or "ru",
            published=story.getMetadata("datePublished") or "",
            updated=story.getMetadata("dateUpdated") or "",
            fandoms=story.getList("category"),
            genres=story.getList("genre"),
            rating=story.getMetadata("rating") or "",
            chapters=chapters,
        )

    def _build_config(self, url: str) -> Configuration:
        config = Configuration(adapters.getConfigSectionsFor(url), "epub")
        config.read([str(self.defaults_ini)])
        try:
            config.add_section("overrides")
        except DuplicateSectionError:
            pass
        for key, value in {
            "is_adult": "true",
            "include_images": "false",
            "replace_text_formatting": "true",
            "username": self.login,
            "password": self.password,
        }.items():
            if value:
                config.set("overrides", key, value)
        return config

    def _adult_message(self) -> str:
        if self.login and self.password:
            return "Ficbook запросил подтверждение 18+, но доступ получить не удалось."
        return "Для 18+ фанфиков укажи FICBOOK_LOGIN и FICBOOK_PASSWORD в окружении контейнера."

    def _load_chapter_html(self, adapter: Any, url: str) -> str:
        soup = adapter.make_soup(adapter.get_request(url))
        chapter = soup.find("div", {"id": "content"}) or soup.find("div", {"class": "public_beta_disabled"})
        if chapter is None:
            raise FicbookError(f"Не удалось извлечь главу: {url}")
        parts: list[str] = [self._render_preline_container(chapter)]
        note_before = soup.select_one("div.part-comment-top div.text-preline")
        note_after = soup.select_one("div.part-comment-bottom div.text-preline")
        if note_before:
            parts.append(self._chapter_note_html(note_before))
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
        pairings = self._join_values(story.getList("ships")) or self._join_values(story.getList("characters"))
        if pairings:
            parts.append(f"<p>Пэйринг и персонажи: {pairings}</p>")
        if story.getMetadata("rating"):
            parts.append(f"<p>Рейтинг: {self._safe_text(story.getMetadata('rating'))}</p>")
        size_text = self._size_text(story)
        if size_text:
            parts.append("<p>Размер:</p>")
            parts.append(f"<p>{size_text}</p>")
        if story.getMetadata("numChapters"):
            parts.append(f"<p>Кол-во частей:{self._safe_text(story.getMetadata('numChapters'))}</p>")
        if story.getMetadata("status"):
            parts.append("<p>Статус:</p>")
            parts.append(f"<p>{self._status_text(story.getMetadata('status'))}</p>")
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
        raw = re.sub(r"(?i)<br\\s*/?>", "\n", html).replace("\r\n", "\n").replace("\r", "\n")
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
        status = self._safe_text(value).lower()
        if status == "completed":
            return "завершён"
        if status == "in progress":
            return "в процессе"
        return self._safe_text(value)

    def _text_line(self, text: str, url: str = "", label: str = "") -> str:
        value = self._safe_text(text)
        link = self._safe_text(url)
        prefix = f"{label}: " if label else ""
        if value and link:
            return f"{prefix}{value} ({link})"
        return f"{prefix}{value or link}"

    def _safe_text(self, value: Any) -> str:
        return str(value or "").replace("\xa0", " ").strip()


def extract_url(text: str) -> str | None:
    for chunk in text.split():
        if "ficbook." in chunk and "/readfic/" in chunk:
            return chunk.strip("()[]<>,.!?\"'")
    return None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme or "https"
    host = parts.netloc.replace("www.", "").replace("ficbook.com", "ficbook.net")
    path = parts.path.rstrip("/")
    cleaned = urlunsplit((scheme, host, path, "", ""))
    return cleaned.split("#", 1)[0]
