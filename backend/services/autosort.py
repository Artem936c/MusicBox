"""Автосортировка треков по папкам исполнителей.

Логика раскладки: у каждого исполнителя своя папка (`folders.is_artist_folder = 1`).
Папка ищется точно по `normalized_name`, затем нечётко (rapidfuzz, порог 88) —
только среди папок исполнителей и только при сопоставимой длине имён,
и лишь потом создаётся новая. Ошибки БД логируются и пробрасываются наверх —
молча их глотать нельзя, иначе трек «потеряется» без следа в логах.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from backend.db.repositories import albums as albums_repo
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.db.repositories import users as users_repo
from backend.errors import ValidationError
from backend.services.metadata import normalize_name, primary_artist

logger = logging.getLogger(__name__)

__all__ = [
    "SortResult",
    "FUZZY_FOLDER_THRESHOLD",
    "MAX_FUZZY_LENGTH_RATIO",
    "suggest_folder",
    "apply_autosort",
    "assign_to_folder",
]

# Порог нечёткого совпадения имени исполнителя и названия папки.
FUZZY_FOLDER_THRESHOLD = 88

# Предельное отношение длин сравниваемых имён (длинное / короткое).
# Без него подстрока даёт высокий скор, и разные исполнители попадают в одну
# папку: «Queen» -> «Queens of the Stone Age», «Ария» -> «Мария».
MAX_FUZZY_LENGTH_RATIO = 1.2

try:  # rapidfuzz — основной движок сравнения
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover — запасной вариант без зависимости
    _fuzz = None
    logger.warning(
        "rapidfuzz не установлен — нечёткий подбор папок работает в упрощённом режиме"
    )


@dataclass(slots=True)
class SortResult:
    """Итог автосортировки одного трека."""

    artist: dict | None = None
    folder: dict | None = None
    folder_created: bool = False
    applied: bool = False


def _similarity(left: str, right: str) -> float:
    """Похожесть двух нормализованных имён в диапазоне 0..100.

    Имена разной длины считаются несовпадающими: `WRatio` для них включает
    `partial_ratio` и любая подстрока даёт 90 («Дискотека» -> «Дискотека Авария»),
    поэтому берём метрику без partial-составляющей плюс ограничение на длину.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    shortest, longest = sorted((len(left), len(right)))
    if longest / shortest >= MAX_FUZZY_LENGTH_RATIO:
        return 0.0
    if _fuzz is not None:
        return float(_fuzz.token_sort_ratio(left, right))
    return SequenceMatcher(None, left, right).ratio() * 100.0


def _resolve_artist_name(artist_name: str | None, track: dict | None = None) -> str | None:
    """Основной исполнитель: из явного аргумента либо из поля трека."""
    source = artist_name if artist_name and artist_name.strip() else None
    if source is None and track is not None:
        raw = track.get("artist")
        source = raw if isinstance(raw, str) and raw.strip() else None
    if source is None:
        return None
    return primary_artist(source)


async def _artist_of_track(user_id: int, track: dict, name: str) -> dict | None:
    """Уже привязанный к треку исполнитель, если его основное имя совпадает с `name`.

    Загрузка заводит исполнителя по ПОЛНОЙ строке performer («MiyaGi & Эндшпиль»),
    а сюда приходит только основной исполнитель («MiyaGi»). Создавать под него
    вторую запись нельзя: первая осталась бы «пустышкой» с нулём треков в списке
    исполнителей, поэтому переиспользуем существующую.
    """
    try:
        artist_id = int(track.get("artist_id") or 0)
    except (TypeError, ValueError):
        return None
    if artist_id <= 0:
        return None

    try:
        existing = await artists_repo.get_artist(user_id, artist_id)
    except Exception:
        logger.exception(
            "Не удалось прочитать исполнителя id=%s (user_id=%s)", artist_id, user_id
        )
        raise
    if existing is None:
        return None

    existing_primary = primary_artist(existing.get("name"))
    if existing_primary and normalize_name(existing_primary) == normalize_name(name):
        return existing
    return None


