"""Хранилище аудиофайлов на приватном Telegram-канале.

Все файлы пользователя физически лежат в приватном канале (``settings.storage_channel_id``),
а в БД хранятся только идентификаторы (``file_id``/``file_unique_id``) и координаты
сообщения в канале. Модуль отвечает за загрузку файлов в канал, их удаление,
получение временной ссылки на скачивание и потоковую отдачу файла клиенту.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import Audio, BufferedInputFile, Document, Message

from backend.config import settings
from backend.errors import FileTooLargeError, StorageError

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Размер чанка при потоковой отдаче файла.
CHUNK_SIZE = 64 * 1024

#: Время жизни закэшированного ``file_path`` (Telegram гарантирует не менее часа).
FILE_PATH_TTL = 50 * 60

#: Максимальный размер кэша путей файлов.
_CACHE_MAX_ITEMS = 1024

#: Лимит длины подписи в Telegram.
_CAPTION_LIMIT = 1024

#: Расширения, которые считаем аудио, если mime-тип не пришёл.
_AUDIO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".mp4a",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".alac",
    ".aif",
    ".aiff",
    ".ape",
)

#: Кэш путей файлов: file_id -> (file_path, expires_at по time.monotonic()).
_file_path_cache: dict[str, tuple[str, float]] = {}
_cache_lock = asyncio.Lock()

#: Общий HTTP-клиент для потоковой отдачи файлов (ленивая инициализация).
_http_client: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()


@dataclass(slots=True)
class StoredAudio:
    """Результат сохранения аудиофайла в канале-хранилище."""

    file_id: str
    file_unique_id: str | None
    message_id: int | None
    chat_id: int | None
    title: str
    artist: str | None
    album: str | None
    duration: int
    file_size: int
    mime_type: str | None
    file_name: str | None
    thumb_file_id: str | None


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _require_channel_id() -> int:
    """Возвращает ID канала-хранилища либо бросает понятную ошибку."""
    try:
        channel_id = int(getattr(settings, "storage_channel_id", 0) or 0)
    except (TypeError, ValueError):
        channel_id = 0
    if not channel_id:
        raise StorageError(
            "Не настроен канал-хранилище. Укажите STORAGE_CHANNEL_ID в файле .env "
            "(ID приватного канала, например -1001234567890), добавьте бота в канал "
            "и выдайте ему права администратора."
        )
    return channel_id


def _clean_text(value: Any, limit: int) -> str | None:
    """Приводит значение к непустой строке ограниченной длины."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _is_audio_document(document: Document | None) -> bool:
    """Проверяет, что документ похож на аудиофайл."""
    if document is None:
        return False
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    if mime_type.startswith("audio/") or mime_type in {"application/ogg", "video/mp4a-latm"}:
        return True
    file_name = (getattr(document, "file_name", None) or "").lower()
    return file_name.endswith(_AUDIO_EXTENSIONS)


def _extract_audio(message: Message) -> Audio | Document | None:
    """Достаёт аудио из сообщения (обычного или пересланного)."""
    audio = getattr(message, "audio", None)
    if audio is not None:
        return audio
    document = getattr(message, "document", None)
    if _is_audio_document(document):
        return document
    return None


def _thumb_file_id(media: Audio | Document | None) -> str | None:
    """Возвращает file_id обложки (в разных версиях aiogram поле называется по-разному)."""
    if media is None:
        return None
    thumbnail = getattr(media, "thumbnail", None) or getattr(media, "thumb", None)
    if thumbnail is None:
        return None
    return getattr(thumbnail, "file_id", None)


def _title_from_file_name(file_name: str | None) -> str | None:
    """Грубое имя трека из имени файла (metadata.py уточнит его позже)."""
    if not file_name:
        return None
    name = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        base, _, ext = name.rpartition(".")
        if base and len(ext) <= 5:
            name = base
    name = name.replace("_", " ").strip()
    return name or None


