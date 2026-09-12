"""Регрессионные тесты заметок со списками пунктов (раздел 1.4, 2 и 8 контракта V2).

Покрывается:

* CRUD заметок (create / list / get / rename / delete) и проверки ввода;
* CRUD пунктов (add / update / delete) и агрегаты ``items_total`` / ``items_done``;
* :func:`toggle_item` и :func:`set_item_done`;
* :func:`reorder_items` — корректный порядок и отказ при неверном составе;
* перенумерация позиций 1..N после удаления пункта;
* изоляция по ``user_id``: чужую заметку нельзя ни прочитать, ни изменить.

Всё выполняется на временной базе (фикстура ``database`` из
:mod:`tests.conftest`), сеть и Telegram API не задействованы.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from backend.db.database import db
from backend.db.repositories import notes as notes_repo
from backend.db.repositories import users as users_repo
from backend.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

#: Telegram-ID «чужого» пользователя для проверок изоляции.
OTHER_USER_ID: int = 606_060

#: Тип фабрики заметок со списком пунктов.
NoteFactory = Callable[..., Awaitable[dict]]


# ---------------------------------------------------------------------------
# Фикстуры и помощники
# ---------------------------------------------------------------------------


@pytest.fixture
async def other_user(database: str) -> dict:
    """Второй пользователь: нужен для проверок изоляции по ``user_id``."""
    return await users_repo.ensure_user(OTHER_USER_ID, username="stranger")


@pytest.fixture
def note_factory(user: dict, user_id: int) -> NoteFactory:
    """Фабрика заметок: создаёт заметку и сразу наполняет её пунктами."""

    async def _create(
        title: str = "Список",
        *,
        items: Sequence[str] = (),
        owner_id: int | None = None,
    ) -> dict:
        owner = owner_id if owner_id is not None else user_id
        note = await notes_repo.create_note(owner, title)
        for text in items:
            await notes_repo.add_item(owner, int(note["id"]), text)
        refreshed = await notes_repo.get_note(owner, int(note["id"]))
        assert refreshed is not None, "Только что созданная заметка не читается"
        return refreshed

    return _create


def _texts(note: dict[str, Any]) -> list[str]:
    """Тексты пунктов заметки в порядке выдачи."""
    return [str(item["text"]) for item in note["items"]]


def _positions(note: dict[str, Any]) -> list[int]:
    """Позиции пунктов заметки в порядке выдачи."""
    return [int(item["position"]) for item in note["items"]]


def _item_ids(note: dict[str, Any]) -> list[int]:
    """Идентификаторы пунктов заметки в порядке выдачи."""
    return [int(item["id"]) for item in note["items"]]


async def _raw_positions(note_id: int) -> list[tuple[int, int]]:
    """Пары (id пункта, позиция) прямо из базы — в порядке позиций."""
    rows = await db.fetch_all(
        "SELECT id, position FROM note_items WHERE note_id = ? ORDER BY position, id",
        (int(note_id),),
    )
    return [(int(row["id"]), int(row["position"])) for row in rows]


# ---------------------------------------------------------------------------
# CRUD заметок
# ---------------------------------------------------------------------------


async def test_create_note_returns_empty_note(user: dict, user_id: int) -> None:
    """Новая заметка создаётся пустой, с нулевыми агрегатами."""
    note = await notes_repo.create_note(user_id, "  Купить к выходным  ")

    assert note["title"] == "Купить к выходным"  # пробелы по краям срезаются
    assert note["user_id"] == user_id
    assert note["items"] == []
    assert note["items_total"] == 0
    assert note["items_done"] == 0

    stored = await notes_repo.get_note(user_id, int(note["id"]))
    assert stored is not None
    assert stored["title"] == "Купить к выходным"
    assert stored["items"] == []


async def test_create_note_validates_title(user: dict, user_id: int) -> None:
    """Пустой и слишком длинный заголовок отвергаются."""
    with pytest.raises(ValidationError):
        await notes_repo.create_note(user_id, "   ")
    with pytest.raises(ValidationError):
        await notes_repo.create_note(user_id, None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await notes_repo.create_note(
            user_id, "я" * (notes_repo.MAX_TITLE_LENGTH + 1)
        )

    assert await notes_repo.list_notes(user_id) == []


async def test_list_notes_counts_items_and_sorts_by_update(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Список заметок отдаёт число пунктов и число выполненных."""
    first = await note_factory("Первая", items=["a", "b", "c"])
    second = await note_factory("Вторая", items=["x"])

    await notes_repo.set_item_done(
        user_id, int(first["id"]), _item_ids(first)[0], True
    )

    listed = {item["title"]: item for item in await notes_repo.list_notes(user_id)}
    assert set(listed) == {"Первая", "Вторая"}
    assert listed["Первая"]["items_total"] == 3
    assert listed["Первая"]["items_done"] == 1
    assert listed["Вторая"]["items_total"] == 1
    assert listed["Вторая"]["items_done"] == 0
    # Список заметок отдаётся без пунктов (пункты — только в get_note).
    assert "items" not in listed["Первая"]

    assert int(second["id"]) in {
        int(item["id"]) for item in await notes_repo.list_notes(user_id)
    }


