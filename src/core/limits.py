from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial, wraps
import asyncio
from io import BytesIO
import os
from time import monotonic
import warnings
import zlib
from urllib.error import URLError


class ResourceLimitError(Exception):
    def __init__(self, message: str = 'Произведение превышает безопасный размер обработки.') -> None:
        super().__init__(message)
        self.user_message = message


class ResponseLimitError(ResourceLimitError):
    """A single HTTP response exceeds the byte budget."""


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_response_bytes: int = 16 * 1024 * 1024
    max_response_seconds: int = 120
    max_cover_pixels: int = 16_000_000
    max_story_bytes: int = 32 * 1024 * 1024
    max_chapters: int = 2000
    max_file_bytes: int = 49_000_000
    max_output_bytes: int = 100 * 1024 * 1024
    max_job_seconds: int = 1800
    max_queue_jobs: int = 500
    max_active_jobs: int = 5
    session_ttl_seconds: int = 1800
    max_sessions: int = 1000
    max_preview_bytes: int = 256 * 1024
    max_preview_chapters: int = 10000
    max_collection_works: int = 1000
    max_collection_pages: int = 100
    max_collection_batches: int = 10

    @classmethod
    def from_environment(cls) -> 'ResourceLimits':
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            value = int(os.getenv('BOT_' + name.upper(), str(getattr(defaults, name))))
            if value <= 0:
                raise ValueError('BOT_' + name.upper() + ' must be positive')
            values[name] = value
        return cls(**values)


@dataclass(slots=True)
class _Budget:
    limits: ResourceLimits
    deadline: float
    chapters: int = 0
    story_bytes: int = 0


_budget: ContextVar[_Budget | None] = ContextVar('download_budget', default=None)
_download_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='downloads')


@contextmanager
def resource_scope(limits: ResourceLimits | None = None):
    if _budget.get() is not None:
        check_deadline()
        yield
        return
    limits = limits or ResourceLimits.from_environment()
    token = _budget.set(_Budget(limits, monotonic() + limits.max_job_seconds))
    try:
        yield
        check_deadline()
    finally:
        _budget.reset(token)


def bounded_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with resource_scope():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def suspend_deadline():
    budget = _budget.get()
    started = monotonic()
    token = _budget.set(None)
    try:
        yield
    finally:
        _budget.reset(token)
        if budget is not None and budget.deadline > 0:
            budget.deadline += monotonic() - started


def bounded_async_operation(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with resource_scope():
            return await function(*args, **kwargs)
    return wrapped


@bounded_async_operation
async def run_worker(function, *args, **kwargs):
    task = asyncio.get_running_loop().run_in_executor(
        _download_executor, copy_context().run, partial(function, *args, **kwargs),
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        budget = _budget.get()
        if budget is not None:
            budget.deadline = 0
        # Cancelling to_thread does not stop its worker. Retain the queue slot
        # until it exits, so cancelled jobs cannot bypass the concurrency limit.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def close_download_workers() -> None:
    _download_executor.shutdown(wait=True, cancel_futures=True)


class LimitedBytesIO(BytesIO):
    def __init__(self, initial_bytes=b''):
        self.limit = current_limits().max_file_bytes
        if len(initial_bytes) > self.limit:
            raise ResourceLimitError('Готовый файл превышает безопасный размер.')
        super().__init__(initial_bytes)

    def write(self, data):
        check_deadline()
        if self.tell() + len(data) > self.limit:
            raise ResourceLimitError('Готовый файл слишком большой. Выбери меньше глав.')
        return super().write(data)


def current_limits() -> ResourceLimits:
    budget = _budget.get()
    return budget.limits if budget else ResourceLimits.from_environment()


def check_deadline() -> None:
    budget = _budget.get()
    if budget is not None and monotonic() >= budget.deadline:
        raise ResourceLimitError('Обработка заняла слишком много времени. Попробуй выбрать меньше глав.')


def request_timeout(default: float = 30) -> float:
    check_deadline()
    budget = _budget.get()
    return min(default, max(0.01, budget.deadline - monotonic())) if budget else default


def bounded_sleep(seconds: float) -> None:
    from time import sleep
    sleep(request_timeout(seconds))
    check_deadline()


def check_chapter_count(count: int) -> None:
    check_deadline()
    if count > current_limits().max_chapters:
        raise ResourceLimitError('В произведении слишком много глав. Выбери отдельные главы для скачивания.')


def account_chapter(html: str) -> None:
    check_deadline()
    budget = _budget.get()
    if budget is None:
        return
    budget.chapters += 1
    budget.story_bytes += len(html.encode('utf-8'))
    check_chapter_count(budget.chapters)
    if budget.story_bytes > budget.limits.max_story_bytes:
        raise ResourceLimitError()


def validate_story(story) -> None:
    check_chapter_count(len(story.chapters))
    size = sum(len(chapter.html.encode('utf-8')) for chapter in story.chapters)
    size += len(story.annotation_html.encode('utf-8')) + len(story.description.encode('utf-8'))
    if story.cover:
        size += len(story.cover.data)
        if not image_is_safe(story.cover.data):
            story.cover = None
    if size > current_limits().max_story_bytes:
        raise ResourceLimitError()


def image_is_safe(payload: bytes) -> bool:
    from PIL import Image, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                return image.width * image.height <= current_limits().max_cover_pixels
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            UnidentifiedImageError, OSError, ValueError):
        return False


def validate_preview(preview) -> None:
    limits = current_limits()
    size = len(preview.title.encode('utf-8')) + len(preview.author.encode('utf-8'))
    size += sum(len(chapter.title.encode('utf-8')) for chapter in preview.chapters)
    if len(preview.chapters) > limits.max_preview_chapters or size > limits.max_preview_bytes:
        raise ResourceLimitError('Список глав слишком большой для безопасной обработки.')


def read_bounded(response, max_bytes: int | None = None) -> bytes:
    check_deadline()
    limit = current_limits().max_response_bytes if max_bytes is None else max_bytes
    headers = getattr(response, 'headers', {})
    declared = headers.get('Content-Length', '')
    if isinstance(declared, str) and declared.isdecimal() and int(declared) > limit:
        raise ResponseLimitError('Ответ сайта превышает безопасный размер загрузки.')
    # read1 returns available bytes rather than waiting for an entire large body.
    if callable(getattr(type(response), 'read1', None)):
        data = bytearray()
        while len(data) <= limit:
            check_deadline()
            chunk = response.read1(min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        payload = bytes(data)
    else:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ResponseLimitError('Ответ сайта превышает безопасный размер загрузки.')
    if getattr(response, '_deadline_expired', False) is True:
        raise ResourceLimitError('Сайт слишком долго отдаёт ответ. Попробуй повторить позже.')
    if isinstance(declared, str) and declared.isdecimal() and len(payload) != int(declared):
        raise URLError('Incomplete external response')
    encoding = headers.get('Content-Encoding', '')
    if isinstance(encoding, str) and encoding.lower() not in {'', 'identity'}:
        if encoding.lower() != 'gzip':
            raise ResourceLimitError('Сайт вернул неподдерживаемое сжатие ответа.')
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        payload = decoder.decompress(payload, limit + 1)
        if len(payload) > limit or decoder.unconsumed_tail:
            raise ResponseLimitError('Распакованный ответ сайта превышает безопасный размер.')
        if not decoder.eof or decoder.unused_data:
            raise ResourceLimitError('Сайт вернул некорректный сжатый ответ.')
    check_deadline()
    return payload
