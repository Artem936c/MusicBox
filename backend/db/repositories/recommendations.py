"""Запросы для рекомендаций (раздел 2 контракта V2): модуль ТОЛЬКО читает данные.

Здесь собрана кросс-пользовательская агрегация, на которой строится
``backend/services/recommendations.py``:

* :func:`played_artist_names` — что пользователь уже слушал (исключается из выдачи);
* :func:`user_genres` — жанровый профиль пользователя;
* :func:`co_listener_artists` — коллаборативная фильтрация: исполнители тех
  пользователей, у которых есть общие с нами прослушанные исполнители;
* :func:`artists_by_genre` — кандидаты по жанрам, без привязки к пользователю.

Особенности реализации:

* Исполнители хранятся ПО ПОЛЬЗОВАТЕЛЯМ, поэтому «один и тот же исполнитель» —
  это совпадение ``artists.normalized_name``; вся агрегация идёт по этому полю,
  идентификаторы пользователей наружу не отдаются.
* Прослушивания исполнителя считаются по ФАКТУ — ``SUM(tracks.play_count)`` по
  существующим трекам (ровно так же, как их пересчитывает
  ``artists.recalc_total_plays``). Денормализованная колонка ``total_plays``
  (миграция 004) в кандидатах НЕ участвует: она не уменьшается при удалении
  трека, поэтому исполнитель без треков иначе рекомендовался бы вечно со
  «замороженной» статистикой. Кандидат без единого трека отбрасывается.
  ``total_plays`` используется только как страховка «пользователь это уже
  слушал» — чтобы отставшая денормализация не «обнуляла» историю.
* Связь «трек ↔ исполнитель» берётся из ``track_artists`` (миграция 003)
  ОБЪЕДИНЁННОЙ с денормализованным ``tracks.artist_id`` — так учитываются и
  дополнительные исполнители, и записи, для которых связь ещё не проставлена.
* Все запросы параметризованы (пользовательский ввод в текст SQL не попадает)
  и ограничены лимитами: см. константы ниже.

Кандидаты (:func:`co_listener_artists`, :func:`artists_by_genre`) возвращаются
в едином формате :data:`CANDIDATE_FIELDS`::

    {
        "normalized_name": str,      # ключ сравнения исполнителей между пользователями
        "name": str,                 # отображаемое имя (у самого слушаемого варианта)
        "total_plays": int,          # суммарные прослушивания по всем пользователям
        "listeners": int,            # сколько пользователей слушали исполнителя
        "co_listeners": int,         # сколько «соседей» слушают его (0 для жанровых)
        "genres": list[str],         # нормализованные жанры, по убыванию прослушиваний
        "genre": str | None,         # главный (для жанровых — совпавший) жанр
        "local_artist_id": int | None,  # id в библиотеке пользователя, если он там есть
        "source": str,               # SOURCE_CO_LISTENER | SOURCE_GENRE
    }

Ошибки базы не пробрасываются: рекомендации — необязательная функция, поэтому
при сбое запроса пишется traceback в лог, а наружу уходит пустой результат
(сервис честно отрабатывает деградацию и сообщает о нехватке кандидатов).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final, Iterable, Iterator, Sequence

import aiosqlite

from backend.db.database import db
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

#: Источник кандидата — коллаборативная фильтрация.
SOURCE_CO_LISTENER: Final[str] = "co_listener"
#: Источник кандидата — совпадение по жанру.
SOURCE_GENRE: Final[str] = "genre"

#: Набор ключей, который гарантируют обе функции-поставщика кандидатов.
CANDIDATE_FIELDS: Final[tuple[str, ...]] = (
    "normalized_name",
    "name",
    "total_plays",
    "listeners",
    "co_listeners",
    "genres",
    "genre",
    "local_artist_id",
    "source",
)

#: Потолок для «прослушанных» исполнителей (самые слушаемые — первыми).
MAX_PLAYED_ARTISTS: Final[int] = 2000
#: Сколько жанров пользователя отдаём по умолчанию.
DEFAULT_GENRE_LIMIT: Final[int] = 50
#: Лимит кандидатов по умолчанию и жёсткий потолок.
DEFAULT_CANDIDATE_LIMIT: Final[int] = 200
MAX_CANDIDATE_LIMIT: Final[int] = 1000
#: Потолок строк агрегатов, читаемых одним запросом (защита от разрастания базы).
MAX_SCAN_ROWS: Final[int] = 5000
#: SQLite ограничивает число параметров запроса — режем IN-списки на части.
_MAX_IN_PARAMS: Final[int] = 300

#: Ошибки драйвера БД, которые гасим ради деградации рекомендаций.
_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (aiosqlite.Error, sqlite3.Error)


# --------------------------------------------------------------------------
# Общие SQL-фрагменты (CTE)
# --------------------------------------------------------------------------

# Связь «исполнитель -> трек»: явные связи V2 плюс денормализованный artist_id.
_ARTIST_TRACKS_CTE: Final[str] = """
    artist_tracks AS (
        SELECT ta.artist_id AS artist_id, ta.track_id AS track_id
          FROM track_artists ta
         UNION
        SELECT t.artist_id AS artist_id, t.id AS track_id
          FROM tracks t
         WHERE t.artist_id IS NOT NULL
    )
