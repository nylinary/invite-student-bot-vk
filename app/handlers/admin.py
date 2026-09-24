"""Админские команды: привязка бесед и статистика."""

import csv
import io
import logging
import time

from app import keyboards as kb
from app import texts
from app.bot import Callback, Ctx, Message
from app.db import Database
from app.handlers.tracking import announce_in_chat, announce_in_plain_chat
from app.tickets import MIN_INVITED, TIERS, tier_for
from app.handlers.unis import make_key
from app.universities import UniversityRegistry
from app.vk import CHAT_PEER_OFFSET, Keyboard, VkApi, VkApiError

logger = logging.getLogger(__name__)


async def _can_manage(ctx: Ctx, message: Message) -> bool:
    """Глобальный админ бота или админ этой конкретной беседы."""
    if ctx.is_admin(message.user):
        return True
    if message.is_chat:
        try:
            members = await ctx.api.chat_members(message.peer_id)
        except VkApiError:
            return False
        for item in members.get("items", []):
            if item.get("member_id") == message.from_id:
                return bool(item.get("is_admin") or item.get("is_owner"))
    return False


async def cmd_id(ctx: Ctx, message: Message) -> None:
    lines = []
    if message.is_chat:
        lines.append(f"peer_id беседы: {message.peer_id} (беседа №{message.peer_id - CHAT_PEER_OFFSET})")
    lines.append(f"твой id: {message.from_id}")
    markup = None if message.is_chat else (
        kb.admin_back_kb() if ctx.is_admin(message.user) else kb.back_kb()
    )
    await ctx.reply(message.peer_id, "\n".join(lines), markup)


# ───────────────────────── привязка бесед ─────────────────────────


CHAT_ONLY = ("bind", "bindchat", "unbind", "announce")


def _target(message: Message, arg: str) -> tuple[int | None, str]:
    """С какой беседой работаем. В самой беседе — с ней, из лички — по её peer_id в аргументе."""
    if message.is_chat:
        return message.peer_id, arg
    head, _, rest = arg.strip().partition(" ")
    if head.isdigit() and int(head) >= CHAT_PEER_OFFSET:
        return int(head), rest.strip()
    return None, arg


async def _need_chat(ctx: Ctx, message: Message, command: str, example: str) -> None:
    """Команду прислали в личку без id беседы — объясняем, как правильно."""
    chats = await ctx.db.all_chats()
    known = "\n".join(
        f"• {chat_label(ctx, row)} — {row['chat_id']}" for row in chats
    ) or "— пока ни одной"
    await ctx.reply(
        message.peer_id,
        f"Команда /{command} работает внутри беседы.\n\n"
        f"Зайди в нужную беседу и отправь там: {example}\n\n"
        f"Либо прямо отсюда, указав id беседы: /{command} 2000000002 …\n"
        f"id беседы бот показывает в сообщении «меня добавили в беседу», "
        f"а ещё его печатает команда /id, отправленная в самой беседе.\n\n"
        f"Беседы, которые бот уже знает:\n{known}",
        kb.admin_back_kb(),
    )


async def cmd_bind(ctx: Ctx, message: Message, arg: str) -> None:
    if not await _can_manage(ctx, message):
        return
    registry = ctx.registry
    target, arg = _target(message, arg)
    if target is None and arg and not registry.get(arg) and not registry.match(arg):
        await _need_chat(ctx, message, "bind", "/bind itmo")
        return

    if not arg:
        keys = ", ".join(u.key for u in registry.items)
        await ctx.reply(message.peer_id, f"Использование: /bind <ключ>\n\nКлючи: {keys}",
                        None if message.is_chat else kb.admin_back_kb())
        return

    if target is None:
        await _need_chat(ctx, message, "bind", f"/bind {arg}")
        return

    uni = registry.get(arg)
    if uni is None:
        matches = registry.match(arg)
        if len(matches) != 1:
            await ctx.reply(message.peer_id,
                            "Не понял, какой это вуз. Укажи ключ из /bind без аргументов.")
            return
        uni = matches[0]

    try:
        title = await ctx.api.chat_title(target) or ""
    except VkApiError:
        title = ""
    await ctx.db.bind_chat(uni.key, target, title, message.from_id)
    # беседу мог создать человек: доводим её до вида «как у бота» — ссылка, ава, закреп
    from app.chats import finish_chat

    where = "" if message.is_chat else f" {title or target}"
    if await finish_chat(ctx, target, uni):
        await ctx.reply(
            message.peer_id,
            f"✅ Беседа{where} привязана к вузу {uni.title}.\n"
            f"Ава и описание на месте, ссылка-приглашение у студентов.\n"
            f"Повторить объяснение для студентов — /announce",
        )
    else:
        await ctx.reply(
            message.peer_id,
            f"✅ Беседа{where} привязана к вузу {uni.title}, но я ещё не администратор.\n"
            f"Назначь меня админом беседы — дальше я сам поставлю аву, закреплю описание "
            f"и начну выдавать ссылки. Проверять будет фоновая задача, команды не нужны.",
        )


