"""Редактирование списка вузов прямо из бота: добавить, поправить, удалить."""

import logging
import re

from app.bot import Callback, Ctx, Message
from app.universities import UniversityRegistry, normalize
from app.vk import Keyboard

logger = logging.getLogger(__name__)

# 4 вуза + перелистывание + «добавить/создать беседы» + «назад» = 9 кнопок, предел VK — 10
PAGE = 4

ADD, FIELD = "un:add", "un:field"

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sch", "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ъ": "", "ь": "",
})


def make_key(title: str, taken: set[str]) -> str:
    base = normalize(title).replace(" ", "_").translate(TRANSLIT)
    base = re.sub(r"[^a-z0-9_]", "", base)[:24] or "uni"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}{n}", n + 1
    return key


async def _reload(ctx: Ctx) -> None:
    ctx.registry.apply_rows(await ctx.db.all_universities())


# ───────────────────────── список ─────────────────────────


def _list_screen(registry: UniversityRegistry, page: int) -> tuple[str, Keyboard]:
    items = registry.items
    pages = max(1, -(-len(items) // PAGE))
    page %= pages

    kb = Keyboard()
    for uni in items[page * PAGE:(page + 1) * PAGE]:
        mark = "" if uni.fallback_link else "⚠️ "
        kb.button(f"{mark}{uni.title}", f"un:e:{uni.key}")
    kb.adjust(2)

    if pages > 1:
        kb.row(Keyboard.btn("‹", f"un:p:{page - 1}"), Keyboard.btn("›", f"un:p:{page + 1}"))
    kb.row(Keyboard.btn("➕ Добавить вуз", "un:add"),
           Keyboard.btn("🏗 Создать беседы", "adm:mkchats"))
    kb.row(Keyboard.btn("⬅️ В админку", "adm:home"))

    paging = f", страница {page + 1} из {pages}" if pages > 1 else ""
    text = (
        f"🎓 Вузы — {len(items)} шт.{paging}\n\n"
        f"Нажми на вуз, чтобы поправить название, ссылку или алиасы.\n"
        f"⚠️ — у вуза нет запасной ссылки на беседу."
    )
    return text, kb


# ───────────────────────── карточка вуза ─────────────────────────


def _card(registry: UniversityRegistry, key: str) -> tuple[str, Keyboard]:
    uni = registry.get(key)
    if uni is None:
        return "Вуз не найден.", _list_screen(registry, 0)[1]

    kb = Keyboard()
    kb.button("✏️ Название", f"un:f:title:{key}")
    kb.button("🔗 Ссылка", f"un:f:link:{key}")
    kb.button("🏷 Алиасы", f"un:f:aliases:{key}")
    kb.button("🗑 Удалить", f"un:del:{key}")
    kb.button("⬅️ К списку", "adm:edu")
    kb.adjust(2, 2, 1)

    text = (
        f"🎓 {uni.title}\n\n"
        f"Ключ: {uni.key}\n"
        f"Ссылка: {uni.fallback_link or '—'}\n"
        f"Алиасы: {', '.join(uni.aliases) or '—'}"
    )
    return text, kb


# ───────────────────────── правка поля ─────────────────────────

PROMPTS = {
    "title": "✏️ Пришли новое название вуза.",
    "link": "🔗 Пришли новую ссылку на беседу (или «-», чтобы убрать).",
    "aliases": "🏷 Пришли алиасы через запятую — как вуз могут написать студенты.",
}


async def on_field(ctx: Ctx, message: Message) -> None:
    form = ctx.form(message.from_id)
    data = form.data
    uni = ctx.registry.get(data["key"])
    if uni is None:
        form.clear()
        await ctx.reply(message.peer_id, "Вуз куда-то делся, открой список заново.",
                        Keyboard().button("⬅️ К списку вузов", "adm:edu"))
        return

    value = message.text.strip()
    title, link, aliases = uni.title, uni.fallback_link, list(uni.aliases)

    if data["field"] == "title":
        title = value
    elif data["field"] == "link":
        link = None if value in {"-", "—"} else value
    else:
        aliases = [a.strip() for a in value.split(",") if a.strip()]

    await ctx.db.upsert_university(uni.key, title, link, aliases)
    await _reload(ctx)
    form.clear()
    await ctx.reply(message.peer_id, *_card(ctx.registry, uni.key))


# ───────────────────────── добавление ─────────────────────────


async def on_add(ctx: Ctx, message: Message) -> None:
    registry = ctx.registry
    parts = [p.strip() for p in message.text.split("|")]
    title = parts[0] if parts else ""
    if not title:
        await ctx.reply(message.peer_id, "Нужно хотя бы название. Пришли ещё раз.",
                        Keyboard().button("⬅️ Отмена", "adm:edu"))
        return

    link = parts[1] if len(parts) > 1 and parts[1] else None
    aliases = [a.strip() for a in parts[2].split(",") if a.strip()] if len(parts) > 2 else []

    if any(u.title.lower() == title.lower() for u in registry.items):
        await ctx.reply(message.peer_id, "Такой вуз уже есть — открой его в списке и поправь.",
                        Keyboard().button("⬅️ К списку вузов", "adm:edu"))
        return

    key = make_key(title, {u.key for u in registry.items})
    await ctx.db.upsert_university(key, title, link, aliases)
    await _reload(ctx)
    ctx.form(message.from_id).clear()
    text, markup = _card(registry, key)
    await ctx.reply(message.peer_id, f"✅ Добавил {title} (ключ {key}).\n\n{text}", markup)


# ───────────────────────── маршруты ─────────────────────────


async def on_message(ctx: Ctx, message: Message, state: str) -> None:
    if state == ADD:
        await on_add(ctx, message)
    elif state == FIELD:
        await on_field(ctx, message)


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    data = cb.data
    registry = ctx.registry
    form = ctx.form(cb.user_id)

    if data == "adm:edu":
        form.clear()
        await ctx.edit(cb, *_list_screen(registry, 0))
    elif data.startswith("un:p:"):
        await ctx.edit(cb, *_list_screen(registry, int(data.split(":")[2])))
    elif data.startswith("un:e:"):
        form.clear()
        await ctx.edit(cb, *_card(registry, data.split(":", 2)[2]))
    elif data.startswith("un:f:"):
        _, _, field, key = data.split(":", 3)
        form.clear()
        form.set_state(FIELD)
        form.update(field=field, key=key)
        await ctx.edit(cb, PROMPTS[field], Keyboard().button("⬅️ Отмена", f"un:e:{key}"))
    elif data == "un:add":
        form.clear()
        form.set_state(ADD)
        await ctx.edit(
            cb,
            "➕ Новый вуз\n\n"
            "Пришли одной строкой: Название | ссылка | алиасы через запятую\n\n"
            "Например:\n"
            "СПбГУВМ | https://vk.me/join/abc123 | ветеринарка, вет\n\n"
            "Ссылку и алиасы можно не указывать — допишешь потом.",
            Keyboard().button("⬅️ Отмена", "adm:edu"),
        )
    elif data.startswith("un:delok:"):
        await ctx.db.delete_university(data.split(":", 2)[2])
        await _reload(ctx)
        await ctx.answer(cb)
        await ctx.edit(cb, *_list_screen(registry, 0))
        return
    elif data.startswith("un:del:"):
        key = data.split(":", 2)[2]
        uni = registry.get(key)
        kb = Keyboard()
        kb.button("🗑 Да, удалить", f"un:delok:{key}")
        kb.button("⬅️ Не надо", f"un:e:{key}")
        kb.adjust(1)
        await ctx.edit(
            cb,
            f"Удалить {uni.title if uni else key} из списка?\n\n"
            f"Статистика и уже выданные ссылки останутся в базе, но студенты "
            f"перестанут находить этот вуз в боте.",
            kb,
        )
    await ctx.answer(cb)
