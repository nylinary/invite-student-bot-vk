"""Учёт вступлений в беседы вузов и появление бота в беседах.

VK сообщает о составе беседы служебными сообщениями (message.action):
- chat_invite_user_by_link — человек вступил по ссылке-приглашению;
- chat_invite_user — человека добавил участник (или он сам вернулся);
- chat_kick_user — человек вышел или его исключили.

По какой ссылке вступили, VK не говорит — у беседы одна общая ссылка. Поэтому
приглашённый засчитывается тому, чью личную ссылку на бота (vk.me/...?ref=...)
человек открыл перед вступлением, а при добавлении в беседу вручную — тому, кто добавил.
"""

import logging

from app import keyboards as kb
from app import texts
from app.bot import Ctx, Message
from app.vk import VkApiError

logger = logging.getLogger(__name__)

JOIN_ACTIONS = {"chat_invite_user", "chat_invite_user_by_link", "chat_invite_user_by_message_request"}
LEAVE_ACTIONS = {"chat_kick_user"}


async def on_action(ctx: Ctx, message: Message) -> None:
    action = message.action or {}
    kind = action.get("type")
    member_id = action.get("member_id") or message.from_id

    if kind in JOIN_ACTIONS:
        if member_id == -ctx.api.group_id:
            await on_bot_added(ctx, message.peer_id, message.from_id)
        elif member_id > 0:
            # добавил другой участник — приглашение его; сам вернулся или по ссылке — смотрим ref
            adder = message.from_id if (
                kind == "chat_invite_user" and 0 < message.from_id != member_id
            ) else None
            await on_user_join(ctx, message.peer_id, member_id, adder)
    elif kind in LEAVE_ACTIONS:
        if member_id == -ctx.api.group_id:
            await on_bot_removed(ctx, message.peer_id)
        elif member_id > 0:
            await ctx.db.record_leave(message.peer_id, member_id)


async def on_user_join(ctx: Ctx, peer_id: int, user_id: int, adder: int | None) -> None:
    db, registry = ctx.db, ctx.registry
    chat_row = await db.get_chat_by_id(peer_id)
    key = chat_row["university_key"] if chat_row else None

    owner_id, link = None, None
    if adder:
        owner_id = adder
    elif key is not None:
        ref = await db.get_referral(user_id)
        if ref is not None and ref["university_key"] == key:
            owner_id, link = ref["owner_id"], ref["link"]

    joiner = await ctx.api.get_user(user_id)
    await db.record_join(
        chat_id=peer_id,
        user_id=user_id,
        username=joiner.username,
        full_name=joiner.full_name,
        university_key=key,
        link=link,
        owner_id=owner_id,
    )
    logger.info("join chat=%s user=%s link=%s owner=%s", peer_id, user_id, link, owner_id)

    if owner_id and owner_id != user_id:
        # массовый залив по одной ссылке — купленные аккаунты, в зачёт не идут
        had_flags = await db.flagged_count(owner_id) > 0
        if await db.flag_bursts(owner_id) and not had_flags:
            await _alert_fraud(ctx, owner_id, registry.title(key))
        if await db.is_suspicious(peer_id, user_id):
            return  # за такие вступления не поздравляем

        _, active = await db.owner_stats(owner_id, key)
        await ctx.safe_send(
            owner_id,
            f"🎉 По твоей ссылке в беседу {registry.title(key)} вступил(а) "
            f"{texts.who(user_id, joiner.username, joiner.full_name)}.\n"
            f"Засчитано приглашённых: {active}",
        )


