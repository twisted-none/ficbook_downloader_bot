from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Chapter:
    title: str
    html: str


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
    fandoms: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    rating: str = ""
    chapters: list[Chapter] = field(default_factory=list)
