"""Сверка состава чатов: кто вступил, кто вышел.

Служебные сообщения о вступлениях VK присылает не всегда — в больших чатах их
может не быть вовсе, и тогда ждать их бессмысленно. Поэтому там, где у бота есть
доступ к списку участников, он сверяет список с базой сам: появился человек —
засчитываем приглашение тому, по чьей ссылке он приходил, пропал — отмечаем выход.

Это надёжнее событий ещё и потому, что переживает перезапуск: пока бота не было,
люди вступали, и первый же проход их подберёт.
"""

import asyncio
import logging
import os

from app.bot import Ctx
from app.vk import VkApiError

logger = logging.getLogger(__name__)

SYNC_INTERVAL = float(os.environ.get("MEMBERS_SYNC_SECONDS", "60"))
PAGE = 200          # столько участников VK отдаёт за один запрос
PAUSE = 0.1


async def chat_member_ids(ctx: Ctx, peer_id: int) -> set[int] | None:
    """Все люди в чате. None — списка не видно (нет прав)."""
    members: set[int] = set()
    offset = 0
    while True:
        try:
            response = await ctx.api.call(
                "messages.getConversationMembers",
                peer_id=peer_id, group_id=ctx.api.group_id, offset=offset, count=PAGE,
            )
        except VkApiError:
            return None
        items = response.get("items") or []
        members.update(i["member_id"] for i in items if i.get("member_id", 0) > 0)
        offset += len(items)
        if len(items) < PAGE or offset >= response.get("count", 0):
            return members
        await asyncio.sleep(PAUSE)


async def sync_chat(ctx: Ctx, chat_row) -> tuple[int, int]:
    """Сверяет один чат. Возвращает (сколько засчитали, сколько отметили вышедшими)."""
    from app.handlers.tracking import on_user_join

    peer_id, key = chat_row["chat_id"], chat_row["university_key"]
    members = await chat_member_ids(ctx, peer_id)
    if members is None:
        return 0, 0

    known = await ctx.db.chat_members(peer_id)          # {user_id: вышел ли}
    joined = [uid for uid in members if known.get(uid, True)]   # новый или вернулся
    left = [uid for uid, gone in known.items() if not gone and uid not in members]

    for user_id in joined:
        # тот же путь, что и для служебного сообщения: реферал, накрутка, уведомление
        await on_user_join(ctx, peer_id, user_id, adder=None)
        await asyncio.sleep(PAUSE)

    for user_id in left:
        await ctx.db.record_leave(peer_id, user_id)

    if joined or left:
        logger.info("Сверка чата %s (%s): засчитано %s, вышли %s",
                    peer_id, key, len(joined), len(left))
    return len(joined), len(left)


async def run_members_sync(ctx: Ctx) -> None:
    """Фоновая сверка всех чатов, до списков которых бот дотягивается."""
    if SYNC_INTERVAL <= 0:
        return
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            for chat_row in await ctx.db.all_chats():
                await sync_chat(ctx, chat_row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фоновая задача не должна ронять бота
            logger.exception("Сверка составов чатов сорвалась, продолжаю")
