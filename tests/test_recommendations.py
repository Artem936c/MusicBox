"""Регрессионные тесты рекомендаций исполнителей (раздел 8 контракта V2).

Проверяется сервис :mod:`backend.services.recommendations` целиком — вместе с
репозиторием-запросником, на настоящей (временной) базе. Сеть не нужна:
рекомендации строятся ТОЛЬКО по данным самой базы MusicBox.

Что фиксируют тесты:

* пользователь без прослушиваний получает пустые списки и подсказку `note`;
* две категории («Популярные» и «Менее известные») не пересекаются;
* «Популярные» отсортированы по убыванию прослушиваний, «Менее известные» —
  по возрастанию;
* уже прослушанные исполнители в рекомендации не попадают;
* при нехватке кандидатов честно заполняются `shortfall` и `note`, а имена
  исполнителей не выдумываются — все они есть в базе.

Данные готовятся «как в жизни»: трек + исполнитель + вызовы
``tracks.register_play`` (именно он поднимает ``artists.total_plays``, по
которому считается история прослушиваний).
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import pytest

from backend.db.database import db
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.services import recommendations as reco
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

#: Telegram-ID «соседа» — пользователя с похожими вкусами.
NEIGHBOUR_ID: int = 515_151

#: Исполнитель, которого слушает и наш пользователь, и сосед (точка пересечения).
SHARED_ARTIST: str = "Кино"

#: Кандидаты соседа: имя -> число прослушиваний (все значения разные).
NEIGHBOUR_LIBRARY: tuple[tuple[str, int], ...] = (
    ("Аквариум", 6),
    ("Наутилус Помпилиус", 5),
    ("ДДТ", 4),
    ("Сплин", 3),
    ("Мумий Тролль", 2),
    ("Земфира", 1),
)

#: Сквозной счётчик файлов — file_unique_id должен быть уникальным.
_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# Помощники подготовки данных
# ---------------------------------------------------------------------------


async def _add_track(
    user_id: int,
    *,
    artist: str,
    title: str | None = None,
    plays: int = 0,
    genre: str | None = None,
) -> dict:
    """Создаёт трек с исполнителем и «проигрывает» его `plays` раз."""
    artist_row = await artists_repo.ensure_artist(user_id, artist)
    assert artist_row is not None, f"Не удалось создать исполнителя «{artist}»"

    index = next(_counter)
    track = await tracks_repo.create_track(
        user_id,
        title=title or f"{artist} — трек {index}",
        artist=artist,
        duration=180,
        file_size=1024,
        mime_type="audio/mpeg",
        file_name=f"track-{index}.mp3",
        file_id=f"reco-file-{index}",
        file_unique_id=f"reco-unique-{index}",
        storage_chat_id=-100123,
        storage_message_id=3000 + index,
        artist_id=int(artist_row["id"]),
        genre=genre,
    )
    for _ in range(max(int(plays), 0)):
        await tracks_repo.register_play(user_id, int(track["id"]), source="test")
    return track


def _names(items: list[Any]) -> list[str]:
    """Нормализованные имена рекомендаций в порядке выдачи."""
    return [item.normalized_name for item in items]


def _plays(items: list[Any]) -> list[int]:
    """Число прослушиваний рекомендаций в порядке выдачи."""
    return [int(item.total_plays) for item in items]


async def _known_artist_names() -> set[str]:
    """Все имена исполнителей, которые реально есть в базе (у любого пользователя)."""
    rows = await db.fetch_all("SELECT DISTINCT normalized_name FROM artists")
    return {str(row["normalized_name"]) for row in rows if row.get("normalized_name")}


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
async def neighbour(user: dict) -> int:
    """Второй пользователь базы — «сосед» с похожими вкусами."""
    await users_repo.ensure_user(
        NEIGHBOUR_ID,
        username="neighbour",
        first_name="Сосед",
        last_name=None,
        language_code="ru",
    )
    return NEIGHBOUR_ID


@pytest.fixture
async def shared_taste(user_id: int, neighbour: int) -> dict[str, int]:
    """Общий вкус: оба слушают «Кино», у соседа сверх того шесть исполнителей.

    Возвращает сопоставление «нормализованное имя -> прослушивания в базе»
    для кандидатов соседа.
    """
    await _add_track(user_id, artist=SHARED_ARTIST, plays=2)
    await _add_track(neighbour, artist=SHARED_ARTIST, plays=1)

    expected: dict[str, int] = {}
    for name, plays in NEIGHBOUR_LIBRARY:
        await _add_track(neighbour, artist=name, plays=plays)
        expected[normalize_name(name)] = plays
    return expected


# ===========================================================================
# 1. Пользователь без прослушиваний
# ===========================================================================


async def test_without_history_lists_are_empty_with_note(
    user_id: int, user: dict
) -> None:
    """Библиотека есть, прослушиваний нет — пустые списки и подсказка в `note`."""
    await _add_track(user_id, artist="Кино", plays=0)

    result = await reco.build(user_id, limit=5)

    assert result["popular"] == []
    assert result["underground"] == []
    assert result["shortfall"] == {"popular": 5, "underground": 5}
    assert result["note"] == reco.NO_HISTORY_NOTE


async def test_empty_library_lists_are_empty_with_note(user_id: int, user: dict) -> None:
    """Совсем пустая библиотека — тот же честный ответ, без исключений."""
    result = await reco.build(user_id)

    assert result["popular"] == []
    assert result["underground"] == []
    assert result["shortfall"] == {
        "popular": reco.DEFAULT_LIMIT,
        "underground": reco.DEFAULT_LIMIT,
    }
    assert result["note"] == reco.NO_HISTORY_NOTE


# ===========================================================================
# 2. Две непересекающиеся категории и их порядок
# ===========================================================================


async def test_categories_do_not_intersect(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """Исполнитель попадает ровно в одну категорию."""
    result = await reco.build(user_id, limit=3)

    popular = _names(result["popular"])
    underground = _names(result["underground"])

    assert popular, "«Популярные» не должны быть пустыми при шести кандидатах"
    assert underground, "«Менее известные» не должны быть пустыми при шести кандидатах"
    assert set(popular) & set(underground) == set()
    assert len(set(popular)) == len(popular)
    assert len(set(underground)) == len(underground)


async def test_popular_sorted_desc_underground_sorted_asc(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """«Популярные» — по убыванию прослушиваний, «Менее известные» — по возрастанию."""
    result = await reco.build(user_id, limit=3)

    popular_plays = _plays(result["popular"])
    underground_plays = _plays(result["underground"])

    assert popular_plays == [6, 5, 4]
    assert underground_plays == [1, 2, 3]
    assert popular_plays == sorted(popular_plays, reverse=True)
    assert underground_plays == sorted(underground_plays)
    # «Менее известные» — с ненулевыми, но наименьшими прослушиваниями.
    assert min(popular_plays) > max(underground_plays)
    assert all(count > 0 for count in underground_plays)


async def test_total_plays_match_database(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """Число прослушиваний в рекомендации совпадает с фактическим в базе."""
    result = await reco.build(user_id, limit=3)

    for item in [*result["popular"], *result["underground"]]:
        assert item.total_plays == shared_taste[item.normalized_name]
        assert item.listeners == 1
        assert item.reason, "У каждой рекомендации должно быть объяснение"


# ===========================================================================
# 3. Прослушанное не рекомендуется
# ===========================================================================


async def test_listened_artists_are_never_recommended(
    user_id: int, neighbour: int
) -> None:
    """Исполнители из истории пользователя в подборку не попадают."""
    await _add_track(user_id, artist=SHARED_ARTIST, plays=2)
    await _add_track(user_id, artist="Аквариум", plays=3)

    # У соседа те же двое (популярнее) плюс один новый.
    await _add_track(neighbour, artist=SHARED_ARTIST, plays=9)
    await _add_track(neighbour, artist="Аквариум", plays=8)
    await _add_track(neighbour, artist="Гражданская оборона", plays=4)

    result = await reco.build(user_id, limit=5)
    recommended = set(_names(result["popular"]) + _names(result["underground"]))

    assert normalize_name(SHARED_ARTIST) not in recommended
    assert normalize_name("Аквариум") not in recommended
    assert normalize_name("Гражданская оборона") in recommended


async def test_listened_artist_stays_excluded_after_extra_plays(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """Даже если пользователь послушает кандидата, тот исчезает из рекомендаций."""
    before = await reco.build(user_id, limit=3)
    target = before["popular"][0]
    assert target.normalized_name == normalize_name("Аквариум")

    # Пользователь добавил себе этого исполнителя и послушал его.
    await _add_track(user_id, artist="Аквариум", plays=1)

    after = await reco.build(user_id, limit=3)
    recommended = set(_names(after["popular"]) + _names(after["underground"]))

    assert target.normalized_name not in recommended


# ===========================================================================
# 4. Деградация: мало кандидатов
# ===========================================================================


@pytest.fixture
async def scarce_base(user_id: int, neighbour: int) -> dict[str, Any]:
    """Бедная база: один настоящий кандидат и один свой непрослушанный исполнитель."""
    await _add_track(user_id, artist=SHARED_ARTIST, plays=1)
    # Свой исполнитель без единого прослушивания — материал для добивки.
    await _add_track(user_id, artist="Сплин", plays=0)

    await _add_track(neighbour, artist=SHARED_ARTIST, plays=1)
    await _add_track(neighbour, artist="Аквариум", plays=2)
    return {"candidate": normalize_name("Аквариум"), "filler": normalize_name("Сплин")}


async def test_shortfall_and_note_when_candidates_are_scarce(
    user_id: int, scarce_base: dict[str, Any]
) -> None:
    """Не хватило кандидатов — честные `shortfall` и `note`, а не выдуманные имена."""
    limit = reco.DEFAULT_LIMIT
    result = await reco.build(user_id, limit=limit)

    popular = result["popular"]
    underground = result["underground"]

    assert len(popular) < limit
    assert len(underground) < limit
    assert result["shortfall"]["popular"] == limit - len(popular)
    assert result["shortfall"]["underground"] == limit - len(underground)
    assert result["shortfall"]["popular"] > 0
    assert result["shortfall"]["underground"] > 0

    note = result["note"]
    assert note, "При неполных категориях `note` обязателен"
    assert reco.SMALL_BASE_REASON in note
    assert str(limit) in note


async def test_degradation_fills_only_from_own_library(
    user_id: int, scarce_base: dict[str, Any]
) -> None:
    """Добивка — непрослушанные исполнители самого пользователя, с честной пометкой."""
    result = await reco.build(user_id, limit=reco.DEFAULT_LIMIT)

    everything = [*result["popular"], *result["underground"]]
    by_name = {item.normalized_name: item for item in everything}

    assert scarce_base["candidate"] in by_name
    assert by_name[scarce_base["candidate"]].reason != reco.REASON_LIBRARY

    filler = by_name.get(scarce_base["filler"])
    assert filler is not None, "Свой непрослушанный исполнитель должен добить категорию"
    assert filler.reason == reco.REASON_LIBRARY
    assert filler.local_artist_id, "У добивки из библиотеки есть локальный исполнитель"
    assert filler.sample_track_ids, "К своему исполнителю прикладываются его треки"

    # Категории по-прежнему не пересекаются.
    assert set(_names(result["popular"])) & set(_names(result["underground"])) == set()


async def test_recommendations_are_never_invented(
    user_id: int, scarce_base: dict[str, Any]
) -> None:
    """Ни одного имени «из воздуха»: всё, что предложено, есть в базе."""
    known = await _known_artist_names()
    result = await reco.build(user_id, limit=reco.DEFAULT_LIMIT)

    for item in [*result["popular"], *result["underground"]]:
        assert item.normalized_name in known, f"Выдуманный исполнитель: {item.name}"
        assert item.name.strip(), "Имя рекомендации не может быть пустым"


async def test_rich_base_needs_no_degradation_note(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """Когда кандидатов хватает, `note` не нужен, а `shortfall` — нулевой."""
    result = await reco.build(user_id, limit=3)

    assert result["shortfall"] == {"popular": 0, "underground": 0}
    assert result["note"] is None
    assert all(
        item.reason != reco.REASON_LIBRARY
        for item in [*result["popular"], *result["underground"]]
    )


async def test_limit_is_clamped_to_allowed_range(
    user_id: int, shared_taste: dict[str, int]
) -> None:
    """limit приводится к 1..MAX_LIMIT — выдача не разрастается от мусора в запросе."""
    huge = await reco.build(user_id, limit=reco.MAX_LIMIT + 100)
    tiny = await reco.build(user_id, limit=0)

    assert len(huge["popular"]) <= reco.MAX_LIMIT
    assert huge["shortfall"]["popular"] == reco.MAX_LIMIT - len(huge["popular"])
    assert len(tiny["popular"]) <= 1
    assert len(tiny["underground"]) <= 1
