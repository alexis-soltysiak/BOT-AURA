from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    discord_token: str
    database_path: Path
    log_level: str
    command_sync_guild_id: int | None
    aura_rebuild_allowed_user_id: int | None
    rebuild_pause_every: int
    rebuild_pause_seconds: float
    rebuild_progress_every: int


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required.")

    database_path = Path(os.getenv("DATABASE_PATH", "data/aura.sqlite3")).expanduser()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    guild_raw = os.getenv("COMMAND_SYNC_GUILD_ID", "").strip()
    command_sync_guild_id = int(guild_raw) if guild_raw else None
    allowed_user_raw = os.getenv("AURA_REBUILD_ALLOWED_USER_ID", "").strip()
    aura_rebuild_allowed_user_id = int(allowed_user_raw) if allowed_user_raw else None
    rebuild_pause_every = int(os.getenv("REBUILD_PAUSE_EVERY", "50"))
    rebuild_pause_seconds = float(os.getenv("REBUILD_PAUSE_SECONDS", "0.75"))
    rebuild_progress_every = int(os.getenv("REBUILD_PROGRESS_EVERY", "100"))

    return Settings(
        discord_token=token,
        database_path=database_path,
        log_level=log_level,
        command_sync_guild_id=command_sync_guild_id,
        aura_rebuild_allowed_user_id=aura_rebuild_allowed_user_id,
        rebuild_pause_every=rebuild_pause_every,
        rebuild_pause_seconds=rebuild_pause_seconds,
        rebuild_progress_every=rebuild_progress_every,
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_rebuild_start_date(
    *, day: int | None, month: int | None, year: int | None
) -> datetime | None:
    provided = [day is not None, month is not None, year is not None]
    if any(provided) and not all(provided):
        raise ValueError("day, month and year must all be provided together.")
    if not any(provided):
        return None
    return datetime(year=year, month=month, day=day, tzinfo=UTC)
