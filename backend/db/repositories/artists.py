"""Репозиторий исполнителей: создание, поиск, отметка «прослушано», статистика.

Исполнитель считается УЧАСТНИКОМ трека, если он основной (``tracks.artist_id``)
либо указан в ``track_artists`` (несколько исполнителей у трека, раздел 1.2
контракта V2). Единственный источник этого условия — ``_PARTICIPATION_SQL``;
на нём построены агрегаты ``track_count`` / ``play_count``,
:func:`artist_tracks`, :func:`count_artist_tracks`,
:func:`artist_unplayed_tracks` и :func:`recalc_total_plays`.

``artists.total_plays`` — денормализованная сумма прослушиваний треков
исполнителя. Быстрый инкремент — :func:`bump_total_plays` (вызывается при
регистрации прослушивания), пересчёт «с нуля» — :func:`recalc_total_plays`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import aiosqlite

from backend.db.database import db
from backend.errors import ValidationError
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

# Ограничение на длину имени исполнителя (как у папок и плейлистов).
MAX_NAME_LENGTH: Final[int] = 200

#: Условие «исполнитель участвует в треке»: он основной (``tracks.artist_id``)
#: либо связан с треком строкой в ``track_artists``. ``{artist}`` — выражение с
#: идентификатором исполнителя: ``a.id``, ``artists.id`` или плейсхолдер ``?``.
_PARTICIPATION_SQL: Final[str] = (
    "t.artist_id = {artist}"
    " OR EXISTS (SELECT 1 FROM track_artists ta"
    " WHERE ta.track_id = t.id AND ta.artist_id = {artist})"
)

#: Условие участия для колонок исполнителя (корреляция с ``artists a``).
_PARTICIPATION_BY_ALIAS: Final[str] = _PARTICIPATION_SQL.format(artist="a.id")

# Колонки исполнителя вместе с агрегатами по трекам (нужны для ArtistOut).
# Агрегаты считаются по УЧАСТИЮ: приглашённый исполнитель видит свои треки так
# же, как основной. Коррелированные подзапросы (а не JOIN к ``track_artists``)
# гарантируют, что трек учитывается ОДИН раз, сколько бы связей у него ни было.
ARTIST_COLUMNS = f"""
        a.id, a.user_id, a.name, a.normalized_name, a.is_listened,
        a.listened_at, a.folder_id, a.created_at, a.total_plays,
        (SELECT COUNT(*)
           FROM tracks t
          WHERE t.user_id = a.user_id
            AND ({_PARTICIPATION_BY_ALIAS})) AS track_count,
        (SELECT COALESCE(SUM(t.play_count), 0)
           FROM tracks t
          WHERE t.user_id = a.user_id
            AND ({_PARTICIPATION_BY_ALIAS})) AS play_count
"""

ARTIST_FROM = """
    FROM artists a
"""


def _artist_query(where: str = "") -> str:
    """Собирает SELECT по исполнителям с агрегатами track_count / play_count."""
    return (
        f"SELECT {ARTIST_COLUMNS}"
        f"{ARTIST_FROM}"
        f"    WHERE a.user_id = ? {where}\n"
    )


def _to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Приводит строку БД к публичному dict: bool-флаги и целые агрегаты."""
    if row is None:
        return None
    data = dict(row)
    data["is_listened"] = bool(data.get("is_listened"))
    data["track_count"] = int(data.get("track_count") or 0)
    data["play_count"] = int(data.get("play_count") or 0)
    data["total_plays"] = int(data.get("total_plays") or 0)
    return data


def _clean_name(value: str | None) -> tuple[str, str]:
    """Проверяет имя исполнителя и возвращает пару (отображаемое, нормализованное)."""
    display = (value or "").strip()
    normalized = normalize_name(display)
    if not normalized:
        raise ValidationError("Имя исполнителя не может быть пустым")
    if len(display) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"Имя исполнителя слишком длинное (максимум {MAX_NAME_LENGTH} символов)"
        )
    return display or normalized, normalized


