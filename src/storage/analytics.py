from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import logging
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from aiogram.types import User
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.exporters.formats import DEFAULT_FORMATS, normalize_formats
from src.sources.registry import is_supported_story_url, normalize_url
from src.storage.alert_retries import AlertRetryStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UserStats:
    user_id: int
    username: str
    full_name: str
    downloads: int
    last_download_url: str


@dataclass(frozen=True, slots=True)
class FanficStats:
    url: str
    title: str
    downloads: int


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    started_users: int
    active_users_30d: int
    download_attempts: int
    successful_downloads: int
    success_ratio: float
    users: list[UserStats]
    top_fanfics: list[FanficStats]


@dataclass(frozen=True, slots=True)
class UserDownloadSettings:
    formats: tuple[str, ...] = DEFAULT_FORMATS
    chapter_selection_enabled: bool = False
    cover_enabled: bool = True


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    download_records: int
    profile_records: int


class AnalyticsStore:
    def __init__(self, database_url: str, *, history_retention_days: int = 30, initialize: bool = True) -> None:
        if history_retention_days < 30:
            raise ValueError("History retention must be at least 30 days to preserve active_users_30d")
        self.database_url = database_url
        self.history_retention_days = history_retention_days
        self.lock = Lock()
        self._pool_lock = Lock()
        self._closed = False
        self._pool = ConnectionPool(
            database_url, min_size=1, max_size=4, open=False, timeout=5, max_waiting=32,
            kwargs={'autocommit': True, 'row_factory': dict_row, 'connect_timeout': 5},
        )
        self._settings_lock = Lock()
        self._settings_write_locks = tuple(Lock() for _ in range(64))
        self._settings_cache: OrderedDict[int, tuple[float, UserDownloadSettings]] = OrderedDict()
        self._settings_generation = 0
        self.alert_retries = AlertRetryStore(self._connect)
        if initialize:
            try:
                self._initialize()
            except Exception:
                self.close()
                raise

    def record_user(self, user: User, *, started: bool = False) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO bot_users
                    (user_id, username, full_name, first_seen_at, last_seen_at, started_at, profile_expires_at)
                VALUES (%s, %s, %s, now(), now(), CASE WHEN %s THEN now() ELSE NULL END,
                        now() + %s * interval '1 day')
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_seen_at = now(),
                    profile_expires_at = EXCLUDED.profile_expires_at,
                    started_at = CASE
                        WHEN %s THEN COALESCE(bot_users.started_at, now())
                        ELSE bot_users.started_at
                    END
                """,
                (user.id, user.username or "", user.full_name or "", started, self.history_retention_days, started),
            )

    def start_download(self, user_id: int, url: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO downloads (user_id, url, status, attempted_at, privacy_expires_at)
                VALUES (%s, %s, 'attempt', now(), now() + %s * interval '1 day')
                RETURNING id
                """,
                (user_id, _public_story_url(url), self.history_retention_days),
            ).fetchone()
            return int(row["id"])

    def finish_download(self, download_id: int, *, success: bool, url: str, title: str = "", error: str = "") -> None:
        status = "success" if success else "error"
        with self._connect() as db:
            db.execute(
                """
                UPDATE downloads
                SET status = %s, url = %s, title = %s, error = %s, finished_at = now()
                WHERE id = %s AND user_id IS NOT NULL
                """,
                (status, _public_story_url(url), title, error[:500], download_id),
            )

    def discard_download(self, download_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM downloads WHERE id = %s AND user_id IS NOT NULL", (download_id,))

    def enroll_legacy_history(self) -> int:
        """Opt existing records into retention only through an explicit maintenance command."""
        with self.lock, self._connect() as db, db.transaction():
            db.execute("SET LOCAL statement_timeout = '15s'")
            result = db.execute(
                """
                UPDATE downloads
                SET privacy_expires_at = attempted_at + %s * interval '1 day'
                WHERE privacy_expires_at IS NULL AND user_id IS NOT NULL
                """,
                (self.history_retention_days,),
            )
            db.execute(
                """
                UPDATE bot_users
                SET profile_expires_at = last_seen_at + %s * interval '1 day'
                WHERE profile_expires_at IS NULL
                """,
                (self.history_retention_days,),
            )
            return result.rowcount

    def cleanup_expired_history(self, *, batch_size: int = 1000) -> int:
        if not 1 <= batch_size <= 10000:
            raise ValueError("Cleanup batch size must be between 1 and 10000")
        with self.lock, self._connect() as db, db.transaction():
            db.execute("SET LOCAL statement_timeout = '15s'")
            db.execute('DELETE FROM alert_retries WHERE expires_at <= now()')
            expired = list(db.execute(
                """
                SELECT id, url FROM downloads
                WHERE user_id IS NOT NULL AND privacy_expires_at <= now()
                ORDER BY privacy_expires_at, id
                LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (batch_size,),
            ))
            if expired:
                ids = [row["id"] for row in expired]
                db.execute(
                    """
                    UPDATE bot_users u
                    SET archived_successful_downloads = u.archived_successful_downloads + totals.successes
                    FROM (
                        SELECT user_id, COUNT(*) AS successes FROM downloads
                        WHERE id = ANY(%s) AND status = 'success' GROUP BY user_id
                    ) totals
                    WHERE u.user_id = totals.user_id
                    """,
                    (ids,),
                )
                with db.cursor() as cursor:
                    cursor.executemany(
                        "UPDATE downloads SET user_id = NULL, error = '', url = %s WHERE id = %s",
                        [(_public_story_url(row["url"]), row["id"]) for row in expired],
                    )
            db.execute(
                """
                UPDATE bot_users SET username = '', full_name = ''
                WHERE profile_expires_at <= now() AND (username <> '' OR full_name <> '')
                """
            )
            return len(expired)

    def retention_preview(self, *, include_legacy: bool = False) -> RetentionPreview:
        with self.lock, self._connect() as db, db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '15s'")
            downloads = db.execute(
                """
                SELECT COUNT(*) FROM downloads WHERE user_id IS NOT NULL AND (
                    privacy_expires_at <= now() OR
                    (%s AND privacy_expires_at IS NULL AND attempted_at <= now() - %s * interval '1 day')
                )
                """,
                (include_legacy, self.history_retention_days),
            ).fetchone()["count"]
            profiles = db.execute(
                """
                SELECT COUNT(*) FROM bot_users WHERE (username <> '' OR full_name <> '') AND (
                    profile_expires_at <= now() OR
                    (%s AND profile_expires_at IS NULL AND last_seen_at <= now() - %s * interval '1 day')
                )
                """,
                (include_legacy, self.history_retention_days),
            ).fetchone()["count"]
        return RetentionPreview(int(downloads), int(profiles))

    def get_user_formats(self, user_id: int) -> tuple[str, ...]:
        return self.get_user_download_settings(user_id).formats

    def save_user_formats(self, user_id: int, formats: tuple[str, ...]) -> None:
        settings = self.get_user_download_settings(user_id)
        self.save_user_download_settings(
            user_id,
            UserDownloadSettings(
                normalize_formats(formats),
                settings.chapter_selection_enabled,
                settings.cover_enabled,
            ),
        )

    def get_chapter_selection_enabled(self, user_id: int) -> bool:
        return self.get_user_download_settings(user_id).chapter_selection_enabled

    def save_chapter_selection_enabled(self, user_id: int, enabled: bool) -> None:
        settings = self.get_user_download_settings(user_id)
        self.save_user_download_settings(
            user_id,
            UserDownloadSettings(settings.formats, enabled, settings.cover_enabled),
        )

    def get_cover_enabled(self, user_id: int) -> bool:
        return self.get_user_download_settings(user_id).cover_enabled

    def save_cover_enabled(self, user_id: int, enabled: bool) -> None:
        settings = self.get_user_download_settings(user_id)
        self.save_user_download_settings(
            user_id,
            UserDownloadSettings(settings.formats, settings.chapter_selection_enabled, enabled),
        )

    def get_user_download_settings(self, user_id: int) -> UserDownloadSettings:
        with self._settings_lock:
            generation = self._settings_generation
            cached = self._settings_cache.get(user_id)
            if cached is not None and cached[0] > monotonic():
                self._settings_cache.move_to_end(user_id)
                return cached[1]
        with self._connect() as db:
            row = db.execute(
                """
                SELECT formats, chapter_selection_enabled, cover_enabled
                FROM user_format_settings
                WHERE user_id = %s
                """,
                (user_id,),
            ).fetchone()
        settings = UserDownloadSettings() if not row else UserDownloadSettings(
            formats=normalize_formats(list(row["formats"] or [])),
            chapter_selection_enabled=bool(row["chapter_selection_enabled"]),
            cover_enabled=bool(row["cover_enabled"]),
        )
        with self._settings_lock:
            if generation == self._settings_generation:
                self._cache_settings(user_id, settings)
            else:
                cached = self._settings_cache.get(user_id)
                if cached is not None and cached[0] > monotonic():
                    return cached[1]
        return settings

    def save_user_download_settings(self, user_id: int, settings: UserDownloadSettings) -> None:
        with self._settings_write_locks[user_id % len(self._settings_write_locks)]:
            self._save_settings(user_id, settings)

    def _save_settings(self, user_id: int, settings: UserDownloadSettings) -> None:
        normalized = normalize_formats(settings.formats)
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO user_format_settings
                    (user_id, formats, chapter_selection_enabled, cover_enabled, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    formats = EXCLUDED.formats,
                    chapter_selection_enabled = EXCLUDED.chapter_selection_enabled,
                    cover_enabled = EXCLUDED.cover_enabled,
                    updated_at = now()
                """,
                (
                    user_id,
                    list(normalized),
                    settings.chapter_selection_enabled,
                    settings.cover_enabled,
                ),
            )

        with self._settings_lock:
            self._settings_generation += 1
            self._cache_settings(user_id, UserDownloadSettings(
                normalized, settings.chapter_selection_enabled, settings.cover_enabled,
            ))

    def _cache_settings(self, user_id: int, settings: UserDownloadSettings) -> None:
        self._settings_cache[user_id] = (monotonic() + 60, settings)
        self._settings_cache.move_to_end(user_id)
        while len(self._settings_cache) > 2048:
            self._settings_cache.popitem(last=False)

    def snapshot(self, *, include_users: bool = True) -> AnalyticsSnapshot:
        with self._connect() as db, db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '15s'")
            started = _scalar(db, "SELECT COUNT(*) FROM bot_users WHERE started_at IS NOT NULL")
            active = _scalar(
                db,
                """
                SELECT COUNT(DISTINCT user_id)
                FROM downloads
                WHERE attempted_at >= now() - interval '30 days'
                """,
            )
            attempts = _scalar(db, "SELECT COUNT(*) FROM downloads")
            successes = _scalar(db, "SELECT COUNT(*) FROM downloads WHERE status = 'success'")
            users = [
                UserStats(
                    user_id=int(row["user_id"]),
                    username=row["username"] or "",
                    full_name=row["full_name"] or "",
                    downloads=int(row["downloads"] or 0),
                    last_download_url=row["last_download_url"] or "",
                )
                for row in db.execute(
                    """
                    SELECT u.user_id, u.username, u.full_name,
                        u.archived_successful_downloads + COUNT(d.id) AS downloads,
                        (
                            SELECT d2.url
                            FROM downloads d2
                            WHERE d2.user_id = u.user_id AND d2.status = 'success'
                              AND (d2.privacy_expires_at IS NULL OR d2.privacy_expires_at > now())
                            ORDER BY d2.finished_at DESC NULLS LAST, d2.id DESC
                            LIMIT 1
                        ) AS last_download_url
                    FROM bot_users u
                    LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success'
                    GROUP BY u.user_id
                    ORDER BY downloads DESC, u.last_seen_at DESC
                    """
                )
            ] if include_users else []
            top = [
                FanficStats(url=row["url"], title=row["title"] or row["url"], downloads=int(row["downloads"]))
                for row in db.execute(
                    """
                    SELECT url, COALESCE(NULLIF(MAX(title), ''), url) AS title, COUNT(*) AS downloads
                    FROM downloads
                    WHERE status = 'success'
                    GROUP BY url
                    ORDER BY downloads DESC, MAX(finished_at) DESC NULLS LAST
                    LIMIT 5
                    """
                )
            ]
        ratio = successes / attempts if attempts else 0.0
        return AnalyticsSnapshot(started, active, attempts, successes, ratio, users, top)

    def _initialize(self) -> None:
        with self.lock, self._connect() as db, db.transaction():
            self.alert_retries.initialize(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    started_at TIMESTAMPTZ
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('attempt', 'success', 'error')),
                    error TEXT NOT NULL DEFAULT '',
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ
                )
                """
            )
            db.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS archived_successful_downloads BIGINT NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS profile_expires_at TIMESTAMPTZ")
            db.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS privacy_expires_at TIMESTAMPTZ")
            db.execute("ALTER TABLE downloads ALTER COLUMN user_id DROP NOT NULL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_format_settings (
                    user_id BIGINT PRIMARY KEY REFERENCES bot_users(user_id) ON DELETE CASCADE,
                    formats TEXT[] NOT NULL DEFAULT ARRAY['fb2','epub','txt','docx','pdf'],
                    chapter_selection_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    cover_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT user_format_settings_formats_not_empty CHECK (array_length(formats, 1) > 0),
                    CONSTRAINT user_format_settings_formats_allowed CHECK (formats <@ ARRAY['fb2','epub','txt','docx','pdf']::TEXT[])
                )
                """
            )
            db.execute(
                """
                DO $$
                DECLARE constraint_name TEXT;
                BEGIN
                    FOR constraint_name IN
                        SELECT c.conname
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = 'user_format_settings'
                          AND t.relnamespace = to_regnamespace(current_schema())
                          AND c.contype = 'c'
                          AND pg_get_constraintdef(c.oid) LIKE '%<@%'
                    LOOP
                        EXECUTE format('ALTER TABLE user_format_settings DROP CONSTRAINT %I', constraint_name);
                    END LOOP;
                END $$;
                """
            )
            db.execute(
                """
                ALTER TABLE user_format_settings
                ADD CONSTRAINT user_format_settings_formats_allowed
                CHECK (formats <@ ARRAY['fb2','epub','txt','docx','pdf']::TEXT[])
                """
            )
            db.execute(
                """
                ALTER TABLE user_format_settings
                ADD COLUMN IF NOT EXISTS chapter_selection_enabled BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            db.execute(
                """
                ALTER TABLE user_format_settings
                ADD COLUMN IF NOT EXISTS cover_enabled BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user_status ON downloads(user_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_downloads_attempted_at ON downloads(attempted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_downloads_url_status ON downloads(url, status)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_privacy_expiry ON downloads(privacy_expires_at, id) "
                "WHERE user_id IS NOT NULL"
            )
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_summary AS
                SELECT
                    (SELECT COUNT(*) FROM bot_users WHERE started_at IS NOT NULL) AS started_users,
                    (
                        SELECT COUNT(DISTINCT user_id)
                        FROM downloads
                        WHERE attempted_at >= now() - interval '30 days'
                    ) AS active_users_30d,
                    (SELECT COUNT(*) FROM downloads) AS download_attempts,
                    (SELECT COUNT(*) FROM downloads WHERE status = 'success') AS successful_downloads,
                    COALESCE(
                        (SELECT COUNT(*)::double precision FROM downloads WHERE status = 'success')
                        / NULLIF((SELECT COUNT(*)::double precision FROM downloads), 0),
                        0
                    ) AS success_ratio
                """
            )
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_users AS
                SELECT u.user_id, u.username, u.full_name,
                    u.archived_successful_downloads + COUNT(d.id) AS downloaded_fanfics,
                    (
                        SELECT d2.url
                        FROM downloads d2
                        WHERE d2.user_id = u.user_id AND d2.status = 'success'
                          AND (d2.privacy_expires_at IS NULL OR d2.privacy_expires_at > now())
                        ORDER BY d2.finished_at DESC NULLS LAST, d2.id DESC
                        LIMIT 1
                    ) AS last_download_url,
                    u.first_seen_at,
                    u.last_seen_at,
                    u.started_at
                FROM bot_users u
                LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success'
                GROUP BY u.user_id
                """
            )
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_top_fanfics AS
                SELECT url, COALESCE(NULLIF(MAX(title), ''), url) AS title, COUNT(*) AS downloads
                FROM downloads
                WHERE status = 'success'
                GROUP BY url
                ORDER BY downloads DESC, MAX(finished_at) DESC NULLS LAST
                LIMIT 5
                """
            )

    def _connect(self):
        with self._pool_lock:
            if self._closed:
                raise RuntimeError('Analytics store is closed')
            if self._pool.closed:
                self._pool.open()
        return self._pool.connection()

    def close(self) -> None:
        with self._pool_lock:
            if not self._closed:
                self._closed = True
                self._pool.close()


def render_prometheus(snapshot: AnalyticsSnapshot) -> str:
    lines = [
        "# HELP ficbook_bot_started_users_total Users who sent /start.",
        "# TYPE ficbook_bot_started_users_total gauge",
        f"ficbook_bot_started_users_total {snapshot.started_users}",
        "# HELP ficbook_bot_active_users_30d Users who sent a download link in the last 30 days.",
        "# TYPE ficbook_bot_active_users_30d gauge",
        f"ficbook_bot_active_users_30d {snapshot.active_users_30d}",
        "# HELP ficbook_bot_download_attempts_total Total download attempts.",
        "# TYPE ficbook_bot_download_attempts_total gauge",
        f"ficbook_bot_download_attempts_total {snapshot.download_attempts}",
        "# HELP ficbook_bot_successful_downloads_total Successful download attempts.",
        "# TYPE ficbook_bot_successful_downloads_total gauge",
        f"ficbook_bot_successful_downloads_total {snapshot.successful_downloads}",
        "# HELP ficbook_bot_download_success_ratio Successful downloads divided by all attempts.",
        "# TYPE ficbook_bot_download_success_ratio gauge",
        f"ficbook_bot_download_success_ratio {snapshot.success_ratio:.6f}",
    ]
    lines.extend(
        [
            "# HELP ficbook_bot_top_fanfic_downloads Top 5 fanfics by successful downloads.",
            "# TYPE ficbook_bot_top_fanfic_downloads gauge",
        ]
    )
    for rank, fanfic in enumerate(snapshot.top_fanfics, 1):
        labels = _labels(rank=str(rank))
        lines.append(f"ficbook_bot_top_fanfic_downloads{{{labels}}} {fanfic.downloads}")
    return "\n".join(lines) + "\n"


def _labels(**values: str) -> str:
    return ",".join(f'{key}="{_escape_label(value)}"' for key, value in values.items())


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _scalar(db: psycopg.Connection, query: str) -> int:
    return int(db.execute(query).fetchone()["count"] or 0)


def _public_story_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return ""
        query = ""
        if parts.hostname in {"hogwartsnet.ru", "www.hogwartsnet.ru"}:
            fid = parse_qs(parts.query).get("fid", [""])[0]
            if not fid.isdecimal():
                return ""
            query = urlencode({"fid": fid})
        public_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        return normalize_url(public_url) if is_supported_story_url(public_url) else ""
    except ValueError:
        return ""
