"""Регрессия вложенных папок (раздел 8 контракта V2: дерево, крошки, обход, запрет цикла).

Проверяется репозиторий :mod:`backend.db.repositories.folders` и рекурсивный
фильтр по папке в :mod:`backend.db.repositories.tracks`. Каждый тест работает на
своей временной базе (фикстура ``database`` из ``tests/conftest.py``), сети и
Telegram здесь нет.

Отдельно зафиксировано ограничение SQLite, описанное в разделе 1.1 контракта:
``UNIQUE (user_id, section, parent_folder_id, normalized_name)`` не ловит дубли
КОРНЕВЫХ папок (``NULL != NULL``), поэтому уникальность корня держит код
репозитория — и это покрыто тестом.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from backend.db.database import db
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import NotFoundError, ValidationError
from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)

#: Предел вложенности из контракта (корень — уровень 1).
MAX_DEPTH: int = folders_repo.MAX_FOLDER_DEPTH


async def _make_chain(user_id: int, levels: int, *, prefix: str = "L") -> list[dict]:
    """Создаёт цепочку из ``levels`` вложенных папок и возвращает её сверху вниз."""
    chain: list[dict] = []
    parent_id: int | None = None
    for level in range(1, levels + 1):
        folder = await folders_repo.create_folder(
            user_id, f"{prefix}{level}", parent_folder_id=parent_id
        )
        chain.append(folder)
        parent_id = int(folder["id"])
    return chain


async def _raw_parent(folder_id: int) -> Any:
    """Родитель папки прямо из базы (мимо репозитория) — для проверок «ничего не изменилось»."""
    return await db.fetch_val(
        "SELECT parent_folder_id FROM folders WHERE id = ?", (int(folder_id),)
    )


# ---------------------------------------------------------------------------
# Создание вложенных папок
# ---------------------------------------------------------------------------


async def test_create_nested_folder_sets_parent_and_depth(user_id: int, user: dict) -> None:
    """Подпапка знает своего родителя, свою глубину и раздел родителя."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "90-е", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(
        user_id, "Альбомы", parent_folder_id=child["id"]
    )

    assert root["parent_folder_id"] is None
    assert root["depth"] == 0
    assert root["section"] == folders_repo.DEFAULT_SECTION

    assert child["parent_folder_id"] == root["id"]
    assert child["depth"] == 1
    assert grandchild["parent_folder_id"] == child["id"]
    assert grandchild["depth"] == 2

    # has_children у родителя пересчитывается при чтении.
    refreshed = await folders_repo.get_folder(user_id, root["id"])
    assert refreshed is not None
    assert refreshed["has_children"] is True


async def test_nested_folder_inherits_section_of_parent(user_id: int, user: dict) -> None:
    """Раздел подпапки всегда берётся у родителя, даже если попросили другой."""
    other_root = await folders_repo.create_folder(user_id, "Документы", section="other")
    child = await folders_repo.create_folder(
        user_id, "Сканы", parent_folder_id=other_root["id"], section="music"
    )

    assert other_root["section"] == "other"
    assert child["section"] == "other"


async def test_create_folder_rejects_unknown_parent(user_id: int, user: dict) -> None:
    """Несуществующий (или чужой) родитель — ошибка, а не молчаливая корневая папка."""
    with pytest.raises(ValidationError):
        await folders_repo.create_folder(user_id, "Сирота", parent_folder_id=999_999)

    assert await folders_repo.list_folders(user_id, section=None) == []


async def test_folders_are_isolated_per_user(user_id: int, user: dict) -> None:
    """Нельзя вложить свою папку в чужую и нельзя её увидеть."""
    from backend.db.repositories import users as users_repo

    stranger_id = user_id + 1
    await users_repo.ensure_user(stranger_id, username="stranger")
    stranger_folder = await folders_repo.create_folder(stranger_id, "Чужая")

    assert await folders_repo.get_folder(user_id, stranger_folder["id"]) is None
    with pytest.raises(ValidationError):
        await folders_repo.create_folder(
            user_id, "Своя", parent_folder_id=stranger_folder["id"]
        )


# ---------------------------------------------------------------------------
# Дерево и крошки
# ---------------------------------------------------------------------------


