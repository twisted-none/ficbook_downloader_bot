from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock


class UserStoreError(Exception):
    """Existing user data cannot safely be read or changed."""


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self._cached_signature: tuple[int, int, int] | None = None
        self._cached_ids: frozenset[int] | None = None

    def add(self, user_id: int) -> None:
        with self.lock:
            user_ids = self._read()
            if user_id in user_ids:
                return
            user_ids = set(user_ids)
            user_ids.add(user_id)
            self._write(user_ids)

    def remove(self, user_id: int) -> None:
        with self.lock:
            user_ids = self._read()
            if user_id not in user_ids:
                return
            user_ids = set(user_ids)
            user_ids.remove(user_id)
            self._write(user_ids)

    def list_user_ids(self) -> list[int]:
        with self.lock:
            return sorted(self._read())

    def _read(self) -> frozenset[int]:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if self._cached_ids is not None and signature == self._cached_signature:
                return self._cached_ids
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._cached_signature = None
            self._cached_ids = None
            return frozenset()
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise UserStoreError('User registry is unreadable; refusing to overwrite it') from None
        if not isinstance(raw, list) or any(
            isinstance(item, bool) or not isinstance(item, (str, int))
            or not str(item).isascii() or not str(item).isdigit() or int(item) <= 0
            for item in raw
        ):
            raise UserStoreError('User registry has an invalid format; refusing to overwrite it')
        self._cached_ids = frozenset(int(item) for item in raw)
        self._cached_signature = signature
        return self._cached_ids

    def _write(self, user_ids: set[int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                    prefix='.' + self.path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(sorted(user_ids), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name == 'posix':
                descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        stat = self.path.stat()
        self._cached_signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        self._cached_ids = frozenset(user_ids)
