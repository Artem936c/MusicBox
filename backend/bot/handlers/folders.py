"""Хендлеры раздела «Папки»: дерево вложенных папок (контракт V2, пп. 4, 16, 19).

Экраны раздела:

* **уровень дерева** — подпапки текущего уровня по алфавиту с индикатором
  вложенности, треки самой папки (пагинация по ``settings.page_size``),
  хлебные крошки через :func:`folders_repo.folder_path` и кнопка «⬆️ Вверх»;
* **создание подпапки** в текущей папке и **переименование** — через FSM
  :class:`backend.bot.states.FolderStates`;
* **удаление** — с подтверждением и явным выбором, удалять ли треки
  (при удалении треков их копии убираются и из канала-хранилища);
* **перемещение папки** — выбор нового родителя; попытка вложить папку в саму
  себя или в собственную подпапку отбивается ``ValidationError`` репозитория,
  и пользователь видит понятное русское объяснение.

Команды модуля: ``/folders``, ``/create_folder``, ``/play_folder``
(воспроизведение папки РЕКУРСИВНО, вместе с подпапками, пачками по 10 треков,
с ``register_play(source="bot_play_folder")`` на каждый отправленный трек).

Клавиатуры и тексты этих экранов объявлены ЛОКАЛЬНО (правила V2). Из
``backend.bot.keyboards`` переиспользуется только :func:`folder_pick_kb` —
выбор папки для трека (``TrackCB(action="move")``), сам перенос трека
выполняет обработчик ``MoveCB`` в ``handlers/upload.py``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Iterable, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import FolderCB, NavCB, TrackCB
from backend.bot.keyboards import folder_pick_kb
from backend.bot.states import FolderStates
from backend.bot.utils import (
    ack,
    escape,
    folder_context,
    format_track_line,
    page_offset,
    plural,
    safe_edit,
    tracks_count_label,
)
from backend.config import settings
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, NotFoundError, StorageError, ValidationError
from backend.services import media, storage

logger = logging.getLogger(__name__)

router = Router(name="folders")


# ---------------------------------------------------------------------------
# Константы модуля
# ---------------------------------------------------------------------------

#: Раздел дерева, с которым работает этот модуль (раздел «Другое» — handlers/other.py).
SECTION: str = folders_repo.DEFAULT_SECTION

#: Максимальная длина названия папки (согласовано с текстами подсказок).
MAX_FOLDER_NAME_LENGTH = 64

#: Сколько папок максимум показываем в клавиатуре выбора папки для трека.
MAX_PICK_FOLDERS = 50

#: Максимальная длина подписи на кнопке (иначе клавиатура «разъезжается»).
MAX_LABEL = 28

#: Сколько треков отправляем за одно нажатие «▶️ Играть папку».
PLAY_BATCH = 10

#: Пауза между отправками, чтобы не поймать flood limit Telegram.
PLAY_DELAY = 0.3

#: Сколько файлов удаляем из канала при удалении папки вместе с треками.
CHANNEL_CLEANUP_LIMIT = 100

#: Пауза между удалениями сообщений в канале.
CLEANUP_DELAY = 0.05

#: Ограничение Telegram на текст в `callback.answer` (с запасом).
ANSWER_LIMIT = 190

#: Ограничение Telegram на длину сообщения (4096) с запасом на хвост.
MESSAGE_LIMIT = 3800

#: Сколько последних крошек показываем целиком (остальные схлопываются в «…»).
MAX_CRUMBS = 4

# --- собственные callback-данные модуля ------------------------------------
# Префиксы намеренно отличаются от «fld», чтобы данные не разбирались фабрикой
# FolderCB (у неё три поля, а здесь нужны четыре / другой набор).

#: Подтверждение удаления: ``fldrm:<folder_id>:<page>:<0|1>`` (1 — удалить треки).
DELETE_CONFIRM_PREFIX = "fldrm"
#: Перенос папки: ``fldmv:<folder_id>:<parent_id>:<page>`` (parent_id 0 — в корень).
MOVE_APPLY_PREFIX = "fldmv"
#: Пагинация выбора нового родителя: ``fldmvp:<folder_id>:<page>``.
MOVE_PAGE_PREFIX = "fldmvp"
#: Воспроизведение папки: ``fldpl:<folder_id>:<offset>``.
PLAY_PREFIX = "fldpl"

#: Ключи данных FSM (с префиксом раздела, чтобы не пересекаться с другими диалогами).
STATE_PARENT_KEY = "folders_parent_id"
STATE_TARGET_KEY = "folders_target_id"
STATE_PAGE_KEY = "folders_page"


# ---------------------------------------------------------------------------
# Локальные русские тексты раздела
# ---------------------------------------------------------------------------

FOLDERS_TITLE = "📁 <b>Папки</b>"
ROOT_CRUMB = "🏠 Папки"
SUBFOLDERS_HEADER = "<b>Подпапки:</b>"
TRACKS_HEADER = "<b>Треки:</b>"
HAS_CHILDREN_MARK = "есть подпапки"
EMPTY_LEVEL = (
    "Здесь пока пусто. Создайте подпапку или пришлите аудио — "
    "я сохраню трек и помогу разложить его по папкам."
)
SUBFOLDER_NAME_PROMPT = "📁 Введите название новой подпапки для «{folder}» (до 64 символов)."
FOLDER_CREATED_IN = "📁 Подпапка «{folder}» создана внутри «{parent}»."
CONFIRM_DELETE_TITLE = "🗑 Что сделать с папкой «{folder}»?"
DELETE_KEEP_TRACKS = "📂 Удалить, треки оставить"
DELETE_WITH_TRACKS = "🔥 Удалить вместе с треками"
DELETE_CANCEL = "✖️ Отмена"
FOLDER_DELETED_WITH_TRACKS = "🗑 Папка «{folder}» удалена вместе с треками ({count})."
SUBFOLDERS_WARNING = "Внутри вложенных папок: {count} — они будут удалены тоже."
CHANNEL_CLEANUP_DONE = "Файлов удалено из хранилища: {count}."
CHANNEL_CLEANUP_PARTIAL = (
    "Из канала-хранилища удалено {count} файлов; остальные остались там — "
    "их можно убрать вручную."
)
MOVE_TITLE = "📦 Куда перенести папку «{folder}»?"
MOVE_CURRENT = "Сейчас она здесь: {path}"
MOVE_TO_ROOT = "🏠 В корень раздела"
MOVE_NO_TARGETS = (
    "Переносить некуда: подходящих папок нет. Создайте новую папку — "
    "и перенос станет возможен."
)
MOVE_DONE_ROOT = "📦 Папка «{folder}» перенесена в корень раздела."
MOVE_DONE_INTO = "📦 Папка «{folder}» перенесена в «{parent}»."
PLAY_HEADER = "▶️ Играю папку «{folder}» — {count} (вместе с подпапками)."
PLAY_MORE = "⏭ Ещё {count}"
PLAY_SENT = "Отправлено треков: {sent} из {total}."
PLAY_FAILED = "Не удалось отправить: {count}."
PLAY_DONE = "Это все треки папки. 🎧"
PLAY_PICK_HINT = (
    "Откройте нужную папку и нажмите «▶️ Играть папку» — "
    "я отправлю треки вместе с подпапками."
)
PLAY_UNKNOWN_FOLDER = "🤷 Папка «{name}» не найдена. Выберите её из списка."
CYCLE_HINT = "Выберите другую папку."
DEPTH_HINT = "Слишком глубокая вложенность — выберите папку выше по дереву."


# ---------------------------------------------------------------------------
# Мелкие помощники
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
    """Безопасное приведение к int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short(value: Any, limit: int = MAX_LABEL) -> str:
    """Подпись кнопки: обрезаем длинные названия (в кнопках HTML не нужен)."""
    text = str(value or "").strip() or "Без названия"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _clip(text: str, limit: int = ANSWER_LIMIT) -> str:
    """Обрезает текст всплывающего уведомления до лимита Telegram."""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _trim_message(text: str) -> str:
    """Страхует от превышения лимита сообщения в 4096 символов."""
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[:MESSAGE_LIMIT].rstrip() + "\n\n<i>…список сокращён</i>"


