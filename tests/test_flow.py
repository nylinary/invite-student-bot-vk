"""Оффлайн-прогон основных сценариев: настоящие хэндлеры, поддельный VK API."""

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bot import Ctx
from app.config import Config
from app.db import Database
from app.handlers import dispatch
from app.universities import UniversityRegistry
from app.vk import CHAT_PEER_OFFSET, VkApi, VkApiError, is_chat

GROUP_ID = 555
STUDENT, FRIEND, ADMIN, CURATOR = 1001, 1002, 9000, 1234
GROUP = CHAT_PEER_OFFSET + 1  # беседа ИТМО

PEOPLE = {
    STUDENT: ("Аня", "anya"),
    FRIEND: ("Петя", "petya"),
    ADMIN: ("Админ", "id9000"),
    CURATOR: ("Куратор", "curator_itmo"),
    7777: ("Куратор", "kurATOR"),
    8888: ("Чужой", "kurator_fake"),
    3131: ("Фермер", "farmer"),
}
TITLES = {GROUP: "ИТМО | беседа"}
# диалоги, которые у сообщества уже были до бота: 40 человек, один запретил сообщения,
# одна беседа (её импорт пропускает) и STUDENT, который боту уже писал
DIALOGS = (
    [{"id": 20_000 + i, "type": "user", "allowed": True} for i in range(40)]
    + [{"id": 20_900, "type": "user", "allowed": False},
       {"id": GROUP, "type": "chat", "allowed": True},
       {"id": STUDENT, "type": "user", "allowed": True}]
)
NO_ADMIN: set[int] = set()  # беседы, где бот не администратор
# что VK ответит по конкретному адресату: {peer_id: код ошибки}
SEND_ERRORS: dict[int, int] = {}

calls: list[tuple[str, dict]] = []
_chat_seq = 100   # номера бесед, которые «создаёт» VK в прогоне


class FakeVk(VkApi):
    """Вместо похода в api.vk.com — заранее заготовленные ответы."""

    def __init__(self):
        super().__init__("vk1.a.TEST", GROUP_ID)
        self.screen_name = "party_bot"

    async def call(self, method: str, **params):
        calls.append((method, params))
        if method == "messages.send":
            if "peer_ids" not in params:
                return len(calls)
            if params.get("keyboard") is None and len(params["peer_ids"]) == 1:
                pass
            items = []
            for pid in params["peer_ids"]:
                code = SEND_ERRORS.get(pid)
                # 950 «ответить уже нельзя» уходит, если повторить с интентом рассылки
                if code == 950 and params.get("intent"):
                    code = None
                if code:
                    items.append({"peer_id": pid, "error": {"code": code, "description": "no"}})
                else:
                    items.append({"peer_id": pid, "message_id": len(calls), "conversation_message_id": 1})
            return items
        if method == "users.get":
            ids = params["user_ids"]
            return [
                {"id": uid, "first_name": PEOPLE.get(uid, (f"Гость{uid}", None))[0],
                 "last_name": "", "screen_name": PEOPLE.get(uid, (None, f"id{uid}"))[1]}
                for uid in ids
            ]
        if method == "messages.getInviteLink":
            if params["peer_id"] in NO_ADMIN:
                raise VkApiError(917, "You don't have access to this chat", method)
            return {"link": f"https://vk.me/join/chat{params['peer_id']}"}
        if method == "messages.createChat":
            global _chat_seq
            _chat_seq += 1
            TITLES[CHAT_PEER_OFFSET + _chat_seq] = params["title"]
            return {"chat_id": _chat_seq, "peer_ids": []}
        if method == "messages.getConversationsById":
            peer = params["peer_ids"]
            if peer in NO_ADMIN:
                return {"count": 0, "items": []}
            title = TITLES.get(peer, "Беседа без понятного названия")
            return {"count": 1, "items": [{"peer": {"id": peer}, "chat_settings": {"title": title}}]}
        if method == "messages.getConversationMembers":
            peer = params["peer_id"]
            if peer in NO_ADMIN:
                raise VkApiError(917, "You don't have access to this chat", method)
            humans = 136 if peer == GROUP else 42
            items = [{"member_id": 50_000 + i} for i in range(humans - 1)]
            items.append({"member_id": CURATOR, "is_admin": True})
            items.append({"member_id": -GROUP_ID, "is_admin": True})
            return {"count": len(items), "items": items}
        if method == "messages.getConversations":
            offset, count = int(params.get("offset", 0)), int(params["count"])
            page = DIALOGS[offset:offset + count]
            return {
                "count": len(DIALOGS),
                "items": [
                    {"conversation": {"peer": {"id": d["id"], "type": d["type"]},
                                      "can_write": {"allowed": d["allowed"]}}}
                    for d in page
                ],
                "profiles": [
                    {"id": d["id"], "first_name": PEOPLE.get(d["id"], (f"Диалог{d['id']}", ""))[0],
                     "last_name": "", "screen_name": PEOPLE.get(d["id"], ("", f"id{d['id']}"))[1]}
                    for d in page if d["type"] == "user"
                ],
            }
        if method == "docs.getMessagesUploadServer":
            return {"upload_url": "https://upload/doc"}
        if method == "docs.save":
            return {"type": "doc", "doc": {"id": 77, "owner_id": -GROUP_ID}}
        if method == "photos.getChatUploadServer":
            return {"upload_url": "https://upload/chatphoto"}
        if method == "messages.setChatPhoto":
            return {"message_id": 1, "chat": {}}
        if method == "photos.getMessagesUploadServer":
            return {"upload_url": "https://upload/photo"}
        if method == "photos.saveMessagesPhoto":
            return [{"id": 88, "owner_id": -GROUP_ID, "access_key": "k"}]
        return 1

    async def _upload(self, url, field, filename, content):
        calls.append(("upload", {"url": url, "filename": filename, "size": len(content)}))
        return {"file": "f", "photo": "p", "server": 1, "hash": "h", "response": "chatphoto"}

    async def download(self, url):
        return b"image-bytes"


# ───────────── конструкторы событий ─────────────

_cmid = 0


def _next() -> int:
    global _cmid
    _cmid += 1
    return _cmid


def msg(text: str, user: int = STUDENT, peer: int | None = None, ref: str | None = None,
        payload: dict | None = None, attachments: list | None = None) -> dict:
    message = {
        "peer_id": peer or user, "from_id": user, "text": text,
        "conversation_message_id": _next(), "attachments": attachments or [],
    }
    if ref:
        message["ref"] = ref
    if payload:
        message["payload"] = json.dumps(payload)
    return {"type": "message_new", "object": {"message": message}}


def action(peer: int, kind: str, member: int, by: int | None = None) -> dict:
    return {"type": "message_new", "object": {"message": {
        "peer_id": peer, "from_id": by or member, "text": "",
        "conversation_message_id": _next(),
        "action": {"type": kind, "member_id": member},
    }}}


