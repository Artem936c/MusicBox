"""Тесты импорта найденного в Telegram аудио: защита от повторного добавления.

Сеть и Telegram полностью замещены: `telegram_search.download` и
`storage.store_from_bytes` / `storage.delete_from_channel` подменяются
счётчиками-заглушками, Telethon не поднимается, объект бота — пустышка.
Проверяется только логика `import_remote`: повторный импорт одного и того же
сообщения не качает файл заново и не создаёт второй трек.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import pytest

from backend.db.repositories import tracks as tracks_repo
from backend.services import telegram_search as ts
from backend.services.storage import StoredAudio
from backend.services.telegram_search import RemoteAudio, import_remote

logger = logging.getLogger(__name__)

#: Байты «скачанного» файла — содержимое не важно, важен факт скачивания.
FAKE_AUDIO_BYTES = b"ID3 fake audio payload"


class FakeBot:
    """Пустышка вместо `aiogram.Bot`: в неё никто не должен обращаться.

    `store_from_bytes` и `delete_from_channel` подменены, поэтому единственный
    способ дотянуться до Bot API — ошибка в коде импорта; тогда упадёт
    `AttributeError` с именем метода, и тест это покажет.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover — страховка от сети
        raise AssertionError(f"Импорт полез в Telegram: bot.{name}")


class ImportSpy:
    """Журнал вызовов подменённых зависимостей импорта."""

    def __init__(self) -> None:
        self.downloads: list[RemoteAudio] = []
        self.stored: list[str] = []
        self.deleted: list[int] = []
        #: `file_unique_id`, который «канал» вернёт на следующее сохранение.
        self.next_unique_id = "unique-remote-1"
        self._message_id = 5000

    async def download(self, remote: RemoteAudio) -> bytes:
        """Заглушка `TelegramSearchService.download`."""
        self.downloads.append(remote)
        return FAKE_AUDIO_BYTES

    async def store_from_bytes(
        self,
        bot: Any,
        data: bytes,
        *,
        file_name: str,
        title: str,
        performer: str | None,
        duration: int = 0,
        mime_type: str | None = None,
        caption: str | None = None,
        album: str | None = None,
    ) -> StoredAudio:
        """Заглушка `storage.store_from_bytes`: «кладёт» файл в канал-хранилище."""
        assert data == FAKE_AUDIO_BYTES
        self._message_id += 1
        self.stored.append(file_name)
        return StoredAudio(
            file_id=f"file-{self._message_id}",
            file_unique_id=self.next_unique_id,
            message_id=self._message_id,
            chat_id=-100123,
            title=title,
            artist=performer,
            album=album,
            duration=duration,
            file_size=len(data),
            mime_type=mime_type,
            file_name=file_name,
            thumb_file_id=None,
        )

    async def delete_from_channel(self, bot: Any, message_id: int | None) -> bool:
        """Заглушка `storage.delete_from_channel`."""
        self.deleted.append(int(message_id or 0))
        return True


@pytest.fixture(autouse=True)
def clean_import_cache() -> Iterator[None]:
    """Кэш импортированных сообщений живёт в модуле — чистим его вокруг теста."""
    ts._IMPORTED.clear()
    ts._IMPORT_LOCKS.clear()
    try:
        yield
    finally:
        ts._IMPORTED.clear()
        ts._IMPORT_LOCKS.clear()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ImportSpy:
    """Подменяет скачивание и работу с каналом-хранилищем на заглушки."""
    recorder = ImportSpy()
    monkeypatch.setattr(ts.telegram_search, "download", recorder.download)
    monkeypatch.setattr(ts.storage, "store_from_bytes", recorder.store_from_bytes)
    monkeypatch.setattr(ts.storage, "delete_from_channel", recorder.delete_from_channel)
    return recorder


@pytest.fixture
def remote() -> RemoteAudio:
    """Найденное в Telegram аудио (одно конкретное сообщение канала)."""
    return RemoteAudio(
        token="tok-1",
        chat_id=-1001111111111,
        chat_title="Музыка",
        chat_username="musicchan",
        message_id=777,
        title="Кукушка",
        performer="Кино",
        duration=284,
        file_size=4096,
        mime_type="audio/mpeg",
        file_name="Кино - Кукушка.mp3",
        link="https://t.me/musicchan/777",
    )


async def _library_size(user_id: int) -> int:
    """Сколько треков сейчас в библиотеке пользователя."""
    return await tracks_repo.total_tracks(user_id)


# ---------------------------------------------------------------------------
# Первый импорт
# ---------------------------------------------------------------------------


