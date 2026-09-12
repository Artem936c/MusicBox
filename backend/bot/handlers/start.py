"""Приветствие, главное меню и общая навигация бота MusicBox.

Здесь живут команды `/start`, `/help`, `/app`, обработчик `NavCB`
(menu|stats|folders|playlists|fav|artists|settings|help|noop) и запасной обработчик
текстовых кнопок reply-клавиатуры (обычно подписи кнопок подменяет на команды
`MenuAliasMiddleware`, но бот остаётся рабочим и без него).

Списки разделов и избранного рисуются функциями из
`backend.bot.handlers.stats`, карточка настроек — из
`backend.bot.handlers.settings`, а экран папок — из
`backend.bot.handlers.folders` (корень дерева V2, тот же, что у `/folders`),
чтобы вид совпадал во всём боте.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from backend.bot import keyboards, texts
from backend.bot.callbacks import NavCB, SectionCB, TrackCB
from backend.bot.handlers import folders as folders_screen
from backend.bot.handlers.settings import show_settings
from backend.bot.handlers.stats import (
    render_favourites,
    render_section,
    show_stats_overview,
)
from backend.bot.utils import (
    answer_or_edit,
    paginate,
    plural,
    render_track_list,
    section_context,
)
from backend.config import settings
from backend.db.repositories import artists as artists_repo
from backend.db.repositories import playlists as playlists_repo
from backend.db.repositories import stats as stats_repo
from backend.db.repositories import users as users_repo

logger = logging.getLogger(__name__)

router = Router(name="start")

#: Размер страницы по умолчанию, если в настройках лежит мусор.
DEFAULT_PAGE_SIZE = 10

#: Максимальная длина названия трека в подписи инлайн-кнопки.
MAX_BUTTON_TITLE = 22

#: Заголовок блока «топ» в приветствии (обычный текст: его экранирует утилита).
TOP_TITLE = texts.SECTION_HEADERS.get("top", "🔥 Самые часто прослушиваемые")

MENU_TEXT = (
    "🏠 <b>Главное меню</b>\n\n"
    "Выберите раздел кнопками ниже — или пришлите аудиофайл, "
    "и он сразу попадёт в библиотеку.\n\n"
) + texts.COMMANDS_REMINDER

PLAYLISTS_HEADER = "💿 <b>Плейлисты</b>"
ARTISTS_HEADER = "🎤 <b>Исполнители</b>"
LISTENED_HINT = "✅ — исполнитель прослушан, ⬜ — ещё нет."
PAGE_SUFFIX = "Страница {page}/{total}"

PLAYLIST_FORMS = ("плейлист", "плейлиста", "плейлистов")
ARTIST_FORMS = ("исполнитель", "исполнителя", "исполнителей")

#: Команды из `texts.MENU_ALIASES`, которые обслуживает этот модуль,
#: и соответствующие им действия меню. Остальные подписи (поиск, Mini App)
#: намеренно не перехватываются — их обрабатывают свои модули.
COMMAND_ACTIONS: dict[str, str] = {
    "/stats": "stats",
    "/top": "top",
    "/folders": "folders",
    "/playlists": "playlists",
    "/favourites": "fav",
    "/artists": "artists",
    "/settings": "settings",
    "/help": "help",
}

_NON_TEXT_RE = re.compile(r"[^0-9a-zа-я\s-]+")


def _normalize_label(value: str | None) -> str:
    """Нормализует подпись кнопки: убирает эмодзи и знаки, приводит к нижнему регистру."""
    if not value:
        return ""
    cleaned = _NON_TEXT_RE.sub(" ", value.casefold().replace("ё", "е"))
    return " ".join(cleaned.split())


def _build_menu_labels() -> dict[str, str]:
    """Словарь «нормализованная подпись кнопки» → «действие меню»."""
    labels: dict[str, str] = {}
    for label, command in texts.MENU_ALIASES.items():
        action = COMMAND_ACTIONS.get(command)
        if action is None:
            continue
        normalized = _normalize_label(label)
        if normalized:
            labels[normalized] = action
    return labels


#: Подписи кнопок нижней клавиатуры, которые понимает этот модуль.
MENU_LABELS: dict[str, str] = _build_menu_labels()


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _per_page() -> int:
    """Размер страницы из настроек с защитой от некорректных значений."""
    try:
        value = int(settings.page_size)
    except (TypeError, ValueError):
        logger.warning("Некорректный page_size в настройках, использую %s", DEFAULT_PAGE_SIZE)
        return DEFAULT_PAGE_SIZE
    return value if value > 0 else DEFAULT_PAGE_SIZE


def _top_limit() -> int:
    """Сколько треков показывать в блоке «Самые часто прослушиваемые»."""
    try:
        value = int(settings.top_limit)
    except (TypeError, ValueError):
        logger.warning("Некорректный top_limit в настройках, использую 10")
        return 10
    return value if value > 0 else 10


def _int_or_zero(value: Any) -> int:
    """Мягкое приведение к int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _user_id(event: Message | CallbackQuery) -> int | None:
    """Идентификатор пользователя из события."""
    user = event.from_user
    return user.id if user is not None else None


