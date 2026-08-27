import os
import sys
import json
import logging
import threading
import traceback
import re
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand, BotCommandScopeDefault
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from supabase import create_client, Client
from dotenv import load_dotenv

# Клиенты ИИ
from google import genai
from groq import Groq

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Загрузка переменных окружения
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    logging.error("ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

# Инициализация сервисов
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Инициализация ИИ провайдеров
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Мультипровайдерный каскад моделей (по приоритету)
CASCADE_MODELS = [
    {"provider": "gemini", "name": "gemini-2.5-flash"},
    {"provider": "groq",   "name": "groq/compound-mini"},
    {"provider": "groq",   "name": "qwen/qwen3.6-27b"},
    {"provider": "groq",   "name": "openai/gpt-oss-20b"},
    {"provider": "groq",   "name": "groq/compound"}
]

def safe_llm_completion(prompt: str) -> str:
    """Универсальный каскад: перебирает Gemini и Groq при ошибках лимита/доступности."""
    for model_info in CASCADE_MODELS:
        provider = model_info["provider"]
        model_name = model_info["name"]
        
        try:
            if provider == "gemini" and gemini_client:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                logging.info(f"✅ Успешный ответ от Gemini ({model_name})")
                return response.text.strip()
                
            elif provider == "groq" and groq_client:
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}]
                )
                logging.info(f"✅ Успешный ответ от Groq ({model_name})")
                return response.choices[0].message.content.strip()

        except Exception as e:
            logging.warning(f"⚠️ Модель {provider}:{model_name} недоступна ({e}). Пробуем следующую...")

    raise Exception("❌ Все доступные модели во всех провайдерах исчерпали лимиты.")

# Нормализатор категорий
def normalize_category(raw_category: str) -> str:
    cat = str(raw_category).lower().strip()
    
    food_words = ["обед", "еда", "продукты", "ужин", "завтрак", "кафе", "ресторан", "перекус"]
    transport_words = ["такси", "автобус", "метро", "бензин", "транспорт", "убер", "яндекс"]
    house_words = ["жкх", "квартира", "аренда", "коммуналка", "жилье"]
    
    if any(w in cat for w in food_words):
        return "Еда"
    if any(w in cat for w in transport_words):
        return "Транспорт"
    if any(w in cat for w in house_words):
        return "Жилье"
    if "кофе" in cat or "капучино" in cat or "латте" in cat:
        return "Кофе"
        
    return raw_category.capitalize()

# Резервный локальный парсер
def fallback_parse(text: str) -> dict:
    text_lower = text.lower().strip()
    numbers = re.findall(r'\d+(?:[\.,]\d+)?', text_lower)
    if not numbers:
        return {"amount": 0, "type": "expense", "category": "Другое"}
    
    amount = float(numbers[0].replace(",", "."))
    income_keywords = ["зарплата", "аванс", "перевод", "доход", "подарили", "кешбэк", "пришло"]
    tx_type = "income" if any(word in text_lower for word in income_keywords) else "expense"
    
    clean_text = re.sub(r'\d+(?:[\.,]\d+)?', '', text_lower).strip()
    raw_cat = clean_text if clean_text else "Другое"
    
    return {"amount": amount, "type": tx_type, "category": normalize_category(raw_cat)}

# Состояния FSM
class ProfileSetup(StatesGroup):
    waiting_for_balance = State()
    waiting_for_budget = State()

class ChatSetup(StatesGroup):
    in_chat = State()

# Настройка интерактивного меню Telegram
async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Перезапустить / Старт"),
        BotCommand(command="chat", description="💬 Пообщаться с ИИ (свободный диалог)"),
        BotCommand(command="profile", description="📊 Мой баланс и бюджет"),
        BotCommand(command="stats", description="📈 Аналитика трат по категориям"),
        BotCommand(command="audit", description="🧠 AI-Аудит и советы по бюджету"),
        BotCommand(command="reset", description="🔄 Сбросить профиль")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    response = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    if not response.data:
        supabase.table("users").insert({"telegram_id": user_id}).execute()
        await message.answer(
            "Привет! Я твой AI-ассистент по финансам.\n\n"
            "Давай настроим профиль. Напиши, сколько денег у тебя сейчас **в наличии** (текущий баланс):"
        )
        await state.set_state(ProfileSetup.waiting_for_balance)
    else:
        await message.answer(
            "С возвращением!\n"
            "• Отправляй текст или голосовые (например, «обед 250» или «зарплата 50000»).\n"
            "• 💬 /chat — свободный диалог с ИИ.\n"
            "• 📊 /profile — баланс и бюджет.\n"
            "• 📈 /stats — статистика.\n"
            "• 🧠 /audit — финансовый аудит.\n"
            "• 🔄 /reset — сбросить профиль."
        )

