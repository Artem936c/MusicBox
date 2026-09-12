"""Сервис рекомендаций исполнителей (раздел 3 контракта V2).

Рекомендации строятся ТОЛЬКО по данным самой базы MusicBox: прослушивания
пользователей, жанры их треков и совпадения вкусов. Внешние сервисы не
опрашиваются, несуществующие исполнители не выдумываются.

Две категории из ТЗ:

* «Популярные» — кандидаты с наибольшим числом прослушиваний в базе;
* «Менее известные» — кандидаты с наименьшим, но ненулевым числом прослушиваний.

Списки НЕ пересекаются: исполнитель попадает ровно в одну категорию.

База пока небольшая (мало пользователей и прослушиваний), поэтому предусмотрен
честный деградационный режим: недостающие позиции добиваются НЕПРОСЛУШАННЫМИ
исполнителями из библиотеки самого пользователя с пометкой «из вашей
библиотеки», а в ``shortfall`` и ``note`` по-русски сообщается, сколько позиций
не хватило и почему.

Данные о кандидатах приходят из репозитория
:mod:`backend.db.repositories.recommendations`; поля строк читаются терпимо
(допускаются синонимы ключей), чтобы сервис не ломался от мелких расхождений
в наименованиях колонок агрегирующих запросов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

from backend.db.repositories import artists as artists_repo
from backend.db.repositories import recommendations as reco_repo
from backend.db.repositories import tracks as tracks_repo
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

__all__ = [
    "Recommendation",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "POPULAR_LABEL",
    "UNDERGROUND_LABEL",
    "REASON_LIBRARY",
    "NO_HISTORY_NOTE",
    "build",
]

# --------------------------------------------------------------------------- #
# Константы алгоритма
# --------------------------------------------------------------------------- #

#: Сколько позиций требуется в каждой категории по ТЗ (не менее 15).
DEFAULT_LIMIT: Final[int] = 15

#: Верхняя граница limit — защита от чрезмерных выборок из API.
MAX_LIMIT: Final[int] = 50

#: Сколько жанров пользователя учитывается при подборе (шаг 2 алгоритма).
TOP_GENRES: Final[int] = 5

#: Сколько кандидатов запрашивается у каждого источника (шаг 3 алгоритма).
CANDIDATE_LIMIT: Final[int] = 200

#: Сколько треков-примеров прикладывается к рекомендации.
SAMPLE_TRACKS: Final[int] = 5

# Веса скоринга (шаг 4 алгоритма):
# 3 * совпадение_жанра + 2 * число_общих_слушателей + 1 * нормированная_популярность.
WEIGHT_GENRE: Final[float] = 3.0
WEIGHT_CO_LISTENERS: Final[float] = 2.0
WEIGHT_POPULARITY: Final[float] = 1.0

#: Русские названия категорий (используются в note и в интерфейсах).
POPULAR_LABEL: Final[str] = "Популярные"
UNDERGROUND_LABEL: Final[str] = "Менее известные"

#: Пометка деградационного режима — точный текст из ТЗ.
REASON_LIBRARY: Final[str] = "из вашей библиотеки"

REASON_SIMILAR_TASTE: Final[str] = "слушают те, у кого похожие вкусы"
REASON_POPULAR: Final[str] = "часто слушают другие пользователи"

#: Подсказка, если пользователь ещё ничего не слушал.
NO_HISTORY_NOTE: Final[str] = (
    "Послушайте несколько треков, и я подберу похожих исполнителей."
)

#: Честная причина неполных списков.
SMALL_BASE_REASON: Final[str] = "в базе мало пользователей и прослушиваний"

# Синонимы ключей в строках репозитория.
_NAME_KEYS: Final[tuple[str, ...]] = ("name", "artist_name", "title")
_NORMALIZED_KEYS: Final[tuple[str, ...]] = (
    "normalized_name",
    "normalized",
    "artist_normalized_name",
)
_TOTAL_PLAYS_KEYS: Final[tuple[str, ...]] = ("total_plays", "plays", "play_count")
_LISTENERS_KEYS: Final[tuple[str, ...]] = ("listeners", "listener_count", "users")
_SHARED_KEYS: Final[tuple[str, ...]] = (
    "shared_listeners",
    "common_listeners",
    "co_listeners",
    "shared",
)
_SHARED_ARTIST_KEYS: Final[tuple[str, ...]] = (
    "shared_artist",
    "shared_artist_name",
    "via_artist",
    "common_artist",
    "source_artist",
)
_GENRE_KEYS: Final[tuple[str, ...]] = ("genre", "matched_genre", "genre_name")
_GENRES_KEYS: Final[tuple[str, ...]] = ("genres", "genre_names")


# --------------------------------------------------------------------------- #
# Публичная модель
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Recommendation:
    """Один рекомендованный исполнитель.

    ``local_artist_id`` заполнен, если исполнитель уже есть в библиотеке
    пользователя; тогда же в ``sample_track_ids`` попадают id его треков —
    их можно сразу поставить в очередь плеера. Треки чужих пользователей
    сюда не попадают никогда.
    """

    name: str
    normalized_name: str
    total_plays: int
    listeners: int
    reason: str
    local_artist_id: int | None = None
    sample_track_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Представление для API и шаблонов бота."""
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "total_plays": self.total_plays,
            "listeners": self.listeners,
            "reason": self.reason,
            "local_artist_id": self.local_artist_id,
            "sample_track_ids": list(self.sample_track_ids),
        }


