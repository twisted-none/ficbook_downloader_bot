from __future__ import annotations

import json
from pathlib import Path
from threading import Lock


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
        if not self.path.exists():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return {int(item) for item in raw if str(item).isdigit()}

    def _write(self, user_ids: set[int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(user_ids)), encoding="utf-8")
