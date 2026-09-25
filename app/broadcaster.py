"""Фоновый отправщик рассылок: и мгновенных, и отложенных.

VK принимает до 100 адресатов за один вызов messages.send и отвечает по каждому
отдельно. Поэтому рассылка на десятки тысяч человек — это сотни запросов, а не
десятки тысяч: сообществу разрешено 20 запросов в секунду.

Жёсткого «столько-то сообщений в сутки» в документации VK нет. Есть коды ошибок,
которыми VK сам сообщает, что пора притормозить: 601 (слишком много действий за
сутки) и 944 (исчерпан лимит интента). На них рассылка встаёт на паузу до завтра.
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import Database
from app.universities import UniversityRegistry
from app.vk import (
    BROKEN_MESSAGE_CODES,
    DAY_LIMIT_CODES,
    INTENT_FORBIDDEN,
    RATE_LIMIT_CODES,
    RECIPIENT_ERRORS,
    SEND_BATCH,
    WINDOW_EXPIRED,
    VkApi,
    VkApiError,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 15.0
# пауза между пачками: пять запросов в секунду при разрешённых двадцати
SEND_PAUSE = 0.2
# «слишком часто» от VK — столько ждём перед повтором пачки
RATE_LIMIT_PAUSE = 2.0
BATCH_TRIES = 3

MSK = ZoneInfo("Europe/Moscow")
# Ограничение на своей стороне: 0 — доверяем сигналам VK (601 и 944).
# Ставится, если хочется растянуть большую рассылку намеренно.
DAILY_LIMIT = int(os.environ.get("BROADCAST_DAILY_LIMIT", "0"))
RESUME_HOUR = 10  # во сколько по Москве продолжать отложенный хвост
# Кому VK не даёт написать обычным сообщением (человек давно не писал сам) — пробуем
# ещё раз с интентом рассылки. Какой интент доступен сообществу, зависит от его
# настроек; неподходящий VK отклонит кодом 943, и мы перестанем пытаться.
NEWSLETTER_INTENT = os.environ.get("BROADCAST_INTENT", "non_promo_newsletter")


def _today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _tomorrow() -> datetime:
    return (datetime.now(MSK) + timedelta(days=1)).replace(
        hour=RESUME_HOUR, minute=0, second=0, microsecond=0
    )


async def run_broadcaster(api: VkApi, db: Database, registry: UniversityRegistry) -> None:
    """Раз в четверть минуты забирает подошедшие по времени рассылки."""
    while True:
        try:
            for row in await db.due_broadcasts():
                if await db.take_broadcast(row["id"]):
                    await deliver(api, db, registry, row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — воркер не должен умирать из-за одной рассылки
            logger.exception("Рассылка сорвалась, продолжаю работу")
        await asyncio.sleep(POLL_INTERVAL)


async def resolve_targets(db: Database, row) -> list[int]:
    """Во что превращается выбор админа: список конкретных peer_id бесед или user_id."""
    if row["kind"] == "chats":
        keys = row["targets"]
        return [
            chat["chat_id"]
            for chat in await db.all_chats()
            if not keys or chat["university_key"] in keys
        ]
    return await db.audience_user_ids(row["audience"] or "all")


class Stop(Exception):
    """Дальше сегодня не шлём: VK попросил притормозить или сообщение битое."""

    def __init__(self, reason: str, fatal: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.fatal = fatal  # fatal — чинить рассылку, а не ждать завтра


class Sender:
    """Отправка одной рассылки: пачки по 100, разбор ответа по каждому адресату."""

    def __init__(self, api: VkApi, db: Database, body: str | None, photo: str | None):
        self.api, self.db = api, db
        self.body, self.photo = body or "", photo or None
        self.sent = self.failed = 0
        self.blocked: list[int] = []   # кому VK не даёт писать — снимаем галочку в базе
        self.intent_ok = bool(NEWSLETTER_INTENT)

    async def send_batch(self, peers: list[int]) -> None:
        items = await self._call(peers)
        retry_with_intent: list[int] = []

        for item in items:
            error = item.get("error") or {}
            code = error.get("code")
            if not code:
                self.sent += 1
            elif code == WINDOW_EXPIRED and self.intent_ok:
                retry_with_intent.append(item["peer_id"])
            elif code in DAY_LIMIT_CODES:
                raise Stop(f"VK: {error.get('description') or code}")
            elif code in RECIPIENT_ERRORS or code == WINDOW_EXPIRED:
                self.failed += 1
                self.blocked.append(item["peer_id"])
            else:
                logger.debug("Не доставлено в %s: %s", item["peer_id"], error)
                self.failed += 1

        if retry_with_intent:
            await self._retry(retry_with_intent)

        if self.blocked:
            await self.db.mark_unwritable(self.blocked)
            self.blocked = []

    async def _retry(self, peers: list[int]) -> None:
        """Человек давно не писал сам — пробуем отправить как рассылку."""
        try:
            items = await self._call(peers, intent=NEWSLETTER_INTENT)
        except VkApiError as err:
            if err.code == INTENT_FORBIDDEN:
                logger.warning("Интент «%s» сообществу недоступен — больше не пробую",
                               NEWSLETTER_INTENT)
                self.intent_ok = False
                self.failed += len(peers)
                return
            raise

        for item in items:
            error = item.get("error") or {}
            code = error.get("code")
            if not code:
                self.sent += 1
            elif code == INTENT_FORBIDDEN:
                self.intent_ok = False
                self.failed += 1
            elif code in DAY_LIMIT_CODES:
                raise Stop(f"VK: {error.get('description') or code}")
            else:
                self.failed += 1
                self.blocked.append(item["peer_id"])

    async def _call(self, peers: list[int], intent: str | None = None) -> list[dict]:
        """Одна пачка с повторами: random_id тот же, поэтому дублей не будет."""
        random_id = random.getrandbits(31)
        for attempt in range(BATCH_TRIES):
            try:
                return await self.api.send_many(
                    peers, self.body, self.photo, intent=intent, random_id=random_id
                )
            except VkApiError as err:
                if err.code in DAY_LIMIT_CODES:
                    raise Stop(f"VK: {err.message}") from err
                if err.code in BROKEN_MESSAGE_CODES:
                    raise Stop(f"VK не принял сообщение: {err.message}", fatal=True) from err
                if err.code in RATE_LIMIT_CODES and attempt < BATCH_TRIES - 1:
                    await asyncio.sleep(RATE_LIMIT_PAUSE * (attempt + 1))
                    continue
                logger.warning("Пачка из %s адресатов не ушла: %s", len(peers), err)
                self.failed += len(peers)
                return []
        return []


async def deliver(api: VkApi, db: Database, registry: UniversityRegistry, row) -> None:
    """Отправка с сохранением прогресса: перезапуск посреди рассылки не начинает её заново.

    Список получателей фиксируется в базе на старте, дальше после каждой пачки
    в базу уходит, до кого уже дошли. После рестарта рассылка подхватывается
    с этой отметки — заново уйдёт максимум последняя пачка.
    """
    targets: list[int] = list(row["target_ids"] or [])
    if not targets:
        targets = await resolve_targets(db, row)
        await db.save_broadcast_targets(row["id"], targets)

    offset = row["sent_offset"] or 0
    if offset:
        logger.info("Рассылка %s продолжается с %s-го получателя", row["id"], offset + 1)

    sender = Sender(api, db, row["body"], row["photo_id"])
    sender.sent, sender.failed = row["sent"] or 0, row["failed"] or 0

    day = _today()
    # своё ограничение включают руками; по умолчанию слушаем сигналы VK
    quota = max(DAILY_LIMIT - await db.daily_sent(day), 0) if DAILY_LIMIT else None
    position = offset

    while position < len(targets):
        # организатор мог нажать «отменить» уже после старта — проверяем каждую пачку
        if await db.broadcast_status(row["id"]) != "sending":
            await db.save_broadcast_progress(row["id"], position, sender.sent, sender.failed)
            await db.finish_broadcast_canceled(row["id"], sender.sent, sender.failed)
            logger.info("Рассылка %s отменена на %s-м получателе", row["id"], position)
            await _report(
                api, row["created_by"],
                f"⛔️ Рассылка #{row['id']} остановлена.\n"
                f"Успели отправить: {sender.sent}, осталось: {len(targets) - position}.",
            )
            return

        size = SEND_BATCH if quota is None else min(SEND_BATCH, quota)
        if size <= 0:
            await _pause_until_tomorrow(api, db, row, position, sender, len(targets),
                                        f"дневной лимит ({DAILY_LIMIT} сообщений) исчерпан")
            return

        chunk = targets[position:position + size]
        try:
            await sender.send_batch(chunk)
        except Stop as stop:
            if stop.fatal:
                await _abort(api, db, row, position, sender, stop.reason)
            else:
                await _pause_until_tomorrow(api, db, row, position, sender,
                                            len(targets), stop.reason)
            return

        position += len(chunk)
        if quota is not None:
            quota -= len(chunk)
        await db.save_broadcast_progress(row["id"], position, sender.sent, sender.failed)
        await db.bump_daily_sent(day, len(chunk))
        await asyncio.sleep(SEND_PAUSE)

    await db.finish_broadcast(row["id"], sender.sent, sender.failed)
    logger.info("Рассылка %s: доставлено %s, не дошло %s", row["id"], sender.sent, sender.failed)

    if row["created_by"]:
        where = "в беседы вузов" if row["kind"] == "chats" else "пользователям"
        await _report(
            api, row["created_by"],
            f"📣 Рассылка #{row['id']} {where} отправлена.\n"
            f"Доставлено: {sender.sent}, не дошло: {sender.failed}",
        )


async def _pause_until_tomorrow(api: VkApi, db: Database, row, position: int,
                                sender: "Sender", total: int, reason: str) -> None:
    """VK попросил притормозить: отмечаемся в базе и ставим хвост на завтра."""
    await db.save_broadcast_progress(row["id"], position, sender.sent, sender.failed)
    when = _tomorrow()
    await db.reschedule_broadcast(row["id"], when)
    left = total - position
    logger.info("Рассылка %s на паузе (%s): отправлено %s, осталось %s — продолжу %s",
                row["id"], reason, sender.sent, left, when)
    await _report(
        api, row["created_by"],
        f"⏸ Рассылка #{row['id']} на паузе: {reason}.\n"
        f"Отправлено: {sender.sent}, осталось: {left}.\n"
        f"Продолжу автоматически {when:%d.%m в %H:%M} МСК.",
    )


async def _abort(api: VkApi, db: Database, row, position: int, sender: "Sender",
                 reason: str) -> None:
    """Чинить нужно саму рассылку — ждать завтра бессмысленно."""
    await db.save_broadcast_progress(row["id"], position, sender.sent, sender.failed)
    await db.finish_broadcast(row["id"], sender.sent, sender.failed)
    logger.warning("Рассылка %s остановлена: %s", row["id"], reason)
    await _report(
        api, row["created_by"],
        f"❌ Рассылка #{row['id']} остановлена: {reason}.\n"
        f"Успели отправить: {sender.sent}. Поправь текст или картинку и запусти заново.",
    )


async def _report(api: VkApi, user_id: int | None, text: str) -> None:
    if not user_id:
        return
    try:
        await api.send(user_id, text)
    except VkApiError:
        pass
