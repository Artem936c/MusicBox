"""Репозиторий статистики прослушиваний: пять разделов, счётчики и сводка.

Все выборки строятся поверх SQL-фрагментов из :mod:`backend.db.repositories.tracks`
(`track_query`), поэтому набор полей трека одинаков во всём проекте.
Любой запрос обязательно фильтруется по ``user_id`` и по ``file_type='audio'``:
статистика — это раздел «Треки», файлы раздела «Другое» в неё не входят.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import settings
from backend.db.database import db
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

# Ключи разделов статистики (порядок = порядок вывода в сводке).
SECTION_KEYS = ("top", "recent", "unplayed", "frequent", "rare")

# Русские заголовки разделов — используются ботом, API и фронтендом.
SECTION_TITLES = {
    "top": "Самые часто прослушиваемые",
    "recent": "Недавно добавленные",
    "unplayed": "Ни разу не проигранные",
    "frequent": "Часто прослушиваемые",
    "rare": "Редко прослушиваемые",
}

# Границы для параметров пагинации.
MIN_LIMIT = 1
MAX_LIMIT = 200

# Значения limit по умолчанию для каждого раздела.
DEFAULT_TOP_LIMIT = 10
DEFAULT_RECENT_LIMIT = 20
DEFAULT_UNPLAYED_LIMIT = 50
DEFAULT_FREQUENT_LIMIT = 50
DEFAULT_RARE_LIMIT = 50

SECTION_DEFAULT_LIMITS = {
    "top": DEFAULT_TOP_LIMIT,
    "recent": DEFAULT_RECENT_LIMIT,
    "unplayed": DEFAULT_UNPLAYED_LIMIT,
    "frequent": DEFAULT_FREQUENT_LIMIT,
    "rare": DEFAULT_RARE_LIMIT,
}

# Порядки сортировки разделов (одно место — чтобы бот, API и тесты совпадали).
ORDER_RECENT = "t.created_at DESC, t.id DESC"
ORDER_UNPLAYED = "t.created_at DESC, t.id DESC"
ORDER_BY_PLAYS = "t.play_count DESC, t.last_played_at DESC, t.id DESC"

_LIMIT_CLAUSE = "LIMIT ? OFFSET ?"

# Статистика — это раздел «Треки», то есть только аудио (ARCHITECTURE-V2, п. 1.3:
# «Раздел «Треки» = file_type='audio'; раздел «Другое» = все остальные»).
# Документы, видео, кружочки и голосовые не попадают ни в разделы, ни в счётчики.
# Тип подставляется плейсхолдером — SQL-текст пользовательским вводом не собирается.
_AUDIO_WHERE = "AND t.file_type = ?"
_AUDIO_TYPE = tracks_repo.DEFAULT_FILE_TYPE


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _coerce_int(value: Any, default: int) -> int:
    """Мягкое приведение к int: при мусоре возвращает default и пишет предупреждение."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Некорректное числовое значение %r, использую %s", value, default)
        return default


def _normalize_user_id(user_id: Any) -> int:
    """Идентификатор пользователя обязателен и должен быть целым числом."""
    if user_id is None or isinstance(user_id, bool):
        raise ValidationError("Не указан идентификатор пользователя")
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Некорректный идентификатор пользователя") from exc


def _normalize_limit(value: Any, default: int) -> int:
    """limit всегда в диапазоне 1..200."""
    limit = _coerce_int(value, default)
    if limit < MIN_LIMIT:
        return MIN_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _normalize_offset(value: Any) -> int:
    """offset не может быть отрицательным."""
    offset = _coerce_int(value, 0)
    return offset if offset > 0 else 0


async def _settings_row(user_id: int) -> dict:
    """Настройки пользователя; при ошибке — пустой dict (сработают дефолты из config)."""
    try:
        row = await users_repo.get_settings(user_id)
    except Exception:  # noqa: BLE001 — статистика не должна падать из-за настроек
        logger.warning("Не удалось получить настройки пользователя %s", user_id, exc_info=True)
        return {}
    return row or {}


def _setting_value(row: dict, key: str, fallback: int) -> int:
    """Значение настройки пользователя с запасным вариантом из backend.config.settings."""
    value = row.get(key) if row else None
    if value is None:
        value = fallback
    return _coerce_int(value, _coerce_int(fallback, 0))


def _resolve_frequent_threshold(row: dict, threshold: Any) -> int:
    """Порог «часто прослушиваемых»: аргумент → настройки пользователя → config."""
    if threshold is None:
        value = _setting_value(row, "frequent_threshold", settings.frequent_threshold)
    else:
        value = _coerce_int(threshold, _setting_value(row, "frequent_threshold", settings.frequent_threshold))
    return value if value >= 1 else 1