async def _library_is_empty(user_id: int) -> bool:
    """Пуста ли библиотека целиком (а не только раздел «топ» без прослушиваний)."""
    try:
        counts = await stats_repo.counts(user_id)
    except Exception:
        logger.exception("Не удалось получить счётчики библиотеки пользователя %s", user_id)
        # При ошибке не утверждаем, что библиотека пуста.
        return False
    return _int_or_zero(counts.get("total")) <= 0


def _page_of(items: list[dict], page: Any) -> tuple[list[dict], int, int]:
    """Срез страницы, фактический номер страницы и общее число страниц."""
    current = _int_or_zero(page) or 1
    chunk, total_pages = paginate(items, current, _per_page())
    return chunk, min(max(current, 1), total_pages), total_pages


def _list_header(
    header: str,
    total: int,
    forms: tuple[str, str, str],
    page: int,
    pages: int,
) -> str:
    """Шапка списка: заголовок, количество и номер страницы."""
    lines = [header, f"Всего: <b>{plural(total, forms)}</b>"]
    if pages > 1:
        lines.append(f"<i>{PAGE_SUFFIX.format(page=page, total=pages)}</i>")
    return "\n".join(lines)


def _short_title(track: dict) -> str:
    """Короткое название трека для подписи кнопки (без HTML)."""
    title = str(track.get("title") or "Без названия").strip() or "Без названия"
    if len(title) <= MAX_BUTTON_TITLE:
        return title
    return title[: MAX_BUTTON_TITLE - 1].rstrip() + "…"


