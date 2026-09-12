"""Регрессия миграций схемы (раздел 8 контракта V2).

Главное требование раздела 1 контракта V2 — «существующие данные обязаны
сохраниться». Проверяем это на КОПИИ реальной базы ``backups/musicbox_before_v2.db``,
снятой до перехода на V2: боевой файл ``data/musicbox.db`` тесты не открывают
вообще, а резервная копия копируется в ``tmp_path`` и мигрируется уже там.

Порядок работы каждого теста:

1. :func:`legacy_copy` кладёт копию базы V1 во временный каталог теста;
2. :func:`v1_snapshot` читает её обычным ``sqlite3`` ДО миграций — это эталон,
   с которым сравнивается состояние после;
3. :func:`migrated_db` вызывает :func:`backend.db.database.init_db`, то есть
   ровно тот путь, которым база обновляется в проде.

Дополнительно проверяются идемпотентность повторного прогона и сборка схемы
с нуля на пустом файле (там миграция 001 выполняется, а не помечается).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from backend.config import Settings
from backend.db import migrations as migrations_pkg
from backend.db.database import apply_migrations, db, init_db, shutdown_db

logger = logging.getLogger(__name__)

#: Корень репозитория (tests/ лежит рядом с backups/).
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Резервная копия боевой базы, снятая до миграций V2.
BACKUP_DB: Path = REPO_ROOT / "backups" / "musicbox_before_v2.db"

#: Сколько строк в базе V1 — зафиксировано в задании на регрессию.
EXPECTED_TRACKS: int = 7
EXPECTED_FOLDERS: int = 8

#: Идентификаторы папок V1. Нумерация с пропуском (папки 7 нет) — именно поэтому
#: «сохранить id» и «сохранить количество» это два разных требования.
EXPECTED_FOLDER_IDS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 8, 9)

#: Таблицы V1, количество строк в которых миграции менять не имеют права.
PRESERVED_TABLES: tuple[str, ...] = (
    "users",
    "user_settings",
    "folders",
    "artists",
    "albums",
    "tracks",
    "playlists",
    "playlist_tracks",
    "favourites",
    "play_history",
)

#: Таблицы, которые обязаны существовать после всех миграций.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "user_settings",
        "folders",
        "artists",
        "albums",
        "tracks",
        "playlists",
        "playlist_tracks",
        "favourites",
        "play_history",
        "schema_migrations",
        "track_artists",
        "notes",
        "note_items",
    }
)

#: Номера миграций, которые должны оказаться применёнными.
EXPECTED_VERSIONS: frozenset[int] = frozenset({1, 2, 3, 4, 5})


# ---------------------------------------------------------------------------
# Снимок базы V1 (обычный sqlite3, до всяких миграций)
# ---------------------------------------------------------------------------


def _read_v1_snapshot(path: Path) -> dict[str, Any]:
    """Снять эталон с немигрированной копии: счётчики, папки и привязки треков."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        counts = {
            name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in sorted(tables)
            if not name.startswith("sqlite_")
        }
        folders = {
            int(row["id"]): {
                "user_id": int(row["user_id"]),
                "name": row["name"],
                "normalized_name": row["normalized_name"],
                "is_artist_folder": int(row["is_artist_folder"] or 0),
                "created_at": row["created_at"],
            }
            for row in conn.execute("SELECT * FROM folders")
        }
        tracks = {
            int(row["id"]): {
                "user_id": int(row["user_id"]),
                "title": row["title"],
                "folder_id": None if row["folder_id"] is None else int(row["folder_id"]),
                "artist_id": None if row["artist_id"] is None else int(row["artist_id"]),
                "play_count": int(row["play_count"] or 0),
            }
            for row in conn.execute("SELECT * FROM tracks")
        }
        folder_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(folders)")
        }
        track_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    finally:
        conn.close()
    return {
        "tables": tables,
        "counts": counts,
        "folders": folders,
        "tracks": tracks,
        "folder_columns": folder_columns,
        "track_columns": track_columns,
    }


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_copy(tmp_path: Path) -> Iterator[Path]:
    """Копия базы V1 во временном каталоге (оригинал только читается)."""
    if not BACKUP_DB.exists():  # pragma: no cover - зависит от наличия артефакта
        pytest.skip(
            f"Нет резервной копии базы V1: {BACKUP_DB}. "
            "Файлы *.db не хранятся в репозитории."
        )
    destination = tmp_path / "musicbox_before_v2.db"
    shutil.copy2(BACKUP_DB, destination)
    logger.debug("Копия базы V1 готова: %s", destination)
    yield destination


