"""Сервисный слой MusicBox.

Пакет намеренно пустой: подмодули (`metadata`, `autosort`, `search`, `storage`,
`telegram_search`) импортируются явно, чтобы не тянуть тяжёлые зависимости
(aiogram, Telethon, httpx) при импорте репозиториев БД.
"""

from __future__ import annotations

__all__: list[str] = []
