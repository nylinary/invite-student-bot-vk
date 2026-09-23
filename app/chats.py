"""Создание бесед вузов силами самого сообщества.

VK разрешает `messages.createChat` с ключом сообщества, и созданную беседу сообщество
заводит как владелец — значит права у бота есть сразу: ссылка-приглашение, список
участников, учёт вступлений. Ручное «назначьте меня администратором» не нужно.
"""

import asyncio
import logging
import os

from app.bot import Ctx
from app.universities import University
from app.vk import CHAT_PEER_OFFSET, FLOOD_CONTROL, VkApiError

logger = logging.getLogger(__name__)

# как называть созданные беседы; {title} — название вуза
CHAT_TITLE = os.environ.get("CHAT_TITLE", "{title} | НОЧЬ СТУДЕНТА")
# VK включает флуд-контроль, если беседы создавать подряд: между ними держим паузу,
# а на «Flood control» ждём дольше и пробуем тот же вуз ещё раз
PAUSE = float(os.environ.get("CHAT_CREATE_PAUSE", "20"))
FLOOD_PAUSE = float(os.environ.get("CHAT_FLOOD_PAUSE", "120"))
FLOOD_TRIES = 4


def chat_title(uni: University) -> str:
    return CHAT_TITLE.format(title=uni.title)[:100]


async def create_uni_chat(ctx: Ctx, uni: University, member_id: int | None) -> dict:
    """Создаёт беседу вуза, привязывает её и публикует объяснение для студентов."""
    from app.handlers.tracking import announce_in_chat

    title = chat_title(uni)
    members = [member_id] if member_id else []
    try:
        response = await ctx.api.call(
            "messages.createChat", title=title, user_ids=members, group_id=ctx.api.group_id
        )
    except VkApiError as err:
        # человека могло не оказаться среди тех, кому сообщество вправе писать
        if not members:
            raise
        logger.warning("Беседа для %s: без участника (%s)", uni.key, err)
        response = await ctx.api.call(
            "messages.createChat", title=title, group_id=ctx.api.group_id
        )

    chat_id = response["chat_id"] if isinstance(response, dict) else response
    peer_id = CHAT_PEER_OFFSET + int(chat_id)

    # «описание» беседы: у VK его нет, поэтому закрепляем сообщение в шапке
    from app import texts
    try:
        # шлём «пачкой из одного»: так VK возвращает conversation_message_id, нужный для закрепа
        items = await ctx.api.send_many(
            [peer_id], texts.chat_promo(uni.title, ctx.config.organizer, ctx.config.event_url)
        )
        cmid = items[0].get("conversation_message_id") if items else None
        if cmid:
            await ctx.api.pin(peer_id, int(cmid))
    except (VkApiError, TypeError, ValueError, KeyError, IndexError) as err:
        logger.warning("Беседа %s: не закрепил описание (%s)", uni.key, err)

    link = await ctx.api.invite_link(peer_id)
    await ctx.db.bind_chat(uni.key, peer_id, title, member_id or 0)
    # ссылку кладём и в карточку вуза: пригодится как запасная и видна организатору
    await ctx.db.upsert_university(uni.key, uni.title, link, list(uni.aliases))
    ctx.registry.apply_rows(await ctx.db.all_universities())
    await announce_in_chat(ctx, peer_id, uni.key)

    logger.info("Беседа вуза %s создана: %s (%s)", uni.key, peer_id, link)
    return {"key": uni.key, "title": title, "peer_id": peer_id, "link": link}


async def missing_chats(ctx: Ctx) -> list[University]:
    """Вузы, у которых беседы ещё нет."""
    bound = {row["university_key"] for row in await ctx.db.all_chats()}
    return [uni for uni in ctx.registry.items if uni.key not in bound]


async def create_missing_chats(ctx: Ctx, member_id: int | None, progress=None) -> dict:
    """Заводит беседы всем вузам, у которых их нет. Возвращает сводку."""
    created: list[dict] = []
    failed: list[tuple[str, str]] = []

    for uni in await missing_chats(ctx):
        for attempt in range(FLOOD_TRIES):
            try:
                created.append(await create_uni_chat(ctx, uni, member_id))
                break
            except VkApiError as err:
                if err.code == FLOOD_CONTROL and attempt < FLOOD_TRIES - 1:
                    logger.info("Флуд-контроль на %s — жду %s с", uni.key, FLOOD_PAUSE)
                    await asyncio.sleep(FLOOD_PAUSE)
                    continue
                logger.warning("Не создал беседу для %s: %s", uni.key, err)
                failed.append((uni.title, err.message))
                break
            except asyncio.CancelledError:
                raise
        if progress is not None and created and len(created) % 10 == 0:
            await progress(len(created))
        await asyncio.sleep(PAUSE)

    return {"created": created, "failed": failed}
