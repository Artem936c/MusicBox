"""API треков: список, карточка, изменение, удаление, прослушивания и отдача файлов.

Особенность модуля — два публичных маршрута (``/stream`` и ``/cover``), которые не
проходят через ``get_current_user``: тег ``<audio>`` и ``<img>`` не умеют слать
заголовок ``X-Telegram-Init-Data``, поэтому доступ к файлу подтверждается
подписанным одноразовым токеном (``backend.api.security.create_stream_token``).

V2 (раздел 4 контракта ARCHITECTURE-V2):

* ``GET /tracks`` принимает ``file_type`` (по умолчанию ``audio`` — раздел
  «Треки») и ``q`` — нечёткий поиск внутри раздела;
* ``GET /tracks/play_all`` отдаёт очередь всех аудиофайлов пользователя.
  Маршрут объявлен ДО ``/tracks/{track_id}``: иначе FastAPI попытается разобрать
  ``play_all`` как ``int`` и вернёт 422;
* ``PATCH /tracks/{track_id}`` умеет менять название (``tracks_repo.rename_track``)
  и состав исполнителей (``tracks_repo.set_track_artists``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from aiogram import Bot
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from backend.api.deps import CurrentUser, get_bot
from backend.api.schemas import (
    AssignFolderIn,
    MoveTrackIn,
    PlayIn,
    TrackOut,
    TrackUpdateIn,
    track_to_out,
    tracks_to_out,
)
from backend.api.security import verify_stream_token
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import favourites as favourites_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import AuthError, FileTooLargeError, StorageError, ValidationError
from backend.services import autosort, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tracks", tags=["tracks"])

BotDep = Annotated[Bot, Depends(get_bot)]

#: Допустимые значения параметра ``order`` (см. tracks_repo.ORDER_MAP).
ALLOWED_ORDERS: Final[tuple[str, ...]] = (
    "created_at_desc",
    "created_at_asc",
    "title",
    "artist",
    "play_count_desc",
    "last_played_desc",
)

#: Псевдотип «все файлы без фильтра» для параметра ``file_type``.
FILE_TYPE_ALL: Final[str] = "all"

#: Псевдотип «раздел „Другое“» — все типы, кроме ``audio``.
FILE_TYPE_OTHER: Final[str] = "other"

#: Полный белый список значений параметра ``file_type``.
ALLOWED_FILE_TYPES: Final[tuple[str, ...]] = (
    *tracks_repo.FILE_TYPES,
    FILE_TYPE_OTHER,
    FILE_TYPE_ALL,
)

#: Сколько результатов нечёткого поиска просматривать до применения фильтров.
#: Поиск и так читает библиотеку целиком, поэтому запас берётся один раз.
SEARCH_SCAN_LIMIT: Final[int] = 500

#: Размер очереди ``/tracks/play_all`` по умолчанию и максимальный.
DEFAULT_QUEUE_LIMIT: Final[int] = 500
MAX_QUEUE_LIMIT: Final[int] = 1000

#: Сообщения об ошибках (RU) — одинаковые формулировки во всех маршрутах.
TRACK_NOT_FOUND: Final[str] = "Трек не найден. Возможно, он уже удалён."
FOLDER_NOT_FOUND: Final[str] = "Папка не найдена. Обновите список папок."
ARTIST_NOT_FOUND: Final[str] = "Исполнитель не найден. Обновите список исполнителей."
BAD_TOKEN: Final[str] = "Ссылка на файл недействительна или устарела. Откройте трек заново."
USER_UNKNOWN: Final[str] = "Не удалось определить пользователя"


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


def _resolve_folder_filter(folder_id: int | None) -> Any:
    """Преобразует query-параметр в значение фильтра репозитория.

    ``None`` (параметр не передан) — без фильтра; ``0`` и меньше — только треки
    без папки; положительное число — конкретная папка.
    """
    if folder_id is None:
        return tracks_repo.UNSET
    if folder_id <= 0:
        return None
    return int(folder_id)


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


def _resolve_file_type(file_type: str | None) -> Any:
    """Преобразует query-параметр ``file_type`` в значение фильтра репозитория.

    Пустое значение — ``audio`` (раздел «Треки»), ``all`` — без фильтра,
    ``other`` — раздел «Другое» (все типы, кроме аудио), остальное — конкретный
    тип из :data:`tracks_repo.FILE_TYPES`.
    """
    value = (file_type or "").strip().lower()
    if not value:
        return tracks_repo.DEFAULT_FILE_TYPE
    if value == FILE_TYPE_ALL:
        return None
    if value == FILE_TYPE_OTHER:
        return tracks_repo.NON_AUDIO_FILE_TYPES
    if value in tracks_repo.FILE_TYPES:
        return value
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Неизвестный тип файла. Допустимые значения: "
        + ", ".join(ALLOWED_FILE_TYPES),
    )


def _search_scopes(file_type_filter: Any) -> tuple[str, ...]:
    """Разделы ``tracks_repo.search_in``, соответствующие фильтру по типу файла."""
    if file_type_filter is None:
        return ("tracks", "other")
    if file_type_filter == tracks_repo.DEFAULT_FILE_TYPE:
        return ("tracks",)
    return ("other",)


def _file_type_matches(track: dict[str, Any], file_type_filter: Any) -> bool:
    """Проверяет строку трека на соответствие фильтру по типу файла."""
    if file_type_filter is None:
        return True
    actual = str(track.get("file_type") or tracks_repo.DEFAULT_FILE_TYPE).lower()
    if isinstance(file_type_filter, (list, tuple, set, frozenset)):
        return actual in {str(item).lower() for item in file_type_filter}
    return actual == str(file_type_filter).lower()


def _track_matches_filters(
    track: dict[str, Any],
    *,
    file_type_filter: Any,
    folder_filter: Any,
    artist_id: int | None,
    album_id: int | None,
) -> bool:
    """Применяет фильтры списка к найденному поиском треку.

    ``tracks_repo.search_in`` ищет по разделу целиком, поэтому остальные
    фильтры (папка, исполнитель, альбом) применяются здесь — к уже полученным
    строкам, без повторного запроса в БД.
    """
    if not _file_type_matches(track, file_type_filter):
        return False

    if folder_filter is not tracks_repo.UNSET:
        actual_folder = track.get("folder_id")
        if folder_filter is None:
            if actual_folder is not None:
                return False
        elif actual_folder is None or int(actual_folder) != int(folder_filter):
            return False

    if artist_id is not None:
        primary = track.get("artist_id")
        linked = tracks_repo.parse_artist_ids(track)
        if int(artist_id) not in linked and (
            primary is None or int(primary) != int(artist_id)
        ):
            return False

    if album_id is not None:
        actual_album = track.get("album_id")
        if actual_album is None or int(actual_album) != int(album_id):
            return False

    return True


async def _search_tracks(
    user_id: int,
    query: str,
    *,
    file_type_filter: Any,
    folder_filter: Any,
    artist_id: int | None,
    album_id: int | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Нечёткий поиск треков внутри раздела с последующей фильтрацией и пагинацией."""
    found: dict[int, dict[str, Any]] = {}
    for scope in _search_scopes(file_type_filter):
        try:
            rows = await tracks_repo.search_in(
                user_id, query, scope=scope, limit=SEARCH_SCAN_LIMIT
            )
        except ValidationError as exc:  # неизвестный раздел — ошибка контракта
            logger.error("Поиск в разделе %r отклонён: %s", scope, exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        for row in rows:
            track_id = int(row["id"])
            previous = found.get(track_id)
            if previous is None or int(row.get("score") or 0) > int(
                previous.get("score") or 0
            ):
                found[track_id] = row

    matched = [
        row
        for row in found.values()
        if _track_matches_filters(
            row,
            file_type_filter=file_type_filter,
            folder_filter=folder_filter,
            artist_id=artist_id,
            album_id=album_id,
        )
    ]
    matched.sort(
        key=lambda row: (
            -int(row.get("score") or 0),
            str(row.get("title") or "").casefold(),
            int(row["id"]),
        )
    )
    logger.debug(
        "Поиск «%s» по трекам пользователя %s: подходит %d из %d",
        query,
        user_id,
        len(matched),
        len(found),
    )
    return matched[offset : offset + limit]


def _artists_update(payload: TrackUpdateIn) -> list[int] | None:
    """Новый состав исполнителей из тела запроса; ``None`` — «не менять».

    ``TrackUpdateIn.updates()`` намеренно не отдаёт ``artist_ids``: состав пишет
    отдельный вызов ``tracks_repo.set_track_artists``. Явный ``null`` в теле,
    как и пустой список, означает «убрать всех исполнителей».
    """
    if "artist_ids" not in payload.model_fields_set:
        return None
    return list(getattr(payload, "artist_ids", None) or [])


def _check_new_title(new_title: str) -> str:
    """Проверяет новое название трека ДО первой записи и возвращает его.

    Лимит длины в схеме (``schemas.MAX_TITLE_LENGTH``) шире репозиторного
    (``tracks_repo.MAX_TITLE_LENGTH``), поэтому слишком длинное название
    проходит pydantic и падает уже в ``tracks_repo.rename_track`` — последнем
    шаге PATCH, когда остальные поля и состав исполнителей УЖЕ записаны.
    Общей транзакции у трёх записей нет, поэтому название проверяется заранее:
    при ошибке запрос отклоняется, ничего не записав.
    """
    title = new_title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Название трека не может быть пустым",
        )
    if len(title) > tracks_repo.MAX_TITLE_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Название трека слишком длинное "
                f"(максимум {tracks_repo.MAX_TITLE_LENGTH} символов)"
            ),
        )
    return title


