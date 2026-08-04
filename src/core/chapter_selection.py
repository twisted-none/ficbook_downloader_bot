from __future__ import annotations

"""Chapter selection parsing and validation."""

import re


TOKEN_RE = re.compile(r"^\d+(?:-\d+)?$")


class ChapterSelectionError(ValueError):
    """Invalid user chapter range input."""


def parse_chapter_selection(text: str, max_chapter: int) -> tuple[int, ...] | None:
    raw = text.strip().lower()
    if raw in {"0", "all", "все", "всё"}:
        return None
    return parse_chapter_numbers(text, max_chapter)


def parse_chapter_numbers(text: str, max_chapter: int) -> tuple[int, ...]:
    if max_chapter < 1:
        raise ChapterSelectionError("У фанфика нет глав для выбора.")
    raw = text.strip()
    if not raw:
        raise ChapterSelectionError("Пришли номера глав, например: 1,2,5-10,17.")
    selected: list[int] = []
    seen: set[int] = set()
    for token in (part.strip() for part in raw.split(",")):
        if not TOKEN_RE.match(token):
            raise ChapterSelectionError(f"Не понял фрагмент: {token!r}.")
        start_text, _, end_text = token.partition("-")
        start = int(start_text)
        end = int(end_text or start_text)
        if start < 1 or end < 1:
            raise ChapterSelectionError("Номера глав начинаются с 1.")
        if start > end:
            raise ChapterSelectionError(f"Диапазон {token} указан в обратном порядке.")
        if end > max_chapter:
            raise ChapterSelectionError(f"В фанфике только {max_chapter} глав.")
        for number in range(start, end + 1):
            if number not in seen:
                selected.append(number)
                seen.add(number)
    if not selected:
        raise ChapterSelectionError("Не выбрано ни одной главы.")
    return tuple(selected)
