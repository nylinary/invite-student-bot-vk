"""Выдача ссылок: в беседу вуза и персональной реферальной ссылки на бота."""

import logging

from app.db import Database
from app.universities import University
from app.vk import VkApi, VkApiError, VkUser

logger = logging.getLogger(__name__)


class InviteResult:
    """Что удалось выдать пользователю по конкретному вузу."""

    def __init__(self, link: str | None, personal: bool, chat_id: int | None = None,
                 ref_link: str | None = None):
        self.link = link  # ссылка на вступление в беседу
        self.personal = personal  # True — беседа под ботом, вступления считаются
        self.chat_id = chat_id
        self.ref_link = ref_link  # личная ссылка для друзей


def ref_payload(owner_id: int, key: str) -> str:
    """ref в ссылке vk.me: кто зовёт и в какой вуз. Буквы, цифры и «_» VK пропускает."""
    return f"r{owner_id}_{key}"


def parse_ref(ref: str) -> tuple[int, str] | None:
    """«r123_itmo» -> (123, "itmo"). Ключ вуза сам может содержать «_»."""
    if not ref.startswith("r"):
        return None
    owner, _, key = ref[1:].partition("_")
    if not owner.isdigit() or not key:
        return None
    return int(owner), key


async def get_or_create_invite(
    api: VkApi, db: Database, user: VkUser, uni: University
) -> InviteResult:
    chat = await db.get_chat(uni.key)
    if chat is None:
        # Бота ещё не привязали к беседе этого вуза — отдаём общую ссылку из списка.
        return InviteResult(uni.fallback_link, personal=False)

    try:
        # ссылку не кэшируем: админ беседы может её сбросить, а запрос дешёвый
        chat_link = await api.invite_link(chat["chat_id"])
    except VkApiError as err:
        # Прав администратора у бота может не быть — VK их сообществам в чужих беседах
        # не даёт. Тогда берём ссылку, которую организатор положил в карточку вуза:
        # вступления всё равно считаются, бот же сидит в беседе и видит события.
        logger.debug("Ссылку беседы %s взял из карточки вуза (%s)", uni.key, err)
        chat_link = uni.fallback_link
        if not chat_link:
            return InviteResult(None, personal=False)

    existing = await db.get_invite_link(user.id, uni.key)
    ref_link = api.dialog_url(ref_payload(user.id, uni.key))
    if existing is None or existing["chat_id"] != chat["chat_id"] or existing["link"] != ref_link:
        await db.save_invite_link(user.id, uni.key, chat["chat_id"], ref_link)
    return InviteResult(chat_link, personal=True, chat_id=chat["chat_id"], ref_link=ref_link)
