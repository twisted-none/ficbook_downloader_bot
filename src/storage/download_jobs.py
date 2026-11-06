"""Durable job descriptions and delivery checkpoints, owned by the bot event loop."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3


restoring_job: ContextVar[int | None] = ContextVar('restoring_job', default=None)


@dataclass(slots=True)
class SavedDownload:
    id: int
    payload: dict
    checkpoint: dict = field(default_factory=dict)


class DownloadJobs:
    def __init__(self, path: Path, bot_id: int, capacity: int, collection_capacity: int = 10):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.bot_id = bot_id
        self.capacity = capacity
        self.collection_capacity = collection_capacity
        self._collection_users: set[int] = set()
        self._collection_changed = asyncio.Condition()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL,
                payload TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '{}')''')
            self.db.execute('CREATE TABLE IF NOT EXISTS paused (bot_id INTEGER PRIMARY KEY)')
        self.entries = {row[0]: SavedDownload(row[0], json.loads(row[1]), json.loads(row[2]))
                        for row in self.db.execute('SELECT id, payload, checkpoint FROM downloads '
                                                   'WHERE bot_id = ? ORDER BY id', (bot_id,))}
        self.paused = self.db.execute('SELECT 1 FROM paused WHERE bot_id = ?', (bot_id,)).fetchone() is not None
        self.cutoff = 0
        self.generation = 0
        self.deferred: set[int] = set()

    def add(self, payload: dict) -> SavedDownload:
        if len(self.entries) >= self.capacity:
            raise OverflowError('Persistent download queue is full')
        with self.db:
            cursor = self.db.execute('INSERT INTO downloads (bot_id, payload) VALUES (?, ?)',
                                     (self.bot_id, json.dumps(payload)))
        entry = SavedDownload(cursor.lastrowid, payload)
        self.entries[entry.id] = entry
        return entry

    def save(self, entry: SavedDownload, **changes) -> None:
        checkpoint = {**entry.checkpoint, **changes}
        with self.db:
            self.db.execute('UPDATE downloads SET checkpoint = ? WHERE id = ? AND bot_id = ?',
                            (json.dumps(checkpoint), entry.id, self.bot_id))
        entry.checkpoint = checkpoint

    def finish(self, entry: SavedDownload) -> None:
        with self.db:
            self.db.execute('DELETE FROM downloads WHERE id = ? AND bot_id = ?', (entry.id, self.bot_id))
        self.entries.pop(entry.id, None)
        self.deferred.discard(entry.id)

    def pause(self) -> int:
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO paused VALUES (?)', (self.bot_id,))
        self.paused = True
        self.generation += 1
        self.cutoff = max(self.entries, default=0)
        return self.cutoff

    def resume(self) -> None:
        with self.db:
            self.db.execute('DELETE FROM paused WHERE bot_id = ?', (self.bot_id,))
        self.paused = False
        self.generation += 1
        self.cutoff = 0
        self.deferred.clear()

    @asynccontextmanager
    async def collection_slot(self, user_id: int):
        async with self._collection_changed:
            await self._collection_changed.wait_for(lambda: user_id not in self._collection_users
                and len(self._collection_users) < self.collection_capacity)
            self._collection_users.add(user_id)
        try:
            yield
        finally:
            async with self._collection_changed:
                self._collection_users.discard(user_id)
                self._collection_changed.notify_all()
