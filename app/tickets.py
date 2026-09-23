"""Уровни билетов за приглашённых."""

import hashlib

# (сколько нужно привести, название, что даёт)
TIERS: tuple[tuple[int, str, str], ...] = (
    (50, "🎪 Сцена и бэкстейдж", "проход на сцену, бэкстейдж, фото с артистами"),
    (25, "🎟 VIP-билет", "VIP-вход на вечеринку"),
    (5, "🎫 Бесплатный вход", "бесплатный вход на вечеринку"),
)

MIN_INVITED = TIERS[-1][0]


def tier_for(invited: int) -> tuple[int, str, str] | None:
    """Самый высокий уровень, который человек уже заработал."""
    for need, name, perks in TIERS:
        if invited >= need:
            return need, name, perks
    return None


def next_tier(invited: int) -> tuple[int, str, str] | None:
    """Следующий уровень, до которого ещё нужно дотянуть."""
    better = [t for t in TIERS if t[0] > invited]
    return better[-1] if better else None


def ticket_code(user_id: int, salt: str) -> str:
    """Короткий код для проверки на входе. Одинаковый для одного человека."""
    digest = hashlib.sha256(f"{user_id}:{salt}".encode()).hexdigest()[:6].upper()
    return f"NS-{digest}"
