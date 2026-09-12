"""Точка входа MusicBox.

Запуск: ``python -m backend.main`` — поднимает FastAPI (Mini App + API) и бота
в одном процессе. Дополнительные режимы:

* ``--api-only`` — только HTTP-сервер, без опроса Telegram (бот создаётся,
  чтобы работала отдача аудио, но обновления не обрабатываются);
* ``--bot-only`` — только бот (long polling), без HTTP-сервера.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

import uvicorn
from aiogram import Bot, Dispatcher

from backend.api.app import create_app
from backend.bot.bot import create_bot, create_dispatcher, setup_commands
from backend.config import settings
from backend.db.database import init_db, shutdown_db
from backend.logging_config import setup_logging
from backend.services.storage import close_http_client
from backend.services.telegram_search import telegram_search

logger = logging.getLogger(__name__)

#: Код возврата при неверных настройках (.env заполнен не полностью).
EXIT_CONFIG_ERROR = 1
#: Код возврата при неожиданной ошибке запуска.
EXIT_RUNTIME_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.main",
        description=(
            "MusicBox — Telegram-бот и Mini App, использующие приватный канал "
            "как облачное хранилище аудиофайлов."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--api-only",
        action="store_true",
        help=(
            "Запустить только веб-сервер (API и Mini App) без обработки "
            "сообщений бота. Полезно, если бот запущен отдельным процессом."
        ),
    )
    mode.add_argument(
        "--bot-only",
        action="store_true",
        help="Запустить только бота (long polling) без веб-сервера.",
    )
    parser.add_argument(
        "--host",
        default=None,
        metavar="АДРЕС",
        help=f"Адрес, на котором слушает веб-сервер (по умолчанию {settings.api_host}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="ПОРТ",
        help=f"Порт веб-сервера (по умолчанию {settings.api_port}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Разобрать аргументы, проверить настройки и запустить выбранный режим."""
    args = build_parser().parse_args(argv)

    setup_logging()

    try:
        settings.validate_runtime()
    except ValueError as exc:
        logger.error("Приложение не запущено — некорректные настройки.\n%s", exc)
        logger.error(
            "Скопируйте .env.example в .env и заполните обязательные значения, "
            "затем повторите запуск."
        )
        sys.exit(EXIT_CONFIG_ERROR)

    host = args.host or settings.api_host
    port = args.port if args.port is not None else settings.api_port

    try:
        if args.bot_only:
            run_bot_only()
        else:
            run_server(host=host, port=port, with_polling=not args.api_only)
    except KeyboardInterrupt:  # pragma: no cover - зависит от сигнала пользователя
        logger.info("Остановлено пользователем.")
    except Exception:  # noqa: BLE001 - логируем любую ошибку запуска и выходим с кодом
        logger.exception("Критическая ошибка при работе MusicBox.")
        sys.exit(EXIT_RUNTIME_ERROR)


def run_server(*, host: str, port: int, with_polling: bool) -> None:
    """Запустить FastAPI (Mini App + API); при `with_polling` бот работает в том же процессе."""
    bot = create_bot()
    dispatcher: Dispatcher | None = create_dispatcher() if with_polling else None

    if with_polling:
        logger.info(
            "Режим: веб-сервер + бот (%s). Слушаем http://%s:%s",
            settings.bot_mode,
            host,
            port,
        )
    else:
        logger.info(
            "Режим: только веб-сервер (--api-only), обновления Telegram не обрабатываются. "
            "Слушаем http://%s:%s",
            host,
            port,
        )

    app = create_app(bot, dispatcher)
    uvicorn.run(app, host=host, port=port, log_config=None)


def run_bot_only() -> None:
    """Запустить только бота: инициализация БД и long polling без веб-сервера."""
    logger.info("Режим: только бот (--bot-only), long polling без веб-сервера.")
    bot = create_bot()
    dispatcher = create_dispatcher()
    asyncio.run(_run_polling(bot, dispatcher))


async def _run_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    """Подготовить окружение и крутить long polling до остановки процесса."""
    await init_db()
    await _start_telegram_search()
    await _prepare_polling(bot)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await _shutdown(bot)


async def _prepare_polling(bot: Bot) -> None:
    """Снять вебхук (иначе Telegram не отдаёт обновления) и обновить меню команд."""
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:  # noqa: BLE001 - отсутствие вебхука не мешает работе
        logger.warning("Не удалось снять вебхук перед запуском опроса.", exc_info=True)
    try:
        await setup_commands(bot)
    except Exception:  # noqa: BLE001 - меню команд не критично для работы бота
        logger.warning("Не удалось обновить список команд бота.", exc_info=True)


async def _start_telegram_search() -> None:
    """Поднять клиент поиска по Telegram, если функция включена в настройках."""
    if not settings.telegram_search_enabled:
        return
    try:
        await telegram_search.start()
    except Exception:  # noqa: BLE001 - без поиска по Telegram бот работает штатно
        logger.warning(
            "Поиск по Telegram недоступен: клиент не запущен. "
            "Пользователи смогут пересылать аудио боту вручную.",
            exc_info=True,
        )


async def _shutdown(bot: Bot) -> None:
    """Аккуратно освободить ресурсы: клиент поиска, HTTP-клиент, сессия бота, БД."""
    try:
        await telegram_search.stop()
    except Exception:  # noqa: BLE001 - ошибки остановки не должны ломать выход
        logger.warning("Ошибка при остановке поиска по Telegram.", exc_info=True)
    try:
        await close_http_client()
    except Exception:  # noqa: BLE001
        logger.warning("Ошибка при закрытии HTTP-клиента.", exc_info=True)
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        logger.warning("Ошибка при закрытии сессии бота.", exc_info=True)
    try:
        await shutdown_db()
    except Exception:  # noqa: BLE001
        logger.warning("Ошибка при закрытии базы данных.", exc_info=True)
    logger.info("MusicBox остановлен.")


if __name__ == "__main__":
    main()
