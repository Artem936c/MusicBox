"""Пакетное распределение загруженных файлов по папкам (ТЗ п. 8).

Пользователь присылает боту несколько файлов подряд; после загрузки их нужно
разложить по папкам — целиком, группами по исполнителю или указанным количеством
(«добавь в папку 5 треков, остальные потом»). Модуль хранит состояние такой
раскладки (:class:`BatchState`), группирует треки по исполнителю, переносит их
в папку и объясняет по-русски, что осталось нераспределённым.

Зависимостей от aiogram здесь НЕТ: это чистая логика поверх репозиториев,
одинаково пригодная для бота и для API. Тексты готовы к отправке в HTML-режиме
Telegram — имена исполнителей экранированы.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Sequence

from backend.db.repositories import folders as folders_repo
from backend.db.repositories import tracks as tracks_repo
from backend.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "BatchState",
    "NO_ARTIST_LABEL",
    "MAX_SUMMARY_GROUPS",
    "group_by_artist",
    "select_batch",
    "assign",
    "assign_state",
    "remaining_summary",
]

#: Подпись группы треков без исполнителя.
NO_ARTIST_LABEL: Final[str] = "Без исполнителя"

#: Сколько исполнителей показывать в сводке об остатке.
MAX_SUMMARY_GROUPS: Final[int] = 8

#: Формы существительного «трек» для склонения по числу.
_TRACK_FORMS: Final[tuple[str, str, str]] = ("трек", "трека", "треков")

#: Формы существительного «исполнитель».
_ARTIST_FORMS: Final[tuple[str, str, str]] = ("исполнителя", "исполнителей", "исполнителей")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _unique_ids(values: Iterable[Any]) -> list[int]:
    """Список целых идентификаторов без повторов, порядок сохраняется."""
    result: list[int] = []
    seen: set[int] = set()
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.debug("Пропускаю некорректный идентификатор трека: %r", value)
            continue
        if number in seen:
            continue
        seen.add(number)
        result.append(number)
    return result


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """Склонение существительного по числу: `_plural(2, _TRACK_FORMS)` → «2 трека».

    Своя копия помощника из `backend.bot.utils`: тот модуль тянет за собой aiogram,
    а этот сервис обязан оставаться независимым от бота.
    """
    number = abs(int(count))
    if number % 10 == 1 and number % 100 != 11:
        form = forms[0]
    elif 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


def _escape(value: str | None) -> str:
    """Экранирует имя для HTML-режима Telegram."""
    return html.escape(str(value or ""), quote=False)


def _positive_limit(limit: Any) -> int:
    """Проверяет введённое пользователем количество треков."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ValidationError(
            "Количество треков нужно указать числом, например: 5"
        ) from None
    if value < 1:
        raise ValidationError("Количество треков должно быть больше нуля")
    return value


def _artist_label(track: dict) -> str:
    """Имя исполнителя трека для группировки и сводки."""
    name = str(track.get("artist") or "").strip()
    return name or NO_ARTIST_LABEL


