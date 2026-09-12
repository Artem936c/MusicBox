"""Репозиторий папок пользователя: вложенные папки и разделы (контракт V2, раздел 2).

Начиная с миграции ``002_nested_folders`` у таблицы ``folders`` есть колонки
``parent_folder_id`` (самоссылка, ``ON DELETE CASCADE``) и ``section``
(``music`` — раздел «Треки», ``other`` — раздел «Другое»), а уникальность имени
действует внутри уровня: ``UNIQUE (user_id, section, parent_folder_id, normalized_name)``.

Важное ограничение SQLite: в UNIQUE-индексе ``NULL != NULL``, поэтому дубли
КОРНЕВЫХ папок база не ловит. Уникальность корня обеспечивает этот модуль:
поиск соседа с тем же нормализованным именем выполняется в одной транзакции с
записью (создание, переименование, перенос — :func:`create_folder`,
:func:`rename_folder`, :func:`move_folder`), поэтому между проверкой и записью
не вклинится параллельный запрос.

Обратная совместимость с V1 сохранена: ``create_folder(user_id, name)`` создаёт
корневую папку раздела ``music``, ``list_folders(user_id)`` отдаёт все папки
этого раздела в порядке обхода дерева (на каждом уровне — алфавит, сортировка в
Python по ``name.casefold()``; у плоских данных V1 это ровно алфавитный список).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final, Iterable, Sequence

from backend.db.database import db
from backend.errors import NotFoundError, ValidationError
from backend.services.metadata import normalize_name

logger = logging.getLogger(__name__)

# Ограничение длины названия папки (Telegram-кнопки и заголовки не резиновые).
MAX_NAME_LENGTH: Final[int] = 100

#: Максимальная глубина вложенности папок (число уровней, корень — уровень 1).
MAX_FOLDER_DEPTH: Final[int] = 16

#: Запас для рекурсивных обходов: на один шаг больше предельной глубины,
#: чтобы обход гарантированно завершился даже на испорченных данных.
_DEPTH_GUARD: Final[int] = MAX_FOLDER_DEPTH + 1

#: Разделы приложения: «Треки» (аудио) и «Другое» (документы, видео, голосовые).
SECTIONS: Final[tuple[str, ...]] = ("music", "other")

#: Раздел по умолчанию — тот, в котором живут все папки V1.
DEFAULT_SECTION: Final[str] = "music"

#: Русские названия разделов для сообщений об ошибках.
SECTION_TITLES: Final[dict[str, str]] = {"music": "Музыка", "other": "Другое"}

#: Разделитель в поле ``path`` («Рок / Русский рок»).
PATH_SEPARATOR: Final[str] = " / "

#: Сентинел «фильтр не задан»: отличает «любой родитель» от «только корневые»
#: (``parent_folder_id=None``). Совпадает по смыслу с ``tracks.UNSET``, но
#: объявлен здесь, чтобы не тянуть зависимость от репозитория треков.
UNSET: Final[Any] = object()

FOLDER_COLUMNS: Final[str] = """
    f.id, f.user_id, f.name, f.normalized_name, f.parent_folder_id,
    f.section, f.is_artist_folder, f.created_at
