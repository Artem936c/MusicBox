"""Репозиторий избранного: добавление, удаление, переключение и выборка треков."""

from __future__ import annotations

import logging

from backend.db.database import db
from backend.db.repositories import tracks as tracks_repo

logger = logging.getLogger(__name__)


def _normalize_limits(limit: int, offset: int) -> tuple[int, int]:
    """Приводит limit/offset к безопасным неотрицательным значениям."""
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        safe_offset = int(offset)
    except (TypeError, ValueError):
        safe_offset = 0
    return max(safe_limit, 0), max(safe_offset, 0)


async def _track_belongs(user_id: int, track_id: int) -> bool:
    """Проверяет, что трек существует и принадлежит пользователю."""
    row = await db.fetch_one(
        "SELECT 1 AS ok FROM tracks WHERE id = ? AND user_id = ?",
        (track_id, user_id),
    )
    return row is not None


async def add(user_id: int, track_id: int) -> bool:
    """Добавляет трек в избранное (идемпотентно).

    True — трек в избранном после вызова; False — трек не найден у пользователя.
    """
    if not await _track_belongs(user_id, track_id):
        logger.debug(
            "Трек %s не найден у пользователя %s, в избранное не добавлен",
            track_id,
            user_id,
        )
        return False

    await db.execute(
        "INSERT OR IGNORE INTO favourites (user_id, track_id) VALUES (?, ?)",
        (user_id, track_id),
    )
    return True


async def remove(user_id: int, track_id: int) -> bool:
    """Убирает трек из избранного. True, если запись действительно была удалена."""
    rowcount = await db.execute(
        "DELETE FROM favourites WHERE user_id = ? AND track_id = ?",
        (user_id, track_id),
    )
    return rowcount > 0


async def is_favourite(user_id: int, track_id: int) -> bool:
    """Проверяет, находится ли трек в избранном пользователя."""
    row = await db.fetch_one(
        "SELECT 1 AS ok FROM favourites WHERE user_id = ? AND track_id = ?",
        (user_id, track_id),
    )
    return row is not None


async def toggle(user_id: int, track_id: int) -> bool:
    """Переключает признак избранного. Возвращает НОВОЕ состояние."""
    if await is_favourite(user_id, track_id):
        await remove(user_id, track_id)
        return False

    added = await add(user_id, track_id)
    if not added:
        return False
    return await is_favourite(user_id, track_id)


async def list_favourites(user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    """Избранные треки пользователя, новые сверху (по времени добавления)."""
    safe_limit, safe_offset = _normalize_limits(limit, offset)
    if safe_limit == 0:
        return []

    sql = f"""
        SELECT {tracks_repo.TRACK_COLUMNS}
        {tracks_repo.TRACK_FROM}
        WHERE t.user_id = ? AND fav.id IS NOT NULL
        ORDER BY fav.added_at DESC, fav.id DESC
        LIMIT ? OFFSET ?
    """
    return await db.fetch_all(sql, (user_id, safe_limit, safe_offset))


async def count(user_id: int) -> int:
    """Количество треков в избранном пользователя."""
    value = await db.fetch_val(
        "SELECT COUNT(*) FROM favourites WHERE user_id = ?",
        (user_id,),
        default=0,
    )
    return int(value or 0)
