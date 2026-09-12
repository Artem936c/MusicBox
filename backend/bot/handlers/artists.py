"""Хендлеры списка исполнителей.

Команда ``/artists`` и колбэки :class:`ArtistCB`: алфавитный список исполнителей
с отметкой «✅/⬜ прослушано» и пагинацией, переключение отметки и список треков
конкретного исполнителя (контекст ``art:<id>``).

V2 добавляет (ТЗ п. 2, 8, 12, 19):

* ``/artists_search`` — нечёткий поиск по исполнителям;
* ``/create_artist`` — ручное создание исполнителя (FSM);
* привязку исполнителя к папке прямо из его карточки;
* экран «непрослушанные треки исполнителя».

Собственные экраны используют ЛОКАЛЬНЫЕ callback-префиксы (``artmark``,
``artfld``, ``artset``, ``artunp``): фабрика :class:`ArtistCB` из V1 несёт всего
три поля, а этим экранам нужна пара «исполнитель + папка/страница». Префиксы
намеренно длиннее ``art``, поэтому ``ArtistCB.filter()`` их не разбирает.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import ArtistCB
from backend.bot.keyboards import artists_kb, tracks_page_kb
from backend.bot.utils import (
    ack,
    artist_context,
    escape,
    page_offset,
    paginate,
    render_track_list,
    safe_edit,
    tracks_count_label,
)
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, ValidationError
from backend.services import search as search_service

logger = logging.getLogger(__name__)

router = Router(name="artists")

#: Собственные callback-данные модуля: отметка «прослушано» из карточки исполнителя.
#: Формат ``artmark:<artist_id>:<page>``; префикс намеренно отличается от «art»,
#: чтобы данные не разбирались фабрикой ArtistCB.
CARD_LISTENED_PREFIX = "artmark"
#: ``artfld:<artist_id>:<page>`` — экран выбора папки для исполнителя (ТЗ п. 8).
FOLDER_PICK_PREFIX = "artfld"
#: ``artset:<artist_id>:<folder_id>`` — привязка исполнителя к папке.
#: ``folder_id == FOLDER_DETACH`` означает «отвязать от папки».
FOLDER_SET_PREFIX = "artset"
#: ``artunp:<artist_id>:<page>`` — непрослушанные треки исполнителя (ТЗ п. 2).
UNPLAYED_PREFIX = "artunp"
#: Специальное значение folder_id: «без папки».
FOLDER_DETACH = -1

#: Сколько исполнителей показываем в выдаче поиска.
SEARCH_LIMIT = 30
#: Сколько непрослушанных треков забираем из репозитория за раз.
UNPLAYED_LIMIT = 200
#: Ограничение Telegram на длину текста сообщения.
MESSAGE_LIMIT = 4096

#: Заголовок списка исполнителей.
ARTISTS_TITLE = "🎤 <b>Исполнители</b>"

#: Текст для исполнителя без треков.
ARTIST_EMPTY = "🎧 У этого исполнителя пока нет треков в библиотеке."

# --- Пользовательские тексты своих экранов (RU) --------------------------------------

SEARCH_PROMPT = (
    "🎤 Кого ищем? Напишите имя исполнителя — я прощаю опечатки "
    "и неправильную раскладку.\n\n"
    "Чтобы отменить, отправьте /cancel."
)
SEARCH_HEADER = "🎤 <b>Исполнители по запросу «{query}»</b>"
SEARCH_EMPTY = (
    "🤷 Такого исполнителя не нашлось.\n"
    "Попробуйте короче или другими словами."
)
CREATE_PROMPT = (
    "🎤 Введите имя нового исполнителя (до 200 символов).\n\n"
    "Чтобы отменить, отправьте /cancel."
)
ARTIST_CREATED = "🎤 Исполнитель «{name}» создан."
ARTIST_EXISTS = "🎤 Исполнитель «{name}» уже есть в библиотеке."
ARTIST_CREATE_FAILED = "😔 Не удалось создать исполнителя. Попробуйте другое имя."
FOLDER_PICK_TITLE = "📁 <b>Папка исполнителя «{name}»</b>"
FOLDER_PICK_HINT = (
    "Выберите папку — новые треки этого исполнителя будут попадать в неё "
    "при автосортировке."
)
FOLDER_CURRENT = "Сейчас: <b>{path}</b>"
FOLDER_CURRENT_NONE = "Сейчас: <i>без папки</i>"
FOLDER_PICK_EMPTY = (
    "📁 Папок пока нет — создайте первую командой /folders."
)
FOLDER_ATTACHED = "📁 Исполнитель привязан к папке «{folder}»."
FOLDER_DETACHED = "🚫 Исполнитель отвязан от папки."
UNPLAYED_TITLE = "💤 <b>Непрослушанное у «{name}»</b>"
UNPLAYED_EMPTY = (
    "💤 У этого исполнителя всё прослушано — отличная работа!"
)
BUTTON_UNPLAYED = "💤 Непрослушанные"
BUTTON_FOLDER = "📁 Папка"
BUTTON_BACK_TO_ARTIST = "⬅️ К исполнителю"
BUTTON_NO_FOLDER = "🚫 Без папки"


# ---------------------------------------------------------------------------
# Состояния FSM (объявлены локально — см. правила V2)
# ---------------------------------------------------------------------------


class ArtistManageStates(StatesGroup):
    """Диалоги раздела «Исполнители»."""

    #: Ожидание запроса для `/artists_search`.
    waiting_search_query = State()
    #: Ожидание имени нового исполнителя для `/create_artist`.
    waiting_new_name = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _page_size() -> int:
    """Размер страницы из конфигурации (не меньше 1)."""
    try:
        size = int(settings.page_size)
    except (TypeError, ValueError):
        size = 0
    return size if size > 0 else 10


def _total_pages(total: int, per_page: int) -> int:
    """Количество страниц (минимум 1)."""
    if total <= 0:
        return 1
    return max(1, math.ceil(total / per_page))


def _clamp_page(page: int, total_pages: int) -> int:
    """Приводит номер страницы к допустимому диапазону."""
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), max(1, int(total_pages)))


def _trim(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Подрезает сообщение под лимит Telegram, не разрывая HTML-тег."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    opened = cut.rfind("<")
    if opened > cut.rfind(">"):
        cut = cut[:opened]
    logger.debug("Сообщение раздела «Исполнители» подрезано до %s символов", limit)
    return cut.rstrip() + "…"


def _int(value: Any, default: int = 0) -> int:
    """Мягкое приведение к int (данные приходят из БД и callback-строк)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short(value: Any, limit: int = 28) -> str:
    """Короткая подпись кнопки (обычный текст, без HTML)."""
    text = " ".join(str(value or "").split())
    if not text:
        return "Без названия"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _prefix_filter(prefix: str) -> Any:
    """Фильтр собственных callback-данных модуля по префиксу."""

    def _check(data: Any) -> bool:
        return isinstance(data, str) and data.startswith(f"{prefix}:")

    return _check


