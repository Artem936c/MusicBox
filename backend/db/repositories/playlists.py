"""Репозиторий плейлистов: CRUD, состав и порядок треков.

Все функции фильтруют данные по ``user_id``: пользователь видит и меняет только
свои плейлисты и только свои треки.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable, Sequence

import aiosqlite

from backend.db.database import db
from backend.db.repositories import tracks as tracks_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

# Ограничения на пользовательский ввод
MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 500

# Плейлист + агрегаты (число треков и суммарная длительность) через подзапросы
_PLAYLIST_SELECT = """
    SELECT p.id, p.user_id, p.name, p.description, p.created_at, p.updated_at,
           (SELECT COUNT(*)
              FROM playlist_tracks pt
             WHERE pt.playlist_id = p.id) AS track_count,
           (SELECT COALESCE(SUM(tr.duration), 0)
              FROM playlist_tracks pt
              JOIN tracks tr ON tr.id = pt.track_id
             WHERE pt.playlist_id = p.id) AS total_duration
      FROM playlists p
"""


def _clean_name(value: str | None) -> str:
    """Проверяет и нормализует название плейлиста."""
    name = (value or "").strip()
    if not name:
        raise ValidationError("Название плейлиста не может быть пустым")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"Название плейлиста слишком длинное (максимум {MAX_NAME_LENGTH} символов)"
        )
    return name


def _clean_description(value: str | None) -> str | None:
    """Нормализует описание плейлиста: пустая строка сохраняется как NULL."""
    if value is None:
        return None
    description = value.strip()
    if not description:
        return None
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError(
            f"Описание плейлиста слишком длинное (максимум {MAX_DESCRIPTION_LENGTH} символов)"
        )
    return description


async def _playlist_belongs(
    conn: aiosqlite.Connection, user_id: int, playlist_id: int
) -> bool:
    """Проверяет, что плейлист принадлежит пользователю (внутри транзакции)."""
    cursor = await conn.execute(
        "SELECT 1 FROM playlists WHERE id = ? AND user_id = ?",
        (playlist_id, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _track_belongs(
    conn: aiosqlite.Connection, user_id: int, track_id: int
) -> bool:
    """Проверяет, что трек принадлежит пользователю (внутри транзакции)."""
    cursor = await conn.execute(
        "SELECT 1 FROM tracks WHERE id = ? AND user_id = ?",
        (track_id, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _ordered_track_ids(conn: aiosqlite.Connection, playlist_id: int) -> list[int]:
    """Текущий состав плейлиста в порядке позиций."""
    cursor = await conn.execute(
        "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, id",
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [int(row["track_id"]) for row in rows]


async def _renumber_positions(conn: aiosqlite.Connection, playlist_id: int) -> None:
    """Перенумеровывает позиции треков плейлиста как 1..N без пропусков."""
    cursor = await conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, id",
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        return
    await conn.executemany(
        "UPDATE playlist_tracks SET position = ? WHERE id = ?",
        [(index, row["id"]) for index, row in enumerate(rows, start=1)],
    )


async def _apply_order(
    conn: aiosqlite.Connection, playlist_id: int, ordered_track_ids: Sequence[int]
) -> None:
    """Расставляет позиции 1..N по переданному порядку идентификаторов треков."""
    await conn.executemany(
        "UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?",
        [
            (index, playlist_id, int(track_id))
            for index, track_id in enumerate(ordered_track_ids, start=1)
        ],
    )


async def _touch_playlist(
    conn: aiosqlite.Connection, user_id: int, playlist_id: int
) -> None:
    """Обновляет updated_at плейлиста после изменения его состава."""
    await conn.execute(
        "UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (playlist_id, user_id),
    )


async def create_playlist(
    user_id: int, name: str, description: str | None = None
) -> dict:
    """Создаёт плейлист. Бросает ValidationError при пустом или занятом названии."""
    clean_name = _clean_name(name)
    clean_description = _clean_description(description)

    existing = await db.fetch_one(
        "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
        (user_id, clean_name),
    )
    if existing is not None:
        raise ValidationError(f"Плейлист «{clean_name}» уже существует")

    try:
        playlist_id = await db.execute_insert(
            "INSERT INTO playlists (user_id, name, description) VALUES (?, ?, ?)",
            (user_id, clean_name, clean_description),
        )
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "Конфликт названия при создании плейлиста %r (пользователь %s): %s",
            clean_name,
            user_id,
            exc,
        )
        raise ValidationError(f"Плейлист «{clean_name}» уже существует") from exc

    playlist = await get_playlist(user_id, playlist_id)
    if playlist is None:
        logger.error(
            "Плейлист %s создан, но не прочитан обратно (пользователь %s)",
            playlist_id,
            user_id,
        )
        raise ValidationError("Не удалось создать плейлист, попробуйте ещё раз")

    logger.info(
        "Создан плейлист %s (%r) пользователя %s", playlist_id, clean_name, user_id
    )
    return playlist


async def get_playlist(user_id: int, playlist_id: int) -> dict | None:
    """Возвращает плейлист с полями track_count и total_duration."""
    return await db.fetch_one(
        f"{_PLAYLIST_SELECT} WHERE p.id = ? AND p.user_id = ?",
        (playlist_id, user_id),
    )


async def list_playlists(user_id: int) -> list[dict]:
    """Все плейлисты пользователя; сортировка в Python по названию."""
    rows = await db.fetch_all(f"{_PLAYLIST_SELECT} WHERE p.user_id = ?", (user_id,))
    return sorted(rows, key=lambda item: (item.get("name") or "").casefold())


async def update_playlist(
    user_id: int,
    playlist_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Обновляет название/описание плейлиста и его updated_at.

    ``None`` в аргументе означает «не менять». Чтобы очистить описание,
    передайте пустую строку.
    """
    current = await get_playlist(user_id, playlist_id)
    if current is None:
        return None

    updates: list[str] = []
    params: list[Any] = []

    if name is not None:
        clean_name = _clean_name(name)
        if clean_name != current.get("name"):
            conflict = await db.fetch_one(
                "SELECT id FROM playlists WHERE user_id = ? AND name = ? AND id <> ?",
                (user_id, clean_name, playlist_id),
            )
            if conflict is not None:
                raise ValidationError(f"Плейлист «{clean_name}» уже существует")
        updates.append("name = ?")
        params.append(clean_name)

    if description is not None:
        updates.append("description = ?")
        params.append(_clean_description(description))

    if not updates:
        return current

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.extend([playlist_id, user_id])

    try:
        await db.execute(
            f"UPDATE playlists SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "Конфликт названия при обновлении плейлиста %s (пользователь %s): %s",
            playlist_id,
            user_id,
            exc,
        )
        raise ValidationError("Плейлист с таким названием уже существует") from exc

    return await get_playlist(user_id, playlist_id)


