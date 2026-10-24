"""Самопроверка конвертера без Telegram.

Генерирует тестовые видео через ffmpeg, конвертирует их в кружки и проверяет
результат: квадрат, лимит размера, длительность, кодек.

Запуск: python scripts/selftest.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg import (  # noqa: E402
    FFmpegError,
    convert_to_video_note,
    ffmpeg_path,
    ffprobe_path,
    probe,
)

SIDE = 640
MAX_BYTES_5MB = 5 * 1024 * 1024
MAX_BYTES_1MB = 1 * 1024 * 1024


def make_video(
    dst: Path, *, size: str, duration: int, fps: int = 30, audio: bool
) -> None:
    args = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    args += [
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p",
    ]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    args.append(str(dst))
    subprocess.run(args, check=True)


def stream_info(path: Path) -> dict:
    exe = ffprobe_path()
    if exe is None:
        return {}
    out = subprocess.run(
        [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True,
    ).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "pix_fmt": video["pix_fmt"],
        "duration": float(data["format"]["duration"]),
        "audio": audio["codec_name"] if audio else None,
    }


async def check_case(
    name: str,
    src: Path,
    out_name: str,
    max_bytes: int,
    *,
    expect_audio: bool,
    expect_duration: float,
) -> None:
    info = await probe(src)
    progress: list[float] = []
    result = await convert_to_video_note(
        src,
        src.with_name(out_name),
        info,
        max_bytes=max_bytes,
        side=SIDE,
        preset="fast",
        on_progress=progress.append,
    )
    meta = stream_info(result.path)

    assert result.size <= max_bytes, f"{name}: {result.size} > {max_bytes}"
    assert meta["width"] == meta["height"] <= SIDE, f"{name}: {meta['width']}x{meta['height']}"
    assert meta["codec"] == "h264", f"{name}: {meta['codec']}"
    assert meta["pix_fmt"] == "yuv420p", f"{name}: {meta['pix_fmt']}"
    assert meta["duration"] <= expect_duration + 1.0, f"{name}: длительность {meta['duration']}"
    if expect_audio:
        assert meta["audio"] == "aac", f"{name}: аудио {meta['audio']}"
    else:
        assert meta["audio"] is None, f"{name}: неожиданное аудио {meta['audio']}"
    assert progress and max(progress) > 0.5, f"{name}: прогресс не приходил ({progress[:3]})"

    print(
        f"  OK  {name:<22} {meta['width']}x{meta['height']} "
        f"{meta['duration']:.1f} с  {result.size / 1024:.0f} КБ  "
        f"попыток: {result.attempts}  видео {result.video_kbps} kbps"
    )


def check_media_detection() -> None:
    """Проверка, что видео находится в сообщении/документе/ответе."""
    from aiogram import types

    from app.media import guess_suffix, pick_media

    video = types.Video(file_id="v", file_unique_id="v1", width=1920, height=1080, duration=10)
    doc = types.Document(file_id="d", file_unique_id="d1", file_name="movie.MKV")
    pdf = types.Document(
        file_id="t", file_unique_id="t1", file_name="notes.pdf", mime_type="application/pdf"
    )
    anim = types.Animation(file_id="a", file_unique_id="a1", width=320, height=240, duration=3)

    def message(**kwargs) -> types.Message:
        return types.Message(
            message_id=1, date=0, chat=types.Chat(id=1, type="private"), **kwargs
        )

    assert pick_media(message(video=video)) is video
    assert pick_media(message(document=doc)) is doc
    assert pick_media(message(document=pdf)) is None
    assert pick_media(message(animation=anim)) is anim
    assert pick_media(message(reply_to_message=message(video=video))) is video
    assert pick_media(message(text="привет")) is None
    assert guess_suffix(video) == ".mp4"
    assert guess_suffix(doc) == ".mkv"
    print("  OK  распознавание видео в сообщении, файле и ответе")


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tgconv-selftest-") as tmp:
        workdir = Path(tmp)
        print("Генерирую тестовые видео…")
        wide = workdir / "wide.mp4"
        portrait = workdir / "portrait.mp4"
        tiny = workdir / "tiny.mp4"
        make_video(wide, size="1280x720", duration=70, audio=True)
        make_video(portrait, size="720x1280", duration=8, audio=False)
        make_video(tiny, size="320x240", duration=5, fps=25, audio=False)

        print("Проверяю конвертацию…")
        await check_case("широкое 720p/70с", wide, "wide-note.mp4", MAX_BYTES_5MB,
                         expect_audio=True, expect_duration=60)
        await check_case("широкое 720p → 1 МБ", wide, "wide-note-1mb.mp4", MAX_BYTES_1MB,
                         expect_audio=True, expect_duration=60)
        await check_case("вертикальное 9:16", portrait, "portrait-note.mp4", MAX_BYTES_5MB,
                         expect_audio=False, expect_duration=8)
        await check_case("маленькое 320x240", tiny, "tiny-note.mp4", MAX_BYTES_5MB,
                         expect_audio=False, expect_duration=5)

        print("Проверяю распознавание медиа…")
        check_media_detection()

    print("\nВсе проверки пройдены ✅")
    return 0


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        raise SystemExit(asyncio.run(main()))
    except FFmpegError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