def _parse_pair(data: str, prefix: str) -> tuple[int, int] | None:
    """Разбирает ``<prefix>:<a>:<b>`` в пару чисел."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


_is_card_listened = _prefix_filter(CARD_LISTENED_PREFIX)
_is_folder_pick = _prefix_filter(FOLDER_PICK_PREFIX)
_is_folder_set = _prefix_filter(FOLDER_SET_PREFIX)
_is_unplayed = _prefix_filter(UNPLAYED_PREFIX)


def _parse_card_listened(data: str) -> tuple[int, int] | None:
    """Разбирает ``artmark:<artist_id>:<page>`` в пару чисел."""
    return _parse_pair(data, CARD_LISTENED_PREFIX)


async def _show(callback: CallbackQuery, bot: Bot, text: str, markup: Any = None) -> None:
    """Показывает экран: правит текущее сообщение либо отправляет новое."""
    message = callback.message
    if message is not None:
        await safe_edit(message, text, markup)
        return
    if callback.from_user is not None:
        await bot.send_message(callback.from_user.id, text, reply_markup=markup)


def _listened_notice(artist: dict[str, Any]) -> str:
    """Короткий ответ на нажатие отметки «прослушано»."""
    return texts.ARTIST_LISTENED_ON if artist.get("is_listened") else texts.ARTIST_LISTENED_OFF


async def _folder_label(user_id: int, folder_id: Any) -> str | None:
    """Путь до папки исполнителя («Рок / Кино») или None, если папки нет."""
    target = _int(folder_id, 0)
    if target <= 0:
        return None
    try:
        path = await folders_repo.folder_path(user_id, target)
    except MusicBoxError as exc:
        logger.warning("Не удалось получить путь папки id=%s: %s", target, exc)
        return None
    if not path:
        return None
    return " / ".join(str(item.get("name") or "") for item in path)


# ---------------------------------------------------------------------------
# Клавиатуры своих экранов (объявлены локально — см. правила V2)
# ---------------------------------------------------------------------------


def artist_search_kb(artists: Sequence[dict]) -> InlineKeyboardMarkup:
    """Кнопки перехода к найденным исполнителям."""
    builder = InlineKeyboardBuilder()
    for artist in artists:
        artist_id = _int(artist.get("id"))
        if artist_id <= 0:
            continue
        mark = "✅" if artist.get("is_listened") else "⬜"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} 🎤 {_short(artist.get('name'))}",
                callback_data=ArtistCB(action="open", artist_id=artist_id, page=1).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Все исполнители",
            callback_data=ArtistCB(action="list", artist_id=0, page=1).pack(),
        )
    )
    return builder.as_markup()


def artist_created_kb(artist_id: int) -> InlineKeyboardMarkup:
    """Что можно сделать сразу после создания исполнителя."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=BUTTON_FOLDER,
            callback_data=f"{FOLDER_PICK_PREFIX}:{_int(artist_id)}:1",
        ),
        InlineKeyboardButton(
            text="🎤 Открыть",
            callback_data=ArtistCB(
                action="open", artist_id=_int(artist_id), page=1
            ).pack(),
        ),
    )
    return builder.as_markup()


