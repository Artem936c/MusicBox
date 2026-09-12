"""Медиафайлы: определение типа, сохранение в канал-хранилище и отправка пользователю.

V1 (`backend.services.storage`) умеет работать только с аудио. Этот модуль расширяет
хранилище на документы, видео, видеосообщения («кружочки») и голосовые:

* определяет тип файла в сообщении (`detect_file_type`);
* отправляет файл в приватный канал подходящим методом Bot API и берёт `file_id`
  ИЗ сообщения, которое вернул канал (`store_media`);
* отдаёт сохранённый файл пользователю тем же методом, каким он был сохранён
  (`send_media_to_user`).

Аудио отправляется КАК ЕСТЬ, с исходным `mime_type` — никакой конвертации.
Вся низкоуровневая логика (проверка канала, повтор после флуд-контроля, перевод
ошибок Telegram на русский, разбор аудио из сообщения) переиспользуется из
`backend.services.storage`, а не дублируется здесь.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import Document, Message

from backend.errors import StorageError
from backend.services import storage

# Внутренние помощники хранилища: используем их напрямую, чтобы не плодить
# вторую реализацию проверки канала, повторов и перевода ошибок Telegram.
from backend.services.storage import (
    _CAPTION_LIMIT,
    StoredAudio,
    _call_with_retry,
    _channel_error,
    _clean_text,
    _describe_telegram_error,
    _is_audio_document,
    _require_channel_id,
    _safe_int,
    _thumb_file_id,
    _title_from_file_name,
    _too_large_error,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_TYPES",
    "DEFAULT_FILE_TYPE",
    "AUDIO_FILE_TYPE",
    "FILE_TYPE_LABELS",
    "FILE_TYPE_ICONS",
    "StoredMedia",
    "detect_file_type",
    "is_audio_document",
    "normalize_file_type",
    "is_audio_type",
    "store_media",
    "send_media_to_user",
]

#: Допустимые значения колонки `tracks.file_type` (контракт V2, п. 1.3).
FILE_TYPES: Final[tuple[str, ...]] = ("audio", "document", "video", "video_note", "voice")

#: Тип по умолчанию: всё, что не распознано, считаем аудио (совместимость с V1).
DEFAULT_FILE_TYPE: Final[str] = "audio"
AUDIO_FILE_TYPE: Final[str] = "audio"

#: Русские названия типов файлов (раздел «Другое» в боте и Mini App).
FILE_TYPE_LABELS: Final[dict[str, str]] = {
    "audio": "Аудио",
    "document": "Документ",
    "video": "Видео",
    "video_note": "Видеосообщение",
    "voice": "Голосовое сообщение",
}

#: Значки типов файлов для списков и кнопок.
FILE_TYPE_ICONS: Final[dict[str, str]] = {
    "audio": "🎵",
    "document": "📄",
    "video": "🎬",
    "video_note": "⭕",
    "voice": "🎤",
}

#: Названия по умолчанию, когда у файла нет ни имени, ни подписи.
_DEFAULT_TITLES: Final[dict[str, str]] = {
    "audio": "Без названия",
    "document": "Файл",
    "video": "Видео",
    "video_note": "Видеосообщение",
    "voice": "Голосовое сообщение",
}

#: Mime-тип, который подставляем, если Telegram его не прислал.
_FALLBACK_MIME: Final[dict[str, str]] = {
    "video_note": "video/mp4",
    "voice": "audio/ogg",
}

#: Порядок распознавания типа: специфичные поля раньше документа,
#: потому что у анимаций и части видео Telegram заполняет и `document`.
_DETECT_ORDER: Final[tuple[str, ...]] = ("audio", "voice", "video_note", "video", "document")

#: Ограничение длины названия файла (как в `tracks.title`).
_TITLE_LIMIT: Final[int] = 256


# ---------------------------------------------------------------------------
# Результат сохранения
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredMedia:
    """Результат сохранения любого файла в канале-хранилище.

    Повторяет поля :class:`backend.services.storage.StoredAudio` и добавляет
    `file_type` — значение колонки `tracks.file_type`.
    """

    file_type: str
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

    @property
    def is_audio(self) -> bool:
        """Попадёт ли файл в раздел «Треки»."""
        return self.file_type == AUDIO_FILE_TYPE

    @classmethod
    def from_stored_audio(
        cls,
        stored: StoredAudio,
        *,
        file_type: str = AUDIO_FILE_TYPE,
        title: str | None = None,
    ) -> StoredMedia:
        """Оборачивает результат `storage.store_from_message` в StoredMedia."""
        return cls(
            file_type=normalize_file_type(file_type),
            file_id=stored.file_id,
            file_unique_id=stored.file_unique_id,
            message_id=stored.message_id,
            chat_id=stored.chat_id,
            title=_clean_text(title, _TITLE_LIMIT) or stored.title,
            artist=stored.artist,
            album=stored.album,
            duration=stored.duration,
            file_size=stored.file_size,
            mime_type=stored.mime_type,
            file_name=stored.file_name,
            thumb_file_id=stored.thumb_file_id,
        )

    def as_stored_audio(self) -> StoredAudio:
        """Приводит результат к StoredAudio — для кода V1, который ждёт именно его."""
        return StoredAudio(
            file_id=self.file_id,
            file_unique_id=self.file_unique_id,
            message_id=self.message_id,
            chat_id=self.chat_id,
            title=self.title,
            artist=self.artist,
            album=self.album,
            duration=self.duration,
            file_size=self.file_size,
            mime_type=self.mime_type,
            file_name=self.file_name,
            thumb_file_id=self.thumb_file_id,
        )


# ---------------------------------------------------------------------------
# Определение типа файла
# ---------------------------------------------------------------------------


def is_audio_document(document: Document | None) -> bool:
    """Похож ли документ на аудиофайл (mime-тип `audio/*` или знакомое расширение).

    Тонкая обёртка над проверкой из `backend.services.storage`, чтобы у бота и API
    был публичный вход и обе части кода считали аудио одинаково.
    """
    return _is_audio_document(document)


def normalize_file_type(value: Any) -> str:
    """Приводит значение к одному из :data:`FILE_TYPES` (иначе — «audio»)."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in FILE_TYPES:
            return candidate
        if candidate:
            logger.debug("Неизвестный тип файла %r — считаю его аудио", value)
    return DEFAULT_FILE_TYPE