# ---------------------------------------------------------------------------
# Состояние раскладки
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BatchState:
    """Состояние пакетного распределения одной группы загруженных треков.

    * `track_ids` — все треки пачки в порядке загрузки;
    * `pending` — ещё не распределённые;
    * `assigned` — `track_id -> folder_id` (значение None означает «оставлен без папки»).

    Если создать состояние только с `track_ids`, все треки считаются нераспределёнными;
    при восстановлении состояния (например, из хранилища FSM) очередь `pending`
    достраивается по `assigned`, чтобы ни один трек пачки не потерялся.
    """

    user_id: int
    track_ids: list[int]
    pending: list[int] = field(default_factory=list)
    assigned: dict[int, int | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.user_id = int(self.user_id)
        self.track_ids = _unique_ids(self.track_ids)
        known = set(self.track_ids)

        # Состояние может вернуться из хранилища FSM, где ключи стали строками,
        # поэтому приводим их к числам и молча отбрасываем мусор.
        restored: dict[int, int | None] = {}
        for raw_track_id, raw_folder_id in dict(self.assigned or {}).items():
            try:
                track_id = int(raw_track_id)
                folder_id = None if raw_folder_id is None else int(raw_folder_id)
            except (TypeError, ValueError):
                logger.debug("Пропускаю некорректную запись раскладки: %r", raw_track_id)
                continue
            if track_id in known:
                restored[track_id] = folder_id
        self.assigned = restored
        if self.pending:
            self.pending = [
                track_id
                for track_id in _unique_ids(self.pending)
                if track_id in known and track_id not in self.assigned
            ]
        else:
            # Очередь не задана: нераспределено всё, что ещё не попало в папку.
            self.pending = [
                track_id for track_id in self.track_ids if track_id not in self.assigned
            ]

    @classmethod
    def start(cls, user_id: int, track_ids: Sequence[int]) -> BatchState:
        """Новое состояние: все треки ждут распределения."""
        return cls(user_id=int(user_id), track_ids=list(track_ids))

    @property
    def total(self) -> int:
        """Сколько треков в пачке."""
        return len(self.track_ids)

    @property
    def remaining(self) -> int:
        """Сколько треков ещё не распределено."""
        return len(self.pending)

    @property
    def is_complete(self) -> bool:
        """Все ли треки разложены."""
        return not self.pending

    def mark_assigned(self, track_ids: Sequence[int], folder_id: int | None) -> list[int]:
        """Помечает треки распределёнными и убирает их из `pending`.

        Возвращает идентификаторы, которые действительно ждали распределения.
        """
        target = None if folder_id is None else int(folder_id)
        marked: list[int] = []
        pending = set(self.pending)
        for track_id in _unique_ids(track_ids):
            if track_id not in pending:
                continue
            self.assigned[track_id] = target
            marked.append(track_id)
        if marked:
            done = set(marked)
            self.pending = [track_id for track_id in self.pending if track_id not in done]
        return marked

    def by_folder(self) -> dict[int | None, list[int]]:
        """Разложенные треки, сгруппированные по папкам."""
        result: dict[int | None, list[int]] = {}
        for track_id in self.track_ids:
            if track_id in self.assigned:
                result.setdefault(self.assigned[track_id], []).append(track_id)
        return result


# ---------------------------------------------------------------------------
# Группировка и распределение
# ---------------------------------------------------------------------------


async def group_by_artist(
    user_id: int, track_ids: Sequence[int]
) -> dict[int | None, list[int]]:
    """Группирует треки по исполнителю: `artist_id -> [track_id, ...]`.

    Ключ None — треки без привязанного исполнителя. Группы отсортированы по имени
    исполнителя (кириллица учитывается), группа без исполнителя идёт последней;
    внутри группы порядок треков совпадает с порядком в `track_ids`.
    Треки, которых нет в библиотеке пользователя, молча пропускаются.
    """
    wanted = _unique_ids(track_ids)
    if not wanted:
        return {}

    tracks = await tracks_repo.get_tracks_by_ids(int(user_id), wanted)
    if len(tracks) != len(wanted):
        logger.warning(
            "Пользователь %s: для группировки найдено %s треков из %s",
            user_id,
            len(tracks),
            len(wanted),
        )

    groups: dict[int | None, list[int]] = {}
    labels: dict[int | None, str] = {}
    for track in tracks:
        artist_id = track.get("artist_id")
        key = int(artist_id) if artist_id is not None else None
        groups.setdefault(key, []).append(int(track["id"]))
        labels.setdefault(key, _artist_label(track))

    order = sorted(groups, key=lambda key: (key is None, labels.get(key, "").casefold()))
    return {key: groups[key] for key in order}


def select_batch(track_ids: Sequence[int], limit: int | None = None) -> list[int]:
    """Первые `limit` треков из списка (без повторов); `limit=None` — все.

    Бросает :class:`~backend.errors.ValidationError`, если количество указано
    не числом или меньше единицы.
    """
    wanted = _unique_ids(track_ids)
    if limit is None:
        return wanted
    return wanted[: _positive_limit(limit)]


async def assign(
    user_id: int,
    track_ids: Sequence[int],
    folder_id: int | None,
    *,
    limit: int | None = None,
) -> int:
    """Переносит треки в папку и возвращает число перемещённых.

    `folder_id=None` — вынести треки из папок. `limit` — «указать количество треков
    для добавления в папку» (ТЗ п. 8.4): берутся первые `limit` идентификаторов
    из `track_ids`, остальные остаются нераспределёнными.

    Бросает :class:`~backend.errors.NotFoundError`, если папки нет у пользователя,
    и :class:`~backend.errors.ValidationError` при некорректном `limit`.
    """
    batch = select_batch(track_ids, limit)
    if not batch:
        return 0

    owner_id = int(user_id)
    target = None if folder_id is None else int(folder_id)
    if target is not None:
        folder = await folders_repo.get_folder(owner_id, target)
        if folder is None:
            raise NotFoundError("Папка не найдена — возможно, её уже удалили.")

    moved = int(await tracks_repo.move_tracks(owner_id, batch, target))
    logger.info(
        "Пользователь %s: в папку %s добавлено %s из %s треков",
        owner_id,
        target if target is not None else "«без папки»",
        moved,
        len(batch),
    )
    return moved


async def assign_state(
    state: BatchState, folder_id: int | None, *, limit: int | None = None
) -> list[int]:
    """Распределяет часть `state.pending` в папку и обновляет состояние.

    Возвращает идентификаторы треков, которые ушли в папку. Треки, пропавшие из
    библиотеки между шагами, тоже убираются из `pending` — иначе распределение
    никогда бы не завершилось.
    """
    batch = select_batch(state.pending, limit)
    if not batch:
        return []

    moved = await assign(state.user_id, batch, folder_id)
    if moved != len(batch):
        logger.warning(
            "Пользователь %s: перемещено %s из %s треков — часть уже удалена",
            state.user_id,
            moved,
            len(batch),
        )
    return state.mark_assigned(batch, folder_id)


# ---------------------------------------------------------------------------
# Сводка об остатке
# ---------------------------------------------------------------------------


async def remaining_summary(user_id: int, state: BatchState) -> str:
    """Русский текст о том, что осталось распределить (готов для HTML-режима бота)."""
    owner_id = int(user_id)
    if owner_id != state.user_id:
        logger.warning(
            "Состояние раскладки принадлежит пользователю %s, а запрос от %s",
            state.user_id,
            owner_id,
        )

    total = state.total
    if state.is_complete:
        if not total:
            return "Распределять нечего: в пачке нет треков."
        return f"Готово: все {_plural(total, _TRACK_FORMS)} разложены по папкам."

    pending = list(state.pending)
    tracks = await tracks_repo.get_tracks_by_ids(owner_id, pending)

    counts: dict[str, int] = {}
    for track in tracks:
        label = _artist_label(track)
        counts[label] = counts.get(label, 0) + 1

    lines = [
        f"Остались нераспределённые: <b>{_plural(len(pending), _TRACK_FORMS)}</b> из {total}."
    ]

    if counts:
        ordered = sorted(
            counts.items(),
            key=lambda item: (item[0] == NO_ARTIST_LABEL, -item[1], item[0].casefold()),
        )
        lines.append("По исполнителям:")
        for label, count in ordered[:MAX_SUMMARY_GROUPS]:
            lines.append(f"• {_escape(label)} — {_plural(count, _TRACK_FORMS)}")
        hidden = len(ordered) - MAX_SUMMARY_GROUPS
        if hidden > 0:
            lines.append(f"…и ещё {_plural(hidden, _ARTIST_FORMS)}")

    missing = len(pending) - len(tracks)
    if missing > 0:
        lines.append(f"Не найдено в библиотеке: {_plural(missing, _TRACK_FORMS)} (уже удалены).")

    lines.append(
        "Создайте новую папку или добавьте треки в существующую — "
        "можно указать, сколько треков добавить."
    )
    return "\n".join(lines)
