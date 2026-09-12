"""Репозиторий треков — центральный модуль доступа к таблице ``tracks``.

Экспортирует SQL-фрагменты (``TRACK_COLUMNS``, ``TRACK_FROM``) и сборщик запроса
``track_query``, на которые опираются репозитории stats/albums/playlists/favourites
и сервис поиска. Пользовательский ввод НИКОГДА не подставляется в SQL-текст —
только через параметры-плейсхолдеры; порядок сортировки выбирается исключительно
из белого списка ``ORDER_MAP``.

V2 (раздел 2 контракта ARCHITECTURE-V2):

* у трека появились ``file_type`` (audio | document | video | video_note | voice)
  и ``genre``; раздел «Треки» — это ``file_type='audio'``, раздел «Другое» — всё
  остальное;
* несколько исполнителей живут в таблице ``track_artists`` (``position=0`` —
  основной), а ``tracks.artist_id`` СОХРАНЯЕТСЯ как денормализованный основной
  исполнитель — на него опирается весь код V1;
* имена и идентификаторы исполнителей попадают в каждую строку трека
  СКАЛЯРНЫМИ ПОДЗАПРОСАМИ (``artist_names`` — для показа, через ', ';
  ``artist_names_packed`` — то же, но через \\x1f, чтобы имя с запятой не
  разваливалось при разборе; ``artist_ids``). Подзапрос выбран
  намеренно: JOIN к ``track_artists`` размножил бы строки треков, а
  ``TRACK_COLUMNS`` подставляется в чужие запросы (favourites, playlists, stats),
  где нет ни GROUP BY, ни DISTINCT.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Final, Iterable, Sequence

from backend.config import settings
from backend.db.database import db
from backend.db.repositories import users as users_repo
from backend.errors import MusicBoxError, ValidationError

logger = logging.getLogger(__name__)

# Сентинел «фильтр не задан»: отличает «без фильтра» от «folder_id IS NULL».
UNSET: Final[Any] = object()

# Допустимые типы файлов (совпадают с services.media.FILE_TYPES; продублированы
# здесь, чтобы репозиторий не зависел от слоя сервисов).
FILE_TYPES: Final[tuple[str, ...]] = ("audio", "document", "video", "video_note", "voice")

#: Тип по умолчанию: всё, что грузилось в V1, — это аудио.
DEFAULT_FILE_TYPE: Final[str] = "audio"

#: Раздел «Другое» — все типы, кроме аудио.
NON_AUDIO_FILE_TYPES: Final[tuple[str, ...]] = tuple(
    file_type for file_type in FILE_TYPES if file_type != DEFAULT_FILE_TYPE
)

#: Разделитель имён исполнителей в агрегате ``artist_names`` (для показа).
ARTIST_NAME_SEPARATOR: Final[str] = ", "

#: Разделитель машинного агрегата ``artist_names_packed`` — символ \x1f (UNIT
#: SEPARATOR). Имя исполнителя вполне может содержать запятую («Tyler, The
#: Creator»), поэтому разбирать ``artist_names`` обратно нельзя: куски не
#: совпали бы с ``artist_ids`` и имена уехали бы к чужим идентификаторам.
#: В имя \x1f не попадает (а на всякий случай заменяется пробелом в SQL),
#: значит число имён ВСЕГДА равно числу идентификаторов.
ARTIST_NAME_PACK_SEPARATOR: Final[str] = "\x1f"

#: Разделы поиска для :func:`search_in`.
SEARCH_SCOPES: Final[tuple[str, ...]] = ("tracks", "favourites", "other")

#: Максимальная длина названия трека при переименовании.
MAX_TITLE_LENGTH: Final[int] = 200

# Агрегаты по track_artists. Внутренний подзапрос с ORDER BY нужен, чтобы
# GROUP_CONCAT склеивал значения В ПОРЯДКЕ position (основной исполнитель —
# первым): вариант «GROUP_CONCAT(... ORDER BY ...)» требует SQLite 3.44+,
# а такой подзапрос работает на любой версии.
_ARTIST_NAMES_SQL: Final[str] = f"""
        (SELECT GROUP_CONCAT(ta_names.name, '{ARTIST_NAME_SEPARATOR}')
           FROM (SELECT a_agg.name AS name
                   FROM track_artists ta_agg
                   JOIN artists a_agg ON a_agg.id = ta_agg.artist_id
                  WHERE ta_agg.track_id = t.id
                  ORDER BY ta_agg.position, a_agg.id) AS ta_names) AS artist_names
"""

_ARTIST_NAMES_PACKED_SQL: Final[str] = """
        (SELECT GROUP_CONCAT(ta_packed.name, char(31))
           FROM (SELECT REPLACE(a_pack.name, char(31), ' ') AS name
                   FROM track_artists ta_pack
                   JOIN artists a_pack ON a_pack.id = ta_pack.artist_id
                  WHERE ta_pack.track_id = t.id
                  ORDER BY ta_pack.position, a_pack.id) AS ta_packed) AS artist_names_packed
"""

_ARTIST_IDS_SQL: Final[str] = """
        (SELECT GROUP_CONCAT(ta_ids.artist_id, ',')
           FROM (SELECT ta_ord.artist_id AS artist_id
                   FROM track_artists ta_ord
                  WHERE ta_ord.track_id = t.id
                  ORDER BY ta_ord.position, ta_ord.artist_id) AS ta_ids) AS artist_ids
"""

TRACK_COLUMNS = f"""
        t.id, t.user_id, t.title, t.artist, t.album, t.duration, t.file_size,
        t.mime_type, t.file_name, t.file_id, t.file_unique_id,
        t.storage_chat_id, t.storage_message_id, t.thumb_file_id,
        t.folder_id, t.artist_id, t.album_id, t.source, t.source_ref,
        t.file_type, t.genre,
        t.play_count, t.last_played_at, t.created_at,
        CASE WHEN fav.id IS NULL THEN 0 ELSE 1 END AS is_favourite,
        fo.name AS folder_name,
        {_ARTIST_NAMES_SQL.strip()},
        {_ARTIST_NAMES_PACKED_SQL.strip()},
        {_ARTIST_IDS_SQL.strip()}
