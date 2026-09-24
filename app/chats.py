"""Создание бесед вузов силами самого сообщества.

VK разрешает `messages.createChat` с ключом сообщества, и созданную беседу сообщество
заводит как владелец — значит права у бота есть сразу: ссылка-приглашение, список
участников, учёт вступлений. Ручное «назначьте меня администратором» не нужно.
"""

import asyncio
import logging
import os

from pathlib import Path

from app.bot import Ctx
from app.universities import University
from app.vk import CHAT_PEER_OFFSET, FLOOD_CONTROL, RECIPIENT_ERRORS, VkApiError

logger = logging.getLogger(__name__)

# как называть созданные беседы; {title} — название вуза
CHAT_TITLE = os.environ.get("CHAT_TITLE", "{title} | НОЧЬ СТУДЕНТА")
# VK включает флуд-контроль, если беседы создавать подряд: между ними держим паузу,
# а на «Flood control» ждём дольше и пробуем тот же вуз ещё раз
# Подряд VK создавать беседы не даёт: после нескольких включается флуд-контроль
# (ошибка 9) надолго. Поэтому заводим их фоновой очередью — по одной раз в несколько
# минут, с увеличением паузы на каждый отказ. Очередь переживает перезапуск: что
# осталось сделать, видно по таблице chats.
FACTORY_INTERVAL = float(os.environ.get("CHAT_FACTORY_INTERVAL", "600"))
FACTORY_MAX_WAIT = float(os.environ.get("CHAT_FACTORY_MAX_WAIT", "3600"))
FACTORY_ON = "chat_factory"        # включена ли очередь
FACTORY_BY = "chat_factory_by"     # кого добавлять в создаваемые беседы


# общая ава для всех бесед вуза; лежит рядом с кодом, чтобы не зависеть от внешних ссылок
AVATAR = Path(__file__).resolve().parent / "assets" / "chat_avatar.jpg"


def avatar_bytes() -> bytes | None:
    try:
        return AVATAR.read_bytes()
    except OSError as err:
        logger.warning("Не нашёл аву для бесед (%s)", err)
        return None


async def set_avatar(ctx: Ctx, peer_id: int, tries: int = 3) -> bool:
    """Ставит беседе общую аву мероприятия. VK временами отвечает отказом — повторяем."""
    content = avatar_bytes()
    if content is None:
        return False
    for attempt in range(tries):
        try:
            await ctx.api.set_chat_photo(peer_id, content)
            return True
        except (VkApiError, KeyError, OSError) as err:
            logger.info("Беседа %s: ава с попытки %s не встала (%s)", peer_id, attempt + 1, err)
            await asyncio.sleep(5 * (attempt + 1))
    logger.warning("Беседа %s: аву поставить не удалось", peer_id)
    return False


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
        # человека могло не оказаться среди тех, кому сообщество вправе писать;
        # на флуд-контроль и прочее это не распространяется — такие ошибки наверх
        if not members or err.code not in RECIPIENT_ERRORS:
            raise
        logger.warning("Беседа для %s: создаю без участника (%s)", uni.key, err)
        response = await ctx.api.call(
            "messages.createChat", title=title, group_id=ctx.api.group_id
        )

    chat_id = response["chat_id"] if isinstance(response, dict) else response
    peer_id = CHAT_PEER_OFFSET + int(chat_id)

    await set_avatar(ctx, peer_id)

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


async def decorate_chat(ctx: Ctx, peer_id: int, uni: University) -> str:
    """Ава и закреплённое описание для беседы, которую создал человек, а не бот."""
    from app import texts

    done = []
    if await set_avatar(ctx, peer_id, tries=2):
        done.append("ава поставлена")

    try:
        items = await ctx.api.send_many(
            [peer_id], texts.chat_promo(uni.title, ctx.config.organizer, ctx.config.event_url)
        )
        cmid = items[0].get("conversation_message_id") if items else None
        if cmid:
            await ctx.api.pin(peer_id, int(cmid))
            done.append("описание закреплено")
    except (VkApiError, TypeError, ValueError, KeyError, IndexError) as err:
        logger.warning("Беседа %s: не закрепил описание (%s)", peer_id, err)

    return ("✅ " + ", ".join(done)) if done else (
        "⚠️ Аву и закреп поставить не смог — проверь, что я администратор беседы."
    )


async def missing_chats(ctx: Ctx) -> list[University]:
    """Вузы, у которых беседы ещё нет."""
    bound = {row["university_key"] for row in await ctx.db.all_chats()}
    return [uni for uni in ctx.registry.items if uni.key not in bound]


async def start_factory(ctx: Ctx, member_id: int) -> int:
    """Включает очередь создания бесед. Возвращает, сколько их осталось завести."""
    await ctx.db.set_setting(FACTORY_ON, "1")
    await ctx.db.set_setting(FACTORY_BY, str(member_id))
    return len(await missing_chats(ctx))


async def stop_factory(ctx: Ctx) -> None:
    await ctx.db.set_setting(FACTORY_ON, "0")


async def factory_running(ctx: Ctx) -> bool:
    return await ctx.db.get_setting(FACTORY_ON, "0") == "1"


async def run_chat_factory(ctx: Ctx) -> None:
    """Фоновая очередь: по одной беседе за подход, с отступлением на флуд-контроль."""
    from app.handlers.tracking import notify_admins

    # отступление живёт между подходами: пока VK держит флуд-контроль, стучаться
    # чаще бессмысленно — каждая попытка только продлевает запрет
    wait = FACTORY_INTERVAL
    skip: set[str] = set()   # вузы, на которых VK ругается не из-за флуда

    while True:
        await asyncio.sleep(wait)
        try:
            if not await factory_running(ctx):
                continue

            left = [uni for uni in await missing_chats(ctx) if uni.key not in skip]
            if not left:
                await stop_factory(ctx)
                done = len(ctx.registry.items) - len(await missing_chats(ctx))
                await notify_admins(
                    ctx,
                    f"🏗 Беседы вузов готовы: {done} из {len(ctx.registry.items)}.\n"
                    f"Ссылки лежат в карточках вузов: «🎓 Список вузов».",
                )
                continue

            member = int(await ctx.db.get_setting(FACTORY_BY, "0")) or None
            uni = left[0]
            try:
                row = await create_uni_chat(ctx, uni, member)
                wait = FACTORY_INTERVAL      # получилось — снова идём обычным шагом
                logger.info("Очередь бесед: %s готова (%s), осталось %s",
                            uni.title, row["peer_id"], len(left) - 1)
            except VkApiError as err:
                if err.code == FLOOD_CONTROL:
                    wait = min(wait * 2, FACTORY_MAX_WAIT)
                    logger.info("Очередь бесед: флуд-контроль, следующая попытка через %.0f с",
                                wait)
                else:
                    logger.warning("Очередь бесед: %s — %s", uni.key, err)
                    skip.add(uni.key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фоновая задача не должна ронять бота
            logger.exception("Очередь бесед сорвалась, продолжаю")
