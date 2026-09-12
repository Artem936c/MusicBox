"""Клавиатуры бота MusicBox.

Кнопки треков намеренно компактные (эмодзи + номер), чтобы в строку помещалось
несколько действий, а сам список треков рендерился текстом
(`backend.bot.utils.render_track_list`) со сквозной нумерацией.

Кнопка Mini App добавляется только если `settings.webapp_url` начинается с `https://`
(Telegram не принимает WebApp-кнопки с другим протоколом).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot.callbacks import (
    FOLDER_NEW,
    FOLDER_NONE,
    ArtistCB,
    ArtistPickCB,
    FolderCB,
    MoveCB,
    NavCB,
    PlaylistCB,
    SectionCB,
    TgSearchCB,
    TrackCB,
)
from backend.bot.texts import MENU_ALIASES, MENU_PLACEHOLDER, SECTION_HEADERS
from backend.bot.utils import page_offset, parse_context
from backend.config import settings
from backend.db.repositories.stats import SECTION_KEYS

logger = logging.getLogger(__name__)

#: Максимальная длина подписи с пользовательским текстом внутри кнопки.
LABEL_LIMIT = 28

WEBAPP_MENU_TEXT = "🎵 Открыть MusicBox"
WEBAPP_INLINE_TEXT = "🎛 Открыть приложение"

NOOP = NavCB(action="noop").pack()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def webapp_url() -> str:
    """Адрес Mini App, если он пригоден для WebApp-кнопки (иначе пустая строка)."""
    url = (settings.webapp_url or "").strip()
    if url.lower().startswith("https://"):
        return url
    if url:
        logger.debug("WEBAPP_URL=%r не начинается с https:// — кнопка Mini App скрыта", url)
    return ""


def webapp_available() -> bool:
    """Можно ли показывать кнопку Mini App."""
    return bool(webapp_url())


def _short(value: Any, limit: int = LABEL_LIMIT) -> str:
    """Обрезает подпись кнопки до разумной длины (без HTML — это обычный текст)."""
    text = str(value or "").replace("\n", " ").strip()
    if not text:
        return "Без названия"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _get(item: Any, key: str, default: Any = None) -> Any:
    """Читает поле из dict или из объекта (RemoteAudio и т.п.)."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _int(value: Any, default: int = 0) -> int:
    """Мягкое приведение к int (идентификаторы приходят из БД и из callback-данных)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def page_callback(ctx: str, page: int) -> str:
    """Callback-данные для перелистывания списка в контексте `ctx`.

    Каждый контекст листается «своей» фабрикой — той, которую слушает
    отвечающий за него роутер:
      * ``folder:<id>`` → `FolderCB(action="page")` (handlers/folders.py);
      * ``pl:<id>`` → `PlaylistCB(action="page")` (handlers/playlists.py);
      * ``art:<id>`` / ``artist:<id>`` → `ArtistCB(action="page")` (handlers/artists.py);
      * разделы статистики → `SectionCB(action="page")` (handlers/stats.py);
      * остальное (``fav``, ``search``, …) → `TrackCB(action="page")`, который
        слушают handlers/favourites.py и handlers/search.py.
    """
    kind, ident = parse_context(ctx)
    if kind == "folder":
        return FolderCB(action="page", folder_id=ident, page=page).pack()
    if kind == "pl":
        return PlaylistCB(action="page", playlist_id=ident, track_id=0, page=page).pack()
    if kind in ("art", "artist"):
        return ArtistCB(action="page", artist_id=ident, page=page).pack()
    if kind in SECTION_KEYS:
        return SectionCB(action="page", key=kind, page=page).pack()
    return TrackCB(action="page", track_id=0, page=page, ctx=ctx).pack()


def back_callback(ctx: str) -> str:
    """Callback-данные кнопки «⬅️ Назад» для списка в контексте `ctx`."""
    kind, _ = parse_context(ctx)
    if kind == "folder":
        return NavCB(action="folders").pack()
    if kind == "pl":
        return NavCB(action="playlists").pack()
    if kind in ("art", "artist"):
        return NavCB(action="artists").pack()
    if kind == "fav":
        return NavCB(action="menu").pack()
    if kind in SECTION_KEYS:
        return NavCB(action="stats").pack()
    return NavCB(action="menu").pack()


def pagination_row(ctx: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    """Строка пагинации «◀️ N/M ▶️» (пустая, если страница всего одна)."""
    if total_pages <= 1:
        return []
    current = min(max(_int(page, 1), 1), total_pages)
    prev_data = page_callback(ctx, current - 1) if current > 1 else NOOP
    next_data = page_callback(ctx, current + 1) if current < total_pages else NOOP
    return [
        InlineKeyboardButton(text="◀️" if current > 1 else "·", callback_data=prev_data),
        InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=NOOP),
        InlineKeyboardButton(text="▶️" if current < total_pages else "·", callback_data=next_data),
    ]


def webapp_button(text: str = WEBAPP_INLINE_TEXT) -> InlineKeyboardButton | None:
    """Инлайн-кнопка Mini App или None, если приложение не настроено."""
    url = webapp_url()
    if not url:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))


# ---------------------------------------------------------------------------
# Главное меню и разделы
# ---------------------------------------------------------------------------


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: кнопка Mini App (если настроена) и разделы бота.

    Подписи кнопок соответствуют командам (`MENU_ALIASES`); подмену подписи
    на команду выполняет `backend.bot.middlewares.MenuAliasMiddleware`.
    """
    rows: list[list[KeyboardButton]] = []

    url = webapp_url()
    if url:
        rows.append([KeyboardButton(text=WEBAPP_MENU_TEXT, web_app=WebAppInfo(url=url))])

    labels = list(MENU_ALIASES)
    for start in range(0, len(labels), 2):
        rows.append([KeyboardButton(text=label) for label in labels[start : start + 2]])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=MENU_PLACEHOLDER,
    )


