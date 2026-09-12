"""Тесты HTTP API: статистика, прослушивания, плейлисты и избранное."""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient

from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)

#: Разделы статистики и их маршруты (проверяются на обоих префиксах).
SECTION_PATHS: tuple[str, ...] = ("top", "recent", "unplayed", "frequent", "rare")


@pytest.fixture
async def library(track_factory: TrackFactory) -> list[dict]:
    """Три трека с числом прослушиваний 0, 4 и 12."""
    return [
        await track_factory("Без прослушиваний", artist="Кино", play_count=0),
        await track_factory("Редкий", artist="Кино", play_count=4),
        await track_factory("Частый", artist="Аквариум", play_count=12),
    ]


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------


async def test_health(client: AsyncClient) -> None:
    """Проверка живости сервиса."""
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_dev_mode_authorizes_without_init_data(
    client: AsyncClient, user_id: int
) -> None:
    """В dev-режиме запрос без initData выполняется от имени dev_user_id."""
    response = await client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["auto_sort_enabled"] is True
    assert body["frequent_threshold"] == 10
    assert body["rare_min"] == 1
    assert body["rare_max"] == 5


# ---------------------------------------------------------------------------
# Статистика: /api/stats/* и /stats/* (двойное монтирование)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["/api", ""])
async def test_stats_sections_available_on_both_mounts(
    client: AsyncClient, library: list[dict], prefix: str
) -> None:
    """Все пять разделов доступны и с префиксом /api, и без него."""
    for key in SECTION_PATHS:
        response = await client.get(f"{prefix}/stats/{key}")
        assert response.status_code == 200, key
        assert isinstance(response.json(), list), key


async def test_stats_counts_same_on_both_mounts(
    client: AsyncClient, library: list[dict]
) -> None:
    """`/api/stats/counts` и `/stats/counts` отдают одинаковые счётчики."""
    with_prefix = await client.get("/api/stats/counts")
    without_prefix = await client.get("/stats/counts")

    assert with_prefix.status_code == 200
    assert without_prefix.status_code == 200
    assert with_prefix.json() == without_prefix.json()

    counts = with_prefix.json()
    assert counts["total"] == 3
    assert counts["unplayed"] == 1
    assert counts["frequent"] == 1
    assert counts["rare"] == 1
    assert counts["top"] == 2
    assert counts["total_plays"] == 16


async def test_stats_boundaries_via_api(
    client: AsyncClient, library: list[dict]
) -> None:
    """Границы разделов сохраняются и на уровне API."""
    unplayed = (await client.get("/api/stats/unplayed")).json()
    assert [item["play_count"] for item in unplayed] == [0]

    frequent = (await client.get("/api/stats/frequent")).json()
    assert [item["play_count"] for item in frequent] == [12]

    rare = (await client.get("/api/stats/rare")).json()
    assert [item["play_count"] for item in rare] == [4]

    top = (await client.get("/api/stats/top")).json()
    assert [item["play_count"] for item in top] == [12, 4]