"""

_SELECT_FOLDER: Final[str] = f"SELECT {FOLDER_COLUMNS} FROM folders f"

# Потомки папки: рекурсивный CTE вниз по дереву. Глубина ограничена _DEPTH_GUARD,
# поэтому обход завершается даже если в данных каким-то образом появился цикл.
_DESCENDANTS_CTE: Final[str] = """
WITH RECURSIVE sub(id, depth) AS (
    SELECT f.id, 0
    FROM folders f
    WHERE f.user_id = ? AND f.id = ?
    UNION ALL
    SELECT f.id, sub.depth + 1
    FROM folders f
    JOIN sub ON f.parent_folder_id = sub.id
    WHERE f.user_id = ? AND sub.depth < ?
)
"""

# Предки папки: рекурсивный CTE вверх по дереву (depth 0 — сама папка).
_ANCESTORS_CTE: Final[str] = """
WITH RECURSIVE up(id, parent_folder_id, depth) AS (
    SELECT f.id, f.parent_folder_id, 0
    FROM folders f
    WHERE f.user_id = ? AND f.id = ?
    UNION ALL
    SELECT f.id, f.parent_folder_id, up.depth + 1
    FROM folders f
    JOIN up ON f.id = up.parent_folder_id
    WHERE f.user_id = ? AND up.depth < ?
)
"""


# --- разбор аргументов ------------------------------------------------------


def _clean_name(name: str | None) -> tuple[str, str]:
    """Проверяет название папки, возвращает (отображаемое имя, нормализованное имя)."""
    display = (name or "").strip()
    if not display:
        raise ValidationError("Название папки не может быть пустым")
    if len(display) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"Название папки слишком длинное (максимум {MAX_NAME_LENGTH} символов)"
        )
    normalized = normalize_name(display)
    if not normalized:
        raise ValidationError("Название папки не может быть пустым")
    return display, normalized


def _clean_section(section: Any) -> str:
    """Проверяет имя раздела. Пустое значение — раздел по умолчанию (``music``)."""
    text = str(section or "").strip().casefold() or DEFAULT_SECTION
    if text not in SECTIONS:
        known = ", ".join(f"{key} ({SECTION_TITLES[key]})" for key in SECTIONS)
        raise ValidationError(f"Неизвестный раздел «{section}». Доступны: {known}")
    return text


def _section_filter(section: Any) -> str | None:
    """``None`` — без фильтра по разделу, иначе проверенное имя раздела."""
    if section is None:
        return None
    return _clean_section(section)


def _is_unset(value: Any) -> bool:
    """Признак «фильтр по родителю не задан».

    Своим считается :data:`UNSET`; любой другой посторонний сентинел (например
    ``tracks.UNSET``) тоже трактуется как «без фильтра», потому что не приводится
    к целому. ``None`` — это осмысленное значение «только корневые папки».
    """
    if value is UNSET:
        return True
    if value is None:
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return True
    return False


def _as_parent_id(value: Any) -> int | None:
    """Приводит идентификатор родителя к ``int`` (``None`` остаётся ``None``)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Некорректный идентификатор родительской папки") from exc


def _sort_by_name(items: list[dict]) -> None:
    """Сортировка по алфавиту в Python (кириллица и регистр — как в V1)."""
    items.sort(key=lambda item: ((item.get("name") or "").casefold(), int(item["id"])))


# --- преобразование строк ---------------------------------------------------


def _row_to_folder(row: dict | None) -> dict | None:
    """Строка выборки -> папка-dict с предсказуемыми типами и агрегатами по умолчанию."""
    if row is None:
        return None
    data = dict(row)
    data["id"] = int(data["id"])
    parent = data.get("parent_folder_id")
    data["parent_folder_id"] = None if parent is None else int(parent)
    data["section"] = str(data.get("section") or DEFAULT_SECTION)
    data["is_artist_folder"] = bool(data.get("is_artist_folder", 0))
    # Полный путь известен только там, где под рукой всё дерево (списки и
    # folder_tree). В одиночной выборке путь совпадает с названием папки.
    data["path"] = str(data.get("name") or "")
    data["depth"] = 0
    data["has_children"] = False
    data["track_count"] = 0
    data["total_track_count"] = 0
    return data


# --- выборки и агрегаты -----------------------------------------------------


async def _own_counts(user_id: int) -> dict[int, int]:
    """Число собственных треков по каждой папке пользователя (без подпапок)."""
    rows = await db.fetch_all(
        """
        SELECT folder_id, COUNT(*) AS track_count
        FROM tracks
        WHERE user_id = ? AND folder_id IS NOT NULL
        GROUP BY folder_id
        """,
        (user_id,),
    )
    return {int(row["folder_id"]): int(row["track_count"] or 0) for row in rows}


async def _count_tracks(user_id: int, folder_ids: Sequence[int]) -> int:
    """Число треков во всех перечисленных папках."""
    ids = [int(value) for value in folder_ids]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    value = await db.fetch_val(
        f"SELECT COUNT(*) FROM tracks WHERE user_id = ? AND folder_id IN ({placeholders})",
        (user_id, *ids),
        default=0,
    )
    return int(value or 0)