CHAT_PREFIX = "chat:"


def is_plain_chat(key: str) -> bool:
    """Беседа без вуза: общий чат мероприятия, тусовка организаторов и тому подобное."""
    return key.startswith(CHAT_PREFIX)


def chat_label(ctx: Ctx, row) -> str:
    """Как называть беседу в админке: вуз или собственное название беседы."""
    key = row["university_key"]
    if is_plain_chat(key):
        return row["title"] or key[len(CHAT_PREFIX):]
    return ctx.registry.title(key)


async def cmd_bindchat(ctx: Ctx, message: Message, arg: str) -> None:
    """Привязать беседу, у которой нет вуза: она нужна только для рассылок."""
    if not await _can_manage(ctx, message):
        return

    target, arg = _target(message, arg)
    if target is None:
        await _need_chat(ctx, message, "bindchat", "/bindchat Общий чат")
        return

    existing = await ctx.db.get_chat_by_id(target)
    if existing is not None and not is_plain_chat(existing["university_key"]):
        await ctx.reply(message.peer_id,
                        f"Эта беседа уже привязана к вузу {ctx.registry.title(existing['university_key'])}. "
                        f"Сначала /unbind")
        return

    # Прав администратора здесь не требуем: для рассылок достаточно быть участником.
    # Проверим на деле — сообщением в саму беседу.
    try:
        title = await ctx.api.chat_title(target) or ""
    except VkApiError:
        title = ""
    title = arg.strip() or title or "Общая беседа"

    key = existing["university_key"] if existing is not None else await ctx.db.free_chat_key(
        make_key(title, set())
    )
    await ctx.db.bind_chat(key, target, title, message.from_id)
    delivered = await announce_in_plain_chat(ctx, target)
    checked = (
        "Проверил: писать в беседу могу ✅"
        if delivered else
        "⚠️ Написать в беседу не смог. Проверь, что сообщество в ней состоит "
        "и ему не запрещено писать (Участники → сообщество → «Запретить писать в чат»)."
    )
    await ctx.reply(
        message.peer_id,
        f"✅ Беседа «{title}» подключена как общая, без вуза.\n\n"
        f"Она будет получать рассылки из админки, но в статистике по вузам не участвует "
        f"и студентам как вуз не предлагается. Права администратора для этого не нужны.\n\n"
        f"{checked}\n\nОтвязать — /unbind",
    )


async def cmd_announce(ctx: Ctx, message: Message, arg: str = "") -> None:
    """Повторно отправить в беседу объяснение для студентов (например, чтобы закрепить)."""
    if not await _can_manage(ctx, message):
        return
    target, _ = _target(message, arg)
    if target is None:
        await _need_chat(ctx, message, "announce", "/announce")
        return
    row = await ctx.db.get_chat_by_id(target)
    if row is not None and is_plain_chat(row["university_key"]):
        await announce_in_plain_chat(ctx, target)
        return
    uni = ctx.registry.get(row["university_key"]) if row else None
    if uni is None:
        await ctx.reply(message.peer_id,
                        "Беседа ещё не привязана — /bind <ключ> для вуза "
                        "или /bindchat <название> для общей беседы")
        return
    await announce_in_chat(ctx, target, uni.key)


