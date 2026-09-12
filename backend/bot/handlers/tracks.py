"""Раздел «Треки» бота MusicBox (контракт V2, п. 11, 12, 16).

Здесь живут три команды:

* ``/tracks`` — список ТОЛЬКО аудио (``tracks.file_type = 'audio'``) по дате
  добавления, страницами по ``settings.page_size``, с кнопками «прослушать»,
  «в плейлист», «в избранное»;
* ``/tracks_search`` — нечёткий поиск по трекам
  (``tracks_repo.search_in(scope="tracks")``); без аргумента спрашивает запрос
  через FSM;
* ``/play_all`` — отправка всех аудиофайлов пачками не более
  :data:`PLAY_BATCH` штук с паузой :data:`PLAY_DELAY` между отправками; на
  КАЖДЫЙ отправленный трек вызывается
  ``tracks_repo.register_play(source="bot_play_all")``, а под пачкой появляется
  кнопка «▶️ Ещё 10».

Папки в этом разделе НЕ показываются — это плоский список аудио библиотеки.
Файлы уходят пользователю в исходном формате через
``backend.services.media.send_media_to_user`` (никакой конвертации, исходный
``mime_type`` и ``file_id`` из канала-хранилища).

Разделение ответственности с V1 (роутеры подключаются в порядке
``… stats, tracks, …``): построчные кнопки списка собирает
``keyboards.tracks_page_kb``, поэтому «▶️» обслуживает ``handlers/stats.py``,
«⭐/☆» — ``handlers/favourites.py``, а «➕» — снова ``handlers/stats.py``.
Этот модуль намеренно НЕ дублирует их обработчики и отвечает только за то,
чего в V1 не было: сами экраны раздела, их пагинацию и пакетную отправку.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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

from backend.bot import keyboards
from backend.bot.callbacks import TrackCB
from backend.bot.utils import (
    ack,
    answer_or_edit,
    escape,
    page_offset,
    paginate,
    render_track_list,
    tracks_count_label,
)
from backend.config import settings
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, StorageError
from backend.services import media
from backend.services.metadata import format_duration

logger = logging.getLogger(__name__)

router = Router(name="tracks")

# --------------------------------------------------------------------------- #
# Константы раздела
# --------------------------------------------------------------------------- #

#: Контекст списка раздела «Треки» в callback-данных (``TrackCB.ctx``).
#: ``keyboards.page_callback`` для неизвестного ему контекста кладёт в кнопки
#: пагинации ``TrackCB(action="page", ctx=CTX)`` — их и слушает этот модуль.
CTX = "tracks"

#: Контекст списка результатов ``/tracks_search``.
CTX_SEARCH = "tsearch"

#: Действие ``TrackCB`` для пакетной отправки (кнопка «▶️ Ещё 10»).
#: Номер пачки (1-based) едет в поле ``page``.
PLAY_ALL_ACTION = "playall"

#: Тип файлов раздела: только аудио (документы, видео и голосовые — в «Другое»).
AUDIO = media.AUDIO_FILE_TYPE

#: Порядок списка — по дате добавления, новые сверху (ТЗ п. 11).
ORDER = "created_at_desc"

#: Сколько треков отправляем за одну пачку в ``/play_all`` (ТЗ п. 16).
PLAY_BATCH = 10

#: Пауза между отправками, чтобы не упереться во флуд-контроль Telegram.
PLAY_DELAY = 0.3

#: Источник прослушивания для пакетной отправки.
PLAY_SOURCE = "bot_play_all"

#: Сколько треков запрашиваем у поиска (дальше листаем срезом в памяти).
SEARCH_LIMIT = 60

#: Минимальная длина поискового запроса.
MIN_QUERY_LEN = 2

#: Ключ данных FSM с последним запросом ``/tracks_search`` (нужен пагинации).
QUERY_KEY = "tracks_search_query"

#: Размер страницы по умолчанию, если в настройках лежит мусор.
DEFAULT_PAGE_SIZE = 10

#: Лимит длины сообщения Telegram.
MESSAGE_LIMIT = 4096

#: Пометка, которой заканчивается сокращённое сообщение.
CLIPPED_MARK = "<i>…список сокращён, откройте следующую страницу</i>"


# --------------------------------------------------------------------------- #
# Тексты раздела (RU) — объявлены локально, texts.py правит другой модуль
# --------------------------------------------------------------------------- #

TRACKS_TITLE = "🎵 Треки"
EMPTY_TRACKS = (
    "🎧 В разделе «Треки» пока пусто.\n"
    "Пришлите боту аудиофайл — он сохранится в библиотеке и появится здесь."
)
SEARCH_TITLE = "🔎 Треки по запросу «{query}»"
SEARCH_PROMPT = (
    "🔎 Что ищем среди треков? Напишите название или исполнителя.\n"
    "Поиск прощает опечатки.\n\n"
    "Чтобы отменить, отправьте /cancel."
)
SEARCH_CANCELLED = "Хорошо, поиск по трекам отменён."
SEARCH_EMPTY_QUERY = "✍️ Пустой запрос. Напишите, что искать среди треков."
SEARCH_TOO_SHORT = f"✍️ Слишком короткий запрос — хотя бы {MIN_QUERY_LEN} символа."
SEARCH_NOTHING = (
    "🤷 Среди треков ничего не нашлось.\n"
    "Попробуйте другое написание — поиск учитывает опечатки и раскладку."
)
SEARCH_QUERY_LOST = "Запрос потерялся. Повторите: /tracks_search и текст запроса."
FOUND_LABEL = "Найдено: {count}"
TOTAL_LABEL = "Всего: {count}"

PLAY_ALL_BUTTON = "▶️ Воспроизвести всё"
PLAY_MORE_BUTTON = "▶️ Ещё {count}"
BACK_TO_TRACKS_BUTTON = "🎵 К списку треков"
SENDING_BATCH = "Отправляю треки…"
NOTHING_TO_PLAY = "🔇 Нечего проигрывать: аудиофайлов в библиотеке нет."
PLAY_ALL_DONE = "✅ Все треки отправлены."
PLAY_ALL_SUMMARY = "▶️ Отправил {sent} ({first}–{last} из {total})."
PLAY_ALL_FAILED = "⚠️ Не удалось отправить: {count}."
PLAY_ALL_REST = "Осталось ещё {count} — нажмите кнопку ниже."
TRACK_NOT_FOUND = "🤷 Трек не найден — возможно, он уже удалён из библиотеки."
ERROR_TRY_AGAIN = "😔 Не получилось. Попробуйте ещё раз чуть позже."


# --------------------------------------------------------------------------- #
# FSM
# --------------------------------------------------------------------------- #


class TracksSearchStates(StatesGroup):
    """Диалог поиска по трекам (`/tracks_search` без аргумента)."""

    #: Ожидание поискового запроса.
    waiting_query = State()


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _per_page() -> int:
    """Размер страницы из настроек с защитой от некорректных значений."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        logger.warning("Некорректный page_size в настройках, использую %s", DEFAULT_PAGE_SIZE)
        return DEFAULT_PAGE_SIZE
    return value if value > 0 else DEFAULT_PAGE_SIZE


