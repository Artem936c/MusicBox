"""API папок: дерево, хлебные крошки, создание, перенос, треки и очередь плеера.

V1 (список, создание, переименование, удаление, треки папки и перенос треков)
сохранён без изменений в поведении. V2 (раздел 4 контракта ARCHITECTURE-V2)
добавляет вложенность и разделы:

* `GET /folders?parent_id=&section=` — плоский список (уровня или всего раздела);
* `GET /folders/tree?section=` — дерево папок;
* `GET /folders/{id}/path` — хлебные крошки от корня;
* `POST /folders` — тело `FolderCreateIn(name, parent_folder_id, section)`;
* `POST /folders/{id}/move` — перенос к другому родителю;
* `GET /folders/{id}/tracks?recursive=` — треки папки (по желанию с подпапками);
* `GET /folders/{id}/play?recursive=` — очередь аудио для плеера (ТЗ п. 16).

Маршрут `/tree` объявлен ДО `/{folder_id}`: иначе FastAPI попытается разобрать
«tree» как число и вернёт 422.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Path, Query, status

from backend.api.deps import CurrentUser
from backend.api.schemas import (
    FolderCreateIn,
    FolderMoveIn,
    FolderOut,
    FolderPathOut,
    FolderTreeOut,
    FolderUpdateIn,
    MoveTracksIn,
    TrackOut,
    folder_path_to_out,
    folder_tree_to_out,
    tracks_to_out,
)
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/folders", tags=["folders"])

# Сообщения об ошибках (видны пользователю Mini App).
FOLDER_NOT_FOUND: Final[str] = "Папка не найдена"
PARENT_FOLDER_NOT_FOUND: Final[str] = "Родительская папка не найдена"
TARGET_FOLDER_NOT_FOUND: Final[str] = "Папка, в которую переносим треки, не найдена"
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"

# Ограничения постраничной выдачи треков папки.
DEFAULT_TRACKS_LIMIT: Final[int] = 100
MAX_TRACKS_LIMIT: Final[int] = 500

# Ограничения очереди плеера (`/folders/{id}/play`).
DEFAULT_QUEUE_LIMIT: Final[int] = 500
MAX_QUEUE_LIMIT: Final[int] = 1000

# Допустимые значения параметра order (см. tracks_repo.ORDER_MAP).
ALLOWED_ORDERS: Final[tuple[str, ...]] = (
    "created_at_desc",
    "created_at_asc",
    "title",
    "artist",
    "play_count_desc",
    "last_played_desc",
)

#: Значение `section=all` — снять фильтр по разделу (папки обоих разделов).
SECTION_ANY: Final[str] = "all"

#: Значение `file_type=all` — снять фильтр по типу файла (поведение V1).
FILE_TYPE_ANY: Final[str] = "all"

#: `parent_id=0` — только корневые папки раздела (в query-строке нет `null`).
ROOT_PARENT_ID: Final[int] = 0


# ---------------------------------------------------------------------------
# Разбор и проверка параметров
# ---------------------------------------------------------------------------


def _check_order(order: str) -> str:
    """Проверяет порядок сортировки по белому списку контракта."""
    value = (order or "").strip().lower()
    if value not in ALLOWED_ORDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный порядок сортировки. Допустимые значения: "
            + ", ".join(ALLOWED_ORDERS),
        )
    return value


def _check_section(section: str | None) -> str | None:
    """Проверяет раздел: `music` | `other` | `all` (``None`` — без фильтра)."""
    value = (section or "").strip().lower()
    if not value:
        return folders_repo.DEFAULT_SECTION
    if value == SECTION_ANY:
        return None
    if value not in folders_repo.SECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный раздел. Допустимые значения: "
            + ", ".join((*folders_repo.SECTIONS, SECTION_ANY)),
        )
    return value


def _check_file_type(file_type: str | None) -> str | None:
    """Проверяет тип файла: значение из `tracks_repo.FILE_TYPES` или ``None``."""
    value = (file_type or "").strip().lower()
    if not value or value == FILE_TYPE_ANY:
        return None
    if value not in tracks_repo.FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный тип файла. Допустимые значения: "
            + ", ".join((*tracks_repo.FILE_TYPES, FILE_TYPE_ANY)),
        )
    return value


def _current_user_id(user: dict[str, Any]) -> int:
    """Достаёт идентификатор пользователя из данных зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %s", sorted(user))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=USER_UNKNOWN,
        )
    return int(raw)


