"""Пакетное распределение загруженных файлов по папкам (ТЗ п. 8).

Когда пользователь присылает боту несколько аудиофайлов подряд,
`backend.bot.handlers.upload` копит их в FSM-данных и по окончании загрузки
передаёт управление сюда (`start_batch`). Дальше работает диалог раскладки:

1. треки группируются по исполнителю (`services.batch_sort.group_by_artist`);
2. для текущей группы предлагается папка — существующая или новая с именем
   исполнителя (у группы без исполнителя название спрашивается текстом);
3. можно указать КОЛИЧЕСТВО треков, которые уйдут в папку
   (`BatchStates.waiting_count` + `services.batch_sort.assign(limit=...)`);
4. после каждого шага показывается сводка «Остались нераспределённые (N)»,
   и диалог повторяется, пока распределены не все треки;
5. кнопка «Оставить остальные без папки» завершает раскладку досрочно.

Состояние партии (список `track_id` и карта «трек → папка») живёт ТОЛЬКО в
FSM-данных пользователя, глобальных переменных с состоянием здесь нет.

Клавиатуры и русские тексты этого раздела объявлены локально: `keyboards.py`
и `texts.py` пишутся параллельно другими модулями и общих экранов для пакетной
раскладки не содержат.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import FOLDER_NEW, FOLDER_NONE, BatchCB, NavCB
from backend.bot.states import BatchStates
from backend.bot.utils import ack, escape, paginate, plural, tracks_count_label
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, NotFoundError, ValidationError
from backend.services import batch_sort
from backend.services.batch_sort import BatchState

logger = logging.getLogger(__name__)

router = Router(name="batch")


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

#: Ключи FSM-данных пакетной раскладки (свои, чужие данные не трогаем).
DATA_TRACK_IDS: Final[str] = "batch_track_ids"
DATA_ASSIGNED: Final[str] = "batch_assigned"
DATA_CHAT_ID: Final[str] = "batch_chat_id"
DATA_MESSAGE_ID: Final[str] = "batch_message_id"
DATA_PAGE: Final[str] = "batch_page"
DATA_TARGET: Final[str] = "batch_target_folder_id"

#: Все ключи раскладки — используются при сбросе состояния.
DATA_KEYS: Final[tuple[str, ...]] = (
    DATA_TRACK_IDS,
    DATA_ASSIGNED,
    DATA_CHAT_ID,
    DATA_MESSAGE_ID,
    DATA_PAGE,
    DATA_TARGET,
)

#: Сколько названий треков показывать в описании текущей группы.
PREVIEW_TRACKS: Final[int] = 5

#: Максимальная длина названия новой папки (как в `handlers/upload.py`).
MAX_FOLDER_NAME_LENGTH: Final[int] = 64

#: Сколько папок показывать в итоговой сводке.
MAX_SUMMARY_FOLDERS: Final[int] = 12

#: Запас до лимита Telegram в 4096 символов на сообщение.
SAFE_MESSAGE_LENGTH: Final[int] = 3800

#: Действия `BatchCB` этого раздела.
ACTION_PICK: Final[str] = "pick"
ACTION_NEW: Final[str] = "new"
ACTION_COUNT: Final[str] = "count"
ACTION_ALL: Final[str] = "all"
ACTION_SKIP: Final[str] = "skip"
ACTION_DONE: Final[str] = "done"
ACTION_CANCEL: Final[str] = "cancel"
ACTION_PAGE: Final[str] = "page"


# ---------------------------------------------------------------------------
# Русские тексты раздела
# ---------------------------------------------------------------------------

BATCH_HEADER = "📦 <b>Раскладка загруженного</b>"
BATCH_GROUP = "Группа: <b>{artist}</b> — {count}"
BATCH_QUESTION = "Куда положить треки этой группы?"
BATCH_NEW_FROM_ARTIST = "🆕 Новая папка «{artist}»"
BATCH_NEW_CUSTOM = "✏️ Новая папка со своим названием"
BATCH_SKIP_GROUP = "⏭ Пропустить группу"
BATCH_LEAVE_REST = "🚫 Оставить остальные без папки"
BATCH_TARGET = "Папка: <b>{folder}</b>"
BATCH_HOW_MANY = "Сколько треков из группы добавить?"
BATCH_ALL_BUTTON = "✅ Добавить все ({count})"
BATCH_COUNT_BUTTON = "🔢 Указать количество"
BATCH_BACK_BUTTON = "⬅️ Выбрать другую папку"
BATCH_COUNT_PROMPT = (
    "🔢 Сколько треков из группы <b>{artist}</b> положить в папку <b>{folder}</b>?\n"
    "Отправьте число от 1 до {total}."
)
BATCH_NAME_PROMPT = (
    "📁 Введите название новой папки (до {limit} символов).\n"
    "Туда уйдут треки группы <b>{artist}</b>."
)
BATCH_CANCEL_HINT = "Чтобы прервать раскладку, отправьте /start."
BATCH_MOVED = "✅ В папку «{folder}» добавлено {count}."
BATCH_GROUP_SKIPPED = "⏭ Группа «{artist}» осталась без папки."
BATCH_REST_LEFT = "🚫 Остальные треки остались без папки."
BATCH_DONE_HEADER = "✅ Готово: разложил {count}."
BATCH_DONE_NO_FOLDER = "🚫 Без папки — {count}"
BATCH_DONE_FOLDER = "📁 {folder} — {count}"
BATCH_DONE_HINT = "Разобрать треки по папкам можно в любой момент командой /folders."
BATCH_ALL_SORTED = (
    "✅ Готово: {count} загружено и разложено по папкам исполнителей.\n"
    "Посмотреть результат — /folders."
)
BATCH_NOTHING = "📦 Распределять нечего: треки пачки уже разложены."
BATCH_EXPIRED = "Раскладка устарела — начните загрузку заново."
BATCH_GONE = "🤷 Треки пачки не найдены — возможно, их уже удалили."
BATCH_FOLDER_GONE = "🤷 Папка не найдена — выберите другую."
BATCH_CANCELLED = "Раскладка отменена. Треки остались в библиотеке."

#: Формы слова «папка» для сводки.
FOLDER_FORMS: Final[tuple[str, str, str]] = ("папка", "папки", "папок")


# ---------------------------------------------------------------------------
# Работа с состоянием раскладки в FSM
# ---------------------------------------------------------------------------


def _int_or(value: Any, default: int = 0) -> int:
    """Безопасное приведение значения FSM-данных к целому числу."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _trim(text: str) -> str:
    """Обрезает текст до безопасной длины сообщения Telegram."""
    if len(text) <= SAFE_MESSAGE_LENGTH:
        return text
    logger.debug("Текст раскладки обрезан: %s символов", len(text))
    return text[: SAFE_MESSAGE_LENGTH - 1].rstrip() + "…"


