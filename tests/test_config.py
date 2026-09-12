"""Тесты проверки настроек перед запуском (`Settings.validate_runtime`).

Настройки создаются с ``_env_file=None`` и явными значениями всех важных полей:
так тест не зависит ни от файла `.env`, ни от переменных окружения, в которых
его запускают.
"""

from __future__ import annotations

import logging

import pytest

from backend.config import Settings

logger = logging.getLogger(__name__)


def make_settings(**overrides: object) -> Settings:
    """Заведомо рабочие настройки, в которых меняется только нужное поле."""
    values: dict[str, object] = {
        "bot_token": "123:test",
        "storage_channel_id": -1001234567890,
        "secret_key": "test-secret",
        "dev_mode": False,
        "bot_mode": "polling",
        "webhook_base_url": "",
        "webhook_secret": "",
        "telegram_search_enabled": False,
        "tg_api_id": 0,
        "tg_api_hash": "",
        "tg_session_string": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_valid_settings_pass() -> None:
    """Полностью заполненные настройки проходят проверку молча."""
    make_settings().validate_runtime()


def test_webhook_with_secret_passes() -> None:
    """Режим webhook с адресом и секретом — корректная конфигурация."""
    make_settings(
        bot_mode="webhook",
        webhook_base_url="https://example.com",
        webhook_secret="s3cret",
    ).validate_runtime()


def test_webhook_without_secret_fails() -> None:
    """Режим webhook без WEBHOOK_SECRET останавливает запуск с понятным текстом."""
    settings = make_settings(
        bot_mode="webhook",
        webhook_base_url="https://example.com",
        webhook_secret="",
    )

    with pytest.raises(ValueError) as error:
        settings.validate_runtime()

    message = str(error.value)
    assert "WEBHOOK_SECRET" in message
    # Текст объясняет причину и предлагает выход, а не просто называет поле.
    assert "X-Telegram-Bot-Api-Secret-Token" in message
    assert "BOT_MODE=polling" in message
    # Адрес задан — про WEBHOOK_BASE_URL ругаться не за что.
    assert "WEBHOOK_BASE_URL" not in message


def test_webhook_secret_of_spaces_fails() -> None:
    """Секрет из одних пробелов считается незаданным."""
    settings = make_settings(
        bot_mode="webhook",
        webhook_base_url="https://example.com",
        webhook_secret="   ",
    )

    with pytest.raises(ValueError) as error:
        settings.validate_runtime()

    assert "WEBHOOK_SECRET" in str(error.value)


def test_polling_without_secret_passes() -> None:
    """В режиме polling секрет вебхука не нужен."""
    make_settings(bot_mode="polling", webhook_secret="").validate_runtime()


def test_webhook_without_url_and_secret_reports_both() -> None:
    """Проверка собирает ВСЕ проблемы сразу, а не падает на первой."""
    settings = make_settings(bot_mode="webhook", webhook_base_url="", webhook_secret="")

    with pytest.raises(ValueError) as error:
        settings.validate_runtime()

    message = str(error.value)
    assert "WEBHOOK_BASE_URL" in message
    assert "WEBHOOK_SECRET" in message


def test_missing_bot_token_fails() -> None:
    """Без BOT_TOKEN приложение не стартует."""
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        make_settings(bot_token="").validate_runtime()


def test_missing_storage_channel_fails() -> None:
    """Без STORAGE_CHANNEL_ID приложение не стартует."""
    with pytest.raises(ValueError, match="STORAGE_CHANNEL_ID"):
        make_settings(storage_channel_id=0).validate_runtime()


def test_default_secret_key_fails_outside_dev() -> None:
    """Ключ по умолчанию «change-me» запрещён вне режима разработки."""
    with pytest.raises(ValueError, match="SECRET_KEY"):
        make_settings(secret_key="change-me", dev_mode=False).validate_runtime()

    # В режиме разработки тот же ключ допустим.
    make_settings(secret_key="change-me", dev_mode=True, dev_user_id=1).validate_runtime()


def test_webhook_url_is_built_from_base_and_path() -> None:
    """Полный адрес вебхука собирается из базы и пути (нужен для validate_runtime)."""
    settings = make_settings(
        bot_mode="webhook",
        webhook_base_url="https://example.com/",
        webhook_path="telegram/webhook",
        webhook_secret="s3cret",
    )

    assert settings.webhook_url == "https://example.com/telegram/webhook"
    assert make_settings(webhook_base_url="").webhook_url == ""