async def on_bot_added(ctx: Ctx, peer_id: int, added_by: int) -> None:
    """Бота добавили в беседу: если она вузовская — привязываем и здороваемся."""
    db, registry = ctx.db, ctx.registry

    existing = await db.get_chat_by_id(peer_id)
    if existing is not None:
        # бота убирали и вернули: беседу уже знаем, но участникам стоит напомнить
        uni = registry.get(existing["university_key"])
        if uni is not None:
            logger.info("Бота вернули в беседу %s (%s)", peer_id, uni.key)
            await announce_in_chat(ctx, peer_id, uni.key)
        return

    try:
        title = await ctx.api.chat_title(peer_id) or ""
    except VkApiError as err:
        logger.info("Не смог узнать название беседы %s: %s", peer_id, err)
        title = ""

    matches = registry.match(title) if title else []
    if len(matches) != 1:
        # Чужая беседа: бота могли добавить куда угодно, в том числе по ошибке.
        # Молча сидим и ничего не пишем — вдруг это беседа под другую задачу.
        logger.info("Добавили в беседу %s (%r) — вуз по названию не определился, молчу",
                    peer_id, title)
        # В саму беседу не пишем (вдруг она вообще не наша), но организатор должен узнать:
        # иначе беседа тихо останется без привязки и без персональных ссылок.
        keys = ", ".join(u.key for u in registry.items[:6])
        await notify_admins(
            ctx,
            f"🤔 Меня добавили в беседу {('«' + title + '»') if title else 'без названия'} "
            f"(id {peer_id}), но по названию я не понял, чей это вуз — поэтому там промолчал.\n\n"
            f"Сначала назначь меня в ней администратором. Дальше — одно из двух:\n\n"
            f"• беседа вуза: /bind {peer_id} <ключ> (ключи: {keys}…)\n"
            f"• общая беседа без вуза: /bindchat {peer_id} <название>\n\n"
            f"Эти команды можно отправить прямо сюда, в личку — id беседы я уже подставил. "
            f"Или зайти в саму беседу и написать там /bind <ключ> без id.",
        )
        return

    uni = matches[0]
    try:
        await ctx.api.invite_link(peer_id)
    except VkApiError:
        # в VK бота добавляют обычным участником, админом его делают отдельно
        await ctx.safe_send(peer_id, texts.no_invite_rights(uni.key))
        return

    await db.bind_chat(uni.key, peer_id, title, added_by)
    logger.info("Беседа %s привязана к вузу %s", peer_id, uni.key)

    from app.chats import decorate_chat

    await decorate_chat(ctx, peer_id, uni)   # ава и закреплённое описание
    await announce_in_chat(ctx, peer_id, uni.key)


async def on_bot_removed(ctx: Ctx, peer_id: int) -> None:
    """Бота исключили из беседы — вступления туда больше не отследить."""
    dropped = await ctx.db.drop_invite_links(peer_id)
    if dropped:
        logger.warning(
            "Бота исключили из беседы %s — выбросил %d персональных ссылок", peer_id, dropped
        )


async def _alert_fraud(ctx: Ctx, owner_id: int, university: str) -> None:
    owner = await ctx.db.pool.fetchrow(
        "SELECT username, full_name FROM users WHERE user_id = $1", owner_id
    )
    name = texts.who(owner_id, owner["username"] if owner else None,
                     owner["full_name"] if owner else None)
    flagged = await ctx.db.flagged_count(owner_id)
    logger.warning("Похоже на накрутку: %s, пометил %s", owner_id, flagged)
    await notify_admins(
        ctx,
        f"🚨 Похоже на накрутку\n\n"
        f"{name} ({university}): по его ссылке {flagged} аккаунтов вступили в беседу "
        f"за считанные секунды — так приходят купленные аккаунты, а не друзья.\n\n"
        f"Такие вступления не засчитываются. Если это ошибка — "
        f"/admin → «🚨 Накрутка».",
    )


async def notify_admins(ctx: Ctx, text: str) -> None:
    """Личка организаторам: то, что важно знать, но не стоит писать в беседу."""
    config = ctx.config
    ids = set(config.admin_ids) | set(await ctx.db.admin_user_ids(config.admin_usernames))
    for admin_id in ids:
        await ctx.safe_send(admin_id, text)


async def announce_in_plain_chat(ctx: Ctx, peer_id: int) -> bool:
    """Беседа без вуза: рассказывать про ссылки вуза незачем, зовём в бота за своей."""
    return await ctx.safe_send(
        peer_id,
        "❗ Информация по билетам и приглашениям\n\n"
        "Напишите боту сообщества в личные сообщения и укажите свой вуз — он выдаст "
        "индивидуальную ссылку-приглашение.\n\n"
        "🎫 5 приглашённых — бесплатный входной билет\n"
        "🎟 25 приглашённых — VIP-билет\n"
        "🎪 50 и больше — сцена, backstage и фото с артистами",
        kb.chat_welcome_kb(ctx.api.dialog_url()),
    )


async def announce_in_chat(ctx: Ctx, peer_id: int, key: str) -> None:
    """Объясняем участникам беседы, что делать: идти в бота и брать свою ссылку."""
    title = ctx.registry.title(key)
    dialog = ctx.api.dialog_url(f"uni_{key}")
    if not await ctx.safe_send(peer_id, texts.chat_welcome(title, ctx.api.dialog_url()),
                               kb.chat_welcome_kb(dialog)):
        logger.warning("Не смог написать в беседу %s", peer_id)
