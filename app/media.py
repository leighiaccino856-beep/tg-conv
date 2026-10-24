"""Поиск видео в сообщении (в самом сообщении или в том, на которое ответили)."""

from __future__ import annotations

from pathlib import Path

from aiogram import types

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".3gp",
    ".flv", ".wmv", ".mpeg", ".mpg", ".m2ts", ".ts", ".gif",
}

MediaObject = types.Video | types.VideoNote | types.Animation | types.Document


def is_video_document(document: types.Document) -> bool:
    if (document.mime_type or "").lower().startswith("video/"):
        return True
    return Path(document.file_name or "").suffix.lower() in VIDEO_EXTENSIONS


def pick_media(message: types.Message) -> MediaObject | None:
    """Первое видео из сообщения или из сообщения, на которое ответили."""
    candidates = [message]
    if message.reply_to_message is not None:
        candidates.append(message.reply_to_message)

    for candidate in candidates:
        if candidate.video is not None:
            return candidate.video
        if candidate.video_note is not None:
            return candidate.video_note
        if candidate.animation is not None:
            return candidate.animation
        if candidate.document is not None and is_video_document(candidate.document):
            return candidate.document
    return None
