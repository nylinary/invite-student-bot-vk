"""Статистика участников по числу засчитанных приглашённых (только просмотр)."""

import csv
import io

from app import texts
from app.bot import Callback, Ctx, Message
from app.handlers.admin import send_csv
from app.handlers.broadcast import RANGE_PROMPT, audience_label, parse_invited_range
from app.vk import Keyboard

PAGE = 20
RANGE = "pp:range"


def _bounds_key(low: int | None, high: int | None) -> str:
    return f"{'' if low is None else low}:{'' if high is None else high}"


def _parse_bounds(raw_low: str, raw_high: str) -> tuple[int | None, int | None]:
    return (int(raw_low) if raw_low else None, int(raw_high) if raw_high else None)


async def _intro_text(ctx: Ctx) -> str:
    counts = await ctx.db.people_distribution()
    buckets = [
        ("0", lambda c: c == 0),
        ("1–4", lambda c: 1 <= c <= 4),
        ("5–24", lambda c: 5 <= c <= 24),
        ("25–49", lambda c: 25 <= c <= 49),
        ("50+", lambda c: c >= 50),
    ]
    people = await ctx.db.count_by_source()
    lines = [
        "👤 Участники по приглашённым",
        "",
        f"Всего людей в базе: {len(counts)} "
        f"(писали боту: {people['bot']}, из диалогов сообщества: {people['import']})",
        "Считаются приглашённые, которые сейчас в беседе (без накрутки).",
        "",
        *(f"{label} — {sum(1 for c in counts if test(c))} чел." for label, test in buckets),
        "",
        RANGE_PROMPT.format(intro="Пришли диапазон, чтобы увидеть список."),
    ]
    return "\n".join(lines)


async def _page(ctx: Ctx, low: int | None, high: int | None, page: int) -> tuple[str, Keyboard]:
    audience = f"n:{_bounds_key(low, high)}"
    total, rows = await ctx.db.people_by_invited(low, high, PAGE, page * PAGE)
    pages = max(1, -(-total // PAGE))

    lines = [
        f"👤 {audience_label(audience).capitalize()}",
        f"Подходит: {total} чел." + (f" (страница {page + 1} из {pages})" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Никого.")
    for i, row in enumerate(rows, start=page * PAGE + 1):
        name = texts.who(row["user_id"], row["username"], row["full_name"])
        lines.append(f"{i}. {name} — {row['counted']} ({ctx.registry.title(row['university_key'])})")

    key = _bounds_key(low, high)
    kb = Keyboard()
    if pages > 1:
        kb.row(
            Keyboard.btn("‹", f"pp:{key}:{(page - 1) % pages}"),
            Keyboard.btn("›", f"pp:{key}:{(page + 1) % pages}"),
        )
    if total:
        kb.row(Keyboard.btn("📥 Выгрузить список CSV", f"ppcsv:{key}"))
    kb.row(Keyboard.btn("🔢 Другой диапазон", "adm:people"))
    kb.row(Keyboard.btn("⬅️ В админку", "adm:home"))
    return "\n".join(lines), kb


async def on_message(ctx: Ctx, message: Message) -> None:
    bounds = parse_invited_range(message.text)
    if bounds is None:
        await ctx.reply(message.peer_id, RANGE_PROMPT.format(intro="🔢 Не понял диапазон."),
                        Keyboard().button("⬅️ В админку", "adm:home"))
        return
    ctx.form(message.from_id).clear()
    await ctx.reply(message.peer_id, *await _page(ctx, *bounds, 0))


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    data = cb.data

    if data == "adm:people":
        form = ctx.form(cb.user_id)
        form.clear()
        form.set_state(RANGE)
        await ctx.edit(cb, await _intro_text(ctx), Keyboard().button("⬅️ В админку", "adm:home"))
        await ctx.answer(cb)
        return

    if data.startswith("pp:"):
        _, raw_low, raw_high, raw_page = data.split(":")
        await ctx.edit(cb, *await _page(ctx, *_parse_bounds(raw_low, raw_high), int(raw_page)))
        await ctx.answer(cb)
        return

    if data.startswith("ppcsv:"):
        _, raw_low, raw_high = data.split(":")
        low, high = _parse_bounds(raw_low, raw_high)
        total, rows = await ctx.db.people_by_invited(low, high, 100_000, 0)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["user_id", "username", "full_name", "university", "invited_in_chat"])
        for row in rows:
            writer.writerow([row["user_id"], row["username"] or "", row["full_name"] or "",
                             ctx.registry.title(row["university_key"]), row["counted"]])

        await ctx.answer(cb)
        await send_csv(
            ctx, cb.peer_id, "participants.csv", buffer.getvalue().encode("utf-8-sig"),
            f"{audience_label(f'n:{raw_low}:{raw_high}').capitalize()}: {total} чел.",
        )