async def cmd_unbind(ctx: Ctx, message: Message, arg: str = "") -> None:
    if not await _can_manage(ctx, message):
        return
    target, _ = _target(message, arg)
    if target is None:
        await _need_chat(ctx, message, "unbind", "/unbind")
        return
    row = await ctx.db.get_chat_by_id(target)
    if row is None:
        await ctx.reply(message.peer_id, "Эта беседа ни к чему не привязана.")
        return
    await ctx.db.unbind_chat(row["university_key"])
    await ctx.reply(message.peer_id, "Отвязал беседу. Персональные ссылки больше не выдаю.")


async def on_chat_command(ctx: Ctx, message: Message) -> None:
    """Команды внутри беседы: /bind, /announce, /unbind, /id. Остальное бот не читает."""
    command = message.command
    if command is None:
        return
    name, arg = command
    message.user = await ctx.api.get_user(message.from_id)
    if name == "bind":
        await cmd_bind(ctx, message, arg)
    elif name == "bindchat":
        await cmd_bindchat(ctx, message, arg)
    elif name == "announce":
        await cmd_announce(ctx, message, arg)
    elif name == "unbind":
        await cmd_unbind(ctx, message, arg)
    elif name == "id":
        await cmd_id(ctx, message)


# ───────────────────────── тексты админки ─────────────────────────


async def _home_text(api: VkApi, db: Database, registry: UniversityRegistry) -> str:
    people = await db.count_by_source()
    links = await db.count_links()
    total, active = await db.totals()
    chats = await db.all_chats()
    stats = await db.full_stats()
    invited = sum(row["invited"] for row in stats)
    counts = await _member_counts(api, chats)
    unis = [c for c in chats if not is_plain_chat(c["university_key"])]
    plain = [c for c in chats if is_plain_chat(c["university_key"])]
    members = [counts[c["university_key"]] for c in unis if counts.get(c["university_key"])]
    plain_members = sum(counts.get(c["university_key"]) or 0 for c in plain)
    _, earned_total = await _earned_by_tier(db)

    return "\n".join([
        "🛠 Админка",
        "",
        f"👤 Писали боту: {people['bot']}",
        f"📥 Из диалогов сообщества: {people['import']} (им можно писать без личной ссылки)",
        f"✉️ Кому можно писать всего: {people['writable']}",
        f"🔗 Выдано личных ссылок: {links}",
        f"🚪 Вступлений в беседы: {total} (сейчас в беседах: {active})",
        f"🎯 Из них приглашено по личным ссылкам: {invited}",
        f"🎫 Заработали билет (от {MIN_INVITED} приглашённых): {earned_total}",
        f"🚨 Отсеяно накрутки: {sum(r['flagged'] for r in stats)}",
        f"👥 Сейчас в беседах вузов: {sum(members)}",
        f"💬 Бот админ в беседах вузов: {len(unis)} из {len(registry.items)}",
        *([f"💬 Беседы без вуза: {len(plain)} (в них {plain_members} чел.)"] if plain else []),
        "",
        "Выбери, что показать:",
    ])


