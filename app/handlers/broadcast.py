"""Рассылки: в беседы вузов и пользователям бота, сразу или по расписанию."""

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.bot import Callback, Ctx, Message
from app.handlers.admin import chat_label
from app.broadcaster import DAILY_LIMIT, deliver, resolve_targets
from app.importer import import_conversations
from app.vk import Keyboard, VkApiError

logger = logging.getLogger(__name__)

# фоновые отправки: без ссылки на задачу сборщик мусора может её убить
_running: set[asyncio.Task] = set()

MSK = ZoneInfo("Europe/Moscow")
# 4 беседы + перелистывание + «все/никого» + «дальше/отмена» = 10 кнопок, предел VK
TARGETS_PAGE = 4


def parse_invited_range(text: str) -> tuple[int | None, int | None] | None:
    """«5-25», «10+», «<5», «до 4», «от 7», «3» -> границы числа приглашённых."""
    raw = text.strip().lower().replace(" ", "")
    patterns: list[tuple[str, callable]] = [
        (r"^(\d+)[-–—](\d+)$", lambda m: (int(m[1]), int(m[2]))),
        (r"^(?:от|>=)(\d+)$|^(\d+)\+$", lambda m: (int(m[1] or m[2]), None)),
        (r"^>(\d+)$", lambda m: (int(m[1]) + 1, None)),
        (r"^(?:до|<=)(\d+)$", lambda m: (None, int(m[1]))),
        (r"^<(\d+)$", lambda m: (None, max(int(m[1]) - 1, 0))),
        (r"^(\d+)$", lambda m: (int(m[1]), int(m[1]))),
    ]
    for pattern, build in patterns:
        match = re.match(pattern, raw)
        if match:
            low, high = build(match)
            if low is not None and high is not None and low > high:
                low, high = high, low
            return low, high
    return None


def audience_label(audience: str) -> str:
    """Человеческая подпись с явными границами — чтобы не гадать, входят они или нет."""
    if audience.startswith("u:"):
        return f"по адресу страницы «{audience[2:]}»"
    if audience == "src:import":
        return "только из диалогов сообщества (боту не писали)"
    if audience == "src:bot":
        return "только тем, кто писал боту"
    if not audience.startswith("n:"):
        return "всем, кто писал боту"
    _, low, high = audience.split(":")
    if low and high:
        if low == high:
            return f"пригласили ровно {low}"
        return f"пригласили от {low} до {high} включительно"
    if low:
        return f"пригласили {low} и больше (сама {low} входит)"
    return f"пригласили от 0 до {high} включительно"


def _input_kb(back: str) -> "Keyboard":
    """Экран ввода без кнопок — ловушка: выйти из него можно только текстом."""
    kb = Keyboard()
    kb.row(Keyboard.btn("⬅️ Назад", back), Keyboard.btn("❌ Отмена", "bc:cancel"))
    return kb


RANGE_PROMPT = (
    "{intro}\n\n"
    "Границы всегда входят в диапазон:\n"
    "• 5-25 — от 5 до 25, вместе с 5 и 25\n"
    "• 10+ — 10 и больше, вместе с 10\n"
    "• до 5 — от 0 до 5, вместе с 5\n"
    "• <5 — строго меньше 5, то есть 0–4\n"
    "• 3 — ровно 3\n\n"
    "Считаются приглашённые, которые сейчас в беседе (без накрутки)."
)


# состояния формы
CONTENT, TARGETS, AUDIENCE, USERNAME, INVITED, WHEN, CONFIRM = (
    "bc:content", "bc:targets", "bc:audience", "bc:username", "bc:invited", "bc:when", "bc:confirm",
)


def start_in_background(coro) -> None:
    """Рассылка уходит в фон: админ получает ответ сразу, а не через полчаса."""
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)


# ───────────────────────── меню ─────────────────────────


