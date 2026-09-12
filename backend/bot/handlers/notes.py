"""Хендлеры заметок со списками (раздел 5 контракта V2, ТЗ п. 5).

Команда ``/notes``: список заметок с прогрессом «сделано/всего», создание,
переименование и удаление заметки с подтверждением. Внутри заметки — пункты
с чекбоксами «☑️/⬜»: нажатие переключает отметку через ``notes_repo.toggle_item``
и тут же перерисовывает сообщение. Режим правки даёт у каждого пункта кнопки
«⬆️ / ⬇️ / 🗑» (перестановка идёт через ``notes_repo.reorder_items``), а нажатие
на текст пункта открывает его редактирование. Заметки и пункты листаются
по ``settings.page_size``.

Все нажатия приходят через фабрику :class:`NoteCB` (префикс ``nt``):

* ``action`` — что делать;
* ``note_id`` — заметка (``0`` — действие относится к списку заметок);
* ``item_id`` — пункт заметки (``0`` — действие не про конкретный пункт);
* ``page`` — страница списка заметок или списка пунктов, 1-based.

Действия: ``list`` / ``back`` (список заметок), ``open`` / ``page`` (заметка),
``edit`` (заметка в режиме правки пунктов), ``create``, ``rename``, ``delete``
(``item_id == CONFIRM_FLAG`` — удаление подтверждено), ``additem``, ``toggle``,
``edititem``, ``delitem``, ``up``, ``down``, ``noop`` (кнопка-индикатор страницы).
``edit`` и ``noop`` — экранные действия этого модуля, остальные соответствуют
перечню в ``backend/bot/callbacks.py``.

Клавиатуры и тексты раздела объявлены локально (``keyboards.py`` и ``texts.py``
правятся другими модулями); из V1 переиспользуется только ``confirm_kb``.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import NoteCB
from backend.bot.keyboards import confirm_kb
from backend.bot.states import NoteStates
from backend.bot.utils import ack, escape, page_offset, paginate, safe_edit
from backend.config import settings
from backend.db.repositories import notes as notes_repo
from backend.errors import MusicBoxError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

router = Router(name="notes")


# ---------------------------------------------------------------------------
# Константы модуля
# ---------------------------------------------------------------------------

#: Значение числового поля NoteCB, когда идентификатор не нужен.
NO_ID: Final[int] = 0
#: Значение ``item_id`` в ``delete``, означающее «удаление подтверждено»
#: (тот же приём, что и ``CONFIRM_FLAG`` в handlers/playlists.py).
CONFIRM_FLAG: Final[int] = 1

#: Запас до лимита сообщения Telegram (4096 символов) — с учётом HTML-разметки.
MESSAGE_LIMIT: Final[int] = 3800
#: Максимальная длина одной строки списка в тексте сообщения.
LINE_LIMIT: Final[int] = 200
#: Максимальная длина подписи на кнопке (пункт занимает весь ряд).
LABEL_LIMIT: Final[int] = 40
#: Подпись заметки в списке — короче на длину суффикса с прогрессом.
NOTE_LABEL_LIMIT: Final[int] = 30
#: Подпись пункта в режиме правки — рядом ещё три кнопки, места мало.
EDIT_LABEL_LIMIT: Final[int] = 20
#: Ширина полоски прогресса заметки.
PROGRESS_WIDTH: Final[int] = 10

#: Ключи данных FSM.
MODE_KEY: Final[str] = "note_mode"
NOTE_KEY: Final[str] = "note_id"
ITEM_KEY: Final[str] = "note_item_id"
PAGE_KEY: Final[str] = "note_page"
EDIT_KEY: Final[str] = "note_edit_mode"

#: Режимы ввода названия (``NoteStates.waiting_title``).
MODE_CREATE: Final[str] = "create"
MODE_RENAME: Final[str] = "rename"


# ---------------------------------------------------------------------------
# Пользовательские тексты раздела (RU)
# ---------------------------------------------------------------------------

NOTES_TITLE: Final[str] = "🗒 <b>Заметки</b>"
EMPTY_NOTES: Final[str] = (
    "Пока нет ни одной заметки.\n"
    "Нажмите «🆕 Новая заметка» — получится список дел с галочками."
)
EMPTY_ITEMS: Final[str] = "В заметке пока нет пунктов. Нажмите «➕ Пункт», чтобы добавить."

NOTE_TITLE_PROMPT: Final[str] = "✍️ Пришлите название новой заметки."
NOTE_RENAME_PROMPT: Final[str] = "✍️ Пришлите новое название заметки."
ITEM_TEXT_PROMPT: Final[str] = "✍️ Пришлите текст пункта."
ITEM_EDIT_PROMPT: Final[str] = "✍️ Пришлите новый текст пункта."
CANCEL_HINT: Final[str] = "Чтобы отменить, отправьте /cancel."
NEXT_ITEM_HINT: Final[str] = "Пришлите следующий пункт или /cancel, чтобы закончить."
EDIT_HINT: Final[str] = (
    "<i>Режим правки: ⬆️/⬇️ меняют порядок, 🗑 удаляет пункт, "
    "нажатие на текст — изменить формулировку.</i>"
)

NOTE_NOT_FOUND: Final[str] = "🤷 Заметка не найдена — возможно, она уже удалена."
ITEM_NOT_FOUND: Final[str] = "🤷 Пункт не найден — возможно, он уже удалён."
NOTE_LOST: Final[str] = f"{NOTE_NOT_FOUND}\nОткройте список заметок: /notes"

NOTE_CREATED: Final[str] = "✅ Заметка «{title}» создана."
NOTE_RENAMED: Final[str] = "✅ Заметка переименована в «{title}»."
NOTE_DELETED: Final[str] = "🗑 Заметка «{title}» удалена."
CONFIRM_DELETE_NOTE: Final[str] = "🗑 Удалить заметку «{title}» вместе со всеми пунктами?"

TITLE_EMPTY: Final[str] = "✍️ Название не может быть пустым."
ITEM_EMPTY: Final[str] = "✍️ Текст пункта не может быть пустым."

ITEM_ADDED: Final[str] = "✅ Пункт добавлен."
ITEM_UPDATED_SHORT: Final[str] = "Пункт изменён"
ITEM_DELETED_SHORT: Final[str] = "Пункт удалён"
NOTE_DELETED_SHORT: Final[str] = "Заметка удалена"
ORDER_UPDATED_SHORT: Final[str] = "Порядок обновлён"
ITEM_FIRST: Final[str] = "Пункт уже первый"
ITEM_LAST: Final[str] = "Пункт уже последний"
MARK_SET: Final[str] = "Отмечено"
MARK_CLEARED: Final[str] = "Отметка снята"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _per_page() -> int:
    """Размер страницы списков (из настроек, с разумными границами)."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, 50))


