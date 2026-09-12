"""API поиска: нечёткий поиск по библиотеке и поиск аудио в Telegram.

Поиск по библиотеке прощает опечатки и неверную раскладку клавиатуры
(`backend.services.search`). Поиск по Telegram работает только если настроен
Telethon; иначе отдаётся 503 с подсказкой переслать аудио боту вручную.

V2 (контракт `docs/ARCHITECTURE-V2.md`, раздел 4): у `GET /search` появились
параметры `artist_ids` (CSV вида «1,2,3» — пересечение по нескольким
исполнителям) и `section` (`music` | `other` — раздел, в котором ищутся папки).
Папки попадают в результат всегда, в том числе при активном фильтре по
исполнителям (ТЗ п. 17).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from backend.api import schemas
from backend.api.deps import CurrentUser, get_bot
from backend.errors import (
    FileTooLargeError,
    StorageError,
    TelegramSearchError,
    TelegramSearchUnavailable,
    ValidationError,
)
from backend.services import search as search_service
from backend.services import telegram_search as telegram_search_service
from backend.services.metadata import format_duration
from backend.services.telegram_search import telegram_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# Русские сообщения об ошибках (видны пользователю).
TOKEN_EXPIRED = (
    "Результат поиска устарел — выполните поиск заново и повторите импорт"
)
LINK_NOT_FOUND = (
    "По этой ссылке не найден аудиофайл. Проверьте ссылку или перешлите аудио боту"
)
IMPORT_FAILED = "Не удалось импортировать трек из Telegram. Попробуйте позже"

MAX_QUERY_LENGTH = 200

#: Сколько исполнителей допускается в фильтре-пересечении `artist_ids`.
#: Больше — почти наверняка ошибка клиента: пересечение по десяткам
#: исполнителей всегда пусто, а запрос стоит дорого.
MAX_ARTIST_FILTER = 20

#: Сообщение об ошибке разбора `artist_ids`.
BAD_ARTIST_IDS = (
    "Список исполнителей должен быть числами через запятую, например «1,2,3»"
)

#: Заголовок ответа импорта: «1» — трек уже был в библиотеке, файл не скачивался.
DUPLICATE_HEADER = "X-MusicBox-Duplicate"

#: Описание заголовка-признака дубликата для OpenAPI (тело ответа не меняется).
IMPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Трек в библиотеке. Заголовок X-MusicBox-Duplicate: «1» — трек уже был "
            "добавлен раньше и повторно не скачивался, «0» — импортирован сейчас."
        ),
        "headers": {
            DUPLICATE_HEADER: {
                "description": "«1» — дубликат уже имевшегося трека, «0» — новый импорт",
                "schema": {"type": "string", "enum": ["0", "1"]},
            }
        },
    }
}


def _user_id(user: dict) -> int:
    """Идентификатор пользователя из зависимости get_current_user."""
    raw = user.get("user_id", user.get("id"))
    if raw is None:
        logger.error("В данных пользователя нет идентификатора: %r", user)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось определить пользователя",
        )
    return int(raw)


def _clean_query(value: str | None) -> str:
    """Нормализует поисковый запрос: схлопывает пробелы и режет длину."""
    return " ".join(str(value or "").split())[:MAX_QUERY_LENGTH]


def _parse_chats(value: str | None) -> list[str]:
    """Разбирает CSV-параметр `chats` в список юзернеймов каналов."""
    if not value:
        return []
    result: list[str] = []
    for raw in str(value).replace(";", ",").split(","):
        chat = raw.strip()
        if chat and chat not in result:
            result.append(chat)
    return result


def _parse_artist_ids(value: str | None) -> list[int]:
    """Разбирает CSV-параметр `artist_ids` («1,2,3») в список идентификаторов.

    Дубликаты убираются с сохранением порядка. Любое нечисловое или
    неположительное значение — ошибка клиента (400), а не молчаливый пропуск:
    иначе «1,x» тихо превратилось бы в фильтр по одному исполнителю.
    """
    if value is None:
        return []

    result: list[int] = []
    seen: set[int] = set()
    for raw in str(value).replace(";", ",").split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            number = int(chunk)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_ARTIST_IDS
            ) from exc
        if number <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_ARTIST_IDS
            )
        if number in seen:
            continue
        seen.add(number)
        result.append(number)

    if len(result) > MAX_ARTIST_FILTER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Слишком много исполнителей в фильтре "
                f"(максимум {MAX_ARTIST_FILTER})"
            ),
        )
    return result


def _check_section(value: str | None) -> str:
    """Проверяет раздел библиотеки (`music` | `other`); пусто — раздел по умолчанию."""
    section = (value or "").strip().casefold()
    if not section:
        return search_service.DEFAULT_SECTION
    if section not in search_service.SECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный раздел. Допустимые значения: "
            + ", ".join(search_service.SECTIONS),
        )
    return section


def _remote_out(remote: telegram_search_service.RemoteAudio) -> schemas.RemoteAudioOut:
    """Преобразует найденное в Telegram аудио в RemoteAudioOut."""
    duration = int(remote.duration or 0)
    return schemas.RemoteAudioOut(
        token=remote.token,
        title=remote.title or "Без названия",
        performer=remote.performer,
        duration=duration,
        duration_label=format_duration(duration),
        file_size=int(remote.file_size or 0),
        chat_title=remote.chat_title or "",
        link=remote.link or "",
    )


def _telegram_failure(exc: Exception) -> HTTPException:
    """Единое преобразование ошибок Telegram-поиска в HTTP-ответ."""
    if isinstance(exc, TelegramSearchUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    if isinstance(exc, FileTooLargeError):
        return HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        )
    if isinstance(exc, (TelegramSearchError, StorageError)):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    logger.exception("Непредвиденная ошибка поиска по Telegram")
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail=IMPORT_FAILED
    )


async def _import_remote(
    bot: Bot,
    user_id: int,
    remote: telegram_search_service.RemoteAudio,
    response: Response,
) -> schemas.TrackOut:
    """Импортирует найденное аудио в хранилище и возвращает трек.

    Тело ответа всегда `TrackOut`. Если такой трек уже был в библиотеке, сервис
    возвращает его без повторной заливки в канал — об этом говорит заголовок
    ``X-MusicBox-Duplicate: 1``.
    """
    try:
        track = await telegram_search_service.import_remote(bot, user_id, remote)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except (
        TelegramSearchUnavailable,
        TelegramSearchError,
        FileTooLargeError,
        StorageError,
    ) as exc:
        raise _telegram_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001 — наружу отдаём понятный текст
        raise _telegram_failure(exc) from exc

    duplicate = track.get("duplicate") is True
    response.headers[DUPLICATE_HEADER] = "1" if duplicate else "0"
    if duplicate:
        logger.info(
            "Трек %s уже был в библиотеке пользователя %s — импорт из Telegram пропущен (%s)",
            track.get("id"),
            user_id,
            remote.link or remote.title,
        )
    else:
        logger.info(
            "Пользователь %s импортировал трек %s из Telegram (%s)",
            user_id,
            track.get("id"),
            remote.link or remote.title,
        )
    return schemas.track_to_out(track, user_id)


@router.get("", response_model=schemas.SearchResultOut, summary="Общий поиск")
@router.get("/", response_model=schemas.SearchResultOut, include_in_schema=False)
async def search_all(
    user: CurrentUser,
    q: Annotated[str, Query(description="Поисковый запрос")] = "",
    limit: Annotated[int, Query(ge=1, le=100, description="Сколько результатов")] = 20,
    artist_ids: Annotated[
        str | None,
        Query(
            description=(
                "Фильтр по нескольким исполнителям через запятую, например «1,2,3». "
                "Отбираются треки, у которых есть ВСЕ перечисленные исполнители"
            )
        ),
    ] = None,
    section: Annotated[
        str | None,
        Query(description="Раздел библиотеки для поиска папок: music или other"),
    ] = None,
) -> schemas.SearchResultOut:
    """Нечёткий поиск по трекам, альбомам, исполнителям и папкам.

    С `artist_ids` сначала берётся пересечение треков по этим исполнителям, и
    только внутри него работает нечёткий поиск; пустой `q` вместе с фильтром
    отдаёт все треки выбранных исполнителей. Папки ищутся всегда — их раздел
    задаётся параметром `section`.
    """
    user_id = _user_id(user)
    query = _clean_query(q)
    selected_artists = _parse_artist_ids(artist_ids)
    active_section = _check_section(section)

    result = await search_service.search_all(
        user_id,
        query,
        limit=limit,
        artist_ids=selected_artists or None,
        section=active_section,
    )

    logger.debug(
        "Поиск «%s» пользователя %s (раздел %s, исполнители %s): "
        "треков %d, альбомов %d, исполнителей %d, папок %d",
        query,
        user_id,
        active_section,
        selected_artists or "—",
        len(result.get("tracks") or []),
        len(result.get("albums") or []),
        len(result.get("artists") or []),
        len(result.get("folders") or []),
    )
    return schemas.search_to_out(result, user_id)


@router.get(
    "/tracks", response_model=list[schemas.TrackOut], summary="Поиск по трекам"
)
async def search_tracks(
    user: CurrentUser,
    q: Annotated[str, Query(description="Поисковый запрос")] = "",
    limit: Annotated[int, Query(ge=1, le=100, description="Сколько результатов")] = 30,
) -> list[schemas.TrackOut]:
    """Нечёткий поиск только по трекам."""
    user_id = _user_id(user)
    tracks = await search_service.search_tracks(user_id, _clean_query(q), limit=limit)
    return schemas.tracks_to_out(tracks, user_id)


@router.get(
    "/albums", response_model=list[schemas.AlbumOut], summary="Поиск по альбомам"
)
async def search_albums(
    user: CurrentUser,
    q: Annotated[str, Query(description="Поисковый запрос")] = "",
    limit: Annotated[int, Query(ge=1, le=100, description="Сколько результатов")] = 30,
) -> list[schemas.AlbumOut]:
    """Нечёткий поиск по альбомам («группам»)."""
    user_id = _user_id(user)
    albums = await search_service.search_albums(user_id, _clean_query(q), limit=limit)
    return schemas.albums_to_out(albums)


@router.get(
    "/telegram",
    response_model=list[schemas.RemoteAudioOut],
    summary="Поиск аудио в Telegram",
)
async def search_telegram(
    user: CurrentUser,
    q: Annotated[str, Query(description="Поисковый запрос")] = "",
    limit: Annotated[int, Query(ge=1, le=50, description="Сколько результатов")] = 20,
    chats: Annotated[
        str | None,
        Query(description="Каналы для поиска через запятую (по умолчанию — из настроек)"),
    ] = None,
) -> list[schemas.RemoteAudioOut]:
    """Ищет аудио в публичных каналах Telegram. 503, если поиск не настроен."""
    user_id = _user_id(user)
    query = _clean_query(q)
    if not query:
        return []

    try:
        found = await telegram_search.search(
            query, chats=_parse_chats(chats) or None, limit=limit
        )
    except (TelegramSearchUnavailable, TelegramSearchError) as exc:
        raise _telegram_failure(exc) from exc

    # Результаты уже попали в LRU-кэш сервиса — их токены примет /telegram/import.
    logger.info(
        "Поиск по Telegram «%s» для пользователя %s: найдено %s",
        query,
        user_id,
        len(found),
    )
    return [_remote_out(item) for item in found]


@router.post(
    "/telegram/import",
    response_model=schemas.TrackOut,
    summary="Импортировать найденный трек",
    responses=IMPORT_RESPONSES,
)
async def import_remote(
    payload: schemas.ImportRemoteIn,
    user: CurrentUser,
    bot: Annotated[Bot, Depends(get_bot)],
    response: Response,
) -> schemas.TrackOut:
    """Импортирует трек по токену результата поиска (токен живёт в кэше).

    Если трек уже есть в библиотеке, он возвращается как есть, а в ответ
    добавляется заголовок ``X-MusicBox-Duplicate: 1``.
    """
    user_id = _user_id(user)
    remote = telegram_search.get_cached(payload.token)
    if remote is None:
        logger.info(
            "Токен %r отсутствует в кэше поиска (пользователь %s)",
            payload.token,
            user_id,
        )
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=TOKEN_EXPIRED)
    return await _import_remote(bot, user_id, remote, response)


@router.post(
    "/telegram/link",
    response_model=schemas.TrackOut,
    summary="Импортировать трек по ссылке",
    responses=IMPORT_RESPONSES,
)
async def import_link(
    payload: schemas.ImportLinkIn,
    user: CurrentUser,
    bot: Annotated[Bot, Depends(get_bot)],
    response: Response,
) -> schemas.TrackOut:
    """Импортирует трек по ссылке вида https://t.me/<канал>/<id>.

    Как и импорт по токену, помечает уже имевшийся трек заголовком
    ``X-MusicBox-Duplicate: 1`` (тело ответа — обычный `TrackOut`).
    """
    user_id = _user_id(user)
    url = str(payload.url or "").strip()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Не указана ссылка"
        )

    try:
        remote = await telegram_search.resolve_link(url)
    except (TelegramSearchUnavailable, TelegramSearchError) as exc:
        raise _telegram_failure(exc) from exc

    if remote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=LINK_NOT_FOUND
        )
    return await _import_remote(bot, user_id, remote, response)
