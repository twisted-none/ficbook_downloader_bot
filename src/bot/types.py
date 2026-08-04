from __future__ import annotations

from dataclasses import dataclass

from src.core.models import StoryPreview


@dataclass(slots=True)
class SettingsDraft:
    formats: tuple[str, ...]
    chapter_selection_enabled: bool = False
    cover_enabled: bool = True


@dataclass(slots=True)
class SettingsSession:
    saved: SettingsDraft
    draft: SettingsDraft
    view: str = "menu"


@dataclass(slots=True)
class PendingChapterSelection:
    url: str
    formats: tuple[str, ...]
    cover_enabled: bool
    preview: StoryPreview
