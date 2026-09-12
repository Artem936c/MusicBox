"""Поиск аудио в Telegram через Telethon и импорт найденных треков в хранилище.

Функция опциональная: если Telethon не установлен, отключён флагом
`telegram_search_enabled` или не заданы `tg_api_id`/`tg_api_hash`/`tg_session_string`,
сервис остаётся недоступным (`available = False`), а его методы бросают
`TelegramSearchUnavailable` с подсказкой переслать аудио боту вручную.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from backend.config import settings
from backend.db.repositories import tracks as tracks_repo
from backend.errors import (
    FileTooLargeError,
    TelegramSearchError,
    TelegramSearchUnavailable,
)
from backend.services import autosort, metadata, storage

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from aiogram import Bot

logger = logging.getLogger(__name__)


class _MissingTelethonError(Exception):
    """Заглушка для исключений Telethon, когда библиотека не установлена."""


try:  # мягкий импорт: Telethon — необязательная зависимость
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.sessions import StringSession
    from telethon.tl.types import (
        DocumentAttributeAudio,
        DocumentAttributeFilename,
        InputMessagesFilterMusic,
        PeerChannel,
    )

    TELETHON_INSTALLED = True
except ImportError:  # pragma: no cover - зависит от окружения
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]
    FloodWaitError = _MissingTelethonError  # type: ignore[assignment,misc]
    DocumentAttributeAudio = None  # type: ignore[assignment]
    DocumentAttributeFilename = None  # type: ignore[assignment]
    InputMessagesFilterMusic = None  # type: ignore[assignment]
    PeerChannel = None  # type: ignore[assignment]

    TELETHON_INSTALLED = False


# --- Пользовательские сообщения (RU) -------------------------------------------------

UNAVAILABLE_MESSAGE = (
    "Поиск по Telegram не настроен. Перешлите аудио боту — "
    "я сохраню его в хранилище и разложу по папкам"
)
NO_CHATS_MESSAGE = (
    "Не указаны каналы для поиска. Добавьте их в TG_SEARCH_CHATS или "
    "перешлите аудио боту — я сохраню его в хранилище и разложу по папкам"
)
DOWNLOAD_FAILED_MESSAGE = "Не удалось скачать аудио из Telegram. Попробуйте позже"
TIMEOUT_MESSAGE = "Telegram не ответил вовремя. Попробуйте ещё раз чуть позже"
LINK_FAILED_MESSAGE = (
    "Не удалось открыть ссылку. Проверьте, что канал публичный и сообщение существует"
)

# --- Настройки сетевых операций ------------------------------------------------------

CONNECT_TIMEOUT = 30.0
SEARCH_TIMEOUT = 45.0
RESOLVE_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 600.0
RETRY_COOLDOWN = 60.0
CACHE_LIMIT = 500
MAX_SEARCH_LIMIT = 100

_LINK_PRIVATE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/c/(\d+)/(?:\d+/)?(\d+)", re.IGNORECASE
)
_LINK_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?!c/|joinchat/|\+)(?:s/)?([A-Za-z0-9_]{3,32})/(?:\d+/)?(\d+)",
    re.IGNORECASE,
)
_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def _new_token() -> str:
    """Короткий идентификатор результата поиска (живёт в inline-кнопках)."""
    return uuid.uuid4().hex[:16]


@dataclass(slots=True)
class RemoteAudio:
    """Найденный в Telegram аудиофайл (ещё не импортированный в хранилище)."""

    token: str = field(default_factory=_new_token)
    chat_id: int = 0
    chat_title: str = ""
    chat_username: str | None = None
    message_id: int = 0
    title: str = "Без названия"
    performer: str | None = None
    duration: int = 0
    file_size: int = 0
    mime_type: str | None = None
    file_name: str | None = None
    link: str = ""


# --- Вспомогательные функции ---------------------------------------------------------


def _mb(value: int) -> str:
    """Размер в мегабайтах для пользовательских сообщений."""
    return f"{max(int(value), 0) / (1024 * 1024):.1f}".replace(".", ",")


def _flood_message(exc: Exception) -> str:
    """Русский текст об ограничении Telegram (FloodWait)."""
    seconds = int(getattr(exc, "seconds", 0) or 0)
    if seconds > 0:
        return f"Telegram временно ограничил запросы. Повторите примерно через {seconds} с"
    return "Telegram временно ограничил запросы. Повторите чуть позже"


def _normalize_chat(raw: str | int) -> str | int | None:
    """Приводит запись канала («@name», «t.me/name», «-100123») к виду для Telethon."""
    if isinstance(raw, int):
        return raw
    value = (raw or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if "t.me/" in lowered:
        value = value[lowered.index("t.me/") + len("t.me/") :]
        value = value.split("?", 1)[0].strip("/")
        if value.lower().startswith("s/"):  # ссылка вида t.me/s/<name>
            value = value[2:]
        value = value.split("/", 1)[0]
    value = value.lstrip("@").strip()
    if not value:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _internal_channel_id(chat_id: int) -> str:
    """Внутренний id канала для ссылок вида https://t.me/c/<id>/<msg>."""
    text = str(int(chat_id))
    if text.startswith("-100"):
        return text[4:]
    return text.lstrip("-")