"""

# Прослушивания каждой строки artists (строка = исполнитель одного пользователя).
# ``plays`` — ФАКТ (сумма по существующим трекам, ровно как считает
# ``artists.recalc_total_plays``), ``track_count`` — сколько треков сейчас есть,
# ``history_plays`` — факт, подстрахованный денормализованным ``total_plays``
# (нужен только для «что пользователь уже слушал», см. _PLAYED_ARTISTS_SQL).
_ARTIST_PLAYS_CTE: Final[str] = """
    artist_plays AS (
        SELECT a.id              AS artist_id,
               a.user_id         AS user_id,
               a.normalized_name AS normalized_name,
               a.name            AS name,
               COALESCE(SUM(t.play_count), 0) AS plays,
               COUNT(t.id)       AS track_count,
               MAX(a.total_plays, COALESCE(SUM(t.play_count), 0)) AS history_plays
          FROM artists a
          LEFT JOIN artist_tracks x ON x.artist_id = a.id
          LEFT JOIN tracks t ON t.id = x.track_id AND t.user_id = a.user_id
         GROUP BY a.id
    )
"""

# Сводка по исполнителю в масштабе всей базы.
_ARTIST_GLOBAL_CTE: Final[str] = """
    artist_global AS (
        SELECT normalized_name,
               COALESCE(SUM(plays), 0) AS total_plays,
               COUNT(DISTINCT CASE WHEN plays > 0 THEN user_id END) AS listeners
          FROM artist_plays
         GROUP BY normalized_name
    )
"""

# Отображаемое имя: берём вариант написания у самого слушающего пользователя.
_ARTIST_DISPLAY_CTE: Final[str] = """
    artist_display AS (
        SELECT normalized_name, name
          FROM (
              SELECT normalized_name,
                     name,
                     ROW_NUMBER() OVER (
                         PARTITION BY normalized_name
                         ORDER BY plays DESC, name ASC
                     ) AS rn
                FROM artist_plays
          )
         WHERE rn = 1
    )
"""

# Жанры исполнителя по всей базе (регистр приводится уже в Python).
_ARTIST_GENRES_CTE: Final[str] = """
    artist_genres AS (
        SELECT ap.normalized_name AS normalized_name,
               TRIM(t.genre)      AS genre,
               COALESCE(SUM(t.play_count), 0) AS genre_plays,
               COUNT(t.id)        AS genre_tracks
          FROM artist_plays ap
          JOIN artist_tracks x ON x.artist_id = ap.artist_id
          JOIN tracks t ON t.id = x.track_id AND t.user_id = ap.user_id
         WHERE t.genre IS NOT NULL AND TRIM(t.genre) <> ''
         GROUP BY ap.normalized_name, TRIM(t.genre)
    )
"""

_WITH_PLAYS: Final[str] = f"WITH {_ARTIST_TRACKS_CTE},\n{_ARTIST_PLAYS_CTE}"
_WITH_GENRES: Final[str] = (
    f"WITH {_ARTIST_TRACKS_CTE},\n{_ARTIST_PLAYS_CTE},\n{_ARTIST_GENRES_CTE}"
)

# --------------------------------------------------------------------------
# Готовые запросы
# --------------------------------------------------------------------------

_PLAYED_ARTISTS_SQL: Final[str] = f"""
{_WITH_PLAYS}
SELECT normalized_name, history_plays AS plays
  FROM artist_plays
 WHERE user_id = ? AND history_plays > 0
 ORDER BY history_plays DESC, normalized_name ASC
 LIMIT ?