async def _get_folder_or_404(user_id: int, folder_id: int) -> dict[str, Any]:
    """Возвращает папку пользователя либо отдаёт 404 с русским пояснением."""
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        logger.info("Пользователь %s запросил несуществующую папку %s", user_id, folder_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=FOLDER_NOT_FOUND,
        )
    return folder


async def _resolve_parent_filter(user_id: int, parent_id: int | None) -> Any:
    """Переводит query-параметр `parent_id` в фильтр репозитория.

    ``None`` (параметр не передан) — все папки раздела (поведение V1),
    ``0`` — только корневые, положительное число — только дети этой папки
    (папка должна существовать, иначе 404).
    """
    if parent_id is None:
        return folders_repo.UNSET
    if parent_id == ROOT_PARENT_ID:
        return None
    if await folders_repo.get_folder(user_id, parent_id) is None:
        logger.info(
            "Пользователь %s запросил подпапки несуществующей папки %s", user_id, parent_id
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PARENT_FOLDER_NOT_FOUND,
        )
    return int(parent_id)


# ---------------------------------------------------------------------------
# Список, дерево и создание
# ---------------------------------------------------------------------------


@router.get("", response_model=list[FolderOut], summary="Список папок")
async def list_folders(
    user: CurrentUser,
    parent_id: Annotated[
        int | None,
        Query(
            ge=0,
            description="Родительская папка: не передан — весь раздел, "
            "0 — только корневые, N — подпапки папки N",
        ),
    ] = None,
    section: Annotated[
        str, Query(description="Раздел: music, other или all")
    ] = folders_repo.DEFAULT_SECTION,
) -> list[dict[str, Any]]:
    """Папки пользователя в алфавитном порядке с количеством треков.

    У каждой папки есть собственный счётчик треков (`track_count`), счётчик
    вместе с подпапками (`total_track_count`) и признак `has_children`.
    """
    user_id = _current_user_id(user)
    section_name = _check_section(section)
    parent_filter = await _resolve_parent_filter(user_id, parent_id)
    return await folders_repo.list_folders(
        user_id,
        parent_folder_id=parent_filter,
        section=section_name,
        include_counts=True,
    )


@router.post(
    "",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать папку",
)
async def create_folder(payload: FolderCreateIn, user: CurrentUser) -> dict[str, Any]:
    """Создаёт папку (корневую или вложенную).

    Повторное создание с тем же названием на том же уровне вернёт существующую
    папку. Несуществующий родитель и превышение предела вложенности — 400.
    """
    user_id = _current_user_id(user)
    folder = await folders_repo.create_folder(
        user_id,
        payload.name,
        parent_folder_id=payload.parent_folder_id,
        section=payload.section,
    )
    logger.info(
        "Пользователь %s создал папку «%s» (id=%s, родитель=%s, раздел=%s)",
        user_id,
        folder["name"],
        folder["id"],
        folder["parent_folder_id"],
        folder["section"],
    )
    return folder


@router.get("/tree", response_model=list[FolderTreeOut], summary="Дерево папок")
async def folder_tree(
    user: CurrentUser,
    section: Annotated[
        str, Query(description="Раздел: music, other или all")
    ] = folders_repo.DEFAULT_SECTION,
) -> list[FolderTreeOut]:
    """Дерево папок раздела: корни с вложенными `children` на каждом уровне."""
    user_id = _current_user_id(user)
    section_name = _check_section(section)
    nodes = await folders_repo.folder_tree(user_id, section=section_name)
    return folder_tree_to_out(nodes)


