"""Безопасность Mini App: проверка Telegram initData и подпись потоковых ссылок.

Здесь два независимых механизма:

* `validate_init_data` — проверка подписи `initData`, которую Telegram WebApp
  передаёт фронтенду (HMAC-SHA256 на производном ключе от токена бота);
* `create_stream_token` / `verify_stream_token` — короткоживущие подписанные
  токены для `<audio src>` и обложек: тег `<audio>` не умеет отправлять
  заголовки, поэтому авторизация таких запросов идёт через параметр `?token=`.

Секреты берутся только из `backend.config.settings`, в коде их нет.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl

from backend.config import settings
from backend.errors import AuthError

logger = logging.getLogger(__name__)

#: Соль Telegram для получения секретного ключа из токена бота.
WEBAPP_SECRET_SALT: bytes = b"WebAppData"

#: Поля пользователя, которые возвращает `validate_init_data`.
USER_FIELDS: tuple[str, ...] = ("id", "first_name", "last_name", "username", "language_code")

#: Допустимое расхождение часов клиента и сервера (секунды).
CLOCK_SKEW_SECONDS: int = 300

#: Время жизни потокового токена по умолчанию, если в настройках указан мусор.
DEFAULT_STREAM_TTL: int = 21600

#: Число частей потокового токена: user_id.track_id.exp.signature
STREAM_TOKEN_PARTS: int = 4


def _signatures_equal(expected: str, received: str) -> bool:
    """Сравнить подписи за постоянное время, не падая на не-ASCII символах.

    `hmac.compare_digest` для строковых аргументов требует ASCII в обоих
    операндах и иначе бросает `TypeError` вместо возврата `False`. Подпись
    приходит от клиента как есть, поэтому сравниваем UTF-8 байты: результат
    для корректных hex-подписей тот же, а мусорный ввод даёт `False`
    (и, как следствие, AuthError → 401), а не 500.
    """
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


# ---------------------------------------------------------------------------
# Telegram WebApp initData
# ---------------------------------------------------------------------------


def validate_init_data(init_data: str, bot_token: str, ttl: int) -> dict[str, Any]:
    """Проверить подпись `initData` Telegram WebApp и вернуть данные пользователя.

    Алгоритм Telegram:
    1. `secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`;
    2. `data_check_string` — пары `ключ=значение`, отсортированные по ключу,
       через `\\n`, без поля `hash`;
    3. `HMAC_SHA256(key=secret_key, msg=data_check_string)` в hex сравнивается
       с полем `hash` (постоянное по времени сравнение).

    :param init_data: строка `initData` из Telegram WebApp.
    :param bot_token: токен бота, которому принадлежит Mini App.
    :param ttl: максимальный возраст `auth_date` в секундах (0 или меньше — не проверять).
    :returns: словарь с ключами `id`, `first_name`, `last_name`, `username`, `language_code`.
    :raises AuthError: при любой проблеме с данными авторизации (текст — на русском).
    """
    raw = (init_data or "").strip()
    if not raw:
        raise AuthError(
            "Не переданы данные авторизации Telegram. Откройте приложение через Telegram."
        )

    token = (bot_token or "").strip()
    if not token:
        logger.error("Проверка initData невозможна: в настройках не задан токен бота")
        raise AuthError(
            "Сервер настроен неверно: не задан токен бота. Сообщите администратору."
        )

    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        logger.debug("Не удалось разобрать initData: %s", exc)
        raise AuthError("Данные авторизации Telegram повреждены. Переоткройте приложение.") from exc

    data: dict[str, str] = dict(pairs)
    received_hash = (data.pop("hash", "") or "").strip().lower()
    if not received_hash:
        raise AuthError("В данных авторизации Telegram нет подписи. Переоткройте приложение.")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(WEBAPP_SECRET_SALT, token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not _signatures_equal(calculated_hash, received_hash):
        logger.warning("Отклонены данные Telegram: подпись initData не совпадает")
        raise AuthError(
            "Подпись данных Telegram не совпадает. Переоткройте приложение через бота."
        )

    _check_auth_date(data.get("auth_date"), ttl)
    return _extract_user(data.get("user"))


def _check_auth_date(raw_auth_date: str | None, ttl: int) -> None:
    """Проверить срок годности `auth_date`. ttl <= 0 — проверка отключена."""
    try:
        lifetime = int(ttl)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime <= 0:
        return

    if not raw_auth_date:
        raise AuthError("В данных Telegram нет времени авторизации. Переоткройте приложение.")
    try:
        auth_date = int(raw_auth_date)
    except (TypeError, ValueError) as exc:
        raise AuthError("Некорректное время авторизации Telegram. Переоткройте приложение.") from exc

    age = time.time() - auth_date
    if age > lifetime:
        logger.info("Отклонены данные Telegram: initData устарел на %.0f с", age - lifetime)
        raise AuthError("Сессия Telegram устарела. Закройте и снова откройте приложение.")
    if age < -CLOCK_SKEW_SECONDS:
        logger.warning("Отклонены данные Telegram: auth_date из будущего (%.0f с)", -age)
        raise AuthError("Некорректное время авторизации Telegram. Переоткройте приложение.")


def _extract_user(raw_user: str | None) -> dict[str, Any]:
    """Разобрать JSON-поле `user` из initData в словарь с нужными полями."""
    if not raw_user:
        raise AuthError(
            "В данных Telegram нет сведений о пользователе. Откройте приложение через бота."
        )
    try:
        payload = json.loads(raw_user)
    except (TypeError, ValueError) as exc:
        raise AuthError("Данные пользователя Telegram повреждены. Переоткройте приложение.") from exc

    if not isinstance(payload, dict):
        raise AuthError("Данные пользователя Telegram повреждены. Переоткройте приложение.")

    try:
        user_id = int(payload.get("id"))
    except (TypeError, ValueError) as exc:
        raise AuthError("Не удалось определить ваш Telegram-ID. Переоткройте приложение.") from exc

    if user_id <= 0:
        raise AuthError("Не удалось определить ваш Telegram-ID. Переоткройте приложение.")

    return {
        "id": user_id,
        "first_name": _optional_text(payload.get("first_name")),
        "last_name": _optional_text(payload.get("last_name")),
        "username": _optional_text(payload.get("username")),
        "language_code": _optional_text(payload.get("language_code")),
    }


def _optional_text(value: Any) -> str | None:
    """Привести значение к непустой строке или None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Потоковые токены (аудио и обложки)
