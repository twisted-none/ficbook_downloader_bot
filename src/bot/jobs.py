"""Event-loop-owned active jobs; cancellation never evicts a running worker."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import re
from secrets import token_urlsafe
from urllib.parse import urlsplit

from src.storage.download_jobs import SavedDownload
from src.sources.registry import normalize_collection_url, normalize_url, is_ficbook_collection_url


def download_key(user_id: int, chat_id: int, url: str, formats: tuple[str, ...],
                 cover_enabled: bool, chapter_numbers: set[int] | None, opening_page: str = 'default') -> tuple:
    url = normalize_collection_url(url) if is_ficbook_collection_url(url) else normalize_url(url)
    parts = urlsplit(url)
    # AO3 chapter links still download the whole work unless chapters were selected.
    if parts.hostname in {'archiveofourown.org', 'www.archiveofourown.org'}:
        match = re.match(r'/works/(\d+)(?:/|$)', parts.path)
        if match:
            url = 'https://archiveofourown.org/works/' + match.group(1)
    key = (user_id, chat_id, url, tuple(sorted(formats)), cover_enabled,
           tuple(sorted(chapter_numbers)) if chapter_numbers is not None else None)
    return key if opening_page == 'default' else (*key, opening_page)


@dataclass(slots=True)
class ActiveJob:
    token: str
    key: tuple
    user_id: int
    chat_id: int
    kind: str
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    cancel_allowed: bool = True
    text: str = 'Подготавливаю загрузку...'
    saved: SavedDownload | None = None
    parent: 'ActiveJob | None' = None
    owner: asyncio.Task | None = None
    started: bool = False
    waiting_for_site: bool = False


current_job: ContextVar[ActiveJob | None] = ContextVar('active_download_job', default=None)


class JobRegistry:
    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self._jobs: dict[str, ActiveJob] = {}
        self._keys: dict[tuple, ActiveJob] = {}

    def reserve(self, key: tuple, user_id: int, chat_id: int, kind: str = 'work', *,
                token: str | None = None) -> tuple[ActiveJob, bool]:
        if key in self._keys:
            return self._keys[key], False
        if len(self._jobs) >= self.max_entries:
            raise OverflowError('Active job capacity reached')
        job = ActiveJob(token or token_urlsafe(9), key, user_id, chat_id, kind)
        self._jobs[job.token] = self._keys[key] = job
        return job, True

    def get(self, token: str) -> ActiveJob | None:
        return self._jobs.get(token)

    def cancel(self, token: str, user_id: int, chat_id: int) -> bool:
        job = self.get(token)
        if job is None or job.user_id != user_id or job.chat_id != chat_id or job.task is None:
            return False
        if job.task.done() or not job.cancel_allowed:
            return False
        if not job.cancel_requested:
            job.cancel_requested = True
            job.task.cancel()
        return True

    def release(self, job: ActiveJob) -> None:
        if self._jobs.get(job.token) is job:
            self._jobs.pop(job.token)
            self._keys.pop(job.key, None)
