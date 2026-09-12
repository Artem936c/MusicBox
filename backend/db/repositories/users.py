"""Репозиторий пользователей и их персональных настроек."""

from __future__ import annotations

import logging
from typing import Any

from backend.config import settings
from backend.db.database import db
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

# Белый список полей, которые разрешено менять через update_settings.
SETTINGS_FIELDS: tuple[str, ...] = (
    "auto_sort_enabled",
    "frequent_threshold",
    "rare_min",
    "rare_max",
    "fuzzy_threshold",
)

# Разумный верхний предел для счётчиков прослушиваний (защита от абсурдных значений).
MAX_COUNT_VALUE = 1_000_000

USER_COLUMNS = """
    user_id, username, first_name, last_name, language_code,
    is_admin, created_at, last_seen_at
"""


# --- вспомогательные функции ------------------------------------------------


def _user_to_dict(row: dict | None) -> dict | None:
    """Приводит строку пользователя к удобному виду (is_admin -> bool)."""
    if row is None:
        return None
    data = dict(row)
    data["is_admin"] = bool(data.get("is_admin", 0))
    return data


def _settings_to_dict(row: dict) -> dict:
    """Приводит строку настроек к удобному виду (auto_sort_enabled -> bool)."""
    data = dict(row)
    data["auto_sort_enabled"] = bool(data.get("auto_sort_enabled", 1))
    for key in ("frequent_threshold", "rare_min", "rare_max", "fuzzy_threshold"):
        if data.get(key) is not None:
            data[key] = int(data[key])
    return data


