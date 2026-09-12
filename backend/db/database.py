"""Асинхронный доступ к SQLite: единое подключение, блокировка записи, миграции."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

import aiosqlite

from backend.config import settings
from backend.db.migrations import Migration, MigrationError, run_migrations

logger = logging.getLogger(__name__)

#: Файл со схемой лежит рядом с этим модулем.
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    """Обёртка над одним подключением aiosqlite с общей блокировкой доступа."""

    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        #: Задача, которая сейчас держит _write_lock (для вложенных вызовов).
        self._lock_owner: asyncio.Task[Any] | None = None
        self._path: str | None = None

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------

    @property
    def connection(self) -> aiosqlite.Connection:
        """Активное подключение. RuntimeError, если база ещё не подключена."""
        if self._conn is None:
            raise RuntimeError(
                "База данных не подключена. Вызовите init_db() до обращения к репозиториям."
            )
        return self._conn

    @property
    def is_connected(self) -> bool:
        """Подключена ли база."""
        return self._conn is not None

    @property
    def path(self) -> str | None:
        """Путь к текущему файлу базы данных."""
        return self._path

    async def connect(self, path: str | None = None) -> None:
        """Открыть подключение, создав каталог, и настроить PRAGMA."""
        if self._conn is not None:
            logger.debug("Подключение к базе уже открыто: %s", self._path)
            return

        db_path = Path(path or settings.database_path).expanduser()
        parent = db_path.parent
        if str(parent) not in ("", "."):
            await asyncio.to_thread(parent.mkdir, parents=True, exist_ok=True)

        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.commit()
        except Exception:
            await conn.close()
            raise

        self._conn = conn
        self._path = str(db_path)
        logger.info("База данных подключена: %s", self._path)

    async def close(self) -> None:
        """Закрыть подключение (безопасно вызывать повторно)."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - закрытие не должно ломать остановку приложения
            logger.exception("Ошибка при закрытии базы данных")
        else:
            logger.info("База данных закрыта: %s", self._path)

    # ------------------------------------------------------------------
    # Доступ к соединению
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[aiosqlite.Connection]:
        """Дать общее соединение под блокировкой (и на запись, и на чтение).

        Подключение одно на всё приложение, поэтому чтения нужно сериализовать
        с записью: иначе SELECT видит данные ещё не закоммиченной (и, возможно,
        откатываемой) транзакции другого пользователя. asyncio.Lock не
        реентрантна, поэтому вложенный вызов из задачи, которая уже держит
        блокировку, выполняется без повторного захвата — иначе был бы дедлок.
        """
        current = asyncio.current_task()
        if self._lock_owner is not None and self._lock_owner is current:
            yield self.connection
            return
        async with self._write_lock:
            self._lock_owner = current
            try:
                yield self.connection
            finally:
                self._lock_owner = None

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    async def executescript(self, script: str) -> None:
        """Выполнить SQL-скрипт (несколько инструкций) целиком."""
        async with self._locked() as conn:
            await conn.executescript(script)
            await conn.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполнить запись и закоммитить. Возвращает число затронутых строк."""
        async with self._locked() as conn:
            cursor = await conn.execute(sql, tuple(params))
            try:
                rowcount = cursor.rowcount
            finally:
                await cursor.close()
            await conn.commit()
            return int(rowcount if rowcount is not None else 0)

    async def execute_insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполнить INSERT и вернуть id вставленной строки (0, если его нет)."""
        async with self._locked() as conn:
            cursor = await conn.execute(sql, tuple(params))
            try:
                last_id = cursor.lastrowid
            finally:
                await cursor.close()
            await conn.commit()
            return int(last_id or 0)

    async def execute_many(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> None:
        """Выполнить один запрос для набора параметров и закоммитить."""
        rows = [tuple(item) for item in params_seq]
        if not rows:
            return
        async with self._locked() as conn:
            await conn.executemany(sql, rows)
            await conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция под блокировкой записи: commit при успехе, rollback при ошибке."""
        async with self._locked() as conn:
            try:
                yield conn
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 - откат не должен подменять исходную ошибку
                    logger.exception("Не удалось откатить транзакцию")
                raise
            else:
                await conn.commit()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        """Вернуть первую строку как dict или None."""
        async with self._locked() as conn:
            cursor = await conn.execute(sql, tuple(params))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return row_to_dict(row)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        """Вернуть все строки как список dict."""
        async with self._locked() as conn:
            cursor = await conn.execute(sql, tuple(params))
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return rows_to_dicts(rows)

    async def fetch_val(
        self, sql: str, params: Sequence[Any] = (), default: Any = None
    ) -> Any:
        """Вернуть первое значение первой строки; если строки нет или значение NULL — default."""
        async with self._locked() as conn:
            cursor = await conn.execute(sql, tuple(params))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if row is None:
            return default
        value = row[0]
        return default if value is None else value


#: Синглтон, который импортируют все репозитории и сервисы.
db = Database()


def row_to_dict(row: aiosqlite.Row | sqlite3.Row | Mapping[str, Any] | None) -> dict | None:
    """Преобразовать строку выборки в обычный dict (None остаётся None)."""
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(
    rows: Iterable[aiosqlite.Row | sqlite3.Row | Mapping[str, Any]] | None,
) -> list[dict]:
    """Преобразовать набор строк выборки в список dict."""
    if not rows:
        return []
    return [dict(row) for row in rows]


async def init_db(path: str | None = None) -> None:
    """Подключиться к базе, применить миграции и schema.sql (идемпотентно)."""
    await db.connect(path)
    try:
        script = await _read_schema()
        # Миграции идут ПЕРВЫМИ: на пустой базе 001_baseline создаёт схему V1,
        # дальше она достраивается до V2. После этого schema.sql (тоже V1,
        # целиком на CREATE ... IF NOT EXISTS) ничего не ломает и служит
        # страховкой на случай частично созданной базы.
        await apply_migrations()
        await db.executescript(script)
        # Повторный проход — на случай таблиц, созданных только что.
        await apply_migrations()
    except BaseException:
        # Иначе подключение останется открытым и рабочий поток aiosqlite
        # не даст процессу корректно завершиться.
        await db.close()
        raise
    logger.info("Схема базы данных актуальна")


async def apply_migrations() -> None:
    """Применить миграции схемы из ``backend/db/migrations`` (идемпотентно).

    Файлы миграций применяются по возрастанию числового префикса, каждая — в
    своей транзакции, с записью в таблицу ``schema_migrations``. Повторный
    вызов не делает ничего. Базу V1, созданную до появления миграций, раннер
    распознаёт по таблице ``tracks`` и помечает базовую миграцию применённой.
    """
    applied: list[Migration] = []
    try:
        # Соединение одно на всё приложение: берём его под блокировкой записи,
        # чтобы миграции не пересекались с запросами репозиториев.
        async with db.transaction() as conn:
            applied = await run_migrations(conn)
    except (aiosqlite.Error, sqlite3.Error) as exc:
        raise MigrationError(f"Не удалось применить миграции схемы: {exc}") from exc
    if applied:
        logger.info(
            "Применены миграции схемы (%d): %s",
            len(applied),
            ", ".join(item.name for item in applied),
        )
    else:
        logger.debug("Новых миграций схемы нет")


async def shutdown_db() -> None:
    """Закрыть подключение к базе при остановке приложения."""
    await db.close()


async def _read_schema() -> str:
    """Прочитать schema.sql рядом с модулем (в отдельном потоке)."""
    if not SCHEMA_PATH.exists():
        raise RuntimeError(
            f"Не найден файл схемы базы данных: {SCHEMA_PATH}. "
            "Проверьте целостность установки backend."
        )
    return await asyncio.to_thread(SCHEMA_PATH.read_text, encoding="utf-8")


__all__ = [
    "Database",
    "SCHEMA_PATH",
    "apply_migrations",
    "db",
    "init_db",
    "row_to_dict",
    "rows_to_dicts",
    "shutdown_db",
]
