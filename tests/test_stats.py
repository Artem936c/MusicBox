"""Тесты разделов статистики: границы, порядок и учёт прослушиваний."""

from __future__ import annotations

import logging

import pytest

from backend.db.database import db
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import stats as stats_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import ValidationError
from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)

#: Числа прослушиваний для подготовленной библиотеки (порядок создания важен).
PLAY_COUNTS: tuple[int, ...] = (0, 1, 3, 5, 7, 10, 25)


@pytest.fixture
async def library(track_factory: TrackFactory) -> dict[int, dict]:
    """Семь треков с числом прослушиваний 0, 1, 3, 5, 7, 10 и 25."""
    result: dict[int, dict] = {}
    for count in PLAY_COUNTS:
        result[count] = await track_factory(
            f"Трек с {count} прослушиваниями", play_count=count
        )
    return result


def _play_counts(tracks: list[dict]) -> list[int]:
    """Список play_count в порядке выдачи раздела."""
    return [int(track["play_count"]) for track in tracks]


# ---------------------------------------------------------------------------
# Границы разделов
# ---------------------------------------------------------------------------


async def test_unplayed_contains_only_zero(
    user_id: int, library: dict[int, dict]
) -> None:
    """Раздел «ни разу не проигранные» — строго play_count = 0."""
    tracks = await stats_repo.unplayed(user_id)

    assert _play_counts(tracks) == [0]
    assert tracks[0]["id"] == library[0]["id"]


async def test_frequent_threshold_boundary(
    user_id: int, library: dict[int, dict]
) -> None:
    """«Часто прослушиваемые» — play_count >= 10 (порог по умолчанию)."""
    tracks = await stats_repo.frequent(user_id)

    assert _play_counts(tracks) == [25, 10]

    # Явный порог перекрывает настройки пользователя.
    assert _play_counts(await stats_repo.frequent(user_id, threshold=7)) == [25, 10, 7]
    assert _play_counts(await stats_repo.frequent(user_id, threshold=26)) == []


async def test_rare_bounds(user_id: int, library: dict[int, dict]) -> None:
    """«Редко прослушиваемые» — диапазон 1..5 включительно, без нулей."""
    tracks = await stats_repo.rare(user_id)

    assert _play_counts(tracks) == [5, 3, 1]

    explicit = await stats_repo.rare(user_id, min_count=1, max_count=7)
    assert _play_counts(explicit) == [7, 5, 3, 1]


async def test_top_order_by_play_count(user_id: int, library: dict[int, dict]) -> None:
    """«Самые часто прослушиваемые» — только play_count > 0, по убыванию."""
    tracks = await stats_repo.top(user_id, limit=10)

    assert _play_counts(tracks) == [25, 10, 7, 5, 3, 1]
    assert all(track["play_count"] > 0 for track in tracks)

    limited = await stats_repo.top(user_id, limit=2)
    assert _play_counts(limited) == [25, 10]


async def test_recent_contains_all_tracks(
    user_id: int, library: dict[int, dict]
) -> None:
    """«Недавно добавленные» — все треки, последний добавленный первым."""
    tracks = await stats_repo.recent(user_id, limit=50)

    assert len(tracks) == len(PLAY_COUNTS)
    assert tracks[0]["id"] == library[PLAY_COUNTS[-1]]["id"]
    assert tracks[-1]["id"] == library[PLAY_COUNTS[0]]["id"]


async def test_section_dispatcher(user_id: int, library: dict[int, dict]) -> None:
    """Диспетчер `section` повторяет результаты отдельных разделов."""
    assert stats_repo.SECTION_KEYS == ("top", "recent", "unplayed", "frequent", "rare")

    for key in stats_repo.SECTION_KEYS:
        assert key in stats_repo.SECTION_TITLES

    assert _play_counts(await stats_repo.section(user_id, "top")) == [25, 10, 7, 5, 3, 1]
    assert _play_counts(await stats_repo.section(user_id, "unplayed")) == [0]
    assert _play_counts(await stats_repo.section(user_id, "frequent")) == [25, 10]
    assert _play_counts(await stats_repo.section(user_id, "rare")) == [5, 3, 1]
    assert len(await stats_repo.section(user_id, "recent")) == len(PLAY_COUNTS)

    with pytest.raises(ValidationError):
        await stats_repo.section(user_id, "unknown")


# ---------------------------------------------------------------------------
# Счётчики и сводка
# ---------------------------------------------------------------------------