def sections_kb(active: str | None = None) -> InlineKeyboardMarkup:
    """Пять разделов статистики + кнопка Mini App."""
    builder = InlineKeyboardBuilder()
    for key in SECTION_KEYS:
        label = SECTION_HEADERS.get(key, key)
        if active and key == active:
            label = f"• {label}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=SectionCB(action="open", key=key, page=1).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(text="📁 Папки", callback_data=NavCB(action="folders").pack()),
        InlineKeyboardButton(text="💿 Плейлисты", callback_data=NavCB(action="playlists").pack()),
    )
    builder.row(
        InlineKeyboardButton(text="⭐ Избранное", callback_data=NavCB(action="fav").pack()),
        InlineKeyboardButton(text="🎤 Исполнители", callback_data=NavCB(action="artists").pack()),
    )

    button = webapp_button()
    if button is not None:
        builder.row(button)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Треки
# ---------------------------------------------------------------------------


def tracks_page_kb(
    tracks: Sequence[dict],
    *,
    ctx: str,
    page: int,
    total_pages: int,
    extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура страницы со списком треков.

    На каждый трек — компактная строка «▶️ N», «⭐/☆», «➕»; номера совпадают
    со сквозной нумерацией из `render_track_list`.
    """
    builder = InlineKeyboardBuilder()
    start = page_offset(page) + 1

    for shift, track in enumerate(tracks):
        track_id = _int(_get(track, "id"))
        number = start + shift
        star = "⭐" if _get(track, "is_favourite") else "☆"
        builder.row(
            InlineKeyboardButton(
                text=f"▶️ {number}",
                callback_data=TrackCB(
                    action="play", track_id=track_id, page=page, ctx=ctx
                ).pack(),
            ),
            InlineKeyboardButton(
                text=star,
                callback_data=TrackCB(
                    action="fav", track_id=track_id, page=page, ctx=ctx
                ).pack(),
            ),
            InlineKeyboardButton(
                text="➕",
                callback_data=TrackCB(
                    action="addpl", track_id=track_id, page=page, ctx=ctx
                ).pack(),
            ),
        )

    for row in extra_rows or ():
        if row:
            builder.row(*row)

    pages = pagination_row(ctx, page, total_pages)
    if pages:
        builder.row(*pages)

    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback(ctx))
    )
    return builder.as_markup()


def track_actions_kb(track: dict, ctx: str, page: int) -> InlineKeyboardMarkup:
    """Карточка одного трека: воспроизведение, избранное, плейлист, папка, удаление.

    «🗑 Удалить» отправляет `TrackCB(action="delete")` с положительной страницей —
    обработчик в `handlers/stats.py` сначала спрашивает подтверждение.
    """
    track_id = _int(_get(track, "id"))
    fav = bool(_get(track, "is_favourite"))
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="▶️ Прослушать",
            callback_data=TrackCB(action="play", track_id=track_id, page=page, ctx=ctx).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⭐ В избранном" if fav else "☆ В избранное",
            callback_data=TrackCB(action="fav", track_id=track_id, page=page, ctx=ctx).pack(),
        ),
        InlineKeyboardButton(
            text="➕ В плейлист",
            callback_data=TrackCB(action="addpl", track_id=track_id, page=page, ctx=ctx).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📁 В папку",
            callback_data=TrackCB(action="move", track_id=track_id, page=page, ctx=ctx).pack(),
        ),
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=TrackCB(
                action="delete", track_id=track_id, page=max(_int(page, 1), 1), ctx=ctx
            ).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=TrackCB(action="back", track_id=track_id, page=page, ctx=ctx).pack(),
        )
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Папки
# ---------------------------------------------------------------------------


def folders_kb(
    folders: Sequence[dict],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Список папок с пагинацией и кнопкой создания."""
    builder = InlineKeyboardBuilder()

    for folder in folders:
        folder_id = _int(_get(folder, "id"))
        count = _get(folder, "track_count")
        name = _short(_get(folder, "name"))
        label = f"📁 {name}" if count is None else f"📁 {name} · {_int(count)}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=FolderCB(action="open", folder_id=folder_id, page=1).pack(),
            )
        )

    if total_pages > 1:
        current = min(max(_int(page, 1), 1), total_pages)
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·",
                callback_data=(
                    FolderCB(action="page", folder_id=0, page=current - 1).pack()
                    if current > 1
                    else NOOP
                ),
            ),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=NOOP),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·",
                callback_data=(
                    FolderCB(action="page", folder_id=0, page=current + 1).pack()
                    if current < total_pages
                    else NOOP
                ),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="🆕 Новая папка",
            callback_data=FolderCB(action="create", folder_id=0, page=_int(page, 1)).pack(),
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ Меню", callback_data=NavCB(action="menu").pack()))
    return builder.as_markup()