def _build_caption(title: str | None, performer: str | None) -> str | None:
    """Технические подписи в канале-хранилище: «Исполнитель — Название»."""
    parts = [part for part in (performer, title) if part]
    if not parts:
        return None
    return _clean_text(" — ".join(parts), _CAPTION_LIMIT)


def _describe_telegram_error(exc: BaseException) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return str(message).strip()


def _too_large_error() -> FileTooLargeError:
    limit_mb = max(1, _safe_int(getattr(settings, "max_download_size", 0), 20971520) // (1024 * 1024))
    return FileTooLargeError(
        f"Файл слишком большой: Telegram Bot API позволяет боту скачивать файлы "
        f"размером до {limit_mb} МБ. Чтобы работать с файлами большего размера, "
        "поднимите локальный Bot API server и укажите его адрес в TELEGRAM_API_BASE."
    )


def _channel_error(exc: BaseException) -> StorageError:
    """Переводит ошибку Telegram в StorageError с человеческим русским текстом."""
    text = _describe_telegram_error(exc)
    low = text.lower()

    if "too big" in low or "too large" in low or "entity too large" in low:
        return _too_large_error()
    if (
        isinstance(exc, TelegramForbiddenError)
        or "not enough rights" in low
        or "have no rights" in low
        or "chat_admin_required" in low
        or "chat_write_forbidden" in low
        or "bot was kicked" in low
        or "bot is not a member" in low
    ):
        return StorageError(
            "У бота нет прав администратора в канале-хранилище. Добавьте бота в канал "
            "и разрешите ему отправлять и удалять сообщения."
        )
    if (
        "chat not found" in low
        or "peer_id_invalid" in low
        or "chat_id_invalid" in low
        or "chat id is empty" in low
    ):
        return StorageError(
            "Канал-хранилище не найден. Проверьте STORAGE_CHANNEL_ID в .env: у приватного "
            "канала ID начинается с -100 (например, -1001234567890)."
        )
    if (
        "wrong file identifier" in low
        or "wrong remote file identifier" in low
        or ("file_id" in low and ("invalid" in low or "empty" in low))
    ):
        return StorageError(
            "Telegram не принял файл: идентификатор файла недействителен. "
            "Попробуйте прислать аудио ещё раз."
        )
    if "flood" in low:
        return StorageError(
            "Telegram временно ограничил частоту запросов. Подождите минуту и повторите попытку."
        )
    if isinstance(exc, TelegramNetworkError):
        return StorageError(
            "Не удалось связаться с Telegram. Проверьте подключение к интернету и повторите попытку."
        )
    return StorageError(f"Telegram отклонил запрос к каналу-хранилищу: {text}")


async def _call_with_retry(factory: Callable[[], Awaitable[T]], *, description: str) -> T:
    """Выполняет запрос к Telegram, один раз повторяя его после TelegramRetryAfter."""
    try:
        return await factory()
    except TelegramRetryAfter as exc:
        delay = float(_safe_int(getattr(exc, "retry_after", 1), 1) or 1)
        delay = min(max(delay, 1.0), 60.0)
        logger.warning(
            "Telegram ограничил частоту запросов (%s), ждём %.1f с и повторяем", description, delay
        )
        await asyncio.sleep(delay)
        try:
            return await factory()
        except TelegramRetryAfter as repeat_exc:
            raise StorageError(
                "Telegram временно ограничил частоту запросов. "
                "Подождите минуту и попробуйте ещё раз."
            ) from repeat_exc


async def _send_audio_to_channel(
    bot: Bot,
    audio: Any,
    *,
    title: str | None,
    performer: str | None,
    duration: int,
    caption: str | None,
) -> Message:
    """Отправляет аудио в канал-хранилище и возвращает сообщение из канала."""
    channel_id = _require_channel_id()
    try:
        return await _call_with_retry(
            lambda: bot.send_audio(
                chat_id=channel_id,
                audio=audio,
                title=title,
                performer=performer,
                duration=duration or None,
                caption=caption,
                parse_mode=None,
                disable_notification=True,
            ),
            description="отправка аудио в канал-хранилище",
        )
    except StorageError:
        raise
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
        logger.warning("Не удалось сохранить аудио в канале: %s", _describe_telegram_error(exc))
        raise _channel_error(exc) from exc
    except TelegramAPIError as exc:
        logger.exception("Ошибка Telegram при сохранении аудио в канале")
        raise _channel_error(exc) from exc


def _stored_from_message(
    sent: Message,
    *,
    fallback_title: str | None = None,
    fallback_performer: str | None = None,
    fallback_duration: int = 0,
    fallback_file_name: str | None = None,
    fallback_mime_type: str | None = None,
    fallback_file_size: int = 0,
    album: str | None = None,
) -> StoredAudio:
    """Собирает StoredAudio из сообщения, которое вернул Telegram при отправке в канал.

    Альбом Bot API не отдаёт ни в одном поле объекта ``Audio``, поэтому он берётся
    только из ``album`` — того, что знает вызывающий код (имя файла, ID3-теги,
    данные внешнего поиска).
    """
    media = _extract_audio(sent)
    if media is None:
        raise StorageError(
            "Telegram не вернул аудио из канала-хранилища. Проверьте права бота в канале "
            "и повторите попытку."
        )

    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise StorageError("Telegram не вернул идентификатор файла. Повторите попытку позже.")

    file_name = getattr(media, "file_name", None) or fallback_file_name
    title = (
        _clean_text(getattr(media, "title", None), 256)
        or _clean_text(fallback_title, 256)
        or _title_from_file_name(file_name)
        or "Без названия"
    )
    performer = _clean_text(getattr(media, "performer", None), 256) or _clean_text(
        fallback_performer, 256
    )
    chat = getattr(sent, "chat", None)

    return StoredAudio(
        file_id=str(file_id),
        file_unique_id=getattr(media, "file_unique_id", None),
        message_id=getattr(sent, "message_id", None),
        chat_id=getattr(chat, "id", None),
        title=title,
        artist=performer,
        album=_clean_text(album, 256),
        duration=_safe_int(getattr(media, "duration", None), fallback_duration),
        file_size=_safe_int(getattr(media, "file_size", None), fallback_file_size),
        mime_type=getattr(media, "mime_type", None) or fallback_mime_type,
        file_name=file_name,
        thumb_file_id=_thumb_file_id(media),
    )


# ---------------------------------------------------------------------------
# Сохранение файлов в канал
# ---------------------------------------------------------------------------


async def store_from_message(
    bot: Bot, message: Message, *, album: str | None = None
) -> StoredAudio:
    """Копирует аудио из сообщения пользователя в канал-хранилище.

    Работает и с обычными, и с пересланными сообщениями, и с документами,
    у которых mime-тип начинается на ``audio/``.

    ``album`` — необязательная подсказка: Telegram альбом не присылает,
    поэтому его подставляет вызывающий код (разбор имени файла или ID3-теги).
    """
    _require_channel_id()

    media = _extract_audio(message)
    if media is None:
        raise StorageError(
            "В сообщении нет аудиофайла. Пришлите или перешлите боту аудио "
            "(mp3, m4a, flac, ogg и т.п.)."
        )

    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise StorageError("Не удалось прочитать файл из сообщения. Попробуйте отправить его снова.")

    file_name = getattr(media, "file_name", None)
    title = (
        _clean_text(getattr(media, "title", None), 256)
        or _title_from_file_name(file_name)
        or "Без названия"
    )
    performer = _clean_text(getattr(media, "performer", None), 256)
    duration = _safe_int(getattr(media, "duration", None), 0)
    caption = _clean_text(getattr(message, "caption", None), _CAPTION_LIMIT) or _build_caption(
        title, performer
    )

    sent = await _send_audio_to_channel(
        bot,
        str(file_id),
        title=title,
        performer=performer,
        duration=duration,
        caption=caption,
    )
    stored = _stored_from_message(
        sent,
        fallback_title=title,
        fallback_performer=performer,
        fallback_duration=duration,
        fallback_file_name=file_name,
        fallback_mime_type=getattr(media, "mime_type", None),
        fallback_file_size=_safe_int(getattr(media, "file_size", None), 0),
        album=album,
    )
    logger.info(
        "Аудио сохранено в канале: message_id=%s file_unique_id=%s",
        stored.message_id,
        stored.file_unique_id,
    )
    return stored


async def store_from_bytes(
    bot: Bot,
    data: bytes,
    *,
    file_name: str,
    title: str,
    performer: str | None,
    duration: int = 0,
    mime_type: str | None = None,
    caption: str | None = None,
    album: str | None = None,
) -> StoredAudio:
    """Сохраняет аудио, переданное байтами (например, скачанное через Telethon).

    ``album`` Telegram не хранит — значение просто переносится в ``StoredAudio``,
    чтобы вызывающий код не терял уже известное название альбома.
    """
    _require_channel_id()

    if not data:
        raise StorageError("Пустой файл: сохранять нечего.")

    max_size = _safe_int(getattr(settings, "max_download_size", 0), 20971520)
    if max_size and len(data) > max_size:
        raise _too_large_error()

    safe_name = _clean_text(file_name, 200) or "audio.mp3"
    if "." not in safe_name.rsplit("/", 1)[-1]:
        safe_name = f"{safe_name}.mp3"

    clean_title = _clean_text(title, 256) or _title_from_file_name(safe_name) or "Без названия"
    clean_performer = _clean_text(performer, 256)
    clean_caption = _clean_text(caption, _CAPTION_LIMIT) or _build_caption(
        clean_title, clean_performer
    )

    input_file = BufferedInputFile(data, filename=safe_name)
    sent = await _send_audio_to_channel(
        bot,
        input_file,
        title=clean_title,
        performer=clean_performer,
        duration=_safe_int(duration, 0),
        caption=clean_caption,
    )
    return _stored_from_message(
        sent,
        fallback_title=clean_title,
        fallback_performer=clean_performer,
        fallback_duration=_safe_int(duration, 0),
        fallback_file_name=safe_name,
        fallback_mime_type=mime_type,
        fallback_file_size=len(data),
        album=album,
    )


async def store_from_file_id(
    bot: Bot,
    file_id: str,
    *,
    title: str | None = None,
    performer: str | None = None,
    duration: int = 0,
    caption: str | None = None,
    album: str | None = None,
) -> StoredAudio:
    """Пересохраняет в канал файл, уже известный Telegram по ``file_id``.

    ``album`` — необязательная подсказка вызывающего кода (Telegram альбом не отдаёт).
    """
    _require_channel_id()

    if not file_id:
        raise StorageError("Не указан идентификатор файла для сохранения.")

    clean_title = _clean_text(title, 256) or "Без названия"
    clean_performer = _clean_text(performer, 256)
    clean_caption = _clean_text(caption, _CAPTION_LIMIT) or _build_caption(
        clean_title, clean_performer
    )

    sent = await _send_audio_to_channel(
        bot,
        str(file_id),
        title=clean_title,
        performer=clean_performer,
        duration=_safe_int(duration, 0),
        caption=clean_caption,
    )
    return _stored_from_message(
        sent,
        fallback_title=clean_title,
        fallback_performer=clean_performer,
        fallback_duration=_safe_int(duration, 0),
        album=album,
    )


async def delete_from_channel(bot: Bot, message_id: int | None) -> bool:
    """Удаляет сообщение с треком из канала-хранилища. Ошибки только логируются."""
    if not message_id:
        return False
    try:
        channel_id = _require_channel_id()
    except StorageError as exc:
        logger.warning("Удаление из канала пропущено: %s", exc)
        return False

    try:
        await _call_with_retry(
            lambda: bot.delete_message(chat_id=channel_id, message_id=int(message_id)),
            description="удаление сообщения из канала-хранилища",
        )
    except StorageError as exc:
        logger.warning("Не удалось удалить сообщение %s из канала: %s", message_id, exc)
        return False
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось удалить сообщение %s из канала: %s",
            message_id,
            _describe_telegram_error(exc),
        )
        return False
    except Exception:  # noqa: BLE001 — удаление не должно ломать основной сценарий
        logger.exception("Неожиданная ошибка при удалении сообщения %s из канала", message_id)
        return False

    logger.info("Сообщение %s удалено из канала-хранилища", message_id)
    return True


