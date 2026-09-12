"""API избранного: список любимых треков, поиск по ним, добавление и удаление.

Переключение (toggle) живёт в роутере треков — ``POST /tracks/{id}/favourite``.
Здесь операции явные и идемпотентные: повторное добавление или удаление
не считается ошибкой и возвращает итоговое состояние.

V2 (контракт `docs/ARCHITECTURE-V2.md`, раздел 4): у ``GET /favourites``
появился параметр ``q`` — нечёткий поиск внутри избранного
(``tracks_repo.search_in(scope="favourites")``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, status

from backend.api.deps import CurrentUser
from backend.api.schemas import TrackOut, tracks_to_out
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/favourites", tags=["favourites"])

TRACK_NOT_FOUND: Final[str] = "Трек не найден. Возможно, он уже удалён."
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"

#: Максимальная длина поискового запроса (совпадает с роутером поиска).
MAX_QUERY_LENGTH: Final[int] = 200

#: Верхняя граница выборки при поиске: репозиторий отдаёт совпадения одним
#: списком без смещения, поэтому страницы нарезаются здесь.
SEARCH_FETCH_CAP: Final[int] = 500


def _user_id(user: dict[str, Any]) -> int:
    """Достаёт идентификатор пользователя из данных зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %s", sorted(user))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=USER_UNKNOWN
        )
    return int(raw)


def _clean_query(value: str | None) -> str:
    """Нормализует поисковый запрос: схлопывает пробелы и режет длину."""
    return " ".join(str(value or "").split())[:MAX_QUERY_LENGTH]


async def _search_favourites(
    user_id: int, query: str, *, limit: int, offset: int
) -> list[dict]:
    """Нечёткий поиск по избранному с нарезкой на страницы.

    ``search_in`` возвращает отсортированный по релевантности список без
    смещения, поэтому страница вырезается здесь. Глубина ограничена
    :data:`SEARCH_FETCH_CAP`: дальше по релевантности всё равно шум.
    """
    fetch_limit = max(1, min(SEARCH_FETCH_CAP, limit + offset))
    try:
        found = await tracks_repo.search_in(
            user_id, query, scope="favourites", limit=fetch_limit
        )
    except ValidationError as exc:  # pragma: no cover — scope задан константой
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if limit + offset > SEARCH_FETCH_CAP:
        logger.info(
            "Поиск по избранному пользователя %s обрезан на %d результатах "
            "(запрошены limit=%d, offset=%d)",
            user_id,
            SEARCH_FETCH_CAP,
            limit,
            offset,
        )
    if offset >= len(found):
        return []
    return found[offset : offset + limit]


async def _ensure_track_exists(user_id: int, track_id: int) -> dict:
    """Проверяет, что трек принадлежит пользователю, иначе — 404."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)
    return track


@router.get("", response_model=list[TrackOut], summary="Избранные треки")
@router.get("/", response_model=list[TrackOut], include_in_schema=False)
async def list_favourites(
    user: CurrentUser,
    q: Annotated[str, Query(description="Поисковый запрос по избранному")] = "",
    limit: int = Query(100, ge=1, le=200, description="Сколько треков вернуть"),
    offset: int = Query(0, ge=0, description="Сколько треков пропустить"),
) -> list[TrackOut]:
    """Возвращает избранные треки пользователя, новые сверху.

    С непустым ``q`` вместо ленты отдаётся нечёткий поиск внутри избранного
    (опечатки и неверная раскладка прощаются), самые релевантные — первыми.
    """
    user_id = _user_id(user)
    query = _clean_query(q)

    if query:
        tracks = await _search_favourites(user_id, query, limit=limit, offset=offset)
        logger.debug(
            "Поиск «%s» по избранному пользователя %s: найдено %d",
            query,
            user_id,
            len(tracks),
        )
    else:
        tracks = await favourites_repo.list_favourites(
            user_id, limit=limit, offset=offset
        )
    return tracks_to_out(tracks, user_id)


@router.post("/{track_id}", summary="Добавить в избранное")
async def add_favourite(user: CurrentUser, track_id: int) -> dict[str, bool]:
    """Добавляет трек в избранное (повторный вызов ничего не меняет)."""
    user_id = _user_id(user)
    await _ensure_track_exists(user_id, track_id)

    added = await favourites_repo.add(user_id, track_id)
    if not added:
        # Трек исчез между проверкой и вставкой — сообщаем честно.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)

    logger.debug("Трек %s добавлен в избранное пользователя %s", track_id, user_id)
    return {"is_favourite": True}


@router.delete("/{track_id}", summary="Убрать из избранного")
async def remove_favourite(user: CurrentUser, track_id: int) -> dict[str, bool]:
    """Убирает трек из избранного (повторный вызов ничего не меняет)."""
    user_id = _user_id(user)
    await _ensure_track_exists(user_id, track_id)

    removed = await favourites_repo.remove(user_id, track_id)
    if not removed:
        logger.debug(
            "Трек %s не был в избранном пользователя %s — удалять нечего", track_id, user_id
        )
    return {"is_favourite": False}


__all__ = ["router"]
