"""Точка входа: запуск бота в режиме long polling."""

from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import router
from app.texts import HELP, NOT_A_VIDEO, START

log = logging.getLogger(__name__)

API_HOST = "api.telegram.org"


class _FixedApiResolver(AbstractResolver):
    """Резолвит api.telegram.org в заданные IP, остальные хосты — как обычно.

    Нужен там, где DNS провайдера указывает на заблокированный адрес
    Telegram, а другие адреса той же подсети доступны.
    """

    def __init__(self, host: str, ips: tuple[str, ...]) -> None:
        self._host = host
        self._ips = ips
        # DefaultResolver требует запущенный event loop, поэтому создаём лениво.
        self._fallback: DefaultResolver | None = None

    def _dns(self) -> DefaultResolver:
        if self._fallback is None:
            self._fallback = DefaultResolver()
        return self._fallback

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict]:
        if host != self._host:
            return await self._dns().resolve(host, port, family)

        port = port or 443
        fixed = [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
            for ip in self._ips
        ]
        # Заданные IP идут первыми, обычный DNS — запасной вариант,
        # если адреса когда-нибудь перестанут отвечать.
        try:
            resolved = await self._dns().resolve(host, port, socket.AF_INET)
        except OSError:
            resolved = []
        return [*fixed, *(item for item in resolved if item["host"] not in self._ips)]

    async def close(self) -> None:
        if self._fallback is not None:
            await self._fallback.close()


class _ApiIpSession(AiohttpSession):
    """Сессия aiogram с жёстко заданными IP для api.telegram.org."""

    def __init__(self, api_ips: tuple[str, ...], **kwargs) -> None:
        super().__init__(**kwargs)
        # Публичного способа подменить DNS-резолвер у aiogram нет,
        # поэтому дописываем его в настройки коннектора.
        self._connector_init["resolver"] = _FixedApiResolver(API_HOST, api_ips)


def _describe_proxy(url: str) -> str:
    """Прокси без логина и пароля — чтобы не светить их в логе."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    auth = " с логином" if parts.username else ""
    return f"{parts.scheme}://{host}{port}{auth}"


def _build_bot() -> Bot:
    session = None
    if settings.api_server:
        # Локальный Bot API server снимает лимит в 20 МБ на скачивание.
        log.info("использую локальный Bot API server: %s", settings.api_server)
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_server))
    elif settings.proxy:
        log.info("использую прокси %s (TELEGRAM_PROXY)", _describe_proxy(settings.proxy))
        try:
            session = AiohttpSession(proxy=settings.proxy)
        except (ImportError, RuntimeError) as exc:
            raise SystemExit(
                "Для TELEGRAM_PROXY нужен пакет aiohttp-socks. "
                "Установи зависимости: pip install -r requirements.txt"
            ) from exc
        if settings.api_ips:
            log.warning("TELEGRAM_PROXY задан — TELEGRAM_API_IP игнорируется")
    elif settings.api_ips:
        log.info("%s → %s (TELEGRAM_API_IP)", API_HOST, ", ".join(settings.api_ips))
        session = _ApiIpSession(settings.api_ips)

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


BOT_COMMANDS = [
    BotCommand(command="start", description="Приветствие"),
    BotCommand(command="help", description="Как пользоваться"),
]

_NETWORK_HINT = (
    "Если это повторяется — проверь VPN/файрвол, задай TELEGRAM_PROXY "
    "или TELEGRAM_API_IP (подробности в README)."
)


async def _prepare_bot(bot: Bot) -> None:
    """Команды и сброс вебхука: при обрыве связи ждём и пробуем снова."""
    attempt = 0
    delay = 1.0
    while True:
        attempt += 1
        try:
            await bot.set_my_commands(BOT_COMMANDS)
            # Сбрасываем вебхук, если бота раньше запускали в этом режиме.
            await bot.delete_webhook(drop_pending_updates=True)
            return
        except TelegramUnauthorizedError:
            raise
        except TelegramNetworkError as exc:
            log.warning(
                "Telegram не отвечает (попытка %d): %s. Повтор через %.0f с. %s",
                attempt,
                exc,
                delay,
                _NETWORK_HINT,
            )
        except TelegramAPIError as exc:
            # Не сетевая ошибка: повтором не лечится, но и падать незачем.
            log.warning("не удалось обновить команды/вебхук: %s", exc)
            return
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


async def _poll(bot: Bot, dispatcher: Dispatcher) -> None:
    """Polling с перезапуском, если связь пропала до первого getUpdates."""
    while True:
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
            return
        except TelegramNetworkError as exc:
            log.warning(
                "связь с Telegram прервалась: %s. Переподключаюсь через 5 с. %s",
                exc,
                _NETWORK_HINT,
            )
            await asyncio.sleep(5)


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

    try:
        await _prepare_bot(bot)
        log.info("бот запущен")
        await _poll(bot, dispatcher)
    finally:
        # Закрываем aiohttp-сессию, иначе на выходе будет «Unclosed client session».
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram отклонил BOT_TOKEN (401 Unauthorized). "
            "Проверь токен в .env — его выдаёт @BotFather."
        ) from None
    except TelegramNetworkError as exc:
        raise SystemExit(
            f"Нет связи с api.telegram.org: {exc}\n"
            "Что можно сделать:\n"
            "  1) запусти ещё раз — блокировки часто кратковременные;\n"
            "  2) укажи прокси в .env: TELEGRAM_PROXY=socks5://127.0.0.1:1080;\n"
            "  3) укажи рабочий адрес: TELEGRAM_API_IP=149.154.167.220 "
            "(можно несколько через запятую);\n"
            "  4) проверь VPN/файрвол."
        ) from None
    except TelegramAPIError as exc:
        raise SystemExit(f"Telegram ответил ошибкой: {exc}") from None
