"""Настройка профиля бота MusicBox: имя, краткое и полное описание.

Запуск из корня проекта:

    python -m scripts.setup_profile              # применить настройки
    python -m scripts.setup_profile --dry-run    # только показать, без запросов

Скрипту достаточно переменной `BOT_TOKEN` (из окружения, из файла .env или
из ключа `--token`). Канал-хранилище и база данных не нужны.

Что ставится через Bot API:

    setMyName             — отображаемое имя бота;
    setMyShortDescription — текст в профиле и в превью при пересылке;
    setMyDescription      — текст в пустом чате, до нажатия «Запустить».

Чего в Bot API НЕТ: метода для фото профиля бота. `setMyPhoto` не существует,
а `setChatPhoto` работает только для чатов и каналов. Поэтому аватарка
ставится вручную через @BotFather -> /setuserpic; пошаговую инструкцию скрипт
печатает в конце работы, а сам файл готовит `python -m scripts.make_avatar`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from backend.config import settings
from backend.logging_config import setup_logging

logger = logging.getLogger("scripts.setup_profile")

#: Ограничения Telegram на длину полей профиля (символы, не байты).
MAX_NAME = 64
MAX_SHORT_DESCRIPTION = 120
MAX_DESCRIPTION = 512

#: Таймаут HTTP-запроса к Bot API.
REQUEST_TIMEOUT = 20.0

BOT_NAME = "MusicBox"

BOT_SHORT_DESCRIPTION = (
    "Личное облачное хранилище музыки в Telegram: загружайте треки, "
    "слушайте и находите их за секунды."
)

BOT_DESCRIPTION = (
    "MusicBox — ваше личное облачное хранилище музыки прямо в Telegram.\n"
    "\n"
    "• Загружайте треки, документы, видео, кружочки и голосовые\n"
    "• Раскладывайте по вложенным папкам, альбомам и плейлистам\n"
    "• Ищите с опечатками и в неверной раскладке клавиатуры\n"
    "• Смотрите статистику прослушиваний и получайте рекомендации\n"
    "• Слушайте в чате или в приложении Mini App\n"
    "\n"
    "Файлы хранятся в вашем приватном канале и не занимают место на устройстве.\n"
    "Нажмите «Запустить», чтобы начать."
)

AVATAR_INSTRUCTIONS = (
    "Аватарка бота — вручную (метода установки фото профиля в Bot API нет):\n"
    "  1. Подготовьте файл:  python -m scripts.make_avatar\n"
    "     (создаст assets/bot_avatar.png 512x512 и assets/bot_avatar.svg)\n"
    "  2. Откройте чат с @BotFather и отправьте команду /setuserpic\n"
    "  3. Выберите нужного бота в списке\n"
    "  4. Отправьте assets/bot_avatar.png как ФОТО, а не как файл-документ:\n"
    "     скрепка -> «Фото или видео» -> выбрать bot_avatar.png\n"
    "  5. BotFather ответит «Success! Profile photo updated.»\n"
    "Проверить результат: откройте профиль бота — новая картинка появляется\n"
    "сразу, в списке чатов может обновиться с задержкой в несколько минут."
)


class ProfileError(RuntimeError):
    """Ошибка настройки профиля с готовым русским описанием."""


@dataclass(frozen=True, slots=True)
class ProfileField:
    """Одно поле профиля: как поставить и как прочитать обратно.

    :param title: название поля для журнала.
    :param setter: метод Bot API для установки значения.
    :param getter: метод Bot API для чтения значения.
    :param payload_key: имя параметра запроса.
    :param result_key: имя поля в ответе `getter`.
    :param value: значение, которое нужно установить.
    :param limit: максимальная длина значения по правилам Telegram.
    """

    title: str
    setter: str
    getter: str
    payload_key: str
    result_key: str
    value: str
    limit: int


PROFILE_FIELDS: tuple[ProfileField, ...] = (
    ProfileField(
        title="Имя бота",
        setter="setMyName",
        getter="getMyName",
        payload_key="name",
        result_key="name",
        value=BOT_NAME,
        limit=MAX_NAME,
    ),
    ProfileField(
        title="Краткое описание",
        setter="setMyShortDescription",
        getter="getMyShortDescription",
        payload_key="short_description",
        result_key="short_description",
        value=BOT_SHORT_DESCRIPTION,
        limit=MAX_SHORT_DESCRIPTION,
    ),
    ProfileField(
        title="Полное описание",
        setter="setMyDescription",
        getter="getMyDescription",
        payload_key="description",
        result_key="description",
        value=BOT_DESCRIPTION,
        limit=MAX_DESCRIPTION,
    ),
)


# ---------------------------------------------------------------------------
# Токен и проверка текстов
# ---------------------------------------------------------------------------


def resolve_token(cli_token: str | None) -> str:
    """Определить токен бота: ключ `--token`, затем `BOT_TOKEN`, затем .env.

    Бросает `ProfileError` с инструкцией, если токен нигде не найден.
    """
    for candidate in (cli_token, os.environ.get("BOT_TOKEN"), settings.bot_token):
        token = (candidate or "").strip()
        if token:
            return token

    raise ProfileError(
        "Не задан токен бота (BOT_TOKEN).\n"
        "Возьмите токен у @BotFather (/mybots -> выбрать бота -> API Token) и\n"
        "укажите его одним из способов:\n"
        "  • в файле .env в корне проекта:   BOT_TOKEN=123456:AA...\n"
        "  • переменной окружения:           $env:BOT_TOKEN = '123456:AA...'\n"
        "  • ключом командной строки:        python -m scripts.setup_profile "
        "--token 123456:AA...\n"
        "Другие настройки (канал-хранилище, база) этому скрипту не нужны."
    )


def mask_token(token: str) -> str:
    """Показать токен безопасно: виден только публичный идентификатор бота."""
    bot_id, separator, _ = token.partition(":")
    if separator and bot_id.isdigit():
        return f"{bot_id}:***"
    return "***"


def validate_fields(fields: Sequence[ProfileField]) -> None:
    """Проверить длину текстов до отправки — Telegram отклоняет их молча и грубо."""
    problems = [
        f"{field.title}: {len(field.value)} символов при лимите {field.limit}"
        for field in fields
        if len(field.value) > field.limit
    ]
    if problems:
        raise ProfileError(
            "Тексты профиля не проходят по длине — исправьте константы в "
            "scripts/setup_profile.py:\n" + "\n".join(f"  • {item}" for item in problems)
        )


# ---------------------------------------------------------------------------
# Работа с Bot API
# ---------------------------------------------------------------------------


def _api_url(token: str, method: str) -> str:
    """Полный адрес метода Bot API с учётом `TELEGRAM_API_BASE`."""
    base = (settings.telegram_api_base or "https://api.telegram.org").strip().rstrip("/")
    return f"{base}/bot{token}/{method}"


async def call_api(
    client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Вызвать метод Bot API и вернуть содержимое поля `result`.

    Бросает `ProfileError` с понятным текстом при сетевой ошибке, некорректном
    ответе или отказе Telegram.
    """
    try:
        response = await client.post(_api_url(token, method), json=payload)
    except httpx.TimeoutException as error:
        raise ProfileError(
            f"{method}: Telegram не ответил за {REQUEST_TIMEOUT:.0f} с. "
            "Проверьте интернет или прокси и повторите запуск."
        ) from error
    except httpx.HTTPError as error:
        raise ProfileError(f"{method}: сеть недоступна ({error}).") from error

    try:
        data = response.json()
    except json.JSONDecodeError as error:
        raise ProfileError(
            f"{method}: Telegram вернул не JSON (HTTP {response.status_code}). "
            "Проверьте адрес TELEGRAM_API_BASE."
        ) from error

    if not isinstance(data, dict) or not data.get("ok"):
        description = ""
        retry_after = None
        if isinstance(data, dict):
            description = str(data.get("description") or "без описания")
            parameters = data.get("parameters")
            if isinstance(parameters, dict):
                retry_after = parameters.get("retry_after")
        if response.status_code == 401:
            raise ProfileError(
                f"{method}: Telegram не принял токен (401). Проверьте BOT_TOKEN — "
                "возможно, он отозван или скопирован не полностью."
            )
        if retry_after:
            raise ProfileError(
                f"{method}: слишком часто. Telegram просит подождать {retry_after} с "
                "(имя бота разрешено менять лишь несколько раз в час)."
            )
        raise ProfileError(f"{method}: Telegram отказал (HTTP {response.status_code}): {description}")

    result = data.get("result")
    return result if isinstance(result, dict) else {}