@pytest.fixture
def v1_snapshot(legacy_copy: Path) -> dict[str, Any]:
    """Эталонное состояние копии ДО миграций (фикстура выполняется раньше migrated_db)."""
    return _read_v1_snapshot(legacy_copy)


@pytest.fixture
async def migrated_db(
    app_settings: Settings, legacy_copy: Path, v1_snapshot: dict[str, Any]
) -> AsyncIterator[Path]:
    """Подключение к копии базы V1 после применения миграций (как в проде)."""
    # Синглтон мог остаться открытым от предыдущего теста — закрываем принудительно.
    await shutdown_db()
    await init_db(str(legacy_copy))
    try:
        yield legacy_copy
    finally:
        await shutdown_db()


async def _table_names() -> set[str]:
    """Имена таблиц текущей подключённой базы."""
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row["name"]) for row in rows}


async def _counts(tables: tuple[str, ...]) -> dict[str, int]:
    """Количество строк по каждой из перечисленных таблиц."""
    result: dict[str, int] = {}
    for name in tables:
        result[name] = int(await db.fetch_val(f'SELECT COUNT(*) FROM "{name}"', default=0))
    return result


# ---------------------------------------------------------------------------
# Исходный файл действительно V1
# ---------------------------------------------------------------------------


def test_backup_copy_is_v1_schema(v1_snapshot: dict[str, Any]) -> None:
    """Копия — именно база V1: старая схема папок и ожидаемый объём данных."""
    assert "schema_migrations" not in v1_snapshot["tables"]
    assert "parent_folder_id" not in v1_snapshot["folder_columns"]
    assert "section" not in v1_snapshot["folder_columns"]
    assert "file_type" not in v1_snapshot["track_columns"]
    assert "track_artists" not in v1_snapshot["tables"]

    assert v1_snapshot["counts"]["tracks"] == EXPECTED_TRACKS
    assert v1_snapshot["counts"]["folders"] == EXPECTED_FOLDERS
    assert tuple(sorted(v1_snapshot["folders"])) == EXPECTED_FOLDER_IDS


# ---------------------------------------------------------------------------
# Данные не потеряны
# ---------------------------------------------------------------------------


async def test_migrations_keep_all_rows(
    migrated_db: Path, v1_snapshot: dict[str, Any]
) -> None:
    """Ни одна строка V1 не потеряна и не размножена: 7 треков, 8 папок и остальное."""
    after = await _counts(PRESERVED_TABLES)

    assert after["tracks"] == EXPECTED_TRACKS
    assert after["folders"] == EXPECTED_FOLDERS

    before = {name: v1_snapshot["counts"][name] for name in PRESERVED_TABLES}
    assert after == before


async def test_migrations_preserve_folder_ids_and_fields(
    migrated_db: Path, v1_snapshot: dict[str, Any]
) -> None:
    """Идентификаторы папок сохранены вместе с именами, флагами и датой создания."""
    rows = await db.fetch_all("SELECT * FROM folders ORDER BY id")
    actual_ids = tuple(int(row["id"]) for row in rows)

    assert actual_ids == EXPECTED_FOLDER_IDS, "id папок обязаны пережить перестройку таблицы"

    for row in rows:
        original = v1_snapshot["folders"][int(row["id"])]
        assert row["user_id"] == original["user_id"]
        assert row["name"] == original["name"]
        assert row["normalized_name"] == original["normalized_name"]
        assert int(row["is_artist_folder"] or 0) == original["is_artist_folder"]
        assert row["created_at"] == original["created_at"], (
            "created_at не должен перезаписываться CURRENT_TIMESTAMP при переливе"
        )


async def test_migrations_keep_track_folder_links(
    migrated_db: Path, v1_snapshot: dict[str, Any]
) -> None:
    """``tracks.folder_id`` указывают на те же самые папки, что и до миграций."""
    rows = await db.fetch_all("SELECT id, user_id, folder_id FROM tracks ORDER BY id")
    actual = {
        int(row["id"]): None if row["folder_id"] is None else int(row["folder_id"])
        for row in rows
    }
    expected = {
        track_id: data["folder_id"] for track_id, data in v1_snapshot["tracks"].items()
    }

    assert actual == expected

    # Каждая непустая ссылка ведёт на существующую папку ТОГО ЖЕ пользователя:
    # выключение внешних ключей на время перестройки не должно было это порвать.
    broken = await db.fetch_all(
        """
        SELECT t.id
        FROM tracks t
        LEFT JOIN folders f ON f.id = t.folder_id
        WHERE t.folder_id IS NOT NULL AND (f.id IS NULL OR f.user_id <> t.user_id)
        """
    )
    assert broken == [], f"Треки с битой ссылкой на папку: {broken}"