def _load_state(user_id: int, data: dict[str, Any]) -> BatchState | None:
    """Собирает `BatchState` из FSM-данных; None — раскладки нет."""
    raw_ids = data.get(DATA_TRACK_IDS) or []
    if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
        return None
    raw_assigned = data.get(DATA_ASSIGNED) or {}
    if not isinstance(raw_assigned, dict):
        logger.warning("Некорректная карта раскладки в FSM пользователя %s", user_id)
        raw_assigned = {}
    return BatchState(user_id=int(user_id), track_ids=list(raw_ids), assigned=dict(raw_assigned))


async def _store_state(state: FSMContext, batch: BatchState) -> None:
    """Сохраняет состояние раскладки в FSM (ключи словаря — строки)."""
    await state.update_data(
        **{
            DATA_TRACK_IDS: list(batch.track_ids),
            DATA_ASSIGNED: {str(key): value for key, value in batch.assigned.items()},
        }
    )


async def _reset_state(state: FSMContext) -> None:
    """Очищает только свои ключи раскладки, не трогая чужие FSM-данные."""
    await state.set_state(None)
    await state.update_data(
        **{
            DATA_TRACK_IDS: [],
            DATA_ASSIGNED: {},
            DATA_MESSAGE_ID: 0,
            DATA_PAGE: 1,
            DATA_TARGET: 0,
        }
    )


# ---------------------------------------------------------------------------
# Отправка и правка экрана раскладки
# ---------------------------------------------------------------------------