async def test_stats_overview_structure(
    client: AsyncClient, library: list[dict]
) -> None:
    """Сводка содержит счётчики и пять разделов в порядке контракта."""
    response = await client.get("/api/stats/overview", params={"limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert [section["key"] for section in body["sections"]] == list(SECTION_PATHS)
    assert body["counts"]["total"] == 3

    top_section = body["sections"][0]
    assert top_section["title"] == "Самые часто прослушиваемые"
    assert top_section["count"] == 2

    first_track = top_section["items"][0]
    assert first_track["stream_url"].startswith(
        f"/api/tracks/{first_track['id']}/stream?token="
    )
    assert first_track["duration_label"] == "3:00"


async def test_stats_rare_rejects_wrong_bounds(
    client: AsyncClient, library: list[dict]
) -> None:
    """Верхняя граница меньше нижней — 400 с русским текстом."""
    response = await client.get(
        "/api/stats/rare", params={"min_count": 5, "max_count": 2}
    )

    assert response.status_code == 400
    assert "минимального" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Треки
# ---------------------------------------------------------------------------


async def test_play_increments_play_count(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """POST /tracks/{id}/play увеличивает счётчик в ответе и в последующем GET."""
    track = await track_factory("Восьмиклассница")
    track_id = track["id"]

    played = await client.post(f"/api/tracks/{track_id}/play", json={"source": "web"})
    assert played.status_code == 200
    body = played.json()
    assert body["play_count"] == 1
    assert body["last_played_at"] is not None

    fetched = await client.get(f"/api/tracks/{track_id}")
    assert fetched.status_code == 200
    assert fetched.json()["play_count"] == 1

    # Тело запроса необязательно.
    again = await client.post(f"/api/tracks/{track_id}/play")
    assert again.status_code == 200
    assert again.json()["play_count"] == 2

    top = (await client.get("/api/stats/top")).json()
    assert [item["id"] for item in top] == [track_id]


async def test_play_unknown_track_returns_404(client: AsyncClient, user: dict) -> None:
    """Прослушивание несуществующего трека — 404."""
    response = await client.post("/api/tracks/999999/play")

    assert response.status_code == 404
    assert response.json()["detail"]


async def test_get_unknown_track_returns_404(client: AsyncClient, user: dict) -> None:
    """Карточка несуществующего трека — 404 с русским сообщением."""
    response = await client.get("/api/tracks/999999")

    assert response.status_code == 404
    assert "не найден" in response.json()["detail"].casefold()


async def test_tracks_list_and_folder_filter(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """Список треков фильтруется по папке, созданной через API."""
    created = await client.post("/api/folders", json={"name": "Кино"})
    assert created.status_code == 201
    folder_id = created.json()["id"]

    track = await track_factory("Пачка сигарет")
    moved = await client.post(
        f"/api/tracks/{track['id']}/move", json={"folder_id": folder_id}
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder_id
    assert moved.json()["folder_name"] == "Кино"

    in_folder = await client.get("/api/tracks", params={"folder_id": folder_id})
    assert [item["id"] for item in in_folder.json()] == [track["id"]]

    folder_tracks = await client.get(f"/api/folders/{folder_id}/tracks")
    assert [item["id"] for item in folder_tracks.json()] == [track["id"]]


# ---------------------------------------------------------------------------
# Избранное
# ---------------------------------------------------------------------------


async def test_favourites_via_api(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """Переключение избранного и список избранных треков."""
    track = await track_factory("Кукушка")
    track_id = track["id"]

    toggled = await client.post(f"/api/tracks/{track_id}/favourite")
    assert toggled.status_code == 200
    assert toggled.json() == {"is_favourite": True}

    listed = await client.get("/api/favourites")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [track_id]
    assert listed.json()[0]["is_favourite"] is True

    toggled_back = await client.post(f"/api/tracks/{track_id}/favourite")
    assert toggled_back.json() == {"is_favourite": False}
    assert (await client.get("/api/favourites")).json() == []

    added = await client.post(f"/api/favourites/{track_id}")
    assert added.json() == {"is_favourite": True}

    removed = await client.delete(f"/api/favourites/{track_id}")
    assert removed.json() == {"is_favourite": False}


async def test_favourite_unknown_track_returns_404(
    client: AsyncClient, user: dict
) -> None:
    """Избранное для несуществующего трека — 404."""
    response = await client.post("/api/tracks/999999/favourite")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Плейлисты
# ---------------------------------------------------------------------------


async def test_playlist_order_changes_track_positions(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """PUT /playlists/{id}/order задаёт новый порядок треков."""
    tracks = [await track_factory(f"Трек {index}") for index in range(1, 4)]
    track_ids = [track["id"] for track in tracks]

    created = await client.post(
        "/api/playlists", json={"name": "Дорожный", "description": "В машину"}
    )
    assert created.status_code == 201
    playlist_id = created.json()["id"]

    filled = await client.post(
        f"/api/playlists/{playlist_id}/tracks", json={"track_ids": track_ids}
    )
    assert filled.status_code == 200
    assert [item["id"] for item in filled.json()["tracks"]] == track_ids
    assert filled.json()["track_count"] == 3

    reversed_ids = list(reversed(track_ids))
    reordered = await client.put(
        f"/api/playlists/{playlist_id}/order", json={"track_ids": reversed_ids}
    )
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.json()["tracks"]] == reversed_ids
    assert [item["position"] for item in reordered.json()["tracks"]] == [1, 2, 3]

    stored = await client.get(f"/api/playlists/{playlist_id}")
    assert [item["id"] for item in stored.json()["tracks"]] == reversed_ids


async def test_playlist_order_rejects_wrong_composition(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """Неверный состав в запросе порядка — 400, порядок не меняется."""
    tracks = [await track_factory(f"Трек {index}") for index in range(1, 4)]
    track_ids = [track["id"] for track in tracks]

    playlist_id = (await client.post("/api/playlists", json={"name": "Вечер"})).json()["id"]
    await client.post(
        f"/api/playlists/{playlist_id}/tracks", json={"track_ids": track_ids}
    )

    response = await client.put(
        f"/api/playlists/{playlist_id}/order", json={"track_ids": track_ids[:2]}
    )
    assert response.status_code == 400
    assert response.json()["detail"]

    stored = await client.get(f"/api/playlists/{playlist_id}")
    assert [item["id"] for item in stored.json()["tracks"]] == track_ids


async def test_playlist_remove_track_and_delete(
    client: AsyncClient, track_factory: TrackFactory
) -> None:
    """Удаление трека из плейлиста перенумеровывает позиции, плейлист удаляется."""
    tracks = [await track_factory(f"Трек {index}") for index in range(1, 4)]
    track_ids = [track["id"] for track in tracks]

    playlist_id = (await client.post("/api/playlists", json={"name": "Утро"})).json()["id"]
    await client.post(
        f"/api/playlists/{playlist_id}/tracks", json={"track_ids": track_ids}
    )

    removed = await client.delete(
        f"/api/playlists/{playlist_id}/tracks/{track_ids[0]}"
    )
    assert removed.status_code == 200
    assert [item["id"] for item in removed.json()["tracks"]] == track_ids[1:]
    assert [item["position"] for item in removed.json()["tracks"]] == [1, 2]

    deleted = await client.delete(f"/api/playlists/{playlist_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert (await client.get("/api/playlists")).json() == []
