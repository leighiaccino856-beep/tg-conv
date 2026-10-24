"""Обработчики: приём видео, скачивание в RAM, конвертация, отправка кружка."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import weakref

from aiogram import Bot, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile

from app import texts
from app.config import settings
from app.convert import (
    ConversionError,
    ConvertResult,
    convert_to_video_note,
    probe_bytes,
)
from app.media import pick_media
from app.utils import human_size

router = Router(name="video")
log = logging.getLogger(__name__)

# Одновременных конвертаций на весь бот и не больше одной на пользователя.
_global_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent))
_user_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


async def _has_video(message: types.Message) -> bool:
    return pick_media(message) is not None


@router.message(_has_video)
async def on_video(message: types.Message, bot: Bot) -> None:
    media = pick_media(message)
    if media is None:  # pragma: no cover - защита от гонок
        return

    user_id = message.from_user.id if message.from_user else 0
    lock = _user_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await message.reply(texts.BUSY)
        return

    async with lock:
        await _process(message, bot, media)


async def _safe_edit(status: types.Message, text: str) -> None:
    # Сообщение могли удалить, или Telegram ответил флуд-контролем — не критично.
    with contextlib.suppress(TelegramAPIError):
        await status.edit_text(text)


async def _watch_progress(
    status: types.Message, state: dict[str, float], suffix: str
) -> None:
    """Периодически обновлять сообщение процентом готовности."""
    shown = -10
    while True:
        await asyncio.sleep(3)
        percent = int(min(state["value"], 1.0) * 100)
        if percent < 5 or percent - shown < 5:
            continue
        shown = percent
        await _safe_edit(
            status, texts.CONVERTING_PERCENT.format(percent=percent) + suffix
        )


async def _send_note(
    bot: Bot, chat_id: int, result: ConvertResult, note_duration: int | None
) -> None:
    extra: dict[str, int] = {}
    if note_duration:
        extra["duration"] = note_duration
    await bot.send_video_note(
        chat_id=chat_id,
        video_note=BufferedInputFile(result.data, filename="note.mp4"),
        length=result.width,
        **extra,
    )


async def _process(
    message: types.Message,
    bot: Bot,
    media: types.Video | types.VideoNote | types.Animation | types.Document,
) -> None:
    file_size = getattr(media, "file_size", None) or 0
    if file_size > settings.max_input_bytes:
        await message.reply(
            texts.TOO_BIG.format(
                size=human_size(file_size), limit=settings.max_input_mb
            )
        )
        return

    status = await message.reply(texts.DOWNLOADING)
    chat_id = message.chat.id

    try:
        # destination=None -> aiogram складывает файл в BytesIO, диск не трогаем.
        try:
            buffer = await bot.download(media.file_id, timeout=settings.download_timeout)
        except TelegramBadRequest as exc:
            # Сюда попадает и «file is too big» для файлов больше лимита Bot API.
            raise ConversionError(f"Telegram не отдал файл: {exc.message}") from exc
        if buffer is None:  # pragma: no cover - на всякий случай
            raise ConversionError("не удалось скачать файл")
        try:
            data = buffer.getvalue()
        finally:
            buffer.close()
        if not data:
            raise ConversionError("файл пришёл пустым")

        log.info(
            "видео от %s: скачано %s в память",
            message.from_user.id if message.from_user else "?",
            human_size(len(data)),
        )

        info = await asyncio.to_thread(probe_bytes, data)
        log.info(
            "параметры: %.1f с, %dx%d, поворот %d°, звук=%s",
            info.duration, info.width, info.height, info.rotation, info.has_audio,
        )
        if not info.has_video:
            await _safe_edit(status, texts.NO_VIDEO_STREAM)
            return

        suffix = ""
        if info.duration > settings.max_duration + 0.05:
            suffix = texts.TRIMMED_NOTE.format(seconds=settings.max_duration)

        state = {"value": 0.0}
        watcher = asyncio.create_task(_watch_progress(status, state, suffix))

        convert_kwargs = dict(
            max_duration=settings.max_duration,
            side=settings.max_side,
            preset=settings.ffmpeg_preset,
            on_progress=lambda value: state.__setitem__("value", value),
            timeout=settings.convert_timeout,
        )

        try:
            async with _global_semaphore:
                await _safe_edit(status, texts.CONVERTING + suffix)
                result = await asyncio.to_thread(
                    convert_to_video_note,
                    data,
                    max_bytes=settings.note_max_bytes,
                    **convert_kwargs,
                )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

        log.info(
            "готово за %d попытку(и): %s, %d kbps",
            result.attempts, human_size(result.size), result.video_kbps,
        )

        note_duration = (
            max(1, min(settings.max_duration, int(round(result.duration))))
            if info.duration > 0
            else None
        )
        try:
            await _send_note(bot, chat_id, result, note_duration)
        except TelegramBadRequest:
            if settings.note_max_bytes <= settings.fallback_note_bytes:
                raise
            log.info(
                "Telegram не принял кружок %s, пробую ужать до %.1f МБ",
                human_size(result.size), settings.fallback_note_mb,
            )
            await _safe_edit(status, texts.CONVERTING + suffix)
            async with _global_semaphore:
                result = await asyncio.to_thread(
                    convert_to_video_note,
                    data,
                    max_bytes=settings.fallback_note_bytes,
                    **convert_kwargs,
                )
            await _send_note(bot, chat_id, result, note_duration)

        try:
            await status.delete()
        except TelegramAPIError:
            # В группе статус мог не удалиться (нет прав) — хотя бы пометим готовым.
            await _safe_edit(status, texts.DONE)

    except ConversionError as exc:
        log.exception("ошибка конвертации")
        await _safe_edit(status, texts.CONVERT_FAILED.format(error=html.escape(str(exc))))
    except TimeoutError:
        log.exception("таймаут скачивания")
        await _safe_edit(status, texts.TIMEOUT)
    except TelegramBadRequest:
        log.exception("Telegram отклонил запрос")
        await _safe_edit(status, texts.SEND_FAILED)
    except Exception:
        log.exception("непредвиденная ошибка при обработке видео")
        await _safe_edit(status, texts.UNKNOWN_ERROR)
