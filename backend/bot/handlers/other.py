"""Раздел «Другое»: документы, видео, кружочки и голосовые (ТЗ п. 13, контракт V2).

Модуль отвечает за две связанные задачи.

**Просмотр раздела** — команда ``/other`` и колбэки :class:`OtherCB`:
навигация по вложенным папкам раздела ``section='other'`` (как в папках музыки),
список файлов с ``file_type != 'audio'``, карточка файла с отправкой пользователю
через :func:`backend.services.media.send_media_to_user`, перенос файла в папку,
создание подпапок, удаление файла (вместе с копией в канале-хранилище) и папки.

**Приём не-аудио сообщений** — документы (кроме аудио-документов), видео,
видеосообщения («кружочки») и голосовые. Файл копируется в приватный канал через
:func:`backend.services.media.store_media`, затем бот спрашивает название
(FSM :class:`OtherSectionStates.waiting_title`) и папку раздела «Другое»
(существующую или новую, FSM :class:`OtherSectionStates.waiting_folder_name`).

ВАЖНО: аудио здесь НЕ перехватывается — им занимается ``handlers/upload.py``.
Документ с аудио внутри (mp3, присланный файлом) отсеивается фильтром
:func:`_is_other_document`, поэтому попадает в раздел «Треки», а не сюда.

Клавиатуры и тексты этого раздела объявлены локально: общие модули
``backend/bot/keyboards.py`` и ``backend/bot/texts.py`` этот раздел не описывают.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any, Final, Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import FOLDER_NEW, FOLDER_NONE, EditCB, NavCB, OtherCB
from backend.bot.keyboards import NOOP, confirm_kb
from backend.bot.utils import ack, escape, format_duration, page_offset, plural, safe_edit
from backend.config import settings
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import FileTooLargeError, MusicBoxError, StorageError, ValidationError
from backend.services import media, storage

logger = logging.getLogger(__name__)

router = Router(name="other")


# ---------------------------------------------------------------------------
# Константы раздела
# ---------------------------------------------------------------------------

#: Раздел папок, в котором живут не-аудио файлы (колонка ``folders.section``).
SECTION: Final[str] = "other"

#: Типы файлов раздела: всё, кроме аудио.
OTHER_FILE_TYPES: Final[tuple[str, ...]] = tuple(
    file_type for file_type in media.FILE_TYPES if file_type != media.AUDIO_FILE_TYPE
)

#: Псевдо-идентификатор корня раздела в ``OtherCB.folder_id`` (0 = «без папки»).
ROOT_ID: Final[int] = 0

#: Ограничения ввода и вёрстки.
MAX_FOLDER_NAME_LENGTH: Final[int] = 64
MAX_TITLE_LENGTH: Final[int] = 128
MAX_PICK_FOLDERS: Final[int] = 40
MAX_LINE_NAME: Final[int] = 64
LABEL_LIMIT: Final[int] = 24
FILE_BUTTONS_PER_ROW: Final[int] = 5
CRUMBS_LIMIT: Final[int] = 4

#: Размер страницы по умолчанию, если в настройках лежит мусор.
DEFAULT_PAGE_SIZE: Final[int] = 10

#: Собственные callback-данные модуля (фабрикой :class:`OtherCB` не разбираются).
DELETE_FILE_PREFIX: Final[str] = "otdf"
DELETE_FOLDER_PREFIX: Final[str] = "otdd"
MOVE_PREFIX: Final[str] = "otmv"
KEEP_TITLE_PREFIX: Final[str] = "otkt"

#: Собственные ключи FSM-данных раздела. FSM общий на весь бот (одно хранилище на
#: пару «чат + пользователь»), поэтому свои данные держим под своими именами и
#: чистим ТОЛЬКО их: ``state.clear()`` стёр бы чужие диалоги — буфер пачки загрузки
#: (``handlers/upload.py``) и раскладку по папкам (``handlers/batch.py``).
DATA_TRACK_ID: Final[str] = "other_track_id"
DATA_PARENT_ID: Final[str] = "other_parent_id"
DATA_PAGE: Final[str] = "other_page"
#: Сообщение с последним приглашением ввести название («нижнее» на экране).
DATA_PROMPT_ID: Final[str] = "other_prompt_id"

#: Пустые значения ключей раздела — ими гасим свой диалог.
_DIALOG_DEFAULTS: Final[dict[str, int]] = {
    DATA_TRACK_ID: 0,
    DATA_PARENT_ID: ROOT_ID,
    DATA_PAGE: 1,
    DATA_PROMPT_ID: 0,
}

#: Замок на свои ключи FSM: aiogram обрабатывает сообщения параллельно, и без
#: синхронизации два файла одной пачки затёрли бы записи друг друга (так же
#: устроены замки пачки в ``handlers/upload.py``). Под замком только работа с
#: хранилищем состояния — сеть и загрузка файлов остаются снаружи.
_STATE_LOCK: Final[asyncio.Lock] = asyncio.Lock()


# ---------------------------------------------------------------------------
# Тексты раздела (русские, HTML-разметка Telegram)
# ---------------------------------------------------------------------------

SECTION_ICON: Final[str] = "📦"
SECTION_NAME: Final[str] = "Другое"
SECTION_TITLE: Final[str] = f"{SECTION_ICON} <b>{SECTION_NAME}</b>"

EMPTY_SECTION: Final[str] = (
    "Здесь пока пусто.\n"
    "Пришлите мне документ, видео, кружочек или голосовое — я сохраню файл "
    "в приватном канале, спрошу название и папку."
)
EMPTY_FOLDER: Final[str] = (
    "В этой папке пока нет файлов и подпапок.\n"
    "Можно создать подпапку кнопкой ниже или прислать мне файл."
)
FOLDER_NAME_PROMPT: Final[str] = (
    f"📁 Введите название новой папки раздела «{SECTION_NAME}» "
    f"(до {MAX_FOLDER_NAME_LENGTH} символов)."
)
TITLE_PROMPT: Final[str] = "✍️ Как назвать файл? Отправьте название или оставьте текущее."
TITLE_PROMPT_BUSY: Final[str] = (
    "✍️ Назовите этот файл кнопками ниже: отправленное текстом название "
    "относится к файлу из последнего сообщения."
)
TITLE_TOO_LONG: Final[str] = (
    f"✍️ Слишком длинное название. Уложитесь в {MAX_TITLE_LENGTH} символов, пожалуйста."
)
SAVING_STATUS: Final[str] = "⏳ Сохраняю файл…"
FILE_SAVED: Final[str] = "✅ Файл сохранён в хранилище."
FILE_NOT_FOUND: Final[str] = "🤷 Файл не найден — возможно, он уже удалён."
FILE_SENT: Final[str] = "📤 Отправил файл."
FILE_DELETED: Final[str] = "🗑 Файл удалён из библиотеки и из канала-хранилища."
FILE_MOVED: Final[str] = "📁 Файл перемещён в папку «{folder}»."
FILE_MOVED_TO_ROOT: Final[str] = "📁 Файл убран из папки — он остался в разделе «Другое»."
FOLDER_CREATED: Final[str] = "📁 Папка «{folder}» создана."
FOLDER_DELETED: Final[str] = "🗑 Папка «{folder}» удалена. Файлы остались в разделе."
DUPLICATE_FILE: Final[str] = "📦 Этот файл уже есть в разделе «Другое» — второй раз не сохраняю."
CHOOSE_FOLDER: Final[str] = "📂 Выберите папку раздела «{section}» для файла «<b>{title}</b>»."
PICK_LIMIT_HINT: Final[str] = (
    "Показаны первые {limit} папок — остальные доступны в Mini App."
)
CONFIRM_DELETE_FILE: Final[str] = (
    "🗑 Удалить файл «<b>{title}</b>»?\n"
    "Он пропадёт и из канала-хранилища — отменить это будет нельзя."
)
CONFIRM_DELETE_FOLDER: Final[str] = (
    "🗑 Удалить папку «<b>{folder}</b>» вместе с подпапками?\n"
    "Файлы не пропадут — они останутся в разделе «Другое» без папки."
)
CANCELLED_UPLOAD: Final[str] = (
    "Готово, отменил. 👌 Файл остался в разделе «Другое» — найдёте его командой /other."
)


# ---------------------------------------------------------------------------
# FSM раздела (объявлены локально: backend/bot/states.py этот раздел не описывает)
# ---------------------------------------------------------------------------


class OtherSectionStates(StatesGroup):
    """Диалоги раздела «Другое»."""

    #: Ожидание названия только что загруженного файла.
    waiting_title = State()
    #: Ожидание названия новой папки раздела (создание из списка или при загрузке).
    waiting_folder_name = State()


# ---------------------------------------------------------------------------
# Мелкие помощники
# ---------------------------------------------------------------------------


def _per_page() -> int:
    """Размер страницы из настроек с защитой от некорректных значений."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        logger.warning("Некорректный page_size в настройках, использую %s", DEFAULT_PAGE_SIZE)
        return DEFAULT_PAGE_SIZE
    return value if value > 0 else DEFAULT_PAGE_SIZE