def _clamp_page(page: Any, total_pages: int) -> int:
    """Приводит номер страницы к диапазону 1..total_pages."""
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), max(1, int(total_pages)))


def _positive(value: Any) -> int:
    """Мягкое приведение идентификатора к неотрицательному int."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _short(text: Any, limit: int = LABEL_LIMIT) -> str:
    """Схлопывает пробелы и обрезает текст (подписи кнопок, всплывающие ответы)."""
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "…"


def _fit(lines: Sequence[str], limit: int = MESSAGE_LIMIT) -> str:
    """Склеивает строки, не превышая лимит сообщения Telegram.

    Строки добавляются целиком — так HTML-разметка не рвётся посередине тега.
    Если помещается не всё, в конце появляется честное многоточие.
    """
    collected: list[str] = []
    length = 0
    for line in lines:
        extra = len(line) + (1 if collected else 0)
        if length + extra > limit:
            collected.append("…")
            break
        collected.append(line)
        length += extra
    return "\n".join(collected)


def _progress_bar(done: int, total: int, width: int = PROGRESS_WIDTH) -> str:
    """Полоска прогресса вида ``▰▰▰▱▱▱▱▱▱▱``."""
    if total <= 0:
        return ""
    if done >= total:
        return "▰" * width
    filled = max(0, min(width - 1, int(width * done / total)))
    return "▰" * filled + "▱" * (width - filled)


def _progress_label(done: int, total: int) -> str:
    """Короткая подпись прогресса для списка заметок и кнопок."""
    if total <= 0:
        return "пусто"
    if done >= total:
        return f"✅ {done}/{total}"
    return f"{done}/{total}"


def _counters(note: dict[str, Any]) -> tuple[int, int]:
    """Пара «выполнено, всего» из заметки."""
    try:
        done = int(note.get("items_done") or 0)
    except (TypeError, ValueError):
        done = 0
    try:
        total = int(note.get("items_total") or 0)
    except (TypeError, ValueError):
        total = 0
    return max(0, done), max(0, total)


def _item_page(position: int) -> int:
    """Номер страницы, на которой окажется пункт с указанной позицией (1-based)."""
    return (max(1, int(position)) - 1) // _per_page() + 1


async def _show(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показывает экран: правит текущее сообщение либо отправляет новое."""
    message = callback.message
    if message is not None:
        await safe_edit(message, text, markup)
        return
    if callback.from_user is not None:
        await bot.send_message(int(callback.from_user.id), text, reply_markup=markup)