# ---------------------------------------------------------------------------
# Доступ к содержимому файлов
# ---------------------------------------------------------------------------


def _cache_get(file_id: str) -> str | None:
    cached = _file_path_cache.get(file_id)
    if not cached:
        return None
    file_path, expires_at = cached
    if expires_at <= time.monotonic():
        _file_path_cache.pop(file_id, None)
        return None
    return file_path


async def _cache_put(file_id: str, file_path: str) -> None:
    async with _cache_lock:
        _file_path_cache[file_id] = (file_path, time.monotonic() + FILE_PATH_TTL)
        if len(_file_path_cache) > _CACHE_MAX_ITEMS:
            now = time.monotonic()
            expired = [key for key, (_, exp) in _file_path_cache.items() if exp <= now]
            for key in expired:
                _file_path_cache.pop(key, None)
            while len(_file_path_cache) > _CACHE_MAX_ITEMS:
                _file_path_cache.pop(next(iter(_file_path_cache)))


async def _cache_drop(file_id: str) -> None:
    async with _cache_lock:
        _file_path_cache.pop(file_id, None)


async def resolve_file_path(bot: Bot, file_id: str) -> str:
    """Возвращает относительный путь файла на серверах Telegram (с кэшем на 50 минут)."""
    if not file_id:
        raise StorageError("Не указан идентификатор файла.")

    cached = _cache_get(file_id)
    if cached:
        return cached

    try:
        file = await _call_with_retry(
            lambda: bot.get_file(file_id), description="получение пути файла"
        )
    except StorageError:
        raise
    except TelegramBadRequest as exc:
        text = _describe_telegram_error(exc)
        low = text.lower()
        if "too big" in low or "too large" in low:
            logger.warning("Файл %s превышает лимит Bot API: %s", file_id, text)
            raise _too_large_error() from exc
        logger.warning("Не удалось получить путь файла %s: %s", file_id, text)
        raise _channel_error(exc) from exc
    except (TelegramForbiddenError, TelegramNetworkError) as exc:
        raise _channel_error(exc) from exc
    except TelegramAPIError as exc:
        logger.exception("Ошибка Telegram при получении пути файла %s", file_id)
        raise _channel_error(exc) from exc

    max_size = _safe_int(getattr(settings, "max_download_size", 0), 20971520)
    file_size = _safe_int(getattr(file, "file_size", None), 0)
    if max_size and file_size > max_size:
        raise _too_large_error()

    file_path = getattr(file, "file_path", None)
    if not file_path:
        raise StorageError(
            "Telegram не вернул путь файла. Попробуйте повторить запрос немного позже."
        )

    await _cache_put(file_id, str(file_path))
    return str(file_path)


