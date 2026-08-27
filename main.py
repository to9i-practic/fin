import os
import sys
import json
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, File
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import Groq

# Загружаем переменные окружения
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    print("ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

# Инициализация клиентов
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

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
            "• /stats — сводка расходов."
        )

@dp.message(ProfileSetup.waiting_for_balance)
async def process_balance(message: Message, state: FSMContext):
    try:
        balance = float(message.text.replace(",", "."))
        user_id = message.from_user.id
        supabase.table("users").update({"balance": balance}).eq("telegram_id", user_id).execute()
        await message.answer("Отлично! Теперь укажи твой **план расходов на месяц** (бюджет):")
        await state.set_state(ProfileSetup.waiting_for_budget)
    except ValueError:
        await message.answer("Пожалуйста, введи число (например, 50000).")

@dp.message(ProfileSetup.waiting_for_budget)
async def process_budget(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(",", "."))
        user_id = message.from_user.id
        supabase.table("users").update({"monthly_budget": budget}).eq("telegram_id", user_id).execute()
        
        await message.answer(
            f" Настройка завершена!\n\n"
            f"• Доступно: {budget:.2f} ₽\n"
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
        await message.answer(
            f"📊 **Твой финансовый профиль:**\n\n"
            f"💰 Доступно средств: **{user['balance']} ₽**\n"
            f"🎯 План расходов на месяц: **{user['monthly_budget']} ₽**"
        )
    else:
        await message.answer("Профиль не найден. Напиши /start для регистрации.")

# Функция парсинга текста через Groq AI
def parse_financial_text(text: str) -> dict:
    prompt = f"""
    Проанализируй текст финансовой транзакции и верни JSON ответа без лишнего текста:
    Текст: "{text}"
    
    Верни строго JSON со следующими полями:
    - "amount": число (сумма)
    - "type": "expense" (если расход/покупка) или "income" (если доход/зарплата/перевод мне)
    - "category": стандартизированная категория с большой буквы (например: "Кофе", "Продукты", "Такси", "Зарплата", "Развлечения", "Рестораны", "Здоровье")
    
    Примеры унификации:
    - "капучино", "кофий", "латте" -> category: "Кофе"
    - "таксичка", "яндекс го" -> category: "Такси"
    - "пятерочка", "еда" -> category: "Продукты"
    
    JSON формат:
    {{"amount": 250, "type": "expense", "category": "Кофе"}}
    """
    
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)

# Вспомогательная функция сохранения транзакции
async def save_transaction(user_id: int, parsed_data: dict, raw_text: str, message: Message):
    amount = parsed_data.get("amount", 0)
    tx_type = parsed_data.get("type", "expense")
    category = parsed_data.get("category", "Другое")
    
    if amount <= 0:
        await message.answer("Не удалось распознать сумму. Попробуй еще раз, например: «Кофе 250»")
        return

    # Записываем транзакцию в БД
    supabase.table("transactions").insert({
        "telegram_id": user_id,
        "amount": amount,
        "type": tx_type,
        "category": category,
        "raw_text": raw_text
    }).execute()

    # Обновляем баланс пользователя
    user_res = supabase.table("users").select("balance").eq("telegram_id", user_id).execute()
    if user_res.data:
        current_balance = float(user_res.data[0]["balance"])
        new_balance = current_balance + amount if tx_type == "income" else current_balance - amount
        supabase.table("users").update({"balance": new_balance}).eq("telegram_id", user_id).execute()
        
        icon = "🟢 + " if tx_type == "income" else "🔴 - "
        await message.answer(
            f"{icon}**{amount:.2f} ₽** ({category})\n"
            f"Текущий баланс: **{new_balance:.2f} ₽**"
        )

# Обработка текстовых сообщений о расходах/доходах
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_transaction(message: Message):
    try:
        parsed = parse_financial_text(message.text)
        await save_transaction(message.from_user.id, parsed, message.text, message)
    except Exception as e:
        print(f"Ошибка AI: {e}")
        await message.answer("Не смог распознать запись. Напиши понятнее, например: «Такси 350»")

# Обработка голосовых сообщений
@dp.message(F.voice)
async def handle_voice_transaction(message: Message):
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    
    local_voice_path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file_path, local_voice_path)
    
    try:
        # Расшифровка голоса через Whisper в Groq
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
        print(f"Ошибка распознавания голоса: {e}")
        await message.answer("Не удалось обработать голосовое сообщение.")
    finally:
        if os.path.exists(local_voice_path):
            os.remove(local_voice_path)
# Команда /stats — Вывод текущих расходов и доходов
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    
    # Получаем все транзакции пользователя
    res = supabase.table("transactions").select("*").eq("telegram_id", user_id).execute()
    transactions = res.data
    
    if not transactions:
        await message.answer("У тебя пока нет записанных транзакций. Напиши, например: «Кофе 250»")
        return

    total_expense = sum(t["amount"] for t in transactions if t["type"] == "expense")
    total_income = sum(t["amount"] for t in transactions if t["type"] == "income")
    
    # Группировка расходов по категориям
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

# Команда /audit — AI-аудит и финансовые советы
@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    user_id = message.from_user.id
    
    # Запрашиваем историю транзакций и профиль
    user_res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    tx_res = supabase.table("transactions").select("*").eq("telegram_id", user_id).limit(30).execute()
    
    if not tx_res.data:
        await message.answer("Недостаточно данных для анализа. Добавь хотя бы 3–5 расходов!")
        return

    await message.answer("🧠 Анализирую твои финансы и готовлю аудит...")

    user_data = user_res.data[0] if user_res.data else {}
    history_summary = "\n".join([f"- {t['type']}: {t['amount']} ₽ ({t['category']})" for t in tx_res.data])
    
    prompt = f"""
    Ты — опытный финансовый советник. Проанализируй финансовый профиль пользователя и историю его транзакций.
    
    Месячный бюджет: {user_data.get('monthly_budget', 0)} ₽
    Текущий баланс: {user_data.get('balance', 0)} ₽
    
    История последних операций:
    {history_summary}
    
    Сделай краткий финансовый аудит (до 150 слов) на русском языке:
    1. Оцени текущий ритм трат (Burn rate).
    2. Укажи на «слепые зоны» (где переплаты, например, слишком частое кофе или такси).
    3. Дай 3 конкретных, дружелюбных совета по оптимизации.
    """

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}]
        )
        audit_text = response.choices[0].message.content
        await message.answer(f"💡 **AI-Аудит твоего бюджета:**\n\n{audit_text}")
    except Exception as e:
        print(f"Ошибка аудита: {e}")
        await message.answer("Не удалось сгенерировать аудит. Попробуй позже.")

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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
    # Запускаем фейковый HTTP-сервер для прохождения проверки портов Render
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    print("Бот с AI-распознаванием запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())