async def _show(
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Правит сообщение раскладки, а если это невозможно — отправляет новое."""
    payload = _trim(text)
    data = await state.get_data()
    message_id = _int_or(data.get(DATA_MESSAGE_ID))

    if message_id > 0:
        try:
            await bot.edit_message_text(
                text=payload,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                logger.debug("Экран раскладки не изменился — правка пропущена")
                return
            logger.debug("Не удалось отредактировать экран раскладки: %s", exc)
        except TelegramAPIError as exc:
            logger.warning("Ошибка Telegram при правке экрана раскладки: %s", exc)

    try:
        sent = await bot.send_message(chat_id, payload, reply_markup=markup)
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить экран раскладки пользователю: %s", exc)
        return
    await state.update_data(**{DATA_MESSAGE_ID: int(sent.message_id), DATA_CHAT_ID: int(chat_id)})


# ---------------------------------------------------------------------------
# Текущая группа треков
# ---------------------------------------------------------------------------


async def _artist_name(user_id: int, artist_id: int | None, track_ids: Sequence[int]) -> str:
    """Имя исполнителя группы: из справочника, иначе из самого трека."""
    if artist_id is not None:
        try:
            artist = await artists_repo.get_artist(user_id, int(artist_id))
        except MusicBoxError as error:
            logger.warning("Не удалось прочитать исполнителя %s: %s", artist_id, error)
            artist = None
        name = str((artist or {}).get("name") or "").strip()
        if name:
            return name

    tracks = await tracks_repo.get_tracks_by_ids(user_id, list(track_ids)[:1])
    if tracks:
        name = str(tracks[0].get("artist") or "").strip()
        if name:
            return name
    return batch_sort.NO_ARTIST_LABEL


async def _current_group(
    user_id: int, batch: BatchState
) -> tuple[int | None, list[int], str] | None:
    """Первая нераспределённая группа: `(artist_id, track_ids, имя исполнителя)`."""
    groups = await batch_sort.group_by_artist(user_id, batch.pending)
    if not groups:
        return None
    artist_id, track_ids = next(iter(groups.items()))
    name = await _artist_name(user_id, artist_id, track_ids)
    return artist_id, list(track_ids), name


# ---------------------------------------------------------------------------
# Клавиатуры (локальные — экраны раскладки есть только в этом модуле)
# ---------------------------------------------------------------------------


def _folder_label(folder: dict[str, Any]) -> str:
    """Подпись папки в клавиатуре: отступ по вложенности + число треков."""
    depth = max(_int_or(folder.get("depth")), 0)
    indent = "· " * min(depth, 4)
    name = str(folder.get("name") or "Без названия")
    count = _int_or(folder.get("total_track_count"), _int_or(folder.get("track_count")))
    label = f"📁 {indent}{name}"
    if count > 0:
        label += f" ({count})"
    return label[:64]


def _pick_kb(
    folders: Sequence[dict[str, Any]],
    *,
    page: int,
    total_pages: int,
    artist: str,
    has_artist: bool,
) -> InlineKeyboardMarkup:
    """Экран выбора папки для текущей группы."""
    builder = InlineKeyboardBuilder()

    if has_artist:
        builder.row(
            InlineKeyboardButton(
                text=BATCH_NEW_FROM_ARTIST.format(artist=artist)[:64],
                callback_data=BatchCB(
                    action=ACTION_NEW, folder_id=FOLDER_NEW, count=0
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=BATCH_NEW_CUSTOM,
            callback_data=BatchCB(action=ACTION_NEW, folder_id=FOLDER_NONE, count=0).pack(),
        )
    )

    for folder in folders:
        builder.row(
            InlineKeyboardButton(
                text=_folder_label(folder),
                callback_data=BatchCB(
                    action=ACTION_PICK, folder_id=_int_or(folder.get("id")), count=0
                ).pack(),
            )
        )

    if total_pages > 1:
        previous = page - 1 if page > 1 else total_pages
        following = page + 1 if page < total_pages else 1
        builder.row(
            InlineKeyboardButton(
                text="◀️",
                callback_data=BatchCB(action=ACTION_PAGE, folder_id=0, count=previous).pack(),
            ),
            InlineKeyboardButton(
                text=texts.PAGE_LABEL.format(page=page, total=total_pages),
                callback_data=NavCB(action="noop").pack(),
            ),
            InlineKeyboardButton(
                text="▶️",
                callback_data=BatchCB(action=ACTION_PAGE, folder_id=0, count=following).pack(),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text=BATCH_SKIP_GROUP,
            callback_data=BatchCB(action=ACTION_SKIP, folder_id=FOLDER_NONE, count=0).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=BATCH_LEAVE_REST,
            callback_data=BatchCB(action=ACTION_DONE, folder_id=FOLDER_NONE, count=0).pack(),
        )
    )
    return builder.as_markup()


def _amount_kb(folder_id: int, total: int) -> InlineKeyboardMarkup:
    """Экран «сколько треков положить в выбранную папку»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=BATCH_ALL_BUTTON.format(count=total),
            callback_data=BatchCB(action=ACTION_ALL, folder_id=int(folder_id), count=0).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=BATCH_COUNT_BUTTON,
            callback_data=BatchCB(action=ACTION_COUNT, folder_id=int(folder_id), count=0).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=BATCH_BACK_BUTTON,
            callback_data=BatchCB(action=ACTION_CANCEL, folder_id=0, count=0).pack(),
        )
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Тексты экранов
# ---------------------------------------------------------------------------