def menu_kb() -> Keyboard:
    kb = Keyboard()
    kb.button("📢 Пост в беседы вузов", "bc:new:chats")
    kb.button("👥 Сообщение пользователям", "bc:new:users")
    kb.button("🗂 Запланированные", "bc:list")
    kb.button("📥 Импорт диалогов", "bc:import")
    kb.button("⬅️ В админку", "adm:home")
    kb.adjust(1)
    return kb


async def _menu_text(ctx: Ctx) -> str:
    pending = await ctx.db.pending_broadcasts()
    people = await ctx.db.count_by_source()
    lines = [
        "📣 Рассылки",
        "",
        "Пост в беседы вузов — текст или картинка во все беседы сразу или в выбранные.",
        "Сообщение пользователям — в личку всем, кому сообщество может писать.",
        "",
        f"👤 Писали боту: {people['bot']}",
        f"📥 Из диалогов сообщества: {people['import']}",
        f"✉️ Можно писать всего: {people['writable']}",
    ]
    if DAILY_LIMIT:
        lines.append(f"Дневной лимит VK: {DAILY_LIMIT} — остальное уйдёт на следующий день.")
    if pending:
        lines += ["", f"Ждут отправки: {len(pending)}"]
    return "\n".join(lines)


def _cancel_kb() -> Keyboard:
    return Keyboard().button("❌ Отмена", "bc:cancel")


# ───────────────────────── шаг 1: содержимое ─────────────────────────


async def _new(ctx: Ctx, cb: Callback, kind: str) -> None:
    form = ctx.form(cb.user_id)
    form.clear()
    form.set_state(CONTENT)
    form.update(kind=kind, selected=[], page=0)

    where = "в беседы вузов" if kind == "chats" else "пользователям бота"
    await ctx.edit(cb, f"✍️ Пришли текст или картинку с подписью — что отправляем {where}.",
                   _cancel_kb())


def _largest_photo_url(attachments: list) -> str | None:
    for item in attachments:
        if item.get("type") != "photo":
            continue
        sizes = item["photo"].get("sizes") or []
        if sizes:
            best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
            return best.get("url")
    return None


async def on_content(ctx: Ctx, message: Message) -> None:
    form = ctx.form(message.from_id)
    body = message.text.strip() or None
    photo = None
    url = _largest_photo_url(message.attachments)
    if url:
        # картинку из чужого сообщения переотправить нельзя — загружаем от имени сообщества
        try:
            photo = await ctx.api.upload_photo(await ctx.api.download(url))
        except (VkApiError, KeyError, OSError) as err:
            logger.warning("Не смог перезалить картинку для рассылки: %s", err)
            await ctx.reply(message.peer_id, "Не получилось загрузить картинку. Пришли ещё раз.",
                            _cancel_kb())
            return

    if not photo and not body:
        await ctx.reply(message.peer_id, "Нужен текст или картинка. Пришли ещё раз.", _cancel_kb())
        return

    form.update(photo_id=photo, body=body)
    if form.data["kind"] == "chats":
        form.set_state(TARGETS)
        text, markup = await _targets_screen(ctx, message.from_id)
    else:
        form.set_state(AUDIENCE)
        text, markup = _audience_screen()
    await ctx.reply(message.peer_id, text, markup)


# ───────────────────────── шаг 2а: выбор бесед ─────────────────────────


