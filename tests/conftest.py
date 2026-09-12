"""Общие фикстуры автотестов MusicBox.

Каждый тест получает СВОЮ временную базу данных (`tmp_path`) и заново
инициализирует синглтон :data:`backend.db.database.db`: перед тестом —
`init_db(path)`, после теста — `shutdown_db()`. Иначе соединение первого теста
осталось бы открытым и все последующие работали бы с чужими данными.

Настройки приложения (`backend.config.settings`) — единственный объект, который
импортируют все модули backend, поэтому подмена его полей через `monkeypatch`
действует на весь код: API работает в режиме разработки (`dev_mode`) и не ходит
ни в Telegram, ни в сеть.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.app import create_app
from backend.config import Settings, settings
from backend.db.database import db, init_db, shutdown_db
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo

logger = logging.getLogger(__name__)

#: Telegram-ID пользователя, от имени которого выполняются тесты.
TEST_USER_ID: int = 424_242

#: Токен «бота» для проверки подписи initData (в сеть не уходит).
TEST_BOT_TOKEN: str = "test:token"

#: Секретный ключ для подписи потоковых токенов.
TEST_SECRET_KEY: str = "test-secret"

#: Идентификатор канала-хранилища (используется только как значение настройки).
TEST_STORAGE_CHANNEL_ID: int = -100123

#: Базовый адрес тестового клиента (ASGI-транспорт, реальных сокетов нет).
TEST_BASE_URL: str = "http://test"

#: Значения настроек, одинаковые для всех тестов.
TEST_SETTINGS: dict[str, Any] = {
    "dev_mode": True,
    "dev_user_id": TEST_USER_ID,
    "bot_token": TEST_BOT_TOKEN,
    "storage_channel_id": TEST_STORAGE_CHANNEL_ID,
    "secret_key": TEST_SECRET_KEY,
    "allowed_user_ids": "",
    "bot_mode": "polling",
    "cors_origins": "*",
    # Пустой путь — статика Mini App не монтируется, тесты не зависят от сборки фронтенда.
    "frontend_dist": "",
    "telegram_search_enabled": False,
    "init_data_ttl": 86400,
    "stream_token_ttl": 21600,
    "frequent_threshold": 10,
    "rare_min": 1,
    "rare_max": 5,
    "top_limit": 10,
    "recent_limit": 20,
    "fuzzy_threshold": 60,
    "webapp_url": "",
    "log_file": "",
}

#: Тип фабрики треков: асинхронный вызов, возвращающий трек-dict.
TrackFactory = Callable[..., Awaitable[dict]]

#: Тип помощника «выставить число прослушиваний».
PlayCountSetter = Callable[[int, int], Awaitable[None]]


@pytest.fixture
def user_id() -> int:
    """Telegram-ID пользователя тестов (совпадает с `settings.dev_user_id`)."""
    return TEST_USER_ID


@pytest.fixture
def app_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Settings:
    """Подменяет поля глобальных настроек на тестовые значения."""
    values = dict(TEST_SETTINGS)
    values["database_path"] = str(tmp_path / "musicbox.db")
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
async def database(app_settings: Settings, tmp_path: Any) -> AsyncIterator[str]:
    """Временная база данных: чистый файл на каждый тест.

    Синглтон `db` переинициализируется принудительно — сначала закрываем то,
    что могло остаться от предыдущего теста, затем подключаемся к новому файлу.
    """
    path = str(tmp_path / "musicbox.db")
    await shutdown_db()
    await init_db(path)
    logger.debug("Тестовая база данных готова: %s", path)
    try:
        yield path
    finally:
        await shutdown_db()


@pytest.fixture
async def user(database: str, user_id: int) -> dict:
    """Пользователь тестов, уже существующий в базе (нужен для внешних ключей)."""
    return await users_repo.ensure_user(
        user_id,
        username="tester",
        first_name="Тест",
        last_name="Тестов",
        language_code="ru",
    )


@pytest.fixture
def set_play_count(database: str, user_id: int) -> PlayCountSetter:
    """Помощник: выставить треку число прослушиваний без вызова register_play.

    Нужен, чтобы быстро подготовить данные для проверки границ разделов
    статистики; сам `register_play` проверяется отдельным тестом.
    """

    async def _set(track_id: int, play_count: int) -> None:
        count = max(int(play_count), 0)
        if count:
            await db.execute(
                "UPDATE tracks SET play_count = ?, last_played_at = CURRENT_TIMESTAMP"
                " WHERE id = ? AND user_id = ?",
                (count, int(track_id), user_id),
            )
        else:
            await db.execute(
                "UPDATE tracks SET play_count = 0, last_played_at = NULL"
                " WHERE id = ? AND user_id = ?",
                (int(track_id), user_id),
            )

    return _set


@pytest.fixture
def track_factory(
    user: dict, user_id: int, set_play_count: PlayCountSetter
) -> TrackFactory:
    """Фабрика треков: создаёт запись в базе и при необходимости счётчик прослушиваний."""
    counter = {"value": 0}

    async def _create(
        title: str = "Трек",
        *,
        artist: str | None = None,
        album: str | None = None,
        duration: int = 180,
        file_size: int = 1024,
        mime_type: str | None = "audio/mpeg",
        file_name: str | None = None,
        file_id: str | None = None,
        file_unique_id: str | None = None,
        folder_id: int | None = None,
        artist_id: int | None = None,
        album_id: int | None = None,
        thumb_file_id: str | None = None,
        source: str = "upload",
        source_ref: str | None = None,
        play_count: int = 0,
    ) -> dict:
        counter["value"] += 1
        index = counter["value"]
        track = await tracks_repo.create_track(
            user_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            file_size=file_size,
            mime_type=mime_type,
            file_name=file_name or f"{title}.mp3",
            file_id=file_id or f"file-id-{index}",
            file_unique_id=file_unique_id or f"unique-{index}",
            storage_chat_id=TEST_STORAGE_CHANNEL_ID,
            storage_message_id=1000 + index,
            thumb_file_id=thumb_file_id,
            folder_id=folder_id,
            artist_id=artist_id,
            album_id=album_id,
            source=source,
            source_ref=source_ref,
        )
        if play_count:
            await set_play_count(int(track["id"]), play_count)
            refreshed = await tracks_repo.get_track(user_id, int(track["id"]))
            if refreshed is not None:
                return refreshed
        return track

    return _create


@pytest.fixture
async def app(database: str) -> FastAPI:
    """Приложение FastAPI без жизненного цикла: база уже поднята фикстурой `database`."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх ASGI-транспорта (lifespan не запускается)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as http_client:
        yield http_client