def _to_int(value: Any, default: int = 0) -> int:
    """Мягкое приведение к int (значения приходят из БД и callback-данных)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_page(value: Any) -> int:
    """Номер страницы (1-based) из callback-данных."""
    page = _to_int(value, 1)
    return page if page > 0 else 1


def _total_pages(total: int, per_page: int) -> int:
    """Количество страниц (минимум 1)."""
    if total <= 0:
        return 1
    return max(1, math.ceil(total / per_page))


def _clamp_page(page: Any, total_pages: int) -> int:
    """Приводит номер страницы к допустимому диапазону."""
    return min(max(_safe_page(page), 1), max(1, int(total_pages)))


def _short(value: Any, limit: int = LABEL_LIMIT) -> str:
    """Обрезает подпись кнопки (обычный текст, без HTML)."""
    text = str(value or "").replace("\n", " ").strip()
    if not text:
        return "Без названия"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _name(value: Any, limit: int = MAX_LINE_NAME) -> str:
    """Экранированное название для строки списка (с обрезкой по длине)."""
    return escape(_short(value, limit))


def _files_label(count: int) -> str:
    """«3 файла» — подпись с числом файлов."""
    return plural(int(count or 0), ("файл", "файла", "файлов"))


def _type_icon(file_type: Any) -> str:
    """Значок типа файла («📄», «🎬», «⭕», «🎤»)."""
    return media.FILE_TYPE_ICONS.get(media.normalize_file_type(file_type), "📄")


def _type_label(file_type: Any) -> str:
    """Русское название типа файла."""
    normalized = media.normalize_file_type(file_type)
    return media.FILE_TYPE_LABELS.get(normalized, normalized)


def _format_size(value: Any) -> str:
    """Размер файла человекочитаемо («2,4 МБ»); пустая строка, если размер неизвестен."""
    size = float(_to_int(value, 0))
    if size <= 0:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f}".replace(".", ",") + f" {unit}"
        size /= 1024
    return ""


def _format_added(value: Any) -> str:
    """Дата добавления («10.09.2026»); пустая строка, если дату не разобрать."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        logger.debug("Не удалось разобрать дату добавления: %r", raw)
        return ""


def _file_meta(track: dict) -> str:
    """Короткая строка «тип · длительность · размер» для списка."""
    parts = [_type_label(track.get("file_type"))]
    duration = _to_int(track.get("duration"), 0)
    if duration > 0:
        parts.append(format_duration(duration))
    size = _format_size(track.get("file_size"))
    if size:
        parts.append(size)
    return " · ".join(parts)


def _is_other_file(track: dict | None) -> bool:
    """Относится ли запись к разделу «Другое» (не аудио)."""
    return track is not None and not media.is_audio_type(track.get("file_type"))


def _is_other_folder(folder: dict | None) -> bool:
    """Папка ли это раздела «Другое»."""
    return folder is not None and str(folder.get("section") or "") == SECTION


def _folder_arg(folder_id: int) -> int | None:
    """Значение фильтра папки: ``ROOT_ID`` -> ``None`` (файлы без папки)."""
    return folder_id if folder_id > 0 else None


async def _show(callback: CallbackQuery, bot: Bot, text: str, markup: Any = None) -> None:
    """Показывает экран: правит текущее сообщение либо отправляет новое."""
    message = callback.message
    if message is not None:
        await safe_edit(message, text, markup)
        return
    if callback.from_user is not None:
        await bot.send_message(callback.from_user.id, text, reply_markup=markup)


