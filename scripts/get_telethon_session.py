"""Получение строки сессии Telethon (TG_SESSION_STRING) для поиска аудио в Telegram.

Запуск из корня проекта:

    python scripts/get_telethon_session.py

Скрипт спросит api_id, api_hash и номер телефона обычного (не бот-) аккаунта,
проведёт вход по коду из Telegram и напечатает строку сессии, которую нужно
вставить в файл .env в переменную TG_SESSION_STRING.

Важно: строка сессии равнозначна доступу к аккаунту — храните её как пароль,
не публикуйте и не передавайте третьим лицам. Скрипт ничего никуда не
отправляет и не сохраняет: строка только выводится в консоль.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

# Корень проекта в sys.path — чтобы подхватить значения из backend.config (файл .env).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

#: Ширина разделительной линии в выводе.
LINE_WIDTH = 74

try:  # мягкий импорт: без Telethon скрипт объясняет, что установить
    from telethon import TelegramClient
    from telethon.errors import (
        ApiIdInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.sessions import StringSession

    TELETHON_AVAILABLE = True
    TELETHON_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - зависит от окружения
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]
    ApiIdInvalidError = PhoneCodeExpiredError = PhoneCodeInvalidError = Exception  # type: ignore[misc]
    PhoneNumberInvalidError = SessionPasswordNeededError = Exception  # type: ignore[misc]
    TELETHON_AVAILABLE = False
    TELETHON_IMPORT_ERROR = str(exc)


def out(text: str = "") -> None:
    """Вывести строку в консоль (обычный вывод скрипта, не журнал)."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def rule() -> None:
    """Нарисовать разделительную линию."""
    out("=" * LINE_WIDTH)


async def ask(prompt: str, default: str = "") -> str:
    """Спросить значение у пользователя, не блокируя цикл событий."""
    suffix = f" [{default}]" if default else ""
    answer = await asyncio.to_thread(input, f"{prompt}{suffix}: ")
    return answer.strip() or default


async def ask_secret(prompt: str) -> str:
    """Спросить значение скрытым вводом (пароль двухфакторной аутентификации)."""
    return (await asyncio.to_thread(getpass.getpass, f"{prompt}: ")).strip()


async def ask_api_id(default: int) -> int:
    """Спросить числовой api_id, повторяя вопрос при неверном вводе."""
    default_text = str(default) if default else ""
    while True:
        raw = await ask("api_id (только цифры)", default_text)
        try:
            value = int(raw)
        except ValueError:
            out("  Нужно целое число, например 1234567. Попробуйте ещё раз.")
            continue
        if value <= 0:
            out("  api_id должен быть положительным числом. Попробуйте ещё раз.")
            continue
        return value


async def ask_non_empty(prompt: str, default: str = "") -> str:
    """Спросить непустое значение, повторяя вопрос при пустом вводе."""
    while True:
        value = await ask(prompt, default)
        if value:
            return value
        out("  Значение не может быть пустым. Попробуйте ещё раз.")


def read_defaults() -> tuple[int, str]:
    """Подставить api_id и api_hash из .env / переменных окружения, если они уже заданы."""
    try:
        from backend.config import settings

        return settings.tg_api_id, settings.tg_api_hash.strip()
    except Exception:  # noqa: BLE001 - работаем и без настроек проекта
        try:
            api_id = int(os.environ.get("TG_API_ID", "0") or 0)
        except ValueError:
            api_id = 0
        return api_id, os.environ.get("TG_API_HASH", "").strip()


def print_intro() -> None:
    """Показать вводную инструкцию."""
    rule()
    out("MusicBox — получение строки сессии Telethon (TG_SESSION_STRING)")
    rule()
    out("Строка сессии нужна только для поиска аудио в публичных каналах Telegram.")
    out("Без неё бот работает полностью: пересылайте ему аудио — он сохранит файлы")
    out("в приватный канал-хранилище и разложит их по папкам исполнителей.")
    out("")
    out("Что понадобится:")
    out("  1. Обычный аккаунт Telegram (не бот) и телефон с доступом к нему.")
    out("  2. api_id и api_hash: https://my.telegram.org → API development tools.")
    out("")
    out("Строка сессии даёт полный доступ к аккаунту. Храните её как пароль:")
    out("вставьте в .env (TG_SESSION_STRING=...) и никому не показывайте.")
    rule()
    out("")