def _to_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Приводит список строк БД к списку публичных dict."""
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _to_dict(row)
        if item is not None:
            result.append(item)
    return result


async def _fetch_by_normalized(user_id: int, normalized: str) -> dict[str, Any] | None:
    """Ищет исполнителя пользователя по нормализованному имени."""
    row = await db.fetch_one(
        _artist_query("AND a.normalized_name = ?"),
        (user_id, normalized),
    )
    return _to_dict(row)


async def ensure_artist(
    user_id: int,
    name: str,
    folder_id: int | None = None,
) -> dict[str, Any] | None:
    """Возвращает исполнителя, создавая его при необходимости (идемпотентно).

    Уникальность — по паре (user_id, normalized_name). Если исполнитель уже есть
    и передан folder_id — привязка к папке обновляется. Пустое имя -> None.
    """
    normalized = normalize_name(name)
    if not normalized:
        logger.debug("Пустое имя исполнителя для пользователя %s — пропускаем", user_id)
        return None

    display_name = (name or "").strip() or normalized

    existing = await _fetch_by_normalized(user_id, normalized)
    if existing is None:
        await db.execute(
            """
            INSERT INTO artists (user_id, name, normalized_name, folder_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, normalized_name) DO NOTHING
            """,
            (user_id, display_name, normalized, folder_id),
        )
        existing = await _fetch_by_normalized(user_id, normalized)
        if existing is None:
            logger.error(
                "Не удалось создать исполнителя «%s» для пользователя %s",
                display_name,
                user_id,
            )
            return None
        logger.info(
            "Создан исполнитель «%s» (id=%s) для пользователя %s",
            display_name,
            existing["id"],
            user_id,
        )

    if folder_id is not None and existing.get("folder_id") != folder_id:
        await db.execute(
            "UPDATE artists SET folder_id = ? WHERE id = ? AND user_id = ?",
            (folder_id, existing["id"], user_id),
        )
        existing["folder_id"] = folder_id
        logger.debug(
            "Исполнитель id=%s привязан к папке id=%s", existing["id"], folder_id
        )

    return existing


async def get_artist(user_id: int, artist_id: int) -> dict[str, Any] | None:
    """Возвращает исполнителя по id или None."""
    row = await db.fetch_one(
        _artist_query("AND a.id = ?"),
        (user_id, artist_id),
    )
    return _to_dict(row)


async def find_artist_by_name(user_id: int, name: str) -> dict[str, Any] | None:
    """Ищет исполнителя по имени (сравнение по нормализованному имени)."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    return await _fetch_by_normalized(user_id, normalized)


async def list_artists(
    user_id: int,
    *,
    only_listened: bool | None = None,
) -> list[dict[str, Any]]:
    """Список исполнителей с количеством треков и суммой прослушиваний.

    only_listened=True — только прослушанные, False — только непрослушанные,
    None — все. Сортировка алфавитная, выполняется в Python (кириллица).
    """
    where = ""
    params: list[Any] = [user_id]
    if only_listened is not None:
        where = "AND a.is_listened = ?"
        params.append(1 if only_listened else 0)

    rows = await db.fetch_all(_artist_query(where), tuple(params))
    artists = _to_dicts(rows)
    artists.sort(key=lambda item: (item.get("name") or "").casefold())
    return artists


async def set_listened(
    user_id: int,
    artist_id: int,
    is_listened: bool,
) -> dict[str, Any] | None:
    """Проставляет/снимает отметку «прослушано» у исполнителя."""
    if is_listened:
        sql = """
            UPDATE artists
               SET is_listened = 1,
                   listened_at = CURRENT_TIMESTAMP
             WHERE id = ? AND user_id = ?
        """
    else:
        sql = """
            UPDATE artists
               SET is_listened = 0,
                   listened_at = NULL
             WHERE id = ? AND user_id = ?
        """

    updated = await db.execute(sql, (artist_id, user_id))
    if not updated:
        logger.warning(
            "Исполнитель id=%s пользователя %s не найден — отметка не изменена",
            artist_id,
            user_id,
        )
        return None
    return await get_artist(user_id, artist_id)


async def toggle_listened(user_id: int, artist_id: int) -> dict[str, Any] | None:
    """Переключает отметку «прослушано» у исполнителя."""
    artist = await get_artist(user_id, artist_id)
    if artist is None:
        logger.warning(
            "Исполнитель id=%s пользователя %s не найден — переключение отменено",
            artist_id,
            user_id,
        )
        return None
    return await set_listened(user_id, artist_id, not artist["is_listened"])