async def _group_block(user_id: int, artist: str, track_ids: Sequence[int]) -> str:
    """Заголовок группы и первые названия треков в ней."""
    lines = [BATCH_GROUP.format(artist=escape(artist), count=tracks_count_label(len(track_ids)))]
    preview = await tracks_repo.get_tracks_by_ids(user_id, list(track_ids)[:PREVIEW_TRACKS])
    for track in preview:
        lines.append(f"• {escape(track.get('title') or 'Без названия')}")
    hidden = len(track_ids) - len(preview)
    if hidden > 0:
        lines.append(f"…и ещё {tracks_count_label(hidden)}")
    return "\n".join(lines)


async def _pick_text(
    user_id: int,
    batch: BatchState,
    artist: str,
    track_ids: Sequence[int],
    notice: str | None,
) -> str:
    """Экран выбора папки: что уже сделано, что осталось и текущая группа."""
    parts = [BATCH_HEADER]
    if notice:
        parts.append(notice)
    parts.append(await batch_sort.remaining_summary(user_id, batch))
    parts.append(await _group_block(user_id, artist, track_ids))
    parts.append(BATCH_QUESTION)
    return "\n\n".join(part for part in parts if part)


async def _final_text(user_id: int, batch: BatchState, notice: str | None = None) -> str:
    """Итоговая сводка: сколько треков и в какие папки ушло."""
    by_folder = batch.by_folder()
    names: dict[int, str] = {}
    for folder_id in by_folder:
        if folder_id is None:
            continue
        folder = await folders_repo.get_folder(user_id, int(folder_id))
        names[int(folder_id)] = str((folder or {}).get("name") or "").strip() or "удалённая папка"

    ordered = sorted(
        by_folder.items(),
        key=lambda item: (item[0] is None, names.get(item[0] or 0, "").casefold()),
    )

    lines = [BATCH_DONE_HEADER.format(count=tracks_count_label(batch.total))]
    if notice:
        lines.append(notice)
    for folder_id, ids in ordered[:MAX_SUMMARY_FOLDERS]:
        label = tracks_count_label(len(ids))
        if folder_id is None:
            lines.append(BATCH_DONE_NO_FOLDER.format(count=label))
        else:
            lines.append(
                BATCH_DONE_FOLDER.format(folder=escape(names[int(folder_id)]), count=label)
            )
    hidden = len(ordered) - MAX_SUMMARY_FOLDERS
    if hidden > 0:
        lines.append(f"…и ещё {plural(hidden, FOLDER_FORMS)}")
    lines.append(BATCH_DONE_HINT)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Отрисовка очередного шага
# ---------------------------------------------------------------------------


