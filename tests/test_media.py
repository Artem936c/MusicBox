"""Регрессионные тесты медиафайлов: тип файла и граница «Треки» / «Другое».

Раздел 8 контракта V2 требует проверить `file_type` и разделение разделов.
Здесь три группы тестов:

1. Чистые функции :mod:`backend.services.media` — определение типа вложения
   (`detect_file_type`) и распознавание аудио, присланного документом
   (`is_audio_document`). Сообщения Telegram собираются вручную, сеть не нужна.
2. Репозиторий треков: раздел «Треки» — это ``file_type='audio'``, раздел
   «Другое» — все остальные типы. Проверяется значение по умолчанию у
   `list_tracks` / `count_tracks` и поиск по разделам (`search_in`).
3. Отправка файла пользователю: метод Bot API выбирается ПО `file_type`
   (аудио — `sendAudio`, кружочек — `sendVideoNote` и т. д.). Сессия
   Bot API подменена офлайн-заглушкой, реальных запросов нет.

Отдельно закреплено требование: разделы статистики и счётчики учитывают
только аудио, файлы раздела «Другое» в них не попадают.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    SendAudio,
    SendDocument,
    SendVideo,
    SendVideoNote,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import (
    Audio,
    Chat,
    Document,
    Message,
    Video,
    VideoNote,
    Voice,
)

from backend.db.repositories import stats as stats_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import ValidationError
from backend.services import media

logger = logging.getLogger(__name__)

#: Токен «бота» тестов — с ним никто не ходит в сеть (сессия подменена).
FAKE_BOT_TOKEN = "123:test"

#: Дата сообщений-заглушек (значение не важно, но должно быть осознанным).
MESSAGE_DATE = dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

#: Тип фабрики файлов раздела «Другое» и треков.
MediaFactory = Callable[..., Awaitable[dict]]


# ---------------------------------------------------------------------------
# Сообщения Telegram без сети
# ---------------------------------------------------------------------------


def _message(**attachments: Any) -> Message:
    """Сообщение с указанными вложениями (всё остальное — минимум полей)."""
    return Message(
        message_id=1,
        date=MESSAGE_DATE,
        chat=Chat(id=1, type="private"),
        **attachments,
    )


def _audio(**fields: Any) -> Audio:
    """Аудио Telegram (поле `message.audio`)."""
    fields.setdefault("file_id", "audio-file-id")
    fields.setdefault("file_unique_id", "audio-unique")
    fields.setdefault("duration", 180)
    return Audio(**fields)


def _document(**fields: Any) -> Document:
    """Документ Telegram (поле `message.document`)."""
    fields.setdefault("file_id", "document-file-id")
    fields.setdefault("file_unique_id", "document-unique")
    return Document(**fields)


def _video(**fields: Any) -> Video:
    """Видео Telegram (поле `message.video`)."""
    fields.setdefault("file_id", "video-file-id")
    fields.setdefault("file_unique_id", "video-unique")
    fields.setdefault("width", 640)
    fields.setdefault("height", 480)
    fields.setdefault("duration", 30)
    return Video(**fields)


def _video_note(**fields: Any) -> VideoNote:
    """Видеосообщение («кружочек»)."""
    fields.setdefault("file_id", "video-note-file-id")
    fields.setdefault("file_unique_id", "video-note-unique")
    fields.setdefault("length", 240)
    fields.setdefault("duration", 12)
    return VideoNote(**fields)


def _voice(**fields: Any) -> Voice:
    """Голосовое сообщение."""
    fields.setdefault("file_id", "voice-file-id")
    fields.setdefault("file_unique_id", "voice-unique")
    fields.setdefault("duration", 7)
    return Voice(**fields)


# ---------------------------------------------------------------------------
# Офлайн-сессия Bot API
# ---------------------------------------------------------------------------


class FakeSession(BaseSession):
    """Сессия-заглушка: запоминает вызовы Bot API и отвечает без сети."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        """Закрывать нечего — сокетов нет."""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        """Записывает вызов и возвращает правдоподобное сообщение."""
        self.calls.append(method)
        chat_id = getattr(method, "chat_id", 0) or 0
        return Message(
            message_id=777,
            date=MESSAGE_DATE,
            chat=Chat(id=int(chat_id), type="private"),
        )

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        """Скачивать в тестах нечего."""
        yield b""

    def method_types(self) -> list[type]:
        """Типы вызванных методов Bot API в порядке вызова."""
        return [type(call) for call in self.calls]


@pytest.fixture
def session() -> FakeSession:
    """Свежий журнал вызовов Bot API на каждый тест."""
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    """Бот поверх офлайн-сессии."""
    return Bot(token=FAKE_BOT_TOKEN, session=session)


# ---------------------------------------------------------------------------
# Фабрика файлов в базе
# ---------------------------------------------------------------------------


@pytest.fixture
def media_factory(user: dict, user_id: int) -> MediaFactory:
    """Создаёт запись в `tracks` с произвольным `file_type`.

    Фабрика из conftest умеет только аудио, а здесь нужны все пять типов,
    поэтому репозиторий вызывается напрямую.
    """
    counter = {"value": 0}

    async def _create(
        title: str,
        *,
        file_type: str = tracks_repo.DEFAULT_FILE_TYPE,
        play_count: int = 0,
        genre: str | None = None,
        folder_id: int | None = None,
        mime_type: str | None = None,
        file_name: str | None = None,
    ) -> dict:
        counter["value"] += 1
        index = counter["value"]
        track = await tracks_repo.create_track(
            user_id,
            title=title,
            duration=60,
            file_size=2048,
            mime_type=mime_type,
            file_name=file_name or f"{title}.bin",
            file_id=f"media-file-{index}",
            file_unique_id=f"media-unique-{index}",
            storage_chat_id=-100123,
            storage_message_id=2000 + index,
            folder_id=folder_id,
            file_type=file_type,
            genre=genre,
        )
        for _ in range(max(int(play_count), 0)):
            await tracks_repo.register_play(user_id, int(track["id"]), source="test")
        refreshed = await tracks_repo.get_track(user_id, int(track["id"]))
        return refreshed or track

    return _create


@pytest.fixture
async def library(media_factory: MediaFactory) -> dict[str, dict]:
    """Библиотека из пяти типов файлов: два аудио и четыре файла «Другого»."""
    return {
        "audio": await media_factory("Песня о море", file_type="audio"),
        "audio_second": await media_factory("Вторая песня", file_type="audio"),
        "document": await media_factory(
            "Отчёт за квартал", file_type="document", file_name="report.pdf"
        ),
        "video": await media_factory("Клип на песню", file_type="video"),
        "video_note": await media_factory("Кружочек с дачи", file_type="video_note"),
        "voice": await media_factory("Голосовое напоминание", file_type="voice"),
    }


def _ids(tracks: list[dict]) -> set[int]:
    """Множество идентификаторов в выдаче."""
    return {int(track["id"]) for track in tracks}


def _file_types(tracks: list[dict]) -> set[str]:
    """Множество типов файлов в выдаче."""
    return {str(track["file_type"]) for track in tracks}


# ===========================================================================
# 1. Определение типа файла
# ===========================================================================


@pytest.mark.parametrize(
    ("attachments", "expected"),
    [
        pytest.param({"audio": _audio()}, "audio", id="audio"),
        pytest.param(
            {"document": _document(mime_type="application/pdf", file_name="doc.pdf")},
            "document",
            id="document",
        ),
        pytest.param({"video": _video()}, "video", id="video"),
        pytest.param({"video_note": _video_note()}, "video_note", id="video_note"),
        pytest.param({"voice": _voice()}, "voice", id="voice"),
    ],
)
def test_detect_file_type_by_attachment(
    attachments: dict[str, Any], expected: str
) -> None:
    """Каждому вложению соответствует свой тип из FILE_TYPES."""
    assert media.detect_file_type(_message(**attachments)) == expected
    assert expected in media.FILE_TYPES


def test_detect_file_type_without_attachment() -> None:
    """В сообщении без файла типа нет — возвращается None, а не «audio»."""
    assert media.detect_file_type(_message(text="просто текст")) is None
    assert media.detect_file_type(None) is None


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            _document(mime_type="audio/mpeg", file_name="track.mp3"), id="mime"
        ),
        pytest.param(_document(file_name="track.flac"), id="extension"),
        pytest.param(
            _document(mime_type="application/ogg", file_name="track.bin"), id="ogg"
        ),
    ],
)
def test_audio_document_goes_to_tracks(document: Document) -> None:
    """Mp3, присланный «файлом», — это трек, а не файл раздела «Другое»."""
    assert media.is_audio_document(document) is True
    assert media.detect_file_type(_message(document=document)) == "audio"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(None, id="none"),
        pytest.param(
            _document(mime_type="application/pdf", file_name="report.pdf"), id="pdf"
        ),
        pytest.param(_document(), id="без имени и mime"),
    ],
)
def test_not_audio_document(document: Document | None) -> None:
    """Обычный документ аудио не считается."""
    assert media.is_audio_document(document) is False


def test_detect_file_type_prefers_specific_attachment() -> None:
    """У видео Telegram заполняет и `document` — тип определяется по видео."""
    message = _message(
        video=_video(),
        document=_document(mime_type="video/mp4", file_name="clip.mp4"),
    )

    assert media.detect_file_type(message) == "video"


def test_normalize_file_type_falls_back_to_audio() -> None:
    """Неизвестный тип в сервисе считается аудио (совместимость с V1)."""
    assert media.normalize_file_type("VIDEO_NOTE") == "video_note"
    assert media.normalize_file_type("sticker") == media.DEFAULT_FILE_TYPE
    assert media.normalize_file_type(None) == media.DEFAULT_FILE_TYPE
    assert media.is_audio_type("audio") is True
    assert media.is_audio_type("voice") is False


def test_file_types_match_repository() -> None:
    """Сервис и репозиторий знают один и тот же набор типов."""
    assert media.FILE_TYPES == tracks_repo.FILE_TYPES
    assert media.AUDIO_FILE_TYPE == tracks_repo.DEFAULT_FILE_TYPE
    assert set(tracks_repo.NON_AUDIO_FILE_TYPES) == set(media.FILE_TYPES) - {
        media.AUDIO_FILE_TYPE
    }


# ===========================================================================
# 2. Раздел «Треки» против раздела «Другое»
# ===========================================================================


async def test_list_tracks_returns_only_audio_by_default(
    user_id: int, library: dict[str, dict]
) -> None:
    """`list_tracks` без аргументов — это раздел «Треки», только аудио."""
    tracks = await tracks_repo.list_tracks(user_id)

    assert _file_types(tracks) == {"audio"}
    assert _ids(tracks) == {int(library["audio"]["id"]), int(library["audio_second"]["id"])}


async def test_list_tracks_other_section(
    user_id: int, library: dict[str, dict]
) -> None:
    """Раздел «Другое» — все типы, кроме аудио, и ни одного трека."""
    others = await tracks_repo.list_tracks(
        user_id, file_type=tracks_repo.NON_AUDIO_FILE_TYPES
    )

    assert _file_types(others) == {"document", "video", "video_note", "voice"}
    assert _ids(others) == {
        int(library[key]["id"]) for key in ("document", "video", "video_note", "voice")
    }


async def test_list_tracks_without_filter_returns_everything(
    user_id: int, library: dict[str, dict]
) -> None:
    """`file_type=None` — выдача без фильтра: разделы не пересекаются, но дают всё."""
    everything = await tracks_repo.list_tracks(user_id, file_type=None)
    audio = await tracks_repo.list_tracks(user_id)
    others = await tracks_repo.list_tracks(
        user_id, file_type=tracks_repo.NON_AUDIO_FILE_TYPES
    )

    assert _ids(everything) == {int(item["id"]) for item in library.values()}
    assert _ids(audio) & _ids(others) == set()
    assert _ids(audio) | _ids(others) == _ids(everything)


async def test_count_tracks_counts_only_audio_by_default(
    user_id: int, library: dict[str, dict]
) -> None:
    """Счётчик раздела «Треки» не учитывает файлы «Другого»."""
    assert await tracks_repo.count_tracks(user_id) == 2
    assert (
        await tracks_repo.count_tracks(
            user_id, file_type=tracks_repo.NON_AUDIO_FILE_TYPES
        )
        == 4
    )
    assert await tracks_repo.count_tracks(user_id, file_type=None) == 6


async def test_search_scopes_do_not_mix_sections(
    user_id: int, library: dict[str, dict]
) -> None:
    """Поиск по разделу «Треки» не находит файлы «Другого» и наоборот."""
    document_id = int(library["document"]["id"])
    audio_id = int(library["audio"]["id"])

    in_other = await tracks_repo.search_in(user_id, "Отчёт", scope="other")
    in_tracks = await tracks_repo.search_in(user_id, "Отчёт", scope="tracks")
    by_title = await tracks_repo.search_in(user_id, "Песня о море", scope="tracks")

    assert document_id in _ids(in_other)
    assert _file_types(in_other) <= set(tracks_repo.NON_AUDIO_FILE_TYPES)
    assert document_id not in _ids(in_tracks)
    assert _file_types(in_tracks) <= {"audio"}
    assert audio_id in _ids(by_title)


async def test_created_file_type_is_validated(media_factory: MediaFactory) -> None:
    """Тип файла проверяется при создании записи — мусор в базу не попадёт."""
    with pytest.raises(ValidationError):
        await media_factory("Стикер", file_type="sticker")


# ===========================================================================
# 3. Отправка пользователю: метод Bot API по file_type
# ===========================================================================


@pytest.mark.parametrize(
    ("file_type", "expected_method"),
    [
        pytest.param("audio", SendAudio, id="audio"),
        pytest.param("document", SendDocument, id="document"),
        pytest.param("video", SendVideo, id="video"),
        pytest.param("video_note", SendVideoNote, id="video_note"),
        pytest.param("voice", SendVoice, id="voice"),
    ],
)
async def test_send_media_uses_method_by_file_type(
    bot: Bot,
    session: FakeSession,
    user_id: int,
    media_factory: MediaFactory,
    file_type: str,
    expected_method: type,
) -> None:
    """Файл уходит пользователю тем же методом, каким он был сохранён."""
    track = await media_factory("Файл раздела", file_type=file_type)

    await media.send_media_to_user(bot, user_id, track, caption="Подпись")

    assert session.method_types() == [expected_method]


# ===========================================================================
# 4. Статистика: файлы «Другого» не должны участвовать
# ===========================================================================


@pytest.fixture
async def mixed_plays(media_factory: MediaFactory) -> dict[str, dict]:
    """Аудиотрек с 5 прослушиваниями и голосовое с 7 — чтобы голосовое было первым."""
    return {
        "audio": await media_factory("Песня для топа", file_type="audio", play_count=5),
        "voice": await media_factory(
            "Голосовое без музыки", file_type="voice", play_count=7
        ),
        "document": await media_factory("Договор", file_type="document"),
    }


async def test_other_files_are_not_in_top(
    user_id: int, mixed_plays: dict[str, dict]
) -> None:
    """Топ прослушиваний — это раздел «Треки»: только аудио."""
    top = await stats_repo.top(user_id)

    assert _file_types(top) == {"audio"}
    assert _ids(top) == {int(mixed_plays["audio"]["id"])}


async def test_other_files_are_not_in_stats_sections(
    user_id: int, mixed_plays: dict[str, dict]
) -> None:
    """Разделы статистики и счётчик «всего» учитывают только аудио."""
    counts = await stats_repo.counts(user_id)
    recent = await stats_repo.recent(user_id)
    unplayed = await stats_repo.unplayed(user_id)

    assert counts["total"] == 1
    assert counts["total_plays"] == 5
    assert _file_types(recent) == {"audio"}
    assert int(mixed_plays["document"]["id"]) not in _ids(unplayed)