def _user_id(event: Message | CallbackQuery) -> int | None:
    """Идентификатор пользователя события (None — событие без автора)."""
    user = event.from_user
    return int(user.id) if user is not None else None


def _noop() -> str:
    """Callback-данные «ничего не делать» (номер страницы, разделители)."""
    return NavCB(action="noop").pack()


def _parse_local(data: str, prefix: str, size: int) -> tuple[int, ...] | None:
    """Разбирает собственные callback-данные вида ``prefix:1:2:3``."""
    parts = (data or "").split(":")
    if len(parts) != size + 1 or parts[0] != prefix:
        return None
    try:
        return tuple(int(part) for part in parts[1:])
    except ValueError:
        return None


def _prefix_filter(prefix: str):
    """Фабрика фильтра ``F.data.func(...)`` для собственных callback-данных."""

    marker = f"{prefix}:"

    def check(data: Any) -> bool:
        return isinstance(data, str) and data.startswith(marker)

    return check


_is_delete_confirm = _prefix_filter(DELETE_CONFIRM_PREFIX)
_is_move_apply = _prefix_filter(MOVE_APPLY_PREFIX)
_is_move_page = _prefix_filter(MOVE_PAGE_PREFIX)
_is_play = _prefix_filter(PLAY_PREFIX)


def _validate_name(text: str) -> str | None:
    """Проверяет введённое название; возвращает текст ошибки или None."""
    if not text:
        return texts.NAME_EMPTY
    if len(text) > MAX_FOLDER_NAME_LENGTH:
        return texts.NAME_TOO_LONG
    return None


def _folder_icon(folder: dict) -> str:
    """Индикатор вложенности: 🗂 — есть подпапки, 🎤 — папка исполнителя, 📁 — обычная."""
    if folder.get("has_children"):
        return "🗂"
    if folder.get("is_artist_folder"):
        return "🎤"
    return "📁"


def _folders_label(count: int) -> str:
    """«3 папки» — подпись с числом папок."""
    return plural(count, ("папка", "папки", "папок"))


def _files_label(count: int) -> str:
    """«3 файла» — подпись с числом файлов (в папке могут лежать не только треки)."""
    return plural(count, ("файл", "файла", "файлов"))


async def _show(callback: CallbackQuery, bot: Bot, text: str, markup: Any = None) -> None:
    """Показывает экран: правит текущее сообщение либо отправляет новое."""
    message = callback.message
    if message is not None:
        await safe_edit(message, _trim_message(text), markup)
        return
    if callback.from_user is not None:
        await bot.send_message(
            int(callback.from_user.id), _trim_message(text), reply_markup=markup
        )


async def _reset_folder_state(state: FSMContext) -> None:
    """Сбрасывает незавершённый ввод названия папки."""
    current = await state.get_state()
    if current in {
        FolderStates.waiting_name.state,
        FolderStates.waiting_rename.state,
        FolderStates.waiting_new_name.state,
    }:
        await state.clear()


# ---------------------------------------------------------------------------
# Хлебные крошки и дерево
# ---------------------------------------------------------------------------


async def _crumbs_line(user_id: int, folder_id: int) -> str:
    """Строка хлебных крошек «🏠 Папки / Рок / Классика» (уже экранированная)."""
    if folder_id <= 0:
        return ROOT_CRUMB
    crumbs = await folders_repo.folder_path(user_id, folder_id)
    names = [escape(crumb.get("name") or "Без названия") for crumb in crumbs]
    if len(names) > MAX_CRUMBS:
        names = ["…", *names[-MAX_CRUMBS:]]
    return " / ".join([ROOT_CRUMB, *names])


