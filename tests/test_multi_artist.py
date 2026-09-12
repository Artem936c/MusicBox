"""Регрессионные тесты нескольких исполнителей у трека (раздел 1.2 и 8 контракта V2).

Проверяется связка ``tracks.artist_id`` (денормализованный основной исполнитель)
и таблицы ``track_artists`` (источник истины для фильтра-пересечения):

* создание трека с несколькими исполнителями и порядок ``position``;
* :func:`tracks_by_artists_intersection` — ВСЕ переданные исполнители,
  один исполнитель, пустой список, чужой пользователь;
* :func:`set_track_artists` — полная замена состава и синхронизация основного;
* :func:`rename_artist` со слиянием тёзки — перенос треков и связей без дублей;
* списки треков не размножаются из-за связи с ``track_artists``.

Сеть и Telegram API не используются: всё живёт во временной базе (фикстура
``database`` из :mod:`tests.conftest`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from backend.db.database import db
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from tests.conftest import TEST_STORAGE_CHANNEL_ID, PlayCountSetter

logger = logging.getLogger(__name__)

#: Telegram-ID «чужого» пользователя для проверок изоляции.
OTHER_USER_ID: int = 515_151

#: Тип фабрики треков с произвольным составом исполнителей.
MultiTrackFactory = Callable[..., Awaitable[dict]]

#: Тип фабрики исполнителей.
ArtistFactory = Callable[..., Awaitable[dict]]


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def artist_factory(user: dict, user_id: int) -> ArtistFactory:
    """Фабрика исполнителей: создаёт исполнителя пользователя тестов."""

    async def _create(name: str, *, owner_id: int | None = None) -> dict:
        artist = await artists_repo.ensure_artist(
            owner_id if owner_id is not None else user_id, name
        )
        assert artist is not None, f"Исполнитель «{name}» не создан"
        return artist

    return _create


@pytest.fixture
def multi_track_factory(user: dict, user_id: int) -> MultiTrackFactory:
    """Фабрика треков с произвольным составом исполнителей (``artist_ids``)."""
    counter = {"value": 0}

    async def _create(
        title: str = "Трек",
        *,
        artist_ids: Sequence[int] | None = None,
        artist_id: int | None = None,
        artist: str | None = None,
        album: str | None = None,
        file_type: str = "audio",
        owner_id: int | None = None,
    ) -> dict:
        counter["value"] += 1
        index = counter["value"]
        owner = owner_id if owner_id is not None else user_id
        return await tracks_repo.create_track(
            owner,
            title=title,
            artist=artist,
            album=album,
            duration=180,
            file_size=1024,
            mime_type="audio/mpeg",
            file_name=f"{title}.mp3",
            file_id=f"multi-file-{owner}-{index}",
            file_unique_id=f"multi-unique-{owner}-{index}",
            storage_chat_id=TEST_STORAGE_CHANNEL_ID,
            storage_message_id=5000 + index,
            file_type=file_type,
            artist_id=artist_id,
            artist_ids=artist_ids,
        )

    return _create


async def _link_count(track_id: int) -> int:
    """Число строк в ``track_artists`` у трека (проверка на дубли)."""
    return int(
        await db.fetch_val(
            "SELECT COUNT(*) FROM track_artists WHERE track_id = ?",
            (int(track_id),),
            default=0,
        )
        or 0
    )


def _ids(rows: Sequence[dict[str, Any]]) -> list[int]:
    """Идентификаторы треков в порядке выдачи."""
    return [int(row["id"]) for row in rows]


# ---------------------------------------------------------------------------
# Создание трека с несколькими исполнителями
# ---------------------------------------------------------------------------


async def test_create_track_links_all_artists_in_order(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Трек с тремя исполнителями: связи по порядку, основной — первый."""
    first = await artist_factory("Кино")
    second = await artist_factory("Наутилус Помпилиус")
    third = await artist_factory("Аквариум")

    track = await multi_track_factory(
        "Общая песня",
        artist_ids=[first["id"], second["id"], third["id"]],
    )

    # Денормализованный основной исполнитель — первый из списка.
    assert track["artist_id"] == first["id"]
    # Агрегаты строки трека сохраняют порядок position.
    assert tracks_repo.parse_artist_ids(track) == [
        first["id"],
        second["id"],
        third["id"],
    ]
    assert tracks_repo.artist_names_of(track) == [
        "Кино",
        "Наутилус Помпилиус",
        "Аквариум",
    ]

    links = await tracks_repo.track_artists(user_id, int(track["id"]))
    assert [item["id"] for item in links] == [first["id"], second["id"], third["id"]]
    assert [item["position"] for item in links] == [0, 1, 2]
    assert await _link_count(int(track["id"])) == 3


