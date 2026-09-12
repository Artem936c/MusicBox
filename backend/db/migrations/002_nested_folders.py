"""002: вложенные папки и разделы — перестройка таблицы ``folders``.

В V1 у ``folders`` стоит inline-ограничение ``UNIQUE (user_id, normalized_name)``,
которое в SQLite нельзя снять через ALTER TABLE. Поэтому таблица пересоздаётся по
рецепту раздела 1.1 контракта V2:

1. ``PRAGMA foreign_keys=OFF`` ДО начала (иначе ``DROP TABLE folders`` обнулил бы
   ``tracks.folder_id`` и ``artists.folder_id`` каскадом);
2. одна транзакция: ``folders_new`` → перелив данных → ``DROP TABLE folders`` →
   ``ALTER TABLE folders_new RENAME TO folders`` → индексы;
3. ``PRAGMA foreign_key_check`` ВНУТРИ этой транзакции, до ``COMMIT``: если
   перестройка добавила нарушений — миграция падает, а изменения откатываются
   и не остаются зафиксированными на диске;
4. ``PRAGMA foreign_keys=ON`` после выхода из транзакции.

Проверка сравнивается со снимком, снятым ДО перестройки: ``PRAGMA
foreign_key_check`` сканирует ВСЮ базу, поэтому нарушения, существовавшие до
миграции (например, в ``play_history``), только пишутся в журнал предупреждением
и не выдаются за последствия перестройки папок.

ID папок СОХРАНЯЮТСЯ: строки переливаются вместе с ``id``, на них ссылаются
``tracks.folder_id`` и ``artists.folder_id``. Внешние ключи в SQLite связаны с
ИМЕНЕМ таблицы, поэтому после RENAME они указывают на новую таблицу; при
выключенных внешних ключах RENAME не переписывает REFERENCES в других таблицах,
а самоссылку внутри переименованной таблицы приводит к новому имени.

Ограничение: ``UNIQUE (user_id, section, parent_folder_id, normalized_name)`` не
ловит дубли КОРНЕВЫХ папок, потому что в SQLite ``NULL != NULL``. Уникальность
корневых папок обеспечивает репозиторий (проверка перед вставкой).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import aiosqlite

from backend.db.migrations import (
    MigrationError,
    fetch_scalar,
    foreign_key_problems,
    safe_rollback,
    set_foreign_keys,
    table_columns,
)

logger = logging.getLogger(__name__)

#: Миграция сама управляет транзакцией: PRAGMA foreign_keys внутри неё не работает.
ATOMIC = False

#: Раздел по умолчанию для всех папок V1.
DEFAULT_SECTION = "music"

#: Колонка, по наличию которой видно, что перестройка уже выполнена.
MARKER_COLUMN = "parent_folder_id"

#: Перелив данных: id, user_id и время создания сохраняются как есть.
COPY_FOLDERS_SQL = """
INSERT INTO folders_new (id, user_id, name, normalized_name, parent_folder_id,
                         section, is_artist_folder, created_at)
    SELECT id, user_id, name, normalized_name, NULL, 'music', is_artist_folder,
           COALESCE(created_at, CURRENT_TIMESTAMP)
    FROM folders
"""

#: Индексы новой таблицы (раздел 1.1 контракта V2).
INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_folders_user    ON folders(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_folders_parent  ON folders(user_id, parent_folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_folders_section ON folders(user_id, section)",
)


def create_table_sql(table: str) -> str:
    """DDL новой таблицы папок (самоссылка указывает на неё же)."""
    return f"""
CREATE TABLE {table} (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    normalized_name  TEXT NOT NULL,
    parent_folder_id INTEGER REFERENCES {table}(id) ON DELETE CASCADE,
    section          TEXT NOT NULL DEFAULT '{DEFAULT_SECTION}',
    is_artist_folder INTEGER NOT NULL DEFAULT 0,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, section, parent_folder_id, normalized_name)
)
"""


async def foreign_key_snapshot(conn: aiosqlite.Connection) -> set[tuple[Any, ...]]:
    """Снимок нарушений внешних ключей как множество сравнимых кортежей."""
    return {tuple(row) for row in await foreign_key_problems(conn)}


def format_problems(problems: Iterable[tuple[Any, ...]]) -> str:
    """Первые пять нарушений одной строкой (для журнала и сообщения об ошибке)."""
    return "; ".join(str(item) for item in sorted(problems, key=repr)[:5])


async def migrate(conn: aiosqlite.Connection) -> None:
    """Перестроить ``folders``, добавив ``parent_folder_id`` и ``section``."""
    if conn.in_transaction:
        await conn.commit()

    columns = await table_columns(conn, "folders")
    if not columns:
        # Такой базы быть не должно (folders создаёт 001), но пустую базу
        # достраиваем сразу в новом формате, а не падаем.
        logger.warning("Таблица folders отсутствует — создаём её сразу в формате V2")
        await conn.execute(create_table_sql("folders"))
        for statement in INDEX_SQL:
            await conn.execute(statement)
        await conn.commit()
        return

    if MARKER_COLUMN in columns:
        logger.info("Таблица folders уже перестроена — миграция пропущена")
        return

    before = int(await fetch_scalar(conn, "SELECT COUNT(*) FROM folders", default=0))
    max_id_before = int(await fetch_scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM folders", default=0))

    # PRAGMA foreign_key_check сканирует всю базу, поэтому «до» и «после» сравниваются:
    # падать надо только на НОВЫХ нарушениях, а не на тех, что уже лежали в базе.
    problems_before = await foreign_key_snapshot(conn)
    if problems_before:
        logger.warning(
            "До перестройки папок в базе уже есть нарушения внешних ключей (%d шт.): %s. "
            "Миграция их не исправляет и из-за них не падает",
            len(problems_before),
            format_problems(problems_before),
        )

    await set_foreign_keys(conn, False)
    try:
        await conn.execute("BEGIN")
        try:
            await conn.execute(create_table_sql("folders_new"))
            await conn.execute(COPY_FOLDERS_SQL)
            await conn.execute("DROP TABLE folders")
            await conn.execute("ALTER TABLE folders_new RENAME TO folders")
            for statement in INDEX_SQL:
                await conn.execute(statement)

            after = int(await fetch_scalar(conn, "SELECT COUNT(*) FROM folders", default=0))
            max_id_after = int(
                await fetch_scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM folders", default=0)
            )
            if after != before or max_id_after != max_id_before:
                raise MigrationError(
                    "Перестройка папок изменила данные: было "
                    f"{before} шт. (max id {max_id_before}), стало {after} шт. "
                    f"(max id {max_id_after}). Изменения отменены."
                )

            # Проверка ДО COMMIT: только так её результат может что-то отменить.
            new_problems = await foreign_key_snapshot(conn) - problems_before
            if new_problems:
                raise MigrationError(
                    "Перестройка папок нарушила внешние ключи "
                    f"({len(new_problems)} шт.): {format_problems(new_problems)}. "
                    "Изменения отменены."
                )
            await conn.execute("COMMIT")
        except BaseException:
            await safe_rollback(conn)
            raise
    finally:
        # Внешние ключи возвращаем всегда, даже если перестройка не удалась.
        await set_foreign_keys(conn, True)

    logger.info("Папки перестроены: %d шт., id сохранены (max id %d)", before, max_id_before)