async def _reset_state(state: FSMContext) -> None:
    """Сбрасывает незавершённый диалог заметок."""
    current = await state.get_state()
    if current in {NoteStates.waiting_title.state, NoteStates.waiting_item.state}:
        await state.clear()


def _is_command(text: str) -> bool:
    """Введённый текст — другая команда, а не ответ на вопрос бота."""
    return text.startswith("/")


# ---------------------------------------------------------------------------
# Клавиатуры раздела (локальные)
# ---------------------------------------------------------------------------


def _button(
    text: str,
    *,
    action: str,
    note_id: int = NO_ID,
    item_id: int = NO_ID,
    page: int = 1,
) -> InlineKeyboardButton:
    """Кнопка раздела с упакованными данными :class:`NoteCB`."""
    return InlineKeyboardButton(
        text=text,
        callback_data=NoteCB(
            action=action, note_id=note_id, item_id=item_id, page=page
        ).pack(),
    )


def _pagination_row(
    *, action: str, note_id: int, page: int, total_pages: int
) -> list[InlineKeyboardButton]:
    """Ряд «◀️ N/M ▶️»; пустой список, если страница всего одна."""
    if total_pages <= 1:
        return []
    previous = page - 1 if page > 1 else total_pages
    following = page + 1 if page < total_pages else 1
    return [
        _button("◀️", action=action, note_id=note_id, page=previous),
        _button(f"{page}/{total_pages}", action="noop", note_id=note_id, page=page),
        _button("▶️", action=action, note_id=note_id, page=following),
    ]


