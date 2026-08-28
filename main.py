import os
import sys
import json
import re
import uuid
import logging
import threading
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

logging.basicConfig(level=logging.INFO)

# Загрузка переменных окружения (один раз)
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Проверка наличия хотя бы одного ИИ-провайдера
if not gemini_client and not groq_client:
    logging.error("Не задан ни один API-ключ для ИИ (GEMINI_API_KEY или GROQ_API_KEY)!")
    sys.exit(1)

CASCADE_MODELS = [
    {"provider": "gemini", "name": "gemini-2.0-flash"},
    {"provider": "groq",   "name": "llama-3.3-70b-versatile"},
    {"provider": "gemini", "name": "gemini-1.5-flash"},
    {"provider": "groq",   "name": "llama-3.1-8b-instant"},
    {"provider": "groq",   "name": "gemma2-9b-it"}
]

def safe_llm_completion(prompt: str) -> str:
    for model_info in CASCADE_MODELS:
        provider = model_info["provider"]
        model_name = model_info["name"]
        try:
            # Блок работы с Groq
            if provider == "groq" and groq_client:
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2
                )
                if response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content.strip()

            # Блок работы с Gemini
            elif provider == "gemini" and gemini_client:
                response = gemini_client.models.generate_content(
                    model=model_name, 
                    contents=prompt
                )
                if response.text:
                    return response.text.strip()

        except Exception as e:
            logging.warning(f"Ошибка модели [{provider}:{model_name}] -> {e}")
            continue

    raise Exception("Ни одна из ИИ-моделей не ответила.")

async def process_transaction_text(text: str, message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    
    prompt = (
        f'Разбери финансовую запись: "{text}". '
        f'Ответь ИСКЛЮЧИТЕЛЬНО валидным JSON без разметки markdown и без слова json. '
        f'Формат: {{"amount": 500, "type": "expense", "category": "Напитки"}}'
    )
    
    try:
        raw = safe_llm_completion(prompt)
        
        # Очистка текста от возможных блоков кода ```json ... ```
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        
        parsed = json.loads(raw)
        amt = float(parsed["amount"])
        tx_type = parsed.get("type", "expense")
        cat = parsed.get("category", "Разное")
        
        # Сохраняем транзакцию
        supabase.table("transactions").insert({
            "telegram_id": int(message.from_user.id),
            "profile_id": pid,
            "amount": amt,
            "type": tx_type,
            "category": cat,
            "raw_text": text
        }).execute()
        
        # Обновляем баланс текущего профиля
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            new_bal = current_bal - amt if tx_type == "expense" else current_bal + amt
            supabase.table("users").update({"balance": new_bal}).eq("id", pid).execute()
        
        await message.answer(f"✅ Записано: {amt:.2f} ₽ ({cat})", reply_markup=get_finance_keyboard())
        await state.set_state(ModeStates.finance_menu)
        
    except Exception as e:
        logging.error(f"Ошибка парсинга транзакции: {e}")
        await message.answer("Ошибка распознавания записи. Попробуйте ввести в формате: «Название Сумма» (например, Кофе 250»).")
            
    raise Exception("Ни одна из ИИ-моделей не ответила.")

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

class ModeStates(StatesGroup):
    translator = State()
    finance_menu = State()
    waiting_for_name = State()
    waiting_for_balance = State()
    waiting_for_budget = State()
    waiting_for_tx = State()
    selecting_profile = State()

async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Главное меню / Общение с ИИ"),
        BotCommand(command="translator", description="🌐 Режим Переводчика"),
        BotCommand(command="finance", description="📊 Финансовый аудитор"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

@dp.message(Command("start"))
@dp.message(F.text.contains("Общение с ИИ"))
@dp.message(F.text == "🏠 Главное меню (Общение с ИИ)")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💬 Режим свободного общения с ИИ (По умолчанию)\n\n"
        "Задавайте любые вопросы текстом или голосом!\n"
        "Для переключения режимов используйте меню ниже.",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("translator"))
@dp.message(F.text.contains("Переводчик"))
async def start_translator(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ModeStates.translator)
    await message.answer(
        "🌐 Режим Переводчика активирован!\n\n"
        "Отправляйте текст или голосовые сообщения — я переведу их на русский (или наоборот).\n\n"
        "Для выхода нажмите «🏠 Главное меню (Общение с ИИ)».",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")]],
            resize_keyboard=True
        )
    )

