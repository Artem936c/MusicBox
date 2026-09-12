"""Сборка всех роутеров API в единый `api_router`.

Каждый модуль-роутер экспортирует переменную `router` (`APIRouter`) со своим
префиксом (`/folders`, `/tracks`, `/notes`, ...). `create_app` подключает
`api_router` дважды: с префиксом `/api` и без него.

Роутер раздела «Другое» (`other.py`) подключается через `_include_optional`:
модуль появляется отдельным шагом доработки V2, и его отсутствие не должно
ронять всё приложение — вместо этого в лог уходит явная ошибка, а остальные
маршруты продолжают работать. Любая другая ошибка импорта (синтаксис,
нехватка зависимости внутри модуля) пробрасывается как есть.
"""

from __future__ import annotations

import logging
from importlib import import_module

from fastapi import APIRouter

from backend.api.routers import albums as albums_router
from backend.api.routers import artists as artists_router
from backend.api.routers import favourites as favourites_router
from backend.api.routers import folders as folders_router
from backend.api.routers import notes as notes_router
from backend.api.routers import playlists as playlists_router
from backend.api.routers import recommendations as recommendations_router
from backend.api.routers import search as search_router
from backend.api.routers import settings as settings_router
from backend.api.routers import stats as stats_router
from backend.api.routers import tracks as tracks_router

logger = logging.getLogger(__name__)

#: Пакет, в котором лежат модули-роутеры.
ROUTERS_PACKAGE = "backend.api.routers"

api_router = APIRouter()


def _include_optional(module_name: str) -> bool:
    """Подключить роутер, модуль которого может ещё отсутствовать.

    :param module_name: имя модуля внутри `backend.api.routers` (без пакета).
    :returns: True, если маршруты подключены.
    :raises ImportError: модуль есть, но сломан или тянет отсутствующую зависимость.
    """
    full_name = f"{ROUTERS_PACKAGE}.{module_name}"
    try:
        module = import_module(full_name)
    except ModuleNotFoundError as exc:
        if exc.name != full_name:
            # Внутри модуля не хватает чужой зависимости — это настоящая ошибка.
            raise
        logger.error(
            "Модуль роутера «%s» не найден: соответствующие маршруты API недоступны",
            full_name,
        )
        return False

    router = getattr(module, "router", None)
    if router is None:
        logger.error("В модуле «%s» нет переменной router — маршруты не подключены", full_name)
        return False

    api_router.include_router(router)
    logger.debug("Подключён роутер «%s»", full_name)
    return True


# Порядок подключения важен только для документации; маршруты не пересекаются.
api_router.include_router(folders_router.router)
api_router.include_router(tracks_router.router)
api_router.include_router(favourites_router.router)
api_router.include_router(playlists_router.router)
api_router.include_router(artists_router.router)
api_router.include_router(albums_router.router)
api_router.include_router(notes_router.router)
api_router.include_router(recommendations_router.router)
_include_optional("other")
api_router.include_router(search_router.router)
api_router.include_router(stats_router.router)
api_router.include_router(settings_router.router)

logger.debug("Подключено маршрутов API: %d", len(api_router.routes))

__all__ = ["api_router"]
