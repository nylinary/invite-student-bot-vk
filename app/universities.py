"""Список вузов и распознавание названия из свободного текста."""

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_PUNCT = re.compile(r"[^0-9a-zа-я]+")


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return _PUNCT.sub(" ", text).strip()


@dataclass(slots=True, frozen=True)
class University:
    key: str
    title: str
    fallback_link: str | None
    aliases: tuple[str, ...]


class UniversityRegistry:
    """Список вузов в памяти. Правки из бота применяются сразу, правки мимо бота —
    фоновым обновлением (см. run_registry_sync)."""

    def __init__(self, items: list[University]):
        self.replace(items)

    def replace(self, items: list[University]) -> None:
        """Перестроить список на месте — объект уже роздан хэндлерам по ссылке."""
        self.items = items
        self._by_key = {u.key: u for u in items}
        # нормализованная форма -> ключи вузов; один алиас может вести в несколько
        # (например «кирова» — это и Лесотехнический, и Военно-медицинская),
        # тогда бот покажет варианты вместо того, чтобы угадывать
        self._index: dict[str, list[str]] = {}
        for u in items:
            for name in (u.title, u.key, *u.aliases):
                norm = normalize(name)
                if norm and u.key not in self._index.setdefault(norm, []):
                    self._index[norm].append(u.key)

    @classmethod
    def from_rows(cls, rows) -> "UniversityRegistry":
        return cls([
            University(
                key=row["key"],
                title=row["title"],
                fallback_link=row["fallback_link"] or None,
                aliases=tuple(row["aliases"] or ()),
            )
            for row in rows
        ])

    def apply_rows(self, rows) -> None:
        self.replace(UniversityRegistry.from_rows(rows).items)

    @staticmethod
    def read_seed(path: Path) -> list[dict]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def load(cls, path: Path) -> "UniversityRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        items = [
            University(
                key=item["key"],
                title=item["title"],
                fallback_link=item.get("fallback_link") or None,
                aliases=tuple(item.get("aliases", [])),
            )
            for item in raw
        ]
        if not items:
            raise ValueError(f"Список вузов пуст: {path}")
        return cls(items)

    def get(self, key: str) -> University | None:
        return self._by_key.get(key)

    def title(self, key: str | None) -> str:
        uni = self._by_key.get(key or "")
        return uni.title if uni else (key or "—")

    def match(self, text: str) -> list[University]:
        """Ищет вуз по свободному тексту. Пусто — не нашли, >1 — неоднозначно."""
        norm = normalize(text)
        if not norm:
            return []

        if norm in self._index:
            return [self._by_key[key] for key in self._index[norm]]

        # алиас встречается внутри фразы («учусь в итмо на 2 курсе»)
        hits: dict[str, int] = {}
        for alias, keys in self._index.items():
            if len(alias) < 3:
                continue
            if re.search(rf"(?<![0-9a-zа-я]){re.escape(alias)}(?![0-9a-zа-я])", norm):
                for key in keys:
                    hits[key] = max(hits.get(key, 0), len(alias))
        if hits:
            best = max(hits.values())
            # если одно совпадение длиннее прочих — берём его (спбгу > гу)
            winners = [k for k, v in hits.items() if v == best]
            return [self._by_key[k] for k in winners]

        # опечатки
        close = difflib.get_close_matches(norm, self._index.keys(), n=3, cutoff=0.72)
        seen: list[str] = []
        for alias in close:
            for key in self._index[alias]:
                if key not in seen:
                    seen.append(key)
        return [self._by_key[k] for k in seen[:3]]
