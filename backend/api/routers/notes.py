"""API заметок со списками пунктов (раздел 4 контракта V2).

Заметка — заголовок и упорядоченный список пунктов с отметкой «выполнено»
(чекбоксы в Mini App). Все изменяющие маршруты возвращают актуальную заметку
целиком (``NoteDetailOut``), чтобы Mini App перерисовывал список без
дополнительного запроса — так же, как это сделано для плейлистов.

Схемы описаны прямо здесь: заметки не пересекаются с моделями треков и папок
из :mod:`backend.api.schemas`, а порядок пунктов и агрегаты (`items_total`,
`items_done`) нужны только этому роутеру.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser
from backend.db.repositories import notes as notes_repo
from backend.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notes", tags=["notes"])

# Сообщения об ошибках (RU, видны пользователю Mini App и бота).
NOTE_NOT_FOUND: Final[str] = notes_repo.NOTE_NOT_FOUND
ITEM_NOT_FOUND: Final[str] = notes_repo.ITEM_NOT_FOUND
ORDER_MISMATCH: Final[str] = (
    "Список пунктов не совпадает с составом заметки. Обновите заметку и повторите."
)
EMPTY_ITEM_PATCH: Final[str] = (
    "Не переданы изменения пункта: укажите новый текст и/или отметку «выполнено»"
)
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"


# ===========================================================================
# Схемы
# ===========================================================================


class NoteItemOut(BaseModel):
    """Пункт заметки (чекбокс) в том виде, в котором его получает Mini App."""

    id: int
    note_id: int
    text: str
    is_done: bool = False
    position: int = 0
    created_at: str | None = None


class NoteOut(BaseModel):
    """Заметка без пунктов: для списка заметок достаточно агрегатов."""

    id: int
    title: str
    items_total: int = 0
    items_done: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class NoteDetailOut(NoteOut):
    """Заметка вместе с пунктами в порядке позиций."""

    items: list[NoteItemOut] = Field(default_factory=list)


class NoteCreateIn(BaseModel):
    """Создание заметки."""

    title: str = Field(..., description="Название заметки")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_text(value, "Название заметки", notes_repo.MAX_TITLE_LENGTH)


class NoteUpdateIn(BaseModel):
    """Переименование заметки."""

    title: str = Field(..., description="Новое название заметки")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_text(value, "Название заметки", notes_repo.MAX_TITLE_LENGTH)


class NoteItemCreateIn(BaseModel):
    """Новый пункт заметки (добавляется в конец списка)."""

    text: str = Field(..., description="Текст пункта")

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _clean_text(value, "Текст пункта", notes_repo.MAX_ITEM_TEXT_LENGTH)


class NoteItemUpdateIn(BaseModel):
    """Изменение пункта: текст и/или отметка «выполнено» (чекбокс)."""

    text: str | None = Field(default=None, description="Новый текст пункта")
    is_done: bool | None = Field(
        default=None, description="Отметка «выполнено»: true/false"
    )

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, "Текст пункта", notes_repo.MAX_ITEM_TEXT_LENGTH)

    def updates(self) -> dict[str, Any]:
        """Только явно переданные поля (`exclude_unset`), без значений `None`."""
        provided = self.model_dump(exclude_unset=True)
        return {key: value for key, value in provided.items() if value is not None}


class NoteOrderIn(BaseModel):
    """Новый порядок пунктов заметки (drag-and-drop в Mini App)."""

    item_ids: list[int] = Field(
        default_factory=list, description="Идентификаторы пунктов в нужном порядке"
    )

    @field_validator("item_ids")
    @classmethod
    def _validate_item_ids(cls, value: list[int]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for raw in value or []:
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Идентификаторы пунктов должны быть числами") from exc
            if number in seen:
                raise ValueError("В новом порядке пунктов есть повторы")
            seen.add(number)
            result.append(number)
        return result


# ===========================================================================
# Вспомогательные функции
# ===========================================================================


def _clean_text(value: str | None, label: str, max_length: int) -> str:
    """Проверяет обязательное текстовое поле (совпадает с проверками репозитория)."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} не может быть пустым")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} слишком длинный (максимум {max_length} символов)")
    return cleaned


