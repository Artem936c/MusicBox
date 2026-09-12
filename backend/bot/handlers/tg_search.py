"""Хендлеры поиска аудио в Telegram: команда ``/tgsearch``, импорт и ссылки t.me.

Поиск идёт через ``backend.services.telegram_search`` (Telethon). Если функция
выключена или не настроена, пользователь получает понятное объяснение и подсказку
переслать аудио боту вручную — такой импорт работает всегда.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from backend.bot import keyboards, texts, utils
from backend.bot.callbacks import TgSearchCB
from backend.errors import (
    FileTooLargeError,
    MusicBoxError,
    TelegramSearchUnavailable,
)
from backend.services import metadata
from backend.services.telegram_search import RemoteAudio, import_remote, telegram_search

logger = logging.getLogger(__name__)

router = Router(name="tg_search")

#: Сколько результатов запрашиваем у Telegram за один поиск.
SEARCH_LIMIT = 20

#: Ссылка на сообщение в Telegram (публичные и приватные каналы).
_TME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?:c/\d+/(?:\d+/)?\d+|[A-Za-z0-9_]{3,32}/(?:\d+/)?\d+)",
    re.IGNORECASE,
)

# --- Пользовательские тексты (RU) ----------------------------------------------------
# Общие формулировки берём из backend.bot.texts, локально — только уточнения.

ASK_QUERY = f"{texts.TG_SEARCH_PROMPT}\n\nЧтобы отменить, отправьте /cancel."
SEARCHING = "🔎 Ищу в Telegram…"
RESULTS_HINT = "Нажмите «⬇️», чтобы добавить трек в свою библиотеку."
RESULTS_EXPIRED = "Результаты поиска устарели. Повторите поиск командой /tgsearch."
IMPORTING = "Добавляю трек…"
IMPORT_DONE_FOLDER = "✅ Трек «{title}» добавлен в папку «{folder}»."
CHANNEL_LINE = "    📣 {chat}"
RESULTS_COUNT = "Результатов: {count}"

#: Метки погашенной кнопки импорта: трек добавлен / трек уже был в библиотеке.
IMPORT_DONE_MARK = "✅"
IMPORT_DUPLICATE_MARK = "🎵"


# --- Состояния FSM -------------------------------------------------------------------


class _TgSearchStatesFallback(StatesGroup):
    """Резервная группа состояний, если backend.bot.states не предоставил свою."""

    waiting_query = State()


def _pick_state(group: Any, names: Sequence[str]) -> State | None:
    """Находит подходящее состояние в группе по одному из ожидаемых имён."""
    if group is None:
        return None
    for name in names:
        candidate = getattr(group, name, None)
        if isinstance(candidate, State):
            return candidate
    for candidate in getattr(group, "__states__", ()) or ():
        if isinstance(candidate, State):
            return candidate
    return None


try:  # состояния объявлены в общем модуле, но модуль может отсутствовать
    from backend.bot.states import TgSearchStates as _ExternalTgSearchStates
except ImportError:  # pragma: no cover - зависит от порядка сборки проекта
    _ExternalTgSearchStates = None  # type: ignore[assignment]

WAITING_QUERY: State = (
    _pick_state(
        _ExternalTgSearchStates,
        ("waiting_query", "waiting_for_query", "query", "waiting_text", "search"),
    )
    or _TgSearchStatesFallback.waiting_query
)


# --- Вспомогательные функции ---------------------------------------------------------


def _callback_message(callback: CallbackQuery) -> Message | None:
    """Возвращает сообщение колбэка, если оно доступно для редактирования."""
    message = callback.message
    return message if isinstance(message, Message) else None


def _size_label(size: int | None) -> str:
    """Размер файла в мегабайтах для пользовательских сообщений."""
    try:
        value = int(size or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "—"
    return f"{value / (1024 * 1024):.1f} МБ".replace(".", ",")


def _remote_title(remote: RemoteAudio) -> str:
    """«Исполнитель — Название» одной строкой (без HTML-разметки)."""
    parts = [part for part in (remote.performer, remote.title) if part]
    return " — ".join(parts) if parts else (remote.title or "Без названия")


def _unavailable_text(exc: Exception) -> str:
    """Понятное объяснение, почему поиск по Telegram сейчас не работает."""
    message = str(exc or "").strip()
    if not message:
        return texts.TG_SEARCH_UNAVAILABLE
    return utils.escape(message)


def _results_text(query: str, items: Sequence[RemoteAudio]) -> str:
    """Текст сообщения со списком найденного в Telegram."""
    header = texts.TG_RESULTS_HEADER.format(query=utils.escape(query))
    lines = [f"<b>{header}</b>", RESULTS_COUNT.format(count=len(items)), ""]
    for index, remote in enumerate(items, start=1):
        title = utils.escape(_remote_title(remote))
        details = [metadata.format_duration(remote.duration), _size_label(remote.file_size)]
        line = f"{index}. <b>{title}</b> · {' · '.join(details)}"
        chat_title = (remote.chat_title or "").strip()
        if chat_title:
            line = f"{line}\n{CHANNEL_LINE.format(chat=utils.escape(chat_title))}"
        lines.append(line)
    lines.append("")
    lines.append(RESULTS_HINT)
    return "\n".join(lines)


def _remote_details(remote: RemoteAudio) -> str:
    """Подробности о найденном треке для всплывающего окна."""
    rows = [
        _remote_title(remote),
        f"Длительность: {metadata.format_duration(remote.duration)}",
        f"Размер: {_size_label(remote.file_size)}",
    ]
    chat_title = (remote.chat_title or "").strip()
    if chat_title:
        rows.append(f"Канал: {chat_title}")
    if remote.link:
        rows.append(remote.link)
    return "\n".join(rows)


def _has_telegram_link(message: Message) -> bool:
    """Фильтр: в тексте сообщения есть ссылка на сообщение Telegram."""
    return bool(message.text and _TME_RE.search(message.text))


def _extract_link(text: str | None) -> str | None:
    """Достаёт первую ссылку t.me из текста."""
    if not text:
        return None
    match = _TME_RE.search(text)
    return match.group(0) if match else None


def _is_duplicate(track: dict | None) -> bool:
    """`True`, если сервис вернул уже имевшийся в библиотеке трек (импорта не было)."""
    return isinstance(track, dict) and track.get("duplicate") is True


def _import_success_text(track: dict) -> str:
    """Итоговое сообщение о результате импорта трека."""
    if _is_duplicate(track):
        return texts.DUPLICATE_TRACK
    title = utils.escape(track.get("title") or "Без названия")
    folder_name = (track.get("folder_name") or "").strip()
    if folder_name:
        return IMPORT_DONE_FOLDER.format(title=title, folder=utils.escape(folder_name))
    return texts.TG_IMPORT_DONE.format(title=title)


def _is_import_button(button: InlineKeyboardButton, token: str) -> bool:
    """`True`, если это кнопка импорта того самого результата поиска."""
    data = getattr(button, "callback_data", None)
    if not data:
        return False
    try:
        parsed = TgSearchCB.unpack(str(data))
    except (TypeError, ValueError):
        return False
    return parsed.action == "import" and parsed.token == token


def _mark_import_button(
    markup: Any, token: str, mark: str
) -> InlineKeyboardMarkup | None:
    """Заменяет кнопку импорта на неактивную отметку. `None` — менять нечего."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    rows: list[list[InlineKeyboardButton]] = []
    changed = False
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            if _is_import_button(button, token):
                number = (button.text or "").split()[-1] if button.text else ""
                new_row.append(
                    InlineKeyboardButton(
                        text=f"{mark} {number}".strip(),
                        callback_data=keyboards.NOOP,
                    )
                )
                changed = True
                continue
            new_row.append(button)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else None


