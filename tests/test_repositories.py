"""Тесты репозиториев: пользователи, папки, треки, избранное и плейлисты."""

from __future__ import annotations

import logging

import pytest

from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import playlists as playlists_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import ValidationError
from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Пользователи и настройки
# ---------------------------------------------------------------------------


async def test_ensure_user_creates_profile_and_settings(database: str) -> None:
    """Новый пользователь создаётся вместе со строкой настроек по умолчанию."""
    new_user_id = 987_654

    created = await users_repo.ensure_user(
        new_user_id,
        username="newbie",
        first_name="Новичок",
        language_code="ru",
    )

    assert created["user_id"] == new_user_id
    assert created["username"] == "newbie"
    assert created["first_name"] == "Новичок"
    assert created["is_admin"] is False

    stored = await users_repo.get_user(new_user_id)
    assert stored is not None
    assert stored["user_id"] == new_user_id

    user_settings = await users_repo.get_settings(new_user_id)
    assert user_settings["auto_sort_enabled"] is True
    assert user_settings["frequent_threshold"] == 10
    assert user_settings["rare_min"] == 1
    assert user_settings["rare_max"] == 5
    assert user_settings["fuzzy_threshold"] == 60


async def test_ensure_user_is_idempotent_and_updates_profile(database: str) -> None:
    """Повторный вызов не создаёт второго пользователя, а обновляет профиль."""
    new_user_id = 987_655

    await users_repo.ensure_user(new_user_id, username="old", first_name="Старое")
    updated = await users_repo.ensure_user(new_user_id, username="new")

    assert updated["username"] == "new"
    # Не переданные поля сохраняются (COALESCE в UPSERT).
    assert updated["first_name"] == "Старое"


async def test_update_settings_validates_and_toggles(user: dict, user_id: int) -> None:
    """Настройки обновляются по белому списку, диапазоны проверяются."""
    updated = await users_repo.update_settings(
        user_id, frequent_threshold=15, rare_min=2, rare_max=7, fuzzy_threshold=75
    )
    assert updated["frequent_threshold"] == 15
    assert updated["rare_min"] == 2
    assert updated["rare_max"] == 7
    assert updated["fuzzy_threshold"] == 75

    with pytest.raises(ValidationError):
        await users_repo.update_settings(user_id, rare_max=1)

    toggled = await users_repo.toggle_auto_sort(user_id)
    assert toggled["auto_sort_enabled"] is False
    toggled_back = await users_repo.toggle_auto_sort(user_id)
    assert toggled_back["auto_sort_enabled"] is True


# ---------------------------------------------------------------------------
# Папки
# ---------------------------------------------------------------------------


async def test_create_folder_is_idempotent(user: dict, user_id: int) -> None:
    """Повторное создание папки с тем же названием возвращает существующую."""
    first = await folders_repo.create_folder(user_id, "Кино")
    second = await folders_repo.create_folder(user_id, "  кино  ")

    assert first["id"] == second["id"]
    assert first["name"] == "Кино"

    folders = await folders_repo.list_folders(user_id)
    assert len(folders) == 1

    found = await folders_repo.find_folder_by_name(user_id, "КИНО")
    assert found is not None
    assert found["id"] == first["id"]


async def test_list_folders_sorted_alphabetically(user: dict, user_id: int) -> None:
    """Папки отдаются по алфавиту с учётом кириллицы и регистра."""
    for name in ("Ялта", "аквариум", "Кино", "Би-2"):
        await folders_repo.create_folder(user_id, name)

    names = [folder["name"] for folder in await folders_repo.list_folders(user_id)]
    assert names == ["аквариум", "Би-2", "Кино", "Ялта"]


