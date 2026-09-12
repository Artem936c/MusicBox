"""HTTP-слой MusicBox: FastAPI-приложение, схемы, зависимости и безопасность Mini App.

Пакет намеренно не импортирует подмодули на уровне пакета: `backend.api.app`
тянет за собой роутеры, репозитории и сервисы, а `backend.api.security`
должен оставаться пригодным для импорта отдельно (например, в тестах).
Точка входа приложения — `backend.api.app.create_app`.
"""

from __future__ import annotations

__all__: list[str] = []
