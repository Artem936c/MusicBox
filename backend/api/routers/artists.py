"""API исполнителей: список, поиск, карточка, треки и отметка «прослушано».

V2 (раздел 4 контракта ARCHITECTURE-V2):

* ``GET /artists?q=`` — нечёткий поиск по имени (``services.search.search_artists``);
* ``POST /artists`` — ручное создание исполнителя (идемпотентно: тёзка не дублируется);
* ``PATCH /artists/{id}`` — переименование; при совпадении имени репозиторий
  выполняет СЛИЯНИЕ с существующим исполнителем и возвращает того, кто остался;
* ``POST /artists/{id}/folder`` — привязка исполнителя к папке (``null`` — отвязать);
* ``GET /artists/{id}/unplayed`` — треки исполнителя, которые ещё ни разу не играли.

``GET /artists/{id}/tracks`` и ``GET /artists/{id}/unplayed`` считают треки по
УЧАСТИЮ исполнителя (``tracks.artist_id`` либо связь в ``track_artists``) — так
же, как агрегат ``track_count`` в карточке, поэтому оба списка и счётчик на
странице исполнителя согласованы между собой.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser
from backend.api.schemas import ArtistOut, ListenedIn, TrackOut, tracks_to_out
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.errors import ValidationError
from backend.services import search as search_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artists", tags=["artists"])

# Сообщения об ошибках (видны пользователю Mini App).
ARTIST_NOT_FOUND: Final[str] = "Исполнитель не найден"
FOLDER_NOT_FOUND: Final[str] = "Папка не найдена. Обновите список папок."
ARTIST_NOT_CREATED: Final[str] = "Не удалось создать исполнителя. Проверьте имя."
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"

# Ограничения постраничной выдачи треков исполнителя.
DEFAULT_TRACKS_LIMIT: Final[int] = 100
MAX_TRACKS_LIMIT: Final[int] = 500

# Ограничения выдачи поиска по исполнителям.
DEFAULT_SEARCH_LIMIT: Final[int] = 50
MAX_SEARCH_LIMIT: Final[int] = 200

# Максимальная длина имени исполнителя — та же, что в репозитории.
MAX_NAME_LENGTH: Final[int] = artists_repo.MAX_NAME_LENGTH

# Допустимые значения параметра order (см. ORDER_MAP в репозитории треков).
ALLOWED_ORDERS: Final[tuple[str, ...]] = (
    "created_at_desc",
    "created_at_asc",
    "title",
    "artist",
    "play_count_desc",
    "last_played_desc",
)


def _clean_artist_name(value: str) -> str:
    """Проверяет имя исполнителя так же, как это делает репозиторий."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("Имя исполнителя не может быть пустым")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(
            f"Имя исполнителя слишком длинное (максимум {MAX_NAME_LENGTH} символов)"
        )
    return cleaned


def _clean_folder_id(value: int | None) -> int | None:
    """Проверяет идентификатор папки: ``null`` — «без папки»."""
    if value is None:
        return None
    number = int(value)
    if number <= 0:
        raise ValueError("Идентификатор папки должен быть положительным")
    return number


class ArtistCreateIn(BaseModel):
    """Ручное создание исполнителя (ТЗ п. 19)."""

    name: str = Field(..., description="Имя исполнителя")
    folder_id: int | None = Field(default=None, description="Папка исполнителя")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_artist_name(value)

    @field_validator("folder_id")
    @classmethod
    def _validate_folder_id(cls, value: int | None) -> int | None:
        return _clean_folder_id(value)


class ArtistRenameIn(BaseModel):
    """Переименование исполнителя (ТЗ п. 10)."""

    name: str = Field(..., description="Новое имя исполнителя")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_artist_name(value)


class ArtistFolderIn(BaseModel):
    """Привязка исполнителя к папке; ``null`` — отвязать (ТЗ п. 8)."""

    folder_id: int | None = Field(default=None, description="Папка исполнителя")

    @field_validator("folder_id")
    @classmethod
    def _validate_folder_id(cls, value: int | None) -> int | None:
        return _clean_folder_id(value)


def _check_order(order: str) -> str:
    """Проверяет порядок сортировки по белому списку контракта."""
    value = (order or "").strip().lower()
    if value not in ALLOWED_ORDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный порядок сортировки. Допустимые значения: "
            + ", ".join(ALLOWED_ORDERS),
        )
    return value


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