def artist_folder_pick_kb(
    artist_id: int,
    folders: Sequence[dict],
    page: int,
    total_pages: int,
    *,
    has_folder: bool,
) -> InlineKeyboardMarkup:
    """Выбор папки для исполнителя: список папок, «без папки» и пагинация."""
    builder = InlineKeyboardBuilder()
    target = _int(artist_id)

    for folder in folders:
        folder_id = _int(folder.get("id"))
        if folder_id <= 0:
            continue
        indent = "· " * min(_int(folder.get("depth")), 4)
        count = _int(folder.get("total_track_count"))
        label = f"📁 {indent}{_short(folder.get('name'))}"
        if count:
            label = f"{label} · {count}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{FOLDER_SET_PREFIX}:{target}:{folder_id}",
            )
        )

    if total_pages > 1:
        current = _clamp_page(page, total_pages)
        prev_data = (
            f"{FOLDER_PICK_PREFIX}:{target}:{current - 1}"
            if current > 1
            else f"{FOLDER_PICK_PREFIX}:{target}:{current}"
        )
        next_data = (
            f"{FOLDER_PICK_PREFIX}:{target}:{current + 1}"
            if current < total_pages
            else f"{FOLDER_PICK_PREFIX}:{target}:{current}"
        )
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·", callback_data=prev_data
            ),
            InlineKeyboardButton(
                text=f"{current}/{total_pages}",
                callback_data=f"{FOLDER_PICK_PREFIX}:{target}:{current}",
            ),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·", callback_data=next_data
            ),
        )

    if has_folder:
        builder.row(
            InlineKeyboardButton(
                text=BUTTON_NO_FOLDER,
                callback_data=f"{FOLDER_SET_PREFIX}:{target}:{FOLDER_DETACH}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=BUTTON_BACK_TO_ARTIST,
            callback_data=ArtistCB(action="open", artist_id=target, page=1).pack(),
        )
    )
    return builder.as_markup()


def _unplayed_extra_rows(
    artist_id: int, page: int, total_pages: int
) -> list[list[InlineKeyboardButton]]:
    """Пагинация и возврат для экрана непрослушанных треков."""
    target = _int(artist_id)
    rows: list[list[InlineKeyboardButton]] = []

    if total_pages > 1:
        current = _clamp_page(page, total_pages)
        prev_data = (
            f"{UNPLAYED_PREFIX}:{target}:{current - 1}"
            if current > 1
            else f"{UNPLAYED_PREFIX}:{target}:{current}"
        )
        next_data = (
            f"{UNPLAYED_PREFIX}:{target}:{current + 1}"
            if current < total_pages
            else f"{UNPLAYED_PREFIX}:{target}:{current}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="◀️" if current > 1 else "·", callback_data=prev_data
                ),
                InlineKeyboardButton(
                    text=f"{current}/{total_pages}",
                    callback_data=f"{UNPLAYED_PREFIX}:{target}:{current}",
                ),
                InlineKeyboardButton(
                    text="▶️" if current < total_pages else "·", callback_data=next_data
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=BUTTON_BACK_TO_ARTIST,
                callback_data=ArtistCB(action="open", artist_id=target, page=1).pack(),
            )
        ]
    )
    return rows


