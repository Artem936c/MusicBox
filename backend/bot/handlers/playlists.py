"""Хендлеры плейлистов: список, создание, удаление, состав и воспроизведение.

Раздел 11 контракта V1: команда ``/playlists`` и все действия фабрики ``PlaylistCB``
(``open|page|create|delete|add|remove|play|up|down|back|list``).

Доработка V2 (ТЗ п. 19, 20):

* ``/create_playlist`` — создание плейлиста (с названием в аргументе или через FSM);
* ``/playlist <название>`` — открыть плейлист по имени, с нечётким поиском,
  если точного совпадения нет;
* кнопка «➕ Добавить трек» в карточке плейлиста — список треков библиотеки
  с пагинацией и поиском, по нажатию трек попадает в ЭТОТ плейлист;
* треки уходят пользователю через ``services.media.send_media_to_user`` — аудио
  как есть, без конвертации.

Соглашения по callback-данным (внутри этого модуля):

* ``page``      — при ``playlist_id > 0`` листает треки плейлиста, иначе список плейлистов;
* ``play``      — при ``track_id > 0`` отправляет один трек, иначе весь плейлист (до 10 треков);
* ``delete``    — при ``track_id == 0`` спрашивает подтверждение, при ``track_id == 1`` удаляет;
* ``create``    — при ``track_id > 0`` после создания сразу кладёт этот трек в новый плейлист;
* ``pick``      — экран выбора трека для добавления (``page`` — страница выбора);
* ``padd``      — добавить выбранный трек и остаться на экране выбора;
* ``pfind``     — спросить поисковую фразу для экрана выбора;
* ``pclr``      — сбросить поиск на экране выбора.

Клавиатуры и тексты новых экранов объявлены локально: общие модули
`backend.bot.keyboards` и `backend.bot.texts` этот хендлер не изменяет.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import keyboards, texts, utils
from backend.bot.callbacks import PlaylistCB
from backend.config import settings
from backend.db.repositories import playlists as playlists_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import MusicBoxError, ValidationError
from backend.services import media, metadata, search

logger = logging.getLogger(__name__)

router = Router(name="playlists")

# --- Настройки поведения -------------------------------------------------------------

#: Сколько треков отправляем за одно нажатие «Проиграть плейлист».
MAX_PLAY_BATCH = 10
#: Пауза между отправками, чтобы не поймать flood limit Telegram.
PLAY_DELAY = 0.3
#: Значение track_id в PlaylistCB, означающее «удаление подтверждено».
CONFIRM_FLAG = 1
#: Максимальная длина текста в callback.answer (ограничение Telegram — 200 символов).
ANSWER_LIMIT = 190

#: Действия PlaylistCB, добавленные в V2 (экран «➕ Добавить трек»).
ACTION_PICK = "pick"
ACTION_PICK_ADD = "padd"
ACTION_PICK_FIND = "pfind"
ACTION_PICK_CLEAR = "pclr"

#: Сколько треков берём из нечёткого поиска на экране выбора.
PICK_SEARCH_LIMIT = 60
#: Сколько кнопок с номерами треков помещаем в одну строку.
PICK_BUTTONS_PER_ROW = 5
#: Оценка для плейлиста, чьё название содержит запрос целиком.
SUBSTRING_SCORE = 90
#: Оценка точного совпадения названия плейлиста.
EXACT_SCORE = 100
#: Максимум плейлистов в подсказке «нашлось несколько».
MAX_NAME_MATCHES = 10

#: Ключи данных FSM для экрана выбора трека.
PICK_ID_KEY = "playlist_pick_id"
PICK_QUERY_KEY = "playlist_pick_query"

#: «ё» и «е» считаем одной буквой при сравнении названий плейлистов.
_YO_TRANSLATION = str.maketrans({"ё": "е", "Ё": "е"})

# --- Пользовательские тексты (RU) ----------------------------------------------------
# Общие формулировки берём из backend.bot.texts, локально держим только те,
# которых там нет.

PLAYLISTS_TITLE = "🎼 <b>Ваши плейлисты</b>"
ASK_NAME = texts.PLAYLIST_NAME_PROMPT + "\n\nЧтобы отменить, отправьте /cancel."
SENDING_ONE = "Отправляю трек…"
TRACKS_TOTAL = "В плейлисте всего {count}."
NOT_IN_PLAYLIST = "Этого трека нет в плейлисте."
ALREADY_IN_PLAYLIST = "Этот трек уже есть в плейлисте."
FIRST_TRACK = "Трек уже первый в плейлисте"
LAST_TRACK = "Трек уже последний в плейлисте"
ORDER_UPDATED = "Порядок обновлён"
PLAYLIST_DELETED_SHORT = "Плейлист удалён"
TRACK_ADDED_SHORT = "Добавлено в «{playlist}»"
SENDING = "Отправляю {count}…"
PLAY_FAILED = "Не удалось отправить: {count}."

CREATE_USAGE = (
    "💿 Как создать плейлист: <code>/create_playlist Тренировка</code>\n"
    "Или просто пришлите название следующим сообщением."
)
OPEN_USAGE = (
    "🎼 Как открыть плейлист по названию: <code>/playlist Любимое</code>\n"
    "Точное совпадение необязательно — найду похожий."
)
NAME_NOT_FOUND = "🤷 Плейлист «{name}» не нашёлся. Вот все ваши плейлисты:"
SEVERAL_MATCHES = "🔎 По запросу «{query}» подходит несколько плейлистов — выберите нужный:"
FUZZY_HIT = "🔎 Точного совпадения нет — открываю «{playlist}»."

ADD_TRACK_BUTTON = "➕ Добавить трек"
PICK_HEADER = "➕ <b>Добавить трек в «{playlist}»</b>"
PICK_HINT = "Нажмите ➕ под нужным номером. ✅ — трек уже в плейлисте."
PICK_QUERY_LINE = "🔎 Поиск: «{query}»"
PICK_EMPTY_LIBRARY = (
    "🎧 В библиотеке пока нет треков. Пришлите боту аудиофайл — "
    "и он появится в этом списке."
)
PICK_EMPTY_SEARCH = "🔎 Ничего не нашлось. Попробуйте другой запрос или сбросьте поиск."
PICK_SEARCH_PROMPT = (
    "🔎 Что ищем в библиотеке? Напишите название трека или исполнителя.\n\n"
    "Чтобы вернуться к списку, отправьте /cancel."
)
PICK_SEARCH_BUTTON = "🔎 Поиск"
PICK_CLEAR_BUTTON = "🧹 Сбросить поиск"
PICK_BACK_BUTTON = "⬅️ К плейлисту"
PICK_SEARCH_RESET = "Поиск сброшен"

# --- Состояния FSM -------------------------------------------------------------------


class _PlaylistStatesFallback(StatesGroup):
    """Резервная группа состояний, если backend.bot.states не предоставил свою."""

    waiting_name = State()


class _PlaylistPickStates(StatesGroup):
    """Локальные состояния экрана «➕ Добавить трек»."""

    #: Ожидание поисковой фразы для списка треков библиотеки.
    waiting_track_query = State()


def _named_state(group: Any, names: Sequence[str]) -> State | None:
    """Ищет состояние в группе строго по имени (без «взять первое попавшееся»)."""
    if group is None:
        return None
    for name in names:
        candidate = getattr(group, name, None)
        if isinstance(candidate, State):
            return candidate
    return None


def _pick_state(group: Any, names: Sequence[str]) -> State | None:
    """Находит подходящее состояние в группе по одному из ожидаемых имён."""
    named = _named_state(group, names)
    if named is not None:
        return named
    for candidate in getattr(group, "__states__", ()) or ():
        if isinstance(candidate, State):
            return candidate
    return None


try:  # состояния объявлены в общем модуле, но модуль может отсутствовать
    from backend.bot.states import PlaylistStates as _ExternalPlaylistStates
except ImportError:  # pragma: no cover - зависит от порядка сборки проекта
    _ExternalPlaylistStates = None  # type: ignore[assignment]

WAITING_NAME: State = (
    _pick_state(
        _ExternalPlaylistStates,
        ("waiting_name", "waiting_for_name", "name", "create_name", "creating"),
    )
    or _PlaylistStatesFallback.waiting_name
)

#: Состояние ввода поисковой фразы на экране «➕ Добавить трек».
#: Общий модуль состояний про этот экран может не знать — тогда берём локальное.
WAITING_PICK_QUERY: State = (
    _named_state(
        _ExternalPlaylistStates,
        ("waiting_track_query", "waiting_add_query", "waiting_pick_query"),
    )
    or _PlaylistPickStates.waiting_track_query
)


# --- Вспомогательные функции ---------------------------------------------------------


def _per_page() -> int:
    """Размер страницы списков (из настроек, с разумными границами)."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, 50))


