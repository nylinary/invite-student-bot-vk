"""Тонкий клиент VK API для бота сообщества: методы, Bots Long Poll, загрузка файлов.

Своя обёртка вместо фреймворка: нужно десяток методов, а от бота требуется
предсказуемость — видно, какой запрос уходит и что с ним делать при ошибке.
"""

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.199"
# peer_id беседы = 2000000000 + её номер
CHAT_PEER_OFFSET = 2_000_000_000

# коды ошибок, которые разбираем отдельно (полный список — errors.json в vk-api-schema)
TOO_MANY_REQUESTS = 6        # слишком много запросов в секунду
FLOOD_CONTROL = 9            # антифлуд
SERVER_ERROR = 10            # временная ошибка VK
RATE_LIMIT_REACHED = 29
RATE_LIMIT_CODES = {TOO_MANY_REQUESTS, FLOOD_CONTROL, SERVER_ERROR, RATE_LIMIT_REACHED}

# получателю не доставить — дело в нём, а не в нас
BLACKLISTED = 900
NO_PERMISSION = 901
PRIVACY = 902
PEER_DENIED = 932
RECIPIENT_ERRORS = {BLACKLISTED, NO_PERMISSION, PRIVACY, PEER_DENIED, 936}

# «ответить уже нельзя»: человек давно не писал, нужен интент рассылки
WINDOW_EXPIRED = 950
INTENT_FORBIDDEN = 943       # этот интент сообществу недоступен
INTENT_LIMIT = 944           # исчерпан лимит интента
DAY_LIMIT = 601              # слишком много действий за сутки
DAY_LIMIT_CODES = {INTENT_LIMIT, DAY_LIMIT}

# сообщение не отправится никому: чинить нужно саму рассылку
MESSAGE_TOO_BIG = 910
MESSAGE_TOO_LONG = 914
BROKEN_MESSAGE_CODES = {MESSAGE_TOO_BIG, MESSAGE_TOO_LONG}

# сколько адресатов VK принимает за один вызов messages.send
SEND_BATCH = 100

_USER_TTL = 600.0


class VkApiError(Exception):
    def __init__(self, code: int, message: str, method: str = ""):
        super().__init__(f"[{code}] {message} ({method})")
        self.code = code
        self.message = message
        self.method = method


@dataclass(slots=True, frozen=True)
class VkUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    screen_name: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or f"id{self.id}"

    @property
    def username(self) -> str | None:
        """Короткий адрес страницы. «id123» — это не ник, а адрес по умолчанию."""
        if not self.screen_name or re.fullmatch(r"id\d+", self.screen_name):
            return None
        return self.screen_name


def is_chat(peer_id: int) -> bool:
    return peer_id >= CHAT_PEER_OFFSET


def _flatten(params: dict[str, Any]) -> dict[str, str]:
    """VK принимает параметры строками: списки через запятую, флаги нулём/единицей."""
    flat: dict[str, str] = {}
    for name, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            flat[name] = "1" if value else "0"
        elif isinstance(value, (list, tuple, set)):
            flat[name] = ",".join(str(v) for v in value)
        elif isinstance(value, dict):
            flat[name] = json.dumps(value, ensure_ascii=False)
        else:
            flat[name] = str(value)
    return flat