async def _render(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    batch: BatchState,
    *,
    notice: str | None = None,
    page: int = 1,
) -> None:
    """Показывает следующий шаг раскладки либо итоговую сводку."""
    await _store_state(state, batch)

    if batch.is_complete:
        await _show(bot, state, chat_id, await _final_text(user_id, batch, notice), None)
        await _reset_state(state)
        return

    group = await _current_group(user_id, batch)
    if group is None:
        # Треки пачки исчезли из библиотеки между шагами — раскладывать нечего.
        logger.info("Пользователь %s: нераспределённые треки пачки не найдены", user_id)
        batch.mark_assigned(list(batch.pending), None)
        await _store_state(state, batch)
        await _show(bot, state, chat_id, await _final_text(user_id, batch, BATCH_GONE), None)
        await _reset_state(state)
        return

    artist_id, track_ids, artist = group
    folders = await folders_repo.list_folders(user_id, section="music")
    items, total_pages = paginate(folders, page)
    current_page = min(max(int(page), 1), total_pages)
    await state.update_data(**{DATA_PAGE: current_page, DATA_CHAT_ID: int(chat_id)})

    text = await _pick_text(user_id, batch, artist, track_ids, notice)
    markup = _pick_kb(
        items,
        page=current_page,
        total_pages=total_pages,
        artist=artist,
        has_artist=artist_id is not None,
    )
    await _show(bot, state, chat_id, text, markup)


# ---------------------------------------------------------------------------
# Точка входа: вызывается из handlers/upload.py
# ---------------------------------------------------------------------------


async def start_batch(
    bot: Bot,
    state: FSMContext,
    *,
    user_id: int,
    chat_id: int,
    track_ids: Sequence[int],
) -> None:
    """Запускает раскладку загруженной пачки треков.

    Треки, которые уже лежат в папке (например, их разложила автосортировка),
    считаются распределёнными и в диалог не попадают; пропавшие из библиотеки —
    тоже. Если распределять нечего, пользователь получает короткую сводку.
    """
    wanted = [int(value) for value in track_ids if _int_or(value) > 0]
    if not wanted:
        logger.debug("Пользователь %s: пустая пачка — раскладка не нужна", user_id)
        return

    tracks = await tracks_repo.get_tracks_by_ids(user_id, wanted)
    known = {int(track["id"]): track for track in tracks}

    assigned: dict[int, int | None] = {}
    for track_id in wanted:
        track = known.get(track_id)
        if track is None:
            assigned[track_id] = None  # трек уже удалён — в диалоге он не нужен
            continue
        folder_id = track.get("folder_id")
        if folder_id is not None:
            assigned[track_id] = int(folder_id)

    batch = BatchState(user_id=int(user_id), track_ids=wanted, assigned=assigned)
    await state.set_state(None)
    await state.update_data(**{DATA_MESSAGE_ID: 0, DATA_PAGE: 1, DATA_TARGET: 0})

    logger.info(
        "Пользователь %s: раскладка пачки из %s треков, нераспределённых %s",
        user_id,
        batch.total,
        batch.remaining,
    )

    if batch.is_complete:
        await _store_state(state, batch)
        text = (
            BATCH_ALL_SORTED.format(count=tracks_count_label(batch.total))
            if any(value is not None for value in batch.assigned.values())
            else BATCH_NOTHING
        )
        await _show(bot, state, chat_id, text, None)
        await _reset_state(state)
        return

    await _render(bot, state, user_id, chat_id, batch, page=1)


# ---------------------------------------------------------------------------
# Общие помощники хендлеров
# ---------------------------------------------------------------------------


async def _require_batch(
    callback: CallbackQuery, state: FSMContext
) -> tuple[int, int, BatchState] | None:
    """Достаёт `(user_id, chat_id, BatchState)` или сообщает, что раскладка устарела."""
    if callback.from_user is None:
        await ack(callback)
        return None

    user_id = int(callback.from_user.id)
    data = await state.get_data()
    batch = _load_state(user_id, data)
    if batch is None:
        await ack(callback, BATCH_EXPIRED, alert=True)
        message = callback.message
        if isinstance(message, Message):
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramAPIError as exc:
                logger.debug("Не удалось снять клавиатуру устаревшей раскладки: %s", exc)
        return None

    chat_id = _int_or(data.get(DATA_CHAT_ID))
    if chat_id == 0:
        message = callback.message
        chat = getattr(message, "chat", None)
        chat_id = _int_or(getattr(chat, "id", None), user_id)
    return user_id, chat_id, batch


