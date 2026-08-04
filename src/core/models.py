from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Chapter:
    title: str
    html: str


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
