"""Хендлеры поиска по библиотеке: команда ``/search``, FSM и инлайн-режим бота.

Поиск нечёткий (``backend.services.search``): прощает опечатки и неправильную
раскладку клавиатуры. Выдаются треки, альбомы («группы»), исполнители и папки.

V2 (ТЗ п. 1, 12, 17, 19) добавляет **мультивыбор исполнителей**: кнопка
«🎤 Фильтр по исполнителям» открывает клавиатуру с отметками «✅/⬜»
(:class:`ArtistPickCB`), выбранные идентификаторы копятся в данных FSM, а после
кнопки «Готово» бот спрашивает текст запроса и вызывает
:func:`backend.services.search.search_all` с ``artist_ids=...``. Фильтр работает
как ПЕРЕСЕЧЕНИЕ: в выдаче остаются только треки, у которых есть ВСЕ отмеченные
исполнители. Найденные папки показываются всегда — вместе с кнопками перехода
в них (ТЗ п. 17).

Пагинация найденных треков приходит как ``TrackCB(action="page", ctx="search")`` —
именно такие данные кладёт в кнопки ``keyboards.page_callback`` для контекста «search».
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedAudio,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import keyboards, texts, utils
from backend.bot.callbacks import ArtistCB, ArtistPickCB, FolderCB, TrackCB
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import tracks as tracks_repo
from backend.services import search as search_service

logger = logging.getLogger(__name__)

router = Router(name="search")

#: Контекст списка результатов в callback-данных (TrackCB.ctx).
CTX = "search"
#: Сколько результатов каждого типа запрашиваем у сервиса поиска.
SEARCH_LIMIT = 30
#: Сколько альбомов/исполнителей/папок показываем в сообщении.
EXTRA_LIMIT = 10
#: Сколько кнопок быстрых переходов добавляем к списку.
BUTTONS_LIMIT = 5
#: Ограничения инлайн-режима.
INLINE_LIMIT = 20
INLINE_CACHE_TIME = 10
#: Ключ в данных FSM, где хранится последний запрос (нужен для пагинации).
QUERY_KEY = "search_query"
#: Ключ в данных FSM со списком выбранных исполнителей (фильтр-пересечение).
FILTER_KEY = "search_artist_ids"
#: Ключ в данных FSM с сужающим запросом по списку исполнителей.
PICK_QUERY_KEY = "search_artist_pick_query"
#: Сколько исполнителей разрешаем отметить одновременно.
MAX_FILTER_ARTISTS = 10
#: Действия мультивыбора, которые обслуживает ЭТОТ модуль. Фильтр объявлен явно:
#: `ArtistPickCB` — общая фабрика, и другие разделы могут использовать свои действия.
PICK_ACTIONS = frozenset(
    {
        "open",
        "page",
        "toggle",
        "find",
        "showall",
        "clear",
        "done",
        "apply",
        "close",
        "cancel",
    }
)
#: Ограничение Telegram на длину текста сообщения.
MESSAGE_LIMIT = 4096

# --- Пользовательские тексты (RU) ----------------------------------------------------
# Общие формулировки берём из backend.bot.texts, локально — только уточнения.

ASK_QUERY = (
    f"{texts.SEARCH_PROMPT}\n"
    "Поиск прощает опечатки и неправильную раскладку.\n\n"
    "Чтобы отменить, отправьте /cancel."
)
NOTHING_FOUND_HINT = (
    "\n\nА ещё можно просто прислать боту аудиофайл — он сразу попадёт в библиотеку."
)
QUERY_LOST = "Повторите поиск: /search и запрос."
EXTRAS_HEADER = "🔍 Ещё найдено по запросу «{query}»"
ALBUMS_HEADER = "💿 <b>Альбомы и группы</b>"
ARTISTS_HEADER = "🎤 <b>Исполнители</b>"
FOLDERS_HEADER = "📁 <b>Папки</b>"

FILTER_BUTTON = "🎤 Фильтр по исполнителям"
FILTER_RESET_BUTTON = "🧹 Сбросить фильтр"
FILTER_APPLY_BUTTON = "📋 Показать все треки фильтра"
FILTER_DONE_BUTTON = "✅ Готово"
FILTER_FIND_BUTTON = "🔎 Найти исполнителя"
FILTER_SHOW_ALL_BUTTON = "♻️ Показать всех исполнителей"
FILTER_CLOSE_BUTTON = "✖️ Закрыть"

PICK_TITLE = "🎤 <b>Фильтр по исполнителям</b>"
PICK_HINT = (
    "Отмечайте исполнителей кнопками — в результатах останутся только треки, "
    "у которых есть <b>все</b> отмеченные."
)
PICK_EMPTY = (
    "🎤 Исполнителей пока нет — добавьте музыку, и они появятся автоматически."
)
PICK_NOT_FOUND = (
    "🤷 По этому запросу исполнители не нашлись. "
    "Нажмите «♻️ Показать всех исполнителей», чтобы вернуть полный список."
)
PICK_SELECTED = "Выбрано: {names}"
PICK_NOTHING_SELECTED = "Пока ничего не выбрано."
PICK_NARROWED = "Список сужен запросом «{query}»."
PICK_LIMIT_REACHED = f"Больше {MAX_FILTER_ARTISTS} исполнителей одновременно выбрать нельзя."
PICK_ASK_NAME = (
    "🔎 Напишите имя исполнителя — я оставлю в списке только подходящих.\n"
    "Чтобы отменить, отправьте /cancel."
)
PICK_FILTER_CLEARED = "🧹 Фильтр по исполнителям сброшен."
PICK_NEED_SELECTION = "Отметьте хотя бы одного исполнителя."
PICK_CLOSED = "Фильтр по исполнителям закрыт."
PICK_READY = (
    "{filter_line}\n\n"
    "Теперь напишите, что искать среди этих треков.\n"
    "Можно ничего не писать — нажмите «📋 Показать все треки фильтра».\n\n"
    "Чтобы отменить, отправьте /cancel."
)
FILTER_LINE = "🎤 Фильтр: <b>{names}</b>"
FILTER_EMPTY_RESULT = (
    "🔍 У выбранных исполнителей нет общих треков.\n"
    "Снимите часть отметок — фильтр ищет треки, где есть <b>все</b> выбранные."
)


# --- Состояния FSM -------------------------------------------------------------------


class _SearchStatesFallback(StatesGroup):
    """Резервная группа состояний, если backend.bot.states не предоставил свою."""

    waiting_query = State()


class SearchFilterStates(StatesGroup):
    """Состояния мультивыбора исполнителей (объявлены локально, см. правила V2)."""

    #: Ожидание имени исполнителя для сужения списка в клавиатуре фильтра.
    waiting_artist_query = State()


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
    from backend.bot.states import SearchStates as _ExternalSearchStates
except ImportError:  # pragma: no cover - зависит от порядка сборки проекта
    _ExternalSearchStates = None  # type: ignore[assignment]

WAITING_QUERY: State = (
    _pick_state(
        _ExternalSearchStates,
        ("waiting_query", "waiting_for_query", "query", "waiting_text", "search"),
    )
    or _SearchStatesFallback.waiting_query
)


# --- Вспомогательные функции ---------------------------------------------------------


def _per_page() -> int:
    """Размер страницы списка треков (из настроек, с разумными границами)."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, 50))


