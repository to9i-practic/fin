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
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from supabase import create_client, Client
from dotenv import load_dotenv
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

if not BOT_TOKEN:
    logging.error("ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

# Инициализация клиентов
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# Точная рабочая модель из логов вашего аккаунта
# Список доступных моделей Groq по приоритету (от легких к тяжелым)
AVAILABLE_MODELS = [
    "groq/compound-mini",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "groq/compound"
]

def safe_groq_completion(prompt: str) -> str:
    """Пробует отправить запрос к Groq по очереди через разные модели при ошибках/лимитах."""
    for model_name in AVAILABLE_MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            logging.info(f"✅ Успешный ответ от модели Groq: {model_name}")
            return response.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"⚠️ Модель {model_name} временно недоступна ({e}). Пробуем следующую...")
    
    raise Exception("Все модели Groq временно недоступны из-за лимитов.")

# Нормализатор категорий (объединяет синонимы)
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

# Резервный парсер
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

class ProfileSetup(StatesGroup):
    waiting_for_balance = State()
    waiting_for_budget = State()

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
            "• /profile — твой баланс и бюджет.\n"
            "• /stats — сводка расходов.\n"
            "• /audit — советы от AI.\n"
            "• /reset — сбросить профиль."
        )

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

# Парсер с работающей моделью
def parse_financial_text(text: str) -> dict:
    prompt = f"""
    Ты — финансовый классификатор. Разбери текст: "{text}"
    
    Категории: Еда (включает: обед, ужин, завтрак, еда, продукты, кафе, ресторан), Кофе, Транспорт (такси, метро, автобус), Жилье, Развлечения, Доход, Другое.
    
    Верни строго чистый JSON без разметки:
    {{"amount": 100, "type": "expense", "category": "Еда"}}
    """
    
    try:
        raw = safe_groq_completion(prompt)
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
        
        data = json.loads(raw)
        data["category"] = normalize_category(data.get("category", "Другое"))
        return data
    except Exception as e:
        logging.warning(f"⚠️ Все модели Groq недоступны ({e}), переходим на резервный локальный парсер.")
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
        logging.error(f"❌ Ошибка голоса: {e}")
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
        audit_text = safe_groq_completion(prompt)
        await message.answer(f"💡 **AI-Аудит твоего бюджета:**\n\n{audit_text}")
    except Exception as e:
        logging.error(f"Ошибка генерации аудита: {e}")
        await message.answer("Не удалось сгенерировать аудит (все модели сейчас перегружены). Попробуй через пару минут.")

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
    threading.Thread(target=run_dummy_server, daemon=True).start()
    logging.info("Бот с AI-распознаванием запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())