async def _universities_text(api: VkApi, db: Database, registry: UniversityRegistry) -> str:
    stats = {row["university_key"]: row for row in await db.full_stats()}
    chat_rows = await db.all_chats()
    chats = {c["university_key"] for c in chat_rows}
    members = await _member_counts(api, chat_rows)

    # показываем и вузы без активности, если бот сидит в их беседе:
    # у них ноль ссылок, но живая беседа — по этой строке видно, где буксует
    keys = [u.key for u in registry.items if u.key in stats or u.key in chats]
    keys.sort(key=lambda k: (stats[k]["total"] if k in stats else 0, members.get(k) or 0), reverse=True)

    lines = [
        "📊 По вузам",
        "🔗 ссылок выдано · 🚪 вступило всего · 🎯 по реферальным · 👥 сейчас в беседе",
        "",
    ]

    if not keys:
        lines.append("Пока пусто: ни одной ссылки не выдано.")
        return "\n".join(lines)

    # VK показывает текст не моноширинным шрифтом, поэтому вместо таблицы — строки
    sum_links = sum_total = sum_invited = sum_members = 0
    for key in keys:
        row = stats.get(key)
        links = row["links"] if row else 0
        total = row["total"] if row else 0
        invited = row["invited"] if row else 0
        in_chat = members.get(key)

        lines.append(
            f"{registry.title(key)} — 🔗 {links} · 🚪 {total} · 🎯 {invited} · "
            f"👥 {in_chat if in_chat is not None else '—'}"
        )
        sum_links += links
        sum_total += total
        sum_invited += invited
        sum_members += in_chat or 0

    lines += [
        "",
        f"ИТОГО — 🔗 {sum_links} · 🚪 {sum_total} · 🎯 {sum_invited} · 👥 {sum_members}",
        "",
        f"👥 Всего в беседах вузов: {sum_members}",
        f"🔗 Выдано личных ссылок: {sum_links}",
        f"🚪 Вступило всего: {sum_total}, из них по реферальным: {sum_invited}",
        f"🚨 Отсеяно накрутки: {sum(r['flagged'] for r in stats.values())} (в 🎯 не входит)",
    ]

    unbound = [u.title for u in registry.items if u.key not in chats]
    if unbound:
        lines += [
            "",
            f"Бот не админ в беседе ({len(unbound)}): " + ", ".join(unbound),
            "Беседа у вуза есть, но пока бот в ней не админ — студенты получают общую "
            "ссылку, вступления не считаются. Добавь бота в беседу, назначь администратором "
            "и отправь там /bind <ключ>.",
        ]
    return "\n".join(lines)


async def _top_text(db: Database) -> str:
    rows = await db.top_referrers(20)
    if not rows:
        return "🏆 Топ пригласивших\n\nПока никто никого не привёл."
    lines = ["🏆 Топ пригласивших", ""]
    for i, row in enumerate(rows, 1):
        name = texts.who(row["owner_id"], row["username"], row["full_name"])
        lines.append(f"{i}. {name} — {row['active']} в беседе (всего приводил: {row['total']})")
    return "\n".join(lines)


async def _chats_text(db: Database, registry: UniversityRegistry) -> str:
    chats = await db.all_chats()
    if not chats:
        return (
            "🔗 Беседы с ботом\n\nПока ни одной. Добавь бота в беседу вуза, назначь "
            "администратором — если название совпадает с вузом, он привяжется сам, "
            "иначе отправь там /bind <ключ>."
        )
    lines = ["🔗 Беседы с ботом", ""]
    for row in chats:
        key = row["university_key"]
        mark = "💬 " if is_plain_chat(key) else ""
        name = row["title"] or key if is_plain_chat(key) else registry.title(key)
        lines.append(f"• {mark}{name} — {row['chat_id']}"
                     + ("" if is_plain_chat(key) else f" ({row['title'] or 'без названия'})"))
    if any(is_plain_chat(r["university_key"]) for r in chats):
        lines += ["", "💬 — беседы без вуза: получают рассылки, в статистике вузов не участвуют."]
    return "\n".join(lines)


# сколько народу в беседах — спрашиваем у VK, но не чаще раза в минуту
_members_cache: dict[int, tuple[float, int]] = {}
_MEMBERS_TTL = 60.0


async def _member_counts(api: VkApi, chats: list) -> dict[str, int | None]:
    """{ключ вуза: сколько сейчас человек в его беседе}. None — не смогли узнать."""
    counts: dict[str, int | None] = {}
    now = time.monotonic()
    for row in chats:
        chat_id = row["chat_id"]
        cached = _members_cache.get(chat_id)
        if cached and now - cached[0] < _MEMBERS_TTL:
            counts[row["university_key"]] = cached[1]
            continue
        try:
            members = await api.chat_members(chat_id)
        except VkApiError as err:
            logger.debug("Не смог посчитать участников беседы %s: %s", chat_id, err)
            counts[row["university_key"]] = None
            continue
        # сообщества (в том числе сам бот) в составе беседы — не люди
        count = sum(1 for item in members.get("items", []) if item.get("member_id", 0) > 0)
        _members_cache[chat_id] = (now, count)
        counts[row["university_key"]] = count
    return counts


