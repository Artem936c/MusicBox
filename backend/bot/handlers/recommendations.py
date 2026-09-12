"""Хендлеры рекомендаций исполнителей (пункт 2 ТЗ, раздел 5 контракта V2).

Команда ``/recommendations`` показывает две категории, подобранные сервисом
:mod:`backend.services.recommendations`:

* «🔥 Популярные» — исполнители с наибольшим числом прослушиваний в базе;
* «💎 Менее известные» — те, кого слушают редко.

Категории переключаются инлайн-кнопками, списки листаются по
``settings.page_size`` записей. Для каждого исполнителя показываются имя, число
прослушиваний, число слушателей и причина рекомендации (поле ``reason``).

Если исполнитель уже есть в библиотеке пользователя (``local_artist_id``), у
строки появляется кнопка перехода к его трекам (карточка исполнителя из
``handlers/artists.py``) и кнопка «🆕» — непрослушанные треки этого исполнителя
(``artists_repo.artist_unplayed_tracks``). Если исполнителя в библиотеке нет —
кнопка «Найти в Telegram», которая запускает тот же поиск, что и ``/tgsearch``.

Деградация сервиса показывается ЯВНО: значения ``shortfall`` и ``note`` из
ответа :func:`backend.services.recommendations.build` выводятся отдельной
строкой, чтобы короткий список не выглядел ошибкой.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import texts
from backend.bot.callbacks import ArtistCB, NavCB, RecoCB
from backend.bot.keyboards import tracks_page_kb
from backend.bot.utils import (
    ack,
    artist_context,
    escape,
    page_offset,
    paginate,
    plural,
    render_track_list,
    safe_edit,
    tracks_count_label,
)
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.errors import MusicBoxError
from backend.services import recommendations as reco_service

logger = logging.getLogger(__name__)

router = Router(name="recommendations")


# ---------------------------------------------------------------------------
# Константы модуля
# ---------------------------------------------------------------------------

#: Категории рекомендаций (значения поля `RecoCB.kind`).
KIND_POPULAR: Final[str] = "popular"
KIND_UNDERGROUND: Final[str] = "underground"

#: Подписи категорий для кнопок и заголовков.
KIND_LABELS: Final[dict[str, str]] = {
    KIND_POPULAR: "🔥 Популярные",
    KIND_UNDERGROUND: "💎 Менее известные",
}

#: Пояснение к каждой категории (первая строка экрана).
KIND_HINTS: Final[dict[str, str]] = {
    KIND_POPULAR: "Их слушают чаще всего в MusicBox.",
    KIND_UNDERGROUND: "Их слушают редко — есть шанс открыть что-то новое.",
}

#: Сокращения и синонимы категорий, которые модуль принимает из callback-данных.
_KIND_ALIASES: Final[dict[str, str]] = {
    KIND_POPULAR: KIND_POPULAR,
    "pop": KIND_POPULAR,
    "top": KIND_POPULAR,
    KIND_UNDERGROUND: KIND_UNDERGROUND,
    "und": KIND_UNDERGROUND,
    "rare": KIND_UNDERGROUND,
}

#: Собственные callback-данные модуля (фабрики из `callbacks.py` их не разбирают,
#: потому что первый сегмент отличается от префиксов «rc», «art», «nav»).
TG_PREFIX: Final[str] = "rcs"  # rcs:<token>            — «Найти в Telegram»
UNPLAYED_PREFIX: Final[str] = "rcu"  # rcu:<artist_id>:<page> — непрослушанные треки

#: Сколько живёт подобранная подборка (пагинация не пересобирает её заново).
CACHE_TTL: Final[float] = 900.0
#: Сколько пользователей держим в кэше подборок одновременно.
CACHE_LIMIT: Final[int] = 256
#: Сколько имён исполнителей держим для кнопки «Найти в Telegram».
TOKEN_LIMIT: Final[int] = 1024

#: Верхняя граница выборки непрослушанных треков (дальше листать нечего).
UNPLAYED_LIMIT: Final[int] = 200

#: Лимит Telegram на длину текстового сообщения.
MESSAGE_LIMIT: Final[int] = 4096
#: Лимит на длину подписи внутри кнопки.
LABEL_LIMIT: Final[int] = 22
#: Лимит на длину всплывающего ответа на callback.
TOAST_LIMIT: Final[int] = 190

PLAYS_FORMS: Final[tuple[str, str, str]] = (
    "прослушивание",
    "прослушивания",
    "прослушиваний",
)
LISTENERS_FORMS: Final[tuple[str, str, str]] = ("слушатель", "слушателя", "слушателей")
ARTISTS_FORMS: Final[tuple[str, str, str]] = (
    "исполнитель",
    "исполнителя",
    "исполнителей",
)


# ---------------------------------------------------------------------------
# Русские тексты экранов (объявлены локально — texts.py правит другой модуль)
# ---------------------------------------------------------------------------

RECO_TITLE = "✨ <b>Рекомендации исполнителей</b>"
RECO_EMPTY_SECTION = (
    "🤷 В этой категории пока пусто. Послушайте ещё несколько треков — "
    "и подборка соберётся."
)
RECO_ERROR = "😔 Не получилось собрать рекомендации. Попробуйте чуть позже."
RECO_REFRESHED = "🔄 Подборка обновлена."
RECO_EXPIRED = "⌛ Подборка устарела. Откройте /recommendations заново."
RECO_NO_REASON = "подобрано по вашей библиотеке"
RECO_IN_LIBRARY = "🎧 уже есть в вашей библиотеке"
RECO_LEGEND = (
    "🎧 — треки исполнителя · 🆕 — непрослушанное · 🌐 — найти в Telegram"
)

UNPLAYED_TITLE = "🆕 Непрослушанные треки — 🎤 {name}"
UNPLAYED_EMPTY = "🎉 У этого исполнителя не осталось непрослушанных треков."
UNPLAYED_TRUNCATED = (
    "Показаны первые {limit} непрослушанных треков — послушайте их, "
    "и я покажу остальные."
)

TG_SEARCH_TOAST = "🌐 Ищу «{name}» в Telegram…"
TG_SEARCH_HINT = (
    "🌐 Чтобы найти «{name}» в Telegram, отправьте команду:\n"
    "<code>/tgsearch {query}</code>"
)


# ---------------------------------------------------------------------------
# Кэш подборок и имён исполнителей
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CachedReco:
    """Подборка пользователя вместе со временем построения."""

    data: dict[str, Any]
    created_at: float


#: user_id -> подборка. Нужен, чтобы листание страниц не пересобирало рекомендации.
_cache: "OrderedDict[int, _CachedReco]" = OrderedDict()

#: короткий токен -> имя исполнителя (для кнопки «Найти в Telegram»:
#: имя не влезает в 64 байта callback_data).
_tokens: "OrderedDict[str, str]" = OrderedDict()


def _cache_get(user_id: int) -> dict[str, Any] | None:
    """Свежая подборка пользователя или None (протухшую сразу удаляем)."""
    entry = _cache.get(user_id)
    if entry is None:
        return None
    if time.monotonic() - entry.created_at > CACHE_TTL:
        _cache.pop(user_id, None)
        logger.debug("Подборка пользователя %s устарела — пересоберу", user_id)
        return None
    _cache.move_to_end(user_id)
    return entry.data


def _cache_put(user_id: int, data: dict[str, Any]) -> None:
    """Кладёт подборку в кэш, вытесняя самые старые записи."""
    _cache[user_id] = _CachedReco(data=data, created_at=time.monotonic())
    _cache.move_to_end(user_id)
    while len(_cache) > CACHE_LIMIT:
        evicted, _ = _cache.popitem(last=False)
        logger.debug("Вытеснил подборку пользователя %s из кэша", evicted)


def _remember_name(name: str) -> str:
    """Запоминает имя исполнителя и возвращает короткий токен для кнопки."""
    token = hashlib.blake2s(name.encode("utf-8"), digest_size=4).hexdigest()
    _tokens[token] = name
    _tokens.move_to_end(token)
    while len(_tokens) > TOKEN_LIMIT:
        _tokens.popitem(last=False)
    return token


def _recall_name(token: str) -> str | None:
    """Имя исполнителя по токену (None, если кнопка слишком старая)."""
    name = _tokens.get(token)
    if name is not None:
        _tokens.move_to_end(token)
    return name


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


def _clamp_page(page: Any, total_pages: int) -> int:
    """Приводит номер страницы к диапазону 1..total_pages."""
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    return min(max(current, 1), max(1, int(total_pages)))


def _as_int(value: Any, default: int = 0) -> int:
    """Мягкое приведение к int (данные приходят из БД и из callback-данных)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _field(item: Any, key: str, default: Any = None) -> Any:
    """Читает поле у dataclass `Recommendation` или у обычного dict."""
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _text_field(item: Any, key: str) -> str:
    """Строковое поле рекомендации без лишних пробелов."""
    value = _field(item, key)
    return str(value).strip() if value is not None else ""