def _build_link(chat_username: str | None, chat_id: int, message_id: int) -> str:
    """Публичная (или приватная) ссылка на сообщение."""
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    internal = _internal_channel_id(chat_id) if chat_id else ""
    if internal:
        return f"https://t.me/c/{internal}/{message_id}"
    return ""


def _parse_link(url: str) -> tuple[Any, int] | None:
    """Разбирает ссылку на сообщение. Возвращает (peer, message_id) либо None."""
    text = (url or "").strip()
    if not text:
        return None
    private = _LINK_PRIVATE_RE.search(text)
    if private:
        internal_id = int(private.group(1))
        message_id = int(private.group(2))
        if PeerChannel is None:  # pragma: no cover - без Telethon сюда не доходим
            return None
        return PeerChannel(internal_id), message_id
    public = _LINK_PUBLIC_RE.search(text)
    if public:
        return public.group(1), int(public.group(2))
    return None


def _audio_attributes(document: Any) -> tuple[Any | None, str | None, bool]:
    """Возвращает (DocumentAttributeAudio | None, имя файла | None, это голосовое)."""
    audio_attr: Any | None = None
    file_name: str | None = None
    is_voice = False
    for attr in getattr(document, "attributes", None) or []:
        if DocumentAttributeAudio is not None and isinstance(attr, DocumentAttributeAudio):
            if getattr(attr, "voice", False):
                is_voice = True
                continue
            audio_attr = attr
        elif DocumentAttributeFilename is not None and isinstance(
            attr, DocumentAttributeFilename
        ):
            file_name = getattr(attr, "file_name", None)
    return audio_attr, file_name, is_voice


def _remote_from_message(message: Any, *, fallback_link: str | None = None) -> RemoteAudio | None:
    """Собирает RemoteAudio из сообщения Telethon. None — если это не аудио."""
    document = getattr(message, "document", None)
    if document is None:
        return None
    mime_type = getattr(document, "mime_type", None)
    audio_attr, file_name, is_voice = _audio_attributes(document)
    if audio_attr is None and (
        is_voice or not str(mime_type or "").lower().startswith("audio/")
    ):
        return None

    title = str(getattr(audio_attr, "title", None) or "").strip()
    performer = str(getattr(audio_attr, "performer", None) or "").strip() or None
    duration = int(getattr(audio_attr, "duration", 0) or 0)

    if not title or not performer:
        guessed_artist, guessed_title = metadata.guess_from_filename(file_name)
        title = title or (guessed_title or "")
        performer = performer or guessed_artist
    if not title:
        title = metadata.clean_title(file_name) or "Без названия"
    title = title.strip() or "Без названия"

    chat = getattr(message, "chat", None)
    chat_username = getattr(chat, "username", None)
    chat_title = str(
        getattr(chat, "title", None) or chat_username or getattr(chat, "first_name", None) or "Telegram"
    )
    try:
        chat_id = int(getattr(message, "chat_id", 0) or 0)
    except (TypeError, ValueError):
        chat_id = 0
    try:
        message_id = int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError):
        message_id = 0

    link = _build_link(chat_username, chat_id, message_id) or (fallback_link or "")

    return RemoteAudio(
        chat_id=chat_id,
        chat_title=chat_title,
        chat_username=chat_username,
        message_id=message_id,
        title=title,
        performer=performer,
        duration=max(duration, 0),
        file_size=int(getattr(document, "size", 0) or 0),
        mime_type=mime_type,
        file_name=file_name,
        link=link,
    )