async def test_folder_tree_is_nested_and_alphabetical(user_id: int, user: dict) -> None:
    """``folder_tree`` отдаёт корни с детьми, на каждом уровне — по алфавиту."""
    rock = await folders_repo.create_folder(user_id, "Рок")
    jazz = await folders_repo.create_folder(user_id, "Джаз")
    # Дети создаются намеренно не по алфавиту.
    await folders_repo.create_folder(user_id, "Панк", parent_folder_id=rock["id"])
    await folders_repo.create_folder(user_id, "Гранж", parent_folder_id=rock["id"])
    metal = await folders_repo.create_folder(user_id, "Метал", parent_folder_id=rock["id"])
    await folders_repo.create_folder(user_id, "Дум", parent_folder_id=metal["id"])

    tree = await folders_repo.folder_tree(user_id)

    assert [node["name"] for node in tree] == ["Джаз", "Рок"]

    jazz_node, rock_node = tree
    assert jazz_node["id"] == jazz["id"]
    assert jazz_node["children"] == []

    assert [node["name"] for node in rock_node["children"]] == ["Гранж", "Метал", "Панк"]
    assert [node["depth"] for node in rock_node["children"]] == [1, 1, 1]

    metal_node = next(node for node in rock_node["children"] if node["id"] == metal["id"])
    assert [node["name"] for node in metal_node["children"]] == ["Дум"]
    assert metal_node["children"][0]["depth"] == 2
    assert metal_node["has_children"] is True


async def test_folder_tree_is_split_by_section(user_id: int, user: dict) -> None:
    """Разделы «Треки» и «Другое» — два независимых дерева."""
    music_root = await folders_repo.create_folder(user_id, "Рок")
    other_root = await folders_repo.create_folder(user_id, "Документы", section="other")
    await folders_repo.create_folder(user_id, "Сканы", parent_folder_id=other_root["id"])

    music_tree = await folders_repo.folder_tree(user_id)
    other_tree = await folders_repo.folder_tree(user_id, section="other")

    assert [node["id"] for node in music_tree] == [music_root["id"]]
    assert [node["id"] for node in other_tree] == [other_root["id"]]
    assert [node["name"] for node in other_tree[0]["children"]] == ["Сканы"]

    both = await folders_repo.folder_tree(user_id, section=None)
    assert {node["id"] for node in both} == {music_root["id"], other_root["id"]}


async def test_folder_path_returns_breadcrumbs(user_id: int, user: dict) -> None:
    """``folder_path`` — крошки от корня до папки включительно, с возрастающей глубиной."""
    chain = await _make_chain(user_id, 4, prefix="Уровень ")
    deepest = chain[-1]

    crumbs = await folders_repo.folder_path(user_id, deepest["id"])

    assert [crumb["id"] for crumb in crumbs] == [folder["id"] for folder in chain]
    assert [crumb["name"] for crumb in crumbs] == [
        "Уровень 1",
        "Уровень 2",
        "Уровень 3",
        "Уровень 4",
    ]
    assert [crumb["depth"] for crumb in crumbs] == [0, 1, 2, 3]
    assert crumbs[0]["parent_folder_id"] is None
    assert crumbs[-1]["parent_folder_id"] == chain[-2]["id"]


async def test_folder_path_of_root_and_of_missing_folder(user_id: int, user: dict) -> None:
    """У корня одна крошка; у несуществующей (или чужой) папки — пустой список."""
    root = await folders_repo.create_folder(user_id, "Корень")

    crumbs = await folders_repo.folder_path(user_id, root["id"])
    assert [crumb["id"] for crumb in crumbs] == [root["id"]]
    assert crumbs[0]["depth"] == 0

    assert await folders_repo.folder_path(user_id, 999_999) == []


# ---------------------------------------------------------------------------
# Рекурсивный обход
# ---------------------------------------------------------------------------


async def test_descendant_ids_covers_whole_subtree(user_id: int, user: dict) -> None:
    """``descendant_ids`` возвращает всё поддерево, порядок — по глубине."""
    root = await folders_repo.create_folder(user_id, "Рок")
    left = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    right = await folders_repo.create_folder(user_id, "Метал", parent_folder_id=root["id"])
    deep = await folders_repo.create_folder(user_id, "Дум", parent_folder_id=right["id"])
    aside = await folders_repo.create_folder(user_id, "Джаз")

    subtree = await folders_repo.descendant_ids(user_id, root["id"])
    assert subtree[0] == root["id"], "первым идёт сама папка (глубина 0)"
    assert set(subtree) == {root["id"], left["id"], right["id"], deep["id"]}
    assert aside["id"] not in subtree

    without_self = await folders_repo.descendant_ids(
        user_id, root["id"], include_self=False
    )
    assert set(without_self) == {left["id"], right["id"], deep["id"]}

    assert await folders_repo.descendant_ids(user_id, deep["id"]) == [deep["id"]]
    assert await folders_repo.descendant_ids(user_id, 999_999) == []


