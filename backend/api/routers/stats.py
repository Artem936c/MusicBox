"""API статистики прослушиваний: сводка, пять разделов и счётчики.

Границы разделов (порог «часто», диапазон «редко») берутся из настроек
пользователя, если параметры запроса не заданы явно.

Статистика — это раздел «Треки»: все маршруты отдают только аудио
(ARCHITECTURE-V2, п. 1.3). Файлы раздела «Другое» (документы, видео,
кружочки, голосовые) не входят ни в разделы, ни в счётчики, поэтому выдачу
можно без дополнительной фильтрации класть в очередь плеера Mini App.
Фильтр по ``file_type`` выполняется в backend.db.repositories.stats.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from backend.api import schemas
from backend.api.deps import CurrentUser
from backend.db.repositories import stats as stats_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["stats"])

# Границы параметров пагинации (совпадают с репозиторием статистики).
MAX_LIMIT = 200
MAX_OVERVIEW_LIMIT = 50
# Разумный потолок для порогов прослушиваний.
MAX_COUNT_VALUE = 1_000_000


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


def _bad_request(exc: ValidationError) -> HTTPException:
    """Ошибка данных репозитория → 400 с русским текстом."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/overview", response_model=schemas.StatsOverviewOut, summary="Сводка статистики"
)
async def overview(
    user: CurrentUser,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_OVERVIEW_LIMIT, description="Сколько треков в каждом разделе"),
    ] = 10,
) -> schemas.StatsOverviewOut:
    """Счётчики библиотеки и пять разделов с превью треков."""
    user_id = _user_id(user)
    try:
        data = await stats_repo.overview(user_id, limit=limit)
    except ValidationError as exc:
        raise _bad_request(exc) from exc

    sections = [
        schemas.SectionOut(
            key=section.get("key", ""),
            title=section.get("title", ""),
            count=int(section.get("count") or 0),
            items=schemas.tracks_to_out(section.get("items") or [], user_id),
        )
        for section in data.get("sections") or []
    ]
    counts_data = {
        key: int(value) for key, value in (data.get("counts") or {}).items()
    }
    return schemas.StatsOverviewOut(counts=counts_data, sections=sections)


@router.get(
    "/recent", response_model=list[schemas.TrackOut], summary="Недавно добавленные"
)
async def recent(
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.TrackOut]:
    """Треки в порядке добавления, от новых к старым."""
    user_id = _user_id(user)
    try:
        tracks = await stats_repo.recent(user_id, limit=limit, offset=offset)
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return schemas.tracks_to_out(tracks, user_id)


@router.get(
    "/unplayed", response_model=list[schemas.TrackOut], summary="Ни разу не проигранные"
)
async def unplayed(
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.TrackOut]:
    """Треки с нулевым числом прослушиваний."""
    user_id = _user_id(user)
    try:
        tracks = await stats_repo.unplayed(user_id, limit=limit, offset=offset)
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return schemas.tracks_to_out(tracks, user_id)


@router.get(
    "/frequent", response_model=list[schemas.TrackOut], summary="Часто прослушиваемые"
)
async def frequent(
    user: CurrentUser,
    threshold: Annotated[
        int | None,
        Query(
            ge=1,
            le=MAX_COUNT_VALUE,
            description="Порог прослушиваний; по умолчанию — из настроек (обычно 10)",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.TrackOut]:
    """Треки, прослушанные не меньше заданного числа раз."""
    user_id = _user_id(user)
    try:
        tracks = await stats_repo.frequent(
            user_id, threshold=threshold, limit=limit, offset=offset
        )
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return schemas.tracks_to_out(tracks, user_id)


@router.get(
    "/rare", response_model=list[schemas.TrackOut], summary="Редко прослушиваемые"
)
async def rare(
    user: CurrentUser,
    min_count: Annotated[
        int | None,
        Query(
            ge=0,
            le=MAX_COUNT_VALUE,
            description="Нижняя граница прослушиваний; по умолчанию — из настроек (обычно 1)",
        ),
    ] = None,
    max_count: Annotated[
        int | None,
        Query(
            ge=0,
            le=MAX_COUNT_VALUE,
            description="Верхняя граница прослушиваний; по умолчанию — из настроек (обычно 5)",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.TrackOut]:
    """Треки, число прослушиваний которых попадает в заданный диапазон."""
    user_id = _user_id(user)
    if min_count is not None and max_count is not None and max_count < min_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Максимальное число прослушиваний не может быть меньше минимального",
        )
    try:
        tracks = await stats_repo.rare(
            user_id,
            min_count=min_count,
            max_count=max_count,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return schemas.tracks_to_out(tracks, user_id)


@router.get(
    "/top", response_model=list[schemas.TrackOut], summary="Самые часто прослушиваемые"
)
async def top(
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.TrackOut]:
    """Треки с наибольшим числом прослушиваний (play_count > 0)."""
    user_id = _user_id(user)
    try:
        tracks = await stats_repo.top(user_id, limit=limit, offset=offset)
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return schemas.tracks_to_out(tracks, user_id)


@router.get("/counts", summary="Счётчики разделов")
async def counts(user: CurrentUser) -> dict[str, int]:
    """Числа треков по разделам, избранное, папки и исполнители.

    Считается только аудио: файлы раздела «Другое» в ``total`` и в счётчики
    разделов не входят — их число берётся отдельным запросом к /other.
    """
    user_id = _user_id(user)
    try:
        data = await stats_repo.counts(user_id)
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    return {key: int(value) for key, value in data.items()}
