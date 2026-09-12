"""Создание и настройка бота MusicBox (aiogram 3.x).

Точки входа для остальных модулей:
    * `create_bot()` — объект `Bot` с HTML-разметкой по умолчанию;
    * `create_dispatcher()` — `Dispatcher` с middleware, роутерами и обработчиком ошибок;
    * `setup_commands(bot)` — меню команд Telegram;
    * `setup_webhook(bot, dispatcher)` — установка или снятие вебхука.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, Message, Update
from aiogram.types.error_event import ErrorEvent

from backend.bot import texts
from backend.bot.middlewares import setup_middlewares
from backend.config import settings
from backend.errors import (
    FileTooLargeError,
    NotFoundError,
    StorageError,
    TelegramSearchError,
    TelegramSearchUnavailable,
    ValidationError,
)

logger = logging.getLogger(__name__)

#: Официальный адрес Bot API — при нём отдельная сессия не нужна.
DEFAULT_API_BASE = "https://api.telegram.org"

#: Сопоставление доменных исключений с понятными пользователю сообщениями.
_ERROR_TEXTS: tuple[tuple[type[BaseException], str], ...] = (
    (FileTooLargeError, texts.FILE_TOO_LARGE),
    (StorageError, texts.STORAGE_ERROR),
    (TelegramSearchUnavailable, texts.TG_SEARCH_UNAVAILABLE),
    (TelegramSearchError, texts.TG_SEARCH_ERROR),
    (NotFoundError, texts.NOT_FOUND),
)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


def create_bot() -> Bot:
    """Создаёт объект бота с HTML-разметкой по умолчанию.

    Если `settings.telegram_api_base` указывает не на официальный Bot API
    (например, на локальный Bot API server без лимита в 20 МБ), запросы уходят туда.
    """
    token = (settings.bot_token or "").strip()
    if not token:
        raise ValueError(
            "Не задан токен бота (BOT_TOKEN). Создайте бота у @BotFather "
            "и укажите токен в файле .env."
        )

    session: AiohttpSession | None = None
    base = (settings.telegram_api_base or "").strip().rstrip("/")
    if base and base != DEFAULT_API_BASE:
        try:
            session = AiohttpSession(api=TelegramAPIServer.from_base(base))
        except Exception:  # noqa: BLE001 — некорректный адрес не должен ронять запуск
            logger.exception(
                "Не удалось использовать TELEGRAM_API_BASE=%r — работаем с официальным Bot API",
                base,
            )
            session = None
        else:
            logger.info("Используется Bot API server: %s", base)

    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    logger.info("Бот создан (режим %s)", settings.bot_mode)
    return bot


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def create_dispatcher() -> Dispatcher:
    """Создаёт диспетчер: память состояний, middleware, роутеры, обработчик ошибок."""
    dp = Dispatcher(storage=MemoryStorage())

    setup_middlewares(dp)

    # Импорт локальный: модули обработчиков сами импортируют клавиатуры и утилиты
    # из пакета `backend.bot`, поэтому подключаем их уже после инициализации пакета.
    from backend.bot.handlers import register_handlers

    register_handlers(dp)

    dp.errors.register(handle_error)
    logger.info("Диспетчер бота готов")
    return dp


async def handle_error(event: ErrorEvent, **_: Any) -> bool:
    """Логирует необработанную ошибку и отвечает пользователю понятным текстом."""
    update = event.update
    update_id = getattr(update, "update_id", None)
    logger.exception(
        "Ошибка при обработке обновления %s: %s",
        update_id,
        event.exception,
        exc_info=event.exception,
    )

    await _notify_user(update, _error_text(event.exception))
    return True


def _error_text(exception: BaseException) -> str:
    """Подбирает пользовательский текст под тип исключения."""
    if isinstance(exception, ValidationError):
        message = str(exception).strip()
        return message or texts.ERROR_GENERIC
    for error_type, text in _ERROR_TEXTS:
        if isinstance(exception, error_type):
            return text
    return texts.ERROR_GENERIC


async def _notify_user(update: Update, text: str) -> None:
    """Пытается сообщить пользователю об ошибке; сбои доставки только логируются."""
    callback = getattr(update, "callback_query", None)
    message = getattr(update, "message", None) or getattr(update, "edited_message", None)

    try:
        if callback is not None:
            # Всплывающее окно Telegram вмещает не более 200 символов.
            await callback.answer(text[:200], show_alert=True)
            return
        if isinstance(message, Message):
            await message.answer(text)
    except TelegramAPIError as exc:
        logger.debug("Не удалось сообщить пользователю об ошибке: %s", exc)


# ---------------------------------------------------------------------------
# Команды и вебхук
# ---------------------------------------------------------------------------


async def setup_commands(bot: Bot) -> None:
    """Публикует меню команд бота (все команды из контракта)."""
    commands = [
        BotCommand(command=name, description=description)
        for name, description in texts.COMMANDS
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить список команд бота: %s", exc)
        return
    logger.info("Список команд бота обновлён (%s шт.)", len(commands))


async def setup_webhook(bot: Bot, dispatcher: Dispatcher | None = None) -> None:
    """Включает вебхук или снимает его (в режиме polling).

    `dispatcher` нужен только для вычисления списка используемых типов обновлений;
    без него Telegram будет присылать типы по умолчанию.
    """
    if settings.bot_mode != "webhook":
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except TelegramAPIError as exc:
            logger.warning("Не удалось снять вебхук перед запуском polling: %s", exc)
        else:
            logger.info("Вебхук снят — бот работает в режиме polling")
        return

    url = settings.webhook_url
    if not url:
        raise ValueError(
            "Выбран режим webhook (BOT_MODE=webhook), но не задан WEBHOOK_BASE_URL. "
            "Укажите публичный HTTPS-адрес сервера в файле .env."
        )

    allowed_updates = None
    if dispatcher is not None:
        try:
            allowed_updates = dispatcher.resolve_used_update_types()
        except Exception:  # noqa: BLE001 — не критично, Telegram применит значения по умолчанию
            logger.exception("Не удалось определить используемые типы обновлений")

    secret = (settings.webhook_secret or "").strip() or None

    try:
        await bot.set_webhook(
            url=url,
            secret_token=secret,
            drop_pending_updates=True,
            allowed_updates=allowed_updates,
        )
    except TelegramAPIError as exc:
        logger.error("Не удалось установить вебхук %s: %s", url, exc)
        raise
    logger.info("Вебхук установлен: %s", url)


__all__ = [
    "DEFAULT_API_BASE",
    "create_bot",
    "create_dispatcher",
    "handle_error",
    "setup_commands",
    "setup_webhook",
]