def _int_field(item: Any, key: str) -> int:
    """Целочисленное поле рекомендации (мусор и None -> 0)."""
    return max(0, _as_int(_field(item, key)))


def _short(value: Any, limit: int = LABEL_LIMIT) -> str:
    """Подпись кнопки: одна строка, обрезанная до разумной длины."""
    text = " ".join(str(value or "").split())
    if not text:
        return "Без названия"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clamp_text(text: str) -> str:
    """Обрезает сообщение до лимита Telegram по границе строки.

    Резать по «\\n» безопасно: каждая строка экрана содержит парную разметку
    целиком, поэтому обрезанный текст остаётся валидным HTML.
    """
    if len(text) <= MESSAGE_LIMIT:
        return text
    cut = text.rfind("\n", 0, MESSAGE_LIMIT - 1)
    if cut <= 0:
        cut = MESSAGE_LIMIT - 1
    logger.warning("Сообщение длиннее %s символов — обрезаю", MESSAGE_LIMIT)
    return text[:cut] + "…"


def _normalize_kind(value: Any) -> str:
    """Категория из callback-данных (неизвестное значение -> «Популярные»)."""
    key = str(value or "").strip().casefold()
    return _KIND_ALIASES.get(key, KIND_POPULAR)


def _other_kind(kind: str) -> str:
    """Вторая категория (для кнопки переключения)."""
    return KIND_UNDERGROUND if kind == KIND_POPULAR else KIND_POPULAR