def press(data: str, user: int = ADMIN) -> dict:
    return {"type": "message_event", "object": {
        "user_id": user, "peer_id": user, "event_id": f"e{_next()}",
        "payload": {"c": data}, "conversation_message_id": 5,
    }}


def sends(peer: int | None = None) -> list[dict]:
    """Отправленные сообщения по одному на адресата: VK принимает пачки по 100."""
    out = []
    for method, params in calls:
        if method != "messages.send":
            continue
        for pid in params.get("peer_ids") or [params.get("peer_id")]:
            out.append({**params, "peer_id": pid})
    return [p for p in out if peer is None or p["peer_id"] == peer]


def texts_of(kind: str = "messages.send") -> list[str]:
    if kind == "messages.send":
        return [p.get("message", "") for p in sends()]
    return [p.get("message", "") for m, p in calls if m == kind]


def last_shown() -> dict:
    """Последний экран: новое сообщение или перерисованное."""
    return [p for m, p in calls if m in ("messages.send", "messages.edit")][-1]


def buttons(params: dict) -> list[dict]:
    markup = json.loads(params.get("keyboard") or '{"buttons": []}')
    # VK примет клавиатуру только в этих пределах
    count = sum(len(row) for row in markup["buttons"])
    assert count <= 10 and len(markup["buttons"]) <= 6, markup
    return [b["action"] for row in markup["buttons"] for b in row]


def pressed(params: dict, prefix: str) -> list[str]:
    return [a["label"] for a in buttons(params)
            if a["type"] == "callback" and json.loads(a["payload"])["c"].startswith(prefix)]


def snackbars() -> list[str]:
    return [json.loads(p["event_data"])["text"] for m, p in calls
            if m == "messages.sendMessageEventAnswer" and p.get("event_data")]