def _resolve_rare_bounds(row: dict, min_count: Any, max_count: Any) -> tuple[int, int]:
    """Границы «редко прослушиваемых»: аргументы → настройки пользователя → config."""
    default_min = _setting_value(row, "rare_min", settings.rare_min)
    default_max = _setting_value(row, "rare_max", settings.rare_max)

    low = default_min if min_count is None else _coerce_int(min_count, default_min)
    high = default_max if max_count is None else _coerce_int(max_count, default_max)

    if low < 0:
        low = 0
    if high < low:
        logger.warning(
            "Верхняя граница раздела «редко» (%s) меньше нижней (%s), выравниваю", high, low
        )
        high = low
    return low, high


async def _fetch_section(sql: str, params: tuple[Any, ...]) -> list[dict]:
    """Единая точка выполнения запросов раздела."""
    rows = await db.fetch_all(sql, params)
    return rows or []


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #


async def recent(user_id: int, limit: int = DEFAULT_RECENT_LIMIT, offset: int = 0) -> list[dict]:
    """Недавно добавленные треки."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_RECENT_LIMIT)
    off = _normalize_offset(offset)
    sql = tracks_repo.track_query(
        where=_AUDIO_WHERE,
        order=ORDER_RECENT,
        limit_clause=_LIMIT_CLAUSE,
    )
    return await _fetch_section(sql, (uid, _AUDIO_TYPE, lim, off))


async def unplayed(user_id: int, limit: int = DEFAULT_UNPLAYED_LIMIT, offset: int = 0) -> list[dict]:
    """Треки, которые ни разу не проигрывались (play_count = 0)."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_UNPLAYED_LIMIT)
    off = _normalize_offset(offset)
    sql = tracks_repo.track_query(
        where=f"{_AUDIO_WHERE} AND t.play_count = 0",
        order=ORDER_UNPLAYED,
        limit_clause=_LIMIT_CLAUSE,
    )
    return await _fetch_section(sql, (uid, _AUDIO_TYPE, lim, off))


async def frequent(
    user_id: int,
    threshold: int | None = None,
    limit: int = DEFAULT_FREQUENT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    """Часто прослушиваемые: play_count >= порог (по умолчанию — из настроек пользователя)."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_FREQUENT_LIMIT)
    off = _normalize_offset(offset)
    row = await _settings_row(uid) if threshold is None else {}
    value = _resolve_frequent_threshold(row, threshold)
    sql = tracks_repo.track_query(
        where=f"{_AUDIO_WHERE} AND t.play_count >= ?",
        order=ORDER_BY_PLAYS,
        limit_clause=_LIMIT_CLAUSE,
    )
    return await _fetch_section(sql, (uid, _AUDIO_TYPE, value, lim, off))


async def rare(
    user_id: int,
    min_count: int | None = None,
    max_count: int | None = None,
    limit: int = DEFAULT_RARE_LIMIT,
    offset: int = 0,
) -> list[dict]:
    """Редко прослушиваемые: play_count в диапазоне [min_count, max_count]."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_RARE_LIMIT)
    off = _normalize_offset(offset)
    row = await _settings_row(uid) if (min_count is None or max_count is None) else {}
    low, high = _resolve_rare_bounds(row, min_count, max_count)
    sql = tracks_repo.track_query(
        where=f"{_AUDIO_WHERE} AND t.play_count BETWEEN ? AND ?",
        order=ORDER_BY_PLAYS,
        limit_clause=_LIMIT_CLAUSE,
    )
    return await _fetch_section(sql, (uid, _AUDIO_TYPE, low, high, lim, off))


async def top(user_id: int, limit: int = DEFAULT_TOP_LIMIT, offset: int = 0) -> list[dict]:
    """Самые часто прослушиваемые: play_count > 0, по убыванию числа прослушиваний."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_TOP_LIMIT)
    off = _normalize_offset(offset)
    sql = tracks_repo.track_query(
        where=f"{_AUDIO_WHERE} AND t.play_count > 0",
        order=ORDER_BY_PLAYS,
        limit_clause=_LIMIT_CLAUSE,
    )
    return await _fetch_section(sql, (uid, _AUDIO_TYPE, lim, off))


async def section(
    user_id: int,
    key: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Диспетчер по ключу раздела. Неизвестный ключ — ValidationError."""
    section_key = (key or "").strip().lower()
    if section_key not in SECTION_KEYS:
        raise ValidationError(f"Неизвестный раздел статистики: «{key}»")

    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, SECTION_DEFAULT_LIMITS[section_key])
    off = _normalize_offset(offset)

    if section_key == "top":
        return await top(uid, limit=lim, offset=off)
    if section_key == "recent":
        return await recent(uid, limit=lim, offset=off)
    if section_key == "unplayed":
        return await unplayed(uid, limit=lim, offset=off)
    if section_key == "frequent":
        return await frequent(uid, limit=lim, offset=off)
    return await rare(uid, limit=lim, offset=off)


