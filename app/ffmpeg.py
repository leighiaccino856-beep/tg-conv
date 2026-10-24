"""Работа с ffmpeg/ffprobe: зондирование файла и сжатие видео в кружок Telegram.

Кружок (video note) — это квадратное MP4-видео (H.264/AAC) со стороной
не больше 640 px и длительностью не больше 60 секунд.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_MAX_ATTEMPTS = 4

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video: ")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: ")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe недоступны или не смогли обработать файл."""


# --------------------------------------------------------------------------- #
# Поиск бинарников
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Путь к ffmpeg: системный в PATH или из пакета imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except ImportError:
        imageio_ffmpeg = None  # type: ignore[assignment]

    if imageio_ffmpeg is not None:
        return imageio_ffmpeg.get_ffmpeg_exe()

    raise FFmpegError(
        "ffmpeg не найден. Установи ffmpeg (https://ffmpeg.org/download.html) "
        "или выполни `pip install imageio-ffmpeg`."
    )


@lru_cache(maxsize=1)
def ffprobe_path() -> str | None:
    exe = shutil.which("ffprobe")
    if exe:
        return exe

    sibling = Path(ffmpeg_path()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return str(sibling) if sibling.is_file() else None


# --------------------------------------------------------------------------- #
# Запуск процессов
# --------------------------------------------------------------------------- #

async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _exec(
    args: list[str],
    *,
    on_progress: Callable[[float], None] | None = None,
    timeout: float = 600.0,
) -> tuple[int, bytes, str]:
    """Запустить процесс и вернуть (код, stdout, stderr).

    Если передан ``on_progress``, из stdout читаются строки ``-progress pipe:1``
    (``out_time_us``) и преобразуются в секунды.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"Не найден исполняемый файл: {args[0]}") from exc
    except OSError as exc:
        raise FFmpegError(f"Не удалось запустить {args[0]}: {exc}") from exc

    async def communicate() -> tuple[bytes, bytes]:
        async def read_stdout() -> bytes:
            assert proc.stdout is not None
            if on_progress is None:
                return await proc.stdout.read()

            chunks: list[bytes] = []
            async for raw in proc.stdout:
                chunks.append(raw)
                key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        on_progress(int(value) / 1_000_000)
                    except ValueError:
                        pass
            return b"".join(chunks)

        assert proc.stderr is not None
        out, err = await asyncio.gather(read_stdout(), proc.stderr.read())
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(communicate(), timeout)
    except TimeoutError as exc:
        await _kill(proc)
        raise FFmpegError(f"ffmpeg не уложился в {int(timeout)} с") from exc
    except BaseException:
        await _kill(proc)
        raise

    return proc.returncode or 0, out, err.decode("utf-8", "replace")


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# --------------------------------------------------------------------------- #
# Зондирование
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_video: bool
    has_audio: bool


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _apply_rotation(stream: dict, width: int, height: int) -> tuple[int, int]:
    """ffmpeg сам поворачивает кадр, поэтому стороны надо поменять местами."""
    rotation = _as_float((stream.get("tags") or {}).get("rotate"))
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = _as_float(side_data["rotation"])
    if int(abs(rotation)) % 180 == 90:
        return height, width
    return width, height


async def probe(path: Path) -> MediaInfo:
    exe = ffprobe_path()
    if exe is not None:
        try:
            return await _probe_with_ffprobe(exe, path)
        except FFmpegError:
            log.warning("ffprobe не смог прочитать %s — пробую через ffmpeg", path, exc_info=True)
    return await _probe_with_ffmpeg(path)


async def _probe_with_ffprobe(exe: str, path: Path) -> MediaInfo:
    args = [
        exe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    code, out, err = await _exec(args, timeout=120)
    if code != 0:
        raise FFmpegError(f"ffprobe: {_tail(err)}")

    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe вернул некорректный JSON") from exc

    streams: list[dict] = data.get("streams") or []
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _as_float((data.get("format") or {}).get("duration"))
    if duration <= 0 and video is not None:
        duration = _as_float(video.get("duration"))

    width = int(_as_float((video or {}).get("width")))
    height = int(_as_float((video or {}).get("height")))
    if video is not None:
        width, height = _apply_rotation(video, width, height)

    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        has_video=video is not None,
        has_audio=audio is not None,
    )


async def _probe_with_ffmpeg(path: Path) -> MediaInfo:
    args = [ffmpeg_path(), "-nostdin", "-hide_banner", "-i", str(path)]
    # Код возврата 1 без выходного файла — это нормально: ffmpeg лишь печатает сведения.
    code, _out, err = await _exec(args, timeout=120)

    match = _DURATION_RE.search(err)
    duration = 0.0
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    has_video = _VIDEO_STREAM_RE.search(err) is not None
    has_audio = _AUDIO_STREAM_RE.search(err) is not None
    if code != 0 and not (has_video or has_audio or match):
        raise FFmpegError(f"ffmpeg: {_tail(err)}")

    width = height = 0
    video_line = next((line for line in err.splitlines() if ": Video: " in line), "")
    size = _SIZE_RE.search(video_line)
    if size:
        width, height = int(size.group(1)), int(size.group(2))

    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        has_video=has_video,
        has_audio=has_audio,
    )


# --------------------------------------------------------------------------- #
# Конвертация
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ConvertResult:
    path: Path
    size: int
    duration: float
    video_kbps: int
    audio_kbps: int
    attempts: int


def video_filter(side: int = 640, fps: int = 30) -> str:
    """Центральный квадратный кроп, уменьшение до ``side``, чётные стороны, 30 fps."""
    return (
        "crop=w='min(iw,ih)':h='min(iw,ih)',"
        f"scale=w='min({side},iw)':h='min({side},ih)':flags=lanczos,"
        "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2',"
        f"fps={fps},setsar=1,format=yuv420p"
    )


def plan_bitrate(
    duration: float,
    max_bytes: int,
    has_audio: bool,
    *,
    shrink: float = 1.0,
    max_video_kbps: int = 4000,
    min_video_kbps: int = 40,
) -> tuple[int, int]:
    """Подобрать битрейты (видео, аудио) под целевой размер файла."""
    duration = max(duration, 0.1)
    budget_kbps = (max_bytes * 8 * 0.94 * shrink) / duration / 1000

    audio_kbps = 0
    if has_audio:
        audio_kbps = 48 if budget_kbps < 420 else 64

    video_kbps = int(budget_kbps - audio_kbps)
    video_kbps = max(min_video_kbps, min(max_video_kbps, video_kbps))
    return video_kbps, audio_kbps


def _encode_base(
    src: Path,
    duration: float,
    vf: str,
    preset: str,
    video_kbps: int,
    log_base: Path,
    trim: bool,
) -> list[str]:
    args = [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
    ]
    if trim:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-preset", preset,
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k",
        "-bufsize", f"{2 * video_kbps}k",
        "-passlogfile", str(log_base),
    ]
    return args


def _cleanup_pass_logs(log_base: Path) -> None:
    with contextlib.suppress(OSError):
        for stale in log_base.parent.glob(log_base.name + "-*"):
            with contextlib.suppress(OSError):
                stale.unlink()


async def convert_to_video_note(
    src: Path,
    dst: Path,
    info: MediaInfo,
    *,
    max_bytes: int,
    max_duration: float = 60.0,
    side: int = 640,
    fps: int = 30,
    preset: str = "fast",
    on_progress: Callable[[float], None] | None = None,
    timeout: float = 600.0,
) -> ConvertResult:
    """Сконвертировать видео в кружок, уложившись в ``max_bytes``.

    Используется двухпроходное кодирование с расчётом битрейта от размера
    файла. Если результат всё равно больше лимита, попытка повторяется
    с меньшим битрейтом.
    """
    if not info.has_video:
        raise FFmpegError("В файле нет видеодорожки.")

    trim = info.duration <= 0 or info.duration > max_duration
    duration = max_duration if trim else info.duration
    duration = max(duration, 0.2)

    ffmpeg = ffmpeg_path()
    vf = video_filter(side, fps)
    log_base = dst.with_suffix(".2pass")
    shrink = 1.0
    last_size = 0

    def report(seconds: float) -> None:
        if on_progress is not None:
            on_progress(min(1.0, seconds / duration))

    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            video_kbps, audio_kbps = plan_bitrate(
                duration, max_bytes, info.has_audio, shrink=shrink
            )
            base = _encode_base(src, duration, vf, preset, video_kbps, log_base, trim)
            log.debug(
                "попытка %d/%d: %d kbps видео, %d kbps аудио",
                attempt, _MAX_ATTEMPTS, video_kbps, audio_kbps,
            )

            code, _out, err = await _exec(
                [ffmpeg, *base, "-pass", "1", "-an", "-f", "null", os.devnull],
                timeout=timeout,
            )
            if code != 0:
                raise FFmpegError(f"ffmpeg (проход 1): {_tail(err)}")

            pass2 = [ffmpeg]
            if on_progress is not None:
                pass2 += ["-progress", "pipe:1", "-nostats"]
            pass2 += base + ["-pass", "2"]
            if info.has_audio:
                pass2 += ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "1", "-ar", "44100"]
            pass2 += ["-movflags", "+faststart", "-f", "mp4", str(dst)]

            code, _out, err = await _exec(
                pass2, on_progress=report, timeout=timeout
            )
            if code != 0:
                raise FFmpegError(f"ffmpeg (проход 2): {_tail(err)}")

            last_size = dst.stat().st_size if dst.exists() else 0
            if 0 < last_size <= max_bytes:
                return ConvertResult(
                    path=dst,
                    size=last_size,
                    duration=duration,
                    video_kbps=video_kbps,
                    audio_kbps=audio_kbps,
                    attempts=attempt,
                )

            # Перелёт по размеру — считаем, насколько ужать битрейт.
            overshoot = (last_size or max_bytes * 2) / max_bytes
            shrink *= max(0.55, min(0.9, 0.95 / overshoot))
            log.warning(
                "кружок вышел на %d байт при лимите %d — сжимаю сильнее (%.2f)",
                last_size, max_bytes, shrink,
            )
            with contextlib.suppress(OSError):
                dst.unlink()

        raise FFmpegError(
            f"не удалось уложиться в {max_bytes / 1024:.0f} КБ "
            f"(последний размер: {last_size / 1024:.0f} КБ)"
        )
    finally:
        _cleanup_pass_logs(log_base)