async def test_import_creates_track(
    user: dict, user_id: int, spy: ImportSpy, remote: RemoteAudio
) -> None:
    """Первый импорт скачивает файл, кладёт его в канал и создаёт трек."""
    track = await import_remote(FakeBot(), user_id, remote)

    assert not track.get("duplicate")
    assert track["title"] == "Кукушка"
    assert track["artist"] == "Кино"
    assert track["source"] == "telegram_search"
    assert track["source_ref"] == remote.link
    assert len(spy.downloads) == 1
    assert len(spy.stored) == 1
    assert spy.deleted == []
    assert await _library_size(user_id) == 1


# ---------------------------------------------------------------------------
# Повторный импорт того же сообщения
# ---------------------------------------------------------------------------


async def test_repeat_import_returns_duplicate(
    user: dict, user_id: int, spy: ImportSpy, remote: RemoteAudio
) -> None:
    """Повторный импорт того же сообщения не качает файл и не плодит треки."""
    first = await import_remote(FakeBot(), user_id, remote)
    second = await import_remote(FakeBot(), user_id, remote)

    assert second.get("duplicate") is True
    assert second["id"] == first["id"]
    # Файл скачан и сохранён РОВНО один раз.
    assert len(spy.downloads) == 1
    assert len(spy.stored) == 1
    assert await _library_size(user_id) == 1


async def test_repeat_import_by_link_without_message_id(
    user: dict, user_id: int, spy: ImportSpy
) -> None:
    """Аудио без chat_id/message_id опознаётся по ссылке — дубль тоже ловится."""
    by_link = RemoteAudio(
        token="tok-link",
        title="Группа крови",
        performer="Кино",
        duration=284,
        file_size=2048,
        mime_type="audio/mpeg",
        link="https://t.me/musicchan/42",
    )

    first = await import_remote(FakeBot(), user_id, by_link)
    second = await import_remote(FakeBot(), user_id, by_link)

    assert second.get("duplicate") is True
    assert second["id"] == first["id"]
    assert len(spy.downloads) == 1
    assert await _library_size(user_id) == 1


async def test_duplicate_does_not_touch_library(
    user: dict, user_id: int, spy: ImportSpy, remote: RemoteAudio
) -> None:
    """Пометка duplicate не меняет сам трек в базе."""
    first = await import_remote(FakeBot(), user_id, remote)
    second = await import_remote(FakeBot(), user_id, remote)

    stored = await tracks_repo.get_track(user_id, int(first["id"]))
    assert stored is not None
    assert "duplicate" not in stored
    assert stored["file_id"] == second["file_id"]


# ---------------------------------------------------------------------------
# Другое сообщение с тем же файлом
# ---------------------------------------------------------------------------


async def test_same_file_from_another_message_is_duplicate(
    user: dict, user_id: int, spy: ImportSpy, remote: RemoteAudio
) -> None:
    """Тот же файл из другого сообщения не дублируется, а лишняя копия удаляется."""
    first = await import_remote(FakeBot(), user_id, remote)

    other = RemoteAudio(
        token="tok-2",
        chat_id=-1002222222222,
        chat_title="Другой канал",
        message_id=999,
        title="Кукушка",
        performer="Кино",
        duration=284,
        file_size=4096,
        mime_type="audio/mpeg",
        file_name="Кино - Кукушка.mp3",
        link="https://t.me/other/999",
    )
    second = await import_remote(FakeBot(), user_id, other)

    assert second.get("duplicate") is True
    assert second["id"] == first["id"]
    # Файл скачали второй раз (сообщение другое), но в библиотеке он один,
    # а свежая копия из канала-хранилища удалена.
    assert len(spy.downloads) == 2
    assert len(spy.deleted) == 1
    assert await _library_size(user_id) == 1


# ---------------------------------------------------------------------------
# Удалённый трек импортируется заново
# ---------------------------------------------------------------------------


async def test_import_after_delete_creates_new_track(
    user: dict, user_id: int, spy: ImportSpy, remote: RemoteAudio
) -> None:
    """Если пользователь удалил трек, то же сообщение импортируется заново."""
    first = await import_remote(FakeBot(), user_id, remote)
    await tracks_repo.delete_track(user_id, int(first["id"]))
    assert await _library_size(user_id) == 0

    # Новая копия в канале-хранилище получит собственный file_unique_id.
    spy.next_unique_id = "unique-remote-2"
    second = await import_remote(FakeBot(), user_id, remote)

    assert not second.get("duplicate")
    assert second["id"] != first["id"]
    assert len(spy.downloads) == 2
    assert await _library_size(user_id) == 1
