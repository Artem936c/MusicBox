"""Сервис нечёткого поиска по библиотеке пользователя.

Умеет прощать опечатки и неверную раскладку клавиатуры: запрос «ghbdtn» находит
трек «Привет», а «Nirvna» — «Nirvana». Поиск ведётся по трекам, альбомам
(группам), исполнителям и папкам; всё считается через rapidfuzz.

V2 (контракт `docs/ARCHITECTURE-V2.md`, раздел 3): :func:`search_all` умеет
фильтровать по НЕСКОЛЬКИМ исполнителям (`artist_ids`) — сначала берётся
пересечение по исполнителям, затем нечёткий поиск идёт ВНУТРИ полученного
множества — и учитывать раздел библиотеки (`section`: music | other) при поиске
папок. Папки ищутся всегда, даже когда задан фильтр по исполнителям (ТЗ п. 17).

Треки сравниваются по ВСЕМУ составу исполнителей (агрегат `artist_names` из
`track_artists`), а не только по текстовой колонке `tracks.artist`: её
переименование исполнителя намеренно не трогает (ТЗ п. 10), поэтому одной её
не хватило бы — как не хватает и на приглашённых исполнителей.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Final, Sequence

from rapidfuzz import fuzz, process

from backend.config import settings
from backend.db.database import db
from backend.db.repositories import albums as albums_repo
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo

logger = logging.getLogger(__name__)

__all__ = [
    "LAYOUT_RU",
    "LAYOUT_EN",
    "SECTIONS",
    "DEFAULT_SECTION",
    "swap_layout",
    "normalize_query",
    "variants",
    "score",
    "search_tracks",
    "search_albums",
    "search_artists",
    "search_folders",
    "search_all",
]

#: Разделы библиотеки V2: «music» — музыка и её папки, «other» — остальные файлы.
SECTIONS: Final[tuple[str, ...]] = ("music", "other")

#: Раздел по умолчанию (совместимо с поведением V1, где раздел был один).
DEFAULT_SECTION: Final[str] = "music"

#: Верхняя граница выборки при фильтре по исполнителям: нечёткий поиск идёт
#: внутри этого множества, поэтому берём с большим запасом относительно limit.
MAX_ARTIST_FILTER_TRACKS: Final[int] = 1000

#: Оценка для элементов, отобранных не текстом, а фильтром (точное совпадение).
_FILTER_SCORE: Final[int] = 100

# --------------------------------------------------------------------------- #
# Раскладка клавиатуры
# --------------------------------------------------------------------------- #

LAYOUT_RU = "йцукенгшщзхъфывапролджэячсмитьбю"
LAYOUT_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,."

# Верхний регистр английских служебных символов (Shift + клавиша).
_EN_SHIFTED: dict[str, str] = {
    "[": "{",
    "]": "}",
    ";": ":",
    "'": '"',
    ",": "<",
    ".": ">",
}

# Клавиши, которых нет в основных таблицах (буква «ё»).
_EXTRA_PAIRS: tuple[tuple[str, str], ...] = (("ё", "`"), ("Ё", "~"))


def _build_layout_map() -> dict[str, str]:
    """Собирает полную таблицу соответствия раскладок в обе стороны."""

    mapping: dict[str, str] = {}
    for ru_char, en_char in zip(LAYOUT_RU, LAYOUT_EN):
        mapping[ru_char] = en_char
        mapping[en_char] = ru_char

        ru_upper = ru_char.upper()
        en_upper = _EN_SHIFTED.get(en_char, en_char.upper())
        mapping[ru_upper] = en_upper
        mapping[en_upper] = ru_upper

    for ru_char, en_char in _EXTRA_PAIRS:
        mapping[ru_char] = en_char
        mapping[en_char] = ru_char
    return mapping


_LAYOUT_MAP: dict[str, str] = _build_layout_map()


def swap_layout(text: str) -> str:
    """Меняет раскладку текста ru<->en с учётом верхнего регистра.

    Символы, которых нет в таблице (цифры, пробелы, дефисы), остаются как есть.
    """

    if not text:
        return ""
    return "".join(_LAYOUT_MAP.get(char, char) for char in str(text))


# --------------------------------------------------------------------------- #
# Нормализация запроса
# --------------------------------------------------------------------------- #


def normalize_query(text: str) -> str:
    """Приводит запрос к единому виду: обрезка, схлопывание пробелов, casefold."""

    if not text:
        return ""
    return " ".join(str(text).split()).casefold()


# «ё» и «е» считаем одной буквой — только внутри сравнения, не в variants(),
# иначе сломалась бы обратная смена раскладки («ё» -> «`»).
_YO_TRANSLATION = str.maketrans({"ё": "е", "Ё": "е"})


def _fold(text: Any) -> str:
    """Готовит строку к сравнению: normalize_query + приведение «ё» к «е»."""

    if not text:
        return ""
    return normalize_query(str(text)).translate(_YO_TRANSLATION)


def variants(query: str) -> list[str]:
    """Возвращает варианты запроса: нормализованный и в другой раскладке.

    Без дублей, пустые строки отбрасываются. Пустой запрос -> пустой список.
    """

    normalized = normalize_query(query)
    if not normalized:
        return []

    result = [normalized]
    swapped = normalize_query(swap_layout(normalized))
    if swapped and swapped not in result:
        result.append(swapped)
    return result


# --------------------------------------------------------------------------- #
# Подсчёт нечёткого совпадения
# --------------------------------------------------------------------------- #

_SCORERS: tuple[Callable[..., float], ...] = (
    fuzz.WRatio,
    fuzz.partial_ratio,
    fuzz.token_set_ratio,
)

# Начиная с этого количества кандидатов расчёт уходит в отдельный поток,
# чтобы не блокировать event loop.
_THREAD_THRESHOLD = 200


def _raw_score(prepared_variants: Sequence[str], prepared_candidate: str) -> float:
    """Максимум по всем скорерам и всем вариантам запроса (строки уже готовы)."""

    if not prepared_candidate:
        return 0.0

    best = 0.0
    for variant in prepared_variants:
        if not variant:
            continue
        for scorer in _SCORERS:
            try:
                value = float(scorer(variant, prepared_candidate))
            except Exception:  # pragma: no cover — защита от сюрпризов rapidfuzz
                logger.warning(
                    "Ошибка расчёта релевантности (%s)", getattr(scorer, "__name__", scorer),
                    exc_info=True,
                )
                continue
            if value > best:
                best = value
                if best >= 100.0:
                    return 100.0
    return best


def score(query_variants: list[str], candidate: str) -> int:
    """Оценка совпадения кандидата с запросом: 0..100."""

    if not query_variants or not candidate:
        return 0
    prepared_variants = [_fold(variant) for variant in query_variants]
    return int(round(_raw_score(prepared_variants, _fold(candidate))))


def _bulk_scores(
    prepared_variants: Sequence[str],
    candidates: Sequence[str],
    cutoff: float,
) -> dict[int, float]:
    """Быстрый пакетный расчёт через rapidfuzz.process.extract (C-уровень)."""

    best: dict[int, float] = {}
    for variant in prepared_variants:
        if not variant:
            continue
        for scorer in _SCORERS:
            matches = process.extract(
                variant,
                candidates,
                scorer=scorer,
                processor=None,
                limit=None,
                score_cutoff=cutoff,
            )
            for _choice, value, index in matches:
                value = float(value)
                if value > best.get(index, 0.0):
                    best[index] = value
    return best


def _compute_scores(
    prepared_variants: Sequence[str],
    candidates: Sequence[str],
    threshold: int,
) -> dict[int, int]:
    """Считает score для всех кандидатов и отсеивает всё ниже threshold."""

    # cutoff со сдвигом на 0.5: результат округляется, отбор должен это учитывать.
    cutoff = max(0.0, float(threshold) - 0.5)
    try:
        raw = _bulk_scores(prepared_variants, candidates, cutoff)
    except Exception:
        logger.warning(
            "Пакетный расчёт релевантности недоступен, используется поэлементный",
            exc_info=True,
        )
        raw = {}
        for index, candidate in enumerate(candidates):
            value = _raw_score(prepared_variants, candidate)
            if value >= cutoff:
                raw[index] = value

    result: dict[int, int] = {}
    for index, value in raw.items():
        rounded = int(round(value))
        if rounded >= threshold:
            result[index] = rounded
    return result


async def _score_candidates(
    prepared_variants: Sequence[str],
    candidates: Sequence[str],
    threshold: int,
) -> dict[int, int]:
    """Расчёт релевантности; большие библиотеки считаются вне event loop."""

    if not candidates:
        return {}
    if len(candidates) >= _THREAD_THRESHOLD:
        return await asyncio.to_thread(
            _compute_scores, prepared_variants, candidates, threshold
        )
    return _compute_scores(prepared_variants, candidates, threshold)


# --------------------------------------------------------------------------- #
# Порог релевантности
# --------------------------------------------------------------------------- #


def _clamp_threshold(value: int) -> int:
    return max(0, min(100, int(value)))


async def _resolve_threshold(user_id: int, threshold: int | None) -> int:
    """Порог: аргумент -> user_settings.fuzzy_threshold -> settings.fuzzy_threshold."""

    if threshold is not None:
        try:
            return _clamp_threshold(int(threshold))
        except (TypeError, ValueError):
            logger.warning("Некорректный порог поиска %r, берём значение из настроек", threshold)

    try:
        user_settings = await users_repo.get_settings(user_id)
    except Exception:
        logger.warning(
            "Не удалось прочитать настройки поиска пользователя %s", user_id, exc_info=True
        )
        user_settings = None

    if user_settings:
        raw = user_settings.get("fuzzy_threshold")
        if raw is not None:
            try:
                return _clamp_threshold(int(raw))
            except (TypeError, ValueError):
                logger.warning(
                    "Некорректный fuzzy_threshold=%r у пользователя %s", raw, user_id
                )

    return _clamp_threshold(int(settings.fuzzy_threshold))


def _normalize_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _candidate_text(*parts: Any) -> str:
    """Склеивает поля кандидата в одну строку для сравнения.

    Полные повторы отбрасываются: текст `tracks.artist` и агрегат
    `artist_names` у большинства треков совпадают, а дубль в строке сравнения
    только занижает оценку.
    """

    pieces: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        folded = _fold(part)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        pieces.append(folded)
    return " ".join(pieces)


# --------------------------------------------------------------------------- #
# Разделы, фильтр по исполнителям и совместимость со схемой V1
# --------------------------------------------------------------------------- #


def _normalize_section(section: str | None) -> str:
    """Приводит раздел к допустимому значению (`music` | `other`)."""

    value = (section or "").strip().casefold()
    if not value:
        return DEFAULT_SECTION
    if value not in SECTIONS:
        logger.warning("Неизвестный раздел %r, использую «%s»", section, DEFAULT_SECTION)
        return DEFAULT_SECTION
    return value


# Кэш проверок сигнатур репозиториев: сигнатуры не меняются после импорта.
_KWARG_SUPPORT: dict[tuple[str, str, str], bool] = {}


def _supports_kwarg(func: Callable[..., Any], name: str) -> bool:
    """Есть ли у функции репозитория именованный аргумент `name`.

    Нужно для мягкой стыковки с репозиториями: пока миграции V2 не применены,
    у `list_folders` нет параметра `section`, и вызов с ним упал бы TypeError.
    """

    key = (getattr(func, "__module__", ""), getattr(func, "__qualname__", repr(func)), name)
    cached = _KWARG_SUPPORT.get(key)
    if cached is not None:
        return cached

    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover — экзотические callable
        logger.debug("Не удалось прочитать сигнатуру %r", func, exc_info=True)
        return False

    supported = name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    _KWARG_SUPPORT[key] = supported
    return supported


async def _load_folders(user_id: int, section: str) -> list[dict]:
    """Папки пользователя нужного раздела (в схеме V1 раздел один — «music»)."""

    if _supports_kwarg(folders_repo.list_folders, "section"):
        return await folders_repo.list_folders(user_id, section=section)

    if section != DEFAULT_SECTION:
        # Схема V1: колонки `section` ещё нет, все папки музыкальные.
        logger.debug("Раздел «%s» недоступен в текущей схеме папок", section)
        return []
    return await folders_repo.list_folders(user_id)


def _clean_artist_ids(artist_ids: Sequence[int] | None) -> list[int]:
    """Отбирает корректные идентификаторы исполнителей без дублей, сохраняя порядок."""

    if not artist_ids:
        return []

    result: list[int] = []
    seen: set[int] = set()
    for raw in artist_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("Некорректный идентификатор исполнителя %r — пропущен", raw)
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


async def _fallback_intersection(user_id: int, artist_ids: Sequence[int], limit: int) -> list[dict]:
    """Пересечение по исполнителям на схеме V1 (без таблицы `track_artists`).

    В схеме V1 у трека ровно один исполнитель (`tracks.artist_id`), поэтому
    пересечение по двум и более исполнителям там пустое по определению.
    """

    per_artist: list[list[dict]] = []
    for artist_id in artist_ids:
        per_artist.append(
            await tracks_repo.list_tracks(user_id, artist_id=artist_id, limit=limit, offset=0)
        )

    if not per_artist:
        return []

    common: set[int] = set()
    for index, tracks in enumerate(per_artist):
        ids = {track["id"] for track in tracks}
        common = ids if index == 0 else (common & ids)
        if not common:
            return []

    return [track for track in per_artist[0] if track["id"] in common]


async def _tracks_by_artists(user_id: int, artist_ids: Sequence[int], limit: int) -> list[dict]:
    """Треки, у которых есть ВСЕ перечисленные исполнители (пересечение)."""

    finder = getattr(tracks_repo, "tracks_by_artists_intersection", None)
    if finder is None:
        logger.warning(
            "Репозиторий треков не поддерживает пересечение по исполнителям — "
            "использую основного исполнителя трека (схема V1)"
        )
        tracks = await _fallback_intersection(user_id, artist_ids, limit)
    else:
        tracks = await finder(user_id, artist_ids, limit=limit, offset=0)

    if len(tracks) >= limit:
        logger.warning(
            "Фильтр по исполнителям %s упёрся в предел %d треков — часть библиотеки "
            "не участвует в поиске",
            list(artist_ids),
            limit,
        )
    return list(tracks)


async def _selected_artists(user_id: int, artist_ids: Sequence[int]) -> list[dict]:
    """Выбранные фильтром исполнители (для отображения «чипов» в результатах)."""

    result: list[dict] = []
    for artist_id in artist_ids:
        artist = await artists_repo.get_artist(user_id, artist_id)
        if artist is None:
            logger.warning("Исполнитель %s не найден у пользователя %s", artist_id, user_id)
            continue
        result.append(dict(artist, score=_FILTER_SCORE))
    return result


def _albums_of_tracks(albums: Sequence[dict], tracks: Sequence[dict]) -> list[dict]:
    """Альбомы, встречающиеся среди указанных треков (порядок исходного списка)."""

    album_ids = {track.get("album_id") for track in tracks if track.get("album_id")}
    if not album_ids:
        return []
    return [album for album in albums if album.get("id") in album_ids]


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #

#: Текстовые поля трека, участвующие в нечётком сравнении. `artist_names` —
#: агрегат состава из `track_artists`: колонка `tracks.artist` хранит текст,
#: который переименование исполнителя намеренно не трогает (ТЗ п. 10), поэтому
#: без агрегата треки не находились бы по новому имени и по приглашённым
#: исполнителям.
_TRACK_TEXT_FIELDS: Final[tuple[str, ...]] = ("title", "artist", "artist_names", "album")

#: Кандидаты для нечёткого поиска треков: тот же агрегат имён, что и в
#: репозитории треков (`_ARTIST_NAMES_SQL`), чтобы оба поиска V2 отвечали
#: одинаково.
_TRACK_CANDIDATES_SQL: Final[str] = f"""
    SELECT t.id, t.title, t.artist, t.album,
           (SELECT GROUP_CONCAT(ta_names.name, '{tracks_repo.ARTIST_NAME_SEPARATOR}')
              FROM (SELECT a_agg.name AS name
                      FROM track_artists ta_agg
                      JOIN artists a_agg ON a_agg.id = ta_agg.artist_id
                     WHERE ta_agg.track_id = t.id
                     ORDER BY ta_agg.position, a_agg.id) AS ta_names) AS artist_names
      FROM tracks t
     WHERE t.user_id = ?