def is_audio_type(value: Any) -> bool:
    """Относится ли тип файла к разделу «Треки»."""
    return normalize_file_type(value) == AUDIO_FILE_TYPE


def detect_file_type(message: Message | None) -> str | None:
    """Определяет тип вложения в сообщении.

    Возвращает один из :data:`FILE_TYPES` либо None, если файла в сообщении нет.
    Документ с аудио внутри (mp3, присланный «файлом») считается аудио — он должен
    попасть в раздел «Треки», а не в «Другое».
    """
    if message is None:
        return None

    for file_type in _DETECT_ORDER:
        media = getattr(message, file_type, None)
        if media is None:
            continue
        if file_type == "document" and is_audio_document(media):
            return AUDIO_FILE_TYPE
        return file_type
    return None


def _message_media(message: Message, file_type: str) -> Any:
    """Объект вложения нужного типа из сообщения пользователя."""
    if file_type == AUDIO_FILE_TYPE:
        # У аудио два источника: поле `audio` и документ с аудио-mime.
        return storage._extract_audio(message)
    return getattr(message, file_type, None)


def _channel_media(sent: Message, expected: str) -> tuple[str, Any]:
    """Вложение из сообщения, которое вернул канал (`file_id` берём только отсюда).

    Обычно тип совпадает с отправленным, но Telegram вправе вернуть, например,
    документ вместо видео — тогда сохраняем фактический тип, а не ожидаемый.
    """
    media = getattr(sent, expected, None)
    if media is not None and getattr(media, "file_id", None):
        return expected, media

    for file_type in _DETECT_ORDER:
        candidate = getattr(sent, file_type, None)
        if candidate is not None and getattr(candidate, "file_id", None):
            logger.info(
                "Канал вернул файл типа %s вместо %s — сохраняю фактический тип",
                file_type,
                expected,
            )
            return file_type, candidate

    raise StorageError(
        "Telegram не вернул файл из канала-хранилища. Проверьте права бота в канале "
        "и повторите попытку."
    )


# ---------------------------------------------------------------------------
# Сохранение в канал
# ---------------------------------------------------------------------------