async def _snapshot(
    user_id: int, section: str | None
) -> tuple[dict[int, dict], dict[int, int | None], dict[int | None, list[int]]]:
    """Все папки раздела одним запросом: (папки по id, родитель по id, дети по родителю)."""
    sql = f"{_SELECT_FOLDER} WHERE f.user_id = ?"
    params: list[Any] = [user_id]
    if section is not None:
        sql += " AND f.section = ?"
        params.append(section)

    rows = await db.fetch_all(sql, tuple(params))
    folders: dict[int, dict] = {}
    for row in rows:
        folder = _row_to_folder(row)
        if folder is not None:
            folders[folder["id"]] = folder

    parent_of: dict[int, int | None] = {}
    for folder_id, folder in folders.items():
        parent = folder["parent_folder_id"]
        if parent is not None and parent not in folders:
            # Возможно только если родитель лежит в другом разделе: показываем узел
            # как корневой, чтобы папка не пропала из списка.
            logger.warning(
                "Папка %s пользователя %s ссылается на родителя %s вне выборки — считаем её корневой",
                folder_id,
                user_id,
                parent,
            )
            parent = None
        parent_of[folder_id] = parent

    children: dict[int | None, list[int]] = {}
    for folder_id, parent in parent_of.items():
        children.setdefault(parent, []).append(folder_id)
    return folders, parent_of, children


def _compute_depths(parent_of: dict[int, int | None]) -> dict[int, int]:
    """Глубина каждой папки (0 — корень). Цикл в данных не зацикливает обход."""
    depths: dict[int, int] = {}
    for folder_id in parent_of:
        chain: list[int] = []
        seen: set[int] = set()
        current: int | None = folder_id
        while current is not None and current not in depths:
            if current in seen:
                logger.error(
                    "Обнаружен цикл во вложенности папок рядом с id=%s — глубина посчитана от корня",
                    current,
                )
                current = None
                break
            seen.add(current)
            chain.append(current)
            current = parent_of.get(current)
        base = depths[current] if current is not None else -1
        for node in reversed(chain):
            base += 1
            depths[node] = base
    return depths


def _compute_paths(
    folders: dict[int, dict], parent_of: dict[int, int | None]
) -> dict[int, str]:
    """Полный путь до каждой папки («Рок / Русский рок»).

    Нужен там, где папки показываются одним плоским списком: без пути две
    одноимённые подпапки из разных веток («Рок/Хиты» и «Джаз/Хиты») выглядят
    одинаково. Цикл в данных обход не зацикливает (он уже залогирован в
    :func:`_compute_depths`).
    """
    paths: dict[int, str] = {}
    for folder_id in parent_of:
        chain: list[int] = []
        seen: set[int] = set()
        current: int | None = folder_id
        while current is not None and current not in paths:
            if current in seen:
                current = None
                break
            seen.add(current)
            chain.append(current)
            current = parent_of.get(current)
        base = paths[current] if current is not None else ""
        for node in reversed(chain):
            name = str((folders.get(node) or {}).get("name") or "")
            base = f"{base}{PATH_SEPARATOR}{name}" if base else name
            paths[node] = base
    return paths


def _tree_order(folders: dict[int, dict], children: dict[int | None, list[int]]) -> list[int]:
    """Идентификаторы папок в порядке обхода дерева (как в :func:`folder_tree`).

    Родитель идёт первым, сразу за ним — его подпапки; внутри уровня порядок
    алфавитный. Благодаря этому плоский список читается сверху вниз, а отступ по
    ``depth`` у потребителей показывает настоящую вложенность, а не «пустоту».
    """

    def level(parent: int | None) -> list[int]:
        items = [folders[node] for node in children.get(parent, ()) if node in folders]
        _sort_by_name(items)
        return [int(item["id"]) for item in items]

    order: list[int] = []
    visited: set[int] = set()
    stack: list[int] = list(reversed(level(None)))
    while stack:
        folder_id = stack.pop()
        if folder_id in visited:
            continue
        visited.add(folder_id)
        order.append(folder_id)
        stack.extend(reversed(level(folder_id)))

    # Папки, до которых обход не добрался (цикл в данных), не теряем.
    rest = [folders[node] for node in folders if node not in visited]
    if rest:
        _sort_by_name(rest)
        order.extend(int(item["id"]) for item in rest)
    return order