def _notes_kb(
    notes: Sequence[dict[str, Any]], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    """Клавиатура списка заметок: кнопка на заметку, пагинация, создание."""
    builder = InlineKeyboardBuilder()
    for note in notes:
        done, total = _counters(note)
        builder.row(
            _button(
                f"📋 {_short(note.get('title') or 'Без названия', NOTE_LABEL_LIMIT)} ·"
                f" {_progress_label(done, total)}",
                action="open",
                note_id=_positive(note.get("id")),
                page=1,
            )
        )

    pagination = _pagination_row(
        action="list", note_id=NO_ID, page=page, total_pages=total_pages
    )
    if pagination:
        builder.row(*pagination)

    builder.row(_button("🆕 Новая заметка", action="create", page=page))
    return builder.as_markup()


def _note_kb(
    note_id: int,
    items: Sequence[dict[str, Any]],
    page: int,
    total_pages: int,
    *,
    edit_mode: bool,
    start_index: int,
) -> InlineKeyboardMarkup:
    """Клавиатура заметки.

    Обычный вид — кнопка-чекбокс на каждый пункт (нажатие переключает отметку).
    Режим правки — «✏️ текст», «⬆️», «⬇️», «🗑» в одном ряду на пункт.
    """
    builder = InlineKeyboardBuilder()

    for offset, item in enumerate(items):
        item_id = _positive(item.get("id"))
        number = start_index + offset
        text = item.get("text")
        if edit_mode:
            builder.row(
                _button(
                    f"✏️ {number}. {_short(text, EDIT_LABEL_LIMIT)}",
                    action="edititem",
                    note_id=note_id,
                    item_id=item_id,
                    page=page,
                ),
                _button("⬆️", action="up", note_id=note_id, item_id=item_id, page=page),
                _button("⬇️", action="down", note_id=note_id, item_id=item_id, page=page),
                _button(
                    "🗑", action="delitem", note_id=note_id, item_id=item_id, page=page
                ),
            )
            continue
        mark = "☑️" if item.get("is_done") else "⬜"
        builder.row(
            _button(
                f"{mark} {number}. {_short(text)}",
                action="toggle",
                note_id=note_id,
                item_id=item_id,
                page=page,
            )
        )

    pagination = _pagination_row(
        action="edit" if edit_mode else "open",
        note_id=note_id,
        page=page,
        total_pages=total_pages,
    )
    if pagination:
        builder.row(*pagination)

    if edit_mode:
        builder.row(
            _button("➕ Пункт", action="additem", note_id=note_id, page=page),
            _button("✅ Готово", action="open", note_id=note_id, page=page),
        )
    else:
        builder.row(
            _button("➕ Пункт", action="additem", note_id=note_id, page=page),
            _button("🛠 Правка", action="edit", note_id=note_id, page=page),
        )
    builder.row(
        _button("✏️ Название", action="rename", note_id=note_id, page=page),
        _button("🗑 Удалить", action="delete", note_id=note_id, page=page),
    )
    builder.row(_button("⬅️ К заметкам", action="list", page=1))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Рендер экранов
# ---------------------------------------------------------------------------


async def _render_notes(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает экран списка заметок."""
    notes = await notes_repo.list_notes(user_id)
    if not notes:
        return f"{NOTES_TITLE}\n\n{EMPTY_NOTES}", _notes_kb((), 1, 1)

    per_page = _per_page()
    items, total_pages = paginate(notes, page, per_page)
    current = _clamp_page(page, total_pages)

    lines = [f"{NOTES_TITLE} — {len(notes)} шт.", ""]
    start = page_offset(current, per_page) + 1
    for index, note in enumerate(items, start=start):
        done, total = _counters(note)
        title = escape(_short(note.get("title") or "Без названия", LINE_LIMIT))
        lines.append(f"{index}. 📋 <b>{title}</b> — {_progress_label(done, total)}")
    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")

    return _fit(lines), _notes_kb(items, current, total_pages)


async def _render_note(
    user_id: int, note_id: int, page: int, *, edit_mode: bool = False
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Собирает экран заметки; ``None`` — заметки нет."""
    note = await notes_repo.get_note(user_id, note_id)
    if note is None:
        return None

    all_items: list[dict[str, Any]] = list(note.get("items") or [])
    per_page = _per_page()
    items, total_pages = paginate(all_items, page, per_page)
    current = _clamp_page(page, total_pages)
    start = page_offset(current, per_page) + 1

    done, total = _counters(note)
    title = escape(_short(note.get("title") or "Без названия", LINE_LIMIT))
    lines = [f"📋 <b>{title}</b>"]
    if total:
        percent = int(round(100 * done / total))
        lines.append(f"{_progress_bar(done, total)} {done}/{total} · {percent}%")
    lines.append("")

    if not all_items:
        lines.append(EMPTY_ITEMS)
    else:
        for index, item in enumerate(items, start=start):
            text = escape(_short(item.get("text"), LINE_LIMIT))
            if item.get("is_done"):
                lines.append(f"{index}. ☑️ <s>{text}</s>")
            else:
                lines.append(f"{index}. ⬜ {text}")

    if total_pages > 1:
        lines.append("")
        lines.append(f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>")
    if edit_mode and all_items:
        lines.append("")
        lines.append(EDIT_HINT)

    markup = _note_kb(
        note_id, items, current, total_pages, edit_mode=edit_mode, start_index=start
    )
    return _fit(lines), markup


async def _show_note(
    callback: CallbackQuery,
    bot: Bot,
    user_id: int,
    note_id: int,
    page: int,
    *,
    edit_mode: bool = False,
    header: str | None = None,
) -> bool:
    """Перерисовывает заметку. ``False`` — заметка исчезла, показан список."""
    rendered = await _render_note(user_id, note_id, page, edit_mode=edit_mode)
    if rendered is None:
        text, markup = await _render_notes(user_id, 1)
        await _show(callback, bot, text, markup)
        return False
    body = rendered[0] if header is None else f"{header}\n\n{rendered[0]}"
    await _show(callback, bot, body, rendered[1])
    return True


async def _answer_note(
    message: Message,
    user_id: int,
    note_id: int,
    page: int,
    *,
    edit_mode: bool = False,
    header: str | None = None,
    footer: str | None = None,
) -> bool:
    """Отправляет экран заметки новым сообщением (ответ на ввод текста)."""
    rendered = await _render_note(user_id, note_id, page, edit_mode=edit_mode)
    if rendered is None:
        await message.answer(NOTE_LOST)
        return False
    parts = [part for part in (header, rendered[0], footer) if part]
    await message.answer("\n\n".join(parts), reply_markup=rendered[1])
    return True


# ---------------------------------------------------------------------------
# Команда /notes
# ---------------------------------------------------------------------------


@router.message(Command("notes"))
async def cmd_notes(message: Message, state: FSMContext) -> None:
    """Показывает список заметок пользователя."""
    if message.from_user is None:
        return
    await _reset_state(state)
    text, markup = await _render_notes(int(message.from_user.id), 1)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Колбэки: навигация
# ---------------------------------------------------------------------------


@router.callback_query(NoteCB.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопка-индикатор страницы: ничего не делает, но «часики» гасит."""
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action.in_({"list", "back"})))
async def cb_list(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Список заметок (в том числе возврат из карточки заметки)."""
    if callback.from_user is None:
        await ack(callback)
        return
    await _reset_state(state)
    text, markup = await _render_notes(
        int(callback.from_user.id), max(1, int(callback_data.page or 1))
    )
    await _show(callback, bot, text, markup)
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action.in_({"open", "page", "edit"})))
async def cb_open(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Открывает заметку: обычный вид или режим правки пунктов."""
    if callback.from_user is None:
        await ack(callback)
        return

    await _reset_state(state)
    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    page = max(1, int(callback_data.page or 1))

    if not note_id:
        # ``page`` без заметки — это пагинация списка заметок.
        text, markup = await _render_notes(user_id, page)
        await _show(callback, bot, text, markup)
        await ack(callback)
        return

    shown = await _show_note(
        callback, bot, user_id, note_id, page, edit_mode=callback_data.action == "edit"
    )
    if shown:
        await ack(callback)
    else:
        await ack(callback, NOTE_NOT_FOUND, alert=True)


# ---------------------------------------------------------------------------
# Колбэки: создание, переименование, удаление заметки
# ---------------------------------------------------------------------------


@router.callback_query(NoteCB.filter(F.action == "create"))
async def cb_create_note(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Спрашивает название новой заметки."""
    if callback.from_user is None:
        await ack(callback)
        return
    await state.set_state(NoteStates.waiting_title)
    await state.update_data(
        {MODE_KEY: MODE_CREATE, PAGE_KEY: max(1, int(callback_data.page or 1))}
    )
    await _show(callback, bot, f"{NOTE_TITLE_PROMPT}\n\n{CANCEL_HINT}")
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action == "rename"))
async def cb_rename_note(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Спрашивает новое название заметки."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    note = await notes_repo.get_note(user_id, note_id) if note_id else None
    if note is None:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return

    await state.set_state(NoteStates.waiting_title)
    await state.update_data(
        {
            MODE_KEY: MODE_RENAME,
            NOTE_KEY: note_id,
            PAGE_KEY: max(1, int(callback_data.page or 1)),
        }
    )
    await _show(
        callback,
        bot,
        f"{NOTE_RENAME_PROMPT}\n"
        f"Текущее название: «{escape(_short(note.get('title'), LINE_LIMIT))}».\n\n"
        f"{CANCEL_HINT}",
    )
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action == "delete"))
async def cb_delete_note(callback: CallbackQuery, callback_data: NoteCB, bot: Bot) -> None:
    """Удаление заметки: сначала вопрос, затем (``item_id == 1``) само удаление."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    page = max(1, int(callback_data.page or 1))
    note = await notes_repo.get_note(user_id, note_id) if note_id else None
    if note is None:
        text, markup = await _render_notes(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return

    title = _short(note.get("title") or "Без названия", LINE_LIMIT)
    done, total = _counters(note)

    if int(callback_data.item_id or 0) != CONFIRM_FLAG:
        markup = confirm_kb(
            NoteCB(
                action="delete", note_id=note_id, item_id=CONFIRM_FLAG, page=page
            ).pack(),
            NoteCB(action="open", note_id=note_id, item_id=NO_ID, page=page).pack(),
        )
        question = CONFIRM_DELETE_NOTE.format(title=escape(title))
        details = (
            "Пунктов в ней нет."
            if total <= 0
            else f"Пунктов: {total}, из них выполнено: {done}."
        )
        await _show(callback, bot, f"{question}\n{details}", markup)
        await ack(callback)
        return

    deleted = await notes_repo.delete_note(user_id, note_id)
    if not deleted:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    logger.info("Пользователь %s удалил заметку %s", user_id, note_id)
    text, markup = await _render_notes(user_id, 1)
    await _show(
        callback, bot, f"{NOTE_DELETED.format(title=escape(title))}\n\n{text}", markup
    )
    await ack(callback, NOTE_DELETED_SHORT)


# ---------------------------------------------------------------------------
# Колбэки: пункты заметки
# ---------------------------------------------------------------------------


@router.callback_query(NoteCB.filter(F.action == "additem"))
async def cb_add_item(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Спрашивает текст нового пункта (состояние держится до /cancel)."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    note = await notes_repo.get_note(user_id, note_id) if note_id else None
    if note is None:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return

    await state.set_state(NoteStates.waiting_item)
    await state.update_data(
        {
            NOTE_KEY: note_id,
            ITEM_KEY: NO_ID,
            PAGE_KEY: max(1, int(callback_data.page or 1)),
            EDIT_KEY: False,
        }
    )
    await _show(
        callback,
        bot,
        f"📋 <b>{escape(_short(note.get('title'), LINE_LIMIT))}</b>\n\n"
        f"{ITEM_TEXT_PROMPT}\n\n{CANCEL_HINT}",
    )
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action == "edititem"))
async def cb_edit_item(
    callback: CallbackQuery, callback_data: NoteCB, bot: Bot, state: FSMContext
) -> None:
    """Спрашивает новый текст существующего пункта."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    item_id = _positive(callback_data.item_id)
    page = max(1, int(callback_data.page or 1))

    note = await notes_repo.get_note(user_id, note_id) if note_id else None
    if note is None:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return

    current = next(
        (
            item
            for item in note.get("items") or []
            if _positive(item.get("id")) == item_id
        ),
        None,
    )
    if current is None:
        await _show_note(callback, bot, user_id, note_id, page, edit_mode=True)
        await ack(callback, ITEM_NOT_FOUND, alert=True)
        return

    await state.set_state(NoteStates.waiting_item)
    await state.update_data(
        {NOTE_KEY: note_id, ITEM_KEY: item_id, PAGE_KEY: page, EDIT_KEY: True}
    )
    await _show(
        callback,
        bot,
        f"{ITEM_EDIT_PROMPT}\n"
        f"Сейчас: «{escape(_short(current.get('text'), LINE_LIMIT))}».\n\n"
        f"{CANCEL_HINT}",
    )
    await ack(callback)


