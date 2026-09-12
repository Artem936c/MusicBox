"""Пакет Telegram-бота MusicBox (aiogram 3.x).

Публичный API пакета:
    * `create_bot()` / `create_dispatcher()` — сборка бота и диспетчера;
    * `setup_commands(bot)` / `setup_webhook(bot, dispatcher)` — настройка Telegram;
    * `register_handlers(dp)` — подключение роутеров разделов.

Клавиатуры, callback-фабрики, состояния, тексты и утилиты импортируются напрямую
из соответствующих модулей, например:
``from backend.bot.keyboards import tracks_page_kb``.
"""

from __future__ import annotations

from backend.bot.bot import (
    create_bot,
    create_dispatcher,
    setup_commands,
    setup_webhook,
)
from backend.bot.handlers import register_handlers

__all__ = [
    "create_bot",
    "create_dispatcher",
    "register_handlers",
    "setup_commands",
    "setup_webhook",
]
