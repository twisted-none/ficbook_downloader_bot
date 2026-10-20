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

    def add(self, user_id: int) -> None:
        with self.lock:
            user_ids = self._read()
            if user_id in user_ids:
                return
            user_ids.add(user_id)
            self._write(user_ids)

    def remove(self, user_id: int) -> None:
        with self.lock:
            user_ids = self._read()
            if user_id not in user_ids:
                return
            user_ids.remove(user_id)
            self._write(user_ids)

    def list_user_ids(self) -> list[int]:
        with self.lock:
            return sorted(self._read())

    def _read(self) -> set[int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise UserStoreError('User registry is unreadable; refusing to overwrite it') from None
        if not isinstance(raw, list) or any(
            isinstance(item, bool) or not isinstance(item, (str, int))
            or not str(item).isascii() or not str(item).isdigit() or int(item) <= 0
            for item in raw
        ):
            raise UserStoreError('User registry has an invalid format; refusing to overwrite it')
        return {int(item) for item in raw}

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
