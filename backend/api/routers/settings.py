"""API персональных настроек пользователя.

Настройки влияют на автосортировку загрузок и на границы разделов
статистики («часто» и «редко прослушиваемые»), а также на порог
нечёткого поиска.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from backend.api import schemas
from backend.api.deps import CurrentUser
from backend.db.repositories import users as users_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# Поля, которые разрешено менять через API (совпадают с белым списком репозитория).
EDITABLE_FIELDS: tuple[str, ...] = (
    "auto_sort_enabled",
    "frequent_threshold",
    "rare_min",
    "rare_max",
    "fuzzy_threshold",
)


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


def _settings_out(row: dict) -> schemas.SettingsOut:
    """Собирает SettingsOut из строки настроек."""
    return schemas.SettingsOut(
        auto_sort_enabled=bool(row.get("auto_sort_enabled")),
        frequent_threshold=int(row.get("frequent_threshold") or 0),
        rare_min=int(row.get("rare_min") or 0),
        rare_max=int(row.get("rare_max") or 0),
        fuzzy_threshold=int(row.get("fuzzy_threshold") or 0),
    )


@router.get("", response_model=schemas.SettingsOut, summary="Настройки пользователя")
async def get_settings(user: CurrentUser) -> schemas.SettingsOut:
    """Текущие настройки; при первом обращении создаются значения по умолчанию."""
    user_id = _user_id(user)
    row = await users_repo.get_settings(user_id)
    return _settings_out(row)


@router.patch("", response_model=schemas.SettingsOut, summary="Изменить настройки")
async def update_settings(
    payload: schemas.SettingsUpdateIn, user: CurrentUser
) -> schemas.SettingsOut:
    """Меняет переданные настройки; непереданные поля остаются прежними."""
    user_id = _user_id(user)

    provided: dict[str, Any] = payload.model_dump(exclude_unset=True)
    updates = {
        key: value
        for key, value in provided.items()
        if key in EDITABLE_FIELDS and value is not None
    }
    if not updates:
        row = await users_repo.get_settings(user_id)
        return _settings_out(row)

    try:
        row = await users_repo.update_settings(user_id, **updates)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    logger.info(
        "Пользователь %s обновил настройки: %s", user_id, ", ".join(sorted(updates))
    )
    return _settings_out(row)
