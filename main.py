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
from aiogram.types import (
    Message, BotCommand, BotCommandScopeDefault,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from supabase import create_client, Client
from dotenv import load_dotenv

from google import genai
from groq import Groq

# Логирование
logging.basicConfig(level=logging.INFO)

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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

CASCADE_MODELS = [
    {"provider": "gemini", "name": "gemini-2.5-flash"},
    {"provider": "groq",   "name": "groq/compound-mini"},
    {"provider": "groq",   "name": "qwen/qwen3.6-27b"},
    {"provider": "groq",   "name": "openai/gpt-oss-20b"}
]

def safe_llm_completion(prompt: str) -> str:
    for model_info in CASCADE_MODELS:
        provider = model_info["provider"]
        model_name = model_info["name"]
        try:
            if provider == "gemini" and gemini_client:
                response = gemini_client.models.generate_content(model=model_name, contents=prompt)
                return response.text.strip()
            elif provider == "groq" and groq_client:
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}]
                )
                return response.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"⚠️ Модель {provider}:{model_name} недоступна: {e}")
    raise Exception("Все модели недоступны.")

# Клавиатуры
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Общение с ИИ (По умолчанию)")],
            [KeyboardButton(text="🌐 Переводчик"), KeyboardButton(text="📊 Финансовый аудитор")]
        ],
        resize_keyboard=True
    )

def get_finance_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Внести расход/доход"), KeyboardButton(text="📈 Статистика")],
            [KeyboardButton(text="🧠 AI-Аудит"), KeyboardButton(text="👤 Управление профилями")],
            [KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")]
        ],
        resize_keyboard=True
    )

# Состояния FSM
class ModeStates(StatesGroup):
    translator = State()
    finance_menu = State()
    
    # Финансовый профиль
    waiting_for_name = State()
    waiting_for_balance = State()
    waiting_for_budget = State()
    waiting_for_tx = State()
    
    # Профили
    selecting_profile = State()
    confirm_delete = State()

# --- МЕНЮ КОМАНД TELEGRAM ---
async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Главное меню / Общение с ИИ"),
        BotCommand(command="translator", description="🌐 Режим Переводчика"),
        BotCommand(command="finance", description="📊 Финансовый аудитор"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

@dp.message(Command("start"))
@dp.message(F.text == "🏠 Главное меню (Общение с ИИ)")
@dp.message(F.text == "💬 Общение с ИИ (По умолчанию)")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💬 **Вы в режиме свободного общения с ИИ (По умолчанию).**\n\n"
        "Задавайте любые вопросы текстом или голосом!\n"
        "Для переключения режимов используйте меню ниже.",
        reply_markup=get_main_keyboard()
    )

# ==========================================
# 2. РЕЖИМ ПЕРЕВОДЧИКА
# ==========================================
@dp.message(Command("translator"))
@dp.message(F.text == "🌐 Переводчик")
async def start_translator(message: Message, state: FSMContext):
    await state.set_state(ModeStates.translator)
    await message.answer(
        "🌐 **Режим Переводчика активирован!**\n\n"
        "Отправляйте текст или **голосовые сообщения** на любом языке — я переведу их на русский (или наоборот).\n\n"
        "Для выхода нажмите «🏠 Главное меню (Общение с ИИ)».",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")]],
            resize_keyboard=True
        )
    )

@dp.message(ModeStates.translator, F.text & ~F.text.startswith("/"))
async def handle_translator_text(message: Message):
    prompt = f"Ты — профессиональный переводчик. Если текст на русском, переведи его на английский. Если на другом языке — переведи на русский. Верни ТОЛЬКО перевод без лишних слов:\n\n{message.text}"
    try:
        translated = safe_llm_completion(prompt)
        await message.answer(f"🔀 **Перевод:**\n{translated}")
    except Exception as e:
        await message.answer("Ошибка перевода.")