def _chat_id(event: Message | CallbackQuery, user_id: int) -> int:
    """Чат, в который отправлять файлы (у приватного бота это чат пользователя)."""
    message = event if isinstance(event, Message) else event.message
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return _to_int(chat_id, user_id) or user_id


def _parse_own(data: Any, prefix: str, count: int) -> list[int] | None:
    """Разбирает собственные callback-данные ``prefix:<int>:<int>…``."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != count + 1 or parts[0] != prefix:
        return None
    try:
        return [int(part) for part in parts[1:]]
    except ValueError:
        logger.warning("Не удалось разобрать callback-данные: %r", data)
        return None


# ---------------------------------------------------------------------------
# Экран раздела: подпапки + файлы
# ---------------------------------------------------------------------------


def _crumbs_line(crumbs: Sequence[dict]) -> str:
    """Хлебные крошки «📦 Другое / Папка / Подпапка»."""
    if not crumbs:
        return SECTION_TITLE
    visible = list(crumbs)
    prefix = f"{SECTION_ICON} {SECTION_NAME}"
    if len(visible) > CRUMBS_LIMIT:
        visible = visible[-CRUMBS_LIMIT:]
        prefix = f"{prefix} / …"
    names = " / ".join(_name(item.get("name"), 32) for item in visible[:-1])
    current = f"<b>{_name(visible[-1].get('name'))}</b>"
    head = f"{prefix} / {names}" if names else prefix
    return f"{head} / {current}"


async def _page_entries(
    user_id: int, folder_id: int, page: int
) -> tuple[list[dict], list[dict], int, int, int, int]:
    """Содержимое страницы: подпапки и файлы уровня.

    Возвращает ``(подпапки страницы, файлы страницы, номер страницы, всего
    страниц, всего подпапок, всего файлов)``. Нумерация сквозная: сначала идут
    папки уровня (по алфавиту), затем файлы (новые сверху).
    """
    subfolders = await folders_repo.list_folders(
        user_id, parent_folder_id=_folder_arg(folder_id), section=SECTION
    )
    files_total = await tracks_repo.count_tracks(
        user_id, folder_id=_folder_arg(folder_id), file_type=OTHER_FILE_TYPES
    )

    per_page = _per_page()
    total = len(subfolders) + files_total
    total_pages = _total_pages(total, per_page)
    current = _clamp_page(page, total_pages)
    start = page_offset(current, per_page)

    folder_slice = subfolders[start : start + per_page]
    files: list[dict] = []
    remaining = per_page - len(folder_slice)
    if remaining > 0 and start + per_page > len(subfolders):
        files = await tracks_repo.list_tracks(
            user_id,
            folder_id=_folder_arg(folder_id),
            file_type=OTHER_FILE_TYPES,
            order="created_at_desc",
            limit=remaining,
            offset=max(0, start - len(subfolders)),
        )
    return folder_slice, files, current, total_pages, len(subfolders), files_total


def _section_kb(
    folder_id: int,
    parent_id: int,
    subfolders: Sequence[dict],
    files: Sequence[dict],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Клавиатура экрана раздела: папки, номера файлов, пагинация и действия."""
    builder = InlineKeyboardBuilder()

    for folder in subfolders:
        builder.row(
            InlineKeyboardButton(
                text=f"📁 {_short(folder.get('name'))}",
                callback_data=OtherCB(
                    action="open",
                    folder_id=_to_int(folder.get("id")),
                    track_id=0,
                    page=1,
                ).pack(),
            )
        )

    start = page_offset(page) + len(subfolders) + 1
    row: list[InlineKeyboardButton] = []
    for shift, track in enumerate(files):
        row.append(
            InlineKeyboardButton(
                text=f"{_type_icon(track.get('file_type'))} {start + shift}",
                callback_data=OtherCB(
                    action="file",
                    folder_id=folder_id,
                    track_id=_to_int(track.get("id")),
                    page=page,
                ).pack(),
            )
        )
        if len(row) == FILE_BUTTONS_PER_ROW:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    if total_pages > 1:
        builder.row(
            InlineKeyboardButton(
                text="◀️" if page > 1 else "·",
                callback_data=(
                    OtherCB(
                        action="page", folder_id=folder_id, track_id=0, page=page - 1
                    ).pack()
                    if page > 1
                    else NOOP
                ),
            ),
            InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data=NOOP),
            InlineKeyboardButton(
                text="▶️" if page < total_pages else "·",
                callback_data=(
                    OtherCB(
                        action="page", folder_id=folder_id, track_id=0, page=page + 1
                    ).pack()
                    if page < total_pages
                    else NOOP
                ),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="🆕 Новая папка",
            callback_data=OtherCB(
                action="create", folder_id=folder_id, track_id=0, page=page
            ).pack(),
        )
    )

    if folder_id > 0:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ Наверх",
                callback_data=OtherCB(
                    action="open", folder_id=parent_id, track_id=0, page=1
                ).pack(),
            ),
            InlineKeyboardButton(
                text="🗑 Удалить папку",
                callback_data=OtherCB(
                    action="delete", folder_id=folder_id, track_id=0, page=page
                ).pack(),
            ),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="⬅️ В меню", callback_data=NavCB(action="menu").pack())
        )
    return builder.as_markup()