async def _ensure_artists_exist(user_id: int, artist_ids: list[int]) -> None:
    """Проверяет, что все переданные исполнители принадлежат пользователю."""
    for artist_id in artist_ids:
        artist = await artists_repo.get_artist(user_id, int(artist_id))
        if artist is None:
            logger.info(
                "Пользователь %s указал несуществующего исполнителя %s",
                user_id,
                artist_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=ARTIST_NOT_FOUND
            )


async def _ensure_folder_exists(user_id: int, folder_id: int | None) -> None:
    """Проверяет, что папка существует у пользователя (``None`` — «без папки»)."""
    if folder_id is None:
        return
    folder = await folders_repo.get_folder(user_id, int(folder_id))
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FOLDER_NOT_FOUND)


async def _get_track_or_404(user_id: int, track_id: int) -> dict:
    """Возвращает трек пользователя либо бросает 404 с русским текстом."""
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)
    return track


async def _authorize_by_token(track_id: int, token: str) -> dict:
    """Проверяет stream-токен и возвращает трек, к которому он выдан."""
    try:
        token_user_id, token_track_id = verify_stream_token(token)
    except AuthError as exc:
        logger.info("Отклонён stream-токен для трека %s: %s", track_id, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc) or BAD_TOKEN
        ) from exc

    if int(token_track_id) != int(track_id):
        logger.warning(
            "Stream-токен выдан на трек %s, а запрошен трек %s", token_track_id, track_id
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=BAD_TOKEN)

    track = await tracks_repo.get_track(int(token_user_id), int(track_id))
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)
    return track


