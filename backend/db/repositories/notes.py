"""Репозиторий заметок со списками пунктов (раздел 1.4 и 2 контракта V2).

Заметка (``notes``) — заголовок и набор пунктов (``note_items``) с отметкой
«выполнено» и позицией. Позиции всегда идут подряд от 1 до N: при удалении
пункта нумерация восстанавливается, при изменении порядка — расставляется
заново в одной транзакции.

Все запросы фильтруются по ``user_id``: пользователь видит и меняет только свои
заметки и только их пункты.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Sequence

import aiosqlite

from backend.db.database import db
from backend.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

# Ограничения на пользовательский ввод.
MAX_TITLE_LENGTH: Final[int] = 200
MAX_ITEM_TEXT_LENGTH: Final[int] = 1000
MAX_ITEMS_PER_NOTE: Final[int] = 500

# Заметка вместе с агрегатами по пунктам (всего / выполнено).
_NOTE_SELECT: Final[str] = """
    SELECT n.id, n.user_id, n.title, n.created_at, n.updated_at,
           (SELECT COUNT(*)
              FROM note_items i
             WHERE i.note_id = n.id) AS items_total,
           (SELECT COUNT(*)
              FROM note_items i
             WHERE i.note_id = n.id AND i.is_done = 1) AS items_done
      FROM notes n
"""

_ITEM_COLUMNS: Final[str] = "id, note_id, text, is_done, position, created_at"

# Сообщения об ошибках (видны пользователю бота и Mini App).
NOTE_NOT_FOUND: Final[str] = "Заметка не найдена"
ITEM_NOT_FOUND: Final[str] = "Пункт заметки не найден"


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


def _clean_title(value: str | None) -> str:
    """Проверяет и нормализует заголовок заметки."""
    title = (value or "").strip()
    if not title:
        raise ValidationError("Название заметки не может быть пустым")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(
            f"Название заметки слишком длинное (максимум {MAX_TITLE_LENGTH} символов)"
        )
    return title


def _clean_item_text(value: str | None) -> str:
    """Проверяет и нормализует текст пункта заметки."""
    text = (value or "").strip()
    if not text:
        raise ValidationError("Текст пункта не может быть пустым")
    if len(text) > MAX_ITEM_TEXT_LENGTH:
        raise ValidationError(
            f"Текст пункта слишком длинный (максимум {MAX_ITEM_TEXT_LENGTH} символов)"
        )
    return text


def _note_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Приводит строку заметки к публичному dict с целыми агрегатами."""
    if row is None:
        return None
    data = dict(row)
    data["items_total"] = int(data.get("items_total") or 0)
    data["items_done"] = int(data.get("items_done") or 0)
    return data


def _item_to_dict(row: Any | None) -> dict[str, Any] | None:
    """Приводит строку пункта к публичному dict: is_done — bool, position — int."""
    if row is None:
        return None
    data = dict(row)
    data["is_done"] = bool(data.get("is_done"))
    data["position"] = int(data.get("position") or 0)
    return data


def _normalize_ids(raw_ids: Sequence[int] | None) -> list[int] | None:
    """Приводит идентификаторы к int; None — если попались мусор или дубли."""
    normalized: list[int] = []
    for raw_id in raw_ids or ():
        try:
            normalized.append(int(raw_id))
        except (TypeError, ValueError):
            logger.warning("Некорректный идентификатор пункта заметки %r", raw_id)
            return None
    if len(set(normalized)) != len(normalized):
        logger.warning("Дубликаты в новом порядке пунктов заметки")
        return None
    return normalized