async def _assign_group(
    user_id: int,
    batch: BatchState,
    folder_id: int,
    track_ids: Sequence[int],
    limit: Any = None,
) -> tuple[int, str]:
    """Переносит треки группы в папку и помечает их распределёнными.

    Возвращает `(сколько_перенесено, имя_папки)`. Бросает `ValidationError`
    (некорректное количество) и `NotFoundError` (папка исчезла).
    """
    selected = batch_sort.select_batch(track_ids, limit)
    if not selected:
        return 0, ""

    await batch_sort.assign(user_id, selected, int(folder_id))
    marked = batch.mark_assigned(selected, int(folder_id))
    folder = await folders_repo.get_folder(user_id, int(folder_id))
    name = str((folder or {}).get("name") or "").strip() or "без названия"
    logger.info(
        "Пользователь %s: в папку «%s» (id=%s) ушло %s треков пачки",
        user_id,
        name,
        folder_id,
        len(marked),
    )
    return len(marked), name


# ---------------------------------------------------------------------------
# Callback-хендлеры
# ---------------------------------------------------------------------------


@router.callback_query(BatchCB.filter(F.action == ACTION_PAGE))
async def on_page(
    callback: CallbackQuery, callback_data: BatchCB, state: FSMContext, bot: Bot
) -> None:
    """Листает список существующих папок."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found
    await _render(bot, state, user_id, chat_id, batch, page=max(callback_data.count, 1))
    await ack(callback)


@router.callback_query(BatchCB.filter(F.action == ACTION_CANCEL))
async def on_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Возврат с экрана «сколько треков» к выбору папки."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found
    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(**{DATA_TARGET: 0})
    await _render(bot, state, user_id, chat_id, batch, page=_int_or(data.get(DATA_PAGE), 1))
    await ack(callback)


@router.callback_query(BatchCB.filter(F.action == ACTION_SKIP))
async def on_skip(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Оставляет текущую группу без папки и переходит к следующей."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    group = await _current_group(user_id, batch)
    if group is None:
        await _render(bot, state, user_id, chat_id, batch)
        await ack(callback)
        return

    _artist_id, track_ids, artist = group
    batch.mark_assigned(track_ids, None)
    logger.info("Пользователь %s: группа «%s» пропущена в раскладке", user_id, artist)
    await _render(
        bot,
        state,
        user_id,
        chat_id,
        batch,
        notice=BATCH_GROUP_SKIPPED.format(artist=escape(artist)),
    )
    await ack(callback, "Пропущено")


@router.callback_query(BatchCB.filter(F.action == ACTION_DONE))
async def on_done(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«Оставить остальные без папки» — завершает раскладку досрочно."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    left = batch.remaining
    batch.mark_assigned(list(batch.pending), None)
    logger.info("Пользователь %s: раскладка завершена, без папки осталось %s", user_id, left)
    await _render(bot, state, user_id, chat_id, batch, notice=BATCH_REST_LEFT)
    await ack(callback, "Готово")


@router.callback_query(BatchCB.filter(F.action == ACTION_PICK))
async def on_pick(
    callback: CallbackQuery, callback_data: BatchCB, state: FSMContext, bot: Bot
) -> None:
    """Выбрана существующая папка — спрашиваем, сколько треков в неё положить."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    folder_id = int(callback_data.folder_id)
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        await ack(callback, BATCH_FOLDER_GONE, alert=True)
        await _render(bot, state, user_id, chat_id, batch)
        return

    await _ask_amount(bot, state, user_id, chat_id, batch, folder_id, str(folder.get("name") or ""))
    await ack(callback)