@dp.message(Command("finance"))
@dp.message(F.text.contains("Финансовый аудитор"))
@dp.message(F.text == "📊 Назад в аудитор")
async def start_finance(message: Message, state: FSMContext):
    # Сначала сохраняем нужные данные из состояния до сброса
    data = await state.get_data()
    current_pid = data.get("profile_id")
    
    await state.clear()
    user_id = int(message.from_user.id)
    
    try:
        res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
        if not res.data:
            await message.answer("👋 Укажите Имя владельца профиля (например: Иван):", reply_markup=ReplyKeyboardRemove())
            await state.set_state(ModeStates.waiting_for_name)
        else:
            prof = res.data[0]
            if current_pid:
                found = next((p for p in res.data if p.get("id") == current_pid), None)
                if found: prof = found
                
            pid = prof.get("id") or prof.get("telegram_id")
            await state.update_data(profile_id=pid)
            await state.set_state(ModeStates.finance_menu)
            
            await message.answer(
                f"📊 Финансовый аудитор (Профиль: {prof.get('name', 'Основной')})\n"
                f"💰 Баланс: {float(prof.get('balance', 0)):.2f} ₽ | План: {float(prof.get('monthly_budget', 0)):.2f} ₽\n\n"
                f"Выберите действие:",
                reply_markup=get_finance_keyboard()
            )
    except Exception as e:
        logging.error(f"Ошибка при входе в финансы: {e}")
        await message.answer("Ошибка при подключении к базе данных.")

@dp.message(ModeStates.waiting_for_name, F.text & ~F.text.startswith("/"))
async def process_name(message: Message, state: FSMContext):
    await state.update_data(new_name=message.text.strip())
    await message.answer("Укажите текущий наличный баланс (числом):")
    await state.set_state(ModeStates.waiting_for_balance)

@dp.message(ModeStates.waiting_for_balance, F.text & ~F.text.startswith("/"))
async def process_balance(message: Message, state: FSMContext):
    try:
        bal = float(message.text.replace(",", "."))
        await state.update_data(new_balance=bal)
        await message.answer("Укажите план расходов на месяц (бюджет):")
        await state.set_state(ModeStates.waiting_for_budget)
    except ValueError:
        await message.answer("Пожалуйста, введите число (например, 10000).")

@dp.message(ModeStates.waiting_for_budget, F.text & ~F.text.startswith("/"))
async def process_budget(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(",", "."))
        data = await state.get_data()
        user_id = int(message.from_user.id)
        
        name = data.get("new_name", "Основной")
        balance = float(data.get("new_balance", 0.0))
        
        res = supabase.table("users").insert({
            "telegram_id": user_id,
            "name": name,
            "balance": balance,
            "monthly_budget": budget
        }).execute()
        
        if res.data:
            profile_id = res.data[0].get("id") or res.data[0].get("telegram_id")
            await state.update_data(profile_id=profile_id)
            await state.set_state(ModeStates.finance_menu)
            await message.answer(f"🎉 Профиль «{name}» успешно создан!", reply_markup=get_finance_keyboard())
        else:
            raise Exception("База вернула пустой результат")
            
    except ValueError:
        await message.answer("Введите числовое значение для бюджета.")
    except Exception as e:
        logging.error(f"Критическая ошибка создания профиля: {e}")
        await message.answer("Произошла ошибка при сохранении профиля в БД. Попробуйте еще раз с помощью /finance")
        await state.clear()