async def _note_belongs(
    conn: aiosqlite.Connection, user_id: int, note_id: int
) -> bool:
    """Проверяет, что заметка принадлежит пользователю (внутри транзакции)."""
    cursor = await conn.execute(
        "SELECT 1 FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _fetch_item(
    conn: aiosqlite.Connection, note_id: int, item_id: int
) -> dict[str, Any] | None:
    """Читает пункт заметки внутри транзакции."""
    cursor = await conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM note_items WHERE id = ? AND note_id = ?",
        (item_id, note_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _item_to_dict(row)


async def _renumber_positions(conn: aiosqlite.Connection, note_id: int) -> None:
    """Перенумеровывает пункты заметки как 1..N без пропусков."""
    cursor = await conn.execute(
        "SELECT id FROM note_items WHERE note_id = ? ORDER BY position, id",
        (note_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        return
    await conn.executemany(
        "UPDATE note_items SET position = ? WHERE id = ?",
        [(index, int(row["id"])) for index, row in enumerate(rows, start=1)],
    )


async def _touch_note(
    conn: aiosqlite.Connection, user_id: int, note_id: int
) -> None:
    """Обновляет updated_at заметки после изменения её содержимого."""
    await conn.execute(
        "UPDATE notes SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (note_id, user_id),
    )


async def _note_items(user_id: int, note_id: int) -> list[dict[str, Any]]:
    """Пункты заметки пользователя в порядке позиций."""
    rows = await db.fetch_all(
        f"""
        SELECT {_ITEM_COLUMNS}
          FROM note_items
         WHERE note_id = (SELECT id FROM notes WHERE id = ? AND user_id = ?)
         ORDER BY position, id
        """,
        (note_id, user_id),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _item_to_dict(row)
        if item is not None:
            items.append(item)
    return items


async def _fetch_note(user_id: int, note_id: int) -> dict[str, Any] | None:
    """Читает заметку с агрегатами (без пунктов)."""
    row = await db.fetch_one(
        f"{_NOTE_SELECT} WHERE n.id = ? AND n.user_id = ?", (note_id, user_id)
    )
    return _note_to_dict(row)


# --------------------------------------------------------------------------
# Заметки
# --------------------------------------------------------------------------


async def create_note(user_id: int, title: str) -> dict[str, Any]:
    """Создаёт заметку. Пустой или слишком длинный заголовок -> ValidationError."""
    clean_title = _clean_title(title)

    note_id = await db.execute_insert(
        "INSERT INTO notes (user_id, title) VALUES (?, ?)", (user_id, clean_title)
    )
    note = await _fetch_note(user_id, note_id)
    if note is None:
        logger.error(
            "Не удалось прочитать только что созданную заметку id=%s пользователя %s",
            note_id,
            user_id,
        )
        raise ValidationError("Не удалось создать заметку, попробуйте ещё раз")

    logger.info(
        "Пользователь %s: создана заметка «%s» (id=%s)", user_id, clean_title, note_id
    )
    note["items"] = []
    return note


async def list_notes(user_id: int) -> list[dict[str, Any]]:
    """Заметки пользователя: сначала недавно изменённые, с числом пунктов."""
    rows = await db.fetch_all(
        f"{_NOTE_SELECT} WHERE n.user_id = ? ORDER BY n.updated_at DESC, n.id DESC",
        (user_id,),
    )
    notes: list[dict[str, Any]] = []
    for row in rows:
        note = _note_to_dict(row)
        if note is not None:
            notes.append(note)
    return notes


async def get_note(user_id: int, note_id: int) -> dict[str, Any] | None:
    """Заметка вместе с пунктами в порядке позиций (None — заметки нет)."""
    note = await _fetch_note(user_id, note_id)
    if note is None:
        logger.debug("Заметка id=%s пользователя %s не найдена", note_id, user_id)
        return None
    note["items"] = await _note_items(user_id, int(note["id"]))
    return note


async def rename_note(
    user_id: int, note_id: int, title: str
) -> dict[str, Any] | None:
    """Переименовывает заметку. None — заметка не найдена."""
    clean_title = _clean_title(title)

    updated = await db.execute(
        "UPDATE notes SET title = ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND user_id = ?",
        (clean_title, note_id, user_id),
    )
    if not updated:
        logger.warning(
            "Заметка id=%s пользователя %s не найдена — переименование отменено",
            note_id,
            user_id,
        )
        return None

    logger.info(
        "Пользователь %s: заметка id=%s переименована в «%s»",
        user_id,
        note_id,
        clean_title,
    )
    return await get_note(user_id, note_id)


async def delete_note(user_id: int, note_id: int) -> bool:
    """Удаляет заметку вместе с пунктами (ON DELETE CASCADE)."""
    deleted = await db.execute(
        "DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
    )
    if not deleted:
        logger.debug(
            "Заметка id=%s пользователя %s не найдена — удалять нечего", note_id, user_id
        )
        return False
    logger.info("Пользователь %s: заметка id=%s удалена", user_id, note_id)
    return True


# --------------------------------------------------------------------------
# Пункты заметки
# --------------------------------------------------------------------------


async def add_item(user_id: int, note_id: int, text: str) -> dict[str, Any]:
    """Добавляет пункт в конец списка. Заметки нет -> NotFoundError."""
    clean_text = _clean_item_text(text)

    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — пункт не добавлен",
                note_id,
                user_id,
            )
            raise NotFoundError(NOTE_NOT_FOUND)

        cursor = await conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(MAX(position), 0) AS last_position"
            "  FROM note_items WHERE note_id = ?",
            (note_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row["total"]) if row is not None else 0
        position = (int(row["last_position"]) if row is not None else 0) + 1
        if total >= MAX_ITEMS_PER_NOTE:
            raise ValidationError(
                f"В заметке не может быть больше {MAX_ITEMS_PER_NOTE} пунктов"
            )

        cursor = await conn.execute(
            "INSERT INTO note_items (note_id, text, position) VALUES (?, ?, ?)",
            (note_id, clean_text, position),
        )
        item_id = int(cursor.lastrowid or 0)
        await cursor.close()

        await _touch_note(conn, user_id, note_id)
        item = await _fetch_item(conn, note_id, item_id)

    if item is None:
        logger.error(
            "Не удалось прочитать только что созданный пункт id=%s заметки id=%s",
            item_id,
            note_id,
        )
        raise ValidationError("Не удалось добавить пункт, попробуйте ещё раз")

    logger.info(
        "Пользователь %s: в заметку id=%s добавлен пункт id=%s", user_id, note_id, item_id
    )
    return item


async def set_item_done(
    user_id: int, note_id: int, item_id: int, is_done: bool
) -> dict[str, Any] | None:
    """Отмечает пункт выполненным/невыполненным. None — заметки или пункта нет."""
    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — отметка не изменена",
                note_id,
                user_id,
            )
            return None
        if await _fetch_item(conn, note_id, item_id) is None:
            logger.warning(
                "Пункт id=%s не найден в заметке id=%s пользователя %s",
                item_id,
                note_id,
                user_id,
            )
            return None

        await conn.execute(
            "UPDATE note_items SET is_done = ? WHERE id = ? AND note_id = ?",
            (1 if is_done else 0, item_id, note_id),
        )
        await _touch_note(conn, user_id, note_id)
        item = await _fetch_item(conn, note_id, item_id)

    logger.debug(
        "Пользователь %s: пункт id=%s заметки id=%s отмечен как %s",
        user_id,
        item_id,
        note_id,
        "выполненный" if is_done else "невыполненный",
    )
    return item


async def toggle_item(
    user_id: int, note_id: int, item_id: int
) -> dict[str, Any] | None:
    """Переключает отметку «выполнено» у пункта заметки."""
    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — переключение отменено",
                note_id,
                user_id,
            )
            return None
        current = await _fetch_item(conn, note_id, item_id)
        if current is None:
            logger.warning(
                "Пункт id=%s не найден в заметке id=%s пользователя %s",
                item_id,
                note_id,
                user_id,
            )
            return None

        await conn.execute(
            "UPDATE note_items SET is_done = ? WHERE id = ? AND note_id = ?",
            (0 if current["is_done"] else 1, item_id, note_id),
        )
        await _touch_note(conn, user_id, note_id)
        item = await _fetch_item(conn, note_id, item_id)

    return item


async def update_item(
    user_id: int, note_id: int, item_id: int, text: str
) -> dict[str, Any] | None:
    """Меняет текст пункта. None — заметки или пункта нет."""
    clean_text = _clean_item_text(text)

    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — пункт не изменён",
                note_id,
                user_id,
            )
            return None
        if await _fetch_item(conn, note_id, item_id) is None:
            logger.warning(
                "Пункт id=%s не найден в заметке id=%s пользователя %s",
                item_id,
                note_id,
                user_id,
            )
            return None

        await conn.execute(
            "UPDATE note_items SET text = ? WHERE id = ? AND note_id = ?",
            (clean_text, item_id, note_id),
        )
        await _touch_note(conn, user_id, note_id)
        item = await _fetch_item(conn, note_id, item_id)

    logger.info(
        "Пользователь %s: изменён текст пункта id=%s заметки id=%s",
        user_id,
        item_id,
        note_id,
    )
    return item


