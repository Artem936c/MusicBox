"""Регрессионные тесты сборки бота: роутеры, команда ``/settings``, удаление трека.

Тесты работают ПОЛНОСТЬЮ офлайн: вместо сетевой сессии aiogram подставляется
:class:`FakeSession`, которая просто складывает вызванные методы Bot API в список
и возвращает правдоподобные ответы. Реальных запросов в Telegram не делается.

Диспетчер создаётся ОДИН раз на всю сессию тестов: роутеры обработчиков —
модульные синглтоны, и повторный `create_dispatcher()` в том же процессе падает
с «Router is already attached». Состояния между тестами диспетчер не хранит
(FSM пуст, троттлинг отключён фикстурой), поэтому общий объект безопасен.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterator

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.filters import Command
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

from backend.bot import texts
from backend.bot.bot import create_dispatcher
from backend.bot.callbacks import TrackCB
from backend.bot.handlers import HANDLER_MODULES
from backend.bot.handlers.stats import TRACK_DELETED
from backend.bot.middlewares import ThrottlingMiddleware
from backend.db.repositories import tracks as tracks_repo
from tests.conftest import TrackFactory

logger = logging.getLogger(__name__)

#: Токен «бота» тестов — в сеть с ним никто не ходит (сессия подменена).
FAKE_BOT_TOKEN = "123:test"


# ---------------------------------------------------------------------------
# Офлайн-сессия Bot API
# ---------------------------------------------------------------------------


class FakeSession(BaseSession):
    """Сессия-заглушка: запоминает вызовы Bot API и отвечает без сети."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        """Закрывать нечего — сокетов нет."""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        """Записывает вызов и возвращает подходящий по типу ответ."""
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = getattr(method, "chat_id", 0) or 0
            return Message(
                message_id=555,
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=int(chat_id), type="private"),
                text=getattr(method, "text", "") or "",
            )
        return True

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        """Скачивать в тестах нечего."""
        yield b""

    # --- Помощники проверок ---------------------------------------------------

    def texts_of(self, method_type: type[TelegramMethod[Any]]) -> list[str]:
        """Тексты всех вызовов указанного метода Bot API."""
        return [
            str(getattr(call, "text", "") or "")
            for call in self.calls
            if isinstance(call, method_type)
        ]

    def count_of(self, method_type: type[TelegramMethod[Any]]) -> int:
        """Сколько раз вызывался метод Bot API."""
        return sum(1 for call in self.calls if isinstance(call, method_type))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dispatcher() -> Dispatcher:
    """Единственный на сессию диспетчер бота с отключённым троттлингом.

    Троттлинг (0.4 с между событиями) снимается, иначе второе обновление в
    одном тесте молча отбрасывалось бы и проверка стала бы ложноположительной.
    """
    dp = create_dispatcher()
    for middleware in dp.message.outer_middleware:
        if isinstance(middleware, ThrottlingMiddleware):
            middleware.rate = 0.0
    for middleware in dp.callback_query.outer_middleware:
        if isinstance(middleware, ThrottlingMiddleware):
            middleware.rate = 0.0
    return dp


@pytest.fixture
def session() -> FakeSession:
    """Свежий журнал вызовов Bot API на каждый тест."""
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    """Бот поверх офлайн-сессии."""
    return Bot(token=FAKE_BOT_TOKEN, session=session)


@pytest.fixture
def tg_user(user_id: int) -> User:
    """Telegram-пользователь тестов (тот же id, что и в фикстуре `user`)."""
    return User(id=user_id, is_bot=False, first_name="Тест", language_code="ru")


@pytest.fixture
def tg_chat(user_id: int) -> Chat:
    """Личный чат с пользователем тестов."""
    return Chat(id=user_id, type="private")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _routers(dp: Dispatcher) -> Iterator[Router]:
    """Диспетчер и все вложенные роутеры (обход в глубину)."""
    yield dp
    stack = list(dp.sub_routers)
    while stack:
        router = stack.pop()
        yield router
        stack.extend(router.sub_routers)


def _command_owners(dp: Dispatcher, command: str) -> list[tuple[str, str]]:
    """Пары «имя роутера, имя функции» для всех обработчиков команды."""
    found: list[tuple[str, str]] = []
    for router in _routers(dp):
        for handler in router.message.handlers:
            for handler_filter in handler.filters or ():
                callback = handler_filter.callback
                if isinstance(callback, Command) and command in callback.commands:
                    found.append((router.name, handler.callback.__name__))
    return found


def _track_callback_handlers(dp: Dispatcher, action: str, ctx: str = "top") -> list[str]:
    """Имена обработчиков `TrackCB`, чей фильтр пропускает такое нажатие.

    Разбор идёт по тем же полям, что использует aiogram при маршрутизации:
    `CallbackQueryFilter.callback_data` — фабрика, `CallbackQueryFilter.rule` —
    магический фильтр (у `TrackCB.filter()` без условий его нет).
    """
    data = TrackCB(action=action, track_id=1, page=1, ctx=ctx)
    found: list[str] = []
    for router in _routers(dp):
        for handler in router.callback_query.handlers:
            for handler_filter in handler.filters or ():
                callback = handler_filter.callback
                if getattr(callback, "callback_data", None) is not TrackCB:
                    continue
                rule = getattr(callback, "rule", None)
                if rule is None or rule.resolve(data):
                    found.append(handler.callback.__name__)
    return found


def _message_update(chat: Chat, from_user: User, text: str) -> Update:
    """Обновление с текстовым сообщением от пользователя."""
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=dt.datetime.now(dt.timezone.utc),
            chat=chat,
            from_user=from_user,
            text=text,
        ),
    )


def _callback_update(chat: Chat, from_user: User, data: str) -> Update:
    """Обновление с нажатием инлайн-кнопки под сообщением бота."""
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=from_user,
            chat_instance="chat-instance",
            data=data,
            message=Message(
                message_id=42,
                date=dt.datetime.now(dt.timezone.utc),
                chat=chat,
                text="Карточка трека",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# /settings: команда, роутер, сквозной прогон
# ---------------------------------------------------------------------------


def test_settings_module_registered() -> None:
    """Модуль обработчиков `settings` подключается вместе с остальными."""
    assert "settings" in HANDLER_MODULES
    # Порядок важен: карточка настроек сбрасывает FSM, поэтому команда должна
    # обрабатываться раньше диалогов ввода (folders/playlists/search/upload).
    for later in ("folders", "playlists", "search", "tg_search", "upload"):
        assert HANDLER_MODULES.index("settings") < HANDLER_MODULES.index(later)


def test_settings_command_in_menu() -> None:
    """Команда `/settings` попадает в меню команд Telegram."""
    names = [name for name, _ in texts.COMMANDS]
    assert "settings" in names
    description = dict(texts.COMMANDS)["settings"]
    assert description.strip()


def test_dispatcher_registers_settings_command(dispatcher: Dispatcher) -> None:
    """Диспетчер собирается, и обработчик `/settings` в нём ровно один."""
    owners = _command_owners(dispatcher, "settings")

    assert owners == [("settings", "cmd_settings")]


def test_every_menu_command_has_handler(dispatcher: Dispatcher) -> None:
    """У каждой команды из меню Telegram есть обработчик в диспетчере."""
    missing = [name for name, _ in texts.COMMANDS if not _command_owners(dispatcher, name)]

    assert missing == []


async def test_settings_command_answers(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    tg_chat: Chat,
    tg_user: User,
    user: dict,
) -> None:
    """`/settings` доходит до обработчика и присылает карточку настроек."""
    await dispatcher.feed_update(bot, _message_update(tg_chat, tg_user, "/settings"))

    answers = session.texts_of(SendMessage)
    assert len(answers) == 1
    assert texts.SETTINGS_HEADER in answers[0]
    assert texts.SETTINGS_HINT in answers[0]


async def test_settings_menu_button_answers(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    tg_chat: Chat,
    tg_user: User,
    user: dict,
) -> None:
    """Кнопка нижней клавиатуры «⚙️ Настройки» работает как команда."""
    label = next(text for text, command in texts.MENU_ALIASES.items() if command == "/settings")

    await dispatcher.feed_update(bot, _message_update(tg_chat, tg_user, label))

    answers = session.texts_of(SendMessage)
    assert len(answers) == 1
    assert texts.SETTINGS_HEADER in answers[0]


# ---------------------------------------------------------------------------
# Удаление трека: TrackCB(action="delete")
# ---------------------------------------------------------------------------


def test_delete_handler_registered(dispatcher: Dispatcher) -> None:
    """Кнопка «🗑 Удалить» обслуживается ровно одним обработчиком."""
    assert _track_callback_handlers(dispatcher, "delete") == ["cb_track_delete"]


async def test_track_delete_asks_confirmation(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    tg_chat: Chat,
    tg_user: User,
    user_id: int,
    track_factory: TrackFactory,
) -> None:
    """Первое нажатие «🗑 Удалить» только спрашивает подтверждение."""
    track = await track_factory("Спокойная ночь", artist="Кино")
    data = TrackCB(action="delete", track_id=int(track["id"]), page=1, ctx="top").pack()

    await dispatcher.feed_update(bot, _callback_update(tg_chat, tg_user, data))

    edits = session.texts_of(EditMessageText)
    assert len(edits) == 1
    assert "Спокойная ночь" in edits[0]
    assert session.count_of(AnswerCallbackQuery) == 1
    # Трек на месте: подтверждения ещё не было.
    assert await tracks_repo.get_track(user_id, int(track["id"])) is not None


async def test_track_delete_confirmed_removes_track(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    tg_chat: Chat,
    tg_user: User,
    user_id: int,
    track_factory: TrackFactory,
) -> None:
    """Подтверждённое удаление убирает трек из библиотеки и из канала."""
    track = await track_factory("Кукушка", artist="Кино")
    track_id = int(track["id"])
    data = TrackCB(action="delete", track_id=track_id, page=-1, ctx="top").pack()

    await dispatcher.feed_update(bot, _callback_update(tg_chat, tg_user, data))

    assert await tracks_repo.get_track(user_id, track_id) is None
    # Сообщение с файлом удалено из канала-хранилища.
    assert session.count_of(DeleteMessage) == 1
    # Пользователь получил уведомление, а список перерисован после удаления.
    assert session.count_of(AnswerCallbackQuery) >= 1
    assert TRACK_DELETED in [
        str(getattr(call, "text", "") or "")
        for call in session.calls
        if isinstance(call, AnswerCallbackQuery)
    ]