def _safe_file_name(remote: RemoteAudio) -> str:
    """Имя файла для загрузки в канал-хранилище."""
    if remote.file_name:
        return remote.file_name
    parts = [part for part in (remote.performer, remote.title) if part]
    base = " - ".join(parts) if parts else "audio"
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", base).strip()
    base = re.sub(r"\s+", " ", base)[:100] or "audio"
    extension = _MIME_EXTENSIONS.get(str(remote.mime_type or "").lower(), ".mp3")
    return f"{base}{extension}"


class TelegramSearchService:
    """Обёртка над Telethon: поиск аудио по каналам, разбор ссылок, скачивание."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._start_failed = False
        self._retry_after = 0.0
        self._cache: OrderedDict[str, RemoteAudio] = OrderedDict()
        self._messages: OrderedDict[str, Any] = OrderedDict()

    # --- Состояние ---------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Telethon установлен, функция включена и заданы все учётные данные."""
        return bool(
            TELETHON_INSTALLED
            and settings.telegram_search_enabled
            and int(settings.tg_api_id or 0)
            and str(settings.tg_api_hash or "").strip()
            and str(settings.tg_session_string or "").strip()
        )

    @property
    def available(self) -> bool:
        """Можно ли пользоваться поиском по Telegram."""
        return self.configured and not self._start_failed

    @property
    def connected(self) -> bool:
        """Установлено ли соединение с Telegram."""
        return self._client is not None

    # --- Жизненный цикл ----------------------------------------------------------

    async def start(self) -> None:
        """Подключает Telethon. Безопасна при выключенной или ненастроенной функции."""
        if not self.configured:
            self._log_not_configured()
            return
        async with self._lock:
            if self._client is not None:
                return
            client: Any | None = None
            try:
                client = TelegramClient(
                    StringSession(str(settings.tg_session_string).strip()),
                    int(settings.tg_api_id),
                    str(settings.tg_api_hash).strip(),
                )
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                authorized = await asyncio.wait_for(
                    client.is_user_authorized(), timeout=CONNECT_TIMEOUT
                )
                if not authorized:
                    raise TelegramSearchError(
                        "сессия Telethon не авторизована (проверьте TG_SESSION_STRING)"
                    )
            except asyncio.CancelledError:
                if client is not None:
                    await self._disconnect(client)
                raise
            except Exception:
                logger.exception(
                    "Не удалось подключить Telethon — поиск по Telegram будет недоступен"
                )
                self._start_failed = True
                self._retry_after = time.monotonic() + RETRY_COOLDOWN
                if client is not None:
                    await self._disconnect(client)
                self._client = None
                return
            self._client = client
            self._start_failed = False
            self._retry_after = 0.0
            logger.info("Поиск по Telegram активен (Telethon подключён)")

    async def stop(self) -> None:
        """Отключает Telethon и очищает состояние соединения."""
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await self._disconnect(client)
            logger.info("Клиент Telethon отключён")

    def _log_not_configured(self) -> None:
        """Пишет в лог причину недоступности поиска."""
        if not settings.telegram_search_enabled:
            logger.info("Поиск по Telegram отключён (telegram_search_enabled=False)")
        elif not TELETHON_INSTALLED:
            logger.warning(
                "Поиск по Telegram включён, но библиотека Telethon не установлена: "
                "pip install telethon"
            )
        else:
            logger.warning(
                "Поиск по Telegram включён, но не заданы TG_API_ID / TG_API_HASH / TG_SESSION_STRING"
            )

    @staticmethod
    async def _disconnect(client: Any) -> None:
        """Аккуратно закрывает соединение Telethon."""
        try:
            result = client.disconnect()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Ошибка при отключении Telethon", exc_info=True)

    async def _ensure_client(self) -> Any:
        """Возвращает подключённый клиент либо бросает TelegramSearchUnavailable."""
        if not self.configured:
            raise TelegramSearchUnavailable(UNAVAILABLE_MESSAGE)
        if self._client is None:
            if self._start_failed and time.monotonic() < self._retry_after:
                raise TelegramSearchUnavailable(UNAVAILABLE_MESSAGE)
            await self.start()
        if self._client is None:
            raise TelegramSearchUnavailable(UNAVAILABLE_MESSAGE)
        return self._client

    # --- Поиск -------------------------------------------------------------------

    async def search(
        self, query: str, *, chats: list[str] | None = None, limit: int = 20
    ) -> list[RemoteAudio]:
        """Ищет аудио по заданным (или дефолтным) каналам."""
        client = await self._ensure_client()
        text = (query or "").strip()
        if not text:
            return []

        raw_chats: Iterable[Any] = chats if chats else settings.tg_search_chats_list
        targets: list[Any] = []
        for raw in raw_chats:
            peer = _normalize_chat(raw)
            if peer is not None and peer not in targets:
                targets.append(peer)
        if not targets:
            raise TelegramSearchUnavailable(NO_CHATS_MESSAGE)

        try:
            total_limit = int(limit)
        except (TypeError, ValueError):
            total_limit = 20
        total_limit = max(1, min(total_limit, MAX_SEARCH_LIMIT))

        results: list[RemoteAudio] = []
        seen: set[tuple[int, int]] = set()
        for peer in targets:
            remaining = total_limit - len(results)
            if remaining <= 0:
                break
            try:
                found = await asyncio.wait_for(
                    self._search_in_chat(client, peer, text, remaining),
                    timeout=SEARCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Таймаут поиска аудио в чате %s", peer)
                continue
            except FloodWaitError as exc:
                logger.warning(
                    "Telegram ограничил запросы (FloodWait %s c) при поиске в %s",
                    getattr(exc, "seconds", "?"),
                    peer,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не удалось выполнить поиск в чате %s", peer)
                continue

            for remote, message in found:
                key = (remote.chat_id, remote.message_id)
                if key in seen:
                    continue
                seen.add(key)
                results.append(remote)
                self._remember_message(remote.token, message)
                if len(results) >= total_limit:
                    break

        self.cache(results)
        logger.info(
            "Поиск в Telegram по запросу %r: найдено %d результатов в %d чатах",
            text,
            len(results),
            len(targets),
        )
        return results

    async def _search_in_chat(
        self, client: Any, peer: Any, query: str, limit: int
    ) -> list[tuple[RemoteAudio, Any]]:
        """Ищет аудио в одном чате. Возвращает пары (RemoteAudio, сообщение)."""
        entity = await client.get_entity(peer)
        found: list[tuple[RemoteAudio, Any]] = []
        async for message in client.iter_messages(
            entity, search=query, filter=InputMessagesFilterMusic, limit=limit
        ):
            remote = _remote_from_message(message)
            if remote is None:
                continue
            found.append((remote, message))
            if len(found) >= limit:
                break
        return found

    async def resolve_link(self, url: str) -> RemoteAudio | None:
        """Разбирает ссылку на сообщение Telegram и возвращает аудио (или None)."""
        client = await self._ensure_client()
        parsed = _parse_link(url)
        if parsed is None:
            return None
        peer, message_id = parsed
        try:
            message = await asyncio.wait_for(
                client.get_messages(peer, ids=message_id), timeout=RESOLVE_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise TelegramSearchError(TIMEOUT_MESSAGE) from exc
        except FloodWaitError as exc:
            raise TelegramSearchError(_flood_message(exc)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Не удалось получить сообщение по ссылке %s: %s", url, exc)
            raise TelegramSearchError(LINK_FAILED_MESSAGE) from exc

        if isinstance(message, (list, tuple)):
            message = message[0] if message else None
        if message is None:
            return None

        remote = _remote_from_message(message, fallback_link=str(url).strip())
        if remote is None:
            return None
        if not remote.link:
            remote.link = str(url).strip()
        self.cache([remote])
        self._remember_message(remote.token, message)
        return remote

    async def download(self, remote: RemoteAudio) -> bytes:
        """Скачивает аудио в память с проверкой размера и таймаутом."""
        client = await self._ensure_client()
        max_size = int(settings.max_download_size or 0)
        if max_size and remote.file_size and remote.file_size > max_size:
            raise FileTooLargeError(
                f"Файл слишком большой ({_mb(remote.file_size)} МБ). "
                f"Максимальный размер — {_mb(max_size)} МБ"
            )

        message = self._get_message(remote.token)
        if message is None:
            message = await self._fetch_message(client, remote)
        if message is None:
            raise TelegramSearchError(LINK_FAILED_MESSAGE)

        buffer = io.BytesIO()
        try:
            await asyncio.wait_for(
                client.download_media(message, file=buffer), timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise TelegramSearchError(
                "Не удалось скачать файл: превышено время ожидания"
            ) from exc
        except FloodWaitError as exc:
            raise TelegramSearchError(_flood_message(exc)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Ошибка скачивания аудио %s", remote.link or remote.token)
            raise TelegramSearchError(DOWNLOAD_FAILED_MESSAGE) from exc

        data = buffer.getvalue()
        if not data:
            raise TelegramSearchError("Telegram вернул пустой файл")
        if max_size and len(data) > max_size:
            raise FileTooLargeError(
                f"Файл слишком большой ({_mb(len(data))} МБ). "
                f"Максимальный размер — {_mb(max_size)} МБ"
            )
        logger.info("Скачано %d байт из Telegram (%s)", len(data), remote.link or remote.token)
        return data

    async def _fetch_message(self, client: Any, remote: RemoteAudio) -> Any | None:
        """Повторно получает сообщение по данным RemoteAudio."""
        peers: list[Any] = []
        if remote.chat_username:
            peers.append(remote.chat_username)
        if remote.chat_id:
            peers.append(remote.chat_id)
            if PeerChannel is not None:
                internal = _internal_channel_id(remote.chat_id)
                if internal.isdigit():
                    peers.append(PeerChannel(int(internal)))
        if not peers or not remote.message_id:
            return None

        last_error: Exception | None = None
        for peer in peers:
            try:
                message = await asyncio.wait_for(
                    client.get_messages(peer, ids=remote.message_id),
                    timeout=RESOLVE_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                last_error = exc
                continue
            except FloodWaitError as exc:
                raise TelegramSearchError(_flood_message(exc)) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # неверный peer — пробуем следующий вариант
                last_error = exc
                logger.debug("Не удалось получить сообщение через peer %s: %s", peer, exc)
                continue
            if isinstance(message, (list, tuple)):
                message = message[0] if message else None
            if message is not None:
                return message
        if last_error is not None:
            logger.warning(
                "Сообщение %s не найдено: %s", remote.link or remote.message_id, last_error
            )
        return None

    # --- Кэш результатов ---------------------------------------------------------

    def cache(self, items: list[RemoteAudio]) -> None:
        """Кладёт результаты в LRU-кэш (по токенам), вытесняя старые."""
        for item in items or []:
            if not isinstance(item, RemoteAudio) or not item.token:
                continue
            self._cache.pop(item.token, None)
            self._cache[item.token] = item
        while len(self._cache) > CACHE_LIMIT:
            token, _ = self._cache.popitem(last=False)
            self._messages.pop(token, None)

    def get_cached(self, token: str) -> RemoteAudio | None:
        """Возвращает результат поиска по токену (и освежает его в LRU)."""
        key = (token or "").strip()
        if not key:
            return None
        item = self._cache.get(key)
        if item is None:
            return None
        self._cache.move_to_end(key)
        return item

    def _remember_message(self, token: str, message: Any) -> None:
        """Держит объект сообщения Telethon рядом с результатом (для скачивания)."""
        if not token or message is None:
            return
        self._messages.pop(token, None)
        self._messages[token] = message
        while len(self._messages) > CACHE_LIMIT:
            self._messages.popitem(last=False)

    def _get_message(self, token: str) -> Any | None:
        """Достаёт сохранённое сообщение Telethon по токену."""
        key = (token or "").strip()
        if not key:
            return None
        message = self._messages.get(key)
        if message is not None:
            self._messages.move_to_end(key)
        return message


# Модульный синглтон — используется API, ботом и lifespan приложения.
telegram_search = TelegramSearchService()


# Повторный импорт одного и того же сообщения (двойной клик «⬇️ Добавить» или та же
# ссылка t.me) не должен качать файл и слать его в канал-хранилище второй раз.
_IMPORT_LOCKS: OrderedDict[tuple[Any, ...], asyncio.Lock] = OrderedDict()
_IMPORTED: OrderedDict[tuple[Any, ...], int] = OrderedDict()


def _import_key(user_id: int, remote: RemoteAudio) -> tuple[Any, ...]:
    """Ключ импорта: одно сообщение Telegram для одного пользователя."""
    if remote.chat_id and remote.message_id:
        return (int(user_id), int(remote.chat_id), int(remote.message_id))
    return (int(user_id), 0, 0, remote.link or remote.token)


def _import_lock(key: tuple[Any, ...]) -> asyncio.Lock:
    """Замок на импорт конкретного сообщения (LRU, вытесняем только свободные)."""
    lock = _IMPORT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _IMPORT_LOCKS[key] = lock
    else:
        _IMPORT_LOCKS.move_to_end(key)
    for old_key in list(_IMPORT_LOCKS):
        if len(_IMPORT_LOCKS) <= CACHE_LIMIT:
            break
        if old_key == key or _IMPORT_LOCKS[old_key].locked():
            continue
        _IMPORT_LOCKS.pop(old_key, None)
    return lock


def _remember_import(key: tuple[Any, ...], track_id: Any) -> None:
    """Запоминает, каким треком закончился импорт этого сообщения."""
    try:
        value = int(track_id)
    except (TypeError, ValueError):
        return
    _IMPORTED.pop(key, None)
    _IMPORTED[key] = value
    while len(_IMPORTED) > CACHE_LIMIT:
        _IMPORTED.popitem(last=False)


async def _imported_track(user_id: int, key: tuple[Any, ...]) -> dict | None:
    """Трек, уже импортированный из этого сообщения (если он ещё в библиотеке)."""
    track_id = _IMPORTED.get(key)
    if track_id is None:
        return None
    track = await tracks_repo.get_track(user_id, int(track_id))
    if track is None:  # трек удалили — импортируем заново
        _IMPORTED.pop(key, None)
        return None
    _IMPORTED.move_to_end(key)
    return track


def _as_duplicate(track: dict) -> dict:
    """Помечает трек как уже бывший в библиотеке: импорта на самом деле не было."""
    marked = dict(track)
    marked["duplicate"] = True
    return marked


async def import_remote(bot: "Bot", user_id: int, remote: RemoteAudio) -> dict:
    """Скачивает найденное аудио, кладёт в канал-хранилище и создаёт трек."""
    key = _import_key(user_id, remote)
    async with _import_lock(key):
        known = await _imported_track(user_id, key)
        if known is not None:
            logger.info(
                "Сообщение %s уже импортировано пользователем %s — файл не скачиваем",
                remote.link or remote.title,
                user_id,
            )
            return _as_duplicate(known)
        return await _import_new(bot, user_id, remote, key)


async def _import_new(
    bot: "Bot", user_id: int, remote: RemoteAudio, key: tuple[Any, ...]
) -> dict:
    """Собственно импорт: скачивание, отправка в канал и создание трека."""
    data = await telegram_search.download(remote)

    file_name = _safe_file_name(remote)
    caption_parts = [part for part in (remote.performer, remote.title) if part]
    caption = " — ".join(caption_parts) if caption_parts else remote.title
    if remote.link:
        caption = f"{caption}\n{remote.link}"

    stored = await storage.store_from_bytes(
        bot,
        data,
        file_name=file_name,
        title=remote.title,
        performer=remote.performer,
        duration=remote.duration,
        mime_type=remote.mime_type,
        caption=caption,
    )

    file_unique_id = getattr(stored, "file_unique_id", None)
    if file_unique_id:
        existing = await tracks_repo.get_track_by_unique_id(user_id, file_unique_id)
        if existing is not None:
            # Копия уже лежит в канале от прошлого импорта — свежее сообщение
            # никому не нужно, убираем его, чтобы не мусорить в хранилище.
            await storage.delete_from_channel(bot, stored.message_id)
            logger.info(
                "Трек уже есть в библиотеке пользователя %s (%s) — импорт пропущен",
                user_id,
                remote.link or remote.title,
            )
            _remember_import(key, existing.get("id"))
            return _as_duplicate(existing)

    track = await tracks_repo.create_track(
        user_id,
        title=stored.title or remote.title,
        artist=stored.artist or remote.performer,
        album=stored.album,
        duration=int(stored.duration or remote.duration or 0),
        file_size=int(stored.file_size or remote.file_size or len(data)),
        mime_type=stored.mime_type or remote.mime_type,
        file_name=stored.file_name or file_name,
        file_id=stored.file_id,
        file_unique_id=stored.file_unique_id,
        storage_chat_id=stored.chat_id,
        storage_message_id=stored.message_id,
        thumb_file_id=stored.thumb_file_id,
        source="telegram_search",
        source_ref=remote.link or None,
    )

    try:
        await autosort.apply_autosort(
            user_id,
            track,
            artist_name=track.get("artist") or remote.performer,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Автосортировка не удалась для трека %s пользователя %s",
            track.get("id"),
            user_id,
        )

    track_id = track.get("id")
    if track_id is not None:
        _remember_import(key, track_id)
        fresh = await tracks_repo.get_track(user_id, int(track_id))
        if fresh is not None:
            return fresh
    return track


__all__ = [
    "RemoteAudio",
    "TelegramSearchService",
    "telegram_search",
    "import_remote",
    "UNAVAILABLE_MESSAGE",
    "TELETHON_INSTALLED",
]