async def _render_section(
    user_id: int, folder_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Экран раздела «Другое». ``None`` — папка не найдена или не из этого раздела."""
    parent_id = ROOT_ID
    crumbs: list[dict] = []

    if folder_id > 0:
        folder = await folders_repo.get_folder(user_id, folder_id)
        if folder is None or not _is_other_folder(folder):
            return None
        parent_id = _to_int(folder.get("parent_folder_id"), ROOT_ID)
        crumbs = await folders_repo.folder_path(user_id, folder_id)
        if not crumbs:
            crumbs = [folder]

    (
        subfolders,
        files,
        current,
        total_pages,
        folders_total,
        files_total,
    ) = await _page_entries(user_id, folder_id, page)

    lines = [_crumbs_line(crumbs)]
    if folder_id > 0:
        summary = _files_label(files_total)
        if folders_total:
            summary += f", подпапок: {folders_total}"
        lines.append(summary)
    else:
        total_files = await tracks_repo.count_tracks(user_id, file_type=OTHER_FILE_TYPES)
        lines.append(f"Всего в разделе: {_files_label(total_files)}")
    lines.append("")

    if not subfolders and not files:
        lines.append(EMPTY_FOLDER if folder_id > 0 else EMPTY_SECTION)
    else:
        index = page_offset(current) + 1
        for folder in subfolders:
            count = _files_label(_to_int(folder.get("total_track_count")))
            suffix = " · есть подпапки" if folder.get("has_children") else ""
            lines.append(f"{index}. 📁 <b>{_name(folder.get('name'))}</b> — {count}{suffix}")
            index += 1
        for track in files:
            icon = _type_icon(track.get("file_type"))
            lines.append(
                f"{index}. {icon} <b>{_name(track.get('title'))}</b> — {_file_meta(track)}"
            )
            index += 1

    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    markup = _section_kb(folder_id, parent_id, subfolders, files, current, total_pages)
    return "\n".join(lines), markup


async def _show_section(
    callback: CallbackQuery, bot: Bot, user_id: int, folder_id: int, page: int
) -> None:
    """Показывает экран раздела, откатываясь к корню, если папки уже нет."""
    rendered = await _render_section(user_id, folder_id, page)
    if rendered is None:
        root = await _render_section(user_id, ROOT_ID, 1)
        if root is not None:
            await _show(callback, bot, root[0], root[1])
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return
    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback)


# ---------------------------------------------------------------------------
# Карточка файла
# ---------------------------------------------------------------------------


def _file_card_kb(track: dict, folder_id: int, page: int) -> InlineKeyboardMarkup:
    """Кнопки карточки файла: получить, перенести, переименовать, удалить."""
    track_id = _to_int(track.get("id"))
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📥 Получить файл",
            callback_data=OtherCB(
                action="send", folder_id=folder_id, track_id=track_id, page=page
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📁 В папку",
            callback_data=OtherCB(
                action="move", folder_id=folder_id, track_id=track_id, page=page
            ).pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Переименовать",
            callback_data=EditCB(action="rename", kind="track", target_id=track_id).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=OtherCB(
                action="delete", folder_id=folder_id, track_id=track_id, page=page
            ).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=OtherCB(
                action="back", folder_id=folder_id, track_id=0, page=page
            ).pack(),
        ),
    )
    return builder.as_markup()


def _file_card_text(track: dict) -> str:
    """Текст карточки файла: название, тип, размер, длительность, папка, дата."""
    icon = _type_icon(track.get("file_type"))
    lines = [f"{icon} <b>{_name(track.get('title'), MAX_TITLE_LENGTH)}</b>", ""]
    lines.append(f"Тип: {_type_label(track.get('file_type'))}")

    duration = _to_int(track.get("duration"), 0)
    if duration > 0:
        lines.append(f"Длительность: {format_duration(duration)}")

    size = _format_size(track.get("file_size"))
    if size:
        lines.append(f"Размер: {size}")

    file_name = (track.get("file_name") or "").strip()
    if file_name:
        lines.append(f"Файл: {_name(file_name)}")

    folder_name = (track.get("folder_name") or "").strip()
    lines.append(f"Папка: {_name(folder_name)}" if folder_name else "Папка: не выбрана")

    added = _format_added(track.get("created_at"))
    if added:
        lines.append(f"Добавлен: {added}")
    return "\n".join(lines)


async def _render_file_card(
    user_id: int, track_id: int, folder_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Карточка файла раздела «Другое». ``None`` — файла нет либо это аудио."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        return None
    return _file_card_text(track), _file_card_kb(track, folder_id, page)


async def _show_file_card(
    callback: CallbackQuery, bot: Bot, user_id: int, track_id: int, folder_id: int, page: int
) -> None:
    """Показывает карточку файла либо возвращает к списку, если файла уже нет."""
    rendered = await _render_file_card(user_id, track_id, folder_id, page)
    if rendered is None:
        await _show_section(callback, bot, user_id, folder_id, page)
        return
    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback)


# ---------------------------------------------------------------------------
# Выбор папки для файла
# ---------------------------------------------------------------------------


async def _render_pick(
    user_id: int, track: dict, folder_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура выбора папки раздела «Другое» для файла."""
    track_id = _to_int(track.get("id"))
    folders = await folders_repo.list_folders(user_id, section=SECTION)

    lines = [CHOOSE_FOLDER.format(section=SECTION_NAME, title=_name(track.get("title")))]
    current_folder = (track.get("folder_name") or "").strip()
    if current_folder:
        lines.append(f"Сейчас файл в папке «{_name(current_folder)}».")
    if len(folders) > MAX_PICK_FOLDERS:
        lines.append(PICK_LIMIT_HINT.format(limit=MAX_PICK_FOLDERS))

    builder = InlineKeyboardBuilder()
    for folder in folders[:MAX_PICK_FOLDERS]:
        depth = max(0, _to_int(folder.get("depth")))
        prefix = "· " * min(depth, 3)
        builder.row(
            InlineKeyboardButton(
                text=f"📁 {prefix}{_short(folder.get('name'))}",
                callback_data=(
                    f"{MOVE_PREFIX}:{track_id}:{_to_int(folder.get('id'))}:{page}"
                ),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🆕 Новая папка",
            callback_data=f"{MOVE_PREFIX}:{track_id}:{FOLDER_NEW}:{page}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🚫 Без папки",
            callback_data=f"{MOVE_PREFIX}:{track_id}:{FOLDER_NONE}:{page}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=OtherCB(
                action="file", folder_id=folder_id, track_id=track_id, page=page
            ).pack(),
        )
    )
    return "\n".join(lines), builder.as_markup()


# ---------------------------------------------------------------------------
# Команда /other
# ---------------------------------------------------------------------------


@router.message(Command("other"))
async def cmd_other(message: Message, state: FSMContext) -> None:
    """Показывает корень раздела «Другое»."""
    if message.from_user is None:
        return
    await _reset_state(state)
    user_id = int(message.from_user.id)
    rendered = await _render_section(user_id, ROOT_ID, 1)
    if rendered is None:  # pragma: no cover - корень раздела существует всегда
        await message.answer(texts.ERROR_TRY_AGAIN)
        return
    await message.answer(rendered[0], reply_markup=rendered[1])