"""

_USER_GENRES_SQL: Final[str] = """
SELECT TRIM(t.genre) AS genre,
       COALESCE(SUM(t.play_count), 0) AS plays,
       COUNT(t.id) AS tracks
  FROM tracks t
 WHERE t.user_id = ?
   AND t.genre IS NOT NULL
   AND TRIM(t.genre) <> ''
 GROUP BY TRIM(t.genre)
 ORDER BY plays DESC, tracks DESC, genre ASC
 LIMIT ?
"""

# Соседи — другие пользователи с общими прослушанными исполнителями;
# кандидаты — то, что слушают соседи, за вычетом уже прослушанного нами.
_CO_LISTENER_SQL: Final[str] = f"""
WITH {_ARTIST_TRACKS_CTE},
{_ARTIST_PLAYS_CTE},
{_ARTIST_GLOBAL_CTE},
{_ARTIST_DISPLAY_CTE},
    mine AS (
        SELECT normalized_name
          FROM artist_plays
         WHERE user_id = ? AND history_plays > 0
    ),
    neighbours AS (
        SELECT DISTINCT ap.user_id AS user_id
          FROM artist_plays ap
          JOIN mine m ON m.normalized_name = ap.normalized_name
         WHERE ap.user_id <> ? AND ap.plays > 0
    ),
    candidates AS (
        SELECT ap.normalized_name AS normalized_name,
               ap.user_id         AS user_id,
               ap.plays           AS plays
          FROM artist_plays ap
          JOIN neighbours n ON n.user_id = ap.user_id
         WHERE ap.plays > 0
           AND ap.track_count > 0
           AND ap.normalized_name NOT IN (SELECT normalized_name FROM mine)
    )
SELECT c.normalized_name                      AS normalized_name,
       d.name                                 AS name,
       COALESCE(g.total_plays, 0)             AS total_plays,
       COALESCE(g.listeners, 0)               AS listeners,
       COUNT(DISTINCT c.user_id)              AS co_listeners,
       COALESCE(SUM(c.plays), 0)              AS co_listener_plays,
       (SELECT ma.id
          FROM artists ma
         WHERE ma.user_id = ? AND ma.normalized_name = c.normalized_name) AS local_artist_id
  FROM candidates c
  LEFT JOIN artist_global  g ON g.normalized_name = c.normalized_name
  LEFT JOIN artist_display d ON d.normalized_name = c.normalized_name
 GROUP BY c.normalized_name
 ORDER BY co_listeners DESC,
          co_listener_plays DESC,
          total_plays DESC,
          c.normalized_name ASC
 LIMIT ?
"""

# Жанровые кандидаты: агрегаты «исполнитель + жанр» по всей базе.
# Совпадение жанров считается в Python — LOWER() в SQLite не знает кириллицы.
_GENRE_CANDIDATES_SQL: Final[str] = f"""
WITH {_ARTIST_TRACKS_CTE},
{_ARTIST_PLAYS_CTE},
{_ARTIST_GLOBAL_CTE},
{_ARTIST_DISPLAY_CTE},
{_ARTIST_GENRES_CTE}
SELECT s.normalized_name          AS normalized_name,
       d.name                     AS name,
       s.genre                    AS genre,
       s.genre_plays              AS genre_plays,
       s.genre_tracks             AS genre_tracks,
       COALESCE(g.total_plays, 0) AS total_plays,
       COALESCE(g.listeners, 0)   AS listeners
  FROM artist_genres s
  LEFT JOIN artist_global  g ON g.normalized_name = s.normalized_name
  LEFT JOIN artist_display d ON d.normalized_name = s.normalized_name
 ORDER BY total_plays DESC, s.genre_plays DESC, s.normalized_name ASC
 LIMIT ?
