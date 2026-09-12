"""Зависимости FastAPI: текущий пользователь Mini App и экземпляр бота."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from aiogram import Bot
from fastapi import Depends, HTTPException, Request, status

from backend.api.security import validate_init_data
from backend.config import settings
from backend.db.repositories import users as users_repo
from backend.errors import AuthError

logger = logging.getLogger(__name__)

#: Основной заголовок с `initData` Telegram WebApp.
INIT_DATA_HEADER = "X-Telegram-Init-Data"

#: Альтернатива: `Authorization: tma <initData>` (схема Telegram Mini Apps).
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "tma"

#: Имя пользователя-заглушки в режиме разработки.
DEV_USER_FIRST_NAME = "Разработчик"


def extract_init_data(request: Request) -> str:
    """Достать `initData` из заголовков запроса. Пустая строка — данных нет."""
    raw = (request.headers.get(INIT_DATA_HEADER) or "").strip()
    if raw:
        return raw

    authorization = (request.headers.get(AUTH_HEADER) or "").strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.strip().lower() == AUTH_SCHEME:
            return value.strip()
    return ""


async def get_current_user(request: Request) -> dict[str, Any]:
    """Определить пользователя Mini App и синхронизировать его профиль в БД.

    Порядок: `initData` из заголовка → проверка подписи; если данных нет и включён
    `dev_mode`, используется `settings.dev_user_id`. Дальше — белый список
    `allowed_user_ids` и `ensure_user` + `touch_user`.

    :returns: словарь пользователя из таблицы `users`.
    :raises AuthError: авторизация не пройдена (обработчик приложения вернёт 401).
    :raises HTTPException: доступ запрещён белым списком (403).
    """
    init_data = extract_init_data(request)

    if init_data:
        telegram_user = validate_init_data(
            init_data, settings.bot_token, settings.init_data_ttl
        )
    elif settings.dev_mode and settings.dev_user_id:
        telegram_user = {
            "id": int(settings.dev_user_id),
            "first_name": DEV_USER_FIRST_NAME,
            "last_name": None,
            "username": None,
            "language_code": "ru",
        }
        logger.debug("Режим разработки: запрос выполняется от имени %s", settings.dev_user_id)
    else:
        raise AuthError(
            "Требуется авторизация Telegram. Откройте приложение кнопкой в боте."
        )

    user_id = int(telegram_user["id"])

    allowed = settings.allowed_user_ids_set
    if allowed and user_id not in allowed:
        logger.warning("Доступ запрещён: Telegram-ID %s отсутствует в белом списке", user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ к этому приложению закрыт. Обратитесь к владельцу бота.",
        )

    user = await users_repo.ensure_user(
        user_id,
        telegram_user.get("username"),
        telegram_user.get("first_name"),
        telegram_user.get("last_name"),
        telegram_user.get("language_code"),
    )
    await users_repo.touch_user(user_id)
    return user


#: Аннотация для роутеров: `user: CurrentUser`.
CurrentUser = Annotated[dict, Depends(get_current_user)]


def get_bot(request: Request) -> Bot:
    """Вернуть экземпляр бота из состояния приложения.

    :raises HTTPException: 503, если приложение запущено без бота
        (например, в тестах или при ошибке конфигурации).
    """
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        logger.error("Запрошена операция с ботом, но бот не сконфигурирован")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Бот сейчас недоступен, операция невозможна. Попробуйте позже.",
        )
    return bot


#: Аннотация для роутеров: `bot: BotDep`.
BotDep = Annotated[Bot, Depends(get_bot)]


__all__ = [
    "AUTH_HEADER",
    "AUTH_SCHEME",
    "INIT_DATA_HEADER",
    "BotDep",
    "CurrentUser",
    "extract_init_data",
    "get_bot",
    "get_current_user",
]
