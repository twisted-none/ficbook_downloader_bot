from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    bot_token: str
    alert_bot_token: str
    ficbook_login: str
    ficbook_password: str
    ficbook_backup_login: str
    ficbook_backup_password: str
    ficbook_account_3_login: str
    ficbook_account_3_password: str
    ficbook_account_4_login: str
    ficbook_account_4_password: str
    ficbook_account_5_login: str
    ficbook_account_5_password: str
    ao3_login: str
    ao3_password: str
    wattpad_login: str
    wattpad_password: str
    hogwartsnet_login: str
    hogwartsnet_password: str
    litnet_login: str
    litnet_password: str
    admin_chat_id: int | None
    log_level: str
    data_dir: Path
    database_url: str
    metrics_host: str
    metrics_port: int
    ficbook_max_concurrent_downloads: int
    ficbook_download_interval_seconds: float
    ficbook_request_delay_seconds: float
    ficbook_retry_attempts: int
    ficbook_retry_base_delay_seconds: float
    ficbook_retry_max_delay_seconds: float
    premium_queue_user_ids: frozenset[int]


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")
    return Settings(
        bot_token=token,
        alert_bot_token=os.getenv("ALERT_BOT_TOKEN", "").strip(),
        ficbook_login=os.getenv("FICBOOK_LOGIN", "").strip(),
        ficbook_password=os.getenv("FICBOOK_PASSWORD", "").strip(),
        ficbook_backup_login=os.getenv("FICBOOK_BACKUP_LOGIN", "").strip(),
        ficbook_backup_password=os.getenv("FICBOOK_BACKUP_PASSWORD", "").strip(),
        ficbook_account_3_login=os.getenv("FICBOOK_ACCOUNT_3_LOGIN", "").strip(),
        ficbook_account_3_password=os.getenv("FICBOOK_ACCOUNT_3_PASSWORD", "").strip(),
        ficbook_account_4_login=os.getenv("FICBOOK_ACCOUNT_4_LOGIN", "").strip(),
        ficbook_account_4_password=os.getenv("FICBOOK_ACCOUNT_4_PASSWORD", "").strip(),
        ficbook_account_5_login=os.getenv("FICBOOK_ACCOUNT_5_LOGIN", "").strip(),
        ficbook_account_5_password=os.getenv("FICBOOK_ACCOUNT_5_PASSWORD", "").strip(),
        ao3_login=os.getenv("AO3_LOGIN", "").strip(),
        ao3_password=os.getenv("AO3_PASSWORD", "").strip(),
        wattpad_login=os.getenv("WATTPAD_LOGIN", "").strip(),
        wattpad_password=os.getenv("WATTPAD_PASSWORD", "").strip(),
        hogwartsnet_login=os.getenv("HOGWARTSNET_LOGIN", "").strip(),
        hogwartsnet_password=os.getenv("HOGWARTSNET_PASSWORD", "").strip(),
        litnet_login=os.getenv("LITNET_LOGIN", "").strip(),
        litnet_password=os.getenv("LITNET_PASSWORD", "").strip(),
        admin_chat_id=_parse_chat_id(os.getenv("ADMIN_CHAT_ID", "").strip()),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://ficbook:ficbook@postgres:5432/ficbook_bot",
        ).strip(),
        metrics_host=os.getenv("METRICS_HOST", "0.0.0.0").strip(),
        metrics_port=int(os.getenv("METRICS_PORT", "8000")),
        ficbook_max_concurrent_downloads=max(1, int(os.getenv("FICBOOK_MAX_CONCURRENT_DOWNLOADS", "1"))),
        ficbook_download_interval_seconds=max(0.0, float(os.getenv("FICBOOK_DOWNLOAD_INTERVAL_SECONDS", "8"))),
        ficbook_request_delay_seconds=max(0.0, float(os.getenv("FICBOOK_REQUEST_DELAY_SECONDS", "1.5"))),
        ficbook_retry_attempts=max(1, int(os.getenv("FICBOOK_RETRY_ATTEMPTS", "3"))),
        ficbook_retry_base_delay_seconds=max(0.0, float(os.getenv("FICBOOK_RETRY_BASE_DELAY_SECONDS", "8"))),
        ficbook_retry_max_delay_seconds=max(0.0, float(os.getenv("FICBOOK_RETRY_MAX_DELAY_SECONDS", "45"))),
        premium_queue_user_ids=_parse_user_ids(os.getenv("PREMIUM_QUEUE_USER_IDS", "")),
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parse_chat_id(value: str) -> int | None:
    if not value:
        return None
    return int(value)


def _parse_user_ids(value: str) -> frozenset[int]:
    user_ids: set[int] = set()
    for chunk in value.replace(";", ",").split(","):
        text = chunk.strip()
        if text:
            user_ids.add(int(text))
    return frozenset(user_ids)