async def test_create_track_keeps_explicit_primary_artist(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Явный ``artist_id`` становится основным, даже если он есть в ``artist_ids``."""
    main = await artist_factory("Мельница")
    guest = await artist_factory("Хелависа")

    track = await multi_track_factory(
        "Дуэт",
        artist_id=main["id"],
        artist_ids=[guest["id"], main["id"]],
    )

    assert track["artist_id"] == main["id"]
    assert tracks_repo.parse_artist_ids(track) == [main["id"], guest["id"]]
    # Дубль «основной указан дважды» не создаёт лишней строки связи.
    assert await _link_count(int(track["id"])) == 2


# ---------------------------------------------------------------------------
# Пересечение по нескольким исполнителям
# ---------------------------------------------------------------------------


async def test_intersection_returns_only_tracks_with_all_artists(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Выдаются только треки, у которых есть ВСЕ переданные исполнители."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")
    gamma = await artist_factory("Гамма")

    only_alpha = await multi_track_factory("Только Альфа", artist_ids=[alpha["id"]])
    alpha_beta = await multi_track_factory(
        "Альфа и Бета", artist_ids=[alpha["id"], beta["id"]]
    )
    all_three = await multi_track_factory(
        "Все трое", artist_ids=[alpha["id"], beta["id"], gamma["id"]]
    )
    only_gamma = await multi_track_factory("Только Гамма", artist_ids=[gamma["id"]])

    pair = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"]]
    )
    assert set(_ids(pair)) == {int(alpha_beta["id"]), int(all_three["id"])}
    assert int(only_alpha["id"]) not in _ids(pair)
    assert int(only_gamma["id"]) not in _ids(pair)

    trio = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"], gamma["id"]]
    )
    assert _ids(trio) == [int(all_three["id"])]

    # Порядок исполнителей в фильтре на результат не влияет.
    reversed_trio = await tracks_repo.tracks_by_artists_intersection(
        user_id, [gamma["id"], beta["id"], alpha["id"]]
    )
    assert _ids(reversed_trio) == _ids(trio)


async def test_intersection_single_artist_returns_all_his_tracks(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """По одному исполнителю выдаются все его треки — и сольные, и совместные."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")

    solo = await multi_track_factory("Сольный", artist_ids=[alpha["id"]])
    duet = await multi_track_factory("Дуэт", artist_ids=[alpha["id"], beta["id"]])
    guest = await multi_track_factory("В гостях", artist_ids=[beta["id"], alpha["id"]])
    foreign = await multi_track_factory("Чужой", artist_ids=[beta["id"]])

    found = await tracks_repo.tracks_by_artists_intersection(user_id, [alpha["id"]])

    assert set(_ids(found)) == {int(solo["id"]), int(duet["id"]), int(guest["id"])}
    assert int(foreign["id"]) not in _ids(found)


async def test_intersection_with_empty_artist_list_returns_nothing(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Пустой список исполнителей — пустая выдача (а не «все треки»)."""
    alpha = await artist_factory("Альфа")
    await multi_track_factory("Сольный", artist_ids=[alpha["id"]])

    assert await tracks_repo.tracks_by_artists_intersection(user_id, []) == []
    assert await tracks_repo.tracks_by_artists_intersection(user_id, ()) == []
    assert await tracks_repo.tracks_by_artists_intersection(user_id, None) == []
    # Мусор вместо идентификаторов отсеивается и даёт пустой фильтр.
    assert await tracks_repo.tracks_by_artists_intersection(user_id, ["нет"]) == []


async def test_intersection_does_not_duplicate_rows(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """JOIN с ``track_artists`` не размножает строки: каждый трек ровно один раз."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")
    gamma = await artist_factory("Гамма")

    track = await multi_track_factory(
        "Трио", artist_ids=[alpha["id"], beta["id"], gamma["id"]]
    )

    found = await tracks_repo.tracks_by_artists_intersection(user_id, [alpha["id"]])
    ids = _ids(found)
    assert ids == [int(track["id"])]
    assert len(ids) == len(set(ids))


async def test_intersection_respects_paging_and_query(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Постраничность и текстовый фильтр применяются к найденному множеству."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")

    first = await multi_track_factory("Первая", artist_ids=[alpha["id"], beta["id"]])
    second = await multi_track_factory("Вторая", artist_ids=[alpha["id"], beta["id"]])
    third = await multi_track_factory("Третья", artist_ids=[alpha["id"], beta["id"]])

    page = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"]], limit=2, offset=0
    )
    assert len(page) == 2
    tail = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"]], limit=2, offset=2
    )
    assert len(tail) == 1
    assert set(_ids(page) + _ids(tail)) == {
        int(first["id"]),
        int(second["id"]),
        int(third["id"]),
    }

    filtered = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"]], query="втор"
    )
    assert _ids(filtered) == [int(second["id"])]


