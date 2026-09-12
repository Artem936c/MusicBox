"""Распознавание музыки по аудиофрагменту (ТЗ п. 7, контракт V2, раздел 3).

ЧЕСТНО О СОСТОЯНИИ МОДУЛЯ
-------------------------
Это ЗАГЛУШКА в том смысле, что без ключей доступа распознавание не работает:
:func:`recognize` сразу бросает :class:`RecognitionUnavailable` с понятным
русским текстом и подсказкой, что нужно прописать в `.env`.

При этом код обращения к провайдерам — настоящий и рабочий: реальные адреса
эндпоинтов, подпись запроса ACRCloud, таймауты httpx, разбор JSON-ответов и
обработка ошибок. НО: ни один из провайдеров НЕ ПРОВЕРЯЛСЯ на живых ключах —
у проекта их нет. Поэтому перед боевым включением обязательно прогоните запрос
вручную и сверьте формат ответа с документацией провайдера: сервисы иногда
меняют структуру JSON, и тогда разбор ответа придётся поправить.

Провайдеры
----------
* ``audd``     — https://audd.io, распознавание по аудиофрагменту (нужен AUDD_API_TOKEN);
* ``acrcloud`` — https://www.acrcloud.com, распознавание по аудиофрагменту
  (нужны ACRCLOUD_HOST / ACRCLOUD_KEY / ACRCLOUD_SECRET);
* ``genius``   — https://genius.com, ТЕКСТОВЫЙ поиск по названию (нужен
  GENIUS_ACCESS_TOKEN). Аудио Genius не распознаёт, поэтому провайдеру нужен
  параметр ``hint`` — название трека или имя исполнителя.

Выбор провайдера — настройка ``RECOGNITION_PROVIDER`` (пусто = выключено).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

import httpx
from rapidfuzz import fuzz

from backend.config import settings
from backend.errors import MusicBoxError

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "PROVIDER_TITLES",
    "RecognitionError",
    "RecognitionUnavailable",
    "RecognitionResult",
    "current_provider",
    "is_configured",
    "recognize",
]


# --------------------------------------------------------------------------- #
# Ошибки
# --------------------------------------------------------------------------- #


class RecognitionError(MusicBoxError):
    """Сбой во время распознавания (сеть, ошибка провайдера, странный ответ)."""

    default_message = "Не удалось распознать трек. Попробуйте позже."


class RecognitionUnavailable(RecognitionError):
    """Распознавание выключено или не настроено.

    Наследуется от :class:`RecognitionError` (и, через него, от
    ``MusicBoxError``), поэтому обработчики, ловящие любую из этих ошибок,
    получат понятное русское сообщение.
    """

    default_message = (
        "Распознавание музыки не настроено. Укажите в файле .env настройку "
        "RECOGNITION_PROVIDER (audd, acrcloud или genius) и ключ доступа "
        "выбранного сервиса. Пока можно указать исполнителя и название вручную."
    )


# --------------------------------------------------------------------------- #
# Результат
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RecognitionResult:
    """Результат распознавания. Пустые ``artist``/``title`` = трек не опознан."""

    artist: str | None
    title: str | None
    provider: str
    confidence: float

    @property
    def matched(self) -> bool:
        """True, если провайдер вернул хоть что-то полезное."""
        return bool(self.artist or self.title)

    @property
    def display_name(self) -> str:
        """Человекочитаемое имя трека для сообщений пользователю."""
        parts = [part for part in (self.artist, self.title) if part]
        return " — ".join(parts) if parts else "Трек не опознан"


# --------------------------------------------------------------------------- #
# Константы провайдеров
# --------------------------------------------------------------------------- #

#: Поддерживаемые провайдеры (значения настройки RECOGNITION_PROVIDER).
PROVIDERS: Final[tuple[str, ...]] = ("audd", "acrcloud", "genius")

#: Названия провайдеров для сообщений пользователю.
PROVIDER_TITLES: Final[dict[str, str]] = {
    "audd": "AudD",
    "acrcloud": "ACRCloud",
    "genius": "Genius",
}

AUDD_ENDPOINT: Final[str] = "https://api.audd.io/"
GENIUS_SEARCH_ENDPOINT: Final[str] = "https://api.genius.com/search"
ACRCLOUD_PATH: Final[str] = "/v1/identify"

#: Таймауты HTTP: соединение короткое, ответ провайдера может быть небыстрым.
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

#: Сколько байт аудио отправлять провайдерам (10–15 секунд достаточно для опознания).
MAX_SAMPLE_BYTES: Final[int] = 1_048_576

#: Сколько результатов Genius разбирать (сервис отдаёт ~10 на страницу).
_GENIUS_MAX_HITS: Final[int] = 10


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


def current_provider() -> str:
    """Текущий провайдер из настроек в нижнем регистре (пустая строка = выключено)."""
    return (settings.recognition_provider or "").strip().lower()


def _provider_keys(provider: str) -> tuple[str, ...]:
    """Ключи доступа выбранного провайдера (пустые значения отбрасываются позже)."""
    if provider == "audd":
        return ((settings.audd_api_token or "").strip(),)
    if provider == "acrcloud":
        return (
            (settings.acrcloud_host or "").strip(),
            (settings.acrcloud_key or "").strip(),
            (settings.acrcloud_secret or "").strip(),
        )
    if provider == "genius":
        return ((settings.genius_access_token or "").strip(),)
    return ()


def is_configured(provider: str | None = None) -> bool:
    """Проверить, что провайдер выбран и его ключи заполнены.

    Без аргумента проверяется провайдер из настроек. Функция удобна хендлерам:
    можно не ловить исключение, а сразу показать подсказку.
    """
    name = (provider or current_provider() or "").strip().lower()
    if name not in PROVIDERS:
        return False
    keys = _provider_keys(name)
    return bool(keys) and all(keys)


def _require_provider() -> str:
    """Вернуть провайдера из настроек или бросить понятную ошибку."""
    provider = current_provider()
    if not provider:
        raise RecognitionUnavailable()
    if provider not in PROVIDERS:
        raise RecognitionUnavailable(
            f"Неизвестный сервис распознавания «{provider}». Допустимые значения "
            f"RECOGNITION_PROVIDER: {', '.join(PROVIDERS)}."
        )
    return provider


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _clean(value: Any) -> str | None:
    """Привести значение из ответа провайдера к непустой строке или None."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _sample(audio_bytes: bytes | None, provider: str) -> bytes:
    """Обрезать аудио до фрагмента, который принимают провайдеры."""
    if not audio_bytes:
        raise RecognitionError(
            f"Нечего распознавать: {PROVIDER_TITLES[provider]} работает по аудиофрагменту. "
            "Пришлите аудиофайл или голосовое сообщение."
        )
    if len(audio_bytes) > MAX_SAMPLE_BYTES:
        logger.debug(
            "Фрагмент для %s обрезан с %d до %d байт",
            provider,
            len(audio_bytes),
            MAX_SAMPLE_BYTES,
        )
        return audio_bytes[:MAX_SAMPLE_BYTES]
    return bytes(audio_bytes)