async def _stream_response(
    bot: Bot, file_id: str, range_header: str | None
) -> StreamingResponse:
    """Готовит потоковый ответ из Telegram, переводя ошибки хранилища в HTTP."""
    try:
        chunks, headers, status_code = await storage.stream_file(
            bot, file_id, range_header=range_header
        )
    except FileTooLargeError as exc:
        logger.warning("Файл %s слишком большой для отдачи: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except StorageError as exc:
        logger.warning("Не удалось отдать файл %s: %s", file_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    media_type = headers.get("Content-Type")
    return StreamingResponse(
        chunks,
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )


# ---------------------------------------------------------------------------
# Список и карточка трека
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TrackOut], summary="Список треков")
@router.get("/", response_model=list[TrackOut], include_in_schema=False)
async def list_tracks(
    user: CurrentUser,
    folder_id: int | None = Query(None, description="ID папки; 0 — только треки без папки"),
    artist_id: int | None = Query(None, ge=1, description="Фильтр по исполнителю"),
    album_id: int | None = Query(None, ge=1, description="Фильтр по альбому"),
    order: str = Query("created_at_desc", description="Порядок сортировки"),
    limit: int = Query(50, ge=1, le=200, description="Сколько треков вернуть"),
    offset: int = Query(0, ge=0, description="Сколько треков пропустить"),
    file_type: str = Query(
        tracks_repo.DEFAULT_FILE_TYPE,
        description=(
            "Тип файла: audio (раздел «Треки»), other (раздел «Другое»), "
            "all (без фильтра) или конкретный тип"
        ),
    ),
    q: str | None = Query(None, description="Нечёткий поиск по названию и исполнителю"),
) -> list[TrackOut]:
    """Возвращает треки пользователя с фильтрами, поиском и постраничной выборкой.

    Без ``q`` список берётся из БД сортированным (``order``). С ``q`` результат
    упорядочен по релевантности нечёткого поиска, а ``order`` игнорируется —
    об этом сказано в описании параметра.
    """
    user_id = _user_id(user)
    order_key = _check_order(order)
    file_type_filter = _resolve_file_type(file_type)
    folder_filter = _resolve_folder_filter(folder_id)

    needle = (q or "").strip()
    if needle:
        tracks = await _search_tracks(
            user_id,
            needle,
            file_type_filter=file_type_filter,
            folder_filter=folder_filter,
            artist_id=artist_id,
            album_id=album_id,
            limit=limit,
            offset=offset,
        )
    else:
        tracks = await tracks_repo.list_tracks(
            user_id,
            folder_id=folder_filter,
            artist_id=artist_id,
            album_id=album_id,
            order=order_key,
            limit=limit,
            offset=offset,
            file_type=file_type_filter,
        )
    return tracks_to_out(tracks, user_id)


