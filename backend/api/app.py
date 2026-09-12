"""Сборка FastAPI-приложения MusicBox: жизненный цикл, middleware, ошибки, статика."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from backend import __version__
from backend.api.routers import api_router
from backend.config import settings
from backend.db.database import init_db, shutdown_db
from backend.errors import (
    AuthError,
    FileTooLargeError,
    MusicBoxError,
    NotFoundError,
    StorageError,
    TelegramSearchError,
    TelegramSearchUnavailable,
    ValidationError,
)
from backend.services.storage import close_http_client
from backend.services.telegram_search import telegram_search

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from aiogram import Bot, Dispatcher

logger = logging.getLogger(__name__)

APP_TITLE = "MusicBox API"
APP_DESCRIPTION = (
    "HTTP API музыкальной библиотеки MusicBox: папки, треки, плейлисты, "
    "избранное, исполнители, поиск и статистика прослушиваний."
)

#: Корень проекта — нужен, чтобы найти каталог фронтенда при запуске из другой директории.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Служебные префиксы вне `api_router`: второе монтирование, документация, вебхук.
SERVICE_PATH_PREFIXES: tuple[str, ...] = (
    "api",
    "health",
    "docs",
    "redoc",
    "openapi.json",
    "telegram",
)

#: Известные префиксы роутеров API — страховка на случай, если собрать префиксы
#: из `api_router` не удалось (например, изменилось внутреннее устройство FastAPI).
KNOWN_ROUTER_PREFIXES: tuple[str, ...] = (
    "folders",
    "tracks",
    "favourites",
    "playlists",
    "artists",
    "albums",
    "notes",
    "recommendations",
    "other",
    "search",
    "stats",
    "settings",
)


def _collect_route_prefixes(router: Any, prefixes: set[str]) -> None:
    """Собрать первые сегменты путей роутера (включая вложенные) в `prefixes`.

    Роутеры могут храниться как вложенные объекты включения, поэтому обходим
    их рекурсивно и работаем через `getattr`: набор атрибутов зависит от версии
    FastAPI, и любое расхождение не должно ронять сборку приложения.
    """
    for route in getattr(router, "routes", ()):  # pragma: no branch - плоский обход
        path = str(getattr(route, "path", "") or "")
        if path:
            segment = path.replace("\\", "/").strip("/").split("/", 1)[0].lower()
            if segment and not segment.startswith("{"):
                prefixes.add(segment)
            continue
        nested = getattr(route, "original_router", None) or getattr(route, "app", None)
        if nested is not None and nested is not router:
            _collect_route_prefixes(nested, prefixes)


def _router_path_prefixes() -> tuple[str, ...]:
    """Первые сегменты путей, реально подключённых в `api_router`.

    Список собирается автоматически, чтобы не расходиться с набором роутеров:
    новый раздел API попадает сюда сам, без ручного пополнения констант.
    """
    prefixes: set[str] = set()
    try:
        _collect_route_prefixes(api_router, prefixes)
    except Exception:  # pragma: no cover - защита от смены внутренностей FastAPI
        logger.warning("Не удалось собрать префиксы маршрутов API — берём известный список")
        return ()
    return tuple(sorted(prefixes))


#: Префиксы путей, которые обслуживает API: для них SPA-fallback не работает.
API_PATH_PREFIXES: tuple[str, ...] = SERVICE_PATH_PREFIXES + tuple(
    sorted(set(KNOWN_ROUTER_PREFIXES) | set(_router_path_prefixes()))
)

#: Хвосты путей, которые нельзя сжимать: потоковое аудио и обложки (Range-запросы).
NO_COMPRESS_SUFFIXES: tuple[str, ...] = ("/stream", "/cover")

#: Минимальный размер ответа для сжатия.
GZIP_MINIMUM_SIZE = 1024

#: Заголовок Telegram с секретом вебхука.
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

#: Эти два кода в разных версиях Starlette называются по-разному — задаём числом.
HTTP_413_PAYLOAD_TOO_LARGE = 413
HTTP_422_VALIDATION_ERROR = 422


# ===========================================================================
# Жизненный цикл
# ===========================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Запуск и корректная остановка приложения (БД, поиск по Telegram, бот)."""
    bot: Any = getattr(app.state, "bot", None)
    dispatcher: Any = getattr(app.state, "dispatcher", None)
    polling_task: asyncio.Task[None] | None = None

    await init_db()
    logger.info("База данных готова: %s", settings.database_path)

    await _start_telegram_search()

    if bot is not None:
        await _setup_bot_commands(bot)
        if settings.bot_mode == "polling":
            if dispatcher is not None:
                polling_task = asyncio.create_task(
                    _run_polling(dispatcher, bot), name="musicbox-polling"
                )
                logger.info("Бот работает в режиме polling")
            else:
                logger.warning("Режим polling выбран, но диспетчер не передан — бот не запущен")
        elif dispatcher is not None:
            await _setup_bot_webhook(bot)
        else:
            logger.warning(
                "Режим webhook выбран, но диспетчер не передан — вебхук не установлен "
                "(обновления обрабатывает другой процесс)"
            )
    else:
        logger.info("Приложение запущено без бота: доступен только API")

    app.state.polling_task = polling_task

    try:
        yield
    finally:
        await _shutdown(app, bot, polling_task)