def file_download_url(file_path: str) -> str:
    """Прямая ссылка на файл в Telegram.

    Содержит токен бота, поэтому НИКОГДА не отдаётся клиенту и не логируется.
    """
    base = str(getattr(settings, "telegram_api_base", "https://api.telegram.org") or "").rstrip("/")
    if not base:
        base = "https://api.telegram.org"
    token = str(getattr(settings, "bot_token", "") or "")
    return f"{base}/file/bot{token}/{str(file_path).lstrip('/')}"


async def _get_http_client() -> httpx.AsyncClient:
    """Ленивая инициализация общего HTTP-клиента."""
    global _http_client
    client = _http_client
    if client is not None and not client.is_closed:
        return client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
                follow_redirects=True,
            )
        return _http_client


def _guess_content_type(file_path: str, response_type: str | None) -> str:
    """Выбирает наиболее осмысленный Content-Type для отдачи клиенту."""
    candidate = (response_type or "").split(";")[0].strip().lower()
    if candidate and candidate not in {"application/octet-stream", "text/plain", "binary/octet-stream"}:
        return candidate
    guessed, _ = mimetypes.guess_type(file_path)
    if guessed:
        return guessed
    if file_path.lower().endswith(_AUDIO_EXTENSIONS):
        return "audio/mpeg"
    return "application/octet-stream"