# ВНИМАНИЕ: маршрут объявлен ДО «/{track_id}» намеренно. FastAPI выбирает первый
# подошедший путь по порядку регистрации, поэтому при обратном порядке запрос
# «/tracks/play_all» попал бы в карточку трека и вернул 422 на разборе int.
@router.get(
    "/play_all",
    response_model=list[TrackOut],
    summary="Очередь: все треки пользователя",
)
async def play_all(
    user: CurrentUser,
    order: str = Query("created_at_desc", description="Порядок треков в очереди"),
    limit: int = Query(
        DEFAULT_QUEUE_LIMIT,
        ge=1,
        le=MAX_QUEUE_LIMIT,
        description="Максимальная длина очереди",
    ),
    offset: int = Query(0, ge=0, description="Сколько треков пропустить"),
) -> list[TrackOut]:
    """Возвращает очередь воспроизведения из всех аудиофайлов пользователя.

    Файлы раздела «Другое» (документы, видео, кружочки, голосовые) в очередь не
    попадают: их нельзя проигрывать плеером Mini App.
    """
    user_id = _user_id(user)
    tracks = await tracks_repo.list_tracks(
        user_id,
        order=_check_order(order),
        limit=limit,
        offset=offset,
        file_type=tracks_repo.DEFAULT_FILE_TYPE,
    )
    logger.info(
        "Пользователь %s запросил очередь «слушать всё»: %d трек(ов)",
        user_id,
        len(tracks),
    )
    return tracks_to_out(tracks, user_id)


@router.get("/{track_id}", response_model=TrackOut, summary="Карточка трека")
async def get_track(user: CurrentUser, track_id: int) -> TrackOut:
    """Возвращает один трек пользователя."""
    user_id = _user_id(user)
    track = await _get_track_or_404(user_id, track_id)
    return track_to_out(track, user_id)


@router.patch("/{track_id}", response_model=TrackOut, summary="Изменить трек")
async def update_track(user: CurrentUser, track_id: int, payload: TrackUpdateIn) -> TrackOut:
    """Меняет название, исполнителей, альбом и/или папку трека.

    Название идёт через ``tracks_repo.rename_track`` (проверка длины и пустоты),
    состав исполнителей — через ``tracks_repo.set_track_artists`` (он же
    синхронизирует денормализованный ``tracks.artist_id``). Поэтому изменение
    состава применяется ПОСЛЕ остальных полей, а итоговый трек перечитывается,
    чтобы агрегаты исполнителей в ответе были актуальными. Название при этом
    проверяется ``_check_new_title`` ДО первой записи: иначе ошибка на последнем
    шаге вернула бы 400 уже после записи остальных полей.
    """
    user_id = _user_id(user)
    await _get_track_or_404(user_id, track_id)

    fields = payload.updates()
    artist_ids = _artists_update(payload)
    new_title = fields.pop("title", None)

    # Все проверки — ДО первой записи: три записи ниже идут разными
    # транзакциями, и падение на последней оставило бы трек изменённым наполовину.
    if new_title is not None:
        new_title = _check_new_title(new_title)
    if "folder_id" in fields:
        await _ensure_folder_exists(user_id, fields["folder_id"])
    if artist_ids is not None:
        await _ensure_artists_exist(user_id, artist_ids)

    try:
        if fields:
            if await tracks_repo.update_track(user_id, track_id, **fields) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND
                )
        if artist_ids is not None:
            await tracks_repo.set_track_artists(user_id, track_id, artist_ids)
        if new_title is not None:
            if await tracks_repo.rename_track(user_id, track_id, new_title) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND
                )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    track = await _get_track_or_404(user_id, track_id)
    return track_to_out(track, user_id)


@router.delete("/{track_id}", summary="Удалить трек")
async def delete_track(
    request: Request,
    user: CurrentUser,
    track_id: int,
    delete_from_channel: bool = Query(
        False, description="Удалить файл и из канала-хранилища"
    ),
) -> dict[str, bool]:
    """Удаляет трек из библиотеки, при необходимости — и из канала-хранилища."""
    user_id = _user_id(user)
    track = await tracks_repo.delete_track(user_id, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)

    if delete_from_channel:
        # Ошибка канала не должна ломать удаление: трек из БД уже убран.
        bot: Bot | None = getattr(request.app.state, "bot", None)
        if bot is None:
            logger.warning(
                "Бот недоступен — сообщение %s не удалено из канала",
                track.get("storage_message_id"),
            )
        else:
            try:
                await storage.delete_from_channel(bot, track.get("storage_message_id"))
            except Exception:  # noqa: BLE001 — канал не должен влиять на ответ API
                logger.exception(
                    "Не удалось удалить сообщение %s из канала-хранилища",
                    track.get("storage_message_id"),
                )

    return {"ok": True}