def _int_or_zero(value: Any) -> int:
    """Мягкое приведение к int (значения приходят из БД и callback-данных)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_page(value: Any) -> int:
    """Номер страницы (1-based) из callback-данных."""
    page = _int_or_zero(value)
    return page if page > 0 else 1


def _user_id(event: Message | CallbackQuery) -> int | None:
    """Идентификатор пользователя из события."""
    user = event.from_user
    return user.id if user is not None else None


def _chat_id(event: Message | CallbackQuery, user_id: int) -> int:
    """Чат, куда отправлять файлы (в личке совпадает с пользователем)."""
    message = event if isinstance(event, Message) else event.message
    if isinstance(message, Message):
        return message.chat.id
    return user_id


def _page_bounds(total: Any, page: Any) -> tuple[int, int, int, int]:
    """Параметры страницы по общему числу элементов.

    Возвращает ``(страница, всего страниц, limit, offset)`` — список листается
    запросом к БД, а не срезом в памяти.
    """
    per_page = _per_page()
    count = max(_int_or_zero(total), 0)
    total_pages = max(1, (count + per_page - 1) // per_page)
    current = min(_safe_page(page), total_pages)
    return current, total_pages, per_page, page_offset(current, per_page)


def _clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Укорачивает сообщение до лимита Telegram по границам строк.

    Резать только по «\\n» безопасно: и заголовок, и каждая строка трека —
    самостоятельные фрагменты HTML с закрытыми тегами, поэтому разметка
    остаётся корректной.
    """
    if len(text) <= limit:
        return text

    budget = limit - len(CLIPPED_MARK) - 1
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        extra = len(line) + (1 if kept else 0)
        if used + extra > budget:
            break
        kept.append(line)
        used += extra

    logger.warning("Сообщение раздела «Треки» обрезано до лимита Telegram")
    kept.append(CLIPPED_MARK)
    return "\n".join(kept)