async def _deactivate_import_button(
    message: Message | None, token: str, mark: str
) -> None:
    """Гасит кнопку импорта обработанного результата, чтобы её не нажали снова."""
    if message is None:
        return
    markup = _mark_import_button(message.reply_markup, token, mark)
    if markup is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramAPIError as exc:
        logger.debug("Не удалось погасить кнопку импорта (токен %s): %s", token, exc)


async def _report(message: Message | None, chat_id: int, bot: Bot, text: str) -> None:
    """Обновляет сообщение о прогрессе или отправляет новое."""
    if message is not None:
        await utils.safe_edit(message, text)
        return
    await bot.send_message(chat_id, text)


async def _do_import(bot: Bot, user_id: int, chat_id: int, remote: RemoteAudio,
                     progress: Message | None) -> dict | None:
    """Импортирует найденное аудио и сообщает результат. `None` — импорт не удался."""
    try:
        track = await import_remote(bot, user_id, remote)
    except TelegramSearchUnavailable as exc:
        await _report(progress, chat_id, bot, _unavailable_text(exc))
        return None
    except FileTooLargeError as exc:
        await _report(progress, chat_id, bot, f"⚠️ {utils.escape(str(exc))}")
        return None
    except MusicBoxError as exc:
        logger.warning("Импорт из Telegram не удался (пользователь %s): %s", user_id, exc)
        await _report(progress, chat_id, bot, f"⚠️ {utils.escape(str(exc))}")
        return None
    except Exception:
        logger.exception("Неожиданная ошибка импорта из Telegram (пользователь %s)", user_id)
        await _report(progress, chat_id, bot, texts.ERROR_GENERIC)
        return None

    await _report(progress, chat_id, bot, _import_success_text(track))
    if _is_duplicate(track):
        logger.info(
            "Трек %s уже был в библиотеке пользователя %s — повторный импорт не нужен (%s)",
            track.get("id"),
            user_id,
            remote.link or remote.title,
        )
    else:
        logger.info(
            "Пользователь %s импортировал трек %s из Telegram (%s)",
            user_id,
            track.get("id"),
            remote.link or remote.title,
        )
    return track


