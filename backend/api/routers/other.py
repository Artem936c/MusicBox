"""API раздела «Другое»: файлы, которые не являются аудио (контракт V2, раздел 4).

Раздел «Треки» — это ``tracks.file_type = 'audio'``; всё остальное (документы,
видео, кружочки и голосовые) живёт здесь. Файлы хранятся в той же таблице
``tracks``, поэтому их идентификаторы сквозные с треками, а папки раздела
отличаются значением ``folders.section = 'other'``.

Отдача файла (``GET /other/{id}/download``) намеренно повторяет модель доступа
аудиопотока: тег ``<a download>``/``<video>`` не умеет слать заголовок
``X-Telegram-Init-Data``, поэтому доступ подтверждается тем же подписанным
stream-токеном, что и ``GET /tracks/{id}/stream``. Проверка токена и обёртка
над потоком из Telegram переиспользуются из :mod:`backend.api.routers.tracks`
(``_authorize_by_token`` / ``_stream_response``) — здесь добавляется только
проверка принадлежности файла разделу и заголовок ``Content-Disposition``.
"""

from __future__ import annotations

import logging
import mimetypes
import unicodedata
from typing import Annotated, Any, Callable, Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser, get_bot
from backend.api.routers.tracks import _authorize_by_token, _stream_response
from backend.api.schemas import (
    MAX_NAME_LENGTH,
    FolderOut,
    TrackOut,
    folder_to_out,
    folders_to_out,
    tracks_to_out,
)
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/other", tags=["other"])

#: Раздел папок, которому принадлежит «Другое».
SECTION: Final[str] = "other"

#: Типы файлов раздела (всё, кроме ``audio``).
OTHER_FILE_TYPES: Final[tuple[str, ...]] = tracks_repo.NON_AUDIO_FILE_TYPES

#: Русские названия типов — для сообщений об ошибках.
FILE_TYPE_TITLES: Final[dict[str, str]] = {
    "document": "документ",
    "video": "видео",
    "video_note": "видеосообщение (кружочек)",
    "voice": "голосовое сообщение",
}

#: Сообщения об ошибках (видны пользователю Mini App).
FILE_NOT_FOUND: Final[str] = "Файл не найден. Возможно, он уже удалён."
FOLDER_NOT_FOUND: Final[str] = "Папка не найдена. Обновите список папок."
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"
AUDIO_NOT_HERE: Final[str] = (
    "Это аудиотрек, а не файл раздела «Другое». Откройте его в разделе «Треки»."
)
FILE_UNAVAILABLE: Final[str] = (
    "Файл недоступен в хранилище. Попробуйте загрузить его заново."
)

#: Ограничения выдачи списка.
DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 200

#: Максимальная длина поискового запроса (совпадает с роутером поиска).
MAX_QUERY_LENGTH: Final[int] = 200

#: Сколько результатов нечёткого поиска забирать до фильтрации по папке.
#: Поиск возвращает совпадения по всему разделу, поэтому при активном фильтре
#: по папке выборку приходится брать с запасом.
SEARCH_FETCH_CAP: Final[int] = 500

#: Имя файла по умолчанию, если у записи нет ни имени, ни названия.
FALLBACK_FILE_NAME: Final[str] = "file"

#: Предел длины имени файла в заголовке Content-Disposition.
MAX_FILE_NAME_LENGTH: Final[int] = 120


# ---------------------------------------------------------------------------
# Схемы запросов
# ---------------------------------------------------------------------------