def folder_pick_kb(folders: Sequence[dict], track_id: int) -> InlineKeyboardMarkup:
    """Выбор папки для трека: существующие папки, новая папка и «Без папки»."""
    builder = InlineKeyboardBuilder()
    track_id = _int(track_id)

    for folder in folders:
        builder.row(
            InlineKeyboardButton(
                text=f"📁 {_short(_get(folder, 'name'))}",
                callback_data=MoveCB(
                    track_id=track_id, folder_id=_int(_get(folder, "id"))
                ).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🆕 Новая папка",
            callback_data=MoveCB(track_id=track_id, folder_id=FOLDER_NEW).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🚫 Без папки",
            callback_data=MoveCB(track_id=track_id, folder_id=FOLDER_NONE).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(text="✖️ Отмена", callback_data=NavCB(action="menu").pack())
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Плейлисты
# ---------------------------------------------------------------------------


def playlists_kb(
    playlists: Sequence[dict],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Список плейлистов с пагинацией и кнопкой создания."""
    builder = InlineKeyboardBuilder()

    for playlist in playlists:
        playlist_id = _int(_get(playlist, "id"))
        count = _get(playlist, "track_count")
        name = _short(_get(playlist, "name"))
        label = f"💿 {name}" if count is None else f"💿 {name} · {_int(count)}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=PlaylistCB(
                    action="open", playlist_id=playlist_id, track_id=0, page=1
                ).pack(),
            )
        )

    if total_pages > 1:
        current = min(max(_int(page, 1), 1), total_pages)
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·",
                callback_data=(
                    PlaylistCB(
                        action="page", playlist_id=0, track_id=0, page=current - 1
                    ).pack()
                    if current > 1
                    else NOOP
                ),
            ),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=NOOP),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·",
                callback_data=(
                    PlaylistCB(
                        action="page", playlist_id=0, track_id=0, page=current + 1
                    ).pack()
                    if current < total_pages
                    else NOOP
                ),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="🆕 Новый плейлист",
            callback_data=PlaylistCB(
                action="create", playlist_id=0, track_id=0, page=_int(page, 1)
            ).pack(),
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ Меню", callback_data=NavCB(action="menu").pack()))
    return builder.as_markup()


def playlist_pick_kb(playlists: Sequence[dict], track_id: int) -> InlineKeyboardMarkup:
    """Выбор плейлиста, в который добавить трек."""
    builder = InlineKeyboardBuilder()
    track_id = _int(track_id)

    for playlist in playlists:
        builder.row(
            InlineKeyboardButton(
                text=f"💿 {_short(_get(playlist, 'name'))}",
                callback_data=PlaylistCB(
                    action="add",
                    playlist_id=_int(_get(playlist, "id")),
                    track_id=track_id,
                    page=1,
                ).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🆕 Новый плейлист",
            callback_data=PlaylistCB(
                action="create", playlist_id=0, track_id=track_id, page=1
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✖️ Отмена",
            callback_data=PlaylistCB(
                action="back", playlist_id=0, track_id=track_id, page=1
            ).pack(),
        )
    )
    return builder.as_markup()


def playlist_tracks_kb(
    tracks: Sequence[dict],
    playlist_id: int,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Состав плейлиста: порядок (⬆️/⬇️), воспроизведение (▶️) и удаление (❌)."""
    builder = InlineKeyboardBuilder()
    playlist_id = _int(playlist_id)
    start = page_offset(page) + 1
    total = len(tracks)

    for shift, track in enumerate(tracks):
        track_id = _int(_get(track, "id"))
        number = start + shift
        first = shift == 0 and _int(page, 1) <= 1
        last = shift == total - 1 and _int(page, 1) >= max(total_pages, 1)
        builder.row(
            InlineKeyboardButton(
                text="⬆️" if not first else "·",
                callback_data=(
                    PlaylistCB(
                        action="up", playlist_id=playlist_id, track_id=track_id, page=page
                    ).pack()
                    if not first
                    else NOOP
                ),
            ),
            InlineKeyboardButton(
                text="⬇️" if not last else "·",
                callback_data=(
                    PlaylistCB(
                        action="down", playlist_id=playlist_id, track_id=track_id, page=page
                    ).pack()
                    if not last
                    else NOOP
                ),
            ),
            InlineKeyboardButton(
                text=f"▶️ {number}",
                callback_data=PlaylistCB(
                    action="play", playlist_id=playlist_id, track_id=track_id, page=page
                ).pack(),
            ),
            InlineKeyboardButton(
                text="❌",
                callback_data=PlaylistCB(
                    action="remove", playlist_id=playlist_id, track_id=track_id, page=page
                ).pack(),
            ),
        )

    if total:
        builder.row(
            InlineKeyboardButton(
                text="▶️ Проиграть плейлист",
                callback_data=PlaylistCB(
                    action="play", playlist_id=playlist_id, track_id=0, page=page
                ).pack(),
            )
        )

    pages = pagination_row(f"pl:{playlist_id}", page, total_pages)
    if pages:
        builder.row(*pages)

    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить плейлист",
            callback_data=PlaylistCB(
                action="delete", playlist_id=playlist_id, track_id=0, page=page
            ).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=PlaylistCB(
                action="list", playlist_id=0, track_id=0, page=1
            ).pack(),
        ),
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Исполнители
# ---------------------------------------------------------------------------


def artists_kb(
    artists: Sequence[dict],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Список исполнителей: отметка «прослушано» и переход к трекам."""
    builder = InlineKeyboardBuilder()

    for artist in artists:
        artist_id = _int(_get(artist, "id"))
        mark = "✅" if _get(artist, "is_listened") else "⬜"
        count = _get(artist, "track_count")
        name = _short(_get(artist, "name"), LABEL_LIMIT - 4)
        label = f"🎤 {name}" if count is None else f"🎤 {name} · {_int(count)}"
        builder.row(
            InlineKeyboardButton(
                text=mark,
                callback_data=ArtistCB(
                    action="listened", artist_id=artist_id, page=_int(page, 1)
                ).pack(),
            ),
            InlineKeyboardButton(
                text=label,
                callback_data=ArtistCB(action="open", artist_id=artist_id, page=1).pack(),
            ),
        )

    if total_pages > 1:
        current = min(max(_int(page, 1), 1), total_pages)
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·",
                callback_data=(
                    ArtistCB(action="page", artist_id=0, page=current - 1).pack()
                    if current > 1
                    else NOOP
                ),
            ),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data=NOOP),
            InlineKeyboardButton(
                text="▶️" if current < total_pages else "·",
                callback_data=(
                    ArtistCB(action="page", artist_id=0, page=current + 1).pack()
                    if current < total_pages
                    else NOOP
                ),
            ),
        )

    builder.row(InlineKeyboardButton(text="⬅️ Меню", callback_data=NavCB(action="menu").pack()))
    return builder.as_markup()


def artist_multiselect_kb(
    artists: Sequence[dict],
    selected_ids: Iterable[int] | None = None,
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Мультивыбор исполнителей для фильтра поиска (`SearchStates.waiting_artists`).

    У отмеченных исполнителей стоит «✅», у остальных — «⬜»; нажатие переключает
    отметку (`ArtistPickCB(action="toggle")`), кнопка «Готово» применяет фильтр
    (`action="done"`). Отмеченные исполнители с других страниц не теряются:
    список накопленных id хендлер хранит в данных FSM и передаёт сюда через
    `selected_ids`.

    Страница листается `ArtistPickCB(action="page")`, «♻️ Сбросить» снимает все
    отметки (`action="clear"`), «✖️ Отмена» закрывает выбор (`action="cancel"`).
    """
    builder = InlineKeyboardBuilder()
    current = min(max(_int(page, 1), 1), max(_int(total_pages, 1), 1))
    selected: set[int] = {_int(item) for item in (selected_ids or ())}

    for artist in artists:
        artist_id = _int(_get(artist, "id"))
        mark = "✅" if artist_id in selected else "⬜"
        count = _get(artist, "track_count")
        name = _short(_get(artist, "name"), LABEL_LIMIT - 4)
        label = f"{mark} {name}" if count is None else f"{mark} {name} · {_int(count)}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=ArtistPickCB(
                    action="toggle", artist_id=artist_id, page=current
                ).pack(),
            )
        )

    pages = max(_int(total_pages, 1), 1)
    if pages > 1:
        builder.row(
            InlineKeyboardButton(
                text="◀️" if current > 1 else "·",
                callback_data=(
                    ArtistPickCB(action="page", artist_id=0, page=current - 1).pack()
                    if current > 1
                    else NOOP
                ),
            ),
            InlineKeyboardButton(text=f"{current}/{pages}", callback_data=NOOP),
            InlineKeyboardButton(
                text="▶️" if current < pages else "·",
                callback_data=(
                    ArtistPickCB(action="page", artist_id=0, page=current + 1).pack()
                    if current < pages
                    else NOOP
                ),
            ),
        )

    done_label = f"✅ Готово ({len(selected)})" if selected else "✅ Готово"
    builder.row(
        InlineKeyboardButton(
            text=done_label,
            callback_data=ArtistPickCB(action="done", artist_id=0, page=current).pack(),
        )
    )

    last_row = [
        InlineKeyboardButton(
            text="✖️ Отмена",
            callback_data=ArtistPickCB(action="cancel", artist_id=0, page=current).pack(),
        )
    ]
    if selected:
        last_row.insert(
            0,
            InlineKeyboardButton(
                text="♻️ Сбросить",
                callback_data=ArtistPickCB(
                    action="clear", artist_id=0, page=current
                ).pack(),
            ),
        )
    builder.row(*last_row)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Подтверждение, автосортировка, поиск в Telegram