async def delete_item(user_id: int, note_id: int, item_id: int) -> bool:
    """Удаляет пункт и перенумеровывает оставшиеся (1..N без пропусков)."""
    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — пункт не удалён",
                note_id,
                user_id,
            )
            return False

        cursor = await conn.execute(
            "DELETE FROM note_items WHERE id = ? AND note_id = ?", (item_id, note_id)
        )
        deleted = int(cursor.rowcount or 0)
        await cursor.close()
        if not deleted:
            logger.debug(
                "Пункт id=%s не найден в заметке id=%s — удалять нечего", item_id, note_id
            )
            return False

        await _renumber_positions(conn, note_id)
        await _touch_note(conn, user_id, note_id)

    logger.info(
        "Пользователь %s: из заметки id=%s удалён пункт id=%s", user_id, note_id, item_id
    )
    return True


async def reorder_items(
    user_id: int, note_id: int, ordered_item_ids: Sequence[int]
) -> bool:
    """Задаёт новый порядок пунктов заметки.

    Множество ``ordered_item_ids`` должно точно совпадать с текущим составом
    заметки, иначе возвращается False и ничего не меняется. Вся сверка и
    расстановка позиций 1..N выполняются в ОДНОЙ транзакции.
    """
    normalized = _normalize_ids(ordered_item_ids)
    if normalized is None:
        return False

    async with db.transaction() as conn:
        if not await _note_belongs(conn, user_id, note_id):
            logger.warning(
                "Заметка id=%s пользователя %s не найдена — порядок не изменён",
                note_id,
                user_id,
            )
            return False

        cursor = await conn.execute(
            "SELECT id FROM note_items WHERE note_id = ? ORDER BY position, id",
            (note_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        current = [int(row["id"]) for row in rows]

        if set(current) != set(normalized):
            logger.warning(
                "Состав заметки %s не совпадает с переданным порядком:"
                " в заметке %s пунктов, передано %s",
                note_id,
                len(current),
                len(normalized),
            )
            return False

        if not normalized:
            return True

        await conn.executemany(
            "UPDATE note_items SET position = ? WHERE id = ? AND note_id = ?",
            [
                (index, item_id, note_id)
                for index, item_id in enumerate(normalized, start=1)
            ],
        )
        await _touch_note(conn, user_id, note_id)

    logger.info(
        "Пользователь %s: изменён порядок пунктов заметки id=%s (%s шт.)",
        user_id,
        note_id,
        len(normalized),
    )
    return True


__all__ = [
    "MAX_ITEMS_PER_NOTE",
    "MAX_ITEM_TEXT_LENGTH",
    "MAX_TITLE_LENGTH",
    "add_item",
    "create_note",
    "delete_item",
    "delete_note",
    "get_note",
    "list_notes",
    "rename_note",
    "reorder_items",
    "set_item_done",
    "toggle_item",
    "update_item",
]