async def delete_playlist(user_id: int, playlist_id: int) -> bool:
    """Удаляет плейлист (состав удаляется каскадно). True, если что-то удалено."""
    rowcount = await db.execute(
        "DELETE FROM playlists WHERE id = ? AND user_id = ?",
        (playlist_id, user_id),
    )
    if rowcount:
        logger.info("Удалён плейлист %s пользователя %s", playlist_id, user_id)
    return rowcount > 0


async def playlist_tracks(user_id: int, playlist_id: int) -> list[dict]:
    """Треки плейлиста по возрастанию позиции; каждый dict дополнен полем position."""
    sql = f"""
        SELECT {tracks_repo.TRACK_COLUMNS},
               pt.position AS position
        {tracks_repo.TRACK_FROM}
        JOIN playlist_tracks pt ON pt.track_id = t.id
        JOIN playlists p ON p.id = pt.playlist_id
        WHERE t.user_id = ? AND pt.playlist_id = ? AND p.user_id = ?
        ORDER BY pt.position, pt.id
    """
    return await db.fetch_all(sql, (user_id, playlist_id, user_id))


async def add_track(user_id: int, playlist_id: int, track_id: int) -> bool:
    """Добавляет трек в конец плейлиста.

    Возвращает False, если плейлист или трек не принадлежат пользователю либо
    трек уже есть в плейлисте.
    """
    async with db.transaction() as conn:
        if not await _playlist_belongs(conn, user_id, playlist_id):
            logger.debug("Плейлист %s не найден у пользователя %s", playlist_id, user_id)
            return False
        if not await _track_belongs(conn, user_id, track_id):
            logger.debug("Трек %s не найден у пользователя %s", track_id, user_id)
            return False

        cursor = await conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        )
        already = await cursor.fetchone()
        await cursor.close()
        if already is not None:
            return False

        cursor = await conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position"
            " FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        position = int(row["next_position"]) if row is not None else 1

        try:
            await conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position)"
                " VALUES (?, ?, ?)",
                (playlist_id, track_id, position),
            )
        except sqlite3.IntegrityError as exc:
            logger.warning("Трек %s уже в плейлисте %s: %s", track_id, playlist_id, exc)
            return False

        await _touch_playlist(conn, user_id, playlist_id)
        return True