async def attach_folder(
    user_id: int,
    artist_id: int,
    folder_id: int | None,
) -> dict[str, Any] | None:
    """Привязывает исполнителя к папке (folder_id=None — отвязывает)."""
    updated = await db.execute(
        "UPDATE artists SET folder_id = ? WHERE id = ? AND user_id = ?",
        (folder_id, artist_id, user_id),
    )
    if not updated:
        logger.warning(
            "Исполнитель id=%s пользователя %s не найден — папка не привязана",
            artist_id,
            user_id,
        )
        return None
    return await get_artist(user_id, artist_id)


async def mark_listened_by_track(user_id: int, track_id: int) -> None:
    """Помечает исполнителя трека прослушанным (если исполнитель определён)."""
    artist_id = await db.fetch_val(
        "SELECT artist_id FROM tracks WHERE id = ? AND user_id = ?",
        (track_id, user_id),
    )
    if not artist_id:
        logger.debug(
            "У трека id=%s пользователя %s нет исполнителя — отметка не нужна",
            track_id,
            user_id,
        )
        return

    await db.execute(
        """
        UPDATE artists
           SET is_listened = 1,
               listened_at = CURRENT_TIMESTAMP
         WHERE id = ? AND user_id = ?
        """,
        (artist_id, user_id),
    )
    logger.debug(
        "Исполнитель id=%s помечен прослушанным по треку id=%s", artist_id, track_id
    )


# --------------------------------------------------------------------------
# V2: переименование со слиянием, папка исполнителя, статистика
# --------------------------------------------------------------------------

#: Пересчёт ``artists.total_plays`` по трекам, где исполнитель участвует
#: (основной исполнитель трека либо связь в ``track_artists``).
_RECALC_TOTAL_PLAYS_SQL: Final[str] = f"""
    UPDATE artists
       SET total_plays = COALESCE((
               SELECT SUM(t.play_count)
                 FROM tracks t
                WHERE t.user_id = artists.user_id
                  AND ({_PARTICIPATION_SQL.format(artist="artists.id")})
           ), 0)
     WHERE artists.user_id = ?
"""

#: Условие «трек принадлежит исполнителю» для сборки запроса треков.
#: Плейсхолдер ``?`` встречается дважды — id исполнителя передаётся два раза.
_ARTIST_TRACK_CONDITION: Final[str] = (
    f"AND ({_PARTICIPATION_SQL.format(artist='?')})"
)

#: Кросс-пользовательская агрегация по нормализованному имени исполнителя.
_GLOBAL_STATS_SQL: Final[str] = """
    SELECT a.normalized_name               AS normalized_name,
           COALESCE(SUM(a.total_plays), 0) AS total_plays,
           COUNT(DISTINCT a.user_id)       AS listeners,
           (SELECT b.name
              FROM artists b
             WHERE b.normalized_name = a.normalized_name
             ORDER BY b.total_plays DESC, b.id ASC
             LIMIT 1)                      AS name
      FROM artists a
     WHERE a.normalized_name <> ''
     GROUP BY a.normalized_name
"""


