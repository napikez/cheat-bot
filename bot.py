import asyncio
import logging
import os
import secrets
from datetime import datetime

import aiosqlite
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    TelegramObject,
    FSInputFile,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)

# ================== НАСТРОЙКИ (переменные окружения) ==================
# На Render всё это задаётся во вкладке Environment, а не прямо в коде.

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Изначальные супер-админы (их нельзя удалить из бота).
# Формат переменной ADMIN_IDS: "123456789,987654321"
SUPER_ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DB_PATH = os.environ.get("DB_PATH", "cheats.db")
PORT = int(os.environ.get("PORT", "10000"))  # Render сам подставит переменную PORT
THROTTLE_SECONDS = float(os.environ.get("THROTTLE_SECONDS", "1.0"))

# Картинка/гифка приветствия. Можно положить рядом с bot.py файл welcome.png
# или welcome.gif и указать соответствующее имя в WELCOME_MEDIA_PATH.
WELCOME_MEDIA_PATH = os.environ.get("WELCOME_MEDIA_PATH", "welcome.png")
WELCOME_TEXT = os.environ.get(
    "WELCOME_TEXT",
    "🟢 <b>Добро пожаловать!</b>\n\n"
    "В наличии есть много кряков для разных серверов.\n"
    "Выбирай нужный раздел в меню ниже 👇",
)

# ---------- ЧЕКЕР-БОТ (отдельный сервис, см. checker_bot.py) ----------
# Спонсоры добавляют администратором НЕ этого бота, а отдельного "чекер-бота".
# Проверку подписки и статус "я админ канала" этот бот делает через HTTP,
# обращаясь к чекер-боту, а не напрямую к Telegram Bot API.
CHECKER_URL = os.environ.get("CHECKER_URL", "").rstrip("/")
CHECKER_SECRET = os.environ.get("CHECKER_SECRET", "")
# Username чекер-бота для текста инструкций — если не задан, бот попробует
# узнать его сам через CHECKER_URL/whoami при старте.
CHECKER_BOT_USERNAME = os.environ.get("CHECKER_BOT_USERNAME", "")
# ========================================================================

logging.basicConfig(level=logging.INFO)

bot: Bot | None = None  # создаётся в main(), после проверки токена
BOT_ID: int | None = None  # id самого бота
BOT_USERNAME: str | None = None
CHECKER_USERNAME: str | None = None  # @username чекер-бота, для инструкций спонсорам
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Множество ID админов, кэшируется в памяти и обновляется при добавлении новых
admin_ids: set[int] = set()

# ---------- ТЕКСТЫ ПО УМОЛЧАНИЮ (редактируются потом через админ-панель) ----------
DEFAULT_INFO_TEXT = (
    "💚 <b>Информация</b>\n\n"
    "Здесь можно узнать всё о боте: как он устроен, кто владелец и куда писать по вопросам.\n\n"
    "👤 Владелец: @username\n"
    "🛟 Поддержка: @username\n\n"
    "(Этот текст можно изменить в админ-панели кнопкой «✏️ Инфо для юзеров».)"
)

DEFAULT_SPONSOR_INSTRUCTIONS = (
    "📖 <b>Как подключить канал в качестве спонсора</b>\n\n"
    "1️⃣ Откройте свой канал → «Управление каналом» → «Администраторы» → «Добавить администратора».\n"
    "2️⃣ Найдите и добавьте бота-проверяльщика подписки: @{bot_username} "
    "(это отдельный служебный бот — НЕ тот, в котором вы сейчас читаете эту инструкцию).\n"
    "3️⃣ Отдельные права ему не нужны — не давайте лишнего (постинг, удаление сообщений, "
    "приглашение и т.д. не требуются). Достаточно того, что он будет просто числиться "
    "администратором — это позволяет ему проверять, кто из пользователей подписан на канал, "
    "и не даёт ему возможности что-либо публиковать или менять в канале.\n"
    "4️⃣ Создайте отдельную новую пригласительную ссылку на канал специально для этой цели "
    "(«Управление каналом» → «Ссылки на приглашение» → «Создать ссылку»). Не используйте "
    "ссылку, которую вы публикуете где-то ещё в других местах.\n"
    "5️⃣ Вернитесь в ЭТОТ чат (к этому боту) и отправьте эту ссылку, а также перешлите сюда "
    "любой пост из своего канала (или отправьте @username канала), когда бот попросит — так "
    "он определит канал и проверит, что @{bot_username} действительно добавлен туда администратором.\n\n"
    "После этого шага бот попросит придумать название для кнопки — так, как вы хотите, "
    "чтобы её видели пользователи."
)

SPONSOR_STEP1_TEXT = (
    "🤝 <b>Добавление спонсорского канала</b>\n\n"
    "Сначала добавьте бота-проверяльщика подписки администратором в свой канал "
    "(см. инструкцию по кнопке ниже — это НЕ тот бот, которому вы сейчас пишете), "
    "затем перешлите сюда, в этот чат, любой пост из канала, либо отправьте его @username."
)

SUBSCRIPTION_PROMPT_TEXT = (
    "🔒 <b>Доступ ограничен</b>\n\n"
    "Чтобы пользоваться ботом, подпишись на канал(ы) ниже 👇, а затем нажми «Проверить»."
)

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="menu", description="🛠 Админ-панель"),
    BotCommand(command="add", description="➕ Добавить чит"),
    BotCommand(command="list", description="📋 Список читов"),
    BotCommand(command="delete", description="🗑 Удалить чит по ID"),
    BotCommand(command="give", description="🎁 Выдать доступ спонсору"),
    BotCommand(command="addadmin", description="👤 Добавить админа"),
    BotCommand(command="admins", description="🗒 Список админов"),
]
USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
]