def _similarity(hint: str | None, artist: str | None, title: str | None) -> float | None:
    """Нечёткая близость подсказки и найденного названия: 0.0–1.0 или None."""
    text = _clean(hint)
    if not text:
        return None
    candidate = " ".join(part for part in (artist, title) if part)
    if not candidate:
        return 0.0
    try:
        value = float(fuzz.WRatio(text.casefold(), candidate.casefold()))
    except Exception:  # pragma: no cover — защита от сюрпризов rapidfuzz
        logger.warning("Не удалось оценить близость названия", exc_info=True)
        return None
    return max(0.0, min(1.0, value / 100.0))


async def _request_json(
    method: str,
    url: str,
    *,
    provider: str,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    files: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Выполнить HTTP-запрос к провайдеру и вернуть разобранный JSON-объект.

    Все сетевые сбои превращаются в :class:`RecognitionError` с русским текстом.
    """
    title = PROVIDER_TITLES.get(provider, provider)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
                files=files,
                headers=headers,
            )
    except httpx.TimeoutException as error:
        logger.warning("Таймаут запроса к %s: %s", title, error)
        raise RecognitionError(
            f"Сервис {title} не ответил вовремя. Попробуйте ещё раз позже."
        ) from error
    except httpx.HTTPError as error:
        logger.warning("Ошибка запроса к %s: %s", title, error)
        raise RecognitionError(
            f"Не удалось связаться с сервисом {title}. Проверьте подключение к интернету."
        ) from error

    if response.status_code >= 400:
        logger.warning(
            "Сервис %s вернул HTTP %s: %s", title, response.status_code, response.text[:300]
        )
        if response.status_code in (401, 403):
            raise RecognitionError(
                f"Сервис {title} отклонил ключ доступа. Проверьте настройки в файле .env."
            )
        if response.status_code == 429:
            raise RecognitionError(
                f"Сервис {title} временно ограничил число запросов. Попробуйте позже."
            )
        raise RecognitionError(f"Сервис {title} вернул ошибку {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as error:
        logger.warning("Неразбираемый ответ %s: %s", title, response.text[:300])
        raise RecognitionError(f"Сервис {title} вернул неожиданный ответ.") from error

    if not isinstance(payload, dict):
        logger.warning("Ответ %s не является объектом: %r", title, payload)
        raise RecognitionError(f"Сервис {title} вернул неожиданный ответ.")
    return payload


# --------------------------------------------------------------------------- #
# Провайдеры
# --------------------------------------------------------------------------- #


async def _recognize_audd(audio_bytes: bytes | None, hint: str | None) -> RecognitionResult:
    """Распознавание через AudD: POST multipart с фрагментом файла.

    Документация: https://docs.audd.io/ — ответ вида
    ``{"status": "success", "result": {"artist": ..., "title": ..., "album": ...}}``;
    ``result`` равен ``null``, если трек не опознан.
    """
    token = (settings.audd_api_token or "").strip()
    if not token:
        raise RecognitionUnavailable(
            "Для распознавания через AudD задайте AUDD_API_TOKEN в файле .env — "
            "ключ выдаётся в личном кабинете audd.io."
        )

    sample = _sample(audio_bytes, "audd")
    payload = await _request_json(
        "POST",
        AUDD_ENDPOINT,
        provider="audd",
        data={"api_token": token, "return": "timecode"},
        files={"file": ("sample.mp3", sample, "application/octet-stream")},
    )

    if payload.get("status") != "success":
        error = payload.get("error")
        message = _clean(error.get("error_message")) if isinstance(error, dict) else None
        raise RecognitionError(
            f"AudD вернул ошибку: {message or 'причина не указана'}."
        )

    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        logger.info("AudD не опознал фрагмент")
        return RecognitionResult(artist=None, title=None, provider="audd", confidence=0.0)

    artist = _clean(result.get("artist"))
    title = _clean(result.get("title"))
    # Своей оценки уверенности AudD не отдаёт: если есть подсказка — считаем
    # близость к ней, иначе доверяем ответу полностью.
    confidence = _similarity(hint, artist, title)
    if confidence is None:
        confidence = 1.0 if (artist and title) else 0.5
    return RecognitionResult(
        artist=artist, title=title, provider="audd", confidence=round(confidence, 3)
    )


def _acrcloud_url(host: str) -> str:
    """Собрать адрес эндпоинта ACRCloud из хоста настройки."""
    clean = host.strip().rstrip("/")
    if clean.startswith("http://") or clean.startswith("https://"):
        return f"{clean}{ACRCLOUD_PATH}"
    return f"https://{clean}{ACRCLOUD_PATH}"


def _acrcloud_signature(key: str, secret: str, timestamp: str) -> str:
    """Подпись запроса ACRCloud (HMAC-SHA1 + base64) по их протоколу v1."""
    string_to_sign = "\n".join(("POST", ACRCLOUD_PATH, key, "audio", "1", timestamp))
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


async def _recognize_acrcloud(audio_bytes: bytes | None, hint: str | None) -> RecognitionResult:
    """Распознавание через ACRCloud: подписанный POST на ``/v1/identify``.

    Ответ вида ``{"status": {"code": 0}, "metadata": {"music": [{...}]}}``;
    код 1001 означает «ничего не найдено».
    """
    host = (settings.acrcloud_host or "").strip()
    key = (settings.acrcloud_key or "").strip()
    secret = (settings.acrcloud_secret or "").strip()
    if not (host and key and secret):
        raise RecognitionUnavailable(
            "Для распознавания через ACRCloud задайте ACRCLOUD_HOST, ACRCLOUD_KEY и "
            "ACRCLOUD_SECRET в файле .env — значения берутся из консоли ACRCloud."
        )

    sample = _sample(audio_bytes, "acrcloud")
    timestamp = str(int(time.time()))
    payload = await _request_json(
        "POST",
        _acrcloud_url(host),
        provider="acrcloud",
        data={
            "access_key": key,
            "data_type": "audio",
            "signature_version": "1",
            "signature": _acrcloud_signature(key, secret, timestamp),
            "sample_bytes": str(len(sample)),
            "timestamp": timestamp,
        },
        files={"sample": ("sample.mp3", sample, "application/octet-stream")},
    )

    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    code = status.get("code")
    if code == 1001:
        logger.info("ACRCloud не опознал фрагмент")
        return RecognitionResult(artist=None, title=None, provider="acrcloud", confidence=0.0)
    if code not in (0, "0"):
        message = _clean(status.get("msg"))
        raise RecognitionError(
            f"ACRCloud вернул ошибку: {message or 'причина не указана'}."
        )

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    music = metadata.get("music")
    if not isinstance(music, list) or not music:
        return RecognitionResult(artist=None, title=None, provider="acrcloud", confidence=0.0)

    best = music[0] if isinstance(music[0], dict) else {}
    title = _clean(best.get("title"))
    artist = _first_artist_name(best.get("artists"))

    raw_score = best.get("score")
    try:
        confidence = max(0.0, min(1.0, float(raw_score) / 100.0))
    except (TypeError, ValueError):
        confidence = _similarity(hint, artist, title)
        if confidence is None:
            confidence = 1.0 if (artist and title) else 0.5

    return RecognitionResult(
        artist=artist, title=title, provider="acrcloud", confidence=round(confidence, 3)
    )


def _first_artist_name(artists: Any) -> str | None:
    """Имя основного исполнителя из списка ACRCloud ``[{"name": ...}, ...]``."""
    if not isinstance(artists, Sequence) or isinstance(artists, (str, bytes)):
        return None
    for item in artists:
        if isinstance(item, dict):
            name = _clean(item.get("name"))
            if name:
                return name
        else:
            name = _clean(item)
            if name:
                return name
    return None


async def _recognize_genius(hint: str | None) -> RecognitionResult:
    """Поиск по названию через Genius (аудио этот сервис не распознаёт).

    Ответ вида ``{"response": {"hits": [{"result": {"title": ...,
    "primary_artist": {"name": ...}}}]}}``. Из найденных вариантов берётся
    самый близкий к подсказке по нечёткому сравнению.
    """
    token = (settings.genius_access_token or "").strip()
    if not token:
        raise RecognitionUnavailable(
            "Для поиска через Genius задайте GENIUS_ACCESS_TOKEN в файле .env — "
            "токен выдаётся на genius.com/api-clients."
        )

    query = _clean(hint)
    if not query:
        raise RecognitionError(
            "Genius ищет только по названию: укажите название трека или имя исполнителя."
        )

    payload = await _request_json(
        "GET",
        GENIUS_SEARCH_ENDPOINT,
        provider="genius",
        params={"q": query},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    hits = response.get("hits")
    if not isinstance(hits, list) or not hits:
        logger.info("Genius ничего не нашёл по запросу «%s»", query)
        return RecognitionResult(artist=None, title=None, provider="genius", confidence=0.0)

    best_artist: str | None = None
    best_title: str | None = None
    best_score = -1.0

    for hit in hits[:_GENIUS_MAX_HITS]:
        result = hit.get("result") if isinstance(hit, dict) else None
        if not isinstance(result, dict):
            continue
        title = _clean(result.get("title"))
        primary = result.get("primary_artist")
        artist = _clean(primary.get("name")) if isinstance(primary, dict) else None
        if not (title or artist):
            continue
        score = _similarity(query, artist, title) or 0.0
        if score > best_score:
            best_score, best_artist, best_title = score, artist, title

    if best_score < 0:
        return RecognitionResult(artist=None, title=None, provider="genius", confidence=0.0)

    return RecognitionResult(
        artist=best_artist,
        title=best_title,
        provider="genius",
        confidence=round(max(0.0, best_score), 3),
    )


# --------------------------------------------------------------------------- #
# Публичная точка входа
# --------------------------------------------------------------------------- #


async def recognize(
    audio_bytes: bytes | None = None,
    *,
    hint: str | None = None,
) -> RecognitionResult:
    """Распознать трек выбранным в настройках провайдером.

    :param audio_bytes: фрагмент аудио (нужен для AudD и ACRCloud; лишнее
        обрезается до :data:`MAX_SAMPLE_BYTES`).
    :param hint: текстовая подсказка — название трека или имя исполнителя.
        Для Genius обязательна, для остальных используется при оценке
        уверенности.
    :raises RecognitionUnavailable: провайдер не выбран или не заполнены ключи.
    :raises RecognitionError: сбой сети или ошибка на стороне провайдера.

    ЗАГЛУШКА ПО ТЗ п. 7: без ключей функция ничего не выполняет — сразу
    бросает :class:`RecognitionUnavailable` с подсказкой, что прописать в
    `.env`. Ниже — настоящие запросы к провайдерам, но на живых ключах они
    не проверялись (у проекта их нет), поэтому формат ответа стоит сверить с
    документацией сервиса перед боевым включением.
    """
    provider = _require_provider()
    logger.info(
        "Запрос распознавания через %s (аудио: %d байт, подсказка: %s)",
        PROVIDER_TITLES[provider],
        len(audio_bytes or b""),
        _clean(hint) or "нет",
    )

    if provider == "audd":
        result = await _recognize_audd(audio_bytes, hint)
    elif provider == "acrcloud":
        result = await _recognize_acrcloud(audio_bytes, hint)
    else:
        result = await _recognize_genius(hint)

    logger.info(
        "Результат распознавания (%s): %s, уверенность %.2f",
        result.provider,
        result.display_name,
        result.confidence,
    )
    return result