# ---------------------------------------------------------------------------
# Одна папка
# ---------------------------------------------------------------------------


@router.get("/{folder_id}", response_model=FolderOut, summary="Папка")
async def get_folder(
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
) -> dict[str, Any]:
    """Возвращает одну папку пользователя."""
    user_id = _current_user_id(user)
    return await _get_folder_or_404(user_id, folder_id)


@router.patch("/{folder_id}", response_model=FolderOut, summary="Переименовать папку")
async def rename_folder(
    payload: FolderUpdateIn,
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
) -> dict[str, Any]:
    """Переименовывает папку; при конфликте названий на уровне репозиторий вернёт 400."""
    user_id = _current_user_id(user)
    folder = await folders_repo.rename_folder(user_id, folder_id, payload.name)
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=FOLDER_NOT_FOUND,
        )
    logger.info("Пользователь %s переименовал папку %s", user_id, folder_id)
    return folder


@router.delete("/{folder_id}", response_model=dict[str, bool], summary="Удалить папку")
async def delete_folder(
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
    delete_tracks: Annotated[
        bool, Query(description="Удалить треки вместе с папкой")
    ] = False,
    recursive: Annotated[
        bool, Query(description="Удалить вместе с вложенными папками")
    ] = True,
) -> dict[str, bool]:
    """Удаляет папку. По умолчанию треки сохраняются и остаются без папки.

    `recursive=false` у папки с подпапками — 400: иначе каскад тихо унёс бы всё
    поддерево.
    """
    user_id = _current_user_id(user)
    deleted = await folders_repo.delete_folder(
        user_id, folder_id, delete_tracks=delete_tracks, recursive=recursive
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=FOLDER_NOT_FOUND,
        )
    logger.info(
        "Пользователь %s удалил папку %s (с треками: %s, с подпапками: %s)",
        user_id,
        folder_id,
        delete_tracks,
        recursive,
    )
    return {"ok": True}


@router.get(
    "/{folder_id}/path", response_model=FolderPathOut, summary="Хлебные крошки папки"
)
async def folder_path(
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
) -> FolderPathOut:
    """Путь от корня раздела до папки включительно (для навигации Mini App)."""
    user_id = _current_user_id(user)
    folder = await _get_folder_or_404(user_id, folder_id)
    crumbs = await folders_repo.folder_path(user_id, folder_id)
    if not crumbs:  # pragma: no cover - папка удалена между двумя запросами
        logger.warning(
            "Не удалось собрать путь до папки %s пользователя %s", folder_id, user_id
        )
        crumbs = [folder]
    return folder_path_to_out(
        crumbs, folder_id=folder_id, section=str(folder.get("section") or "")
    )


@router.post("/{folder_id}/move", response_model=FolderOut, summary="Перенести папку")
async def move_folder(
    payload: FolderMoveIn,
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
) -> dict[str, Any]:
    """Переносит папку к другому родителю (`parent_folder_id = null` — в корень).

    Перенос в саму себя, в собственную подпапку, в другой раздел, за предел
    вложенности или к «тёзке» на новом уровне — 400 с русским пояснением.
    """
    user_id = _current_user_id(user)
    folder = await folders_repo.move_folder(user_id, folder_id, payload.parent_folder_id)
    logger.info(
        "Пользователь %s перенёс папку %s к родителю %s",
        user_id,
        folder_id,
        payload.parent_folder_id,
    )
    return folder


# ---------------------------------------------------------------------------
# Треки папки
# ---------------------------------------------------------------------------