async def test_list_tracks_folder_recursive(
    user_id: int, user: dict, track_factory: TrackFactory
) -> None:
    """Рекурсивный список треков собирает папку вместе со всеми подпапками."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])
    aside = await folders_repo.create_folder(user_id, "Джаз")

    in_root = await track_factory("В корне", folder_id=root["id"])
    in_child = await track_factory("В подпапке", folder_id=child["id"])
    in_grandchild = await track_factory("Глубоко", folder_id=grandchild["id"])
    await track_factory("В другой ветке", folder_id=aside["id"])
    await track_factory("Без папки")

    plain = await tracks_repo.list_tracks(user_id, folder_id=root["id"])
    assert [track["id"] for track in plain] == [in_root["id"]]

    recursive = await tracks_repo.list_tracks(
        user_id, folder_id=root["id"], folder_recursive=True
    )
    assert {track["id"] for track in recursive} == {
        in_root["id"],
        in_child["id"],
        in_grandchild["id"],
    }

    assert await tracks_repo.count_tracks(user_id, folder_id=root["id"]) == 1
    assert (
        await tracks_repo.count_tracks(
            user_id, folder_id=root["id"], folder_recursive=True
        )
        == 3
    )

    assert await folders_repo.folder_track_count(user_id, root["id"]) == 1
    assert await folders_repo.folder_track_count(user_id, root["id"], recursive=True) == 3


async def test_list_tracks_recursive_falls_back_to_recursive_cte(
    user_id: int,
    user: dict,
    track_factory: TrackFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запасной путь фильтра по папке (рекурсивный подзапрос) даёт тот же результат.

    Список подпапок подставляется в ``IN (...)``, пока он влезает в лимит
    параметров; иначе (и если ``folders.descendant_ids`` недоступна) в ход идёт
    ``WITH RECURSIVE`` прямо в запросе. Обе ветки обязаны совпадать.
    """
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])
    aside = await folders_repo.create_folder(user_id, "Джаз")

    expected = {
        (await track_factory("В корне", folder_id=root["id"]))["id"],
        (await track_factory("В подпапке", folder_id=child["id"]))["id"],
        (await track_factory("Глубоко", folder_id=grandchild["id"]))["id"],
    }
    await track_factory("В другой ветке", folder_id=aside["id"])

    # Ветка 1: список подпапок «не влезает» в IN-список.
    monkeypatch.setattr(tracks_repo, "_MAX_SQL_PARAMS", 0)
    by_cte = await tracks_repo.list_tracks(
        user_id, folder_id=root["id"], folder_recursive=True
    )
    assert {track["id"] for track in by_cte} == expected
    assert (
        await tracks_repo.count_tracks(
            user_id, folder_id=root["id"], folder_recursive=True
        )
        == 3
    )

    # Ветка 2: репозиторий папок не смог отдать поддерево — деградация без падения.
    async def _broken(*args: Any, **kwargs: Any) -> list[int]:
        raise RuntimeError("подпапки временно недоступны")

    monkeypatch.setattr(folders_repo, "descendant_ids", _broken)
    degraded = await tracks_repo.list_tracks(
        user_id, folder_id=root["id"], folder_recursive=True
    )
    assert {track["id"] for track in degraded} == expected


