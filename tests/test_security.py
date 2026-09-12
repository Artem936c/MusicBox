"""Тесты безопасности Mini App: подпись initData и потоковые токены."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import pytest

from backend.api.security import (
    WEBAPP_SECRET_SALT,
    create_stream_token,
    validate_init_data,
    verify_stream_token,
)
from backend.config import Settings, settings
from backend.errors import AuthError
from tests.conftest import TEST_BOT_TOKEN, TEST_SECRET_KEY, TEST_USER_ID

logger = logging.getLogger(__name__)


def build_init_data(
    *,
    bot_token: str = TEST_BOT_TOKEN,
    user_id: int = TEST_USER_ID,
    auth_date: int | None = None,
    broken_hash: bool = False,
) -> str:
    """Собрать корректную строку initData Telegram WebApp.

    Подпись считается ровно по алгоритму Telegram: секрет — HMAC от токена бота
    с ключом «WebAppData», строка проверки — отсортированные пары `ключ=значение`
    в РАСКОДИРОВАННОМ виде.
    """
    payload = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": "Тест",
                "last_name": "Тестов",
                "username": "tester",
                "language_code": "ru",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(payload.items())
    )
    secret_key = hmac.new(
        WEBAPP_SECRET_SALT, bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    signature = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if broken_hash:
        # Меняем один символ подписи, сохраняя длину и hex-алфавит.
        first = "0" if signature[0] != "0" else "1"
        signature = first + signature[1:]

    return urlencode({**payload, "hash": signature})


def sign_stream_payload(payload: str, secret_key: str = TEST_SECRET_KEY) -> str:
    """Подпись потокового токена (та же формула, что и в backend.api.security)."""
    return hmac.new(
        secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# initData
# ---------------------------------------------------------------------------


def test_valid_init_data(app_settings: Settings) -> None:
    """Корректная подпись — данные пользователя разбираются."""
    init_data = build_init_data()

    user = validate_init_data(init_data, TEST_BOT_TOKEN, settings.init_data_ttl)

    assert user["id"] == TEST_USER_ID
    assert user["first_name"] == "Тест"
    assert user["last_name"] == "Тестов"
    assert user["username"] == "tester"
    assert user["language_code"] == "ru"


def test_forged_hash_rejected(app_settings: Settings) -> None:
    """Подделанная подпись отклоняется."""
    init_data = build_init_data(broken_hash=True)

    with pytest.raises(AuthError) as exc_info:
        validate_init_data(init_data, TEST_BOT_TOKEN, settings.init_data_ttl)

    assert "подпись" in str(exc_info.value).casefold()


def test_init_data_signed_by_other_bot_rejected(app_settings: Settings) -> None:
    """Данные, подписанные чужим токеном, не проходят проверку."""
    init_data = build_init_data(bot_token="other:token")

    with pytest.raises(AuthError):
        validate_init_data(init_data, TEST_BOT_TOKEN, settings.init_data_ttl)


def test_expired_init_data_rejected(app_settings: Settings) -> None:
    """Просроченный auth_date отклоняется."""
    init_data = build_init_data(auth_date=int(time.time()) - 100_000)

    with pytest.raises(AuthError) as exc_info:
        validate_init_data(init_data, TEST_BOT_TOKEN, 3600)

    assert "устарел" in str(exc_info.value).casefold()


def test_expired_init_data_accepted_when_ttl_disabled(app_settings: Settings) -> None:
    """ttl <= 0 отключает проверку срока (полезно для отладки)."""
    init_data = build_init_data(auth_date=int(time.time()) - 100_000)

    user = validate_init_data(init_data, TEST_BOT_TOKEN, 0)

    assert user["id"] == TEST_USER_ID


def test_empty_init_data_rejected(app_settings: Settings) -> None:
    """Пустая строка и строка без подписи отклоняются."""
    with pytest.raises(AuthError):
        validate_init_data("", TEST_BOT_TOKEN, 3600)

    with pytest.raises(AuthError):
        validate_init_data("auth_date=1&user=%7B%22id%22%3A1%7D", TEST_BOT_TOKEN, 3600)


# ---------------------------------------------------------------------------
# Потоковые токены
# ---------------------------------------------------------------------------


def test_stream_token_roundtrip(app_settings: Settings) -> None:
    """Созданный токен успешно проверяется и отдаёт пару (user_id, track_id)."""
    token = create_stream_token(TEST_USER_ID, 17)

    assert token.count(".") == 3
    assert verify_stream_token(token) == (TEST_USER_ID, 17)


def test_stream_token_with_broken_signature(app_settings: Settings) -> None:
    """Испорченная подпись токена отклоняется."""
    token = create_stream_token(TEST_USER_ID, 17)
    user_part, track_part, expires_part, signature = token.split(".")
    broken = "0" if signature[0] != "0" else "1"
    forged = ".".join([user_part, track_part, expires_part, broken + signature[1:]])

    with pytest.raises(AuthError):
        verify_stream_token(forged)


def test_stream_token_with_tampered_payload(app_settings: Settings) -> None:
    """Подмена идентификатора трека без пересчёта подписи отклоняется."""
    token = create_stream_token(TEST_USER_ID, 17)
    user_part, _track_part, expires_part, signature = token.split(".")

    with pytest.raises(AuthError):
        verify_stream_token(".".join([user_part, "18", expires_part, signature]))


def test_expired_stream_token(app_settings: Settings) -> None:
    """Просроченный (но корректно подписанный) токен отклоняется."""
    expires_at = int(time.time()) - 10
    payload = f"{TEST_USER_ID}.17.{expires_at}"
    token = f"{payload}.{sign_stream_payload(payload)}"

    with pytest.raises(AuthError) as exc_info:
        verify_stream_token(token)

    assert "устарел" in str(exc_info.value).casefold()


def test_malformed_stream_token(app_settings: Settings) -> None:
    """Токен неверного формата отклоняется без исключений разбора."""
    for value in ("", "   ", "abc", "1.2.3", "1.2.3.4.5", "a.b.c.d"):
        with pytest.raises(AuthError):
            verify_stream_token(value)


def test_stream_token_uses_secret_key(app_settings: Settings) -> None:
    """Токен подписан секретным ключом приложения, а не чем-то ещё."""
    token = create_stream_token(TEST_USER_ID, 42)
    user_part, track_part, expires_part, signature = token.split(".")

    expected = sign_stream_payload(f"{user_part}.{track_part}.{expires_part}")
    assert hmac.compare_digest(expected, signature)