def _compute_totals(
    parent_of: dict[int, int | None],
    depths: dict[int, int],
    own_counts: dict[int, int],
) -> dict[int, int]:
    """Треки папки вместе с подпапками: складываем снизу вверх, от самых глубоких."""
    totals: dict[int, int] = {
        folder_id: int(own_counts.get(folder_id, 0)) for folder_id in parent_of
    }
    for folder_id in sorted(parent_of, key=lambda item: depths.get(item, 0), reverse=True):
        parent = parent_of.get(folder_id)
        if parent is not None and parent in totals:
            totals[parent] += totals[folder_id]
    return totals


def _decorate(
    folder: dict,
    *,
    depths: dict[int, int],
    children: dict[int | None, list[int]],
    own_counts: dict[int, int],
    totals: dict[int, int],
    paths: dict[int, str] | None = None,
) -> dict:
    """Копия папки с агрегатами: глубина, путь, наличие подпапок и счётчики треков."""
    folder_id = int(folder["id"])
    item = dict(folder)
    item["depth"] = int(depths.get(folder_id, 0))
    item["path"] = str((paths or {}).get(folder_id) or item.get("name") or "")
    item["has_children"] = bool(children.get(folder_id))
    item["track_count"] = int(own_counts.get(folder_id, 0))
    item["total_track_count"] = int(totals.get(folder_id, item["track_count"]))
    return item


async def _fetch_sibling(
    user_id: int, section: str, parent_folder_id: int | None, normalized: str
) -> dict | None:
    """Папка с таким же нормализованным именем на том же уровне (или ``None``)."""
    row = await db.fetch_one(
        f"""
        {_SELECT_FOLDER}
        WHERE f.user_id = ? AND f.section = ? AND f.parent_folder_id IS ?
          AND f.normalized_name = ?
        ORDER BY f.id
        LIMIT 1
        """,
        (user_id, section, parent_folder_id, normalized),
    )
    return _row_to_folder(row)


async def _fetch_plain(user_id: int, folder_id: int) -> dict | None:
    """Папка без агрегатов (используется внутри проверок)."""
    row = await db.fetch_one(
        f"{_SELECT_FOLDER} WHERE f.user_id = ? AND f.id = ?",
        (user_id, int(folder_id)),
    )
    return _row_to_folder(row)


async def _depth_of(user_id: int, folder_id: int) -> int:
    """Глубина папки: 0 — корневая, 1 — вложенная в корневую и так далее."""
    value = await db.fetch_val(
        f"{_ANCESTORS_CTE} SELECT MAX(depth) FROM up",
        (user_id, int(folder_id), user_id, _DEPTH_GUARD),
        default=0,
    )
    return int(value or 0)


async def _subtree_height(user_id: int, folder_id: int) -> int:
    """Высота поддерева: 0 — подпапок нет, 1 — есть дети, и так далее."""
    value = await db.fetch_val(
        f"{_DESCENDANTS_CTE} SELECT MAX(depth) FROM sub",
        (user_id, int(folder_id), user_id, _DEPTH_GUARD),
        default=0,
    )
    return int(value or 0)


def _check_depth(depth: int, *, height: int = 0) -> None:
    """Проверяет, что папка на глубине ``depth`` с поддеревом высоты ``height`` влезает в лимит."""
    if depth + height + 1 > MAX_FOLDER_DEPTH:
        raise ValidationError(
            f"Слишком глубокая вложенность папок (максимум {MAX_FOLDER_DEPTH} уровней)"
        )


# --- публичный API ----------------------------------------------------------