async def _selfcheck_text(api: VkApi, db: Database, registry: UniversityRegistry) -> str:
    """Сверяем каждую привязку с реальностью: та ли это беседа и админ ли там бот.

    Ровно та проверка, которая ловит «нажал РАНХиГС, попал в беседу ГЛТУ»:
    если беседа привязана не к тому вузу, здесь это видно сразу.
    """
    chats = await db.all_chats()
    if not chats:
        return "🔍 Проверка привязок\n\nПривязанных бесед пока нет."

    problems: list[str] = []
    checked = 0
    for row in chats:
        plain = is_plain_chat(row["university_key"])
        expected = row["title"] or "Общая беседа" if plain else registry.title(row["university_key"])
        try:
            title = await api.chat_title(row["chat_id"])
        except VkApiError as err:
            problems.append(f"❌ {expected}: беседа недоступна ({err.message})")
            continue

        checked += 1
        guess = [] if plain else registry.match(title or "")
        if len(guess) == 1 and guess[0].key != row["university_key"]:
            problems.append(
                f"⚠️ {expected}: беседа называется «{title}» — "
                f"похоже на {guess[0].title}. Проверь /bind."
            )

        if plain:
            continue  # беседе без вуза права администратора не нужны: там только рассылки
        try:
            await api.invite_link(row["chat_id"])
        except VkApiError:
            problems.append(f"❌ {expected}: бот больше не админ — ссылку в беседу не выдать")

    lines = ["🔍 Проверка привязок", "", f"Проверено бесед: {checked} из {len(chats)}"]
    if problems:
        lines += ["", *problems]
    else:
        lines += ["", "✅ Все беседы на месте, названия совпадают с вузами, права у бота есть."]
    return "\n".join(lines)


async def _earned_by_tier(db: Database) -> tuple[dict[str, int], int]:
    """Кто сколько заработал прямо сейчас, независимо от того, нажимал ли кнопку.

    Уровни не суммируются: человек с 27 приглашёнными попадает только в VIP.
    """
    earned = {name: 0 for _, name, _ in TIERS}
    for count in await db.owner_invite_counts():
        tier = tier_for(count)
        if tier is not None:
            earned[tier[1]] += 1
    return earned, sum(earned.values())


async def _tickets_text(db: Database) -> str:
    enabled = await db.get_setting("tickets", "0") == "1"
    by_tier = {row["tier"]: row["count"] for row in await db.tickets_by_tier()}
    issued = sum(by_tier.values())
    earned, earned_total = await _earned_by_tier(db)
    counts = await db.owner_invite_counts()
    almost = sum(1 for c in counts if 0 < c < MIN_INVITED)

    lines = [
        "🎫 Билеты",
        "",
        f"Кнопка «Получить билет» у студентов: {'включена' if enabled else 'выключена'}",
        "",
        f"Уже заработали билет: {earned_total} чел.",
    ]
    for need, name, _ in TIERS:
        lines.append(f"{name} (от {need}): {earned[name]}")
    lines += [
        "",
        f"Приглашают, но до билета не дотянули: {almost} чел.",
        f"Забрали билет кнопкой: {issued} из {earned_total}",
    ]
    if issued:
        lines.append("")
        for need, name, _ in TIERS:
            lines.append(f"— забрали {name}: {by_tier.get(name, 0)}")
    lines += [
        "",
        "Проверить билет на входе: /ticket КОД",
    ]
    return "\n".join(lines)


