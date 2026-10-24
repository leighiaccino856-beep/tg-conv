"""Точка входа: запуск бота в режиме long polling."""

from __future__ import annotations

import asyncio
import logging
import socket

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import router
from app.texts import HELP, NOT_A_VIDEO, START

log = logging.getLogger(__name__)

API_HOST = "api.telegram.org"


class _FixedApiResolver(AbstractResolver):
    """Резолвит api.telegram.org в заданный IP, остальные хосты — как обычно.

    Нужен там, где DNS провайдера указывает на заблокированный адрес
    Telegram, а другие адреса той же подсети доступны.
    """

    def __init__(self, host: str, ip: str) -> None:
        self._host = host
        self._ip = ip
        self._fallback = DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict]:
        if host != self._host:
            return await self._fallback.resolve(host, port, family)

        fixed = {
            "hostname": host,
            "host": self._ip,
            "port": port or 443,
            "family": socket.AF_INET,
            "proto": 0,
            "flags": 0,
        }
        # Заданный IP первым, обычный DNS — запасной вариант,
        # если адрес когда-нибудь перестанет отвечать.
        try:
            resolved = await self._fallback.resolve(host, port, socket.AF_INET)
        except OSError:
            resolved = []
        return [fixed, *(item for item in resolved if item["host"] != self._ip)]

    async def close(self) -> None:
        await self._fallback.close()


class _ApiIpSession(AiohttpSession):
    """Сессия aiogram с жёстко заданным IP для api.telegram.org."""

    def __init__(self, api_ip: str, **kwargs) -> None:
        super().__init__(**kwargs)
        # Публичного способа подменить DNS-резолвер у aiogram нет,
        # поэтому дописываем его в настройки коннектора.
        self._connector_init["resolver"] = _FixedApiResolver(API_HOST, api_ip)


def _build_bot() -> Bot:
    session = None
    if settings.api_server:
        # Локальный Bot API server снимает лимит в 20 МБ на скачивание.
        log.info("использую локальный Bot API server: %s", settings.api_server)
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_server))
    elif settings.api_ip:
        log.info("%s → %s (TELEGRAM_API_IP)", API_HOST, settings.api_ip)
        session = _ApiIpSession(settings.api_ip)

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


def create_dispatcher() -> Dispatcher:
    """Собрать диспетчер: команды, обработчик видео, подсказка в личке."""
    dispatcher = Dispatcher()
    dispatcher.message.register(on_start, Command("start"))
    dispatcher.message.register(on_help, Command("help"))
    dispatcher.include_router(router)
    # Подсказка только на текст в личке, чтобы не спамить в группах.
    dispatcher.message.register(on_other, F.text, F.chat.type == "private")
    return dispatcher


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
    dispatcher = create_dispatcher()

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
