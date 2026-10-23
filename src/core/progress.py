"""Optional metadata notifications without changing existing text callbacks."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgressReporter:
    on_progress: Callable[[str], None]
    on_title: Callable[[str], None]

    def __call__(self, text: str) -> None:
        self.on_progress(text)

    def set_title(self, title: str) -> None:
        self.on_title(title)


def report_title(progress: Callable[[str], None] | None, title: str | None) -> None:
    if isinstance(progress, ProgressReporter) and isinstance(title, str) and title.strip():
        progress.set_title(title)