def _download_http_error(status: int, file_id: str) -> StorageError:
    """Ошибка HTTP при скачивании файла — в понятный русский текст."""
    logger.warning("Telegram вернул %s при скачивании файла %s", status, file_id)
    if status in (401, 403):
        return StorageError(
            "Telegram отказал в доступе к файлу. Проверьте токен бота "
            "и права бота в канале-хранилище."
        )
    if status == 404:
        return StorageError(
            "Файл больше недоступен в Telegram. Попробуйте загрузить его заново."
        )
    if status == 413:
        return _too_large_error()
    return StorageError(f"Telegram вернул ошибку при скачивании файла (код {status}).")


async def download_bytes(bot: Bot, file_id: str, max_size: int | None = None) -> bytes:
    """Скачивает файл из Telegram целиком в память.

    ``max_size`` — предельный размер в байтах; если не задан, берётся
    ``settings.max_download_size``. Превышение лимита (по заголовку
    ``Content-Length`` или по факту принятых байт) — ``FileTooLargeError``,
    любая проблема со стороны Telegram — ``StorageError``.

    Скачивание идёт чанками через общий ``httpx.AsyncClient``, поэтому
    event loop не блокируется.
    """
    if not file_id:
        raise StorageError("Не указан идентификатор файла.")

    if max_size is None:
        limit = _safe_int(getattr(settings, "max_download_size", 0), 20971520)
    else:
        limit = _safe_int(max_size, 0)

    file_path = await resolve_file_path(bot, file_id)
    url = file_download_url(file_path)
    client = await _get_http_client()

    try:
        request = client.build_request("GET", url)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Не удалось скачать файл %s: %s", file_id, exc)
        raise StorageError(
            "Не удалось получить файл из Telegram. Проверьте соединение и повторите попытку."
        ) from exc

    buffer = bytearray()
    try:
        if response.status_code >= 400:
            status = response.status_code
            await _cache_drop(file_id)
            raise _download_http_error(status, file_id)

        declared = _safe_int(response.headers.get("content-length"), 0)
        if limit and declared > limit:
            raise _too_large_error()

        try:
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                if not chunk:
                    continue
                buffer.extend(chunk)
                if limit and len(buffer) > limit:
                    raise _too_large_error()
        except httpx.HTTPError as exc:
            logger.warning("Скачивание файла %s прервалось: %s", file_id, exc)
            raise StorageError(
                "Скачивание файла прервалось. Повторите попытку позже."
            ) from exc
    finally:
        await response.aclose()

    if not buffer:
        raise StorageError("Telegram вернул пустой файл. Повторите попытку позже.")
    return bytes(buffer)