def _noop_data() -> str:
    """Callback-данные «ничего не делать» (обрабатывает handlers/start.py)."""
    return NavCB(action="noop").pack()


def _unplayed_data(artist_id: int, page: int) -> str:
    """Callback-данные экрана непрослушанных треков исполнителя."""
    return f"{UNPLAYED_PREFIX}:{int(artist_id)}:{max(1, int(page))}"


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
        await bot.send_message(callback.from_user.id, text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Данные подборки
# ---------------------------------------------------------------------------


async def _load(user_id: int, *, refresh: bool = False) -> dict[str, Any]:
    """Возвращает подборку пользователя: из кэша либо из сервиса рекомендаций."""
    if not refresh:
        cached = _cache_get(user_id)
        if cached is not None:
            return cached

    data = await reco_service.build(user_id, limit=reco_service.DEFAULT_LIMIT)
    if not isinstance(data, Mapping):
        raise MusicBoxError(
            f"Сервис рекомендаций вернул {type(data).__name__} вместо словаря"
        )

    result = dict(data)
    _cache_put(user_id, result)
    return result


def _items(data: Mapping[str, Any], kind: str) -> list[Any]:
    """Список рекомендаций выбранной категории."""
    raw = data.get(kind)
    if raw is None:
        return []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return list(raw)
    logger.warning(
        "Категория «%s» пришла как %s — показываю пустой список", kind, type(raw).__name__
    )
    return []


def _shortfall(data: Mapping[str, Any], kind: str) -> int:
    """Сколько позиций категории не хватило до запрошенного лимита."""
    raw = data.get("shortfall")
    if not isinstance(raw, Mapping):
        return 0
    return max(0, _as_int(raw.get(kind)))


def _degradation_text(data: Mapping[str, Any], kind: str, shown: int) -> str | None:
    """Понятное пояснение к неполной подборке (или None, если всё в порядке).

    Приоритет у ``note`` сервиса — он честно объясняет, сколько позиций добрано
    из библиотеки пользователя и сколько не хватило. Если сервис ``note`` не дал,
    но ``shortfall`` не нулевой, пояснение собирается здесь: короткий список
    не должен выглядеть сбоем.
    """
    note = str(data.get("note") or "").strip()
    missing = _shortfall(data, kind)
    if note:
        return f"ℹ️ <i>{escape(note)}</i>"
    if missing > 0:
        label = KIND_LABELS.get(kind, kind)
        return (
            f"ℹ️ <i>В категории «{escape(label)}» пока {shown} из {shown + missing}: "
            "в базе мало пользователей и прослушиваний.</i>"
        )
    return None


# ---------------------------------------------------------------------------
# Рендер экрана рекомендаций
# ---------------------------------------------------------------------------


def _item_lines(number: int, item: Any) -> list[str]:
    """Три строки одной рекомендации: имя, статистика, причина."""
    name = escape(_text_field(item, "name") or "Без названия")
    plays = _int_field(item, "total_plays")
    listeners = _int_field(item, "listeners")
    raw_reason = _text_field(item, "reason") or RECO_NO_REASON

    lines = [
        f"{number}. <b>{name}</b>",
        f"   ▶️ {plural(plays, PLAYS_FORMS)} · 👥 {plural(listeners, LISTENERS_FORMS)}",
    ]
    tail = f"   💡 {escape(raw_reason)}"
    # Пометку про библиотеку не дублируем: причина «из вашей библиотеки» уже
    # означает ровно это (деградационный режим сервиса).
    if (
        _int_field(item, "local_artist_id") > 0
        and raw_reason.casefold() != reco_service.REASON_LIBRARY.casefold()
    ):
        tail += f" · {RECO_IN_LIBRARY}"
    lines.append(tail)
    return lines


def _reco_kb(
    kind: str,
    page: int,
    total_pages: int,
    items: Sequence[Any],
    start_number: int,
) -> InlineKeyboardMarkup:
    """Клавиатура экрана: строка на исполнителя, переключение категорий, листание."""
    builder = InlineKeyboardBuilder()

    for shift, item in enumerate(items):
        number = start_number + shift
        name = _text_field(item, "name") or "Без названия"
        label = _short(name)
        local_id = _int_field(item, "local_artist_id")
        if local_id > 0:
            builder.row(
                InlineKeyboardButton(
                    text=f"🎧 {number}. {label}",
                    callback_data=ArtistCB(
                        action="open", artist_id=local_id, page=1
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="🆕",
                    callback_data=_unplayed_data(local_id, 1),
                ),
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"🌐 {number}. {label}",
                    callback_data=f"{TG_PREFIX}:{_remember_name(name)}",
                )
            )

    active = KIND_LABELS.get(kind, kind)
    other = _other_kind(kind)
    builder.row(
        InlineKeyboardButton(
            text=f"• {active}",
            callback_data=RecoCB(action="open", kind=kind, page=1).pack(),
        ),
        InlineKeyboardButton(
            text=KIND_LABELS.get(other, other),
            callback_data=RecoCB(action="open", kind=other, page=1).pack(),
        ),
    )

    if total_pages > 1:
        current = _clamp_page(page, total_pages)
        prev_data = (
            RecoCB(action="page", kind=kind, page=current - 1).pack()
            if current > 1
            else _noop_data()
        )
        next_data = (
            RecoCB(action="page", kind=kind, page=current + 1).pack()
            if current < total_pages
            else _noop_data()
        )
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·", callback_data=prev_data
            ),
            InlineKeyboardButton(
                text=f"{current}/{total_pages}", callback_data=_noop_data()
            ),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·", callback_data=next_data
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=RecoCB(action="refresh", kind=kind, page=1).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ В меню", callback_data=NavCB(action="menu").pack()
        ),
    )
    return builder.as_markup()