# ---------------------------------------------------------------------------
# Рендер экранов
# ---------------------------------------------------------------------------


async def _render_artists(user_id: int, page: int) -> tuple[str, Any]:
    """Собирает текст и клавиатуру алфавитного списка исполнителей."""
    artists = await artists_repo.list_artists(user_id)
    if not artists:
        return f"{ARTISTS_TITLE}\n\n{texts.EMPTY_ARTISTS}", artists_kb([], 1, 1)

    per_page = _page_size()
    items, total_pages = paginate(artists, page, per_page)
    current = _clamp_page(page, total_pages)

    listened = sum(1 for artist in artists if artist.get("is_listened"))
    lines = [
        f"{ARTISTS_TITLE} — {len(artists)} шт. · прослушано: {listened}",
        "",
    ]
    start = page_offset(current, per_page) + 1
    for index, artist in enumerate(items, start=start):
        mark = "✅" if artist.get("is_listened") else "⬜"
        name = escape(artist.get("name") or "Без названия")
        count = tracks_count_label(int(artist.get("track_count") or 0))
        line = f"{index}. {mark} <b>{name}</b> — {count}"
        plays = int(artist.get("play_count") or 0)
        if plays:
            line += f" · ▶️{plays}"
        lines.append(line)
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    return _trim("\n".join(lines)), artists_kb(items, current, total_pages)


async def _render_artist(user_id: int, artist_id: int, page: int) -> tuple[str, Any] | None:
    """Собирает карточку исполнителя со списком треков; None — исполнитель не найден."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        return None

    per_page = _page_size()
    total = await tracks_repo.count_tracks(user_id, artist_id=artist_id)
    total_pages = _total_pages(total, per_page)
    current = _clamp_page(page, total_pages)
    tracks = await tracks_repo.list_tracks(
        user_id,
        artist_id=artist_id,
        limit=per_page,
        offset=page_offset(current, per_page),
    )

    mark = "✅" if artist.get("is_listened") else "⬜"
    # Имя исполнителя задаёт пользователь: «<» и «&» экранируем сами, иначе
    # заголовок пришлось бы чинить санитайзеру render_track_list.
    name = escape(artist.get("name") or "Без названия")
    title = f"{mark} 🎤 {name} — {tracks_count_label(total)}"
    folder_label = await _folder_label(user_id, artist.get("folder_id"))
    if folder_label:
        title = f"{title}\n📁 {escape(folder_label)}"
    text = _trim(render_track_list(title, tracks, current, total_pages, ARTIST_EMPTY))

    if artist.get("is_listened"):
        toggle_text = "⬜ Снять отметку «прослушано»"
    else:
        toggle_text = "✅ Отметить прослушанным"
    extra_rows = [
        [
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f"{CARD_LISTENED_PREFIX}:{artist_id}:{current}",
            )
        ],
        [
            InlineKeyboardButton(
                text=BUTTON_UNPLAYED,
                callback_data=f"{UNPLAYED_PREFIX}:{_int(artist_id)}:1",
            ),
            InlineKeyboardButton(
                text=BUTTON_FOLDER,
                callback_data=f"{FOLDER_PICK_PREFIX}:{_int(artist_id)}:1",
            ),
        ],
    ]
    markup = tracks_page_kb(
        tracks,
        ctx=artist_context(artist_id),
        page=current,
        total_pages=total_pages,
        extra_rows=extra_rows,
    )
    return text, markup


async def _render_folder_pick(
    user_id: int, artist_id: int, page: int
) -> tuple[str, Any] | None:
    """Экран выбора папки для исполнителя; None — исполнитель не найден."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        return None

    folders = await folders_repo.list_folders(user_id, section="music")
    per_page = _page_size()
    items, total_pages = paginate(folders, page, per_page)
    current = _clamp_page(page, total_pages)

    name = escape(artist.get("name") or "Без названия")
    lines = [FOLDER_PICK_TITLE.format(name=name), ""]
    folder_label = await _folder_label(user_id, artist.get("folder_id"))
    lines.append(
        FOLDER_CURRENT.format(path=escape(folder_label)) if folder_label else FOLDER_CURRENT_NONE
    )
    lines.append("")
    lines.append(FOLDER_PICK_HINT if folders else FOLDER_PICK_EMPTY)
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    markup = artist_folder_pick_kb(
        artist_id,
        items,
        current,
        total_pages,
        has_folder=bool(folder_label),
    )
    return _trim("\n".join(lines)), markup


