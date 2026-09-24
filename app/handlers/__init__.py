"""Разбор событий Long Poll: кому из хэндлеров отдать сообщение или нажатие кнопки."""

import logging

from app.bot import Callback, Ctx, Message
from app.handlers import admin, broadcast, people, tracking, unis, user

logger = logging.getLogger(__name__)

# кнопки организаторов: чужим — отказ, своим — в нужный раздел
ADMIN_PREFIXES = ("adm:", "bc:", "un:", "pp:", "ppcsv:")


async def dispatch(ctx: Ctx, update: dict) -> None:
    kind = update.get("type")
    obj = update.get("object") or {}
    if kind == "message_new":
        await on_message(ctx, Message.from_vk(obj.get("message") or obj))
    elif kind == "message_event":
        await on_callback(ctx, Callback.from_vk(obj))


async def on_message(ctx: Ctx, message: Message) -> None:
    if message.is_chat:
        if message.action:
            await tracking.on_action(ctx, message)
        else:
            await admin.on_chat_command(ctx, message)
        return
    if message.from_id <= 0:
        return

    message.user = await ctx.api.get_user(message.from_id)

    # нажатие кнопки постоянного меню приходит как обычное сообщение с payload
    pressed = (message.payload or {}).get("c")
    if pressed and pressed != "noop":
        await on_menu_press(ctx, message, pressed)
        return

    command = message.command

    # недозаполненная форма организатора: текст — это ответ на неё.
    # Команда посреди формы её бросает — так из неё всегда можно выйти.
    form = ctx.form(message.from_id)
    state = form.state
    if state and ctx.is_admin(message.user):
        if command is None:
            if state.startswith("bc:"):
                await broadcast.on_message(ctx, message, state)
            elif state.startswith("un:"):
                await unis.on_message(ctx, message, state)
            elif state.startswith("pp:"):
                await people.on_message(ctx, message)
            return
        form.clear()

    if command and await admin.on_private_command(ctx, message, *command):
        return
    await user.on_message(ctx, message)


async def on_menu_press(ctx: Ctx, message: Message, action: str) -> None:
    """Кнопка постоянного меню: те же экраны, что и по командам."""
    await user.remember(ctx, message.user)
    if action.startswith("adm:"):
        if ctx.is_admin(message.user):
            await admin.cmd_admin(ctx, message)
        return
    if action == "list":
        await user.cmd_list(ctx, message)
    elif action.startswith("stats"):
        await user.cmd_stats(ctx, message)
    elif action == "help":
        await user.cmd_help(ctx, message)
    else:
        await user.on_message(ctx, message)


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    cb.user = await ctx.api.get_user(cb.user_id)
    data = cb.data

    if data.startswith(ADMIN_PREFIXES):
        if not ctx.is_admin(cb.user):
            await ctx.answer(cb, "Раздел только для организаторов")
            return
        if data == "adm:bc" or data.startswith("bc:"):
            await broadcast.on_callback(ctx, cb)
        elif data == "adm:edu" or data.startswith("un:"):
            await unis.on_callback(ctx, cb)
        elif data == "adm:people" or data.startswith(("pp:", "ppcsv:")):
            await people.on_callback(ctx, cb)
        else:
            await admin.on_callback(ctx, cb)
        return

    await user.on_callback(ctx, cb)
