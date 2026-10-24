"""Настройки бота из переменных окружения / файла .env."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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


_PROXY_SCHEMES = {"http", "socks4", "socks5"}


def _env_proxy() -> str | None:
    """Прокси для Telegram: http://, socks4://, socks5://. Без схемы считаем http."""
    raw = _env_str("TELEGRAM_PROXY")
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _PROXY_SCHEMES:
        allowed = ", ".join(sorted(_PROXY_SCHEMES))
        raise SystemExit(
            f"TELEGRAM_PROXY: неизвестная схема {scheme!r}. Поддерживаются: {allowed}."
        )
    if not parts.hostname:
        raise SystemExit(f"TELEGRAM_PROXY: в адресе {raw!r} нет хоста.")
    try:
        port = parts.port
    except ValueError as exc:
        raise SystemExit(f"TELEGRAM_PROXY: некорректный порт в {raw!r}.") from exc
    if port is None:
        raise SystemExit(
            f"TELEGRAM_PROXY: укажи порт, например {scheme}://{parts.hostname}:1080."
        )
    return raw


@dataclass(frozen=True, slots=True)
class Settings:
    """Все настройки приложения."""

    bot_token: str
    api_server: str | None
    api_ip: str | None
    proxy: str | None
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
    def api_ips(self) -> tuple[str, ...]:
        """IP из TELEGRAM_API_IP: можно указать несколько через запятую."""
        if not self.api_ip:
            return ()
        ips: list[str] = []
        for raw in self.api_ip.split(","):
            candidate = raw.strip()
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError as exc:
                raise SystemExit(
                    f"TELEGRAM_API_IP: {candidate!r} — это не IP-адрес. "
                    "Укажи IPv4, например 149.154.167.220."
                ) from exc
            if candidate not in ips:
                ips.append(candidate)
        return tuple(ips)

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
            proxy=_env_proxy(),
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
