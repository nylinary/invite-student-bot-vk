import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_admins(raw: str) -> tuple[set[int], set[str]]:
    """«123456789, durov, vk.com/id42, @ivan» -> ({123456789, 42}, {"durov", "ivan"})."""
    ids: set[int] = set()
    names: set[str] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = re.sub(r"^(https?://)?(m\.)?vk\.(com|ru)/", "", chunk.strip()).lstrip("@")
        if not chunk:
            continue
        if chunk.isdigit():
            ids.add(int(chunk))
        elif re.fullmatch(r"id\d+", chunk):
            ids.add(int(chunk[2:]))
        else:
            names.add(chunk.lower())
    return ids, names


@dataclass(slots=True)
class Config:
    vk_token: str
    database_url: str
    group_id: int = 0
    admin_ids: set[int] = field(default_factory=set)
    admin_usernames: set[str] = field(default_factory=set)
    db_schema: str = "public"
    universities_file: Path = BASE_DIR / "universities.json"
    organizer: str = "@kakputinn"
    event_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("VK_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "VK_TOKEN не задан. Создай ключ доступа сообщества (Управление → Работа с API) "
                "с правами на сообщения, документы и фотографии."
            )

        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            raise RuntimeError(
                "DATABASE_URL не задан. На Railway пропиши ${{Postgres.DATABASE_URL}}, "
                "локально — строку подключения к своей базе."
            )

        admin_ids, admin_usernames = _parse_admins(os.environ.get("ADMIN_IDS", ""))
        raw_group = os.environ.get("GROUP_ID", "").strip().lstrip("-").removeprefix("club")

        return cls(
            vk_token=token,
            database_url=dsn,
            group_id=int(raw_group) if raw_group.isdigit() else 0,
            admin_ids=admin_ids,
            admin_usernames=admin_usernames,
            db_schema=os.environ.get("DB_SCHEMA", "public").strip() or "public",
            universities_file=Path(
                os.environ.get("UNIVERSITIES_FILE", BASE_DIR / "universities.json")
            ),
            organizer=os.environ.get("ORGANIZER", "@kakputinn").strip(),
            event_url=os.environ.get("EVENT_URL", "").strip(),
        )

    def is_admin(self, user_id: int, username: str | None = None) -> bool:
        """Админ — либо по id, либо по короткому адресу страницы.

        Адрес удобнее выдавать, но человек может его сменить: тогда доступ
        пропадёт, а освободившийся адрес может занять кто-то другой. Для постоянных
        организаторов надёжнее id — его видно по команде /id.
        """
        if user_id in self.admin_ids:
            return True
        return bool(username) and username.lower() in self.admin_usernames

    @property
    def has_admins(self) -> bool:
        return bool(self.admin_ids or self.admin_usernames)