def _fallback_kb() -> InlineKeyboardMarkup:
    """Минимальная клавиатура для экрана ошибки: повтор и возврат в меню."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔄 Повторить",
            callback_data=RecoCB(action="refresh", kind=KIND_POPULAR, page=1).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ В меню", callback_data=NavCB(action="menu").pack()
        ),
    )
    return builder.as_markup()


def _render(data: Mapping[str, Any], kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру страницы категории."""
    items = _items(data, kind)
    per_page = _page_size()
    page_items, total_pages = paginate(items, page, per_page)
    current = _clamp_page(page, total_pages)
    start = page_offset(current, per_page) + 1

    label = KIND_LABELS.get(kind, kind)
    header = f"{RECO_TITLE}\n<b>{escape(label)}</b>"
    if items:
        header += f" · {plural(len(items), ARTISTS_FORMS)}"
    hint = KIND_HINTS.get(kind)
    lines = [header]
    if hint:
        lines.append(f"<i>{escape(hint)}</i>")
    lines.append("")

    if page_items:
        for shift, item in enumerate(page_items):
            lines.extend(_item_lines(start + shift, item))
            lines.append("")
        if total_pages > 1:
            lines.append(
                f"<i>{texts.PAGE_LABEL.format(page=current, total=total_pages)}</i>"
            )
        lines.append(f"<i>{RECO_LEGEND}</i>")
        lines.append("")
    else:
        lines.append(RECO_EMPTY_SECTION)
        lines.append("")

    degradation = _degradation_text(data, kind, len(items))
    if degradation:
        lines.append(degradation)

    text = "\n".join(lines).strip()
    markup = _reco_kb(kind, current, total_pages, page_items, start)
    return _clamp_text(text), markup