async def test_intersection_is_isolated_by_user(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Чужие треки в пересечение не попадают, даже если id исполнителя угадан."""
    await users_repo.ensure_user(OTHER_USER_ID, username="stranger")
    mine = await artist_factory("Альфа")
    theirs = await artists_repo.ensure_artist(OTHER_USER_ID, "Альфа")
    assert theirs is not None

    my_track = await multi_track_factory("Мой", artist_ids=[mine["id"]])
    their_track = await multi_track_factory(
        "Чужой", artist_ids=[theirs["id"]], owner_id=OTHER_USER_ID
    )

    found = await tracks_repo.tracks_by_artists_intersection(
        user_id, [mine["id"], theirs["id"]]
    )
    assert found == []

    own_only = await tracks_repo.tracks_by_artists_intersection(user_id, [mine["id"]])
    assert _ids(own_only) == [int(my_track["id"])]

    stranger_only = await tracks_repo.tracks_by_artists_intersection(
        OTHER_USER_ID, [theirs["id"]]
    )
    assert _ids(stranger_only) == [int(their_track["id"])]


# ---------------------------------------------------------------------------
# Замена состава исполнителей
# ---------------------------------------------------------------------------


async def test_set_track_artists_replaces_composition(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Новый состав заменяет прежний целиком и переносит основного исполнителя."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")
    gamma = await artist_factory("Гамма")

    track = await multi_track_factory("Сменный", artist_ids=[alpha["id"], beta["id"]])
    track_id = int(track["id"])

    await tracks_repo.set_track_artists(user_id, track_id, [gamma["id"], beta["id"]])

    updated = await tracks_repo.get_track(user_id, track_id)
    assert updated is not None
    assert tracks_repo.parse_artist_ids(updated) == [gamma["id"], beta["id"]]
    # Основной исполнитель синхронизирован с первым в новом составе.
    assert updated["artist_id"] == gamma["id"]
    assert await _link_count(track_id) == 2

    links = await tracks_repo.track_artists(user_id, track_id)
    assert [item["position"] for item in links] == [0, 1]

    # Прежнего исполнителя в пересечении больше нет.
    assert await tracks_repo.tracks_by_artists_intersection(user_id, [alpha["id"]]) == []
    still_there = await tracks_repo.tracks_by_artists_intersection(
        user_id, [gamma["id"], beta["id"]]
    )
    assert _ids(still_there) == [track_id]


async def test_set_track_artists_with_empty_list_clears_composition(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Пустой список снимает всех исполнителей и обнуляет ``tracks.artist_id``."""
    alpha = await artist_factory("Альфа")
    track = await multi_track_factory("Ничей", artist_ids=[alpha["id"]])
    track_id = int(track["id"])

    await tracks_repo.set_track_artists(user_id, track_id, [])

    updated = await tracks_repo.get_track(user_id, track_id)
    assert updated is not None
    assert updated["artist_id"] is None
    assert tracks_repo.parse_artist_ids(updated) == []
    assert await _link_count(track_id) == 0


async def test_set_track_artists_ignores_foreign_and_unknown_artists(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Исполнители чужого пользователя и несуществующие id игнорируются."""
    await users_repo.ensure_user(OTHER_USER_ID, username="stranger")
    mine = await artist_factory("Альфа")
    theirs = await artists_repo.ensure_artist(OTHER_USER_ID, "Чужой кумир")
    assert theirs is not None

    track = await multi_track_factory("Свой", artist_ids=[mine["id"]])
    track_id = int(track["id"])

    await tracks_repo.set_track_artists(
        user_id, track_id, [mine["id"], int(theirs["id"]), 10_000_001]
    )

    updated = await tracks_repo.get_track(user_id, track_id)
    assert updated is not None
    assert tracks_repo.parse_artist_ids(updated) == [mine["id"]]
    assert await _link_count(track_id) == 1


async def test_set_track_artists_for_foreign_track_changes_nothing(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Чужой трек не меняется: состав исполнителей остаётся прежним."""
    await users_repo.ensure_user(OTHER_USER_ID, username="stranger")
    theirs = await artists_repo.ensure_artist(OTHER_USER_ID, "Чужой кумир")
    assert theirs is not None
    mine = await artist_factory("Альфа")

    their_track = await multi_track_factory(
        "Чужой", artist_ids=[theirs["id"]], owner_id=OTHER_USER_ID
    )
    track_id = int(their_track["id"])

    await tracks_repo.set_track_artists(user_id, track_id, [mine["id"]])

    untouched = await tracks_repo.get_track(OTHER_USER_ID, track_id)
    assert untouched is not None
    assert tracks_repo.parse_artist_ids(untouched) == [int(theirs["id"])]
    assert await _link_count(track_id) == 1


# ---------------------------------------------------------------------------
# Переименование со слиянием тёзок
# ---------------------------------------------------------------------------


async def test_rename_artist_merges_namesake_and_moves_tracks(
    artist_factory: ArtistFactory,
    multi_track_factory: MultiTrackFactory,
    user_id: int,
    set_play_count: PlayCountSetter,
) -> None:
    """Слияние тёзок переносит треки и связи и не плодит дубликатов."""
    survivor = await artist_factory("Кино")
    duplicate = await artist_factory("KINO")
    guest = await artist_factory("Гость")

    # Трек только у выжившего, только у дубликата и трек, где оба сразу.
    own = await multi_track_factory("Только выживший", artist_ids=[survivor["id"]])
    foreign = await multi_track_factory("Только дубликат", artist_ids=[duplicate["id"]])
    both = await multi_track_factory(
        "Оба сразу", artist_ids=[survivor["id"], duplicate["id"], guest["id"]]
    )
    await set_play_count(int(own["id"]), 3)
    await set_play_count(int(foreign["id"]), 2)
    await set_play_count(int(both["id"]), 5)

    merged = await artists_repo.rename_artist(user_id, int(duplicate["id"]), "Кино")

    assert merged is not None
    assert int(merged["id"]) == int(survivor["id"])
    assert merged["name"] == "Кино"
    # Дубликат исчез, тёзок в библиотеке не осталось.
    assert await artists_repo.get_artist(user_id, int(duplicate["id"])) is None
    assert [item["name"] for item in await artists_repo.list_artists(user_id)] == [
        "Гость",
        "Кино",
    ]

    # Основной исполнитель треков дубликата перевешен на выжившего.
    moved = await tracks_repo.get_track(user_id, int(foreign["id"]))
    assert moved is not None
    assert moved["artist_id"] == survivor["id"]
    assert tracks_repo.parse_artist_ids(moved) == [int(survivor["id"])]

    # Трек, где были оба, получил ОДНУ связь — дубля связи не появилось.
    combined = await tracks_repo.get_track(user_id, int(both["id"]))
    assert combined is not None
    assert tracks_repo.parse_artist_ids(combined) == [
        int(survivor["id"]),
        int(guest["id"]),
    ]
    assert tracks_repo.artist_names_of(combined) == ["Кино", "Гость"]
    assert await _link_count(int(both["id"])) == 2

    # В пересечении каждый трек ровно один раз.
    found = await tracks_repo.tracks_by_artists_intersection(
        user_id, [int(survivor["id"])]
    )
    ids = _ids(found)
    assert sorted(ids) == sorted(
        [int(own["id"]), int(foreign["id"]), int(both["id"])]
    )
    assert len(ids) == len(set(ids))

    # Счётчик прослушиваний выжившего пересчитан по всем перенесённым трекам.
    refreshed = await artists_repo.get_artist(user_id, int(survivor["id"]))
    assert refreshed is not None
    assert refreshed["total_plays"] == 10


async def test_rename_artist_merge_keeps_single_link_per_track(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """После слияния в ``track_artists`` нет пары строк на один и тот же трек."""
    survivor = await artist_factory("Сплин")
    duplicate = await artist_factory("SPLEAN")

    first = await multi_track_factory(
        "Оба", artist_ids=[survivor["id"], duplicate["id"]]
    )
    second = await multi_track_factory("Оба ещё раз", artist_ids=[duplicate["id"], survivor["id"]])

    await artists_repo.rename_artist(user_id, int(duplicate["id"]), "Сплин")

    for track in (first, second):
        assert await _link_count(int(track["id"])) == 1
    orphans = await db.fetch_val(
        "SELECT COUNT(*) FROM track_artists WHERE artist_id = ?",
        (int(duplicate["id"]),),
        default=0,
    )
    assert int(orphans or 0) == 0


async def test_rename_artist_without_namesake_keeps_composition(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Обычное переименование (без тёзки) не трогает состав исполнителей."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")
    track = await multi_track_factory("Дуэт", artist_ids=[alpha["id"], beta["id"]])

    renamed = await artists_repo.rename_artist(user_id, int(alpha["id"]), "Альфа-2")

    assert renamed is not None
    assert int(renamed["id"]) == int(alpha["id"])
    assert renamed["name"] == "Альфа-2"

    updated = await tracks_repo.get_track(user_id, int(track["id"]))
    assert updated is not None
    assert tracks_repo.parse_artist_ids(updated) == [alpha["id"], beta["id"]]
    assert tracks_repo.artist_names_of(updated) == ["Альфа-2", "Бета"]
    assert await _link_count(int(track["id"])) == 2


# ---------------------------------------------------------------------------
# Списки треков не дублируются
# ---------------------------------------------------------------------------


async def test_track_lists_are_not_duplicated_by_track_artists(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Список, счётчик, избранное и поиск отдают трек с тремя исполнителями один раз."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")
    gamma = await artist_factory("Гамма")

    track = await multi_track_factory(
        "Уникальная песня",
        artist="Альфа, Бета, Гамма",
        artist_ids=[alpha["id"], beta["id"], gamma["id"]],
    )
    track_id = int(track["id"])

    listed = await tracks_repo.list_tracks(user_id, limit=100)
    assert _ids(listed) == [track_id]
    assert await tracks_repo.count_tracks(user_id) == 1

    by_artist = await tracks_repo.list_tracks(user_id, artist_id=alpha["id"], limit=100)
    assert _ids(by_artist) == [track_id]

    by_ids = await tracks_repo.get_tracks_by_ids(user_id, [track_id, track_id])
    assert _ids(by_ids) == [track_id]

    assert await favourites_repo.add(user_id, track_id) is True
    favourites = await favourites_repo.list_favourites(user_id)
    assert _ids(favourites) == [track_id]
    assert await favourites_repo.count(user_id) == 1

    found = await tracks_repo.search_in(user_id, "Уникальная", scope="tracks")
    assert _ids(found) == [track_id]

    unplayed = await artists_repo.artist_unplayed_tracks(user_id, int(beta["id"]))
    assert _ids(unplayed) == [track_id]


async def test_autosort_primary_artist_is_visible_in_intersection(
    multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Исполнитель, проставленный автосортировкой, должен участвовать в пересечении."""
    from backend.services import autosort

    track = await multi_track_factory("Импортированная песня", artist="Кино")
    assert track["artist_id"] is None
    assert tracks_repo.parse_artist_ids(track) == []

    result = await autosort.apply_autosort(user_id, dict(track), force=True)
    assert result.artist is not None
    artist_id = int(result.artist["id"])

    refreshed = await tracks_repo.get_track(user_id, int(track["id"]))
    assert refreshed is not None
    # Денормализованный основной исполнитель проставлен...
    assert refreshed["artist_id"] == artist_id
    assert _ids(await tracks_repo.list_tracks(user_id, artist_id=artist_id)) == [
        int(track["id"])
    ]

    # ...а связь в track_artists — нет, поэтому обе проверки ниже падают.
    assert await _link_count(int(track["id"])) == 1
    found = await tracks_repo.tracks_by_artists_intersection(user_id, [artist_id])
    assert _ids(found) == [int(track["id"])]


async def test_artist_track_count_is_not_inflated_by_links(
    artist_factory: ArtistFactory, multi_track_factory: MultiTrackFactory, user_id: int
) -> None:
    """Агрегаты исполнителя считают треки, а не строки связей."""
    alpha = await artist_factory("Альфа")
    beta = await artist_factory("Бета")

    await multi_track_factory("Первый", artist_ids=[alpha["id"], beta["id"]])
    await multi_track_factory("Второй", artist_ids=[alpha["id"], beta["id"]])

    listed = {item["name"]: item for item in await artists_repo.list_artists(user_id)}
    # Каждый трек считается ОДИН раз, а не по числу связей в track_artists:
    # у обоих исполнителей по 2 трека, а не по 4.
    # Считается УЧАСТИЕ (track_artists), а не только основной исполнитель, —
    # приглашённый исполнитель тоже поёт на этих треках. Этот же смысл обязан
    # быть у списка треков исполнителя, иначе карточка покажет «2 трека»,
    # а при открытии окажется пусто.
    assert listed["Альфа"]["track_count"] == 2
    assert listed["Бета"]["track_count"] == 2

    for artist in (alpha, beta):
        own = await tracks_repo.list_tracks(user_id, artist_id=artist["id"], limit=50)
        assert len(own) == 2, f"список треков «{artist['name']}» разошёлся с track_count"

    both = await tracks_repo.tracks_by_artists_intersection(
        user_id, [alpha["id"], beta["id"]]
    )
    assert len(both) == 2
