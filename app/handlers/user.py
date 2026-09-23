"""Личка студента: вуз -> беседа и личная ссылка, статистика, билет."""

import logging

from app import keyboards as kb
from app import texts
from app.bot import Callback, Ctx, Message
from app.services import get_or_create_invite, parse_ref
from app.tickets import tier_for, ticket_code
from app.vk import VkUser

logger = logging.getLogger(__name__)

START_WORDS = {"начать", "start", "старт"}


async def _tickets_on(ctx: Ctx) -> bool:
    """Кнопка «Получить билет» включается организатором из админки."""
    return await ctx.db.get_setting("tickets", "0") == "1"


async def remember(ctx: Ctx, user: VkUser) -> None:
    await ctx.db.upsert_user(user.id, user.username, user.full_name)


async def _send_invite(ctx: Ctx, peer_id: int, user: VkUser, key: str,
                       cb: Callback | None = None) -> None:
    uni = ctx.registry.get(key)
    if uni is None:
        await ctx.reply(peer_id, texts.NOT_FOUND.format(organizer=ctx.config.organizer),
                        kb.back_kb())
        return

    await ctx.db.set_user_university(user.id, uni.key)
    result = await get_or_create_invite(ctx.api, ctx.db, user, uni)

    if result.link is None:
        text = texts.NO_CHAT_AT_ALL.format(title=uni.title, organizer=ctx.config.organizer)
    elif result.personal:
        _, active = await ctx.db.owner_stats(user.id, uni.key)
        text = texts.invite_message(uni.title, result.link, result.ref_link, active)
    else:
        text = texts.fallback_message(uni.title, result.link, ctx.config.organizer)

    markup = kb.invite_kb(result.link, result.ref_link, uni.key, await _tickets_on(ctx))
    if cb is not None:
        await ctx.edit(cb, text, markup)
    else:
        await ctx.reply(peer_id, text, markup)


async def _take_ref(ctx: Ctx, message: Message) -> str | None:
    """Человек пришёл по ссылке vk.me/...?ref=... Возвращает вуз, который ему сразу показать.

    ref=uni_<ключ> — кнопка из беседы вуза; ref=r<id>_<ключ> — чужая личная ссылка:
    запоминаем, кто позвал, и засчитаем ему, когда человек вступит в беседу.
    """
    ref = (message.ref or "").strip()
    if ref.startswith("uni_"):
        return ref[4:]
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    owner_id, key = parsed
    link = await ctx.db.get_invite_link(owner_id, key)
    # ссылку должен был выдать бот: подделанный ref никому ничего не засчитает
    if link is not None and owner_id != message.from_id:
        await ctx.db.save_referral(message.from_id, owner_id, key, link["link"])
        logger.info("ref user=%s owner=%s uni=%s", message.from_id, owner_id, key)
    return key


async def cmd_start(ctx: Ctx, message: Message) -> None:
    await ctx.reply(message.peer_id, texts.GREETING, kb.greeting_kb(ctx.is_admin(message.user)))


async def cmd_help(ctx: Ctx, message: Message) -> None:
    key = await ctx.db.get_user_university(message.from_id)
    await ctx.reply(message.peer_id, texts.HELP.format(organizer=ctx.config.organizer),
                    kb.student_kb(key, await _tickets_on(ctx)))


def _list_title(ctx: Ctx, page: int) -> str:
    pages = kb.total_pages(ctx.registry.items)
    tail = f" (страница {page % pages + 1} из {pages})" if pages > 1 else ""
    return f"Выбери свой вуз{tail}:\n\nМожно просто написать название — так быстрее."


async def cmd_list(ctx: Ctx, message: Message) -> None:
    await ctx.reply(message.peer_id, _list_title(ctx, 0), kb.universities_kb(ctx.registry.items))


async def cmd_stats(ctx: Ctx, message: Message) -> None:
    await _show_stats(ctx, message.peer_id, message.user, None)