@router.callback_query(NoteCB.filter(F.action == "toggle"))
async def cb_toggle_item(callback: CallbackQuery, callback_data: NoteCB, bot: Bot) -> None:
    """Переключает отметку «выполнено» и перерисовывает заметку."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    item_id = _positive(callback_data.item_id)
    page = max(1, int(callback_data.page or 1))

    item = await notes_repo.toggle_item(user_id, note_id, item_id)
    shown = await _show_note(callback, bot, user_id, note_id, page)
    if not shown:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return
    if item is None:
        await ack(callback, ITEM_NOT_FOUND, alert=True)
        return
    await ack(callback, MARK_SET if item.get("is_done") else MARK_CLEARED)


@router.callback_query(NoteCB.filter(F.action == "delitem"))
async def cb_delete_item(callback: CallbackQuery, callback_data: NoteCB, bot: Bot) -> None:
    """Удаляет пункт заметки (нумерация оставшихся восстанавливается в репозитории)."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    item_id = _positive(callback_data.item_id)
    page = max(1, int(callback_data.page or 1))

    deleted = await notes_repo.delete_item(user_id, note_id, item_id)
    shown = await _show_note(callback, bot, user_id, note_id, page, edit_mode=True)
    if not shown:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return
    if not deleted:
        await ack(callback, ITEM_NOT_FOUND, alert=True)
        return

    logger.info("Пользователь %s удалил пункт %s заметки %s", user_id, item_id, note_id)
    await ack(callback, ITEM_DELETED_SHORT)


