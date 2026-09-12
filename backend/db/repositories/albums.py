"""Репозиторий альбомов («групп»): создание, список, треки альбома."""

from __future__ import annotations

import logging
from typing import Any

from backend.db.database import db
from backend.db.repositories import tracks as tracks_repo
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

# Колонки альбома вместе с именем исполнителя и количеством треков.
ALBUM_COLUMNS = """
        al.id, al.user_id, al.title, al.normalized_title, al.artist_id,
        al.year, al.created_at,
        ar.name AS artist_name,
        COUNT(t.id) AS track_count
"""

ALBUM_FROM = """
    FROM albums al
    LEFT JOIN artists ar ON ar.id = al.artist_id AND ar.user_id = al.user_id
    LEFT JOIN tracks  t  ON t.album_id = al.id AND t.user_id = al.user_id
"""


def _album_query(where: str = "") -> str:
    """Собирает SELECT по альбомам с artist_name и track_count."""
    return (
        f"SELECT {ALBUM_COLUMNS}"
        f"{ALBUM_FROM}"
        f"    WHERE al.user_id = ? {where}\n"
        f"    GROUP BY al.id"
    )


def _to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Приводит строку БД к публичному dict альбома."""
    if row is None:
        return None
    data = dict(row)
    data["track_count"] = int(data.get("track_count") or 0)
    return data


def _to_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Приводит список строк БД к списку публичных dict."""
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _to_dict(row)
        if item is not None:
            result.append(item)
    return result


async def _fetch_by_key(
    user_id: int,
    normalized_title: str,
    artist_id: int | None,
) -> dict[str, Any] | None:
    """Ищет альбом по ключу (user_id, normalized_title, artist_id).

    Используется оператор IS, чтобы корректно сравнивать NULL в artist_id.
    """
    row = await db.fetch_one(
        _album_query("AND al.normalized_title = ? AND al.artist_id IS ?"),
        (user_id, normalized_title, artist_id),
    )
    return _to_dict(row)


async def ensure_album(
    user_id: int,
    title: str,
    artist_id: int | None = None,
    year: int | None = None,
) -> dict[str, Any] | None:
    """Возвращает альбом, создавая его при необходимости (идемпотентно).

    Пустое название -> None. Уникальность — по (user_id, normalized_title,
    artist_id). Если у существующего альбома год не заполнен, а он передан —
    год дописывается.
    """
    normalized = normalize_name(title)
    if not normalized:
        logger.debug("Пустое название альбома для пользователя %s — пропускаем", user_id)
        return None

    display_title = (title or "").strip() or normalized

    existing = await _fetch_by_key(user_id, normalized, artist_id)
    if existing is None:
        await db.execute(
            """
            INSERT INTO albums (user_id, title, normalized_title, artist_id, year)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, normalized_title, artist_id) DO NOTHING
            """,
            (user_id, display_title, normalized, artist_id, year),
        )
        existing = await _fetch_by_key(user_id, normalized, artist_id)
        if existing is None:
            logger.error(
                "Не удалось создать альбом «%s» для пользователя %s",
                display_title,
                user_id,
            )
            return None
        logger.info(
            "Создан альбом «%s» (id=%s) для пользователя %s",
            display_title,
            existing["id"],
            user_id,
        )
        return existing

    if year is not None and existing.get("year") is None:
        await db.execute(
            "UPDATE albums SET year = ? WHERE id = ? AND user_id = ?",
            (year, existing["id"], user_id),
        )
        existing["year"] = year
        logger.debug("У альбома id=%s проставлен год %s", existing["id"], year)

    return existing


async def get_album(user_id: int, album_id: int) -> dict[str, Any] | None:
    """Возвращает альбом по id (с artist_name и track_count) или None."""
    row = await db.fetch_one(
        _album_query("AND al.id = ?"),
        (user_id, album_id),
    )
    return _to_dict(row)


async def list_albums(user_id: int) -> list[dict[str, Any]]:
    """Список альбомов пользователя с исполнителем и количеством треков.

    Сортировка алфавитная, выполняется в Python (корректная кириллица).
    """
    rows = await db.fetch_all(_album_query(), (user_id,))
    albums = _to_dicts(rows)
    albums.sort(key=lambda item: (item.get("title") or "").casefold())
    return albums


async def album_tracks(
    user_id: int,
    album_id: int,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Треки альбома в порядке добавления (постранично)."""
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    sql = tracks_repo.track_query(
        where="AND t.album_id = ?",
        order="t.created_at ASC, t.id ASC",
        limit_clause="LIMIT ? OFFSET ?",
    )
    rows = await db.fetch_all(sql, (user_id, album_id, limit, offset))
    return [dict(row) for row in rows]