"""

#: Запасной вариант для схемы V1, где таблицы `track_artists` ещё нет.
_TRACK_CANDIDATES_SQL_V1: Final[str] = (
    "SELECT id, title, artist, album FROM tracks WHERE user_id = ?"
)


async def _track_candidates(user_id: int) -> list[dict]:
    """Строки-кандидаты для нечёткого поиска треков (с составом исполнителей)."""

    try:
        return await db.fetch_all(_TRACK_CANDIDATES_SQL, (user_id,))
    except Exception:
        logger.warning(
            "Состав исполнителей недоступен, ищу только по полям трека (схема V1)",
            exc_info=True,
        )
        return await db.fetch_all(_TRACK_CANDIDATES_SQL_V1, (user_id,))


async def search_tracks(
    user_id: int,
    query: str,
    limit: int = 30,
    threshold: int | None = None,
) -> list[dict]:
    """Нечёткий поиск треков. Возвращает трек-dict с добавленным полем "score"."""

    query_variants = variants(query)
    max_items = _normalize_limit(limit)
    if not query_variants or max_items == 0:
        return []

    effective_threshold = await _resolve_threshold(user_id, threshold)
    rows = await _track_candidates(user_id)
    if not rows:
        return []

    prepared_variants = [_fold(variant) for variant in query_variants]
    candidates = [
        _candidate_text(*(row.get(field) for field in _TRACK_TEXT_FIELDS))
        for row in rows
    ]
    scores = await _score_candidates(prepared_variants, candidates, effective_threshold)
    if not scores:
        return []

    matched = [(rows[index], value) for index, value in scores.items()]
    matched.sort(
        key=lambda item: (
            -item[1],
            (item[0].get("title") or "").casefold(),
            item[0].get("id") or 0,
        )
    )
    matched = matched[:max_items]

    score_by_id = {row["id"]: value for row, value in matched}
    ordered_ids = [row["id"] for row, _ in matched]

    full_tracks = await tracks_repo.get_tracks_by_ids(user_id, ordered_ids)
    tracks_by_id = {track["id"]: track for track in full_tracks}

    result: list[dict] = []
    for track_id in ordered_ids:
        track = tracks_by_id.get(track_id)
        if track is None:
            continue
        item = dict(track)
        item["score"] = score_by_id[track_id]
        result.append(item)

    logger.debug(
        "Поиск треков (%s): найдено %d из %d", query_variants[0], len(result), len(rows)
    )
    return result


async def _search_named(
    items: Sequence[dict],
    query_variants: Sequence[str],
    *,
    text_fields: Sequence[str],
    sort_field: str,
    limit: int,
    threshold: int,
) -> list[dict]:
    """Общий нечёткий поиск по готовому списку словарей (альбомы/исполнители/папки)."""

    if not items or limit == 0:
        return []

    prepared_variants = [_fold(variant) for variant in query_variants]
    candidates = [
        _candidate_text(*(item.get(field) for field in text_fields)) for item in items
    ]
    scores = await _score_candidates(prepared_variants, candidates, threshold)
    if not scores:
        return []

    matched = [(items[index], value) for index, value in scores.items()]
    matched.sort(
        key=lambda entry: (
            -entry[1],
            (entry[0].get(sort_field) or "").casefold(),
            entry[0].get("id") or 0,
        )
    )
    return [dict(item, score=value) for item, value in matched[:limit]]


async def search_albums(
    user_id: int,
    query: str,
    limit: int = 30,
    threshold: int | None = None,
) -> list[dict]:
    """Нечёткий поиск альбомов (групп). При равном score — алфавит по названию."""

    query_variants = variants(query)
    max_items = _normalize_limit(limit)
    if not query_variants or max_items == 0:
        return []

    effective_threshold = await _resolve_threshold(user_id, threshold)
    albums = await albums_repo.list_albums(user_id)
    return await _search_named(
        albums,
        query_variants,
        text_fields=("title", "artist_name"),
        sort_field="title",
        limit=max_items,
        threshold=effective_threshold,
    )


async def search_artists(
    user_id: int,
    query: str,
    limit: int = 30,
    threshold: int | None = None,
) -> list[dict]:
    """Нечёткий поиск исполнителей. При равном score — алфавит по имени."""

    query_variants = variants(query)
    max_items = _normalize_limit(limit)
    if not query_variants or max_items == 0:
        return []

    effective_threshold = await _resolve_threshold(user_id, threshold)
    artists = await artists_repo.list_artists(user_id)
    return await _search_named(
        artists,
        query_variants,
        text_fields=("name",),
        sort_field="name",
        limit=max_items,
        threshold=effective_threshold,
    )


async def search_folders(
    user_id: int,
    query: str,
    limit: int = 30,
    threshold: int | None = None,
    *,
    section: str = DEFAULT_SECTION,
) -> list[dict]:
    """Нечёткий поиск папок. При равном score — алфавит по названию.

    `section` — раздел библиотеки (`music` | `other`). На схеме V1, где раздела
    у папок ещё нет, все папки считаются музыкальными.
    """

    query_variants = variants(query)
    max_items = _normalize_limit(limit)
    if not query_variants or max_items == 0:
        return []

    effective_threshold = await _resolve_threshold(user_id, threshold)
    folders = await _load_folders(user_id, _normalize_section(section))
    return await _search_named(
        folders,
        query_variants,
        text_fields=("name",),
        sort_field="name",
        limit=max_items,
        threshold=effective_threshold,
    )


async def search_all(
    user_id: int,
    query: str,
    limit: int = 20,
    threshold: int | None = None,
    *,
    artist_ids: Sequence[int] | None = None,
    section: str = DEFAULT_SECTION,
) -> dict:
    """Общий поиск: треки, альбомы, исполнители и папки в одном ответе.

    :param artist_ids: фильтр по нескольким исполнителям. Если задан, сначала
        берётся ПЕРЕСЕЧЕНИЕ треков по этим исполнителям (у трека должны быть все
        перечисленные), и только потом внутри полученного множества работает
        нечёткий поиск. Альбомы в этом режиме ограничены альбомами отобранных
        треков, а список исполнителей — самим фильтром.
    :param section: раздел библиотеки (`music` | `other`) — влияет на поиск папок.
    :param limit: сколько элементов вернуть в каждом списке.
    :param threshold: порог релевантности; по умолчанию — из настроек пользователя.

    Папки ищутся ВСЕГДА, в том числе при активном фильтре по исполнителям (ТЗ п. 17).
    Пустой запрос без фильтра даёт пустые списки; пустой запрос с фильтром —
    все треки выбранных исполнителей.
    """

    normalized_query = " ".join(str(query or "").split())
    result: dict[str, Any] = {
        "query": normalized_query,
        "tracks": [],
        "albums": [],
        "artists": [],
        "folders": [],
    }

    query_variants = variants(query)
    max_items = _normalize_limit(limit)
    selected_ids = _clean_artist_ids(artist_ids)
    active_section = _normalize_section(section)

    if max_items == 0 or (not query_variants and not selected_ids):
        return result

    # Порог считаем один раз на весь запрос, чтобы не дёргать настройки четырежды.
    effective_threshold = await _resolve_threshold(user_id, threshold)

    if selected_ids:
        base_tracks = await _tracks_by_artists(
            user_id, selected_ids, MAX_ARTIST_FILTER_TRACKS
        )
        albums = await albums_repo.list_albums(user_id)
        scoped_albums = _albums_of_tracks(albums, base_tracks)

        if query_variants:
            result["tracks"] = await _search_named(
                base_tracks,
                query_variants,
                text_fields=_TRACK_TEXT_FIELDS,
                sort_field="title",
                limit=max_items,
                threshold=effective_threshold,
            )
            result["albums"] = await _search_named(
                scoped_albums,
                query_variants,
                text_fields=("title", "artist_name"),
                sort_field="title",
                limit=max_items,
                threshold=effective_threshold,
            )
        else:
            result["tracks"] = [
                dict(track, score=_FILTER_SCORE) for track in base_tracks[:max_items]
            ]
            scoped_albums.sort(key=lambda album: (album.get("title") or "").casefold())
            result["albums"] = [
                dict(album, score=_FILTER_SCORE) for album in scoped_albums[:max_items]
            ]

        result["artists"] = (await _selected_artists(user_id, selected_ids))[:max_items]
    else:
        result["tracks"] = await search_tracks(
            user_id, query, limit=max_items, threshold=effective_threshold
        )
        result["albums"] = await search_albums(
            user_id, query, limit=max_items, threshold=effective_threshold
        )
        result["artists"] = await search_artists(
            user_id, query, limit=max_items, threshold=effective_threshold
        )

    result["folders"] = await search_folders(
        user_id,
        query,
        limit=max_items,
        threshold=effective_threshold,
        section=active_section,
    )

    logger.debug(
        "Общий поиск «%s» (раздел %s, исполнители %s): треков %d, альбомов %d, "
        "исполнителей %d, папок %d",
        normalized_query,
        active_section,
        selected_ids or "—",
        len(result["tracks"]),
        len(result["albums"]),
        len(result["artists"]),
        len(result["folders"]),
    )
    return result
