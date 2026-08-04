from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock

from aiogram.types import User
import psycopg
from psycopg.rows import dict_row

from src.exporters.formats import DEFAULT_FORMATS, normalize_formats

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


class AnalyticsStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.lock = Lock()
        self._initialize()

    def record_user(self, user: User, *, started: bool = False) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO bot_users (user_id, username, full_name, first_seen_at, last_seen_at, started_at)
                VALUES (%s, %s, %s, now(), now(), CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_seen_at = now(),
                    started_at = CASE
                        WHEN %s THEN COALESCE(bot_users.started_at, now())
                        ELSE bot_users.started_at
                    END
                """,
                (user.id, user.username or "", user.full_name or "", started, started),
            )

    def start_download(self, user_id: int, url: str) -> int:
        with self.lock, self._connect() as db:
            row = db.execute(
                """
                INSERT INTO downloads (user_id, url, status, attempted_at)
                VALUES (%s, %s, 'attempt', now())
                RETURNING id
                """,
                (user_id, url),
            ).fetchone()
            return int(row["id"])

    def finish_download(self, download_id: int, *, success: bool, url: str, title: str = "", error: str = "") -> None:
        status = "success" if success else "error"
        with self.lock, self._connect() as db:
            db.execute(
                """
                UPDATE downloads
                SET status = %s, url = %s, title = %s, error = %s, finished_at = now()
                WHERE id = %s
                """,
                (status, url, title, error[:500], download_id),
            )

    def discard_download(self, download_id: int) -> None:
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM downloads WHERE id = %s", (download_id,))

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
        with self.lock, self._connect() as db:
            row = db.execute(
                """
                SELECT formats, chapter_selection_enabled, cover_enabled
                FROM user_format_settings
                WHERE user_id = %s
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return UserDownloadSettings()
        return UserDownloadSettings(
            formats=normalize_formats(list(row["formats"] or [])),
            chapter_selection_enabled=bool(row["chapter_selection_enabled"]),
            cover_enabled=bool(row["cover_enabled"]),
        )

    def save_user_download_settings(self, user_id: int, settings: UserDownloadSettings) -> None:
        normalized = normalize_formats(settings.formats)
        with self.lock, self._connect() as db:
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

    def snapshot(self) -> AnalyticsSnapshot:
        with self.lock, self._connect() as db:
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
                        COUNT(d.id) AS downloads,
                        (
                            SELECT d2.url
                            FROM downloads d2
                            WHERE d2.user_id = u.user_id AND d2.status = 'success'
                            ORDER BY d2.finished_at DESC NULLS LAST, d2.id DESC
                            LIMIT 1
                        ) AS last_download_url
                    FROM bot_users u
                    LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success'
                    GROUP BY u.user_id
                    ORDER BY downloads DESC, u.last_seen_at DESC
                    """
                )
            ]
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
        with self.lock, self._connect() as db:
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
                    user_id BIGINT NOT NULL REFERENCES bot_users(user_id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('attempt', 'success', 'error')),
                    error TEXT NOT NULL DEFAULT '',
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ
                )
                """
            )
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
                    COUNT(d.id) AS downloaded_fanfics,
                    (
                        SELECT d2.url
                        FROM downloads d2
                        WHERE d2.user_id = u.user_id AND d2.status = 'success'
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

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)


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
        "# HELP ficbook_bot_user_downloads_total Successful downloads by user.",
        "# TYPE ficbook_bot_user_downloads_total gauge",
    ]
    for user in snapshot.users:
        labels = _labels(
            user_id=str(user.user_id),
            username=user.username,
            full_name=user.full_name,
            last_download_url=user.last_download_url,
        )
        lines.append(f"ficbook_bot_user_downloads_total{{{labels}}} {user.downloads}")
    lines.extend(
        [
            "# HELP ficbook_bot_top_fanfic_downloads Top 5 fanfics by successful downloads.",
            "# TYPE ficbook_bot_top_fanfic_downloads gauge",
        ]
    )
    for rank, fanfic in enumerate(snapshot.top_fanfics, 1):
        labels = _labels(rank=str(rank), title=fanfic.title, url=fanfic.url)
        lines.append(f"ficbook_bot_top_fanfic_downloads{{{labels}}} {fanfic.downloads}")
    return "\n".join(lines) + "\n"


def _labels(**values: str) -> str:
    return ",".join(f'{key}="{_escape_label(value)}"' for key, value in values.items())


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _scalar(db: psycopg.Connection, query: str) -> int:
    return int(db.execute(query).fetchone()["count"] or 0)
