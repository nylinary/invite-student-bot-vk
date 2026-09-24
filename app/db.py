"""Слой доступа к данным (PostgreSQL через asyncpg).

Пул соединений на процесс. Схему создаём на старте — отдельных миграций нет,
таблиц мало и они аддитивные.
"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

# Накрутка купленными аккаунтами. Живые люди, даже когда ссылку кидают в большой чат,
# вступают по 2-3 за десять секунд (максимум по реальным данным — 3), а фермы
# заливают 40-70 аккаунтов в одну и ту же секунду. Порог 5 за ±5 секунд
# отделяет одних от других с запасом.
BURST_WINDOW = "5 seconds"
BURST_MIN = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    username       TEXT,
    full_name      TEXT,
    university_key TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Привязка «вуз -> беседа VK». chat_id — это peer_id беседы (2000000000 + номер);
-- появляется после того, как бота сделали админом беседы и там выполнили /bind <ключ>.
-- Беседы без вуза (общий чат мероприятия) живут здесь же с ключом вида «chat:obshchiy»:
-- они получают рассылки, но в статистику по вузам и в список для студентов не идут.
CREATE TABLE IF NOT EXISTS chats (
    university_key TEXT PRIMARY KEY,
    chat_id        BIGINT NOT NULL UNIQUE,
    title          TEXT,
    bound_by       BIGINT,
    bound_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Персональная реферальная ссылка на бота (vk.me/...?ref=...): своя на пару (человек, вуз).
-- chat_id — беседа вуза, в которую она зовёт.
CREATE TABLE IF NOT EXISTS invite_links (
    user_id        BIGINT NOT NULL,
    university_key TEXT NOT NULL,
    chat_id        BIGINT NOT NULL,
    link           TEXT NOT NULL UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, university_key)
);
CREATE INDEX IF NOT EXISTS idx_invite_links_link ON invite_links(link);

-- Факт вступления в чат. Ключ (chat_id, user_id): человек состоит в чате
-- либо не состоит, повторное вступление обновляет запись.
CREATE TABLE IF NOT EXISTS joins (
    chat_id        BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    username       TEXT,
    full_name      TEXT,
    university_key TEXT,
    link           TEXT,
    owner_id       BIGINT,
    joined_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at        TIMESTAMPTZ,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_joins_owner ON joins(owner_id);
CREATE INDEX IF NOT EXISTS idx_joins_user ON joins(user_id);
CREATE INDEX IF NOT EXISTS idx_joins_university ON joins(university_key);

-- Список вузов живёт в базе, чтобы админ мог править его прямо из бота.
-- universities.json остаётся только сидом для первого запуска.
CREATE TABLE IF NOT EXISTS universities (
    key           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    fallback_link TEXT,
    aliases       TEXT[] NOT NULL DEFAULT '{}',
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Рассылки: и мгновенные, и отложенные — разница только в scheduled_at.
CREATE TABLE IF NOT EXISTS broadcasts (
    id           SERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,                     -- chats | users
    body         TEXT,
    photo_id     TEXT,
    targets      TEXT[],                            -- ключи вузов; NULL = все чаты
    audience     TEXT,                              -- для kind=users
    scheduled_at TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending|sending|done|canceled
    created_by   BIGINT,
    sent         INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_broadcasts_due ON broadcasts(status, scheduled_at);
-- список получателей и отметка прогресса: чтобы перезапуск посреди отправки
-- не начинал рассылку заново и не терял хвост
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS target_ids BIGINT[];
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS sent_offset INTEGER NOT NULL DEFAULT 0;

-- Беседа доведена до готовности: есть права, ава, закреп и ссылка-приглашение.
-- Беседу мог создать человек и выдать права не сразу — тогда бот дооформит её позже.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS ready BOOLEAN NOT NULL DEFAULT FALSE;

-- Откуда человек взялся: 'bot' — писал боту сам, 'import' — подтянут из диалогов
-- сообщества (ему можно писать, но личную ссылку он ещё не брал).
ALTER TABLE users ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'bot';
-- можно ли писать человеку: VK отдаёт это в списке диалогов
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_write BOOLEAN NOT NULL DEFAULT TRUE;

-- вступление похоже на накрутку (массовый залив по одной ссылке) — в зачёт не идёт
ALTER TABLE joins ADD COLUMN IF NOT EXISTS suspicious BOOLEAN NOT NULL DEFAULT FALSE;

-- Кто кого позвал: человек пришёл в бота по чужой реферальной ссылке.
-- Засчитывается, когда он вступает в беседу этого вуза. Последняя ссылка побеждает.
CREATE TABLE IF NOT EXISTS referrals (
    user_id        BIGINT PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    university_key TEXT NOT NULL,
    link           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Пригласившие, которых админ вручную признал честными: детектор их не трогает.
CREATE TABLE IF NOT EXISTS trusted_inviters (
    owner_id   BIGINT PRIMARY KEY,
    trusted_by BIGINT,
    trusted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Переключатели, которые организатор щёлкает из бота (например, выдача билетов).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Выданные билеты: один на человека, уровень растёт вместе с числом приглашённых.
CREATE TABLE IF NOT EXISTS tickets (
    user_id        BIGINT PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    tier           TEXT NOT NULL,
    invited        INTEGER NOT NULL,
    university_key TEXT,
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Database:
    def __init__(self, dsn: str, schema: str = "public"):
        self.dsn = dsn
        self.schema = schema
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
            server_settings={"search_path": self.schema},
        )
        async with self._pool.acquire() as conn:
            if self.schema != "public":
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ───────────── пользователи ─────────────

    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> None:
        """Человек написал боту: он точно «наш», даже если раньше был подтянут из диалогов."""
        await self.pool.execute(
            """
            INSERT INTO users (user_id, username, full_name) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                source = 'bot',
                can_write = TRUE,
                updated_at = now()
            """,
            user_id, username, full_name,
        )

    async def import_users(self, rows: list[tuple[int, str | None, str, bool]]) -> int:
        """Диалоги сообщества -> таблица users. Тех, кто уже писал боту, не переписываем в «импорт»."""
        if not rows:
            return 0
        result = await self.pool.executemany(
            """
            INSERT INTO users (user_id, username, full_name, source, can_write)
            VALUES ($1, $2, $3, 'import', $4)
            ON CONFLICT (user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                full_name = COALESCE(NULLIF(EXCLUDED.full_name, ''), users.full_name),
                can_write = EXCLUDED.can_write,
                updated_at = now()
            """,
            rows,
        )
        return len(rows) if result is None else len(rows)

    async def mark_unwritable(self, user_ids: list[int]) -> None:
        """VK отказался доставлять — больше не тратим на них попытки и дневной лимит."""
        if not user_ids:
            return
        await self.pool.execute(
            "UPDATE users SET can_write = FALSE, updated_at = now() WHERE user_id = ANY($1::bigint[])",
            user_ids,
        )

    async def count_by_source(self) -> dict[str, int]:
        """{'bot': писали боту, 'import': подтянуты из диалогов, 'writable': кому можно писать}."""
        row = await self.pool.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE source = 'bot')    AS bot,
                   COUNT(*) FILTER (WHERE source = 'import') AS imported,
                   COUNT(*) FILTER (WHERE can_write)         AS writable
            FROM users
            """
        )
        return {"bot": row["bot"], "import": row["imported"], "writable": row["writable"]}

    async def set_user_university(self, user_id: int, key: str) -> None:
        await self.pool.execute(
            "UPDATE users SET university_key = $2, updated_at = now() WHERE user_id = $1",
            user_id, key,
        )

    async def get_user_university(self, user_id: int) -> str | None:
        return await self.pool.fetchval(
            "SELECT university_key FROM users WHERE user_id = $1", user_id
        )

    async def admin_user_ids(self, usernames: set[str]) -> list[int]:
        """id админов, заданных через @username — узнаём их из тех, кто писал боту."""
        if not usernames:
            return []
        rows = await self.pool.fetch(
            "SELECT user_id FROM users WHERE lower(username) = ANY($1::text[])",
            [u.lower() for u in usernames],
        )
        return [row["user_id"] for row in rows]

    async def count_users(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM users")

    # ───────────── привязка чатов ─────────────

    async def bind_chat(self, key: str, chat_id: int, title: str, bound_by: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # этот чат мог быть привязан к другому вузу — снимаем старую привязку
                await conn.execute(
                    "DELETE FROM chats WHERE chat_id = $1 AND university_key <> $2", chat_id, key
                )
                await conn.execute(
                    """
                    INSERT INTO chats (university_key, chat_id, title, bound_by)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (university_key) DO UPDATE SET
                        chat_id = EXCLUDED.chat_id,
                        title = EXCLUDED.title,
                        bound_by = EXCLUDED.bound_by,
                        bound_at = now(),
                        -- вуз переехал в другую беседу: её ещё предстоит дооформить
                        ready = (chats.chat_id = EXCLUDED.chat_id AND chats.ready)
                    """,
                    key, chat_id, title, bound_by,
                )
                # Вступления, записанные до привязки, лежали без вуза («—» в админке).
                # Чат теперь опознан — проставляем им вуз задним числом.
                await conn.execute(
                    "UPDATE joins SET university_key = $1 WHERE chat_id = $2 "
                    "AND university_key IS DISTINCT FROM $1",
                    key, chat_id,
                )

    async def unbind_chat(self, key: str) -> bool:
        row = await self.pool.fetchrow(
            "DELETE FROM chats WHERE university_key = $1 RETURNING university_key", key
        )
        return row is not None

    async def get_chat(self, key: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM chats WHERE university_key = $1", key)

    async def get_chat_by_id(self, chat_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM chats WHERE chat_id = $1", chat_id)

    async def all_chats(self) -> list[asyncpg.Record]:
        return await self.pool.fetch("SELECT * FROM chats ORDER BY university_key")

    async def unready_chats(self) -> list[asyncpg.Record]:
        """Беседы вузов, которые ещё не дооформлены (обычно ждут прав администратора)."""
        return await self.pool.fetch(
            "SELECT * FROM chats WHERE NOT ready AND university_key NOT LIKE 'chat:%' "
            "ORDER BY bound_at"
        )

    async def mark_chat_ready(self, chat_id: int) -> None:
        await self.pool.execute("UPDATE chats SET ready = TRUE WHERE chat_id = $1", chat_id)

    async def free_chat_key(self, base: str) -> str:
        """Ключ для беседы без вуза: chat:obshchiy, chat:obshchiy2 и так далее."""
        taken = {row["university_key"] for row in await self.all_chats()}
        key, n = f"chat:{base}", 2
        while key in taken:
            key, n = f"chat:{base}{n}", n + 1
        return key

    # ───────────── пригласительные ссылки ─────────────

    async def get_invite_link(self, user_id: int, key: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM invite_links WHERE user_id = $1 AND university_key = $2", user_id, key
        )

    async def save_invite_link(self, user_id: int, key: str, chat_id: int, link: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO invite_links (user_id, university_key, chat_id, link)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, university_key) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                link = EXCLUDED.link,
                created_at = now()
            """,
            user_id, key, chat_id, link,
        )

    async def drop_invite_links(self, chat_id: int) -> int:
        """Бота выгнали из беседы — выданные под неё ссылки выбрасываем.

        Вступления в беседу без бота не отследить, поэтому звать туда по «личной»
        ссылке — обман. Вернут бота — ссылки выдадутся заново.
        Записи о прошлых вступлениях и кто кого позвал остаются нетронутыми.
        """
        rows = await self.pool.fetch(
            "DELETE FROM invite_links WHERE chat_id = $1 RETURNING user_id", chat_id
        )
        return len(rows)

    async def find_link_owner(self, link: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM invite_links WHERE link = $1", link)

    # ───────────── кто кого позвал ─────────────

    async def save_referral(self, user_id: int, owner_id: int, key: str, link: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO referrals (user_id, owner_id, university_key, link)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                university_key = EXCLUDED.university_key,
                link = EXCLUDED.link,
                created_at = now()
            """,
            user_id, owner_id, key, link,
        )

    async def get_referral(self, user_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM referrals WHERE user_id = $1", user_id)

    # ───────────── вступления ─────────────

    async def record_join(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        full_name: str,
        university_key: str | None,
        link: str | None,
        owner_id: int | None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO joins (chat_id, user_id, username, full_name, university_key, link, owner_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                university_key = EXCLUDED.university_key,
                link = COALESCE(EXCLUDED.link, joins.link),
                owner_id = COALESCE(EXCLUDED.owner_id, joins.owner_id),
                joined_at = now(),
                -- вернулся в чат: снова засчитываем, запись одна на человека,
                -- поэтому цикл «вышел-зашёл» не накручивает счётчик
                left_at = NULL
            """,
            chat_id, user_id, username, full_name, university_key, link, owner_id,
        )

    async def record_leave(self, chat_id: int, user_id: int) -> None:
        await self.pool.execute(
            "UPDATE joins SET left_at = now() WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
        )

    async def owner_stats(self, owner_id: int, key: str | None = None) -> tuple[int, int]:
        """(сколько человек он привёл, из них сейчас в чате).

        Собственное вступление владельца по своей же ссылке не считается
        приглашением — иначе счётчик у всех стартовал бы с единицы.
        """
        row = await self.pool.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE left_at IS NULL AND NOT suspicious) AS active
            FROM joins
            WHERE owner_id = $1 AND user_id <> owner_id
              AND ($2::text IS NULL OR university_key = $2)
            """,
            owner_id, key,
        )
        return row["total"], row["active"]

    async def flag_bursts(self, owner_id: int | None = None) -> int:
        """Помечает массовые заливы по одной ссылке. Возвращает, сколько пометил впервые."""
        rows = await self.pool.fetch(
            f"""
            UPDATE joins j SET suspicious = TRUE
            FROM (
                SELECT chat_id, user_id,
                       COUNT(*) OVER (
                           PARTITION BY owner_id ORDER BY joined_at
                           RANGE BETWEEN INTERVAL '{BURST_WINDOW}' PRECEDING
                                     AND INTERVAL '{BURST_WINDOW}' FOLLOWING
                       ) AS around
                FROM joins
                WHERE owner_id IS NOT NULL AND owner_id <> user_id
                  AND ($1::bigint IS NULL OR owner_id = $1)
                  AND owner_id NOT IN (SELECT owner_id FROM trusted_inviters)
            ) burst
            WHERE j.chat_id = burst.chat_id AND j.user_id = burst.user_id
              AND burst.around >= $2 AND NOT j.suspicious
            RETURNING j.user_id
            """,
            owner_id, BURST_MIN,
        )
        return len(rows)

    async def flagged_count(self, owner_id: int, key: str | None = None) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM joins WHERE owner_id = $1 AND suspicious "
            "AND ($2::text IS NULL OR university_key = $2)",
            owner_id, key,
        )

    async def is_suspicious(self, chat_id: int, user_id: int) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT suspicious FROM joins WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
        ))

    async def fraud_report(self, limit: int = 30) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT j.owner_id, u.username, u.full_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE j.suspicious) AS flagged,
                   COUNT(*) FILTER (WHERE j.left_at IS NOT NULL AND NOT j.suspicious) AS gone,
                   COUNT(*) FILTER (WHERE j.left_at IS NULL AND NOT j.suspicious) AS counted
            FROM joins j LEFT JOIN users u ON u.user_id = j.owner_id
            WHERE j.owner_id IS NOT NULL AND j.owner_id <> j.user_id
            GROUP BY j.owner_id, u.username, u.full_name
            HAVING COUNT(*) FILTER (WHERE j.suspicious) > 0
            ORDER BY flagged DESC
            LIMIT $1
            """,
            limit,
        )

    async def trust_inviter(self, owner_id: int, trusted_by: int) -> int:
        """Админ решил, что это не накрутка: снимаем пометки и больше не трогаем."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO trusted_inviters (owner_id, trusted_by) VALUES ($1, $2) "
                    "ON CONFLICT (owner_id) DO NOTHING",
                    owner_id, trusted_by,
                )
                rows = await conn.fetch(
                    "UPDATE joins SET suspicious = FALSE WHERE owner_id = $1 AND suspicious "
                    "RETURNING user_id",
                    owner_id,
                )
        return len(rows)

    async def university_stats(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT university_key,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE left_at IS NULL) AS active,
                   COUNT(*) FILTER (WHERE owner_id IS NOT NULL AND owner_id <> user_id) AS invited
            FROM joins
            GROUP BY university_key
            ORDER BY total DESC
            """
        )

    async def full_stats(self) -> list[asyncpg.Record]:
        """По каждому вузу: сколько выдано личных ссылок и что по ним пришло."""
        return await self.pool.fetch(
            """
            SELECT COALESCE(l.university_key, j.university_key) AS university_key,
                   COALESCE(l.links, 0)   AS links,
                   COALESCE(j.total, 0)   AS total,
                   COALESCE(j.invited, 0) AS invited,
                   COALESCE(j.flagged, 0) AS flagged,
                   COALESCE(j.active, 0)  AS active
            FROM (
                SELECT university_key, COUNT(*) AS links
                FROM invite_links GROUP BY university_key
            ) l
            FULL OUTER JOIN (
                SELECT university_key,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE owner_id IS NOT NULL AND owner_id <> user_id
                                          AND NOT suspicious) AS invited,
                       COUNT(*) FILTER (WHERE suspicious) AS flagged,
                       COUNT(*) FILTER (WHERE left_at IS NULL) AS active
                FROM joins
                WHERE university_key IS NOT NULL
                  AND university_key NOT LIKE 'chat:%'  -- чужие и общие чаты не в счёт
                GROUP BY university_key
            ) j ON j.university_key = l.university_key
            ORDER BY total DESC, links DESC
            """
        )

    async def count_links(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM invite_links")

    async def top_referrers(self, limit: int = 20) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT j.owner_id,
                   u.username,
                   u.full_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE j.left_at IS NULL AND NOT j.suspicious) AS active
            FROM joins j
            LEFT JOIN users u ON u.user_id = j.owner_id
            WHERE j.owner_id IS NOT NULL AND j.owner_id <> j.user_id
            GROUP BY j.owner_id, u.username, u.full_name
            ORDER BY active DESC, total DESC
            LIMIT $1
            """,
            limit,
        )

    async def totals(self) -> tuple[int, int]:
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE left_at IS NULL) AS active "
            "FROM joins WHERE university_key IS NOT NULL AND university_key NOT LIKE 'chat:%'"
        )
        return row["total"], row["active"]

    # ───────────── список вузов ─────────────

    async def all_universities(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM universities ORDER BY position, key"
        )

    async def seed_universities(self, items: list[dict]) -> int:
        """Первый запуск: переносим список из файла в базу. Потом файл не трогаем."""
        if await self.pool.fetchval("SELECT COUNT(*) FROM universities"):
            return 0
        for position, item in enumerate(items):
            await self.pool.execute(
                "INSERT INTO universities (key, title, fallback_link, aliases, position) "
                "VALUES ($1, $2, $3, $4, $5)",
                item["key"], item["title"], item.get("fallback_link"),
                list(item.get("aliases", [])), position,
            )
        return len(items)

    async def upsert_university(
        self, key: str, title: str, link: str | None, aliases: list[str],
        position: int | None = None,
    ) -> None:
        if position is None:
            position = await self.pool.fetchval(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM universities"
            )
        await self.pool.execute(
            """
            INSERT INTO universities (key, title, fallback_link, aliases, position)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (key) DO UPDATE SET
                title = EXCLUDED.title,
                fallback_link = EXCLUDED.fallback_link,
                aliases = EXCLUDED.aliases,
                updated_at = now()
            """,
            key, title, link, aliases, position,
        )

    async def delete_university(self, key: str) -> bool:
        row = await self.pool.fetchrow(
            "DELETE FROM universities WHERE key = $1 RETURNING key", key
        )
        return row is not None

    # ───────────── рассылки ─────────────

    async def create_broadcast(
        self, kind: str, body: str | None, photo_id: str | None,
        targets: list[str] | None, audience: str | None,
        scheduled_at, created_by: int,
    ) -> int:
        return await self.pool.fetchval(
            """
            INSERT INTO broadcasts (kind, body, photo_id, targets, audience,
                                    scheduled_at, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
            """,
            kind, body, photo_id, targets, audience, scheduled_at, created_by,
        )

    async def due_broadcasts(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM broadcasts WHERE status = 'pending' AND scheduled_at <= now() "
            "ORDER BY scheduled_at"
        )

    async def pending_broadcasts(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM broadcasts WHERE status IN ('pending', 'sending') "
            "ORDER BY scheduled_at LIMIT 20"
        )

    async def recent_broadcasts(self, limit: int = 5) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM broadcasts WHERE status IN ('done', 'canceled') "
            "ORDER BY id DESC LIMIT $1", limit,
        )

    async def take_broadcast(self, broadcast_id: int) -> bool:
        """Помечаем «в работе», чтобы две копии воркера не отправили дважды."""
        row = await self.pool.fetchrow(
            "UPDATE broadcasts SET status = 'sending' WHERE id = $1 AND status = 'pending' "
            "RETURNING id", broadcast_id,
        )
        return row is not None

    async def save_broadcast_targets(self, broadcast_id: int, ids: list[int]) -> None:
        await self.pool.execute(
            "UPDATE broadcasts SET target_ids = $2 WHERE id = $1", broadcast_id, ids
        )

    async def save_broadcast_progress(
        self, broadcast_id: int, offset: int, sent: int, failed: int
    ) -> None:
        await self.pool.execute(
            "UPDATE broadcasts SET sent_offset = $2, sent = $3, failed = $4 WHERE id = $1",
            broadcast_id, offset, sent, failed,
        )

    async def requeue_stuck_broadcasts(self) -> list[int]:
        """После перезапуска: то, что осталось в статусе «отправляется», снова в очередь."""
        rows = await self.pool.fetch(
            "UPDATE broadcasts SET status = 'pending' WHERE status = 'sending' RETURNING id"
        )
        return [row["id"] for row in rows]

    async def finish_broadcast(self, broadcast_id: int, sent: int, failed: int) -> None:
        await self.pool.execute(
            "UPDATE broadcasts SET status = 'done', sent = $2, failed = $3, "
            "finished_at = now() WHERE id = $1",
            broadcast_id, sent, failed,
        )

    async def cancel_broadcast(self, broadcast_id: int) -> bool:
        row = await self.pool.fetchrow(
            "UPDATE broadcasts SET status = 'canceled', finished_at = now() "
            "WHERE id = $1 AND status = 'pending' RETURNING id",
            broadcast_id,
        )
        return row is not None

    _AUDIENCE_CTE = """
        WITH inv AS (
            SELECT owner_id AS user_id,
                   COUNT(*) FILTER (WHERE left_at IS NULL AND NOT suspicious) AS cnt
            FROM joins
            WHERE owner_id IS NOT NULL AND owner_id <> user_id
              AND university_key IS NOT NULL AND university_key NOT LIKE 'chat:%'
            GROUP BY owner_id
        ), audience AS (
            SELECT u.user_id, u.source
            FROM users u LEFT JOIN inv ON inv.user_id = u.user_id
            WHERE u.can_write
              AND ($1::int IS NULL OR COALESCE(inv.cnt, 0) >= $1)
              AND ($2::int IS NULL OR COALESCE(inv.cnt, 0) <= $2)
              AND ($3::text IS NULL OR u.source = $3)
        )
    """

    @staticmethod
    def _audience_bounds(audience: str) -> tuple[int | None, int | None, str | None]:
        """all | n:<мин>:<макс> по приглашённым | src:bot|import -> параметры запроса.

        Фильтр по числу приглашённых считает и тех, кто боту не писал: у подтянутых
        из диалогов ноль приглашённых, и в выборку «меньше пяти» они обязаны попадать.
        """
        low = high = source = None
        if audience.startswith("n:"):
            _, raw_low, raw_high = audience.split(":")
            low = int(raw_low) if raw_low else None
            high = int(raw_high) if raw_high else None
        elif audience.startswith("src:"):
            source = audience[4:]
        return low, high, source

    async def audience_user_ids(self, audience: str) -> list[int]:
        """Кому отправлять: all | u:<адрес> | n:<мин>:<макс> | src:bot|import.

        Тех, кто запретил сообщения, не возвращаем вообще: на них всё равно
        уйдёт ошибка, а дневной лимит рассылки они бы съели.
        """
        if audience.startswith("u:"):
            rows = await self.pool.fetch(
                "SELECT user_id FROM users WHERE can_write "
                "AND lower(username) LIKE lower($1) ORDER BY user_id",
                f"%{audience[2:].lstrip('@')}%",
            )
            return [row["user_id"] for row in rows]

        rows = await self.pool.fetch(
            f"{self._AUDIENCE_CTE} SELECT user_id FROM audience ORDER BY user_id",
            *self._audience_bounds(audience),
        )
        return [row["user_id"] for row in rows]

    async def audience_stats(self, audience: str) -> tuple[int, int]:
        """(сколько человек подходит, из них подтянутых из диалогов) — без выгрузки списка."""
        if audience.startswith("u:"):
            total = await self.pool.fetchval(
                "SELECT COUNT(*) FROM users WHERE can_write AND lower(username) LIKE lower($1)",
                f"%{audience[2:].lstrip('@')}%",
            )
            return total, 0
        row = await self.pool.fetchrow(
            f"{self._AUDIENCE_CTE} "
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE source = 'import') AS imported "
            "FROM audience",
            *self._audience_bounds(audience),
        )
        return row["total"], row["imported"]

    async def reschedule_broadcast(self, broadcast_id: int, when) -> None:
        """Упёрлись в дневной лимит VK — рассылка ждёт следующего дня и продолжится с отметки."""
        await self.pool.execute(
            "UPDATE broadcasts SET status = 'pending', scheduled_at = $2 WHERE id = $1",
            broadcast_id, when,
        )

    # ───────────── дневной лимит рассылки ─────────────

    async def daily_sent(self, day: str) -> int:
        return int(await self.get_setting(f"sent:{day}", "0") or 0)

    async def bump_daily_sent(self, day: str, count: int) -> int:
        """Сколько сообщений сообщество отправило за день — лимит общий на все рассылки."""
        return await self.pool.fetchval(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2::bigint::text)
            ON CONFLICT (key) DO UPDATE SET
                value = (settings.value::bigint + $2::bigint)::text, updated_at = now()
            RETURNING value::bigint
            """,
            f"sent:{day}", count,
        )

    # ───────────── настройки и билеты ─────────────

    async def get_setting(self, key: str, default: str = "") -> str:
        value = await self.pool.fetchval("SELECT value FROM settings WHERE key = $1", key)
        return default if value is None else value

    async def set_setting(self, key: str, value: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key, value,
        )

    async def invited_with_own_links(self, owner_id: int) -> int:
        """Сколько из приглашённых сами взяли ссылку в боте и зовут дальше."""
        return await self.pool.fetchval(
            """
            SELECT COUNT(DISTINCT j.user_id)
            FROM joins j
            WHERE j.owner_id = $1 AND j.user_id <> j.owner_id
              AND EXISTS (SELECT 1 FROM invite_links il WHERE il.user_id = j.user_id)
            """,
            owner_id,
        )

    async def owner_invite_counts(self) -> list[int]:
        """По одному числу на каждого, кто хоть кого-то привёл — сколько у него сейчас.

        Считаем так же, как при выдаче билета: только те приглашённые,
        которые сейчас состоят в чате.
        """
        rows = await self.pool.fetch(
            """
            SELECT COUNT(*) FILTER (WHERE left_at IS NULL AND NOT suspicious) AS cnt
            FROM joins
            WHERE owner_id IS NOT NULL AND owner_id <> user_id
              AND university_key IS NOT NULL AND university_key NOT LIKE 'chat:%'
            GROUP BY owner_id
            """
        )
        return [row["cnt"] for row in rows if row["cnt"]]

    _PEOPLE_CTE = """
        WITH inv AS (
            SELECT owner_id AS user_id,
                   COUNT(*) FILTER (WHERE left_at IS NULL AND NOT suspicious) AS cnt
            FROM joins
            WHERE owner_id IS NOT NULL AND owner_id <> user_id
              AND university_key IS NOT NULL AND university_key NOT LIKE 'chat:%'
            GROUP BY owner_id
        ), people AS (
            SELECT u.user_id, u.username, u.full_name, u.university_key,
                   COALESCE(inv.cnt, 0) AS counted
            FROM users u LEFT JOIN inv ON inv.user_id = u.user_id
        )
    """

    async def people_by_invited(
        self, low: int | None, high: int | None, limit: int, offset: int
    ) -> tuple[int, list[asyncpg.Record]]:
        """Участники с засчитанными приглашёнными в диапазоне (для статистики)."""
        where = "($1::int IS NULL OR counted >= $1) AND ($2::int IS NULL OR counted <= $2)"
        total = await self.pool.fetchval(
            f"{self._PEOPLE_CTE} SELECT COUNT(*) FROM people WHERE {where}", low, high
        )
        rows = await self.pool.fetch(
            f"{self._PEOPLE_CTE} SELECT * FROM people WHERE {where} "
            f"ORDER BY counted DESC, user_id LIMIT $3 OFFSET $4",
            low, high, limit, offset,
        )
        return total, rows

    async def people_distribution(self) -> list[int]:
        """Засчитанные приглашённые по каждому, кто писал боту (включая нули)."""
        rows = await self.pool.fetch(f"{self._PEOPLE_CTE} SELECT counted FROM people")
        return [row["counted"] for row in rows]

    async def issue_ticket(
        self, user_id: int, code: str, tier: str, invited: int, university_key: str | None
    ) -> asyncpg.Record:
        """Выдаёт билет или поднимает уровень уже выданного. Код не меняется."""
        return await self.pool.fetchrow(
            """
            INSERT INTO tickets (user_id, code, tier, invited, university_key)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                tier = EXCLUDED.tier,
                invited = EXCLUDED.invited,
                university_key = EXCLUDED.university_key,
                updated_at = now()
            RETURNING *
            """,
            user_id, code, tier, invited, university_key,
        )

    async def get_ticket(self, user_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM tickets WHERE user_id = $1", user_id)

    async def find_ticket(self, code: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            SELECT t.*, u.username, u.full_name
            FROM tickets t LEFT JOIN users u ON u.user_id = t.user_id
            WHERE upper(t.code) = upper($1)
            """,
            code,
        )

    async def tickets_by_tier(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT tier, COUNT(*) AS count FROM tickets GROUP BY tier ORDER BY count DESC"
        )

    async def export_joins(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT j.university_key, j.user_id, j.username, j.full_name,
                   j.owner_id, o.username AS owner_username, o.full_name AS owner_name,
                   j.link, j.joined_at, j.left_at
            FROM joins j
            LEFT JOIN users o ON o.user_id = j.owner_id
            ORDER BY j.joined_at DESC
            """
        )