def print_result(session_string: str, api_id: int, api_hash: str, name: str) -> None:
    """Показать итоговую строку сессии и подсказки по настройке .env."""
    out("")
    rule()
    out(f"Готово! Вход выполнен от имени: {name}")
    rule()
    out("Скопируйте эти строки в файл .env:")
    out("")
    out("TELEGRAM_SEARCH_ENABLED=true")
    out(f"TG_API_ID={api_id}")
    out(f"TG_API_HASH={api_hash}")
    out(f"TG_SESSION_STRING={session_string}")
    out("")
    out("Дополнительно можно перечислить каналы для поиска по умолчанию:")
    out("TG_SEARCH_CHATS=musicchannel,best_music_archive")
    rule()
    out("После правки .env перезапустите MusicBox.")


async def create_session() -> int:
    """Провести интерактивный вход и вывести строку сессии. Возвращает код выхода."""
    print_intro()

    default_api_id, default_api_hash = read_defaults()
    api_id = await ask_api_id(default_api_id)
    api_hash = await ask_non_empty("api_hash", default_api_hash)
    phone = await ask_non_empty("Номер телефона в международном формате (+79001234567)")

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
    except ApiIdInvalidError:
        out("")
        out("Ошибка: api_id и api_hash не подходят друг к другу.")
        out("Проверьте значения на https://my.telegram.org → API development tools.")
        return 1
    except OSError as exc:
        out("")
        out(f"Ошибка сети: не удалось подключиться к Telegram ({exc}).")
        out("Проверьте интернет-соединение и повторите запуск.")
        return 1

    try:
        if await client.is_user_authorized():
            out("")
            out("Аккаунт уже авторизован в этой сессии.")
        else:
            if not await _sign_in(client, phone):
                return 1

        me = await client.get_me()
        name = _describe_user(me)
        session_string = client.session.save()
    finally:
        await client.disconnect()

    print_result(session_string, api_id, api_hash, name)
    return 0


async def _sign_in(client: "TelegramClient", phone: str) -> bool:
    """Отправить код, принять его от пользователя и войти. True — вход выполнен."""
    try:
        await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        out("")
        out("Ошибка: номер телефона указан неверно.")
        out("Используйте международный формат, например +79001234567.")
        return False

    out("")
    out("Код подтверждения отправлен в Telegram (сообщение от Telegram, не SMS).")
    out("Если код пришёл в само приложение — откройте чат «Telegram».")
    out("")

    for attempt in range(1, 4):
        code = await ask_non_empty("Код подтверждения")
        try:
            await client.sign_in(phone=phone, code=code)
            return True
        except PhoneCodeInvalidError:
            remaining = 3 - attempt
            if remaining:
                out(f"  Неверный код. Осталось попыток: {remaining}.")
                continue
            out("  Неверный код. Запустите скрипт заново.")
            return False
        except PhoneCodeExpiredError:
            out("  Срок действия кода истёк. Запустите скрипт заново.")
            return False
        except SessionPasswordNeededError:
            return await _sign_in_with_password(client)
    return False


async def _sign_in_with_password(client: "TelegramClient") -> bool:
    """Вход при включённой двухфакторной аутентификации (облачный пароль)."""
    out("")
    out("На аккаунте включена двухфакторная аутентификация.")
    out("Введите облачный пароль (символы не отображаются).")
    for attempt in range(1, 4):
        password = await ask_secret("Пароль")
        if not password:
            out("  Пароль не может быть пустым.")
            continue
        try:
            await client.sign_in(password=password)
            return True
        except Exception as exc:  # noqa: BLE001 - Telethon бросает разные типы ошибок пароля
            remaining = 3 - attempt
            if remaining:
                out(f"  Пароль не подошёл ({exc}). Осталось попыток: {remaining}.")
                continue
            out("  Пароль не подошёл. Запустите скрипт заново.")
            return False
    return False


def _describe_user(user: object) -> str:
    """Собрать читаемое имя пользователя для итогового сообщения."""
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    username = getattr(user, "username", None)
    full_name = " ".join(part for part in (first, last) if part).strip()
    if username:
        return f"{full_name} (@{username})" if full_name else f"@{username}"
    return full_name or "неизвестный пользователь"


def main() -> int:
    """Точка входа скрипта. Возвращает код выхода процесса."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.ERROR)

    if not TELETHON_AVAILABLE:
        out("Библиотека Telethon не установлена, а без неё получить сессию нельзя.")
        out("Установите её командой:")
        out("    pip install telethon")
        out("или установите все зависимости проекта:")
        out("    pip install -r requirements.txt")
        if TELETHON_IMPORT_ERROR:
            out("")
            out(f"Подробности: {TELETHON_IMPORT_ERROR}")
        return 1

    try:
        return asyncio.run(create_session())
    except KeyboardInterrupt:
        out("")
        out("Прервано пользователем. Строка сессии не создана.")
        return 130
    except EOFError:
        out("")
        out("Ввод недоступен: скрипт нужно запускать в интерактивном терминале.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
