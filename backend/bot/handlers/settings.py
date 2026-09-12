"""Настройки пользователя прямо в боте: команда ``/settings`` и переключатели.

Карточка показывает всё, что хранится в `user_settings`: автосортировку, порог
раздела 📈 «Часто прослушиваемые», диапазон раздела 📉 «Редко прослушиваемые»
и точность нечёткого поиска — с пояснением, на что влияет каждый параметр.
Значения меняются в один тап готовыми кнопками, без FSM и ручного ввода чисел.

Настройки общие с Mini App (`/app`): и там, и здесь читается и пишется
`backend.db.repositories.users`, поэтому изменения видны сразу в обоих местах.

Фабрика callback-данных `SettingsCB` объявлена ЛОКАЛЬНО: её использует только
этот модуль, а `backend.bot.callbacks` собирает фабрики, общие для нескольких
разделов. Данные кнопок короткие («set:freq:10»), лимит Telegram в 64 байта
не нарушается.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backend.bot import keyboards, texts, utils
from backend.bot.callbacks import NavCB
from backend.db.repositories import users as users_repo
from backend.errors import MusicBoxError, ValidationError

logger = logging.getLogger(__name__)

router = Router(name="settings")

#: Готовые значения порога раздела 📈 «Часто прослушиваемые».
FREQUENT_CHOICES: tuple[int, ...] = (5, 10, 20)
#: Нижняя граница раздела 📉 «Редко прослушиваемые» (один раз всё-таки послушали).
RARE_MIN = 1
#: Готовые верхние границы раздела 📉 «Редко прослушиваемые».
RARE_CHOICES: tuple[int, ...] = (3, 5, 9)
#: Готовые пороги нечёткого поиска: «прощать много опечаток» → «искать строго».
FUZZY_CHOICES: tuple[int, ...] = (50, 60, 75)

#: Значения по умолчанию на случай, если в строке настроек оказался мусор.
DEFAULTS: dict[str, int] = {
    "frequent_threshold": 10,
    "rare_min": RARE_MIN,
    "rare_max": 5,
    "fuzzy_threshold": 60,
}

# --- Пользовательские тексты (RU) ----------------------------------------------------
# Общие формулировки берём из backend.bot.texts, локально — только подписи кнопок.

FREQUENT_LABEL = "📈 Порог «часто прослушиваемых»"
RARE_LABEL = "📉 Диапазон «редко прослушиваемых»"
FUZZY_LABEL = "🔎 Точность поиска"
AUTOSORT_TURN_OFF = "🗂 Выключить автосортировку"
AUTOSORT_TURN_ON = "🗂 Включить автосортировку"
BACK_LABEL = "⬅️ Назад"
UNKNOWN_ACTION = "🤔 Не понял кнопку — вот настройки заново."


class SettingsCB(CallbackData, prefix="set"):
    """Кнопки карточки настроек.

    action: ``show`` (перерисовать карточку) | ``toggle`` (автосортировка)
    | ``freq`` (порог «часто») | ``rare`` (верхняя граница «редко»)
    | ``fuzzy`` (порог нечёткого поиска).
    value: новое значение параметра; для ``show`` и ``toggle`` не используется (0).
    """

    action: str
    value: int


# --- Вспомогательные функции ---------------------------------------------------------


def _int_field(data: dict, key: str) -> int:
    """Числовое поле настроек с падением на разумное значение по умолчанию."""
    try:
        return int(data.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Некорректное значение настройки %r, использую значение по умолчанию", key)
        return DEFAULTS[key]


def _choice_button(label: str, active: bool, callback_data: str) -> InlineKeyboardButton:
    """Кнопка выбора значения; текущее значение помечается точкой (как в разделах)."""
    return InlineKeyboardButton(
        text=f"• {label}" if active else label,
        callback_data=callback_data,
    )


def _label_row(text: str) -> InlineKeyboardButton:
    """Строка-подпись над рядом кнопок (нажатие ничего не делает)."""
    return InlineKeyboardButton(text=text, callback_data=keyboards.NOOP)


def _settings_text(data: dict) -> str:
    """Текст карточки настроек: состояние параметров и пояснения к ним."""
    autosort = data.get("auto_sort_enabled")
    body = texts.SETTINGS_BODY.format(
        autosort=texts.SETTINGS_STATE_ON if autosort else texts.SETTINGS_STATE_OFF,
        frequent=_int_field(data, "frequent_threshold"),
        rare_min=_int_field(data, "rare_min"),
        rare_max=_int_field(data, "rare_max"),
        fuzzy=f"{_int_field(data, 'fuzzy_threshold')} из 100",
    )
    return f"{texts.SETTINGS_HEADER}\n\n{body}\n\n{texts.SETTINGS_HINT}"


def _settings_kb(data: dict) -> InlineKeyboardMarkup:
    """Клавиатура карточки: переключатель автосортировки и готовые значения."""
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=AUTOSORT_TURN_OFF if data.get("auto_sort_enabled") else AUTOSORT_TURN_ON,
            callback_data=SettingsCB(action="toggle", value=0).pack(),
        )
    )

    frequent = _int_field(data, "frequent_threshold")
    builder.row(_label_row(FREQUENT_LABEL))
    builder.row(
        *(
            _choice_button(
                f"от {value}",
                value == frequent,
                SettingsCB(action="freq", value=value).pack(),
            )
            for value in FREQUENT_CHOICES
        )
    )

    rare_min = _int_field(data, "rare_min")
    rare_max = _int_field(data, "rare_max")
    builder.row(_label_row(RARE_LABEL))
    builder.row(
        *(
            _choice_button(
                f"{RARE_MIN}–{value}",
                rare_min == RARE_MIN and rare_max == value,
                SettingsCB(action="rare", value=value).pack(),
            )
            for value in RARE_CHOICES
        )
    )

    fuzzy = _int_field(data, "fuzzy_threshold")
    builder.row(_label_row(FUZZY_LABEL))
    builder.row(
        *(
            _choice_button(
                str(value),
                value == fuzzy,
                SettingsCB(action="fuzzy", value=value).pack(),
            )
            for value in FUZZY_CHOICES
        )
    )

    builder.row(
        InlineKeyboardButton(text=BACK_LABEL, callback_data=NavCB(action="menu").pack())
    )
    return builder.as_markup()


def _settings_view(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Готовая пара «текст + клавиатура» карточки настроек."""
    return _settings_text(data), _settings_kb(data)


