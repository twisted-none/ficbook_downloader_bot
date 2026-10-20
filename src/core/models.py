from __future__ import annotations

from dataclasses import dataclass, field

from src.core.limits import account_chapter


@dataclass(slots=True)
class Chapter:
    title: str
    html: str
    source_url: str = ""

    @classmethod
    def from_download(cls, title: str, html: str, source_url: str = '') -> 'Chapter':
        account_chapter(html)
        return cls(title, html, source_url)


@dataclass(slots=True)
class ChapterPreview:
    index: int
    title: str


@dataclass(slots=True)
class CoverImage:
    data: bytes
    mime: str
    extension: str


@dataclass(slots=True)
class StoryPreview:
    title: str
    author: str
    source_url: str
    chapters: list[ChapterPreview] = field(default_factory=list)


@dataclass(slots=True)
class Story:
    title: str
    author: str
    source_url: str
    description: str = ""
    annotation_html: str = ""
    language: str = "ru"
    published: str = ""
    updated: str = ""
    start_date: str = ""
    finish_date: str = ""
    status: str = ""
    fandoms: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    pairings: list[str] = field(default_factory=list)
    rating: str = ""
    page_count: str = ""
    word_count: str = ""
    cover: CoverImage | None = None
    chapters: list[Chapter] = field(default_factory=list)
    translator: str = ""
    # Full frontmatter already includes title, credits and size.
    annotation_complete: bool = False