async def _targets_screen(ctx: Ctx, user_id: int) -> tuple[str, Keyboard]:
    data = ctx.form(user_id).data
    selected: list[str] = data.get("selected", [])
    page: int = data.get("page", 0)

    chats = await ctx.db.all_chats()
    labels = {c["university_key"]: chat_label(ctx, c) for c in chats}
    keys = list(labels)
    pages = max(1, -(-len(keys) // TARGETS_PAGE))
    page %= pages

    kb = Keyboard()
    for key in keys[page * TARGETS_PAGE:(page + 1) * TARGETS_PAGE]:
        mark = "✅" if key in selected else "▫️"
        kb.button(f"{mark} {labels[key]}", f"bc:t:{key}")
    kb.adjust(2)

    if pages > 1:
        kb.row(Keyboard.btn("‹", f"bc:p:{page - 1}"), Keyboard.btn("›", f"bc:p:{page + 1}"))
    kb.row(Keyboard.btn("Выбрать все", "bc:all"), Keyboard.btn("Снять все", "bc:none"))
    kb.row(Keyboard.btn("➡️ Дальше", "bc:targets_done"), Keyboard.btn("❌ Отмена", "bc:cancel"))

    chosen = "все беседы" if not selected else f"выбрано: {len(selected)} из {len(keys)}"
    paging = f" (страница {page + 1} из {pages})" if pages > 1 else ""
    text = (
        f"💬 Куда отправляем{paging}\n\n"
        f"Сейчас: {chosen}\n\n"
        f"Отметь нужные беседы или жми «Дальше» — уйдёт во все {len(keys)}."
    )
    return text, kb


# ───────────────────────── шаг 2б: аудитория ─────────────────────────


def _audience_screen() -> tuple[str, Keyboard]:
    kb = Keyboard()
    kb.button("👥 Всем", "bc:a:all")
    kb.button("📥 Только из диалогов", "bc:a:src:import")
    kb.button("🤖 Только писавшим боту", "bc:a:src:bot")
    kb.button("🔢 По числу приглашённых", "bc:a:count")
    kb.button("🔍 По адресу страницы", "bc:a:search")
    kb.button("❌ Отмена", "bc:cancel")
    kb.adjust(1)
    text = (
        "👥 Кому отправляем\n\n"
        "«Всем» — каждому, кому сообщество может писать: и тем, кто писал боту, "
        "и тем, кто просто переписывался с сообществом раньше."
    )
    return text, kb


async def on_invited(ctx: Ctx, message: Message) -> None:
    form = ctx.form(message.from_id)
    bounds = parse_invited_range(message.text)
    if bounds is None:
        await ctx.reply(message.peer_id, RANGE_PROMPT.format(intro="🔢 Не понял диапазон."),
                        _input_kb("bc:back:aud"))
        return

    low, high = bounds
    audience = f"n:{low if low is not None else ''}:{high if high is not None else ''}"
    found, imported = await ctx.db.audience_stats(audience)
    if not found:
        await ctx.reply(
            message.peer_id,
            f"Под «{audience_label(audience)}» сейчас никто не подходит. Пришли другой диапазон.",
            _input_kb("bc:back:aud"),
        )
        return

    form.update(audience=audience)
    form.set_state(WHEN)
    text, markup = _when_screen()
    detail = f"\nиз них не писали боту: {imported}" if imported else ""
    await ctx.reply(
        message.peer_id,
        f"Беру: {audience_label(audience)}\nПодходит человек: {found}{detail}\n\n{text}",
        markup,
    )


async def on_username(ctx: Ctx, message: Message) -> None:
    form = ctx.form(message.from_id)
    form.update(audience=f"u:{message.text.strip()}")
    form.set_state(WHEN)
    text, markup = _when_screen()
    await ctx.reply(message.peer_id, text, markup)


# ───────────────────────── шаг 3: когда ─────────────────────────


def _when_screen() -> tuple[str, Keyboard]:
    kb = Keyboard()
    kb.button("🚀 Отправить сейчас", "bc:w:now")
    kb.button("🕒 Выбрать время", "bc:w:later")
    kb.button("❌ Отмена", "bc:cancel")
    kb.adjust(1)
    return "⏰ Когда отправляем", kb


def parse_when(text: str) -> datetime | None:
    """«25.09 18:30», «25.09.2026 18:30» или просто «18:30» — по Москве."""
    now = datetime.now(MSK)
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        elif fmt == "%d.%m %H:%M":
            parsed = parsed.replace(year=now.year)
        return parsed.replace(tzinfo=MSK)
    return None


async def on_when(ctx: Ctx, message: Message) -> None:
    form = ctx.form(message.from_id)
    when = parse_when(message.text)
    if when is None:
        await ctx.reply(message.peer_id, "Не понял время. Пример: 25.09 18:30",
                        _input_kb("bc:back:when"))
        return
    if when <= datetime.now(MSK):
        await ctx.reply(message.peer_id, "Это время уже прошло. Пришли будущее.",
                        _input_kb("bc:back:when"))
        return
    form.update(scheduled_at=when.isoformat())
    form.set_state(CONFIRM)
    text, markup = await _confirm_screen(ctx, message.from_id)
    await ctx.reply(message.peer_id, text, markup)


# ───────────────────────── шаг 4: подтверждение ─────────────────────────


async def _confirm_screen(ctx: Ctx, user_id: int) -> tuple[str, Keyboard]:
    data = ctx.form(user_id).data
    row = {
        "kind": data["kind"],
        "targets": data.get("selected") or None,
        "audience": data.get("audience", "all"),
    }
    if data["kind"] == "chats":
        recipients, imported = len(await resolve_targets(ctx.db, row)), 0
    else:
        recipients, imported = await ctx.db.audience_stats(data.get("audience", "all"))

    if data["kind"] == "chats":
        selected = data.get("selected") or []
        names = {c["university_key"]: chat_label(ctx, c) for c in await ctx.db.all_chats()}
        where = "во все беседы" if not selected else (
            "в беседы: " + ", ".join(names.get(k, k) for k in selected[:5])
            + ("…" if len(selected) > 5 else "")
        )
    else:
        where = audience_label(data.get("audience", "all"))

    raw = data.get("scheduled_at")
    when = "сейчас" if not raw else f"{datetime.fromisoformat(raw).astimezone(MSK):%d.%m %H:%M} МСК"

    kb = Keyboard()
    kb.button("✅ Отправить", "bc:go")
    kb.button("❌ Отмена", "bc:cancel")
    kb.adjust(1)

    preview = data.get("body") or "(только картинка)"
    if len(preview) > 500:
        preview = preview[:500] + "…"

    text = (
        f"📣 Проверь перед отправкой\n\n"
        f"Куда: {where}\n"
        f"Получателей: {recipients}\n"
        f"{f'из них не писали боту: {imported}' + chr(10) if imported else ''}"
        f"Когда: {when}\n"
        f"{'📷 с картинкой' if data.get('photo_id') else ''}\n\n"
        f"─────────────\n{preview}"
    )
    return text, kb


async def _go(ctx: Ctx, cb: Callback) -> None:
    await ctx.answer(cb)   # если сюда пришли не из меню — всё равно снимаем «часики»
    form = ctx.form(cb.user_id)
    data = form.data
    raw = data.get("scheduled_at")
    when = datetime.fromisoformat(raw) if raw else datetime.now(MSK)

    broadcast_id = await ctx.db.create_broadcast(
        kind=data["kind"],
        body=data.get("body"),
        photo_id=data.get("photo_id"),
        targets=data.get("selected") or None,
        audience=data.get("audience", "all") if data["kind"] == "users" else None,
        scheduled_at=when,
        created_by=cb.user_id,
    )
    form.clear()

    if raw:
        await ctx.edit(
            cb,
            f"✅ Рассылка #{broadcast_id} запланирована на "
            f"{when.astimezone(MSK):%d.%m %H:%M} МСК.\n\n"
            f"Отменить можно в «Запланированные».",
            menu_kb(),
        )
    else:
        await ctx.edit(cb, f"🚀 Рассылка #{broadcast_id} пошла. Отчёт пришлю, когда закончу.",
                       menu_kb())
        row = await ctx.db.pool.fetchrow("SELECT * FROM broadcasts WHERE id = $1", broadcast_id)
        if await ctx.db.take_broadcast(broadcast_id):
            # не ждём отправки: пока она идёт, бот отвечает всем остальным
            start_in_background(deliver(ctx.api, ctx.db, ctx.registry, row))


# ───────────────────────── запланированные ─────────────────────────


async def _list(ctx: Ctx, cb: Callback) -> None:
    # кнопок у VK максимум 10: девять «отменить» и «назад»
    pending = (await ctx.db.pending_broadcasts())[:Keyboard.MAX_BUTTONS - 1]
    kb = Keyboard()

    if not pending:
        lines = ["🗂 Запланированные", "", "Пусто."]
    else:
        lines = ["🗂 Запланированные", ""]
        for row in pending:
            where = "беседы" if row["kind"] == "chats" else "пользователи"
            lines.append(
                f"#{row['id']} · {row['scheduled_at'].astimezone(MSK):%d.%m %H:%M} МСК · {where}"
            )
            kb.button(f"❌ Отменить #{row['id']}", f"bc:x:{row['id']}")

    recent = await ctx.db.recent_broadcasts(5)
    if recent:
        lines += ["", "Последние:"]
        for row in recent:
            status = "отменена" if row["status"] == "canceled" else (
                f"доставлено {row['sent']}, не дошло {row['failed']}"
            )
            lines.append(f"#{row['id']} — {status}")

    kb.button("⬅️ К рассылкам", "adm:bc")
    kb.adjust(1 if len(pending) <= 5 else 2)
    await ctx.edit(cb, "\n".join(lines), kb)


# ───────────────────────── импорт диалогов ─────────────────────────


async def _import(ctx: Ctx, cb: Callback) -> None:
    """Подтягиваем в базу всех, с кем у сообщества уже есть переписка.

    Диалогов бывает под сотню тысяч, и VK отдаёт их минутами — поэтому читаем в фоне,
    а админ сразу получает ответ и может дальше пользоваться ботом.
    """
    await ctx.answer(cb)
    await ctx.edit(
        cb,
        "📥 Читаю диалоги сообщества…\n\n"
        "Это фоновая задача: у больших сообществ она идёт минутами. "
        "Можно закрыть этот экран — как закончу, пришлю отчёт сюда же.",
        menu_kb(),
    )
    start_in_background(_import_job(ctx, cb.peer_id))


async def _import_job(ctx: Ctx, peer_id: int) -> None:
    async def progress(seen: int, total: int) -> None:
        await ctx.safe_send(peer_id, f"📥 Импорт диалогов: {seen} из {total}…")

    try:
        result = await import_conversations(ctx.api, ctx.db, progress)
    except VkApiError as err:
        logger.warning("Импорт диалогов сорвался: %s", err)
        await ctx.safe_send(
            peer_id,
            f"❌ Не получилось прочитать диалоги: {err.message}\n\n"
            f"Обычно это значит, что у ключа нет права «сообщения сообщества».",
            menu_kb(),
        )
        return

    people = await ctx.db.count_by_source()
    await ctx.safe_send(
        peer_id,
        "📥 Импорт диалогов закончен\n\n"
        f"Просмотрено диалогов: {result['seen']}\n"
        f"Сохранено собеседников: {result['saved']}\n"
        f"Из них запретили сообщения: {result['blocked']} — им писать не будем\n"
        + (f"VK не отдал диалогов: {result['lost']} — подхвачу их при следующем проходе\n" 
           if result.get("lost") else "") + "\n"
        f"👤 Писали боту: {people['bot']}\n"
        f"📥 Из диалогов сообщества: {people['import']}\n"
        f"✉️ Можно писать всего: {people['writable']}\n\n"
        "Дальше бот перечитывает диалоги сам, раз в несколько часов.",
        menu_kb(),
    )


# ───────────────────────── маршруты ─────────────────────────


async def on_message(ctx: Ctx, message: Message, state: str) -> None:
    handler = {
        CONTENT: on_content,
        INVITED: on_invited,
        USERNAME: on_username,
        WHEN: on_when,
    }.get(state)
    if handler is not None:
        await handler(ctx, message)
    # в остальных шагах ждём нажатия кнопки — текст не трогаем, форма остаётся


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    data = cb.data
    form = ctx.form(cb.user_id)
    state = form.state

    # подтверждаем нажатие сразу: иначе у админа крутится загрузка, пока мы считаем
    if data == "bc:cancel":
        await ctx.answer(cb)
    elif data.startswith("bc:x:"):
        await ctx.answer(cb)
    elif data == "bc:go":
        await ctx.answer(cb)
    elif data != "bc:import":  # там свой текст в снекбаре
        await ctx.answer(cb)

    if data == "adm:bc":
        form.clear()
        await ctx.edit(cb, await _menu_text(ctx), menu_kb())
    elif data.startswith("bc:new:"):
        await _new(ctx, cb, data.split(":")[2])
    elif data == "bc:cancel":
        form.clear()
        await ctx.edit(cb, await _menu_text(ctx), menu_kb())
        return
    elif data == "bc:list":
        await _list(ctx, cb)
    elif data == "bc:import":
        await _import(ctx, cb)
        return
    elif data.startswith("bc:x:"):
        await ctx.db.cancel_broadcast(int(data.split(":")[2]))
        await _list(ctx, cb)
        return

    elif state == TARGETS and data.startswith("bc:t:"):
        key = data.split(":", 2)[2]
        selected = list(form.data.get("selected", []))
        selected.remove(key) if key in selected else selected.append(key)
        form.update(selected=selected)
        await ctx.edit(cb, *await _targets_screen(ctx, cb.user_id))
    elif state == TARGETS and data.startswith("bc:p:"):
        form.update(page=int(data.split(":")[2]))
        await ctx.edit(cb, *await _targets_screen(ctx, cb.user_id))
    elif state == TARGETS and data in ("bc:all", "bc:none"):
        everything = [c["university_key"] for c in await ctx.db.all_chats()]
        form.update(selected=everything if data == "bc:all" else [])
        await ctx.edit(cb, *await _targets_screen(ctx, cb.user_id))
    elif state == TARGETS and data == "bc:targets_done":
        form.set_state(WHEN)
        await ctx.edit(cb, *_when_screen())

    elif state == AUDIENCE and data.startswith("bc:a:src:"):
        form.update(audience=data.split("bc:a:")[1])
        form.set_state(WHEN)
        await ctx.edit(cb, *_when_screen())
    elif state == AUDIENCE and data.startswith("bc:a:"):
        choice = data.split(":")[2]
        if choice == "search":
            form.set_state(USERNAME)
            await ctx.edit(cb, "🔍 Пришли короткий адрес страницы или его часть — отправлю "
                               "всем, кто подходит.\nНапример: anya",
                           _input_kb("bc:back:aud"))
        elif choice == "count":
            form.set_state(INVITED)
            await ctx.edit(cb, RANGE_PROMPT.format(
                intro="🔢 Сколько человек должен был пригласить получатель?"
            ), _input_kb("bc:back:aud"))
        else:
            form.update(audience=choice)
            form.set_state(WHEN)
            await ctx.edit(cb, *_when_screen())

    elif state == WHEN and data == "bc:w:now":
        form.update(scheduled_at=None)
        form.set_state(CONFIRM)
        await ctx.edit(cb, *await _confirm_screen(ctx, cb.user_id))
    elif state == WHEN and data == "bc:w:later":
        now = datetime.now(MSK)
        await ctx.edit(
            cb,
            "🕒 Пришли дату и время по Москве.\n\n"
            "Форматы: 25.09 18:30, 25.09.2026 18:30 или просто 18:30 (сегодня).\n"
            f"Сейчас в Москве: {now:%d.%m %H:%M}",
            _input_kb("bc:back:when"),
        )
    elif data == "bc:back:aud":
        form.set_state(AUDIENCE)
        await ctx.edit(cb, *_audience_screen())
    elif data == "bc:back:when":
        form.set_state(WHEN)
        await ctx.edit(cb, *_when_screen())

    elif state == CONFIRM and data == "bc:go":
        await _go(ctx, cb)

    await ctx.answer(cb)