async def add_tracks(user_id: int, playlist_id: int, track_ids: Iterable[int]) -> int:
    """Добавляет несколько треков в конец плейлиста. Возвращает число добавленных."""
    requested: list[int] = []
    seen: set[int] = set()
    for raw_id in track_ids or ():
        try:
            track_id = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("Пропущен некорректный идентификатор трека %r", raw_id)
            continue
        if track_id in seen:
            continue
        seen.add(track_id)
        requested.append(track_id)

    if not requested:
        return 0

    async with db.transaction() as conn:
        if not await _playlist_belongs(conn, user_id, playlist_id):
            logger.debug("Плейлист %s не найден у пользователя %s", playlist_id, user_id)
            return 0

        # IN-список режем на части: SQLite ограничивает число параметров запроса.
        owned: set[int] = set()
        for chunk in tracks_repo._chunks(requested):
            placeholders = tracks_repo._placeholders(len(chunk))
            cursor = await conn.execute(
                f"SELECT id FROM tracks WHERE user_id = ? AND id IN ({placeholders})",
                (user_id, *chunk),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            owned.update(int(row["id"]) for row in rows)

        cursor = await conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        already = {int(row["track_id"]) for row in rows}

        cursor = await conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_position"
            " FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        position = int(row["max_position"]) if row is not None else 0

        to_insert: list[tuple[int, int, int]] = []
        for track_id in requested:
            if track_id not in owned or track_id in already:
                continue
            position += 1
            to_insert.append((playlist_id, track_id, position))

        if not to_insert:
            return 0

        await conn.executemany(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position)"
            " VALUES (?, ?, ?)",
            to_insert,
        )
        await _touch_playlist(conn, user_id, playlist_id)
        logger.info(
            "В плейлист %s добавлено треков: %s (пользователь %s)",
            playlist_id,
            len(to_insert),
            user_id,
        )
        return len(to_insert)


async def remove_track(user_id: int, playlist_id: int, track_id: int) -> bool:
    """Удаляет трек из плейлиста и перенумеровывает оставшиеся позиции."""
    async with db.transaction() as conn:
        if not await _playlist_belongs(conn, user_id, playlist_id):
            return False

        cursor = await conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        )
        removed = cursor.rowcount
        await cursor.close()
        if not removed:
            return False

        await _renumber_positions(conn, playlist_id)
        await _touch_playlist(conn, user_id, playlist_id)
        return True


async def reorder(
    user_id: int, playlist_id: int, ordered_track_ids: Sequence[int]
) -> bool:
    """Задаёт новый порядок треков плейлиста.

    Множество ``ordered_track_ids`` должно точно совпадать с текущим составом
    плейлиста, иначе возвращается False и ничего не меняется.
    """
    normalized: list[int] = []
    for raw_id in ordered_track_ids or ():
        try:
            normalized.append(int(raw_id))
        except (TypeError, ValueError):
            logger.warning(
                "Некорректный идентификатор трека %r при переупорядочивании плейлиста %s",
                raw_id,
                playlist_id,
            )
            return False

    if len(set(normalized)) != len(normalized):
        logger.warning(
            "Дубликаты в новом порядке треков плейлиста %s (пользователь %s)",
            playlist_id,
            user_id,
        )
        return False

    async with db.transaction() as conn:
        if not await _playlist_belongs(conn, user_id, playlist_id):
            return False

        current = await _ordered_track_ids(conn, playlist_id)
        if set(current) != set(normalized):
            logger.warning(
                "Состав плейлиста %s не совпадает с переданным порядком:"
                " в плейлисте %s треков, передано %s",
                playlist_id,
                len(current),
                len(normalized),
            )
            return False

        if not normalized:
            return True

        await _apply_order(conn, playlist_id, normalized)
        await _touch_playlist(conn, user_id, playlist_id)
        return True


async def move_track_position(
    user_id: int, playlist_id: int, track_id: int, new_position: int
) -> bool:
    """Перемещает трек на позицию new_position (1..N) со сдвигом остальных."""
    try:
        target = int(new_position)
    except (TypeError, ValueError):
        logger.warning("Некорректная позиция %r для трека %s", new_position, track_id)
        return False

    async with db.transaction() as conn:
        if not await _playlist_belongs(conn, user_id, playlist_id):
            return False

        current = await _ordered_track_ids(conn, playlist_id)
        try:
            old_index = current.index(int(track_id))
        except ValueError:
            logger.debug("Трек %s отсутствует в плейлисте %s", track_id, playlist_id)
            return False

        new_index = max(0, min(target - 1, len(current) - 1))
        if new_index == old_index:
            # Порядок не меняется, но позиции всё равно нормализуем.
            await _renumber_positions(conn, playlist_id)
            return True

        moved = current.pop(old_index)
        current.insert(new_index, moved)

        await _apply_order(conn, playlist_id, current)
        await _touch_playlist(conn, user_id, playlist_id)
        return True
