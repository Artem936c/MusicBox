"""Тесты нечёткого поиска: опечатки, смена раскладки, пустой запрос, алфавит."""

from __future__ import annotations

import logging

import pytest

from backend.db.repositories import albums as albums_repo
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.services import search as search_service
from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)


@pytest.fixture
async def music_library(
    user: dict, user_id: int, track_factory: TrackFactory
) -> dict[str, object]:
    """Библиотека «Кино» и «Аквариум»: исполнители, альбомы, папки и треки."""
    kino = await artists_repo.ensure_artist(user_id, "Кино")
    aquarium = await artists_repo.ensure_artist(user_id, "Аквариум")
    assert kino is not None and aquarium is not None

    folder = await folders_repo.create_folder(user_id, "Кино", is_artist_folder=True)

    albums: dict[str, dict] = {}
    for title in ("Ночь", "Группа крови", "Звезда по имени Солнце"):
        album = await albums_repo.ensure_album(user_id, title, artist_id=kino["id"])
        assert album is not None
        albums[title] = album

    tracks = {
        "blood": await track_factory(
            "Группа крови",
            artist="Кино",
            album="Группа крови",
            artist_id=kino["id"],
            album_id=albums["Группа крови"]["id"],
            folder_id=folder["id"],
        ),
        "cuckoo": await track_factory(
            "Кукушка",
            artist="Кино",
            album="Чёрный альбом",
            artist_id=kino["id"],
            folder_id=folder["id"],
        ),
        "city": await track_factory(
            "Город золотой",
            artist="Аквариум",
            album="Десять стрел",
            artist_id=aquarium["id"],
        ),
    }
    return {"artists": {"kino": kino, "aquarium": aquarium}, "albums": albums, "tracks": tracks}


# ---------------------------------------------------------------------------
# Раскладка и варианты запроса
# ---------------------------------------------------------------------------


def test_swap_layout_both_directions() -> None:
    """Смена раскладки работает в обе стороны, включая верхний регистр."""
    assert search_service.swap_layout("rbyj") == "кино"
    assert search_service.swap_layout("кино") == "rbyj"
    assert search_service.swap_layout("Rbyj") == "Кино"
    assert search_service.swap_layout("") == ""
    # Цифры и пробелы не меняются.
    assert search_service.swap_layout("rbyj 2") == "кино 2"


def test_normalize_query() -> None:
    """Запрос приводится к нижнему регистру без лишних пробелов."""
    assert search_service.normalize_query("  КИНО   Группа  ") == "кино группа"
    assert search_service.normalize_query("") == ""


def test_variants() -> None:
    """Варианты запроса: нормализованный и в другой раскладке, без дублей."""
    assert search_service.variants("rbyj") == ["rbyj", "кино"]
    assert search_service.variants("") == []
    assert len(search_service.variants("123")) == 1


def test_score_tolerates_typo() -> None:
    """Оценка совпадения переживает опечатку в одну букву."""
    assert search_service.score(["кино"], "Кино") == 100
    assert search_service.score(["кина"], "Кино") >= 60
    assert search_service.score([], "Кино") == 0
    assert search_service.score(["кино"], "") == 0


# ---------------------------------------------------------------------------
# Поиск треков
# ---------------------------------------------------------------------------


async def test_search_tracks_exact(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Точный запрос находит все треки исполнителя."""
    found = await search_service.search_tracks(user_id, "Кино")

    titles = {track["title"] for track in found}
    assert {"Группа крови", "Кукушка"} <= titles
    assert all(track["score"] >= 60 for track in found)


async def test_search_tracks_with_typo(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Опечатка «кина» вместо «кино» не мешает поиску."""
    found = await search_service.search_tracks(user_id, "кина")

    titles = {track["title"] for track in found}
    assert "Кукушка" in titles
    assert "Группа крови" in titles


async def test_search_tracks_wrong_layout(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Запрос «rbyj» в латинской раскладке находит «Кино»."""
    found = await search_service.search_tracks(user_id, "rbyj")

    assert found, "Поиск в другой раскладке ничего не нашёл"
    titles = {track["title"] for track in found}
    assert {"Группа крови", "Кукушка"} <= titles
    assert max(track["score"] for track in found) == 100


async def test_search_tracks_empty_query(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Пустой запрос ничего не возвращает."""
    assert await search_service.search_tracks(user_id, "") == []
    assert await search_service.search_tracks(user_id, "   ") == []
    assert await search_service.search_tracks(user_id, "Кино", limit=0) == []


async def test_search_tracks_returns_full_track_dict(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Найденный трек — полный dict репозитория плюс поле score."""
    found = await search_service.search_tracks(user_id, "Кукушка")

    assert found
    track = found[0]
    assert track["title"] == "Кукушка"
    assert track["artist"] == "Кино"
    assert "score" in track
    assert "play_count" in track
    assert "is_favourite" in track
    assert "folder_name" in track


# ---------------------------------------------------------------------------
# Альбомы, исполнители, папки
# ---------------------------------------------------------------------------


async def test_search_albums_sorted_alphabetically(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Альбомы с одинаковой релевантностью идут по алфавиту."""
    found = await search_service.search_albums(user_id, "Кино")

    titles = [album["title"] for album in found]
    assert titles == ["Группа крови", "Звезда по имени Солнце", "Ночь"]
    assert all(album["artist_name"] == "Кино" for album in found)


async def test_search_artists_and_folders(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Поиск исполнителей и папок использует те же правила."""
    artists = await search_service.search_artists(user_id, "кина")
    assert [artist["name"] for artist in artists] == ["Кино"]

    folders = await search_service.search_folders(user_id, "rbyj")
    assert [folder["name"] for folder in folders] == ["Кино"]


async def test_search_all_structure(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Общий поиск возвращает все четыре списка и исходный запрос."""
    result = await search_service.search_all(user_id, "  Кино ")

    assert result["query"] == "Кино"
    assert set(result) == {"query", "tracks", "albums", "artists", "folders"}
    assert result["tracks"]
    assert result["albums"]
    assert result["artists"]
    assert result["folders"]


async def test_search_all_empty_query(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Пустой запрос — пустые списки во всех разделах."""
    result = await search_service.search_all(user_id, "   ")

    assert result == {
        "query": "",
        "tracks": [],
        "albums": [],
        "artists": [],
        "folders": [],
    }


async def test_search_ignores_unrelated_query(
    user_id: int, music_library: dict[str, object]
) -> None:
    """Совсем непохожий запрос не возвращает ничего."""
    result = await search_service.search_all(user_id, "zzzzqqqq")

    assert result["tracks"] == []
    assert result["albums"] == []
    assert result["artists"] == []
    assert result["folders"] == []