async def _render_unplayed(
    user_id: int, artist_id: int, page: int
) -> tuple[str, Any] | None:
    """Экран непрослушанных треков исполнителя; None — исполнитель не найден."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        return None

    tracks = await artists_repo.artist_unplayed_tracks(
        user_id, artist_id, limit=UNPLAYED_LIMIT
    )
    per_page = _page_size()
    items, total_pages = paginate(tracks, page, per_page)
    current = _clamp_page(page, total_pages)

    name = escape(artist.get("name") or "Без названия")
    title = f"{UNPLAYED_TITLE.format(name=name)} — {tracks_count_label(len(tracks))}"
    text = _trim(render_track_list(title, items, current, total_pages, UNPLAYED_EMPTY))
    markup = tracks_page_kb(
        items,
        ctx=artist_context(artist_id),
        page=current,
        # Пагинация у экрана своя (`artunp:...`), поэтому клавиатуре списка
        # сообщаем «страница одна» — иначе она добавила бы кнопки карточки.
        total_pages=1,
        extra_rows=_unplayed_extra_rows(artist_id, current, total_pages),
    )
    return text, markup


# ---------------------------------------------------------------------------
# Команда /artists
# ---------------------------------------------------------------------------


@router.message(Command("artists"))
async def cmd_artists(message: Message) -> None:
    """Показывает алфавитный список исполнителей с отметками «прослушано»."""
    if message.from_user is None:
        return
    text, markup = await _render_artists(int(message.from_user.id), 1)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Поиск по исполнителям (/artists_search, ТЗ п. 12)
# ---------------------------------------------------------------------------


async def _run_artist_search(message: Message, user_id: int, query: str) -> None:
    """Выполняет поиск исполнителей и показывает результат."""
    clean = " ".join(str(query or "").split())
    if not clean:
        await message.answer(texts.EMPTY_QUERY)
        return

    try:
        artists = await search_service.search_artists(user_id, clean, limit=SEARCH_LIMIT)
    except Exception:
        logger.exception("Ошибка поиска исполнителей «%s» пользователя %s", clean, user_id)
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    header = SEARCH_HEADER.format(query=escape(clean))
    if not artists:
        await message.answer(f"{header}\n\n{SEARCH_EMPTY}")
        logger.info("Поиск исполнителей «%s» пользователя %s: пусто", clean, user_id)
        return

    per_page = _page_size()
    shown = artists[:per_page]
    lines = [header, ""]
    for index, artist in enumerate(shown, start=1):
        mark = "✅" if artist.get("is_listened") else "⬜"
        name = escape(artist.get("name") or "Без имени")
        line = f"{index}. {mark} <b>{name}</b> — " + tracks_count_label(
            _int(artist.get("track_count"))
        )
        plays = _int(artist.get("play_count"))
        if plays:
            line += f" · ▶️{plays}"
        lines.append(line)
    if len(artists) > len(shown):
        lines.append("")
        lines.append(f"<i>Показаны первые {len(shown)} из {len(artists)}.</i>")

    await message.answer(_trim("\n".join(lines)), reply_markup=artist_search_kb(shown))
    logger.info(
        "Поиск исполнителей «%s» пользователя %s: найдено %s", clean, user_id, len(artists)
    )


@router.message(Command("artists_search"))
async def cmd_artists_search(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/artists_search` — нечёткий поиск по исполнителям."""
    if message.from_user is None:
        return
    query = (command.args or "").strip()
    if not query:
        await state.set_state(ArtistManageStates.waiting_search_query)
        await message.answer(SEARCH_PROMPT)
        return
    await state.set_state(None)
    await _run_artist_search(message, int(message.from_user.id), query)


@router.message(StateFilter(ArtistManageStates.waiting_search_query), Command("cancel"))
async def cancel_artists_search(message: Message, state: FSMContext) -> None:
    """Отменяет ожидание запроса поиска по исполнителям."""
    await state.set_state(None)
    await message.answer(texts.SEARCH_CANCELLED)