async def show_settings(event: Message | CallbackQuery) -> None:
    """Показывает карточку настроек: на команду — новым сообщением, на кнопку — правкой.

    Используется командой `/settings` и запасной обработкой кнопки нижней
    клавиатуры «⚙️ Настройки» в `backend.bot.handlers.start`.
    """
    user = event.from_user
    if user is None:
        logger.warning("Настройки запрошены без данных пользователя — пропускаю")
        return
    data = await users_repo.get_settings(user.id)
    text, markup = _settings_view(data)
    await utils.answer_or_edit(event, text, markup)


# --- Команда /settings ---------------------------------------------------------------


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    """`/settings` — карточка настроек с кнопками изменения значений."""
    if message.from_user is None:
        return
    await state.set_state(None)
    await show_settings(message)


# --- Кнопки карточки настроек --------------------------------------------------------


@router.callback_query(SettingsCB.filter())
async def cb_settings(callback: CallbackQuery, callback_data: SettingsCB) -> None:
    """Меняет параметр и перерисовывает карточку настроек на месте."""
    if callback.from_user is None:
        await callback.answer()
        return

    user_id = callback.from_user.id
    action = (callback_data.action or "").strip().casefold()
    value = int(callback_data.value)
    notice: str | None = None

    try:
        if action == "toggle":
            data = await users_repo.toggle_auto_sort(user_id)
            notice = texts.AUTOSORT_ON if data.get("auto_sort_enabled") else texts.AUTOSORT_OFF
        elif action == "freq":
            data = await users_repo.update_settings(user_id, frequent_threshold=value)
            notice = texts.SETTINGS_SAVED
        elif action == "rare":
            data = await users_repo.update_settings(user_id, rare_min=RARE_MIN, rare_max=value)
            notice = texts.SETTINGS_SAVED
        elif action == "fuzzy":
            data = await users_repo.update_settings(user_id, fuzzy_threshold=value)
            notice = texts.SETTINGS_SAVED
        elif action == "show":
            data = await users_repo.get_settings(user_id)
        else:
            logger.warning("Неизвестное действие настроек: %r", callback_data.action)
            data = await users_repo.get_settings(user_id)
            notice = UNKNOWN_ACTION
    except ValidationError as error:
        logger.warning("Настройки пользователя %s не сохранены: %s", user_id, error)
        await callback.answer(error.message, show_alert=True)
        return
    except MusicBoxError:
        logger.exception("Ошибка при изменении настроек пользователя %s", user_id)
        await callback.answer(texts.ERROR_TRY_AGAIN, show_alert=True)
        return

    if action in ("toggle", "freq", "rare", "fuzzy"):
        logger.info("Пользователь %s изменил настройки: %s=%s", user_id, action, value)

    message = callback.message
    if isinstance(message, Message):
        text, markup = _settings_view(data)
        await utils.safe_edit(message, text, markup)
    await callback.answer(notice)


__all__ = ["SettingsCB", "router", "show_settings"]