def is_admin(user_id: int) -> bool:
    return user_id in admin_ids


# ---------- БАЗОВАЯ ЗАЩИТА (антифлуд) ----------
class ThrottlingMiddleware(BaseMiddleware):
    """Не даёт одному пользователю дёргать бота слишком часто."""

    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = rate_limit
        self._last_call: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is not None:
            now = asyncio.get_event_loop().time()
            last = self._last_call.get(user.id, 0.0)
            if now - last < self.rate_limit:
                if isinstance(event, CallbackQuery):
                    await event.answer("Не так быстро 🙂", show_alert=False)
                return
            self._last_call[user.id] = now
        return await handler(event, data)


# ---------- FSM (объявляем заранее, нужно для middleware проверки подписки) ----------
class AddCheat(StatesGroup):
    waiting_title = State()
    waiting_content = State()


class AddAdmin(StatesGroup):
    waiting_id = State()


class GiveAccess(StatesGroup):
    waiting_id = State()


class SponsorAddLink(StatesGroup):
    waiting_channel = State()
    waiting_invite_url = State()
    waiting_button_text = State()


class EditText(StatesGroup):
    waiting_text = State()


class SetWelcomeMedia(StatesGroup):
    waiting_media = State()


_SPONSOR_FLOW_STATES = {
    SponsorAddLink.waiting_channel.state,
    SponsorAddLink.waiting_invite_url.state,
    SponsorAddLink.waiting_button_text.state,
}
_SUBSCRIPTION_BYPASS_CALLBACKS = {"check_subs", "sponsor_howto"}


# ---------- ПРОВЕРКА ПОДПИСКИ НА СПОНСОРОВ ----------
class SubscriptionMiddleware(BaseMiddleware):
    """Перед любой командой/кнопкой обычного пользователя проверяет,
    подписан ли он на все активные спонсорские каналы."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if is_admin(user.id):
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        current_state = await state.get_state() if state else None
        if current_state in _SPONSOR_FLOW_STATES:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data in _SUBSCRIPTION_BYPASS_CALLBACKS:
            return await handler(event, data)

        # Свежему обладателю доступа даём пройти /start и добавить свою ссылку,
        # не требуя от него подписки на остальных спонсоров заранее.
        if await get_unused_grant(user.id):
            return await handler(event, data)

        ok, missing = await check_subscription(user.id)
        if not ok:
            await send_subscription_prompt(event, missing)
            return
        return await handler(event, data)


router.message.middleware(ThrottlingMiddleware(THROTTLE_SECONDS))
router.callback_query.middleware(ThrottlingMiddleware(THROTTLE_SECONDS / 2))
router.message.middleware(SubscriptionMiddleware())
router.callback_query.middleware(SubscriptionMiddleware())


# ---------- БАЗА ДАННЫХ ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS cheats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                content_type TEXT NOT NULL,   -- 'text' | 'document' | 'photo' | 'animation'
                content TEXT NOT NULL,        -- текст или file_id
                activations INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS cheat_votes (
                cheat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (cheat_id, user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                is_super INTEGER DEFAULT 0,
                added_by INTEGER,
                added_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sponsors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                invite_url TEXT NOT NULL,
                button_text TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                is_admin_link INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sponsor_grants (
                user_id INTEGER PRIMARY KEY,
                granted_by INTEGER,
                granted_at TEXT NOT NULL,
                used INTEGER DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.commit()

        # Миграции для баз, созданных старыми версиями кода
        cursor = await db.execute("PRAGMA table_info(cheats)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "likes" not in columns:
            await db.execute("ALTER TABLE cheats ADD COLUMN likes INTEGER DEFAULT 0")
        if "dislikes" not in columns:
            await db.execute("ALTER TABLE cheats ADD COLUMN dislikes INTEGER DEFAULT 0")
        await db.commit()

        # Засеваем супер-админов из переменной окружения ADMIN_IDS
        now = datetime.utcnow().isoformat()
        for uid in SUPER_ADMIN_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO admins (user_id, is_super, added_by, added_at) "
                "VALUES (?, 1, NULL, ?)",
                (uid, now),
            )
        await db.commit()

        # Засеваем тексты по умолчанию, если их ещё нет
        for key, value in (
            ("info_text", DEFAULT_INFO_TEXT),
            ("sponsor_instructions", DEFAULT_SPONSOR_INSTRUCTIONS),
        ):
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        await db.commit()


async def load_admin_ids() -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM admins")
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def add_admin_db(user_id: int, added_by: int) -> bool:
    """Возвращает True, если админ был добавлен, False если уже существовал."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        if await cursor.fetchone():
            return False
        await db.execute(
            "INSERT INTO admins (user_id, is_super, added_by, added_at) VALUES (?, 0, ?, ?)",
            (user_id, added_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
    return True


async def remove_admin_db(user_id: int) -> bool:
    """Удаляет не-супер админа. Возвращает True при успехе."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT is_super FROM admins WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        if not row or row[0] == 1:
            return False
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()
    return True


async def list_admins_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, is_super FROM admins ORDER BY is_super DESC, user_id"
        )
        return await cursor.fetchall()


async def add_cheat(title: str, content_type: str, content: str) -> str:
    token = secrets.token_urlsafe(6)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO cheats (token, title, content_type, content, created_at) VALUES (?,?,?,?,?)",
            (token, title, content_type, content, datetime.utcnow().isoformat()),
        )
        await db.commit()
    return token


async def get_cheat_by_token(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, title, content_type, content, activations, likes, dislikes "
            "FROM cheats WHERE token = ?",
            (token,),
        )
        return await cursor.fetchone()


async def get_cheat_by_id(cheat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, title, content_type, content, activations, likes, dislikes "
            "FROM cheats WHERE id = ?",
            (cheat_id,),
        )
        return await cursor.fetchone()


async def increment_activation(cheat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cheats SET activations = activations + 1 WHERE id = ?", (cheat_id,)
        )
        await db.commit()


async def vote_cheat(cheat_id: int, user_id: int, value: int):
    """value: 1 = лайк, -1 = дизлайк.
    Повторный такой же голос игнорируется, противоположный — переключает голос."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM cheat_votes WHERE cheat_id=? AND user_id=?",
            (cheat_id, user_id),
        )
        row = await cursor.fetchone()

        if row is None:
            await db.execute(
                "INSERT INTO cheat_votes (cheat_id, user_id, value) VALUES (?,?,?)",
                (cheat_id, user_id, value),
            )
            if value == 1:
                await db.execute("UPDATE cheats SET likes = likes + 1 WHERE id=?", (cheat_id,))
            else:
                await db.execute("UPDATE cheats SET dislikes = dislikes + 1 WHERE id=?", (cheat_id,))
            changed = True
        elif row[0] == value:
            changed = False
        else:
            await db.execute(
                "UPDATE cheat_votes SET value=? WHERE cheat_id=? AND user_id=?",
                (value, cheat_id, user_id),
            )
            if value == 1:
                await db.execute(
                    "UPDATE cheats SET likes = likes + 1, dislikes = dislikes - 1 WHERE id=?",
                    (cheat_id,),
                )
            else:
                await db.execute(
                    "UPDATE cheats SET dislikes = dislikes + 1, likes = likes - 1 WHERE id=?",
                    (cheat_id,),
                )
            changed = True

        await db.commit()
        cursor = await db.execute("SELECT likes, dislikes FROM cheats WHERE id=?", (cheat_id,))
        likes, dislikes = await cursor.fetchone()

    return likes, dislikes, changed


async def list_cheats():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, token, title, content_type, activations, likes, dislikes "
            "FROM cheats ORDER BY id DESC"
        )
        return await cursor.fetchall()