async def _sync_track_artists(user_id: int, track: dict, artist_id: int) -> None:
    """Согласовать состав `track_artists` с основным исполнителем трека.

    `tracks_repo.update_track(artist_id=...)` пишет только денормализованное поле
    `tracks.artist_id`, а фильтр-пересечение в поиске строится исключительно по
    `track_artists`. Без синхронизации трек, разложенный автосортировкой (импорт
    из Telegram-поиска), не находился бы по фильтру «исполнитель» и приходил бы
    с пустым составом. Прежний состав сохраняется: основной исполнитель просто
    встаёт первым (`position = 0`), как того требуют миграция 003 и `create_track`.
    """
    track_id = track.get("id")
    if track_id is None:
        return

    list_artists = getattr(tracks_repo, "track_artists", None)
    set_artists = getattr(tracks_repo, "set_track_artists", None)
    if list_artists is None or set_artists is None:  # pragma: no cover — схема V1
        logger.warning(
            "Репозиторий треков не поддерживает состав исполнителей (схема V1) — "
            "трек id=%s остался только с денормализованным artist_id",
            track_id,
        )
        return

    try:
        current = await list_artists(user_id, int(track_id))
    except Exception:
        logger.exception(
            "Не удалось прочитать состав исполнителей трека id=%s (user_id=%s)",
            track_id, user_id,
        )
        raise

    current_ids = [int(item["id"]) for item in current if item.get("id")]
    if current_ids and current_ids[0] == artist_id:
        logger.debug(
            "Состав исполнителей трека id=%s уже согласован (основной id=%s)",
            track_id, artist_id,
        )
        return

    composition = [artist_id, *(other for other in current_ids if other != artist_id)]
    try:
        await set_artists(user_id, int(track_id), composition)
        refreshed = await tracks_repo.get_track(user_id, int(track_id))
    except Exception:
        logger.exception(
            "Не удалось записать состав исполнителей %s трека id=%s (user_id=%s)",
            composition, track_id, user_id,
        )
        raise

    if refreshed:
        track.update(refreshed)
    logger.info(
        "Состав исполнителей трека id=%s приведён к %s (основной id=%s)",
        track_id, composition, artist_id,
    )


async def suggest_folder(user_id: int, artist_name: str | None) -> tuple[dict | None, bool]:
    """Подобрать папку для исполнителя.

    Возвращает `(папка | None, нужно_ли_создавать)`:
    точное совпадение по `normalized_name` (по любой папке), затем нечёткое (>= 88)
    среди папок исполнителей. Если имя пустое — `(None, False)`: создавать нечего.
    """
    name = (artist_name or "").strip()
    if not name:
        logger.debug("Подбор папки пропущен: имя исполнителя не задано (user_id=%s)", user_id)
        return None, False

    normalized = normalize_name(name)
    if not normalized:
        logger.debug("Подбор папки пропущен: имя «%s» пустое после нормализации", name)
        return None, False

    try:
        exact = await folders_repo.find_folder_by_name(user_id, name)
    except Exception:
        logger.exception("Ошибка поиска папки «%s» (user_id=%s)", name, user_id)
        raise
    if exact:
        logger.debug(
            "Папка «%s» (id=%s) найдена точно для исполнителя «%s»",
            exact.get("name"), exact.get("id"), name,
        )
        return exact, False

    try:
        folders = await folders_repo.list_folders(user_id, include_counts=False)
    except Exception:
        logger.exception("Ошибка получения списка папок (user_id=%s)", user_id)
        raise

    best: dict | None = None
    best_score = 0.0
    for folder in folders:
        # Нечётко подбираем только папки исполнителей: тематическая папка
        # («Дискотека», «Рок») не должна перехватывать нового исполнителя.
        if not folder.get("is_artist_folder"):
            continue
        candidate = folder.get("normalized_name") or normalize_name(folder.get("name"))
        score = _similarity(normalized, candidate)
        if score > best_score:
            best_score = score
            best = folder

    if best is not None and best_score >= FUZZY_FOLDER_THRESHOLD:
        logger.info(
            "Папка «%s» (id=%s) подобрана нечётко для исполнителя «%s», совпадение %.1f",
            best.get("name"), best.get("id"), name, best_score,
        )
        return best, False

    logger.debug(
        "Подходящей папки для исполнителя «%s» нет (лучшее совпадение %.1f), нужна новая",
        name, best_score,
    )
    return None, True