"""

TRACK_FROM = """
    FROM tracks t
    LEFT JOIN favourites fav ON fav.track_id = t.id AND fav.user_id = t.user_id
    LEFT JOIN folders   fo  ON fo.id = t.folder_id
"""

# Подзапрос-поддерево папок: используется, когда репозиторий папок ещё не отдал
# готовый список потомков или когда потомков больше, чем влезает в параметры
# запроса. Параметры: (folder_id, user_id, user_id).
_FOLDER_SUBTREE_SQL: Final[str] = """
        SELECT sub.id FROM (
            WITH RECURSIVE sub(id) AS (
                SELECT root.id FROM folders root WHERE root.id = ? AND root.user_id = ?
                UNION
                SELECT f.id FROM folders f JOIN sub ON f.parent_folder_id = sub.id
                 WHERE f.user_id = ?
            )
            SELECT id FROM sub
        ) AS sub
"""

# Белый список сортировок: ключ из API/бота -> безопасное SQL-выражение.
# Для title/artist сортируем по LOWER(...), чтобы регистр не влиял на порядок.
ORDER_MAP: Final[dict[str, str]] = {
    "created_at_desc": "t.created_at DESC, t.id DESC",
    "created_at_asc": "t.created_at ASC, t.id ASC",
    "title": "LOWER(t.title) ASC, t.id ASC",
    "artist": "LOWER(COALESCE(t.artist, '')) ASC, LOWER(t.title) ASC, t.id ASC",
    "play_count_desc": "t.play_count DESC, t.last_played_at DESC, t.id DESC",
    "last_played_desc": "t.last_played_at DESC, t.id DESC",
}

DEFAULT_ORDER: Final[str] = "created_at_desc"

# Поля, которые разрешено менять через update_track.
UPDATABLE_FIELDS: Final[tuple[str, ...]] = (
    "title",
    "artist",
    "album",
    "folder_id",
    "artist_id",
    "album_id",
    "thumb_file_id",
    "file_id",
    "storage_message_id",
    "storage_chat_id",
    "file_type",
    "genre",
)

# Колонки с NOT NULL: значение None означает «не менять», а не «обнулить».
_NOT_NULL_FIELDS: Final[frozenset[str]] = frozenset({"title", "file_id"})

# SQLite по умолчанию ограничивает число параметров запроса — режем IN-списки.
_MAX_SQL_PARAMS: Final[int] = 400

# «ё» и «е» считаем одной буквой при сравнении текста (как в сервисе поиска).
_YO_TRANSLATION: Final[dict[int, str]] = str.maketrans({"ё": "е", "Ё": "е"})

# Начиная с этого числа кандидатов нечёткий поиск уходит в отдельный поток,
# чтобы не блокировать event loop.
_FUZZY_THREAD_THRESHOLD: Final[int] = 200

# Допустимые символы для ORDER BY / LIMIT-фрагментов (защита от инъекций
# в служебных вызовах track_query из других репозиториев). Разрешены имена
# колонок, функции, строковые литералы; запрещены ';', '--' и прочее.
_ORDER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.,()'\s]+$")
_LIMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9?\s-]*$")


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


def _clean_text(value: Any) -> str | None:
    """Приводит значение к непустой строке либо к None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any, default: int = 0, *, minimum: int | None = 0) -> int:
    """Безопасно приводит значение к int (с нижней границей)."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return minimum
    return result


def _optional_int(value: Any) -> int | None:
    """Приводит значение к int или None (для внешних идентификаторов)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Некорректное числовое значение %r, использую NULL", value)
        return None


def _placeholders(count: int) -> str:
    """Строит строку плейсхолдеров вида ``?, ?, ?``."""
    return ", ".join("?" * count)


def _fold_text(value: Any) -> str:
    """Готовит строку к сравнению: схлопывание пробелов, casefold, «ё» -> «е».

    LOWER() в SQLite работает только с латиницей, поэтому сравнение подстрок
    делается в Python — иначе «КИНО» не нашлось бы по запросу «кино».
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold().translate(_YO_TRANSLATION)


def _track_haystack(row: dict) -> str:
    """Строка для сравнения с запросом: название, исполнители, альбом, файл."""
    return _fold_text(
        " ".join(
            str(row.get(field) or "")
            for field in ("title", "artist", "artist_names", "album", "file_name")
        )
    )


def _chunks(items: Sequence[Any], size: int = _MAX_SQL_PARAMS) -> Iterable[Sequence[Any]]:
    """Разбивает последовательность на части для IN-списков."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _unique_ids(ids: Sequence[int]) -> list[int]:
    """Оставляет корректные целые id без дублей, сохраняя исходный порядок."""
    result: list[int] = []
    seen: set[int] = set()
    for raw in ids or ():
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("Пропускаю некорректный id трека: %r", raw)
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_file_type(value: Any, *, strict: bool = True) -> str:
    """Приводит тип файла к значению из :data:`FILE_TYPES`.

    ``strict=True`` (создание/изменение трека) — неизвестный тип приводит к
    :class:`ValidationError`; ``strict=False`` (фильтр списка) — значение
    используется как есть, но пишется предупреждение в лог.
    """
    text = _clean_text(value)
    if text is None:
        return DEFAULT_FILE_TYPE
    normalized = text.lower()
    if normalized in FILE_TYPES:
        return normalized
    if strict:
        raise ValidationError(
            "Неизвестный тип файла: допустимы " + ", ".join(FILE_TYPES)
        )
    logger.warning("Неизвестный тип файла %r в фильтре списка треков", value)
    return normalized