def _flatten_tree(nodes: Iterable[dict], depth: int = 0) -> list[dict]:
    """Разворачивает дерево папок в плоский список с сохранением порядка обхода."""
    result: list[dict] = []
    for node in nodes:
        item = dict(node)
        item.pop("children", None)
        item["depth"] = depth
        result.append(item)
        children = node.get("children") or []
        if children:
            result.extend(_flatten_tree(children, depth + 1))
    return result


def _indent(depth: int) -> str:
    """Отступ для подписи вложенной папки в плоском списке."""
    return "· " * max(0, int(depth))


# ---------------------------------------------------------------------------
# Экран уровня дерева
# ---------------------------------------------------------------------------


def _level_kb(
    *,
    folder: dict | None,
    children: Sequence[dict],
    tracks: Sequence[dict],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Клавиатура уровня: подпапки, треки, пагинация и действия с папкой."""
    builder = InlineKeyboardBuilder()
    folder_id = _to_int(folder.get("id")) if folder else 0

    for child in children:
        child_id = _to_int(child.get("id"))
        builder.row(
            InlineKeyboardButton(
                text=f"{_folder_icon(child)} {_short(child.get('name'))}",
                callback_data=FolderCB(action="open", folder_id=child_id, page=1).pack(),
            )
        )

    if folder is not None and tracks:
        ctx = folder_context(folder_id)
        start = page_offset(page) + 1
        for shift, track in enumerate(tracks):
            track_id = _to_int(track.get("id"))
            number = start + shift
            star = "⭐" if track.get("is_favourite") else "☆"
            builder.row(
                InlineKeyboardButton(
                    text=f"▶️ {number}",
                    callback_data=TrackCB(
                        action="play", track_id=track_id, page=page, ctx=ctx
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=star,
                    callback_data=TrackCB(
                        action="fav", track_id=track_id, page=page, ctx=ctx
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="➕",
                    callback_data=TrackCB(
                        action="addpl", track_id=track_id, page=page, ctx=ctx
                    ).pack(),
                ),
            )

    if total_pages > 1:
        current = _clamp_page(page, total_pages)
        prev_data = (
            FolderCB(action="page", folder_id=folder_id, page=current - 1).pack()
            if current > 1
            else _noop()
        )
        next_data = (
            FolderCB(action="page", folder_id=folder_id, page=current + 1).pack()
            if current < total_pages
            else _noop()
        )
        builder.row(
            InlineKeyboardButton(text="◀️" if current > 1 else "·", callback_data=prev_data),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=_noop()),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·", callback_data=next_data
            ),
        )

    if folder is None:
        builder.row(
            InlineKeyboardButton(
                text="🆕 Новая папка",
                callback_data=FolderCB(action="create", folder_id=0, page=page).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(text="⬅️ Меню", callback_data=NavCB(action="menu").pack())
        )
        return builder.as_markup()

    parent_id = _to_int(folder.get("parent_folder_id"))
    builder.row(
        InlineKeyboardButton(
            text="⬆️ Вверх",
            callback_data=FolderCB(action="open", folder_id=parent_id, page=1).pack(),
        ),
        InlineKeyboardButton(
            text="🏠 Все папки",
            callback_data=FolderCB(action="list", folder_id=0, page=1).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ Подпапка",
            callback_data=FolderCB(action="create", folder_id=folder_id, page=page).pack(),
        ),
        InlineKeyboardButton(
            text="▶️ Играть папку",
            callback_data=f"{PLAY_PREFIX}:{folder_id}:0",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Переименовать",
            callback_data=FolderCB(action="rename", folder_id=folder_id, page=page).pack(),
        ),
        InlineKeyboardButton(
            text="📦 Переместить",
            callback_data=FolderCB(action="move", folder_id=folder_id, page=page).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=FolderCB(action="delete", folder_id=folder_id, page=page).pack(),
        )
    )
    return builder.as_markup()


def _level_text(
    *,
    crumbs: str,
    folder: dict | None,
    total_folders: int,
    children: Sequence[dict],
    page_children: Sequence[dict],
    tracks: Sequence[dict],
    total_tracks: int,
    page: int,
    total_pages: int,
) -> str:
    """Текст экрана уровня: заголовок, крошки, подпапки и треки текущей страницы."""
    lines: list[str] = [crumbs]

    if folder is None:
        header = f"{FOLDERS_TITLE} — {_folders_label(len(children))} на верхнем уровне"
        if total_folders > len(children):
            header += f" (всего в разделе: {total_folders})"
        lines.append(header)
    else:
        name = escape(folder.get("name") or "Без названия")
        summary = [tracks_count_label(total_tracks)]
        total_files = _to_int(folder.get("total_track_count"))
        if total_files != total_tracks:
            summary.append(f"с подпапками — {_files_label(total_files)}")
        if children:
            summary.append(_folders_label(len(children)))
        lines.append(f"{_folder_icon(folder)} <b>{name}</b> — {' · '.join(summary)}")

    if not children and not tracks and total_tracks == 0:
        lines.append("")
        lines.append(texts.EMPTY_FOLDERS if folder is None else EMPTY_LEVEL)
        return "\n".join(lines)

    if page_children:
        lines.append("")
        lines.append(SUBFOLDERS_HEADER)
        for child in page_children:
            child_name = escape(child.get("name") or "Без названия")
            own = _to_int(child.get("track_count"))
            total_child = _to_int(child.get("total_track_count"), own)
            parts = [tracks_count_label(own)]
            if total_child != own:
                parts.append(f"с подпапками — {_files_label(total_child)}")
            if child.get("has_children"):
                parts.append(HAS_CHILDREN_MARK)
            lines.append(f"{_folder_icon(child)} <b>{child_name}</b> — {' · '.join(parts)}")

    if tracks:
        lines.append("")
        lines.append(TRACKS_HEADER)
        start = page_offset(page) + 1
        for shift, track in enumerate(tracks):
            lines.append(format_track_line(start + shift, track))
    elif folder is not None and total_tracks == 0 and children:
        lines.append("")
        lines.append("<i>Своих треков в этой папке нет — они лежат в подпапках.</i>")

    if total_pages > 1:
        lines.append("")
        lines.append(
            f"<i>{texts.PAGE_LABEL.format(page=_clamp_page(page, total_pages), total=total_pages)}</i>"
        )
    return "\n".join(lines)


async def _render_level(
    user_id: int, folder_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Собирает экран уровня дерева. ``None`` — папка не найдена."""
    folder: dict | None = None
    if folder_id > 0:
        folder = await folders_repo.get_folder(user_id, folder_id)
        if folder is None:
            return None

    section = str(folder.get("section") or SECTION) if folder else SECTION
    all_folders = await folders_repo.list_folders(user_id, section=section)
    parent_key = _to_int(folder.get("id")) if folder else None
    children = [
        item for item in all_folders if _as_parent(item.get("parent_folder_id")) == parent_key
    ]

    per_page = _page_size()
    total_tracks = 0
    if folder is not None:
        total_tracks = await tracks_repo.count_tracks(user_id, folder_id=parent_key)

    total_pages = max(
        _total_pages(len(children), per_page), _total_pages(total_tracks, per_page)
    )
    current = _clamp_page(page, total_pages)
    offset = page_offset(current, per_page)
    page_children = list(children[offset : offset + per_page])

    tracks: list[dict] = []
    if folder is not None and offset < total_tracks:
        tracks = await tracks_repo.list_tracks(
            user_id, folder_id=parent_key, limit=per_page, offset=offset
        )

    crumbs = await _crumbs_line(user_id, folder_id if folder else 0)
    text = _level_text(
        crumbs=crumbs,
        folder=folder,
        total_folders=len(all_folders),
        children=children,
        page_children=page_children,
        tracks=tracks,
        total_tracks=total_tracks,
        page=current,
        total_pages=total_pages,
    )
    markup = _level_kb(
        folder=folder,
        children=page_children,
        tracks=tracks,
        page=current,
        total_pages=total_pages,
    )
    return text, markup


def _as_parent(value: Any) -> int | None:
    """Нормализует ``parent_folder_id``: 0 и None означают «корень»."""
    if value is None:
        return None
    parent = _to_int(value)
    return parent if parent > 0 else None


async def _show_level(
    event: Message | CallbackQuery, bot: Bot, user_id: int, folder_id: int, page: int
) -> bool:
    """Показывает уровень дерева; False — папка не найдена."""
    rendered = await _render_level(user_id, folder_id, page)
    if rendered is None:
        return False
    text, markup = rendered
    if isinstance(event, CallbackQuery):
        await _show(event, bot, text, markup)
    else:
        await event.answer(_trim_message(text), reply_markup=markup)
    return True


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@router.message(Command("folders"))
async def cmd_folders(message: Message, state: FSMContext, bot: Bot) -> None:
    """`/folders` — корень дерева папок."""
    user_id = _user_id(message)
    if user_id is None:
        return
    await _reset_folder_state(state)
    await _show_level(message, bot, user_id, 0, 1)


@router.message(Command("create_folder"))
async def cmd_create_folder(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/create_folder [название]` — создание корневой папки раздела «Треки»."""
    user_id = _user_id(message)
    if user_id is None:
        return

    name = (command.args or "").strip()
    if not name:
        await state.set_state(FolderStates.waiting_name)
        await state.update_data(**{STATE_PARENT_KEY: 0, STATE_PAGE_KEY: 1})
        await message.answer(f"{texts.FOLDER_NAME_PROMPT}\n\n{texts.CANCEL_HINT}")
        return

    error_text = _validate_name(name)
    if error_text:
        await message.answer(error_text)
        return

    try:
        folder = await folders_repo.create_folder(user_id, name, section=SECTION)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось создать папку: %s", user_id, error)
        await message.answer(f"❌ {escape(str(error))}")
        return

    logger.info(
        "Пользователь %s создал папку «%s» (id=%s)", user_id, folder["name"], folder["id"]
    )
    header = texts.FOLDER_CREATED.format(folder=escape(folder["name"]))
    rendered = await _render_level(user_id, int(folder["id"]), 1)
    if rendered is None:
        await message.answer(header)
        return
    await message.answer(_trim_message(f"{header}\n\n{rendered[0]}"), reply_markup=rendered[1])


@router.message(Command("play_folder"))
async def cmd_play_folder(
    message: Message, command: CommandObject, state: FSMContext, bot: Bot
) -> None:
    """`/play_folder [название]` — воспроизведение папки вместе с подпапками."""
    user_id = _user_id(message)
    if user_id is None:
        return
    await _reset_folder_state(state)

    name = (command.args or "").strip()
    if not name:
        rendered = await _render_level(user_id, 0, 1)
        if rendered is None:  # pragma: no cover - корень существует всегда
            await message.answer(texts.EMPTY_FOLDERS)
            return
        await message.answer(
            _trim_message(f"{PLAY_PICK_HINT}\n\n{rendered[0]}"), reply_markup=rendered[1]
        )
        return

    folder = await folders_repo.find_folder_by_name(user_id, name, section=SECTION)
    if folder is None:
        rendered = await _render_level(user_id, 0, 1)
        header = PLAY_UNKNOWN_FOLDER.format(name=escape(name))
        if rendered is None:  # pragma: no cover - корень существует всегда
            await message.answer(header)
            return
        await message.answer(
            _trim_message(f"{header}\n\n{rendered[0]}"), reply_markup=rendered[1]
        )
        return

    await _play_folder(bot, user_id, message.chat.id, int(folder["id"]), 0, notify=message)


# ---------------------------------------------------------------------------
# Колбэки папок (FolderCB)
# ---------------------------------------------------------------------------


@router.callback_query(FolderCB.filter())
async def on_folder_callback(
    callback: CallbackQuery,
    callback_data: FolderCB,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Единый обработчик действий с папками.

    ``folder_id > 0`` — работа с конкретной папкой (и пагинация её содержимого),
    ``folder_id <= 0`` — корень дерева.
    """
    user_id = _user_id(callback)
    if user_id is None:
        await ack(callback)
        return

    action = (callback_data.action or "").strip()
    folder_id = _to_int(callback_data.folder_id)
    page = max(1, _to_int(callback_data.page, 1))

    if action == "create":
        await _start_create(callback, bot, state, user_id, folder_id, page)
        return

    if action == "rename":
        await _start_rename(callback, bot, state, user_id, folder_id, page)
        return

    if action == "delete":
        await _ask_delete(callback, bot, user_id, folder_id, page)
        return

    if action == "move":
        await _ask_move(callback, bot, user_id, folder_id, page, 1)
        return

    if action in {"open", "page", "pick", "list", "back"}:
        target = folder_id if action not in {"list", "back"} else 0
        if not await _show_level(callback, bot, user_id, target, page):
            await _show_level(callback, bot, user_id, 0, 1)
            await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
            return
        await ack(callback)
        return

    logger.debug("Неизвестное действие с папкой: %r", action)
    await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)


async def _start_create(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user_id: int,
    folder_id: int,
    page: int,
) -> None:
    """Спрашивает название новой папки (корневой или вложенной)."""
    parent_name = ""
    if folder_id > 0:
        parent = await folders_repo.get_folder(user_id, folder_id)
        if parent is None:
            await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
            await _show_level(callback, bot, user_id, 0, 1)
            return
        parent_name = str(parent.get("name") or "")

    await state.set_state(FolderStates.waiting_name)
    await state.update_data(**{STATE_PARENT_KEY: max(folder_id, 0), STATE_PAGE_KEY: page})
    prompt = (
        SUBFOLDER_NAME_PROMPT.format(folder=escape(parent_name))
        if folder_id > 0
        else texts.FOLDER_NAME_PROMPT
    )
    await _show(callback, bot, f"{prompt}\n\n{texts.CANCEL_HINT}")
    await ack(callback)


async def _start_rename(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user_id: int,
    folder_id: int,
    page: int,
) -> None:
    """Спрашивает новое название существующей папки."""
    folder = await folders_repo.get_folder(user_id, folder_id) if folder_id > 0 else None
    if folder is None:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return

    await state.set_state(FolderStates.waiting_rename)
    await state.update_data(**{STATE_TARGET_KEY: folder_id, STATE_PAGE_KEY: page})
    await _show(
        callback,
        bot,
        f"{texts.FOLDER_RENAME_PROMPT}\n"
        f"Текущее название: «{escape(folder['name'])}».\n\n{texts.CANCEL_HINT}",
    )
    await ack(callback)


# ---------------------------------------------------------------------------
# Удаление папки
# ---------------------------------------------------------------------------


async def _ask_delete(
    callback: CallbackQuery, bot: Bot, user_id: int, folder_id: int, page: int
) -> None:
    """Показывает подтверждение удаления с выбором судьбы треков."""
    folder = await folders_repo.get_folder(user_id, folder_id) if folder_id > 0 else None
    if folder is None:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return

    subtree = await folders_repo.descendant_ids(user_id, folder_id, include_self=True)
    nested = max(0, len(subtree) - 1)
    total_files = _to_int(folder.get("total_track_count"))

    name = escape(folder.get("name") or "Без названия")
    lines = [CONFIRM_DELETE_TITLE.format(folder=name)]
    lines.append(
        f"Внутри: {_files_label(total_files)} (вместе с подпапками)."
        if nested
        else f"Внутри: {_files_label(total_files)}."
    )
    if nested:
        lines.append(SUBFOLDERS_WARNING.format(count=_folders_label(nested)))
    lines.append("")
    lines.append(
        "«Треки оставить» — файлы останутся в библиотеке без папки. "
        "«Вместе с треками» — записи удалятся и из библиотеки, и из канала-хранилища."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=DELETE_KEEP_TRACKS,
            callback_data=f"{DELETE_CONFIRM_PREFIX}:{folder_id}:{page}:0",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=DELETE_WITH_TRACKS,
            callback_data=f"{DELETE_CONFIRM_PREFIX}:{folder_id}:{page}:1",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=DELETE_CANCEL,
            callback_data=FolderCB(action="open", folder_id=folder_id, page=page).pack(),
        )
    )

    await _show(callback, bot, "\n".join(lines), builder.as_markup())
    await ack(callback)


@router.callback_query(F.data.func(_is_delete_confirm))
async def on_delete_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    """Подтверждённое удаление папки (вместе с подпапками; треки — по выбору)."""
    user_id = _user_id(callback)
    if user_id is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_local(callback.data, DELETE_CONFIRM_PREFIX, 3)
    if parsed is None:
        logger.warning("Не удалось разобрать подтверждение удаления: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    folder_id, page, flag = parsed
    delete_tracks = bool(flag)
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        await _show_level(callback, bot, user_id, 0, 1)
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return

    parent_id = _to_int(folder.get("parent_folder_id"))
    name = escape(folder.get("name") or "Без названия")

    doomed: list[dict] = []
    if delete_tracks:
        doomed = await tracks_repo.list_tracks(
            user_id,
            folder_id=folder_id,
            folder_recursive=True,
            file_type=None,
            limit=CHANNEL_CLEANUP_LIMIT,
            offset=0,
        )
    total_files = _to_int(folder.get("total_track_count"))

    try:
        deleted = await folders_repo.delete_folder(
            user_id, folder_id, delete_tracks=delete_tracks, recursive=True
        )
    except ValidationError as error:
        await ack(callback, _clip(str(error)), alert=True)
        return
    except MusicBoxError:
        logger.exception("Не удалось удалить папку %s пользователя %s", folder_id, user_id)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    if not deleted:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_level(callback, bot, user_id, 0, 1)
        return

    logger.info(
        "Пользователь %s удалил папку %s (треки удалены: %s)", user_id, folder_id, delete_tracks
    )

    if delete_tracks:
        header = FOLDER_DELETED_WITH_TRACKS.format(
            folder=name, count=_files_label(total_files)
        )
        removed = await _cleanup_channel(bot, doomed)
        if total_files > len(doomed):
            header += "\n" + CHANNEL_CLEANUP_PARTIAL.format(count=removed)
        elif removed:
            header += "\n" + CHANNEL_CLEANUP_DONE.format(count=removed)
    else:
        header = texts.FOLDER_DELETED.format(folder=name)

    rendered = await _render_level(user_id, parent_id, 1)
    if rendered is None:
        rendered = await _render_level(user_id, 0, 1)
    if rendered is None:  # pragma: no cover - корень существует всегда
        await _show(callback, bot, header)
    else:
        await _show(callback, bot, f"{header}\n\n{rendered[0]}", rendered[1])
    await ack(callback, "Папка удалена")


async def _cleanup_channel(bot: Bot, tracks: Sequence[dict]) -> int:
    """Удаляет копии треков из канала-хранилища; возвращает число удалённых."""
    removed = 0
    for track in tracks:
        message_id = track.get("storage_message_id")
        if not message_id:
            continue
        try:
            if await storage.delete_from_channel(bot, int(message_id)):
                removed += 1
        except (StorageError, TelegramAPIError) as error:  # pragma: no cover - сеть
            logger.warning("Не удалось удалить сообщение %s из канала: %s", message_id, error)
        await asyncio.sleep(CLEANUP_DELAY)
    return removed


# ---------------------------------------------------------------------------
# Перемещение папки
# ---------------------------------------------------------------------------


async def _move_targets(user_id: int, folder: dict) -> list[dict]:
    """Папки, куда можно перенести ``folder``: без неё самой, её потомков и текущего родителя."""
    section = str(folder.get("section") or SECTION)
    tree = await folders_repo.folder_tree(user_id, section=section)
    flat = _flatten_tree(tree)
    forbidden = set(await folders_repo.descendant_ids(user_id, int(folder["id"]), include_self=True))
    current_parent = _as_parent(folder.get("parent_folder_id"))
    return [
        item
        for item in flat
        if _to_int(item.get("id")) not in forbidden
        and _to_int(item.get("id")) != current_parent
    ]


async def _ask_move(
    callback: CallbackQuery,
    bot: Bot,
    user_id: int,
    folder_id: int,
    page: int,
    pick_page: int,
) -> None:
    """Показывает выбор нового родителя для папки."""
    folder = await folders_repo.get_folder(user_id, folder_id) if folder_id > 0 else None
    if folder is None:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return

    targets = await _move_targets(user_id, folder)
    per_page = _page_size()
    total_pages = _total_pages(len(targets), per_page)
    current = _clamp_page(pick_page, total_pages)
    offset = page_offset(current, per_page)
    page_items = targets[offset : offset + per_page]

    name = escape(folder.get("name") or "Без названия")
    crumbs = await _crumbs_line(user_id, folder_id)
    lines = [MOVE_TITLE.format(folder=name), MOVE_CURRENT.format(path=crumbs)]
    at_root = _as_parent(folder.get("parent_folder_id")) is None
    if not targets and at_root:
        lines.append("")
        lines.append(MOVE_NO_TARGETS)
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    builder = InlineKeyboardBuilder()
    if not at_root:
        builder.row(
            InlineKeyboardButton(
                text=MOVE_TO_ROOT,
                callback_data=f"{MOVE_APPLY_PREFIX}:{folder_id}:0:{page}",
            )
        )
    for item in page_items:
        target_id = _to_int(item.get("id"))
        label = f"{_indent(_to_int(item.get('depth')))}{_folder_icon(item)} {_short(item.get('name'))}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{MOVE_APPLY_PREFIX}:{folder_id}:{target_id}:{page}",
            )
        )
    if total_pages > 1:
        prev_data = (
            f"{MOVE_PAGE_PREFIX}:{folder_id}:{current - 1}" if current > 1 else _noop()
        )
        next_data = (
            f"{MOVE_PAGE_PREFIX}:{folder_id}:{current + 1}"
            if current < total_pages
            else _noop()
        )
        builder.row(
            InlineKeyboardButton(text="◀️" if current > 1 else "·", callback_data=prev_data),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=_noop()),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·", callback_data=next_data
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=DELETE_CANCEL,
            callback_data=FolderCB(action="open", folder_id=folder_id, page=page).pack(),
        )
    )

    await _show(callback, bot, "\n".join(lines), builder.as_markup())
    await ack(callback)


@router.callback_query(F.data.func(_is_move_page))
async def on_move_page(callback: CallbackQuery, bot: Bot) -> None:
    """Пагинация списка папок-получателей."""
    user_id = _user_id(callback)
    if user_id is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_local(callback.data, MOVE_PAGE_PREFIX, 2)
    if parsed is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    folder_id, pick_page = parsed
    await _ask_move(callback, bot, user_id, folder_id, 1, max(1, pick_page))


@router.callback_query(F.data.func(_is_move_apply))
async def on_move_apply(callback: CallbackQuery, bot: Bot) -> None:
    """Переносит папку к выбранному родителю (0 — в корень раздела)."""
    user_id = _user_id(callback)
    if user_id is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_local(callback.data, MOVE_APPLY_PREFIX, 3)
    if parsed is None:
        logger.warning("Не удалось разобрать перенос папки: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    folder_id, target_id, page = parsed
    new_parent = target_id if target_id > 0 else None

    try:
        moved = await folders_repo.move_folder(user_id, folder_id, new_parent)
    except NotFoundError:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        await _show_level(callback, bot, user_id, 0, 1)
        return
    except ValidationError as error:
        # Цикл, чужой раздел, предел вложенности или тёзка на уровне —
        # текст ошибки уже русский и понятный, показываем его как есть.
        message = str(error)
        hint = DEPTH_HINT if "вложенност" in message.casefold() else CYCLE_HINT
        logger.info("Пользователь %s: перенос папки %s отклонён: %s", user_id, folder_id, message)
        await ack(callback, _clip(f"{message}. {hint}"), alert=True)
        await _ask_move(callback, bot, user_id, folder_id, max(1, page), 1)
        return
    except MusicBoxError:
        logger.exception("Не удалось перенести папку %s пользователя %s", folder_id, user_id)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    name = escape(moved.get("name") or "Без названия")
    if new_parent is None:
        header = MOVE_DONE_ROOT.format(folder=name)
    else:
        parent = await folders_repo.get_folder(user_id, new_parent)
        parent_name = escape((parent or {}).get("name") or "Без названия")
        header = MOVE_DONE_INTO.format(folder=name, parent=parent_name)

    logger.info(
        "Пользователь %s перенёс папку %s к родителю %s", user_id, folder_id, new_parent
    )
    rendered = await _render_level(user_id, int(moved["id"]), 1)
    if rendered is None:  # pragma: no cover - папка только что существовала
        await _show(callback, bot, header)
    else:
        await _show(callback, bot, f"{header}\n\n{rendered[0]}", rendered[1])
    await ack(callback, "Папка перенесена")


# ---------------------------------------------------------------------------
# FSM: создание и переименование папки
# ---------------------------------------------------------------------------


@router.message(StateFilter(FolderStates.waiting_name), Command("cancel"))
@router.message(StateFilter(FolderStates.waiting_rename), Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает ввод названия папки."""
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(StateFilter(FolderStates.waiting_name), F.text)
async def on_folder_name(message: Message, state: FSMContext) -> None:
    """Создаёт папку (корневую или подпапку текущей) по введённому названию."""
    user_id = _user_id(message)
    if user_id is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return

    error_text = _validate_name(text)
    if error_text:
        await message.answer(error_text)
        return

    data = await state.get_data()
    parent_id = max(0, _to_int(data.get(STATE_PARENT_KEY)))
    page = max(1, _to_int(data.get(STATE_PAGE_KEY), 1))

    parent: dict | None = None
    if parent_id > 0:
        parent = await folders_repo.get_folder(user_id, parent_id)
        if parent is None:
            await state.clear()
            await message.answer(
                f"{texts.FOLDER_NOT_FOUND}\nОткройте список папок: /folders"
            )
            return

    try:
        folder = await folders_repo.create_folder(
            user_id,
            text,
            parent_folder_id=parent_id or None,
            section=str((parent or {}).get("section") or SECTION),
        )
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось создать папку: %s", user_id, error)
        await state.clear()
        await message.answer(f"❌ {escape(str(error))}")
        return

    await state.clear()
    logger.info(
        "Пользователь %s создал папку «%s» (id=%s, родитель=%s)",
        user_id,
        folder["name"],
        folder["id"],
        folder["parent_folder_id"],
    )
    if parent is not None:
        header = FOLDER_CREATED_IN.format(
            folder=escape(folder["name"]), parent=escape(parent["name"])
        )
    else:
        header = texts.FOLDER_CREATED.format(folder=escape(folder["name"]))

    rendered = await _render_level(user_id, parent_id, page)
    if rendered is None:
        rendered = await _render_level(user_id, 0, 1)
    if rendered is None:  # pragma: no cover - корень существует всегда
        await message.answer(header)
        return
    await message.answer(_trim_message(f"{header}\n\n{rendered[0]}"), reply_markup=rendered[1])


@router.message(StateFilter(FolderStates.waiting_rename), F.text)
async def on_folder_rename(message: Message, state: FSMContext) -> None:
    """Переименовывает папку по введённому названию."""
    user_id = _user_id(message)
    if user_id is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return

    error_text = _validate_name(text)
    if error_text:
        await message.answer(error_text)
        return

    data = await state.get_data()
    folder_id = _to_int(data.get(STATE_TARGET_KEY))
    page = max(1, _to_int(data.get(STATE_PAGE_KEY), 1))
    if folder_id <= 0:
        await state.clear()
        await message.answer(f"{texts.FOLDER_NOT_FOUND}\nОткройте список папок: /folders")
        return

    try:
        folder = await folders_repo.rename_folder(user_id, folder_id, text)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось переименовать папку: %s", user_id, error)
        await state.clear()
        await message.answer(f"❌ {escape(str(error))}")
        return

    await state.clear()
    if folder is None:
        await message.answer(texts.FOLDER_NOT_FOUND)
        return

    logger.info(
        "Пользователь %s переименовал папку %s в «%s»", user_id, folder_id, folder["name"]
    )
    header = texts.FOLDER_RENAMED.format(folder=escape(folder["name"]))
    rendered = await _render_level(user_id, folder_id, page)
    if rendered is None:
        await message.answer(header)
        return
    await message.answer(_trim_message(f"{header}\n\n{rendered[0]}"), reply_markup=rendered[1])


# ---------------------------------------------------------------------------
# Воспроизведение папки (рекурсивно, вместе с подпапками)
# ---------------------------------------------------------------------------


def _play_more_kb(folder_id: int, offset: int, left: int) -> InlineKeyboardMarkup | None:
    """Кнопка «⏭ Ещё N» под последней отправленной пачкой."""
    if left <= 0:
        return None
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=PLAY_MORE.format(count=min(left, PLAY_BATCH)),
            callback_data=f"{PLAY_PREFIX}:{folder_id}:{offset}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📁 К папке",
            callback_data=FolderCB(action="open", folder_id=folder_id, page=1).pack(),
        )
    )
    return builder.as_markup()


async def _play_folder(
    bot: Bot,
    user_id: int,
    chat_id: int,
    folder_id: int,
    offset: int,
    *,
    notify: Message | None = None,
    callback: CallbackQuery | None = None,
) -> None:
    """Отправляет очередную пачку треков папки (рекурсивно) и считает прослушивания."""
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        if callback is not None:
            await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        elif notify is not None:
            await notify.answer(texts.FOLDER_NOT_FOUND)
        return

    total = await tracks_repo.count_tracks(
        user_id, folder_id=folder_id, folder_recursive=True
    )
    if total <= 0:
        if callback is not None:
            await ack(callback, _clip(texts.NOTHING_TO_PLAY), alert=True)
        elif notify is not None:
            await notify.answer(texts.NOTHING_TO_PLAY)
        return

    start = max(0, int(offset))
    if start >= total:
        # Кнопка «Ещё» из старого сообщения: треки уже закончились.
        if callback is not None:
            await ack(callback, _clip(PLAY_DONE))
        elif notify is not None:
            await notify.answer(PLAY_DONE)
        return

    batch = await tracks_repo.list_tracks(
        user_id,
        folder_id=folder_id,
        folder_recursive=True,
        order="title",
        limit=PLAY_BATCH,
        offset=start,
    )
    if not batch:
        if callback is not None:
            await ack(callback, _clip(PLAY_DONE))
        elif notify is not None:
            await notify.answer(PLAY_DONE)
        return

    name = escape(folder.get("name") or "Без названия")
    header = PLAY_HEADER.format(folder=name, count=tracks_count_label(total))
    if callback is not None:
        await ack(callback, _clip(f"Отправляю {tracks_count_label(len(batch))}…"))
    elif notify is not None:
        await notify.answer(header)

    sent = 0
    failed = 0
    for track in batch:
        try:
            await media.send_media_to_user(bot, chat_id, track)
        except (StorageError, TelegramAPIError, MusicBoxError) as error:
            failed += 1
            logger.warning(
                "Не удалось отправить трек %s пользователю %s: %s",
                track.get("id"),
                user_id,
                error,
            )
        else:
            sent += 1
            await tracks_repo.register_play(
                user_id, int(track["id"]), source="bot_play_folder"
            )
        await asyncio.sleep(PLAY_DELAY)

    done = start + len(batch)
    left = max(0, total - done)
    lines = [PLAY_SENT.format(sent=min(done, total), total=total)]
    if failed:
        lines.append(PLAY_FAILED.format(count=failed))
    if not left:
        lines.append(PLAY_DONE)

    logger.info(
        "Пользователь %s: папка %s — отправлено %s треков (осталось %s)",
        user_id,
        folder_id,
        sent,
        left,
    )
    await bot.send_message(
        chat_id, "\n".join(lines), reply_markup=_play_more_kb(folder_id, done, left)
    )


@router.callback_query(F.data.func(_is_play))
async def on_play_folder(callback: CallbackQuery, bot: Bot) -> None:
    """«▶️ Играть папку» и «⏭ Ещё N» — отправка треков пачками по 10."""
    user_id = _user_id(callback)
    if user_id is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_local(callback.data, PLAY_PREFIX, 2)
    if parsed is None:
        logger.warning("Не удалось разобрать запуск папки: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    folder_id, offset = parsed
    chat = getattr(callback.message, "chat", None)
    chat_id = int(chat.id) if chat is not None else user_id
    await _play_folder(bot, user_id, chat_id, folder_id, max(0, offset), callback=callback)


# ---------------------------------------------------------------------------
# Перемещение трека в папку
# ---------------------------------------------------------------------------


@router.callback_query(TrackCB.filter(F.action == "move"))
async def on_track_move(callback: CallbackQuery, callback_data: TrackCB, bot: Bot) -> None:
    """Показывает клавиатуру выбора папки для трека.

    Список папок разворачивается из дерева, поэтому вложенные папки видны с
    отступом. Сам перенос выполняет обработчик ``MoveCB`` в ``handlers/upload.py``.
    """
    user_id = _user_id(callback)
    if user_id is None:
        await ack(callback)
        return

    track_id = _to_int(callback_data.track_id)
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    tree = await folders_repo.folder_tree(user_id, section=SECTION)
    flat = _flatten_tree(tree)
    shown = flat[:MAX_PICK_FOLDERS]
    # Копии с отступом в названии: folder_pick_kb сама собирает подписи кнопок.
    items = [
        {**item, "name": f"{_indent(_to_int(item.get('depth')))}{item.get('name') or ''}"}
        for item in shown
    ]

    lines = [
        "📂 Выберите папку для трека "
        f"<b>{escape(track.get('title') or 'Без названия')}</b>."
    ]
    if track.get("folder_name"):
        lines.append(f"Сейчас он в папке «{escape(track['folder_name'])}».")
    if len(flat) > MAX_PICK_FOLDERS:
        lines.append(
            f"Показаны первые {MAX_PICK_FOLDERS} папок — остальные доступны в Mini App."
        )

    markup = folder_pick_kb(items, track_id)
    text = "\n".join(lines)
    message = callback.message
    if isinstance(message, Message):
        await message.answer(text, reply_markup=markup)
    else:
        await bot.send_message(user_id, text, reply_markup=markup)
    await ack(callback)


__all__ = ["router"]
