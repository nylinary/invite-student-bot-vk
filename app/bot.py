"""Входящие события VK, общий контекст хэндлеров и состояния форм."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.db import Database
from app.universities import UniversityRegistry
from app.vk import VkApi, VkApiError, VkUser, is_chat

logger = logging.getLogger(__name__)

# «[club123|@party_bot] /bind itmo» — так приходит сообщение с упоминанием бота в беседе
_MENTION = re.compile(r"^\s*\[(?:club|public)\d+\|[^\]]*\]\s*[,:]?\s*")


@dataclass(slots=True)
class Message:
    peer_id: int
    from_id: int
    text: str = ""
    cmid: int = 0
    payload: dict | None = None
    attachments: list = field(default_factory=list)
    action: dict | None = None
    ref: str | None = None
    user: VkUser | None = None

    @property
    def is_chat(self) -> bool:
        return is_chat(self.peer_id)

    @property
    def command(self) -> tuple[str, str] | None:
        """«/bind itmo» -> ("bind", "itmo"). Не команда -> None."""
        text = _MENTION.sub("", self.text or "").strip()
        if not text.startswith("/"):
            return None
        name, _, args = text[1:].partition(" ")
        return name.split("@", 1)[0].lower(), args.strip()

    @classmethod
    def from_vk(cls, raw: dict) -> "Message":
        payload = raw.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        return cls(
            peer_id=raw["peer_id"],
            from_id=raw.get("from_id", 0),
            text=raw.get("text") or "",
            cmid=raw.get("conversation_message_id", 0),
            payload=payload if isinstance(payload, dict) else None,
            attachments=raw.get("attachments") or [],
            action=raw.get("action"),
            ref=raw.get("ref"),
        )


@dataclass(slots=True)
class Callback:
    """Нажатие callback-кнопки (событие message_event)."""

    user_id: int
    peer_id: int
    event_id: str
    data: str
    cmid: int = 0
    user: VkUser | None = None
    answered: bool = False

    @classmethod
    def from_vk(cls, raw: dict) -> "Callback":
        payload = raw.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        return cls(
            user_id=raw["user_id"],
            peer_id=raw["peer_id"],
            event_id=raw["event_id"],
            data=str(payload.get("c", "")),
            cmid=raw.get("conversation_message_id", 0),
        )


class Form:
    """Состояние недозаполненной формы одного человека (в памяти, как черновик)."""

    def __init__(self, store: dict[int, dict], user_id: int):
        self._store = store
        self._user_id = user_id

    @property
    def state(self) -> str | None:
        return self._store.get(self._user_id, {}).get("_state")

    def set_state(self, state: str) -> None:
        self._store.setdefault(self._user_id, {})["_state"] = state

    def update(self, **values: Any) -> None:
        self._store.setdefault(self._user_id, {}).update(values)

    @property
    def data(self) -> dict:
        return dict(self._store.get(self._user_id, {}))

    def clear(self) -> None:
        self._store.pop(self._user_id, None)


@dataclass(slots=True)
class Ctx:
    api: VkApi
    db: Database
    registry: UniversityRegistry
    config: Config
    forms: dict[int, dict] = field(default_factory=dict)

    def form(self, user_id: int) -> Form:
        return Form(self.forms, user_id)

    def is_admin(self, user: VkUser | None) -> bool:
        return user is not None and self.config.is_admin(user.id, user.screen_name)

    # ───────────── ответы ─────────────

    async def reply(self, peer_id: int, text: str, keyboard=None, attachment: str | None = None) -> None:
        markup = keyboard.as_json() if keyboard is not None else None
        await self.api.send(peer_id, text, markup, attachment)

    async def safe_send(self, peer_id: int, text: str, keyboard=None) -> bool:
        """Отправка туда, где нас могут не ждать: закрытая личка, беседа без бота."""
        try:
            await self.reply(peer_id, text, keyboard)
            return True
        except VkApiError as err:
            logger.debug("Не смог написать в %s: %s", peer_id, err)
            return False

    async def edit(self, cb: Callback, text: str, keyboard=None) -> None:
        """Перерисовать сообщение с кнопками. Не вышло (старое сообщение) — шлём новое."""
        markup = keyboard.as_json() if keyboard is not None else None
        try:
            await self.api.edit(cb.peer_id, cb.cmid, text, markup)
        except VkApiError as err:
            logger.debug("Не смог обновить сообщение %s: %s", cb.cmid, err)
            await self.api.send(cb.peer_id, text, markup)

    async def answer(self, cb: Callback, text: str | None = None) -> None:
        """Снять «часики» с кнопки. Отвечаем один раз и как можно раньше:
        пока нажатие не подтверждено, у человека крутится загрузка."""
        if cb.answered:
            return
        cb.answered = True
        try:
            await self.api.answer_event(cb.event_id, cb.user_id, cb.peer_id, text)
        except VkApiError as err:
            # на одно нажатие отвечают один раз, а устаревшее нажатие VK уже не принимает
            logger.debug("Не смог ответить на нажатие: %s", err)