def parse_artist_ids(value: Any) -> list[int]:
    """Разбирает агрегат ``artist_ids`` строки трека в список id.

    Принимает как строку из ``GROUP_CONCAT`` («5,3,9»), так и сам трек-``dict``.
    Порядок сохраняется: первым идёт основной исполнитель (``position = 0``).
    """
    raw = value.get("artist_ids") if isinstance(value, dict) else value
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return _unique_ids(list(raw))
    result: list[int] = []
    for part in str(raw).split(","):
        chunk = part.strip()
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            logger.warning("Не удалось разобрать идентификатор исполнителя %r", chunk)
    return _unique_ids(result)


def artist_names_of(track: dict | None) -> list[str]:
    """Имена исполнителей трека в порядке ``position`` (основной — первым).

    Берётся машинный агрегат ``artist_names_packed`` (разделитель \\x1f): только
    он даёт имена, которые один-к-одному совпадают с ``artist_ids`` даже когда
    в имени есть запятая. Разбор ``artist_names`` (через ', ') остаётся
    запасным вариантом для строк, собранных без нового агрегата.
    """
    if not track:
        return []
    packed = track.get("artist_names_packed")
    if packed:
        return [
            part.strip()
            for part in str(packed).split(ARTIST_NAME_PACK_SEPARATOR)
            if part.strip()
        ]
    raw = track.get("artist_names")
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(ARTIST_NAME_SEPARATOR) if part.strip()]


def resolve_order(order: str | None) -> str:
    """Возвращает SQL-выражение сортировки по ключу из белого списка."""
    key = (order or DEFAULT_ORDER).strip().lower()
    expression = ORDER_MAP.get(key)
    if expression is None:
        logger.warning("Неизвестный порядок сортировки %r, использую %s", order, DEFAULT_ORDER)
        expression = ORDER_MAP[DEFAULT_ORDER]
    return expression


def track_query(
    where: str = "",
    order: str = "t.created_at DESC",
    limit_clause: str = "",
) -> str:
    """Собирает SELECT по трекам пользователя.

    Итоговый SQL: ``SELECT {TRACK_COLUMNS} {TRACK_FROM} WHERE t.user_id = ?
    {where} ORDER BY {order} {limit_clause}``. Первым параметром запроса всегда
    идёт ``user_id``; все значения фильтров передаются плейсхолдерами.
    """
    order_sql = (order or "").strip() or ORDER_MAP[DEFAULT_ORDER]
    if not _ORDER_RE.match(order_sql):
        logger.warning("Недопустимое выражение сортировки %r, использую значение по умолчанию", order)
        order_sql = ORDER_MAP[DEFAULT_ORDER]

    limit_sql = (limit_clause or "").strip()
    if limit_sql and not _LIMIT_RE.match(limit_sql):
        logger.warning("Недопустимый LIMIT-фрагмент %r, игнорирую", limit_clause)
        limit_sql = ""

    where_sql = (where or "").strip()
    return (
        f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
        f"WHERE t.user_id = ? {where_sql} "
        f"ORDER BY {order_sql} {limit_sql}"
    ).strip()


def _file_type_filter(file_type: Any) -> tuple[str, list[Any]]:
    """Условие по типу файла.

    ``"audio"`` (по умолчанию) — только аудио, ``None``/``UNSET`` — без фильтра,
    последовательность типов — раздел «Другое» (``file_type IN (...)``).
    """
    if file_type is UNSET or file_type is None:
        return "", []

    if isinstance(file_type, (list, tuple, set, frozenset)):
        values = [normalize_file_type(item, strict=False) for item in file_type]
        values = [value for value in dict.fromkeys(values) if value]
        if not values:
            return "", []
        if len(values) == 1:
            return "AND t.file_type = ?", [values[0]]
        return f"AND t.file_type IN ({_placeholders(len(values))})", list(values)

    return "AND t.file_type = ?", [normalize_file_type(file_type, strict=False)]


async def _folder_descendants(user_id: int, folder_id: int) -> list[int] | None:
    """Идентификаторы папки и всех её подпапок через репозиторий папок.

    Возвращает None, если репозиторий папок не предоставляет ``descendant_ids``
    (частично обновлённая установка) или запрос не удался — тогда вызывающий код
    использует рекурсивный подзапрос :data:`_FOLDER_SUBTREE_SQL`.
    """
    # Импорт внутри функции: folders -> services.metadata -> ... , а модуль
    # tracks подключается репозиторием папок косвенно, поэтому верхнеуровневый
    # импорт создал бы цикл.
    from backend.db.repositories import folders as folders_repo

    resolver = getattr(folders_repo, "descendant_ids", None)
    if resolver is None:
        logger.debug("folders.descendant_ids недоступна, беру подпапки рекурсивным запросом")
        return None
    try:
        ids = await resolver(user_id, folder_id, include_self=True)
    except Exception:
        logger.warning(
            "Не удалось получить подпапки папки %s пользователя %s", folder_id, user_id,
            exc_info=True,
        )
        return None
    return _unique_ids(list(ids or []))


async def _folder_filter(
    user_id: int, folder_id: Any, recursive: bool
) -> tuple[str, list[Any]]:
    """Условие по папке; при ``recursive=True`` — вместе со всеми подпапками."""
    if folder_id is UNSET:
        return "", []
    if folder_id is None:
        if recursive:
            logger.debug("folder_recursive не применяется к трекам без папки")
        return "AND t.folder_id IS NULL", []

    target = int(folder_id)
    if not recursive:
        return "AND t.folder_id = ?", [target]

    descendants = await _folder_descendants(user_id, target)
    if descendants and len(descendants) <= _MAX_SQL_PARAMS:
        if len(descendants) == 1:
            return "AND t.folder_id = ?", [descendants[0]]
        return (
            f"AND t.folder_id IN ({_placeholders(len(descendants))})",
            list(descendants),
        )
    if descendants is not None and not descendants:
        # Папки нет (или она чужая) — треков в её поддереве быть не может.
        return "AND t.folder_id = ?", [target]

    return f"AND t.folder_id IN ({_FOLDER_SUBTREE_SQL})", [target, int(user_id), int(user_id)]


