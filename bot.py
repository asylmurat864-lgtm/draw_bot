import asyncio
import random
import logging
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import asyncpg

# ============================================
#  НАСТРОЙКИ (С ЗАЩИТОЙ ОТ ПРОБЕЛОВ!)
# ============================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    BOT_TOKEN = "8915886468:AAEyfaKl08r3KvHUKrD7Rp-es7PHuXI6OdY"
BOT_TOKEN = ''.join(BOT_TOKEN.split())  # Удаляем все пробелы

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# ============================================
#  ЛОГИРОВАНИЕ
# ============================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
#  ИНИЦИАЛИЗАЦИЯ
# ============================================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ============================================
#  FSM СОСТОЯНИЯ
# ============================================
class CreateDraw(StatesGroup):
    waiting_for_title = State()
    waiting_for_max = State()
    waiting_for_winners = State()
    waiting_for_chat = State()

# ============================================
#  ПОДКЛЮЧЕНИЕ К POSTGRESQL
# ============================================
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS draws (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            message_id BIGINT,
            creator_id BIGINT,
            title TEXT,
            max_participants INTEGER DEFAULT 0,
            winners_count INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            user_id BIGINT,
            draw_id INTEGER,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, draw_id)
        )
    """)
    await conn.close()
    logger.info("✅ PostgreSQL инициализирован")

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

# ============================================
#  ФУНКЦИИ БАЗЫ ДАННЫХ
# ============================================

async def create_draw(chat_id, message_id, creator_id, title, max_p, winners):
    conn = await get_db()
    result = await conn.fetchrow(
        "INSERT INTO draws (chat_id, message_id, creator_id, title, max_participants, winners_count) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        chat_id, message_id, creator_id, title, max_p, winners
    )
    await conn.close()
    return result['id']

async def add_participant(user_id, username, first_name, draw_id):
    conn = await get_db()
    try:
        await conn.execute(
            "INSERT INTO participants (user_id, draw_id, username, first_name) VALUES ($1, $2, $3, $4)",
            user_id, draw_id, username, first_name
        )
        await conn.close()
        return True
    except:
        await conn.close()
        return False

async def get_participants_count(draw_id):
    conn = await get_db()
    result = await conn.fetchval("SELECT COUNT(*) FROM participants WHERE draw_id = $1", draw_id)
    await conn.close()
    return result or 0

async def get_all_participants(draw_id):
    conn = await get_db()
    rows = await conn.fetch("SELECT user_id, username, first_name FROM participants WHERE draw_id = $1", draw_id)
    await conn.close()
    return [(r['user_id'], r['username'], r['first_name']) for r in rows]

async def clear_participants(draw_id):
    conn = await get_db()
    await conn.execute("DELETE FROM participants WHERE draw_id = $1", draw_id)
    await conn.close()

async def close_draw(draw_id):
    conn = await get_db()
    await conn.execute("UPDATE draws SET is_active = 0 WHERE id = $1", draw_id)
    await conn.close()

async def get_draw_info(draw_id):
    conn = await get_db()
    row = await conn.fetchrow(
        "SELECT chat_id, message_id, creator_id, title, max_participants, winners_count, is_active FROM draws WHERE id = $1",
        draw_id
    )
    await conn.close()
    return row

async def is_draw_active(draw_id):
    conn = await get_db()
    result = await conn.fetchval("SELECT is_active FROM draws WHERE id = $1", draw_id)
    await conn.close()
    return result == 1

async def get_active_draw_by_chat(chat_id):
    conn = await get_db()
    result = await conn.fetchval("SELECT id FROM draws WHERE chat_id = $1 AND is_active = 1 ORDER BY id DESC LIMIT 1", chat_id)
    await conn.close()
    return result

async def get_active_draw_by_creator(creator_id):
    conn = await get_db()
    result = await conn.fetchval("SELECT id FROM draws WHERE creator_id = $1 AND is_active = 1 ORDER BY id DESC LIMIT 1", creator_id)
    await conn.close()
    return result

async def get_all_draws():
    conn = await get_db()
    rows = await conn.fetch("SELECT id, title, is_active FROM draws ORDER BY id DESC")
    await conn.close()
    return [(r['id'], r['title'], r['is_active']) for r in rows]

async def update_message_id(draw_id, message_id):
    conn = await get_db()
    await conn.execute("UPDATE draws SET message_id = $1 WHERE id = $2", message_id, draw_id)
    await conn.close()

# ============================================
#  КЛАВИАТУРЫ
# ============================================

def get_join_keyboard(draw_id):
    bot_id = BOT_TOKEN.split(':')[0]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎯 Участвовать в конкурсе",
            url=f"https://t.me/{bot_id}?start=join_{draw_id}"
        )]
    ])

# ============================================
#  ОБРАБОТЧИКИ КОМАНД
# ============================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    args = message.text.split()
    
    if len(args) > 1 and args[1].startswith("join_"):
        try:
            draw_id = int(args[1].split("_")[1])
            await handle_join(message, draw_id)
            return
        except Exception as e:
            logger.error(f"Ошибка join: {e}")
            await message.answer("❌ Неверная ссылка")
            return
    
    await message.answer(
        "👋 Привет! Я бот для конкурсов 😉\n\n"
        "📌 **Создать конкурс:** `/create` в ЛС\n"
        "📌 **Участвовать:** кнопка под постом\n"
        "📌 **Завершить:** `/draw` в чате",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_join(message: types.Message, draw_id: int):
    user = message.from_user
    
    draw_info = await get_draw_info(draw_id)
    if not draw_info:
        await message.answer(f"❌ Конкурс #{draw_id} не найден")
        return
    
    if not await is_draw_active(draw_id):
        await message.answer("❌ Конкурс завершён")
        return
    
    title = draw_info['title']
    max_p = draw_info['max_participants']
    
    current_count = await get_participants_count(draw_id)
    if max_p > 0 and current_count >= max_p:
        await message.answer(f"😔 Конкурс «{title}» заполнен", parse_mode=ParseMode.MARKDOWN)
        return
    
    if await add_participant(user.id, user.username, user.first_name, draw_id):
        new_count = await get_participants_count(draw_id)
        await message.answer(
            f"✅ Ты участник конкурса «{title}»! 🎉\n"
            f"👥 Всего: {new_count}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer(f"⚠️ Ты уже участвуешь в «{title}»", parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("create"))
async def create_cmd(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.answer("⚠️ Пиши /create в ЛС!")
        return
    
    active = await get_active_draw_by_creator(message.from_user.id)
    if active:
        await message.answer("⚠️ У тебя уже есть активный конкурс!")
        return
    
    await state.set_state(CreateDraw.waiting_for_title)
    await message.answer("📝 **Название конкурса:**", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(CreateDraw.waiting_for_title), F.text)
async def process_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title or len(title) > 100:
        await message.answer("❌ Название от 1 до 100 символов")
        return
    await state.update_data(title=title)
    await state.set_state(CreateDraw.waiting_for_max)
    await message.answer("👥 **Максимум участников** (0 = безлимит):", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(CreateDraw.waiting_for_max), F.text)
async def process_max(message: types.Message, state: FSMContext):
    try:
        max_p = int(message.text.strip())
        if max_p < 0:
            raise ValueError
    except:
        await message.answer("❌ Введи число (0 или больше)")
        return
    await state.update_data(max_participants=max_p)
    await state.set_state(CreateDraw.waiting_for_winners)
    await message.answer("🏆 **Количество победителей:**", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(CreateDraw.waiting_for_winners), F.text)
async def process_winners(message: types.Message, state: FSMContext):
    try:
        winners = int(message.text.strip())
        if winners <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введи число (1, 2, 3...)")
        return
    await state.update_data(winners_count=winners)
    await state.set_state(CreateDraw.waiting_for_chat)
    await message.answer("📤 **@username канала/группы** (например, @my_channel):", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(CreateDraw.waiting_for_chat), F.text)
async def process_chat(message: types.Message, state: FSMContext):
    chat_input = message.text.strip()
    
    if not chat_input.startswith('@'):
        await message.answer("❌ Отправь @username")
        return
    
    try:
        chat = await bot.get_chat(chat_input)
    except Exception as e:
        await message.answer(f"❌ Не найден чат: {e}")
        return
    
    data = await state.get_data()
    title = data['title']
    max_p = data['max_participants']
    winners = data['winners_count']
    creator_id = message.from_user.id
    chat_id = chat.id
    
    post_text = (
        f"🎉 **{title}**\n\n"
        f"👥 Максимум: {max_p if max_p > 0 else '♾ безлимит'}\n"
        f"🏆 Победителей: {winners}\n\n"
        "👇 Нажми на кнопку!"
    )
    
    draw_id = await create_draw(chat_id, 0, creator_id, title, max_p, winners)
    keyboard = get_join_keyboard(draw_id)
    
    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=post_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nБот должен быть админом!")
        await state.clear()
        return
    
    await update_message_id(draw_id, sent_msg.message_id)
    
    if chat_id < 0:
        post_link = f"https://t.me/c/{str(chat_id)[4:]}/{sent_msg.message_id}"
    else:
        post_link = f"https://t.me/{chat.username}/{sent_msg.message_id}"
    
    await state.clear()
    await message.answer(
        f"✅ **Конкурс создан!**\n\n"
        f"📌 {title}\n"
        f"📤 {chat.title or chat.username}\n"
        f"🔗 [Перейти к посту]({post_link})",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(Command("draw"))
async def draw_cmd(message: types.Message):
    chat_id = message.chat.id
    draw_id = await get_active_draw_by_chat(chat_id)
    
    if not draw_id:
        await message.answer("❌ Нет активного конкурса")
        return
    
    draw_info = await get_draw_info(draw_id)
    title = draw_info['title']
    winners_count = draw_info['winners_count']
    
    participants = await get_all_participants(draw_id)
    if not participants:
        await message.answer("❌ Нет участников")
        return
    
    if len(participants) <= winners_count:
        winners = participants
    else:
        winners = random.sample(participants, winners_count)
    
    winner_text = ""
    medals = ["🥇", "🥈", "🥉"]
    for idx, (uid, uname, fname) in enumerate(winners, 1):
        name = f"@{uname}" if uname else fname or str(uid)
        medal = medals[idx-1] if idx <= 3 else f"#{idx}"
        winner_text += f"{medal} {name}\n"
    
    await close_draw(draw_id)
    await clear_participants(draw_id)
    
    await message.answer(
        f"🏆 **РОЗЫГРЫШ ЗАВЕРШЁН!**\n\n"
        f"📌 {title}\n\n"
        f"🎉 **Победители:**\n{winner_text}",
        parse_mode=ParseMode.MARKDOWN
    )
    
    for uid, uname, fname in winners:
        try:
            await bot.send_message(
                uid,
                f"🎉 Ты выиграл в конкурсе **«{title}»**!",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

@dp.message(Command("draws"))
async def list_draws(message: types.Message):
    draws = await get_all_draws()
    if not draws:
        await message.answer("📭 Нет конкурсов")
        return
    
    text = "📋 **Конкурсы:**\n\n"
    for draw_id, title, is_active in draws:
        status = "✅" if is_active else "❌"
        text += f"{status} #{draw_id} | {title}\n"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ============================================
#  WEBHOOK (ИСПРАВЛЕНО!)
# ============================================
async def on_startup(app):
    """Запускается при старте веб-приложения"""
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="create", description="Создать конкурс"),
        BotCommand(command="draw", description="Завершить конкурс"),
        BotCommand(command="draws", description="Список конкурсов"),
    ])
    
    logger.info("✅ Бот запущен через Webhook!")

async def on_shutdown(app):
    """Запускается при остановке"""
    await bot.delete_webhook()
    await bot.session.close()
    logger.info("❌ Бот остановлен")

# ============================================
#  ЗАПУСК
# ============================================
def main():
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

if __name__ == "__main__":
    main()
