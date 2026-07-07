import os
import asyncio
import sqlite3
import google-generativeai
from typing import Any, Awaitable, Callable, Dict, List
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, BaseMiddleware, types
from aiogram.filters import CommandStart, Command
from google import genai
from google.genai import types as genai_types

load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
THROTTLING_DELAY = float(os.getenv("THROTTLING_DELAY", 3.0))

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher()
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_NAME = "gemini-2.5-flash"

# --- ХРАНИЛИЩЕ КОНТЕКСТА ---
# Словарь, где ключ - user_id, а значение - список объектов Content (история)
USER_HISTORY: Dict[int, List[genai_types.Content]] = {}
# Лимит истории (в парах вопрос-ответ). 10 значит, что бот помнит последние 10 реплик.
# Это защищает оперативную память от переполнения и экономит токены.
MAX_HISTORY_LEN = 10 
# ----------------------------

DB_FILE = "users.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS allowed_users (user_id INTEGER PRIMARY KEY)")
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
        return False

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


# 🛠️ MIDDLEWARE
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

        if not is_user_allowed(user_id):
            if event.text and not event.text.startswith('/'):
                await event.answer("⛔ Доступ ограничен. Вы не внесены в белый список.")
            return

        last_request_time = self.user_cooldowns.get(user_id, 0.0)
        if current_time - last_request_time < THROTTLING_DELAY:
            if current_time - last_request_time > 1.0:
                await event.answer(f"⚠️ Пауза! Подождите {THROTTLING_DELAY} сек.")
            return

        self.user_cooldowns[user_id] = current_time
        return await handler(event, data)

dp.message.middleware(SecurityMiddleware())


# 👑 АДМИН-КОМАНДЫ
@dp.message(Command("add"))
async def cmd_add_user(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(message.text.split()[1])
        if add_user_to_db(target_id):
            await message.answer(f"✅ Пользователь `{target_id}` добавлен.")
        else:
            await message.answer("ℹ️ Уже в списке.")
    except (IndexError, ValueError):
        await message.answer("❌ Формат: `/add ID`")

@dp.message(Command("del"))
async def cmd_del_user(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(message.text.split()[1])
        if target_id == ADMIN_ID:
            await message.answer("❌ Нельзя удалить себя.")
            return
        if remove_user_from_db(target_id):
            await message.answer(f"🗑️ Пользователь `{target_id}` удален.")
            # Стираем его контекст из памяти при удалении
            USER_HISTORY.pop(target_id, None)
        else:
            await message.answer("ℹ️ Не найден.")
    except (IndexError, ValueError):
        await message.answer("❌ Формат: `/del ID`")

@dp.message(Command("list"))
async def cmd_list_users(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    users = get_all_users()
    users_str = "\n".join([f"• `{u}`" + (" (Админ)" if u == ADMIN_ID else "") for u in users])
    await message.answer(f"📋 **Белый список:**\n\n{users_str}", parse_mode="Markdown")


# 🧹 КОМАНДА ДЛЯ СБРОСА КОНТЕКСТА
@dp.message(Command("clear"))
async def cmd_clear_context(message: types.Message):
    user_id = message.from_user.id
    if user_id in USER_HISTORY:
        USER_HISTORY[user_id] = []
        await message.answer("🧹 История нашего диалога очищена. Бот всё забыл!")
    else:
        await message.answer("ℹ️ Ваша история диалога и так пуста.")


# 🤖 РАБОТА С ИИ И КОНТЕКСТОМ
def ask_gemini_with_context(user_id: int, prompt: str) -> str:
    try:
        # Инициализируем историю для пользователя, если её нет
        if user_id not in USER_HISTORY:
            USER_HISTORY[user_id] = []
            
        # Используем встроенный механизм чата из актуальной библиотеки google-genai
        chat = ai_client.chats.create(
            model=MODEL_NAME,
            history=USER_HISTORY[user_id]
        )
        
        # Отправляем сообщение ИИ
        response = chat.send_message(prompt)
        
        # Обновляем историю в нашем словаре
        USER_HISTORY[user_id] = chat.get_history()
        
        # Обрезаем историю, если она превышает лимит (убираем самые старые сообщения)
        # Умножаем на 2, так как одна реплика состоит из 'user' и 'model'
        if len(USER_HISTORY[user_id]) > MAX_HISTORY_LEN * 2:
            USER_HISTORY[user_id] = USER_HISTORY[user_id][-(MAX_HISTORY_LEN * 2):]
            
        return response.text
    except Exception as e:
        return f"Ошибка при обращении к AI: {e}"


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Вы авторизованы.\n"
        "Я помню контекст нашей беседы. Если вы захотите начать тему с чистого листа, введите команду /clear"
    )

@dp.message()
async def handle_message(message: types.Message):
    if not message.text or message.text.startswith('/'):
        return
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    # Передаем user_id, чтобы Gemini знал, чью историю подгружать
    reply_text = await asyncio.to_thread(ask_gemini_with_context, message.from_user.id, message.text)
    await message.answer(reply_text)


async def main():
    init_db()
    print("Бот запущен с контекстом и БД...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