async def _merge_albums(
    conn: aiosqlite.Connection, user_id: int, source_id: int, target_id: int
) -> None:
    """Переносит альбомы исполнителя-дубликата на выжившего исполнителя.

    У ``albums`` стоит UNIQUE (user_id, normalized_title, artist_id): если такой
    альбом у выжившего уже есть, перенос игнорируется, а треки дубликата
    перевешиваются на альбом-выживший, после чего дубликат удаляется.
    """
    await conn.execute(
        "UPDATE OR IGNORE albums SET artist_id = ? WHERE artist_id = ? AND user_id = ?",
        (target_id, source_id, user_id),
    )

    cursor = await conn.execute(
        "SELECT id, normalized_title FROM albums WHERE artist_id = ? AND user_id = ?",
        (source_id, user_id),
    )
    leftovers = await cursor.fetchall()
    await cursor.close()

    for row in leftovers:
        album_id = int(row["id"])
        cursor = await conn.execute(
            "SELECT id FROM albums"
            " WHERE user_id = ? AND normalized_title = ? AND artist_id = ? LIMIT 1",
            (user_id, row["normalized_title"], target_id),
        )
        survivor = await cursor.fetchone()
        await cursor.close()
        if survivor is None:
            # Переносить некуда: оставляем альбом как есть, внешний ключ обнулит artist_id.
            logger.warning(
                "Альбом id=%s пользователя %s не удалось перенести на исполнителя id=%s",
                album_id,
                user_id,
                target_id,
            )
            continue
        survivor_id = int(survivor["id"])
        await conn.execute(
            "UPDATE tracks SET album_id = ? WHERE album_id = ? AND user_id = ?",
            (survivor_id, album_id, user_id),
        )
        await conn.execute(
            "DELETE FROM albums WHERE id = ? AND user_id = ?", (album_id, user_id)
        )
        logger.info(
            "Пользователь %s: альбом id=%s слит с альбомом id=%s при слиянии исполнителей",
            user_id,
            album_id,
            survivor_id,
        )


async def _merge_artists(
    conn: aiosqlite.Connection,
    user_id: int,
    source: dict[str, Any],
    target: dict[str, Any],
    display_name: str,
) -> None:
    """Сливает исполнителя ``source`` в ``target`` внутри одной транзакции."""
    source_id = int(source["id"])
    target_id = int(target["id"])

    # Треки: основной исполнитель.
    await conn.execute(
        "UPDATE tracks SET artist_id = ? WHERE artist_id = ? AND user_id = ?",
        (target_id, source_id, user_id),
    )
    # Связи «трек — исполнитель»: переносим, а дубли (у трека уже есть выживший)
    # отсекает первичный ключ (track_id, artist_id) — такие строки просто удаляем.
    await conn.execute(
        "UPDATE OR IGNORE track_artists SET artist_id = ? WHERE artist_id = ?",
        (target_id, source_id),
    )
    await conn.execute("DELETE FROM track_artists WHERE artist_id = ?", (source_id,))

    await _merge_albums(conn, user_id, source_id, target_id)

    # Папка: своя важнее, чужую берём, только если у выжившего папки нет.
    folder_id = target.get("folder_id") or source.get("folder_id")
    # Отметка «прослушано» суммарная: слушали любого из двух — значит, слушали.
    is_listened = bool(target.get("is_listened")) or bool(source.get("is_listened"))
    listened_at = target.get("listened_at") or source.get("listened_at")
    if not is_listened:
        listened_at = None

    await conn.execute(
        """
        UPDATE artists
           SET name = ?,
               folder_id = ?,
               is_listened = ?,
               listened_at = ?
         WHERE id = ? AND user_id = ?
        """,
        (
            display_name,
            folder_id,
            1 if is_listened else 0,
            listened_at,
            target_id,
            user_id,
        ),
    )
    await conn.execute(
        "DELETE FROM artists WHERE id = ? AND user_id = ?", (source_id, user_id)
    )
    await conn.execute(
        f"{_RECALC_TOTAL_PLAYS_SQL} AND artists.id = ?", (user_id, target_id)
    )
    logger.info(
        "Пользователь %s: исполнитель id=%s слит с id=%s («%s»)",
        user_id,
        source_id,
        target_id,
        display_name,
    )


async def rename_artist(
    user_id: int,
    artist_id: int,
    new_name: str,
) -> dict[str, Any] | None:
    """Переименовывает исполнителя, сливая его с тёзкой при конфликте имён.

    Если у пользователя уже есть ДРУГОЙ исполнитель с таким же нормализованным
    именем, выполняется СЛИЯНИЕ: треки, связи ``track_artists`` и альбомы
    перевешиваются на существующего исполнителя, дубликат удаляется. Возвращает
    итогового исполнителя (при слиянии — того, который остался), либо None, если
    исходный исполнитель не найден. Пустое имя -> ValidationError.
    """
    display_name, normalized = _clean_name(new_name)

    artist = await get_artist(user_id, artist_id)
    if artist is None:
        logger.warning(
            "Исполнитель id=%s пользователя %s не найден — переименование отменено",
            artist_id,
            user_id,
        )
        return None

    existing = await _fetch_by_normalized(user_id, normalized)
    if existing is not None and int(existing["id"]) != int(artist["id"]):
        async with db.transaction() as conn:
            await _merge_artists(conn, user_id, artist, existing, display_name)
        return await get_artist(user_id, int(existing["id"]))

    await db.execute(
        "UPDATE artists SET name = ?, normalized_name = ? WHERE id = ? AND user_id = ?",
        (display_name, normalized, int(artist["id"]), user_id),
    )
    logger.info(
        "Пользователь %s: исполнитель id=%s переименован в «%s»",
        user_id,
        artist_id,
        display_name,
    )
    return await get_artist(user_id, int(artist["id"]))