async def _run_tg_search(message: Message, user_id: int, query: str) -> None:
    """Выполняет поиск в Telegram и отправляет результаты."""
    clean_query = " ".join(str(query or "").split())
    if not clean_query:
        await message.answer(texts.EMPTY_QUERY)
        return

    progress = await message.answer(SEARCHING)

    try:
        items = await telegram_search.search(clean_query, limit=SEARCH_LIMIT)
    except TelegramSearchUnavailable as exc:
        await utils.safe_edit(progress, _unavailable_text(exc))
        return
    except MusicBoxError as exc:
        logger.warning("Поиск в Telegram не удался (пользователь %s): %s", user_id, exc)
        await utils.safe_edit(progress, f"⚠️ {utils.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Неожиданная ошибка поиска в Telegram (пользователь %s)", user_id)
        await utils.safe_edit(progress, texts.TG_SEARCH_ERROR)
        return

    if not items:
        header = texts.TG_RESULTS_HEADER.format(query=utils.escape(clean_query))
        await utils.safe_edit(
            progress, f"<b>{header}</b>\n\n{texts.EMPTY_TG_RESULTS}"
        )
        return

    await utils.safe_edit(
        progress, _results_text(clean_query, items), keyboards.tg_results_kb(items)
    )
    logger.info(
        "Поиск в Telegram «%s» пользователя %s: %s результатов",
        clean_query,
        user_id,
        len(items),
    )


# --- Команда /tgsearch ---------------------------------------------------------------


@router.message(Command("tgsearch"))
async def cmd_tg_search(message: Message, command: CommandObject,
                        state: FSMContext) -> None:
    """Поиск аудио в публичных каналах Telegram."""
    if message.from_user is None:
        return
    query = (command.args or "").strip()
    if not query:
        await state.set_state(WAITING_QUERY)
        await message.answer(ASK_QUERY)
        return
    await state.set_state(None)
    await _run_tg_search(message, message.from_user.id, query)