# ==========================================
# 3. ФИНАНСОВЫЙ АУДИТОР (ПОДУЧЕТ)
# ==========================================
@dp.message(Command("finance"))
@dp.message(F.text == "📊 Финансовый аудитор")
async def start_finance(message: Message, state: FSMContext):
    user_id = message.from_user.id
    res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    if not res.data:
        await message.answer("👋 Добро пожаловать в Финансовый аудитор!\nУкажите **Имя владельца профиля** (например: Иван):", reply_markup=ReplyKeyboardRemove())
        await state.set_state(ModeStates.waiting_for_name)
    else:
        # Берем первый профиль
        data = await state.get_data()
        current_profile_id = data.get("profile_id", res.data[0]["id"])
        await state.update_data(profile_id=current_profile_id)
        
        prof = next((p for p in res.data if p["id"] == current_profile_id), res.data[0])
        await state.set_state(ModeStates.finance_menu)
        await message.answer(
            f"📊 **Финансовый аудитор** (Активный профиль: **{prof.get('name', 'Основной')}**)\n"
            f"💰 Баланс: {prof.get('balance', 0):.2f} ₽ | План: {prof.get('monthly_budget', 0):.2f} ₽\n\n"
            f"Выберите действие:",
            reply_markup=get_finance_keyboard()
        )