async def apply_profile(
    token: str, *, language_code: str, fields: Sequence[ProfileField]
) -> int:
    """Применить настройки профиля. Возвращает число неудачных полей."""
    failures = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        me = await call_api(client, token, "getMe", {})
        logger.info(
            "Настраиваю бота @%s (id=%s)", me.get("username", "?"), me.get("id", "?")
        )

        scope: dict[str, Any] = {"language_code": language_code} if language_code else {}
        for field in fields:
            try:
                await call_api(client, token, field.setter, {field.payload_key: field.value, **scope})
                current = await call_api(client, token, field.getter, dict(scope))
            except ProfileError as error:
                failures += 1
                logger.error("%s — не удалось: %s", field.title, error)
                continue

            applied = str(current.get(field.result_key, ""))
            if applied.strip() == field.value.strip():
                logger.info("%s — установлено (%d символов)", field.title, len(field.value))
            else:
                failures += 1
                logger.error(
                    "%s — Telegram сохранил другое значение: %r", field.title, applied
                )

    return failures


def report_planned(fields: Sequence[ProfileField], token: str, language_code: str) -> None:
    """Показать, что именно будет отправлено, не обращаясь к сети."""
    logger.info(
        "Режим --dry-run: запросов к Telegram нет. Токен %s, язык профиля: %s",
        mask_token(token),
        language_code or "по умолчанию (для всех языков)",
    )
    for field in fields:
        logger.info(
            "%s (%s, %d/%d символов):\n%s",
            field.title,
            field.setter,
            len(field.value),
            field.limit,
            field.value,
        )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.setup_profile",
        description="Ставит имя и описания бота MusicBox через Bot API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=AVATAR_INSTRUCTIONS,
    )
    parser.add_argument(
        "--token",
        default=None,
        help="токен бота; по умолчанию берётся из BOT_TOKEN или из файла .env",
    )
    parser.add_argument(
        "--language-code",
        default="",
        help=(
            "код языка профиля (например ru). Пусто — значения по умолчанию, "
            "которые видят пользователи с любым языком интерфейса"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать тексты и выйти, не обращаясь к Telegram",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Основная работа скрипта. Возвращает код выхода процесса."""
    try:
        validate_fields(PROFILE_FIELDS)
        token = resolve_token(args.token)
    except ProfileError as error:
        logger.error("%s", error)
        logger.info("%s", AVATAR_INSTRUCTIONS)
        return 2

    if args.dry_run:
        report_planned(PROFILE_FIELDS, token, args.language_code)
        logger.info("%s", AVATAR_INSTRUCTIONS)
        return 0

    try:
        failures = await apply_profile(
            token, language_code=args.language_code, fields=PROFILE_FIELDS
        )
    except ProfileError as error:
        logger.error("%s", error)
        logger.info("%s", AVATAR_INSTRUCTIONS)
        return 1

    if failures:
        logger.error(
            "Не удалось применить полей: %d из %d. Исправьте причину и повторите запуск.",
            failures,
            len(PROFILE_FIELDS),
        )
    else:
        logger.info("Профиль бота обновлён: имя, краткое и полное описание.")

    logger.info("%s", AVATAR_INSTRUCTIONS)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Разобрать аргументы и запустить асинхронную часть."""
    args = _parse_args(argv)
    setup_logging()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем — профиль мог остаться не до конца настроенным.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
