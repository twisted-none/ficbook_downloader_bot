from __future__ import annotations

import asyncio

from src.bot import run_bot
from src.config import load_settings, setup_logging


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