async def _build_filters(
    user_id: int,
    *,
    folder_id: Any = UNSET,
    artist_id: Any = None,
    album_id: Any = None,
    file_type: Any = UNSET,
    folder_recursive: bool = False,
) -> tuple[str, list[Any]]:
    """Строит условия WHERE и список параметров для фильтров списка треков."""
    parts: list[str] = []
    params: list[Any] = []

    folder_sql, folder_params = await _folder_filter(user_id, folder_id, folder_recursive)
    if folder_sql:
        parts.append(folder_sql)
        params.extend(folder_params)

    if artist_id is not UNSET and artist_id is not None:
        # Трек принадлежит исполнителю, если тот основной (tracks.artist_id) ИЛИ
        # участвует в нём (track_artists). Иначе список исполнителя расходится с
        # агрегатом track_count, который считает именно участие: приглашённый
        # исполнитель показывал бы «2 трека» и пустой список при открытии.
        parts.append(
            "AND (t.artist_id = ? OR EXISTS ("
            "SELECT 1 FROM track_artists ta_f "
            "WHERE ta_f.track_id = t.id AND ta_f.artist_id = ?))"
        )
        params.append(int(artist_id))
        params.append(int(artist_id))

    if album_id is not UNSET and album_id is not None:
        parts.append("AND t.album_id = ?")
        params.append(int(album_id))

    type_sql, type_params = _file_type_filter(file_type)
    if type_sql:
        parts.append(type_sql)
        params.extend(type_params)

    return " ".join(parts), params


def _limit_clause(limit: int | None, offset: int | None) -> tuple[str, list[Any]]:
    """Возвращает LIMIT/OFFSET-фрагмент с плейсхолдерами и его параметры."""
    safe_offset = _to_int(offset, 0)
    if limit is None:
        if safe_offset > 0:
            return "LIMIT -1 OFFSET ?", [safe_offset]
        return "", []
    safe_limit = _to_int(limit, 0)
    if safe_limit <= 0:
        if safe_offset > 0:
            return "LIMIT -1 OFFSET ?", [safe_offset]
        return "", []
    return "LIMIT ? OFFSET ?", [safe_limit, safe_offset]


def _compose_artist_ids(
    artist_id: Any, artist_ids: Sequence[int] | None
) -> list[int]:
    """Итоговый порядок исполнителей: основной первым, дубли убраны."""
    result: list[int] = []
    primary = _optional_int(artist_id)
    if primary is not None:
        result.append(primary)
    result.extend(_unique_ids(list(artist_ids or ())))
    return _unique_ids(result)


async def _existing_artist_ids(user_id: int, artist_ids: Sequence[int]) -> list[int]:
    """Оставляет только исполнителей пользователя, сохраняя переданный порядок."""
    wanted = _unique_ids(list(artist_ids or ()))
    if not wanted:
        return []

    allowed: set[int] = set()
    for chunk in _chunks(wanted):
        rows = await db.fetch_all(
            f"SELECT id FROM artists WHERE user_id = ? AND id IN ({_placeholders(len(chunk))})",
            (int(user_id), *chunk),
        )
        allowed.update(int(row["id"]) for row in rows)

    skipped = [artist_id for artist_id in wanted if artist_id not in allowed]
    if skipped:
        logger.warning(
            "Исполнители %s не принадлежат пользователю %s — пропускаю", skipped, user_id
        )
    return [artist_id for artist_id in wanted if artist_id in allowed]


async def _link_artists(
    user_id: int,
    track_id: int,
    artist_ids: Sequence[int],
    *,
    replace: bool = False,
    sync_primary: bool = False,
) -> list[int]:
    """Пишет связи трека с исполнителями (``position`` = порядок в списке).

    ``replace=True`` — сначала очистить прежний состав, ``sync_primary=True`` —
    привести денормализованный ``tracks.artist_id`` к первому исполнителю.
    """
    valid = await _existing_artist_ids(user_id, artist_ids)

    async with db.transaction() as conn:
        if replace:
            cursor = await conn.execute(
                "DELETE FROM track_artists WHERE track_id = ?", (int(track_id),)
            )
            await cursor.close()

        if valid:
            cursor = await conn.executemany(
                """
                INSERT OR IGNORE INTO track_artists (track_id, artist_id, position)
                VALUES (?, ?, ?)
                """,
                [(int(track_id), artist_id, position) for position, artist_id in enumerate(valid)],
            )
            await cursor.close()

        if sync_primary:
            cursor = await conn.execute(
                "UPDATE tracks SET artist_id = ? WHERE id = ? AND user_id = ?",
                (valid[0] if valid else None, int(track_id), int(user_id)),
            )
            await cursor.close()

    return valid


# --------------------------------------------------------------------------
# Создание и чтение
# --------------------------------------------------------------------------


