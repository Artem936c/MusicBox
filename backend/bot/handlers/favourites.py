"""Хендлеры избранного: команда ``/favourites``, поиск по нему и звёздочка.

Здесь находится ЕДИНСТВЕННЫЙ в проекте обработчик ``TrackCB`` с ``action="fav"``:
он переключает избранное, отвечает всплывающим сообщением и обновляет клавиатуру
исходного сообщения (звёздочка ⭐/☆ меняется на месте).

Пагинация списка избранного приходит как ``TrackCB(action="page", ctx="fav")`` —
именно такие данные кладёт в кнопки ``keyboards.page_callback`` для контекста «fav».

V2 (ТЗ п. 12) добавляет команду ``/favourites_search`` — нечёткий поиск внутри
избранного через ``tracks_repo.search_in(..., scope="favourites")``. У результатов
свой контекст ``favsearch``, чтобы пагинация выдачи не путалась с пагинацией
полного списка избранного.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.bot import keyboards, texts, utils
from backend.bot.callbacks import TrackCB
from backend.config import settings
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError

logger = logging.getLogger(__name__)

router = Router(name="favourites")

#: Контекст списка избранного в callback-данных (TrackCB.ctx).
CTX = "fav"
#: Контекст выдачи поиска по избранному (собственная пагинация).
SEARCH_CTX = "favsearch"
#: Все написания ключа, которые считаем «избранным».
FAV_KEYS = frozenset({"fav", "favs", "favourites", "favorites"})
#: Сколько треков забираем из репозитория за один поиск.
SEARCH_LIMIT = 50
#: Ключ в данных FSM с последним запросом по избранному (нужен для пагинации).
SEARCH_QUERY_KEY = "fav_search_query"
#: Ограничение Telegram на длину текста сообщения.
MESSAGE_LIMIT = 4096

# --- Пользовательские тексты (RU) ----------------------------------------------------
# Общие формулировки берём из backend.bot.texts.

FAVOURITES_TITLE = "⭐ <b>Избранное</b>"
SEARCH_PROMPT = (
    "⭐ Что ищем в избранном? Напишите название трека или исполнителя — "
    "я прощаю опечатки и неправильную раскладку.\n\n"
    "Чтобы отменить, отправьте /cancel."
)
SEARCH_HEADER = "⭐ <b>Избранное по запросу «{query}»</b>"
SEARCH_EMPTY = (
    "🔍 В избранном ничего не нашлось.\n"
    "Попробуйте другой запрос или посмотрите весь список: /favourites."
)
SEARCH_QUERY_LOST = "Повторите поиск: /favourites_search и запрос."


# --- Состояния FSM (объявлены локально — см. правила V2) -----------------------------


class FavouritesSearchStates(StatesGroup):
    """Диалог поиска по избранному."""

    #: Ожидание запроса для `/favourites_search`.
    waiting_query = State()


# --- Вспомогательные функции ---------------------------------------------------------


def _per_page() -> int:
    """Размер страницы списка (из настроек, с разумными границами)."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, 50))


def _callback_message(callback: CallbackQuery) -> Message | None:
    """Возвращает сообщение колбэка, если оно доступно для редактирования."""
    message = callback.message
    return message if isinstance(message, Message) else None