"""


def _genres_for_names_sql(count: int) -> str:
    """Собирает запрос жанров для перечня исполнителей (плейсхолдеры по числу имён)."""
    placeholders = ", ".join("?" for _ in range(count))
    return f"""
{_WITH_GENRES}
SELECT normalized_name, genre, genre_plays, genre_tracks
  FROM artist_genres
 WHERE normalized_name IN ({placeholders})
 ORDER BY genre_plays DESC, genre_tracks DESC, genre ASC
 LIMIT ?
"""


def _local_artists_sql(count: int) -> str:
    """Собирает запрос «есть ли эти исполнители в библиотеке пользователя»."""
    placeholders = ", ".join("?" for _ in range(count))
    return f"""
SELECT id, normalized_name
  FROM artists
 WHERE user_id = ?
   AND normalized_name IN ({placeholders})
"""


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


def _clamp(value: Any, default: int, maximum: int, minimum: int = 1) -> int:
    """Приводит лимит к целому числу в допустимых границах."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.debug("Некорректный лимит %r — используем значение по умолчанию", value)
        return default
    if number < minimum:
        return minimum
    return min(number, maximum)


def _chunks(items: Sequence[str], size: int = _MAX_IN_PARAMS) -> Iterator[Sequence[str]]:
    """Режет последовательность на куски, влезающие в IN-список SQLite."""
    step = max(int(size), 1)
    for start in range(0, len(items), step):
        yield items[start : start + step]


def _unique_normalized(values: Iterable[str | None]) -> list[str]:
    """Нормализует значения, убирает пустые и дубли, сохраняя исходный порядок."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalize_name(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _as_int(value: Any) -> int:
    """Безопасно приводит агрегат из БД к int (NULL -> 0)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _candidate(
    *,
    normalized_name: str,
    name: str | None,
    total_plays: Any,
    listeners: Any,
    co_listeners: Any,
    genres: Sequence[str],
    genre: str | None,
    local_artist_id: Any,
    source: str,
) -> dict[str, Any]:
    """Собирает кандидата в едином формате :data:`CANDIDATE_FIELDS`."""
    display = (name or "").strip() or normalized_name
    return {
        "normalized_name": normalized_name,
        "name": display,
        "total_plays": _as_int(total_plays),
        "listeners": _as_int(listeners),
        "co_listeners": _as_int(co_listeners),
        "genres": list(genres),
        "genre": genre,
        "local_artist_id": int(local_artist_id) if local_artist_id else None,
        "source": source,
    }


async def _genres_by_artist(names: Sequence[str]) -> dict[str, list[str]]:
    """Жанры (нормализованные, по убыванию прослушиваний) для перечня исполнителей."""
    if not names:
        return {}

    result: dict[str, list[str]] = {}
    for chunk in _chunks(list(names)):
        params: list[Any] = [*chunk, MAX_SCAN_ROWS]
        rows = await db.fetch_all(_genres_for_names_sql(len(chunk)), params)
        for row in rows:
            key = row.get("normalized_name")
            genre = normalize_name(row.get("genre"))
            if not key or not genre:
                continue
            bucket = result.setdefault(key, [])
            if genre not in bucket:
                bucket.append(genre)
    return result


async def _local_artist_ids(user_id: int, names: Sequence[str]) -> dict[str, int]:
    """Сопоставление normalized_name -> id исполнителя в библиотеке пользователя."""
    if not names:
        return {}

    result: dict[str, int] = {}
    for chunk in _chunks(list(names)):
        params: list[Any] = [int(user_id), *chunk]
        rows = await db.fetch_all(_local_artists_sql(len(chunk)), params)
        for row in rows:
            key = row.get("normalized_name")
            artist_id = row.get("id")
            if key and artist_id:
                result[str(key)] = int(artist_id)
    return result


# --------------------------------------------------------------------------
# Публичные запросы
# --------------------------------------------------------------------------


