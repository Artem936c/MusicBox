"""Исключения MusicBox. У каждого класса есть понятное сообщение по умолчанию (RU)."""

from __future__ import annotations


class MusicBoxError(Exception):
    """Базовая ошибка MusicBox."""

    default_message = "Что-то пошло не так. Попробуйте позже."

    def __init__(self, message: str | None = None) -> None:
        text = (message or "").strip() or self.default_message
        super().__init__(text)
        self.message = text

    def __str__(self) -> str:
        return self.message


class NotFoundError(MusicBoxError):
    """Запрошенный объект не найден."""

    default_message = "Не найдено. Возможно, объект уже удалён."


class ValidationError(MusicBoxError):
    """Некорректные данные от пользователя."""

    default_message = "Некорректные данные. Проверьте введённые значения."


class StorageError(MusicBoxError):
    """Ошибка работы с каналом-хранилищем или файлом."""

    default_message = "Не удалось сохранить файл в хранилище. Попробуйте ещё раз."


class AuthError(MusicBoxError):
    """Ошибка авторизации Mini App или потокового токена."""

    default_message = "Не удалось подтвердить авторизацию. Откройте приложение через Telegram."


class TelegramSearchUnavailable(MusicBoxError):
    """Поиск по Telegram выключен или не настроен."""

    default_message = (
        "Поиск по Telegram не настроен. Перешлите аудио боту — "
        "я сохраню его в хранилище и разложу по папкам."
    )


class TelegramSearchError(MusicBoxError):
    """Сбой во время поиска или импорта аудио из Telegram."""

    default_message = "Не удалось выполнить поиск в Telegram. Попробуйте позже."


class FileTooLargeError(StorageError):
    """Файл превышает лимит Bot API (20 МБ на скачивание)."""

    default_message = (
        "Файл слишком большой: Telegram Bot API позволяет скачивать не более 20 МБ. "
        "Пришлите файл поменьше или подключите локальный Bot API server."
    )


__all__ = [
    "MusicBoxError",
    "NotFoundError",
    "ValidationError",
    "StorageError",
    "AuthError",
    "TelegramSearchUnavailable",
    "TelegramSearchError",
    "FileTooLargeError",
]
