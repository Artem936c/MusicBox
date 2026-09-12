"""Система миграций схемы MusicBox.

Файлы миграций лежат рядом с этим модулем и называются ``NNN_описание.sql``
или ``NNN_описание.py``, где ``NNN`` — числовой префикс (версия). Порядок
применения определяется ТОЛЬКО этим числом.

Поддерживаются два вида миграций:

* ``.sql`` — скрипт целиком выполняется через ``executescript``. Транзакцию
  открывает и закрывает раннер: скрипт оборачивается в ``BEGIN``/``COMMIT``
  вместе с записью в ``schema_migrations``, поэтому «применена, но не
  записана» невозможно. Инструкции ``ALTER TABLE ... ADD COLUMN`` перед
  выполнением проверяются через ``PRAGMA table_info`` и вырезаются, если
  колонка уже есть (иначе SQLite падает при повторе).
* ``.py`` — модуль с асинхронной функцией ``migrate(conn)``, принимающей
  ``aiosqlite.Connection``. По умолчанию раннер сам открывает транзакцию.
  Если модуль объявляет ``ATOMIC = False``, транзакциями (и PRAGMA, которые
  внутри транзакции не работают) управляет сама миграция, а раннер только
  записывает результат.

Учёт ведётся в таблице ``schema_migrations(version, name, applied_at)``.
Повторный запуск не делает ничего.

Существующая база V1 (созданная до появления миграций) распознаётся по
наличию таблицы ``tracks``: базовая миграция 001 помечается применённой без
выполнения, дальнейшие применяются обычным порядком.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from backend.errors import MusicBoxError

logger = logging.getLogger(__name__)

#: Каталог с файлами миграций (этот же пакет).
MIGRATIONS_DIR: Path = Path(__file__).parent

#: Версия базовой миграции, соответствующей схеме V1.
BASELINE_VERSION: int = 1

#: Таблица, по наличию которой распознаётся уже существующая база V1.
BASELINE_MARKER_TABLE: str = "tracks"

#: Таблица учёта применённых миграций.
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

#: Имя файла миграции: числовой префикс, подчёркивание, описание, расширение.
_FILE_RE = re.compile(r"^(?P<version>\d{1,6})_(?P<slug>[A-Za-z0-9_]+)\.(?P<kind>sql|py)$")

#: ALTER TABLE ... ADD COLUMN в .sql-миграции (проверяется на идемпотентность).
_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"ADD\s+(?:COLUMN\s+)?(?P<column>[A-Za-z_][A-Za-z0-9_]*)\b[^;]*;",
    re.IGNORECASE,
)

#: Файлы пакета, которые миграциями не являются.
_IGNORED_FILES = frozenset({"__init__.py"})


class MigrationError(MusicBoxError):
    """Не удалось применить миграцию схемы базы данных."""

    default_message = "Не удалось обновить схему базы данных. Проверьте журнал приложения."


@dataclass(frozen=True, slots=True)
class Migration:
    """Одна миграция: версия, имя модуля/файла, путь и вид (sql | py)."""

    version: int
    name: str
    path: Path
    kind: str

    @property
    def is_python(self) -> bool:
        """Является ли миграция Python-модулем."""
        return self.kind == "py"


# ----------------------------------------------------------------------
# Вспомогательные функции для самих миграций
# ----------------------------------------------------------------------


async def fetch_rows(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()
) -> list[Any]:
    """Выполнить SELECT/PRAGMA и вернуть все строки (без привязки к row_factory)."""
    cursor = await conn.execute(sql, tuple(params))
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


async def fetch_scalar(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = (), default: Any = None
) -> Any:
    """Вернуть первое значение первой строки (или ``default``)."""
    rows = await fetch_rows(conn, sql, params)
    if not rows:
        return default
    value = rows[0][0]
    return default if value is None else value


async def table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    """Имена колонок таблицы (пустое множество, если таблицы нет)."""
    if not _is_identifier(table):
        raise MigrationError(f"Недопустимое имя таблицы в миграции: {table!r}")
    rows = await fetch_rows(conn, f"PRAGMA table_info({table})")
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk) — берём по индексу,
    # чтобы не зависеть от row_factory соединения.
    return {str(row[1]) for row in rows}


async def table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    """Есть ли в базе таблица с таким именем."""
    rows = await fetch_rows(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    )
    return bool(rows)


async def safe_rollback(conn: aiosqlite.Connection) -> None:
    """Откатить транзакцию, не подменяя исходную ошибку."""
    try:
        await conn.rollback()
    except Exception:  # noqa: BLE001 - откат не должен скрывать причину сбоя
        logger.exception("Не удалось откатить транзакцию миграции")


async def set_foreign_keys(conn: aiosqlite.Connection, enabled: bool) -> None:
    """Включить/выключить проверку внешних ключей и убедиться, что это сработало.

    SQLite молча игнорирует ``PRAGMA foreign_keys`` внутри транзакции, поэтому
    результат обязательно проверяется: перестройка таблицы при включённых
    внешних ключах порвала бы ссылки из ``tracks`` и ``artists``.
    """
    if conn.in_transaction:
        await conn.commit()
    await conn.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    actual = bool(await fetch_scalar(conn, "PRAGMA foreign_keys", default=0))
    if actual != bool(enabled):
        raise MigrationError(
            "Не удалось "
            + ("включить" if enabled else "выключить")
            + " проверку внешних ключей (PRAGMA foreign_keys). "
            "Схема базы данных не изменена."
        )


async def foreign_key_problems(conn: aiosqlite.Connection) -> list[Any]:
    """Строки ``PRAGMA foreign_key_check`` (пустой список — нарушений нет)."""
    return await fetch_rows(conn, "PRAGMA foreign_key_check")


# ----------------------------------------------------------------------
# Поиск и учёт миграций
# ----------------------------------------------------------------------


def discover_migrations() -> list[Migration]:
    """Найти файлы миграций и отсортировать их по числовому префиксу."""
    found: dict[int, Migration] = {}
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        if not path.is_file() or path.name in _IGNORED_FILES:
            continue
        if path.suffix not in (".sql", ".py"):
            continue
        match = _FILE_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"Файл {path.name} лежит в каталоге миграций, но не подходит под шаблон "
                "«NNN_описание.sql» или «NNN_описание.py». Переименуйте его или уберите."
            )
        version = int(match.group("version"))
        migration = Migration(
            version=version,
            name=path.stem,
            path=path,
            kind=match.group("kind"),
        )
        previous = found.get(version)
        if previous is not None:
            raise MigrationError(
                f"Две миграции с одинаковым номером {version}: "
                f"{previous.path.name} и {path.name}."
            )
        found[version] = migration
    return [found[version] for version in sorted(found)]


async def ensure_schema_migrations(conn: aiosqlite.Connection) -> None:
    """Создать таблицу учёта миграций, если её ещё нет."""
    if conn.in_transaction:
        await conn.commit()
    await conn.execute(SCHEMA_MIGRATIONS_DDL)
    await conn.commit()


async def applied_versions(conn: aiosqlite.Connection) -> set[int]:
    """Номера уже применённых миграций."""
    rows = await fetch_rows(conn, "SELECT version FROM schema_migrations")
    return {int(row[0]) for row in rows}


async def run_migrations(conn: aiosqlite.Connection) -> list[Migration]:
    """Применить все непринятые миграции по порядку. Возвращает применённые."""
    migrations = discover_migrations()
    if not migrations:
        logger.warning("Каталог миграций пуст: %s", MIGRATIONS_DIR)
        return []

    await ensure_schema_migrations(conn)
    applied = await applied_versions(conn)
    applied |= await _mark_baseline(conn, migrations, applied)

    done: list[Migration] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        logger.info("Применяется миграция %s", migration.name)
        if migration.is_python:
            await _apply_python(conn, migration)
        else:
            await _apply_sql(conn, migration)
        done.append(migration)
        logger.info("Миграция %s применена", migration.name)
    return done


# ----------------------------------------------------------------------
# Применение миграций
# ----------------------------------------------------------------------


async def _mark_baseline(
    conn: aiosqlite.Connection, migrations: Sequence[Migration], applied: set[int]
) -> set[int]:
    """Пометить базовую миграцию применённой для уже существующей базы V1."""
    if BASELINE_VERSION in applied:
        return set()
    baseline = next((m for m in migrations if m.version == BASELINE_VERSION), None)
    if baseline is None:
        return set()
    if not await table_exists(conn, BASELINE_MARKER_TABLE):
        return set()
    await _record(conn, baseline)
    logger.info(
        "Обнаружена база V1 (таблица %s уже есть): миграция %s помечена применённой "
        "без выполнения",
        BASELINE_MARKER_TABLE,
        baseline.name,
    )
    return {BASELINE_VERSION}


async def _record(conn: aiosqlite.Connection, migration: Migration) -> None:
    """Записать миграцию как применённую (в отдельной транзакции)."""
    if conn.in_transaction:
        await conn.commit()
    await conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (migration.version, migration.name),
    )
    await conn.commit()


def _record_statement(migration: Migration) -> str:
    """SQL-инструкция записи в ``schema_migrations`` для включения в скрипт."""
    name = migration.name.replace("'", "''")
    return (
        "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
        f"VALUES ({int(migration.version)}, '{name}', CURRENT_TIMESTAMP);"
    )


async def _skip_existing_columns(conn: aiosqlite.Connection, script: str) -> str:
    """Убрать из скрипта ADD COLUMN для колонок, которые уже есть в таблице."""
    matches = list(_ADD_COLUMN_RE.finditer(script))
    if not matches:
        return script
    cache: dict[str, set[str]] = {}
    parts: list[str] = []
    cursor = 0
    for match in matches:
        table = match.group("table")
        column = match.group("column")
        columns = cache.get(table)
        if columns is None:
            columns = await table_columns(conn, table)
            cache[table] = columns
        if column not in columns:
            continue
        parts.append(script[cursor : match.start()])
        parts.append(f"-- пропущено: колонка {table}.{column} уже существует\n")
        cursor = match.end()
        logger.debug("Миграция: колонка %s.%s уже существует, ADD COLUMN пропущен", table, column)
    if not parts:
        return script
    parts.append(script[cursor:])
    return "".join(parts)


async def _apply_sql(conn: aiosqlite.Connection, migration: Migration) -> None:
    """Выполнить .sql-миграцию через executescript в одной транзакции."""
    raw = await asyncio.to_thread(migration.path.read_text, encoding="utf-8")
    body = await _skip_existing_columns(conn, raw)
    script = "BEGIN;\n" + body.strip() + "\n" + _record_statement(migration) + "\nCOMMIT;\n"
    if conn.in_transaction:
        await conn.commit()
    try:
        # executescript сам коммитит незакрытую транзакцию и НЕ добавляет своих
        # BEGIN/COMMIT — управление транзакцией целиком в тексте скрипта.
        await conn.executescript(script)
    except Exception as exc:  # noqa: BLE001 - оборачиваем в понятную ошибку
        await safe_rollback(conn)
        raise MigrationError(
            f"Миграция {migration.name} не применена: {exc}. Схема базы данных не изменена."
        ) from exc


async def _apply_python(conn: aiosqlite.Connection, migration: Migration) -> None:
    """Выполнить .py-миграцию: модуль с асинхронной функцией migrate(conn)."""
    module_name = f"{__name__}.{migration.name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - оборачиваем в понятную ошибку
        raise MigrationError(
            f"Не удалось загрузить миграцию {migration.name}: {exc}"
        ) from exc

    migrate = getattr(module, "migrate", None)
    if not callable(migrate) or not inspect.iscoroutinefunction(migrate):
        raise MigrationError(
            f"Миграция {migration.name} должна определять асинхронную функцию "
            "migrate(conn: aiosqlite.Connection)."
        )

    atomic = bool(getattr(module, "ATOMIC", True))
    if not atomic:
        # Миграция сама управляет транзакциями и PRAGMA (например, перестройка таблицы).
        try:
            await migrate(conn)
        except MigrationError:
            await safe_rollback(conn)
            raise
        except Exception as exc:  # noqa: BLE001 - оборачиваем в понятную ошибку
            await safe_rollback(conn)
            raise MigrationError(
                f"Миграция {migration.name} не применена: {exc}. "
                "Схема базы данных не изменена."
            ) from exc
        await _record(conn, migration)
        return

    if conn.in_transaction:
        await conn.commit()
    await conn.execute("BEGIN")
    try:
        await migrate(conn)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (migration.version, migration.name),
        )
        await conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - оборачиваем в понятную ошибку
        await safe_rollback(conn)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"Миграция {migration.name} не применена: {exc}. Схема базы данных не изменена."
        ) from exc


def _is_identifier(value: str) -> bool:
    """Безопасно ли подставлять имя в SQL напрямую."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


__all__ = [
    "BASELINE_MARKER_TABLE",
    "BASELINE_VERSION",
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationError",
    "applied_versions",
    "discover_migrations",
    "ensure_schema_migrations",
    "fetch_rows",
    "fetch_scalar",
    "foreign_key_problems",
    "run_migrations",
    "safe_rollback",
    "set_foreign_keys",
    "table_columns",
    "table_exists",
]