@router.message(StateFilter(ArtistManageStates.waiting_search_query), F.text)
async def artists_search_entered(message: Message, state: FSMContext) -> None:
    """Принимает запрос поиска по исполнителям из FSM."""
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
    await _run_artist_search(message, int(message.from_user.id), text)


# ---------------------------------------------------------------------------
# Ручное создание исполнителя (/create_artist, ТЗ п. 19)
# ---------------------------------------------------------------------------


async def _create_artist(message: Message, user_id: int, name: str) -> None:
    """Создаёт исполнителя вручную, сообщая о тёзке вместо дубликата."""
    clean = " ".join(str(name or "").split())
    if not clean:
        await message.answer(texts.NAME_EMPTY)
        return
    if len(clean) > artists_repo.MAX_NAME_LENGTH:
        await message.answer(
            f"✍️ Слишком длинное имя. Уложитесь в {artists_repo.MAX_NAME_LENGTH} символов."
        )
        return

    try:
        existing = await artists_repo.find_artist_by_name(user_id, clean)
        if existing is not None:
            await message.answer(
                ARTIST_EXISTS.format(name=escape(existing.get("name") or clean)),
                reply_markup=artist_created_kb(_int(existing.get("id"))),
            )
            return
        artist = await artists_repo.ensure_artist(user_id, clean)
    except ValidationError as exc:
        await message.answer(f"✍️ {escape(str(exc))}")
        return
    except MusicBoxError:
        logger.exception("Ошибка создания исполнителя «%s» пользователя %s", clean, user_id)
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    if artist is None:
        await message.answer(ARTIST_CREATE_FAILED)
        return

    logger.info(
        "Пользователь %s создал исполнителя «%s» (id=%s)",
        user_id,
        artist.get("name"),
        artist.get("id"),
    )
    await message.answer(
        ARTIST_CREATED.format(name=escape(artist.get("name") or clean)),
        reply_markup=artist_created_kb(_int(artist.get("id"))),
    )