# ---------------------------------------------------------------------------
# Прослушивания, папки и избранное
# ---------------------------------------------------------------------------


@router.post("/{track_id}/play", response_model=TrackOut, summary="Учесть прослушивание")
async def register_play(
    user: CurrentUser,
    track_id: int,
    payload: PlayIn | None = Body(None),
) -> TrackOut:
    """Увеличивает счётчик прослушиваний трека и пишет запись в историю."""
    user_id = _user_id(user)
    source = (payload.source if payload is not None else "web") or "web"

    track = await tracks_repo.register_play(user_id, track_id, source=source)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)

    logger.debug(
        "Прослушивание трека %s учтено (пользователь %s, источник %s)",
        track_id,
        user_id,
        source,
    )
    return track_to_out(track, user_id)


@router.post("/{track_id}/move", response_model=TrackOut, summary="Перенести трек в папку")
async def move_track(user: CurrentUser, track_id: int, payload: MoveTrackIn) -> TrackOut:
    """Переносит трек в существующую папку или выносит его из папок."""
    user_id = _user_id(user)
    await _get_track_or_404(user_id, track_id)
    await _ensure_folder_exists(user_id, payload.folder_id)

    track = await tracks_repo.move_track(user_id, track_id, payload.folder_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TRACK_NOT_FOUND)
    return track_to_out(track, user_id)


@router.post("/{track_id}/folder", response_model=TrackOut, summary="Папка по названию")
async def assign_folder(user: CurrentUser, track_id: int, payload: AssignFolderIn) -> TrackOut:
    """Кладёт трек в папку по названию, создавая её при отсутствии."""
    user_id = _user_id(user)
    await _get_track_or_404(user_id, track_id)

    folder_name = (payload.folder_name or "").strip()
    if not folder_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Название папки не может быть пустым.",
        )

    try:
        track = await autosort.assign_to_folder(user_id, track_id, folder_name)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if track is None:
        logger.warning(
            "Не удалось перенести трек %s в папку %r (пользователь %s)",
            track_id,
            folder_name,
            user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось перенести трек в папку «{folder_name}».",
        )
    return track_to_out(track, user_id)


@router.post("/{track_id}/favourite", summary="Избранное: переключить")
async def toggle_favourite(user: CurrentUser, track_id: int) -> dict[str, bool]:
    """Переключает признак «в избранном» и возвращает новое состояние."""
    user_id = _user_id(user)
    await _get_track_or_404(user_id, track_id)

    is_favourite = await favourites_repo.toggle(user_id, track_id)
    return {"is_favourite": bool(is_favourite)}


# ---------------------------------------------------------------------------
# Отдача файлов (авторизация по подписанному токену)
# ---------------------------------------------------------------------------


@router.get("/{track_id}/stream", summary="Аудиопоток трека")
async def stream_track(
    request: Request,
    track_id: int,
    token: str = Query(..., min_length=1, description="Подписанный stream-токен"),
) -> StreamingResponse:
    """Отдаёт аудиофайл трека потоком с поддержкой Range-запросов."""
    # Токен проверяется ДО обращения к боту: неавторизованный запрос не должен
    # узнавать о состоянии сервиса (иначе 503 приходил бы раньше 401).
    track = await _authorize_by_token(track_id, token)
    bot = get_bot(request)

    file_id = track.get("file_id")
    if not file_id:
        logger.error("У трека %s нет file_id — отдавать нечего", track_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Файл трека недоступен. Попробуйте загрузить его заново.",
        )

    return await _stream_response(bot, str(file_id), request.headers.get("range"))


@router.get("/{track_id}/cover", summary="Обложка трека")
async def track_cover(
    request: Request,
    track_id: int,
    token: str = Query(..., min_length=1, description="Подписанный stream-токен"),
) -> StreamingResponse:
    """Отдаёт обложку трека (миниатюру из Telegram); 404, если её нет."""
    track = await _authorize_by_token(track_id, token)
    bot = get_bot(request)

    thumb_file_id = track.get("thumb_file_id")
    if not thumb_file_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="У этого трека нет обложки."
        )

    return await _stream_response(bot, str(thumb_file_id), request.headers.get("range"))


__all__ = ["router"]