# --------------------------------------------------------------------------- #
# Счётчики и сводка
# --------------------------------------------------------------------------- #

_COUNTS_TRACKS_SQL = f"""
    SELECT
        COUNT(*) AS total,
        COALESCE(SUM(t.play_count), 0) AS total_plays,
        COALESCE(SUM(CASE WHEN t.play_count > 0 THEN 1 ELSE 0 END), 0) AS top,
        COALESCE(SUM(CASE WHEN t.play_count = 0 THEN 1 ELSE 0 END), 0) AS unplayed,
        COALESCE(SUM(CASE WHEN t.play_count >= ? THEN 1 ELSE 0 END), 0) AS frequent,
        COALESCE(SUM(CASE WHEN t.play_count BETWEEN ? AND ? THEN 1 ELSE 0 END), 0) AS rare
    FROM tracks t
    WHERE t.user_id = ? {_AUDIO_WHERE}
"""

_COUNTS_RELATED_SQL = """
    SELECT
        (SELECT COUNT(*) FROM favourites f WHERE f.user_id = ?) AS favourites,
        (SELECT COUNT(*) FROM folders   fo WHERE fo.user_id = ?) AS folders,
        (SELECT COUNT(*) FROM artists   a  WHERE a.user_id = ?)  AS artists
"""


async def counts(user_id: int) -> dict:
    """Счётчики для всех разделов и библиотеки в целом (два запроса)."""
    uid = _normalize_user_id(user_id)
    row = await _settings_row(uid)
    threshold = _resolve_frequent_threshold(row, None)
    rare_min, rare_max = _resolve_rare_bounds(row, None, None)

    tracks_row = (
        await db.fetch_one(_COUNTS_TRACKS_SQL, (threshold, rare_min, rare_max, uid, _AUDIO_TYPE))
        or {}
    )
    related_row = await db.fetch_one(_COUNTS_RELATED_SQL, (uid, uid, uid)) or {}

    total = _coerce_int(tracks_row.get("total"), 0)
    result = {
        "total": total,
        "total_plays": _coerce_int(tracks_row.get("total_plays"), 0),
        "top": _coerce_int(tracks_row.get("top"), 0),
        # «Недавно добавленные» — весь список треков, поэтому счётчик равен общему числу.
        "recent": total,
        "unplayed": _coerce_int(tracks_row.get("unplayed"), 0),
        "frequent": _coerce_int(tracks_row.get("frequent"), 0),
        "rare": _coerce_int(tracks_row.get("rare"), 0),
        "favourites": _coerce_int(related_row.get("favourites"), 0),
        "folders": _coerce_int(related_row.get("folders"), 0),
        "artists": _coerce_int(related_row.get("artists"), 0),
    }
    logger.debug("Статистика пользователя %s: %s", uid, result)
    return result


async def overview(user_id: int, limit: int = DEFAULT_TOP_LIMIT) -> dict:
    """Сводка: счётчики + пять разделов с превью треков (порядок из SECTION_KEYS)."""
    uid = _normalize_user_id(user_id)
    lim = _normalize_limit(limit, DEFAULT_TOP_LIMIT)

    stats_counts = await counts(uid)
    row = await _settings_row(uid)
    threshold = _resolve_frequent_threshold(row, None)
    rare_min, rare_max = _resolve_rare_bounds(row, None, None)

    sections: list[dict] = []
    for key in SECTION_KEYS:
        if key == "top":
            items = await top(uid, limit=lim, offset=0)
        elif key == "recent":
            items = await recent(uid, limit=lim, offset=0)
        elif key == "unplayed":
            items = await unplayed(uid, limit=lim, offset=0)
        elif key == "frequent":
            items = await frequent(uid, threshold=threshold, limit=lim, offset=0)
        else:
            items = await rare(uid, min_count=rare_min, max_count=rare_max, limit=lim, offset=0)

        sections.append(
            {
                "key": key,
                "title": SECTION_TITLES[key],
                "count": _coerce_int(stats_counts.get(key), 0),
                "items": items,
            }
        )

    return {"counts": stats_counts, "sections": sections}