def _callback_message(callback: CallbackQuery) -> Message | None:
    """Возвращает сообщение колбэка, если оно доступно для редактирования."""
    message = callback.message
    return message if isinstance(message, Message) else None


def _button_text(prefix: str, name: str, limit: int = 40) -> str:
    """Короткая подпись кнопки без HTML-разметки."""
    clean = " ".join(str(name or "").split()) or "Без названия"
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return f"{prefix} {clean}"


def _trim(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Подрезает сообщение под лимит Telegram, не разрывая HTML-тег."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    opened = cut.rfind("<")
    if opened > cut.rfind(">"):
        cut = cut[:opened]
    logger.debug("Сообщение поиска подрезано до %s символов", limit)
    return cut.rstrip() + "…"


def _int(value: Any, default: int = 0) -> int:
    """Мягкое приведение к int (идентификаторы приходят из FSM и callback-данных)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_ids(value: Any) -> list[int]:
    """Приводит содержимое FSM к списку положительных id без дублей."""
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[int] = []
    for item in value:
        number = _int(item)
        if number > 0 and number not in result:
            result.append(number)
    return result[:MAX_FILTER_ARTISTS]


async def _load_filter(state: FSMContext) -> list[int]:
    """Читает выбранных исполнителей из данных FSM."""
    data = await state.get_data()
    return _clean_ids(data.get(FILTER_KEY))


async def _store_filter(state: FSMContext, artist_ids: Sequence[int]) -> None:
    """Сохраняет выбранных исполнителей в данные FSM."""
    await state.update_data(**{FILTER_KEY: _clean_ids(list(artist_ids))})


async def _filter_names(user_id: int, artist_ids: Sequence[int]) -> list[str]:
    """Имена выбранных исполнителей (пропавшие из библиотеки — молча опускаются)."""
    names: list[str] = []
    for artist_id in artist_ids:
        artist = await artists_repo.get_artist(user_id, artist_id)
        if artist is None:
            logger.debug("Исполнитель id=%s пропал из библиотеки — убран из фильтра", artist_id)
            continue
        names.append(str(artist.get("name") or "Без имени"))
    return names


def _filter_line(names: Sequence[str]) -> str:
    """Строка-заголовок с перечислением выбранных исполнителей."""
    if not names:
        return ""
    return FILTER_LINE.format(names=utils.escape(", ".join(names)))


# --- Клавиатуры своих экранов (объявлены локально, см. правила V2) --------------------


def search_prompt_kb(selected: Sequence[int]) -> InlineKeyboardMarkup:
    """Кнопки под приглашением к поиску: вход в фильтр и управление им."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=FILTER_BUTTON,
            callback_data=ArtistPickCB(action="open", artist_id=0, page=1).pack(),
        )
    )
    if selected:
        builder.row(
            InlineKeyboardButton(
                text=FILTER_APPLY_BUTTON,
                callback_data=ArtistPickCB(action="apply", artist_id=0, page=1).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=FILTER_RESET_BUTTON,
                callback_data=ArtistPickCB(action="clear", artist_id=0, page=1).pack(),
            )
        )
    return builder.as_markup()