def _to_bool(value: Any) -> bool:
    """Мягкое приведение значения к bool (принимает bool/int/строку)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "да", "вкл"}:
            return True
        if normalized in {"0", "false", "no", "off", "нет", "выкл"}:
            return False
    raise ValidationError("Некорректное значение для параметра «автосортировка»")


def _to_int(field: str, value: Any) -> int:
    """Мягкое приведение значения к int с понятной ошибкой."""
    if isinstance(value, bool):
        raise ValidationError(f"Некорректное числовое значение для параметра «{field}»")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError:
            pass
    raise ValidationError(f"Некорректное числовое значение для параметра «{field}»")


def _validate_settings(values: dict[str, int]) -> None:
    """Проверяет диапазоны настроек, бросает ValidationError с русским текстом."""
    frequent_threshold = values["frequent_threshold"]
    rare_min = values["rare_min"]
    rare_max = values["rare_max"]
    fuzzy_threshold = values["fuzzy_threshold"]

    if frequent_threshold < 1:
        raise ValidationError(
            "Порог «часто прослушиваемых» должен быть не меньше 1"
        )
    if frequent_threshold > MAX_COUNT_VALUE:
        raise ValidationError(
            f"Порог «часто прослушиваемых» слишком большой (максимум {MAX_COUNT_VALUE})"
        )
    if rare_min < 1:
        raise ValidationError(
            "Минимальное число прослушиваний для «редко прослушиваемых» должно быть не меньше 1"
        )
    if rare_max < rare_min:
        raise ValidationError(
            "Максимальное число прослушиваний не может быть меньше минимального"
        )
    if rare_max > MAX_COUNT_VALUE:
        raise ValidationError(
            f"Максимальное число прослушиваний слишком большое (максимум {MAX_COUNT_VALUE})"
        )
    if not 0 <= fuzzy_threshold <= 100:
        raise ValidationError("Порог нечёткого поиска должен быть от 0 до 100")


def _default_settings_values() -> dict[str, int]:
    """Дефолты настроек пользователя из конфигурации приложения."""
    values = {
        "auto_sort_enabled": 1,
        "frequent_threshold": int(getattr(settings, "frequent_threshold", 10) or 10),
        "rare_min": int(getattr(settings, "rare_min", 1) or 1),
        "rare_max": int(getattr(settings, "rare_max", 5) or 5),
        "fuzzy_threshold": int(getattr(settings, "fuzzy_threshold", 60)),
    }
    try:
        _validate_settings(values)
    except ValidationError:
        # Некорректные значения в .env не должны ломать создание пользователя.
        logger.warning(
            "Некорректные дефолты настроек в конфигурации, используются встроенные значения"
        )
        values = {
            "auto_sort_enabled": 1,
            "frequent_threshold": 10,
            "rare_min": 1,
            "rare_max": 5,
            "fuzzy_threshold": 60,
        }
    return values


async def _ensure_settings_row(user_id: int) -> None:
    """Создаёт строку user_settings с дефолтами, если её ещё нет."""
    defaults = _default_settings_values()
    await db.execute(
        """
        INSERT INTO user_settings (
            user_id, auto_sort_enabled, frequent_threshold, rare_min, rare_max, fuzzy_threshold
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO NOTHING
        """,
        (
            user_id,
            defaults["auto_sort_enabled"],
            defaults["frequent_threshold"],
            defaults["rare_min"],
            defaults["rare_max"],
            defaults["fuzzy_threshold"],
        ),
    )


async def _ensure_user_row(user_id: int) -> None:
    """Гарантирует наличие строки в users (нужно из-за внешнего ключа user_settings)."""
    await db.execute(
        "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )


# --- публичный API ----------------------------------------------------------


async def ensure_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> dict:
    """Создаёт пользователя или обновляет его профиль. Всегда возвращает dict пользователя."""
    await db.execute(
        """
        INSERT INTO users (user_id, username, first_name, last_name, language_code)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username      = COALESCE(excluded.username, users.username),
            first_name    = COALESCE(excluded.first_name, users.first_name),
            last_name     = COALESCE(excluded.last_name, users.last_name),
            language_code = COALESCE(excluded.language_code, users.language_code),
            last_seen_at  = CURRENT_TIMESTAMP
        """,
        (user_id, username, first_name, last_name, language_code),
    )
    await _ensure_settings_row(user_id)

    row = await db.fetch_one(
        f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?",
        (user_id,),
    )
    user = _user_to_dict(row)
    if user is None:
        # Практически недостижимо: строка только что была вставлена.
        logger.error("Не удалось прочитать пользователя %s после UPSERT", user_id)
        raise ValidationError("Не удалось создать пользователя")
    return user


async def get_user(user_id: int) -> dict | None:
    """Возвращает пользователя или None."""
    row = await db.fetch_one(
        f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?",
        (user_id,),
    )
    return _user_to_dict(row)


async def touch_user(user_id: int) -> None:
    """Обновляет отметку последней активности пользователя."""
    await db.execute(
        "UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE user_id = ?",
        (user_id,),
    )


async def get_settings(user_id: int) -> dict:
    """Возвращает настройки пользователя, создавая строку с дефолтами при отсутствии."""
    row = await db.fetch_one(
        "SELECT * FROM user_settings WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        await _ensure_user_row(user_id)
        await _ensure_settings_row(user_id)
        row = await db.fetch_one(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
    if row is None:
        # Строка не создалась — отдаём дефолты, чтобы не ронять запрос пользователя.
        logger.error("Не удалось создать настройки для пользователя %s", user_id)
        row = {"user_id": user_id, **_default_settings_values(), "updated_at": None}
    return _settings_to_dict(row)


async def update_settings(user_id: int, **fields: Any) -> dict:
    """Обновляет настройки по белому списку полей с проверкой диапазонов."""
    current = await get_settings(user_id)

    updates: dict[str, int] = {}
    for key, value in fields.items():
        if key not in SETTINGS_FIELDS:
            logger.debug("Поле настроек %r не поддерживается и пропущено", key)
            continue
        if value is None:
            continue
        if key == "auto_sort_enabled":
            updates[key] = 1 if _to_bool(value) else 0
        else:
            updates[key] = _to_int(key, value)

    if not updates:
        return current

    merged: dict[str, int] = {
        "auto_sort_enabled": 1 if current["auto_sort_enabled"] else 0,
        "frequent_threshold": int(current["frequent_threshold"]),
        "rare_min": int(current["rare_min"]),
        "rare_max": int(current["rare_max"]),
        "fuzzy_threshold": int(current["fuzzy_threshold"]),
    }
    merged.update(updates)
    _validate_settings(merged)

    assignments = ", ".join(f"{key} = ?" for key in updates)
    params: list[Any] = [updates[key] for key in updates]
    params.append(user_id)
    await db.execute(
        f"UPDATE user_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
        params,
    )
    logger.info("Настройки пользователя %s обновлены: %s", user_id, ", ".join(updates))
    return await get_settings(user_id)


async def toggle_auto_sort(user_id: int) -> dict:
    """Переключает автосортировку и возвращает обновлённые настройки."""
    current = await get_settings(user_id)
    return await update_settings(
        user_id, auto_sort_enabled=not bool(current["auto_sort_enabled"])
    )