async def _fraud_screen(db: Database) -> tuple[str, Keyboard]:
    rows = await db.fraud_report(20)
    markup = Keyboard()

    if not rows:
        lines = ["🚨 Накрутка", "", "Подозрительных вступлений не найдено."]
    else:
        total_flagged = sum(r["flagged"] for r in rows)
        lines = [
            "🚨 Накрутка",
            "",
            f"Не засчитано вступлений: {total_flagged} у {len(rows)} пригласивших.",
            "Признак — пачка из 5+ аккаунтов по одной ссылке за ±5 секунд. Живые люди "
            "так не заходят даже из большого чата: по нашим данным максимум 3 за 10 секунд.",
            "",
            "всего = отсеяно + вышли из беседы + засчитано",
            "",
        ]
        for row in rows:
            name = texts.who(row["owner_id"], row["username"], row["full_name"])
            gone = f" · вышли {row['gone']}" if row["gone"] else ""
            lines.append(
                f"• {name}: {row['total']} = отсеяно {row['flagged']}{gone}"
                f" · засчитано {row['counted']}"
            )
        lines += [
            "",
            "Отсеянное уже не засчитывается — делать ничего не нужно. "
            "Если кто-то из списка честный и попал по ошибке, его можно вернуть в зачёт.",
        ]
        markup.button("↩️ Вернуть кого-то в зачёт…", "adm:fraud_restore")

    markup.button("⬅️ В админку", "adm:home")
    markup.adjust(1)
    return "\n".join(lines), markup


def _plain_who(row, id_key: str = "owner_id") -> str:
    """Подпись кнопки: там упоминания [id|...] не работают."""
    return f"@{row['username']}" if row["username"] else (row["full_name"] or str(row[id_key]))


async def _restore_screen(db: Database) -> tuple[str, Keyboard]:
    """Отдельный экран, чтобы ручное «помиловать» не лежало под пальцем рядом с отчётом."""
    rows = await db.fraud_report(Keyboard.MAX_BUTTONS - 1)
    markup = Keyboard()
    for row in rows:
        markup.button(f"↩️ {_plain_who(row)} ({row['flagged']})", f"adm:trust:{row['owner_id']}")
    markup.button("⬅️ Назад к отчёту", "adm:fraud")
    # в одну колонку VK пускает не больше шести рядов
    markup.adjust(1 if len(rows) <= 5 else 2)
    text = (
        "↩️ Вернуть в зачёт\n\n"
        "Выбери пригласившего, у которого детектор ошибся. Его отсеянные вступления "
        "снова засчитаются, и больше детектор его проверять не будет.\n\n"
        "Делай это только если уверен, что это живые люди — например, знаешь, "
        "что он выложил ссылку на лекции и все зашли одновременно."
    )
    return text, markup


async def _csv_bytes(db: Database) -> bytes | None:
    rows = await db.export_joins()
    if not rows:
        return None
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["university", "user_id", "username", "full_name", "owner_id",
         "owner_username", "owner_name", "link", "joined_at", "left_at"]
    )
    for row in rows:
        writer.writerow([row[k] for k in row.keys()])
    return buffer.getvalue().encode("utf-8-sig")


async def send_csv(ctx: Ctx, peer_id: int, filename: str, content: bytes, caption: str) -> None:
    try:
        attachment = await ctx.api.upload_doc(peer_id, filename, content)
    except (VkApiError, KeyError) as err:
        logger.warning("Не смог загрузить %s: %s", filename, err)
        await ctx.reply(peer_id, "Не получилось загрузить файл в VK, попробуй ещё раз.",
                        kb.admin_back_kb())
        return
    await ctx.reply(peer_id, caption, kb.admin_back_kb(), attachment=attachment)


# ───────────────────────── команды и кнопки ─────────────────────────


async def cmd_admin(ctx: Ctx, message: Message) -> None:
    await ctx.reply(message.peer_id, await _home_text(ctx.api, ctx.db, ctx.registry), kb.admin_kb())


async def cmd_chats(ctx: Ctx, message: Message) -> None:
    await ctx.reply(message.peer_id, await _chats_text(ctx.db, ctx.registry), kb.admin_back_kb())


async def cmd_top(ctx: Ctx, message: Message) -> None:
    await ctx.reply(message.peer_id, await _top_text(ctx.db), kb.admin_back_kb())


async def cmd_export(ctx: Ctx, message: Message) -> None:
    content = await _csv_bytes(ctx.db)
    if content is None:
        await ctx.reply(message.peer_id, "Выгружать пока нечего.", kb.admin_back_kb())
        return
    await send_csv(ctx, message.peer_id, "joins.csv", content,
                   "Все вступления: кто, куда и по чьей ссылке")


