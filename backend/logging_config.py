"""Настройка логирования MusicBox: консоль + опциональный файл с ротацией."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Размер файла лога до ротации и число хранимых копий.
FILE_MAX_BYTES = 5 * 1024 * 1024
FILE_BACKUP_COUNT = 5

#: Болтливые библиотечные логгеры и уровень, до которого их приглушаем.
#: `uvicorn.access` намеренно НЕ трогаем — журнал HTTP-запросов нужен.
NOISY_LOGGERS: dict[str, int] = {
    "aiogram.event": logging.WARNING,
    "aiogram.dispatcher": logging.INFO,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "asyncio": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "telethon": logging.WARNING,
    "multipart": logging.WARNING,
    "python_multipart": logging.WARNING,
    "watchfiles": logging.WARNING,
}

_configured = False


def setup_logging(level: str | None = None, log_file: str | None = None) -> None:
    """Настроить корневой логгер. Повторные вызовы игнорируются (идемпотентно).

    :param level: уровень логирования, по умолчанию `settings.log_level`.
    :param log_file: путь к файлу лога, по умолчанию `settings.log_file` (пусто = только консоль).
    """
    global _configured
    if _configured:
        return

    resolved_level = _resolve_level(level if level is not None else settings.log_level)
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    # Убираем хендлеры, которые могли поставить сторонние библиотеки.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        _close_quietly(handler)
    root.setLevel(resolved_level)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(resolved_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    target_file = log_file if log_file is not None else settings.log_file
    file_path = (target_file or "").strip()
    if file_path:
        file_handler = _create_file_handler(file_path, resolved_level, formatter)
        if file_handler is not None:
            root.addHandler(file_handler)

    for name, noisy_level in NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(noisy_level, resolved_level))

    _configured = True
    logger.info(
        "Логирование настроено: уровень %s, файл: %s",
        logging.getLevelName(resolved_level),
        file_path or "не используется",
    )


def _create_file_handler(
    file_path: str, level: int, formatter: logging.Formatter
) -> RotatingFileHandler | None:
    """Создать файловый хендлер с ротацией; при ошибке вернуть None и предупредить в консоль."""
    try:
        path = Path(file_path).expanduser()
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            filename=str(path),
            maxBytes=FILE_MAX_BYTES,
            backupCount=FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Не удалось открыть файл лога %s: %s. Логи пишутся только в консоль.", file_path, exc
        )
        return None
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _resolve_level(level: str | int | None) -> int:
    """Преобразовать уровень логирования из строки в число; неизвестное значение -> INFO."""
    if isinstance(level, int):
        return level
    name = (level or "INFO").strip().upper()
    resolved = logging.getLevelName(name)
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def _close_quietly(handler: logging.Handler) -> None:
    """Закрыть хендлер, не мешая настройке логирования при ошибке."""
    try:
        handler.close()
    except Exception:  # noqa: BLE001 - закрытие логгера не должно ломать запуск
        pass


__all__ = ["setup_logging", "LOG_FORMAT", "DATE_FORMAT", "NOISY_LOGGERS"]