def _track_caption(track: dict) -> str:
    """Подпись к отправляемому файлу (HTML)."""
    caption = f"🎧 <b>{escape(track.get('title') or 'Без названия')}</b>"
    artist = (track.get("artist") or "").strip()
    if artist:
        caption += f" — {escape(artist)}"
    return (
        f"{caption}\n"
        f"⏱ {format_duration(track.get('duration'))} · "
        f"▶️ {_int_or_zero(track.get('play_count'))}"
    )


def _clean_query(value: Any) -> str:
    """Нормализует поисковый запрос (схлопывает пробелы)."""
    return " ".join(str(value or "").split())


async def _notify(event: Message | CallbackQuery, text: str) -> None:
    """Отдельное сообщение — список, из которого пришёл callback, не трогаем."""
    if isinstance(event, Message):
        await event.answer(text)
        return

    message = event.message
    if isinstance(message, Message):
        await message.answer(text)
        return

    bot = event.bot
    if bot is not None and event.from_user is not None:
        await bot.send_message(event.from_user.id, text)
        return
    logger.warning("Не удалось отправить уведомление по callback %s", event.id)


async def _drop_markup(callback: CallbackQuery) -> None:
    """Снимает клавиатуру с сообщения — чтобы «Ещё 10» не нажали дважды."""
    message = callback.message
    if not isinstance(message, Message) or message.reply_markup is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:
        logger.debug("Не удалось снять клавиатуру пачки: %s", exc)
    except TelegramAPIError:
        logger.warning("Ошибка Telegram при снятии клавиатуры пачки", exc_info=True)


# --------------------------------------------------------------------------- #
# Клавиатуры раздела (локальные — keyboards.py правит другой модуль)
# --------------------------------------------------------------------------- #


def _play_all_row() -> list[InlineKeyboardButton]:
    """Строка «▶️ Воспроизвести всё» под списком треков."""
    return [
        InlineKeyboardButton(
            text=PLAY_ALL_BUTTON,
            callback_data=TrackCB(
                action=PLAY_ALL_ACTION, track_id=0, page=1, ctx=CTX
            ).pack(),
        )
    ]