async def test_counts(user_id: int, library: dict[int, dict]) -> None:
    """Счётчики совпадают с содержимым разделов."""
    await folders_repo.create_folder(user_id, "Кино")
    await artists_repo.ensure_artist(user_id, "Кино")
    await favourites_repo.add(user_id, library[25]["id"])

    counts = await stats_repo.counts(user_id)

    assert counts["total"] == len(PLAY_COUNTS)
    assert counts["total_plays"] == sum(PLAY_COUNTS)
    assert counts["recent"] == len(PLAY_COUNTS)
    assert counts["top"] == 6
    assert counts["unplayed"] == 1
    assert counts["frequent"] == 2
    assert counts["rare"] == 3
    assert counts["favourites"] == 1
    assert counts["folders"] == 1
    assert counts["artists"] == 1


async def test_overview_sections_order(user_id: int, library: dict[int, dict]) -> None:
    """Сводка отдаёт пять разделов в порядке контракта с превью треков."""
    data = await stats_repo.overview(user_id, limit=3)

    keys = [section["key"] for section in data["sections"]]
    assert keys == list(stats_repo.SECTION_KEYS)

    titles = {section["key"]: section["title"] for section in data["sections"]}
    assert titles["top"] == "Самые часто прослушиваемые"
    assert titles["unplayed"] == "Ни разу не проигранные"

    by_key = {section["key"]: section for section in data["sections"]}
    assert by_key["top"]["count"] == 6
    assert len(by_key["top"]["items"]) == 3
    assert _play_counts(by_key["top"]["items"]) == [25, 10, 7]
    assert by_key["unplayed"]["count"] == 1
    assert by_key["frequent"]["count"] == 2
    assert by_key["rare"]["count"] == 3
    assert data["counts"]["total"] == len(PLAY_COUNTS)


async def test_empty_library_sections(user: dict, user_id: int) -> None:
    """Пустая библиотека: разделы пустые, счётчики нулевые."""
    for key in stats_repo.SECTION_KEYS:
        assert await stats_repo.section(user_id, key) == []

    counts = await stats_repo.counts(user_id)
    assert counts["total"] == 0
    assert counts["total_plays"] == 0


# ---------------------------------------------------------------------------
# Учёт прослушиваний
# ---------------------------------------------------------------------------


async def test_register_play_increments_counter(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """`register_play` увеличивает счётчик и проставляет last_played_at."""
    track = await track_factory("Спокойная ночь")
    assert track["play_count"] == 0
    assert track["last_played_at"] is None

    first = await tracks_repo.register_play(user_id, track["id"], source="web")
    assert first is not None
    assert first["play_count"] == 1
    assert first["last_played_at"] is not None

    second = await tracks_repo.register_play(user_id, track["id"], source="bot")
    assert second is not None
    assert second["play_count"] == 2

    history = await db.fetch_all(
        "SELECT source FROM play_history WHERE user_id = ? AND track_id = ? ORDER BY id",
        (user_id, track["id"]),
    )
    assert [row["source"] for row in history] == ["web", "bot"]

    assert await tracks_repo.total_plays(user_id) == 2
    assert await tracks_repo.total_tracks(user_id) == 1


async def test_register_play_marks_artist_listened(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Прослушивание отмечает исполнителя как «прослушано»."""
    artist = await artists_repo.ensure_artist(user_id, "Кино")
    assert artist is not None
    assert artist["is_listened"] is False

    track = await track_factory("Группа крови", artist="Кино", artist_id=artist["id"])
    await tracks_repo.register_play(user_id, track["id"])

    updated = await artists_repo.get_artist(user_id, artist["id"])
    assert updated is not None
    assert updated["is_listened"] is True
    assert updated["listened_at"] is not None


async def test_register_play_unknown_track(user: dict, user_id: int) -> None:
    """Несуществующий трек не меняет статистику."""
    assert await tracks_repo.register_play(user_id, 999_999) is None
    assert await tracks_repo.total_plays(user_id) == 0


async def test_register_play_moves_track_between_sections(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """После первого прослушивания трек уходит из «ни разу» в «редко» и «топ»."""
    track = await track_factory("Мама, мы все тяжело больны")

    assert [item["id"] for item in await stats_repo.unplayed(user_id)] == [track["id"]]
    assert await stats_repo.top(user_id) == []

    await tracks_repo.register_play(user_id, track["id"])

    assert await stats_repo.unplayed(user_id) == []
    assert [item["id"] for item in await stats_repo.rare(user_id)] == [track["id"]]
    assert [item["id"] for item in await stats_repo.top(user_id)] == [track["id"]]
    assert await stats_repo.frequent(user_id) == []