async def test_folder_counts_include_subfolders(
    user_id: int, user: dict, track_factory: TrackFactory
) -> None:
    """``track_count`` — свои треки, ``total_track_count`` — вместе с подпапками."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])

    await track_factory("В корне", folder_id=root["id"])
    await track_factory("В подпапке", folder_id=child["id"])
    await track_factory("Глубоко 1", folder_id=grandchild["id"])
    await track_factory("Глубоко 2", folder_id=grandchild["id"])

    fetched = await folders_repo.get_folder(user_id, root["id"])
    assert fetched is not None
    assert fetched["track_count"] == 1
    assert fetched["total_track_count"] == 4

    listed = {item["id"]: item for item in await folders_repo.list_folders(user_id)}
    assert listed[child["id"]]["track_count"] == 1
    assert listed[child["id"]]["total_track_count"] == 3
    assert listed[grandchild["id"]]["track_count"] == 2
    assert listed[grandchild["id"]]["total_track_count"] == 2
    assert listed[grandchild["id"]]["has_children"] is False

    only_children = await folders_repo.list_folders(user_id, parent_folder_id=root["id"])
    assert [item["id"] for item in only_children] == [child["id"]]

    only_roots = await folders_repo.list_folders(user_id, parent_folder_id=None)
    assert [item["id"] for item in only_roots] == [root["id"]]


# ---------------------------------------------------------------------------
# Перенос папок и запрет цикла
# ---------------------------------------------------------------------------


async def test_move_folder_changes_parent(user_id: int, user: dict) -> None:
    """Обычный перенос: папка меняет родителя и глубину, дерево перестраивается."""
    first = await folders_repo.create_folder(user_id, "Рок")
    second = await folders_repo.create_folder(user_id, "Джаз")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=first["id"])

    moved = await folders_repo.move_folder(user_id, child["id"], second["id"])

    assert moved["parent_folder_id"] == second["id"]
    assert moved["depth"] == 1
    assert await folders_repo.descendant_ids(user_id, first["id"]) == [first["id"]]
    assert set(await folders_repo.descendant_ids(user_id, second["id"])) == {
        second["id"],
        child["id"],
    }


async def test_move_folder_to_root(user_id: int, user: dict) -> None:
    """``new_parent_id=None`` поднимает папку в корень раздела вместе с поддеревом."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])

    moved = await folders_repo.move_folder(user_id, child["id"], None)

    assert moved["parent_folder_id"] is None
    assert moved["depth"] == 0
    crumbs = await folders_repo.folder_path(user_id, grandchild["id"])
    assert [crumb["id"] for crumb in crumbs] == [child["id"], grandchild["id"]]


async def test_move_folder_into_itself_is_rejected(user_id: int, user: dict) -> None:
    """Папку нельзя вложить саму в себя."""
    root = await folders_repo.create_folder(user_id, "Рок")

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, root["id"], root["id"])

    assert await _raw_parent(root["id"]) is None


async def test_move_folder_into_own_subfolder_is_rejected(user_id: int, user: dict) -> None:
    """Перенос в собственного потомка запрещён — иначе в дереве появился бы цикл."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, root["id"], child["id"])

    # И на любой глубине поддерева, а не только у прямого ребёнка.
    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, root["id"], grandchild["id"])

    # Дерево осталось прежним: цикла в данных нет, обходы завершаются.
    assert await _raw_parent(root["id"]) is None
    assert await _raw_parent(child["id"]) == root["id"]
    assert await _raw_parent(grandchild["id"]) == child["id"]
    assert set(await folders_repo.descendant_ids(user_id, root["id"])) == {
        root["id"],
        child["id"],
        grandchild["id"],
    }
    assert [crumb["id"] for crumb in await folders_repo.folder_path(user_id, grandchild["id"])] == [
        root["id"],
        child["id"],
        grandchild["id"],
    ]


async def test_move_folder_rejects_missing_or_foreign_target(user_id: int, user: dict) -> None:
    """Нет папки — ``NotFoundError``; нет родителя — ``ValidationError``."""
    root = await folders_repo.create_folder(user_id, "Рок")

    with pytest.raises(NotFoundError):
        await folders_repo.move_folder(user_id, 999_999, None)

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, root["id"], 999_999)


async def test_move_folder_rejects_cross_section(user_id: int, user: dict) -> None:
    """Разделы «Треки» и «Другое» не смешиваются переносом."""
    music_root = await folders_repo.create_folder(user_id, "Рок")
    other_root = await folders_repo.create_folder(user_id, "Документы", section="other")
    other_child = await folders_repo.create_folder(
        user_id, "Сканы", parent_folder_id=other_root["id"]
    )

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, other_child["id"], music_root["id"])

    assert await _raw_parent(other_child["id"]) == other_root["id"]


async def test_move_folder_rejects_name_conflict_on_target_level(
    user_id: int, user: dict
) -> None:
    """На новом уровне не может появиться второй «тёзка»."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Хиты", parent_folder_id=root["id"])
    await folders_repo.create_folder(user_id, "Хиты")

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, child["id"], None)

    assert await _raw_parent(child["id"]) == root["id"]