class OtherFolderCreateIn(BaseModel):
    """Создание папки раздела «Другое».

    Раздел не принимается из тела запроса: маршрут работает только с ``other``,
    а вложенная папка в любом случае наследует раздел родителя.
    """

    name: str = Field(..., description="Название папки")
    parent_folder_id: int | None = Field(
        default=None, description="Родительская папка; null — папка верхнего уровня"
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Название папки не может быть пустым")
        if len(cleaned) > MAX_NAME_LENGTH:
            raise ValueError(
                f"Название папки слишком длинное (максимум {MAX_NAME_LENGTH} символов)"
            )
        return cleaned

    @field_validator("parent_folder_id")
    @classmethod
    def _validate_parent(cls, value: int | None) -> int | None:
        if value is None:
            return None
        number = int(value)
        if number <= 0:
            raise ValueError("Идентификатор родительской папки должен быть положительным")
        return number


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _user_id(user: dict[str, Any]) -> int:
    """Достаёт идентификатор пользователя из данных зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %s", sorted(user))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=USER_UNKNOWN
        )
    return int(raw)


def _clean_query(value: str | None) -> str:
    """Нормализует поисковый запрос: схлопывает пробелы и режет длину."""
    return " ".join(str(value or "").split())[:MAX_QUERY_LENGTH]


def _check_order(order: str) -> str:
    """Проверяет порядок сортировки по белому списку репозитория треков."""
    value = (order or "").strip().lower()
    if value not in tracks_repo.ORDER_MAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный порядок сортировки. Допустимые значения: "
            + ", ".join(tracks_repo.ORDER_MAP),
        )
    return value


def _check_file_type(file_type: str | None) -> str | None:
    """Проверяет фильтр по типу файла: разрешены только типы раздела «Другое»."""
    if file_type is None:
        return None
    value = file_type.strip().lower()
    if not value:
        return None
    if value not in OTHER_FILE_TYPES:
        known = ", ".join(
            f"{name} ({FILE_TYPE_TITLES.get(name, name)})" for name in OTHER_FILE_TYPES
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"В разделе «Другое» нет типа «{file_type}». Доступны: {known}",
        )
    return value


def _resolve_folder_filter(folder_id: int | None) -> Any:
    """Преобразует query-параметр в значение фильтра репозитория.

    ``None`` (параметр не передан) — без фильтра; ``0`` и меньше — только файлы
    без папки; положительное число — конкретная папка.
    """
    if folder_id is None:
        return tracks_repo.UNSET
    if folder_id <= 0:
        return None
    return int(folder_id)


async def _ensure_folder_exists(user_id: int, folder_id: int) -> dict[str, Any]:
    """Проверяет, что папка существует у пользователя, иначе — 404."""
    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        logger.info("Пользователь %s запросил несуществующую папку %s", user_id, folder_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=FOLDER_NOT_FOUND
        )
    if folder.get("section") != SECTION:
        # Файл мог попасть в музыкальную папку (например, при ручном переносе) —
        # это не ошибка, но такое стоит видеть в журнале.
        logger.debug(
            "Папка %s пользователя %s относится к разделу «%s», а запрошена в «Другом»",
            folder_id,
            user_id,
            folder.get("section"),
        )
    return folder


async def _allowed_folder_ids(user_id: int, folder_id: int, recursive: bool) -> set[int]:
    """Идентификаторы папки (и её подпапок при ``recursive``) для фильтра поиска."""
    if not recursive:
        return {int(folder_id)}
    ids = await folders_repo.descendant_ids(user_id, folder_id, include_self=True)
    return {int(value) for value in ids} or {int(folder_id)}


def _folder_predicate(
    folder_filter: Any, allowed_ids: set[int]
) -> Callable[[dict[str, Any]], bool]:
    """Строит проверку «файл лежит в нужной папке» для результатов поиска."""
    if folder_filter is tracks_repo.UNSET:
        return lambda item: True
    if folder_filter is None:
        return lambda item: item.get("folder_id") is None
    return lambda item: item.get("folder_id") is not None and int(item["folder_id"]) in allowed_ids


async def _search_files(
    user_id: int,
    query: str,
    *,
    folder_filter: Any,
    recursive: bool,
    file_type: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Нечёткий поиск по разделу «Другое» с фильтрами по папке и типу файла.

    Репозиторий ищет по всему разделу, поэтому фильтры применяются здесь, а
    выборка берётся с запасом. Если запас исчерпан, в журнал пишется
    предупреждение: часть совпадений могла не попасть в ответ.
    """
    filtered_by_folder = folder_filter is not tracks_repo.UNSET
    needs_reserve = filtered_by_folder or file_type is not None
    fetch_limit = SEARCH_FETCH_CAP if needs_reserve else limit + offset
    fetch_limit = max(1, min(SEARCH_FETCH_CAP, fetch_limit))

    try:
        found = await tracks_repo.search_in(
            user_id, query, scope="other", limit=fetch_limit
        )
    except ValidationError as exc:  # pragma: no cover — scope задан константой
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    allowed_ids: set[int] = set()
    if filtered_by_folder and folder_filter is not None:
        allowed_ids = await _allowed_folder_ids(user_id, int(folder_filter), recursive)

    matches = _folder_predicate(folder_filter, allowed_ids)
    result = [item for item in found if matches(item)]
    if file_type is not None:
        result = [item for item in result if item.get("file_type") == file_type]

    if len(found) >= SEARCH_FETCH_CAP:
        logger.warning(
            "Поиск «%s» в разделе «Другое» упёрся в предел %d результатов — "
            "часть совпадений могла не попасть в выдачу (пользователь %s)",
            query,
            SEARCH_FETCH_CAP,
            user_id,
        )
    if offset >= len(result):
        return []
    return result[offset : offset + limit]


def _sanitize_file_name(name: str) -> str:
    """Убирает из имени файла разделители пути и управляющие символы."""
    cleaned = "".join(
        char
        for char in name.replace("\\", "/").split("/")[-1]
        if unicodedata.category(char)[0] != "C"
    ).strip()
    cleaned = cleaned.strip(". ")
    if len(cleaned) > MAX_FILE_NAME_LENGTH:
        cleaned = cleaned[:MAX_FILE_NAME_LENGTH].strip()
    return cleaned


def _download_name(record: dict[str, Any]) -> str:
    """Имя файла для скачивания: из ``file_name``, иначе из названия и mime-типа."""
    raw = str(record.get("file_name") or "").strip()
    cleaned = _sanitize_file_name(raw) if raw else ""
    if cleaned:
        return cleaned

    title = _sanitize_file_name(str(record.get("title") or "").strip())
    if not title:
        title = FALLBACK_FILE_NAME
    mime_type = str(record.get("mime_type") or "").strip()
    extension = mimetypes.guess_extension(mime_type) if mime_type else None
    return f"{title}{extension}" if extension else title


def _ascii_fallback(name: str) -> str:
    """ASCII-версия имени файла для старых клиентов (параметр ``filename``)."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = folded.replace('"', "").replace("\\", "").strip()
    return folded or FALLBACK_FILE_NAME


def _content_disposition(record: dict[str, Any], *, inline: bool) -> str:
    """Заголовок ``Content-Disposition`` с именем файла (RFC 5987 + ASCII-запас)."""
    name = _download_name(record)
    kind = "inline" if inline else "attachment"
    return (
        f"{kind}; filename=\"{_ascii_fallback(name)}\"; "
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


async def _authorize_download(other_id: int, token: str) -> dict[str, Any]:
    """Проверяет stream-токен и то, что файл действительно из раздела «Другое»."""
    record = await _authorize_by_token(other_id, token)
    file_type = tracks_repo.normalize_file_type(record.get("file_type"), strict=False)
    if file_type == tracks_repo.DEFAULT_FILE_TYPE:
        logger.info(
            "Запрошено скачивание аудиотрека %s через раздел «Другое»", other_id
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=AUDIO_NOT_HERE
        )
    return record


# ---------------------------------------------------------------------------
# Папки раздела
# ---------------------------------------------------------------------------


@router.get("/folders", response_model=list[FolderOut], summary="Папки раздела «Другое»")
async def list_other_folders(
    user: CurrentUser,
    parent_id: Annotated[
        int | None,
        Query(
            description=(
                "Родительская папка: не передан — все папки раздела, "
                "0 — только папки верхнего уровня, число — подпапки этой папки"
            )
        ),
    ] = None,
) -> list[FolderOut]:
    """Плоский список папок раздела «Другое» с количеством файлов."""
    user_id = _user_id(user)

    if parent_id is None:
        parent_filter: Any = folders_repo.UNSET
    elif parent_id <= 0:
        parent_filter = None
    else:
        await _ensure_folder_exists(user_id, int(parent_id))
        parent_filter = int(parent_id)

    folders = await folders_repo.list_folders(
        user_id, parent_folder_id=parent_filter, section=SECTION, include_counts=True
    )
    return folders_to_out(folders)


@router.post(
    "/folders",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать папку раздела «Другое»",
)
async def create_other_folder(payload: OtherFolderCreateIn, user: CurrentUser) -> FolderOut:
    """Создаёт папку раздела «Другое» (повторный вызов вернёт существующую)."""
    user_id = _user_id(user)
    if payload.parent_folder_id is not None:
        await _ensure_folder_exists(user_id, int(payload.parent_folder_id))

    try:
        folder = await folders_repo.create_folder(
            user_id,
            payload.name,
            parent_folder_id=payload.parent_folder_id,
            section=SECTION,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    logger.info(
        "Пользователь %s создал папку «%s» в разделе «Другое» (id=%s, родитель=%s)",
        user_id,
        folder["name"],
        folder["id"],
        folder.get("parent_folder_id"),
    )
    return folder_to_out(folder)


# ---------------------------------------------------------------------------
# Список файлов
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TrackOut], summary="Файлы раздела «Другое»")
@router.get("/", response_model=list[TrackOut], include_in_schema=False)
async def list_other(
    user: CurrentUser,
    folder_id: Annotated[
        int | None,
        Query(description="ID папки; 0 — только файлы без папки; не передан — все"),
    ] = None,
    q: Annotated[str, Query(description="Поисковый запрос (нечёткий поиск)")] = "",
    file_type: Annotated[
        str | None,
        Query(description="Тип файла: document, video, video_note, voice"),
    ] = None,
    recursive: Annotated[
        bool, Query(description="Вместе с файлами вложенных папок")
    ] = False,
    order: Annotated[str, Query(description="Порядок сортировки")] = "created_at_desc",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TrackOut]:
    """Файлы пользователя, не являющиеся аудио: документы, видео, кружочки, голосовые.

    Без ``q`` это обычный постраничный список раздела; с ``q`` — нечёткий поиск
    по названию, имени файла и исполнителю внутри раздела (фильтры по папке и
    типу файла продолжают действовать).
    """
    user_id = _user_id(user)
    query = _clean_query(q)
    type_filter = _check_file_type(file_type)
    folder_filter = _resolve_folder_filter(folder_id)

    if folder_filter is not tracks_repo.UNSET and folder_filter is not None:
        await _ensure_folder_exists(user_id, int(folder_filter))

    if query:
        files = await _search_files(
            user_id,
            query,
            folder_filter=folder_filter,
            recursive=recursive,
            file_type=type_filter,
            limit=limit,
            offset=offset,
        )
    else:
        files = await tracks_repo.list_tracks(
            user_id,
            folder_id=folder_filter,
            file_type=type_filter or OTHER_FILE_TYPES,
            folder_recursive=recursive,
            order=_check_order(order),
            limit=limit,
            offset=offset,
        )

    logger.debug(
        "Раздел «Другое» пользователя %s: отдано %d файлов (папка %s, запрос %r, тип %s)",
        user_id,
        len(files),
        folder_id,
        query,
        type_filter or "любой",
    )
    return tracks_to_out(files, user_id)


# ---------------------------------------------------------------------------
# Отдача файла (авторизация по подписанному токену)
# ---------------------------------------------------------------------------


@router.get("/{other_id}/download", summary="Скачать файл раздела «Другое»")
async def download_other(
    request: Request,
    other_id: Annotated[int, Path(ge=1, description="Идентификатор файла")],
    token: Annotated[str, Query(min_length=1, description="Подписанный stream-токен")],
    inline: Annotated[
        bool,
        Query(description="Отдать для просмотра в браузере, а не скачиванием"),
    ] = False,
) -> StreamingResponse:
    """Отдаёт файл раздела «Другое» потоком с поддержкой Range-запросов.

    Токен — тот же, что и у ``GET /tracks/{id}/stream``: он выдаётся в полях
    ``stream_url`` карточки файла, поэтому отдельного механизма доступа нет.
    """
    # Токен проверяется ДО обращения к боту: неавторизованный запрос не должен
    # узнавать о состоянии сервиса (иначе 503 приходил бы раньше 401).
    record = await _authorize_download(other_id, token)
    bot = get_bot(request)

    file_id = record.get("file_id")
    if not file_id:
        logger.error("У файла %s нет file_id — отдавать нечего", other_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=FILE_UNAVAILABLE
        )

    response = await _stream_response(bot, str(file_id), request.headers.get("range"))
    response.headers["Content-Disposition"] = _content_disposition(record, inline=inline)
    return response


__all__ = ["OtherFolderCreateIn", "router"]