async def apply_autosort(
    user_id: int,
    track: dict,
    *,
    artist_name: str | None = None,
    create_folder: bool = True,
    force: bool = False,
) -> SortResult:
    """Разложить трек по папке исполнителя.

    1. Исполнитель есть всегда, даже если автосортировка выключена: если трек уже
       привязан к исполнителю с тем же основным именем — он переиспользуется,
       иначе вызывается `ensure_artist`.
    2. Если включена настройка `auto_sort_enabled` (или передан `force=True`) —
       найти либо создать папку исполнителя (`is_artist_folder = 1`),
       привязать её к исполнителю и обновить у трека `folder_id` / `artist_id`
       (а также `album_id`, если у трека указан альбом).
    3. Состав `track_artists` синхронизируется с основным исполнителем: иначе
       трек виден на странице исполнителя, но пропадает из фильтра-пересечения
       в поиске (он строится только по `track_artists`).

    Переданный словарь `track` обновляется на месте актуальными значениями.
    """
    track_id = track.get("id")
    name = _resolve_artist_name(artist_name, track)
    if not name:
        logger.info(
            "Автосортировка пропущена: у трека id=%s нет исполнителя (user_id=%s)",
            track_id, user_id,
        )
        return SortResult()

    artist = await _artist_of_track(user_id, track, name)
    if artist is not None:
        logger.debug(
            "Трек id=%s уже привязан к исполнителю «%s» (id=%s) — вторая запись не нужна",
            track_id, artist.get("name"), artist.get("id"),
        )
    else:
        try:
            artist = await artists_repo.ensure_artist(user_id, name)
        except Exception:
            logger.exception(
                "Не удалось создать исполнителя «%s» (user_id=%s)", name, user_id
            )
            raise

    if not force:
        try:
            user_settings = await users_repo.get_settings(user_id)
        except Exception:
            logger.exception("Не удалось прочитать настройки пользователя %s", user_id)
            raise
        enabled = bool((user_settings or {}).get("auto_sort_enabled", 1))
        if not enabled:
            logger.info(
                "Автосортировка выключена пользователем %s — трек id=%s оставлен без папки",
                user_id, track_id,
            )
            return SortResult(artist=artist, folder=None, folder_created=False, applied=False)

    folder, need_create = await suggest_folder(user_id, name)
    folder_created = False
    if folder is None:
        if not need_create or not create_folder:
            logger.info(
                "Папка для исполнителя «%s» не создана (создание запрещено), трек id=%s",
                name, track_id,
            )
            return SortResult(artist=artist, folder=None, folder_created=False, applied=False)
        try:
            folder = await folders_repo.create_folder(user_id, name, is_artist_folder=True)
        except Exception:
            logger.exception(
                "Не удалось создать папку «%s» (user_id=%s)", name, user_id
            )
            raise
        folder_created = True
        logger.info(
            "Создана папка исполнителя «%s» (id=%s) для пользователя %s",
            folder.get("name"), folder.get("id"), user_id,
        )

    folder_id = folder.get("id")

    if artist and artist.get("folder_id") != folder_id:
        try:
            attached = await artists_repo.attach_folder(user_id, artist["id"], folder_id)
        except Exception:
            logger.exception(
                "Не удалось привязать папку id=%s к исполнителю id=%s",
                folder_id, artist.get("id"),
            )
            raise
        if attached:
            artist = attached
        else:
            logger.warning(
                "Исполнитель id=%s не найден при привязке папки id=%s (user_id=%s)",
                artist.get("id"), folder_id, user_id,
            )

    if track_id is None:
        logger.warning(
            "Трек без id: папка «%s» подобрана, но связать её не с чем (user_id=%s)",
            folder.get("name"), user_id,
        )
        return SortResult(artist=artist, folder=folder, folder_created=folder_created, applied=False)

    fields: dict[str, Any] = {}
    if track.get("folder_id") != folder_id:
        fields["folder_id"] = folder_id
    artist_id = (artist or {}).get("id")
    if artist_id and track.get("artist_id") != artist_id:
        fields["artist_id"] = artist_id

    album_title = track.get("album")
    if not track.get("album_id") and isinstance(album_title, str) and album_title.strip():
        try:
            album = await albums_repo.ensure_album(
                user_id, album_title, artist_id=(artist or {}).get("id")
            )
        except Exception:
            logger.exception(
                "Не удалось создать альбом «%s» (user_id=%s)", album_title, user_id
            )
            raise
        if album:
            fields["album_id"] = album.get("id")

    if fields:
        try:
            updated = await tracks_repo.update_track(user_id, track_id, **fields)
        except Exception:
            logger.exception(
                "Не удалось обновить трек id=%s полями %s (user_id=%s)",
                track_id, sorted(fields), user_id,
            )
            raise
        if updated:
            track.update(updated)
        else:
            logger.warning(
                "Трек id=%s не найден при автосортировке (user_id=%s)", track_id, user_id
            )
            return SortResult(
                artist=artist, folder=folder, folder_created=folder_created, applied=False
            )
    else:
        logger.debug("Трек id=%s уже лежит в папке «%s»", track_id, folder.get("name"))

    if artist_id:
        await _sync_track_artists(user_id, track, int(artist_id))

    logger.info(
        "Трек id=%s разложен в папку «%s» (id=%s), исполнитель «%s»",
        track_id, folder.get("name"), folder_id, name,
    )
    return SortResult(artist=artist, folder=folder, folder_created=folder_created, applied=True)


