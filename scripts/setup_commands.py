"""Регистрирует меню команд бота в Telegram (`setMyCommands`).

Запускается один раз после настройки .env:

    python -m scripts.setup_commands

Хранилище (STORAGE_CHANNEL_ID) для этой операции не требуется, поэтому скрипт
удобно выполнять сразу после получения токена, до создания канала.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.types import BotCommandScopeAllPrivateChats

from backend.bot.bot import create_bot, setup_commands
from backend.logging_config import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    bot = create_bot()
    try:
        me = await bot.get_me()
        await setup_commands(bot)
        # Читать нужно ту же область видимости, в которую пишет setup_commands:
        # у scope=default отдельный набор команд, и он останется пустым.
        commands = await bot.get_my_commands(scope=BotCommandScopeAllPrivateChats())
        logger.info("Бот @%s: зарегистрировано команд — %d", me.username, len(commands))
        for command in commands:
            logger.info("  /%s — %s", command.command, command.description)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
