"""Хендлеры загрузки аудио в хранилище.

Принимает обычные и пересланные сообщения с аудио (``message.audio``) и документами
с mime-типом ``audio/*``, копирует файл в приватный канал-хранилище, разбирает
метаданные (включая всех исполнителей и жанр), создаёт трек в БД и раскладывает
его по папкам: автоматически (``auto_sort_enabled``) либо по выбору пользователя
(``MoveCB`` и FSM ввода названия новой папки).

Одиночная загрузка работает ровно как в V1. Если файлы приходят ПАЧКОЙ (альбомом
Telegram или просто несколько сообщений подряд), бот не задаёт вопрос по каждому
треку: файлы копятся в FSM-данных пользователя, а через ``BATCH_IDLE`` секунд
тишины управление уходит в :mod:`backend.bot.handlers.batch` — там пачка
раскладывается группами по исполнителю (ТЗ п. 8).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Any, Final

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Audio,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from backend.bot import texts
from backend.bot.callbacks import FOLDER_NEW, FOLDER_NONE, MoveCB
from backend.bot.handlers import batch as batch_handlers
from backend.bot.keyboards import autosort_kb, folder_pick_kb
from backend.bot.states import UploadStates
from backend.bot.utils import ack, escape, safe_edit
from backend.config import settings
from backend.db.repositories import albums as albums_repo
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import FileTooLargeError, MusicBoxError, StorageError, ValidationError
from backend.services import autosort, media, metadata, storage

logger = logging.getLogger(__name__)

router = Router(name="upload")

#: Служебное сообщение, которое показывается на время копирования файла в канал.
UPLOAD_STATUS = "⏳ Загружаю…"

#: Максимальная длина названия папки (согласовано с текстами подсказок).
MAX_FOLDER_NAME_LENGTH = 64

#: Сколько папок максимум показываем в клавиатуре выбора папки.
MAX_PICK_FOLDERS = 50

#: Сколько исполнителей максимум связываем с одним треком.
MAX_TRACK_ARTISTS: Final[int] = 8

#: Собственные callback-данные модуля: создать папку с именем исполнителя
#: без ввода текста. Формат ``upnew:<track_id>`` — фабрикой MoveCB не разбирается.
NEW_FROM_ARTIST_PREFIX = "upnew"


# ---------------------------------------------------------------------------
# Пачка файлов: накопление во FSM и передача в handlers/batch.py
# ---------------------------------------------------------------------------

#: Ключи FSM-данных буфера пачки. Правим только их — чужие данные не трогаем.
BATCH_IDS_KEY: Final[str] = "upload_batch_ids"
BATCH_SEQ_KEY: Final[str] = "upload_batch_seq"
BATCH_INFLIGHT_KEY: Final[str] = "upload_batch_inflight"
BATCH_SENT_AT_KEY: Final[str] = "upload_batch_sent_at"
BATCH_GROUP_KEY: Final[str] = "upload_batch_media_group"
BATCH_ACTIVE_KEY: Final[str] = "upload_batch_active"
BATCH_FIRST_MESSAGE_KEY: Final[str] = "upload_batch_first_message"

#: Файлы, отправленные с интервалом не больше этого (секунды, по времени
#: ОТПРАВКИ, а не обработки), считаются одной пачкой.
BATCH_WINDOW: Final[float] = 10.0

#: Сколько ждём тишины после последнего файла, прежде чем начать раскладку.
BATCH_IDLE: Final[float] = 2.5

#: Предел размера одной пачки — дальше файлы просто сохраняются без раскладки.
MAX_BATCH_TRACKS: Final[int] = 200

#: Замки на буфер пачки: aiogram обрабатывает сообщения параллельно, и без
#: синхронизации два файла одной пачки затёрли бы записи друг друга в FSM.
#: Здесь ТОЛЬКО примитивы синхронизации — состояние пачки живёт в FSM-данных.
_BATCH_LOCKS: dict[Any, asyncio.Lock] = {}

#: Предел размера кэша замков (чистится от свободных при переполнении).
_BATCH_LOCK_LIMIT: Final[int] = 500

#: Ссылки на запущенные таймеры раскладки — иначе задачи соберёт сборщик мусора.
_BATCH_TASKS: set[asyncio.Task[None]] = set()


def _to_int(value: Any, default: int = 0) -> int:
    """Безопасное приведение значения FSM-данных к целому числу."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _prune_batch_locks() -> None:
    """Убирает свободные замки неактивных пользователей."""
    stale = [key for key, lock in _BATCH_LOCKS.items() if not lock.locked()]
    for key in stale:
        _BATCH_LOCKS.pop(key, None)
    logger.debug("Кэш замков пачки очищен: удалено %s записей", len(stale))


def _batch_lock(state: FSMContext) -> asyncio.Lock:
    """Замок на буфер пачки конкретного пользователя."""
    key = state.key
    lock = _BATCH_LOCKS.get(key)
    if lock is None:
        if len(_BATCH_LOCKS) >= _BATCH_LOCK_LIMIT:
            _prune_batch_locks()
        lock = asyncio.Lock()
        _BATCH_LOCKS[key] = lock
    return lock


def _sent_at(message: Message) -> float:
    """Время отправки сообщения (UNIX-время); подстраховка — время сервера."""
    stamp = getattr(getattr(message, "date", None), "timestamp", None)
    if callable(stamp):
        try:
            return float(stamp())
        except (TypeError, ValueError, OSError):
            logger.debug("Не удалось прочитать время отправки сообщения — беру текущее")
    return time.time()