@router.callback_query(NoteCB.filter(F.action.in_({"up", "down"})))
async def cb_move_item(callback: CallbackQuery, callback_data: NoteCB, bot: Bot) -> None:
    """Перемещает пункт вверх или вниз через ``notes_repo.reorder_items``."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    note_id = _positive(callback_data.note_id)
    item_id = _positive(callback_data.item_id)
    page = max(1, int(callback_data.page or 1))

    note = await notes_repo.get_note(user_id, note_id) if note_id else None
    if note is None:
        text, markup = await _render_notes(user_id, 1)
        await _show(callback, bot, text, markup)
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return

    order = [_positive(item.get("id")) for item in note.get("items") or []]
    if item_id not in order:
        await _show_note(callback, bot, user_id, note_id, page, edit_mode=True)
        await ack(callback, ITEM_NOT_FOUND, alert=True)
        return

    index = order.index(item_id)
    target = index - 1 if callback_data.action == "up" else index + 1
    if target < 0:
        await ack(callback, ITEM_FIRST)
        return
    if target >= len(order):
        await ack(callback, ITEM_LAST)
        return

    order[index], order[target] = order[target], order[index]
    if not await notes_repo.reorder_items(user_id, note_id, order):
        logger.warning(
            "Пользователь %s: не удалось изменить порядок пунктов заметки %s",
            user_id,
            note_id,
        )
        await _show_note(callback, bot, user_id, note_id, page, edit_mode=True)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    # Пункт мог уехать на соседнюю страницу — показываем ту, где он теперь.
    shown = await _show_note(
        callback, bot, user_id, note_id, _item_page(target + 1), edit_mode=True
    )
    if not shown:
        await ack(callback, NOTE_NOT_FOUND, alert=True)
        return
    await ack(callback, ORDER_UPDATED_SHORT)


# ---------------------------------------------------------------------------
# FSM: ввод названия заметки и текста пунктов
# ---------------------------------------------------------------------------


@router.message(
    StateFilter(NoteStates.waiting_title, NoteStates.waiting_item), Command("cancel")
)
async def on_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает любой диалог заметок."""
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(StateFilter(NoteStates.waiting_title), F.text)
async def on_note_title(message: Message, state: FSMContext) -> None:
    """Создаёт заметку или переименовывает существующую."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if _is_command(text):
        await state.clear()
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return
    if not text:
        await message.answer(TITLE_EMPTY)
        return

    user_id = int(message.from_user.id)
    data = await state.get_data()
    mode = str(data.get(MODE_KEY) or MODE_CREATE)
    note_id = _positive(data.get(NOTE_KEY))
    page = max(1, int(data.get(PAGE_KEY) or 1))

    if mode == MODE_RENAME and not note_id:
        await state.clear()
        await message.answer(NOTE_LOST)
        return

    try:
        if mode == MODE_RENAME:
            note = await notes_repo.rename_note(user_id, note_id, text)
        else:
            note = await notes_repo.create_note(user_id, text)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось сохранить название заметки: %s", user_id, error
        )
        await state.clear()
        await message.answer(f"❌ {escape(str(error))}")
        return

    await state.clear()
    if note is None:
        await message.answer(NOTE_LOST)
        return

    title = _short(note.get("title") or "Без названия", LINE_LIMIT)
    if mode == MODE_RENAME:
        logger.info(
            "Пользователь %s переименовал заметку %s в «%s»", user_id, note_id, title
        )
        header = NOTE_RENAMED.format(title=escape(title))
    else:
        note_id = _positive(note.get("id"))
        page = 1
        logger.info(
            "Пользователь %s создал заметку «%s» (id=%s)", user_id, title, note_id
        )
        header = NOTE_CREATED.format(title=escape(title))

    await _answer_note(message, user_id, note_id, page, header=header)


@router.message(StateFilter(NoteStates.waiting_item), F.text)
async def on_item_text(message: Message, state: FSMContext) -> None:
    """Добавляет новый пункт или меняет текст существующего."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if _is_command(text):
        await state.clear()
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return
    if not text:
        await message.answer(ITEM_EMPTY)
        return

    user_id = int(message.from_user.id)
    data = await state.get_data()
    note_id = _positive(data.get(NOTE_KEY))
    item_id = _positive(data.get(ITEM_KEY))
    page = max(1, int(data.get(PAGE_KEY) or 1))
    edit_mode = bool(data.get(EDIT_KEY))

    if not note_id:
        await state.clear()
        await message.answer(NOTE_LOST)
        return

    try:
        if item_id:
            item = await notes_repo.update_item(user_id, note_id, item_id, text)
        else:
            item = await notes_repo.add_item(user_id, note_id, text)
    except NotFoundError:
        await state.clear()
        await message.answer(NOTE_LOST)
        return
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nПришлите другой текст.")
        return
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось сохранить пункт заметки %s: %s",
            user_id,
            note_id,
            error,
        )
        await state.clear()
        await message.answer(f"❌ {escape(str(error))}")
        return

    if item is None:
        await state.clear()
        await message.answer(ITEM_NOT_FOUND)
        return

    target_page = _item_page(int(item.get("position") or 1))

    if item_id:
        await state.clear()
        logger.info(
            "Пользователь %s изменил пункт %s заметки %s", user_id, item_id, note_id
        )
        await _answer_note(
            message,
            user_id,
            note_id,
            target_page,
            edit_mode=True,
            header=f"✅ {ITEM_UPDATED_SHORT}.",
        )
        return

    # Новый пункт: остаёмся в состоянии, чтобы принять следующий.
    await state.update_data({PAGE_KEY: target_page})
    logger.info(
        "Пользователь %s добавил пункт %s в заметку %s", user_id, item.get("id"), note_id
    )
    added = await _answer_note(
        message,
        user_id,
        note_id,
        target_page,
        edit_mode=edit_mode,
        header=ITEM_ADDED,
        footer=f"<i>{NEXT_ITEM_HINT}</i>",
    )
    if not added:
        await state.clear()


__all__ = ["CONFIRM_FLAG", "router"]
