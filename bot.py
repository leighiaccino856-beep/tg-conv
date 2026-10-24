"""Точка входа: запуск бота в режиме long polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import router
from app.texts import HELP, NOT_A_VIDEO, START

log = logging.getLogger(__name__)


def _build_bot() -> Bot:
    session = None
    if settings.api_server:
        # Локальный Bot API server снимает лимит в 20 МБ на скачивание.
        log.info("использую локальный Bot API server: %s", settings.api_server)
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_server))

    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def on_start(message: types.Message) -> None:
    await message.answer(START)


async def on_help(message: types.Message) -> None:
    await message.answer(HELP)


async def on_other(message: types.Message) -> None:
    await message.answer(NOT_A_VIDEO)


async def main() -> None:
    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен от @BotFather."
        )

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    bot = _build_bot()
    dispatcher = Dispatcher()

    dispatcher.message.register(on_start, commands=["start"])
    dispatcher.message.register(on_help, commands=["help"])
    dispatcher.include_router(router)
    # Подсказка только на текст в личке, чтобы не спамить в группах.
    dispatcher.message.register(on_other, F.text, F.chat.type == "private")

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Приветствие"),
            BotCommand(command="help", description="Как пользоваться"),
        ]
    )
    # Сбрасываем вебхук, если бота раньше запускали в этом режиме.
    await bot.delete_webhook(drop_pending_updates=True)

    log.info("бот запущен")
    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
