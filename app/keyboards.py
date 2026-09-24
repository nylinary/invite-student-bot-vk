"""Inline-клавиатуры. У VK лимит 10 кнопок и 6 рядов — поэтому страницы короче, чем в Telegram."""

from urllib.parse import quote

from app.universities import University
from app.vk import Keyboard

# 6 вузов + перелистывание + выход = 9 кнопок при лимите VK в 10
PAGE_SIZE = 6


def total_pages(items: list[University]) -> int:
    return max(1, -(-len(items) // PAGE_SIZE))


def universities_kb(items: list[University], page: int = 0, is_admin: bool = False) -> Keyboard:
    """Страница списка вузов: кнопки вузов, перелистывание и выход с экрана."""
    pages = total_pages(items)
    page %= pages  # с последней страницы «вперёд» уводит на первую

    kb = Keyboard()
    for uni in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        kb.button(uni.title, f"uni:{uni.key}")
    kb.adjust(2)

    if pages > 1:
        kb.row(
            Keyboard.btn("‹ Назад", f"list:{page - 1}"),
            Keyboard.btn("Вперёд ›", f"list:{page + 1}"),
        )
    # с любого экрана должно быть куда выйти: VK старые кнопки не показывает
    kb.row(Keyboard.btn("🛠 Админка", "adm:home") if is_admin
           else Keyboard.btn("🏠 В начало", "home"))
    return kb


def suggestions_kb(items: list[University]) -> Keyboard:
    kb = Keyboard()
    for uni in items[:8]:
        kb.button(uni.title, f"uni:{uni.key}")
    kb.button("📋 Все вузы", "list")
    kb.adjust(1) if len(items) < 5 else kb.adjust(2)
    return kb


def invite_kb(link: str | None, ref_link: str | None, key: str, tickets: bool = False,
              single: bool = False) -> Keyboard:
    kb = Keyboard()
    if link:
        kb.button("✅ Вступить в чат" if single else "✅ Вступить в беседу вуза", url=link)
    if ref_link:
        kb.button("📤 Поделиться ссылкой", url=share_url(ref_link))
    kb.button("📊 Моя статистика", f"stats:{key}")
    if tickets:
        kb.button("🎫 Получить билет", "ticket")
    if not single:
        kb.button("📋 Другой вуз", "list")
    kb.adjust(1)
    return kb


def menu_kb(is_admin: bool = False, single: bool = False) -> Keyboard:
    """Постоянное меню под полем ввода: в VK такая клавиатура живёт до замены."""
    kb = Keyboard(inline=False)
    if not single:            # в режиме одного чата вузы студенту не нужны
        kb.button("📋 Вузы", "list")
    kb.button("📊 Моя статистика", "stats:")
    kb.button("ℹ️ Как получить билет", "help")
    if is_admin:
        kb.button("🛠 Админка", "adm:home")
    kb.adjust(2)
    return kb


def back_kb() -> Keyboard:
    return Keyboard().button("📋 Список вузов", "list")


def student_kb(key: str | None = None, tickets: bool = False) -> Keyboard:
    """Куда уйти с любого экрана студента: свой вуз, статистика, список."""
    kb = Keyboard()
    if key:
        kb.button("📊 Моя статистика", f"stats:{key}")
    if tickets:
        kb.button("🎫 Получить билет", "ticket")
    kb.button("📋 Список вузов", "list")
    kb.adjust(1)
    return kb


def share_url(link: str) -> str:
    text = "Идём на самую большую студенческую вечеринку 26 сентября! Бери ссылку в беседу нашего вуза:"
    return f"https://vk.com/share.php?url={quote(link, safe='')}&title={quote(text, safe='')}"


def chat_welcome_kb(dialog_url: str) -> Keyboard:
    """Кнопка из беседы вуза в бота, сразу с выбранным вузом."""
    return Keyboard().button("🔗 Получить свою ссылку", url=dialog_url)


def admin_kb() -> Keyboard:
    kb = Keyboard()
    kb.button("📊 По вузам", "adm:unis")
    kb.button("🏆 Топ пригласивших", "adm:top")
    kb.button("👤 Участники", "adm:people")
    kb.button("🔗 Беседы с ботом", "adm:chats")
    kb.button("🔍 Проверить привязки", "adm:check")
    kb.button("🎫 Билеты", "adm:tickets")
    kb.button("🚨 Накрутка", "adm:fraud")
    kb.button("📣 Рассылки", "adm:bc")
    kb.button("🎓 Список вузов", "adm:edu")
    kb.button("📥 Выгрузить CSV", "adm:csv")
    kb.adjust(2)
    return kb


def tickets_kb(enabled: bool) -> Keyboard:
    kb = Keyboard()
    kb.button(
        "🔴 Выключить кнопку билета" if enabled else "🟢 Включить кнопку билета",
        "adm:tickets_toggle",
    )
    kb.button("⬅️ В админку", "adm:home")
    kb.adjust(1)
    return kb


def admin_back_kb() -> Keyboard:
    return Keyboard().button("⬅️ В админку", "adm:home")


def greeting_kb(is_admin: bool = False) -> Keyboard:
    kb = Keyboard()
    kb.button("📋 Список вузов", "list")
    if is_admin:
        kb.button("🛠 Админка", "adm:home")
    kb.adjust(1)
    return kb