# ---------------------------------------------------------------------------


def create_stream_token(user_id: int, track_id: int, ttl: int | None = None) -> str:
    """Создать подписанный токен для потоковой ссылки на трек.

    Формат: ``{user_id}.{track_id}.{expires_at}.{signature}``,
    где ``signature = HMAC_SHA256(settings.secret_key, "{user_id}.{track_id}.{expires_at}")``.

    :param user_id: Telegram-ID владельца трека.
    :param track_id: идентификатор трека.
    :param ttl: время жизни в секундах; по умолчанию `settings.stream_token_ttl`.
    """
    lifetime = _resolve_stream_ttl(ttl)
    expires_at = int(time.time()) + lifetime
    payload = f"{int(user_id)}.{int(track_id)}.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def verify_stream_token(token: str) -> tuple[int, int]:
    """Проверить потоковый токен и вернуть `(user_id, track_id)`.

    :raises AuthError: токен повреждён, подделан или просрочен.
    """
    raw = (token or "").strip()
    if not raw:
        raise AuthError("Ссылка на трек без токена доступа. Обновите страницу приложения.")

    parts = raw.split(".")
    if len(parts) != STREAM_TOKEN_PARTS:
        raise AuthError("Ссылка на трек повреждена. Обновите страницу приложения.")

    raw_user_id, raw_track_id, raw_expires_at, signature = parts
    try:
        user_id = int(raw_user_id)
        track_id = int(raw_track_id)
        expires_at = int(raw_expires_at)
    except (TypeError, ValueError) as exc:
        raise AuthError("Ссылка на трек повреждена. Обновите страницу приложения.") from exc

    expected = _sign(f"{user_id}.{track_id}.{expires_at}")
    if not _signatures_equal(expected, signature.strip().lower()):
        logger.warning("Отклонён потоковый токен с неверной подписью (трек %s)", track_id)
        raise AuthError("Ссылка на трек недействительна. Обновите страницу приложения.")

    if expires_at < int(time.time()):
        raise AuthError("Ссылка на трек устарела. Обновите страницу приложения.")

    return user_id, track_id


def _resolve_stream_ttl(ttl: int | None) -> int:
    """Определить время жизни потокового токена (секунды)."""
    raw = ttl if ttl is not None else getattr(settings, "stream_token_ttl", DEFAULT_STREAM_TTL)
    try:
        lifetime = int(raw)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime <= 0:
        logger.debug("Некорректное время жизни потокового токена, используется значение по умолчанию")
        lifetime = DEFAULT_STREAM_TTL
    return lifetime


def _sign(payload: str) -> str:
    """Подписать строку секретным ключом приложения (hex-строка HMAC-SHA256)."""
    key = (settings.secret_key or "").encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "DEFAULT_STREAM_TTL",
    "STREAM_TOKEN_PARTS",
    "USER_FIELDS",
    "WEBAPP_SECRET_SALT",
    "create_stream_token",
    "validate_init_data",
    "verify_stream_token",
]