# Создание нового профиля
@dp.message(ModeStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(new_name=message.text.strip())
    await message.answer("Укажите текущий **наличный баланс** (числом):")
    await state.set_state(ModeStates.waiting_for_balance)

@dp.message(ModeStates.waiting_for_balance)
async def process_balance(message: Message, state: FSMContext):
    try:
        bal = float(message.text.replace(",", "."))
        await state.update_data(new_balance=bal)
        await message.answer("Укажите **план расходов на месяц** (бюджет):")
        await state.set_state(ModeStates.waiting_for_budget)
    except:
        await message.answer("Пожалуйста, введите число.")

@dp.message(ModeStates.waiting_for_budget)
async def process_budget(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(",", "."))
        data = await state.get_data()
        user_id = message.from_user.id
        
        res = supabase.table("users").insert({
            "telegram_id": user_id,
            "name": data["new_name"],
            "balance": data["new_balance"],
            "monthly_budget": budget
        }).execute()
        
        profile_id = res.data[0]["id"]
        await state.update_data(profile_id=profile_id)
        await state.set_state(ModeStates.finance_menu)
        await message.answer(f"🎉 Профиль **{data['new_name']}** успешно создан!", reply_markup=get_finance_keyboard())
    except Exception as e:
        logging.error(f"Ошибка создания: {e}")
        await message.answer("Ошибка при создании профиля.")

# Управление профилями (Смена/Удаление/Добавление)
@dp.message(ModeStates.finance_menu, F.text == "👤 Управление профилями")
async def manage_profiles(message: Message, state: FSMContext):
    user_id = message.from_user.id
    res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    text = "📂 **Ваши профили:**\n\n"
    buttons = []
    for p in res.data:
        text += f"• **{p['name']}** (Баланс: {p['balance']} ₽)\n"
        buttons.append([KeyboardButton(text=f"Выбрать: {p['name']}")])
        
    buttons.append([KeyboardButton(text="➕ Добавить новый профиль")])
    buttons.append([KeyboardButton(text="🗑 Удалить текущий профиль")])
    buttons.append([KeyboardButton(text="📊 Назад в аудитор")])
    
    await state.set_state(ModeStates.selecting_profile)
    await message.answer(text, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

@dp.message(ModeStates.selecting_profile, F.text.startswith("Выбрать: "))
async def select_profile(message: Message, state: FSMContext):
    name = message.text.replace("Выбрать: ", "").strip()
    user_id = message.from_user.id
    res = supabase.table("users").select("*").eq("telegram_id", user_id).eq("name", name).execute()
    
    if res.data:
        await state.update_data(profile_id=res.data[0]["id"])
        await message.answer(f"✅ Переключено на профиль: **{name}**")
    await start_finance(message, state)

@dp.message(ModeStates.selecting_profile, F.text == "➕ Добавить новый профиль")
async def add_new_profile_start(message: Message, state: FSMContext):
    await message.answer("Введите **Имя** для нового профиля:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ModeStates.waiting_for_name)

@dp.message(ModeStates.selecting_profile, F.text == "🗑 Удалить текущий профиль")
async def delete_profile(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    
    if pid:
        supabase.table("users").delete().eq("id", pid).execute()
        supabase.table("transactions").delete().eq("profile_id", pid).execute()
        await message.answer("🗑 Профиль и все его транзакции успешно удалены!")
        await state.clear()
        await start_finance(message, state)

@dp.message(ModeStates.finance_menu, F.text == "📈 Статистика")
async def finance_stats(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    res = supabase.table("transactions").select("*").eq("profile_id", pid).execute()
    
    if not res.data:
        await message.answer("Нет транзакций.")
        return
        
    total_exp = sum(float(t["amount"]) for t in res.data if t["type"] == "expense")
    cats = {}
    for t in res.data:
        if t["type"] == "expense":
            cats[t["category"]] = cats.get(t["category"], 0) + float(t["amount"])
            
    cat_str = "\n".join([f"• {k}: **{v:.2f} ₽**" for k, v in cats.items()])
    await message.answer(f"📈 **Расходы по категориям:**\n\n{cat_str}\n\n🔴 Всего: **{total_exp:.2f} ₽**")

@dp.message(ModeStates.finance_menu, F.text == "🧠 AI-Аудит")
async def finance_audit(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    prof = supabase.table("users").select("*").eq("id", pid).execute().data[0]
    txs = supabase.table("transactions").select("*").eq("profile_id", pid).limit(30).execute().data
    
    prompt = f"Сделай краткий финансовый аудит для {prof['name']}. Бюджет: {prof['monthly_budget']} ₽, Баланс: {prof['balance']} ₽. Транзакции: {txs}"
    try:
        reply = safe_llm_completion(prompt)
        await message.answer(f"💡 **AI-Аудит:**\n\n{reply}")
    except:
        await message.answer("Не удалось сгенерировать аудит.")

@dp.message(ModeStates.finance_menu, F.text == "➕ Внести расход/доход")
async def add_tx_prompt(message: Message, state: FSMContext):
    await state.set_state(ModeStates.waiting_for_tx)
    await message.answer("Отправьте текст или голосовое (например: «Обед 300» или «Зарплата 50000»):")

@dp.message(ModeStates.waiting_for_tx, F.text)
async def process_tx_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    
    prompt = f"Разбери запись: \"{message.text}\". Верни JSON: {{\"amount\": 100, \"type\": \"expense\", \"category\": \"Еда\"}}"
    try:
        raw = safe_llm_completion(prompt)
        if "```" in raw: raw = raw.split("```")[1].replace("json", "").strip()
        parsed = json.loads(raw)
        
        amt = float(parsed["amount"])
        supabase.table("transactions").insert({
            "telegram_id": message.from_user.id,
            "profile_id": pid,
            "amount": amt,
            "type": parsed["type"],
            "category": parsed["category"],
            "raw_text": message.text
        }).execute()
        
        await message.answer(f"✅ Записано: **{amt} ₽** ({parsed['category']})", reply_markup=get_finance_keyboard())
        await state.set_state(ModeStates.finance_menu)
    except Exception as e:
        await message.answer("Ошибка распознавания.")

# ==========================================
# ОБРАБОТКА ГОЛОСА (АДАПТИВНАЯ)
# ==========================================
@dp.message(F.voice)
async def handle_voice_global(message: Message, state: FSMContext):
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    local_path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file.file_path, local_path)
    
    try:
        with open(local_path, "rb") as f:
            trans = groq_client.audio.transcriptions.create(file=(local_path, f.read()), model="whisper-large-v3", language="ru")
        text = trans.text
        
        current_state = await state.get_state()
        if current_state == ModeStates.translator:
            message.text = text
            await handle_translator_text(message)
        elif current_state == ModeStates.waiting_for_tx:
            message.text = text
            await process_tx_text(message, state)
        else:
            # Общение с ИИ по умолчанию
            reply = safe_llm_completion(f"Ответь на сообщение пользователю: {text}")
            await message.answer(f"🗣 *«{text}»*\n\n🤖 {reply}")
    finally:
        if os.path.exists(local_path): os.remove(local_path)

# ОБРАБОТКА ТЕКСТА ПО УМОЛЧАНИЮ (ОБЩЕНИЕ С ИИ)
@dp.message(F.text & ~F.text.startswith("/"))
async def default_ai_chat(message: Message):
    try:
        reply = safe_llm_completion(f"Ты — дружелюбный ассистент. Ответь пользователю: {message.text}")
        await message.answer(reply)
    except Exception as e:
        await message.answer("Извините, не удалось обработать запрос.")

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
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())