@router.message(Command("create_artist"))
async def cmd_create_artist(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/create_artist` — ручное создание исполнителя."""
    if message.from_user is None:
        return
    name = (command.args or "").strip()
    if not name:
        await state.set_state(ArtistManageStates.waiting_new_name)
        await message.answer(CREATE_PROMPT)
        return
    await state.set_state(None)
    await _create_artist(message, int(message.from_user.id), name)


@router.message(StateFilter(ArtistManageStates.waiting_new_name), Command("cancel"))
async def cancel_create_artist(message: Message, state: FSMContext) -> None:
    """Отменяет создание исполнителя."""
    await state.set_state(None)
    await message.answer(texts.CANCELLED)


@router.message(StateFilter(ArtistManageStates.waiting_new_name), F.text)
async def create_artist_name_entered(message: Message, state: FSMContext) -> None:
    """Принимает имя нового исполнителя из FSM."""
    if message.from_user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(texts.NAME_EMPTY)
        return
    if text.startswith("/"):
        await state.set_state(None)
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return
    await state.set_state(None)
    await _create_artist(message, int(message.from_user.id), text)


# ---------------------------------------------------------------------------
# Колбэки исполнителей
# ---------------------------------------------------------------------------


@router.callback_query(ArtistCB.filter())
async def on_artist_callback(
    callback: CallbackQuery,
    callback_data: ArtistCB,
    bot: Bot,
) -> None:
    """Единый обработчик действий со списком исполнителей.

    ``artist_id > 0`` — работа с конкретным исполнителем (открытие карточки и
    пагинация его треков), ``artist_id <= 0`` — работа со списком.
    """
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    action = (callback_data.action or "").strip()
    artist_id = int(callback_data.artist_id or 0)
    page = max(1, int(callback_data.page or 1))

    if action == "listened" and artist_id > 0:
        artist = await artists_repo.toggle_listened(user_id, artist_id)
        if artist is None:
            await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
            return
        logger.info(
            "Пользователь %s: исполнитель %s теперь %s",
            user_id,
            artist_id,
            "прослушан" if artist.get("is_listened") else "не прослушан",
        )
        text, markup = await _render_artists(user_id, page)
        await _show(callback, bot, text, markup)
        await ack(callback, _listened_notice(artist))
        return

    if action in {"open", "tracks", "page"} and artist_id > 0:
        rendered = await _render_artist(user_id, artist_id, page)
        if rendered is None:
            text, markup = await _render_artists(user_id, 1)
            await _show(callback, bot, text, markup)
            await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
            return
        await _show(callback, bot, rendered[0], rendered[1])
        await ack(callback)
        return

    if action in {"open", "tracks", "page", "list", "back"}:
        # «Назад» из карточки исполнителя возвращает к первой странице списка.
        list_page = 1 if artist_id > 0 else page
        text, markup = await _render_artists(user_id, list_page)
        await _show(callback, bot, text, markup)
        await ack(callback)
        return

    logger.debug("Неизвестное действие с исполнителем: %r", action)
    await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)


@router.callback_query(F.data.func(_is_card_listened))
async def on_card_listened(callback: CallbackQuery, bot: Bot) -> None:
    """Переключает отметку «прослушано» прямо в карточке исполнителя."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_card_listened(callback.data)
    if parsed is None:
        logger.warning("Не удалось разобрать отметку исполнителя: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    artist_id, page = parsed
    user_id = int(callback.from_user.id)
    artist = await artists_repo.toggle_listened(user_id, artist_id)
    if artist is None:
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    logger.info(
        "Пользователь %s: исполнитель %s теперь %s",
        user_id,
        artist_id,
        "прослушан" if artist.get("is_listened") else "не прослушан",
    )
    rendered = await _render_artist(user_id, artist_id, max(1, page))
    if rendered is None:
        text, markup = await _render_artists(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback, _listened_notice(artist))


# ---------------------------------------------------------------------------
# Непрослушанные треки исполнителя (ТЗ п. 2)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.func(_is_unplayed))
async def on_unplayed(callback: CallbackQuery, bot: Bot) -> None:
    """Показывает треки исполнителя, которые ни разу не проигрывались."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_pair(callback.data, UNPLAYED_PREFIX)
    if parsed is None:
        logger.warning("Не удалось разобрать переход к непрослушанным: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    artist_id, page = parsed
    user_id = int(callback.from_user.id)
    rendered = await _render_unplayed(user_id, artist_id, max(1, page))
    if rendered is None:
        text, markup = await _render_artists(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback)


# ---------------------------------------------------------------------------
# Привязка исполнителя к папке (ТЗ п. 8)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.func(_is_folder_pick))
async def on_folder_pick(callback: CallbackQuery, bot: Bot) -> None:
    """Открывает экран выбора папки для исполнителя."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_pair(callback.data, FOLDER_PICK_PREFIX)
    if parsed is None:
        logger.warning("Не удалось разобрать выбор папки исполнителя: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    artist_id, page = parsed
    user_id = int(callback.from_user.id)
    rendered = await _render_folder_pick(user_id, artist_id, max(1, page))
    if rendered is None:
        text, markup = await _render_artists(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback)


@router.callback_query(F.data.func(_is_folder_set))
async def on_folder_set(callback: CallbackQuery, bot: Bot) -> None:
    """Привязывает исполнителя к выбранной папке (или отвязывает)."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_pair(callback.data, FOLDER_SET_PREFIX)
    if parsed is None:
        logger.warning("Не удалось разобрать привязку папки: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    artist_id, folder_id = parsed
    user_id = int(callback.from_user.id)
    target: int | None = None if folder_id == FOLDER_DETACH else folder_id

    try:
        artist = await artists_repo.set_folder(user_id, artist_id, target)
    except ValidationError as exc:
        await ack(callback, str(exc), alert=True)
        return
    except MusicBoxError:
        logger.exception(
            "Ошибка привязки исполнителя %s к папке %s пользователя %s",
            artist_id,
            target,
            user_id,
        )
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    if artist is None:
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    folder_label = await _folder_label(user_id, artist.get("folder_id"))
    notice = (
        FOLDER_ATTACHED.format(folder=folder_label) if folder_label else FOLDER_DETACHED
    )
    logger.info(
        "Пользователь %s: исполнитель %s привязан к папке %s",
        user_id,
        artist_id,
        target if target is not None else "—",
    )

    rendered = await _render_artist(user_id, artist_id, 1)
    if rendered is None:
        text, markup = await _render_artists(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback, notice)


__all__ = [
    "ArtistManageStates",
    "artist_folder_pick_kb",
    "artist_search_kb",
    "router",
]
