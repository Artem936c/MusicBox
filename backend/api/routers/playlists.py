"""API плейлистов: CRUD, состав и порядок треков (drag-and-drop).

Все изменяющие эндпоинты возвращают актуальный ``PlaylistDetailOut`` —
плейлист вместе с треками в порядке ``position``, чтобы Mini App мог
сразу перерисовать список без дополнительного запроса.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from backend.api import schemas
from backend.api.deps import CurrentUser
from backend.db.repositories import playlists as playlists_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playlists", tags=["playlists"])

# Русские сообщения об ошибках (видны пользователю).
PLAYLIST_NOT_FOUND = "Плейлист не найден"
TRACK_NOT_IN_PLAYLIST = "Трек не найден в плейлисте"
ORDER_MISMATCH = "Список треков не совпадает с составом плейлиста"


def _user_id(user: dict) -> int:
    """Идентификатор пользователя из зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %r", user)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось определить пользователя",
        )
    return int(raw)


def _playlist_payload(playlist: dict) -> dict[str, Any]:
    """Поля плейлиста для PlaylistOut / PlaylistDetailOut."""
    return {
        "id": int(playlist["id"]),
        "name": playlist.get("name") or "",
        "description": playlist.get("description"),
        "track_count": int(playlist.get("track_count") or 0),
        "total_duration": int(playlist.get("total_duration") or 0),
        "created_at": playlist.get("created_at"),
        "updated_at": playlist.get("updated_at"),
    }


def _playlist_out(playlist: dict) -> schemas.PlaylistOut:
    """Собирает PlaylistOut из dict репозитория."""
    return schemas.PlaylistOut(**_playlist_payload(playlist))


def _tracks_out(tracks: list[dict], user_id: int) -> list[schemas.TrackOut]:
    """Треки плейлиста с гарантированно заполненным полем position."""
    items = schemas.tracks_to_out(tracks, user_id)
    result: list[schemas.TrackOut] = []
    for source, item in zip(tracks, items):
        position = source.get("position")
        if position is not None and getattr(item, "position", None) is None:
            item = item.model_copy(update={"position": int(position)})
        result.append(item)
    return result


async def _detail(user_id: int, playlist_id: int) -> schemas.PlaylistDetailOut:
    """Актуальное состояние плейлиста: сам плейлист + треки по позициям."""
    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PLAYLIST_NOT_FOUND
        )
    tracks = await playlists_repo.playlist_tracks(user_id, playlist_id)
    return schemas.PlaylistDetailOut(
        **_playlist_payload(playlist),
        tracks=_tracks_out(tracks, user_id),
    )


async def _require_playlist(user_id: int, playlist_id: int) -> dict:
    """Возвращает плейлист пользователя или 404."""
    playlist = await playlists_repo.get_playlist(user_id, playlist_id)
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PLAYLIST_NOT_FOUND
        )
    return playlist


@router.get("", response_model=list[schemas.PlaylistOut], summary="Список плейлистов")
async def list_playlists(user: CurrentUser) -> list[schemas.PlaylistOut]:
    """Все плейлисты пользователя в алфавитном порядке."""
    user_id = _user_id(user)
    playlists = await playlists_repo.list_playlists(user_id)
    return [_playlist_out(item) for item in playlists]


@router.post(
    "",
    response_model=schemas.PlaylistOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать плейлист",
)
async def create_playlist(
    payload: schemas.PlaylistCreateIn, user: CurrentUser
) -> schemas.PlaylistOut:
    """Создаёт новый плейлист. Название должно быть уникальным."""
    user_id = _user_id(user)
    try:
        playlist = await playlists_repo.create_playlist(
            user_id, payload.name, payload.description
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    logger.info("Пользователь %s создал плейлист %s", user_id, playlist.get("id"))
    return _playlist_out(playlist)


@router.get(
    "/{playlist_id}",
    response_model=schemas.PlaylistDetailOut,
    summary="Плейлист с треками",
)
async def get_playlist(playlist_id: int, user: CurrentUser) -> schemas.PlaylistDetailOut:
    """Плейлист вместе с треками в порядке позиций."""
    return await _detail(_user_id(user), playlist_id)


@router.patch(
    "/{playlist_id}", response_model=schemas.PlaylistOut, summary="Изменить плейлист"
)
async def update_playlist(
    playlist_id: int, payload: schemas.PlaylistUpdateIn, user: CurrentUser
) -> schemas.PlaylistOut:
    """Меняет название и/или описание плейлиста."""
    user_id = _user_id(user)
    try:
        playlist = await playlists_repo.update_playlist(
            user_id,
            playlist_id,
            name=payload.name,
            description=payload.description,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PLAYLIST_NOT_FOUND
        )
    return _playlist_out(playlist)


@router.delete("/{playlist_id}", summary="Удалить плейлист")
async def delete_playlist(playlist_id: int, user: CurrentUser) -> dict[str, bool]:
    """Удаляет плейлист вместе с его составом (треки остаются в библиотеке)."""
    user_id = _user_id(user)
    deleted = await playlists_repo.delete_playlist(user_id, playlist_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=PLAYLIST_NOT_FOUND
        )
    logger.info("Пользователь %s удалил плейлист %s", user_id, playlist_id)
    return {"ok": True}


@router.post(
    "/{playlist_id}/tracks",
    response_model=schemas.PlaylistDetailOut,
    summary="Добавить треки в плейлист",
)
async def add_tracks(
    playlist_id: int, payload: schemas.PlaylistAddIn, user: CurrentUser
) -> schemas.PlaylistDetailOut:
    """Добавляет треки в конец плейлиста; дубликаты и чужие треки пропускаются."""
    user_id = _user_id(user)
    await _require_playlist(user_id, playlist_id)

    track_ids = list(payload.track_ids or [])
    if not track_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не переданы треки для добавления",
        )

    added = await playlists_repo.add_tracks(user_id, playlist_id, track_ids)
    if not added:
        logger.info(
            "В плейлист %s ничего не добавлено (пользователь %s, запрошено %s треков)",
            playlist_id,
            user_id,
            len(track_ids),
        )
    return await _detail(user_id, playlist_id)


@router.delete(
    "/{playlist_id}/tracks/{track_id}",
    response_model=schemas.PlaylistDetailOut,
    summary="Убрать трек из плейлиста",
)
async def remove_track(
    playlist_id: int, track_id: int, user: CurrentUser
) -> schemas.PlaylistDetailOut:
    """Убирает трек из плейлиста и перенумеровывает оставшиеся позиции."""
    user_id = _user_id(user)
    await _require_playlist(user_id, playlist_id)

    removed = await playlists_repo.remove_track(user_id, playlist_id, track_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_IN_PLAYLIST
        )
    return await _detail(user_id, playlist_id)


@router.put(
    "/{playlist_id}/order",
    response_model=schemas.PlaylistDetailOut,
    summary="Изменить порядок треков",
)
async def reorder_tracks(
    playlist_id: int, payload: schemas.PlaylistOrderIn, user: CurrentUser
) -> schemas.PlaylistDetailOut:
    """Задаёт новый порядок треков (drag-and-drop в Mini App).

    Переданный список должен содержать ровно те же треки, что и плейлист,
    иначе возвращается 400 и порядок не меняется.
    """
    user_id = _user_id(user)
    await _require_playlist(user_id, playlist_id)

    ordered = await playlists_repo.reorder(
        user_id, playlist_id, list(payload.track_ids or [])
    )
    if not ordered:
        logger.warning(
            "Отклонён порядок треков плейлиста %s пользователя %s", playlist_id, user_id
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_MISMATCH
        )
    return await _detail(user_id, playlist_id)