async def _start_telegram_search() -> None:
    """Поднять клиент поиска по Telegram; сбой не должен мешать старту API."""
    try:
        await telegram_search.start()
    except Exception:  # noqa: BLE001 - поиск по Telegram необязателен
        logger.warning("Поиск по Telegram не запущен — функция будет недоступна", exc_info=True)


async def _run_polling(dispatcher: Any, bot: Any) -> None:
    """Фоновая задача опроса Telegram (long polling)."""
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:  # noqa: BLE001 - вебхука могло и не быть
            logger.warning("Не удалось снять вебхук перед запуском polling", exc_info=True)
        await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
    except asyncio.CancelledError:
        logger.info("Опрос Telegram остановлен")
        raise
    except Exception:  # noqa: BLE001 - падение опроса не должно ронять API
        logger.exception("Опрос Telegram завершился с ошибкой")


async def _setup_bot_commands(bot: Any) -> None:
    """Зарегистрировать команды бота (меню в клиенте Telegram)."""
    try:
        from backend.bot.bot import setup_commands
    except ImportError:
        logger.warning("Модуль бота недоступен — список команд не настроен")
        return
    try:
        await setup_commands(bot)
        logger.info("Команды бота зарегистрированы")
    except Exception:  # noqa: BLE001 - неудача не критична для запуска
        logger.warning("Не удалось зарегистрировать команды бота", exc_info=True)


async def _setup_bot_webhook(bot: Any) -> None:
    """Прописать вебхук в Telegram (режим webhook)."""
    if not _webhook_secret():
        logger.error(
            "Вебхук не установлен: не задан WEBHOOK_SECRET. Без секретного токена любой "
            "может прислать поддельное обновление Telegram. Укажите WEBHOOK_SECRET в .env "
            "(например, результат команды: python -c \"import secrets; "
            'print(secrets.token_hex(32))") и перезапустите приложение.'
        )
        return
    try:
        from backend.bot.bot import setup_webhook
    except ImportError:
        logger.warning("Модуль бота недоступен — вебхук не установлен")
        return
    try:
        await setup_webhook(bot)
        logger.info("Вебхук Telegram установлен: %s", settings.webhook_url or settings.webhook_path)
    except Exception:  # noqa: BLE001 - сообщаем, но не мешаем работе API
        logger.exception("Не удалось установить вебхук Telegram")


async def _shutdown(app: FastAPI, bot: Any, polling_task: asyncio.Task[None] | None) -> None:
    """Аккуратно погасить фоновые задачи и соединения."""
    if polling_task is not None and not polling_task.done():
        polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling_task
    app.state.polling_task = None

    try:
        await telegram_search.stop()
    except Exception:  # noqa: BLE001 - остановка не должна ронять завершение
        logger.warning("Не удалось корректно остановить поиск по Telegram", exc_info=True)

    try:
        await close_http_client()
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось закрыть HTTP-клиент хранилища", exc_info=True)

    if bot is not None:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось закрыть сессию бота", exc_info=True)

    try:
        await shutdown_db()
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось корректно закрыть базу данных", exc_info=True)

    logger.info("Приложение остановлено")


# ===========================================================================
# Middleware
# ===========================================================================