async def test_get_missing_note_returns_none(user: dict, user_id: int) -> None:
    """Несуществующая заметка — None, а не исключение."""
    assert await notes_repo.get_note(user_id, 10_000_001) is None


async def test_rename_note(note_factory: NoteFactory, user_id: int) -> None:
    """Переименование меняет заголовок и сохраняет пункты."""
    note = await note_factory("Старое имя", items=["хлеб", "молоко"])

    renamed = await notes_repo.rename_note(user_id, int(note["id"]), " Новое имя ")

    assert renamed is not None
    assert renamed["title"] == "Новое имя"
    assert _texts(renamed) == ["хлеб", "молоко"]

    with pytest.raises(ValidationError):
        await notes_repo.rename_note(user_id, int(note["id"]), "")

    assert await notes_repo.rename_note(user_id, 10_000_001, "Нет такой") is None


async def test_delete_note_removes_items(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Удаление заметки уносит её пункты (ON DELETE CASCADE)."""
    note = await note_factory("На удаление", items=["один", "два"])
    note_id = int(note["id"])

    assert await notes_repo.delete_note(user_id, note_id) is True

    assert await notes_repo.get_note(user_id, note_id) is None
    assert await notes_repo.list_notes(user_id) == []
    assert await _raw_positions(note_id) == []
    # Повторное удаление — False, без исключения.
    assert await notes_repo.delete_note(user_id, note_id) is False


# ---------------------------------------------------------------------------
# CRUD пунктов
# ---------------------------------------------------------------------------


async def test_add_item_appends_to_the_end(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Пункты добавляются в конец, позиции идут подряд с единицы."""
    note = await note_factory("Покупки")
    note_id = int(note["id"])

    first = await notes_repo.add_item(user_id, note_id, "  хлеб  ")
    second = await notes_repo.add_item(user_id, note_id, "молоко")
    third = await notes_repo.add_item(user_id, note_id, "сыр")

    assert first["text"] == "хлеб"  # пробелы срезаются
    assert [first["position"], second["position"], third["position"]] == [1, 2, 3]
    assert first["is_done"] is False

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert _texts(stored) == ["хлеб", "молоко", "сыр"]
    assert _positions(stored) == [1, 2, 3]
    assert stored["items_total"] == 3
    assert stored["items_done"] == 0


async def test_add_item_validates_text_and_note(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Пустой/длинный текст — ValidationError, отсутствующая заметка — NotFoundError."""
    note = await note_factory("Покупки")
    note_id = int(note["id"])

    with pytest.raises(ValidationError):
        await notes_repo.add_item(user_id, note_id, "   ")
    with pytest.raises(ValidationError):
        await notes_repo.add_item(
            user_id, note_id, "я" * (notes_repo.MAX_ITEM_TEXT_LENGTH + 1)
        )
    with pytest.raises(NotFoundError):
        await notes_repo.add_item(user_id, 10_000_001, "хлеб")

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert stored["items"] == []


async def test_update_item_changes_text_only(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Изменение текста пункта не трогает позицию и отметку «выполнено»."""
    note = await note_factory("Покупки", items=["хлеб", "молоко"])
    note_id = int(note["id"])
    item_id = _item_ids(note)[1]

    await notes_repo.set_item_done(user_id, note_id, item_id, True)
    updated = await notes_repo.update_item(user_id, note_id, item_id, "кефир")

    assert updated is not None
    assert updated["text"] == "кефир"
    assert updated["position"] == 2
    assert updated["is_done"] is True

    with pytest.raises(ValidationError):
        await notes_repo.update_item(user_id, note_id, item_id, " ")

    assert await notes_repo.update_item(user_id, note_id, 10_000_001, "нет") is None
    assert await notes_repo.update_item(user_id, 10_000_001, item_id, "нет") is None


async def test_item_of_another_note_is_not_touched(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Пункт чужой заметки того же пользователя не редактируется «через» её id."""
    first = await note_factory("Первая", items=["хлеб"])
    second = await note_factory("Вторая", items=["молоко"])
    alien_item = _item_ids(second)[0]

    assert (
        await notes_repo.update_item(
            user_id, int(first["id"]), alien_item, "подмена"
        )
        is None
    )
    assert (
        await notes_repo.set_item_done(user_id, int(first["id"]), alien_item, True)
        is None
    )
    assert await notes_repo.toggle_item(user_id, int(first["id"]), alien_item) is None
    assert (
        await notes_repo.delete_item(user_id, int(first["id"]), alien_item) is False
    )

    untouched = await notes_repo.get_note(user_id, int(second["id"]))
    assert untouched is not None
    assert _texts(untouched) == ["молоко"]
    assert untouched["items"][0]["is_done"] is False


# ---------------------------------------------------------------------------
# Отметка «выполнено»
# ---------------------------------------------------------------------------


async def test_set_item_done_and_toggle(
    note_factory: NoteFactory, user_id: int
) -> None:
    """set_item_done ставит явное значение, toggle_item — переключает."""
    note = await note_factory("Дела", items=["позвонить", "написать"])
    note_id = int(note["id"])
    item_id = _item_ids(note)[0]

    done = await notes_repo.set_item_done(user_id, note_id, item_id, True)
    assert done is not None and done["is_done"] is True

    # Повторная установка того же значения идемпотентна.
    again = await notes_repo.set_item_done(user_id, note_id, item_id, True)
    assert again is not None and again["is_done"] is True

    undone = await notes_repo.set_item_done(user_id, note_id, item_id, False)
    assert undone is not None and undone["is_done"] is False

    toggled = await notes_repo.toggle_item(user_id, note_id, item_id)
    assert toggled is not None and toggled["is_done"] is True
    toggled_back = await notes_repo.toggle_item(user_id, note_id, item_id)
    assert toggled_back is not None and toggled_back["is_done"] is False

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert stored["items_done"] == 0
    assert stored["items_total"] == 2


async def test_done_flag_counted_in_aggregates(
    note_factory: NoteFactory, user_id: int
) -> None:
    """items_done растёт вместе с числом отмеченных пунктов."""
    note = await note_factory("Дела", items=["раз", "два", "три"])
    note_id = int(note["id"])

    for item_id in _item_ids(note)[:2]:
        await notes_repo.toggle_item(user_id, note_id, item_id)

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert stored["items_total"] == 3
    assert stored["items_done"] == 2
    assert [item["is_done"] for item in stored["items"]] == [True, True, False]


async def test_toggle_missing_item_returns_none(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Несуществующий пункт или заметка — None, без изменений в базе."""
    note = await note_factory("Дела", items=["раз"])
    note_id = int(note["id"])

    assert await notes_repo.toggle_item(user_id, note_id, 10_000_001) is None
    assert await notes_repo.set_item_done(user_id, note_id, 10_000_001, True) is None
    assert (
        await notes_repo.toggle_item(user_id, 10_000_001, _item_ids(note)[0]) is None
    )

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert stored["items_done"] == 0


# ---------------------------------------------------------------------------
# Порядок пунктов
# ---------------------------------------------------------------------------


async def test_reorder_items_sets_new_order(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Корректный порядок применяется, позиции перенумеровываются 1..N."""
    note = await note_factory("Дела", items=["раз", "два", "три", "четыре"])
    note_id = int(note["id"])
    first, second, third, fourth = _item_ids(note)

    assert (
        await notes_repo.reorder_items(
            user_id, note_id, [fourth, second, first, third]
        )
        is True
    )

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert _texts(stored) == ["четыре", "два", "раз", "три"]
    assert _positions(stored) == [1, 2, 3, 4]
    assert await _raw_positions(note_id) == [
        (fourth, 1),
        (second, 2),
        (first, 3),
        (third, 4),
    ]


async def test_reorder_items_rejects_wrong_composition(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Неверный состав — False и полностью неизменённый порядок."""
    note = await note_factory("Дела", items=["раз", "два", "три"])
    note_id = int(note["id"])
    first, second, third = _item_ids(note)
    before = await _raw_positions(note_id)

    # Не хватает пункта.
    assert await notes_repo.reorder_items(user_id, note_id, [third, first]) is False
    # Лишний, чужой идентификатор.
    assert (
        await notes_repo.reorder_items(
            user_id, note_id, [third, second, first, 10_000_001]
        )
        is False
    )
    # Дубликат в переданном порядке.
    assert (
        await notes_repo.reorder_items(user_id, note_id, [first, first, second])
        is False
    )
    # Мусор вместо идентификатора.
    assert (
        await notes_repo.reorder_items(
            user_id, note_id, ["не-число", second, third]  # type: ignore[list-item]
        )
        is False
    )
    # Пустой список при непустой заметке.
    assert await notes_repo.reorder_items(user_id, note_id, []) is False
    # Несуществующая заметка.
    assert (
        await notes_repo.reorder_items(user_id, 10_000_001, [first, second, third])
        is False
    )

    assert await _raw_positions(note_id) == before
    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert _texts(stored) == ["раз", "два", "три"]


async def test_reorder_empty_note_is_noop(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Пустой порядок у пустой заметки — успех без изменений."""
    note = await note_factory("Пустая")

    assert await notes_repo.reorder_items(user_id, int(note["id"]), []) is True
    assert await _raw_positions(int(note["id"])) == []


async def test_delete_item_renumbers_positions(
    note_factory: NoteFactory, user_id: int
) -> None:
    """После удаления пункта позиции снова идут подряд от 1 без пропусков."""
    note = await note_factory("Дела", items=["раз", "два", "три", "четыре"])
    note_id = int(note["id"])
    first, second, third, fourth = _item_ids(note)

    assert await notes_repo.delete_item(user_id, note_id, second) is True

    stored = await notes_repo.get_note(user_id, note_id)
    assert stored is not None
    assert _texts(stored) == ["раз", "три", "четыре"]
    assert _positions(stored) == [1, 2, 3]
    assert await _raw_positions(note_id) == [(first, 1), (third, 2), (fourth, 3)]

    # Удаление первого — тоже без дырок.
    assert await notes_repo.delete_item(user_id, note_id, first) is True
    assert await _raw_positions(note_id) == [(third, 1), (fourth, 2)]

    # Новый пункт встаёт в конец, позиция продолжает ряд.
    added = await notes_repo.add_item(user_id, note_id, "пять")
    assert added["position"] == 3

    # Повторное удаление и удаление несуществующего пункта — False.
    assert await notes_repo.delete_item(user_id, note_id, first) is False
    assert await notes_repo.delete_item(user_id, note_id, 10_000_001) is False


async def test_reorder_after_delete_keeps_positions_dense(
    note_factory: NoteFactory, user_id: int
) -> None:
    """Удаление и перестановка вместе не ломают плотную нумерацию."""
    note = await note_factory("Дела", items=["раз", "два", "три", "четыре", "пять"])
    note_id = int(note["id"])
    ids = _item_ids(note)

    await notes_repo.delete_item(user_id, note_id, ids[2])
    remaining = [ids[0], ids[1], ids[3], ids[4]]

    assert (
        await notes_repo.reorder_items(user_id, note_id, list(reversed(remaining)))
        is True
    )

    positions = await _raw_positions(note_id)
    assert [item_id for item_id, _ in positions] == list(reversed(remaining))
    assert [position for _, position in positions] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Изоляция по user_id
# ---------------------------------------------------------------------------


async def test_foreign_note_is_not_readable(
    note_factory: NoteFactory, other_user: dict, user_id: int
) -> None:
    """Чужая заметка не видна ни в списке, ни по прямому id."""
    mine = await note_factory("Моя", items=["моё дело"])
    theirs = await note_factory(
        "Чужая", items=["чужое дело"], owner_id=OTHER_USER_ID
    )

    assert [item["id"] for item in await notes_repo.list_notes(user_id)] == [
        int(mine["id"])
    ]
    assert [item["id"] for item in await notes_repo.list_notes(OTHER_USER_ID)] == [
        int(theirs["id"])
    ]

    assert await notes_repo.get_note(user_id, int(theirs["id"])) is None
    assert await notes_repo.get_note(OTHER_USER_ID, int(mine["id"])) is None


async def test_foreign_note_is_not_modifiable(
    note_factory: NoteFactory, other_user: dict, user_id: int
) -> None:
    """Чужую заметку нельзя переименовать, удалить или дополнить пунктом."""
    theirs = await note_factory(
        "Чужая", items=["чужое дело"], owner_id=OTHER_USER_ID
    )
    note_id = int(theirs["id"])

    assert await notes_repo.rename_note(user_id, note_id, "Захвачено") is None
    assert await notes_repo.delete_note(user_id, note_id) is False
    with pytest.raises(NotFoundError):
        await notes_repo.add_item(user_id, note_id, "подкинутый пункт")

    intact = await notes_repo.get_note(OTHER_USER_ID, note_id)
    assert intact is not None
    assert intact["title"] == "Чужая"
    assert _texts(intact) == ["чужое дело"]


async def test_foreign_note_items_are_not_modifiable(
    note_factory: NoteFactory, other_user: dict, user_id: int
) -> None:
    """Пункты чужой заметки нельзя ни отметить, ни изменить, ни переставить."""
    theirs = await note_factory(
        "Чужая", items=["первое", "второе"], owner_id=OTHER_USER_ID
    )
    note_id = int(theirs["id"])
    first, second = _item_ids(theirs)
    before = await _raw_positions(note_id)

    assert await notes_repo.set_item_done(user_id, note_id, first, True) is None
    assert await notes_repo.toggle_item(user_id, note_id, first) is None
    assert await notes_repo.update_item(user_id, note_id, first, "подмена") is None
    assert await notes_repo.delete_item(user_id, note_id, first) is False
    assert await notes_repo.reorder_items(user_id, note_id, [second, first]) is False

    intact = await notes_repo.get_note(OTHER_USER_ID, note_id)
    assert intact is not None
    assert _texts(intact) == ["первое", "второе"]
    assert [item["is_done"] for item in intact["items"]] == [False, False]
    assert await _raw_positions(note_id) == before


async def test_deleting_user_removes_his_notes(
    note_factory: NoteFactory, other_user: dict, user_id: int
) -> None:
    """Заметки пользователя уходят вместе с ним (ON DELETE CASCADE)."""
    mine = await note_factory("Моя", items=["моё дело"])
    theirs = await note_factory(
        "Чужая", items=["чужое дело"], owner_id=OTHER_USER_ID
    )

    await db.execute("DELETE FROM users WHERE user_id = ?", (OTHER_USER_ID,))

    assert await notes_repo.get_note(OTHER_USER_ID, int(theirs["id"])) is None
    assert await _raw_positions(int(theirs["id"])) == []

    still_here = await notes_repo.get_note(user_id, int(mine["id"]))
    assert still_here is not None
    assert _texts(still_here) == ["моё дело"]
