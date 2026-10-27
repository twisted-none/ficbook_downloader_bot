from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from secrets import token_urlsafe

from psycopg import Connection

from src.exporters.formats import ALLOWED_FORMATS
from src.sources.registry import is_supported_story_url


@dataclass(frozen=True, slots=True)
class AlertRetry:
    token: str
    user_id: int
    chat_id: int
    url: str
    formats: tuple[str, ...]
    cover_enabled: bool
    chapter_numbers: tuple[int, ...] | None
    sent_formats: tuple[str, ...] = ()
    delivered: bool = False


def _release_connection(db: Connection, connection: AbstractContextManager, token: str, locked: bool) -> None:
    try:
        if locked:
            db.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 78124))', (token,))
    except BaseException:
        db.close()
        raise
    finally:
        connection.__exit__(None, None, None)


class RetryClaim:
    """Keep a session lock until delivery ends; a process crash releases it in PostgreSQL."""

    def __init__(self, db: Connection, connection: AbstractContextManager, request: AlertRetry):
        self.db = db
        self.connection = connection
        self.request = request

    def record_sent(self, fmt: str) -> None:
        if fmt not in self.request.formats or fmt in self.request.sent_formats:
            raise ValueError('Unexpected delivered format')
        row = self.db.execute(
            """UPDATE alert_retries SET
                sent_formats = array_append(sent_formats, %s),
                delivered = formats <@ array_append(sent_formats, %s)
               WHERE token = %s RETURNING sent_formats, delivered""",
            (fmt, fmt, self.request.token),
        ).fetchone()
        if row is None:
            raise RuntimeError('Retry request expired during delivery')
        self.request = replace(self.request, sent_formats=tuple(row['sent_formats']), delivered=row['delivered'])

    def close(self) -> None:
        _release_connection(self.db, self.connection, self.request.token, True)


class AlertRetryStore:
    def __init__(self, connect: Callable[[], AbstractContextManager[Connection]]):
        self.connect = connect

    @staticmethod
    def initialize(db: Connection) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alert_retries (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                url TEXT NOT NULL,
                formats TEXT[] NOT NULL,
                cover_enabled BOOLEAN NOT NULL,
                chapter_numbers INTEGER[],
                sent_formats TEXT[] NOT NULL DEFAULT '{}',
                delivered BOOLEAN NOT NULL DEFAULT FALSE,
                bot_id BIGINT NOT NULL,
                admin_chat_id BIGINT NOT NULL,
                message_id BIGINT,
                expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days'
            )
        """)
        db.execute('CREATE INDEX IF NOT EXISTS idx_alert_retries_expiry ON alert_retries(expires_at)')

    def create(self, *, user_id: int, chat_id: int, url: str, formats: tuple[str, ...],
               cover_enabled: bool, chapter_numbers: set[int] | None,
               bot_id: int, admin_chat_id: int) -> str:
        if (not formats or len(set(formats)) != len(formats) or not set(formats) <= set(ALLOWED_FORMATS)
                or len(url) > 4096 or not is_supported_story_url(url) or user_id <= 0
                or (chapter_numbers is not None and (not chapter_numbers or len(chapter_numbers) > 10000
                                                     or any(n < 1 for n in chapter_numbers)))):
            raise ValueError('Invalid retry request')
        token = token_urlsafe(18)
        with self.connect() as db:
            db.execute("""
                INSERT INTO alert_retries
                    (token, user_id, chat_id, url, formats, cover_enabled, chapter_numbers, bot_id, admin_chat_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (token, user_id, chat_id, url, list(formats), cover_enabled,
                      sorted(chapter_numbers) if chapter_numbers is not None else None, bot_id, admin_chat_id))
        return token

    def bind_message(self, token: str, message_id: int) -> None:
        with self.connect() as db:
            db.execute('UPDATE alert_retries SET message_id = %s WHERE token = %s AND message_id IS NULL',
                       (message_id, token))

    def claim(self, token: str, bot_id: int, admin_chat_id: int, message_id: int) -> RetryClaim | None:
        connection = self.connect()
        db = connection.__enter__()
        locked = False
        try:
            locked = db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 78124)) AS locked',
                                (token,)).fetchone()['locked']
            if locked:
                row = db.execute("""SELECT * FROM alert_retries WHERE token = %s AND bot_id = %s
                    AND admin_chat_id = %s AND message_id = %s AND expires_at > now()""",
                    (token, bot_id, admin_chat_id, message_id)).fetchone()
                if row is not None:
                    request = AlertRetry(
                        row['token'], row['user_id'], row['chat_id'], row['url'], tuple(row['formats']),
                        row['cover_enabled'], tuple(row['chapter_numbers']) if row['chapter_numbers'] is not None else None,
                        tuple(row['sent_formats']), row['delivered'],
                    )
                    return RetryClaim(db, connection, request)
        except BaseException:
            _release_connection(db, connection, token, locked)
            raise
        _release_connection(db, connection, token, locked)
        return None