async def played_artist_names(
    user_id: int,
    *,
    limit: int = MAX_PLAYED_ARTISTS,
) -> set[str]:
    """Нормализованные имена исполнителей, которых пользователь уже слушал.

    Прослушанным считается исполнитель, у которого есть прослушивания по трекам
    ЛИБО ненулевой ``artists.total_plays`` (колонка страхует историю: треки
    могли быть удалены, но рекомендовать уже слушанное всё равно не нужно).
    Результат — множество ключей для исключения из рекомендаций.
    Отдаётся не более ``limit`` самых слушаемых исполнителей.
    """
    count = _clamp(limit, MAX_PLAYED_ARTISTS, MAX_PLAYED_ARTISTS)
    try:
        rows = await db.fetch_all(_PLAYED_ARTISTS_SQL, (int(user_id), count))
    except _DB_ERRORS:
        logger.exception(
            "Не удалось получить прослушанных исполнителей пользователя %s", user_id
        )
        return set()

    names = {str(row["normalized_name"]) for row in rows if row.get("normalized_name")}
    logger.debug(
        "Пользователь %s слушал %d исполнител(я/ей)", user_id, len(names)
    )
    return names


async def user_genres(
    user_id: int,
    *,
    limit: int = DEFAULT_GENRE_LIMIT,
) -> dict[str, int]:
    """Жанровый профиль пользователя: нормализованный жанр -> число прослушиваний.

    Значение — сумма ``tracks.play_count`` по трекам жанра; жанры, которые есть
    в библиотеке, но ещё не слушались, остаются в конце со значением 0.
    Словарь упорядочен по убыванию прослушиваний, поэтому сервису достаточно
    взять первые N ключей. Разные написания одного жанра («Rock», «rock»)
    схлопываются в один ключ.
    """
    count = _clamp(limit, DEFAULT_GENRE_LIMIT, MAX_SCAN_ROWS)
    try:
        rows = await db.fetch_all(_USER_GENRES_SQL, (int(user_id), MAX_SCAN_ROWS))
    except _DB_ERRORS:
        logger.exception("Не удалось получить жанры пользователя %s", user_id)
        return {}

    merged: dict[str, list[int]] = {}
    for row in rows:
        genre = normalize_name(row.get("genre"))
        if not genre:
            continue
        stats = merged.setdefault(genre, [0, 0])
        stats[0] += _as_int(row.get("plays"))
        stats[1] += _as_int(row.get("tracks"))

    ordered = sorted(
        merged.items(),
        key=lambda item: (-item[1][0], -item[1][1], item[0]),
    )
    result = {genre: stats[0] for genre, stats in ordered[:count]}
    logger.debug("У пользователя %s найдено %d жанров", user_id, len(result))
    return result


