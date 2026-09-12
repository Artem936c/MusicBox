"""Вспомогательные функции бота: пагинация, рендер списков, безопасное редактирование.

Все тексты бота отправляются в режиме HTML (`DefaultBotProperties(parse_mode=ParseMode.HTML)`),
поэтому любые пользовательские данные обязаны проходить через `escape`.
"""

from __future__ import annotations

import html
import logging
import math
import re
from typing import Any, Sequence

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from backend.bot.texts import PAGE_LABEL
from backend.config import settings
from backend.db.repositories.stats import SECTION_KEYS
from backend.services.metadata import format_duration

logger = logging.getLogger(__name__)

#: Фрагменты ошибок Telegram, которые означают «редактировать нечего/незачем».
_NOT_MODIFIED = "message is not modified"
_UNEDITABLE = (
    "message to edit not found",
    "message can't be edited",
    "there is no text in the message",
    "message identifier is not specified",
)

#: Контексты списков, у которых нет собственного идентификатора.
CTX_FAVOURITES = "fav"
CTX_SEARCH = "search"
CTX_UPLOAD = "upload"

#: Теги без атрибутов, которые Telegram понимает в режиме HTML и которые
#: разрешено оставлять в заголовке списка (`render_track_list`). Всё остальное
#: заголовок показывает как обычный текст — см. `_safe_header`.
_HEADER_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "blockquote",
        "tg-spoiler",
    }
)

#: Разметочный «токен» заголовка: тег (возможно с атрибутами) или HTML-сущность.
_HEADER_TOKEN = re.compile(
    r"</?(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>\s[^<>]*)?>"
    r"|&(?:#\d+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);"
)