def _short(text: str, limit: int = ANSWER_LIMIT) -> str:
    """Обрезает текст до лимита всплывающего ответа Telegram."""
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _callback_message(callback: CallbackQuery) -> Message | None:
    """Возвращает сообщение колбэка, если оно доступно для редактирования."""
    message = callback.message
    return message if isinstance(message, Message) else None


def _chat_id(callback: CallbackQuery) -> int:
    """Чат для отправки треков: сообщение колбэка или личный чат пользователя."""
    message = _callback_message(callback)
    if message is not None:
        return message.chat.id
    return callback.from_user.id


def _safe_page(total: int, page: int, per_page: int) -> int:
    """Приводит номер страницы к существующему диапазону 1..N."""
    pages = max(1, (max(int(total), 0) + per_page - 1) // per_page)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), pages)


def _playlist_name(playlist: dict | None) -> str:
    """Название плейлиста «как есть» (без экранирования)."""
    if not playlist:
        return "Без названия"
    return str(playlist.get("name") or "Без названия")


def _playlists_text(playlists: Sequence[dict], page_items: Sequence[dict], page: int,
                    total_pages: int) -> str:
    """Текст страницы со списком плейлистов."""
    if not playlists:
        return texts.EMPTY_PLAYLISTS

    start = (max(page, 1) - 1) * _per_page()
    total_label = utils.plural(
        len(playlists), ("плейлист", "плейлиста", "плейлистов")
    )
    header = f"{PLAYLISTS_TITLE}\nВсего: {total_label}"
    lines = [header, ""]
    for offset, playlist in enumerate(page_items, start=start + 1):
        name = utils.escape(playlist.get("name") or "Без названия")
        count = int(playlist.get("track_count") or 0)
        duration = int(playlist.get("total_duration") or 0)
        details = [utils.tracks_count_label(count)]
        if duration > 0:
            details.append(metadata.format_duration(duration))
        line = f"{offset}. <b>{name}</b> — {' · '.join(details)}"
        description = (playlist.get("description") or "").strip()
        if description:
            line = f"{line}\n<i>{utils.escape(description)}</i>"
        lines.append(line)
    if total_pages > 1:
        lines.append("")
        lines.append(texts.PAGE_LABEL.format(page=max(page, 1), total=total_pages))
    return "\n".join(lines)


async def _playlists_view(user_id: int, page: int) -> tuple[str, Any]:
    """Готовит текст и клавиатуру списка плейлистов."""
    playlists = await playlists_repo.list_playlists(user_id)
    per_page = _per_page()
    safe_page = _safe_page(len(playlists), page, per_page)
    page_items, total_pages = utils.paginate(playlists, safe_page, per_page)
    text = _playlists_text(playlists, page_items, safe_page, total_pages)
    markup = keyboards.playlists_kb(page_items, safe_page, total_pages)
    return text, markup


def _with_add_button(
    markup: InlineKeyboardMarkup,
    playlist_id: int,
) -> InlineKeyboardMarkup:
    """Добавляет «➕ Добавить трек» в клавиатуру состава плейлиста.

    `keyboards.playlist_tracks_kb` принадлежит V1 и здесь не меняется — новая
    строка вставляется перед последним рядом («🗑 Удалить плейлист» / «⬅️ К списку»),
    чтобы кнопки навигации оставались внизу.
    """
    rows: list[list[InlineKeyboardButton]] = [
        list(row) for row in (markup.inline_keyboard or [])
    ]
    button = InlineKeyboardButton(
        text=ADD_TRACK_BUTTON,
        callback_data=PlaylistCB(
            action=ACTION_PICK, playlist_id=int(playlist_id), track_id=0, page=1
        ).pack(),
    )
    if rows:
        rows.insert(len(rows) - 1, [button])
    else:  # pragma: no cover - playlist_tracks_kb всегда даёт хотя бы один ряд
        rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _playlist_view(user_id: int, playlist_id: int, page: int) -> tuple[str, Any] | None:
    """Готовит текст и клавиатуру состава плейлиста. None — плейлист не найден."""
    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        return None

    tracks = await playlists_repo.playlist_tracks(user_id, playlist_id)
    per_page = _per_page()
    page = _safe_page(len(tracks), page, per_page)
    page_items, total_pages = utils.paginate(tracks, page, per_page)

    name = utils.escape(playlist.get("name") or "Без названия")
    duration = int(playlist.get("total_duration") or 0)
    header = f"🎼 <b>{name}</b>\n{utils.tracks_count_label(len(tracks))}"
    if duration > 0:
        header = f"{header} · {metadata.format_duration(duration)}"
    description = (playlist.get("description") or "").strip()
    if description:
        header = f"{header}\n<i>{utils.escape(description)}</i>"

    text = utils.render_track_list(
        header, page_items, max(page, 1), total_pages, texts.EMPTY_PLAYLIST
    )
    markup = keyboards.playlist_tracks_kb(page_items, playlist_id, max(page, 1), total_pages)
    return text, _with_add_button(markup, playlist_id)


async def _show_playlists(message: Message, user_id: int, page: int = 1) -> None:
    """Отправляет новым сообщением список плейлистов."""
    text, markup = await _playlists_view(user_id, page)
    await message.answer(text, reply_markup=markup)


async def _edit_playlists(callback: CallbackQuery, user_id: int, page: int) -> None:
    """Перерисовывает список плейлистов в сообщении колбэка."""
    message = _callback_message(callback)
    text, markup = await _playlists_view(user_id, page)
    if message is None:
        await callback.answer()
        return
    await utils.safe_edit(message, text, markup)


async def _edit_playlist(callback: CallbackQuery, user_id: int, playlist_id: int,
                         page: int) -> bool:
    """Перерисовывает состав плейлиста. False — плейлист не найден."""
    view = await _playlist_view(user_id, playlist_id, page)
    if view is None:
        return False
    message = _callback_message(callback)
    if message is not None:
        await utils.safe_edit(message, view[0], view[1])
    return True


async def _send_track(bot: Bot, chat_id: int, track: dict) -> bool:
    """Отправляет трек пользователю. Ошибки логируются, исключения не пробрасываются.

    Отправка идёт через `services.media.send_media_to_user`: аудио уходит как есть
    (исходный `mime_type`, без конвертации), остальные типы — своим методом Bot API.
    """
    try:
        await media.send_media_to_user(bot, chat_id, track)
        return True
    except MusicBoxError as exc:
        logger.warning("Не удалось отправить трек %s: %s", track.get("id"), exc)
        return False
    except TelegramAPIError:
        logger.exception("Ошибка Telegram при отправке трека %s", track.get("id"))
        return False


# --- Поиск плейлиста по названию -----------------------------------------------------


def _fold_name(value: Any) -> str:
    """Нормализует название для сравнения: пробелы, регистр, «ё» -> «е»."""
    return search.normalize_query(str(value or "")).translate(_YO_TRANSLATION)


async def _fuzzy_threshold(user_id: int) -> int:
    """Порог нечёткого совпадения: настройки пользователя, иначе общий из config."""
    raw: Any = None
    try:
        user_settings = await users_repo.get_settings(user_id)
    except Exception:  # настройки не критичны — поиск должен работать всегда
        logger.warning(
            "Не удалось прочитать настройки поиска пользователя %s", user_id, exc_info=True
        )
        user_settings = None
    if user_settings:
        raw = user_settings.get("fuzzy_threshold")
    if raw is None:
        raw = settings.fuzzy_threshold
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Некорректный порог поиска %r у пользователя %s", raw, user_id)
        value = 60
    return max(0, min(100, value))


def _match_playlists(
    playlists: Sequence[dict], query: str, threshold: int
) -> list[dict]:
    """Нечёткий отбор плейлистов по названию (лучшие — первыми).

    Точное совпадение получает 100, вхождение запроса в название — не меньше
    `SUBSTRING_SCORE`, остальное считает `services.search.score` (rapidfuzz
    со сменой раскладки). Результат — копии словарей с полем ``score``.
    """
    normalized = _fold_name(query)
    if not normalized:
        return []

    query_variants = search.variants(query)
    scored: list[tuple[dict, int]] = []
    for playlist in playlists:
        name = str(playlist.get("name") or "")
        if not name:
            continue
        folded = _fold_name(name)
        if folded == normalized:
            value = EXACT_SCORE
        else:
            value = search.score(query_variants, name)
            if normalized in folded:
                value = max(value, SUBSTRING_SCORE)
        if value >= threshold:
            scored.append((playlist, value))

    scored.sort(key=lambda item: (-item[1], (item[0].get("name") or "").casefold()))
    return [dict(playlist, score=value) for playlist, value in scored]


# --- Экран «➕ Добавить трек» ---------------------------------------------------------


async def _pick_context(state: FSMContext) -> tuple[int, str]:
    """Плейлист и поисковая фраза экрана выбора, сохранённые в FSM."""
    data = await state.get_data()
    try:
        playlist_id = int(data.get(PICK_ID_KEY) or 0)
    except (TypeError, ValueError):
        playlist_id = 0
    query = " ".join(str(data.get(PICK_QUERY_KEY) or "").split())
    return playlist_id, query


async def _pick_query_for(state: FSMContext, playlist_id: int) -> str:
    """Поисковая фраза для конкретного плейлиста (для чужой — пустая)."""
    stored_id, query = await _pick_context(state)
    return query if stored_id == int(playlist_id) else ""


async def _clear_pick_state(state: FSMContext) -> None:
    """Сбрасывает ожидание поисковой фразы, не трогая другие диалоги."""
    if await state.get_state() == WAITING_PICK_QUERY.state:
        await state.set_state(None)


async def _pick_page(
    user_id: int, query: str, page: int, per_page: int
) -> tuple[list[dict], int, int]:
    """Страница библиотеки для экрана выбора: (треки, номер страницы, всего страниц)."""
    if query:
        found = await tracks_repo.search_in(
            user_id, query, scope="tracks", limit=PICK_SEARCH_LIMIT
        )
        safe_page = _safe_page(len(found), page, per_page)
        page_items, total_pages = utils.paginate(found, safe_page, per_page)
        return page_items, safe_page, total_pages

    total = await tracks_repo.count_tracks(user_id)
    safe_page = _safe_page(total, page, per_page)
    total_pages = max(1, (max(total, 0) + per_page - 1) // per_page)
    page_items = await tracks_repo.list_tracks(
        user_id,
        order="created_at_desc",
        limit=per_page,
        offset=(safe_page - 1) * per_page,
    )
    return page_items, safe_page, total_pages


def _pick_kb(
    tracks: Sequence[dict],
    *,
    playlist_id: int,
    page: int,
    total_pages: int,
    start_number: int,
    in_playlist: set[int],
    has_query: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура экрана выбора: номера треков, пагинация, поиск, возврат."""
    builder = InlineKeyboardBuilder()
    playlist_id = int(playlist_id)

    for shift, track in enumerate(tracks):
        try:
            track_id = int(track.get("id") or 0)
        except (TypeError, ValueError):
            continue
        mark = "✅" if track_id in in_playlist else "➕"
        builder.button(
            text=f"{mark} {start_number + shift}",
            callback_data=PlaylistCB(
                action=ACTION_PICK_ADD,
                playlist_id=playlist_id,
                track_id=track_id,
                page=page,
            ).pack(),
        )
    if tracks:
        builder.adjust(PICK_BUTTONS_PER_ROW)

    if total_pages > 1:
        current = min(max(int(page), 1), total_pages)
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·",
                callback_data=(
                    PlaylistCB(
                        action=ACTION_PICK,
                        playlist_id=playlist_id,
                        track_id=0,
                        page=current - 1,
                    ).pack()
                    if current > 1
                    else keyboards.NOOP
                ),
            ),
            InlineKeyboardButton(
                text=f"{current}/{total_pages}", callback_data=keyboards.NOOP
            ),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·",
                callback_data=(
                    PlaylistCB(
                        action=ACTION_PICK,
                        playlist_id=playlist_id,
                        track_id=0,
                        page=current + 1,
                    ).pack()
                    if current < total_pages
                    else keyboards.NOOP
                ),
            ),
        )

    search_row = [
        InlineKeyboardButton(
            text=PICK_SEARCH_BUTTON,
            callback_data=PlaylistCB(
                action=ACTION_PICK_FIND, playlist_id=playlist_id, track_id=0, page=page
            ).pack(),
        )
    ]
    if has_query:
        search_row.append(
            InlineKeyboardButton(
                text=PICK_CLEAR_BUTTON,
                callback_data=PlaylistCB(
                    action=ACTION_PICK_CLEAR,
                    playlist_id=playlist_id,
                    track_id=0,
                    page=1,
                ).pack(),
            )
        )
    builder.row(*search_row)

    builder.row(
        InlineKeyboardButton(
            text=PICK_BACK_BUTTON,
            callback_data=PlaylistCB(
                action="open", playlist_id=playlist_id, track_id=0, page=1
            ).pack(),
        )
    )
    return builder.as_markup()


async def _pick_view(
    user_id: int, playlist: dict, query: str, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана «➕ Добавить трек»."""
    playlist_id = int(playlist["id"])
    per_page = _per_page()
    page_items, safe_page, total_pages = await _pick_page(user_id, query, page, per_page)

    playlist_track_ids = {
        int(track["id"])
        for track in await playlists_repo.playlist_tracks(user_id, playlist_id)
        if track.get("id") is not None
    }

    header = PICK_HEADER.format(playlist=utils.escape(_playlist_name(playlist)))
    if query:
        header = f"{header}\n{PICK_QUERY_LINE.format(query=utils.escape(query))}"
    header = f"{header}\n<i>{PICK_HINT}</i>"

    text = utils.render_track_list(
        header,
        page_items,
        safe_page,
        total_pages,
        PICK_EMPTY_SEARCH if query else PICK_EMPTY_LIBRARY,
        per_page=per_page,
    )
    markup = _pick_kb(
        page_items,
        playlist_id=playlist_id,
        page=safe_page,
        total_pages=total_pages,
        start_number=utils.page_offset(safe_page, per_page) + 1,
        in_playlist=playlist_track_ids,
        has_query=bool(query),
    )
    return text, markup


async def _show_pick(
    callback: CallbackQuery,
    user_id: int,
    playlist: dict,
    query: str,
    page: int,
) -> None:
    """Перерисовывает экран выбора трека в сообщении колбэка."""
    text, markup = await _pick_view(user_id, playlist, query, page)
    message = _callback_message(callback)
    if message is None:
        logger.debug("Сообщение колбэка недоступно — экран выбора не перерисован")
        return
    await utils.safe_edit(message, text, markup)


# --- Команда /playlists --------------------------------------------------------------


@router.message(Command("playlists"))
async def cmd_playlists(message: Message, state: FSMContext) -> None:
    """Показывает список плейлистов пользователя."""
    if message.from_user is None:
        return
    await state.set_state(None)
    await _show_playlists(message, message.from_user.id, 1)


# --- Команда /create_playlist --------------------------------------------------------


@router.message(Command("create_playlist"))
async def cmd_create_playlist(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """Создаёт плейлист: сразу по аргументу команды либо через диалог ввода названия."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    name = " ".join((command.args or "").split())

    await state.set_state(None)
    await state.update_data(playlist_pending_track_id=0, playlist_return_page=1)

    if name:
        if await _create_and_open(message, state, user_id, name, 0):
            return
        # Название не подошло (пустое или занятое) — продолжаем диалогом.

    await state.set_state(WAITING_NAME)
    if not name:
        await message.answer(CREATE_USAGE)
    await message.answer(ASK_NAME)


# --- Команда /playlist <название> ----------------------------------------------------


@router.message(Command("playlist"))
async def cmd_playlist(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """Открывает плейлист по названию; при отсутствии точного совпадения — похожий."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    query = " ".join((command.args or "").split())

    await state.set_state(None)

    playlists = await playlists_repo.list_playlists(user_id)
    if not playlists:
        await message.answer(CREATE_USAGE)
        await _show_playlists(message, user_id, 1)
        return

    if not query:
        await message.answer(OPEN_USAGE)
        await _show_playlists(message, user_id, 1)
        return

    threshold = await _fuzzy_threshold(user_id)
    matches = _match_playlists(playlists, query, threshold)

    if not matches:
        logger.info("Плейлист по запросу %r у пользователя %s не найден", query, user_id)
        await message.answer(NAME_NOT_FOUND.format(name=utils.escape(query)))
        await _show_playlists(message, user_id, 1)
        return

    best = matches[0]
    exact = int(best.get("score") or 0) >= EXACT_SCORE
    if exact or len(matches) == 1:
        if not exact:
            await message.answer(
                FUZZY_HIT.format(playlist=utils.escape(_playlist_name(best)))
            )
        view = await _playlist_view(user_id, int(best["id"]), 1)
        if view is None:
            await message.answer(texts.PLAYLIST_NOT_FOUND)
            await _show_playlists(message, user_id, 1)
            return
        await message.answer(view[0], reply_markup=view[1])
        return

    shown = matches[:MAX_NAME_MATCHES]
    await message.answer(
        SEVERAL_MATCHES.format(query=utils.escape(query)),
        reply_markup=keyboards.playlists_kb(shown, 1, 1),
    )


# --- Навигация по спискам ------------------------------------------------------------


@router.callback_query(PlaylistCB.filter(F.action.in_({"list", "back"})))
async def cb_playlists_list(callback: CallbackQuery, callback_data: PlaylistCB,
                            state: FSMContext) -> None:
    """Возврат к списку плейлистов."""
    await _clear_pick_state(state)
    await _edit_playlists(callback, callback.from_user.id, max(callback_data.page, 1))
    await callback.answer()


@router.callback_query(PlaylistCB.filter(F.action == "page"))
async def cb_playlists_page(callback: CallbackQuery, callback_data: PlaylistCB) -> None:
    """Пагинация: список плейлистов либо треки внутри плейлиста."""
    user_id = callback.from_user.id
    page = max(callback_data.page, 1)
    if callback_data.playlist_id > 0:
        if not await _edit_playlist(callback, user_id, callback_data.playlist_id, page):
            await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
            return
    else:
        await _edit_playlists(callback, user_id, page)
    await callback.answer()


@router.callback_query(PlaylistCB.filter(F.action == "open"))
async def cb_playlist_open(callback: CallbackQuery, callback_data: PlaylistCB,
                           state: FSMContext) -> None:
    """Открывает состав плейлиста."""
    await _clear_pick_state(state)
    ok = await _edit_playlist(
        callback,
        callback.from_user.id,
        callback_data.playlist_id,
        max(callback_data.page, 1),
    )
    if not ok:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        return
    await callback.answer()


# --- Создание плейлиста (FSM) --------------------------------------------------------


async def _create_and_open(
    message: Message, state: FSMContext, user_id: int, name: str, pending_track_id: int
) -> bool:
    """Создаёт плейлист и показывает его карточку.

    Возвращает False, если название не подошло (пустое или занятое) — в этом
    случае пользователю уже отправлена подсказка и диалог ввода нужно продолжить.
    При любом другом исходе диалог закрывается (состояние сбрасывается здесь же).
    """
    if not name:
        await message.answer(texts.NAME_EMPTY)
        return False

    try:
        playlist = await playlists_repo.create_playlist(user_id, name)
    except ValidationError as exc:
        await message.answer(
            f"⚠️ {utils.escape(str(exc))}\n\nПришлите другое название или /cancel."
        )
        return False
    except MusicBoxError:
        logger.exception("Не удалось создать плейлист для пользователя %s", user_id)
        await state.set_state(None)
        await state.update_data(playlist_pending_track_id=0)
        await message.answer(texts.ERROR_GENERIC)
        return True

    await state.set_state(None)
    await state.update_data(playlist_pending_track_id=0)

    playlist_id = int(playlist["id"])
    created_text = texts.PLAYLIST_CREATED.format(playlist=utils.escape(playlist["name"]))

    if pending_track_id > 0:
        added = await playlists_repo.add_track(user_id, playlist_id, pending_track_id)
        if added:
            created_text = (
                f"{created_text}\n"
                + texts.PLAYLIST_TRACK_ADDED.format(
                    playlist=utils.escape(playlist["name"])
                )
            )
        else:
            created_text = f"{created_text}\n{texts.ERROR_TRY_AGAIN}"

    await message.answer(created_text)

    view = await _playlist_view(user_id, playlist_id, 1)
    if view is not None:
        await message.answer(view[0], reply_markup=view[1])
    logger.info("Пользователь %s создал плейлист %s", user_id, playlist_id)
    return True


@router.callback_query(PlaylistCB.filter(F.action == "create"))
async def cb_playlist_create(callback: CallbackQuery, callback_data: PlaylistCB,
                             state: FSMContext) -> None:
    """Запрашивает название нового плейлиста."""
    await state.set_state(WAITING_NAME)
    await state.update_data(
        playlist_pending_track_id=int(callback_data.track_id or 0),
        playlist_return_page=max(callback_data.page, 1),
    )
    message = _callback_message(callback)
    if message is not None:
        await message.answer(ASK_NAME)
    await callback.answer()


@router.message(StateFilter(WAITING_NAME), Command("cancel"))
async def cancel_create(message: Message, state: FSMContext) -> None:
    """Отменяет создание плейлиста."""
    await state.set_state(None)
    await state.update_data(playlist_pending_track_id=0)
    await message.answer(texts.CANCELLED)
    if message.from_user is not None:
        await _show_playlists(message, message.from_user.id, 1)


@router.message(StateFilter(WAITING_NAME), F.text)
async def create_playlist_name(message: Message, state: FSMContext) -> None:
    """Создаёт плейлист по присланному названию."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    name = (message.text or "").strip()
    if not name:
        await message.answer(texts.NAME_EMPTY)
        return
    if name.lower() in {"отмена", "/cancel", "cancel"}:
        await cancel_create(message, state)
        return

    data = await state.get_data()
    pending_track_id = int(data.get("playlist_pending_track_id") or 0)

    await _create_and_open(message, state, user_id, name, pending_track_id)


# --- Удаление плейлиста --------------------------------------------------------------


@router.callback_query(PlaylistCB.filter(F.action == "delete"))
async def cb_playlist_delete(callback: CallbackQuery, callback_data: PlaylistCB) -> None:
    """Удаляет плейлист: сначала спрашивает подтверждение, потом удаляет."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    page = max(callback_data.page, 1)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        await _edit_playlists(callback, user_id, page)
        return

    name = utils.escape(playlist.get("name") or "Без названия")

    if int(callback_data.track_id or 0) != CONFIRM_FLAG:
        message = _callback_message(callback)
        if message is not None:
            confirm_text = (
                texts.CONFIRM_DELETE_PLAYLIST.format(playlist=name)
                + "\n"
                + TRACKS_TOTAL.format(
                    count=utils.tracks_count_label(int(playlist.get("track_count") or 0))
                )
            )
            markup = keyboards.confirm_kb(
                PlaylistCB(
                    action="delete",
                    playlist_id=playlist_id,
                    track_id=CONFIRM_FLAG,
                    page=page,
                ).pack(),
                PlaylistCB(
                    action="open", playlist_id=playlist_id, track_id=0, page=page
                ).pack(),
            )
            await utils.safe_edit(message, confirm_text, markup)
        await callback.answer()
        return

    deleted = await playlists_repo.delete_playlist(user_id, playlist_id)
    await callback.answer(
        PLAYLIST_DELETED_SHORT if deleted else texts.PLAYLIST_NOT_FOUND
    )
    await _edit_playlists(callback, user_id, page)


# --- Состав плейлиста ----------------------------------------------------------------


@router.callback_query(PlaylistCB.filter(F.action == "add"))
async def cb_playlist_add_track(callback: CallbackQuery, callback_data: PlaylistCB) -> None:
    """Добавляет выбранный трек в плейлист (кнопка из playlist_pick_kb)."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    track_id = int(callback_data.track_id)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        return

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await callback.answer(texts.TRACK_NOT_FOUND, show_alert=True)
        return

    name = playlist.get("name") or "Без названия"
    added = await playlists_repo.add_track(user_id, playlist_id, track_id)
    if added:
        answer_text = _short(TRACK_ADDED_SHORT.format(playlist=name))
        result_text = texts.PLAYLIST_TRACK_ADDED.format(playlist=utils.escape(name))
    else:
        answer_text = ALREADY_IN_PLAYLIST
        result_text = texts.PLAYLIST_TRACK_EXISTS.format(playlist=utils.escape(name))

    await callback.answer(answer_text)
    message = _callback_message(callback)
    if message is not None:
        markup = keyboards.playlists_kb([playlist], 1, 1)
        await utils.safe_edit(message, result_text, markup)


@router.callback_query(PlaylistCB.filter(F.action == "remove"))
async def cb_playlist_remove_track(callback: CallbackQuery, callback_data: PlaylistCB) -> None:
    """Убирает трек из плейлиста."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    track_id = int(callback_data.track_id)
    page = max(callback_data.page, 1)

    removed = await playlists_repo.remove_track(user_id, playlist_id, track_id)
    if not removed:
        await callback.answer(NOT_IN_PLAYLIST, show_alert=True)
    else:
        await callback.answer(texts.PLAYLIST_TRACK_REMOVED)

    if not await _edit_playlist(callback, user_id, playlist_id, page):
        await _edit_playlists(callback, user_id, 1)


@router.callback_query(PlaylistCB.filter(F.action.in_({"up", "down"})))
async def cb_playlist_move_track(callback: CallbackQuery, callback_data: PlaylistCB) -> None:
    """Перемещает трек вверх или вниз по плейлисту."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    track_id = int(callback_data.track_id)
    page = max(callback_data.page, 1)

    tracks = await playlists_repo.playlist_tracks(user_id, playlist_id)
    if not tracks:
        await callback.answer(texts.NOTHING_TO_PLAY, show_alert=True)
        await _edit_playlist(callback, user_id, playlist_id, page)
        return

    positions = {
        int(track["id"]): int(track.get("position") or index)
        for index, track in enumerate(tracks, start=1)
    }
    current = positions.get(track_id)
    if current is None:
        await callback.answer(NOT_IN_PLAYLIST, show_alert=True)
        await _edit_playlist(callback, user_id, playlist_id, page)
        return

    new_position = current - 1 if callback_data.action == "up" else current + 1
    if new_position < 1:
        await callback.answer(FIRST_TRACK)
        return
    if new_position > len(tracks):
        await callback.answer(LAST_TRACK)
        return

    moved = await playlists_repo.move_track_position(
        user_id, playlist_id, track_id, new_position
    )
    if not moved:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    # Страница может смениться, если трек «переехал» через её границу.
    per_page = _per_page()
    target_page = (new_position - 1) // per_page + 1
    await _edit_playlist(callback, user_id, playlist_id, target_page)
    await callback.answer(ORDER_UPDATED)


# --- Экран «➕ Добавить трек» (ТЗ п. 20) ---------------------------------------------


@router.callback_query(PlaylistCB.filter(F.action == ACTION_PICK))
async def cb_pick_open(callback: CallbackQuery, callback_data: PlaylistCB,
                       state: FSMContext) -> None:
    """Открывает список треков библиотеки для добавления в текущий плейлист."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    page = max(callback_data.page, 1)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        await _edit_playlists(callback, user_id, 1)
        return

    query = await _pick_query_for(state, playlist_id)
    await _clear_pick_state(state)
    await state.update_data(**{PICK_ID_KEY: playlist_id, PICK_QUERY_KEY: query})

    await callback.answer()
    await _show_pick(callback, user_id, playlist, query, page)


@router.callback_query(PlaylistCB.filter(F.action == ACTION_PICK_ADD))
async def cb_pick_add(callback: CallbackQuery, callback_data: PlaylistCB,
                      state: FSMContext) -> None:
    """Добавляет выбранный трек в плейлист и остаётся на экране выбора."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    track_id = int(callback_data.track_id)
    page = max(callback_data.page, 1)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        await _edit_playlists(callback, user_id, 1)
        return

    query = await _pick_query_for(state, playlist_id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await callback.answer(texts.TRACK_NOT_FOUND, show_alert=True)
        await _show_pick(callback, user_id, playlist, query, page)
        return

    name = _playlist_name(playlist)
    try:
        added = await playlists_repo.add_track(user_id, playlist_id, track_id)
    except MusicBoxError:
        logger.exception(
            "Не удалось добавить трек %s в плейлист %s", track_id, playlist_id
        )
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    if added:
        logger.info(
            "Трек %s добавлен в плейлист %s пользователя %s", track_id, playlist_id, user_id
        )
        await callback.answer(_short(TRACK_ADDED_SHORT.format(playlist=name)))
    else:
        await callback.answer(ALREADY_IN_PLAYLIST)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id) or playlist
    await _show_pick(callback, user_id, playlist, query, page)


@router.callback_query(PlaylistCB.filter(F.action == ACTION_PICK_FIND))
async def cb_pick_find(callback: CallbackQuery, callback_data: PlaylistCB,
                       state: FSMContext) -> None:
    """Спрашивает поисковую фразу для списка треков библиотеки."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        await _edit_playlists(callback, user_id, 1)
        return

    await state.set_state(WAITING_PICK_QUERY)
    await state.update_data(**{PICK_ID_KEY: playlist_id})

    await callback.answer()
    message = _callback_message(callback)
    if message is not None:
        await message.answer(PICK_SEARCH_PROMPT)


@router.callback_query(PlaylistCB.filter(F.action == ACTION_PICK_CLEAR))
async def cb_pick_clear(callback: CallbackQuery, callback_data: PlaylistCB,
                        state: FSMContext) -> None:
    """Сбрасывает поиск на экране выбора трека."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        await _edit_playlists(callback, user_id, 1)
        return

    await _clear_pick_state(state)
    await state.update_data(**{PICK_ID_KEY: playlist_id, PICK_QUERY_KEY: ""})
    await callback.answer(PICK_SEARCH_RESET)
    await _show_pick(callback, user_id, playlist, "", 1)


@router.message(StateFilter(WAITING_PICK_QUERY), Command("cancel"))
async def cancel_pick_search(message: Message, state: FSMContext) -> None:
    """Отменяет ввод поисковой фразы и возвращает список треков без фильтра."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    playlist_id, _query = await _pick_context(state)

    await state.set_state(None)
    await message.answer(texts.CANCELLED)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await _show_playlists(message, user_id, 1)
        return
    text, markup = await _pick_view(user_id, playlist, "", 1)
    await message.answer(text, reply_markup=markup)


@router.message(StateFilter(WAITING_PICK_QUERY), F.text)
async def pick_search_query(message: Message, state: FSMContext) -> None:
    """Применяет поисковую фразу к списку треков библиотеки."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    query = " ".join((message.text or "").split())
    if query.lower() in {"отмена", "/cancel", "cancel"}:
        await cancel_pick_search(message, state)
        return

    playlist_id, _stored = await _pick_context(state)
    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await state.set_state(None)
        await message.answer(texts.PLAYLIST_NOT_FOUND)
        await _show_playlists(message, user_id, 1)
        return

    if not query:
        await message.answer(texts.EMPTY_QUERY)
        return

    await state.set_state(None)
    await state.update_data(**{PICK_ID_KEY: playlist_id, PICK_QUERY_KEY: query})

    text, markup = await _pick_view(user_id, playlist, query, 1)
    await message.answer(text, reply_markup=markup)
    logger.debug(
        "Поиск «%s» на экране добавления в плейлист %s (пользователь %s)",
        query,
        playlist_id,
        user_id,
    )


# --- Воспроизведение -----------------------------------------------------------------


@router.callback_query(PlaylistCB.filter(F.action == "play"))
async def cb_playlist_play(callback: CallbackQuery, callback_data: PlaylistCB,
                           bot: Bot) -> None:
    """Отправляет один трек плейлиста или весь плейлист (до 10 треков за раз)."""
    user_id = callback.from_user.id
    playlist_id = int(callback_data.playlist_id)
    track_id = int(callback_data.track_id or 0)
    page = max(callback_data.page, 1)
    chat_id = _chat_id(callback)

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await callback.answer(texts.PLAYLIST_NOT_FOUND, show_alert=True)
        return

    tracks = await playlists_repo.playlist_tracks(user_id, playlist_id)
    if not tracks:
        await callback.answer(texts.NOTHING_TO_PLAY, show_alert=True)
        return

    if track_id > 0:
        track = next((item for item in tracks if int(item["id"]) == track_id), None)
        if track is None:
            await callback.answer(NOT_IN_PLAYLIST, show_alert=True)
            return
        await callback.answer(SENDING_ONE)
        if not await _send_track(bot, chat_id, track):
            await bot.send_message(chat_id, texts.ERROR_TRY_AGAIN)
            return
        await tracks_repo.register_play(user_id, track_id, source="bot_playlist")
        return

    per_page = _per_page()
    page_items, _total_pages = utils.paginate(
        tracks, _safe_page(len(tracks), page, per_page), per_page
    )
    batch = list(page_items or tracks)[:MAX_PLAY_BATCH]
    if not batch:
        batch = tracks[:MAX_PLAY_BATCH]

    name = utils.escape(playlist.get("name") or "Без названия")
    await callback.answer(
        _short(SENDING.format(count=utils.tracks_count_label(len(batch))))
    )

    sent = 0
    failed = 0
    for track in batch:
        if await _send_track(bot, chat_id, track):
            sent += 1
            await tracks_repo.register_play(
                user_id, int(track["id"]), source="bot_playlist"
            )
        else:
            failed += 1
        # Небольшая пауза, чтобы Telegram не ограничил частоту отправки.
        await asyncio.sleep(PLAY_DELAY)

    summary = texts.PLAYLIST_PLAY_HEADER.format(
        playlist=name, count=utils.tracks_count_label(sent)
    )
    if failed:
        summary = f"{summary}\n{PLAY_FAILED.format(count=failed)}"
    if len(tracks) > len(batch):
        summary = (
            f"{summary}\n"
            + TRACKS_TOTAL.format(count=utils.tracks_count_label(len(tracks)))
            + " "
            + texts.PLAYLIST_PLAY_LIMIT.format(limit=len(batch))
        )
    await bot.send_message(chat_id, summary)
    logger.info(
        "Плейлист %s пользователя %s: отправлено %s из %s треков",
        playlist_id,
        user_id,
        sent,
        len(batch),
    )


__all__ = ["router"]
