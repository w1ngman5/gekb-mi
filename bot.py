import os
import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_NAME = "database.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_KEY)

# --- Инициализация БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, model TEXT)")
        await db.execute("INSERT OR IGNORE INTO whitelist VALUES (?)", (ADMIN_ID,))
        await db.commit()

# --- Вспомогательные функции ---
async def is_allowed(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def get_model(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT model FROM user_settings WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else "gpt-4o-mini"

# --- Клавиатура настроек ---
def get_settings_kb(current_model):
    buttons = [
        [InlineKeyboardButton(text="Очистить историю", callback_data="reset")],
        [
            InlineKeyboardButton(text="✅ GPT-4o" if current_model == "gpt-4o" else "GPT-4o", callback_data="model_gpt-4o"),
            InlineKeyboardButton(text="✅ GPT-mini" if current_model == "gpt-4o-mini" else "GPT-mini", callback_data="model_gpt-4o-mini")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Бот готов к работе. Используйте меню настроек для выбора модели.", reply_markup=get_settings_kb(await get_model(message.from_user.id)))

@dp.callback_query(F.data.startswith("model_"))
async def change_model(callback: types.CallbackQuery):
    model = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO user_settings (user_id, model) VALUES (?, ?)", (callback.from_user.id, model))
        await db.commit()
    await callback.answer(f"Выбрана модель: {model}")
    await callback.message.edit_reply_markup(reply_markup=get_settings_kb(model))

@dp.callback_query(F.data == "reset")
async def reset_history(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM history WHERE user_id = ?", (callback.from_user.id,))
        await db.commit()
    await callback.answer("История очищена")

@dp.message()
async def chat_handler(message: Message):
    if not await is_allowed(message.from_user.id):
        return await message.answer("У вас нет доступа.")

    user_id = message.from_user.id
    model = await get_model(user_id)
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем контекст
        async with db.execute("SELECT role, content FROM history WHERE user_id = ?", (user_id,)) as cursor:
            messages = [{"role": r[0], "content": r[1]} for r in await cursor.fetchall()]
        
        if not messages:
            messages = [{"role": "system", "content": "Ты полезный ассистент."}]
        
        messages.append({"role": "user", "content": message.text})

        # Запрос
        response = await client.chat.completions.create(model=model, messages=messages)
        answer = response.choices[0].message.content
        
        # Сохранение
        await db.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "user", message.text))
        await db.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "assistant", answer))
        await db.commit()

    await message.answer(answer, reply_markup=get_settings_kb(model))

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())