async def create_folder(
    user_id: int,
    name: str,
    *,
    parent_folder_id: int | None = None,
    section: str = DEFAULT_SECTION,
    is_artist_folder: bool = False,
) -> dict:
    """Создаёт папку идемпотентно: если на этом уровне такое имя уже есть — вернёт её.

    Раздел вложенной папки всегда наследуется от родителя, поэтому подпапка не
    может оказаться в чужом разделе. Проверяются существование родителя и предел
    вложенности :data:`MAX_FOLDER_DEPTH`.
    """
    display, normalized = _clean_name(name)
    parent_id = _as_parent_id(parent_folder_id)
    section_name = _clean_section(section)

    if parent_id is not None:
        parent = await _fetch_plain(user_id, parent_id)
        if parent is None:
            raise ValidationError("Родительская папка не найдена")
        if parent["section"] != section_name:
            logger.info(
                "Пользователь %s: раздел подпапки «%s» взят от родителя (%s вместо %s)",
                user_id,
                display,
                parent["section"],
                section_name,
            )
        section_name = parent["section"]
        _check_depth(await _depth_of(user_id, parent_id) + 1)

    # Проверка соседа и вставка — в одной транзакции: так уникальность корневых
    # папок (UNIQUE их не ловит, потому что NULL != NULL) гарантируется кодом.
    async with db.transaction() as conn:
        existing = await _fetch_sibling(user_id, section_name, parent_id, normalized)
        if existing is None:
            cursor = await conn.execute(
                """
                INSERT INTO folders
                    (user_id, name, normalized_name, parent_folder_id, section, is_artist_folder)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    user_id,
                    display,
                    normalized,
                    parent_id,
                    section_name,
                    1 if is_artist_folder else 0,
                ),
            )
            await cursor.close()
            existing = await _fetch_sibling(user_id, section_name, parent_id, normalized)

    if existing is None:
        logger.error("Не удалось создать папку «%s» для пользователя %s", display, user_id)
        raise ValidationError("Не удалось создать папку, попробуйте ещё раз")

    folder = await get_folder(user_id, existing["id"])
    if folder is None:  # pragma: no cover - возможно только при удалении прямо во время вставки
        raise ValidationError("Не удалось создать папку, попробуйте ещё раз")
    logger.info(
        "Пользователь %s: папка «%s» готова (id=%s, раздел=%s, родитель=%s)",
        user_id,
        folder["name"],
        folder["id"],
        folder["section"],
        folder["parent_folder_id"],
    )
    return folder


async def get_folder(user_id: int, folder_id: int) -> dict | None:
    """Папка пользователя с агрегатами (собственные треки, треки с подпапками) или ``None``."""
    folder = await _fetch_plain(user_id, folder_id)
    if folder is None:
        return None

    subtree = await descendant_ids(user_id, folder["id"], include_self=True)
    folder["has_children"] = len(subtree) > 1
    folder["track_count"] = await _count_tracks(user_id, [folder["id"]])
    folder["total_track_count"] = await _count_tracks(user_id, subtree)
    folder["depth"] = await _depth_of(user_id, folder["id"])
    return folder


async def find_folder_by_name(
    user_id: int,
    name: str,
    *,
    parent_folder_id: Any = UNSET,
    section: str | None = DEFAULT_SECTION,
) -> dict | None:
    """Ищет папку по нормализованному названию (без учёта регистра и лишних пробелов).

    По умолчанию — на любом уровне раздела ``music`` (поведение V1: все папки V1
    после миграции лежат именно там). Приоритет у корневой папки, затем — по
    возрастанию идентификатора, чтобы результат не «прыгал» между вызовами.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None

    sql = f"{_SELECT_FOLDER} WHERE f.user_id = ? AND f.normalized_name = ?"
    params: list[Any] = [user_id, normalized]
    section_name = _section_filter(section)
    if section_name is not None:
        sql += " AND f.section = ?"
        params.append(section_name)
    if not _is_unset(parent_folder_id):
        sql += " AND f.parent_folder_id IS ?"
        params.append(_as_parent_id(parent_folder_id))
    sql += " ORDER BY (f.parent_folder_id IS NOT NULL), f.id LIMIT 1"

    row = await db.fetch_one(sql, tuple(params))
    folder = _row_to_folder(row)
    if folder is None:
        return None
    return await get_folder(user_id, folder["id"])


async def list_folders(
    user_id: int,
    *,
    parent_folder_id: Any = UNSET,
    section: str | None = DEFAULT_SECTION,
    include_counts: bool = True,
) -> list[dict]:
    """Плоский список папок раздела, отсортированный по названию (в Python).

    ``parent_folder_id=UNSET`` — все папки раздела (поведение V1),
    ``parent_folder_id=None`` — только корневые, число — только дети этой папки.
    ``section=None`` снимает фильтр по разделу.
    У каждой папки: ``track_count`` (свои треки), ``total_track_count``
    (вместе с подпапками), ``has_children``, ``depth`` и ``path`` (полный путь
    вида «Рок / Русский рок»).

    Порядок для ``UNSET`` — обход дерева: подпапка идёт сразу за своим
    родителем, внутри уровня — алфавит. У плоских данных V1 (все папки
    корневые) это в точности алфавитный список, как и раньше. Для одного уровня
    (``None`` или id родителя) порядок алфавитный.
    """
    section_name = _section_filter(section)
    folders, parent_of, children = await _snapshot(user_id, section_name)
    if not folders:
        return []

    depths = _compute_depths(parent_of)
    paths = _compute_paths(folders, parent_of)
    own_counts = await _own_counts(user_id) if include_counts else {}
    totals = _compute_totals(parent_of, depths, own_counts) if include_counts else {}

    whole_section = _is_unset(parent_folder_id)
    if whole_section:
        # Плоский список всего раздела — в порядке дерева, иначе подпапки
        # перемешиваются с корневыми и родителя по списку не угадать.
        selected: Iterable[int] = _tree_order(folders, children)
    else:
        target = _as_parent_id(parent_folder_id)
        selected = [
            folder_id for folder_id in folders if parent_of.get(folder_id) == target
        ]

    result = [
        _decorate(
            folders[folder_id],
            depths=depths,
            children=children,
            own_counts=own_counts,
            totals=totals,
            paths=paths,
        )
        for folder_id in selected
    ]
    if not whole_section:
        _sort_by_name(result)
    return result


async def folder_tree(user_id: int, *, section: str | None = DEFAULT_SECTION) -> list[dict]:
    """Дерево папок раздела: список корней, у каждого узла — ключ ``children``.

    Узлы отсортированы по алфавиту на каждом уровне и несут те же агрегаты,
    что и :func:`list_folders`.
    """
    section_name = _section_filter(section)
    folders, parent_of, children = await _snapshot(user_id, section_name)
    if not folders:
        return []

    depths = _compute_depths(parent_of)
    paths = _compute_paths(folders, parent_of)
    own_counts = await _own_counts(user_id)
    totals = _compute_totals(parent_of, depths, own_counts)

    nodes: dict[int, dict] = {}
    for folder_id, folder in folders.items():
        node = _decorate(
            folder,
            depths=depths,
            children=children,
            own_counts=own_counts,
            totals=totals,
            paths=paths,
        )
        node["children"] = []
        nodes[folder_id] = node

    roots: list[dict] = []
    for folder_id, node in nodes.items():
        parent = parent_of.get(folder_id)
        if parent is not None and parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    # Алфавитный порядок на каждом уровне (обход стеком, без рекурсии).
    _sort_by_name(roots)
    stack = list(roots)
    while stack:
        node = stack.pop()
        _sort_by_name(node["children"])
        stack.extend(node["children"])
    return roots


async def folder_path(user_id: int, folder_id: int) -> list[dict]:
    """Хлебные крошки от корня до папки включительно (пустой список — папки нет).

    В элементах только «опознавательные» поля папки и ``depth``; счётчики треков
    не считаются, чтобы крошки не стоили лишних запросов.
    """
    rows = await db.fetch_all(
        f"""
        {_ANCESTORS_CTE}
        SELECT {FOLDER_COLUMNS}, up.depth AS up_depth
        FROM up
        JOIN folders f ON f.id = up.id
        WHERE f.user_id = ?
        ORDER BY up.depth DESC
        """,
        (user_id, int(folder_id), user_id, _DEPTH_GUARD, user_id),
    )

    crumbs: list[dict] = []
    for row in rows:
        data = dict(row)
        data.pop("up_depth", None)
        crumb = _row_to_folder(data)
        if crumb is not None:
            crumb["depth"] = len(crumbs)
            if crumbs:
                crumb["path"] = f"{crumbs[-1]['path']}{PATH_SEPARATOR}{crumb['name']}"
            crumbs.append(crumb)
    return crumbs


async def descendant_ids(
    user_id: int, folder_id: int, *, include_self: bool = True
) -> list[int]:
    """Идентификаторы папки и всех её подпапок (рекурсивный CTE ``WITH RECURSIVE``).

    Порядок — по возрастанию глубины, затем по id. Пустой список означает, что
    папки нет у этого пользователя.
    """
    rows = await db.fetch_all(
        f"{_DESCENDANTS_CTE} SELECT id, depth FROM sub ORDER BY depth, id",
        (user_id, int(folder_id), user_id, _DEPTH_GUARD),
    )
    return [
        int(row["id"])
        for row in rows
        if include_self or int(row["depth"] or 0) > 0
    ]


async def rename_folder(user_id: int, folder_id: int, new_name: str) -> dict | None:
    """Переименовывает папку. ``None`` — папки нет, ``ValidationError`` — имя занято соседом."""
    display, normalized = _clean_name(new_name)

    # Проверка тёзки и UPDATE — в одной транзакции (как в create_folder): иначе
    # между ними успевает вклиниться параллельное переименование, а UNIQUE
    # корневых дублей не ловит (NULL != NULL).
    async with db.transaction() as conn:
        folder = await _fetch_plain(user_id, folder_id)
        if folder is None:
            return None

        conflict = await _fetch_sibling(
            user_id, folder["section"], folder["parent_folder_id"], normalized
        )
        if conflict is not None and conflict["id"] != folder["id"]:
            raise ValidationError(f"Папка «{conflict['name']}» уже существует на этом уровне")

        try:
            cursor = await conn.execute(
                "UPDATE folders SET name = ?, normalized_name = ? WHERE id = ? AND user_id = ?",
                (display, normalized, folder["id"], user_id),
            )
        except sqlite3.IntegrityError as exc:
            # Подстраховка на случай, если тёзка появился вопреки проверке:
            # понятная ошибка вместо 500 из обработчика непредвиденных исключений.
            raise ValidationError(f"Папка «{display}» уже существует на этом уровне") from exc
        await cursor.close()

    logger.info("Пользователь %s: папка %s переименована в «%s»", user_id, folder_id, display)
    return await get_folder(user_id, folder["id"])


async def move_folder(user_id: int, folder_id: int, new_parent_id: int | None) -> dict:
    """Переносит папку к другому родителю (``None`` — в корень раздела).

    Запрещено: переносить папку в саму себя или в собственную подпапку (цикл),
    переносить в другой раздел, превышать :data:`MAX_FOLDER_DEPTH` и создавать
    двух «тёзок» на одном уровне.
    """
    target_id = _as_parent_id(new_parent_id)

    # Все проверки и UPDATE — в одной транзакции (как в create_folder): иначе
    # между проверкой и записью успевает вклиниться параллельный перенос, и на
    # уровне появляются две одноимённые папки (UNIQUE не ловит корневые дубли,
    # потому что NULL != NULL) либо падает необработанный IntegrityError.
    async with db.transaction() as conn:
        folder = await _fetch_plain(user_id, folder_id)
        if folder is None:
            raise NotFoundError("Папка не найдена")

        if target_id == folder["id"]:
            raise ValidationError("Папку нельзя вложить саму в себя")

        if target_id is None:
            parent_depth = -1
        else:
            parent = await _fetch_plain(user_id, target_id)
            if parent is None:
                raise ValidationError("Родительская папка не найдена")
            if parent["section"] != folder["section"]:
                raise ValidationError(
                    "Нельзя перенести папку в другой раздел — "
                    f"«{SECTION_TITLES.get(folder['section'], folder['section'])}» "
                    f"и «{SECTION_TITLES.get(parent['section'], parent['section'])}» разделены"
                )
            subtree = await descendant_ids(user_id, folder["id"], include_self=True)
            if target_id in subtree:
                raise ValidationError("Нельзя перенести папку внутрь её же подпапки")
            parent_depth = await _depth_of(user_id, target_id)

        if folder["parent_folder_id"] == target_id:
            logger.debug("Пользователь %s: папка %s уже находится там же", user_id, folder_id)
        else:
            _check_depth(parent_depth + 1, height=await _subtree_height(user_id, folder["id"]))

            conflict = await _fetch_sibling(
                user_id, folder["section"], target_id, folder["normalized_name"]
            )
            if conflict is not None and conflict["id"] != folder["id"]:
                raise ValidationError(
                    f"Папка «{conflict['name']}» уже существует на этом уровне"
                )

            try:
                cursor = await conn.execute(
                    "UPDATE folders SET parent_folder_id = ? WHERE id = ? AND user_id = ?",
                    (target_id, folder["id"], user_id),
                )
            except sqlite3.IntegrityError as exc:
                # Подстраховка: понятная ошибка вместо 500 из обработчика
                # непредвиденных исключений.
                raise ValidationError(
                    f"Папка «{folder['name']}» уже существует на этом уровне"
                ) from exc
            await cursor.close()
            logger.info(
                "Пользователь %s: папка %s перенесена (родитель %s -> %s)",
                user_id,
                folder_id,
                folder["parent_folder_id"],
                target_id,
            )

    moved = await get_folder(user_id, folder["id"])
    if moved is None:  # pragma: no cover - папка удалена между запросами
        raise NotFoundError("Папка не найдена")
    return moved


async def delete_folder(
    user_id: int,
    folder_id: int,
    *,
    delete_tracks: bool = False,
    recursive: bool = True,
) -> bool:
    """Удаляет папку. ``False`` — папки не было.

    ``recursive=True`` (по умолчанию) удаляет папку вместе со всеми подпапками.
    При ``recursive=False`` и наличии подпапок бросается ``ValidationError`` —
    иначе каскад ``ON DELETE CASCADE`` тихо унёс бы всё поддерево.
    ``delete_tracks=False`` (по умолчанию) треки сохраняет: они остаются без папки.
    """
    folder = await _fetch_plain(user_id, folder_id)
    if folder is None:
        return False

    subtree = await descendant_ids(user_id, folder["id"], include_self=True)
    if not subtree:  # pragma: no cover - папка удалена между запросами
        return False
    if not recursive and len(subtree) > 1:
        raise ValidationError(
            f"Папка «{folder['name']}» содержит вложенные папки. "
            "Удалите их отдельно или разрешите удаление вместе с подпапками"
        )

    placeholders = ", ".join("?" for _ in subtree)
    async with db.transaction() as conn:
        if delete_tracks:
            cursor = await conn.execute(
                f"DELETE FROM tracks WHERE user_id = ? AND folder_id IN ({placeholders})",
                (user_id, *subtree),
            )
            removed: int = cursor.rowcount or 0
            await cursor.close()
            logger.info(
                "Пользователь %s: вместе с папкой %s удалено треков: %s",
                user_id,
                folder_id,
                removed,
            )
        else:
            cursor = await conn.execute(
                f"UPDATE tracks SET folder_id = NULL "
                f"WHERE user_id = ? AND folder_id IN ({placeholders})",
                (user_id, *subtree),
            )
            await cursor.close()

        # Исполнители остаются, но теряют привязку к удаляемым папкам.
        cursor = await conn.execute(
            f"UPDATE artists SET folder_id = NULL "
            f"WHERE user_id = ? AND folder_id IN ({placeholders})",
            (user_id, *subtree),
        )
        await cursor.close()

        cursor = await conn.execute(
            f"DELETE FROM folders WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *subtree),
        )
        deleted: int = cursor.rowcount or 0
        await cursor.close()

    logger.info(
        "Пользователь %s: удалена папка %s (всего папок удалено: %s)",
        user_id,
        folder_id,
        deleted,
    )
    return deleted > 0


async def folder_track_count(user_id: int, folder_id: int, *, recursive: bool = False) -> int:
    """Количество треков в папке (``recursive=True`` — вместе с подпапками)."""
    if not recursive:
        return await _count_tracks(user_id, [int(folder_id)])
    subtree = await descendant_ids(user_id, folder_id, include_self=True)
    return await _count_tracks(user_id, subtree)