async def _get_artist_or_404(user_id: int, artist_id: int) -> dict[str, Any]:
    """Возвращает исполнителя пользователя либо отдаёт 404 с русским пояснением."""
    artist = await artists_repo.get_artist(user_id, artist_id)
    if artist is None:
        logger.info(
            "Пользователь %s запросил несуществующего исполнителя %s", user_id, artist_id
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ARTIST_NOT_FOUND,
        )
    return artist


async def _ensure_folder_exists(user_id: int, folder_id: int | None) -> None:
    """Проверяет, что папка существует у пользователя (``None`` — «без папки»)."""
    if folder_id is None:
        return
    folder = await folders_repo.get_folder(user_id, int(folder_id))
    if folder is None:
        logger.info("Пользователь %s указал несуществующую папку %s", user_id, folder_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=FOLDER_NOT_FOUND,
        )


@router.get("", response_model=list[ArtistOut], summary="Список исполнителей")
async def list_artists(
    user: CurrentUser,
    only_listened: Annotated[
        bool | None,
        Query(description="true — только прослушанные, false — только непрослушанные"),
    ] = None,
    q: Annotated[
        str | None, Query(description="Нечёткий поиск по имени исполнителя")
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_SEARCH_LIMIT,
            description="Ограничение выдачи; действует только вместе с q",
        ),
    ] = DEFAULT_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Исполнители пользователя с отметкой «прослушано».

    Без ``q`` — весь список в алфавитном порядке (поведение V1). С ``q`` —
    результаты нечёткого поиска по релевантности, не больше ``limit`` штук.
    Фильтр ``only_listened`` работает в обоих случаях.
    """
    user_id = _current_user_id(user)

    needle = (q or "").strip()
    if not needle:
        return await artists_repo.list_artists(user_id, only_listened=only_listened)

    found = await search_service.search_artists(user_id, needle, limit=limit)
    if only_listened is not None:
        found = [
            artist
            for artist in found
            if bool(artist.get("is_listened")) is bool(only_listened)
        ]
    logger.debug(
        "Поиск исполнителей «%s» пользователя %s: найдено %d", needle, user_id, len(found)
    )
    return found


@router.post(
    "",
    response_model=ArtistOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать исполнителя",
)
async def create_artist(
    user: CurrentUser,
    response: Response,
    payload: Annotated[ArtistCreateIn, Body(description="Имя и папка исполнителя")],
) -> dict[str, Any]:
    """Создаёт исполнителя вручную (ТЗ п. 19).

    Операция идемпотентна: если исполнитель с таким именем уже есть, он
    возвращается со статусом 200 и при необходимости перепривязывается к
    указанной папке. Новый исполнитель — 201.
    """
    user_id = _current_user_id(user)
    await _ensure_folder_exists(user_id, payload.folder_id)

    existing = await artists_repo.find_artist_by_name(user_id, payload.name)
    try:
        artist = await artists_repo.ensure_artist(
            user_id, payload.name, folder_id=payload.folder_id
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if artist is None:
        logger.warning(
            "Пользователь %s: исполнитель «%s» не создан", user_id, payload.name
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ARTIST_NOT_CREATED
        )

    if existing is not None:
        response.status_code = status.HTTP_200_OK
        logger.info(
            "Пользователь %s: исполнитель «%s» уже существовал (id=%s)",
            user_id,
            payload.name,
            artist["id"],
        )
    else:
        logger.info(
            "Пользователь %s создал исполнителя «%s» (id=%s)",
            user_id,
            payload.name,
            artist["id"],
        )
    return artist


@router.get("/{artist_id}", response_model=ArtistOut, summary="Карточка исполнителя")
async def get_artist(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
) -> dict[str, Any]:
    """Возвращает одного исполнителя пользователя."""
    user_id = _current_user_id(user)
    return await _get_artist_or_404(user_id, artist_id)


@router.patch(
    "/{artist_id}",
    response_model=ArtistOut,
    summary="Переименовать исполнителя",
)
async def rename_artist(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
    payload: Annotated[ArtistRenameIn, Body(description="Новое имя исполнителя")],
) -> dict[str, Any]:
    """Переименовывает исполнителя (ТЗ п. 10).

    Если у пользователя уже есть тёзка, репозиторий сливает записи: треки,
    связи ``track_artists`` и альбомы переходят к оставшемуся исполнителю, а
    дубликат удаляется. В ответе — тот исполнитель, который остался, поэтому
    его ``id`` может отличаться от запрошенного.
    """
    user_id = _current_user_id(user)
    await _get_artist_or_404(user_id, artist_id)

    try:
        artist = await artists_repo.rename_artist(user_id, artist_id, payload.name)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if artist is None:
        # Исполнителя удалили между проверкой и переименованием.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ARTIST_NOT_FOUND,
        )
    logger.info(
        "Пользователь %s: исполнитель %s переименован в «%s» (итоговый id=%s)",
        user_id,
        artist_id,
        payload.name,
        artist["id"],
    )
    return artist


@router.get(
    "/{artist_id}/tracks",
    response_model=list[TrackOut],
    summary="Треки исполнителя",
)
async def artist_tracks(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
    limit: Annotated[int, Query(ge=1, le=MAX_TRACKS_LIMIT)] = DEFAULT_TRACKS_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    order: Annotated[str, Query(description="Порядок сортировки треков")] = "created_at_desc",
) -> list[TrackOut]:
    """Постраничный список треков исполнителя.

    Как и «непрослушанные» ниже, учитывает и треки, где исполнитель основной,
    и те, где он указан через ``track_artists`` (приглашённый). Поэтому список
    совпадает с агрегатом ``track_count`` из карточки исполнителя.
    """
    user_id = _current_user_id(user)
    order_key = _check_order(order)
    await _get_artist_or_404(user_id, artist_id)
    tracks = await artists_repo.artist_tracks(
        user_id,
        artist_id,
        order=order_key,
        limit=limit,
        offset=offset,
    )
    return tracks_to_out(tracks, user_id)


@router.get(
    "/{artist_id}/unplayed",
    response_model=list[TrackOut],
    summary="Непрослушанные треки исполнителя",
)
async def artist_unplayed(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
    limit: Annotated[int, Query(ge=1, le=MAX_TRACKS_LIMIT)] = DEFAULT_TRACKS_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TrackOut]:
    """Треки исполнителя, которые ещё ни разу не проигрывались (ТЗ п. 2).

    Учитываются и треки, где исполнитель основной, и те, где он указан через
    ``track_artists``; порядок — сначала недавно добавленные.
    """
    user_id = _current_user_id(user)
    await _get_artist_or_404(user_id, artist_id)
    tracks = await artists_repo.artist_unplayed_tracks(
        user_id, artist_id, limit, offset=offset
    )
    return tracks_to_out(tracks, user_id)


@router.post(
    "/{artist_id}/folder",
    response_model=ArtistOut,
    summary="Папка исполнителя",
)
async def set_artist_folder(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
    payload: Annotated[ArtistFolderIn, Body(description="Папка исполнителя")],
) -> dict[str, Any]:
    """Привязывает исполнителя к папке; ``folder_id = null`` — отвязывает (ТЗ п. 8)."""
    user_id = _current_user_id(user)
    await _get_artist_or_404(user_id, artist_id)
    await _ensure_folder_exists(user_id, payload.folder_id)

    try:
        artist = await artists_repo.set_folder(user_id, artist_id, payload.folder_id)
    except ValidationError as exc:
        # Папку могли удалить между проверкой и привязкой.
        logger.warning(
            "Пользователь %s: папка %s недоступна для исполнителя %s (%s)",
            user_id,
            payload.folder_id,
            artist_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=FOLDER_NOT_FOUND
        ) from exc

    if artist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ARTIST_NOT_FOUND,
        )
    logger.info(
        "Пользователь %s: исполнитель %s привязан к папке %s",
        user_id,
        artist_id,
        payload.folder_id,
    )
    return artist


@router.post(
    "/{artist_id}/listened",
    response_model=ArtistOut,
    summary="Отметка «прослушано»",
)
async def set_artist_listened(
    user: CurrentUser,
    artist_id: Annotated[int, Path(ge=1, description="Идентификатор исполнителя")],
    payload: Annotated[ListenedIn | None, Body(description="Желаемое состояние отметки")] = None,
) -> dict[str, Any]:
    """Ставит или снимает отметку «прослушано».

    Тело запроса необязательно: если оно не передано или ``is_listened`` равно
    ``null`` — отметка переключается на противоположную.
    """
    user_id = _current_user_id(user)
    await _get_artist_or_404(user_id, artist_id)

    desired = payload.is_listened if payload is not None else None
    if desired is None:
        artist = await artists_repo.toggle_listened(user_id, artist_id)
    else:
        artist = await artists_repo.set_listened(user_id, artist_id, bool(desired))

    if artist is None:
        # Исполнителя удалили между проверкой и обновлением.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ARTIST_NOT_FOUND,
        )
    logger.info(
        "Пользователь %s: исполнитель %s теперь %s",
        user_id,
        artist_id,
        "прослушан" if artist.get("is_listened") else "не прослушан",
    )
    return artist


__all__ = ["ArtistCreateIn", "ArtistFolderIn", "ArtistRenameIn", "router"]