@router.message(StateFilter(WAITING_QUERY), Command("cancel"))
async def cancel_tg_search(message: Message, state: FSMContext) -> None:
    """Отменяет ожидание запроса для поиска в Telegram."""
    await state.set_state(None)
    await message.answer(texts.SEARCH_CANCELLED)


@router.message(StateFilter(WAITING_QUERY), F.text)
async def tg_search_query_entered(message: Message, state: FSMContext,
                                  bot: Bot) -> None:
    """Принимает запрос из FSM: ссылку импортирует, текст ищет в каналах."""
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(texts.EMPTY_QUERY)
        return
    if text.startswith("/"):
        await state.set_state(None)
        await message.answer(f"{texts.SEARCH_CANCELLED} Повторите команду ещё раз.")
        return

    await state.set_state(None)

    link = _extract_link(text)
    if link:
        await import_by_link(message, bot, link)
        return

    await _run_tg_search(message, message.from_user.id, text)


# --- Импорт по кнопке ----------------------------------------------------------------


@router.callback_query(TgSearchCB.filter(F.action == "import"))
async def cb_import_remote(callback: CallbackQuery, callback_data: TgSearchCB,
                           bot: Bot) -> None:
    """Импортирует найденный в Telegram трек в библиотеку пользователя."""
    remote = telegram_search.get_cached(callback_data.token)
    if remote is None:
        await callback.answer(RESULTS_EXPIRED, show_alert=True)
        return

    await callback.answer(IMPORTING)
    message = _callback_message(callback)
    chat_id = message.chat.id if message is not None else callback.from_user.id
    progress = await bot.send_message(chat_id, texts.TG_IMPORT_STARTED)
    track = await _do_import(bot, callback.from_user.id, chat_id, remote, progress)
    if track is None:
        # Импорт не удался — кнопку оставляем, чтобы можно было повторить попытку.
        return
    mark = IMPORT_DUPLICATE_MARK if _is_duplicate(track) else IMPORT_DONE_MARK
    await _deactivate_import_button(message, callback_data.token, mark)


@router.callback_query(TgSearchCB.filter(F.action == "info"))
async def cb_remote_info(callback: CallbackQuery, callback_data: TgSearchCB) -> None:
    """Показывает подробности найденного трека."""
    remote = telegram_search.get_cached(callback_data.token)
    if remote is None:
        await callback.answer(RESULTS_EXPIRED, show_alert=True)
        return
    await callback.answer(_remote_details(remote)[:200], show_alert=True)


# --- Импорт по ссылке t.me -----------------------------------------------------------


async def import_by_link(message: Message, bot: Bot, link: str) -> None:
    """Импортирует аудио по ссылке на сообщение Telegram."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    progress = await message.answer(texts.TG_IMPORT_STARTED)

    try:
        remote = await telegram_search.resolve_link(link)
    except TelegramSearchUnavailable as exc:
        await utils.safe_edit(progress, _unavailable_text(exc))
        return
    except MusicBoxError as exc:
        logger.warning("Не удалось разобрать ссылку %s: %s", link, exc)
        await utils.safe_edit(progress, f"⚠️ {utils.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Неожиданная ошибка разбора ссылки %s", link)
        await utils.safe_edit(progress, texts.ERROR_GENERIC)
        return

    if remote is None:
        await utils.safe_edit(progress, texts.TG_LINK_NOT_FOUND)
        return

    await _do_import(bot, user_id, chat_id, remote, progress)


@router.message(StateFilter(None), F.text, _has_telegram_link)
async def handle_telegram_link(message: Message, bot: Bot) -> None:
    """Ловит ссылку t.me в обычном сообщении и импортирует трек."""
    link = _extract_link(message.text)
    if not link:
        return
    await import_by_link(message, bot, link)


__all__ = ["router"]
