from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    bot_token: str
    ficbook_login: str
    ficbook_password: str
    admin_chat_id: int | None
    log_level: str


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")
    return Settings(
        bot_token=token,
        ficbook_login=os.getenv("FICBOOK_LOGIN", "").strip(),
        ficbook_password=os.getenv("FICBOOK_PASSWORD", "").strip(),
        admin_chat_id=_parse_chat_id(os.getenv("ADMIN_CHAT_ID", "").strip()),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
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