def _user_id(user: dict) -> int:
    """Идентификатор пользователя из зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %r", user)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=USER_UNKNOWN
        )
    return int(raw)


def _as_int(value: Any, default: int = 0) -> int:
    """Безопасное приведение к int (None и мусор -> default)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str | None:
    """Дата/строка из БД -> str (None остаётся None)."""
    if value is None:
        return None
    return str(value)


def _item_out(item: dict) -> NoteItemOut:
    """Собирает NoteItemOut из dict репозитория."""
    return NoteItemOut(
        id=_as_int(item.get("id")),
        note_id=_as_int(item.get("note_id")),
        text=str(item.get("text") or ""),
        is_done=bool(item.get("is_done")),
        position=_as_int(item.get("position")),
        created_at=_as_text(item.get("created_at")),
    )


def _note_payload(note: dict) -> dict[str, Any]:
    """Общие поля заметки для NoteOut / NoteDetailOut."""
    return {
        "id": _as_int(note.get("id")),
        "title": str(note.get("title") or ""),
        "items_total": _as_int(note.get("items_total")),
        "items_done": _as_int(note.get("items_done")),
        "created_at": _as_text(note.get("created_at")),
        "updated_at": _as_text(note.get("updated_at")),
    }


def _note_out(note: dict) -> NoteOut:
    """Заметка без пунктов (для списка)."""
    return NoteOut(**_note_payload(note))


def _detail_out(note: dict) -> NoteDetailOut:
    """Заметка вместе с пунктами."""
    items = [_item_out(item) for item in note.get("items") or []]
    return NoteDetailOut(**_note_payload(note), items=items)


async def _detail(user_id: int, note_id: int) -> NoteDetailOut:
    """Актуальное состояние заметки или 404, если её нет."""
    note = await notes_repo.get_note(user_id, note_id)
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOTE_NOT_FOUND
        )
    return _detail_out(note)


async def _require_note(user_id: int, note_id: int) -> dict:
    """Возвращает заметку пользователя или 404."""
    note = await notes_repo.get_note(user_id, note_id)
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOTE_NOT_FOUND
        )
    return note