@dp.message(ModeStates.finance_menu, F.text == "👤 Управление профилями")
async def manage_profiles(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    text = "📂 Ваши профили:\n\n"
    buttons = []
    for p in res.data:
        text += f"• {p.get('name', 'Профиль')} (Баланс: {p.get('balance', 0)} ₽)\n"
        buttons.append([KeyboardButton(text=f"Выбрать: {p.get('name')}")])
        
    buttons.append([KeyboardButton(text="➕ Добавить новый профиль")])
    buttons.append([KeyboardButton(text="🗑 Удалить текущий профиль")])
    buttons.append([KeyboardButton(text="📊 Назад в аудитор")])
    
    await state.set_state(ModeStates.selecting_profile)
    await message.answer(text, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

@dp.message(ModeStates.selecting_profile, F.text.startswith("Выбрать: "))
async def select_profile(message: Message, state: FSMContext):
    name = message.text.replace("Выбрать: ", "").strip()
    user_id = int(message.from_user.id)
    res = supabase.table("users").select("*").eq("telegram_id", user_id).eq("name", name).execute()
    
    if res.data:
        pid = res.data[0].get("id") or res.data[0].get("telegram_id")
        await state.update_data(profile_id=pid)
        await message.answer(f"✅ Переключено на профиль: {name}")
    await start_finance(message, state)

@dp.message(ModeStates.selecting_profile, F.text == "➕ Добавить новый профиль")
async def add_new_profile_start(message: Message, state: FSMContext):
    await message.answer("Введите Имя для нового профиля:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ModeStates.waiting_for_name)

@dp.message(ModeStates.selecting_profile, F.text == "🗑 Удалить текущий профиль")
async def delete_profile(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    if pid:
        supabase.table("users").delete().eq("id", pid).execute()
        await message.answer("🗑 Профиль удален!")
    await start_finance(message, state)

@dp.message(ModeStates.finance_menu, F.text == "📈 Статистика")
async def finance_stats(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    res = supabase.table("transactions").select("*").eq("profile_id", pid).execute()
    
    if not res.data:
        await message.answer("Нет транзакций.")
        return
        
    total_exp = sum(float(t["amount"]) for t in res.data if t.get("type") == "expense")
    cats = {}
    for t in res.data:
        if t.get("type") == "expense":
            c = t.get("category", "Разное")
            cats[c] = cats.get(c, 0) + float(t.get("amount", 0))
            
    cat_str = "\n".join([f"• {k}: {v:.2f} ₽" for k, v in cats.items()])
    await message.answer(f"📈 Расходы по категориям:\n\n{cat_str}\n\n🔴 Всего: {total_exp:.2f} ₽")

@dp.message(ModeStates.finance_menu, F.text == "🧠 AI-Аудит")
async def finance_audit(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    
    prof_res = supabase.table("users").select("*").eq("id", pid).execute()
    if not prof_res.data:
        await message.answer("Профиль не найден.")
        return
    prof = prof_res.data[0]
    txs = supabase.table("transactions").select("*").eq("profile_id", pid).limit(30).execute().data
    
    prompt = f"Сделай краткий финансовый аудит для {prof.get('name')}. Бюджет: {prof.get('monthly_budget')} ₽, Баланс: {prof.get('balance')} ₽. Транзакции: {txs}"
    try:
        reply = safe_llm_completion(prompt)
        await message.answer(f"💡 AI-Аудит:\n\n{reply}")
    except Exception as e:
        await message.answer("Не удалось сгенерировать аудит.")

@dp.message(ModeStates.finance_menu, F.text == "➕ Внести расход/доход")
async def add_tx_prompt(message: Message, state: FSMContext):
    await state.set_state(ModeStates.waiting_for_tx)
    await message.answer("Отправьте текст или голосовое (например: «Обед 300» или «Зарплата 50000»):")

async def process_translation(text: str, message: Message):
    prompt = f"""
    Ты — профессиональный переводчик.
    Правила:
    1. Если пользователь явно попросил перевести на конкретный язык (например, "переведи на японский..."), переведи на этот язык.
    2. Если текст на ЛЮБОМ иностранном языке — переведи его НА РУССКИЙ ЯЗЫК.
    3. Если текст на русском языке — переведи его НА АНГЛИЙСКИЙ ЯЗЫК.
    
    Верни ТОЛЬКО перевод без вводных слов.
    Текст: {text}
    """
    try:
        translated = safe_llm_completion(prompt)
        await message.answer(f"🔀 Перевод:\n\n{translated}")
    except Exception:
        await message.answer("Ошибка сервиса перевода.")

async def process_transaction_text(text: str, message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    prompt = f'Разбери запись: "{text}". Верни ТОЛЬКО валидный JSON, без markdown-оберток и пояснений формата: {{"amount": 100, "type": "expense", "category": "Еда"}}'
    try:
        raw = safe_llm_completion(prompt)
        
        # Надежный парсинг JSON с помощью регулярного выражения
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError("Не удалось найти JSON в ответе ИИ")
            
        parsed = json.loads(match.group(0))
        
        amt = float(parsed["amount"])
        supabase.table("transactions").insert({
            "telegram_id": int(message.from_user.id),
            "profile_id": pid,
            "amount": amt,
            "type": parsed.get("type", "expense"),
            "category": parsed.get("category", "Разное"),
            "raw_text": text
        }).execute()
        
        await message.answer(f"✅ Записано: {amt} ₽ ({parsed.get('category')})", reply_markup=get_finance_keyboard())
        await state.set_state(ModeStates.finance_menu)
    except Exception as e:
        logging.error(f"Ошибка парсинга транзакции: {e}")
        await message.answer("Ошибка распознавания записи. Попробуйте еще раз.")

@dp.message(ModeStates.translator, F.text & ~F.text.startswith("/"))
async def handle_translator_text(message: Message):
    await process_translation(message.text, message)

@dp.message(ModeStates.waiting_for_tx, F.text & ~F.text.startswith("/"))
async def handle_tx_text(message: Message, state: FSMContext):
    await process_transaction_text(message.text, message, state)

@dp.message(F.voice)
async def handle_voice_global(message: Message, state: FSMContext):
    if not groq_client:
        await message.answer("Распознавание речи временно недоступно.")
        return

    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    
    # Генерация уникального имени для временного файла
    unique_filename = f"voice_{uuid.uuid4().hex}.ogg"
    
    try:
        await bot.download_file(file.file_path, unique_filename)
        
        with open(unique_filename, "rb") as f:
            trans = groq_client.audio.transcriptions.create(
                file=(unique_filename, f.read()),
                model="whisper-large-v3",
                language="ru"
            )
        text = trans.text
        current_state = await state.get_state()
        
        if current_state == ModeStates.translator:
            await process_translation(text, message)
        elif current_state == ModeStates.waiting_for_tx:
            await process_transaction_text(text, message, state)
        else:
            reply = safe_llm_completion(f"Ответь пользователю: {text}")
            await message.answer(f"🗣 «{text}»\n\n🤖 {reply}")
    except Exception as e:
        logging.error(f"Ошибка Whisper: {e}")
        await message.answer("Ошибка распознавания голоса.")
    finally:
        if os.path.exists(unique_filename):
            os.remove(unique_filename)

@dp.message(F.text & ~F.text.startswith("/"))
async def default_ai_chat(message: Message):
    try:
        reply = safe_llm_completion(message.text)
        await message.answer(reply)
    except Exception as e:
        logging.error(f"Ошибка главного ИИ: {e}")
        await message.answer("Ошибка генерации ответа.")

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
    logging.info("Бот обновлен и запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())