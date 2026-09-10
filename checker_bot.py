import asyncio
import logging
import os
from datetime import datetime

import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ChatMemberUpdated

# ============================================================================
#  ЧЕКЕР-БОТ (sponsor checker bot)
#
#  Отдельный, "нейтральный" бот, у которого только одна задача: спонсоры
#  добавляют ЕГО администратором в свой канал (вместо основного бота с читами),
#  а этот бот по запросу основного бота проверяет, подписан ли конкретный
#  пользователь на канал. Никакого контента, никакой раздачи читов тут нет —
#  только проверка подписки через Telegram Bot API (getChatMember).
#
#  Основной бот (bot.py) обращается к этому боту не напрямую через Telegram,
#  а по HTTP (см. функции handle_check_member / handle_status ниже), передавая
#  секретный ключ CHECKER_SECRET, чтобы посторонние не могли дёргать эндпоинт.
# ============================================================================

BOT_TOKEN = os.environ.get("CHECKER_BOT_TOKEN", "")
CHECKER_SECRET = os.environ.get("CHECKER_SECRET", "")

# ID тех, кому можно писать этому боту команды типа /channels (необязательно).
SUPER_ADMIN_IDS = {
    int(x) for x in os.environ.get("CHECKER_ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DB_PATH = os.environ.get("CHECKER_DB_PATH", "checker.db")
PORT = int(os.environ.get("PORT", "10000"))  # Render сам подставит переменную PORT

logging.basicConfig(level=logging.INFO)

bot: Bot | None = None
BOT_ID: int | None = None
BOT_USERNAME: str | None = None
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


def is_admin_user(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


# ---------- БАЗА ДАННЫХ (только лог каналов, для команды /channels) ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                is_admin INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def upsert_channel(chat_id: int, title: str, is_admin_flag: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO channels (chat_id, title, is_admin, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title, is_admin=excluded.is_admin, updated_at=excluded.updated_at
            """,
            (chat_id, title, int(is_admin_flag), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def list_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT chat_id, title, is_admin FROM channels ORDER BY updated_at DESC"
        )
        return await cursor.fetchall()


# ---------- TELEGRAM: реагируем на добавление/удаление из канала ----------
@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    chat = update.chat
    new_status = update.new_chat_member.status
    is_admin_flag = new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    await upsert_channel(chat.id, chat.title or chat.username or str(chat.id), is_admin_flag)
    logging.info(f"Статус в чате {chat.id} ({chat.title}) изменился на {new_status}")


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🔒 Это служебный бот-«проверяльщик» подписок.\n\n"
        "У него нет читов, контента и админ-панели с настройками — единственная задача: "
        "быть администратором в каналах-спонсорах, чтобы можно было проверять, кто из "
        "пользователей на них подписан.\n\n"
        "Если вас попросили добавить меня в канал — просто добавьте администратором "
        "без дополнительных прав (постинг, удаление сообщений, приглашения и т.д. не нужны). "
        "Больше от вас ничего не требуется."
    )


@router.message(Command("channels"))
async def cmd_channels(message: Message):
    if not is_admin_user(message.from_user.id):
        return
    rows = await list_channels()
    if not rows:
        await message.answer("Пока ни один канал меня не добавлял.")
        return
    lines = [
        f"<code>{chat_id}</code> — {title} [{'🛡 админ' if flag else '❌ не админ / убрали'}]"
        for chat_id, title, flag in rows
    ]
    await message.answer("📃 <b>Каналы</b>\n\n" + "\n".join(lines))


# ---------- HTTP API — им пользуется основной бот с читами ----------
def secret_ok(request: web.Request) -> bool:
    if not CHECKER_SECRET:
        # Секрет не задан — считаем небезопасным, но не блокируем (см. предупреждение в логах при старте).
        return True
    return (
        request.query.get("secret") == CHECKER_SECRET
        or request.headers.get("X-Checker-Secret") == CHECKER_SECRET
    )


async def handle_check_member(request: web.Request) -> web.Response:
    """GET /check_member?chat_id=..&user_id=..&secret=..
    Проверяет, подписан ли user_id на канал chat_id."""
    if not secret_ok(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        chat_id = int(request.query["chat_id"])
        user_id = int(request.query["user_id"])
    except (KeyError, ValueError):
        return web.json_response({"ok": False, "error": "chat_id and user_id required"}, status=400)

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status = member.status
        is_subscribed = status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
        return web.json_response({"ok": True, "status": str(status), "is_subscribed": is_subscribed})
    except Exception as e:
        logging.warning(f"Не удалось проверить {user_id} в {chat_id}: {e}")
        # Если канал недоступен / бот не админ — считаем, что подписки нет,
        # чтобы по ошибке не открыть доступ.
        return web.json_response({"ok": False, "error": str(e), "is_subscribed": False})


async def handle_status(request: web.Request) -> web.Response:
    """GET /status?chat_id=..&secret=..
    Проверяет, является ли САМ чекер-бот админом канала chat_id.
    Основной бот вызывает это при подключении нового спонсора."""
    if not secret_ok(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        chat_id = int(request.query["chat_id"])
    except (KeyError, ValueError):
        return web.json_response({"ok": False, "error": "chat_id required"}, status=400)
    try:
        member = await bot.get_chat_member(chat_id, BOT_ID)
        is_admin_flag = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        return web.json_response({"ok": True, "is_admin": is_admin_flag})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e), "is_admin": False})


async def handle_whoami(request: web.Request) -> web.Response:
    """GET /whoami — основной бот вызывает это при старте, чтобы узнать
    @username чекер-бота и подставить его в инструкцию для спонсоров."""
    return web.json_response({"ok": True, "id": BOT_ID, "username": BOT_USERNAME})


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_head("/", handle_ping)
    app.router.add_get("/check_member", handle_check_member)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/whoami", handle_whoami)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info(f"HTTP API чекер-бота запущен на порту {PORT}")


# ---------- ЗАПУСК ----------
async def main():
    global bot, BOT_ID, BOT_USERNAME

    if not BOT_TOKEN:
        raise SystemExit(
            "CHECKER_BOT_TOKEN не задан. Установите переменную окружения CHECKER_BOT_TOKEN "
            "(токен ВТОРОГО бота от @BotFather, отдельного от основного)."
        )
    if not CHECKER_SECRET:
        logging.warning(
            "CHECKER_SECRET не задан — HTTP API этого бота будет доступен без пароля "
            "любому, кто узнает адрес сервиса. Обязательно задайте CHECKER_SECRET "
            "в переменных окружения на Render!"
        )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = me.username

    await init_db()
    await start_http_server()

    logging.info(f"Чекер-бот запущен: @{BOT_USERNAME} (id={BOT_ID})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
