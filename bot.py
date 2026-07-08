import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import Message, TelegramObject, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
import aiosqlite
from google import genai
from google.genai import types

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКА И ИНИЦИАЛИЗАЦИЯ ИЗ ENV ХОСТИНГА ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_PATH = "bot_database.db"
MAX_CONTEXT_LEN = 50  # Лимит контекста (последние 50 сообщений)

if not BOT_TOKEN or not ADMIN_ID:
    logger.critical("Критические переменные окружения (TELEGRAM_BOT_TOKEN или ADMIN_ID) не заданы!")
    raise ValueError("Критические переменные окружения не заданы!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client()


# --- REPLY КЛАВИАТУРА ---
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🧹 Очистить контекст")]],
        resize_keyboard=True,
        input_field_placeholder="Отправьте text или любой файл..."
    )


# --- БАЗА ДАННЫХ (SQLite) ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (int(ADMIN_ID),))
        await db.commit()


async def is_user_allowed(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


async def add_to_whitelist(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,))
        await db.commit()


async def remove_from_whitelist(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
        await db.commit()


async def clear_user_context(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        await db.commit()


async def save_chat_message(user_id: int, role: str, text: str):
    """Сохранение сообщения и автоматическая очистка старого контекста при превышении лимита"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Записываем новое сообщение
        await db.execute(
            "INSERT INTO chat_history (user_id, role, text) VALUES (?, ?, ?)", 
            (user_id, role, text)
        )
        
        # 2. Проверяем текущее количество сообщений пользователя
        async with db.execute(
            "SELECT COUNT(*) FROM chat_history WHERE user_id = ?", 
            (user_id,)
        ) as cursor:
            res = await cursor.fetchone()
            count = res[0] if res else 0

        # 3. Если перешагнули лимит — удаляем самое старое сообщение (FIFO)
        if count > MAX_CONTEXT_LEN:
            await db.execute("""
                DELETE FROM chat_history 
                WHERE id IN (
                    SELECT id FROM chat_history 
                    WHERE user_id = ? 
                    ORDER BY timestamp ASC 
                    LIMIT ?
                )
            """, (user_id, count - MAX_CONTEXT_LEN))
            
        await db.commit()


async def get_user_context(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, text FROM chat_history WHERE user_id = ? ORDER BY timestamp ASC", 
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            history = []
            for role, text in rows:
                history.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=text)]
                ))
            return history


# --- МИДЛВАРЬ: ДОСТУП И ТРОТТЛИНГ ---
class AccessAndThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.5):
        self.rate_limit = rate_limit
        self.last_requests: Dict[int, float] = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id

        if not await is_user_allowed(user_id):
            if event.chat.type == "private":
                await event.answer("⛔ Вас нет в whitelist. Доступ заблокирован.")
            return

        now = asyncio.get_event_loop().time()
        last_time = self.last_requests.get(user_id, 0)
        if now - last_time < self.rate_limit:
            await event.answer("⚠️ Не спамьте. Подождите перед следующим запросом.")
            return

        self.last_requests[user_id] = now
        return await handler(event, data)

dp.message.middleware(AccessAndThrottlingMiddleware())

# --- ОБРАБОТЧИКИ КОМАНД АДМИНИСТРАТОРА ---
@dp.message(Command("add"))
async def cmd_add_user(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("❌ Сделайте ответ (reply) на сообщение пользователя, чтобы добавить его.")
        return

    target_user = message.reply_to_message.from_user
    await add_to_whitelist(target_user.id)
    
    username_str = f" (@{target_user.username})" if target_user.username else ""
    await message.answer(f"✅ Пользователь {target_user.full_name}{username_str} добавлен в whitelist.")


@dp.message(Command("remove"))
async def cmd_remove_user(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("❌ Сделайте ответ (reply) на сообщение пользователя, чтобы удалить его.")
        return

    target_user = message.reply_to_message.from_user
    
    if str(target_user.id) == str(ADMIN_ID):
        await message.answer("❌ Нельзя удалить из вайтлиста главного администратора.")
        return

    await remove_from_whitelist(target_user.id)
    await clear_user_context(target_user.id)
    
    username_str = f" (@{target_user.username})" if target_user.username else ""
    await message.answer(f"🗑️ Пользователь {target_user.full_name}{username_str} удален из whitelist. Его контекст очищен.")


# --- ОБРАБОТЧИКИ УПРАВЛЕНИЯ КОНТЕКСТОМ ---
@dp.message(F.text == "🧹 Очистить контекст")
@dp.message(Command("clear"))
async def handle_clear_context(message: Message):
    await clear_user_context(message.from_user.id)
    await message.answer("🧹 История вашей беседы с ИИ полностью очищена!", reply_markup=get_main_keyboard())


@dp.message(Command("start"))
async def handle_start(message: Message):
    await message.answer("Привет! Бот готов к работе. Отправьте мне текст или любой файл.", reply_markup=get_main_keyboard())


# --- ВСЕЯДНАЯ МУЛЬТИМОДАЛЬНОСТЬ ---
def get_mime_type(message: Message) -> str:
    if message.photo: return "image/jpeg"
    if message.document: return message.document.mime_type or "application/octet-stream"
    if message.voice: return message.voice.mime_type or "audio/ogg"
    if message.audio: return message.audio.mime_type or "audio/mpeg"
    if message.video: return message.video.mime_type or "video/mp4"
    if message.video_note: return "video/mp4"
    return "application/octet-stream"


def get_file_id_from_message(message: Message) -> str:
    if message.photo: return message.photo[-1].file_id
    if message.document: return message.document.file_id
    if message.voice: return message.voice.file_id
    if message.audio: return message.audio.file_id
    if message.video: return message.video.file_id
    if message.video_note: return message.video_note.file_id
    return ""


# --- ФИЛЬТР УПОМИНАНИЙ И ОТВЕТОВ В ГРУППАХ ---
async def should_respond(message: Message, bot_info) -> bool:
    if message.chat.type == "private":
        return True
        
    if message.text and f"@{bot_info.username}" in message.text:
        return True
    if message.caption and f"@{bot_info.username}" in message.caption:
        return True
        
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_info.id:
            return True
            
    return False


# --- ОСНОВНОЙ ХЕНДЛЕР: СТРИМИНГ С КОНТЕКСТОМ И ФАЙЛАМИ ---
@dp.message()
async def handle_universal_input(message: Message, bot: Bot):
    user_id = message.from_user.id
    bot_info = await bot.get_me()
    
    if not await should_respond(message, bot_info):
        return

    status_msg = await message.answer("🤖 Анализирую данные...")

    raw_text = message.text or message.caption or "Проанализируй этот файл"
    prompt_text = raw_text.replace(f"@{bot_info.username}", "").strip()

    contents_payload = []
    file_id = get_file_id_from_message(message)
    
    if file_id:
        try:
            file_info = await bot.get_file(file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            mime_type = get_mime_type(message)
            
            file_part = types.Part.from_bytes(
                data=file_bytes.read(),
                mime_type=mime_type
            )
            contents_payload.append(file_part)
        except Exception as file_err:
            logger.error(f"Сетевая ошибка при скачивании файла из Telegram для пользователя {user_id}: {file_err}", exc_info=True)
            await bot.edit_message_text(f"❌ Ошибка загрузки медиафайла мессенджером.", chat_id=message.chat.id, message_id=status_msg.message_id)
            return

    contents_payload.append(types.Part.from_text(text=prompt_text))
    history_context = await get_user_context(user_id)
    
    current_user_content = types.Content(
        role="user",
        parts=contents_payload
    )
    full_request_contents = history_context + [current_user_content]

    collected_text = ""
    last_ui_text = ""
    chunk_counter = 0

    try:
        response_stream = await ai_client.aio.models.generate_content_stream(
            model="gemini-2.0-flash",
            contents=full_request_contents
        )

        async for chunk in response_stream:
            if chunk.text:
                collected_text += chunk.text
                chunk_counter += 1

                if chunk_counter % 15 == 0:
                    if collected_text.strip() != last_ui_text:
                        await bot.edit_message_text(
                            chat_id=message.chat.id,
                            message_id=status_msg.message_id,
                            text=collected_text,
                        )
                        last_ui_text = collected_text
                        await asyncio.sleep(0.1)

        if collected_text.strip() != last_ui_text:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=collected_text,
                parse_mode="Markdown"
            )

        await save_chat_message(user_id, "user", prompt_text)
        await save_chat_message(user_id, "model", collected_text)

    except Exception as e:
        logger.error(f"Сетевая ошибка Gemini API при запросе пользователя {user_id}: {e}", exc_info=True)
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            text="❌ Ошибка связи с сервером ИИ. Попробуйте позже.",
        )


# --- ЗАПУСК БОТА ---
async def main():
    await init_db()
    logger.info("База данных успешно инициализирована. Запуск бота...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
