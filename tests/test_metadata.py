"""Тесты разбора метаданных: имена файлов, названия, исполнители, длительность."""

from __future__ import annotations

import logging

import pytest

from backend.services.metadata import (
    DEFAULT_TITLE,
    NO_DURATION,
    clean_title,
    format_duration,
    guess_from_filename,
    normalize_name,
    parse_telegram_audio,
    primary_artist,
    split_artists,
)

logger = logging.getLogger(__name__)


class FakeAudio:
    """Минимальная замена `aiogram.types.Audio` для проверки разбора."""

    def __init__(
        self,
        *,
        title: str | None = None,
        performer: str | None = None,
        duration: int | None = None,
        file_name: str | None = None,
    ) -> None:
        self.title = title
        self.performer = performer
        self.duration = duration
        self.file_name = file_name


# ---------------------------------------------------------------------------
# guess_from_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("Artist - Title.mp3", ("Artist", "Title")),
        ("01. Artist — Title.flac", ("Artist", "Title")),
        ("03 - Кино - Группа крови.mp3", ("Кино", "Группа крови")),
        ("Artist_-_Title.flac", ("Artist", "Title")),
        ("Кино — Кукушка.m4a", ("Кино", "Кукушка")),
        ("Просто название.mp3", (None, "Просто название")),
    ],
)
def test_guess_from_filename(
    file_name: str, expected: tuple[str | None, str | None]
) -> None:
    """Из имени файла достаются исполнитель и название."""
    assert guess_from_filename(file_name) == expected


def test_guess_from_filename_empty() -> None:
    """Пустое имя файла — оба значения None."""
    assert guess_from_filename(None) == (None, None)
    assert guess_from_filename("   ") == (None, None)


def test_guess_from_filename_strips_path() -> None:
    """Путь до файла отбрасывается, разбирается только имя."""
    assert guess_from_filename("music/rock/Кино - Кукушка.mp3") == ("Кино", "Кукушка")


# ---------------------------------------------------------------------------
# clean_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Кукушка.mp3", "Кукушка"),
        ("Название_трека.flac", "Название трека"),
        ("Song [Official Video]", "Song"),
        ("Song (Official Audio)", "Song"),
        ("Song (lyrics)", "Song"),
        ("01. Группа крови", "Группа крови"),
        ("   Много    пробелов   ", "Много пробелов"),
    ],
)
def test_clean_title(raw: str, expected: str) -> None:
    """Название очищается от расширения, подчёркиваний и рекламного мусора."""
    assert clean_title(raw) == expected


def test_clean_title_empty() -> None:
    """Пустое значение превращается в пустую строку."""
    assert clean_title(None) == ""
    assert clean_title("   ") == ""


# ---------------------------------------------------------------------------
# Исполнители
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("performer", "expected"),
    [
        ("Кино", ["Кино"]),
        ("Artist feat. Other", ["Artist", "Other"]),
        ("Artist ft. Other & Third", ["Artist", "Other", "Third"]),
        ("Artist, Other", ["Artist", "Other"]),
        ("Artist vs Other", ["Artist", "Other"]),
        ("Artist x Other", ["Artist", "Other"]),
        ("AC/DC", ["AC/DC"]),
    ],
)
def test_split_artists(performer: str, expected: list[str]) -> None:
    """Строка исполнителей разбивается по общепринятым разделителям."""
    assert split_artists(performer) == expected


def test_split_artists_removes_duplicates() -> None:
    """Повторы (в том числе в другом регистре) убираются."""
    assert split_artists("Кино, кино, КИНО") == ["Кино"]


def test_split_artists_empty() -> None:
    """Пустое значение — пустой список."""
    assert split_artists(None) == []
    assert split_artists("  ") == []


def test_primary_artist() -> None:
    """Основной исполнитель — первый в списке."""
    assert primary_artist("Artist feat. Other") == "Artist"
    assert primary_artist("Кино") == "Кино"
    assert primary_artist(None) is None
    assert primary_artist("   ") is None


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Кино  ", "кино"),
        ("КИНО", "кино"),
        ("Ёлка", "елка"),
        ("Много    пробелов", "много пробелов"),
        (None, ""),
    ],
)
def test_normalize_name(raw: str | None, expected: str) -> None:
    """Нормализация имени: casefold, схлопывание пробелов, «ё» → «е»."""
    assert normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# parse_telegram_audio
# ---------------------------------------------------------------------------


def test_parse_telegram_audio_uses_tags() -> None:
    """Теги Telegram приоритетнее имени файла."""
    metadata = parse_telegram_audio(
        FakeAudio(
            title="Группа крови",
            performer="Кино",
            duration=284,
            file_name="кино - другое.mp3",
        )
    )

    assert metadata.title == "Группа крови"
    assert metadata.artist == "Кино"
    assert metadata.duration == 284
    assert metadata.file_name == "кино - другое.mp3"