class ConditionalGZipMiddleware:
    """GZip для всех ответов, кроме потокового аудио и обложек.

    Сжатие ломает частичные ответы (`206 Partial Content`) и не нужно для
    уже сжатых медиаданных, поэтому пути `/stream` и `/cover` пропускаются.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = GZIP_MINIMUM_SIZE) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and _skip_compression(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


def _skip_compression(path: str) -> bool:
    """Нужно ли обойти сжатие для этого пути."""
    return path.endswith(NO_COMPRESS_SUFFIXES)


async def _log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Записать в журнал метод, путь, статус и длительность запроса."""
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.warning(
            "%s %s — необработанная ошибка за %.1f мс", request.method, path, elapsed_ms
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    logger.log(
        _log_level_for(path, response.status_code),
        "%s %s -> %s за %.1f мс",
        request.method,
        path,
        response.status_code,
        elapsed_ms,
    )
    return response


def _log_level_for(path: str, status_code: int) -> int:
    """Подобрать уровень журналирования, чтобы не засорять лог здоровьем сервиса."""
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    if path in ("/health", "/api/health"):
        return logging.DEBUG
    return logging.INFO


# ===========================================================================
# Обработчики ошибок
# ===========================================================================


def _error_response(status_code: int, message: str) -> JSONResponse:
    """Ответ об ошибке в формате FastAPI: `{"detail": "..."}`."""
    return JSONResponse(status_code=status_code, content={"detail": message})


async def _handle_not_found(request: Request, exc: Exception) -> Response:
    """`NotFoundError` -> 404."""
    return _error_response(status.HTTP_404_NOT_FOUND, str(exc))


async def _handle_validation(request: Request, exc: Exception) -> Response:
    """`ValidationError` -> 400."""
    return _error_response(status.HTTP_400_BAD_REQUEST, str(exc))


async def _handle_auth(request: Request, exc: Exception) -> Response:
    """`AuthError` -> 401."""
    return _error_response(status.HTTP_401_UNAUTHORIZED, str(exc))


async def _handle_file_too_large(request: Request, exc: Exception) -> Response:
    """`FileTooLargeError` -> 413."""
    return _error_response(HTTP_413_PAYLOAD_TOO_LARGE, str(exc))


async def _handle_storage(request: Request, exc: Exception) -> Response:
    """`StorageError` -> 502."""
    logger.warning("Ошибка хранилища на %s: %s", request.url.path, exc)
    return _error_response(status.HTTP_502_BAD_GATEWAY, str(exc))


async def _handle_search_unavailable(request: Request, exc: Exception) -> Response:
    """`TelegramSearchUnavailable` -> 503."""
    return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


async def _handle_search_error(request: Request, exc: Exception) -> Response:
    """`TelegramSearchError` -> 502."""
    logger.warning("Ошибка поиска по Telegram на %s: %s", request.url.path, exc)
    return _error_response(status.HTTP_502_BAD_GATEWAY, str(exc))


async def _handle_musicbox_error(request: Request, exc: Exception) -> Response:
    """Прочие ошибки MusicBox -> 500 (текст уже на русском)."""
    logger.error("Ошибка MusicBox на %s: %s", request.url.path, exc, exc_info=True)
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


async def _handle_request_validation(request: Request, exc: Exception) -> Response:
    """Ошибки разбора тела/параметров запроса -> 422 с понятным русским текстом."""
    details: list[str] = []
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    for error in errors:
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")
        )
        message = str(error.get("msg", "")).removeprefix("Value error, ").strip()
        if location and message:
            details.append(f"«{location}»: {message}")
        elif message:
            details.append(message)
    text = "Некорректные данные запроса"
    if details:
        text = f"{text}: {'; '.join(details)}"
    logger.info("Отклонён запрос %s: %s", request.url.path, text)
    return _error_response(HTTP_422_VALIDATION_ERROR, text)


async def _handle_unexpected(request: Request, exc: Exception) -> Response:
    """Непредвиденная ошибка -> 500 с полным traceback в журнале."""
    logger.exception("Непредвиденная ошибка при обработке %s %s", request.method, request.url.path)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Внутренняя ошибка сервера. Попробуйте позже.",
    )


