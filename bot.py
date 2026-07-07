import os
import asyncio
import sqlite3
from typing import Any, Awaitable, Callable, Dict
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, BaseMiddleware, types
from aiogram.filters import CommandStart, Command
from google import genai

load_dotenv()

# Инициализация конфигурации
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
THROTTLING_DELAY = float(os.getenv("THROTTLING_DELAY", 3.0))

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher()
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_NAME = "gemini-2.5-flash"


# 🗄️ РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
DB_FILE = "users.db"

def init_db():
    """Создает таблицу пользователей и добавляет админа, если их нет"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        # Всегда добавляем главного админа в белый список автоматически
        if ADMIN_ID != 0:
            cursor.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (ADMIN_ID,))
        conn.commit()

def add_user_to_db(user_id: int) -> bool:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO allowed_users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False  # Пользователь уже был в базе

def remove_user_from_db(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0

def get_all_users() -> list:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM allowed_users")
        return [row[0] for row in cursor.fetchall()]

def is_user_allowed(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None


# 🛠️ MIDDLEWARE БЕЗОПАСНОСТИ
class SecurityMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self.user_cooldowns: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, types.Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        current_time = asyncio.get_event_loop().time()

        # 1. Проверка вайтлиста через БД
        if not is_user_allowed(user_id):
            if event.text and not event.text.startswith('/'):
                await event.answer("⛔ Доступ ограничен. Вы не внесены в белый список.")
            return

        # 2. Проверка троттлинга (админа тоже ограничиваем, чтобы случайно не зафлудить API)
        last_request_time = self.user_cooldowns.get(user_id, 0.0)
        if current_time - last_request_time < THROTTLING_DELAY:
            if current_time - last_request_time > 1.0:
                await event.answer(f"⚠️ Пауза! Подождите {THROTTLING_DELAY} сек. между запросами.")
            return

        self.user_cooldowns[user_id] = current_time
        return await handler(event, data)


dp.message.middleware(SecurityMiddleware())


# 👑 АДМИН-КОМАНДЫ
@dp.message(Command("add"))
async def cmd_add_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        # Извлекаем ID из текста команды (например, "/add 123456")
        target_id = int(message.text.split()[1])
        if add_user_to_db(target_id):
            await message.answer(f"✅ Пользователь `{target_id}` успешно добавлен в вайтлист.")
        else:
            await message.answer("ℹ️ Этот пользователь уже есть в списке.")
    except (IndexError, ValueError):
        await message.answer("❌ Ошибка. Используйте формат: `/add ТЕЛЕГРАМ_ID`")

@dp.message(Command("del"))
async def cmd_del_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    try:
        target_id = int(message.text.split()[1])
        if target_id == ADMIN_ID:
            await message.answer("❌ Вы не можете удалить сами себя из списка администраторов.")
            return
            
        if remove_user_from_db(target_id):
            await message.answer(f"🗑️ Пользователь `{target_id}` удален из вайтлиста.")
        else:
            await message.answer("ℹ️ Пользователь не найден в базе данных.")
    except (IndexError, ValueError):
        await message.answer("❌ Ошибка. Используйте формат: `/del ТЕЛЕГРАМ_ID`")

@dp.message(Command("list"))
async def cmd_list_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    users = get_all_users()
    users_str = "\n".join([f"• `{u}`" + (" (Админ)" if u == ADMIN_ID else "") for u in users])
    await message.answer(f"📋 **Белый список пользователей:**\n\n{users_str}", parse_mode="Markdown")


# 🤖 ОСНОВНАЯ ЛОГИКА
def ask_gemini(prompt: str) -> str:
    try:
        response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text
    except Exception as e:
        return f"Ошибка при обращении к AI: {e}"

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Вы авторизованы в системе. Отправьте мне любой вопрос для Gemini.")

@dp.message()
async def handle_message(message: types.Message):
    if not message.text or message.text.startswith('/'):
        return
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    reply_text = await asyncio.to_thread(ask_gemini, message.text)
    await message.answer(reply_text)


async def main():
    init_db()  # Инициализируем базу данных перед стартом
    print("Бот запущен с базой данных и админ-панелью...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