async def co_listener_artists(
    user_id: int,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """Коллаборативная фильтрация: что слушают пользователи с общими вкусами.

    «Соседи» — другие пользователи, у которых есть хотя бы один общий с нами
    прослушанный исполнитель (сравнение по ``normalized_name``). Кандидаты —
    исполнители, которых слушают соседи, за вычетом всего, что пользователь
    слушал сам. Сортировка: число общих слушателей, затем их прослушивания,
    затем общая популярность.

    Возвращает список dict с ключами :data:`CANDIDATE_FIELDS`
    (``co_listeners`` — сколько соседей слушают исполнителя,
    ``local_artist_id`` — id, если исполнитель уже заведён в библиотеке).
    """
    count = _clamp(limit, DEFAULT_CANDIDATE_LIMIT, MAX_CANDIDATE_LIMIT)
    owner = int(user_id)
    try:
        rows = await db.fetch_all(_CO_LISTENER_SQL, (owner, owner, owner, count))
    except _DB_ERRORS:
        logger.exception(
            "Не удалось подобрать исполнителей по общим слушателям (пользователь %s)",
            user_id,
        )
        return []

    names = [
        str(row["normalized_name"]) for row in rows if row.get("normalized_name")
    ]
    try:
        genres = await _genres_by_artist(names)
    except _DB_ERRORS:
        logger.exception("Не удалось получить жанры кандидатов пользователя %s", user_id)
        genres = {}

    candidates: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("normalized_name")
        if not key:
            continue
        artist_genres = genres.get(str(key), [])
        candidates.append(
            _candidate(
                normalized_name=str(key),
                name=row.get("name"),
                total_plays=row.get("total_plays"),
                listeners=row.get("listeners"),
                co_listeners=row.get("co_listeners"),
                genres=artist_genres,
                genre=artist_genres[0] if artist_genres else None,
                local_artist_id=row.get("local_artist_id"),
                source=SOURCE_CO_LISTENER,
            )
        )

    logger.debug(
        "Для пользователя %s найдено %d кандидатов по общим слушателям",
        user_id,
        len(candidates),
    )
    return candidates


async def artists_by_genre(
    genres: Sequence[str],
    exclude: set[str],
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Кандидаты по жанрам: исполнители всей базы, чьи треки попадают в ``genres``.

    ``genres`` — жанры в порядке убывания важности (обычно топ пользователя из
    :func:`user_genres`); сравнение идёт по нормализованному написанию, поэтому
    «Rock» и «rock» — один жанр. ``exclude`` — нормализованные имена, которые
    нельзя предлагать (как правило, уже прослушанные исполнители).
    ``user_id`` (необязательный) заполняет ``local_artist_id`` — id исполнителя
    в библиотеке этого пользователя.

    Порядок: сначала совпадения по более важному жанру, затем по прослушиваниям
    внутри жанра и общей популярности. Возвращает не более ``limit`` кандидатов
    в формате :data:`CANDIDATE_FIELDS`.
    """
    wanted = _unique_normalized(genres)
    if not wanted:
        logger.debug("Список жанров пуст — жанровых кандидатов нет")
        return []

    count = _clamp(limit, DEFAULT_CANDIDATE_LIMIT, MAX_CANDIDATE_LIMIT)
    skip = {normalize_name(item) for item in (exclude or set())}
    skip.discard("")
    priority = {genre: index for index, genre in enumerate(wanted)}

    try:
        rows = await db.fetch_all(_GENRE_CANDIDATES_SQL, (MAX_SCAN_ROWS,))
    except _DB_ERRORS:
        logger.exception("Не удалось подобрать исполнителей по жанрам")
        return []

    # Собираем по исполнителю: все его жанры + лучший из запрошенных.
    collected: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("normalized_name")
        genre = normalize_name(row.get("genre"))
        if not key or not genre:
            continue
        name_key = str(key)
        if name_key in skip:
            continue

        item = collected.get(name_key)
        if item is None:
            item = {
                "name": row.get("name"),
                "total_plays": _as_int(row.get("total_plays")),
                "listeners": _as_int(row.get("listeners")),
                "genres": [],
                "genre": None,
                "genre_rank": len(priority),
                "genre_plays": 0,
            }
            collected[name_key] = item

        if genre not in item["genres"]:
            item["genres"].append(genre)

        rank = priority.get(genre)
        if rank is None:
            continue
        genre_plays = _as_int(row.get("genre_plays"))
        if rank < item["genre_rank"] or (
            rank == item["genre_rank"] and genre_plays > item["genre_plays"]
        ):
            item["genre_rank"] = rank
            item["genre_plays"] = genre_plays
            item["genre"] = genre

    matched = [
        (name_key, item)
        for name_key, item in collected.items()
        if item["genre"] is not None
    ]
    matched.sort(
        key=lambda pair: (
            pair[1]["genre_rank"],
            -pair[1]["genre_plays"],
            -pair[1]["total_plays"],
            pair[0],
        )
    )
    matched = matched[:count]

    local_ids: dict[str, int] = {}
    if user_id is not None and matched:
        try:
            local_ids = await _local_artist_ids(
                int(user_id), [name_key for name_key, _ in matched]
            )
        except _DB_ERRORS:
            logger.exception(
                "Не удалось сопоставить жанровых кандидатов с библиотекой пользователя %s",
                user_id,
            )

    candidates = [
        _candidate(
            normalized_name=name_key,
            name=item["name"],
            total_plays=item["total_plays"],
            listeners=item["listeners"],
            co_listeners=0,
            genres=item["genres"],
            genre=item["genre"],
            local_artist_id=local_ids.get(name_key),
            source=SOURCE_GENRE,
        )
        for name_key, item in matched
    ]

    logger.debug(
        "По жанрам %s найдено %d кандидатов", ", ".join(wanted[:5]), len(candidates)
    )
    return candidates


__all__ = [
    "CANDIDATE_FIELDS",
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_GENRE_LIMIT",
    "MAX_CANDIDATE_LIMIT",
    "MAX_PLAYED_ARTISTS",
    "MAX_SCAN_ROWS",
    "SOURCE_CO_LISTENER",
    "SOURCE_GENRE",
    "artists_by_genre",
    "co_listener_artists",
    "played_artist_names",
    "user_genres",
]