def artist_multiselect_kb(
    artists: Sequence[dict],
    selected: Sequence[int],
    page: int,
    total_pages: int,
    *,
    narrowed: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура мультивыбора исполнителей с отметками «✅/⬜»."""
    builder = InlineKeyboardBuilder()
    chosen = set(selected)

    for artist in artists:
        artist_id = _int(artist.get("id"))
        if artist_id <= 0:
            continue
        mark = "✅" if artist_id in chosen else "⬜"
        count = _int(artist.get("track_count"))
        label = _button_text(mark, artist.get("name") or "", limit=32)
        if count:
            label = f"{label} · {count}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=ArtistPickCB(
                    action="toggle", artist_id=artist_id, page=max(int(page), 1)
                ).pack(),
            )
        )

    if total_pages > 1:
        current = min(max(int(page), 1), total_pages)
        prev_data = (
            ArtistPickCB(action="page", artist_id=0, page=current - 1).pack()
            if current > 1
            else keyboards.NOOP
        )
        next_data = (
            ArtistPickCB(action="page", artist_id=0, page=current + 1).pack()
            if current < total_pages
            else keyboards.NOOP
        )
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·", callback_data=prev_data
            ),
            InlineKeyboardButton(
                text=f"{current}/{total_pages}", callback_data=keyboards.NOOP
            ),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·", callback_data=next_data
            ),
        )

    if chosen:
        builder.row(
            InlineKeyboardButton(
                text=FILTER_DONE_BUTTON,
                callback_data=ArtistPickCB(action="done", artist_id=0, page=1).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=FILTER_APPLY_BUTTON,
                callback_data=ArtistPickCB(action="apply", artist_id=0, page=1).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=FILTER_RESET_BUTTON,
                callback_data=ArtistPickCB(action="clear", artist_id=0, page=1).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text=FILTER_FIND_BUTTON,
            callback_data=ArtistPickCB(action="find", artist_id=0, page=1).pack(),
        )
    )
    if narrowed:
        builder.row(
            InlineKeyboardButton(
                text=FILTER_SHOW_ALL_BUTTON,
                callback_data=ArtistPickCB(action="showall", artist_id=0, page=1).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=FILTER_CLOSE_BUTTON,
            callback_data=ArtistPickCB(action="close", artist_id=0, page=1).pack(),
        )
    )
    return builder.as_markup()


def filter_ready_kb() -> InlineKeyboardMarkup:
    """Кнопки экрана «фильтр собран, ждём запрос»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=FILTER_APPLY_BUTTON,
            callback_data=ArtistPickCB(action="apply", artist_id=0, page=1).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=FILTER_BUTTON,
            callback_data=ArtistPickCB(action="open", artist_id=0, page=1).pack(),
        ),
        InlineKeyboardButton(
            text=FILTER_RESET_BUTTON,
            callback_data=ArtistPickCB(action="clear", artist_id=0, page=1).pack(),
        ),
    )
    return builder.as_markup()