async def _reset_state(state: FSMContext) -> None:
    """Сбрасывает незавершённый диалог этого раздела.

    Чистит только свои ключи и своё состояние: FSM общий на весь бот, и
    ``state.clear()`` унёс бы вместе с диалогом раздела чужие данные —
    буфер пачки загрузки и незаконченную раскладку по папкам.
    """
    async with _STATE_LOCK:
        current = await state.get_state()
        if current in {
            OtherSectionStates.waiting_title.state,
            OtherSectionStates.waiting_folder_name.state,
        }:
            await state.set_state(None)
        await state.update_data(**_DIALOG_DEFAULTS)


async def _mark_prompt(state: FSMContext, prompt_id: int) -> None:
    """Запоминает самое нижнее приглашение раздела на экране пользователя.

    Идентификаторы сообщений в чате растут, поэтому «нижнее» приглашение — это
    приглашение с наибольшим номером. Отметку ставим сразу при отправке статуса
    «Сохраняю файл…»: порядок статусов совпадает с порядком файлов на экране,
    а порядок окончания загрузок — нет.
    """
    if prompt_id <= 0:
        return
    async with _STATE_LOCK:
        data = await state.get_data()
        if prompt_id > _to_int(data.get(DATA_PROMPT_ID)):
            await state.update_data(**{DATA_PROMPT_ID: prompt_id})


async def _claim_title_input(state: FSMContext, track_id: int, prompt_id: int) -> bool:
    """Связывает ввод названия с этим файлом; False — ввод занят файлом ниже.

    Файлы одной пачки сохраняются параллельно, поэтому «последний сохранённый»
    и «последний на экране» — разные файлы. Название, отправленное текстом,
    относится к нижнему приглашению: только его файл и забирает ввод.
    """
    async with _STATE_LOCK:
        data = await state.get_data()
        if prompt_id < _to_int(data.get(DATA_PROMPT_ID)):
            return False
        await state.set_state(OtherSectionStates.waiting_title)
        await state.update_data(
            **{
                DATA_TRACK_ID: track_id,
                DATA_PROMPT_ID: prompt_id,
                DATA_PARENT_ID: ROOT_ID,
                DATA_PAGE: 1,
            }
        )
        return True


async def _release_title_input(state: FSMContext, track_id: int) -> None:
    """Завершает ожидание названия, если оно относится именно к этому файлу."""
    async with _STATE_LOCK:
        if await state.get_state() != OtherSectionStates.waiting_title.state:
            return
        data = await state.get_data()
        if _to_int(data.get(DATA_TRACK_ID)) != int(track_id):
            return
        await state.set_state(None)
        await state.update_data(**_DIALOG_DEFAULTS)


# ---------------------------------------------------------------------------
# Колбэки раздела
# ---------------------------------------------------------------------------


@router.callback_query(OtherCB.filter())
async def on_other_callback(
    callback: CallbackQuery,
    callback_data: OtherCB,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Единый обработчик действий раздела «Другое»."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    action = (callback_data.action or "").strip()
    folder_id = max(ROOT_ID, _to_int(callback_data.folder_id))
    track_id = max(0, _to_int(callback_data.track_id))
    page = _safe_page(callback_data.page)

    if action in {"open", "page", "list", "back"}:
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    if action == "file":
        await _show_file_card(callback, bot, user_id, track_id, folder_id, page)
        return

    if action == "send":
        await _send_file(callback, bot, user_id, track_id, folder_id, page)
        return

    if action == "move":
        await _ask_folder(callback, bot, user_id, track_id, folder_id, page)
        return

    if action == "create":
        await _ask_folder_name(callback, bot, state, folder_id, page)
        return

    if action == "delete":
        if track_id > 0:
            await _confirm_delete_file(callback, bot, user_id, track_id, folder_id, page)
        else:
            await _confirm_delete_folder(callback, bot, user_id, folder_id, page)
        return

    logger.debug("Неизвестное действие раздела «Другое»: %r", action)
    await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)


async def _send_file(
    callback: CallbackQuery, bot: Bot, user_id: int, track_id: int, folder_id: int, page: int
) -> None:
    """Отправляет пользователю сохранённый файл нужным методом Bot API."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    icon = _type_icon(track.get("file_type"))
    caption = f"{icon} <b>{_name(track.get('title'), MAX_TITLE_LENGTH)}</b>"
    try:
        await media.send_media_to_user(bot, _chat_id(callback, user_id), track, caption=caption)
    except StorageError as error:
        logger.warning(
            "Пользователь %s: не удалось отправить файл %s: %s", user_id, track_id, error
        )
        await ack(callback, str(error)[:200], alert=True)
        return
    except MusicBoxError as error:  # pragma: no cover - защита от новых доменных ошибок
        logger.warning(
            "Пользователь %s: ошибка при отправке файла %s: %s", user_id, track_id, error
        )
        await ack(callback, str(error)[:200], alert=True)
        return

    logger.info("Пользователь %s получил файл %s", user_id, track_id)
    await ack(callback, FILE_SENT)


async def _ask_folder(
    callback: CallbackQuery, bot: Bot, user_id: int, track_id: int, folder_id: int, page: int
) -> None:
    """Показывает выбор папки для файла."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    text, markup = await _render_pick(user_id, track, folder_id, page)
    await _show(callback, bot, text, markup)
    await ack(callback)


async def _ask_folder_name(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    parent_id: int,
    page: int,
    *,
    track_id: int = 0,
) -> None:
    """Просит ввести название новой папки раздела."""
    await state.set_state(OtherSectionStates.waiting_folder_name)
    await state.update_data(
        **{DATA_PARENT_ID: parent_id, DATA_TRACK_ID: track_id, DATA_PAGE: page}
    )
    await _show(callback, bot, f"{FOLDER_NAME_PROMPT}\n\n{texts.CANCEL_HINT}")
    await ack(callback)