@dataclass(slots=True)
class _Candidate:
    """Внутренний кандидат до разбиения по категориям (наружу не отдаётся)."""

    name: str
    normalized_name: str
    total_plays: int = 0
    listeners: int = 0
    shared_listeners: int = 0
    shared_artist: str | None = None
    genre: str | None = None
    genres: list[str] = field(default_factory=list)
    genre_matched: bool = False
    genre_weight: float = 0.0
    score: float = 0.0


# --------------------------------------------------------------------------- #
# Мелкие помощники
# --------------------------------------------------------------------------- #


def _as_int(value: Any, default: int = 0) -> int:
    """Безопасное приведение к int (None и мусор -> default)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_int(row: Mapping[str, Any], keys: Sequence[str], default: int = 0) -> int:
    """Первое непустое целое значение из строки по списку синонимов ключа."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return _as_int(value, default)
    return default


def _read_str(row: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """Первая непустая строка по списку синонимов ключа (список -> первый элемент)."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return None


def _read_str_list(row: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    """Все непустые строки по списку синонимов ключа, без дублей и по порядку."""
    result: list[str] = []
    seen: set[str] = set()

    def _append(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)

    for key in keys:
        value = row.get(key)
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _append(item)
        else:
            _append(value)
    return result


def _clamp_limit(value: Any) -> int:
    """Приводит limit к диапазону 1..MAX_LIMIT."""
    limit = _as_int(value, DEFAULT_LIMIT)
    clamped = max(1, min(limit, MAX_LIMIT))
    if clamped != limit:
        logger.warning(
            "limit=%r вне диапазона 1..%s — использую %s", value, MAX_LIMIT, clamped
        )
    return clamped


# --------------------------------------------------------------------------- #
# Шаги 1-2: что пользователь уже слушал
# --------------------------------------------------------------------------- #


async def _played_names(user_id: int) -> set[str]:
    """Нормализованные имена исполнителей, которых пользователь уже слушал."""
    try:
        raw = await reco_repo.played_artist_names(user_id)
    except Exception:
        logger.exception(
            "Не удалось получить прослушанных исполнителей (user_id=%s)", user_id
        )
        raise
    names = {normalize_name(str(item)) for item in (raw or ())}
    return {name for name in names if name}


async def _user_genres(user_id: int) -> dict[str, int]:
    """Жанры пользователя: название -> число прослушиваний (пусто при сбое).

    Жанры библиотеки, которые пользователь ещё не слушал, приходят со значением
    0 и тоже сохраняются: в маленькой базе это единственная жанровая подсказка.
    """
    try:
        raw = await reco_repo.user_genres(user_id)
    except Exception:
        logger.exception("Не удалось получить жанры пользователя %s", user_id)
        return {}

    if not isinstance(raw, Mapping):
        logger.warning(
            "user_genres вернул %s вместо словаря — жанры не учитываю",
            type(raw).__name__,
        )
        return {}

    counts: dict[str, int] = {}
    for genre, plays in raw.items():
        name = str(genre or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + max(0, _as_int(plays))
    return counts


def _top_genres(counts: Mapping[str, int]) -> list[str]:
    """Топ-5 жанров пользователя (по числу прослушиваний, затем по алфавиту)."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
    return [genre for genre, _ in ranked[:TOP_GENRES]]


def _genre_weights(counts: Mapping[str, int]) -> dict[str, float]:
    """Вес топ-жанров в диапазоне 0..1: у самого прослушиваемого — 1.0.

    Наличие ключа = жанр совпал с профилем пользователя (даже с весом 0).
    Если прослушиваний по жанрам ещё нет, все топ-жанры равнозначны (вес 1.0).
    """
    top = _top_genres(counts)
    if not top:
        return {}
    best = max(counts[genre] for genre in top)
    weights: dict[str, float] = {}
    for genre in top:
        key = normalize_name(genre)
        if key:
            weights[key] = counts[genre] / best if best > 0 else 1.0
    return weights


# --------------------------------------------------------------------------- #
# Шаг 3: кандидаты
# --------------------------------------------------------------------------- #


def _candidate_from_row(row: Any) -> _Candidate | None:
    """Строка репозитория -> кандидат (None, если имя пустое или строка чужая)."""
    if not isinstance(row, Mapping):
        logger.debug("Пропускаю кандидата неизвестного вида: %s", type(row).__name__)
        return None

    name = _read_str(row, _NAME_KEYS) or ""
    normalized = _read_str(row, _NORMALIZED_KEYS) or normalize_name(name)
    if not normalized:
        return None

    primary_genre = _read_str(row, _GENRE_KEYS)
    genres = _read_str_list(row, _GENRES_KEYS)
    if primary_genre and primary_genre.casefold() not in {
        item.casefold() for item in genres
    }:
        genres.insert(0, primary_genre)

    return _Candidate(
        name=name or normalized,
        normalized_name=normalized,
        total_plays=max(0, _read_int(row, _TOTAL_PLAYS_KEYS)),
        listeners=max(0, _read_int(row, _LISTENERS_KEYS)),
        shared_listeners=max(0, _read_int(row, _SHARED_KEYS)),
        shared_artist=_read_str(row, _SHARED_ARTIST_KEYS),
        genre=primary_genre or (genres[0] if genres else None),
        genres=genres,
    )


def _merge_candidate(target: _Candidate, extra: _Candidate) -> None:
    """Дополняет кандидата данными второго источника (жанры + общие слушатели)."""
    target.total_plays = max(target.total_plays, extra.total_plays)
    target.listeners = max(target.listeners, extra.listeners)
    target.shared_listeners = max(target.shared_listeners, extra.shared_listeners)
    target.shared_artist = target.shared_artist or extra.shared_artist
    target.genre = target.genre or extra.genre

    known = {item.casefold() for item in target.genres}
    for genre in extra.genres:
        if genre.casefold() not in known:
            known.add(genre.casefold())
            target.genres.append(genre)


def _add_rows(
    store: dict[str, _Candidate], rows: Iterable[Any], played: set[str]
) -> None:
    """Складывает строки источника в общий словарь кандидатов без дублей."""
    for row in rows:
        candidate = _candidate_from_row(row)
        if candidate is None:
            continue
        if candidate.normalized_name in played:
            continue  # шаг 3: всё прослушанное исключаем
        existing = store.get(candidate.normalized_name)
        if existing is None:
            store[candidate.normalized_name] = candidate
        else:
            _merge_candidate(existing, candidate)


async def _fetch_co_listeners(user_id: int) -> list[Any]:
    """Кандидаты по совпадению вкусов (коллаборативная фильтрация)."""
    try:
        rows = await reco_repo.co_listener_artists(user_id, limit=CANDIDATE_LIMIT)
    except Exception:
        logger.exception(
            "Не удалось подобрать исполнителей по совпадению вкусов (user_id=%s)",
            user_id,
        )
        return []
    return list(rows or [])


async def _fetch_by_genres(genres: Sequence[str], exclude: set[str]) -> list[Any]:
    """Кандидаты по жанрам пользователя."""
    if not genres:
        return []
    try:
        rows = await reco_repo.artists_by_genre(genres, exclude, limit=CANDIDATE_LIMIT)
    except Exception:
        logger.exception("Не удалось подобрать исполнителей по жанрам %s", list(genres))
        return []
    return list(rows or [])


# --------------------------------------------------------------------------- #
# Шаги 4-5: скоринг и разбиение по категориям
# --------------------------------------------------------------------------- #


def _score_candidates(
    candidates: list[_Candidate], genre_weights: Mapping[str, float]
) -> None:
    """Скор = 3*жанр + 2*общие слушатели + 1*нормированная популярность.

    Из всех жанров кандидата берётся самый «весомый» для пользователя — он же
    показывается в объяснении.
    """
    max_plays = max((item.total_plays for item in candidates), default=0)
    for candidate in candidates:
        matched: str | None = None
        weight = 0.0
        for genre in candidate.genres:
            found = genre_weights.get(normalize_name(genre))
            if found is None:
                continue
            if matched is None or found > weight:
                matched, weight = genre, found

        candidate.genre_matched = matched is not None
        candidate.genre_weight = weight
        if matched is not None:
            candidate.genre = matched

        popularity = candidate.total_plays / max_plays if max_plays > 0 else 0.0
        candidate.score = (
            WEIGHT_GENRE * weight
            + WEIGHT_CO_LISTENERS * float(candidate.shared_listeners)
            + WEIGHT_POPULARITY * popularity
        )


def _is_library_only(candidate: _Candidate, local_artist_id: int | None) -> bool:
    """Кандидат — просто непрослушанный исполнитель из библиотеки пользователя.

    Никто в базе его ни разу не слушал, а у пользователя он уже есть: это не
    находка, а материал для деградационного режима (шаг 6) с честной пометкой
    «из вашей библиотеки».
    """
    return (
        local_artist_id is not None
        and candidate.total_plays <= 0
        and candidate.listeners <= 0
        and candidate.shared_listeners <= 0
    )


def _reason(candidate: _Candidate) -> str:
    """Человекочитаемое объяснение: что именно привело исполнителя в подборку."""
    social = WEIGHT_CO_LISTENERS * candidate.shared_listeners
    genre = WEIGHT_GENRE * candidate.genre_weight
    together = (
        f"слушают вместе с «{candidate.shared_artist}»"
        if candidate.shared_artist
        else REASON_SIMILAR_TASTE
    )

    if candidate.shared_listeners > 0 and social >= genre:
        return together
    if candidate.genre_matched and candidate.genre:
        return f"жанр: {candidate.genre}"
    if candidate.shared_listeners > 0:
        return together
    return REASON_POPULAR


def _split(
    candidates: list[_Candidate], limit: int
) -> tuple[list[_Candidate], list[_Candidate]]:
    """Делит кандидатов на «Популярные» и «Менее известные» без пересечений.

    В категории попадают ТОЛЬКО кандидаты С ПРОСЛУШИВАНИЯМИ: самые слушаемые — в
    «Популярные», наименее слушаемые — в «Менее известные». Если их меньше, чем
    нужно на две полные категории, они делятся примерно пополам, иначе одна
    категория забрала бы всех и вторая осталась бы пустой.

    Кандидату, которого в базе ещё никто не слушал, не подходит ни одна
    категория: в «Менее известные» по контракту нужен total_plays > 0, а
    «Популярные» — это самые слушаемые, и чужой молчаливый исполнитель выдавал бы
    там себя за находку. Свободные места остаются шагу 6 (добивка «из вашей
    библиотеки»), а всё, что не удалось набрать, честно уходит в shortfall и note.
    """
    positive = sorted(
        [item for item in candidates if item.total_plays > 0],
        key=lambda item: (-item.total_plays, -item.score, item.normalized_name),
    )
    silent = len(candidates) - len(positive)
    if silent:
        logger.debug(
            "Кандидатов без прослушиваний: %s — в категории не беру, "
            "места оставляю добивке из библиотеки",
            silent,
        )

    if not positive:
        quota = 0
    elif len(positive) >= 2 * limit:
        quota = limit
    else:
        quota = min(limit, max(1, (len(positive) + 1) // 2))

    popular = positive[:quota]
    underground = sorted(
        positive[quota:],
        key=lambda item: (item.total_plays, -item.score, item.normalized_name),
    )[:limit]
    return popular, underground


# --------------------------------------------------------------------------- #
# Сборка ответа
# --------------------------------------------------------------------------- #


async def _sample_track_ids(user_id: int, artist_id: int | None) -> list[int]:
    """id треков исполнителя в библиотеке пользователя (пусто, если его там нет)."""
    if not artist_id:
        return []
    try:
        rows = await tracks_repo.list_tracks(
            user_id,
            artist_id=int(artist_id),
            order="play_count_desc",
            limit=SAMPLE_TRACKS,
        )
    except Exception:
        logger.exception(
            "Не удалось получить треки исполнителя id=%s (user_id=%s)",
            artist_id,
            user_id,
        )
        return []
    return [_as_int(row.get("id")) for row in rows if row.get("id") is not None]


async def _to_recommendation(
    user_id: int,
    candidate: _Candidate,
    local_by_norm: Mapping[str, dict[str, Any]],
) -> Recommendation:
    """Кандидат -> публичная рекомендация (с привязкой к библиотеке, если есть)."""
    local = local_by_norm.get(candidate.normalized_name)
    local_id = (_as_int(local.get("id")) or None) if local else None
    display_name = candidate.name
    if local and str(local.get("name") or "").strip():
        display_name = str(local["name"]).strip()

    return Recommendation(
        name=display_name,
        normalized_name=candidate.normalized_name,
        total_plays=candidate.total_plays,
        listeners=candidate.listeners,
        reason=_reason(candidate),
        local_artist_id=local_id,
        sample_track_ids=await _sample_track_ids(user_id, local_id),
    )


async def _library_recommendation(
    user_id: int, artist: Mapping[str, Any]
) -> Recommendation:
    """Рекомендация деградационного режима: непрослушанный свой исполнитель."""
    artist_id = _as_int(artist.get("id")) or None
    name = str(artist.get("name") or "").strip()
    normalized = str(artist.get("normalized_name") or "") or normalize_name(name)
    return Recommendation(
        name=name or normalized,
        normalized_name=normalized,
        total_plays=0,
        listeners=0,
        reason=REASON_LIBRARY,
        local_artist_id=artist_id,
        sample_track_ids=await _sample_track_ids(user_id, artist_id),
    )


async def _library_artists(user_id: int) -> list[dict[str, Any]]:
    """Все исполнители пользователя (пустой список при сбое запроса)."""
    try:
        return list(await artists_repo.list_artists(user_id) or [])
    except Exception:
        logger.exception("Не удалось получить библиотеку исполнителей %s", user_id)
        return []


def _unplayed_library_artists(
    library: Sequence[Mapping[str, Any]], played: set[str], used: set[str]
) -> list[dict[str, Any]]:
    """Непрослушанные исполнители пользователя для добивки категорий.

    Сначала — те, у кого больше треков в библиотеке: их пользователю интереснее
    открыть. Уже попавшие в подборку (``used``) и прослушанные исключаются.
    """
    result: list[dict[str, Any]] = []
    for artist in library:
        name = str(artist.get("name") or "").strip()
        normalized = str(artist.get("normalized_name") or "") or normalize_name(name)
        if not normalized or normalized in played or normalized in used:
            continue
        if artist.get("is_listened"):
            continue
        if _as_int(artist.get("play_count")) > 0:
            continue
        if _as_int(artist.get("total_plays")) > 0:
            continue
        result.append(dict(artist))

    result.sort(
        key=lambda item: (
            -_as_int(item.get("track_count")),
            str(item.get("name") or "").casefold(),
        )
    )
    return result


def _share_fillers(
    fillers: Sequence[Mapping[str, Any]],
    *,
    popular_size: int,
    underground_size: int,
    limit: int,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Раздаёт добивку так, чтобы обе категории росли равномерно.

    Очередной исполнитель уходит в ту категорию, где сейчас меньше позиций;
    при равенстве — в «Популярные». Как только категория набрала ``limit``,
    она из раздачи выбывает.
    """
    to_popular: list[Mapping[str, Any]] = []
    to_underground: list[Mapping[str, Any]] = []
    sizes = [popular_size, underground_size]

    for artist in fillers:
        room_popular = limit - sizes[0]
        room_underground = limit - sizes[1]
        if room_popular <= 0 and room_underground <= 0:
            break
        if room_popular > 0 and (room_underground <= 0 or sizes[0] <= sizes[1]):
            to_popular.append(artist)
            sizes[0] += 1
        else:
            to_underground.append(artist)
            sizes[1] += 1

    return to_popular, to_underground


def _drop_intersections(
    popular: list[Recommendation], underground: list[Recommendation]
) -> list[Recommendation]:
    """Страховка от пересечения категорий: дубль остаётся только в «Популярных»."""
    seen = {item.normalized_name for item in popular}
    result: list[Recommendation] = []
    for item in underground:
        if item.normalized_name in seen:
            logger.warning(
                "Исполнитель «%s» попал в обе категории — оставляю только в «%s»",
                item.name,
                POPULAR_LABEL,
            )
            continue
        seen.add(item.normalized_name)
        result.append(item)
    return result


def _count_library(items: Sequence[Recommendation]) -> int:
    """Сколько позиций в списке взято из библиотеки самого пользователя."""
    return sum(1 for item in items if item.reason == REASON_LIBRARY)


def _compose_note(
    *,
    limit: int,
    filled_popular: int,
    filled_underground: int,
    shortfall: Mapping[str, int],
) -> str | None:
    """Честное пояснение: сколько добрано из библиотеки и сколько не хватило."""
    missing_popular = _as_int(shortfall.get("popular"))
    missing_underground = _as_int(shortfall.get("underground"))
    filled_total = filled_popular + filled_underground
    missing_total = missing_popular + missing_underground
    if not filled_total and not missing_total:
        return None

    parts = [
        f"Пока {SMALL_BASE_REASON}, поэтому набрать по {limit} новых исполнителей "
        "в обе категории не удалось."
    ]
    if filled_total:
        parts.append(
            "Добавил непрослушанных исполнителей из вашей библиотеки: "
            f"«{POPULAR_LABEL}» — {filled_popular}, "
            f"«{UNDERGROUND_LABEL}» — {filled_underground}."
        )
    if missing_total:
        parts.append(
            f"До {limit} позиций не хватает: "
            f"«{POPULAR_LABEL}» — {missing_popular}, "
            f"«{UNDERGROUND_LABEL}» — {missing_underground}."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


async def build(user_id: int, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Подбирает две категории рекомендованных исполнителей для пользователя.

    Возвращает ``{"popular": [Recommendation, ...], "underground": [...],
    "shortfall": {"popular": int, "underground": int}, "note": str | None}``.

    ``shortfall`` — сколько позиций не удалось набрать до ``limit`` даже после
    добивки из библиотеки пользователя; ``note`` — русское пояснение (None,
    если обе категории заполнены настоящими рекомендациями).

    Если пользователь ещё ничего не слушал, обе категории пустые, а в ``note``
    подсказка «Послушайте несколько треков…».
    """
    user_id = int(user_id)
    limit = _clamp_limit(limit)

    # Шаг 1. Что пользователь уже слушал.
    played = await _played_names(user_id)
    if not played:
        logger.info(
            "У пользователя %s ещё нет прослушиваний — рекомендации не строим", user_id
        )
        return {
            "popular": [],
            "underground": [],
            "shortfall": {"popular": limit, "underground": limit},
            "note": NO_HISTORY_NOTE,
        }

    # Шаг 2. Любимые жанры (топ-5).
    genre_counts = await _user_genres(user_id)
    top_genres = _top_genres(genre_counts)
    genre_weights = _genre_weights(genre_counts)

    # Библиотека пользователя нужна и для привязки кандидатов, и для деградации.
    library = await _library_artists(user_id)
    local_by_norm: dict[str, dict[str, Any]] = {}
    for artist in library:
        key = str(artist.get("normalized_name") or "") or normalize_name(
            str(artist.get("name") or "")
        )
        if key:
            local_by_norm.setdefault(key, dict(artist))

    # Шаг 3. Кандидаты: совпадение вкусов + жанры, всё прослушанное исключено.
    store: dict[str, _Candidate] = {}
    _add_rows(store, await _fetch_co_listeners(user_id), played)
    _add_rows(store, await _fetch_by_genres(top_genres, played), played)

    candidates: list[_Candidate] = []
    library_only = 0
    for candidate in store.values():
        local = local_by_norm.get(candidate.normalized_name)
        local_id = (_as_int(local.get("id")) or None) if local else None
        if _is_library_only(candidate, local_id):
            # Не находка, а свой же непрослушанный исполнитель — отдаём шагу 6.
            library_only += 1
            continue
        candidates.append(candidate)
    if library_only:
        logger.debug(
            "Кандидатов из собственной библиотеки без прослушиваний: %s — "
            "они пойдут в деградационный режим",
            library_only,
        )

    # Шаг 4. Скоринг.
    _score_candidates(candidates, genre_weights)

    # Шаг 5. Две непересекающиеся категории.
    popular_candidates, underground_candidates = _split(candidates, limit)

    popular = [
        await _to_recommendation(user_id, item, local_by_norm)
        for item in popular_candidates
    ]
    underground = [
        await _to_recommendation(user_id, item, local_by_norm)
        for item in underground_candidates
    ]
    underground = _drop_intersections(popular, underground)

    # Шаг 6. Деградация: добиваем непрослушанными исполнителями из библиотеки.
    used = {item.normalized_name for item in popular}
    used.update(item.normalized_name for item in underground)
    fillers = _unplayed_library_artists(library, played, used)
    fill_popular, fill_underground = _share_fillers(
        fillers,
        popular_size=len(popular),
        underground_size=len(underground),
        limit=limit,
    )

    popular.extend(
        [await _library_recommendation(user_id, item) for item in fill_popular]
    )
    underground.extend(
        [await _library_recommendation(user_id, item) for item in fill_underground]
    )
    underground = _drop_intersections(popular, underground)

    shortfall = {
        "popular": max(0, limit - len(popular)),
        "underground": max(0, limit - len(underground)),
    }
    # Считаем позиции по факту пометки «из вашей библиотеки», а не по счётчикам
    # шага 6: так число в note всегда совпадает с тем, что видит пользователь.
    from_library_popular = _count_library(popular)
    from_library_underground = _count_library(underground)
    note = _compose_note(
        limit=limit,
        filled_popular=from_library_popular,
        filled_underground=from_library_underground,
        shortfall=shortfall,
    )

    logger.info(
        "Рекомендации для %s: кандидатов %s, «%s» — %s (из библиотеки %s), "
        "«%s» — %s (из библиотеки %s), не хватает %s/%s",
        user_id,
        len(candidates),
        POPULAR_LABEL,
        len(popular),
        from_library_popular,
        UNDERGROUND_LABEL,
        len(underground),
        from_library_underground,
        shortfall["popular"],
        shortfall["underground"],
    )

    return {
        "popular": popular,
        "underground": underground,
        "shortfall": shortfall,
        "note": note,
    }