async def test_delete_folder_keeps_tracks(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Удаление папки не удаляет треки: они остаются без папки."""
    folder = await folders_repo.create_folder(user_id, "Рок")
    track = await track_factory("Группа крови", folder_id=folder["id"])

    assert await folders_repo.folder_track_count(user_id, folder["id"]) == 1
    assert await folders_repo.delete_folder(user_id, folder["id"]) is True

    remaining = await tracks_repo.get_track(user_id, track["id"])
    assert remaining is not None
    assert remaining["folder_id"] is None


# ---------------------------------------------------------------------------
# Треки
# ---------------------------------------------------------------------------


async def test_create_track_returns_full_dict(user: dict, user_id: int) -> None:
    """Созданный трек содержит все поля контракта."""
    track = await tracks_repo.create_track(
        user_id,
        title="Звезда по имени Солнце",
        artist="Кино",
        album="Звезда по имени Солнце",
        duration=227,
        file_id="file-abc",
        file_unique_id="unique-abc",
    )

    assert track["id"] > 0
    assert track["title"] == "Звезда по имени Солнце"
    assert track["artist"] == "Кино"
    assert track["play_count"] == 0
    assert track["last_played_at"] is None
    assert track["is_favourite"] == 0
    assert track["folder_name"] is None


async def test_create_track_duplicate_by_unique_id(user: dict, user_id: int) -> None:
    """Повторная загрузка того же файла возвращает уже сохранённый трек."""
    first = await tracks_repo.create_track(
        user_id, title="Первый", file_id="file-1", file_unique_id="same-unique"
    )
    second = await tracks_repo.create_track(
        user_id, title="Второй", file_id="file-2", file_unique_id="same-unique"
    )

    assert first["id"] == second["id"]
    assert second["title"] == "Первый"
    assert await tracks_repo.count_tracks(user_id) == 1

    by_unique = await tracks_repo.get_track_by_unique_id(user_id, "same-unique")
    assert by_unique is not None
    assert by_unique["id"] == first["id"]


async def test_move_track_between_folders(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Трек переносится в папку и обратно «без папки»."""
    source = await folders_repo.create_folder(user_id, "Входящие")
    target = await folders_repo.create_folder(user_id, "Кино")
    track = await track_factory("Пачка сигарет", folder_id=source["id"])

    moved = await tracks_repo.move_track(user_id, track["id"], target["id"])
    assert moved is not None
    assert moved["folder_id"] == target["id"]
    assert moved["folder_name"] == "Кино"

    in_target = await tracks_repo.list_tracks(user_id, folder_id=target["id"])
    assert [item["id"] for item in in_target] == [track["id"]]

    detached = await tracks_repo.move_track(user_id, track["id"], None)
    assert detached is not None
    assert detached["folder_id"] is None

    without_folder = await tracks_repo.list_tracks(user_id, folder_id=None)
    assert [item["id"] for item in without_folder] == [track["id"]]


async def test_move_tracks_bulk(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Массовый перенос возвращает число перемещённых треков."""
    folder = await folders_repo.create_folder(user_id, "Сборник")
    tracks = [await track_factory(f"Трек {index}") for index in range(1, 4)]

    moved = await tracks_repo.move_tracks(
        user_id, [track["id"] for track in tracks], folder["id"]
    )
    assert moved == 3
    assert await tracks_repo.count_tracks(user_id, folder_id=folder["id"]) == 3


async def test_get_tracks_by_ids_keeps_order(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Порядок треков совпадает с порядком переданных идентификаторов."""
    first = await track_factory("Первый")
    second = await track_factory("Второй")
    third = await track_factory("Третий")

    wanted = [third["id"], first["id"], second["id"]]
    found = await tracks_repo.get_tracks_by_ids(user_id, wanted)
    assert [item["id"] for item in found] == wanted


async def test_delete_track_returns_deleted_row(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Удалённый трек возвращается целиком — данные нужны для чистки канала."""
    track = await track_factory("Ненужный")

    deleted = await tracks_repo.delete_track(user_id, track["id"])
    assert deleted is not None
    assert deleted["id"] == track["id"]
    assert await tracks_repo.get_track(user_id, track["id"]) is None
    assert await tracks_repo.delete_track(user_id, track["id"]) is None


# ---------------------------------------------------------------------------
# Избранное
# ---------------------------------------------------------------------------


async def test_favourites_toggle(
    user: dict, user_id: int, track_factory: TrackFactory
) -> None:
    """Переключение избранного возвращает НОВОЕ состояние."""
    track = await track_factory("Кукушка")

    assert await favourites_repo.is_favourite(user_id, track["id"]) is False

    assert await favourites_repo.toggle(user_id, track["id"]) is True
    assert await favourites_repo.is_favourite(user_id, track["id"]) is True
    assert await favourites_repo.count(user_id) == 1

    listed = await favourites_repo.list_favourites(user_id)
    assert [item["id"] for item in listed] == [track["id"]]
    assert listed[0]["is_favourite"] == 1

    assert await favourites_repo.toggle(user_id, track["id"]) is False
    assert await favourites_repo.is_favourite(user_id, track["id"]) is False
    assert await favourites_repo.count(user_id) == 0


async def test_favourites_ignore_foreign_track(user: dict, user_id: int) -> None:
    """Чужой (несуществующий) трек в избранное не попадает."""
    assert await favourites_repo.add(user_id, 999_999) is False
    assert await favourites_repo.toggle(user_id, 999_999) is False


# ---------------------------------------------------------------------------
# Плейлисты
# ---------------------------------------------------------------------------


@pytest.fixture
async def playlist_with_tracks(
    user: dict, user_id: int, track_factory: TrackFactory
) -> tuple[dict, list[dict]]:
    """Плейлист «Любимое» с тремя треками в порядке добавления."""
    playlist = await playlists_repo.create_playlist(user_id, "Любимое", "Для тестов")
    tracks = [await track_factory(f"Трек {index}") for index in range(1, 4)]
    for track in tracks:
        assert await playlists_repo.add_track(user_id, playlist["id"], track["id"]) is True
    return playlist, tracks


async def test_playlist_add_track(
    user_id: int, playlist_with_tracks: tuple[dict, list[dict]]
) -> None:
    """Треки добавляются в конец, повторное добавление отклоняется."""
    playlist, tracks = playlist_with_tracks

    stored = await playlists_repo.playlist_tracks(user_id, playlist["id"])
    assert [item["id"] for item in stored] == [track["id"] for track in tracks]
    assert [item["position"] for item in stored] == [1, 2, 3]

    assert await playlists_repo.add_track(user_id, playlist["id"], tracks[0]["id"]) is False

    detail = await playlists_repo.get_playlist(user_id, playlist["id"])
    assert detail is not None
    assert detail["track_count"] == 3
    assert detail["total_duration"] == sum(track["duration"] for track in tracks)


async def test_playlist_remove_track_renumbers_positions(
    user_id: int, playlist_with_tracks: tuple[dict, list[dict]]
) -> None:
    """После удаления позиции пересчитываются как 1..N без пропусков."""
    playlist, tracks = playlist_with_tracks

    assert await playlists_repo.remove_track(user_id, playlist["id"], tracks[0]["id"]) is True

    stored = await playlists_repo.playlist_tracks(user_id, playlist["id"])
    assert [item["id"] for item in stored] == [tracks[1]["id"], tracks[2]["id"]]
    assert [item["position"] for item in stored] == [1, 2]

    assert await playlists_repo.remove_track(user_id, playlist["id"], tracks[0]["id"]) is False


async def test_playlist_reorder(
    user_id: int, playlist_with_tracks: tuple[dict, list[dict]]
) -> None:
    """Корректный порядок применяется, неверный состав отклоняется без изменений."""
    playlist, tracks = playlist_with_tracks
    reversed_ids = [track["id"] for track in reversed(tracks)]

    assert await playlists_repo.reorder(user_id, playlist["id"], reversed_ids) is True
    stored = await playlists_repo.playlist_tracks(user_id, playlist["id"])
    assert [item["id"] for item in stored] == reversed_ids
    assert [item["position"] for item in stored] == [1, 2, 3]

    # Неполный состав — порядок не меняется.
    assert await playlists_repo.reorder(user_id, playlist["id"], reversed_ids[:2]) is False
    # Лишний идентификатор — тоже отказ.
    assert (
        await playlists_repo.reorder(user_id, playlist["id"], [*reversed_ids, 999_999])
        is False
    )

    unchanged = await playlists_repo.playlist_tracks(user_id, playlist["id"])
    assert [item["id"] for item in unchanged] == reversed_ids


async def test_playlist_move_track_position(
    user_id: int, playlist_with_tracks: tuple[dict, list[dict]]
) -> None:
    """Перемещение трека на конкретную позицию сдвигает остальные."""
    playlist, tracks = playlist_with_tracks

    assert (
        await playlists_repo.move_track_position(
            user_id, playlist["id"], tracks[2]["id"], 1
        )
        is True
    )
    stored = await playlists_repo.playlist_tracks(user_id, playlist["id"])
    assert [item["id"] for item in stored] == [
        tracks[2]["id"],
        tracks[0]["id"],
        tracks[1]["id"],
    ]


async def test_playlist_unique_name_and_delete(user: dict, user_id: int) -> None:
    """Название плейлиста уникально; удаление снимает плейлист из списка."""
    playlist = await playlists_repo.create_playlist(user_id, "Дорожный")

    with pytest.raises(ValidationError):
        await playlists_repo.create_playlist(user_id, "Дорожный")

    assert len(await playlists_repo.list_playlists(user_id)) == 1
    assert await playlists_repo.delete_playlist(user_id, playlist["id"]) is True
    assert await playlists_repo.list_playlists(user_id) == []