async def _confirm_delete_file(
    callback: CallbackQuery, bot: Bot, user_id: int, track_id: int, folder_id: int, page: int
) -> None:
    """Спрашивает подтверждение удаления файла."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    markup = confirm_kb(
        f"{DELETE_FILE_PREFIX}:{track_id}:{folder_id}:{page}",
        OtherCB(action="file", folder_id=folder_id, track_id=track_id, page=page).pack(),
    )
    question = CONFIRM_DELETE_FILE.format(title=_name(track.get("title")))
    await _show(callback, bot, question, markup)
    await ack(callback)


async def _confirm_delete_folder(
    callback: CallbackQuery, bot: Bot, user_id: int, folder_id: int, page: int
) -> None:
    """Спрашивает подтверждение удаления папки раздела."""
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None or not _is_other_folder(folder):
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return

    markup = confirm_kb(
        f"{DELETE_FOLDER_PREFIX}:{folder_id}:{page}",
        OtherCB(action="open", folder_id=folder_id, track_id=0, page=page).pack(),
    )
    question = CONFIRM_DELETE_FOLDER.format(folder=_name(folder.get("name")))
    count = _files_label(_to_int(folder.get("total_track_count")))
    await _show(callback, bot, f"{question}\nСейчас в ней {count}.", markup)
    await ack(callback)


# ---------------------------------------------------------------------------
# Собственные callback-данные: подтверждения и перенос
# ---------------------------------------------------------------------------


def _is_delete_file(data: Any) -> bool:
    """Фильтр подтверждения удаления файла."""
    return isinstance(data, str) and data.startswith(f"{DELETE_FILE_PREFIX}:")


def _is_delete_folder(data: Any) -> bool:
    """Фильтр подтверждения удаления папки."""
    return isinstance(data, str) and data.startswith(f"{DELETE_FOLDER_PREFIX}:")


def _is_move(data: Any) -> bool:
    """Фильтр выбора папки для файла."""
    return isinstance(data, str) and data.startswith(f"{MOVE_PREFIX}:")


def _is_keep_title(data: Any) -> bool:
    """Фильтр кнопки «оставить текущее название»."""
    return isinstance(data, str) and data.startswith(f"{KEEP_TITLE_PREFIX}:")


@router.callback_query(F.data.func(_is_delete_file))
async def on_delete_file_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    """Удаляет файл из библиотеки и из канала-хранилища."""
    if callback.from_user is None:
        await ack(callback)
        return

    parsed = _parse_own(callback.data, DELETE_FILE_PREFIX, 3)
    if parsed is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    track_id, folder_id, page = parsed
    folder_id = max(ROOT_ID, folder_id)
    page = _safe_page(page)
    user_id = int(callback.from_user.id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    deleted = await tracks_repo.delete_track(user_id, track_id)
    if deleted is None:
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, folder_id, page)
        return

    message_id = deleted.get("storage_message_id")
    if message_id and not await storage.delete_from_channel(bot, message_id):
        # Ошибку канала только логируем: в библиотеке файла уже нет.
        logger.warning(
            "Файл %s удалён у пользователя %s, но сообщение %s осталось в канале",
            track_id,
            user_id,
            message_id,
        )

    logger.info("Пользователь %s удалил файл %s", user_id, track_id)
    rendered = await _render_section(user_id, folder_id, page)
    if rendered is None:
        rendered = await _render_section(user_id, ROOT_ID, 1)
    if rendered is not None:
        await _show(callback, bot, f"{FILE_DELETED}\n\n{rendered[0]}", rendered[1])
    await ack(callback, "Файл удалён")


@router.callback_query(F.data.func(_is_delete_folder))
async def on_delete_folder_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    """Удаляет папку раздела вместе с подпапками; файлы остаются в разделе."""
    if callback.from_user is None:
        await ack(callback)
        return

    parsed = _parse_own(callback.data, DELETE_FOLDER_PREFIX, 2)
    if parsed is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    folder_id, page = parsed
    page = _safe_page(page)
    user_id = int(callback.from_user.id)

    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None or not _is_other_folder(folder):
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return

    parent_id = _to_int(folder.get("parent_folder_id"), ROOT_ID)
    try:
        removed = await folders_repo.delete_folder(
            user_id, folder_id, delete_tracks=False, recursive=True
        )
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось удалить папку %s: %s", user_id, folder_id, error
        )
        await ack(callback, str(error)[:200], alert=True)
        return

    if not removed:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return

    logger.info("Пользователь %s удалил папку %s раздела «Другое»", user_id, folder_id)
    header = FOLDER_DELETED.format(folder=_name(folder.get("name")))
    rendered = await _render_section(user_id, parent_id, 1)
    if rendered is None:
        rendered = await _render_section(user_id, ROOT_ID, 1)
    if rendered is not None:
        await _show(callback, bot, f"{header}\n\n{rendered[0]}", rendered[1])
    await ack(callback, "Папка удалена")


@router.callback_query(F.data.func(_is_move))
async def on_move_selected(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Переносит файл в выбранную папку, убирает из папки или просит название новой."""
    if callback.from_user is None:
        await ack(callback)
        return

    parsed = _parse_own(callback.data, MOVE_PREFIX, 3)
    if parsed is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    track_id, target_id, page = parsed
    page = _safe_page(page)
    user_id = int(callback.from_user.id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return
    current_folder = _to_int(track.get("folder_id"), ROOT_ID)

    if target_id == FOLDER_NEW:
        await _ask_folder_name(callback, bot, state, ROOT_ID, page, track_id=track_id)
        return

    if target_id in (FOLDER_NONE, ROOT_ID):
        await _apply_move(callback, bot, user_id, track_id, None, page)
        return

    folder = await folders_repo.get_folder(user_id, target_id)
    if folder is None or not _is_other_folder(folder):
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_file_card(callback, bot, user_id, track_id, current_folder, page)
        return

    await _apply_move(callback, bot, user_id, track_id, target_id, page)


async def _apply_move(
    callback: CallbackQuery,
    bot: Bot,
    user_id: int,
    track_id: int,
    target_id: int | None,
    page: int,
) -> None:
    """Выполняет перенос файла и показывает его карточку с итогом."""
    try:
        updated = await tracks_repo.move_track(user_id, track_id, target_id)
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось перенести файл %s: %s", user_id, track_id, error
        )
        await ack(callback, str(error)[:200], alert=True)
        return

    if updated is None:
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return

    folder_name = (updated.get("folder_name") or "").strip()
    header = (
        FILE_MOVED.format(folder=_name(folder_name)) if folder_name else FILE_MOVED_TO_ROOT
    )
    logger.info("Пользователь %s: файл %s перенесён в папку %s", user_id, track_id, target_id)

    folder_id = _to_int(updated.get("folder_id"), ROOT_ID)
    rendered = await _render_file_card(user_id, track_id, folder_id, page)
    if rendered is None:  # pragma: no cover - файл удалён между запросами
        await _show_section(callback, bot, user_id, folder_id, page)
        return
    await _show(callback, bot, f"{header}\n\n{rendered[0]}", rendered[1])
    await ack(callback, "Готово")