def _play_more_kb(next_batch: int, remaining: int) -> InlineKeyboardMarkup:
    """Кнопки под отправленной пачкой: «Ещё N» и возврат к списку."""
    builder = InlineKeyboardBuilder()
    if remaining > 0:
        builder.row(
            InlineKeyboardButton(
                text=PLAY_MORE_BUTTON.format(count=min(remaining, PLAY_BATCH)),
                callback_data=TrackCB(
                    action=PLAY_ALL_ACTION, track_id=0, page=next_batch, ctx=CTX
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=BACK_TO_TRACKS_BUTTON,
            callback_data=TrackCB(action="page", track_id=0, page=1, ctx=CTX).pack(),
        )
    )
    return builder.as_markup()


# --------------------------------------------------------------------------- #
# Экран «Треки»
# --------------------------------------------------------------------------- #


async def _tracks_view(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и клавиатура страницы раздела «Треки»."""
    total = await tracks_repo.count_tracks(user_id, file_type=AUDIO)
    current, total_pages, limit, offset = _page_bounds(total, page)
    items = await tracks_repo.list_tracks(
        user_id, order=ORDER, limit=limit, offset=offset, file_type=AUDIO
    )

    header = TRACKS_TITLE
    if total:
        header = f"{TRACKS_TITLE} · {TOTAL_LABEL.format(count=tracks_count_label(total))}"

    text = _clip(render_track_list(header, items, current, total_pages, EMPTY_TRACKS))
    if not items:
        return text, None

    markup = keyboards.tracks_page_kb(
        items,
        ctx=CTX,
        page=current,
        total_pages=total_pages,
        extra_rows=[_play_all_row()],
    )
    return text, markup


async def render_tracks(event: Message | CallbackQuery, page: int = 1) -> None:
    """Показывает страницу раздела «Треки».

    Публичная функция: ею же можно открыть раздел из главного меню.
    """
    user_id = _user_id(event)
    if user_id is None:
        logger.warning("Событие без пользователя — раздел «Треки» не показан")
        return

    try:
        text, markup = await _tracks_view(user_id, page)
    except MusicBoxError:
        logger.exception("Не удалось собрать раздел «Треки» пользователя %s", user_id)
        await _notify(event, ERROR_TRY_AGAIN)
        if isinstance(event, CallbackQuery):
            await ack(event)
        return

    await answer_or_edit(event, text, markup)


@router.message(Command("tracks"))
async def cmd_tracks(message: Message, state: FSMContext) -> None:
    """``/tracks`` — первая страница раздела «Треки» (только аудио)."""
    if message.from_user is None:
        return
    await state.set_state(None)
    await render_tracks(message, 1)


@router.callback_query(TrackCB.filter((F.action.in_({"page", "back"})) & (F.ctx == CTX)))
async def cb_tracks_page(callback: CallbackQuery, callback_data: TrackCB) -> None:
    """Пагинация раздела «Треки» и возврат к списку из карточки трека."""
    await render_tracks(callback, _safe_page(callback_data.page))


# --------------------------------------------------------------------------- #
# Поиск по трекам
# --------------------------------------------------------------------------- #


async def _search_view(
    user_id: int, query: str, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и клавиатура страницы результатов поиска по трекам."""
    found = await tracks_repo.search_in(user_id, query, scope="tracks", limit=SEARCH_LIMIT)
    per_page = _per_page()
    total_pages = max(1, (len(found) + per_page - 1) // per_page)
    current = min(_safe_page(page), total_pages)
    items, total_pages = paginate(found, current, per_page)

    header = SEARCH_TITLE.format(query=escape(query))
    if found:
        header = f"{header}\n{FOUND_LABEL.format(count=tracks_count_label(len(found)))}"

    text = _clip(render_track_list(header, items, current, total_pages, SEARCH_NOTHING))
    if not items:
        return text, None

    markup = keyboards.tracks_page_kb(
        items, ctx=CTX_SEARCH, page=current, total_pages=total_pages
    )
    return text, markup


async def render_tracks_search(
    event: Message | CallbackQuery, query: str, page: int = 1
) -> None:
    """Показывает страницу результатов поиска по трекам."""
    user_id = _user_id(event)
    if user_id is None:
        return

    try:
        text, markup = await _search_view(user_id, query, page)
    except MusicBoxError:
        logger.exception("Поиск по трекам пользователя %s не выполнен", user_id)
        await _notify(event, ERROR_TRY_AGAIN)
        if isinstance(event, CallbackQuery):
            await ack(event)
        return

    await answer_or_edit(event, text, markup)


async def _run_search(event: Message | CallbackQuery, query: str, state: FSMContext) -> None:
    """Проверяет запрос, запоминает его в FSM и показывает результаты."""
    clean = _clean_query(query)
    if not clean:
        await _notify(event, SEARCH_EMPTY_QUERY)
        if isinstance(event, CallbackQuery):
            await ack(event)
        return
    if len(clean) < MIN_QUERY_LEN:
        await _notify(event, SEARCH_TOO_SHORT)
        if isinstance(event, CallbackQuery):
            await ack(event)
        return

    await state.update_data(**{QUERY_KEY: clean})
    await render_tracks_search(event, clean, 1)
    logger.info("Поиск по трекам «%s» пользователя %s", clean, _user_id(event))


@router.message(Command("tracks_search"))
async def cmd_tracks_search(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    """``/tracks_search`` — поиск по трекам; без аргумента спрашивает запрос."""
    if message.from_user is None:
        return

    query = _clean_query(command.args)
    if not query:
        await state.set_state(TracksSearchStates.waiting_query)
        await message.answer(SEARCH_PROMPT)
        return

    await state.set_state(None)
    await _run_search(message, query, state)


@router.message(StateFilter(TracksSearchStates.waiting_query), Command("cancel"))
async def cancel_tracks_search(message: Message, state: FSMContext) -> None:
    """Отменяет ожидание поискового запроса."""
    await state.set_state(None)
    await message.answer(SEARCH_CANCELLED)


@router.message(StateFilter(TracksSearchStates.waiting_query), F.text)
async def tracks_search_query_entered(message: Message, state: FSMContext) -> None:
    """Принимает поисковый запрос из FSM."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        # Пользователь передумал и позвал другую команду — выходим из диалога.
        await state.set_state(None)
        await message.answer(f"{SEARCH_CANCELLED} Повторите команду ещё раз.")
        return

    await state.set_state(None)
    await _run_search(message, text, state)


@router.callback_query(
    TrackCB.filter((F.action.in_({"page", "back"})) & (F.ctx == CTX_SEARCH))
)
async def cb_tracks_search_page(
    callback: CallbackQuery, callback_data: TrackCB, state: FSMContext
) -> None:
    """Пагинация результатов поиска (запрос берётся из данных FSM)."""
    data = await state.get_data()
    query = _clean_query(data.get(QUERY_KEY))
    if not query:
        await ack(callback, SEARCH_QUERY_LOST, alert=True)
        return
    await render_tracks_search(callback, query, _safe_page(callback_data.page))


# --------------------------------------------------------------------------- #
# /play_all — пакетная отправка всех треков
# --------------------------------------------------------------------------- #


async def _send_one(bot: Bot, chat_id: int, track: dict) -> bool:
    """Отправляет один трек пользователю в исходном формате.

    Возвращает True, если файл ушёл (и прослушивание можно засчитать).
    """
    try:
        await media.send_media_to_user(bot, chat_id, track, caption=_track_caption(track))
    except StorageError as exc:
        logger.warning("Трек %s не отправлен: %s", track.get("id"), exc)
        return False
    except TelegramAPIError:
        logger.exception("Ошибка Telegram при отправке трека %s", track.get("id"))
        return False
    return True


async def _play_batch(
    event: Message | CallbackQuery, bot: Bot, batch: int
) -> None:
    """Отправляет очередную пачку треков и предлагает продолжить.

    ``batch`` — номер пачки, 1-based; смещение считается как
    ``(batch - 1) * PLAY_BATCH``, поэтому порядок отправки совпадает со
    списком раздела и не сбивается от растущих счётчиков прослушиваний.
    """
    user_id = _user_id(event)
    if user_id is None:
        return

    chat_id = _chat_id(event, user_id)
    number = max(_int_or_zero(batch), 1)
    offset = (number - 1) * PLAY_BATCH

    try:
        total = await tracks_repo.count_tracks(user_id, file_type=AUDIO)
        items = (
            await tracks_repo.list_tracks(
                user_id,
                order=ORDER,
                limit=PLAY_BATCH,
                offset=offset,
                file_type=AUDIO,
            )
            if offset < total
            else []
        )
    except MusicBoxError:
        logger.exception("Не удалось получить треки для /play_all (пользователь %s)", user_id)
        await _notify(event, ERROR_TRY_AGAIN)
        return

    if not total:
        await _notify(event, NOTHING_TO_PLAY)
        return
    if not items:
        await _notify(event, PLAY_ALL_DONE)
        return

    sent = 0
    failed = 0
    for track in items:
        if await _send_one(bot, chat_id, track):
            sent += 1
            try:
                await tracks_repo.register_play(
                    user_id, _int_or_zero(track.get("id")), source=PLAY_SOURCE
                )
            except MusicBoxError:
                # Файл пользователь уже получил — прослушивание просто не учли.
                logger.exception(
                    "Прослушивание трека %s не учтено (пользователь %s)",
                    track.get("id"),
                    user_id,
                )
        else:
            failed += 1
        await asyncio.sleep(PLAY_DELAY)

    delivered = offset + len(items)
    remaining = max(total - delivered, 0)

    lines = [
        PLAY_ALL_SUMMARY.format(
            sent=tracks_count_label(sent),
            first=offset + 1,
            last=delivered,
            total=total,
        )
    ]
    if failed:
        lines.append(PLAY_ALL_FAILED.format(count=failed))
    lines.append(
        PLAY_ALL_REST.format(count=tracks_count_label(remaining))
        if remaining
        else PLAY_ALL_DONE
    )

    try:
        await bot.send_message(
            chat_id, "\n".join(lines), reply_markup=_play_more_kb(number + 1, remaining)
        )
    except TelegramAPIError:
        logger.warning("Не удалось отправить итог пачки /play_all", exc_info=True)

    logger.info(
        "/play_all пользователя %s: пачка %s, отправлено %s, ошибок %s, осталось %s",
        user_id,
        number,
        sent,
        failed,
        remaining,
    )


@router.message(Command("play_all"))
async def cmd_play_all(message: Message, bot: Bot, state: FSMContext) -> None:
    """``/play_all`` — отправляет первые 10 треков и предлагает продолжить."""
    if message.from_user is None:
        return
    await state.set_state(None)
    await _play_batch(message, bot, 1)


@router.callback_query(TrackCB.filter(F.action == PLAY_ALL_ACTION))
async def cb_play_all(callback: CallbackQuery, callback_data: TrackCB, bot: Bot) -> None:
    """«▶️ Воспроизвести всё» и «▶️ Ещё 10» — следующая пачка треков."""
    await ack(callback, SENDING_BATCH)
    await _drop_markup(callback)
    await _play_batch(callback, bot, _safe_page(callback_data.page))


__all__ = [
    "CTX",
    "CTX_SEARCH",
    "PLAY_ALL_ACTION",
    "PLAY_BATCH",
    "TracksSearchStates",
    "render_tracks",
    "render_tracks_search",
    "router",
]
