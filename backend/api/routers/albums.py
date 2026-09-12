"""API альбомов («групп»): список в алфавитном порядке и треки альбома."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Path, Query, status

from backend.api.deps import CurrentUser
from backend.api.schemas import AlbumOut, TrackOut, tracks_to_out
from backend.db.repositories import albums as albums_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/albums", tags=["albums"])

# Сообщения об ошибках (видны пользователю Mini App).
ALBUM_NOT_FOUND: Final[str] = "Альбом не найден"
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"

# Ограничения постраничной выдачи треков альбома.
DEFAULT_TRACKS_LIMIT: Final[int] = 100
MAX_TRACKS_LIMIT: Final[int] = 500


def _current_user_id(user: dict[str, Any]) -> int:
    """Достаёт идентификатор пользователя из данных зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %s", sorted(user))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=USER_UNKNOWN,
        )
    return int(raw)


@router.get("", response_model=list[AlbumOut], summary="Список альбомов")
async def list_albums(user: CurrentUser) -> list[dict[str, Any]]:
    """Альбомы пользователя в алфавитном порядке с исполнителем и числом треков."""
    user_id = _current_user_id(user)
    return await albums_repo.list_albums(user_id)


@router.get("/{album_id}/tracks", response_model=list[TrackOut], summary="Треки альбома")
async def album_tracks(
    user: CurrentUser,
    album_id: Annotated[int, Path(ge=1, description="Идентификатор альбома")],
    limit: Annotated[int, Query(ge=1, le=MAX_TRACKS_LIMIT)] = DEFAULT_TRACKS_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TrackOut]:
    """Треки альбома в порядке добавления (постранично)."""
    user_id = _current_user_id(user)
    album = await albums_repo.get_album(user_id, album_id)
    if album is None:
        logger.info("Пользователь %s запросил несуществующий альбом %s", user_id, album_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ALBUM_NOT_FOUND,
        )
    tracks = await albums_repo.album_tracks(user_id, album_id, limit=limit, offset=offset)
    return tracks_to_out(tracks, user_id)