async def stream_file(
    bot: Bot, file_id: str, range_header: str | None = None
) -> tuple[AsyncIterator[bytes], dict[str, str], int]:
    """Готовит потоковую отдачу файла из Telegram с поддержкой Range-запросов.

    Возвращает кортеж ``(генератор чанков, заголовки, код ответа)``.
    Генератор обязателен к полному прочтению или закрытию — соединение
    закрывается в ``finally``.
    """
    file_path = await resolve_file_path(bot, file_id)
    url = file_download_url(file_path)
    client = await _get_http_client()

    request_headers: dict[str, str] = {}
    if range_header:
        request_headers["Range"] = range_header

    try:
        request = client.build_request("GET", url, headers=request_headers)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Не удалось начать загрузку файла %s: %s", file_id, exc)
        raise StorageError(
            "Не удалось получить файл из Telegram. Проверьте соединение и повторите попытку."
        ) from exc

    if response.status_code >= 400:
        status = response.status_code
        await response.aclose()
        await _cache_drop(file_id)
        logger.warning("Telegram вернул %s при скачивании файла %s", status, file_id)
        if status == 416:
            raise StorageError("Запрошен некорректный диапазон байтов файла.")
        if status in (401, 403):
            raise StorageError(
                "Telegram отказал в доступе к файлу. Проверьте токен бота и права в канале-хранилище."
            )
        if status == 404:
            raise StorageError(
                "Файл больше недоступен в Telegram. Попробуйте открыть трек ещё раз "
                "или загрузите его заново."
            )
        raise StorageError(f"Telegram вернул ошибку при скачивании файла (код {status}).")

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Type": _guess_content_type(file_path, response.headers.get("content-type")),
        "Cache-Control": "private, max-age=600",
    }
    content_length = response.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length
    content_range = response.headers.get("content-range")
    if content_range:
        headers["Content-Range"] = content_range

    status_code = 206 if response.status_code == 206 else 200

    async def iterator() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                if chunk:
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning("Передача файла %s прервана: %s", file_id, exc)
            raise StorageError("Передача файла прервалась. Попробуйте включить трек ещё раз.") from exc
        finally:
            await response.aclose()

    return iterator(), headers, status_code


