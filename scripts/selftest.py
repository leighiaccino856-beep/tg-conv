"""Самопроверка конвертера без Telegram.

Генерирует тестовые видео (ffmpeg нужен только для теста), конвертирует их
целиком в памяти и проверяет результат.

Запуск: python scripts/selftest.py
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.convert import ConversionError, convert_to_video_note, probe_bytes  # noqa: E402

SIDE = 640
FIVE_MB = 5 * 1024 * 1024
ONE_MB = 1 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Генерация исходников (только для теста)
# --------------------------------------------------------------------------- #

def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("Для самопроверки нужен ffmpeg в PATH (боту он не нужен).")
    return exe


def make_video(
    dst: Path,
    *,
    size: str = "1280x720",
    seconds: float = 5,
    fps: int = 30,
    audio: bool = False,
    half_white: bool = False,
) -> None:
    width, height = (int(x) for x in size.split("x"))
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    if half_white:
        source = (
            f"color=c=black:s={size}:d={seconds},"
            f"drawbox=x=0:y=0:w={width}:h={height // 2}:c=white:t=fill"
        )
        args += ["-f", "lavfi", "-i", source]
    else:
        args += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    args += [
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p",
    ]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    args.append(str(dst))
    subprocess.run(args, check=True)


def add_rotation(src: Path, dst: Path, angle: int) -> None:
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-display_rotation", str(angle), "-i", str(src), "-c", "copy", str(dst)],
        check=True,
    )


def probe_file(path: Path) -> dict:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True,
    ).stdout
    data = json.loads(raw)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "pix_fmt": video["pix_fmt"],
        "duration": float(data["format"]["duration"]),
        "audio": audio["codec_name"] if audio else None,
        "size": int(data["format"]["size"]),
    }


def quadrant_means(data: bytes) -> dict[str, float]:
    """Средняя яркость четвертей первого кадра — проверка поворота."""
    with av.open(io.BytesIO(data)) as container:
        frame = next(container.decode(container.streams.video[0]))
        plane = frame.planes[0]
        buffer = bytes(plane)
        line_size, width, height = plane.line_size, frame.width, frame.height

        def mean(x0: int, x1: int, y0: int, y1: int) -> float:
            total = count = 0
            for y in range(y0, y1, 4):
                row = buffer[y * line_size + x0 : y * line_size + x1]
                total += sum(row)
                count += len(row)
            return total / count if count else 0.0

        return {
            "left": mean(0, width // 4, 0, height),
            "right": mean(3 * width // 4, width, 0, height),
            "top": mean(0, width, 0, height // 4),
            "bottom": mean(0, width, 3 * height // 4, height),
        }


# --------------------------------------------------------------------------- #
# Проверки
# --------------------------------------------------------------------------- #

def check_case(
    name: str,
    src: Path,
    out: Path,
    max_bytes: int,
    *,
    expect_duration: float | None = None,
    expect_side: int = SIDE,
    expect_audio: bool,
    **kwargs,
) -> bytes:
    data = src.read_bytes()
    info = probe_bytes(data)
    print(
        f"\n{name}\n  вход: {info.duration:.1f} с, {info.width}x{info.height}, "
        f"поворот {info.rotation}°, звук={info.has_audio}, {len(data) / 1024:.0f} КБ"
    )

    progress: list[float] = []
    result = convert_to_video_note(
        data, max_bytes=max_bytes, on_progress=progress.append, **kwargs
    )
    out.write_bytes(result.data)
    meta = probe_file(out)

    print(
        f"  выход: {meta['width']}x{meta['height']}, {meta['codec']}/{meta['pix_fmt']}, "
        f"{meta['duration']:.2f} с, звук={meta['audio']}, "
        f"{meta['size'] / 1024:.0f} КБ, попыток {result.attempts}"
    )

    assert meta["size"] <= max_bytes, f"{name}: {meta['size']} > {max_bytes}"
    assert meta["width"] == meta["height"] == expect_side, name
    assert meta["codec"] == "h264", name
    assert meta["pix_fmt"] == "yuv420p", name
    assert meta["audio"] == ("aac" if expect_audio else None), name
    if expect_duration is not None:
        assert abs(meta["duration"] - expect_duration) <= 1.0, f"{name}: {meta['duration']}"
    assert progress and max(progress) > 0.3, f"{name}: прогресс не приходил"
    return result.data


def check_media_detection() -> None:
    from aiogram import types

    from app.media import pick_media

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
    print("\n  OK  видео находится в сообщении, файле и ответе")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tgconv-selftest-") as tmp:
        work = Path(tmp)
        print("Генерирую тестовые видео…")
        wide = work / "wide.mp4"
        long = work / "long.mp4"
        portrait = work / "portrait.mp4"
        tiny = work / "tiny.mp4"
        half = work / "half.mp4"
        rotated = work / "rotated.mp4"

        make_video(wide, size="1280x720", seconds=12, audio=True)
        make_video(long, size="1280x720", seconds=65, audio=True)
        make_video(portrait, size="720x1280", seconds=3)
        make_video(tiny, size="320x240", seconds=3)
        make_video(half, size="720x1280", seconds=2, half_white=True)
        add_rotation(half, rotated, 90)

        print("Проверяю конвертацию…")
        check_case("16:9 со звуком → 5 МБ", wide, work / "out-wide.mp4", FIVE_MB,
                   expect_duration=12, expect_audio=True)
        check_case("16:9 со звуком → 1 МБ", wide, work / "out-wide-1mb.mp4", ONE_MB,
                   expect_duration=12, expect_audio=True)
        check_case("длинное видео 65 с → обрезка", long, work / "out-long.mp4", ONE_MB,
                   expect_duration=60, expect_audio=True)
        check_case("вертикальное 9:16 без звука", portrait, work / "out-portrait.mp4",
                   FIVE_MB, expect_duration=3, expect_audio=False)
        check_case("маленькое 320x240", tiny, work / "out-tiny.mp4", FIVE_MB,
                   expect_duration=3, expect_side=240, expect_audio=False)

        data = check_case("поворот 90° (метаданные)", rotated, work / "out-rot.mp4",
                          FIVE_MB, expect_duration=2, expect_audio=False)
        quadrants = quadrant_means(data)
        assert quadrants["left"] > 200 and quadrants["right"] < 60, quadrants
        print(f"  OK  поворот применён: четверти {quadrants}")

        print("\nПроверяю распознавание медиа…")
        check_media_detection()

    print("\nВсе проверки пройдены ✅")
    return 0


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        raise SystemExit(main())
    except (ConversionError, AssertionError) as exc:
        print(f"\nОшибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