@router.get("/{folder_id}/tracks", response_model=list[TrackOut], summary="Треки папки")
async def folder_tracks(
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
    limit: Annotated[int, Query(ge=1, le=MAX_TRACKS_LIMIT)] = DEFAULT_TRACKS_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    order: Annotated[str, Query(description="Порядок сортировки треков")] = "created_at_desc",
    recursive: Annotated[
        bool, Query(description="Считать вместе с треками подпапок")
    ] = False,
    file_type: Annotated[
        str,
        Query(description="Тип файла: audio, document, video, video_note, voice или all"),
    ] = FILE_TYPE_ANY,
) -> list[TrackOut]:
    """Постраничный список файлов папки.

    По умолчанию — только собственные файлы папки и все типы (как в V1).
    `recursive=true` добавляет содержимое подпапок, `file_type` сужает выдачу
    до одного типа (например `audio` для раздела «Треки»).
    """
    user_id = _current_user_id(user)
    order_key = _check_order(order)
    type_filter = _check_file_type(file_type)
    await _get_folder_or_404(user_id, folder_id)
    tracks = await tracks_repo.list_tracks(
        user_id,
        folder_id=folder_id,
        order=order_key,
        limit=limit,
        offset=offset,
        file_type=type_filter,
        folder_recursive=recursive,
    )
    return tracks_to_out(tracks, user_id)


@router.get(
    "/{folder_id}/play",
    response_model=list[TrackOut],
    summary="Очередь треков папки для плеера",
)
async def folder_play(
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
    recursive: Annotated[
        bool, Query(description="Включить треки подпапок")
    ] = True,
    order: Annotated[str, Query(description="Порядок треков в очереди")] = "created_at_desc",
    limit: Annotated[int, Query(ge=1, le=MAX_QUEUE_LIMIT)] = DEFAULT_QUEUE_LIMIT,
) -> list[TrackOut]:
    """Очередь аудио папки для плеера (ТЗ п. 16).

    В очередь попадают только файлы с `file_type = audio`; по умолчанию —
    вместе с подпапками. Прослушивания не засчитываются: их регистрирует
    `POST /tracks/{id}/play` при фактическом воспроизведении.
    """
    user_id = _current_user_id(user)
    order_key = _check_order(order)
    await _get_folder_or_404(user_id, folder_id)
    tracks = await tracks_repo.list_tracks(
        user_id,
        folder_id=folder_id,
        order=order_key,
        limit=limit,
        offset=0,
        file_type=tracks_repo.DEFAULT_FILE_TYPE,
        folder_recursive=recursive,
    )
    logger.info(
        "Пользователь %s собрал очередь из %s треков папки %s (с подпапками: %s)",
        user_id,
        len(tracks),
        folder_id,
        recursive,
    )
    return tracks_to_out(tracks, user_id)


@router.post(
    "/{folder_id}/tracks",
    response_model=dict[str, int],
    summary="Перенести треки в папку",
)
async def move_tracks_to_folder(
    payload: MoveTracksIn,
    user: CurrentUser,
    folder_id: Annotated[int, Path(ge=1, description="Идентификатор папки")],
) -> dict[str, int]:
    """Переносит треки в папку из пути.

    ``folder_id`` из тела запроса обычно дублирует путь и игнорируется; если он
    указан явно и отличается — треки уедут в него (папка должна существовать).
    """
    user_id = _current_user_id(user)
    await _get_folder_or_404(user_id, folder_id)

    target_id = folder_id
    body_folder_id = getattr(payload, "folder_id", None)
    if body_folder_id is not None and int(body_folder_id) != folder_id:
        target_id = int(body_folder_id)
        target = await folders_repo.get_folder(user_id, target_id)
        if target is None:
            logger.info(
                "Пользователь %s указал несуществующую целевую папку %s", user_id, target_id
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=TARGET_FOLDER_NOT_FOUND,
            )

    track_ids = list(payload.track_ids or [])
    if not track_ids:
        return {"moved": 0}

    moved = await tracks_repo.move_tracks(user_id, track_ids, target_id)
    logger.info(
        "Пользователь %s перенёс %s треков в папку %s", user_id, moved, target_id
    )
    return {"moved": int(moved)}