def _total_pages(total: int, per_page: int) -> int:
    """Число страниц (не меньше одной)."""
    if total <= 0 or per_page <= 0:
        return 1
    return max(1, (total + per_page - 1) // per_page)


def _trim(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Подрезает сообщение под лимит Telegram, не разрывая HTML-тег."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    opened = cut.rfind("<")
    if opened > cut.rfind(">"):
        cut = cut[:opened]
    logger.debug("Сообщение избранного подрезано до %s символов", limit)
    return cut.rstrip() + "…"


async def _favourites_view(user_id: int, page: int) -> tuple[str, Any]:
    """Готовит текст и клавиатуру страницы избранного."""
    per_page = _per_page()
    total = await favourites_repo.count(user_id)
    total_pages = _total_pages(total, per_page)
    safe_page = min(max(int(page or 1), 1), total_pages)

    tracks = await favourites_repo.list_favourites(
        user_id, limit=per_page, offset=(safe_page - 1) * per_page
    )
    if not tracks and total == 0:
        return texts.EMPTY_FAVOURITES, keyboards.tracks_page_kb(
            [], ctx=CTX, page=1, total_pages=1
        )

    header = (
        f"{FAVOURITES_TITLE}\n"
        f"Всего: {utils.tracks_count_label(total)}"
    )
    text = utils.render_track_list(
        header, tracks, safe_page, total_pages, texts.EMPTY_FAVOURITES
    )
    markup = keyboards.tracks_page_kb(
        tracks, ctx=CTX, page=safe_page, total_pages=total_pages
    )
    return _trim(text), markup


async def _search_favourites(user_id: int, query: str) -> list[dict]:
    """Нечёткий поиск по избранному (пустой список — ничего не нашлось)."""
    clean = " ".join(str(query or "").split())
    if not clean:
        return []
    try:
        return await tracks_repo.search_in(
            user_id, clean, scope="favourites", limit=SEARCH_LIMIT
        )
    except MusicBoxError as exc:
        logger.warning("Поиск по избранному «%s» отклонён: %s", clean, exc)
        return []
    except Exception:
        logger.exception("Ошибка поиска по избранному «%s» пользователя %s", clean, user_id)
        return []


def _search_view(query: str, tracks: Sequence[dict], page: int) -> tuple[str, Any]:
    """Текст и клавиатура страницы результатов поиска по избранному."""
    per_page = _per_page()
    items, total_pages = utils.paginate(list(tracks), page, per_page)
    safe_page = min(max(int(page or 1), 1), total_pages)

    header = (
        f"{SEARCH_HEADER.format(query=utils.escape(query))}\n"
        f"Найдено: {utils.tracks_count_label(len(tracks))}"
    )
    text = utils.render_track_list(header, items, safe_page, total_pages, SEARCH_EMPTY)
    markup = keyboards.tracks_page_kb(
        items, ctx=SEARCH_CTX, page=safe_page, total_pages=total_pages
    )
    return _trim(text), markup


async def _run_favourites_search(message: Message, user_id: int, query: str,
                                 state: FSMContext) -> None:
    """Выполняет поиск по избранному и показывает результаты."""
    clean = " ".join(str(query or "").split())
    if not clean:
        await message.answer(texts.EMPTY_QUERY)
        return

    tracks = await _search_favourites(user_id, clean)
    await state.update_data(**{SEARCH_QUERY_KEY: clean})

    if not tracks:
        header = SEARCH_HEADER.format(query=utils.escape(clean))
        await message.answer(f"{header}\n\n{SEARCH_EMPTY}")
        logger.info("Поиск по избранному «%s» пользователя %s: пусто", clean, user_id)
        return

    text, markup = _search_view(clean, tracks, 1)
    await message.answer(text, reply_markup=markup)
    logger.info(
        "Поиск по избранному «%s» пользователя %s: найдено %s", clean, user_id, len(tracks)
    )


def _fav_label(text: str, is_favourite: bool) -> str:
    """Меняет звёздочку в подписи кнопки, сохраняя остальной текст."""
    base = (text or "").replace("⭐", "").replace("★", "").replace("☆", "").strip()
    icon = "⭐" if is_favourite else "☆"
    return f"{icon} {base}".strip() if base else icon


def _parse_track_cb(data: str | None) -> TrackCB | None:
    """Безопасно разбирает callback_data кнопки в TrackCB."""
    if not data:
        return None
    try:
        return TrackCB.unpack(data)
    except (ValueError, TypeError):
        return None


def _update_markup(
    markup: InlineKeyboardMarkup | None, track_id: int, is_favourite: bool
) -> InlineKeyboardMarkup | None:
    """Перерисовывает кнопку «избранное» нужного трека. None — менять нечего."""
    rows: Sequence[Sequence[InlineKeyboardButton]] = getattr(
        markup, "inline_keyboard", ()
    ) or ()
    if not rows:
        return None

    changed = False
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in rows:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            parsed = _parse_track_cb(getattr(button, "callback_data", None))
            if (
                parsed is not None
                and parsed.action == "fav"
                and int(parsed.track_id) == int(track_id)
            ):
                new_text = _fav_label(button.text, is_favourite)
                if new_text != button.text:
                    button = button.model_copy(update={"text": new_text})
                    changed = True
            new_row.append(button)
        new_rows.append(new_row)

    if not changed:
        return None
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


async def _refresh_markup(
    message: Message, track_id: int, is_favourite: bool
) -> None:
    """Обновляет клавиатуру сообщения после переключения избранного."""
    markup = _update_markup(message.reply_markup, track_id, is_favourite)
    if markup is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Не удалось обновить клавиатуру избранного: %s", exc)
    except TelegramAPIError:
        logger.warning("Ошибка Telegram при обновлении клавиатуры избранного", exc_info=True)


# --- Команда /favourites -------------------------------------------------------------


@router.message(Command("favourites"))
async def cmd_favourites(message: Message, state: FSMContext) -> None:
    """Показывает список избранных треков."""
    if message.from_user is None:
        return
    await state.set_state(None)
    text, markup = await _favourites_view(message.from_user.id, 1)
    await message.answer(text, reply_markup=markup)


# --- Команда /favourites_search (ТЗ п. 12) -------------------------------------------


@router.message(Command("favourites_search"))
async def cmd_favourites_search(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """`/favourites_search` — нечёткий поиск внутри избранного."""
    if message.from_user is None:
        return
    query = (command.args or "").strip()
    if not query:
        await state.set_state(FavouritesSearchStates.waiting_query)
        await message.answer(SEARCH_PROMPT)
        return
    await state.set_state(None)
    await _run_favourites_search(message, int(message.from_user.id), query, state)


@router.message(StateFilter(FavouritesSearchStates.waiting_query), Command("cancel"))
async def cancel_favourites_search(message: Message, state: FSMContext) -> None:
    """Отменяет ожидание запроса поиска по избранному."""
    await state.set_state(None)
    await message.answer(texts.SEARCH_CANCELLED)


@router.message(StateFilter(FavouritesSearchStates.waiting_query), F.text)
async def favourites_search_entered(message: Message, state: FSMContext) -> None:
    """Принимает запрос поиска по избранному из FSM."""
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
    await _run_favourites_search(message, int(message.from_user.id), text, state)


# --- Переключение избранного (единственный обработчик TrackCB action="fav") -----------


@router.callback_query(TrackCB.filter(F.action == "fav"))
async def cb_toggle_favourite(callback: CallbackQuery, callback_data: TrackCB,
                              state: FSMContext) -> None:
    """Добавляет трек в избранное или убирает из него."""
    user_id = callback.from_user.id
    track_id = int(callback_data.track_id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await callback.answer(texts.TRACK_NOT_FOUND, show_alert=True)
        return

    is_favourite = await favourites_repo.toggle(user_id, track_id)
    await callback.answer(
        texts.FAVOURITE_ADDED if is_favourite else texts.FAVOURITE_REMOVED
    )
    logger.info(
        "Пользователь %s %s трек %s",
        user_id,
        "добавил в избранное" if is_favourite else "убрал из избранного",
        track_id,
    )

    message = _callback_message(callback)
    if message is None:
        return

    ctx = (callback_data.ctx or "").strip().casefold()
    page = max(callback_data.page, 1)

    # В списке избранного убранный трек должен исчезнуть — перерисовываем страницу.
    if ctx in FAV_KEYS and not is_favourite:
        text, markup = await _favourites_view(user_id, page)
        await utils.safe_edit(message, text, markup)
        return

    # То же самое в выдаче поиска по избранному: трек больше ей не принадлежит.
    if ctx == SEARCH_CTX and not is_favourite:
        data = await state.get_data()
        query = str(data.get(SEARCH_QUERY_KEY) or "").strip()
        if query:
            tracks = await _search_favourites(user_id, query)
            if tracks:
                text, markup = _search_view(query, tracks, page)
            else:
                text, markup = (
                    f"{SEARCH_HEADER.format(query=utils.escape(query))}\n\n{SEARCH_EMPTY}",
                    None,
                )
            await utils.safe_edit(message, text, markup)
            return

    await _refresh_markup(message, track_id, is_favourite)


# --- Навигация по списку избранного --------------------------------------------------


@router.callback_query(
    TrackCB.filter((F.action.in_({"page", "back"})) & (F.ctx.in_(FAV_KEYS)))
)
async def cb_favourites_page(callback: CallbackQuery, callback_data: TrackCB) -> None:
    """Пагинация списка избранного (кнопки «◀️ N/M ▶️») и возврат к нему."""
    message = _callback_message(callback)
    text, markup = await _favourites_view(callback.from_user.id, max(callback_data.page, 1))
    if message is not None:
        await utils.safe_edit(message, text, markup)
    await callback.answer()


@router.callback_query(
    TrackCB.filter((F.action.in_({"page", "back"})) & (F.ctx == SEARCH_CTX))
)
async def cb_favourites_search_page(
    callback: CallbackQuery, callback_data: TrackCB, state: FSMContext
) -> None:
    """Пагинация выдачи поиска по избранному (запрос берётся из данных FSM)."""
    data = await state.get_data()
    query = str(data.get(SEARCH_QUERY_KEY) or "").strip()
    if not query:
        await callback.answer(SEARCH_QUERY_LOST, show_alert=True)
        return

    tracks = await _search_favourites(int(callback.from_user.id), query)
    if not tracks:
        await callback.answer(SEARCH_EMPTY, show_alert=True)
        return

    text, markup = _search_view(query, tracks, max(callback_data.page, 1))
    message = _callback_message(callback)
    if message is not None:
        await utils.safe_edit(message, text, markup)
    await callback.answer()


__all__ = [
    "CTX",
    "SEARCH_CTX",
    "FavouritesSearchStates",
    "router",
]