# ---------------------------------------------------------------------------
# Отправка треков пользователю
# ---------------------------------------------------------------------------


async def send_track_to_user(
    bot: Bot,
    chat_id: int,
    track: dict,
    *,
    caption: str | None = None,
    reply_markup: Any = None,
) -> Message:
    """Отправляет пользователю трек из хранилища по сохранённому ``file_id``."""
    file_id = track.get("file_id")
    if not file_id:
        raise StorageError(
            "У этого трека нет файла в хранилище. Загрузите аудио заново, чтобы прослушать его."
        )

    title = _clean_text(track.get("title"), 256) or "Без названия"
    performer = _clean_text(track.get("artist"), 256)
    duration = _safe_int(track.get("duration"), 0)

    try:
        return await _call_with_retry(
            lambda: bot.send_audio(
                chat_id=chat_id,
                audio=str(file_id),
                title=title,
                performer=performer,
                duration=duration or None,
                caption=_clean_text(caption, _CAPTION_LIMIT),
                reply_markup=reply_markup,
            ),
            description="отправка трека пользователю",
        )
    except StorageError:
        raise
    except TelegramBadRequest as exc:
        text = _describe_telegram_error(exc)
        low = text.lower()
        logger.warning("Не удалось отправить трек %s: %s", track.get("id"), text)
        if "wrong file identifier" in low or "wrong remote file identifier" in low:
            raise StorageError(
                "Не удалось отправить трек: файл больше недоступен в Telegram. "
                "Загрузите его в хранилище заново."
            ) from exc
        if "too big" in low or "too large" in low:
            raise _too_large_error() from exc
        raise StorageError(f"Telegram не смог отправить трек: {text}") from exc
    except TelegramForbiddenError as exc:
        logger.warning("Пользователь %s недоступен для отправки трека: %s", chat_id, exc)
        raise StorageError(
            "Не удалось отправить трек: бот заблокирован или чат недоступен."
        ) from exc
    except TelegramNetworkError as exc:
        raise StorageError(
            "Не удалось связаться с Telegram при отправке трека. Повторите попытку."
        ) from exc
    except TelegramAPIError as exc:
        logger.exception("Ошибка Telegram при отправке трека %s", track.get("id"))
        raise StorageError(
            f"Не удалось отправить трек: {_describe_telegram_error(exc)}"
        ) from exc


async def close_http_client() -> None:
    """Закрывает общий HTTP-клиент (вызывается при остановке приложения)."""
    global _http_client
    async with _http_client_lock:
        client = _http_client
        _http_client = None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — остановка не должна падать
            logger.exception("Ошибка при закрытии HTTP-клиента хранилища")
    async with _cache_lock:
        _file_path_cache.clear()
