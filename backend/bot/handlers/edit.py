"""Хендлеры редактирования и удаления (ТЗ п. 9, 10).

Раздел закрывает три команды:

* ``/edit_track [id]``   — переименование трека (:func:`tracks_repo.rename_track`);
* ``/edit_artist [id]``  — переименование исполнителя (:func:`artists_repo.rename_artist`,
  при совпадении имён — СЛИЯНИЕ, о котором пользователя предупреждают заранее);
* ``/delete_track [id]`` — удаление трека из библиотеки и из канала-хранилища
  (:func:`tracks_repo.delete_track` + :func:`storage.delete_from_channel`).

Каждая команда работает и с аргументом (``/delete_track 12``), и без него:
без аргумента показывается список последних файлов (для исполнителей — алфавитный
список) с кнопками выбора, а идентификатор можно прислать обычным сообщением.

Здесь же живёт единый обработчик :class:`EditCB` — его шлют кнопки
«✏️ Переименовать» и «🗑 Удалить» из других разделов бота. Договорённость по полям:

============================  ===============================  ==========================
``action``                    ``kind``                         ``target_id``
============================  ===============================  ==========================
``rename``/``edit``/``pick``  ``track``                        id трека
``rename``/``edit``/``pick``  ``artist``                       id исполнителя
``delete``/``del``            любой                            id трека
``page``                      ``track``/``artist``/``del``     номер страницы списка
``confirm``/``yes``/``no``    ``del`` (удаление), ``merge``    id трека / исполнителя
``cancel``                    любой                            0
============================  ===============================  ==========================

Если ``kind`` пришёл незнакомый, вид объекта определяется по самому ``target_id``
(сначала трек, затем исполнитель) — так кнопки соседних разделов продолжают
работать, даже если заполнили поле по-своему.

Все FSM-состояния и тексты объявлены локально: файл пишется параллельно с
``keyboards.py``, ``texts.py`` и ``states.py``, которые ведут другие модули.
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
from backend.bot.callbacks import EditCB
from backend.bot.keyboards import confirm_kb
from backend.bot.utils import (
    ack,
    escape,
    page_offset,
    paginate,
    safe_edit,
    tracks_count_label,
)
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, ValidationError
from backend.services import storage

logger = logging.getLogger(__name__)

router = Router(name="edit")


# ---------------------------------------------------------------------------
# Константы модуля
# ---------------------------------------------------------------------------

#: Виды объектов в `EditCB.kind`.
KIND_TRACK: str = "track"
KIND_ARTIST: str = "artist"
#: Список выбора трека для удаления (тот же трек, другой сценарий).
KIND_DELETE: str = "del"
#: Подтверждение слияния исполнителей при переименовании.
KIND_MERGE: str = "merge"

#: Действия `EditCB`, начинающие переименование.
RENAME_ACTIONS: frozenset[str] = frozenset({"rename", "edit", "open", "start", "title", "name"})
#: Действия `EditCB`, начинающие удаление трека.
DELETE_ACTIONS: frozenset[str] = frozenset({"delete", "del", "remove", "drop"})
#: Подтверждение и отказ (кнопки `keyboards.confirm_kb`).
YES_ACTIONS: frozenset[str] = frozenset({"yes", "confirm", "ok"})
NO_ACTIONS: frozenset[str] = frozenset({"no", "abort", "back"})

#: Сколько кнопок с номерами помещаем в один ряд.
BUTTONS_IN_ROW: int = 5

#: Лимит текста сообщения Telegram.
MESSAGE_LIMIT: int = 4096

#: До скольких символов укорачиваем название в строке списка.
LABEL_LIMIT: int = 48

#: Максимальные длины значений — те же, что проверяют репозитории.
MAX_TRACK_TITLE: int = tracks_repo.MAX_TITLE_LENGTH
MAX_ARTIST_NAME: int = artists_repo.MAX_NAME_LENGTH


# ---------------------------------------------------------------------------
# Тексты раздела (русские, HTML-разметка Telegram)
# ---------------------------------------------------------------------------

TRACK_PICK_TITLE = "✏️ <b>Переименование трека</b>"
TRACK_DELETE_TITLE = "🗑 <b>Удаление трека</b>"
ARTIST_PICK_TITLE = "✏️ <b>Переименование исполнителя</b>"

PICK_TRACK_HINT = "Выбери файл кнопкой с его номером или пришли id сообщением."
PICK_ARTIST_HINT = "Выбери исполнителя кнопкой с его номером или пришли id сообщением."

TRACK_RENAME_PROMPT = "✏️ Введи новое название трека."
ARTIST_RENAME_PROMPT = "✏️ Введи новое имя исполнителя."
CURRENT_VALUE = "Сейчас: «{value}»."

BAD_ID = "✍️ Нужен числовой id — например, <code>12</code>."
NO_TRACKS = "🎧 В библиотеке пока нет файлов — сначала пришли мне аудио."
NO_ARTISTS = "🎤 Исполнителей пока нет — они появятся вместе с треками."

TRACK_RENAMED = "✏️ Трек переименован: «{title}»."
ARTIST_RENAMED = "✏️ Исполнитель переименован: «{name}»."
ARTIST_MERGED = "🔀 «{source}» объединён с «{target}». Теперь у «{target}» {count}."

MERGE_WARNING = (
    "⚠️ Исполнитель «{target}» уже есть в библиотеке ({count}).\n"
    "Если продолжить, «{source}» будет объединён с ним: треки, альбомы и отметки "
    "перейдут к «{target}», а дубликат исчезнет. Отменить объединение нельзя.\n\n"
    "Объединяем?"
)
MERGE_LOST = "🤔 Я потерял новое имя — пришли его ещё раз."

DELETE_QUESTION = (
    "🗑 Удалить трек «{title}»?\n"
    "Файл будет удалён и из библиотеки, и из канала-хранилища — восстановить не получится."
)
TRACK_DELETED_RESULT = "🗑 Трек «{title}» удалён из библиотеки."
DELETE_FAILED = "😔 Не получилось удалить трек. Попробуй ещё раз чуть позже."

CANCELLED_TEXT = texts.CANCELLED
MERGE_CANCELLED = "❌ Объединение отменено — имя исполнителя не изменилось."
DELETE_CANCELLED = "❌ Удаление отменено — трек остался в библиотеке."
COMMAND_INTERRUPTED = f"{texts.CANCELLED} Повторите команду ещё раз."
VALUE_TOO_LONG = "✍️ Слишком длинно. Уложись в {limit} символов, пожалуйста."


# ---------------------------------------------------------------------------
# FSM-состояния раздела
# ---------------------------------------------------------------------------


class EditStates(StatesGroup):
    """Диалоги переименования и удаления."""

    #: Ожидание id трека для переименования (`/edit_track` без аргумента).
    waiting_track_id = State()
    #: Ожидание нового названия трека.
    waiting_track_title = State()
    #: Ожидание id исполнителя (`/edit_artist` без аргумента).
    waiting_artist_id = State()
    #: Ожидание нового имени исполнителя (и подтверждения слияния).
    waiting_artist_name = State()
    #: Ожидание id трека для удаления (`/delete_track` без аргумента).
    waiting_delete_id = State()


#: Все состояния раздела — для фильтра «/cancel».
ALL_STATES = (
    EditStates.waiting_track_id,
    EditStates.waiting_track_title,
    EditStates.waiting_artist_id,
    EditStates.waiting_artist_name,
    EditStates.waiting_delete_id,
)


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


def _clamp_page(page: Any, total_pages: int) -> int:
    """Приводит номер страницы к допустимому диапазону."""
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), max(1, int(total_pages)))


def _to_int(value: Any, default: int = 0) -> int:
    """Безопасное приведение к int (нечисловое значение -> `default`)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _user_id(event: Message | CallbackQuery) -> int | None:
    """Идентификатор пользователя события или None."""
    user = getattr(event, "from_user", None)
    if user is None:
        return None
    return int(user.id)