async def set_folder(
    user_id: int,
    artist_id: int,
    folder_id: int | None,
) -> dict[str, Any] | None:
    """Привязывает исполнителя к папке пользователя (None — отвязывает).

    В отличие от :func:`attach_folder` проверяет, что папка существует и
    принадлежит этому же пользователю: несуществующая папка -> ValidationError,
    несуществующий исполнитель -> None.
    """
    target_folder: int | None = None
    if folder_id is not None:
        try:
            target_folder = int(folder_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Некорректный идентификатор папки") from exc
        exists = await db.fetch_val(
            "SELECT 1 FROM folders WHERE id = ? AND user_id = ?",
            (target_folder, user_id),
        )
        if not exists:
            logger.warning(
                "Пользователь %s: папка id=%s не найдена — исполнитель id=%s не привязан",
                user_id,
                target_folder,
                artist_id,
            )
            raise ValidationError("Папка не найдена")

    return await attach_folder(user_id, artist_id, target_folder)


async def artist_tracks(
    user_id: int,
    artist_id: int,
    *,
    order: str = "created_at_desc",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Треки исполнителя: и где он основной, и где он приглашённый.

    Замена ``tracks_repo.list_tracks(artist_id=...)``, которая видит только
    основного исполнителя (``tracks.artist_id``) и поэтому отдаёт пустой список
    приглашённому. Считает то же множество треков, что и агрегат
    ``track_count``. Порядок — ключ из белого списка ``tracks.resolve_order``.
    """
    # Локальный импорт: tracks.py обращается к этому модулю (bump_total_plays),
    # импорт на уровне модуля дал бы цикл.
    from backend.db.repositories import tracks as tracks_repo

    try:
        target = int(artist_id)
    except (TypeError, ValueError):
        logger.warning("Некорректный идентификатор исполнителя %r", artist_id)
        return []

    safe_limit = max(1, min(int(limit or 0) or 50, 500))
    safe_offset = max(0, int(offset or 0))

    sql = tracks_repo.track_query(
        _ARTIST_TRACK_CONDITION,
        tracks_repo.resolve_order(order),
        "LIMIT ? OFFSET ?",
    )
    rows = await db.fetch_all(sql, (user_id, target, target, safe_limit, safe_offset))
    logger.debug(
        "Пользователь %s: у исполнителя id=%s треков в выдаче — %s",
        user_id,
        target,
        len(rows),
    )
    return rows


async def count_artist_tracks(user_id: int, artist_id: int) -> int:
    """Сколько всего треков у исполнителя (с учётом участия, как ``track_count``)."""
    try:
        target = int(artist_id)
    except (TypeError, ValueError):
        logger.warning("Некорректный идентификатор исполнителя %r", artist_id)
        return 0

    value = await db.fetch_val(
        f"SELECT COUNT(*) FROM tracks t WHERE t.user_id = ? {_ARTIST_TRACK_CONDITION}",
        (user_id, target, target),
        default=0,
    )
    return int(value or 0)


async def artist_unplayed_tracks(
    user_id: int,
    artist_id: int,
    limit: int = 50,
    *,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Треки исполнителя, которые ни разу не проигрывались (play_count = 0).

    Учитываются треки, где исполнитель основной, и треки, где он указан через
    ``track_artists``. Порядок — сначала недавно добавленные.
    """
    # Локальный импорт: tracks.py может обращаться к этому модулю (bump_total_plays),
    # импорт на уровне модуля дал бы цикл.
    from backend.db.repositories import tracks as tracks_repo

    try:
        target = int(artist_id)
    except (TypeError, ValueError):
        logger.warning("Некорректный идентификатор исполнителя %r", artist_id)
        return []

    safe_limit = max(1, min(int(limit or 0) or 50, 500))
    safe_offset = max(0, int(offset or 0))

    sql = tracks_repo.track_query(
        f"AND t.play_count = 0 {_ARTIST_TRACK_CONDITION}",
        tracks_repo.resolve_order("created_at_desc"),
        "LIMIT ? OFFSET ?",
    )
    rows = await db.fetch_all(sql, (user_id, target, target, safe_limit, safe_offset))
    logger.debug(
        "Пользователь %s: у исполнителя id=%s непрослушанных треков в выдаче — %s",
        user_id,
        target,
        len(rows),
    )
    return rows


async def recalc_total_plays(user_id: int, artist_id: int | None = None) -> None:
    """Пересчитывает ``artists.total_plays`` по фактическим прослушиваниям треков.

    Без ``artist_id`` пересчитываются все исполнители пользователя.
    """
    if artist_id is None:
        await db.execute(_RECALC_TOTAL_PLAYS_SQL, (user_id,))
        logger.info(
            "Пользователь %s: пересчитаны прослушивания всех исполнителей", user_id
        )
        return

    try:
        target = int(artist_id)
    except (TypeError, ValueError):
        logger.warning("Некорректный идентификатор исполнителя %r", artist_id)
        return

    await db.execute(f"{_RECALC_TOTAL_PLAYS_SQL} AND artists.id = ?", (user_id, target))
    logger.debug(
        "Пользователь %s: пересчитаны прослушивания исполнителя id=%s", user_id, target
    )


async def bump_total_plays(
    user_id: int, artist_id: int | None, amount: int = 1
) -> None:
    """Увеличивает счётчик прослушиваний исполнителя (быстрый инкремент).

    Вызывается при регистрации прослушивания трека. ``artist_id=None`` — у трека
    нет исполнителя, делать нечего. Счётчик не опускается ниже нуля.
    """
    if artist_id is None:
        return
    try:
        target = int(artist_id)
        delta = int(amount)
    except (TypeError, ValueError):
        logger.warning(
            "Некорректные аргументы bump_total_plays: artist_id=%r, amount=%r",
            artist_id,
            amount,
        )
        return
    if delta == 0:
        return

    updated = await db.execute(
        "UPDATE artists SET total_plays = MAX(0, total_plays + ?)"
        " WHERE id = ? AND user_id = ?",
        (delta, target, user_id),
    )
    if not updated:
        logger.debug(
            "Исполнитель id=%s пользователя %s не найден — счётчик не изменён",
            target,
            user_id,
        )
        return
    logger.debug(
        "Исполнитель id=%s пользователя %s: total_plays изменён на %+d",
        target,
        user_id,
        delta,
    )


async def global_artist_stats() -> list[dict[str, Any]]:
    """Кросс-пользовательская статистика исполнителей по нормализованному имени.

    Возвращает список dict с ключами ``normalized_name``, ``name`` (самый
    «популярный» вариант написания), ``total_plays`` (сумма прослушиваний у всех
    пользователей) и ``listeners`` (сколько пользователей держат этого
    исполнителя в библиотеке). Сортировка — по популярности, затем по алфавиту.

    ВНИМАНИЕ: это единственная функция репозитория без фильтра по ``user_id`` —
    она агрегирует данные всех пользователей и НЕ раскрывает их идентификаторы.
    """
    rows = await db.fetch_all(_GLOBAL_STATS_SQL)
    stats: list[dict[str, Any]] = []
    for row in rows:
        normalized = str(row.get("normalized_name") or "")
        if not normalized:
            continue
        stats.append(
            {
                "normalized_name": normalized,
                "name": str(row.get("name") or normalized),
                "total_plays": int(row.get("total_plays") or 0),
                "listeners": int(row.get("listeners") or 0),
            }
        )
    stats.sort(
        key=lambda item: (
            -item["total_plays"],
            -item["listeners"],
            item["name"].casefold(),
        )
    )
    logger.debug("Глобальная статистика исполнителей: %s записей", len(stats))
    return stats