async def _screen(
    user_id: int, kind: str, page: int, *, refresh: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Готовый экран рекомендаций; при сбое — понятное сообщение об ошибке."""
    try:
        data = await _load(user_id, refresh=refresh)
    except MusicBoxError as exc:
        logger.warning("Рекомендации для %s недоступны: %s", user_id, exc)
        return f"{RECO_TITLE}\n\n{RECO_ERROR}", _fallback_kb()
    except Exception:
        logger.exception("Не удалось построить рекомендации для %s", user_id)
        return f"{RECO_TITLE}\n\n{RECO_ERROR}", _fallback_kb()

    try:
        return _render(data, kind, page)
    except Exception:
        logger.exception("Не удалось отрисовать рекомендации для %s", user_id)
        return f"{RECO_TITLE}\n\n{RECO_ERROR}", _fallback_kb()


# ---------------------------------------------------------------------------
# Экран непрослушанных треков исполнителя
# ---------------------------------------------------------------------------


async def _render_unplayed(
    user_id: int, artist_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Страница непрослушанных треков исполнителя; None — исполнитель не найден."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        return None

    tracks = await artists_repo.artist_unplayed_tracks(
        user_id, artist_id, limit=UNPLAYED_LIMIT
    )
    per_page = _page_size()
    page_items, total_pages = paginate(tracks, page, per_page)
    current = _clamp_page(page, total_pages)

    name = escape(artist.get("name") or "Без названия")
    title = UNPLAYED_TITLE.format(name=name)
    if tracks:
        title += f" — {tracks_count_label(len(tracks))}"

    text = render_track_list(
        title, page_items, current, total_pages, UNPLAYED_EMPTY, per_page=per_page
    )
    if len(tracks) >= UNPLAYED_LIMIT:
        text += "\n\n<i>" + escape(
            UNPLAYED_TRUNCATED.format(limit=UNPLAYED_LIMIT)
        ) + "</i>"

    extra_rows: list[list[InlineKeyboardButton]] = []
    if total_pages > 1:
        prev_data = (
            _unplayed_data(artist_id, current - 1) if current > 1 else _noop_data()
        )
        next_data = (
            _unplayed_data(artist_id, current + 1)
            if current < total_pages
            else _noop_data()
        )
        extra_rows.append(
            [
                InlineKeyboardButton(
                    text="◀️" if current > 1 else "·", callback_data=prev_data
                ),
                InlineKeyboardButton(
                    text=f"{current}/{total_pages}", callback_data=_noop_data()
                ),
                InlineKeyboardButton(
                    text="▶️" if current < total_pages else "·", callback_data=next_data
                ),
            ]
        )
    extra_rows.append(
        [
            InlineKeyboardButton(
                text="✨ К рекомендациям",
                callback_data=RecoCB(action="open", kind=KIND_POPULAR, page=1).pack(),
            )
        ]
    )

    markup = tracks_page_kb(
        page_items,
        ctx=artist_context(artist_id),
        page=current,
        # Пагинация у этого списка своя (`rcu:`), поэтому штатную строку
        # листания клавиатура треков не рисует.
        total_pages=1,
        extra_rows=extra_rows,
    )
    return _clamp_text(text), markup


# ---------------------------------------------------------------------------
# Поиск исполнителя в Telegram
# ---------------------------------------------------------------------------


def _tg_hint(name: str) -> str:
    """Подсказка с готовой командой /tgsearch, если запустить поиск не вышло."""
    safe = escape(name)
    return TG_SEARCH_HINT.format(name=safe, query=safe)


async def _run_telegram_search(message: Message, user_id: int, query: str) -> bool:
    """Запускает поиск в Telegram силами handlers/tg_search.py.

    Логика поиска (доступность Telethon, кнопки импорта, обработка ошибок) живёт
    в разделе ``/tgsearch`` — дублировать её здесь нельзя, иначе кнопки импорта
    перестанут совпадать с обработчиками того модуля. Если точка входа почему-то
    недоступна, возвращаем False, и пользователь получает готовую команду.
    """
    try:
        from backend.bot.handlers import tg_search as tg_search_handlers
    except Exception:
        logger.exception("Модуль поиска в Telegram недоступен")
        return False

    runner = getattr(tg_search_handlers, "run_tg_search", None) or getattr(
        tg_search_handlers, "_run_tg_search", None
    )
    if not callable(runner):
        logger.error(
            "handlers/tg_search.py не предоставляет точку входа поиска — "
            "показываю пользователю команду /tgsearch"
        )
        return False

    try:
        await runner(message, user_id, query)
    except Exception:
        logger.exception(
            "Поиск «%s» в Telegram по кнопке рекомендаций не удался (пользователь %s)",
            query,
            user_id,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Команда /recommendations
# ---------------------------------------------------------------------------


@router.message(Command("recommendations"))
async def cmd_recommendations(message: Message) -> None:
    """Показывает свежую подборку: сначала категория «🔥 Популярные»."""
    if message.from_user is None:
        return
    user_id = int(message.from_user.id)
    text, markup = await _screen(user_id, KIND_POPULAR, 1, refresh=True)
    await message.answer(text, reply_markup=markup)
    logger.info("Пользователь %s открыл рекомендации", user_id)


# ---------------------------------------------------------------------------
# Колбэки рекомендаций
# ---------------------------------------------------------------------------


@router.callback_query(RecoCB.filter())
async def cb_recommendations(
    callback: CallbackQuery, callback_data: RecoCB, bot: Bot
) -> None:
    """Переключение категорий, пагинация и обновление подборки."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    action = (callback_data.action or "").strip()
    kind = _normalize_kind(callback_data.kind)
    page = max(1, _as_int(callback_data.page, 1))

    if action == "noop":
        await ack(callback)
        return

    if action == "back":
        kind, page = KIND_POPULAR, 1
    elif action not in {"open", "page", "refresh"}:
        logger.debug("Неизвестное действие рекомендаций: %r", action)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    refresh = action == "refresh"
    text, markup = await _screen(user_id, kind, page, refresh=refresh)
    await _show(callback, bot, text, markup)
    await ack(callback, RECO_REFRESHED if refresh else None)


# ---------------------------------------------------------------------------
# Непрослушанные треки исполнителя
# ---------------------------------------------------------------------------


def _is_unplayed(data: Any) -> bool:
    """Фильтр собственных callback-данных ``rcu:<artist_id>:<page>``."""
    return isinstance(data, str) and data.startswith(f"{UNPLAYED_PREFIX}:")


def _parse_unplayed(data: str) -> tuple[int, int] | None:
    """Разбирает ``rcu:<artist_id>:<page>`` в пару чисел."""
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.func(_is_unplayed))
async def cb_artist_unplayed(callback: CallbackQuery, bot: Bot) -> None:
    """Показывает треки исполнителя, которые пользователь ещё не слушал."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parsed = _parse_unplayed(callback.data)
    if parsed is None:
        logger.warning("Не удалось разобрать кнопку непрослушанных: %r", callback.data)
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    artist_id, page = parsed
    user_id = int(callback.from_user.id)

    try:
        rendered = await _render_unplayed(user_id, artist_id, max(1, page))
    except MusicBoxError as exc:
        logger.warning(
            "Непрослушанные треки исполнителя %s недоступны (пользователь %s): %s",
            artist_id,
            user_id,
            exc,
        )
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return
    except Exception:
        logger.exception(
            "Не удалось показать непрослушанные треки исполнителя %s (пользователь %s)",
            artist_id,
            user_id,
        )
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    if rendered is None:
        await ack(callback, texts.ARTIST_NOT_FOUND, alert=True)
        return

    await _show(callback, bot, rendered[0], rendered[1])
    await ack(callback)


# ---------------------------------------------------------------------------
# Кнопка «Найти в Telegram»
# ---------------------------------------------------------------------------


def _is_tg_search(data: Any) -> bool:
    """Фильтр собственных callback-данных ``rcs:<token>``."""
    return isinstance(data, str) and data.startswith(f"{TG_PREFIX}:")


@router.callback_query(F.data.func(_is_tg_search))
async def cb_find_in_telegram(callback: CallbackQuery, bot: Bot) -> None:
    """Ищет рекомендованного исполнителя в Telegram (тот же поиск, что /tgsearch)."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    token = callback.data.split(":", 1)[1]
    name = _recall_name(token)
    if not name:
        logger.debug("Токен рекомендации %r больше не известен", token)
        await ack(callback, RECO_EXPIRED, alert=True)
        return

    user_id = int(callback.from_user.id)
    await ack(callback, _short(TG_SEARCH_TOAST.format(name=name), TOAST_LIMIT))

    message = callback.message if isinstance(callback.message, Message) else None
    if message is None:
        try:
            await bot.send_message(user_id, _tg_hint(name))
        except Exception:
            logger.exception(
                "Не удалось отправить подсказку по поиску «%s» пользователю %s",
                name,
                user_id,
            )
        return

    logger.info("Пользователь %s ищет «%s» в Telegram из рекомендаций", user_id, name)
    if await _run_telegram_search(message, user_id, name):
        return

    try:
        await message.answer(_tg_hint(name))
    except Exception:
        logger.exception(
            "Не удалось отправить подсказку по поиску «%s» пользователю %s",
            name,
            user_id,
        )


__all__ = [
    "KIND_LABELS",
    "KIND_POPULAR",
    "KIND_UNDERGROUND",
    "router",
]