def _parse_id(raw: Any) -> int | None:
    """Разбирает введённый идентификатор (``12``, ``#12``); None — не число."""
    value = str(raw or "").strip().lstrip("#").strip()
    if not value.isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _short(value: Any, limit: int = LABEL_LIMIT) -> str:
    """Укорачивает длинное название до `limit` символов (с многоточием)."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _fit(text: str) -> str:
    """Подрезает сообщение под лимит Telegram (4096 символов)."""
    if len(text) <= MESSAGE_LIMIT:
        return text
    logger.debug("Текст раздела редактирования подрезан: %s символов", len(text))
    return text[: MESSAGE_LIMIT - 1].rstrip() + "…"


def _validate_value(value: str, limit: int) -> str | None:
    """Проверяет введённое название; возвращает текст ошибки или None."""
    if not value:
        return texts.NAME_EMPTY
    if len(value) > limit:
        return VALUE_TOO_LONG.format(limit=limit)
    return None


def _track_title(track: dict | None) -> str:
    """Экранированное название трека для сообщений."""
    if not track:
        return "Без названия"
    return escape(track.get("title") or "Без названия")


def _artist_name(artist: dict | None) -> str:
    """Экранированное имя исполнителя для сообщений."""
    if not artist:
        return "Без названия"
    return escape(artist.get("name") or "Без названия")


async def _show(
    event: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    notice: str | None = None,
    alert: bool = False,
) -> None:
    """Показывает экран: команде — новым сообщением, кнопке — правкой сообщения."""
    body = _fit(text)
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            await safe_edit(event.message, body, markup)
        elif event.bot is not None and event.from_user is not None:
            await event.bot.send_message(event.from_user.id, body, reply_markup=markup)
        await ack(event, notice, alert=alert)
        return
    await event.answer(body, reply_markup=markup)


def _cancel_row(kind: str) -> list[InlineKeyboardButton]:
    """Ряд с единственной кнопкой отмены диалога."""
    return [
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=EditCB(action="cancel", kind=kind, target_id=0).pack(),
        )
    ]


def _cancel_kb(kind: str) -> InlineKeyboardMarkup:
    """Клавиатура с одной кнопкой «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(*_cancel_row(kind))
    return builder.as_markup()