def _message_id(message: Message | None) -> int:
    """Идентификатор сообщения или 0, если сообщения нет."""
    return _to_int(getattr(message, "message_id", 0)) if isinstance(message, Message) else 0


def _stored_ids(data: dict[str, Any]) -> list[int]:
    """Список идентификаторов треков пачки из FSM-данных."""
    raw = data.get(BATCH_IDS_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    return [value for value in (_to_int(item) for item in raw) if value > 0]


def _batch_continues(data: dict[str, Any], message: Message, sent_at: float) -> bool:
    """Продолжает ли этот файл уже начатую пачку."""
    if not _stored_ids(data) and _to_int(data.get(BATCH_INFLIGHT_KEY)) <= 0:
        return False

    group = getattr(message, "media_group_id", None)
    if group and str(group) == str(data.get(BATCH_GROUP_KEY) or ""):
        return True

    previous = data.get(BATCH_SENT_AT_KEY)
    if previous is None:
        return False
    try:
        gap = sent_at - float(previous)
    except (TypeError, ValueError):
        return False
    return 0.0 <= gap <= BATCH_WINDOW


async def _batch_arrived(state: FSMContext, message: Message) -> None:
    """Отмечает начало обработки файла: обновляет окно пачки и счётчик в работе."""
    sent_at = _sent_at(message)
    group = getattr(message, "media_group_id", None)
    async with _batch_lock(state):
        data = await state.get_data()
        updates: dict[str, Any] = {}
        if not _batch_continues(data, message, sent_at):
            # Прошлое окно закрылось — начинаем новую пачку.
            updates[BATCH_IDS_KEY] = []
            updates[BATCH_ACTIVE_KEY] = False
            updates[BATCH_FIRST_MESSAGE_KEY] = 0
        updates[BATCH_INFLIGHT_KEY] = max(_to_int(data.get(BATCH_INFLIGHT_KEY)), 0) + 1
        updates[BATCH_SENT_AT_KEY] = sent_at
        updates[BATCH_GROUP_KEY] = str(group or "")
        await state.update_data(**updates)


async def _batch_departed(state: FSMContext, track_id: int) -> tuple[bool, int, int, int]:
    """Отмечает конец обработки файла и решает, пачка это или одиночная загрузка.

    Возвращает `(режим_пачки, id_сообщения_первого_файла, номер_шага, id_первого_трека)`.
    `id_сообщения_первого_файла` не ноль только в момент перехода в режим пачки:
    ответ на первый файл нужно переоформить, чтобы его кнопки не спорили
    с диалогом раскладки.
    """
    async with _batch_lock(state):
        data = await state.get_data()
        inflight = max(_to_int(data.get(BATCH_INFLIGHT_KEY)) - 1, 0)
        ids = _stored_ids(data)
        if track_id > 0 and track_id not in ids:
            if len(ids) < MAX_BATCH_TRACKS:
                ids.append(int(track_id))
            else:
                logger.warning(
                    "Пачка достигла предела в %s треков — трек %s раскладывается отдельно",
                    MAX_BATCH_TRACKS,
                    track_id,
                )

        active = bool(data.get(BATCH_ACTIVE_KEY))
        batch_mode = active or inflight > 0 or len(ids) >= 2
        first_message_id = _to_int(data.get(BATCH_FIRST_MESSAGE_KEY))
        rewrite_id = 0
        if batch_mode and not active:
            active = True
            rewrite_id = first_message_id
            first_message_id = 0

        seq = _to_int(data.get(BATCH_SEQ_KEY)) + 1
        await state.update_data(
            **{
                BATCH_INFLIGHT_KEY: inflight,
                BATCH_IDS_KEY: ids,
                BATCH_ACTIVE_KEY: active,
                BATCH_SEQ_KEY: seq,
                BATCH_FIRST_MESSAGE_KEY: first_message_id,
            }
        )
    return batch_mode, rewrite_id, seq, (ids[0] if ids else 0)


async def _remember_first_message(state: FSMContext, message_id: int) -> bool:
    """Запоминает ответ на первый файл; True — пачка началась, пока мы отвечали."""
    async with _batch_lock(state):
        data = await state.get_data()
        if bool(data.get(BATCH_ACTIVE_KEY)):
            return True
        if message_id > 0:
            await state.update_data(**{BATCH_FIRST_MESSAGE_KEY: int(message_id)})
        return False


def _schedule_batch(bot: Bot, state: FSMContext, user_id: int, chat_id: int, seq: int) -> None:
    """Ставит таймер: раскладка начнётся, если новых файлов больше не будет."""
    task = asyncio.create_task(_batch_timer(bot, state, user_id, chat_id, seq))
    _BATCH_TASKS.add(task)
    task.add_done_callback(_BATCH_TASKS.discard)


async def _batch_timer(
    bot: Bot, state: FSMContext, user_id: int, chat_id: int, seq: int
) -> None:
    """Ждёт тишины и передаёт накопленную пачку в раскладку."""
    try:
        await asyncio.sleep(BATCH_IDLE)
        async with _batch_lock(state):
            data = await state.get_data()
            if _to_int(data.get(BATCH_SEQ_KEY)) != seq:
                return  # пришёл ещё файл — раскладку начнёт его таймер
            if _to_int(data.get(BATCH_INFLIGHT_KEY)) > 0:
                return  # что-то ещё загружается
            if not bool(data.get(BATCH_ACTIVE_KEY)):
                return  # это была одиночная загрузка
            ids = _stored_ids(data)
            await state.update_data(
                **{BATCH_IDS_KEY: [], BATCH_ACTIVE_KEY: False, BATCH_FIRST_MESSAGE_KEY: 0}
            )
        if not ids:
            return
        logger.info("Пользователь %s: пачка из %s треков готова к раскладке", user_id, len(ids))
        await batch_handlers.start_batch(
            bot, state, user_id=user_id, chat_id=chat_id, track_ids=ids
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — фоновая задача не должна ронять процесс
        logger.exception("Пользователь %s: не удалось начать раскладку пачки", user_id)


# ---------------------------------------------------------------------------
# Определение аудио в сообщении
# ---------------------------------------------------------------------------


def _is_audio_document(document: Any) -> bool:
    """Проверяет, что документ — аудиофайл (по mime-типу или расширению).

    Признак «документ — это аудио» в проекте один: `media.is_audio_document`.
    Раздел «Другое» берёт ровно дополнение к нему, поэтому своей копии списка
    расширений здесь быть не должно — иначе часть документов (например
    ``*.mp4a`` или mime ``application/ogg`` без аудио-расширения в имени) не
    подхватит ни один хендлер и файл потеряется молча.
    """
    return media.is_audio_document(document)


def _extract_media(message: Message) -> Audio | Document | None:
    """Возвращает аудио или аудио-документ из сообщения."""
    audio = getattr(message, "audio", None)
    if audio is not None:
        return audio
    document = getattr(message, "document", None)
    if _is_audio_document(document):
        return document
    return None


# ---------------------------------------------------------------------------
# Источник трека (обычная загрузка или пересланное сообщение)
# ---------------------------------------------------------------------------


def _forward_source_ref(message: Message) -> str | None:
    """Собирает ссылку/идентификатор источника для пересланного сообщения."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        origin_message_id = getattr(origin, "message_id", None)
        if chat is not None:
            username = getattr(chat, "username", None)
            if username and origin_message_id:
                return f"https://t.me/{username}/{origin_message_id}"
            chat_id = getattr(chat, "id", None)
            if chat_id is not None and origin_message_id:
                return f"channel:{chat_id}:{origin_message_id}"
            if chat_id is not None:
                return f"chat:{chat_id}"
        sender_user = getattr(origin, "sender_user", None)
        if sender_user is not None:
            return f"user:{getattr(sender_user, 'id', '')}"
        sender_name = getattr(origin, "sender_user_name", None)
        if sender_name:
            return f"user:{sender_name}"
        return "telegram_forward"

    # Совместимость со старым форматом полей Bot API (до 7.0).
    legacy_chat = getattr(message, "forward_from_chat", None)
    legacy_message_id = getattr(message, "forward_from_message_id", None)
    if legacy_chat is not None:
        username = getattr(legacy_chat, "username", None)
        if username and legacy_message_id:
            return f"https://t.me/{username}/{legacy_message_id}"
        chat_id = getattr(legacy_chat, "id", None)
        if chat_id is not None:
            return f"channel:{chat_id}:{legacy_message_id or 0}"
    legacy_user = getattr(message, "forward_from", None)
    if legacy_user is not None:
        return f"user:{getattr(legacy_user, 'id', '')}"
    return None


def _is_forwarded(message: Message) -> bool:
    """True, если сообщение переслано из другого чата или канала."""
    for attribute in ("forward_origin", "forward_from_chat", "forward_from", "forward_date"):
        if getattr(message, attribute, None) is not None:
            return True
    return False


def _resolve_source(message: Message) -> tuple[str, str | None]:
    """Возвращает пару ``(source, source_ref)`` для записи трека."""
    if _is_forwarded(message):
        return "telegram_forward", _forward_source_ref(message)
    return "upload", None


# ---------------------------------------------------------------------------
# Ответы пользователю
# ---------------------------------------------------------------------------


def _album_line(track: dict[str, Any]) -> str:
    """Строка с альбомом («💿 Альбом: X») или пустая, если альбом неизвестен."""
    album = (track.get("album") or "").strip()
    if not album:
        return ""
    return f"\n💿 Альбом: {escape(album)}"


def _track_caption(track: dict[str, Any]) -> str:
    """Короткое описание трека: «<b>Название</b> — Исполнитель · 3:07»."""
    line = f"<b>{escape(track.get('title') or 'Без названия')}</b>"
    artist = track.get("artist")
    if artist:
        line += f" — {escape(artist)}"
    duration = metadata.format_duration(track.get("duration"))
    if duration and duration != metadata.NO_DURATION:
        line += f" · {duration}"
    return line + _album_line(track)


def _batch_item_line(track: dict[str, Any]) -> str:
    """Короткий ответ на файл из пачки: подробности покажет диалог раскладки."""
    line = f"✅ {_track_caption(track)}"
    folder_name = track.get("folder_name")
    if folder_name:
        line += f"\n📁 {escape(folder_name)}"
    return line


def _artist_names(performer: str | None) -> list[str]:
    """Все исполнители трека: «MiyaGi & Эндшпиль» → ``["MiyaGi", "Эндшпиль"]``.

    Первый в списке становится основным (`tracks.artist_id`), остальные попадают
    в `track_artists` с положительным `position` (контракт V2, п. 1.2).
    """
    names = metadata.split_artists(performer)
    if names:
        return names[:MAX_TRACK_ARTISTS]
    single = (performer or "").strip()
    return [single] if single else []


async def _ensure_artists(user_id: int, names: list[str]) -> list[int]:
    """Заводит исполнителей (идемпотентно) и возвращает их id по порядку."""
    artist_ids: list[int] = []
    for name in names:
        row = await artists_repo.ensure_artist(user_id, name)
        if row:
            artist_ids.append(int(row["id"]))
    return artist_ids


async def _rewrite_first_message(
    bot: Bot, chat_id: int, message_id: int, user_id: int, track_id: int
) -> None:
    """Переоформляет ответ на первый файл пачки в короткую строку без кнопок."""
    if message_id <= 0 or track_id <= 0:
        return
    try:
        track = await tracks_repo.get_track(user_id, track_id)
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось прочитать трек %s: %s", user_id, track_id, error)
        return
    if track is None:
        return

    try:
        await bot.edit_message_text(
            text=_batch_item_line(track),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramBadRequest as exc:
        logger.debug("Ответ на первый файл пачки не переоформлен: %s", exc)
    except TelegramAPIError as exc:
        logger.warning("Ошибка Telegram при переоформлении ответа на первый файл: %s", exc)


def _folder_hint(track: dict[str, Any]) -> str:
    """Строка «где лежит трек» — для сообщения о дубликате."""
    folder_name = track.get("folder_name")
    if folder_name:
        return f"Он лежит в папке «{escape(folder_name)}»."
    return "Он пока без папки — можно разложить его командой /folders."


async def _reply_status(message: Message) -> Message | None:
    """Отправляет служебное сообщение «⏳ Загружаю…»."""
    try:
        return await message.answer(UPLOAD_STATUS)
    except Exception:  # noqa: BLE001 — статусное сообщение не должно ломать загрузку
        logger.exception("Не удалось отправить статусное сообщение пользователю")
        return None


async def _finish(
    status: Message | None,
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показывает итог: правит статусное сообщение либо отвечает новым."""
    if status is not None:
        await safe_edit(status, text, markup)
        return
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Дочитывание тегов из самого файла (альбом, год, недостающие поля)
# ---------------------------------------------------------------------------


def _download_limit() -> int:
    """Предел скачивания в байтах (0 — лимит не задан)."""
    try:
        value = int(getattr(settings, "max_download_size", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _tag_suffix(file_name: str | None) -> str:
    """Расширение для временного файла: mutagen выбирает парсер в том числе по нему."""
    name = (file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    _, dot, ext = name.rpartition(".")
    if not dot:
        return ".mp3"
    ext = ext.strip().lower()
    if not ext or len(ext) > 5 or not ext.isalnum():
        return ".mp3"
    return f".{ext}"


def _write_temp_file(data: bytes, suffix: str) -> str:
    """Записывает байты во временный файл (вызывается в отдельном потоке)."""
    handle, path = tempfile.mkstemp(prefix="musicbox-tags-", suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
    except BaseException:
        _remove_temp_file(path)
        raise
    return path


def _remove_temp_file(path: str) -> None:
    """Удаляет временный файл; ошибки только логируются."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Не удалось удалить временный файл %s", path)


async def _read_file_tags(bot: Bot, stored: storage.StoredAudio) -> metadata.AudioMetadata | None:
    """Скачивает файл во временный и читает его ID3/Vorbis-теги через mutagen.

    Возвращает None, если файл больше лимита Bot API, скачать его не удалось
    либо теги не читаются. Ошибки только логируются: трек уже сохранён.
    """
    limit = _download_limit()
    file_size = 0
    try:
        file_size = int(getattr(stored, "file_size", 0) or 0)
    except (TypeError, ValueError):
        file_size = 0

    if limit and file_size > limit:
        logger.info(
            "Теги файла не читаем: размер %s Б больше лимита %s Б", file_size, limit
        )
        return None

    try:
        data = await storage.download_bytes(bot, stored.file_id, max_size=limit or None)
    except FileTooLargeError:
        logger.info("Теги файла не читаем: Telegram отдал файл больше лимита")
        return None
    except StorageError as error:
        logger.warning("Не удалось скачать файл для чтения тегов: %s", error)
        return None
    except Exception:  # noqa: BLE001 — чтение тегов необязательно
        logger.warning("Сбой при скачивании файла для чтения тегов", exc_info=True)
        return None

    try:
        path = await asyncio.to_thread(_write_temp_file, data, _tag_suffix(stored.file_name))
    except OSError:
        logger.warning("Не удалось создать временный файл для чтения тегов", exc_info=True)
        return None

    try:
        return await metadata.extract_tags(path)
    except Exception:  # noqa: BLE001 — extract_tags глушит свои ошибки, но подстрахуемся
        logger.warning("Не удалось прочитать теги файла", exc_info=True)
        return None
    finally:
        await asyncio.to_thread(_remove_temp_file, path)


def _better_value(tag_value: str, current: str, guessed: str | None) -> bool:
    """Стоит ли заменить текущее значение поля значением из тега файла.

    Заменяем, только если тег непустой и отличается, а текущее значение либо
    пустое, либо это «Без названия», либо всего лишь догадка по имени файла.
    """
    if not tag_value:
        return False
    if metadata.normalize_name(tag_value) == metadata.normalize_name(current):
        return False
    if not current or current == metadata.DEFAULT_TITLE:
        return True
    return bool(guessed) and metadata.normalize_name(current) == metadata.normalize_name(guessed)


async def _enrich_with_tags(
    bot: Bot,
    user_id: int,
    track: dict[str, Any],
    stored: storage.StoredAudio,
    fallback_year: int | None,
) -> dict[str, Any]:
    """Дозаполняет альбом, жанр и состав исполнителей трека тегами самого файла.

    Вызывается, когда Telegram и имя файла не дали альбом или жанр. Возвращает
    обновлённый трек либо исходный, если дополнить нечем.
    """
    tags = await _read_file_tags(bot, stored)
    if tags is None:
        return track

    album = (tags.album or "").strip()
    artist = (tags.artist or "").strip()
    title = (tags.title or "").strip()
    genre = (tags.genre or "").strip()
    year = tags.year or fallback_year

    # Настоящий тег точнее догадки по имени файла, но осмысленные данные,
    # присланные Telegram, не затираем.
    guessed_artist, guessed_title = metadata.guess_from_filename(
        stored.file_name or track.get("file_name")
    )
    current_title = (track.get("title") or "").strip()
    current_artist = (track.get("artist") or "").strip()

    updates: dict[str, Any] = {}
    if album and not (track.get("album") or "").strip():
        updates["album"] = album
    if genre and not (track.get("genre") or "").strip():
        updates["genre"] = genre
    if title != metadata.DEFAULT_TITLE and _better_value(title, current_title, guessed_title):
        updates["title"] = title
    if _better_value(artist, current_artist, guessed_artist):
        updates["artist"] = artist

    try:
        artist_id = track.get("artist_id")
        artist_name = updates.get("artist") or current_artist
        # Состав исполнителей переписываем, только если тег дал новое имя
        # или связей ещё нет вовсе (например, файл пришёл без performer).
        linked_ids = tracks_repo.parse_artist_ids(track)
        new_artist_ids: list[int] = []
        if artist_name and ("artist" in updates or not linked_ids):
            new_artist_ids = await _ensure_artists(user_id, _artist_names(artist_name))
            if new_artist_ids:
                artist_id = new_artist_ids[0]
            if new_artist_ids == linked_ids:
                new_artist_ids = []

        album_name = updates.get("album") or (track.get("album") or "").strip()
        if album_name and track.get("album_id") is None:
            album_row = await albums_repo.ensure_album(user_id, album_name, artist_id, year)
            if album_row:
                updates["album_id"] = album_row["id"]

        if not updates and not new_artist_ids:
            return track

        if new_artist_ids:
            # set_track_artists сама приводит tracks.artist_id к основному
            # исполнителю, поэтому отдельно его в updates не кладём.
            await tracks_repo.set_track_artists(user_id, int(track["id"]), new_artist_ids)
            updates.pop("artist_id", None)

        if not updates:
            refreshed = await tracks_repo.get_track(user_id, int(track["id"]))
            return refreshed if refreshed is not None else track

        updated = await tracks_repo.update_track(user_id, int(track["id"]), **updates)
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось дописать теги треку %s: %s",
            user_id,
            track.get("id"),
            error,
        )
        return track
    except Exception:  # noqa: BLE001 — дозаполнение не должно ломать загрузку
        logger.exception(
            "Пользователь %s: сбой при дописывании тегов треку %s", user_id, track.get("id")
        )
        return track

    if updated is None:
        return track

    logger.info(
        "Пользователь %s: трек %s дополнен тегами файла (%s)",
        user_id,
        updated.get("id"),
        ", ".join(sorted(updates)),
    )
    return updated


# ---------------------------------------------------------------------------
# Основной поток загрузки
# ---------------------------------------------------------------------------


@router.message(F.audio)
async def handle_audio(message: Message, bot: Bot, state: FSMContext) -> None:
    """Приём аудиосообщения (в том числе пересланного из публичного канала)."""
    await _process_upload(message, bot, state)


@router.message(F.document.func(_is_audio_document))
async def handle_audio_document(message: Message, bot: Bot, state: FSMContext) -> None:
    """Приём аудиофайла, отправленного документом."""
    await _process_upload(message, bot, state)


async def _process_upload(message: Message, bot: Bot, state: FSMContext) -> None:
    """Полный цикл сохранения одного аудиофайла.

    Одиночный файл обрабатывается ровно как в V1. Если рядом оказались другие
    файлы (альбом Telegram или несколько сообщений подряд), ответы становятся
    короткими, а раскладку по папкам берёт на себя диалог из `handlers/batch.py`.
    """
    if message.from_user is None:
        logger.debug("Сообщение без отправителя — загрузка пропущена")
        return

    audio_media = _extract_media(message)
    if audio_media is None:
        await message.answer(texts.UNSUPPORTED_FILE)
        return

    user_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    # Гарантирует наличие пользователя и его настроек (внешние ключи треков и папок).
    user_settings = await users_repo.get_settings(user_id)

    await _batch_arrived(state, message)
    saved: tuple[dict[str, Any], str | None, Message | None] | None = None
    try:
        saved = await _save_upload(message, bot, audio_media, user_id)
    finally:
        batch_mode, rewrite_id, seq, first_id = await _batch_departed(
            state, int(saved[0]["id"]) if saved else 0
        )

    track_id = int(saved[0]["id"]) if saved else 0
    if rewrite_id > 0 and first_id > 0 and first_id != track_id:
        await _rewrite_first_message(bot, chat_id, rewrite_id, user_id, first_id)

    if saved is None:
        # Файл не сохранён (дубликат, слишком большой, ошибка канала): пользователь
        # уже получил объяснение, но пачку из-за этого бросать нельзя.
        if batch_mode:
            _schedule_batch(bot, state, user_id, chat_id, seq)
        return

    track, artist_name, status = saved

    if batch_mode:
        await _finish_batch_item(status, message, user_id, track, artist_name, user_settings)
        _schedule_batch(bot, state, user_id, chat_id, seq)
        return

    await _finish_upload(status, message, user_id, track, artist_name, user_settings)

    # Пока мы отвечали, мог прийти второй файл — тогда это всё-таки пачка.
    if await _remember_first_message(state, _message_id(status)):
        await _rewrite_first_message(bot, chat_id, _message_id(status), user_id, track_id)
        _schedule_batch(bot, state, user_id, chat_id, seq)


async def _save_upload(
    message: Message,
    bot: Bot,
    media_item: Audio | Document,
    user_id: int,
) -> tuple[dict[str, Any], str | None, Message | None] | None:
    """Копирует файл в канал и создаёт трек.

    Возвращает `(трек, имя исполнителя, статусное сообщение)` либо None, если
    сохранить не удалось — пользователю в этом случае уже отправлено объяснение.
    """
    # Дубликат ловим до копирования файла в канал — так не мусорим в хранилище.
    incoming_unique_id = getattr(media_item, "file_unique_id", None)
    if incoming_unique_id:
        existing = await tracks_repo.get_track_by_unique_id(user_id, str(incoming_unique_id))
        if existing is not None:
            await message.answer(
                f"{texts.DUPLICATE_TRACK}\n{_track_caption(existing)}\n{_folder_hint(existing)}"
            )
            return None

    status = await _reply_status(message)

    # Bot API альбом не присылает ни в одном поле Audio, поэтому подсказку берём
    # из имени файла — так канал-хранилище и StoredAudio знают его сразу.
    album_hint = metadata.parse_telegram_audio(
        media_item, file_name=getattr(media_item, "file_name", None)
    ).album

    try:
        stored = await storage.store_from_message(bot, message, album=album_hint)
    except FileTooLargeError as error:
        logger.warning("Пользователь %s: файл слишком большой: %s", user_id, error)
        await _finish(status, message, texts.FILE_TOO_LARGE)
        return None
    except StorageError as error:
        logger.warning("Пользователь %s: ошибка хранилища: %s", user_id, error)
        await _finish(status, message, f"{texts.STORAGE_ERROR}\n\n{escape(str(error))}")
        return None
    except Exception:  # noqa: BLE001 — сбой Telegram не должен ронять обработчик
        logger.exception("Пользователь %s: непредвиденная ошибка при загрузке аудио", user_id)
        await _finish(status, message, texts.ERROR_TRY_AGAIN)
        return None

    if stored.file_unique_id:
        existing = await tracks_repo.get_track_by_unique_id(user_id, str(stored.file_unique_id))
        if existing is not None:
            await _finish(
                status,
                message,
                f"{texts.DUPLICATE_TRACK}\n{_track_caption(existing)}\n{_folder_hint(existing)}",
            )
            return None

    meta = metadata.parse_telegram_audio(
        media_item,
        file_name=stored.file_name or getattr(media_item, "file_name", None),
    )
    source, source_ref = _resolve_source(message)
    album_name = meta.album or stored.album

    try:
        # Исполнителей у трека может быть несколько («A feat. B»): первый —
        # основной (`tracks.artist_id`), остальные попадают в `track_artists`.
        artist_ids = await _ensure_artists(user_id, _artist_names(meta.artist))
        primary_artist_id = artist_ids[0] if artist_ids else None
        album_row = (
            await albums_repo.ensure_album(
                user_id,
                album_name,
                primary_artist_id,
                meta.year,
            )
            if album_name
            else None
        )
        track = await tracks_repo.create_track(
            user_id,
            title=meta.title,
            artist=meta.artist,
            album=album_name,
            duration=meta.duration or stored.duration,
            file_size=stored.file_size,
            mime_type=stored.mime_type,
            file_name=stored.file_name or meta.file_name,
            file_id=stored.file_id,
            file_unique_id=stored.file_unique_id,
            storage_chat_id=stored.chat_id,
            storage_message_id=stored.message_id,
            thumb_file_id=stored.thumb_file_id,
            artist_id=primary_artist_id,
            album_id=album_row["id"] if album_row else None,
            source=source,
            source_ref=source_ref,
            file_type=media.AUDIO_FILE_TYPE,
            genre=meta.genre,
            artist_ids=artist_ids,
        )
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось сохранить трек в БД: %s", user_id, error)
        await _finish(status, message, f"❌ {escape(str(error))}")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Пользователь %s: непредвиденная ошибка при создании трека", user_id)
        await _finish(status, message, texts.ERROR_TRY_AGAIN)
        return None

    logger.info(
        "Пользователь %s загрузил трек #%s (source=%s, исполнителей %s)",
        user_id,
        track.get("id"),
        source,
        len(artist_ids),
    )

    # Ни Telegram, ни имя файла не дают альбом и жанр — читаем теги самого файла.
    if not album_name or not meta.genre:
        track = await _enrich_with_tags(bot, user_id, track, stored, meta.year)

    artist_name = (track.get("artist") or "").strip() or meta.artist
    return track, artist_name, status


async def _finish_batch_item(
    status: Message | None,
    message: Message,
    user_id: int,
    track: dict[str, Any],
    artist_name: str | None,
    user_settings: dict[str, Any],
) -> None:
    """Ответ на файл из пачки: автосортировка (если включена) и короткая строка.

    Вопрос про папку здесь не задаётся: после загрузки всей пачки её разложит
    диалог из `handlers/batch.py`, а нераспределённые треки попадут в его цикл.
    """
    if bool(user_settings.get("auto_sort_enabled")):
        try:
            await autosort.apply_autosort(user_id, track, artist_name=artist_name)
        except Exception:  # noqa: BLE001 — сбой сортировки не должен «терять» трек
            logger.exception(
                "Пользователь %s: ошибка автосортировки трека %s из пачки",
                user_id,
                track.get("id"),
            )
    await _finish(status, message, _batch_item_line(track))


async def _finish_upload(
    status: Message | None,
    message: Message,
    user_id: int,
    track: dict[str, Any],
    artist_name: str | None,
    user_settings: dict[str, Any],
) -> None:
    """Автосортировка либо предложение выбрать папку вручную."""
    caption = _track_caption(track)
    track_id = int(track["id"])

    if bool(user_settings.get("auto_sort_enabled")):
        try:
            result = await autosort.apply_autosort(user_id, track, artist_name=artist_name)
        except Exception:  # noqa: BLE001 — сбой сортировки не должен «терять» трек
            logger.exception(
                "Пользователь %s: ошибка автосортировки трека %s", user_id, track_id
            )
            await _finish(status, message, f"{texts.TRACK_SAVED}\n{caption}")
            return

        folder = result.folder if result.applied else None
        if folder and folder.get("name"):
            header = texts.TRACK_SAVED_TO_FOLDER.format(folder=escape(folder["name"]))
            await _finish(status, message, f"{header}\n{caption}")
            return
        await _finish(
            status,
            message,
            f"{texts.TRACK_SAVED}\n{caption}\n"
            "Исполнителя определить не удалось — трек остался без папки.",
        )
        return

    title = escape(track.get("title") or "Без названия")
    if artist_name:
        try:
            folder, _needs_create = await autosort.suggest_folder(user_id, artist_name)
        except Exception:  # noqa: BLE001
            logger.exception("Пользователь %s: не удалось подобрать папку", user_id)
            folder = None
        question = texts.AUTOSORT_QUESTION.format(title=title, artist=escape(artist_name))
        await _finish(
            status,
            message,
            question + _album_line(track),
            autosort_kb(track_id, folder, artist_name),
        )
        return

    folders = await folders_repo.list_folders(user_id)
    await _finish(
        status,
        message,
        texts.AUTOSORT_NO_ARTIST.format(title=title) + _album_line(track),
        folder_pick_kb(folders[:MAX_PICK_FOLDERS], track_id),
    )


# ---------------------------------------------------------------------------
# Выбор папки для загруженного трека (MoveCB)
# ---------------------------------------------------------------------------


@router.callback_query(MoveCB.filter())
async def handle_move(
    callback: CallbackQuery,
    callback_data: MoveCB,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Переносит трек в папку, оставляет без папки или спрашивает название новой."""
    if callback.from_user is None:
        await ack(callback)
        return

    user_id = int(callback.from_user.id)
    track_id = int(callback_data.track_id)
    folder_id = int(callback_data.folder_id)

    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    if folder_id == FOLDER_NEW:
        await _ask_folder_name(callback, bot, state, track)
        await ack(callback)
        return

    if folder_id in (FOLDER_NONE, 0):
        updated = await tracks_repo.move_track(user_id, track_id, None)
        caption = _track_caption(updated or track)
        await _replace(callback, bot, f"{texts.TRACK_MOVED_TO_ROOT}\n{caption}")
        await ack(callback, "Готово")
        return

    folder = await folders_repo.get_folder(user_id, folder_id)
    if folder is None:
        await ack(callback, texts.FOLDER_NOT_FOUND, alert=True)
        return

    updated = await tracks_repo.move_track(user_id, track_id, folder_id)
    if updated is None:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    logger.info("Пользователь %s: трек %s перенесён в папку %s", user_id, track_id, folder_id)
    header = texts.TRACK_MOVED.format(folder=escape(folder["name"]))
    await _replace(callback, bot, f"{header}\n{_track_caption(updated)}")
    await ack(callback, "Готово")


async def _ask_folder_name(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    track: dict[str, Any],
) -> None:
    """Просит ввести название новой папки, предлагая имя исполнителя одной кнопкой."""
    track_id = int(track["id"])
    artist_name = (track.get("artist") or "").strip()

    await state.set_state(UploadStates.waiting_folder_name)
    await state.update_data(track_id=track_id, suggested_name=artist_name)

    lines = [texts.FOLDER_NAME_PROMPT]
    markup: InlineKeyboardMarkup | None = None
    if artist_name:
        lines.append(f"Можно сразу создать папку «{escape(artist_name)}» кнопкой ниже.")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Создать «{artist_name[:24]}»",
                        callback_data=f"{NEW_FROM_ARTIST_PREFIX}:{track_id}",
                    )
                ]
            ]
        )
    lines.append(texts.CANCEL_HINT)

    await _send_new(callback, bot, "\n".join(lines), markup)


def _is_new_from_artist(data: Any) -> bool:
    """Фильтр собственных callback-данных «создать папку по имени исполнителя»."""
    return isinstance(data, str) and data.startswith(f"{NEW_FROM_ARTIST_PREFIX}:")


@router.callback_query(F.data.func(_is_new_from_artist))
async def handle_new_from_artist(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Создаёт папку с именем исполнителя без ручного ввода названия."""
    if callback.from_user is None or not callback.data:
        await ack(callback)
        return

    parts = callback.data.split(":")
    if len(parts) != 2:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return
    try:
        track_id = int(parts[1])
    except ValueError:
        await ack(callback, texts.ERROR_TRY_AGAIN, alert=True)
        return

    user_id = int(callback.from_user.id)
    track = await tracks_repo.get_track(user_id, track_id)
    if track is None:
        await state.clear()
        await ack(callback, texts.TRACK_NOT_FOUND, alert=True)
        return

    artist_name = (track.get("artist") or "").strip()
    if not artist_name:
        await ack(callback, "У трека нет исполнителя — введите название вручную", alert=True)
        return

    updated = await _assign_folder(user_id, track_id, artist_name)
    await state.clear()
    if updated is None:
        await _replace(callback, bot, texts.ERROR_TRY_AGAIN)
        await ack(callback)
        return

    folder_name = updated.get("folder_name") or artist_name
    header = texts.TRACK_SAVED_TO_FOLDER.format(folder=escape(folder_name))
    await _replace(callback, bot, f"{header}\n{_track_caption(updated)}")
    await ack(callback, "Готово")


# ---------------------------------------------------------------------------
# FSM: ввод названия новой папки
# ---------------------------------------------------------------------------


@router.message(StateFilter(UploadStates.waiting_folder_name), Command("cancel"))
async def on_cancel_folder_name(message: Message, state: FSMContext) -> None:
    """Прерывает ввод названия новой папки (трек остаётся в библиотеке)."""
    await state.clear()
    await message.answer(f"{texts.CANCELLED} Трек остался в библиотеке без папки.")


@router.message(StateFilter(UploadStates.waiting_folder_name), F.text)
async def on_new_folder_name(message: Message, state: FSMContext) -> None:
    """Создаёт папку по введённому названию и переносит в неё трек."""
    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer(f"{texts.CANCELLED} Повторите команду ещё раз.")
        return

    if not text:
        await message.answer(texts.NAME_EMPTY)
        return
    if len(text) > MAX_FOLDER_NAME_LENGTH:
        await message.answer(texts.NAME_TOO_LONG)
        return

    user_id = int(message.from_user.id)
    data = await state.get_data()
    try:
        track_id = int(data.get("track_id") or 0)
    except (TypeError, ValueError):
        track_id = 0
    if track_id <= 0:
        await state.clear()
        await message.answer(texts.TRACK_NOT_FOUND)
        return

    try:
        track = await autosort.assign_to_folder(user_id, track_id, text)
    except ValidationError as error:
        await message.answer(f"❌ {escape(str(error))}\nВведите другое название.")
        return
    except MusicBoxError as error:
        logger.warning("Пользователь %s: не удалось создать папку «%s»: %s", user_id, text, error)
        await state.clear()
        await message.answer(f"❌ {escape(str(error))}")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Пользователь %s: не удалось создать папку «%s»", user_id, text)
        await state.clear()
        await message.answer(texts.ERROR_TRY_AGAIN)
        return

    await state.clear()
    if track is None:
        await message.answer(texts.TRACK_NOT_FOUND)
        return

    folder_name = track.get("folder_name") or text
    logger.info(
        "Пользователь %s: трек %s перенесён в новую папку «%s»", user_id, track_id, folder_name
    )
    header = texts.TRACK_SAVED_TO_FOLDER.format(folder=escape(folder_name))
    await message.answer(f"{header}\n{_track_caption(track)}")


# ---------------------------------------------------------------------------
# Вспомогательные операции
# ---------------------------------------------------------------------------


async def _assign_folder(user_id: int, track_id: int, folder_name: str) -> dict[str, Any] | None:
    """Создаёт папку при необходимости и переносит в неё трек."""
    try:
        return await autosort.assign_to_folder(user_id, track_id, folder_name)
    except MusicBoxError as error:
        logger.warning(
            "Пользователь %s: не удалось положить трек %s в папку «%s»: %s",
            user_id,
            track_id,
            folder_name,
            error,
        )
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "Пользователь %s: сбой при переносе трека %s в папку «%s»",
            user_id,
            track_id,
            folder_name,
        )
        return None


async def _replace(callback: CallbackQuery, bot: Bot, text: str) -> None:
    """Заменяет сообщение с клавиатурой на итоговый результат."""
    message = callback.message
    if message is not None:
        await safe_edit(message, text, None)
        return
    if callback.from_user is not None:
        await bot.send_message(callback.from_user.id, text)


async def _send_new(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет новое сообщение в чат, из которого пришёл callback."""
    message = callback.message
    if isinstance(message, Message):
        await message.answer(text, reply_markup=markup)
        return
    if callback.from_user is not None:
        await bot.send_message(callback.from_user.id, text, reply_markup=markup)
