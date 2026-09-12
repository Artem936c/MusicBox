"""Сборка роутеров бота MusicBox.

Каждый модуль-обработчик экспортирует переменную `router: Router`.
Порядок подключения важен: `upload` идёт ПОСЛЕДНИМ, потому что он ловит любые
сообщения с аудио (в том числе пересланные) и не должен перехватывать
сообщения, адресованные другим разделам.

Перед разделами включается страж диалогов (`_build_dialog_guard`): он пропускает
команду к её собственному разделу, даже если пользователь находится посреди
диалога ввода (см. комментарий у функции).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any

from aiogram import Dispatcher, Router
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)

#: Модули обработчиков в порядке подключения (upload — последним).
#: `settings` стоит сразу после `start`: команда `/settings` (и кнопка
#: «⚙️ Настройки» нижней клавиатуры) должна срабатывать даже посреди диалогов
#: ввода — их FSM-хендлеры ловят любой текст, а карточка настроек сама
#: сбрасывает состояние.
#: Порядок разделов V2 зафиксирован разделом 5 контракта `docs/ARCHITECTURE-V2.md`:
#: диалоговые модули (`search`, `tg_search`) и всеядный `upload` — в самом конце,
#: чтобы не перехватывать сообщения соседних разделов.
HANDLER_MODULES: tuple[str, ...] = (
    "start",
    "settings",
    "stats",
    "tracks",
    "folders",
    "artists",
    "playlists",
    "favourites",
    "notes",
    "recommendations",
    "other",
    "edit",
    "batch",
    "search",
    "tg_search",
    "upload",
)


def _command_name(text: str | None) -> str:
    """Имя команды из текста сообщения: «/search@Bot запрос» → «search».

    Разбор повторяет `aiogram.filters.Command`: префикс «/», упоминание бота
    после «@», аргументы через пробел. Для обычного текста возвращается "".
    """
    head = (text or "").strip().split(maxsplit=1)
    if not head or not head[0].startswith("/"):
        return ""
    return head[0][1:].partition("@")[0]


def _build_dialog_guard() -> Callable[..., Awaitable[Any]]:
    """Страж диалогов: команда, присланная посреди ввода, доходит до своего раздела.

    FSM-хендлеры разделов объявлены как `StateFilter(...), F.text` и ловят ЛЮБОЙ
    текст в своём состоянии. Поэтому команда раздела, чей роутер стоит ПОЗЖЕ
    (например `/search` или `/tgsearch` из состояния заметок или раскладки),
    не выполнялась вовсе: ранний диалог только отменял себя и просил повторить
    ввод, тогда как команды ранних роутеров (`/folders`, `/settings`) срабатывали
    с первого раза. Кнопки нижней клавиатуры (`MenuAliasMiddleware` подменяет их
    на команды) вели себя так же половинчато.

    Страж — outer-middleware сообщений: он работает до роутеров и, увидев
    известную команду при активном состоянии, снимает состояние. Дальше событие
    маршрутизируется как обычное сообщение без состояния, то есть попадает ровно
    в тот раздел, чью команду нажал пользователь. Порядок разделов
    (`HANDLER_MODULES`, раздел 5 контракта `docs/ARCHITECTURE-V2.md`) не меняется.

    Именно middleware, а не роутер: `StateFilter` сравнивает состояние со
    значением `raw_state`, которое кладут в данные обновления ДО маршрутизации,
    поэтому обработчику его уже не изменить — правку нужно вносить в те же данные.

    Снимается только состояние (`set_state(None)`), данные FSM остаются: их делят
    разные разделы (например фильтр по исполнителям у `/search`), и чужие ключи
    чистить нельзя — тем же правилом живёт `batch._reset_state`.

    `/cancel` намеренно не перехватывается: этой команды нет в `texts.COMMANDS`,
    а её обработчики объявлены с `StateFilter(...)` и нуждаются в состоянии.
    """
    # Импорт локальный: пакет `backend.bot` в этот момент ещё инициализируется.
    from backend.bot.texts import COMMANDS

    known_commands = frozenset(name for name, _ in COMMANDS)

    async def dialog_guard(
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Снимает состояние диалога, если пришла известная команда бота."""
        state = data.get("state")
        raw_state = data.get("raw_state")
        if raw_state is not None and state is not None and isinstance(event, Message):
            command = _command_name(event.text or event.caption)
            if command in known_commands:
                logger.debug(
                    "Команда /%s прервала диалог (%s) — состояние снято", command, raw_state
                )
                await state.set_state(None)
                # Фильтры `StateFilter` смотрят именно в эти данные, а не в хранилище.
                data["raw_state"] = None
        return await handler(event, data)

    return dialog_guard


def _load_router(name: str) -> Router | None:
    """Импортирует модуль обработчиков и возвращает его `router`.

    Отсутствие самого модуля не считается фатальной ошибкой: бот поднимется
    без соответствующего раздела, а в лог попадёт понятное сообщение.
    Любые другие ошибки импорта пробрасываются — их нужно чинить, а не прятать.
    """
    module_path = f"{__name__}.{name}"
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            logger.error(
                "Модуль обработчиков %s не найден — соответствующий раздел бота недоступен",
                module_path,
            )
            return None
        raise

    router = getattr(module, "router", None)
    if not isinstance(router, Router):
        logger.error("Модуль %s не экспортирует Router — раздел пропущен", module_path)
        return None
    return router


def register_handlers(dp: Dispatcher) -> None:
    """Подключает все роутеры обработчиков к диспетчеру (и стража диалогов)."""
    # Страж включается до роутеров: иначе диалог раннего раздела съест команду позднего.
    dp.message.outer_middleware(_build_dialog_guard())
    logger.debug("Страж диалогов включён")

    connected = 0
    for name in HANDLER_MODULES:
        router = _load_router(name)
        if router is None:
            continue
        dp.include_router(router)
        connected += 1
        logger.debug("Роутер %s подключён", name)

    if not connected:
        logger.error("Не подключён ни один роутер — бот не сможет отвечать на сообщения")
    else:
        logger.info("Подключено роутеров: %s из %s", connected, len(HANDLER_MODULES))


__all__ = ["HANDLER_MODULES", "register_handlers"]