async def create_track(
    user_id: int,
    *,
    title: str,
    artist: str | None = None,
    album: str | None = None,
    duration: int = 0,
    file_size: int = 0,
    mime_type: str | None = None,
    file_name: str | None = None,
    file_id: str,
    file_unique_id: str | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    thumb_file_id: str | None = None,
    folder_id: int | None = None,
    artist_id: int | None = None,
    album_id: int | None = None,
    source: str = "upload",
    source_ref: str | None = None,
    file_type: str = DEFAULT_FILE_TYPE,
    genre: str | None = None,
    artist_ids: Sequence[int] | None = None,
) -> dict:
    """Создаёт трек; при дубликате (user_id, file_unique_id) возвращает существующий.

    ``artist_ids`` — все исполнители трека по порядку; основным (``position = 0``
    в ``track_artists`` и ``tracks.artist_id``) становится ``artist_id``, если он
    задан, иначе первый элемент списка. Связи пишутся только для НОВОГО трека:
    у дубликата состав исполнителей уже есть и перезаписывать его не нужно.
    """
    safe_file_id = _clean_text(file_id)
    if not safe_file_id:
        raise ValidationError("Не удалось сохранить трек: отсутствует идентификатор файла")

    safe_title = _clean_text(title) or "Без названия"
    safe_unique_id = _clean_text(file_unique_id)  # пусто -> NULL (UNIQUE допускает много NULL)
    safe_source = _clean_text(source) or "upload"
    safe_file_type = normalize_file_type(file_type)

    ordered_artists = _compose_artist_ids(artist_id, artist_ids)
    primary_artist_id = ordered_artists[0] if ordered_artists else _optional_int(artist_id)

    params = (
        int(user_id),
        safe_title,
        _clean_text(artist),
        _clean_text(album),
        _to_int(duration, 0),
        _to_int(file_size, 0),
        _clean_text(mime_type),
        _clean_text(file_name),
        safe_file_id,
        safe_unique_id,
        _optional_int(storage_chat_id),
        _optional_int(storage_message_id),
        _clean_text(thumb_file_id),
        _optional_int(folder_id),
        primary_artist_id,
        _optional_int(album_id),
        safe_source,
        _clean_text(source_ref),
        safe_file_type,
        _clean_text(genre),
    )

    sql = """
        INSERT INTO tracks (
            user_id, title, artist, album, duration, file_size, mime_type, file_name,
            file_id, file_unique_id, storage_chat_id, storage_message_id, thumb_file_id,
            folder_id, artist_id, album_id, source, source_ref, file_type, genre,
            play_count, last_played_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
        ON CONFLICT(user_id, file_unique_id) DO NOTHING
    """
    new_id = await db.execute_insert(sql, params)

    track: dict | None
    if safe_unique_id is not None:
        # При конфликте вставки не было — берём уже существующую запись.
        track = await get_track_by_unique_id(user_id, safe_unique_id)
    else:
        track = await get_track(user_id, int(new_id))

    if track is None:
        logger.error(
            "Не удалось прочитать созданный трек: user_id=%s file_unique_id=%s",
            user_id,
            safe_unique_id,
        )
        raise MusicBoxError("Не удалось сохранить трек в базе данных")

    if ordered_artists and not track.get("artist_ids"):
        await _link_artists(int(user_id), int(track["id"]), ordered_artists)
        refreshed = await get_track(user_id, int(track["id"]))
        if refreshed is not None:
            track = refreshed

    logger.debug("Трек #%s сохранён для пользователя %s", track["id"], user_id)
    return track


async def get_track(user_id: int, track_id: int) -> dict | None:
    """Возвращает трек пользователя по id."""
    sql = track_query(where="AND t.id = ?")
    return await db.fetch_one(sql, (int(user_id), int(track_id)))


async def get_tracks_by_ids(user_id: int, ids: Sequence[int]) -> list[dict]:
    """Возвращает треки по списку id, СОХРАНЯЯ порядок из ``ids``."""
    wanted = _unique_ids(ids)
    if not wanted:
        return []

    found: dict[int, dict] = {}
    for chunk in _chunks(wanted):
        sql = track_query(where=f"AND t.id IN ({_placeholders(len(chunk))})")
        rows = await db.fetch_all(sql, (int(user_id), *chunk))
        for row in rows:
            found[int(row["id"])] = row

    return [found[track_id] for track_id in wanted if track_id in found]


async def get_track_by_unique_id(user_id: int, file_unique_id: str) -> dict | None:
    """Возвращает трек по Telegram file_unique_id."""
    unique_id = _clean_text(file_unique_id)
    if unique_id is None:
        return None
    sql = track_query(where="AND t.file_unique_id = ?")
    return await db.fetch_one(sql, (int(user_id), unique_id))


async def list_tracks(
    user_id: int,
    *,
    folder_id: Any = UNSET,
    artist_id: int | None = None,
    album_id: int | None = None,
    order: str = "created_at_desc",
    limit: int = 50,
    offset: int = 0,
    file_type: Any = DEFAULT_FILE_TYPE,
    folder_recursive: bool = False,
) -> list[dict]:
    """Список треков пользователя с фильтрами и сортировкой из белого списка.

    ``folder_id=UNSET`` — без фильтра, ``folder_id=None`` — только треки без папки.
    ``file_type`` по умолчанию ``"audio"`` (раздел «Треки»); ``None`` — без
    фильтра по типу, последовательность типов — раздел «Другое».
    ``folder_recursive=True`` — вместе с треками всех подпапок.
    """
    where_sql, where_params = await _build_filters(
        user_id,
        folder_id=folder_id,
        artist_id=artist_id,
        album_id=album_id,
        file_type=file_type,
        folder_recursive=folder_recursive,
    )
    limit_sql, limit_params = _limit_clause(limit, offset)
    sql = track_query(where=where_sql, order=resolve_order(order), limit_clause=limit_sql)
    params = [int(user_id), *where_params, *limit_params]
    return await db.fetch_all(sql, params)