async def main() -> None:
    dsn = os.environ.get(
        "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
    )
    # отдельная схема, чтобы прогон не трогал боевые таблицы; в конце удаляем
    schema = f"test_flow_{os.getpid()}"
    # админы заданы и id, и адресом страницы — проверяем оба способа
    os.environ.update(
        VK_TOKEN="vk1.a.TEST", DATABASE_URL=dsn, DB_SCHEMA=schema,
        ADMIN_IDS="9000, vk.com/Kurator",
    )

    config = Config.from_env()
    db = Database(config.database_url, config.db_schema)
    await db.connect()
    await db.seed_universities(UniversityRegistry.read_seed(config.universities_file))
    registry = UniversityRegistry.from_rows(await db.all_universities())
    api = FakeVk()
    ctx = Ctx(api=api, db=db, registry=registry, config=config)

    async def feed(update: dict) -> None:
        await dispatch(ctx, update)

    # 1. «Начать» в личке: здороваемся и ставим постоянное меню под полем ввода
    await feed(msg("Начать", payload={"command": "start"}))
    assert "вечеринка" in texts_of()[-1].lower(), texts_of()[-1]
    greeting = json.loads(sends(STUDENT)[-1]["keyboard"])
    assert greeting["inline"] is False and greeting["one_time"] is False, greeting

    # 2. вуз без привязанной беседы: запасной ссылки нет -> «беседы пока нет»
    await feed(msg("ИТМО"))
    assert "Беседы этого вуза пока нет" in texts_of()[-1], texts_of()[-1]
    # а с запасной ссылкой из списка — отдаём её, VK не дёргаем
    uni = registry.get("itmo")
    await db.upsert_university("itmo", uni.title, "https://vk.me/join/fallback", list(uni.aliases))
    registry.apply_rows(await db.all_universities())
    calls.clear()
    await feed(msg("ИТМО"))
    assert "https://vk.me/join/fallback" in texts_of()[-1]
    assert not any(m == "messages.getInviteLink" for m, _ in calls)

    # 3. куратор привязывает беседу командой /bind (с упоминанием бота, как шлёт VK)
    calls.clear()
    await feed(msg("[club555|@party_bot] /bind itmo", user=ADMIN, peer=GROUP))
    assert (await db.get_chat("itmo"))["chat_id"] == GROUP
    assert (await db.get_chat("itmo"))["title"] == "ИТМО | беседа"
    bind_msgs = texts_of()
    assert any("привязана" in t for t in bind_msgs), bind_msgs
    # в беседу вуза улетает объяснение для студентов с кнопкой в бота
    chat_msgs = [p["message"] for p in sends(GROUP)]
    assert any("Система наград" in t and "vk.me/party_bot" in t for t in chat_msgs)
    assert any("Бесплатные билеты" in t for t in chat_msgs), "нет закреплённого описания"
    links = [a["link"] for p in sends(GROUP) for a in buttons(p) if a["type"] == "open_link"]
    assert "https://vk.me/party_bot?ref=uni_itmo" in links, links

    # 4. теперь тот же студент получает беседу и ПЕРСОНАЛЬНУЮ ссылку
    calls.clear()
    await feed(msg("учусь в итмо"))
    ref_link = "https://vk.me/party_bot?ref=r1001_itmo"
    assert (await db.get_invite_link(STUDENT, "itmo"))["link"] == ref_link
    shown = texts_of()[-1]
    assert f"https://vk.me/join/chat{GROUP}" in shown and ref_link in shown, shown

    # повторный запрос не плодит новые ссылки
    await feed(msg("итмо"))
    assert await db.count_links() == 1

    # 5. владелец сам вступил в беседу — приглашением не считается
    calls.clear()
    await feed(action(GROUP, "chat_invite_user_by_link", STUDENT))
    assert await db.owner_stats(STUDENT, "itmo") == (0, 0)
    assert not sends()  # и уведомления самому себе нет

    # 6. друг пришёл в бота по ссылке, взял беседу и вступил -> засчитано владельцу
    calls.clear()
    await feed(msg("Начать", user=FRIEND, ref="r1001_itmo", payload={"command": "start"}))
    assert f"https://vk.me/join/chat{GROUP}" in texts_of()[-1]  # сразу беседа ИТМО
    assert (await db.get_referral(FRIEND))["owner_id"] == STUDENT
    await feed(action(GROUP, "chat_invite_user_by_link", FRIEND))
    assert await db.owner_stats(STUDENT, "itmo") == (1, 1)
    notify = sends(STUDENT)[-1]
    assert "[id1002|@petya]" in notify["message"], notify
    assert "Засчитано приглашённых: 1" in notify["message"]

    # подделанный ref (ссылку бот не выдавал) никому ничего не засчитывает
    await feed(msg("Начать", user=1003, ref="r4444_itmo"))
    assert await db.get_referral(1003) is None

    # участник добавил знакомого в беседу вручную — приглашение засчитывается ему
    await feed(action(GROUP, "chat_invite_user", 1005, by=1010))
    assert await db.owner_stats(1010, "itmo") == (1, 1)

    # 7. /stats показывает единицу
    calls.clear()
    await feed(msg("/stats"))
    assert "Засчитано приглашённых: 1" in texts_of()[-1]

    # 8. вышел из беседы -> total остаётся, active падает
    await feed(action(GROUP, "chat_kick_user", FRIEND))
    assert await db.owner_stats(STUDENT, "itmo") == (1, 0)

    calls.clear()
    await feed(msg("/stats"))
    after_leave = texts_of()[-1]
    assert "Засчитано приглашённых: 0" in after_leave
    assert "вышли из беседы: 1" in after_leave, after_leave

    # 9. непонятный текст
    calls.clear()
    await feed(msg("хочу билет на вечеринку"))
    assert "Не нашёл такой вуз" in texts_of()[-1]

    # 10. админка и выгрузка
    calls.clear()
    await feed(msg("/admin", user=ADMIN))
    assert "Админка" in texts_of()[-1]
    await feed(msg("/top", user=ADMIN))
    top = texts_of()[-1]
    # друг к этому моменту вышел -> в зачёте ноль, но история «всего приводил» осталась
    assert "[id1001|@anya] — 0 в беседе (всего приводил: 1)" in top, top
    await feed(msg("/export", user=ADMIN))
    assert any(m == "docs.save" for m, _ in calls)
    assert sends(ADMIN)[-1]["attachment"] == f"doc-{GROUP_ID}_77"

    # 11. админ по адресу страницы (id в конфиге нет, регистр другой) тоже проходит
    calls.clear()
    await feed(msg("/admin", user=7777))
    assert "Админка" in texts_of()[-1]

    # 12. обычный пользователь админку не видит — ни по id, ни по похожему адресу
    calls.clear()
    await feed(msg("/admin"))
    await feed(msg("/admin", user=8888))
    assert not sends()

    # 13. бота добавили в ЧУЖУЮ беседу -> молчим и ничего не привязываем
    calls.clear()
    other = CHAT_PEER_OFFSET + 99
    TITLES[other] = "Барахолка СПб"
    await feed(action(other, "chat_invite_user", -GROUP_ID, by=ADMIN))
    # в саму беседу — ни слова, но организатору уходит подсказка про /bind
    assert not sends(other)
    assert sends(ADMIN) and "/bind" in sends(ADMIN)[-1]["message"]
    assert await db.get_chat_by_id(other) is None

    # 14. а в беседу вуза (название совпало) — привязка и приветствие
    calls.clear()
    known = CHAT_PEER_OFFSET + 77
    TITLES[known] = "ЛЭТИ | беседа первокурсников"
    await feed(action(known, "chat_invite_user", -GROUP_ID, by=ADMIN))
    assert (await db.get_chat("leti"))["chat_id"] == known
    assert "Система наград" in texts_of()[-1]

    # бота добавили, но админом ещё не сделали: беседа ждёт прав в pending_chats
    calls.clear()
    rights = CHAT_PEER_OFFSET + 66
    TITLES[rights] = "Горный — беседа"
    NO_ADMIN.add(rights)
    await feed(action(rights, "chat_invite_user", -GROUP_ID, by=ADMIN))
    assert rights in {r["chat_id"] for r in await db.pending_chats()}
    assert await db.get_chat_by_id(rights) is None
    NO_ADMIN.discard(rights)
    await db.drop_pending_chat(rights)   # дальше сценарии рассчитывают на прежний состав

    # 15. вступление в ещё не привязанную беседу, а потом /bind -> вуз проставится задним числом
    late = CHAT_PEER_OFFSET + 55
    await feed(action(late, "chat_invite_user_by_link", 4242))
    # чужая беседа в статистику не попадает совсем, хотя вступление в базе есть
    assert not [r for r in await db.full_stats() if r["university_key"] is None]
    unknown = "SELECT COUNT(*) FROM joins WHERE university_key IS NULL"
    assert await db.pool.fetchval(unknown) == 1
    # владелец, друг и добавленный вручную в беседе ИТМО; чужая беседа не в счёт
    assert (await db.totals())[0] == 3

    await feed(msg("/bind guap", user=ADMIN, peer=late))
    assert await db.pool.fetchval(unknown) == 0
    assert [r["total"] for r in await db.full_stats() if r["university_key"] == "guap"] == [1]

    # 16. админка: сводка и разделы по кнопкам
    calls.clear()
    await feed(msg("/admin", user=ADMIN))
    home = sends(ADMIN)[-1]
    assert "Выдано личных ссылок" in home["message"] and "Сейчас в беседах вузов" in home["message"]
    assert len(pressed(home, "adm:")) == 10

    for section, expect in [("unis", "По вузам"), ("top", "Топ пригласивших"),
                            ("chats", "Беседы с ботом"), ("home", "Админка")]:
        calls.clear()
        await feed(press(f"adm:{section}"))
        shown = texts_of("messages.edit")
        assert shown and expect in shown[-1], (section, shown)
        if section == "unis":
            assert "👥 136" in shown[-1], shown[-1]  # участники беседы ИТМО из VK
            assert "ИТОГО" in shown[-1] and "Всего в чатах" in shown[-1]
            # итог по колонке «в беседе» = 136 (ИТМО) + 42 + 42 (ЛЭТИ и ГУАП из мока)
            assert "Всего в чатах: 220" in shown[-1], shown[-1]

    # 17. чужой в админку по кнопке не попадёт
    calls.clear()
    await feed(press("adm:unis", user=STUDENT))
    assert not texts_of("messages.edit")
    assert snackbars() == ["Раздел только для организаторов"]   # отказ показываем

    # 18. бота исключили из беседы вуза -> личные ссылки туда выброшены
    assert await db.get_invite_link(STUDENT, "itmo") is not None
    await feed(action(GROUP, "chat_kick_user", -GROUP_ID, by=ADMIN))
    assert await db.get_invite_link(STUDENT, "itmo") is None
    # история вступлений при этом цела
    assert await db.owner_stats(STUDENT, "itmo") == (1, 0)

    # 19. бота вернули -> беседа уже привязана, но приветствие шлём заново
    calls.clear()
    await feed(action(GROUP, "chat_invite_user", -GROUP_ID, by=ADMIN))
    assert "Система наград" in sends(GROUP)[-1]["message"]
    # и личная ссылка выдаётся заново
    await feed(msg("итмо"))
    assert await db.get_invite_link(STUDENT, "itmo") is not None

    # 20. /announce — повторное объяснение по команде: глобальный админ и админ беседы
    for who in (ADMIN, CURATOR):
        calls.clear()
        await feed(msg("/announce", user=who, peer=GROUP))
        assert "Система наград" in texts_of()[-1], who
    # рядовой участник беседы — нет
    calls.clear()
    await feed(msg("/announce", user=STUDENT, peer=GROUP))
    assert not sends()
    # обычные сообщения в беседе бот не читает
    await feed(msg("всем привет", user=STUDENT, peer=GROUP))
    assert not sends()

    # 20б. общая беседа без вуза: в рассылки идёт, в статистику вузов — нет
    calls.clear()
    common = CHAT_PEER_OFFSET + 42
    TITLES[common] = "Ночь студента | общий чат"
    await feed(action(common, "chat_invite_user", -GROUP_ID, by=ADMIN))
    assert await db.get_chat_by_id(common) is None          # по названию вуза нет — молчим
    NO_ADMIN.add(common)          # прав администратора у бота в ней нет — и не нужно
    await feed(msg("/bindchat Общий чат", user=ADMIN, peer=common))
    bound = await db.get_chat_by_id(common)
    assert bound["university_key"] == "chat:obschiy_chat", dict(bound)
    reply = [t for t in texts_of() if "подключена как общая" in t][-1]
    assert "Права администратора для этого не нужны" in reply and "писать в беседу могу" in reply
    NO_ADMIN.discard(common)

    # вступления в неё не идут в статистику вузов
    await feed(action(common, "chat_invite_user_by_link", 7654))
    assert not [r for r in await db.full_stats() if r["university_key"].startswith("chat:")]

    # зато она есть в списке бесед и в выборе адресатов рассылки
    calls.clear()
    await feed(press("adm:chats"))
    assert "💬 Общий чат" in texts_of("messages.edit")[-1]
    await feed(press("adm:bc"))
    await feed(press("bc:new:chats"))
    await feed(msg("Всем привет", user=ADMIN))
    labels = pressed(sends(ADMIN)[-1], "bc:t:")
    assert any("Общий чат" in name for name in labels), labels
    await feed(press("bc:cancel"))

    # 20в. команды бесед работают и из лички — по id беседы
    calls.clear()
    remote = CHAT_PEER_OFFSET + 43
    TITLES[remote] = "Вечеринки от доброго"
    await feed(action(remote, "chat_invite_user", -GROUP_ID, by=ADMIN))
    hint = sends(ADMIN)[-1]["message"]
    assert f"/bindchat {remote}" in hint or f"/bind {remote}" in hint, hint

    # без id бот объясняет, а не ищет вуз
    calls.clear()
    await feed(msg("/bindchat Вечеринки", user=ADMIN))
    assert "работает внутри беседы" in texts_of()[-1], texts_of()[-1]

    # с id — привязывает, не заходя в беседу
    calls.clear()
    await feed(msg(f"/bindchat {remote} Вечеринки от доброго", user=ADMIN))
    assert (await db.get_chat_by_id(remote))["title"] == "Вечеринки от доброго"
    assert "подключена как общая" in texts_of()[-1]
    await feed(msg(f"/unbind {remote}", user=ADMIN))
    assert await db.get_chat_by_id(remote) is None

    # 20г. беседы вузов бот заводит сам, фоновой очередью по одной
    calls.clear()
    import app.chats as chats_mod
    before = len(await db.all_chats())
    no_chat = [u.key for u in registry.items
               if u.key not in {c["university_key"] for c in await db.all_chats()}]
    await feed(msg("/makechats", user=ADMIN))
    offer = sends(ADMIN)[-1]
    assert f"Без беседы: {len(no_chat)}" in offer["message"], offer["message"]
    assert pressed(offer, "adm:mkchats_go"), offer

    await feed(press("adm:mkchats_go"))
    assert await db.get_setting(chats_mod.FACTORY_ON) == "1"
    assert pressed(sends(ADMIN)[-1], "adm:mkchats_stop"), "нет кнопки остановки"

    # прокручиваем очередь: по беседе за подход
    chats_mod.FACTORY_INTERVAL = 0.01
    factory = asyncio.create_task(chats_mod.run_chat_factory(ctx))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not await chats_mod.factory_running(ctx):
            break
    await asyncio.sleep(0.2)   # даём очереди дописать отчёт
    factory.cancel()

    chats = await db.all_chats()
    assert len(chats) == before + len(no_chat), (before, len(chats))
    made = await db.get_chat("korabelka")
    assert made["title"] == "Корабелка | НОЧЬ СТУДЕНТА"
    # ссылка-приглашение сохранена в карточке вуза
    assert registry.get("korabelka").fallback_link.startswith("https://vk.me/join/")
    # в беседе закреплено описание и лежит объяснение для студентов
    in_chat = [p["message"] for p in sends(made["chat_id"])]
    assert any("Бесплатные билеты" in t and "Корабелка" in t for t in in_chat), in_chat
    assert any("Система наград" in t for t in in_chat)
    assert any(m == "messages.pin" for m, _ in calls)
    assert any(m == "messages.setChatPhoto" for m, _ in calls), "беседам не поставили аву"
    assert any("Беседы вузов готовы" in t for t in texts_of()), "нет отчёта админам"

    # 20е. бота добавили без прав: названия не видно, ждём админку и опознаём потом
    calls.clear()
    blind = CHAT_PEER_OFFSET + 52
    TITLES[blind] = "Техноложка | НОЧЬ СТУДЕНТА"
    NO_ADMIN.add(blind)          # без прав VK не отдаёт даже название
    await feed(action(blind, "chat_invite_user", -GROUP_ID, by=ADMIN))
    assert blind in {r["chat_id"] for r in await db.pending_chats()}
    assert await db.get_chat_by_id(blind) is None
    assert not sends(blind), "в беседу без прав писать не о чем"

    NO_ADMIN.discard(blind)      # куратор выдал права
    calls.clear()
    import app.chats as chats_mod
    await chats_mod.identify_pending(ctx)
    bound_blind = await db.get_chat_by_id(blind)
    assert bound_blind and bound_blind["university_key"] == "tehnologichka", bound_blind
    assert bound_blind["ready"]
    assert blind not in {r["chat_id"] for r in await db.pending_chats()}
    assert any("готова" in t for t in texts_of()), texts_of()[-3:]

    # 20ж. беседа без прав администратора: привязываем со ссылкой из команды
    calls.clear()
    manual = CHAT_PEER_OFFSET + 53
    NO_ADMIN.add(manual)
    await feed(msg(f"/bind spbgu https://vk.me/join/HANDMADE {manual}".replace(
        f" {manual}", ""), user=ADMIN, peer=manual))
    bound_manual = await db.get_chat_by_id(manual)
    assert bound_manual and bound_manual["university_key"] == "spbgu", bound_manual
    assert registry.get("spbgu").fallback_link == "https://vk.me/join/HANDMADE"
    assert "привязана" in texts_of()[-1] and "vk.me/join/HANDMADE" in texts_of()[-1]

    # студент получает эту ссылку и свою личную — считаем как обычно
    calls.clear()
    await feed(msg("СПбГУ", user=FRIEND))
    invite = texts_of()[-1]
    assert "https://vk.me/join/HANDMADE" in invite and "ref=r1002_spbgu" in invite, invite
    NO_ADMIN.discard(manual)

    # 20з. рядовой участник не может перебить привязку чужой беседы
    calls.clear()
    NO_ADMIN.add(manual)                     # прав у бота нет — админов беседы не проверить
    await feed(msg("/bind itmo", user=STUDENT, peer=manual))
    assert not sends(), "посторонний смог отправить /bind"
    assert (await db.get_chat_by_id(manual))["university_key"] == "spbgu"
    # и «отвязать» тоже нельзя
    await feed(msg("/unbind", user=STUDENT, peer=manual))
    assert await db.get_chat_by_id(manual) is not None
    NO_ADMIN.discard(manual)

    # а админ беседы (там, где бот админ и может это проверить) — может
    calls.clear()
    await feed(msg("/announce", user=CURATOR, peer=GROUP))
    assert any("Система наград" in t for t in texts_of()), "куратор беседы не смог"

    # 21. проверка привязок
    calls.clear()
    NO_ADMIN.add(manual)        # у беседы СПбГУ прав нет, но ссылка задана руками
    await feed(press("adm:check"))
    check = texts_of("messages.edit")[-1]
    assert "Проверка привязок" in check and "Проверено бесед:" in check, check
    # беседа без прав, но со ссылкой из карточки вуза — это не поломка
    assert "Без прав администратора" in check, check
    assert "больше не админ" not in check, check
    NO_ADMIN.discard(manual)

    # 22. билеты: кнопка выключена по умолчанию
    calls.clear()
    await feed(msg("итмо"))
    assert not pressed(sends(STUDENT)[-1], "ticket")

    # организатор включает выдачу из админки
    calls.clear()
    await feed(press("adm:tickets_toggle"))
    assert await db.get_setting("tickets") == "1"
    assert "включена" in texts_of("messages.edit")[-1]

    # теперь кнопка есть
    calls.clear()
    await feed(msg("итмо"))
    assert pressed(sends(STUDENT)[-1], "ticket") == ["🎫 Получить билет"]

    # 23. приглашённых мало -> билета нет, показываем сколько осталось
    calls.clear()
    await feed(press("ticket", user=STUDENT))
    assert "осталось пригласить" in texts_of()[-1] and await db.get_ticket(STUDENT) is None

    # 24. привели пятерых -> выдаётся билет с кодом
    for i in range(5):
        await db.record_join(GROUP, 5000 + i, None, f"Гость{i}", "itmo", ref_link, STUDENT)
    calls.clear()
    await feed(press("ticket", user=STUDENT))
    ticket_text = texts_of()[-1]
    row = await db.get_ticket(STUDENT)
    assert row is not None and row["code"] in ticket_text
    assert "БЕСПЛАТНЫЙ ВХОД" in ticket_text.upper() and "23:50" in ticket_text

    # код проверяется на входе командой организатора
    calls.clear()
    await feed(msg(f"/ticket {row['code'].lower()}", user=ADMIN))
    assert "Билет" in texts_of()[-1] and row["code"] in texts_of()[-1]

    # 25. уровень растёт: ещё 20 приглашённых -> VIP, код тот же
    for i in range(20):
        await db.record_join(GROUP, 6000 + i, None, f"Друг{i}", "itmo", ref_link, STUDENT)
    await feed(press("ticket", user=STUDENT))
    upgraded = await db.get_ticket(STUDENT)
    assert "VIP" in upgraded["tier"] and upgraded["code"] == row["code"]
    assert upgraded["invited"] == 25

    # 26. список вузов листается по 8 штук
    calls.clear()
    await feed(msg("/list"))
    first = sends(STUDENT)[-1]
    # админ с того же экрана попадает в админку
    await feed(msg("/list", user=ADMIN))
    assert pressed(sends(ADMIN)[-1], "adm:home"), "админу некуда выйти со списка вузов"
    page1 = pressed(first, "uni:")
    assert len(page1) == 6, page1
    assert pressed(first, "home"), "со списка вузов некуда выйти"
    assert "страница 1 из" in first["message"]

    calls.clear()
    await feed(press("list:1", user=STUDENT))
    second = [p for m, p in calls if m == "messages.edit"][-1]
    page2 = pressed(second, "uni:")
    assert len(page2) == 6 and not set(page1) & set(page2)
    assert "страница 2 из" in second["message"]

    # 27. админка видит, сколько человек уже заработали билет, даже без нажатия кнопки
    calls.clear()
    await feed(press("adm:tickets"))
    tickets_screen = texts_of("messages.edit")[-1]
    # у STUDENT 25 приглашённых -> он один и в VIP, и в «заработали»
    assert "Уже заработали билет: 1" in tickets_screen, tickets_screen
    assert "VIP-билет (от 25): 1" in tickets_screen
    assert "Бесплатный вход (от 5): 0" in tickets_screen  # уровни не суммируются

    # 28. общее для двух вузов название -> бот предлагает выбрать, а не угадывает
    calls.clear()
    await feed(msg("кирова"))
    assert pressed(sends(STUDENT)[-1], "uni:") == ["СПбГЛТУ", "ВМА Кирова"]

    # 29. рассылка в беседы: картинка с подписью, отправили сейчас
    calls.clear()
    photo = {"type": "photo", "photo": {"sizes": [
        {"width": 100, "height": 100, "url": "https://img/small"},
        {"width": 800, "height": 600, "url": "https://img/big"},
    ]}}
    await feed(press("adm:bc"))
    await feed(press("bc:new:chats"))
    await feed(msg("Приходите на вечеринку!", user=ADMIN, attachments=[photo]))
    targets = sends(ADMIN)[-1]
    assert "Куда отправляем" in targets["message"] and len(pressed(targets, "bc:t:")) == 4
    assert "страница 1 из" in targets["message"]
    await feed(press("bc:targets_done"))
    await feed(press("bc:w:now"))
    confirm = last_shown()["message"]
    assert "Проверь перед отправкой" in confirm and "Получателей: " in confirm, confirm

    calls.clear()
    await feed(press("bc:go"))
    row = await db.pool.fetchrow("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    assert row["kind"] == "chats" and row["body"] == "Приходите на вечеринку!"
    assert row["photo_id"] == f"photo-{GROUP_ID}_88_k"
    await asyncio.sleep(0.4)  # даём фоновой задаче доставить
    delivered = [p["peer_id"] for p in sends() if p.get("attachment")]
    assert set(delivered) == {c["chat_id"] for c in await db.all_chats()}, delivered

    # 30. отложенная рассылка пользователям с фильтром — и её отмена
    calls.clear()
    await feed(press("bc:new:users"))
    await feed(msg("Осталось чуть-чуть до билета", user=ADMIN))
    await feed(press("bc:a:count"))
    await feed(msg("ерунда", user=ADMIN))
    assert "Не понял" in texts_of()[-1]
    # пустой диапазон бот не пропускает — предупреждает сразу
    await feed(msg("1-4", user=ADMIN))
    assert "никто не подходит" in texts_of()[-1]
    await feed(msg("20-30", user=ADMIN))
    assert "Подходит человек: 1" in texts_of()[-1]
    await feed(press("bc:w:later"))
    await feed(msg("31.12 23:00", user=ADMIN))
    await feed(press("bc:go"))
    planned = await db.pool.fetchrow("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    assert planned["status"] == "pending" and planned["audience"] == "n:20:30"
    assert planned["scheduled_at"] > dt.datetime.now(dt.timezone.utc)
    assert not await db.due_broadcasts()  # время ещё не пришло

    await feed(press(f"bc:x:{planned['id']}"))
    assert (await db.pool.fetchval(
        "SELECT status FROM broadcasts WHERE id = $1", planned["id"])) == "canceled"

    # 31. вузы: добавили, переименовали, удалили — всё из бота
    await feed(press("adm:edu"))
    assert len(pressed(last_shown(), "un:e:")) == 4
    await feed(press("un:add"))
    await feed(msg("Тестовый вуз | https://vk.me/join/testlink | тестик, тест-вуз", user=ADMIN))
    added = [u for u in registry.items if u.title == "Тестовый вуз"]
    assert added and added[0].fallback_link == "https://vk.me/join/testlink"
    key = added[0].key
    assert [u.title for u in registry.match("тестик")] == ["Тестовый вуз"]

    await feed(press(f"un:f:title:{key}"))
    await feed(msg("Переименованный", user=ADMIN))
    assert registry.get(key).title == "Переименованный"

    await feed(press(f"un:delok:{key}"))
    assert registry.get(key) is None
    assert not await db.pool.fetchval("SELECT COUNT(*) FROM universities WHERE key = $1", key)

    # 32. рассылку прервали перезапуском -> продолжается с места, а не с начала
    calls.clear()
    ids = [7000 + i for i in range(50)]
    bid = await db.create_broadcast("users", "Догоняем", None, None, "all",
                                    dt.datetime.now(dt.timezone.utc), ADMIN)
    await db.save_broadcast_targets(bid, ids)
    await db.save_broadcast_progress(bid, 30, 28, 2)   # как будто успели 30 из 50
    await db.pool.execute("UPDATE broadcasts SET status = 'sending' WHERE id = $1", bid)

    assert await db.requeue_stuck_broadcasts() == [bid]   # рестарт вернул в очередь
    assert [r["id"] for r in await db.due_broadcasts()] == [bid]

    from app.broadcaster import deliver as deliver_now
    await deliver_now(api, db, registry, await db.pool.fetchrow(
        "SELECT * FROM broadcasts WHERE id = $1", bid))

    got = [p["peer_id"] for p in sends() if p["peer_id"] in ids]
    assert got == ids[30:], f"ушло {len(got)} вместо 20"
    done = await db.pool.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)
    assert done["status"] == "done" and done["sent"] == 48 and done["failed"] == 2

    # 33. кастомный диапазон приглашённых реально фильтрует
    everyone = await db.audience_user_ids("all")
    top = await db.audience_user_ids("n:25:")          # у STUDENT ровно 25
    nobody_yet = await db.audience_user_ids("n:0:0")
    assert STUDENT in top and len(top) == 1, top
    assert STUDENT not in nobody_yet and set(nobody_yet) < set(everyone)
    assert await db.audience_user_ids("n:26:") == []

    # 34. цикл «вступил — вышел — вернулся» не накручивает счётчик
    rejoiner = 4321
    await db.save_invite_link(FRIEND, "itmo", GROUP, "https://vk.me/party_bot?ref=r1002_itmo")
    await feed(msg("Начать", user=rejoiner, ref="r1002_itmo"))
    await feed(action(GROUP, "chat_invite_user_by_link", rejoiner))
    assert await db.owner_stats(FRIEND, "itmo") == (1, 1)

    await feed(action(GROUP, "chat_kick_user", rejoiner))
    assert await db.owner_stats(FRIEND, "itmo") == (1, 0)   # ушёл — не засчитан
    left_at = "SELECT left_at FROM joins WHERE chat_id = $1 AND user_id = $2"
    assert await db.pool.fetchval(left_at, GROUP, rejoiner) is not None

    # вернулся сам (в VK это chat_invite_user от него же)
    await feed(action(GROUP, "chat_invite_user", rejoiner))
    assert await db.owner_stats(FRIEND, "itmo") == (1, 1)   # вернулся — снова в зачёте
    assert await db.pool.fetchval(left_at, GROUP, rejoiner) is None
    assert await db.pool.fetchval(
        "SELECT COUNT(*) FROM joins WHERE chat_id = $1 AND user_id = $2",
        GROUP, rejoiner) == 1   # строка одна, а не три

    # 35. ферма: пачка аккаунтов по одной ссылке за секунду -> не засчитываются
    farmer = 3131
    await db.upsert_user(farmer, "farmer", "Фермер")
    farm_link = "https://vk.me/party_bot?ref=r3131_itmo"
    await db.save_invite_link(farmer, "itmo", GROUP, farm_link)
    calls.clear()
    for i in range(8):
        acc = 9_100_000 + i
        await db.save_referral(acc, farmer, "itmo", farm_link)
        await feed(action(GROUP, "chat_invite_user_by_link", acc))
    assert await db.flagged_count(farmer) == 8
    assert (await db.owner_stats(farmer, "itmo"))[1] == 0      # в зачёт ноль
    alerts = [p for p in sends() if "накрутку" in p["message"]]
    assert len(alerts) == 1 and alerts[0]["peer_id"] == ADMIN   # админу — один раз
    assert len(sends(farmer)) == 4, sends(farmer)   # поздравили только до срабатывания порога

    # студент видит, что часть не засчитана и почему
    calls.clear()
    await feed(msg("/stats", user=farmer))
    assert "не засчитано как накрутка: 8" in texts_of()[-1], texts_of()[-1]

    # 36. живые люди с нормальными интервалами не трогаются, даже если их много
    honest = 3232
    await db.upsert_user(honest, "honest", "Честный")
    for i in range(12):
        await db.pool.execute(
            "INSERT INTO joins (chat_id, user_id, university_key, link, owner_id, joined_at) "
            "VALUES ($1, $2, 'itmo', 'honest', $3, now() - make_interval(mins => $4))",
            GROUP, 9_200_000 + i, honest, i * 3,
        )
    assert await db.flag_bursts(honest) == 0
    assert (await db.owner_stats(honest, "itmo"))[1] == 12

    # 37. админ видит отчёт и может признать фермера честным
    calls.clear()
    await feed(press("adm:fraud"))
    report_msg = [p for m, p in calls if m == "messages.edit"][-1]
    report = report_msg["message"]
    assert "[id3131|@farmer]: 8 = отсеяно 8 · засчитано 0" in report, report
    # на экране отчёта нет кнопок «помиловать» по людям — только вход в отдельный экран
    assert not any("farmer" in a["label"] for a in buttons(report_msg))
    await feed(press("adm:fraud_restore"))
    restore = [p for m, p in calls if m == "messages.edit"][-1]
    assert any("@farmer" in a["label"] for a in buttons(restore))
    await feed(press(f"adm:trustok:{farmer}"))
    assert await db.flagged_count(farmer) == 0
    assert (await db.owner_stats(farmer, "itmo"))[1] == 8
    assert await db.flag_bursts(farmer) == 0   # доверенного больше не трогаем

    # 38. статистика участников по числу засчитанных приглашённых
    calls.clear()
    await feed(press("adm:people"))
    intro = texts_of("messages.edit")[-1]
    assert "Участники по приглашённым" in intro and "50+" in intro
    await feed(msg("20+", user=ADMIN))
    listing = texts_of()[-1]
    # STUDENT: 25 засчитанных; фермер после «помилования» — 8, в выборку не попадает
    assert "[id1001|@anya] — 25" in listing and "farmer" not in listing, listing
    assert "Подходит: 1 чел." in listing

    # после выхода в меню текст снова обычный, а не ответ на форму
    await feed(press("adm:people"))
    await feed(press("adm:home"))
    calls.clear()
    await feed(msg("итмо", user=ADMIN))
    assert "Не понял" not in texts_of()[-1]

    # команда посреди формы выводит из неё
    await feed(press("adm:people"))
    calls.clear()
    await feed(msg("/admin", user=ADMIN))
    assert "Админка" in texts_of()[-1]

    # 38б. постоянное меню под полем ввода: приходит там, где своих кнопок нет
    calls.clear()
    await ctx.db.set_setting("noop", "1")           # любое сообщение без клавиатуры
    await feed(action(GROUP, "chat_invite_user_by_link", 7001))   # уведомление владельцу
    notify = [p for p in sends() if not is_chat(p["peer_id"])]
    if notify:
        markup = json.loads(notify[-1]["keyboard"])
        assert markup["inline"] is False and markup["one_time"] is False, markup
        labels = [b["action"]["label"] for row in markup["buttons"] for b in row]
        assert "📋 Вузы" in labels, labels

    # нажатие такой кнопки приходит обычным сообщением с payload
    calls.clear()
    await feed(msg("📋 Вузы", payload={"c": "list"}))
    assert "Выбери свой вуз" in texts_of()[-1], texts_of()[-1]
    calls.clear()
    await feed(msg("🛠 Админка", user=ADMIN, payload={"c": "adm:home"}))
    assert "Админка" in texts_of()[-1]
    calls.clear()
    await feed(msg("🛠 Админка", user=STUDENT, payload={"c": "adm:home"}))
    assert not sends(), "чужому админка по кнопке не открывается"

    # 38в. временный режим «один чат на всех»
    calls.clear()
    common_chat = CHAT_PEER_OFFSET + 71
    TITLES[common_chat] = "НОЧЬ СТУДЕНТОВ | общий чат"
    NO_ADMIN.add(common_chat)                      # прав у бота нет, ссылку даёт организатор
    await feed(msg("/single https://vk.me/join/OBSHIY", user=ADMIN, peer=common_chat))
    assert "режим одного чата" in texts_of()[-1], texts_of()[-1]
    assert not any("Пишите боту" in t for t in texts_of()), "пост не должен дублироваться"
    # пост публикуется только по /announce
    calls.clear()
    await feed(msg("/announce", user=ADMIN, peer=common_chat))
    assert any("Пишите боту" in t for t in texts_of()), texts_of()
    assert (await db.get_chat_by_id(common_chat))["university_key"] == "common"

    # студент: вуз не спрашивают, сразу чат и личная ссылка
    calls.clear()
    await feed(msg("Начать", user=1201, payload={"command": "start"}))
    invite = texts_of()[-1]
    assert "https://vk.me/join/OBSHIY" in invite and "ref=r1201_common" in invite, invite
    assert "вуз" not in invite.lower(), invite
    menu = json.loads(sends(1201)[0]["keyboard"])
    assert "📋 Вузы" not in [b["action"]["label"] for row in menu["buttons"] for b in row]

    # друг по ссылке получает тот же чат, вступление засчитано
    calls.clear()
    await feed(msg("Начать", user=1202, ref="r1201_common", payload={"command": "start"}))
    assert "https://vk.me/join/OBSHIY" in texts_of()[-1]
    await feed(action(common_chat, "chat_invite_user_by_link", 1202))
    assert await db.owner_stats(1201, "common") == (1, 1)
    assert "Засчитано приглашённых: 1" in sends(1201)[-1]["message"]

    # админка тоже говорит про один чат, а не про 43 вуза
    calls.clear()
    await feed(msg("/admin", user=ADMIN))
    home = texts_of()[-1]
    assert "Режим одного чата" in home and "Бот админ в беседах вузов" not in home, home
    calls.clear()
    await feed(press("adm:unis"))
    unis_screen = texts_of("messages.edit")[-1]
    assert "Общий чат" in unis_screen and "Бот не админ в беседе" not in unis_screen, unis_screen

    # любой текст в режиме одного чата — это «дай ссылку», а не поиск вуза
    calls.clear()
    await feed(msg("привет", user=1201))
    assert "ref=r1201_common" in texts_of()[-1]

    await feed(msg("/single off", user=ADMIN))     # возвращаем вузы
    calls.clear()
    await feed(msg("итмо", user=STUDENT))
    assert "ИТМО" in texts_of()[-1] or "итмо" in texts_of()[-1].lower()
    NO_ADMIN.discard(common_chat)

    # 39. не-текст в личке (стикер, фото, голосовое) — бот всё равно отвечает
    calls.clear()
    await feed(msg("", attachments=[{"type": "sticker", "sticker": {}}]))
    assert "только" in texts_of()[-1], texts_of()

    # 40. все клавиатуры, что бот отправил, укладываются в лимиты VK (проверка в buttons)
    for m, p in calls:
        if p.get("keyboard"):
            buttons(p)

    # 41. импорт диалогов сообщества: людям можно писать, хотя боту они не писали
    calls.clear()
    await feed(press("adm:bc"))
    await feed(press("bc:import"))
    assert "Читаю диалоги сообщества" in texts_of("messages.edit")[-1]
    await asyncio.sleep(0.3)          # импорт идёт в фоне, отчёт приходит сообщением
    report = texts_of()[-1]
    people = await db.count_by_source()
    assert "Импорт диалогов закончен" in report, report
    assert people["import"] == 41, people          # 40 открытых + 1 запретивший, беседа не в счёт
    assert people["writable"] == people["bot"] + 40, people
    # тот, кто писал боту, импортом не перетирается
    assert await db.pool.fetchval(
        "SELECT source FROM users WHERE user_id = $1", STUDENT) == "bot"
    # повторный импорт ничего не ломает и не плодит дублей
    await feed(press("bc:import"))
    await asyncio.sleep(0.3)
    assert (await db.count_by_source())["import"] == 41

    # запретившему сообщения не пишем вообще — он не попадает ни в одну выборку
    everyone = await db.audience_user_ids("all")
    assert 20_900 not in everyone and 20_000 in everyone
    imported = await db.audience_user_ids("src:import")
    assert len(imported) == 40 and STUDENT not in imported
    wrote_to_bot = await db.audience_user_ids("src:bot")
    assert STUDENT in wrote_to_bot and 20_000 not in wrote_to_bot
    assert set(everyone) == set(imported) | set(wrote_to_bot)

    # рассылка «только из диалогов» собирается кнопками
    calls.clear()
    await feed(press("bc:new:users"))
    await feed(msg("Привет из бота!", user=ADMIN))
    await feed(press("bc:a:src:import"))
    await feed(press("bc:w:now"))
    confirm = last_shown()["message"]
    assert "только из диалогов" in confirm and "Получателей: 40" in confirm, confirm
    await feed(press("bc:cancel"))

    # 41б. фильтр по числу приглашённых берёт и тех, кто боту не писал
    total, imported = await db.audience_stats("n::4")      # «меньше пяти»
    assert imported == 40 and total >= 41, (total, imported)
    assert set(await db.audience_user_ids("n::4")) >= {20_000, 20_039}

    calls.clear()
    await feed(press("bc:new:users"))
    await feed(msg("Ещё не поздно позвать друзей", user=ADMIN))
    await feed(press("bc:a:count"))
    await feed(msg("<5", user=ADMIN))
    assert "из них не писали боту: 40" in texts_of()[-1], texts_of()[-1]
    await feed(press("bc:w:now"))
    assert "из них не писали боту: 40" in last_shown()["message"]

    # 41в. «часики» на кнопке снимаются до отправки, а не после
    import app.broadcaster as bc
    bc.SEND_PAUSE = 0.0        # в прогоне паузы между сообщениями не нужны
    calls.clear()
    await feed(press("bc:go"))
    order = [m for m, _ in calls]
    # нажатие подтверждено сразу, отправка ещё даже не началась
    assert "messages.sendMessageEventAnswer" in order and "messages.send" not in order, order
    assert snackbars()[-1] == "Принял"
    await asyncio.sleep(1.0)
    big = await db.pool.fetchrow("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    assert big["status"] == "done" and big["sent"] == total, dict(big)

    # 41г. из любого экрана есть куда нажать — VK не показывает старые кнопки
    calls.clear()
    await feed(msg("/help"))
    assert pressed(sends(STUDENT)[-1], "list"), "у /help нет кнопок"
    await feed(press("adm:people"))
    await feed(msg("ерунда", user=ADMIN))
    assert pressed(sends(ADMIN)[-1], "adm:home"), "из подсказки про диапазон не выйти"
    await feed(press("adm:home"))

    # 41д. VK отвечает по каждому адресату: кого-то не пустил, кому-то нужен интент
    calls.clear()
    SEND_ERRORS.update({20_001: 901, 20_002: 950, 20_003: 902})
    ids = [20_000, 20_001, 20_002, 20_003]
    bid = await db.create_broadcast("users", "Проверка ответов", None, None, "all",
                                    dt.datetime.now(dt.timezone.utc), ADMIN)
    await db.save_broadcast_targets(bid, ids)
    await db.take_broadcast(bid)
    await bc.deliver(api, db, registry, await db.pool.fetchrow(
        "SELECT * FROM broadcasts WHERE id = $1", bid))

    row = await db.pool.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)
    # 20000 дошло сразу, 20002 — со второй попытки с интентом, двое отказались
    assert (row["sent"], row["failed"]) == (2, 2), dict(row)
    retried = [p for m, p in calls if m == "messages.send" and p.get("intent")]
    assert retried and retried[-1]["peer_ids"] == [20_002], retried
    # кому VK не даёт — больше не пытаемся
    blocked = await db.pool.fetch(
        "SELECT user_id FROM users WHERE NOT can_write AND user_id = ANY($1::bigint[])", ids)
    assert {r["user_id"] for r in blocked} == {20_001, 20_003}
    assert 20_001 not in await db.audience_user_ids("all")
    SEND_ERRORS.clear()

    # 42. дневной лимит VK: хвост уезжает на завтра и продолжается с места
    calls.clear()
    # дневной лимит по умолчанию выключен: включаем руками, чтобы проверить паузу
    bc.DAILY_LIMIT, day_one = 30, "2099-01-01"
    bc._today = lambda: day_one
    ids = [8000 + i for i in range(50)]
    bid = await db.create_broadcast("users", "Длинная рассылка", None, None, "all",
                                    dt.datetime.now(dt.timezone.utc), ADMIN)
    await db.save_broadcast_targets(bid, ids)
    await db.take_broadcast(bid)
    await bc.deliver(api, db, registry, await db.pool.fetchrow(
        "SELECT * FROM broadcasts WHERE id = $1", bid))

    paused = await db.pool.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)
    assert paused["status"] == "pending" and paused["sent_offset"] == 30, dict(paused)
    assert paused["scheduled_at"] > dt.datetime.now(dt.timezone.utc)
    assert len([p for p in sends() if p["peer_id"] in ids]) == 30
    assert await db.daily_sent(day_one) == 30
    warned = [p["message"] for p in sends(ADMIN) if "лимит" in p["message"]]
    assert warned and "осталось: 20" in warned[-1], warned

    calls.clear()
    bc._today = lambda: "2099-01-02"          # наступил следующий день
    await db.take_broadcast(bid)
    await bc.deliver(api, db, registry, await db.pool.fetchrow(
        "SELECT * FROM broadcasts WHERE id = $1", bid))
    done = await db.pool.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)
    assert done["status"] == "done" and done["sent"] == 50, dict(done)
    assert [p["peer_id"] for p in sends() if p["peer_id"] in ids] == ids[30:]

    await db.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await db.close()
    print("✅ все сценарии прошли")


asyncio.run(main())