async def delete_cheat(cheat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cheats WHERE id = ?", (cheat_id,))
        await db.execute("DELETE FROM cheat_votes WHERE cheat_id = ?", (cheat_id,))
        await db.commit()


# ---------- НАСТРОЙКИ (редактируемые тексты) ----------
async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cursor.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


# ---------- СПОНСОРЫ ----------
async def add_sponsor(
    chat_id: int, chat_title: str, invite_url: str, button_text: str, owner_id: int, is_admin_link: bool
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO sponsors (chat_id, chat_title, invite_url, button_text, owner_id, "
            "is_admin_link, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
            (
                chat_id,
                chat_title,
                invite_url,
                button_text,
                owner_id,
                int(is_admin_link),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def list_sponsors():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, chat_title, invite_url, button_text, owner_id, is_admin_link, active "
            "FROM sponsors ORDER BY id DESC"
        )
        return await cursor.fetchall()


async def get_active_sponsors():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, chat_id, invite_url, button_text FROM sponsors WHERE active=1 ORDER BY id"
        )
        return await cursor.fetchall()


async def delete_sponsor(sponsor_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sponsors WHERE id=?", (sponsor_id,))
        await db.commit()


async def grant_sponsor_access(user_id: int, granted_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sponsor_grants (user_id, granted_by, granted_at, used) VALUES (?,?,?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET used=0, granted_by=excluded.granted_by, "
            "granted_at=excluded.granted_at",
            (user_id, granted_by, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_unused_grant(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT used FROM sponsor_grants WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
    return row is not None and row[0] == 0


async def mark_grant_used(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE sponsor_grants SET used=1 WHERE user_id=?", (user_id,))
        await db.commit()


async def checker_request(path: str, **params) -> dict | None:
    """Вызывает HTTP API чекер-бота. Возвращает распарсенный JSON или None при ошибке связи."""
    if not CHECKER_URL:
        logging.error("CHECKER_URL не задан — проверка подписки невозможна.")
        return None
    params = {**params}
    if CHECKER_SECRET:
        params["secret"] = CHECKER_SECRET
    url = f"{CHECKER_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    except Exception as e:
        logging.warning(f"Не удалось обратиться к чекер-боту ({url}): {e}")
        return None


async def check_subscription(user_id: int):
    """Возвращает (все_ли_подписан, список_недостающих[(id, invite_url, button_text)]).

    Сама проверка членства в канале делается НЕ этим ботом, а отдельным чекер-ботом
    (см. checker_bot.py), которого спонсоры добавляют администратором в свои каналы.
    Этот бот лишь спрашивает у чекер-бота по HTTP."""
    sponsors = await get_active_sponsors()
    missing = []
    for sponsor_id, chat_id, invite_url, button_text in sponsors:
        data = await checker_request("/check_member", chat_id=chat_id, user_id=user_id)
        is_subscribed = bool(data and data.get("is_subscribed"))
        if not data:
            logging.warning(f"Чекер-бот недоступен при проверке {user_id} на канале {chat_id}.")
        if not is_subscribed:
            missing.append((sponsor_id, invite_url, button_text))
    return (len(missing) == 0), missing


def extract_forwarded_chat(message: Message):
    """Telegram давно перевёл информацию о пересланном сообщении в поле
    forward_origin (MessageOriginChannel и т.д.), старые forward_from/forward_from_chat
    больше не заполняются в новых клиентах — поддерживаем оба варианта."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        return origin.chat
    return message.forward_from_chat


def extract_forwarded_user_id(message: Message):
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "sender_user", None) is not None:
        return origin.sender_user.id
    if message.forward_from:
        return message.forward_from.id
    return None


async def resolve_channel_from_message(message: Message):
    """Возвращает (chat_id, title) либо None, если распознать канал не удалось."""
    forwarded_chat = extract_forwarded_chat(message)
    if forwarded_chat:
        chat = forwarded_chat
        return chat.id, (chat.title or chat.username or str(chat.id))

    text = (message.text or "").strip()
    if not text:
        return None
    try:
        if text.startswith("@"):
            chat = await bot.get_chat(text)
            return chat.id, (chat.title or chat.username or str(chat.id))
        if text.startswith("https://t.me/") or text.startswith("http://t.me/") or text.startswith("t.me/"):
            username = text.rstrip("/").split("/")[-1].lstrip("@")
            if username and not username.startswith("+"):
                chat = await bot.get_chat(f"@{username}")
                return chat.id, (chat.title or chat.username or str(chat.id))
            return None
        if text.lstrip("-").isdigit():
            chat = await bot.get_chat(int(text))
            return chat.id, (chat.title or chat.username or str(chat.id))
    except Exception as e:
        logging.warning(f"Не удалось получить чат из '{text}': {e}")
        return None
    return None


def normalize_invite_url(raw: str) -> str | None:
    url = raw.strip()
    if url.startswith("@"):
        return f"https://t.me/{url.lstrip('@')}"
    if url.startswith("t.me/"):
        return "https://" + url
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return None


# ---------- КЛАВИАТУРЫ ----------
def cheat_keyboard(cheat_id: int, likes: int, dislikes: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"like:{cheat_id}"),
                InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"dislike:{cheat_id}"),
            ]
        ]
    )


def user_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Читы", callback_data="user_cheats")],
            [InlineKeyboardButton(text="💚 Информация", callback_data="user_info")],
        ]
    )


def back_to_user_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="user_menu")]]
    )


def sponsor_howto_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Инструкция как добавить бота", callback_data="sponsor_howto")]
        ]
    )


def subscription_keyboard(missing) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=title, url=url)] for (_id, url, title) in missing]
    rows.append([InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить чит", callback_data="admin_add")],
            [InlineKeyboardButton(text="🟢 Фото/гифка на /start", callback_data="admin_set_welcome")],
            [InlineKeyboardButton(text="📋 Список читов", callback_data="admin_list")],
            [InlineKeyboardButton(text="🎁 Выдать доступ спонсору", callback_data="admin_give")],
            [InlineKeyboardButton(text="🔗 Добавить свою ссылку", callback_data="admin_add_sponsor")],
            [InlineKeyboardButton(text="📃 Список спонсоров", callback_data="admin_sponsors_list")],
            [InlineKeyboardButton(text="✏️ Инфо для юзеров", callback_data="admin_edit_info")],
            [InlineKeyboardButton(text="📖 Инструкция для спонсоров", callback_data="admin_edit_howto")],
            [InlineKeyboardButton(text="👤 Добавить админа", callback_data="admin_addadmin")],
            [InlineKeyboardButton(text="🗒 Список админов", callback_data="admin_admins")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_menu")]]
    )


async def build_list_view():
    cheats = await list_cheats()
    if not cheats:
        return "Читов пока нет.", back_to_menu_keyboard()

    lines = []
    kb_rows = []
    for cheat_id, token, title, content_type, activations, likes, dislikes in cheats:
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        lines.append(
            f"<b>#{cheat_id} {title}</b> [{content_type}]\n"
            f"Активаций: {activations} | 👍 {likes} | 👎 {dislikes}\n"
            f"<code>{link}</code>"
        )
        kb_rows.append(
            [InlineKeyboardButton(text=f"🗑 Удалить #{cheat_id}", callback_data=f"cheat_del:{cheat_id}")]
        )
    kb_rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_menu")])
    text = "📋 <b>Список читов</b>\n\n" + "\n\n".join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def build_admins_view():
    admins = await list_admins_db()
    lines = []
    for user_id, is_super in admins:
        mark = "⭐ супер-админ" if is_super else "👤 админ"
        lines.append(f"<code>{user_id}</code> — {mark}")
    text = "🗒 <b>Список админов</b>\n\n" + "\n".join(lines)
    return text, back_to_menu_keyboard()


async def build_sponsors_view():
    sponsors = await list_sponsors()
    if not sponsors:
        return "Спонсоров пока нет.", back_to_menu_keyboard()

    lines = []
    kb_rows = []
    for sid, chat_title, invite_url, button_text, owner_id, is_admin_link, active in sponsors:
        tag = "🛡 админ" if is_admin_link else "🤝 спонсор"
        status = "🟢" if active else "🔴"
        lines.append(
            f"{status} <b>#{sid} {button_text}</b> [{tag}]\n"
            f"Канал: {chat_title}\n"
            f"Владелец: <code>{owner_id}</code>\n"
            f"<code>{invite_url}</code>"
        )
        kb_rows.append(
            [InlineKeyboardButton(text=f"🗑 Удалить #{sid}", callback_data=f"sponsor_del:{sid}")]
        )
    kb_rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_menu")])
    text = "📃 <b>Список спонсоров</b>\n\n" + "\n\n".join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ---------- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ----------
async def send_subscription_prompt(event, missing):
    kb = subscription_keyboard(missing)
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(SUBSCRIPTION_PROMPT_TEXT, reply_markup=kb)
        except Exception:
            await event.message.answer(SUBSCRIPTION_PROMPT_TEXT, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(SUBSCRIPTION_PROMPT_TEXT, reply_markup=kb)


@router.callback_query(F.data == "check_subs")
async def cb_check_subs(callback: CallbackQuery):
    ok, missing = await check_subscription(callback.from_user.id)
    if not ok:
        await callback.answer("Ты ещё не подписался на все каналы ❌", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=subscription_keyboard(missing))
        except Exception:
            pass
        return
    await callback.answer("Подписка подтверждена ✅")
    await callback.message.edit_text("🟢 <b>Главное меню</b>", reply_markup=user_main_keyboard())


@router.callback_query(F.data == "sponsor_howto")
async def cb_sponsor_howto(callback: CallbackQuery):
    text = await get_setting("sponsor_instructions", DEFAULT_SPONSOR_INSTRUCTIONS)
    text = text.replace("{bot_username}", CHECKER_USERNAME or "(чекер-бот не настроен, см. CHECKER_URL)")
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "user_menu")
async def cb_user_menu(callback: CallbackQuery):
    await callback.message.edit_text("🟢 <b>Главное меню</b>", reply_markup=user_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "user_info")
async def cb_user_info(callback: CallbackQuery):
    text = await get_setting("info_text", DEFAULT_INFO_TEXT)
    await callback.message.edit_text(text, reply_markup=back_to_user_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "user_cheats")
async def cb_user_cheats(callback: CallbackQuery):
    cheats = await list_cheats()
    if not cheats:
        await callback.message.edit_text("Пока читов нет 😔", reply_markup=back_to_user_menu_keyboard())
        await callback.answer()
        return
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"open_cheat:{cid}")]
        for cid, token, title, content_type, activations, likes, dislikes in cheats
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="user_menu")])
    await callback.message.edit_text(
        "🎮 <b>Доступные читы</b>\nВыбери нужный:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("open_cheat:"))
async def cb_open_cheat(callback: CallbackQuery):
    cheat_id = int(callback.data.split(":", 1)[1])
    cheat = await get_cheat_by_id(cheat_id)
    if not cheat:
        await callback.answer("Этот чит уже удалён.", show_alert=True)
        return

    cheat_id, title, content_type, content, activations, likes, dislikes = cheat
    await increment_activation(cheat_id)

    keyboard = cheat_keyboard(cheat_id, likes, dislikes)
    caption = f"<b>{title}</b>"
    chat_id = callback.from_user.id

    if content_type == "text":
        await bot.send_message(chat_id, f"{caption}\n\n{content}", reply_markup=keyboard)
    elif content_type == "photo":
        await bot.send_photo(chat_id, content, caption=caption, reply_markup=keyboard)
    elif content_type == "animation":
        await bot.send_animation(chat_id, content, caption=caption, reply_markup=keyboard)
    else:
        await bot.send_document(chat_id, content, caption=caption, reply_markup=keyboard)
    await callback.answer()


@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: Message, command: CommandObject):
    token = command.args
    cheat = await get_cheat_by_token(token)
    if not cheat:
        await message.answer("Ссылка недействительна или чит был удалён.")
        return

    cheat_id, title, content_type, content, activations, likes, dislikes = cheat
    await increment_activation(cheat_id)

    keyboard = cheat_keyboard(cheat_id, likes, dislikes)
    caption = f"<b>{title}</b>"

    if content_type == "text":
        await message.answer(f"{caption}\n\n{content}", reply_markup=keyboard)
    elif content_type == "photo":
        await message.answer_photo(content, caption=caption, reply_markup=keyboard)
    elif content_type == "animation":
        await message.answer_animation(content, caption=caption, reply_markup=keyboard)
    else:  # document — пересылаем по file_id
        await message.answer_document(content, caption=caption, reply_markup=keyboard)


async def send_welcome(message: Message):
    kb = user_main_keyboard()

    # Сначала пробуем медиа, загруженное админом через кнопку "🟢 Фото/гифка на /start"
    media_type = await get_setting("welcome_media_type", "")
    media_id = await get_setting("welcome_media_id", "")
    if media_type and media_id:
        try:
            if media_type == "animation":
                await message.answer_animation(media_id, caption=WELCOME_TEXT, reply_markup=kb)
            else:
                await message.answer_photo(media_id, caption=WELCOME_TEXT, reply_markup=kb)
            return
        except Exception as e:
            logging.warning(f"Не удалось отправить сохранённое приветственное медиа: {e}")

    # Резервный вариант — файл welcome.png/welcome.gif рядом с bot.py
    ext = os.path.splitext(WELCOME_MEDIA_PATH)[1].lower()
    try:
        media = FSInputFile(WELCOME_MEDIA_PATH)
        if ext in (".gif",):
            await message.answer_animation(media, caption=WELCOME_TEXT, reply_markup=kb)
        else:
            await message.answer_photo(media, caption=WELCOME_TEXT, reply_markup=kb)
    except FileNotFoundError:
        logging.warning(f"Файл приветственного медиа не найден: {WELCOME_MEDIA_PATH}")
        await message.answer(WELCOME_TEXT, reply_markup=kb)


@router.message(CommandStart())
async def start_plain(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await message.answer(
            "🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=admin_main_keyboard()
        )
        return

    if await get_unused_grant(message.from_user.id):
        await state.set_state(SponsorAddLink.waiting_channel)
        await state.update_data(owner_id=message.from_user.id, is_admin_link=False)
        await message.answer(SPONSOR_STEP1_TEXT, reply_markup=sponsor_howto_keyboard())
        return

    await send_welcome(message)


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=admin_main_keyboard())


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=admin_main_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_close")
async def cb_admin_close(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("like:"))
async def process_like(callback: CallbackQuery):
    cheat_id = int(callback.data.split(":", 1)[1])
    cheat = await get_cheat_by_id(cheat_id)
    if not cheat:
        await callback.answer("Этот чит уже удалён.", show_alert=True)
        return

    likes, dislikes, changed = await vote_cheat(cheat_id, callback.from_user.id, 1)
    if not changed:
        await callback.answer("Ты уже лайкал этот чит 👍", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=cheat_keyboard(cheat_id, likes, dislikes))
    except Exception:
        pass
    await callback.answer("Лайк засчитан!")


@router.callback_query(F.data.startswith("dislike:"))
async def process_dislike(callback: CallbackQuery):
    cheat_id = int(callback.data.split(":", 1)[1])
    cheat = await get_cheat_by_id(cheat_id)
    if not cheat:
        await callback.answer("Этот чит уже удалён.", show_alert=True)
        return

    likes, dislikes, changed = await vote_cheat(cheat_id, callback.from_user.id, -1)
    if not changed:
        await callback.answer("Ты уже ставил дизлайк этому читу 👎", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=cheat_keyboard(cheat_id, likes, dislikes))
    except Exception:
        pass
    await callback.answer("Дизлайк засчитан!")


# ---------- ДОБАВЛЕНИЕ ЧИТА ----------
async def _start_add_cheat(state: FSMContext):
    await state.set_state(AddCheat.waiting_title)


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _start_add_cheat(state)
    await message.answer("Введите название чита:")


@router.callback_query(F.data == "admin_add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _start_add_cheat(state)
    await callback.message.edit_text("Введите название чита:")
    await callback.answer()


@router.message(AddCheat.waiting_title)
async def add_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(title=message.text)
    await state.set_state(AddCheat.waiting_content)
    await message.answer(
        "Теперь отправьте сам чит:\n"
        "— текстовым сообщением (ключ, код, инструкция);\n"
        "— файлом (документом);\n"
        "— фото; или\n"
        "— гифкой (GIF)."
    )


async def _finish_add(message: Message, state: FSMContext, token: str):
    await state.clear()
    link = f"https://t.me/{BOT_USERNAME}?start={token}"
    await message.answer(f"✅ Чит добавлен!\nВаша ссылка:\n<code>{link}</code>")


@router.message(AddCheat.waiting_content, F.document)
async def add_content_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    token = await add_cheat(data["title"], "document", message.document.file_id)
    await _finish_add(message, state, token)


@router.message(AddCheat.waiting_content, F.photo)
async def add_content_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    token = await add_cheat(data["title"], "photo", message.photo[-1].file_id)
    await _finish_add(message, state, token)


@router.message(AddCheat.waiting_content, F.animation)
async def add_content_animation(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    token = await add_cheat(data["title"], "animation", message.animation.file_id)
    await _finish_add(message, state, token)


@router.message(AddCheat.waiting_content, F.text)
async def add_content_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    token = await add_cheat(data["title"], "text", message.text)
    await _finish_add(message, state, token)


# ---------- СПИСОК / УДАЛЕНИЕ ЧИТОВ ----------
@router.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    text, kb = await build_list_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_list")
async def cb_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    text, kb = await build_list_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cheat_del:"))
async def cb_cheat_del_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    cheat_id = int(callback.data.split(":", 1)[1])
    cheat = await get_cheat_by_id(cheat_id)
    if not cheat:
        await callback.answer("Чит уже удалён.", show_alert=True)
        return
    title = cheat[1]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"cheat_delyes:{cheat_id}"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="admin_list"),
            ]
        ]
    )
    await callback.message.edit_text(f"Удалить чит <b>#{cheat_id} {title}</b>?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cheat_delyes:"))
async def cb_cheat_del_yes(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    cheat_id = int(callback.data.split(":", 1)[1])
    await delete_cheat(cheat_id)
    text, kb = await build_list_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Удалено ✅")


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /delete <id>")
        return
    await delete_cheat(int(parts[1]))
    await message.answer("Удалено (если такой ID существовал).")


# ---------- ВЫДАЧА ДОСТУПА СПОНСОРУ (/give) ----------
async def _do_give(message: Message, uid: int):
    await grant_sponsor_access(uid, granted_by=message.from_user.id)
    await message.answer(
        f"✅ Пользователю <code>{uid}</code> выдан доступ на добавление спонсорской ссылки.\n"
        "Пусть отправит боту /start — бот сам проведёт его через все шаги."
    )


@router.message(Command("give"))
async def cmd_give(message: Message, state: FSMContext, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if command.args and command.args.strip().lstrip("-").isdigit():
        await _do_give(message, int(command.args.strip()))
        return
    await state.set_state(GiveAccess.waiting_id)
    await message.answer(
        "Отправьте числовой Telegram ID пользователя, которому выдать доступ спонсора, "
        "или перешлите сюда любое его сообщение.\n"
        "(Узнать ID можно у @userinfobot)"
    )


@router.callback_query(F.data == "admin_give")
async def cb_admin_give(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(GiveAccess.waiting_id)
    await callback.message.edit_text(
        "Отправьте числовой Telegram ID пользователя, которому выдать доступ спонсора, "
        "или перешлите сюда любое его сообщение.\n"
        "(Узнать ID можно у @userinfobot)",
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.message(GiveAccess.waiting_id)
async def give_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    uid = extract_forwarded_user_id(message)
    if uid is None and message.text and message.text.strip().lstrip("-").isdigit():
        uid = int(message.text.strip())

    if uid is None:
        await message.answer("Не удалось определить ID. Отправьте число или перешлите сообщение пользователя.")
        return

    await state.clear()
    await _do_give(message, uid)


# ---------- ДОБАВЛЕНИЕ СПОНСОРСКОЙ ССЫЛКИ (сам спонсор или админ) ----------
@router.callback_query(F.data == "admin_add_sponsor")
async def cb_admin_add_sponsor(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(SponsorAddLink.waiting_channel)
    await state.update_data(owner_id=callback.from_user.id, is_admin_link=True)
    await callback.message.edit_text(SPONSOR_STEP1_TEXT, reply_markup=sponsor_howto_keyboard())
    await callback.answer()


@router.message(SponsorAddLink.waiting_channel)
async def sponsor_receive_channel(message: Message, state: FSMContext):
    resolved = await resolve_channel_from_message(message)
    if not resolved:
        await message.answer(
            "Не понял 🤔 Перешлите сюда любой пост из своего канала, либо отправьте его @username.\n\n"
            "Не забудьте сначала добавить бота в канал администратором!",
            reply_markup=sponsor_howto_keyboard(),
        )
        return

    chat_id, chat_title = resolved
    data = await checker_request("/status", chat_id=chat_id)
    if not data or not data.get("is_admin"):
        checker_name = f"@{CHECKER_USERNAME}" if CHECKER_USERNAME else "бота-проверяльщика"
        await message.answer(
            f"⚠️ {checker_name} пока не администратор канала «{chat_title}» "
            "(или сервис проверки временно недоступен).\n"
            f"Добавьте {checker_name} в администраторы канала и отправьте канал ещё раз.",
            reply_markup=sponsor_howto_keyboard(),
        )
        return

    await state.update_data(chat_id=chat_id, chat_title=chat_title)
    await state.set_state(SponsorAddLink.waiting_invite_url)
    await message.answer(
        f"✅ Канал «{chat_title}» подключен.\n\n"
        "Теперь отправьте вашу рефералку (отдельную пригласительную ссылку на канал), "
        "которую увидят пользователи."
    )


@router.message(SponsorAddLink.waiting_invite_url, F.text)
async def sponsor_receive_url(message: Message, state: FSMContext):
    url = normalize_invite_url(message.text)
    if not url:
        await message.answer(
            "Похоже, это не ссылка. Отправьте корректную пригласительную ссылку "
            "(например, https://t.me/+xxxxxxxx)."
        )
        return
    await state.update_data(invite_url=url)
    await state.set_state(SponsorAddLink.waiting_button_text)
    await message.answer("Отлично! Как назвать кнопку для пользователей? Например: 🎯 Спонсор")


@router.message(SponsorAddLink.waiting_button_text, F.text)
async def sponsor_receive_title(message: Message, state: FSMContext):
    data = await state.get_data()
    button_text = message.text.strip()[:64]
    if not button_text:
        await message.answer("Название кнопки не может быть пустым. Отправьте текст.")
        return

    await add_sponsor(
        chat_id=data["chat_id"],
        chat_title=data["chat_title"],
        invite_url=data["invite_url"],
        button_text=button_text,
        owner_id=data["owner_id"],
        is_admin_link=data["is_admin_link"],
    )
    if not data["is_admin_link"]:
        await mark_grant_used(data["owner_id"])

    await state.clear()
    await message.answer(f"🎉 Готово! Спонсорская кнопка «{button_text}» добавлена и уже видна пользователям.")


@router.callback_query(F.data == "admin_sponsors_list")
async def cb_admin_sponsors_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    text, kb = await build_sponsors_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("sponsor_del:"))
async def cb_sponsor_del_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    sid = int(callback.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"sponsor_delyes:{sid}"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="admin_sponsors_list"),
            ]
        ]
    )
    await callback.message.edit_text(f"Удалить спонсора #{sid}?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("sponsor_delyes:"))
async def cb_sponsor_del_yes(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    sid = int(callback.data.split(":", 1)[1])
    await delete_sponsor(sid)
    text, kb = await build_sponsors_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Удалено ✅")


# ---------- РЕДАКТИРОВАНИЕ ТЕКСТОВ (Информация / Инструкция для спонсоров) ----------
@router.callback_query(F.data == "admin_edit_info")
async def cb_admin_edit_info(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    current = await get_setting("info_text", DEFAULT_INFO_TEXT)
    await state.set_state(EditText.waiting_text)
    await state.update_data(key="info_text")
    await callback.message.edit_text(
        f"Текущий текст «Информации»:\n\n{current}\n\n✏️ Отправьте новый текст (поддерживается HTML).",
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_edit_howto")
async def cb_admin_edit_howto(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    current = await get_setting("sponsor_instructions", DEFAULT_SPONSOR_INSTRUCTIONS)
    await state.set_state(EditText.waiting_text)
    await state.update_data(key="sponsor_instructions")
    await callback.message.edit_text(
        f"Текущая инструкция для спонсоров:\n\n{current}\n\n"
        "✏️ Отправьте новый текст (поддерживается HTML, можно использовать {bot_username}).",
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.message(EditText.waiting_text, F.text)
async def edit_text_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data["key"]
    await set_setting(key, message.text)
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=admin_main_keyboard())


# ---------- ПРИВЕТСТВЕННОЕ ФОТО/ГИФКА НА /start ----------
@router.callback_query(F.data == "admin_set_welcome")
async def cb_set_welcome(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(SetWelcomeMedia.waiting_media)
    await callback.message.edit_text(
        "🟢 Пришлите фото или гифку (GIF) — она будет показываться всем пользователям "
        "каждый раз при /start вместо текущей.",
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.message(SetWelcomeMedia.waiting_media, F.photo)
async def set_welcome_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await set_setting("welcome_media_type", "photo")
    await set_setting("welcome_media_id", message.photo[-1].file_id)
    await state.clear()
    await message.answer(
        "✅ Готово! Это фото теперь будет показываться на /start.",
        reply_markup=admin_main_keyboard(),
    )


@router.message(SetWelcomeMedia.waiting_media, F.animation)
async def set_welcome_animation(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await set_setting("welcome_media_type", "animation")
    await set_setting("welcome_media_id", message.animation.file_id)
    await state.clear()
    await message.answer(
        "✅ Готово! Эта гифка теперь будет показываться на /start.",
        reply_markup=admin_main_keyboard(),
    )


@router.message(SetWelcomeMedia.waiting_media)
async def set_welcome_invalid(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Нужно прислать именно фото или гифку (GIF). Попробуйте ещё раз.")


# ---------- УПРАВЛЕНИЕ АДМИНАМИ ----------
@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, state: FSMContext, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if command.args and command.args.strip().lstrip("-").isdigit():
        uid = int(command.args.strip())
        await _do_add_admin(message, uid)
        return
    await state.set_state(AddAdmin.waiting_id)
    await message.answer(
        "Отправьте числовой Telegram ID нового админа, или перешлите сюда любое его сообщение.\n"
        "(Узнать свой ID можно у @userinfobot)"
    )


@router.callback_query(F.data == "admin_addadmin")
async def cb_addadmin(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AddAdmin.waiting_id)
    await callback.message.edit_text(
        "Отправьте числовой Telegram ID нового админа, или перешлите сюда любое его сообщение.\n"
        "(Узнать свой ID можно у @userinfobot)"
    )
    await callback.answer()


async def _do_add_admin(message: Message, uid: int):
    added = await add_admin_db(uid, added_by=message.from_user.id)
    global admin_ids
    admin_ids = await load_admin_ids()
    if added:
        await set_admin_commands(uid)
        await message.answer(f"✅ Пользователь <code>{uid}</code> назначен админом.")
    else:
        await message.answer(f"Пользователь <code>{uid}</code> уже является админом.")


@router.message(AddAdmin.waiting_id)
async def addadmin_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    uid = extract_forwarded_user_id(message)
    if uid is None and message.text and message.text.strip().lstrip("-").isdigit():
        uid = int(message.text.strip())

    if uid is None:
        await message.answer("Не удалось определить ID. Отправьте число или перешлите сообщение пользователя.")
        return

    await state.clear()
    await _do_add_admin(message, uid)


@router.message(Command("admins"))
async def cmd_admins(message: Message):
    if not is_admin(message.from_user.id):
        return
    text, kb = await build_admins_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_admins")
async def cb_admins(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    text, kb = await build_admins_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ---------- КОМАНДЫ БОТА (появляются по кнопке "/") ----------
async def set_admin_commands(uid: int):
    try:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
    except Exception as e:
        logging.warning(f"Не удалось установить команды для {uid}: {e}")


async def setup_commands():
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for uid in admin_ids:
        await set_admin_commands(uid)


# ---------- HTTP-СЕРВЕР ДЛЯ RENDER + UPTIMEROBOT ----------
# Render Web Service требует открытый порт, а UptimeRobot будет дергать этот
# адрес каждые несколько минут, чтобы бесплатный сервис не засыпал.
async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_head("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info(f"Health-check сервер запущен на порту {PORT}")


# ---------- ЗАПУСК ----------
async def main():
    global bot, admin_ids, BOT_ID, BOT_USERNAME, CHECKER_USERNAME

    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN "
            "(в настройках Render: Environment -> Add Environment Variable)."
        )
    if not CHECKER_URL:
        logging.warning(
            "CHECKER_URL не задан — проверка подписки на спонсоров работать не будет, "
            "пока не укажете адрес чекер-бота (например, https://sponsor-checker.onrender.com)."
        )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = me.username

    # Узнаём @username чекер-бота, чтобы подставлять его в инструкции для спонсоров.
    if CHECKER_BOT_USERNAME:
        CHECKER_USERNAME = CHECKER_BOT_USERNAME
    else:
        data = await checker_request("/whoami")
        if data and data.get("username"):
            CHECKER_USERNAME = data["username"]
        else:
            logging.warning(
                "Не удалось узнать username чекер-бота через /whoami. "
                "Задайте CHECKER_BOT_USERNAME вручную, иначе инструкция для спонсоров "
                "будет показывать заглушку вместо @username."
            )

    await init_db()
    admin_ids = await load_admin_ids()
    if not admin_ids:
        logging.warning(
            "Нет ни одного админа: задайте ADMIN_IDS в переменных окружения, "
            "иначе панель админа будет недоступна."
        )

    await start_health_server()
    await setup_commands()

    logging.info("Бот запускается (polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