async def count_tracks(
    user_id: int,
    *,
    folder_id: Any = UNSET,
    artist_id: int | None = None,
    album_id: int | None = None,
    file_type: Any = DEFAULT_FILE_TYPE,
    folder_recursive: bool = False,
) -> int:
    """Количество треков пользователя с теми же фильтрами, что и list_tracks."""
    where_sql, where_params = await _build_filters(
        user_id,
        folder_id=folder_id,
        artist_id=artist_id,
        album_id=album_id,
        file_type=file_type,
        folder_recursive=folder_recursive,
    )
    sql = f"SELECT COUNT(*) FROM tracks t WHERE t.user_id = ? {where_sql}"
    value = await db.fetch_val(sql, [int(user_id), *where_params], default=0)
    return _to_int(value, 0)


# --------------------------------------------------------------------------
# Изменение
# --------------------------------------------------------------------------


async def update_track(user_id: int, track_id: int, **fields: Any) -> dict | None:
    """Обновляет разрешённые поля трека и возвращает его актуальную версию."""
    existing = await get_track(user_id, track_id)
    if existing is None:
        return None

    assignments: list[str] = []
    params: list[Any] = []

    for name, value in fields.items():
        if name not in UPDATABLE_FIELDS:
            logger.warning("Поле %r нельзя изменить через update_track — пропускаю", name)
            continue

        if name == "file_type":
            if value is None:
                continue  # NOT NULL-колонка: None означает «не менять»
            params.append(normalize_file_type(value))
        elif name in _NOT_NULL_FIELDS:
            if value is None:
                continue  # None для NOT NULL-колонки означает «не менять»
            cleaned = _clean_text(value)
            if cleaned is None:
                raise ValidationError("Название трека не может быть пустым")
            params.append(cleaned)
        elif name in ("folder_id", "artist_id", "album_id", "storage_message_id", "storage_chat_id"):
            params.append(_optional_int(value))
        else:
            params.append(_clean_text(value))

        assignments.append(f"{name} = ?")

    if not assignments:
        return existing

    sql = f"UPDATE tracks SET {', '.join(assignments)} WHERE id = ? AND user_id = ?"
    params.extend([int(track_id), int(user_id)])
    await db.execute(sql, params)
    return await get_track(user_id, track_id)


async def move_track(user_id: int, track_id: int, folder_id: int | None) -> dict | None:
    """Переносит трек в папку (``folder_id=None`` — вынести из папок)."""
    return await update_track(user_id, track_id, folder_id=folder_id)


async def move_tracks(user_id: int, track_ids: Sequence[int], folder_id: int | None) -> int:
    """Массовый перенос треков в папку. Возвращает число перемещённых треков."""
    wanted = _unique_ids(track_ids)
    if not wanted:
        return 0

    target = _optional_int(folder_id)
    moved = 0
    for chunk in _chunks(wanted):
        sql = (
            f"UPDATE tracks SET folder_id = ? "
            f"WHERE user_id = ? AND id IN ({_placeholders(len(chunk))})"
        )
        moved += await db.execute(sql, [target, int(user_id), *chunk])

    logger.debug("Перемещено треков: %s (папка %s, пользователь %s)", moved, target, user_id)
    return moved


async def delete_track(user_id: int, track_id: int) -> dict | None:
    """Удаляет трек и возвращает его данные ДО удаления (нужны для чистки канала)."""
    track = await get_track(user_id, track_id)
    if track is None:
        return None

    deleted = await db.execute(
        "DELETE FROM tracks WHERE id = ? AND user_id = ?",
        (int(track_id), int(user_id)),
    )
    if not deleted:
        return None

    logger.info("Трек #%s удалён у пользователя %s", track_id, user_id)
    return track


async def rename_track(user_id: int, track_id: int, new_title: str) -> dict | None:
    """Переименовывает трек. None — трек не найден, ValidationError — пустое имя."""
    title = _clean_text(new_title)
    if title is None:
        raise ValidationError("Название трека не может быть пустым")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(
            f"Название трека слишком длинное (максимум {MAX_TITLE_LENGTH} символов)"
        )

    track = await update_track(user_id, track_id, title=title)
    if track is None:
        logger.debug("Переименование: трек #%s не найден у пользователя %s", track_id, user_id)
        return None
    logger.info("Пользователь %s: трек #%s переименован в «%s»", user_id, track_id, title)
    return track


# --------------------------------------------------------------------------
# Исполнители трека (track_artists)
# --------------------------------------------------------------------------

# Колонки исполнителя для состава трека: без агрегатов, зато с позицией.
_TRACK_ARTIST_COLUMNS: Final[str] = """
        a.id, a.user_id, a.name, a.normalized_name, a.is_listened,
        a.listened_at, a.folder_id, a.created_at, a.total_plays,
        ta.position, ta.track_id
"""


def _artist_link_to_dict(row: dict) -> dict:
    """Приводит строку состава исполнителей к публичному виду."""
    data = dict(row)
    data["is_listened"] = bool(data.get("is_listened", 0))
    data["position"] = _to_int(data.get("position"), 0)
    data["total_plays"] = _to_int(data.get("total_plays"), 0)
    return data


async def set_track_artists(
    user_id: int, track_id: int, artist_ids: Sequence[int]
) -> None:
    """Задаёт состав исполнителей трека (первый — основной, ``position = 0``).

    Прежние связи заменяются целиком; денормализованный ``tracks.artist_id``
    приводится к первому исполнителю (пустой список — обнуляется). Исполнители
    чужих пользователей игнорируются. Текстовое поле ``tracks.artist`` НЕ
    трогается: за него отвечает :func:`update_track`.
    """
    track = await get_track(user_id, track_id)
    if track is None:
        logger.debug(
            "Состав исполнителей не изменён: трек #%s не найден у пользователя %s",
            track_id,
            user_id,
        )
        return

    linked = await _link_artists(
        int(user_id),
        int(track_id),
        _unique_ids(list(artist_ids or ())),
        replace=True,
        sync_primary=True,
    )
    logger.info(
        "Пользователь %s: у трека #%s теперь %d исполнител(я/ей)",
        user_id,
        track_id,
        len(linked),
    )


