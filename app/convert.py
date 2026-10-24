"""Конвертация видео в кружок Telegram целиком в оперативной памяти.

Используется PyAV (libav + libx264 внутри процесса): входные байты читаются
из ``io.BytesIO``, результат собирается в ``io.BytesIO``. Ничего не пишется
на диск — ни видео, ни промежуточные файлы.

Кружок (video note) — квадратное MP4-видео (H.264/AAC) со стороной
не больше 640 px и длительностью не больше 60 секунд.
"""

from __future__ import annotations

import io
import logging
import math
import struct
import time
from dataclasses import dataclass
from typing import Callable

import av

log = logging.getLogger(__name__)

av.logging.set_level(av.logging.ERROR)

_AV_TIME_BASE = 1_000_000
_DISPLAY_MATRIX = "display_matrix"
_MAX_ATTEMPTS = 4


class ConversionError(RuntimeError):
    """Файл не читается или не удалось уложиться в целевой размер."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_video: bool
    has_audio: bool
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class ConvertResult:
    data: bytes
    width: int
    height: int
    duration: float
    video_kbps: int
    audio_kbps: int
    attempts: int

    @property
    def size(self) -> int:
        return len(self.data)


# --------------------------------------------------------------------------- #
# Разбор входного файла
# --------------------------------------------------------------------------- #

def _is_attached_pic(stream: av.stream.Stream) -> bool:
    disposition = getattr(stream, "disposition", None)
    if disposition is None:
        return False
    # disposition — Flag-enum: сравнивать нужно по битовой маске,
    # обращение к атрибуту возвращает сам флаг и всегда истинно.
    flag = getattr(type(disposition), "attached_pic", None)
    try:
        return bool(disposition & flag)
    except TypeError:  # pragma: no cover - на случай другой реализации
        return False


def _rotation(stream: av.stream.Stream) -> int:
    """Угол поворота кадра по метаданным контейнера (0/90/180/270)."""
    try:
        side_data = stream.codec_context.coded_side_data or {}
        raw = side_data.get(_DISPLAY_MATRIX)
        if raw and len(raw) >= 36:
            matrix = struct.unpack("=9i", bytes(raw[:36]))
            scale = abs(matrix[8]) or 1
            angle = math.degrees(math.atan2(matrix[1] / scale, matrix[0] / scale))
            return round((-angle % 360) / 90) * 90 % 360
    except Exception:  # pragma: no cover - метаданные бывают битыми
        log.debug("не удалось разобрать display matrix", exc_info=True)

    try:
        return int(float(stream.metadata.get("rotate") or 0)) % 360
    except (TypeError, ValueError):
        return 0


def _duration(container: av.container.InputContainer, video: av.stream.Stream | None) -> float:
    duration = float(container.duration or 0) / _AV_TIME_BASE
    if duration <= 0 and video is not None and video.duration:
        duration = float(video.duration * video.time_base)
    return max(duration, 0.0)


def probe_bytes(data: bytes) -> MediaInfo:
    """Прочитать параметры видео из памяти."""
    try:
        with av.open(io.BytesIO(data)) as container:
            video = next(
                (s for s in container.streams.video if not _is_attached_pic(s)), None
            )
            audio = next(iter(container.streams.audio), None)

            width = int(video.width or 0) if video is not None else 0
            height = int(video.height or 0) if video is not None else 0
            rotation = _rotation(video) if video is not None else 0
            if rotation in (90, 270):
                width, height = height, width

            return MediaInfo(
                duration=_duration(container, video),
                width=width,
                height=height,
                has_video=video is not None,
                has_audio=audio is not None,
                rotation=rotation,
            )
    except av.FFmpegError as exc:
        raise ConversionError(f"не удалось прочитать видео: {exc}") from exc


# --------------------------------------------------------------------------- #
# Подбор параметров
# --------------------------------------------------------------------------- #

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


def _target_side(info: MediaInfo, side: int) -> int:
    target = min(side, info.width, info.height)
    target -= target % 2
    return max(target, 2)


def _video_graph(
    stream: av.stream.Stream, rotation: int, side: int, fps: int
) -> av.filter.Graph:
    """Квадратный кроп по центру + уменьшение + 30 fps, всё в памяти."""
    filters: list[tuple[str, str | None]] = []
    if rotation == 90:
        filters.append(("transpose", "cclock"))
    elif rotation == 180:
        filters += [("hflip", None), ("vflip", None)]
    elif rotation == 270:
        filters.append(("transpose", "clock"))

    filters += [
        ("crop", "min(iw,ih):min(iw,ih)"),
        ("scale", f"w='min({side},iw)':h='min({side},ih)':flags=lanczos"),
        ("scale", "w='trunc(iw/2)*2':h='trunc(ih/2)*2'"),
        ("fps", str(fps)),
        ("setsar", "1"),
        ("format", "yuv420p"),
    ]

    graph = av.filter.Graph()
    node = graph.add_buffer(template=stream)
    for name, arg in filters:
        link = graph.add(name, arg) if arg is not None else graph.add(name)
        node.link_to(link)
        node = link
    sink = graph.add("buffersink")
    node.link_to(sink)
    graph.configure()
    return graph


# --------------------------------------------------------------------------- #
# Кодирование
# --------------------------------------------------------------------------- #

def _encode_once(
    data: bytes,
    info: MediaInfo,
    *,
    duration: float,
    trim: bool,
    side: int,
    fps: int,
    preset: str,
    video_kbps: int,
    audio_kbps: int,
    deadline: float | None,
    on_progress: Callable[[float], None] | None,
) -> tuple[bytes, int]:
    target = _target_side(info, side)
    output = io.BytesIO()

    try:
        source = av.open(io.BytesIO(data))
    except av.FFmpegError as exc:
        raise ConversionError(f"не удалось открыть видео: {exc}") from exc

    with source:
        video_in = next(
            (s for s in source.streams.video if not _is_attached_pic(s)), None
        )
        if video_in is None:
            raise ConversionError("В файле нет видеодорожки.")
        audio_in = next(iter(source.streams.audio), None)
        rotation = _rotation(video_in)
        graph = _video_graph(video_in, rotation, side, fps)

        container = av.open(output, mode="w", format="mp4")
        try:
            video_out = container.add_stream("libx264", rate=fps)
            video_out.width = target
            video_out.height = target
            video_out.pix_fmt = "yuv420p"
            video_out.options = {
                "b": f"{video_kbps}k",
                "maxrate": f"{video_kbps}k",
                "bufsize": f"{2 * video_kbps}k",
                "preset": preset,
            }

            audio_out = None
            resampler = None
            if audio_in is not None:
                audio_out = container.add_stream("aac", rate=44100)
                audio_out.layout = "mono"
                audio_out.bit_rate = audio_kbps * 1000
                resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)

            def flush_video() -> None:
                while True:
                    try:
                        frame = graph.pull()
                    except (av.error.BlockingIOError, av.error.EOFError):
                        return
                    for packet in video_out.encode(frame):
                        container.mux(packet)

            streams = [video_in] + ([audio_in] if audio_in is not None else [])
            video_done = False
            audio_done = audio_in is None
            frames = 0
            last_report = -1.0

            for packet in source.demux(*streams):
                if packet.dts is None:
                    continue

                for frame in packet.decode():
                    timestamp = frame.time

                    if isinstance(frame, av.VideoFrame):
                        if timestamp is not None and timestamp >= duration:
                            video_done = True
                            continue
                        graph.push(frame)
                        flush_video()
                        frames += 1

                        if (
                            on_progress is not None
                            and timestamp is not None
                            and timestamp - last_report >= 0.5
                        ):
                            last_report = timestamp
                            on_progress(min(0.99, timestamp / duration))

                        if deadline is not None and frames % 30 == 0:
                            if time.monotonic() > deadline:
                                raise ConversionError("превышено время конвертации")
                    elif audio_out is not None and resampler is not None:
                        if timestamp is not None and timestamp >= duration:
                            audio_done = True
                            continue
                        for resampled in resampler.resample(frame):
                            for packet in audio_out.encode(resampled):
                                container.mux(packet)

                if video_done and audio_done:
                    break

            graph.push(None)
            flush_video()
            for packet in video_out.encode(None):
                container.mux(packet)

            if audio_out is not None and resampler is not None:
                for resampled in resampler.resample(None):
                    for packet in audio_out.encode(resampled):
                        container.mux(packet)
                for packet in audio_out.encode(None):
                    container.mux(packet)
        except av.FFmpegError as exc:
            raise ConversionError(f"ошибка кодирования: {exc}") from exc
        finally:
            container.close()

    return output.getvalue(), target


def convert_to_video_note(
    data: bytes,
    *,
    max_bytes: int,
    max_duration: float = 60.0,
    side: int = 640,
    fps: int = 30,
    preset: str = "fast",
    on_progress: Callable[[float], None] | None = None,
    timeout: float = 600.0,
) -> ConvertResult:
    """Сделать кружок из байтов видео, уложившись в ``max_bytes``.

    Кодирование однопроходное (ABR + VBV): если файл всё равно больше цели,
    попытка повторяется с меньшим битрейтом.
    """
    info = probe_bytes(data)
    if not info.has_video:
        raise ConversionError("В файле нет видеодорожки.")

    trim = info.duration <= 0 or info.duration > max_duration
    duration = max(max_duration if trim else info.duration, 0.2)
    deadline = time.monotonic() + timeout if timeout else None
    shrink = 1.0
    last_size = 0

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        video_kbps, audio_kbps = plan_bitrate(
            duration, max_bytes, info.has_audio, shrink=shrink
        )
        if on_progress is not None:
            on_progress(0.0)
        log.debug(
            "попытка %d/%d: %d kbps видео, %d kbps аудио",
            attempt, _MAX_ATTEMPTS, video_kbps, audio_kbps,
        )

        encoded, target = _encode_once(
            data,
            info,
            duration=duration,
            trim=trim,
            side=side,
            fps=fps,
            preset=preset,
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            deadline=deadline,
            on_progress=on_progress,
        )
        last_size = len(encoded)

        if 0 < last_size <= max_bytes:
            return ConvertResult(
                data=encoded,
                width=target,
                height=target,
                duration=duration,
                video_kbps=video_kbps,
                audio_kbps=audio_kbps,
                attempts=attempt,
            )

        overshoot = (last_size or max_bytes * 2) / max_bytes
        shrink *= max(0.55, min(0.9, 0.95 / overshoot))
        log.warning(
            "кружок вышел на %d байт при лимите %d — сжимаю сильнее (%.2f)",
            last_size, max_bytes, shrink,
        )

    raise ConversionError(
        f"не удалось уложиться в {max_bytes / 1024:.0f} КБ "
        f"(последний размер: {last_size / 1024:.0f} КБ)"
    )
