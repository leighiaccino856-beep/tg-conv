"""Настройки бота из переменных окружения / файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise SystemExit(f"Переменная {name} должна быть числом, получено: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Все настройки приложения."""

    bot_token: str
    api_server: str | None
    api_ip: str | None
    max_input_mb: int
    note_max_mb: float
    fallback_note_mb: float
    max_side: int
    max_duration: int
    ffmpeg_preset: str
    max_concurrent: int
    download_timeout: int
    convert_timeout: int
    log_level: str

    @property
    def max_input_bytes(self) -> int:
        return self.max_input_mb * 1024 * 1024

    @property
    def note_max_bytes(self) -> int:
        return int(self.note_max_mb * 1024 * 1024)

    @property
    def fallback_note_bytes(self) -> int:
        return int(self.fallback_note_mb * 1024 * 1024)

    @classmethod
    def from_env(cls) -> "Settings":
        api_server = _env_str("TELEGRAM_API_SERVER") or None
        return cls(
            bot_token=_env_str("BOT_TOKEN"),
            api_server=api_server,
            api_ip=_env_str("TELEGRAM_API_IP") or None,
            max_input_mb=max(1, _env_int("MAX_INPUT_MB", 20)),
            note_max_mb=max(0.1, _env_float("NOTE_MAX_MB", 5.0)),
            fallback_note_mb=max(0.1, _env_float("FALLBACK_NOTE_MB", 1.0)),
            # Telegram не примет кружок больше 640 px и длиннее 60 секунд.
            max_side=min(640, max(16, _env_int("MAX_SIDE", 640))),
            max_duration=min(60, max(1, _env_int("MAX_DURATION", 60))),
            ffmpeg_preset=_env_str("FFMPEG_PRESET", "fast") or "fast",
            max_concurrent=max(1, _env_int("MAX_CONCURRENT", 2)),
            download_timeout=max(1, _env_int("DOWNLOAD_TIMEOUT", 600)),
            convert_timeout=max(1, _env_int("CONVERT_TIMEOUT", 600)),
            log_level=(_env_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        )


settings = Settings.from_env()