async def track_artists(user_id: int, track_id: int) -> list[dict]:
    """Исполнители трека по возрастанию ``position`` (основной — первым)."""
    rows = await db.fetch_all(
        f"""
        SELECT {_TRACK_ARTIST_COLUMNS}
          FROM track_artists ta
          JOIN artists a ON a.id = ta.artist_id
          JOIN tracks  t ON t.id = ta.track_id
         WHERE ta.track_id = ? AND t.user_id = ? AND a.user_id = ?
         ORDER BY ta.position, a.id
        """,
        (int(track_id), int(user_id), int(user_id)),
    )
    return [_artist_link_to_dict(row) for row in rows]


async def artists_by_tracks(
    user_id: int, track_ids: Sequence[int]
) -> dict[int, list[dict]]:
    """Состав исполнителей сразу для нескольких треков (без запроса на каждый).

    Возвращает ``{track_id: [исполнитель, ...]}``; треки без исполнителей в
    результат не попадают.
    """
    wanted = _unique_ids(list(track_ids or ()))
    if not wanted:
        return {}

    result: dict[int, list[dict]] = {}
    for chunk in _chunks(wanted):
        rows = await db.fetch_all(
            f"""
            SELECT {_TRACK_ARTIST_COLUMNS}
              FROM track_artists ta
              JOIN artists a ON a.id = ta.artist_id
              JOIN tracks  t ON t.id = ta.track_id
             WHERE t.user_id = ? AND a.user_id = ?
               AND ta.track_id IN ({_placeholders(len(chunk))})
             ORDER BY ta.track_id, ta.position, a.id
            """,
            (int(user_id), int(user_id), *chunk),
        )
        for row in rows:
            item = _artist_link_to_dict(row)
            result.setdefault(int(item["track_id"]), []).append(item)
    return result


async def tracks_by_artists_intersection(
    user_id: int,
    artist_ids: Sequence[int],
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = DEFAULT_ORDER,
) -> list[dict]:
    """Треки, у которых есть ВСЕ перечисленные исполнители (пересечение).

    Реализовано через ``HAVING COUNT(DISTINCT ta.artist_id) = <число id>``.
    ``query`` — дополнительный фильтр по подстроке (название, исполнитель,
    альбом, имя файла) без нечёткости: он применяется к уже найденному
    множеству, поэтому постраничность считается после фильтра.
    """
    wanted = _unique_ids(list(artist_ids or ()))
    if not wanted:
        logger.debug("Пересечение по исполнителям вызвано без идентификаторов")
        return []
    if len(wanted) > _MAX_SQL_PARAMS:
        raise ValidationError("Слишком много исполнителей в фильтре")

    needle = _fold_text(query) if query else ""
    safe_limit = _to_int(limit, 0)
    safe_offset = _to_int(offset, 0)

    base_sql = f"""
        SELECT {TRACK_COLUMNS}
        {TRACK_FROM}
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE t.user_id = ? AND ta.artist_id IN ({_placeholders(len(wanted))})
        GROUP BY t.id
        HAVING COUNT(DISTINCT ta.artist_id) = ?
        ORDER BY {resolve_order(order)}
    """
    params: list[Any] = [int(user_id), *wanted, len(wanted)]

    if needle:
        rows = await db.fetch_all(base_sql, params)
        matched = [row for row in rows if needle in _track_haystack(row)]
        if safe_limit <= 0:
            return matched[safe_offset:]
        return matched[safe_offset : safe_offset + safe_limit]

    limit_sql, limit_params = _limit_clause(safe_limit, safe_offset)
    return await db.fetch_all(f"{base_sql} {limit_sql}", [*params, *limit_params])


# --------------------------------------------------------------------------
# Поиск по разделам
# --------------------------------------------------------------------------

# Кандидаты для нечёткого поиска: сам трек и агрегат имён исполнителей.
_SEARCH_CANDIDATE_COLUMNS: Final[str] = f"""
        t.id, t.title, t.artist, t.album, t.file_name,
        {_ARTIST_NAMES_SQL.strip()}
"""

_SCOPE_SQL: Final[dict[str, str]] = {
    "tracks": (
        f"SELECT {_SEARCH_CANDIDATE_COLUMNS} FROM tracks t "
        f"WHERE t.user_id = ? AND t.file_type = '{DEFAULT_FILE_TYPE}'"
    ),
    "other": (
        f"SELECT {_SEARCH_CANDIDATE_COLUMNS} FROM tracks t "
        f"WHERE t.user_id = ? AND t.file_type <> '{DEFAULT_FILE_TYPE}'"
    ),
    "favourites": (
        f"SELECT {_SEARCH_CANDIDATE_COLUMNS} FROM tracks t "
        "JOIN favourites fav ON fav.track_id = t.id AND fav.user_id = t.user_id "
        "WHERE t.user_id = ?"
    ),
}


