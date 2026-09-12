"""Разбор и нормализация метаданных аудиофайлов.

Модуль сознательно не зависит от слоя БД (`backend.db.*`): его импортируют
репозитории (для `normalize_name`), поэтому обратный импорт создал бы цикл.
Здесь нет ни обращений к сети, ни блокирующих операций — единственная
потенциально долгая операция (`extract_tags`) выполняется в `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "AudioMetadata",
    "DEFAULT_TITLE",
    "NO_DURATION",
    "AUDIO_EXTENSIONS",
    "normalize_name",
    "clean_title",
    "split_artists",
    "primary_artist",
    "guess_from_filename",
    "parse_telegram_audio",
    "extract_tags",
    "format_duration",
]

# Название трека, если определить его не удалось.
DEFAULT_TITLE = "Без названия"
# Подпись неизвестной длительности (совпадает с фронтендом).
NO_DURATION = "—"

# Расширения, которые считаем аудио и срезаем из названий.
AUDIO_EXTENSIONS = frozenset(
    {
        "mp3", "m4a", "m4b", "m4p", "aac", "flac", "wav", "wave", "ogg", "oga",
        "opus", "wma", "aiff", "aif", "aifc", "alac", "ape", "mpc", "mka",
        "webm", "mp4", "amr", "3gp", "dsf", "dff", "spx", "ac3", "mid", "midi",
    }
)

# --- Служебные регулярные выражения -----------------------------------------

# Невидимые символы: мягкий перенос, нулевой ширины, метки направления, BOM.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u202a-\u202e"
    "\u2060-\u2064\u206a-\u206f\ufeff]"
)
# Управляющие символы (кроме привычных пробельных \t \n \r).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
# Пробельные символы, которые стоит привести к обычному пробелу.
_SPACE_CHARS = "\u00a0\u1680\u202f\u205f\u3000" + "".join(
    chr(code) for code in range(0x2000, 0x200b)
)
_NBSP_TABLE = {ord(char): " " for char in _SPACE_CHARS}

# Скобочная группа без вложенности.
_BRACKET_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")

# Мусор, который вырезаем целиком, если он занимает всю скобку.
_JUNK_INNER_PATTERNS = (
    r"(?:офиц\w*|official|offical|officiel)\b.*",
    r"(?:full\s*)?(?:hd|hq|uhd|4k|8k|1080p?|720p?|480p?|360p?)",
    r"(?:lyrics?|lyrics?\s*video|lyric\s*video|текст\s*песни|текст|караоке|karaoke)",
    r"(?:music\s*)?video(?:\s*clip)?",
    r"(?:audio|аудио|sound|звук|soundtrack\s*version)",
    r"mv|visuali[sz]er|clip|клип|видеоклип",
    r"премьер\w*(?:\s+[\w\-]+)*",
    r"\d{2,4}\s*(?:kbps|kbit|kb/s|кбит(?:/с|с)?)",
    r"(?:mp3|flac|wav|m4a|aac|ogg|opus)(?:\s*\d{2,4}\s*(?:kbps|kbit))?",
    r"remaster\w*(?:\s*\d{4})?|\d{4}\s*remaster\w*",
    r"[\w\-]+\.(?:ru|com|net|org|fm|me|info|biz|cc|club|online|site|su|ua|kz|by)(?:/\S*)?",
    r"(?:скачать|бесплатно|download|free\s*download)\b.*",
    r"(?:explicit|clean|censored|цензура|без\s*цензуры)",
    r"(?:x?minus|минус(?:овка)?\s*версия)",
)
_JUNK_INNER_RE = re.compile(
    r"^(?:" + "|".join(_JUNK_INNER_PATTERNS) + r")$",
    re.IGNORECASE,
)

# Тот же мусор, но написанный без скобок.
_BARE_JUNK_RE = re.compile(
    r"(?:[\s\-–—|·•]+)?\b(?:"
    r"official\s+(?:music\s+)?(?:video|audio)|"
    r"official\s+lyrics?\s*video|"
    r"lyrics?\s*video|"
    r"music\s*video|"
    r"audio\s*hq|"
    r"hd\s*quality|"
    r"\d{2,4}\s*(?:kbps|kbit|кбит(?:/с)?)"
    r")\b",
    re.IGNORECASE,
)

# Пустые скобки, оставшиеся после вырезания мусора.
_EMPTY_BRACKETS_RE = re.compile(r"[\(\[\{]\s*[\)\]\}]")
# Порядковый номер в начале имени файла: «01. », «1) », «03 - ».
# Тире требует пробела после себя, иначе пострадали бы имена вроде «2-Pac».
_LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*(?:[.)\]]\s*|[-–—_]\s+)")
# Разделитель «исполнитель — название» с пробелами.
_DASH_SPACED_RE = re.compile(r"\s+[-–—]+\s+")
# Длинное тире без пробелов («Artist—Title»).
_DASH_LONG_RE = re.compile(r"\s*[–—]\s*")

# Разделители исполнителей.
_SEP = "\x00"
_FEAT_RE = re.compile(
    r"[\(\[\{]?\s*\b(?:featuring|feat|ft|при\s+участии|с\s+участием)\b\.?\s*",
    re.IGNORECASE,
)
_WORD_SEP_RE = re.compile(r"\s+(?:vs|versus|x|х)\b\.?\s+", re.IGNORECASE)
_SLASH_SPACED_RE = re.compile(r"\s+/\s+")
# Голый слэш режем только между «длинными» словами, чтобы не разрушить «AC/DC».
_SLASH_BARE_RE = re.compile(r"(?<=\w\w\w)/(?=\w\w\w)")
_HARD_SEP_RE = re.compile(r"\s*[,;&]\s*")
_PART_STRIP_CHARS = " \t\r\n.,;:&/\\-–—_|·•()[]{}\"'«»"

# Год в тегах: «2001», «2001-05-03», «2001/05».
_YEAR_RE = re.compile(r"(\d{4})")
# Год в скобках рядом с названием альбома: «Группа крови (1988)», «[2001]».
_BRACKET_YEAR_RE = re.compile(r"[\(\[\{]\s*(\d{4})\s*[\)\]\}]")

# Пометки версии/ремикса: такую часть имени файла альбомом не считаем
# («Кино - Кукушка - Live» — это не альбом «Кукушка»).
_VERSION_MARK_RE = re.compile(
    r"\b(?:remix|rmx|mix|edit|version|cover|live|acoustic|instrumental|"
    r"remaster\w*|radio|extended|club|dub|slowed|reverb|sped\s*up|speed\s*up|"
    r"nightcore|bootleg|mashup|demo|snippet|prod|bonus|intro|outro|"
    r"ремикс|версия|кавер|лайв|акустика|минус(?:овка)?|бонус)\b",
    re.IGNORECASE,
)

# Состояние мягкого импорта mutagen (None — ещё не проверяли).
_MUTAGEN_PRESENT: bool | None = None


@dataclass(slots=True)
class AudioMetadata:
    """Метаданные аудиофайла, приведённые к единому виду."""

    title: str
    artist: str | None
    album: str | None
    duration: int
    year: int | None = None
    genre: str | None = None
    file_name: str | None = None


# --- Базовая очистка строк ---------------------------------------------------


def _sanitize(value: Any) -> str:
    """Убрать управляющие и невидимые символы, схлопнуть пробелы."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.translate(_NBSP_TABLE)
    text = _CONTROL_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def normalize_name(value: str | None) -> str:
    """Ключ для сравнения имён: casefold, без лишних пробелов, «ё» → «е».

    Используется для полей `normalized_name` / `normalized_title` в БД,
    поэтому результат должен быть стабильным между запусками.
    """
    text = _sanitize(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _SPACE_RE.sub(" ", text).strip()
    text = text.casefold()
    # После casefold «Ё» уже стала «ё» — достаточно одной замены.
    text = text.replace("ё", "е")
    return text


def _strip_extension(value: str) -> str:
    """Отрезать расширение файла, если оно похоже на аудио-расширение."""
    base, dot, ext = value.rpartition(".")
    if not dot or not base:
        return value
    ext_clean = ext.strip().lower()
    if len(ext_clean) <= 5 and ext_clean in AUDIO_EXTENSIONS:
        return base
    return value


def _drop_junk_brackets(text: str) -> str:
    """Вырезать скобочные пометки вида «[Official Video]», «(lyrics)»."""

    def _replace(match: re.Match[str]) -> str:
        inner = _SPACE_RE.sub(" ", match.group(1)).strip(" .-–—_")
        if not inner:
            return " "
        if _JUNK_INNER_RE.match(inner):
            return " "
        return match.group(0)

    result = text
    # Несколько проходов — на случай вложенных скобок «((official))».
    for _ in range(3):
        previous = result
        result = _BRACKET_RE.sub(_replace, result)
        if result == previous:
            break
    return result


def clean_title(value: str | None) -> str:
    """Привести название к читаемому виду.

    Убирает расширение файла, заменяет подчёркивания пробелами, вырезает
    рекламный мусор («[official video]», «(Official Audio)», «(lyrics)»,
    «320kbps», адреса сайтов) и лидирующий номер трека.
    """
    text = _sanitize(value)
    if not text:
        return ""
    text = _strip_extension(text)
    text = text.replace("_", " ")
    text = _drop_junk_brackets(text)
    text = _BARE_JUNK_RE.sub(" ", text)
    text = _EMPTY_BRACKETS_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()

    without_number = _LEADING_NUMBER_RE.sub("", text, count=1).strip()
    if without_number:
        text = without_number

    text = text.strip(" \t-–—_|·•")
    text = _SPACE_RE.sub(" ", text).strip()
    return text


# --- Исполнители -------------------------------------------------------------


def split_artists(performer: str | None) -> list[str]:
    """Разбить строку исполнителей на отдельные имена.

    Разделители: «feat.», «feat», «ft.», «ft», «&», «,», «;», « x », « vs »,
    « vs. », «/» (голый слэш — только между длинными словами, чтобы не
    разрушить «AC/DC»). Порядок сохраняется, дубликаты убираются.
    """
    text = _sanitize(performer)
    if not text:
        return []

    marked = _FEAT_RE.sub(_SEP, text)
    marked = _WORD_SEP_RE.sub(_SEP, marked)
    marked = _SLASH_SPACED_RE.sub(_SEP, marked)
    marked = _SLASH_BARE_RE.sub(_SEP, marked)
    marked = _HARD_SEP_RE.sub(_SEP, marked)

    result: list[str] = []
    seen: set[str] = set()
    for raw_part in marked.split(_SEP):
        part = clean_title(raw_part.strip(_PART_STRIP_CHARS))
        part = part.strip(_PART_STRIP_CHARS)
        if not part or part.isdigit():
            continue
        key = normalize_name(part)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result


def primary_artist(performer: str | None) -> str | None:
    """Основной исполнитель (первый в списке) или None."""
    artists = split_artists(performer)
    if not artists:
        return None
    return artists[0]


# --- Имя файла ---------------------------------------------------------------


def guess_from_filename(file_name: str | None) -> tuple[str | None, str | None]:
    """Достать (исполнитель, название) из имени файла.

    Понимает «Artist - Title.mp3», «01. Artist - Title.mp3», «Artist — Title»,
    «Artist_-_Title.flac». Если разделителя нет — исполнитель None,
    название — очищенное имя файла.
    """
    raw = _sanitize(file_name)
    if not raw:
        return None, None

    base = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    base = _strip_extension(base)
    base = base.replace("_", " ")
    base = _SPACE_RE.sub(" ", base).strip()
    if not base:
        return None, None

    # Номер трека убираем до разбора, иначе «01 - Artist - Title» распадётся неверно.
    without_number = _LEADING_NUMBER_RE.sub("", base, count=1).strip()
    if without_number:
        base = without_number

    artist_part: str | None = None
    title_part: str = base

    parts = _DASH_SPACED_RE.split(base, maxsplit=1)
    if len(parts) == 2:
        artist_part, title_part = parts[0], parts[1]
    else:
        long_dash = _DASH_LONG_RE.split(base, maxsplit=1)
        if len(long_dash) == 2:
            artist_part, title_part = long_dash[0], long_dash[1]
        elif base.count("-") == 1:
            left, right = base.split("-", 1)
            if len(left.strip()) >= 2 and len(right.strip()) >= 2:
                artist_part, title_part = left, right

    artist = clean_title(artist_part) if artist_part else ""
    title = clean_title(title_part)

    if artist.isdigit():
        artist = ""
    if not title:
        # Разделитель нашёлся, но правая часть пустая — берём всё имя целиком.
        title = clean_title(base)
        artist = ""

    return (artist or None, title or None)


def _split_year(value: str) -> tuple[str, int | None]:
    """Отделить год в скобках: «Группа крови (1988)» → («Группа крови», 1988)."""
    match = _BRACKET_YEAR_RE.search(value)
    if not match:
        return value, None
    year = _parse_year(match.group(1))
    if year is None:
        return value, None
    rest = f"{value[: match.start()]} {value[match.end():]}"
    rest = _SPACE_RE.sub(" ", rest).strip(" \t-–—_|·•")
    return (rest or value), year


def _guess_album_from_filename(file_name: str | None) -> tuple[str | None, str | None]:
    """Достать (альбом, название) из имени файла с явной схемой альбома.

    Понимает «Исполнитель - Альбом - Название.mp3» и «Исполнитель - Альбом -
    01 - Название.mp3». Если схема не распознана (обычное «Исполнитель -
    Название») либо средняя часть похожа на пометку версии («Live», «Remix»),
    возвращает (None, None) — тогда альбом остаётся неизвестным.
    """
    raw = _sanitize(file_name)
    if not raw:
        return None, None

    base = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    base = _strip_extension(base)
    base = base.replace("_", " ")
    base = _SPACE_RE.sub(" ", base).strip()
    if not base:
        return None, None

    without_number = _LEADING_NUMBER_RE.sub("", base, count=1).strip()
    if without_number:
        base = without_number

    parts = [part.strip() for part in _DASH_SPACED_RE.split(base)]
    if len(parts) == 4 and parts[2].isdigit():
        # «Исполнитель - Альбом - 01 - Название»: номер трека посередине.
        parts = [parts[0], parts[1], parts[3]]
    if len(parts) != 3:
        return None, None

    album = clean_title(parts[1])
    title = clean_title(parts[2])
    if not album or not title or title.isdigit():
        return None, None
    if len(album) < 2 or album.isdigit():
        return None, None
    # Пометка версии в любой из частей — значит это «Исполнитель - Название -
    # Live/Remix», а не альбом. Лучше не угадать альбом, чем выдумать его.
    if _VERSION_MARK_RE.search(album) or _VERSION_MARK_RE.search(title):
        return None, None
    return album, title


# --- Telegram ----------------------------------------------------------------


def _attr_text(obj: Any, name: str) -> str:
    """Прочитать строковый атрибут aiogram-объекта (Audio/Document)."""
    value = getattr(obj, name, None)
    if value is None:
        return ""
    return _sanitize(value)


def _attr_int(obj: Any, name: str) -> int:
    """Прочитать целочисленный атрибут; при неудаче — 0."""
    value = getattr(obj, name, None)
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.debug("Не удалось прочитать поле %s=%r как число", name, value)
        return 0
    return number if number > 0 else 0


def parse_telegram_audio(audio: Any, file_name: str | None = None) -> AudioMetadata:
    """Собрать метаданные из объекта `aiogram.types.Audio` или `Document`.

    У `Document` нет полей title/performer/duration — тогда работает разбор
    имени файла. Если и он ничего не дал, название = «Без названия».

    Альбом и год Bot API не присылает (у `Audio` есть только title/performer/
    duration/file_name), поэтому их берём из имени файла, когда там явно
    записана схема «Исполнитель - Альбом - Название (1988)». Настоящие
    ID3-теги читает `extract_tags` по скачанному файлу.
    """
    resolved_name = _sanitize(file_name) or _attr_text(audio, "file_name")
    guessed_artist, guessed_title = guess_from_filename(resolved_name)
    album, album_title = _guess_album_from_filename(resolved_name)

    year: int | None = None
    if album:
        album, year = _split_year(album)
        album = clean_title(album) or None

    title = clean_title(_attr_text(audio, "title"))
    if not title:
        # Если альбом распознан, название — последняя часть имени файла,
        # иначе `guess_from_filename` оставил бы альбом внутри названия.
        title = album_title or guessed_title or ""
    if not title:
        title = DEFAULT_TITLE
        logger.debug("Название трека не определено, используем «%s»", DEFAULT_TITLE)

    performer = clean_title(_attr_text(audio, "performer"))
    artist = performer or guessed_artist or None

    duration = _attr_int(audio, "duration")

    return AudioMetadata(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        year=year,
        genre=None,
        file_name=resolved_name or None,
    )


# --- Теги из файла (mutagen, опционально) ------------------------------------


def _mutagen_installed() -> bool:
    """Проверить наличие mutagen один раз за процесс."""
    global _MUTAGEN_PRESENT
    if _MUTAGEN_PRESENT is None:
        try:
            _MUTAGEN_PRESENT = importlib.util.find_spec("mutagen") is not None
        except (ImportError, ValueError):
            _MUTAGEN_PRESENT = False
        if not _MUTAGEN_PRESENT:
            logger.info("Библиотека mutagen не установлена — теги из файлов не читаются")
    return _MUTAGEN_PRESENT


def _first_tag(tags: Any, *keys: str) -> str:
    """Первое непустое значение тега по списку возможных ключей."""
    if tags is None:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:  # noqa: BLE001 — форматы тегов ведут себя по-разному
            value = None
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = next((item for item in value if item), None)
        if value is None:
            continue
        text = _sanitize(value)
        if text:
            return text
    return ""


def _parse_year(value: str) -> int | None:
    """Год из строки даты («2001-05-03» → 2001)."""
    if not value:
        return None
    match = _YEAR_RE.search(value)
    if not match:
        return None
    year = int(match.group(1))
    if 1900 <= year <= 2200:
        return year
    return None


def _read_tags_sync(path: str) -> AudioMetadata | None:
    """Синхронное чтение тегов (выполняется в отдельном потоке)."""
    import mutagen  # локальный импорт: зависимость опциональна

    handle = mutagen.File(path, easy=True)
    if handle is None:
        logger.info("Формат файла не распознан mutagen: %s", path)
        return None

    tags = getattr(handle, "tags", None)
    file_name = os.path.basename(path)
    guessed_artist, guessed_title = guess_from_filename(file_name)

    title = clean_title(_first_tag(tags, "title", "TIT2")) or guessed_title or ""
    if not title:
        title = DEFAULT_TITLE
    artist = clean_title(_first_tag(tags, "artist", "albumartist", "performer", "TPE1"))
    album = clean_title(_first_tag(tags, "album", "TALB"))
    genre = clean_title(_first_tag(tags, "genre", "TCON"))
    year = _parse_year(_first_tag(tags, "date", "originaldate", "year", "TDRC"))

    info = getattr(handle, "info", None)
    length = getattr(info, "length", 0) or 0
    try:
        duration = int(round(float(length)))
    except (TypeError, ValueError):
        duration = 0

    return AudioMetadata(
        title=title,
        artist=artist or guessed_artist or None,
        album=album or None,
        duration=max(duration, 0),
        year=year,
        genre=genre or None,
        file_name=file_name or None,
    )


async def extract_tags(path: str) -> AudioMetadata | None:
    """Прочитать теги локального файла через mutagen.

    Возвращает None, если mutagen не установлен, файл недоступен или формат
    не распознан. Чтение уходит в `asyncio.to_thread`, чтобы не блокировать
    event loop.
    """
    if not path:
        return None
    if not _mutagen_installed():
        return None
    try:
        return await asyncio.to_thread(_read_tags_sync, path)
    except FileNotFoundError:
        logger.warning("Файл для чтения тегов не найден: %s", path)
        return None
    except Exception:  # noqa: BLE001 — mutagen бросает разнородные ошибки
        logger.exception("Не удалось прочитать теги файла %s", path)
        return None


# --- Форматирование ----------------------------------------------------------


def format_duration(seconds: int | None) -> str:
    """Длительность в виде «3:07» / «1:02:03»; для пустой — «—»."""
    if seconds is None:
        return NO_DURATION
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return NO_DURATION
    if total <= 0:
        return NO_DURATION
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