def _bad_request(exc: ValidationError) -> HTTPException:
    """`ValidationError` репозитория -> 400 с русским текстом."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ===========================================================================
# Заметки
# ===========================================================================


@router.get("", response_model=list[NoteOut], summary="Список заметок")
@router.get("/", response_model=list[NoteOut], include_in_schema=False)
async def list_notes(user: CurrentUser) -> list[NoteOut]:
    """Заметки пользователя: сначала недавно изменённые, с числом пунктов."""
    user_id = _user_id(user)
    notes = await notes_repo.list_notes(user_id)
    return [_note_out(note) for note in notes]


@router.post(
    "",
    response_model=NoteDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать заметку",
)
@router.post(
    "/",
    response_model=NoteDetailOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_note(payload: NoteCreateIn, user: CurrentUser) -> NoteDetailOut:
    """Создаёт пустую заметку с указанным названием."""
    user_id = _user_id(user)
    try:
        note = await notes_repo.create_note(user_id, payload.title)
    except ValidationError as exc:
        raise _bad_request(exc) from exc

    logger.info("Пользователь %s создал заметку %s", user_id, note.get("id"))
    return _detail_out(note)


@router.get("/{note_id}", response_model=NoteDetailOut, summary="Заметка с пунктами")
async def get_note(note_id: int, user: CurrentUser) -> NoteDetailOut:
    """Заметка вместе с пунктами в порядке позиций."""
    return await _detail(_user_id(user), note_id)


@router.patch(
    "/{note_id}", response_model=NoteDetailOut, summary="Переименовать заметку"
)
async def rename_note(
    note_id: int, payload: NoteUpdateIn, user: CurrentUser
) -> NoteDetailOut:
    """Меняет название заметки; пункты не затрагиваются."""
    user_id = _user_id(user)
    try:
        note = await notes_repo.rename_note(user_id, note_id, payload.title)
    except ValidationError as exc:
        raise _bad_request(exc) from exc
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOTE_NOT_FOUND
        )
    return _detail_out(note)


@router.delete("/{note_id}", summary="Удалить заметку")
async def delete_note(note_id: int, user: CurrentUser) -> dict[str, bool]:
    """Удаляет заметку вместе со всеми её пунктами."""
    user_id = _user_id(user)
    deleted = await notes_repo.delete_note(user_id, note_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOTE_NOT_FOUND
        )
    logger.info("Пользователь %s удалил заметку %s", user_id, note_id)
    return {"ok": True}


# ===========================================================================
# Пункты заметки
# ===========================================================================


@router.post(
    "/{note_id}/items",
    response_model=NoteDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить пункт",
)
async def add_item(
    note_id: int, payload: NoteItemCreateIn, user: CurrentUser
) -> NoteDetailOut:
    """Добавляет пункт в конец списка и возвращает заметку целиком."""
    user_id = _user_id(user)
    try:
        item = await notes_repo.add_item(user_id, note_id, payload.text)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValidationError as exc:
        raise _bad_request(exc) from exc

    logger.info(
        "Пользователь %s добавил пункт %s в заметку %s",
        user_id,
        item.get("id"),
        note_id,
    )
    return await _detail(user_id, note_id)


@router.patch(
    "/{note_id}/items/{item_id}",
    response_model=NoteDetailOut,
    summary="Изменить пункт (текст и/или отметку)",
)
async def update_item(
    note_id: int, item_id: int, payload: NoteItemUpdateIn, user: CurrentUser
) -> NoteDetailOut:
    """Меняет текст пункта и/или снимает-ставит отметку «выполнено».

    Оба поля необязательные, но хотя бы одно должно быть передано: пустое тело
    запроса — ошибка клиента, а не «молчаливое ничего».
    """
    user_id = _user_id(user)
    updates = payload.updates()
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=EMPTY_ITEM_PATCH
        )
    await _require_note(user_id, note_id)

    try:
        if "text" in updates:
            item = await notes_repo.update_item(
                user_id, note_id, item_id, str(updates["text"])
            )
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=ITEM_NOT_FOUND
                )
        if "is_done" in updates:
            item = await notes_repo.set_item_done(
                user_id, note_id, item_id, bool(updates["is_done"])
            )
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=ITEM_NOT_FOUND
                )
    except ValidationError as exc:
        raise _bad_request(exc) from exc

    logger.debug(
        "Пользователь %s изменил пункт %s заметки %s: %s",
        user_id,
        item_id,
        note_id,
        ", ".join(sorted(updates)),
    )
    return await _detail(user_id, note_id)


@router.delete(
    "/{note_id}/items/{item_id}",
    response_model=NoteDetailOut,
    summary="Удалить пункт",
)
async def delete_item(note_id: int, item_id: int, user: CurrentUser) -> NoteDetailOut:
    """Удаляет пункт и перенумеровывает оставшиеся (позиции 1..N)."""
    user_id = _user_id(user)
    await _require_note(user_id, note_id)

    deleted = await notes_repo.delete_item(user_id, note_id, item_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ITEM_NOT_FOUND
        )
    logger.info(
        "Пользователь %s удалил пункт %s заметки %s", user_id, item_id, note_id
    )
    return await _detail(user_id, note_id)


@router.put(
    "/{note_id}/order",
    response_model=NoteDetailOut,
    summary="Изменить порядок пунктов",
)
async def reorder_items(
    note_id: int, payload: NoteOrderIn, user: CurrentUser
) -> NoteDetailOut:
    """Задаёт новый порядок пунктов (drag-and-drop).

    Переданный список должен содержать ровно те же пункты, что и заметка,
    иначе возвращается 400 и порядок не меняется.
    """
    user_id = _user_id(user)
    await _require_note(user_id, note_id)

    ordered = await notes_repo.reorder_items(user_id, note_id, payload.item_ids)
    if not ordered:
        logger.warning(
            "Отклонён порядок пунктов заметки %s пользователя %s (передано %s шт.)",
            note_id,
            user_id,
            len(payload.item_ids),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_MISMATCH
        )
    return await _detail(user_id, note_id)


__all__ = [
    "NoteCreateIn",
    "NoteDetailOut",
    "NoteItemCreateIn",
    "NoteItemOut",
    "NoteItemUpdateIn",
    "NoteOrderIn",
    "NoteOut",
    "NoteUpdateIn",
    "router",
]