class VkApi:
    def __init__(self, token: str, group_id: int = 0):
        self.token = token
        self.group_id = group_id
        self.screen_name = ""
        self.name = ""
        self._session: aiohttp.ClientSession | None = None
        self._users: dict[int, tuple[float, VkUser]] = {}

    # ───────────── транспорт ─────────────

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def call(self, method: str, **params: Any) -> Any:
        data = _flatten(params)
        data.update(access_token=self.token, v=API_VERSION)
        for attempt in range(3):
            session = await self.session()
            async with session.post(API_URL + method, data=data) as resp:
                payload = await resp.json(content_type=None)
            error = payload.get("error")
            if error is None:
                return payload["response"]
            err = VkApiError(error.get("error_code", 0), error.get("error_msg", ""), method)
            # «слишком много запросов в секунду» — лимит сообщества 20 в секунду, ждём и повторяем
            if err.code == TOO_MANY_REQUESTS and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise err
        raise AssertionError("unreachable")

    async def _upload(self, url: str, field: str, filename: str, content: bytes) -> dict:
        form = aiohttp.FormData()
        form.add_field(field, content, filename=filename)
        session = await self.session()
        async with session.post(url, data=form) as resp:
            return await resp.json(content_type=None)

    async def download(self, url: str) -> bytes:
        session = await self.session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    # ───────────── сообщество ─────────────

    async def setup(self) -> None:
        """Узнаём id и короткий адрес сообщества по токену — из них строятся ссылки vk.me."""
        response = await self.call("groups.getById", group_id=self.group_id or None)
        groups = response["groups"] if isinstance(response, dict) else response
        group = groups[0]
        self.group_id = group["id"]
        self.screen_name = group.get("screen_name") or f"club{self.group_id}"
        self.name = group.get("name", "")

    async def ensure_longpoll(self) -> None:
        """Включаем боту всё нужное, чтобы не искать галочки в настройках сообщества.

        Для этого ключу нужно право «управление сообществом». Нет его — не страшно,
        но тогда то же самое надо включить руками (см. README).
        """
        try:
            await self.call(
                "groups.setSettings", group_id=self.group_id,
                messages=True, bots_capabilities=True, bots_add_to_chat=True,
            )
            await self.call(
                "groups.setLongPollSettings", group_id=self.group_id,
                enabled=True, api_version=API_VERSION, message_new=True, message_event=True,
            )
        except VkApiError as err:
            logger.warning(
                "Не смог сам включить Long Poll и возможности бота (%s) — "
                "проверь их в настройках сообщества", err,
            )

    def dialog_url(self, ref: str | None = None) -> str:
        """Ссылка на диалог с сообществом. ref VK передаёт боту вместе с сообщением."""
        base = f"https://vk.me/{self.screen_name}"
        return f"{base}?ref={ref}" if ref else base

    # ───────────── сообщения ─────────────

    async def send(self, peer_id: int, text: str, keyboard: str | None = None,
                   attachment: str | None = None) -> Any:
        return await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text,
            random_id=random.getrandbits(31),
            keyboard=keyboard,
            attachment=attachment,
            dont_parse_links=True,
            disable_mentions=True,
        )

    async def send_many(self, peer_ids: list[int], text: str, attachment: str | None = None,
                        intent: str | None = None, random_id: int | None = None) -> list[dict]:
        """До 100 адресатов за один запрос — так VK разрешает массовые рассылки.

        Возвращает по элементу на адресата: доставлено или с полем error.
        random_id один на пачку: повтор той же пачки не продублирует сообщения.
        """
        response = await self.call(
            "messages.send",
            peer_ids=peer_ids,
            message=text,
            random_id=random_id if random_id is not None else random.getrandbits(31),
            attachment=attachment,
            intent=intent,
            dont_parse_links=True,
            disable_mentions=True,
        )
        return response if isinstance(response, list) else []

    async def edit(self, peer_id: int, cmid: int, text: str, keyboard: str | None = None) -> Any:
        return await self.call(
            "messages.edit",
            peer_id=peer_id,
            conversation_message_id=cmid,
            message=text,
            keyboard=keyboard,
            dont_parse_links=True,
            disable_mentions=True,
        )

    async def pin(self, peer_id: int, cmid: int) -> Any:
        """Закрепить сообщение — у беседы VK нет «описания», шапку делает закреп.

        Сообществу VK разрешает указывать только conversation_message_id.
        """
        return await self.call("messages.pin", peer_id=peer_id, cmid=cmid)

    async def answer_event(self, event_id: str, user_id: int, peer_id: int,
                           text: str | None = None) -> Any:
        """Нажатие callback-кнопки надо подтвердить, иначе у человека крутится загрузка."""
        event_data = None
        if text:
            event_data = json.dumps({"type": "show_snackbar", "text": text[:90]}, ensure_ascii=False)
        return await self.call(
            "messages.sendMessageEventAnswer",
            event_id=event_id, user_id=user_id, peer_id=peer_id, event_data=event_data,
        )

    # ───────────── пользователи ─────────────

    async def get_users(self, ids: list[int]) -> dict[int, VkUser]:
        now = time.monotonic()
        found: dict[int, VkUser] = {}
        missing = []
        for uid in ids:
            cached = self._users.get(uid)
            if cached and now - cached[0] < _USER_TTL:
                found[uid] = cached[1]
            elif uid > 0:
                missing.append(uid)
        if missing:
            try:
                rows = await self.call("users.get", user_ids=missing, fields="screen_name")
            except VkApiError as err:
                logger.warning("Не смог получить профили %s: %s", missing, err)
                rows = []
            for row in rows:
                user = VkUser(
                    id=row["id"],
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    screen_name=row.get("screen_name"),
                )
                self._users[user.id] = (now, user)
                found[user.id] = user
        for uid in ids:
            found.setdefault(uid, VkUser(id=uid))
        return found

    async def get_user(self, user_id: int) -> VkUser:
        return (await self.get_users([user_id]))[user_id]

    # ───────────── беседы ─────────────

    async def chat_info(self, peer_id: int) -> dict | None:
        response = await self.call(
            "messages.getConversationsById", peer_ids=peer_id, group_id=self.group_id
        )
        items = response.get("items") or []
        return items[0] if items else None

    async def chat_title(self, peer_id: int) -> str | None:
        info = await self.chat_info(peer_id)
        return ((info or {}).get("chat_settings") or {}).get("title")

    async def chat_members(self, peer_id: int) -> dict:
        """Работает, только если бот — администратор беседы."""
        return await self.call(
            "messages.getConversationMembers", peer_id=peer_id, group_id=self.group_id
        )

    async def invite_link(self, peer_id: int) -> str:
        """Ссылка-приглашение в беседу. Без прав администратора VK её не отдаёт."""
        response = await self.call(
            "messages.getInviteLink", peer_id=peer_id, group_id=self.group_id, reset=False
        )
        return response["link"]

    # ───────────── файлы ─────────────

    async def upload_doc(self, peer_id: int, filename: str, content: bytes) -> str:
        server = await self.call("docs.getMessagesUploadServer", type="doc", peer_id=peer_id)
        uploaded = await self._upload(server["upload_url"], "file", filename, content)
        saved = await self.call("docs.save", file=uploaded["file"], title=filename)
        doc = saved["doc"] if isinstance(saved, dict) else saved[0]
        return f"doc{doc['owner_id']}_{doc['id']}"

    async def upload_photo(self, content: bytes, peer_id: int = 0) -> str:
        server = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        uploaded = await self._upload(server["upload_url"], "photo", "photo.jpg", content)
        saved = await self.call(
            "photos.saveMessagesPhoto",
            photo=uploaded["photo"], server=uploaded["server"], hash=uploaded["hash"],
        )
        photo = saved[0]
        key = f"_{photo['access_key']}" if photo.get("access_key") else ""
        return f"photo{photo['owner_id']}_{photo['id']}{key}"

    # ───────────── Bots Long Poll ─────────────

    async def listen(self, ts: str | None = None) -> AsyncIterator[tuple[list[dict], str]]:
        """Пачки событий вместе с ts, до которого они прочитаны.

        ts из прошлого запуска продолжает чтение с того места, где бот остановился:
        события, случившиеся во время перезапуска, не теряются, пока VK их хранит.
        """
        server = await self.call("groups.getLongPollServer", group_id=self.group_id)
        key, url = server["key"], server["server"]
        ts = ts or server["ts"]
        while True:
            try:
                session = await self.session()
                async with session.get(
                    url, params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                logger.warning("Long Poll: %s, переподключаюсь", err.__class__.__name__)
                await asyncio.sleep(3)
                continue

            failed = data.get("failed")
            if failed == 1:
                # история событий устарела — продолжаем с того, что VK ещё помнит
                logger.warning("Long Poll: часть событий потеряна, продолжаю с ts=%s", data["ts"])
                ts = data["ts"]
                continue
            if failed:
                server = await self.call("groups.getLongPollServer", group_id=self.group_id)
                key, url = server["key"], server["server"]
                if failed == 3:
                    ts = server["ts"]
                continue

            ts = data["ts"]
            yield data.get("updates") or [], ts


# ───────────── клавиатуры ─────────────


class Keyboard:
    """Inline-клавиатура VK. Лимиты у VK жёсткие: 10 кнопок, 6 рядов, подпись до 40 символов."""

    MAX_BUTTONS = 10
    MAX_ROWS = 6

    def __init__(self) -> None:
        self._rows: list[list[dict]] = []
        self._pending: list[dict] = []

    @staticmethod
    def btn(text: str, data: str | None = None, url: str | None = None) -> dict:
        label = text if len(text) <= 40 else text[:39] + "…"
        if url:
            return {"action": {"type": "open_link", "link": url, "label": label}}
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps({"c": data or "noop"}, ensure_ascii=False),
            }
        }

    def button(self, text: str, data: str | None = None, url: str | None = None) -> "Keyboard":
        self._pending.append(self.btn(text, data, url))
        return self

    def adjust(self, *sizes: int) -> "Keyboard":
        """Разложить добавленные кнопки по рядам: adjust(2) — по две, adjust(2, 1) — 2, потом по одной."""
        sizes = sizes or (1,)
        buttons, self._pending = self._pending, []
        i = 0
        while buttons:
            size = sizes[min(i, len(sizes) - 1)]
            self._rows.append(buttons[:size])
            buttons = buttons[size:]
            i += 1
        return self

    def row(self, *buttons: dict) -> "Keyboard":
        if self._pending:
            self.adjust(1)
        if buttons:
            self._rows.append(list(buttons))
        return self

    @property
    def rows(self) -> list[list[dict]]:
        if self._pending:
            self.adjust(1)
        return self._rows

    def as_json(self) -> str:
        rows = self.rows
        count = sum(len(r) for r in rows)
        if count > self.MAX_BUTTONS or len(rows) > self.MAX_ROWS:
            raise ValueError(f"VK не примет клавиатуру: {count} кнопок в {len(rows)} рядах")
        return json.dumps({"inline": True, "buttons": rows}, ensure_ascii=False)