# ---------------------------------------------------------------------------
# Приём не-аудио сообщений
# ---------------------------------------------------------------------------


def _is_other_document(document: Document | None) -> bool:
    """Документ, который НЕ является аудио (аудио забирает handlers/upload.py)."""
    return document is not None and not media.is_audio_document(document)


@router.message(F.document.func(_is_other_document))
@router.message(F.video)
@router.message(F.video_note)
@router.message(F.voice)
async def handle_media(message: Message, state: FSMContext, bot: Bot) -> None:
    """Приём документа, видео, кружочка или голосового сообщения."""
    await _process_media(message, state, bot)


def _forward_ref(message: Message) -> str | None:
    """Ссылка или идентификатор источника для пересланного сообщения."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None

    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    origin_message_id = getattr(origin, "message_id", None)
    if chat is not None:
        username = getattr(chat, "username", None)
        if username and origin_message_id:
            return f"https://t.me/{username}/{origin_message_id}"
        chat_id = getattr(chat, "id", None)
        if chat_id is not None:
            if origin_message_id:
                return f"chat:{chat_id}:{origin_message_id}"
            return f"chat:{chat_id}"

    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        return f"user:{getattr(sender_user, 'id', '')}"
    sender_name = getattr(origin, "sender_user_name", None)
    if sender_name:
        return f"user:{sender_name}"
    return "telegram_forward"


def _resolve_source(message: Message) -> tuple[str, str | None]:
    """Пара ``(source, source_ref)`` для записи файла."""
    forwarded = any(
        getattr(message, attribute, None) is not None
        for attribute in ("forward_origin", "forward_date")
    )
    if forwarded:
        return "telegram_forward", _forward_ref(message)
    return "upload", None


async def _finish(
    status: Message | None,
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показывает итог: правит статусное сообщение либо отвечает новым.

    Если правку выполнить не удалось, ответ уходит новым сообщением: итог загрузки
    и приглашение ввести название важнее способа доставки — иначе пользователь
    остался бы в диалоге без единой подсказки.
    """
    if status is not None:
        try:
            await safe_edit(status, text, markup)
            return
        except Exception:  # noqa: BLE001 — доставляем итог любым доступным способом
            logger.warning(
                "Не удалось обновить статусное сообщение — отвечаю новым", exc_info=True
            )
    await message.answer(text, reply_markup=markup)


async def _process_media(message: Message, state: FSMContext, bot: Bot) -> None:
    """Полный цикл сохранения одного не-аудио файла."""
    if message.from_user is None:
        logger.debug("Сообщение без отправителя — файл пропущен")
        return

    file_type = media.detect_file_type(message)
    if file_type is None or media.is_audio_type(file_type):
        # Аудио и аудио-документы обрабатывает handlers/upload.py.
        logger.debug("Сообщение с типом %r не относится к разделу «Другое»", file_type)
        return

    user_id = int(message.from_user.id)
    # Гарантирует наличие пользователя и его настроек (внешние ключи треков и папок).
    await users_repo.get_settings(user_id)

    incoming = getattr(getattr(message, file_type, None), "file_unique_id", None)
    if incoming:
        existing = await tracks_repo.get_track_by_unique_id(user_id, str(incoming))
        if existing is not None:
            await message.answer(f"{DUPLICATE_FILE}\n{_file_card_text(existing)}")
            return

    status: Message | None
    try:
        status = await message.answer(SAVING_STATUS)
    except Exception:  # noqa: BLE001 — статусное сообщение не должно ломать загрузку
        logger.exception("Не удалось отправить статусное сообщение пользователю")
        status = None

    # Приглашение ввести название встанет на место статуса, поэтому место файла
    # на экране известно уже сейчас — до долгой перезаливки в канал.
    prompt_id = _to_int(getattr(status, "message_id", 0))
    await _mark_prompt(state, prompt_id)

    try:
        stored = await media.store_media(bot, message)
    except FileTooLargeError as error:
        logger.warning("Пользователь %s: файл слишком большой: %s", user_id, error)
        await _finish(status, message, texts.FILE_TOO_LARGE)
        return
    except StorageError as error:
        logger.warning("Пользователь %s: ошибка хранилища: %s", user_id, error)
        await _finish(status, message, f"{texts.STORAGE_ERROR}\n\n{escape(str(error))}")
        return
    except Exception:  # noqa: BLE001 — сбой Telegram не должен ронять обработчик
        logger.exception("Пользователь %s: непредвиденная ошибка при сохранении файла", user_id)
        await _finish(status, message, texts.ERROR_TRY_AGAIN)
        return

    if stored.file_unique_id:
        existing = await tracks_repo.get_track_by_unique_id(user_id, str(stored.file_unique_id))
        if existing is not None:
            await _finish(status, message, f"{DUPLICATE_FILE}\n{_file_card_text(existing)}")
            return

    source, source_ref = _resolve_source(message)
    try:
        track = await tracks_repo.create_track(
            user_id,
            title=stored.title,
            duration=stored.duration,
            file_size=stored.file_size,
            mime_type=stored.mime_type,
            file_name=stored.file_name,
            file_id=stored.file_id,
            file_unique_id=stored.file_unique_id,
            storage_chat_id=stored.chat_id,
            storage_message_id=stored.message_id,
            thumb_file_id=stored.thumb_file_id,
            source=source,
            source_ref=source_ref,
            file_type=stored.file_type,
        )
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось сохранить файл в БД: %s", user_id, error)
        await _finish(status, message, f"❌ {escape(str(error))}")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Пользователь %s: непредвиденная ошибка при создании записи", user_id)
        await _finish(status, message, texts.ERROR_TRY_AGAIN)
        return

    track_id = _to_int(track.get("id"))
    logger.info(
        "Пользователь %s сохранил файл #%s (тип=%s, source=%s)",
        user_id,
        track_id,
        stored.file_type,
        source,
    )

    claimed = await _claim_title_input(state, track_id, prompt_id)

    title = _name(track.get("title"), MAX_TITLE_LENGTH)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"✅ Оставить «{_short(track.get('title'))}»",
            callback_data=f"{KEEP_TITLE_PREFIX}:{track_id}",
        )
    )
    if claimed:
        prompt = f"{TITLE_PROMPT}\n\n{texts.CANCEL_HINT}"
    else:
        # Ввод текстом занят файлом, приглашение которого ниже на экране, —
        # этому файлу даём кнопку переименования с его собственным id внутри.
        prompt = TITLE_PROMPT_BUSY
        builder.row(
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=EditCB(action="rename", kind="track", target_id=track_id).pack(),
            )
        )
    text = f"{FILE_SAVED}\n{_type_icon(stored.file_type)} <b>{title}</b>\n\n{prompt}"
    await _finish(status, message, text, builder.as_markup())