async def test_migrations_leave_no_foreign_key_violations(migrated_db: Path) -> None:
    """После перестройки ``folders`` внешние ключи включены и не нарушены."""
    assert int(await db.fetch_val("PRAGMA foreign_keys", default=0)) == 1

    problems = await db.fetch_all("PRAGMA foreign_key_check")
    assert problems == [], f"PRAGMA foreign_key_check нашёл нарушения: {problems}"

    assert await db.fetch_val("PRAGMA integrity_check") == "ok"


async def test_migrations_fill_track_artists(
    migrated_db: Path, v1_snapshot: dict[str, Any]
) -> None:
    """``track_artists`` заполнена из ``tracks.artist_id``: основной исполнитель — position 0."""
    rows = await db.fetch_all("SELECT track_id, artist_id, position FROM track_artists")
    actual = {(int(row["track_id"]), int(row["artist_id"])) for row in rows}

    expected = {
        (track_id, data["artist_id"])
        for track_id, data in v1_snapshot["tracks"].items()
        if data["artist_id"] is not None
    }

    assert expected, "В базе V1 должны быть треки с исполнителем — иначе тест бессмыслен"
    assert actual == expected
    assert all(int(row["position"]) == 0 for row in rows)

    # Денормализованный tracks.artist_id остаётся источником «основного» исполнителя.
    orphans = await db.fetch_all(
        """
        SELECT ta.track_id
        FROM track_artists ta
        LEFT JOIN artists a ON a.id = ta.artist_id
        WHERE a.id IS NULL
        """
    )
    assert orphans == []


async def test_migrations_add_v2_columns(migrated_db: Path) -> None:
    """Схема доросла до V2: вложенность, разделы, типы файлов, жанр, заметки."""
    folder_columns = {
        str(row["name"]) for row in await db.fetch_all("PRAGMA table_info(folders)")
    }
    assert {"parent_folder_id", "section"} <= folder_columns

    track_columns = {
        str(row["name"]) for row in await db.fetch_all("PRAGMA table_info(tracks)")
    }
    assert {"file_type", "genre"} <= track_columns
    # Имя колонки V1 не переименовано (раздел 1.3 контракта).
    assert "storage_message_id" in track_columns

    assert EXPECTED_TABLES <= await _table_names()

    # Все папки V1 становятся корневыми папками раздела «music».
    sections = await db.fetch_all("SELECT DISTINCT section FROM folders")
    assert [row["section"] for row in sections] == ["music"]
    assert int(
        await db.fetch_val(
            "SELECT COUNT(*) FROM folders WHERE parent_folder_id IS NOT NULL", default=0
        )
    ) == 0

    # Все файлы V1 — аудио (раздел «Треки»).
    types = await db.fetch_all("SELECT DISTINCT file_type FROM tracks")
    assert [row["file_type"] for row in types] == ["audio"]


async def test_migrations_recount_artist_total_plays(
    migrated_db: Path, v1_snapshot: dict[str, Any]
) -> None:
    """``artists.total_plays`` пересчитан из прослушиваний треков (миграция 004)."""
    expected: dict[int, int] = {}
    for data in v1_snapshot["tracks"].values():
        artist_id = data["artist_id"]
        if artist_id is not None:
            expected[artist_id] = expected.get(artist_id, 0) + data["play_count"]

    rows = await db.fetch_all("SELECT id, total_plays FROM artists")
    for row in rows:
        assert int(row["total_plays"]) == expected.get(int(row["id"]), 0)


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------


async def test_baseline_is_marked_not_executed_on_existing_v1_database(
    app_settings: Settings, legacy_copy: Path, v1_snapshot: dict[str, Any]
) -> None:
    """Базовая миграция на существующей базе V1 помечается применённой без выполнения."""
    conn = await aiosqlite.connect(str(legacy_copy))
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        applied = await migrations_pkg.run_migrations(conn)
        names = [item.name for item in applied]

        assert "001_baseline" not in names, (
            "На базе V1 миграция 001 не выполняется — иначе CREATE TABLE снёс бы данные"
        )
        assert names == ["002_nested_folders", "003_track_artists", "004_media", "005_notes"]
        assert await migrations_pkg.applied_versions(conn) == set(EXPECTED_VERSIONS)

        # Повторный прогон на том же соединении не делает ничего.
        assert await migrations_pkg.run_migrations(conn) == []
    finally:
        await conn.close()