def _send_to_channel_factory(
    bot: Bot,
    channel_id: int,
    file_type: str,
    file_id: str,
    *,
    caption: str | None,
    duration: int,
    length: int,
) -> Callable[[], Awaitable[Message]]:
    """Готовит вызов Bot API, подходящий типу файла."""
    if file_type == "document":
        return lambda: bot.send_document(
            chat_id=channel_id,
            document=file_id,
            caption=caption,
            parse_mode=None,
            disable_notification=True,
        )
    if file_type == "video":
        return lambda: bot.send_video(
            chat_id=channel_id,
            video=file_id,
            duration=duration or None,
            caption=caption,
            parse_mode=None,
            disable_notification=True,
        )
    if file_type == "video_note":
        # У видеосообщений Bot API не поддерживает подпись — она просто игнорируется.
        return lambda: bot.send_video_note(
            chat_id=channel_id,
            video_note=file_id,
            duration=duration or None,
            length=length or None,
            disable_notification=True,
        )
    if file_type == "voice":
        return lambda: bot.send_voice(
            chat_id=channel_id,
            voice=file_id,
            duration=duration or None,
            caption=caption,
            parse_mode=None,
            disable_notification=True,
        )
    raise StorageError(f"Тип файла «{file_type}» не поддерживается хранилищем.")


async def _send_to_channel(
    bot: Bot,
    file_type: str,
    file_id: str,
    *,
    caption: str | None,
    duration: int,
    length: int,
) -> Message:
    """Отправляет не-аудио файл в канал-хранилище и возвращает сообщение из канала."""
    channel_id = _require_channel_id()
    factory = _send_to_channel_factory(
        bot,
        channel_id,
        file_type,
        file_id,
        caption=caption,
        duration=duration,
        length=length,
    )
    description = f"отправка файла ({FILE_TYPE_LABELS.get(file_type, file_type)}) в канал-хранилище"
    try:
        return await _call_with_retry(factory, description=description)
    except StorageError:
        raise
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
        logger.warning(
            "Не удалось сохранить файл (%s) в канале: %s", file_type, _describe_telegram_error(exc)
        )
        raise _channel_error(exc) from exc
    except TelegramAPIError as exc:
        logger.exception("Ошибка Telegram при сохранении файла (%s) в канале", file_type)
        raise _channel_error(exc) from exc


async def store_media(
    bot: Bot,
    message: Message,
    *,
    title: str | None = None,
    album: str | None = None,
) -> StoredMedia:
    """Сохраняет любой файл из сообщения в канал-хранилище.

    Аудио (в том числе присланное документом) уходит через
    `storage.store_from_message` — без конвертации, с исходным mime-типом.
    Документы, видео, «кружочки» и голосовые отправляются в канал своим методом
    Bot API; `file_id`, `file_unique_id` и размер берутся ИЗ сообщения в канале,
    потому что у копии в канале идентификаторы свои.

    `title` — необязательное название (например, введённое пользователем),
    `album` — подсказка для аудио (Bot API альбом не присылает).
    Бросает :class:`~backend.errors.StorageError` с русским текстом при неудаче.
    """
    file_type = detect_file_type(message)
    if file_type is None:
        raise StorageError(
            "В сообщении нет файла. Пришлите или перешлите боту аудио, документ, "
            "видео, кружочек или голосовое сообщение."
        )

    if file_type == AUDIO_FILE_TYPE:
        stored = await storage.store_from_message(bot, message, album=album)
        return StoredMedia.from_stored_audio(stored, title=title)

    media = _message_media(message, file_type)
    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise StorageError(
            "Не удалось прочитать файл из сообщения. Попробуйте отправить его снова."
        )

    file_name = getattr(media, "file_name", None)
    caption = _clean_text(getattr(message, "caption", None), _CAPTION_LIMIT)
    display_title = (
        _clean_text(title, _TITLE_LIMIT)
        or _title_from_file_name(file_name)
        or _clean_text(caption, _TITLE_LIMIT)
        or _DEFAULT_TITLES[file_type]
    )
    duration = _safe_int(getattr(media, "duration", None), 0)
    length = _safe_int(getattr(media, "length", None), 0)

    sent = await _send_to_channel(
        bot,
        file_type,
        str(file_id),
        caption=caption or _clean_text(display_title, _CAPTION_LIMIT),
        duration=duration,
        length=length,
    )

    stored_type, stored_media = _channel_media(sent, file_type)
    chat = getattr(sent, "chat", None)
    stored_file_name = getattr(stored_media, "file_name", None) or file_name
    result = StoredMedia(
        file_type=normalize_file_type(stored_type),
        file_id=str(getattr(stored_media, "file_id")),
        file_unique_id=getattr(stored_media, "file_unique_id", None),
        message_id=getattr(sent, "message_id", None),
        chat_id=getattr(chat, "id", None),
        title=(
            _clean_text(title, _TITLE_LIMIT)
            or _title_from_file_name(stored_file_name)
            or display_title
        ),
        artist=None,
        album=_clean_text(album, _TITLE_LIMIT),
        duration=_safe_int(getattr(stored_media, "duration", None), duration),
        file_size=_safe_int(
            getattr(stored_media, "file_size", None),
            _safe_int(getattr(media, "file_size", None), 0),
        ),
        mime_type=(
            getattr(stored_media, "mime_type", None)
            or getattr(media, "mime_type", None)
            or _FALLBACK_MIME.get(stored_type)
        ),
        file_name=stored_file_name,
        thumb_file_id=_thumb_file_id(stored_media),
    )
    logger.info(
        "Файл сохранён в канале: тип=%s message_id=%s file_unique_id=%s",
        result.file_type,
        result.message_id,
        result.file_unique_id,
    )
    return result