async def test_move_folder_to_same_parent_is_noop(user_id: int, user: dict) -> None:
    """Повторный перенос туда же не ломается на «тёзке» — это сама папка."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])

    same = await folders_repo.move_folder(user_id, child["id"], root["id"])

    assert same["id"] == child["id"]
    assert same["parent_folder_id"] == root["id"]


# ---------------------------------------------------------------------------
# Каскадное удаление
# ---------------------------------------------------------------------------


async def test_delete_folder_removes_whole_subtree(
    user_id: int, user: dict, track_factory: TrackFactory
) -> None:
    """Удаление папки уносит все подпапки; треки по умолчанию остаются без папки."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    grandchild = await folders_repo.create_folder(user_id, "Хардкор", parent_folder_id=child["id"])
    aside = await folders_repo.create_folder(user_id, "Джаз")

    deep_track = await track_factory("Глубоко", folder_id=grandchild["id"])
    aside_track = await track_factory("Рядом", folder_id=aside["id"])

    assert await folders_repo.delete_folder(user_id, root["id"]) is True

    remaining = {item["id"] for item in await folders_repo.list_folders(user_id)}
    assert remaining == {aside["id"]}
    assert await folders_repo.get_folder(user_id, child["id"]) is None
    assert await folders_repo.get_folder(user_id, grandchild["id"]) is None

    survived = await tracks_repo.get_track(user_id, deep_track["id"])
    assert survived is not None
    assert survived["folder_id"] is None

    untouched = await tracks_repo.get_track(user_id, aside_track["id"])
    assert untouched is not None
    assert untouched["folder_id"] == aside["id"]

    assert await db.fetch_all("PRAGMA foreign_key_check") == []


