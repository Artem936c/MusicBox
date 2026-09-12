"""Конфигурация MusicBox: чтение настроек из окружения и файла .env."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Разделители в CSV-полях: запятая, точка с запятой и любые пробельные символы.
_CSV_SPLIT_RE = re.compile(r"[,;\s]+")


class Settings(BaseSettings):
    """Настройки приложения. Значения читаются из переменных окружения и файла .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = ""
    storage_channel_id: int = 0
    webapp_url: str = ""
    secret_key: str = "change-me"

    # --- Хранилище и сеть ---
    database_path: str = "data/musicbox.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Режим работы бота ---
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""
    telegram_api_base: str = "https://api.telegram.org"

    # --- Безопасность Mini App ---
    init_data_ttl: int = 86400
    stream_token_ttl: int = 21600
    dev_mode: bool = False
    dev_user_id: int = 0
    allowed_user_ids: str = ""

    # --- Разделы статистики и пагинация ---
    frequent_threshold: int = 10
    rare_min: int = 1
    rare_max: int = 5
    top_limit: int = 10
    recent_limit: int = 20
    page_size: int = 10
    fuzzy_threshold: int = 60
    max_download_size: int = 20971520

    # --- Поиск по Telegram (Telethon) ---
    telegram_search_enabled: bool = False
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_string: str = ""
    tg_search_chats: str = ""

    # --- Распознавание музыки (ТЗ п. 7) ---
    # Пустой recognition_provider = распознавание выключено: services/recognition.py
    # сообщит об этом понятным текстом и не пойдёт в сеть.
    # Допустимые значения: audd | acrcloud | genius.
    recognition_provider: str = ""
    audd_api_token: str = ""
    acrcloud_host: str = ""
    acrcloud_key: str = ""
    acrcloud_secret: str = ""
    genius_access_token: str = ""

    # --- Логирование и статика ---
    log_level: str = "INFO"
    log_file: str = ""
    cors_origins: str = "*"
    frontend_dist: str = "frontend/dist"

    # ------------------------------------------------------------------
    # Производные значения
    # ------------------------------------------------------------------

    @property
    def allowed_user_ids_set(self) -> set[int]:
        """Белый список Telegram-ID. Пустое множество = доступ разрешён всем."""
        result: set[int] = set()
        for chunk in _split_csv(self.allowed_user_ids):
            try:
                result.add(int(chunk))
            except ValueError:
                logger.warning(
                    "Некорректный Telegram-ID в ALLOWED_USER_IDS: %r — значение пропущено", chunk
                )
        return result

    @property
    def cors_origins_list(self) -> list[str]:
        """Список разрешённых источников для CORS. `*` = разрешить всё."""
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        origins = [item.rstrip("/") for item in _split_csv(raw)]
        return origins or ["*"]

    @property
    def tg_search_chats_list(self) -> list[str]:
        """Каналы по умолчанию для поиска аудио в Telegram."""
        return _split_csv(self.tg_search_chats)

    @property
    def webhook_url(self) -> str:
        """Полный публичный URL вебхука или пустая строка, если база не задана."""
        base = (self.webhook_base_url or "").strip().rstrip("/")
        if not base:
            return ""
        path = (self.webhook_path or "").strip() or "/telegram/webhook"
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    @property
    def database_dir(self) -> Path:
        """Каталог, в котором лежит файл базы данных (создаётся при подключении)."""
        return self.database_file.parent

    @property
    def database_file(self) -> Path:
        """Путь к файлу базы данных как `pathlib.Path`."""
        return Path(self.database_path).expanduser()

    # ------------------------------------------------------------------
    # Проверка перед запуском
    # ------------------------------------------------------------------

    def validate_runtime(self) -> None:
        """Проверить настройки, без которых приложение не сможет работать.

        Бросает `ValueError` с понятным русским описанием всех найденных проблем.
        """
        problems: list[str] = []

        if not self.bot_token.strip():
            problems.append(
                "Не задан токен бота (BOT_TOKEN). "
                "Создайте бота у @BotFather и укажите токен в файле .env."
            )

        if not self.storage_channel_id:
            problems.append(
                "Не задан ID канала-хранилища (STORAGE_CHANNEL_ID). "
                "Создайте приватный канал, добавьте бота администратором "
                "и укажите ID канала (например -1001234567890) в файле .env."
            )

        if self.secret_key.strip() in ("", "change-me") and not self.dev_mode:
            problems.append(
                "Не задан секретный ключ (SECRET_KEY) — значение по умолчанию "
                "'change-me' небезопасно. Укажите случайную строку в файле .env "
                "(например, результат команды: python -c \"import secrets; "
                'print(secrets.token_hex(32))").'
            )

        if self.bot_mode == "webhook" and not self.webhook_url:
            problems.append(
                "Выбран режим webhook (BOT_MODE=webhook), но не задан публичный адрес "
                "(WEBHOOK_BASE_URL). Укажите HTTPS-адрес сервера, например "
                "https://example.com, либо переключитесь на BOT_MODE=polling."
            )

        if self.bot_mode == "webhook" and not self.webhook_secret.strip():
            problems.append(
                "Для режима webhook задайте WEBHOOK_SECRET: без него бот не сможет "
                "принимать обновления — секрет сверяется с заголовком "
                "X-Telegram-Bot-Api-Secret-Token, и без него все запросы отклоняются. "
                "Укажите случайную строку в файле .env (например, результат команды: "
                'python -c "import secrets; print(secrets.token_hex(32))"), '
                "либо переключитесь на BOT_MODE=polling."
            )

        if self.telegram_search_enabled and not (
            self.tg_api_id and self.tg_api_hash.strip() and self.tg_session_string.strip()
        ):
            problems.append(
                "Включён поиск по Telegram (TELEGRAM_SEARCH_ENABLED=true), но не заполнены "
                "TG_API_ID, TG_API_HASH и TG_SESSION_STRING. Заполните их или отключите поиск."
            )

        if self.dev_mode and not self.dev_user_id:
            logger.warning(
                "Включён режим разработки (DEV_MODE=true), но DEV_USER_ID не задан — "
                "запросы без initData будут отклонены."
            )

        if problems:
            raise ValueError(
                "Проверьте настройки MusicBox (файл .env):\n"
                + "\n".join(f"  • {item}" for item in problems)
            )


def _split_csv(value: str | None) -> list[str]:
    """Разобрать строку с перечислением через запятую (или пробел) в список без пустых значений."""
    if not value:
        return []
    return [chunk for chunk in _CSV_SPLIT_RE.split(value.strip()) if chunk]


@lru_cache
def get_settings() -> Settings:
    """Вернуть настройки приложения (создаются один раз и кэшируются)."""
    return Settings()


settings: Settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