async def assign_to_folder(user_id: int, track_id: int, folder_name: str) -> dict | None:
    """Перенести трек в папку с указанным именем, создав её при необходимости.

    Возвращает обновлённый трек либо None, если трек не найден.
    """
    name = (folder_name or "").strip()
    if not name:
        logger.warning("Пустое имя папки при переносе трека id=%s (user_id=%s)", track_id, user_id)
        raise ValidationError("Название папки не может быть пустым")

    try:
        folder = await folders_repo.find_folder_by_name(user_id, name)
    except Exception:
        logger.exception("Ошибка поиска папки «%s» (user_id=%s)", name, user_id)
        raise

    if folder is None:
        try:
            folder = await folders_repo.create_folder(user_id, name)
        except Exception:
            logger.exception("Не удалось создать папку «%s» (user_id=%s)", name, user_id)
            raise
        logger.info(
            "Создана папка «%s» (id=%s) для пользователя %s", folder.get("name"), folder.get("id"), user_id
        )
    else:
        logger.debug("Папка «%s» (id=%s) уже существует", folder.get("name"), folder.get("id"))

    try:
        moved = await tracks_repo.move_track(user_id, track_id, folder.get("id"))
    except Exception:
        logger.exception(
            "Не удалось перенести трек id=%s в папку id=%s (user_id=%s)",
            track_id, folder.get("id"), user_id,
        )
        raise

    if moved is None:
        logger.warning("Трек id=%s не найден при переносе в папку «%s»", track_id, name)
        return None

    logger.info("Трек id=%s перенесён в папку «%s» (id=%s)", track_id, name, folder.get("id"))
    return moved