async def test_delete_folder_with_tracks_removes_them(
    user_id: int, user: dict, track_factory: TrackFactory
) -> None:
    """``delete_tracks=True`` удаляет треки всего поддерева."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])
    await track_factory("В корне", folder_id=root["id"])
    await track_factory("В подпапке", folder_id=child["id"])
    kept = await track_factory("Без папки")

    assert await folders_repo.delete_folder(user_id, root["id"], delete_tracks=True) is True

    assert await tracks_repo.count_tracks(user_id) == 1
    remaining = await tracks_repo.list_tracks(user_id)
    assert [track["id"] for track in remaining] == [kept["id"]]


async def test_delete_folder_non_recursive_rejects_children(user_id: int, user: dict) -> None:
    """``recursive=False`` не даёт каскаду тихо снести поддерево."""
    root = await folders_repo.create_folder(user_id, "Рок")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])

    with pytest.raises(ValidationError):
        await folders_repo.delete_folder(user_id, root["id"], recursive=False)

    assert await folders_repo.get_folder(user_id, root["id"]) is not None
    assert await folders_repo.get_folder(user_id, child["id"]) is not None

    # Лист без подпапок так удалить можно.
    assert await folders_repo.delete_folder(user_id, child["id"], recursive=False) is True
    assert await folders_repo.get_folder(user_id, child["id"]) is None


async def test_delete_missing_folder_returns_false(user_id: int, user: dict) -> None:
    """Удаление несуществующей папки — ``False``, без исключения."""
    assert await folders_repo.delete_folder(user_id, 999_999) is False


# ---------------------------------------------------------------------------
# Ограничение глубины
# ---------------------------------------------------------------------------


async def test_max_folder_depth_is_enforced(user_id: int, user: dict) -> None:
    """Ровно MAX_FOLDER_DEPTH уровней создаётся, следующий — ошибка."""
    chain = await _make_chain(user_id, MAX_DEPTH)

    assert len(chain) == MAX_DEPTH
    assert chain[-1]["depth"] == MAX_DEPTH - 1

    with pytest.raises(ValidationError):
        await folders_repo.create_folder(
            user_id, "Слишком глубоко", parent_folder_id=chain[-1]["id"]
        )

    assert len(await folders_repo.list_folders(user_id)) == MAX_DEPTH
    crumbs = await folders_repo.folder_path(user_id, chain[-1]["id"])
    assert len(crumbs) == MAX_DEPTH


async def test_move_folder_respects_depth_limit(user_id: int, user: dict) -> None:
    """Перенос учитывает высоту переносимого поддерева, а не только его корень."""
    chain = await _make_chain(user_id, MAX_DEPTH)
    deepest = chain[-1]
    one_before = chain[-2]

    # Поддерево высоты 1: сам корень плюс один ребёнок.
    movable = await folders_repo.create_folder(user_id, "Переносимая")
    await folders_repo.create_folder(user_id, "Внутри", parent_folder_id=movable["id"])

    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, movable["id"], deepest["id"])
    with pytest.raises(ValidationError):
        await folders_repo.move_folder(user_id, movable["id"], one_before["id"])

    assert await _raw_parent(movable["id"]) is None

    # Одиночная папка (высота 0) на предпоследний уровень влезает.
    leaf = await folders_repo.create_folder(user_id, "Одиночная")
    moved = await folders_repo.move_folder(user_id, leaf["id"], one_before["id"])
    assert moved["depth"] == MAX_DEPTH - 1


# ---------------------------------------------------------------------------
# Уникальность имён по уровням
# ---------------------------------------------------------------------------


async def test_same_name_allowed_in_different_parents(user_id: int, user: dict) -> None:
    """Одноимённые папки у разных родителей — это разные папки."""
    rock = await folders_repo.create_folder(user_id, "Рок")
    jazz = await folders_repo.create_folder(user_id, "Джаз")

    first = await folders_repo.create_folder(user_id, "Хиты", parent_folder_id=rock["id"])
    second = await folders_repo.create_folder(user_id, "Хиты", parent_folder_id=jazz["id"])

    assert first["id"] != second["id"]
    assert first["parent_folder_id"] == rock["id"]
    assert second["parent_folder_id"] == jazz["id"]
    assert first["normalized_name"] == second["normalized_name"]

    all_folders = await folders_repo.list_folders(user_id)
    assert sum(1 for item in all_folders if item["name"] == "Хиты") == 2


async def test_duplicate_root_folder_is_not_created(user_id: int, user: dict) -> None:
    """Две одноимённые КОРНЕВЫЕ папки не создаются — уникальность держит репозиторий.

    UNIQUE-индекс здесь бессилен: в SQLite ``NULL != NULL``, поэтому проверку
    делает :func:`backend.db.repositories.folders.create_folder` (раздел 1.1 контракта).
    """
    first = await folders_repo.create_folder(user_id, "Рок")
    again = await folders_repo.create_folder(user_id, "Рок")
    # Регистр и лишние пробелы нормализуются — это та же самая папка.
    normalized = await folders_repo.create_folder(user_id, "  рОк  ")

    assert again["id"] == first["id"]
    assert normalized["id"] == first["id"]

    roots = await folders_repo.list_folders(user_id, parent_folder_id=None)
    assert [item["id"] for item in roots] == [first["id"]]

    stored = int(
        await db.fetch_val(
            "SELECT COUNT(*) FROM folders WHERE user_id = ? AND parent_folder_id IS NULL"
            " AND normalized_name = ?",
            (user_id, first["normalized_name"]),
            default=0,
        )
    )
    assert stored == 1


async def test_duplicate_child_folder_is_not_created(user_id: int, user: dict) -> None:
    """Внутри одного родителя повторное создание тоже возвращает существующую папку."""
    root = await folders_repo.create_folder(user_id, "Рок")

    first = await folders_repo.create_folder(user_id, "Хиты", parent_folder_id=root["id"])
    again = await folders_repo.create_folder(user_id, "ХИТЫ", parent_folder_id=root["id"])

    assert again["id"] == first["id"]
    children = await folders_repo.list_folders(user_id, parent_folder_id=root["id"])
    assert [item["id"] for item in children] == [first["id"]]


async def test_same_root_name_allowed_in_different_sections(user_id: int, user: dict) -> None:
    """Корневые «тёзки» в разных разделах — разные папки."""
    music = await folders_repo.create_folder(user_id, "Архив")
    other = await folders_repo.create_folder(user_id, "Архив", section="other")

    assert music["id"] != other["id"]
    assert music["section"] == "music"
    assert other["section"] == "other"


async def test_rename_folder_rejects_sibling_conflict(user_id: int, user: dict) -> None:
    """Переименование не может создать «тёзку» на том же уровне, но может — на другом."""
    root = await folders_repo.create_folder(user_id, "Рок")
    other_root = await folders_repo.create_folder(user_id, "Джаз")
    child = await folders_repo.create_folder(user_id, "Панк", parent_folder_id=root["id"])

    with pytest.raises(ValidationError):
        await folders_repo.rename_folder(user_id, other_root["id"], "рок")

    unchanged = await folders_repo.get_folder(user_id, other_root["id"])
    assert unchanged is not None
    assert unchanged["name"] == "Джаз"

    # На другом уровне имя свободно.
    renamed = await folders_repo.rename_folder(user_id, child["id"], "Джаз")
    assert renamed is not None
    assert renamed["name"] == "Джаз"
    assert renamed["parent_folder_id"] == root["id"]
