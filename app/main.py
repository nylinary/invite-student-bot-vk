import asyncio
import logging
import os
import sys

from app.bot import Ctx
from app.broadcaster import run_broadcaster
from app.config import Config
from app.db import Database
from app.handlers import dispatch
from app.chats import run_chat_factory
from app.importer import run_dialog_sync
from app.universities import UniversityRegistry
from app.vk import VkApi

logger = logging.getLogger(__name__)

# до какого события дочитали Long Poll — чтобы после перезапуска продолжить с него
TS_SETTING = "longpoll_ts"


def _safe_dsn(dsn: str) -> str:
    """host:port/db без логина и пароля — чтобы не светить их в логах."""
    tail = dsn.rsplit("@", 1)[-1]
    return tail.split("?", 1)[0]


async def _handle(ctx: Ctx, update: dict) -> None:
    try:
        await dispatch(ctx, update)
    except Exception:  # noqa: BLE001 — одно кривое событие не должно ронять бота
        logger.exception("Не смог обработать событие %s", update.get("type"))


async def run() -> None:
    config = Config.from_env()
    db = Database(config.database_url, config.db_schema)
    await db.connect()

    # список вузов живёт в базе, файл нужен только для первого запуска
    seeded = await db.seed_universities(UniversityRegistry.read_seed(config.universities_file))
    if seeded:
        logger.info("Список вузов перенесён в базу: %d шт.", seeded)
    registry = UniversityRegistry.from_rows(await db.all_universities())
    logger.info("База подключена: %s (схема %s)", _safe_dsn(config.database_url), config.db_schema)

    api = VkApi(config.vk_token, config.group_id)
    ctx = Ctx(api=api, db=db, registry=registry, config=config)

    flagged = await db.flag_bursts()
    if flagged:
        logger.warning("Детектор накрутки: помечено %d вступлений из истории", flagged)

    stuck = await db.requeue_stuck_broadcasts()
    if stuck:
        logger.warning("Рассылки %s прервались перезапуском — вернул в очередь", stuck)
    broadcaster = asyncio.create_task(run_broadcaster(api, db, registry))
    dialogs: asyncio.Task | None = None
    factory: asyncio.Task | None = None

    try:
        await api.setup()
        await api.ensure_longpoll()
        # диалоги сообщества подтягиваем сами: люди, писавшие до бота, тоже получают рассылки
        dialogs = asyncio.create_task(run_dialog_sync(api, db))
        # беседы вузов заводятся фоновой очередью: VK не даёт создавать их подряд
        factory = asyncio.create_task(run_chat_factory(ctx))
        logger.info(
            "Запускаю vk.me/%s (club%d): %d вузов, админов %d (по id: %d, по адресу: %d)",
            api.screen_name, api.group_id, len(registry.items),
            len(config.admin_ids) + len(config.admin_usernames),
            len(config.admin_ids), len(config.admin_usernames),
        )
        if not config.has_admins:
            logger.warning("ADMIN_IDS пуст — админские команды не сработают ни у кого")

        # НЕ начинаем с чистого листа: пока бот перезапускался, кто-то вступал в беседы,
        # и эти события — единственный источник правды по приглашённым.
        ts = await db.get_setting(TS_SETTING, "") or None
        async for updates, ts in api.listen(ts):
            # пачку обрабатываем целиком и только потом запоминаем ts:
            # упадём посередине — после рестарта пачка придёт ещё раз
            await asyncio.gather(*(_handle(ctx, update) for update in updates))
            await db.set_setting(TS_SETTING, str(ts))
    finally:
        for task in (dialogs, factory):
            if task is not None:
                task.cancel()
        broadcaster.cancel()
        await db.close()
        await api.close()


def _setup_logging() -> None:
    """Обычные логи в stdout, предупреждения и ошибки в stderr.

    Railway красит всё, что пришло в stderr, как ошибку — иначе в логах
    каждая строчка выглядит аварией и настоящие проблемы теряются.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")

    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(lambda record: record.levelno < logging.WARNING)

    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        handlers=[out, err],
    )


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")


if __name__ == "__main__":
    main()