async def cmd_makechats(ctx: Ctx, message: Message, arg: str = "") -> None:
    """Показать, каким вузам не хватает беседы, и включить фоновую очередь."""
    from app.chats import FACTORY_INTERVAL, chat_title, factory_running, missing_chats

    left = await missing_chats(ctx)
    running = await factory_running(ctx)
    markup = Keyboard()

    if not left:
        text = "У всех вузов из списка беседа уже есть."
    else:
        minutes = max(1, round(len(left) * FACTORY_INTERVAL / 60))
        text = (
            f"🏗 Беседы вузов\n\n"
            f"Без беседы: {len(left)} из {len(ctx.registry.items)}\n"
            f"{', '.join(u.title for u in left[:12])}{'…' if len(left) > 12 else ''}\n\n"
            f"Бот создаёт их сам и становится владельцем — права выдавать не придётся. "
            f"Название: «{chat_title(left[0])}».\n"
            f"В каждой беседе он закрепит описание и опубликует объяснение для студентов, "
            f"а ссылку-приглашение положит в карточку вуза.\n\n"
            f"VK не даёт создавать беседы подряд, поэтому очередь идёт по одной примерно "
            f"раз в {round(FACTORY_INTERVAL / 60)} мин — это около {minutes} мин на все. "
            f"Бот доделает сам, даже если его перезапустить."
        )
        if running:
            text += "\n\n▶️ Очередь уже идёт."
            markup.button("⏹ Остановить", "adm:mkchats_stop")
        else:
            markup.button(f"🏗 Создать {len(left)} бесед", "adm:mkchats_go")

    markup.button("⬅️ В админку", "adm:home")
    markup.adjust(1)
    await ctx.reply(message.peer_id, text, markup)


async def cmd_ticket(ctx: Ctx, message: Message, code: str) -> None:
    """Проверка билета по коду — для тех, кто стоит на входе."""
    if not code:
        await ctx.reply(message.peer_id, "Использование: /ticket NS-A1B2C3", kb.admin_back_kb())
        return

    row = await ctx.db.find_ticket(code)
    if row is None:
        await ctx.reply(message.peer_id, f"❌ Билет {code} не найден.", kb.admin_back_kb())
        return

    await ctx.reply(
        message.peer_id,
        f"✅ Билет {row['code']}\n"
        f"Уровень: {row['tier']}\n"
        f"Гость: {texts.who(row['user_id'], row['username'], row['full_name'])}\n"
        f"Вуз: {ctx.registry.title(row['university_key'])}\n"
        f"Приглашено: {row['invited']}\n"
        f"Выдан: {row['issued_at']:%d.%m %H:%M}",
        kb.admin_back_kb(),
    )


ADMIN_COMMANDS = {
    "makechats": cmd_makechats,
    "admin": cmd_admin,
    "chats": cmd_chats,
    "top": cmd_top,
    "export": cmd_export,
}


async def on_private_command(ctx: Ctx, message: Message, name: str, arg: str) -> bool:
    """Админская команда в личке. False — это не она, пусть разбирает студенческая часть."""
    if name == "id":
        await cmd_id(ctx, message)
        return True
    if name not in ADMIN_COMMANDS and name not in CHAT_ONLY and name != "ticket":
        return False
    if not ctx.is_admin(message.user):
        return True  # чужим админку не показываем и не подсказываем, что она есть
    if name in CHAT_ONLY:
        await {"bind": cmd_bind, "bindchat": cmd_bindchat,
               "announce": cmd_announce, "unbind": cmd_unbind}[name](ctx, message, arg)
    elif name == "ticket":
        await cmd_ticket(ctx, message, arg)
    else:
        await ADMIN_COMMANDS[name](ctx, message)
    return True