@router.callback_query(BatchCB.filter(F.action == ACTION_NEW))
async def on_new(
    callback: CallbackQuery, callback_data: BatchCB, state: FSMContext, bot: Bot
) -> None:
    """Создание новой папки: с именем исполнителя либо со своим названием."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    group = await _current_group(user_id, batch)
    if group is None:
        await _render(bot, state, user_id, chat_id, batch)
        await ack(callback)
        return

    artist_id, track_ids, artist = group
    if int(callback_data.folder_id) == FOLDER_NEW and artist_id is not None:
        try:
            folder = await folders_repo.create_folder(user_id, artist, is_artist_folder=True)
        except ValidationError as error:
            await ack(callback, str(error), alert=True)
            return
        except MusicBoxError as error:
            logger.warning("Пользователь %s: не удалось создать папку: %s", user_id, error)
            await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
            return
        await _ask_amount(
            bot, state, user_id, chat_id, batch, int(folder["id"]), str(folder.get("name") or "")
        )
        await ack(callback)
        return

    # Своё название папки (и единственный вариант для группы без исполнителя).
    await state.set_state(BatchStates.waiting_folder_name)
    await state.update_data(**{DATA_TARGET: 0, DATA_CHAT_ID: int(chat_id)})
    text = (
        f"{BATCH_NAME_PROMPT.format(limit=MAX_FOLDER_NAME_LENGTH, artist=escape(artist))}\n"
        f"{BATCH_CANCEL_HINT}"
    )
    await _show(bot, state, chat_id, text, None)
    logger.debug("Пользователь %s: запрошено название новой папки для пачки", user_id)
    await ack(callback)


@router.callback_query(BatchCB.filter(F.action == ACTION_ALL))
async def on_all(
    callback: CallbackQuery, callback_data: BatchCB, state: FSMContext, bot: Bot
) -> None:
    """Кладёт в выбранную папку всю текущую группу."""
    await _apply_amount(callback, state, bot, int(callback_data.folder_id), limit=None)


@router.callback_query(BatchCB.filter(F.action == ACTION_COUNT))
async def on_count(
    callback: CallbackQuery, callback_data: BatchCB, state: FSMContext, bot: Bot
) -> None:
    """Спрашивает количество треков для добавления в выбранную папку."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    folder_id = int(callback_data.folder_id)
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        await ack(callback, BATCH_FOLDER_GONE, alert=True)
        await _render(bot, state, user_id, chat_id, batch)
        return

    group = await _current_group(user_id, batch)
    if group is None:
        await _render(bot, state, user_id, chat_id, batch)
        await ack(callback)
        return
    _artist_id, track_ids, artist = group

    await state.set_state(BatchStates.waiting_count)
    await state.update_data(**{DATA_TARGET: folder_id, DATA_CHAT_ID: int(chat_id)})
    text = (
        BATCH_COUNT_PROMPT.format(
            artist=escape(artist),
            folder=escape(str(folder.get("name") or "")),
            total=len(track_ids),
        )
        + f"\n{BATCH_CANCEL_HINT}"
    )
    await _show(bot, state, chat_id, text, None)
    await ack(callback)


async def _ask_amount(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    batch: BatchState,
    folder_id: int,
    folder_name: str,
) -> None:
    """Экран «сколько треков положить»; для группы из одного трека — сразу перенос."""
    group = await _current_group(user_id, batch)
    if group is None:
        await _render(bot, state, user_id, chat_id, batch)
        return
    _artist_id, track_ids, artist = group

    if len(track_ids) == 1:
        await _move_and_render(bot, state, user_id, chat_id, batch, folder_id, track_ids, None)
        return

    await state.set_state(None)
    await state.update_data(**{DATA_TARGET: int(folder_id), DATA_CHAT_ID: int(chat_id)})
    text = "\n\n".join(
        [
            BATCH_HEADER,
            BATCH_TARGET.format(folder=escape(folder_name)),
            await _group_block(user_id, artist, track_ids),
            BATCH_HOW_MANY,
        ]
    )
    await _show(bot, state, chat_id, text, _amount_kb(folder_id, len(track_ids)))


async def _apply_amount(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    folder_id: int,
    *,
    limit: Any,
) -> None:
    """Общий путь для кнопки «Добавить все» и введённого числа."""
    found = await _require_batch(callback, state)
    if found is None:
        return
    user_id, chat_id, batch = found

    group = await _current_group(user_id, batch)
    if group is None:
        await _render(bot, state, user_id, chat_id, batch)
        await ack(callback)
        return

    _artist_id, track_ids, _artist = group
    moved = await _move_and_render(
        bot, state, user_id, chat_id, batch, folder_id, track_ids, limit
    )
    await ack(callback, "Готово" if moved else None)