def _register_exception_handlers(app: FastAPI) -> None:
    """Зарегистрировать все обработчики ошибок приложения."""
    app.add_exception_handler(NotFoundError, _handle_not_found)
    app.add_exception_handler(ValidationError, _handle_validation)
    app.add_exception_handler(AuthError, _handle_auth)
    app.add_exception_handler(FileTooLargeError, _handle_file_too_large)
    app.add_exception_handler(StorageError, _handle_storage)
    app.add_exception_handler(TelegramSearchUnavailable, _handle_search_unavailable)
    app.add_exception_handler(TelegramSearchError, _handle_search_error)
    app.add_exception_handler(MusicBoxError, _handle_musicbox_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(Exception, _handle_unexpected)


# ===========================================================================
# Статика фронтенда
# ===========================================================================


def _is_api_path(path: str) -> bool:
    """Относится ли путь к API (для таких путей SPA-fallback запрещён).

    `StaticFiles` отдаёт путь в виде относительного пути файловой системы,
    поэтому на Windows в нём обратные слеши — приводим к единому виду.
    """
    normalized = path.replace("\\", "/").strip("/").lower()
    if not normalized:
        return False
    for prefix in API_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return True
    return False


class SPAStaticFiles(StaticFiles):
    """Статика Mini App с возвратом `index.html` для неизвестных не-API путей."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND and not _is_api_path(path):
                return await super().get_response("index.html", scope)
            raise


def _resolve_frontend_dir() -> Path | None:
    """Найти каталог собранного фронтенда (относительно CWD или корня проекта)."""
    raw = (settings.frontend_dist or "").strip()
    if not raw:
        return None
    candidates = [Path(raw).expanduser()]
    if not candidates[0].is_absolute():
        candidates.append(PROJECT_ROOT / raw)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _mount_frontend(app: FastAPI) -> None:
    """Смонтировать статику Mini App на `/` (последним маршрутом)."""
    directory = _resolve_frontend_dir()
    if directory is None:
        logger.info(
            "Каталог фронтенда «%s» не найден — раздаётся только API. "
            "Соберите Mini App командой: npm run build",
            settings.frontend_dist,
        )
        return
    if not (directory / "index.html").is_file():
        logger.warning(
            "В каталоге фронтенда %s нет index.html — Mini App работать не будет", directory
        )
    app.mount("/", SPAStaticFiles(directory=directory, html=True), name="frontend")
    logger.info("Статика Mini App подключена из %s", directory)


# ===========================================================================
# Фабрика приложения
# ===========================================================================


def create_app(bot: "Bot | None" = None, dispatcher: "Dispatcher | None" = None) -> FastAPI:
    """Создать приложение FastAPI.

    :param bot: экземпляр бота aiogram (может отсутствовать — тогда работает только API).
    :param dispatcher: диспетчер aiogram (нужен для polling и вебхука).
    """
    app = FastAPI(
        title=APP_TITLE,
        description=APP_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.polling_task = None

    # Порядок важен: последний добавленный middleware — самый внешний.
    app.add_middleware(ConditionalGZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)
    app.middleware("http")(_log_requests)

    origins = settings.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "X-Process-Time-Ms"],
    )

    _register_exception_handlers(app)

    @app.get("/health", tags=["Сервис"], summary="Проверка доступности сервиса")
    async def health() -> dict[str, str]:
        """Простая проверка живости для мониторинга и Docker healthcheck."""
        return {"status": "ok"}

    if settings.bot_mode == "webhook":
        _register_webhook_route(app)

    # API подключается дважды: с префиксом /api (для Mini App) и без него (по ТЗ).
    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, include_in_schema=False)

    # Статика монтируется последней, чтобы не перехватывать маршруты API.
    _mount_frontend(app)

    logger.info(
        "Приложение MusicBox собрано: режим бота %s, бот %s",
        settings.bot_mode,
        "подключён" if bot is not None else "не подключён",
    )
    return app


def _webhook_path() -> str:
    """Нормализованный путь вебхука (всегда начинается со слеша)."""
    path = (settings.webhook_path or "").strip() or "/telegram/webhook"
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _webhook_secret() -> str:
    """Секретный токен вебхука без окружающих пробелов (пустая строка = не задан)."""
    return (settings.webhook_secret or "").strip()


def _register_webhook_route(app: FastAPI) -> None:
    """Зарегистрировать приём обновлений Telegram в режиме webhook."""
    path = _webhook_path()

    @app.post(path, include_in_schema=False)
    async def telegram_webhook(request: Request) -> Response:
        """Принять обновление Telegram и передать его диспетчеру aiogram."""
        expected_secret = _webhook_secret()
        if not expected_secret:
            # Без секрета отличить Telegram от постороннего отправителя невозможно,
            # поэтому маршрут отвечает отказом всем (fail-closed).
            logger.error("Отклонено обновление Telegram: не задан WEBHOOK_SECRET")
            return _error_response(
                status.HTTP_403_FORBIDDEN, "Вебхук не настроен: не задан секретный токен"
            )

        provided = request.headers.get(WEBHOOK_SECRET_HEADER, "")
        if not hmac.compare_digest(provided, expected_secret):
            logger.warning("Отклонено обновление Telegram: неверный секретный токен вебхука")
            return _error_response(status.HTTP_403_FORBIDDEN, "Неверный секретный токен вебхука")

        bot = getattr(request.app.state, "bot", None)
        dispatcher = getattr(request.app.state, "dispatcher", None)
        if bot is None or dispatcher is None:
            logger.error("Получено обновление Telegram, но бот не сконфигурирован")
            return _error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Бот сейчас недоступен. Попробуйте позже."
            )

        try:
            payload = await request.json()
        except ValueError:
            logger.warning("Получено обновление Telegram с некорректным телом запроса")
            return _error_response(
                status.HTTP_400_BAD_REQUEST, "Некорректное тело запроса вебхука"
            )

        try:
            await dispatcher.feed_webhook_update(bot, payload)
        except Exception:  # noqa: BLE001 - Telegram не должен ретраить из-за ошибки хендлера
            logger.exception("Ошибка обработки обновления Telegram")

        return Response(status_code=status.HTTP_200_OK)

    if _webhook_secret():
        logger.info("Вебхук Telegram принимает обновления на %s", path)
    else:
        logger.error(
            "Вебхук %s будет отклонять все запросы: не задан WEBHOOK_SECRET. "
            "Укажите его в файле .env, иначе обновления Telegram принимать небезопасно.",
            path,
        )


__all__ = ["API_PATH_PREFIXES", "SPAStaticFiles", "create_app", "lifespan"]
