"""Подтягивание существующих диалогов сообщества в таблицу users.

У сообщества обычно уже есть переписка с тысячами людей: они писали в личку,
но бота не запускали и личную ссылку не брали. Писать им можно (диалог открыт),
поэтому для рассылок они нужны в базе — с нулём приглашённых и пометкой «из диалогов».
"""

import asyncio
import logging
import os

from app.db import Database
from app.vk import VkApi, VkApiError

logger = logging.getLogger(__name__)

PAGE = 200
PAUSE = 0.06  # 20 запросов в секунду — лимит сообщества
# как часто перечитывать диалоги: новые собеседники должны попадать в рассылки сами
SYNC_HOURS = float(os.environ.get("DIALOG_SYNC_HOURS", "6"))
RETRY_DELAY = 600.0   # после сбоя перечитываем не через шесть часов, а через десять минут
PAGE_TRIES = 4        # VK на больших списках иногда отвечает «Internal server error»


def _name(profile: dict) -> str:
    return " ".join(p for p in (profile.get("first_name", ""), profile.get("last_name", "")) if p)


def _screen_name(profile: dict) -> str | None:
    name = profile.get("screen_name")
    return None if not name or name.startswith("id") else name


async def import_conversations(api: VkApi, db: Database, progress=None) -> dict:
    """Проходит все диалоги сообщества и сохраняет собеседников. Возвращает сводку.

    У больших сообществ диалогов десятки тысяч, и VK отдаёт их небыстро — поэтому
    вызывать это стоит только в фоне и с отчётом о прогрессе.
    """
    offset, total, saved, blocked, page, lost = 0, 0, 0, 0, 0, 0
    logger.info("Импорт диалогов начался")
    while True:
        response = await _page(api, offset)
        if response is None:
            # VK не отдал эту страницу даже с повторами — пропускаем её и идём дальше,
            # иначе один сбой посреди списка обнуляет весь проход
            logger.warning("Импорт диалогов: пропускаю страницу с %s", offset)
            lost += PAGE
            offset += PAGE
            if total and offset >= total:
                break
            continue
        items = response.get("items") or []
        total = response.get("count", 0)
        profiles = {p["id"]: p for p in response.get("profiles") or []}

        rows: list[tuple[int, str | None, str, bool]] = []
        for item in items:
            peer = item["conversation"]["peer"]
            if peer.get("type") != "user" or peer["id"] <= 0:
                continue  # беседы и сообщения от сообществ здесь не нужны
            profile = profiles.get(peer["id"], {})
            can_write = bool((item["conversation"].get("can_write") or {}).get("allowed", True))
            blocked += not can_write
            rows.append((peer["id"], _screen_name(profile), _name(profile), can_write))

        saved += await db.import_users(rows)
        offset += len(items)
        page += 1
        if page % 10 == 0:
            logger.info("Импорт диалогов: %s из %s", offset, total)
            if progress is not None:
                await progress(offset, total)
        # короткая страница посреди списка — не повод считать, что диалоги кончились
        if not items or (offset >= total and total):
            break
        await asyncio.sleep(PAUSE)

    logger.info("Импорт диалогов: просмотрено %s, сохранено %s, запретили сообщения %s, "
                "не отдал VK %s", offset, saved, blocked, lost)
    return {"seen": offset, "saved": saved, "blocked": blocked, "total": total, "lost": lost}


async def _page(api: VkApi, offset: int) -> dict | None:
    """Одна страница диалогов. VK временами отвечает ошибкой — пробуем ещё раз."""
    for attempt in range(PAGE_TRIES):
        try:
            return await api.call(
                "messages.getConversations",
                count=PAGE, offset=offset, extended=True, fields="screen_name",
            )
        except VkApiError as err:
            if err.code not in (1, 10):   # «неизвестная» и «внутренняя» ошибки VK — временные
                raise
            logger.info("Импорт диалогов: VK ответил «%s» на %s, повтор", err.message, offset)
            await asyncio.sleep(2 * (attempt + 1))
    return None


async def run_dialog_sync(api: VkApi, db: Database) -> None:
    """Фоновое обновление: кто написал сообществу — тот сразу в базе и в рассылках."""
    if SYNC_HOURS <= 0:
        return
    while True:
        delay = SYNC_HOURS * 3600
        try:
            result = await import_conversations(api, db)
            logger.info("Синхронизация диалогов: %s собеседников из %s диалогов",
                        result["saved"], result["seen"])
        except asyncio.CancelledError:
            raise
        except VkApiError as err:
            logger.warning("Не смог перечитать диалоги сообщества: %s", err)
            delay = RETRY_DELAY
        except Exception:  # noqa: BLE001 — фоновая задача не должна ронять бота
            logger.exception("Синхронизация диалогов сорвалась")
            delay = RETRY_DELAY
        await asyncio.sleep(delay)