async def test_migrations_are_idempotent_within_one_connection(migrated_db: Path) -> None:
    """Повторный ``apply_migrations()`` ничего не меняет."""
    before_counts = await _counts(PRESERVED_TABLES)
    before_versions = {
        int(row["version"])
        for row in await db.fetch_all("SELECT version FROM schema_migrations")
    }
    before_folders = await db.fetch_all("SELECT * FROM folders ORDER BY id")

    await apply_migrations()
    await apply_migrations()

    assert await _counts(PRESERVED_TABLES) == before_counts
    assert {
        int(row["version"])
        for row in await db.fetch_all("SELECT version FROM schema_migrations")
    } == before_versions == set(EXPECTED_VERSIONS)
    assert await db.fetch_all("SELECT * FROM folders ORDER BY id") == before_folders
    assert await db.fetch_all("PRAGMA foreign_key_check") == []


async def test_migrations_are_idempotent_across_restarts(
    app_settings: Settings, legacy_copy: Path, v1_snapshot: dict[str, Any]
) -> None:
    """Полный перезапуск приложения на уже мигрированной базе безопасен."""
    await shutdown_db()
    try:
        await init_db(str(legacy_copy))
        first = {
            "counts": await _counts(PRESERVED_TABLES),
            "folders": await db.fetch_all("SELECT * FROM folders ORDER BY id"),
            "track_artists": await db.fetch_all(
                "SELECT * FROM track_artists ORDER BY track_id, artist_id"
            ),
        }
        await shutdown_db()

        # Второй «запуск» — ровно тот же путь инициализации.
        await init_db(str(legacy_copy))
        second = {
            "counts": await _counts(PRESERVED_TABLES),
            "folders": await db.fetch_all("SELECT * FROM folders ORDER BY id"),
            "track_artists": await db.fetch_all(
                "SELECT * FROM track_artists ORDER BY track_id, artist_id"
            ),
        }

        assert second == first
        assert second["counts"]["tracks"] == EXPECTED_TRACKS
        assert second["counts"]["folders"] == EXPECTED_FOLDERS
        assert await db.fetch_all("PRAGMA foreign_key_check") == []
    finally:
        await shutdown_db()


# ---------------------------------------------------------------------------
# Пустая база
# ---------------------------------------------------------------------------


async def test_migrations_build_schema_on_empty_database(tmp_path: Path) -> None:
    """На пустом файле миграции собирают схему целиком, включая 001_baseline."""
    path = tmp_path / "fresh.db"
    conn = await aiosqlite.connect(str(path))
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        applied = await migrations_pkg.run_migrations(conn)

        assert [item.name for item in applied] == [
            "001_baseline",
            "002_nested_folders",
            "003_track_artists",
            "004_media",
            "005_notes",
        ]

        rows = await migrations_pkg.fetch_rows(
            conn, "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        assert EXPECTED_TABLES <= {str(row[0]) for row in rows}

        # Свежая база сразу в формате V2: перестройка папок отработала и здесь.
        assert {"parent_folder_id", "section"} <= await migrations_pkg.table_columns(
            conn, "folders"
        )
        assert {"file_type", "genre"} <= await migrations_pkg.table_columns(conn, "tracks")

        indexes = await migrations_pkg.fetch_rows(
            conn,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_folders%'",
        )
        assert {"idx_folders_user", "idx_folders_parent", "idx_folders_section"} <= {
            str(row[0]) for row in indexes
        }

        assert await migrations_pkg.foreign_key_problems(conn) == []
        assert await migrations_pkg.applied_versions(conn) == set(EXPECTED_VERSIONS)
        assert await migrations_pkg.run_migrations(conn) == []
    finally:
        await conn.close()


async def test_init_db_on_empty_file_is_idempotent(
    app_settings: Settings, tmp_path: Path
) -> None:
    """``init_db`` на новом файле создаёт схему и переживает повторный запуск."""
    path = tmp_path / "created-by-init.db"
    await shutdown_db()
    try:
        await init_db(str(path))
        assert EXPECTED_TABLES <= await _table_names()
        assert await _counts(("tracks", "folders")) == {"tracks": 0, "folders": 0}
        await shutdown_db()

        await init_db(str(path))
        versions = {
            int(row["version"])
            for row in await db.fetch_all("SELECT version FROM schema_migrations")
        }
        assert versions == set(EXPECTED_VERSIONS)
        assert await db.fetch_all("PRAGMA foreign_key_check") == []
    finally:
        await shutdown_db()


# ---------------------------------------------------------------------------
# Каталог миграций
# ---------------------------------------------------------------------------


def test_discover_migrations_is_ordered_and_unique() -> None:
    """Миграции находятся по номеру, номера уникальны и идут по возрастанию."""
    found = migrations_pkg.discover_migrations()
    versions = [item.version for item in found]

    assert versions, "Каталог миграций не должен быть пустым"
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == migrations_pkg.BASELINE_VERSION
    assert set(versions) == set(EXPECTED_VERSIONS)
    assert all(item.kind in ("sql", "py") for item in found)