def escape(text: str | None) -> str:
    """Экранирует пользовательский текст для HTML-разметки Telegram."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _page_size(per_page: int | None = None) -> int:
    """Размер страницы: явный аргумент, иначе `settings.page_size`, иначе 10."""
    size = per_page if per_page else settings.page_size
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    return size if size > 0 else 10


def paginate(items: Sequence[Any], page: int, per_page: int | None = None) -> tuple[list, int]:
    """Возвращает срез страницы и общее число страниц.

    `page` — 1-based, значения вне диапазона мягко приводятся к границам.
    Общее число страниц всегда не меньше единицы (даже для пустого списка).
    """
    size = _page_size(per_page)
    data = list(items)
    total_pages = max(1, math.ceil(len(data) / size))
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), total_pages)
    start = (current - 1) * size
    return data[start : start + size], total_pages


def page_offset(page: int, per_page: int | None = None) -> int:
    """Смещение первого элемента страницы (для запросов к БД с limit/offset)."""
    size = _page_size(per_page)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return max(current - 1, 0) * size


def plural(count: int, forms: tuple[str, str, str]) -> str:
    """Склонение существительного по числу: `plural(2, ("трек", "трека", "треков"))`."""
    number = abs(int(count))
    if number % 10 == 1 and number % 100 != 11:
        form = forms[0]
    elif 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


def tracks_count_label(count: int) -> str:
    """«3 трека» — подпись с числом треков."""
    return plural(count, ("трек", "трека", "треков"))


def format_track_line(index: int, track: dict) -> str:
    """Строка трека для списка: ``1. <b>Title</b> — Artist · 3:07 · ▶️5 ⭐``."""
    title = escape(track.get("title") or "Без названия")
    line = f"{index}. <b>{title}</b>"

    artist = (track.get("artist") or "").strip()
    if artist:
        line += f" — {escape(artist)}"

    meta: list[str] = [format_duration(track.get("duration"))]

    try:
        plays = int(track.get("play_count") or 0)
    except (TypeError, ValueError):
        plays = 0
    stat = f"▶️{plays}"
    if track.get("is_favourite"):
        stat += " ⭐"
    meta.append(stat)

    return line + " · " + " · ".join(meta)


def _safe_header(title: str) -> str:
    """Приводит заголовок списка к разметке, которую Telegram гарантированно разберёт.

    Правила простые и идемпотентные:

    * парные теги из `_HEADER_TAGS` без атрибутов остаются разметкой;
    * корректные HTML-сущности (``&amp;``, ``&lt;``, ``&#171;``) остаются как есть —
      поэтому уже экранированное название не превращается в «&amp;lt;»;
    * всё остальное («<», «&», чужие теги) экранируется и показывается текстом;
    * если теги не сходятся в пары, заголовок экранируется целиком — сломанная
      разметка не должна мешать пользователю открыть свой список.
    """
    raw = title or ""
    parts: list[str] = []
    stack: list[str] = []
    pos = 0

    for match in _HEADER_TOKEN.finditer(raw):
        parts.append(escape(raw[pos : match.start()]))
        token = match.group(0)
        tag = match.group("tag")
        pos = match.end()

        if tag is None:  # HTML-сущность — она уже безопасна.
            parts.append(token)
            continue

        name = tag.casefold()
        if name not in _HEADER_TAGS or match.group("attrs"):
            parts.append(escape(token))
            continue

        if token.startswith("</"):
            if not stack or stack.pop() != name:
                return escape(raw)
        else:
            stack.append(name)
        parts.append(token)

    parts.append(escape(raw[pos:]))
    if stack:
        return escape(raw)
    return "".join(parts)


def render_track_list(
    title: str,
    tracks: Sequence[dict],
    page: int,
    total_pages: int,
    empty_text: str,
    *,
    per_page: int | None = None,
) -> str:
    """Собирает текст страницы со списком треков.

    `title` — HTML-заголовок, но перед отправкой он проходит через `_safe_header`:
    разметкой остаются только парные теги без атрибутов из `_HEADER_TAGS`, всё
    прочее экранируется. Поэтому «сырое» название папки, плейлиста или исполнителя
    в заголовке уже не ломает разбор HTML, а вызовы, которые (правильно) применили
    `escape` сами, не получают двойного экранирования. Если после этого в заголовке
    не осталось собственной разметки, он выделяется жирным автоматически.

    `empty_text` — ГОТОВЫЙ HTML (наши собственные тексты из `backend.bot.texts`).
    Нумерация сквозная: на второй странице список начинается с 11-го номера
    (при размере страницы 10).
    """
    header = _safe_header(title)
    if "<" not in header:
        header = f"<b>{header}</b>"
    items = list(tracks)
    if not items:
        return f"{header}\n\n{empty_text}"

    start = page_offset(page, per_page) + 1
    lines = [format_track_line(start + shift, track) for shift, track in enumerate(items)]

    body = "\n".join(lines)
    if total_pages > 1:
        footer = PAGE_LABEL.format(page=min(max(int(page or 1), 1), total_pages), total=total_pages)
        return f"{header}\n\n{body}\n\n<i>{footer}</i>"
    return f"{header}\n\n{body}"


def section_context(key: str) -> str:
    """Контекст (`TrackCB.ctx`) для раздела статистики.

    Неизвестные ключи приводятся к `top`, чтобы кнопки списка всегда оставались рабочими.
    """
    normalized = (key or "").strip()
    if normalized in SECTION_KEYS or normalized in (CTX_FAVOURITES, CTX_SEARCH, CTX_UPLOAD):
        return normalized
    return "top"


def folder_context(folder_id: int) -> str:
    """Контекст списка треков папки: ``folder:12``."""
    return f"folder:{int(folder_id)}"


def playlist_context(playlist_id: int) -> str:
    """Контекст списка треков плейлиста: ``pl:3``."""
    return f"pl:{int(playlist_id)}"


def artist_context(artist_id: int) -> str:
    """Контекст списка треков исполнителя: ``art:7``."""
    return f"art:{int(artist_id)}"


def parse_context(ctx: str) -> tuple[str, int]:
    """Разбирает контекст в пару ``(вид, идентификатор)``.

    ``"folder:12" -> ("folder", 12)``, ``"top" -> ("top", 0)``.
    """
    raw = (ctx or "").strip()
    if ":" in raw:
        kind, _, value = raw.partition(":")
        try:
            return kind, int(value)
        except ValueError:
            return kind, 0
    return raw or "top", 0


async def safe_edit(
    message: Any,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует сообщение, молча проглатывая «message is not modified».

    Если сообщение отредактировать невозможно (устарело, недоступно, без текста) —
    отправляет новое сообщение в тот же чат.
    """
    if not isinstance(message, Message):
        # InaccessibleMessage и прочие «непригодные» объекты редактировать нельзя.
        await _fallback_send(message, text, reply_markup)
        return

    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if _NOT_MODIFIED in detail:
            logger.debug("Сообщение %s не изменилось — правка пропущена", message.message_id)
            return
        if any(fragment in detail for fragment in _UNEDITABLE):
            logger.debug("Сообщение %s нельзя отредактировать: %s", message.message_id, exc)
            await _fallback_send(message, text, reply_markup)
            return
        raise
    except TelegramForbiddenError as exc:
        logger.warning("Нет доступа к чату при редактировании сообщения: %s", exc)


async def _fallback_send(
    message: Any,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет новое сообщение вместо неудавшегося редактирования."""
    chat = getattr(message, "chat", None)
    bot = getattr(message, "bot", None)
    try:
        if isinstance(message, Message):
            await message.answer(text, reply_markup=reply_markup)
        elif bot is not None and chat is not None:
            await bot.send_message(chat.id, text, reply_markup=reply_markup)
        else:
            logger.warning("Не удалось отправить сообщение: нет контекста чата")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Не удалось отправить сообщение: %s", exc)


async def answer_or_edit(
    event: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Универсальный ответ: на команду — новым сообщением, на callback — правкой."""
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            await safe_edit(event.message, text, reply_markup)
        elif event.from_user is not None and event.bot is not None:
            await event.bot.send_message(event.from_user.id, text, reply_markup=reply_markup)
        await ack(event)
        return

    if isinstance(event, Message):
        await event.answer(text, reply_markup=reply_markup)
        return

    logger.warning("answer_or_edit: неподдерживаемый тип события %s", type(event).__name__)


async def ack(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Закрывает «часики» на кнопке; устаревший callback не считается ошибкой."""
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramBadRequest as exc:
        logger.debug("Не удалось ответить на callback: %s", exc)


__all__ = [
    "CTX_FAVOURITES",
    "CTX_SEARCH",
    "CTX_UPLOAD",
    "ack",
    "answer_or_edit",
    "artist_context",
    "escape",
    "folder_context",
    "format_duration",
    "format_track_line",
    "page_offset",
    "paginate",
    "parse_context",
    "playlist_context",
    "plural",
    "render_track_list",
    "safe_edit",
    "section_context",
    "tracks_count_label",
]
