"""Определяет ID приватного канала-хранилища и прописывает его в .env.

Запуск:

    python -m scripts.detect_channel [минуты_ожидания]

Скрипт опрашивает ``getUpdates`` и ждёт, когда бота добавят администратором
в канал. Как только это происходит, он проверяет права на публикацию и
записывает ``STORAGE_CHANNEL_ID`` в файл ``.env``.

Важно: смещение (offset) намеренно НЕ подтверждается, поэтому сообщения,
присланные боту во время ожидания, останутся в очереди и будут обработаны
ботом после запуска.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import httpx

from backend.config import settings
from backend.logging_config import setup_logging

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
POLL_INTERVAL = 3.0


def _api(method: str) -> str:
    return f"{settings.telegram_api_base}/bot{settings.bot_token}/{method}"


def _write_env(key: str, value: str) -> bool:
    """Заменяет значение переменной в .env. Возвращает True, если файл изменён."""
    if not ENV_PATH.exists():
        logger.error("Файл .env не найден: %s", ENV_PATH)
        return False
    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    return True


async def _check_can_post(client: httpx.AsyncClient, chat_id: int) -> tuple[bool, str]:
    """Проверяет, что бот — администратор канала и может публиковать сообщения."""
    me = (await client.get(_api("getMe"))).json()
    bot_id = me.get("result", {}).get("id")
    resp = (
        await client.get(_api("getChatMember"), params={"chat_id": chat_id, "user_id": bot_id})
    ).json()
    if not resp.get("ok"):
        return False, str(resp.get("description", "неизвестная ошибка"))
    member = resp.get("result", {})
    status = member.get("status")
    if status != "administrator":
        return False, f"бот в канале со статусом «{status}», нужен администратор"
    if not member.get("can_post_messages", False):
        return False, "у бота нет права публиковать сообщения в канале"
    return True, "администратор с правом публикации"


async def main() -> None:
    setup_logging()
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    deadline = asyncio.get_running_loop().time() + minutes * 60

    logger.info("Жду добавления бота в канал (до %.0f мин). Опрос каждые %.0f с.",
                minutes, POLL_INTERVAL)

    seen_users: set[int] = set()
    async with httpx.AsyncClient(timeout=20.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                data = (
                    await client.get(_api("getUpdates"), params={"limit": 100, "timeout": 0})
                ).json()
            except httpx.HTTPError as exc:
                logger.warning("Ошибка опроса Telegram: %s", exc)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if not data.get("ok"):
                logger.error("Telegram вернул ошибку: %s", data.get("description"))
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for update in data.get("result", []):
                # Личное сообщение — запоминаем user id владельца для ALLOWED_USER_IDS.
                message = update.get("message") or update.get("edited_message") or {}
                sender = (message.get("from") or {})
                if sender.get("id") and message.get("chat", {}).get("type") == "private":
                    uid = int(sender["id"])
                    if uid not in seen_users:
                        seen_users.add(uid)
                        logger.info("Обнаружен пользователь: id=%s username=@%s",
                                    uid, sender.get("username") or "—")

                # Бота добавили в канал либо в канале появился пост.
                chat = {}
                if "my_chat_member" in update:
                    chat = update["my_chat_member"].get("chat", {})
                elif "channel_post" in update:
                    chat = update["channel_post"].get("chat", {})
                if chat.get("type") != "channel":
                    continue

                chat_id = int(chat["id"])
                title = chat.get("title") or "без названия"
                ok, reason = await _check_can_post(client, chat_id)
                if not ok:
                    logger.warning("Канал «%s» (%s) не подходит: %s", title, chat_id, reason)
                    continue

                logger.info("Найден канал-хранилище: «%s», id=%s (%s)", title, chat_id, reason)
                if _write_env("STORAGE_CHANNEL_ID", str(chat_id)):
                    logger.info("STORAGE_CHANNEL_ID=%s записан в .env", chat_id)
                if seen_users:
                    logger.info("Подсказка: ALLOWED_USER_IDS=%s",
                                ",".join(str(u) for u in sorted(seen_users)))
                return

            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("Канал так и не появился за отведённое время.")
    if seen_users:
        logger.info("Замеченные пользователи: %s", sorted(seen_users))


if __name__ == "__main__":
    asyncio.run(main())