# --- Рендер экранов ------------------------------------------------------------------


def _extras_text(query: str, albums: Sequence[dict], artists: Sequence[dict],
                 folders: Sequence[dict]) -> str:
    """Текстовый блок с альбомами, исполнителями и папками."""
    blocks: list[str] = []

    if albums:
        lines = [ALBUMS_HEADER]
        # Альбомы («группы») показываем по алфавиту — требование контракта.
        ordered_albums = sorted(
            albums[:EXTRA_LIMIT], key=lambda item: (item.get("title") or "").casefold()
        )
        for album in ordered_albums:
            title = utils.escape(album.get("title") or "Без названия")
            line = f"• <b>{title}</b>"
            artist_name = (album.get("artist_name") or "").strip()
            if artist_name:
                line = f"{line} — {utils.escape(artist_name)}"
            line = f"{line} · {utils.tracks_count_label(int(album.get('track_count') or 0))}"
            lines.append(line)
        blocks.append("\n".join(lines))

    if artists:
        lines = [ARTISTS_HEADER]
        for artist in artists[:EXTRA_LIMIT]:
            name = utils.escape(artist.get("name") or "Без имени")
            mark = "✅" if artist.get("is_listened") else "⬜"
            lines.append(
                f"• {mark} <b>{name}</b> · "
                f"{utils.tracks_count_label(int(artist.get('track_count') or 0))}"
            )
        blocks.append("\n".join(lines))

    if folders:
        lines = [FOLDERS_HEADER]
        for folder in folders[:EXTRA_LIMIT]:
            name = utils.escape(folder.get("name") or "Без названия")
            total = folder.get("total_track_count")
            count = _int(folder.get("track_count"))
            line = f"• <b>{name}</b> · {utils.tracks_count_label(count)}"
            if total is not None and _int(total) != count:
                line = f"{line} (с подпапками — {_int(total)})"
            lines.append(line)
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    header = EXTRAS_HEADER.format(query=utils.escape(query))
    return _trim(header + "\n\n" + "\n\n".join(blocks))