async def on_callback(ctx: Ctx, cb: Callback) -> None:
    db, registry = ctx.db, ctx.registry
    # ушёл из недозаполненной формы в меню — следующий текст уже не ответ на неё
    ctx.form(cb.user_id).clear()

    section = cb.data.split(":", 1)[1]
    # эти экраны считаются долго (опрос VK по каждой беседе) — снимаем «часики» сразу
    await ctx.answer(cb, {
        "home": "Считаю…", "unis": "Считаю…",
        "check": "Проверяю беседы…", "csv": "Готовлю файл…",
    }.get(section))

    if section == "mkchats":
        await cmd_makechats(ctx, Message(peer_id=cb.peer_id, from_id=cb.user_id, user=cb.user))
        return

    if section in ("mkchats_go", "mkchats_stop"):
        from app.chats import start_factory, stop_factory

        if section == "mkchats_stop":
            await stop_factory(ctx)
            await ctx.answer(cb, "Остановил")
        else:
            left = await start_factory(ctx, cb.user_id)
            await ctx.answer(cb, f"Запустил: {left} бесед")
        await cmd_makechats(ctx, Message(peer_id=cb.peer_id, from_id=cb.user_id, user=cb.user))
        return

    if section == "csv":
        content = await _csv_bytes(db)
        if content is None:
            await ctx.answer(cb, "Выгружать пока нечего")
            return
        await ctx.answer(cb)
        await send_csv(ctx, cb.peer_id, "joins.csv", content,
                       "Все вступления: кто, куда и по чьей ссылке")
        return

    if section == "tickets_toggle":
        enabled = await db.get_setting("tickets", "0") == "1"
        await db.set_setting("tickets", "0" if enabled else "1")
        await ctx.answer(cb, "Выключил" if enabled else "Включил, кнопка у студентов есть")
        enabled = not enabled
        await ctx.edit(cb, await _tickets_text(db), kb.tickets_kb(enabled))
        return

    if section == "tickets":
        enabled = await db.get_setting("tickets", "0") == "1"
        await ctx.edit(cb, await _tickets_text(db), kb.tickets_kb(enabled))
        await ctx.answer(cb)
        return

    if section == "fraud":
        text, markup = await _fraud_screen(db)
        await ctx.edit(cb, text, markup)
        await ctx.answer(cb)
        return

    if section == "fraud_restore":
        text, markup = await _restore_screen(db)
        await ctx.edit(cb, text, markup)
        await ctx.answer(cb)
        return

    if section.startswith("trust:"):
        owner_id = int(section.split(":")[1])
        owner = await db.pool.fetchrow(
            "SELECT username, full_name FROM users WHERE user_id = $1", owner_id
        )
        name = texts.who(owner_id, owner["username"] if owner else None,
                         owner["full_name"] if owner else None)
        markup = Keyboard()
        markup.button("↩️ Да, вернуть в зачёт", f"adm:trustok:{owner_id}")
        markup.button("⬅️ Нет, оставить отсеянным", "adm:fraud_restore")
        markup.adjust(1)
        await ctx.edit(
            cb,
            f"Засчитать {name} все отсеянные вступления?\n\n"
            f"Детектор больше не будет проверять этого пригласившего — "
            f"делай так, только если уверен, что это живые люди.",
            markup,
        )
        await ctx.answer(cb)
        return

    if section.startswith("trustok:"):
        restored = await db.trust_inviter(int(section.split(":")[1]), cb.user_id)
        await ctx.answer(cb, f"Вернул в зачёт: {restored}")
        text, markup = await _fraud_screen(db)
        await ctx.edit(cb, text, markup)
        return

    if section == "check":
        await ctx.answer(cb, "Проверяю беседы…")
        await ctx.edit(cb, await _selfcheck_text(ctx.api, db, registry), kb.admin_back_kb())
        return

    if section == "unis":
        await ctx.answer(cb, "Считаю…")
        await ctx.edit(cb, await _universities_text(ctx.api, db, registry), kb.admin_back_kb())
        return

    if section == "top":
        text, markup = await _top_text(db), kb.admin_back_kb()
    elif section == "chats":
        text, markup = await _chats_text(db, registry), kb.admin_back_kb()
    else:
        text, markup = await _home_text(ctx.api, db, registry), kb.admin_kb()

    await ctx.edit(cb, text, markup)
    await ctx.answer(cb)