# ---------------------------------------------------------------------------
# Отправка пользователю
# ---------------------------------------------------------------------------


def _send_to_user_factory(
    bot: Bot,
    chat_id: int,
    file_type: str,
    file_id: str,
    *,
    caption: str | None,
    duration: int,
    reply_markup: Any,
) -> Callable[[], Awaitable[Message]]:
    """Готовит вызов Bot API для отправки файла пользователю."""
    if file_type == "document":
        return lambda: bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=caption,
            reply_markup=reply_markup,
        )
    if file_type == "video":
        return lambda: bot.send_video(
            chat_id=chat_id,
            video=file_id,
            duration=duration or None,
            caption=caption,
            reply_markup=reply_markup,
        )
    if file_type == "video_note":
        if caption:
            logger.debug("Видеосообщение не поддерживает подпись — отправляю без неё")
        return lambda: bot.send_video_note(
            chat_id=chat_id,
            video_note=file_id,
            duration=duration or None,
            reply_markup=reply_markup,
        )
    if file_type == "voice":
        return lambda: bot.send_voice(
            chat_id=chat_id,
            voice=file_id,
            duration=duration or None,
            caption=caption,
            reply_markup=reply_markup,
        )
    raise StorageError(f"Тип файла «{file_type}» не поддерживается хранилищем.")


def _user_send_error(exc: BaseException, track: dict) -> StorageError:
    """Переводит ошибку отправки файла пользователю в русский StorageError."""
    text = _describe_telegram_error(exc)
    low = text.lower()
    logger.warning("Не удалось отправить файл %s: %s", track.get("id"), text)

    if "wrong file identifier" in low or "wrong remote file identifier" in low:
        return StorageError(
            "Не удалось отправить файл: он больше недоступен в Telegram. "
            "Загрузите его в хранилище заново."
        )
    if "too big" in low or "too large" in low:
        return _too_large_error()
    if isinstance(exc, TelegramForbiddenError):
        return StorageError("Не удалось отправить файл: бот заблокирован или чат недоступен.")
    if isinstance(exc, TelegramNetworkError):
        return StorageError(
            "Не удалось связаться с Telegram при отправке файла. Повторите попытку."
        )
    return StorageError(f"Telegram не смог отправить файл: {text}")


async def send_media_to_user(
    bot: Bot,
    chat_id: int,
    track: dict,
    *,
    caption: str | None = None,
    reply_markup: Any = None,
) -> Message:
    """Отправляет пользователю сохранённый файл, выбирая метод по `track['file_type']`.

    Аудио уходит через `storage.send_track_to_user` — как есть, с исполнителем,
    названием и длительностью. Остальные типы отправляются соответствующим методом
    Bot API по сохранённому `file_id`; файл не скачивается, поэтому лимит 20 МБ
    на скачивание здесь не действует.
    """
    file_type = normalize_file_type(track.get("file_type"))
    if file_type == AUDIO_FILE_TYPE:
        return await storage.send_track_to_user(
            bot, chat_id, track, caption=caption, reply_markup=reply_markup
        )

    file_id = track.get("file_id")
    if not file_id:
        raise StorageError(
            "У этого файла нет копии в хранилище. Загрузите его заново, чтобы открыть."
        )

    factory = _send_to_user_factory(
        bot,
        int(chat_id),
        file_type,
        str(file_id),
        caption=_clean_text(caption, _CAPTION_LIMIT),
        duration=_safe_int(track.get("duration"), 0),
        reply_markup=reply_markup,
    )
    try:
        return await _call_with_retry(factory, description="отправка файла пользователю")
    except StorageError:
        raise
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
        raise _user_send_error(exc, track) from exc
    except TelegramAPIError as exc:
        logger.exception("Ошибка Telegram при отправке файла %s", track.get("id"))
        raise _user_send_error(exc, track) from exc