# Свободный диалог с ИИ
@dp.message(Command("chat"))
async def cmd_start_chat(message: Message, state: FSMContext):
    await state.set_state(ChatSetup.in_chat)
    await message.answer(
        "💬 **Режим свободного общения с ИИ активирован!**\n\n"
        "Спрашивай обо всем: от финансовых советов до бытовых тем.\n"
        "Чтобы вернуть бота к трекингу расходов, напиши `/exit` или выбери любую команду в меню."
    )

@dp.message(Command("exit"), ChatSetup.in_chat)
async def cmd_exit_chat(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚪 Вы вышли из режима диалога. Сообщения снова записываются как расходы/доходы!")

@dp.message(ChatSetup.in_chat, F.text & ~F.text.startswith("/"))
async def handle_ai_chat(message: Message):
    prompt = f"""
    Ты — дружелюбный и умный AI-ассистент. Пользователь общается с тобой в свободном формате.
    Отвечай вежливо, лаконично (до 2-3 абзацев), поддерживай диалог на любые темы.
    
    Сообщение пользователя: "{message.text}"
    """
    try:
        reply = safe_llm_completion(prompt)
        await message.answer(reply)
    except Exception as e:
        logging.error(f"Ошибка чата ИИ: {e}")
        await message.answer("Извини, не удалось обработать ответ. Попробуй спросить еще раз.")

@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        supabase.table("users").delete().eq("telegram_id", user_id).execute()
        await state.clear()
        await message.answer("🔄 Профиль сброшен. Напиши /start для повторной настройки!")
    except Exception as e:
        logging.error(f"Ошибка сброса: {e}")
        await message.answer("Не удалось сбросить профиль.")

@dp.message(ProfileSetup.waiting_for_balance)
async def process_balance(message: Message, state: FSMContext):
    try:
        balance = round(float(message.text.replace(",", ".")), 2)
        user_id = message.from_user.id
        supabase.table("users").update({"balance": balance}).eq("telegram_id", user_id).execute()
        await message.answer("Отлично! Теперь укажи твой **план расходов на месяц** (бюджет):")
        await state.set_state(ProfileSetup.waiting_for_budget)
    except ValueError:
        await message.answer("Пожалуйста, введи число.")

@dp.message(ProfileSetup.waiting_for_budget)
async def process_budget(message: Message, state: FSMContext):
    try:
        budget = round(float(message.text.replace(",", ".")), 2)
        user_id = message.from_user.id
        supabase.table("users").update({"monthly_budget": budget}).eq("telegram_id", user_id).execute()
        
        user_res = supabase.table("users").select("balance").eq("telegram_id", user_id).execute()
        raw_bal = user_res.data[0].get("balance") if user_res.data else 0.0
        balance = float(raw_bal) if raw_bal is not None else 0.0
        
        await message.answer(
            f"🎉 Настройка завершена!\n\n"
            f"• Доступно: {balance:.2f} ₽\n"
            f"• План на месяц: {budget:.2f} ₽\n\n"
            f"Просто пиши или наговаривай свои расходы и доходы!"
        )
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введи число.")

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    response = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    if response.data:
        user = response.data[0]
        balance = float(user.get('balance') or 0.0)
        budget = float(user.get('monthly_budget') or 0.0)
        await message.answer(
            f"📊 **Твой финансовый профиль:**\n\n"
            f"💰 Доступно средств: **{balance:.2f} ₽**\n"
            f"🎯 План расходов на месяц: **{budget:.2f} ₽**"
        )
    else:
        await message.answer("Профиль не найден. Напиши /start для регистрации.")

# Парсинг финансовых записей
def parse_financial_text(text: str) -> dict:
    prompt = f"""
    Ты — финансовый классификатор. Разбери текст: "{text}"
    Категории: Еда (включает: обед, ужин, завтрак, еда, продукты, кафе, ресторан), Кофе, Транспорт, Жилье, Развлечения, Доход, Другое.
    Верни строго чистый JSON без разметки:
    {{"amount": 100, "type": "expense", "category": "Еда"}}
    """
    try:
        raw = safe_llm_completion(prompt)
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
        
        data = json.loads(raw)
        data["category"] = normalize_category(data.get("category", "Другое"))
        return data
    except Exception as e:
        logging.warning(f"⚠️ Все модели отказали ({e}), переходим на резервный парсер.")
        return fallback_parse(text)

# Сохранение транзакции
async def save_transaction(user_id: int, parsed_data: dict, raw_text: str, message: Message):
    try:
        amount = round(float(parsed_data.get("amount", 0)), 2)
        tx_type = str(parsed_data.get("type", "expense"))
        category = normalize_category(parsed_data.get("category", "Другое"))
        
        if amount <= 0:
            await message.answer("Не удалось распознать сумму. Попробуй еще раз, например: «Кофе 250»")
            return

        supabase.table("transactions").insert({
            "telegram_id": int(user_id),
            "amount": amount,
            "type": tx_type,
            "category": category,
            "raw_text": str(raw_text)
        }).execute()

        user_res = supabase.table("users").select("balance").eq("telegram_id", user_id).execute()
        if user_res.data:
            raw_bal = user_res.data[0].get("balance")
            current_balance = float(raw_bal) if raw_bal is not None else 0.0
            
            new_balance = current_balance + amount if tx_type == "income" else current_balance - amount
            new_balance = round(new_balance, 2)
            
            supabase.table("users").update({"balance": new_balance}).eq("telegram_id", user_id).execute()
            
            icon = "🟢 + " if tx_type == "income" else "🔴 - "
            await message.answer(
                f"{icon}**{amount:.2f} ₽** ({category})\n"
                f"Текущий баланс: **{new_balance:.2f} ₽**"
            )
    except Exception as e:
        logging.error(f"❌ Ошибка в save_transaction: {e}")
        traceback.print_exc()
        await message.answer("Ошибка сохранения данных.")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_transaction(message: Message):
    try:
        parsed = parse_financial_text(message.text)
        await save_transaction(message.from_user.id, parsed, message.text, message)
    except Exception as e:
        logging.error(f"❌ Ошибка обработки текста: {e}")
        traceback.print_exc()
        await message.answer("Не смог распознать запись.")

@dp.message(F.voice)
async def handle_voice_transaction(message: Message):
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    
    local_voice_path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file_path, local_voice_path)
    
    try:
        if not groq_client:
            raise Exception("Groq client не инициализирован для Whisper.")

        with open(local_voice_path, "rb") as file_obj:
            transcription = groq_client.audio.transcriptions.create(
                file=(local_voice_path, file_obj.read()),
                model="whisper-large-v3",
                language="ru"
            )
        
        text = transcription.text
        await message.answer(f"🗣 Распознано: *«{text}»*")
        
        parsed = parse_financial_text(text)
        await save_transaction(message.from_user.id, parsed, text, message)
    except Exception as e:
        logging.error(f"❌ Ошибка распознавания голоса: {e}")
        traceback.print_exc()
        await message.answer("Не удалось обработать голосовое сообщение.")
    finally:
        if os.path.exists(local_voice_path):
            os.remove(local_voice_path)

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    res = supabase.table("transactions").select("*").eq("telegram_id", user_id).execute()
    transactions = res.data
    
    if not transactions:
        await message.answer("У тебя пока нет записанных транзакций.")
        return

    total_expense = sum(float(t["amount"]) for t in transactions if t["type"] == "expense")
    total_income = sum(float(t["amount"]) for t in transactions if t["type"] == "income")
    
    categories = {}
    for t in transactions:
        if t["type"] == "expense":
            cat = normalize_category(t["category"])
            categories[cat] = categories.get(cat, 0) + float(t["amount"])
            
    cat_text = "\n".join([f"• {cat}: **{amt:.2f} ₽**" for cat, amt in categories.items()])
    
    await message.answer(
        f"📊 **Аналитика трат:**\n\n"
        f"🔴 Всего расходов: **{total_expense:.2f} ₽**\n"
        f"🟢 Всего доходов: **{total_income:.2f} ₽**\n\n"
        f"**Расходы по категориям:**\n{cat_text if cat_text else 'Нет расходов'}"
    )

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    user_id = message.from_user.id
    user_res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    tx_res = supabase.table("transactions").select("*").eq("telegram_id", user_id).limit(30).execute()
    
    if not tx_res.data:
        await message.answer("Недостаточно данных для анализа. Добавь хотя бы 3–5 расходов!")
        return

    await message.answer("🧠 Анализирую твои финансы и готовлю аудит...")

    user_data = user_res.data[0] if user_res.data else {}
    history_summary = "\n".join([f"- {t['type']}: {t['amount']} ₽ ({t['category']})" for t in tx_res.data])
    
    prompt = f"""
    Ты — финансовый советник.
    Месячный бюджет: {user_data.get('monthly_budget', 0)} ₽
    Текущий баланс: {user_data.get('balance', 0)} ₽
    
    История последних операций:
    {history_summary}
    
    Сделай краткий финансовый аудит с 2-3 полезными советами на русском языке.
    """

    try:
        audit_text = safe_llm_completion(prompt)
        await message.answer(f"💡 **AI-Аудит твоего бюджета:**\n\n{audit_text}")
    except Exception as e:
        logging.error(f"Ошибка аудита: {e}")
        await message.answer("Не удалось сгенерировать аудит. Попробуй позже.")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

async def main():
    await set_main_menu(bot)
    threading.Thread(target=run_dummy_server, daemon=True).start()
    logging.info("Бот с каскадным AI-распознаванием запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())