def _clamp_threshold(value: Any) -> int:
    """Порог релевантности в допустимом диапазоне 0..100."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return max(0, min(100, int(settings.fuzzy_threshold)))


async def _resolve_threshold(user_id: int, threshold: int | None) -> int:
    """Порог: аргумент -> настройки пользователя -> настройки приложения."""
    if threshold is not None:
        return _clamp_threshold(threshold)
    try:
        user_settings = await users_repo.get_settings(user_id)
    except Exception:
        logger.warning(
            "Не удалось прочитать настройки поиска пользователя %s", user_id, exc_info=True
        )
        user_settings = None
    if user_settings and user_settings.get("fuzzy_threshold") is not None:
        return _clamp_threshold(user_settings["fuzzy_threshold"])
    return _clamp_threshold(settings.fuzzy_threshold)


def _substring_scores(query: str, candidates: Sequence[str]) -> dict[int, int]:
    """Запасной вариант отбора: точное вхождение подстроки (без нечёткости)."""
    needle = _fold_text(query)
    if not needle:
        return {}
    return {
        index: 100
        for index, candidate in enumerate(candidates)
        if needle in _fold_text(candidate)
    }


async def _fuzzy_scores(
    query: str, candidates: Sequence[str], threshold: int
) -> dict[int, int]:
    """Нечёткие оценки кандидатов сервисом поиска (0..100), ниже порога — отброшены."""
    if not candidates:
        return {}
    try:
        # Импорт внутри функции: сервис поиска сам импортирует этот репозиторий.
        from backend.services import search as search_service
    except Exception:
        logger.warning("Сервис поиска недоступен, ищу по вхождению подстроки", exc_info=True)
        return _substring_scores(query, candidates)

    query_variants = search_service.variants(query)
    if not query_variants:
        return {}

    def _compute() -> dict[int, int]:
        result: dict[int, int] = {}
        for index, candidate in enumerate(candidates):
            value = search_service.score(query_variants, candidate)
            if value >= threshold:
                result[index] = value
        return result

    if len(candidates) >= _FUZZY_THREAD_THRESHOLD:
        return await asyncio.to_thread(_compute)
    return _compute()


async def search_in(
    user_id: int,
    query: str,
    *,
    scope: str,
    limit: int = 30,
    threshold: int | None = None,
) -> list[dict]:
    """Нечёткий поиск треков внутри раздела.

    ``scope``: ``tracks`` — только аудио, ``favourites`` — только избранное,
    ``other`` — файлы раздела «Другое». Возвращает трек-``dict`` с полем
    ``score`` (0..100), самые релевантные — первыми.
    """
    scope_key = (scope or "").strip().lower()
    scope_sql = _SCOPE_SQL.get(scope_key)
    if scope_sql is None:
        raise ValidationError("Неизвестный раздел поиска: " + ", ".join(SEARCH_SCOPES))

    text = " ".join(str(query or "").split())
    max_items = _to_int(limit, 0)
    if not text or max_items <= 0:
        return []

    rows = await db.fetch_all(scope_sql, (int(user_id),))
    if not rows:
        return []

    candidates = [_track_haystack(row) for row in rows]
    effective_threshold = await _resolve_threshold(user_id, threshold)
    scores = await _fuzzy_scores(text, candidates, effective_threshold)
    if not scores:
        return []

    matched = [(rows[index], value) for index, value in scores.items()]
    matched.sort(
        key=lambda item: (
            -item[1],
            (item[0].get("title") or "").casefold(),
            _to_int(item[0].get("id"), 0),
        )
    )
    matched = matched[:max_items]

    ordered_ids = [_to_int(row.get("id"), 0) for row, _ in matched]
    score_by_id = {_to_int(row.get("id"), 0): value for row, value in matched}

    result: list[dict] = []
    for track in await get_tracks_by_ids(user_id, ordered_ids):
        item = dict(track)
        item["score"] = score_by_id.get(int(track["id"]), 0)
        result.append(item)

    logger.debug(
        "Поиск «%s» в разделе %s: найдено %d из %d", text, scope_key, len(result), len(rows)
    )
    return result


# --------------------------------------------------------------------------
# Прослушивания
# --------------------------------------------------------------------------


async def register_play(user_id: int, track_id: int, source: str = "web") -> dict | None:
    """Учитывает прослушивание трека: счётчик, история и отметка исполнителя.

    Все изменения выполняются АТОМАРНО в одной транзакции. Основному исполнителю
    трека дополнительно увеличивается ``artists.total_plays`` (V2) и ставится
    отметка «прослушано». Если трек не найден, история не пишется и
    возвращается None.
    """
    safe_user_id = int(user_id)
    safe_track_id = int(track_id)
    safe_source = (_clean_text(source) or "web")[:32]

    async with db.transaction() as conn:
        cursor = await conn.execute(
            """
            UPDATE tracks
               SET play_count = play_count + 1,
                   last_played_at = CURRENT_TIMESTAMP
             WHERE id = ? AND user_id = ?
            """,
            (safe_track_id, safe_user_id),
        )
        updated = cursor.rowcount
        await cursor.close()

        if not updated:
            logger.debug(
                "Прослушивание не учтено: трек #%s не найден у пользователя %s",
                safe_track_id,
                safe_user_id,
            )
            return None

        await conn.execute(
            "INSERT INTO play_history (user_id, track_id, source) VALUES (?, ?, ?)",
            (safe_user_id, safe_track_id, safe_source),
        )

        artist_cursor = await conn.execute(
            "SELECT artist_id FROM tracks WHERE id = ? AND user_id = ?",
            (safe_track_id, safe_user_id),
        )
        artist_row = await artist_cursor.fetchone()
        await artist_cursor.close()

        artist_id = artist_row[0] if artist_row is not None else None
        if artist_id is not None:
            await conn.execute(
                """
                UPDATE artists
                   SET is_listened = 1,
                       listened_at = CURRENT_TIMESTAMP,
                       total_plays = total_plays + 1
                 WHERE id = ? AND user_id = ?
                """,
                (int(artist_id), safe_user_id),
            )

    return await get_track(safe_user_id, safe_track_id)


async def total_tracks(user_id: int) -> int:
    """Общее число треков пользователя."""
    value = await db.fetch_val(
        "SELECT COUNT(*) FROM tracks WHERE user_id = ?", (int(user_id),), default=0
    )
    return _to_int(value, 0)


async def total_plays(user_id: int) -> int:
    """Суммарное число прослушиваний по всем трекам пользователя."""
    value = await db.fetch_val(
        "SELECT COALESCE(SUM(play_count), 0) FROM tracks WHERE user_id = ?",
        (int(user_id),),
        default=0,
    )
    return _to_int(value, 0)