def _extras_kb(artists: Sequence[dict], folders: Sequence[dict]) -> InlineKeyboardMarkup | None:
    """Кнопки быстрого перехода к найденным исполнителям и папкам (ТЗ п. 17)."""
    builder = InlineKeyboardBuilder()
    added = 0

    for artist in artists[:BUTTONS_LIMIT]:
        artist_id = artist.get("id")
        if artist_id is None:
            continue
        builder.button(
            text=_button_text("🎤", artist.get("name") or ""),
            callback_data=ArtistCB(action="open", artist_id=int(artist_id), page=1),
        )
        added += 1

    for folder in folders[:BUTTONS_LIMIT]:
        folder_id = folder.get("id")
        if folder_id is None:
            continue
        builder.button(
            text=_button_text("📁", folder.get("name") or ""),
            callback_data=FolderCB(action="open", folder_id=int(folder_id), page=1),
        )
        added += 1

    if not added:
        return None
    builder.adjust(1)
    return builder.as_markup()


def _safe_page(total: int, page: int, per_page: int) -> int:
    """Приводит номер страницы к существующему диапазону 1..N."""
    pages = max(1, (max(int(total), 0) + per_page - 1) // per_page)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), pages)


def _tracks_view(
    query: str,
    tracks: Sequence[dict],
    page: int,
    filter_names: Sequence[str] = (),
) -> tuple[str, Any]:
    """Текст и клавиатура страницы найденных треков (с учётом фильтра)."""
    per_page = _per_page()
    safe_page = _safe_page(len(tracks), page, per_page)
    page_items, total_pages = utils.paginate(list(tracks), safe_page, per_page)

    if query:
        title = texts.SEARCH_RESULTS_HEADER.format(query=utils.escape(query))
    else:
        title = "🔎 Треки выбранных исполнителей"
    header = f"<b>{title}</b>"
    line = _filter_line(filter_names)
    if line:
        header = f"{header}\n{line}"
    header = f"{header}\nНайдено: {utils.tracks_count_label(len(tracks))}"

    text = _trim(
        utils.render_track_list(
            header, page_items, safe_page, total_pages, texts.EMPTY_SEARCH_RESULTS
        )
    )
    markup = keyboards.tracks_page_kb(
        page_items, ctx=CTX, page=safe_page, total_pages=total_pages
    )
    return text, markup


async def _pick_view(
    user_id: int, state: FSMContext, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран мультивыбора исполнителей: текст и клавиатура с отметками."""
    data = await state.get_data()
    selected = _clean_ids(data.get(FILTER_KEY))
    narrow_query = " ".join(str(data.get(PICK_QUERY_KEY) or "").split())

    if narrow_query:
        artists = await search_service.search_artists(
            user_id, narrow_query, limit=SEARCH_LIMIT
        )
    else:
        artists = await artists_repo.list_artists(user_id)

    per_page = _per_page()
    items, total_pages = utils.paginate(list(artists), page, per_page)
    current = min(max(_int(page, 1), 1), total_pages)

    lines = [PICK_TITLE, "", PICK_HINT, ""]
    names = await _filter_names(user_id, selected)
    if names:
        lines.append(PICK_SELECTED.format(names=utils.escape(", ".join(names))))
    else:
        lines.append(PICK_NOTHING_SELECTED)
    if narrow_query:
        lines.append(f"<i>{PICK_NARROWED.format(query=utils.escape(narrow_query))}</i>")
    if not artists:
        lines.append("")
        lines.append(PICK_NOT_FOUND if narrow_query else PICK_EMPTY)
    elif total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    markup = artist_multiselect_kb(
        items, selected, current, total_pages, narrowed=bool(narrow_query)
    )
    return _trim("\n".join(lines)), markup


async def _show_pick(callback: CallbackQuery, state: FSMContext, page: int) -> None:
    """Перерисовывает экран фильтра в сообщении, из которого пришло нажатие."""
    if callback.from_user is None:
        return
    text, markup = await _pick_view(int(callback.from_user.id), state, page)
    message = _callback_message(callback)
    if message is not None:
        await utils.safe_edit(message, text, markup)
    elif callback.bot is not None:
        await callback.bot.send_message(
            callback.from_user.id, text, reply_markup=markup
        )


# --- Выполнение поиска ---------------------------------------------------------------


async def _run_search(
    message: Message,
    user_id: int,
    query: str,
    state: FSMContext,
    *,
    artist_ids: Sequence[int] | None = None,
) -> None:
    """Выполняет поиск и отправляет результаты пользователю."""
    clean_query = " ".join(str(query or "").split())
    selected = _clean_ids(list(artist_ids or []))

    if not clean_query and not selected:
        await message.answer(texts.EMPTY_QUERY)
        return

    try:
        result = await search_service.search_all(
            user_id,
            clean_query,
            limit=SEARCH_LIMIT,
            artist_ids=selected or None,
        )
    except Exception:
        logger.exception(
            "Ошибка поиска «%s» пользователя %s (фильтр %s)",
            clean_query,
            user_id,
            selected or "—",
        )
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    tracks = result.get("tracks") or []
    albums = result.get("albums") or []
    artists = result.get("artists") or []
    folders = result.get("folders") or []

    await state.update_data(**{QUERY_KEY: clean_query})
    names = await _filter_names(user_id, selected) if selected else []

    if not any((tracks, albums, artists, folders)):
        if clean_query:
            header = texts.SEARCH_RESULTS_HEADER.format(query=utils.escape(clean_query))
        else:
            header = "🔎 Треки выбранных исполнителей"
        parts = [f"<b>{header}</b>"]
        line = _filter_line(names)
        if line:
            parts.append(line)
        body = FILTER_EMPTY_RESULT if selected else texts.EMPTY_SEARCH_RESULTS
        hint = "" if selected else NOTHING_FOUND_HINT
        await message.answer(
            _trim("\n".join(parts) + "\n\n" + body + hint),
            reply_markup=search_prompt_kb(selected),
        )
        logger.info(
            "Поиск «%s» пользователя %s (фильтр %s): ничего не найдено",
            clean_query,
            user_id,
            selected or "—",
        )
        return

    if tracks:
        text, markup = _tracks_view(clean_query, tracks, 1, names)
        await message.answer(text, reply_markup=markup)

    extras = _extras_text(clean_query or ", ".join(names), albums, artists, folders)
    if extras:
        await message.answer(extras, reply_markup=_extras_kb(artists, folders))

    logger.info(
        "Поиск «%s» пользователя %s (фильтр %s): треков %s, альбомов %s, "
        "исполнителей %s, папок %s",
        clean_query,
        user_id,
        selected or "—",
        len(tracks),
        len(albums),
        len(artists),
        len(folders),
    )


# --- Команда /search -----------------------------------------------------------------


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Поиск по библиотеке: с аргументом — сразу, без аргумента — спрашиваем запрос."""
    if message.from_user is None:
        return
    selected = await _load_filter(state)
    query = (command.args or "").strip()
    if not query:
        await state.set_state(WAITING_QUERY)
        text = ASK_QUERY
        if selected:
            names = await _filter_names(int(message.from_user.id), selected)
            line = _filter_line(names)
            if line:
                text = f"{line}\n\n{text}"
        await message.answer(text, reply_markup=search_prompt_kb(selected))
        return
    await state.set_state(None)
    await _run_search(
        message, message.from_user.id, query, state, artist_ids=selected
    )


@router.message(StateFilter(WAITING_QUERY), Command("cancel"))
async def cancel_search(message: Message, state: FSMContext) -> None:
    """Отменяет ожидание поискового запроса."""
    await state.set_state(None)
    await message.answer(texts.SEARCH_CANCELLED)


@router.message(StateFilter(WAITING_QUERY), F.text)
async def search_query_entered(message: Message, state: FSMContext) -> None:
    """Принимает поисковый запрос из FSM (с учётом фильтра по исполнителям)."""
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

    selected = await _load_filter(state)
    await state.set_state(None)
    await _run_search(message, message.from_user.id, text, state, artist_ids=selected)


# --- Мультивыбор исполнителей (ТЗ п. 1, 19) ------------------------------------------


@router.message(StateFilter(SearchFilterStates.waiting_artist_query), Command("cancel"))
async def cancel_artist_query(message: Message, state: FSMContext) -> None:
    """Отменяет ввод имени исполнителя для сужения списка фильтра."""
    if message.from_user is None:
        return
    await state.set_state(None)
    text, markup = await _pick_view(int(message.from_user.id), state, 1)
    await message.answer(text, reply_markup=markup)


@router.message(StateFilter(SearchFilterStates.waiting_artist_query), F.text)
async def artist_query_entered(message: Message, state: FSMContext) -> None:
    """Сужает список исполнителей в клавиатуре фильтра по введённому имени."""
    if message.from_user is None:
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await state.set_state(None)
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return
    if not raw:
        await message.answer(texts.EMPTY_QUERY)
        return

    await state.update_data(**{PICK_QUERY_KEY: " ".join(raw.split())})
    await state.set_state(None)
    text, markup = await _pick_view(int(message.from_user.id), state, 1)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ArtistPickCB.filter(F.action.in_(PICK_ACTIONS)))
async def cb_artist_pick(
    callback: CallbackQuery, callback_data: ArtistPickCB, state: FSMContext
) -> None:
    """Единый обработчик мультивыбора исполнителей для фильтра поиска."""
    if callback.from_user is None:
        await utils.ack(callback)
        return

    user_id = int(callback.from_user.id)
    action = (callback_data.action or "").strip().casefold()
    page = max(_int(callback_data.page, 1), 1)
    artist_id = _int(callback_data.artist_id)

    if action == "open":
        await state.set_state(None)
        await _show_pick(callback, state, page)
        await utils.ack(callback)
        return

    if action == "page":
        await _show_pick(callback, state, page)
        await utils.ack(callback)
        return

    if action == "toggle":
        selected = await _load_filter(state)
        if artist_id <= 0:
            await utils.ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
            return
        if artist_id in selected:
            selected.remove(artist_id)
            notice = "Исполнитель убран из фильтра."
        else:
            if len(selected) >= MAX_FILTER_ARTISTS:
                await utils.ack(callback, PICK_LIMIT_REACHED, alert=True)
                return
            artist = await artists_repo.get_artist(user_id, artist_id)
            if artist is None:
                await utils.ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
                return
            selected.append(artist_id)
            notice = "Исполнитель добавлен в фильтр."
        await _store_filter(state, selected)
        await _show_pick(callback, state, page)
        await utils.ack(callback, notice)
        return

    if action == "find":
        await state.set_state(SearchFilterStates.waiting_artist_query)
        message = _callback_message(callback)
        if message is not None:
            await message.answer(PICK_ASK_NAME)
        elif callback.bot is not None:
            await callback.bot.send_message(user_id, PICK_ASK_NAME)
        await utils.ack(callback)
        return

    if action == "showall":
        await state.update_data(**{PICK_QUERY_KEY: ""})
        await _show_pick(callback, state, 1)
        await utils.ack(callback)
        return

    if action == "clear":
        await _store_filter(state, [])
        await state.update_data(**{PICK_QUERY_KEY: ""})
        await _show_pick(callback, state, 1)
        await utils.ack(callback, PICK_FILTER_CLEARED)
        logger.info("Пользователь %s сбросил фильтр по исполнителям", user_id)
        return

    if action == "done":
        selected = await _load_filter(state)
        if not selected:
            await utils.ack(callback, PICK_NEED_SELECTION, alert=True)
            return
        names = await _filter_names(user_id, selected)
        await state.set_state(WAITING_QUERY)
        text = _trim(PICK_READY.format(filter_line=_filter_line(names)))
        message = _callback_message(callback)
        if message is not None:
            await utils.safe_edit(message, text, filter_ready_kb())
        elif callback.bot is not None:
            await callback.bot.send_message(user_id, text, reply_markup=filter_ready_kb())
        await utils.ack(callback)
        logger.info("Пользователь %s выбрал фильтр по исполнителям %s", user_id, selected)
        return

    if action == "apply":
        selected = await _load_filter(state)
        if not selected:
            await utils.ack(callback, PICK_NEED_SELECTION, alert=True)
            return
        await state.set_state(None)
        message = _callback_message(callback)
        if message is None:
            await utils.ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
            return
        await utils.ack(callback)
        await _run_search(message, user_id, "", state, artist_ids=selected)
        return

    if action in {"close", "cancel"}:
        await state.set_state(None)
        message = _callback_message(callback)
        if message is not None:
            selected = await _load_filter(state)
            names = await _filter_names(user_id, selected)
            line = _filter_line(names)
            body = f"{line}\n\n{PICK_CLOSED}" if line else PICK_CLOSED
            await utils.safe_edit(message, body, search_prompt_kb(selected))
        await utils.ack(callback)
        return

    logger.debug("Неизвестное действие фильтра исполнителей: %r", action)
    await utils.ack(callback, texts.ERROR_TRY_AGAIN, alert=True)


# --- Пагинация результатов -----------------------------------------------------------


@router.callback_query(
    TrackCB.filter((F.action.in_({"page", "back"})) & (F.ctx == CTX))
)
async def cb_search_page(callback: CallbackQuery, callback_data: TrackCB,
                         state: FSMContext) -> None:
    """Листает страницы найденных треков (запрос и фильтр берутся из данных FSM)."""
    data = await state.get_data()
    query = str(data.get(QUERY_KEY) or "").strip()
    selected = _clean_ids(data.get(FILTER_KEY))
    if not query and not selected:
        await callback.answer(QUERY_LOST, show_alert=True)
        return

    user_id = int(callback.from_user.id)
    try:
        if selected:
            result = await search_service.search_all(
                user_id, query, limit=SEARCH_LIMIT, artist_ids=selected
            )
            tracks = result.get("tracks") or []
        else:
            tracks = await search_service.search_tracks(
                user_id, query, limit=SEARCH_LIMIT
            )
    except Exception:
        logger.exception("Ошибка пагинации поиска пользователя %s", user_id)
        await callback.answer(texts.ERROR_TRY_AGAIN, show_alert=True)
        return

    if not tracks:
        await callback.answer(texts.EMPTY_SEARCH_RESULTS, show_alert=True)
        return

    names = await _filter_names(user_id, selected) if selected else []
    text, markup = _tracks_view(query, tracks, max(callback_data.page, 1), names)
    message = _callback_message(callback)
    if message is not None:
        await utils.safe_edit(message, text, markup)
    await callback.answer()


# --- Инлайн-режим --------------------------------------------------------------------


@router.inline_query()
async def inline_search(query: InlineQuery) -> None:
    """Инлайн-поиск по своей библиотеке: отдаёт аудио по сохранённым file_id."""
    user_id = query.from_user.id
    text = (query.query or "").strip()

    try:
        if text:
            tracks = await search_service.search_tracks(user_id, text, limit=INLINE_LIMIT)
        else:
            tracks = await tracks_repo.list_tracks(
                user_id, order="created_at_desc", limit=INLINE_LIMIT
            )
    except Exception:
        logger.exception("Ошибка инлайн-поиска пользователя %s", user_id)
        tracks = []

    results: list[InlineQueryResultCachedAudio] = []
    for track in tracks:
        file_id = track.get("file_id")
        track_id = track.get("id")
        if not file_id or track_id is None:
            continue
        results.append(
            InlineQueryResultCachedAudio(
                id=str(track_id),
                audio_file_id=str(file_id),
            )
        )
        if len(results) >= INLINE_LIMIT:
            break

    try:
        await query.answer(
            results,
            cache_time=INLINE_CACHE_TIME,
            is_personal=True,
        )
    except TelegramAPIError:
        logger.warning("Не удалось ответить на инлайн-запрос пользователя %s", user_id)


__all__ = [
    "CTX",
    "FILTER_KEY",
    "MAX_FILTER_ARTISTS",
    "SearchFilterStates",
    "artist_multiselect_kb",
    "router",
    "search_prompt_kb",
]