async def _show_stats(ctx: Ctx, peer_id: int, user: VkUser, key: str | None,
                      cb: Callback | None = None) -> None:
    if key is None:
        key = await ctx.db.get_user_university(user.id)

    total, active = await ctx.db.owner_stats(user.id, key)
    flagged = await ctx.db.flagged_count(user.id, key)
    link_row = await ctx.db.get_invite_link(user.id, key) if key else None
    ref_link = link_row["link"] if link_row else None
    text = texts.stats_message(ctx.registry.title(key), ref_link, total, active, flagged)
    markup = kb.invite_kb(None, ref_link, key or "", await _tickets_on(ctx))
    if cb is not None:
        await ctx.edit(cb, text, markup)
    else:
        await ctx.reply(peer_id, text, markup)


async def on_message(ctx: Ctx, message: Message) -> None:
    """Всё, что пишут боту в личку и что не забрали админские формы."""
    await remember(ctx, message.user)

    uni_key = await _take_ref(ctx, message) if message.ref else None
    if uni_key and ctx.registry.get(uni_key):
        await _send_invite(ctx, message.peer_id, message.user, uni_key)
        return

    command = message.command
    is_start = (message.payload or {}).get("command") == "start" \
        or message.text.strip().lower() in START_WORDS
    if is_start or (command and command[0] == "start"):
        await cmd_start(ctx, message)
        return
    if command:
        handler = {"help": cmd_help, "list": cmd_list, "stats": cmd_stats}.get(command[0])
        if handler is not None:
            await handler(ctx, message)
            return

    if message.text.strip():
        await on_text(ctx, message)
    else:
        # стикер, фото, голосовое — молчать нельзя, подсказываем
        await ctx.reply(message.peer_id, texts.ONLY_UNIVERSITY_NAME, kb.back_kb())


async def on_text(ctx: Ctx, message: Message) -> None:
    matches = ctx.registry.match(message.text)

    if not matches:
        await ctx.reply(message.peer_id, texts.NOT_FOUND.format(organizer=ctx.config.organizer),
                        kb.back_kb())
        return

    if len(matches) == 1:
        await _send_invite(ctx, message.peer_id, message.user, matches[0].key)
        return

    await ctx.reply(message.peer_id, texts.AMBIGUOUS, kb.suggestions_kb(matches))


# ───────────────────────── кнопки ─────────────────────────


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    data = cb.data
    if data == "noop":
        await ctx.answer(cb)
        return

    if data.startswith("list"):
        _, _, raw_page = data.partition(":")
        try:
            page = int(raw_page)
        except ValueError:
            page = 0
        await ctx.edit(cb, _list_title(ctx, page), kb.universities_kb(ctx.registry.items, page))
        await ctx.answer(cb)
        return

    if data.startswith("uni:"):
        await remember(ctx, cb.user)
        await _send_invite(ctx, cb.peer_id, cb.user, data.split(":", 1)[1], cb)
        await ctx.answer(cb)
        return

    if data.startswith("stats:"):
        await _show_stats(ctx, cb.peer_id, cb.user, data.split(":", 1)[1] or None, cb)
        await ctx.answer(cb)
        return

    if data == "ticket":
        await cb_ticket(ctx, cb)
        return

    await ctx.answer(cb)


async def cb_ticket(ctx: Ctx, cb: Callback) -> None:
    """Считаем приглашённых и выдаём билет того уровня, который человек заработал."""
    user = cb.user
    await remember(ctx, user)

    if not await _tickets_on(ctx):
        await ctx.answer(cb)
        await ctx.reply(cb.peer_id, texts.TICKETS_OFF, kb.student_kb(
            await ctx.db.get_user_university(user.id)))
        return

    key = await ctx.db.get_user_university(user.id)
    total, active = await ctx.db.owner_stats(user.id, key)
    with_links = await ctx.db.invited_with_own_links(user.id)
    earned = tier_for(active)

    if earned is None:
        await ctx.reply(cb.peer_id, texts.ticket_progress(active, with_links),
                        kb.student_kb(key, tickets=True))
        await ctx.answer(cb)
        return

    need, name, perks = earned
    existing = await ctx.db.get_ticket(user.id)
    code = existing["code"] if existing else ticket_code(user.id, ctx.config.vk_token)
    await ctx.db.issue_ticket(user.id, code, name, active, key)

    holder = f"@{user.username}" if user.username else user.full_name
    await ctx.reply(
        cb.peer_id, texts.ticket(code, name, perks, active, ctx.registry.title(key), holder),
        kb.student_kb(key),
    )
    await ctx.answer(cb, "Билет у тебя 🎫")
