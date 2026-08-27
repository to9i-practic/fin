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

# Резервный парсер на случай сбоя API
def fallback_parse(text: str) -> dict:
    text_lower = text.lower().strip()
    numbers = re.findall(r'\d+(?:[\.,]\d+)?', text_lower)
    if not numbers:
        return {"amount": 0, "type": "expense", "category": "Другое"}
    
    amount = float(numbers[0].replace(",", "."))
    income_keywords = ["зарплата", "аванс", "перевод", "доход", "подарили", "кешбэк", "пришло"]
    tx_type = "income" if any(word in text_lower for word in income_keywords) else "expense"
    
    clean_text = re.sub(r'\d+(?:[\.,]\d+)?', '', text_lower).strip()
    category = clean_text.capitalize() if clean_text else "Другое"
    
    return {"amount": amount, "type": tx_type, "category": category}

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
            "• Отправляй текст или голосовые (например, «капучино 250» или «зарплата 50000»).\n"
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

# Функция парсинга через AI Groq (с подстраховкой)
def parse_financial_text(text: str) -> dict:
    prompt = f"""
    Ты — универсальный парсер финансовых транзакций. Проанализируй текст и выдели сумму, тип и категорию.
    
    Текст: "{text}"
    
    Правила:
    1. "amount": число (любые цифры в тексте, например "Кофе100" -> 100). Если цифр нет, верни 0.
    2. "type": "expense" (расход/покупка/оплата) или "income" (доход/зарплата/перевод мне).
    3. "category": название категории с большой буквы на русском (например: "Такси", "Продукты", "Жилье", "Кофе", "Развлечения").
    
    Верни СТРОГО valid JSON:
    {{"amount": 100, "type": "expense", "category": "Кофе"}}
    """
    
    try:
        response = groq_client.chat.completions.create(
            model="llama3-8b-8192",  # Используем официально поддерживаемую модель Groq
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.warning(f"⚠️ Groq API недоступен ({e}), переключаемся на резервный парсер.")
        return fallback_parse(text)

# Сохранение транзакции
async def save_transaction(user_id: int, parsed_data: dict, raw_text: str, message: Message):
    try:
        amount = round(float(parsed_data.get("amount", 0)), 2)
        tx_type = str(parsed_data.get("type", "expense"))
        category = str(parsed_data.get("category", "Другое"))
        
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
        await message.answer("Ошибка сохранения данных. Напиши /start и попробуй снова.")

# Обработка текстовых сообщений
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_transaction(message: Message):
    try:
        parsed = parse_financial_text(message.text)
        await save_transaction(message.from_user.id, parsed, message.text, message)
    except Exception as e:
        logging.error(f"❌ Ошибка обработки текста: {e}")
        traceback.print_exc()
        await message.answer("Не смог распознать запись. Напиши понятнее, например: «Такси 350»")

# Обработка голосовых сообщений через Whisper AI
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
        await message.answer("У тебя пока нет записанных транзакций. Напиши, например: «Кофе 250»")
        return

    total_expense = sum(float(t["amount"]) for t in transactions if t["type"] == "expense")
    total_income = sum(float(t["amount"]) for t in transactions if t["type"] == "income")
    
    categories = {}
    for t in transactions:
        if t["type"] == "expense":
            cat = t["category"]
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
    Ты — опытный финансовый советник. Проанализируй профиль пользователя и историю его транзакций.
    
    Месячный бюджет: {user_data.get('monthly_budget', 0)} ₽
    Текущий баланс: {user_data.get('balance', 0)} ₽
    
    История последних операций:
    {history_summary}
    
    Сделай краткий финансовый аудит (до 150 слов) на русском языке с рекомендациями.
    """

    try:
        response = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[{"role": "user", "content": prompt}]
        )
        audit_text = response.choices[0].message.content
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
    # 1. Автоматически проверяем и выводим в логи доступные модели Groq
    try:
        models = groq_client.models.list()
        available_ids = [m.id for m in models.data]
        logging.info("=" * 50)
        logging.info(f"✅ ДОСТУПНЫЕ МОДЕЛИ GROQ ДЛЯ ВАШЕГО КЛЮЧА:\n{available_ids}")
        logging.info("=" * 50)
    except Exception as e:
        logging.error(f"❌ Ошибка получения списка моделей Groq: {e}")

    # 2. Запускаем заглушку порта и Telegram-бота
    threading.Thread(target=run_dummy_server, daemon=True).start()
    logging.info("Бот с AI-распознаванием запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())