def _top_keyboard(tracks: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура приветствия: «Прослушать»/«Подробнее» + переходы в разделы."""
    ctx = section_context("top")
    rows: list[list[InlineKeyboardButton]] = []

    for index, track in enumerate(tracks, start=1):
        track_id = _int_or_zero(track.get("id"))
        if track_id <= 0:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶️ {index}. {_short_title(track)}",
                    callback_data=TrackCB(
                        action="play", track_id=track_id, page=1, ctx=ctx
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="ℹ️ Подробнее",
                    callback_data=TrackCB(
                        action="info", track_id=track_id, page=1, ctx=ctx
                    ).pack(),
                ),
            ]
        )

    for key in ("recent", "unplayed"):
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.SECTION_HEADERS.get(key, key),
                    callback_data=SectionCB(action="open", key=key, page=1).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📈 Часто",
                callback_data=SectionCB(action="open", key="frequent", page=1).pack(),
            ),
            InlineKeyboardButton(
                text="📉 Редко",
                callback_data=SectionCB(action="open", key="rare", page=1).pack(),
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📊 Вся статистика",
                callback_data=NavCB(action="stats").pack(),
            )
        ]
    )

    button = keyboards.webapp_button()
    if button is not None:
        rows.append([button])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Показ разделов
# --------------------------------------------------------------------------- #


async def _show_main_menu(event: Message | CallbackQuery) -> None:
    """Главное меню: на команду — нижняя клавиатура, на callback — инлайн-разделы."""
    if isinstance(event, CallbackQuery):
        await answer_or_edit(event, MENU_TEXT, keyboards.sections_kb())
        return
    await event.answer(MENU_TEXT, reply_markup=keyboards.main_menu_kb())


async def _show_help(event: Message | CallbackQuery) -> None:
    """Справка по возможностям бота."""
    await answer_or_edit(event, texts.HELP, keyboards.sections_kb())


async def _show_app(event: Message | CallbackQuery) -> None:
    """Кнопка запуска Mini App."""
    button = keyboards.webapp_button()
    if button is None:
        logger.info("WEBAPP_URL не задан — показываю подсказку вместо кнопки Mini App")
        await answer_or_edit(event, texts.APP_NOT_CONFIGURED, keyboards.sections_kb())
        return
    await answer_or_edit(event, texts.APP_HINT, InlineKeyboardMarkup(inline_keyboard=[[button]]))


async def _show_folders(event: Message | CallbackQuery, page: int = 1) -> None:
    """Корень дерева папок — ровно тот же экран, что открывает `/folders`.

    Раньше здесь рисовался плоский список V1, а его пагинация уходила в
    V2-обработчик `FolderCB` и молча подменяла экран. Теперь обе точки входа
    («📁 Папки» в `sections_kb` и «⬅️ Назад» из списка треков папки) ведут
    в дерево, где подпапки видны с отступами и пагинация согласована.
    """
    user_id = _user_id(event)
    if user_id is None:
        return

    rendered = await folders_screen._render_level(user_id, 0, page)
    if rendered is None:  # pragma: no cover - корень дерева существует всегда
        await answer_or_edit(event, texts.EMPTY_FOLDERS, keyboards.sections_kb())
        return

    text, markup = rendered
    await answer_or_edit(event, folders_screen._trim_message(text), markup)


async def _show_playlists(event: Message | CallbackQuery, page: int = 1) -> None:
    """Список плейлистов пользователя."""
    user_id = _user_id(event)
    if user_id is None:
        return

    playlists = await playlists_repo.list_playlists(user_id)
    if not playlists:
        await answer_or_edit(event, texts.EMPTY_PLAYLISTS, keyboards.playlists_kb([], 1, 1))
        return

    items, current_page, total_pages = _page_of(playlists, page)
    text = _list_header(
        PLAYLISTS_HEADER, len(playlists), PLAYLIST_FORMS, current_page, total_pages
    )
    await answer_or_edit(event, text, keyboards.playlists_kb(items, current_page, total_pages))


async def _show_artists(event: Message | CallbackQuery, page: int = 1) -> None:
    """Список исполнителей с отметкой «прослушано»."""
    user_id = _user_id(event)
    if user_id is None:
        return

    artists = await artists_repo.list_artists(user_id)
    if not artists:
        await answer_or_edit(event, texts.EMPTY_ARTISTS, keyboards.sections_kb())
        return

    items, current_page, total_pages = _page_of(artists, page)
    text = _list_header(ARTISTS_HEADER, len(artists), ARTIST_FORMS, current_page, total_pages)
    text = f"{text}\n\n{LISTENED_HINT}"
    await answer_or_edit(event, text, keyboards.artists_kb(items, current_page, total_pages))


async def _dispatch_menu_action(event: Message | CallbackQuery, action: str) -> None:
    """Единая точка обработки действий меню (`NavCB` и текстовые кнопки)."""
    if action == "menu":
        await _show_main_menu(event)
    elif action == "stats":
        await show_stats_overview(event)
    elif action == "top":
        await render_section(event, "top", 1)
    elif action == "folders":
        await _show_folders(event)
    elif action == "playlists":
        await _show_playlists(event)
    elif action == "fav":
        await render_favourites(event, 1)
    elif action == "artists":
        await _show_artists(event)
    elif action == "settings":
        await show_settings(event)
    elif action == "help":
        await _show_help(event)
    elif action == "app":
        await _show_app(event)
    else:
        logger.warning("Неизвестное действие меню: %r", action)
        await _show_main_menu(event)


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """`/start` — приветствие, топ прослушиваний и главное меню."""
    await state.clear()

    user = message.from_user
    if user is None:
        logger.warning("Команда /start без данных пользователя — пропускаю")
        return

    try:
        await users_repo.ensure_user(
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )
    except Exception:
        logger.exception("Не удалось сохранить пользователя %s", user.id)

    await message.answer(texts.WELCOME, reply_markup=keyboards.main_menu_kb())

    tracks = await stats_repo.top(user.id, limit=_top_limit())
    if not tracks:
        # `top` берёт только треки с прослушиваниями, поэтому пустой список ещё не
        # означает пустую библиотеку: треки могут ждать в 💤 «Ни разу не проигранные».
        hint = (
            texts.EMPTY_LIBRARY
            if await _library_is_empty(user.id)
            else texts.SECTION_EMPTY_HINTS["top"]
        )
        await message.answer(
            f"{hint}\n\n{texts.COMMANDS_REMINDER}",
            reply_markup=keyboards.sections_kb(),
        )
        return

    top_text = render_track_list(TOP_TITLE, tracks, 1, 1, texts.SECTION_EMPTY_HINTS["top"])
    await message.answer(
        f"{top_text}\n\n{texts.COMMANDS_REMINDER}",
        reply_markup=_top_keyboard(tracks),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """`/help` — подробная справка."""
    await _show_help(message)


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    """`/app` — кнопка открытия Mini App."""
    await _show_app(message)


# --------------------------------------------------------------------------- #
# Навигация
# --------------------------------------------------------------------------- #


@router.callback_query(NavCB.filter())
async def cb_nav(callback: CallbackQuery, callback_data: NavCB) -> None:
    """`NavCB` — переходы между разделами по инлайн-кнопкам."""
    action = (callback_data.action or "").strip().casefold()
    if action == "noop":
        # Служебная кнопка-заглушка (например, «1/3» в пагинации).
        await callback.answer()
        return
    await _dispatch_menu_action(callback, action)


async def _menu_button_filter(message: Message) -> dict[str, str] | bool:
    """Пропускает только подписи кнопок главного меню, которые ведёт этот модуль."""
    action = MENU_LABELS.get(_normalize_label(message.text))
    if action is None:
        return False
    return {"menu_action": action}


@router.message(StateFilter(None), _menu_button_filter)
async def on_menu_button(message: Message, menu_action: str) -> None:
    """Запасная обработка текстовых кнопок нижней клавиатуры."""
    await _dispatch_menu_action(message, menu_action)


__all__ = ["router"]