# ---------------------------------------------------------------------------


def confirm_kb(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    """Пара кнопок «Да»/«Нет» с готовыми callback-данными."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=yes_data),
                InlineKeyboardButton(text="❌ Нет", callback_data=no_data),
            ]
        ]
    )


def autosort_kb(
    track_id: int,
    folder: dict | None,
    artist_name: str,
) -> InlineKeyboardMarkup:
    """Куда положить только что загруженный трек.

    * если папка исполнителя уже есть — «✅ Сохранить в «X»» с реальным id папки;
    * если папки нет — «🆕 Создать папку «X»» с `folder_id = FOLDER_NEW (-2)`;
      обработчику загрузки достаточно взять имя исполнителя из самого трека
      и создать папку без лишних вопросов;
    * «📂 Другая папка» открывает `folder_pick_kb` (`TrackCB(action="move")`);
    * «🚫 Без папки» — `folder_id = FOLDER_NONE (-1)`.
    """
    builder = InlineKeyboardBuilder()
    track_id = _int(track_id)
    artist = _short(artist_name, LABEL_LIMIT - 6) if artist_name else ""
    folder_name = _short(_get(folder, "name"), LABEL_LIMIT - 6) if folder else ""

    if folder:
        builder.row(
            InlineKeyboardButton(
                text=f"✅ Сохранить в «{folder_name}»",
                callback_data=MoveCB(
                    track_id=track_id, folder_id=_int(_get(folder, "id"))
                ).pack(),
            )
        )

    if artist and (not folder or folder_name.casefold() != artist.casefold()):
        builder.row(
            InlineKeyboardButton(
                text=f"🆕 Создать папку «{artist}»",
                callback_data=MoveCB(track_id=track_id, folder_id=FOLDER_NEW).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📂 Другая папка",
            callback_data=TrackCB(
                action="move", track_id=track_id, page=1, ctx="upload"
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🚫 Без папки",
            callback_data=MoveCB(track_id=track_id, folder_id=FOLDER_NONE).pack(),
        )
    )
    return builder.as_markup()


def tg_results_kb(items: Sequence[Any]) -> InlineKeyboardMarkup:
    """Результаты поиска в Telegram: «⬇️ N» — импорт, «ℹ️ N» — подробности."""
    builder = InlineKeyboardBuilder()

    for index, item in enumerate(items, start=1):
        token = str(_get(item, "token") or "")
        if not token:
            logger.warning("Результат поиска без токена пропущен: %r", item)
            continue
        builder.add(
            InlineKeyboardButton(
                text=f"⬇️ {index}",
                callback_data=TgSearchCB(action="import", token=token).pack(),
            )
        )
    builder.adjust(4)

    builder.row(InlineKeyboardButton(text="⬅️ Меню", callback_data=NavCB(action="menu").pack()))
    return builder.as_markup()


__all__ = [
    "LABEL_LIMIT",
    "NOOP",
    "artist_multiselect_kb",
    "artists_kb",
    "autosort_kb",
    "back_callback",
    "confirm_kb",
    "folder_pick_kb",
    "folders_kb",
    "main_menu_kb",
    "page_callback",
    "pagination_row",
    "playlist_pick_kb",
    "playlist_tracks_kb",
    "playlists_kb",
    "sections_kb",
    "tg_results_kb",
    "track_actions_kb",
    "tracks_page_kb",
    "webapp_available",
    "webapp_button",
    "webapp_url",
]