async def _move_and_render(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    batch: BatchState,
    folder_id: int,
    track_ids: Sequence[int],
    limit: Any,
) -> bool:
    """Переносит треки в папку и показывает следующий шаг; False — перенести не вышло."""
    await state.set_state(None)
    await state.update_data(**{DATA_TARGET: 0})
    try:
        moved, folder_name = await _assign_group(user_id, batch, folder_id, track_ids, limit)
    except NotFoundError as error:
        logger.info("Пользователь %s: папка пачки исчезла: %s", user_id, error)
        await _render(bot, state, user_id, chat_id, batch, notice=BATCH_FOLDER_GONE)
        return False
    except ValidationError as error:
        await _render(bot, state, user_id, chat_id, batch, notice=f"❌ {escape(str(error))}")
        return False
    except MusicBoxError as error:
        logger.warning("Пользователь %s: сбой раскладки пачки: %s", user_id, error)
        await _render(bot, state, user_id, chat_id, batch, notice=texts.ERROR_TRY_AGAIN)
        return False

    notice = (
        BATCH_MOVED.format(folder=escape(folder_name), count=tracks_count_label(moved))
        if moved
        else None
    )
    await _render(bot, state, user_id, chat_id, batch, notice=notice)
    return moved > 0


# ---------------------------------------------------------------------------
# FSM: ввод количества треков и названия новой папки
# ---------------------------------------------------------------------------


async def _abort(state: FSMContext, message: Message, text: str) -> None:
    """Прерывает ввод: сбрасывает состояние раскладки и отвечает пользователю."""
    await _reset_state(state)
    await message.answer(text)


@router.message(StateFilter(BatchStates.waiting_count), F.text)
async def on_count_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Принимает количество треков, которые нужно положить в выбранную папку."""
    if message.from_user is None:
        return

    user_id = int(message.from_user.id)
    text = (message.text or "").strip()
    if text.startswith("/"):
        await _abort(state, message, BATCH_CANCELLED)
        return

    data = await state.get_data()
    batch = _load_state(user_id, data)
    chat_id = _int_or(data.get(DATA_CHAT_ID), int(message.chat.id))
    folder_id = _int_or(data.get(DATA_TARGET))
    if batch is None or folder_id <= 0:
        await _abort(state, message, BATCH_EXPIRED)
        return

    group = await _current_group(user_id, batch)
    if group is None:
        await state.set_state(None)
        await _render(bot, state, user_id, chat_id, batch)
        return

    _artist_id, track_ids, _artist = group
    try:
        selected = batch_sort.select_batch(track_ids, text)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}")
        return
    if not selected:
        await message.answer("❌ Нечего добавлять: укажите число больше нуля.")
        return

    await _move_and_render(
        bot, state, user_id, chat_id, batch, folder_id, track_ids, len(selected)
    )


@router.message(StateFilter(BatchStates.waiting_folder_name), F.text)
async def on_folder_name_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Создаёт папку с введённым названием и предлагает выбрать количество треков."""
    if message.from_user is None:
        return

    user_id = int(message.from_user.id)
    name = (message.text or "").strip()
    if name.startswith("/"):
        await _abort(state, message, BATCH_CANCELLED)
        return
    if not name:
        await message.answer(texts.NAME_EMPTY)
        return
    if len(name) > MAX_FOLDER_NAME_LENGTH:
        await message.answer(texts.NAME_TOO_LONG)
        return

    data = await state.get_data()
    batch = _load_state(user_id, data)
    chat_id = _int_or(data.get(DATA_CHAT_ID), int(message.chat.id))
    if batch is None:
        await _abort(state, message, BATCH_EXPIRED)
        return

    try:
        folder = await folders_repo.create_folder(user_id, name)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось создать папку «%s»: %s", user_id, name, error)
        await _abort(state, message, texts.ERROR_TRY_AGAIN)
        return

    await state.set_state(None)
    logger.info(
        "Пользователь %s: для пачки создана папка «%s» (id=%s)",
        user_id,
        folder.get("name"),
        folder.get("id"),
    )
    await _ask_amount(
        bot, state, user_id, chat_id, batch, int(folder["id"]), str(folder.get("name") or name)
    )


__all__ = ["router", "start_batch"]
