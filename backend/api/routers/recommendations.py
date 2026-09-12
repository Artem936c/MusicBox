"""API рекомендаций исполнителей (раздел 4 контракта V2).

Роутер тонкий: весь подбор выполняет :func:`backend.services.recommendations.build`
по данным самой базы MusicBox (прослушивания, жанры, совпадения вкусов). Внешние
сервисы не опрашиваются, несуществующие исполнители не выдумываются.

Ответ — две непересекающиеся категории («Популярные» и «Менее известные»),
честный `shortfall` (сколько позиций не удалось набрать) и русское пояснение
`note`, если рекомендации пришлось добить исполнителями из библиотеки самого
пользователя или если слушать он ещё ничего не начинал.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser
from backend.services import recommendations as reco_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

#: Границы limit берём из сервиса, чтобы они не разъезжались с алгоритмом.
DEFAULT_LIMIT: Final[int] = reco_service.DEFAULT_LIMIT
MAX_LIMIT: Final[int] = reco_service.MAX_LIMIT

USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"


# ===========================================================================
# Схемы
# ===========================================================================


class RecommendationOut(BaseModel):
    """Один рекомендованный исполнитель."""

    name: str
    normalized_name: str
    total_plays: int = 0
    listeners: int = 0
    reason: str = ""
    local_artist_id: int | None = None
    sample_track_ids: list[int] = Field(default_factory=list)


class ShortfallOut(BaseModel):
    """Сколько позиций не удалось набрать в каждой категории."""

    popular: int = 0
    underground: int = 0


class RecommendationsOut(BaseModel):
    """Ответ `GET /recommendations`."""

    popular: list[RecommendationOut] = Field(default_factory=list)
    underground: list[RecommendationOut] = Field(default_factory=list)
    shortfall: ShortfallOut = Field(default_factory=ShortfallOut)
    note: str | None = None


# ===========================================================================
# Вспомогательные функции
# ===========================================================================


def _user_id(user: dict) -> int:
    """Идентификатор пользователя из зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %r", user)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=USER_UNKNOWN
        )
    return int(raw)


def _as_int(value: Any, default: int = 0) -> int:
    """Безопасное приведение к int (None и мусор -> default)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_dict(item: Any) -> dict[str, Any]:
    """Recommendation (dataclass) или dict -> dict."""
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(item, dict):
        return dict(item)
    logger.warning("Неожиданный тип рекомендации: %r", type(item))
    return {}


def _track_ids(value: Any) -> list[int]:
    """Идентификаторы треков-примеров (мусор молча отбрасывается)."""
    if not value:
        return []
    ids: list[int] = []
    for raw in value:
        track_id = _as_int(raw)
        if track_id > 0:
            ids.append(track_id)
    return ids


def _recommendation_out(item: Any) -> RecommendationOut | None:
    """Собирает RecommendationOut; None — если у записи нет имени."""
    data = _as_dict(item)
    name = str(data.get("name") or "").strip()
    if not name:
        logger.warning("Рекомендация без имени пропущена: %r", data)
        return None
    local_raw = data.get("local_artist_id")
    local_artist_id = _as_int(local_raw) if local_raw is not None else None
    return RecommendationOut(
        name=name,
        normalized_name=str(data.get("normalized_name") or "").strip() or name,
        total_plays=_as_int(data.get("total_plays")),
        listeners=_as_int(data.get("listeners")),
        reason=str(data.get("reason") or ""),
        local_artist_id=local_artist_id or None,
        sample_track_ids=_track_ids(data.get("sample_track_ids")),
    )


def _recommendations_out(items: Any) -> list[RecommendationOut]:
    """Список рекомендаций без «пустых» записей."""
    result: list[RecommendationOut] = []
    for item in items or []:
        converted = _recommendation_out(item)
        if converted is not None:
            result.append(converted)
    return result


def _shortfall_out(value: Any, limit: int) -> ShortfallOut:
    """Разбор `shortfall`; при отсутствии данных считаем, что не хватает всего."""
    if not isinstance(value, dict):
        logger.warning("Некорректный shortfall от сервиса рекомендаций: %r", value)
        return ShortfallOut(popular=limit, underground=limit)
    return ShortfallOut(
        popular=max(0, _as_int(value.get("popular"))),
        underground=max(0, _as_int(value.get("underground"))),
    )


# ===========================================================================
# Маршруты
# ===========================================================================


@router.get(
    "",
    response_model=RecommendationsOut,
    summary="Рекомендации исполнителей",
)
@router.get("/", response_model=RecommendationsOut, include_in_schema=False)
async def get_recommendations(
    user: CurrentUser,
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Сколько исполнителей вернуть в каждой категории",
    ),
) -> RecommendationsOut:
    """Две категории рекомендаций, честный `shortfall` и пояснение `note`.

    Если пользователь ещё ничего не слушал, обе категории пустые, а в `note`
    приходит подсказка: сначала нужно послушать несколько треков.
    """
    user_id = _user_id(user)
    result = await reco_service.build(user_id, limit=limit)

    note_raw = result.get("note")
    response = RecommendationsOut(
        popular=_recommendations_out(result.get("popular")),
        underground=_recommendations_out(result.get("underground")),
        shortfall=_shortfall_out(result.get("shortfall"), limit),
        note=str(note_raw) if note_raw else None,
    )
    logger.info(
        "Рекомендации для %s: «%s» — %s, «%s» — %s (limit=%s)",
        user_id,
        reco_service.POPULAR_LABEL,
        len(response.popular),
        reco_service.UNDERGROUND_LABEL,
        len(response.underground),
        limit,
    )
    return response


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "RecommendationOut",
    "RecommendationsOut",
    "ShortfallOut",
    "router",
]
