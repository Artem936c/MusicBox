"""Middleware бота MusicBox.

Все middleware регистрируются как ВНЕШНИЕ (`outer_middleware`) на `dp.message`
и `dp.callback_query`: хендлеры живут во вложенных роутерах, а внутренние
middleware диспетчера до них не доходят. На `dp.inline_query` вешается только
`AccessMiddleware` — белый список обязан работать и в инлайн-режиме.

Порядок выполнения:
    1. `ThrottlingMiddleware` — отсекает слишком частые события;
    2. `AccessMiddleware` — белый список Telegram-ID;
    3. `MenuAliasMiddleware` — подменяет подпись кнопки нижней клавиатуры на команду;
    4. `UserMiddleware` — регистрирует пользователя и кладёт `user` / `settings` в `data`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, MutableMapping

from aiogram import BaseMiddleware, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject, User

from backend.bot.texts import ACCESS_DENIED, MENU_ALIASES
from backend.config import settings
from backend.db.repositories import users as users_repo
from backend.services import media

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

#: Минимальный интервал между событиями одного пользователя, секунды.
THROTTLE_RATE = 0.4
#: После какого размера кэша чистить старые записи троттлинга.
THROTTLE_CACHE_LIMIT = 5_000
#: Записи старше этого возраста считаются просроченными и удаляются.
THROTTLE_CACHE_TTL = 300.0

#: Ключ в workflow-данных диспетчера, защищающий от повторной регистрации.
_SETUP_FLAG = "musicbox_middlewares_installed"


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя в БД и добавляет `user` и `settings` в контекст."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        try:
            user = await users_repo.ensure_user(
                tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                language_code=tg_user.language_code,
            )
            await users_repo.touch_user(tg_user.id)
            user_settings = await users_repo.get_settings(tg_user.id)
        except Exception:  # noqa: BLE001 — падение БД не должно ронять обработку события
            logger.exception("Не удалось подготовить данные пользователя %s", tg_user.id)
            return await handler(event, data)

        data["user"] = user
        data["settings"] = user_settings
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из `settings.allowed_user_ids_set`.

    Пустой белый список означает «бот доступен всем».
    """

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        allowed = settings.allowed_user_ids_set
        if not allowed:
            return await handler(event, data)

        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        if tg_user.id in allowed:
            return await handler(event, data)

        logger.warning("Доступ запрещён для пользователя %s", tg_user.id)
        await self._deny(event)
        return None

    @staticmethod
    async def _deny(event: TelegramObject) -> None:
        """Сообщает пользователю об ограничении доступа."""
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(ACCESS_DENIED, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(ACCESS_DENIED)
            elif isinstance(event, InlineQuery):
                # В инлайн-режиме текст показать негде — закрываем запрос пустым ответом.
                await event.answer([], cache_time=1, is_personal=True)
        except TelegramAPIError as exc:
            logger.debug("Не удалось отправить отказ в доступе: %s", exc)


class ThrottlingMiddleware(BaseMiddleware):
    """Простой rate-limit: не чаще одного события в `rate` секунд на пользователя.

    Для `CallbackQuery` при превышении просто закрываются «часики» (`answer()`)
    без выполнения действия; сообщения молча игнорируются.

    Сообщения с файлами (аудио, документы, видео, кружочки и голосовые) лимитом
    НЕ ограничиваются: Telegram присылает пачку пересланных треков или альбом
    одним батчем, и молчаливый отброс потерял бы все файлы, кроме первого.
    """

    def __init__(self, rate: float = THROTTLE_RATE) -> None:
        self.rate = float(rate)
        self._last_event: MutableMapping[int, float] = {}

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        if self._is_upload(event):
            return await handler(event, data)

        now = time.monotonic()
        previous = self._last_event.get(tg_user.id)
        if previous is not None and now - previous < self.rate:
            logger.debug("Событие пользователя %s отброшено троттлингом", tg_user.id)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except TelegramAPIError as exc:
                    logger.debug("Не удалось закрыть callback при троттлинге: %s", exc)
            return None

        self._last_event[tg_user.id] = now
        self._cleanup(now)
        return await handler(event, data)

    @staticmethod
    def _is_upload(event: TelegramObject) -> bool:
        """Сообщение с файлом: такие события пропускаем мимо лимита.

        Список типов берём из `media.detect_file_type`, чтобы он всегда совпадал
        с фильтрами хендлеров загрузки и раздела «Другое».
        """
        return isinstance(event, Message) and media.detect_file_type(event) is not None

    def _cleanup(self, now: float) -> None:
        """Убирает из кэша давно неактивных пользователей."""
        if len(self._last_event) < THROTTLE_CACHE_LIMIT:
            return
        stale = [
            user_id
            for user_id, moment in self._last_event.items()
            if now - moment > THROTTLE_CACHE_TTL
        ]
        for user_id in stale:
            self._last_event.pop(user_id, None)
        logger.debug("Кэш троттлинга очищен: удалено %s записей", len(stale))


class MenuAliasMiddleware(BaseMiddleware):
    """Превращает нажатие кнопки нижней клавиатуры в обычную команду.

    Пользователь видит дружелюбную подпись («📊 Статистика»), а хендлеры
    получают текст `/stats` и срабатывают по фильтру `Command(...)`.
    """

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text:
            command = MENU_ALIASES.get(event.text.strip())
            if command:
                patched = self._patch(event, command)
                if patched is not None:
                    return await handler(patched, data)
        return await handler(event, data)

    @staticmethod
    def _patch(message: Message, command: str) -> Message | None:
        """Копия сообщения с текстом команды (или None, если копия не удалась)."""
        try:
            patched = message.model_copy(update={"text": command, "entities": None})
            bot = getattr(message, "bot", None)
            if bot is not None:
                patched = patched.as_(bot)
            return patched
        except Exception:  # noqa: BLE001 — подмена не критична, продолжим с оригиналом
            logger.exception("Не удалось подменить текст кнопки меню на команду %s", command)
            return None


def setup_middlewares(dp: Dispatcher) -> None:
    """Регистрирует middleware на сообщениях, кнопках и инлайн-запросах (идемпотентно)."""
    if dp.workflow_data.get(_SETUP_FLAG):
        logger.debug("Middleware уже зарегистрированы — повторная установка пропущена")
        return

    throttling = ThrottlingMiddleware()
    access = AccessMiddleware()
    alias = MenuAliasMiddleware()
    user = UserMiddleware()

    dp.message.outer_middleware(throttling)
    dp.message.outer_middleware(access)
    dp.message.outer_middleware(alias)
    dp.message.outer_middleware(user)

    dp.callback_query.outer_middleware(throttling)
    dp.callback_query.outer_middleware(access)
    dp.callback_query.outer_middleware(user)

    # Инлайн-режим: белый список обязателен, иначе посторонние обращаются к БД
    # мимо проверки. Троттлинг и `UserMiddleware` здесь НЕ вешаем — Telegram шлёт
    # `inline_query` на каждое нажатие клавиши.
    dp.inline_query.outer_middleware(access)

    dp.workflow_data[_SETUP_FLAG] = True
    logger.info("Middleware бота зарегистрированы")


__all__ = [
    "THROTTLE_RATE",
    "AccessMiddleware",
    "MenuAliasMiddleware",
    "ThrottlingMiddleware",
    "UserMiddleware",
    "setup_middlewares",
]