# ---------------------------------------------------------------------------
# FSM: название файла
# ---------------------------------------------------------------------------


@router.callback_query(F.data.func(_is_keep_title))
async def on_keep_title(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«Оставить текущее название» — сразу переходим к выбору папки."""
    if callback.from_user is None:
        await ack(callback)
        return

    parsed = _parse_own(callback.data, KEEP_TITLE_PREFIX, 1)
    if parsed is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    track_id = parsed[0]
    user_id = int(callback.from_user.id)
    await _release_title_input(state, track_id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None or not _is_other_file(track):
        await ack(callback, FILE_NOT_FOUND, alert=True)
        await _show_section(callback, bot, user_id, ROOT_ID, 1)
        return

    text, markup = await _render_pick(user_id, track, ROOT_ID, 1)
    await _show(callback, bot, text, markup)
    await ack(callback)


@router.message(StateFilter(OtherSectionStates.waiting_title), Command("cancel"))
@router.message(StateFilter(OtherSectionStates.waiting_folder_name), Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает диалог раздела: файл уже сохранён и остаётся в библиотеке."""
    await _reset_state(state)
    await message.answer(CANCELLED_UPLOAD)


@router.message(StateFilter(OtherSectionStates.waiting_title), F.text)
async def on_title(message: Message, state: FSMContext) -> None:
    """Переименовывает загруженный файл и предлагает выбрать папку."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await _reset_state(state)
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return

    if not text:
        await message.answer(texts.NAME_EMPTY)
        return
    if len(text) > MAX_TITLE_LENGTH:
        await message.answer(TITLE_TOO_LONG)
        return

    user_id = int(message.from_user.id)
    data = await state.get_data()
    track_id = _to_int(data.get(DATA_TRACK_ID))
    if track_id <= 0:
        await _reset_state(state)
        await message.answer(f"{FILE_NOT_FOUND}\nОткройте раздел командой /other.")
        return

    try:
        track = await tracks_repo.rename_track(user_id, track_id, text)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось переименовать файл %s: %s", user_id, track_id, error
        )
        await _reset_state(state)
        await message.answer(f"❌ {escape(str(error))}")
        return

    await _reset_state(state)
    if track is None or not _is_other_file(track):
        await message.answer(FILE_NOT_FOUND)
        return

    logger.info("Пользователь %s назвал файл %s «%s»", user_id, track_id, track.get("title"))
    pick_text, markup = await _render_pick(user_id, track, ROOT_ID, 1)
    await message.answer(pick_text, reply_markup=markup)


# ---------------------------------------------------------------------------
# FSM: название новой папки раздела
# ---------------------------------------------------------------------------


@router.message(StateFilter(OtherSectionStates.waiting_folder_name), F.text)
async def on_folder_name(message: Message, state: FSMContext) -> None:
    """Создаёт папку раздела «Другое» и, если нужно, переносит в неё файл."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await _reset_state(state)
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return

    if not text:
        await message.answer(texts.NAME_EMPTY)
        return
    if len(text) > MAX_FOLDER_NAME_LENGTH:
        await message.answer(texts.NAME_TOO_LONG)
        return

    user_id = int(message.from_user.id)
    data = await state.get_data()
    parent_id = max(ROOT_ID, _to_int(data.get(DATA_PARENT_ID)))
    track_id = max(0, _to_int(data.get(DATA_TRACK_ID)))
    page = _safe_page(data.get(DATA_PAGE))

    try:
        folder = await folders_repo.create_folder(
            user_id, text, parent_folder_id=_folder_arg(parent_id), section=SECTION
        )
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось создать папку «%s»: %s", user_id, text, error)
        await _reset_state(state)
        await message.answer(f"❌ {escape(str(error))}")
        return

    await _reset_state(state)
    folder_id = _to_int(folder.get("id"))
    logger.info(
        "Пользователь %s создал папку «%s» (id=%s) раздела «Другое»",
        user_id,
        folder.get("name"),
        folder_id,
    )
    header = FOLDER_CREATED.format(folder=_name(folder.get("name")))

    if track_id > 0:
        await _move_after_create(message, user_id, track_id, folder_id, header, page)
        return

    rendered = await _render_section(user_id, folder_id, 1)
    if rendered is None:  # pragma: no cover - папка удалена между запросами
        await message.answer(header)
        return
    await message.answer(f"{header}\n\n{rendered[0]}", reply_markup=rendered[1])


async def _move_after_create(
    message: Message,
    user_id: int,
    track_id: int,
    folder_id: int,
    header: str,
    page: int,
) -> None:
    """Переносит файл в только что созданную папку и показывает его карточку."""
    try:
        updated = await tracks_repo.move_track(user_id, track_id, folder_id)
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось перенести файл %s: %s", user_id, track_id, error
        )
        await message.answer(f"{header}\n❌ {escape(str(error))}")
        return

    if updated is None:
        await message.answer(f"{header}\n{FILE_NOT_FOUND}")
        return

    folder_name = (updated.get("folder_name") or "").strip()
    moved = FILE_MOVED.format(folder=_name(folder_name)) if folder_name else FILE_MOVED_TO_ROOT
    logger.info(
        "Пользователь %s: файл %s перенесён в новую папку %s", user_id, track_id, folder_id
    )

    rendered = await _render_file_card(user_id, track_id, folder_id, page)
    if rendered is None:  # pragma: no cover - файл удалён между запросами
        await message.answer(f"{header}\n{moved}")
        return
    await message.answer(f"{header}\n{moved}\n\n{rendered[0]}", reply_markup=rendered[1])


__all__ = [
    "OTHER_FILE_TYPES",
    "SECTION",
    "OtherSectionStates",
    "cmd_other",
    "handle_media",
    "router",
]