def test_parse_telegram_audio_falls_back_to_filename() -> None:
    """Без тегов метаданные берутся из имени файла."""
    metadata = parse_telegram_audio(FakeAudio(file_name="Кино - Кукушка.mp3"))

    assert metadata.title == "Кукушка"
    assert metadata.artist == "Кино"
    assert metadata.duration == 0


def test_parse_telegram_audio_default_title() -> None:
    """Если ничего не известно — название «Без названия»."""
    metadata = parse_telegram_audio(FakeAudio())

    assert metadata.title == DEFAULT_TITLE
    assert metadata.artist is None
    assert metadata.album is None


# ---------------------------------------------------------------------------
# parse_telegram_audio: альбом из имени файла
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        # Классическая схема «Исполнитель - Альбом - Название».
        (
            "Кино - Группа крови - Спокойная ночь.mp3",
            ("Кино", "Группа крови", "Спокойная ночь"),
        ),
        # Номер трека посередине не мешает.
        (
            "Кино - Группа крови - 03 - Спокойная ночь.mp3",
            ("Кино", "Группа крови", "Спокойная ночь"),
        ),
        # Подчёркивания вместо пробелов.
        (
            "Кино_-_Группа_крови_-_Спокойная_ночь.mp3",
            ("Кино", "Группа крови", "Спокойная ночь"),
        ),
        # Длинное тире и путь к файлу.
        (
            "music/Pink Floyd — The Wall — Hey You.flac",
            ("Pink Floyd", "The Wall", "Hey You"),
        ),
    ],
)
def test_parse_telegram_audio_reads_album_from_filename(
    file_name: str, expected: tuple[str, str, str]
) -> None:
    """Из «Исполнитель - Альбом - Название» достаётся альбом, а не кусок названия."""
    metadata = parse_telegram_audio(FakeAudio(file_name=file_name))

    assert (metadata.artist, metadata.album, metadata.title) == expected


def test_parse_telegram_audio_album_with_year() -> None:
    """Год в скобках после альбома попадает в отдельное поле."""
    metadata = parse_telegram_audio(
        FakeAudio(file_name="Pink Floyd - The Wall (1979) - Hey You.mp3")
    )

    assert metadata.album == "The Wall"
    assert metadata.year == 1979
    assert metadata.title == "Hey You"


@pytest.mark.parametrize(
    "file_name",
    [
        # Обычное «Исполнитель - Название»: альбома тут нет.
        "Кино - Кукушка.mp3",
        "01. Кино - Кукушка.mp3",
        # Средняя часть — пометка версии, а не альбом.
        "Кино - Кукушка - Live.mp3",
        "Nirvana - Lithium - Remix.mp3",
        # Совсем без разделителей.
        "Просто название.mp3",
    ],
)
def test_parse_telegram_audio_does_not_invent_album(file_name: str) -> None:
    """Альбом не выдумывается: непонятная схема — значит альбом неизвестен."""
    metadata = parse_telegram_audio(FakeAudio(file_name=file_name))

    assert metadata.album is None
    assert metadata.year is None


def test_parse_telegram_audio_album_survives_telegram_tags() -> None:
    """Теги Telegram перебивают название и исполнителя, но альбом остаётся из имени."""
    metadata = parse_telegram_audio(
        FakeAudio(
            title="Спокойная ночь",
            performer="Кино",
            duration=284,
            file_name="Кино - Группа крови - Спокойная ночь.mp3",
        )
    )

    assert metadata.artist == "Кино"
    assert metadata.title == "Спокойная ночь"
    assert metadata.album == "Группа крови"
    assert metadata.duration == 284


def test_parse_telegram_audio_album_from_explicit_file_name() -> None:
    """Имя файла, переданное вторым аргументом, важнее поля `file_name` объекта."""
    metadata = parse_telegram_audio(
        FakeAudio(file_name="audio_2024.mp3"),
        "Кино - Группа крови - Спокойная ночь.mp3",
    )

    assert metadata.album == "Группа крови"
    assert metadata.title == "Спокойная ночь"
    assert metadata.file_name == "Кино - Группа крови - Спокойная ночь.mp3"


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (187, "3:07"),
        (60, "1:00"),
        (59, "0:59"),
        (3723, "1:02:03"),
        (0, NO_DURATION),
        (-5, NO_DURATION),
        (None, NO_DURATION),
    ],
)
def test_format_duration(seconds: int | None, expected: str) -> None:
    """Длительность выводится как «3:07» / «1:02:03», иначе — прочерк."""
    assert format_duration(seconds) == expected