def _picker_kb(
    ids: Sequence[int],
    *,
    kind: str,
    page: int,
    total_pages: int,
    start_number: int,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора: номера элементов, пагинация и отмена."""
    builder = InlineKeyboardBuilder()

    row: list[InlineKeyboardButton] = []
    for shift, target_id in enumerate(ids):
        row.append(
            InlineKeyboardButton(
                text=str(start_number + shift),
                callback_data=EditCB(
                    action="pick", kind=kind, target_id=int(target_id)
                ).pack(),
            )
        )
        if len(row) == BUTTONS_IN_ROW:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    if total_pages > 1:
        prev_page = page - 1 if page > 1 else total_pages
        next_page = page + 1 if page < total_pages else 1
        builder.row(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=EditCB(action="page", kind=kind, target_id=prev_page).pack(),
            ),
            InlineKeyboardButton(
                text=f"{page}/{total_pages}",
                callback_data=EditCB(action="noop", kind=kind, target_id=page).pack(),
            ),
            InlineKeyboardButton(
                text="➡️",
                callback_data=EditCB(action="page", kind=kind, target_id=next_page).pack(),
            ),
        )

    builder.row(*_cancel_row(kind))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Экраны выбора объекта
# ---------------------------------------------------------------------------


async def _render_track_picker(
    user_id: int, page: int, *, kind: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Список последних файлов пользователя с кнопками выбора.

    `kind` задаёт сценарий: `KIND_TRACK` — переименование, `KIND_DELETE` — удаление.
    Фильтра по типу файла нет: переименовать и удалить можно и документ, и кружочек.
    """
    per_page = _page_size()
    total = await tracks_repo.count_tracks(user_id, file_type=None)
    if total <= 0:
        return NO_TRACKS, None

    total_pages = _total_pages(total, per_page)
    current = _clamp_page(page, total_pages)
    tracks = await tracks_repo.list_tracks(
        user_id,
        limit=per_page,
        offset=page_offset(current, per_page),
        file_type=None,
        order="created_at_desc",
    )
    if not tracks and current > 1:
        current = 1
        tracks = await tracks_repo.list_tracks(
            user_id, limit=per_page, offset=0, file_type=None, order="created_at_desc"
        )
    if not tracks:
        return NO_TRACKS, None

    title = TRACK_DELETE_TITLE if kind == KIND_DELETE else TRACK_PICK_TITLE
    lines = [title, "", PICK_TRACK_HINT, ""]
    start = page_offset(current, per_page) + 1
    for index, track in enumerate(tracks, start=start):
        line = f"{index}. <b>{escape(_short(track.get('title') or 'Без названия'))}</b>"
        artist = str(track.get("artist") or "").strip()
        if artist:
            line += f" — {escape(_short(artist, 24))}"
        line += f" · id <code>{_to_int(track.get('id'))}</code>"
        lines.append(line)
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    markup = _picker_kb(
        [_to_int(track.get("id")) for track in tracks],
        kind=kind,
        page=current,
        total_pages=total_pages,
        start_number=start,
    )
    return "\n".join(lines), markup


async def _render_artist_picker(
    user_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Алфавитный список исполнителей с кнопками выбора."""
    artists = await artists_repo.list_artists(user_id)
    if not artists:
        return NO_ARTISTS, None

    per_page = _page_size()
    items, total_pages = paginate(artists, page, per_page)
    current = _clamp_page(page, total_pages)

    lines = [ARTIST_PICK_TITLE, "", PICK_ARTIST_HINT, ""]
    start = page_offset(current, per_page) + 1
    for index, artist in enumerate(items, start=start):
        count = tracks_count_label(_to_int(artist.get("track_count")))
        lines.append(
            f"{index}. <b>{escape(_short(artist.get('name') or 'Без названия'))}</b>"
            f" — {count} · id <code>{_to_int(artist.get('id'))}</code>"
        )
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    markup = _picker_kb(
        [_to_int(artist.get("id")) for artist in items],
        kind=KIND_ARTIST,
        page=current,
        total_pages=total_pages,
        start_number=start,
    )
    return "\n".join(lines), markup


async def _ask_track(
    event: Message | CallbackQuery, state: FSMContext, user_id: int, *, kind: str
) -> None:
    """Показывает список файлов и ждёт выбор кнопкой либо id сообщением."""
    text, markup = await _render_track_picker(user_id, 1, kind=kind)
    if markup is None:
        await state.clear()
        await _show(event, text)
        return
    await state.set_state(
        EditStates.waiting_delete_id if kind == KIND_DELETE else EditStates.waiting_track_id
    )
    await _show(event, text, markup)


async def _ask_artist(
    event: Message | CallbackQuery, state: FSMContext, user_id: int
) -> None:
    """Показывает список исполнителей и ждёт выбор кнопкой либо id сообщением."""
    text, markup = await _render_artist_picker(user_id, 1)
    if markup is None:
        await state.clear()
        await _show(event, text)
        return
    await state.set_state(EditStates.waiting_artist_id)
    await _show(event, text, markup)


# ---------------------------------------------------------------------------
# Начало диалогов
# ---------------------------------------------------------------------------


async def _start_track_rename(
    event: Message | CallbackQuery, state: FSMContext, user_id: int, track_id: int
) -> bool:
    """Запрашивает новое название трека; False — трек не найден."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        logger.debug("Переименование: трек %s пользователя %s не найден", track_id, user_id)
        await state.clear()
        await _show(event, texts.TRACK_NOT_FOUND, notice=texts.TRACK_NOT_FOUND, alert=True)
        return False

    await state.set_state(EditStates.waiting_track_title)
    await state.update_data(track_id=int(track_id))
    body = (
        f"{TRACK_RENAME_PROMPT}\n"
        f"{CURRENT_VALUE.format(value=_track_title(track))}\n\n{texts.CANCEL_HINT}"
    )
    await _show(event, body, _cancel_kb(KIND_TRACK))
    return True


async def _start_artist_rename(
    event: Message | CallbackQuery, state: FSMContext, user_id: int, artist_id: int
) -> bool:
    """Запрашивает новое имя исполнителя; False — исполнитель не найден."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        logger.debug(
            "Переименование: исполнитель %s пользователя %s не найден", artist_id, user_id
        )
        await state.clear()
        await _show(event, texts.ARTIST_NOT_FOUND, notice=texts.ARTIST_NOT_FOUND, alert=True)
        return False

    await state.set_state(EditStates.waiting_artist_name)
    await state.update_data(artist_id=int(artist_id), pending_name="")
    body = (
        f"{ARTIST_RENAME_PROMPT}\n"
        f"{CURRENT_VALUE.format(value=_artist_name(artist))}\n\n{texts.CANCEL_HINT}"
    )
    await _show(event, body, _cancel_kb(KIND_ARTIST))
    return True


async def _ask_delete(
    event: Message | CallbackQuery, state: FSMContext, user_id: int, track_id: int
) -> bool:
    """Спрашивает подтверждение удаления трека; False — трек не найден."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await state.clear()
        await _show(event, texts.TRACK_NOT_FOUND, notice=texts.TRACK_NOT_FOUND, alert=True)
        return False

    await state.clear()
    markup = confirm_kb(
        EditCB(action="yes", kind=KIND_DELETE, target_id=int(track_id)).pack(),
        EditCB(action="no", kind=KIND_DELETE, target_id=int(track_id)).pack(),
    )
    await _show(event, DELETE_QUESTION.format(title=_track_title(track)), markup)
    return True


# ---------------------------------------------------------------------------
# Применение изменений
# ---------------------------------------------------------------------------


async def _apply_track_rename(
    message: Message, state: FSMContext, user_id: int, track_id: int, title: str
) -> None:
    """Переименовывает трек и сообщает результат."""
    try:
        track = await tracks_repo.rename_track(user_id, track_id, title)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведи другое название.")
        return
    except MusicBoxError:
        logger.exception(
            "Не удалось переименовать трек %s пользователя %s", track_id, user_id
        )
        await state.clear()
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    await state.clear()
    if track is None:
        await message.answer(texts.TRACK_NOT_FOUND)
        return

    logger.info("Пользователь %s переименовал трек %s", user_id, track_id)
    await message.answer(TRACK_RENAMED.format(title=_track_title(track)))


async def _apply_artist_rename(
    message: Message, state: FSMContext, user_id: int, artist_id: int, name: str
) -> None:
    """Переименовывает исполнителя; при конфликте имён сначала спрашивает о слиянии."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        await state.clear()
        await message.answer(texts.ARTIST_NOT_FOUND)
        return

    existing = await artists_repo.find_artist_by_name(user_id, name)
    if existing is not None and int(existing["id"]) != int(artist["id"]):
        await state.update_data(artist_id=int(artist_id), pending_name=name)
        markup = confirm_kb(
            EditCB(action="yes", kind=KIND_MERGE, target_id=int(artist_id)).pack(),
            EditCB(action="no", kind=KIND_MERGE, target_id=int(artist_id)).pack(),
        )
        warning = MERGE_WARNING.format(
            source=_artist_name(artist),
            target=_artist_name(existing),
            count=tracks_count_label(_to_int(existing.get("track_count"))),
        )
        logger.info(
            "Пользователь %s: переименование исполнителя %s приведёт к слиянию с %s",
            user_id,
            artist_id,
            existing["id"],
        )
        await message.answer(_fit(warning), reply_markup=markup)
        return

    await _rename_artist_now(message, state, user_id, artist, name)


async def _rename_artist_now(
    message: Message,
    state: FSMContext,
    user_id: int,
    artist: dict,
    name: str,
) -> None:
    """Вызывает `rename_artist` (со слиянием) и сообщает результат."""
    artist_id = int(artist["id"])
    source_name = _artist_name(artist)
    try:
        result = await artists_repo.rename_artist(user_id, artist_id, name)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведи другое имя.")
        return
    except MusicBoxError:
        logger.exception(
            "Не удалось переименовать исполнителя %s пользователя %s", artist_id, user_id
        )
        await state.clear()
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    await state.clear()
    if result is None:
        await message.answer(texts.ARTIST_NOT_FOUND)
        return

    merged = int(result["id"]) != artist_id
    logger.info(
        "Пользователь %s: исполнитель %s %s («%s»)",
        user_id,
        artist_id,
        "объединён с " + str(result["id"]) if merged else "переименован",
        name,
    )
    if merged:
        await message.answer(
            ARTIST_MERGED.format(
                source=source_name,
                target=_artist_name(result),
                count=tracks_count_label(_to_int(result.get("track_count"))),
            )
        )
        return
    await message.answer(ARTIST_RENAMED.format(name=_artist_name(result)))


async def _delete_track_now(
    callback: CallbackQuery, bot: Bot, state: FSMContext, user_id: int, track_id: int
) -> None:
    """Удаляет трек из БД и из канала-хранилища."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await state.clear()
        await _show(callback, texts.TRACK_NOT_FOUND, notice=texts.TRACK_NOT_FOUND, alert=True)
        return

    title = _track_title(track)
    try:
        deleted = await tracks_repo.delete_track(user_id, track_id)
    except MusicBoxError:
        logger.exception("Не удалось удалить трек %s пользователя %s", track_id, user_id)
        await ack(callback, DELETE_FAILED, alert=True)
        return

    if deleted is None:
        await state.clear()
        await _show(callback, texts.TRACK_NOT_FOUND, notice=texts.TRACK_NOT_FOUND, alert=True)
        return

    # `delete_from_channel` сама глушит ошибки Telegram и возвращает False:
    # для пользователя трека уже нет в библиотеке, а мусор в канале — наша забота.
    message_id = deleted.get("storage_message_id")
    if message_id and not await storage.delete_from_channel(bot, message_id):
        logger.warning(
            "Трек %s удалён у пользователя %s, но сообщение %s осталось в канале",
            track_id,
            user_id,
            message_id,
        )

    await state.clear()
    logger.info("Пользователь %s удалил трек %s", user_id, track_id)
    await _show(
        callback,
        TRACK_DELETED_RESULT.format(title=title),
        notice=texts.TRACK_DELETED,
    )


async def _resolve_kind(user_id: int, kind: str, target_id: int) -> str | None:
    """Определяет вид объекта: явный `kind`, иначе — по существованию записи."""
    if kind == KIND_ARTIST:
        return KIND_ARTIST
    if kind in {KIND_TRACK, KIND_DELETE}:
        return KIND_TRACK
    if target_id <= 0:
        return None
    if await tracks_repo.get_track(user_id, target_id) is not None:
        return KIND_TRACK
    if await artists_repo.get_artist(user_id, target_id) is not None:
        return KIND_ARTIST
    return None


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@router.message(Command("edit_track"))
async def cmd_edit_track(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/edit_track [id]` — переименование трека."""
    user_id = _user_id(message)
    if user_id is None:
        return

    raw = (command.args or "").strip()
    if not raw:
        await _ask_track(message, state, user_id, kind=KIND_TRACK)
        return

    track_id = _parse_id(raw)
    if track_id is None:
        await message.answer(BAD_ID)
        await _ask_track(message, state, user_id, kind=KIND_TRACK)
        return

    if not await _start_track_rename(message, state, user_id, track_id):
        # Такого id нет — сразу показываем, из чего выбирать.
        await _ask_track(message, state, user_id, kind=KIND_TRACK)


@router.message(Command("edit_artist"))
async def cmd_edit_artist(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/edit_artist [id]` — переименование исполнителя (со слиянием тёзок)."""
    user_id = _user_id(message)
    if user_id is None:
        return

    raw = (command.args or "").strip()
    if not raw:
        await _ask_artist(message, state, user_id)
        return

    artist_id = _parse_id(raw)
    if artist_id is None:
        await message.answer(BAD_ID)
        await _ask_artist(message, state, user_id)
        return

    if not await _start_artist_rename(message, state, user_id, artist_id):
        await _ask_artist(message, state, user_id)


@router.message(Command("delete_track"))
async def cmd_delete_track(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/delete_track [id]` — удаление трека из библиотеки и из канала."""
    user_id = _user_id(message)
    if user_id is None:
        return

    raw = (command.args or "").strip()
    if not raw:
        await _ask_track(message, state, user_id, kind=KIND_DELETE)
        return

    track_id = _parse_id(raw)
    if track_id is None:
        await message.answer(BAD_ID)
        await _ask_track(message, state, user_id, kind=KIND_DELETE)
        return

    if not await _ask_delete(message, state, user_id, track_id):
        await _ask_track(message, state, user_id, kind=KIND_DELETE)


# ---------------------------------------------------------------------------
# Колбэки EditCB
# ---------------------------------------------------------------------------


@router.callback_query(EditCB.filter())
async def on_edit_callback(
    callback: CallbackQuery,
    callback_data: EditCB,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Единая точка входа для кнопок «✏️ Переименовать» и «🗑 Удалить»."""
    user_id = _user_id(callback)
    if user_id is None:
        await ack(callback)
        return

    action = (callback_data.action or "").strip().casefold()
    kind = (callback_data.kind or "").strip().casefold()
    target_id = _to_int(callback_data.target_id)

    if action == "noop":
        await ack(callback)
        return

    if action == "cancel":
        await state.clear()
        await _show(callback, CANCELLED_TEXT)
        return

    if action == "page":
        page = max(1, target_id)
        if kind == KIND_ARTIST:
            text, markup = await _render_artist_picker(user_id, page)
        else:
            picker_kind = KIND_DELETE if kind == KIND_DELETE else KIND_TRACK
            text, markup = await _render_track_picker(user_id, page, kind=picker_kind)
        await _show(callback, text, markup)
        return

    if action in NO_ACTIONS:
        await state.clear()
        if kind == KIND_MERGE:
            await _show(callback, MERGE_CANCELLED)
        elif kind == KIND_DELETE:
            await _show(callback, DELETE_CANCELLED)
        else:
            await _show(callback, CANCELLED_TEXT)
        return

    if action in YES_ACTIONS:
        if kind == KIND_MERGE:
            await _confirm_merge(callback, state, user_id, target_id)
            return
        await _delete_track_now(callback, bot, state, user_id, target_id)
        return

    if action in DELETE_ACTIONS or (action == "pick" and kind == KIND_DELETE):
        await _ask_delete(callback, state, user_id, target_id)
        return

    if action in RENAME_ACTIONS or action == "pick":
        resolved = await _resolve_kind(user_id, kind, target_id)
        if resolved == KIND_ARTIST:
            await _start_artist_rename(callback, state, user_id, target_id)
            return
        if resolved == KIND_TRACK:
            await _start_track_rename(callback, state, user_id, target_id)
            return
        await ack(callback, texts.NOT_FOUND, alert=True)
        return

    logger.debug("Неизвестное действие раздела редактирования: %r (kind=%r)", action, kind)
    await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)


async def _confirm_merge(
    callback: CallbackQuery, state: FSMContext, user_id: int, artist_id: int
) -> None:
    """Подтверждённое слияние исполнителей при переименовании."""
    data = await state.get_data()
    name = str(data.get("pending_name") or "").strip()
    target_id = _to_int(data.get("artist_id"), artist_id) or artist_id
    if not name:
        await state.clear()
        await _show(callback, MERGE_LOST, notice=MERGE_LOST, alert=True)
        return

    artist = await artists_repo.get_artist(user_id, target_id)
    if artist is None:
        await state.clear()
        await _show(callback, texts.ARTIST_NOT_FOUND, notice=texts.ARTIST_NOT_FOUND, alert=True)
        return

    message = callback.message
    await ack(callback)
    if not isinstance(message, Message):
        if callback.bot is not None:
            sent = await callback.bot.send_message(user_id, "⏳ Объединяю исполнителей…")
            await _rename_artist_now(sent, state, user_id, artist, name)
        return
    await _rename_artist_now(message, state, user_id, artist, name)


# ---------------------------------------------------------------------------
# FSM: ввод идентификаторов и новых названий
# ---------------------------------------------------------------------------


@router.message(StateFilter(*ALL_STATES), Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает любой диалог раздела."""
    await state.clear()
    await message.answer(CANCELLED_TEXT)


async def _interrupted(message: Message, state: FSMContext) -> bool:
    """Обрабатывает команду, присланную вместо ожидаемого значения."""
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        await message.answer(COMMAND_INTERRUPTED)
        return True
    return False


@router.message(StateFilter(EditStates.waiting_track_id), F.text)
async def on_track_id(message: Message, state: FSMContext) -> None:
    """Получен id трека для переименования."""
    user_id = _user_id(message)
    if user_id is None or await _interrupted(message, state):
        return

    track_id = _parse_id(message.text)
    if track_id is None:
        await message.answer(BAD_ID)
        return
    if not await _start_track_rename(message, state, user_id, track_id):
        # Ошиблись идентификатором — остаёмся в диалоге и ждём другой.
        await state.set_state(EditStates.waiting_track_id)


@router.message(StateFilter(EditStates.waiting_delete_id), F.text)
async def on_delete_id(message: Message, state: FSMContext) -> None:
    """Получен id трека для удаления."""
    user_id = _user_id(message)
    if user_id is None or await _interrupted(message, state):
        return

    track_id = _parse_id(message.text)
    if track_id is None:
        await message.answer(BAD_ID)
        return
    if not await _ask_delete(message, state, user_id, track_id):
        await state.set_state(EditStates.waiting_delete_id)


@router.message(StateFilter(EditStates.waiting_artist_id), F.text)
async def on_artist_id(message: Message, state: FSMContext) -> None:
    """Получен id исполнителя для переименования."""
    user_id = _user_id(message)
    if user_id is None or await _interrupted(message, state):
        return

    artist_id = _parse_id(message.text)
    if artist_id is None:
        await message.answer(BAD_ID)
        return
    if not await _start_artist_rename(message, state, user_id, artist_id):
        await state.set_state(EditStates.waiting_artist_id)


@router.message(StateFilter(EditStates.waiting_track_title), F.text)
async def on_track_title(message: Message, state: FSMContext) -> None:
    """Получено новое название трека."""
    user_id = _user_id(message)
    if user_id is None or await _interrupted(message, state):
        return

    title = (message.text or "").strip()
    error_text = _validate_value(title, MAX_TRACK_TITLE)
    if error_text:
        await message.answer(error_text)
        return

    data = await state.get_data()
    track_id = _to_int(data.get("track_id"))
    if track_id <= 0:
        await state.clear()
        await message.answer(texts.TRACK_NOT_FOUND)
        return

    await _apply_track_rename(message, state, user_id, track_id, title)


@router.message(StateFilter(EditStates.waiting_artist_name), F.text)
async def on_artist_name(message: Message, state: FSMContext) -> None:
    """Получено новое имя исполнителя."""
    user_id = _user_id(message)
    if user_id is None or await _interrupted(message, state):
        return

    name = (message.text or "").strip()
    error_text = _validate_value(name, MAX_ARTIST_NAME)
    if error_text:
        await message.answer(error_text)
        return

    data = await state.get_data()
    artist_id = _to_int(data.get("artist_id"))
    if artist_id <= 0:
        await state.clear()
        await message.answer(texts.ARTIST_NOT_FOUND)
        return

    await _apply_artist_rename(message, state, user_id, artist_id, name)


__all__ = [
    "ALL_STATES",
    "KIND_ARTIST",
    "KIND_DELETE",
    "KIND_MERGE",
    "KIND_TRACK",
    "EditStates",
    "router",
]
