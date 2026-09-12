"""Хендлеры разделов статистики бота MusicBox.

Здесь живут команды `/stats`, `/top`, `/recent`, `/unplayed`, `/frequent`, `/rare`,
навигация по разделам (`SectionCB`) и действия над треком (`TrackCB`):
прослушивание, карточка трека, добавление в плейлист, удаление и возврат к списку.

ВАЖНО: обработчик `TrackCB(action="fav")` здесь НЕ регистрируется —
переключение избранного живёт в `backend/bot/handlers/favourites.py`.
Оттуда же обслуживается возврат к списку избранного (`ctx="fav"`), а из
`search.py` — возврат к результатам поиска, поэтому эти контексты
намеренно пропускаются фильтром обработчика «⬅️ Назад».

Публичные рендереры (`render_section`, `render_favourites`, `show_stats_overview`)
переиспользуются в `start.py`, чтобы списки выглядели одинаково во всём боте.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from backend.bot import keyboards, texts
from backend.bot.callbacks import SectionCB, TrackCB
from backend.bot.utils import (
    CTX_FAVOURITES,
    ack,
    answer_or_edit,
    artist_context,
    escape,
    folder_context,
    page_offset,
    paginate,
    parse_context,
    render_track_list,
    section_context,
)
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import playlists as playlists_repo
from backend.db.repositories import stats as stats_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import MusicBoxError, StorageError, ValidationError
from backend.services import media, storage
from backend.services.metadata import format_duration

logger = logging.getLogger(__name__)

router = Router(name="stats")

#: Размер страницы по умолчанию, если в настройках лежит мусор.
DEFAULT_PAGE_SIZE = 10

#: Ключи, которые обслуживает `SectionCB` этого модуля: пять разделов + избранное
#: (пагинация списка избранного из `tracks_page_kb` приходит именно сюда).
SECTION_CB_KEYS = frozenset(stats_repo.SECTION_KEYS) | {CTX_FAVOURITES}

#: Виды контекстов, для которых этот модуль умеет возвращать список из карточки трека.
BACK_CONTEXT_KINDS = frozenset(stats_repo.SECTION_KEYS) | {"folder", "pl", "art"}

#: Признак подтверждённого удаления в `TrackCB(action="delete")`: номер страницы
#: приходит отрицательным. Поля фабрики менять нельзя (`track_id` занят треком,
#: `ctx` — списком, куда возвращаемся), поэтому «да/нет» кодируем знаком страницы.
CONFIRMED_PAGE_SIGN = -1

# --- Тексты, которых нет в texts.py (списки треков папки/плейлиста/исполнителя) -------

EMPTY_ARTIST_TRACKS = (
    "🎤 У этого исполнителя пока нет треков.\n"
    "Пришлите его аудио — они появятся здесь автоматически."
)
UNKNOWN_SECTION = "🤷 Такого раздела статистики нет. Выберите раздел ниже."
SENDING_TRACK = "Отправляю трек…"
PLAY_FAILED = "Не удалось учесть прослушивание. Попробуйте ещё раз."
NO_PLAYLISTS = (
    "Плейлистов пока нет. Создайте первый командой /playlists — "
    "и сможете добавлять в него треки."
)
CHOOSE_PLAYLIST = "➕ В какой плейлист добавить «<b>{title}</b>»?"
SECTIONS_PROMPT = "Выберите раздел 👇"
CONFIRM_DELETE = (
    "🗑 Удалить трек «<b>{title}</b>» из библиотеки?\n"
    "Файл пропадёт и из канала-хранилища — отменить это будет нельзя."
)
TRACK_DELETED = "🗑 Трек удалён из библиотеки."
DELETE_FAILED = "Не удалось удалить трек. Попробуйте ещё раз."


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


def _page_of(items: list[dict], page: Any) -> tuple[list[dict], int, int]:
    """Срез страницы, фактический номер страницы и общее число страниц."""
    chunk, total_pages = paginate(items, _safe_page(page), _per_page())
    current = min(_safe_page(page), total_pages)
    return chunk, current, total_pages


def _page_bounds(total: Any, page: Any) -> tuple[int, int, int, int]:
    """Параметры страницы по общему числу элементов.

    Возвращает `(страница, всего страниц, limit, offset)` — списки листаются
    запросом к БД, а не срезом в памяти, иначе раздел упирался бы в потолок
    выборки (репозиторий отдаёт максимум 200 строк за раз).
    """
    per_page = _per_page()
    count = max(_int_or_zero(total), 0)
    total_pages = max(1, (count + per_page - 1) // per_page)
    current = min(_safe_page(page), total_pages)
    return current, total_pages, per_page, page_offset(current, per_page)


def _section_title(key: str) -> str:
    """Заголовок раздела с иконкой (обычный текст — экранирует render_track_list)."""
    header = texts.SECTION_HEADERS.get(key)
    if header:
        return header
    return stats_repo.SECTION_TITLES.get(key, "Статистика")


def _empty_hint(key: str) -> str:
    """Дружелюбная подсказка для пустого раздела."""
    return texts.SECTION_EMPTY_HINTS.get(key, texts.EMPTY_LIBRARY)


def _format_dt(value: Any) -> str:
    """Дата из БД («YYYY-MM-DD HH:MM:SS», UTC) в вид «06.09.2026 10:00»."""
    if not value:
        return "—"
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue
    return escape(raw)


def _track_caption(track: dict) -> str:
    """Подпись к отправляемому аудиофайлу (HTML)."""
    caption = f"🎧 <b>{escape(track.get('title') or 'Без названия')}</b>"
    artist = track.get("artist")
    if artist:
        caption += f" — {escape(artist)}"
    return f"{caption}\n▶️ Прослушиваний: {_int_or_zero(track.get('play_count'))}"


def _track_card(track: dict) -> str:
    """Подробная карточка трека."""
    artist = track.get("artist")
    album = track.get("album")
    folder_name = track.get("folder_name")
    lines = [
        f"🎵 <b>{escape(track.get('title') or 'Без названия')}</b>",
        "",
        f"👤 Исполнитель: {escape(artist) if artist else '—'}",
        f"💿 Альбом: {escape(album) if album else '—'}",
        f"📁 Папка: {escape(folder_name) if folder_name else 'без папки'}",
        f"⏱ Длительность: {format_duration(track.get('duration'))}",
        f"▶️ Прослушиваний: {_int_or_zero(track.get('play_count'))}",
        f"📅 Добавлен: {_format_dt(track.get('created_at'))}",
        f"🕒 Последнее прослушивание: {_format_dt(track.get('last_played_at'))}",
    ]
    if track.get("is_favourite"):
        lines.append("⭐ В избранном")
    return "\n".join(lines)


def _counts_text(data: dict) -> str:
    """Сводка по библиотеке и всем пяти разделам статистики."""
    lines = [
        texts.STATS_HEADER,
        texts.STATS_SUMMARY.format(
            total=_int_or_zero(data.get("total")),
            plays=_int_or_zero(data.get("total_plays")),
            folders=_int_or_zero(data.get("folders")),
            artists=_int_or_zero(data.get("artists")),
            favourites=_int_or_zero(data.get("favourites")),
        ),
        "",
    ]
    for key in stats_repo.SECTION_KEYS:
        lines.append(f"{_section_title(key)}: <b>{_int_or_zero(data.get(key))}</b>")
    lines.extend(["", SECTIONS_PROMPT])
    return "\n".join(lines)


async def _notify(event: Message | CallbackQuery, text: str) -> None:
    """Отдельное сообщение (список, из которого пришёл callback, не трогаем)."""
    if isinstance(event, CallbackQuery):
        message = event.message
        if isinstance(message, Message):
            await message.answer(text)
            return
        bot = event.bot
        if bot is not None and event.from_user is not None:
            await bot.send_message(event.from_user.id, text)
            return
        logger.warning("Не удалось отправить уведомление по callback %s", event.id)
        return
    await event.answer(text)


# --------------------------------------------------------------------------- #
# Рендереры списков (используются и в start.py)
# --------------------------------------------------------------------------- #


async def render_section(event: Message | CallbackQuery, key: str, page: int = 1) -> None:
    """Показывает страницу раздела статистики."""
    user_id = _user_id(event)
    if user_id is None:
        logger.warning("Событие без пользователя: раздел %r не показан", key)
        return

    try:
        counts = await stats_repo.counts(user_id)
        current_page, total_pages, limit, offset = _page_bounds(counts.get(key), page)
        items = await stats_repo.section(user_id, key, limit=limit, offset=offset)
    except ValidationError:
        logger.warning("Запрошен неизвестный раздел статистики: %r", key)
        await answer_or_edit(event, UNKNOWN_SECTION, keyboards.sections_kb())
        return

    text = render_track_list(
        _section_title(key), items, current_page, total_pages, _empty_hint(key)
    )
    if not items:
        await answer_or_edit(event, text, keyboards.sections_kb(key))
        return

    markup = keyboards.tracks_page_kb(
        items,
        ctx=section_context(key),
        page=current_page,
        total_pages=total_pages,
    )
    await answer_or_edit(event, text, markup)


async def render_favourites(event: Message | CallbackQuery, page: int = 1) -> None:
    """Страница избранного (кнопка «⭐ Избранное» и пагинация списка)."""
    user_id = _user_id(event)
    if user_id is None:
        return

    total = await favourites_repo.count(user_id)
    current_page, total_pages, limit, offset = _page_bounds(total, page)
    items = await favourites_repo.list_favourites(user_id, limit=limit, offset=offset)
    text = render_track_list(
        "⭐ Избранное", items, current_page, total_pages, texts.EMPTY_FAVOURITES
    )
    if not items:
        await answer_or_edit(event, text, keyboards.sections_kb())
        return

    markup = keyboards.tracks_page_kb(
        items, ctx=CTX_FAVOURITES, page=current_page, total_pages=total_pages
    )
    await answer_or_edit(event, text, markup)


async def render_folder_tracks(
    event: Message | CallbackQuery, folder_id: int, page: int = 1
) -> None:
    """Треки папки — возврат из карточки с контекстом «folder:N»."""
    user_id = _user_id(event)
    if user_id is None:
        return

    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        await answer_or_edit(event, texts.FOLDER_NOT_FOUND, keyboards.sections_kb())
        return

    total = await tracks_repo.count_tracks(user_id, folder_id=folder_id)
    current_page, total_pages, limit, offset = _page_bounds(total, page)
    items = await tracks_repo.list_tracks(
        user_id, folder_id=folder_id, limit=limit, offset=offset
    )
    title = f"📁 {escape(folder.get('name') or 'Папка')}"
    text = render_track_list(title, items, current_page, total_pages, texts.EMPTY_FOLDER)
    if not items:
        await answer_or_edit(event, text, keyboards.sections_kb())
        return

    markup = keyboards.tracks_page_kb(
        items,
        ctx=folder_context(folder_id),
        page=current_page,
        total_pages=total_pages,
    )
    await answer_or_edit(event, text, markup)


async def render_playlist_tracks(
    event: Message | CallbackQuery, playlist_id: int, page: int = 1
) -> None:
    """Состав плейлиста — возврат из карточки с контекстом «pl:N»."""
    user_id = _user_id(event)
    if user_id is None:
        return

    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        await answer_or_edit(event, texts.PLAYLIST_NOT_FOUND, keyboards.sections_kb())
        return

    tracks = await playlists_repo.playlist_tracks(user_id, playlist_id)
    items, current_page, total_pages = _page_of(tracks, page)
    title = f"💿 {escape(playlist.get('name') or 'Плейлист')}"
    text = render_track_list(title, items, current_page, total_pages, texts.EMPTY_PLAYLIST)
    if not items:
        await answer_or_edit(event, text, keyboards.sections_kb())
        return

    markup = keyboards.playlist_tracks_kb(items, playlist_id, current_page, total_pages)
    await answer_or_edit(event, text, markup)


async def render_artist_tracks(
    event: Message | CallbackQuery, artist_id: int, page: int = 1
) -> None:
    """Треки исполнителя — возврат из карточки с контекстом «art:N»."""
    user_id = _user_id(event)
    if user_id is None:
        return

    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        await answer_or_edit(event, texts.ARTIST_NOT_FOUND, keyboards.sections_kb())
        return

    total = await tracks_repo.count_tracks(user_id, artist_id=artist_id)
    current_page, total_pages, limit, offset = _page_bounds(total, page)
    items = await tracks_repo.list_tracks(
        user_id, artist_id=artist_id, limit=limit, offset=offset
    )
    title = f"🎤 {escape(artist.get('name') or 'Исполнитель')}"
    text = render_track_list(title, items, current_page, total_pages, EMPTY_ARTIST_TRACKS)
    if not items:
        await answer_or_edit(event, text, keyboards.sections_kb())
        return

    markup = keyboards.tracks_page_kb(
        items,
        ctx=artist_context(artist_id),
        page=current_page,
        total_pages=total_pages,
    )
    await answer_or_edit(event, text, markup)


async def show_stats_overview(event: Message | CallbackQuery) -> None:
    """Счётчики библиотеки + меню из пяти разделов."""
    user_id = _user_id(event)
    if user_id is None:
        return
    data = await stats_repo.counts(user_id)
    await answer_or_edit(event, _counts_text(data), keyboards.sections_kb())


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """`/stats` — топ прослушиваний и меню разделов со счётчиками."""
    await render_section(message, "top", 1)
    await show_stats_overview(message)


@router.message(Command(*stats_repo.SECTION_KEYS))
async def cmd_section(message: Message, command: CommandObject) -> None:
    """`/top`, `/recent`, `/unplayed`, `/frequent`, `/rare` — первая страница раздела."""
    key = (command.command or "top").strip().casefold()
    if key not in stats_repo.SECTION_KEYS:
        key = "top"
    await render_section(message, key, 1)


# --------------------------------------------------------------------------- #
# Навигация по разделам
# --------------------------------------------------------------------------- #


@router.callback_query(SectionCB.filter(F.key.in_(SECTION_CB_KEYS)))
async def cb_section(callback: CallbackQuery, callback_data: SectionCB) -> None:
    """`SectionCB(open|page)` — открытие раздела и перелистывание страниц.

    Ключ `fav` приходит от пагинации списка избранного (`tracks_page_kb`).
    """
    key = (callback_data.key or "top").strip().casefold()
    page = _safe_page(callback_data.page)
    if key == CTX_FAVOURITES:
        await render_favourites(callback, page)
        return
    await render_section(callback, key, page)


# --------------------------------------------------------------------------- #
# Действия над треком (action="fav" — только в favourites.py)
# --------------------------------------------------------------------------- #


@router.callback_query(TrackCB.filter(F.action == "play"))
async def cb_track_play(callback: CallbackQuery, callback_data: TrackCB, bot: Bot) -> None:
    """«Прослушать»: отправляем файл в исходном формате, затем учитываем воспроизведение.

    В разделах статистики, избранном и поиске встречается не только аудио
    (файлы раздела «Другое»), поэтому отправка идёт через
    `media.send_media_to_user`: документ, видео, кружок и голосовое уходят
    своим методом Bot API. Прослушивание засчитывается только после успешной
    отправки — как в `folders.py` и `tracks.py`.
    """
    user_id = _user_id(callback)
    track_id = _int_or_zero(callback_data.track_id)
    if user_id is None or track_id <= 0:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    try:
        track = await tracks_repo.get_track(user_id, track_id)
    except MusicBoxError:
        logger.exception("Не удалось получить трек %s", track_id)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    await ack(callback, SENDING_TRACK)

    message = callback.message
    chat_id = message.chat.id if isinstance(message, Message) else user_id
    try:
        await media.send_media_to_user(bot, chat_id, track, caption=_track_caption(track))
    except StorageError as exc:
        logger.warning("Не удалось отправить трек %s пользователю %s: %s", track_id, user_id, exc)
        await _notify(callback, f"⚠️ {escape(str(exc))}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка при отправке трека %s", track_id)
        await _notify(callback, texts.ERROR_TRY_AGAIN)
        return

    try:
        await tracks_repo.register_play(user_id, track_id, source="bot")
    except MusicBoxError:
        logger.exception("Не удалось учесть прослушивание трека %s", track_id)
        await _notify(callback, f"⚠️ {PLAY_FAILED}")


@router.callback_query(TrackCB.filter(F.action == "info"))
async def cb_track_info(callback: CallbackQuery, callback_data: TrackCB) -> None:
    """«Подробнее» — карточка трека со всеми данными."""
    user_id = _user_id(callback)
    if user_id is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    track = await tracks_repo.get_track(user_id, _int_or_zero(callback_data.track_id))
    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    ctx = callback_data.ctx or section_context("top")
    markup = keyboards.track_actions_kb(track, ctx, _safe_page(callback_data.page))
    await answer_or_edit(callback, _track_card(track), markup)


@router.callback_query(TrackCB.filter(F.action == "addpl"))
async def cb_track_add_to_playlist(callback: CallbackQuery, callback_data: TrackCB) -> None:
    """«➕» — выбор плейлиста, в который добавить трек."""
    user_id = _user_id(callback)
    if user_id is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    track_id = _int_or_zero(callback_data.track_id)
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    playlists = await playlists_repo.list_playlists(user_id)
    if not playlists:
        await ack(callback, NO_PLAYLISTS, alert=True)
        return

    text = CHOOSE_PLAYLIST.format(title=escape(track.get("title") or "Без названия"))
    await answer_or_edit(callback, text, keyboards.playlist_pick_kb(playlists, track_id))


async def _show_list(event: Message | CallbackQuery, ctx: Any, page: int) -> None:
    """Показывает список, из которого открыли карточку трека.

    Контекст `fav` сюда попадает только после удаления трека: кнопку «⬅️ Назад»
    для избранного обслуживает `favourites.py`, а поиск (`search`) переигрывать
    отсюда нечем — для него показываем сводку по разделам.
    """
    kind, ident = parse_context(str(ctx or ""))

    if kind in stats_repo.SECTION_KEYS:
        await render_section(event, kind, page)
        return
    if kind == "folder":
        await render_folder_tracks(event, ident, page)
        return
    if kind == "pl":
        await render_playlist_tracks(event, ident, page)
        return
    if kind == "art":
        await render_artist_tracks(event, ident, page)
        return
    if kind == CTX_FAVOURITES:
        await render_favourites(event, page)
        return

    logger.debug("Возврат к списку с контекстом %r — показываю статистику", kind)
    await show_stats_overview(event)


@router.callback_query(TrackCB.filter(F.action == "delete"))
async def cb_track_delete(callback: CallbackQuery, callback_data: TrackCB, bot: Bot) -> None:
    """«🗑 Удалить» из карточки трека — подтверждение, затем удаление.

    Первое нажатие приходит с положительной страницей — показываем вопрос;
    кнопка «✅ Да» присылает тот же `TrackCB` с отрицательной страницей
    (`CONFIRMED_PAGE_SIGN`), и только тогда трек удаляется из БД и из канала.
    """
    user_id = _user_id(callback)
    track_id = _int_or_zero(callback_data.track_id)
    if user_id is None or track_id <= 0:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    raw_page = _int_or_zero(callback_data.page)
    page = _safe_page(abs(raw_page))
    ctx = callback_data.ctx or section_context("top")

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        await _show_list(callback, ctx, page)
        return

    if raw_page >= 0:
        markup = keyboards.confirm_kb(
            TrackCB(
                action="delete",
                track_id=track_id,
                page=CONFIRMED_PAGE_SIGN * page,
                ctx=ctx,
            ).pack(),
            TrackCB(action="info", track_id=track_id, page=page, ctx=ctx).pack(),
        )
        title = escape(track.get("title") or "Без названия")
        await answer_or_edit(callback, CONFIRM_DELETE.format(title=title), markup)
        return

    try:
        deleted = await tracks_repo.delete_track(user_id, track_id)
    except MusicBoxError:
        logger.exception("Не удалось удалить трек %s пользователя %s", track_id, user_id)
        await ack(callback, DELETE_FAILED, alert=True)
        return

    if deleted is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        await _show_list(callback, ctx, page)
        return

    message_id = deleted.get("storage_message_id")
    if message_id and not await storage.delete_from_channel(bot, message_id):
        # Ошибку канала только логируем: в библиотеке трека уже нет.
        logger.warning(
            "Трек %s удалён у пользователя %s, но сообщение %s осталось в канале",
            track_id,
            user_id,
            message_id,
        )

    logger.info("Пользователь %s удалил трек %s", user_id, track_id)
    await ack(callback, TRACK_DELETED)
    await _show_list(callback, ctx, page)


def _owns_back_context(ctx: Any) -> bool:
    """Возвращает True, если возврат к списку обслуживает этот модуль.

    Контексты `fav` и `search` намеренно пропускаются: их обрабатывают
    `favourites.py` и `search.py`.
    """
    kind, _ = parse_context(str(ctx or ""))
    return kind in BACK_CONTEXT_KINDS


@router.callback_query(TrackCB.filter((F.action == "back") & F.ctx.func(_owns_back_context)))
async def cb_track_back(callback: CallbackQuery, callback_data: TrackCB) -> None:
    """«⬅️ Назад» из карточки трека — возврат к списку, откуда её открыли."""
    await _show_list(callback, callback_data.ctx or "", _safe_page(callback_data.page))


__all__ = [
    "render_artist_tracks",
    "render_favourites",
    "render_folder_tracks",
    "render_playlist_tracks",
    "render_section",
    "router",
    "show_stats_overview",
]
