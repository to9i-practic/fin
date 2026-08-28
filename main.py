import os
import sys
import json
import logging
import threading
import uuid
import base64
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
from google.genai import types
from groq import Groq
from openai import OpenAI

# Логирование
logging.basicConfig(level=logging.INFO)

# Загрузка переменных окружения
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Инициализация клиентов ИИ
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# КАСКАД МОДЕЛЕЙ: OpenAI (Responses API) -> Gemini -> Groq
CASCADE_MODELS = [
    {"provider": "openai", "name": "gpt-5.4-mini", "supports_image": True},
    {"provider": "openai", "name": "gpt-4o-mini", "supports_image": True},
    {"provider": "gemini", "name": "gemini-2.0-flash", "supports_image": True},
    {"provider": "gemini", "name": "gemini-1.5-flash", "supports_image": True},
    {"provider": "groq", "name": "openai/gpt-oss-120b", "supports_image": False},
    {"provider": "groq", "name": "qwen/qwen3.6-27b", "supports_image": False}
]

def safe_llm_completion(prompt: str, image_bytes: bytes = None, mime_type: str = None) -> str:
    """Универсальная функция с каскадным переключением и поддержкой нового OpenAI Responses API."""
    errors = []
    for model_info in CASCADE_MODELS:
        provider = model_info["provider"]
        model_name = model_info["name"]
        supports_image = model_info.get("supports_image", False)

        if image_bytes and not supports_image:
            continue

        try:
            if provider == "openai":
                if not openai_client:
                    continue
                
                # НОВЫЙ RESPONSES API ОТ OPENAI
                if image_bytes:
                    base64_image = base64.b64encode(image_bytes).decode('utf-8')
                    input_data = [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": f"data:{mime_type};base64,{base64_image}"}
                            ]
                        }
                    ]
                else:
                    input_data = [{"role": "user", "content": prompt}]
                
                response = openai_client.responses.create(
                    model=model_name,
                    input=input_data,
                    store=True
                )
                if response.output_text:
                    return response.output_text.strip()

            elif provider == "gemini":
                if not gemini_client:
                    continue
                contents = [prompt]
                if image_bytes:
                    contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
                response = gemini_client.models.generate_content(model=model_name, contents=contents)
                if response.text:
                    return response.text.strip()

            elif provider == "groq":
                if not groq_client:
                    continue
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    timeout=12
                )
                if response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content.strip()

        except Exception as e:
            err_details = f"[{provider}:{model_name}] Ошибка -> {e}"
            logging.warning(f"Каскад ИИ: {err_details}")
            errors.append(err_details)
            continue
            
    logging.error(f"❌ Все модели в каскаде вышли из строя: {errors}")
    raise Exception("Ни одна из ИИ-моделей не доступна.")

async def send_chunked_message(message: Message, text: str, parse_mode: str = "Markdown"):
    """Защита от ошибки Telegram 'message is too long'."""
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        try:
            await message.answer(chunk, parse_mode=parse_mode)
        except Exception:
            await message.answer(chunk)

# ==========================================
# FSM И КЛАВИАТУРЫ
# ==========================================
class ModeStates(StatesGroup):
    finance_menu = State()
    waiting_for_name = State()
    waiting_for_balance = State()
    waiting_for_budget = State()
    waiting_for_tx = State()
    selecting_profile = State()
    edit_profile_menu = State()
    edit_name = State()
    edit_balance = State()
    edit_budget = State()

def get_main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💬 Общение с ИИ (По умолчанию)")],
        [KeyboardButton(text="📊 Финансовый контроль")]
    ], resize_keyboard=True)

def get_finance_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Внести расход/доход"), KeyboardButton(text="📈 Статистика")],
        [KeyboardButton(text="🧠 AI-Анализ"), KeyboardButton(text="⚙️ Корректировка пользователя")],
        [KeyboardButton(text="👥 Сменить профиль"), KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")]
    ], resize_keyboard=True)

def get_edit_profile_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✏️ Изменить имя"), KeyboardButton(text="💰 Изменить баланс")],
        [KeyboardButton(text="🎯 Изменить план на месяц")],
        [KeyboardButton(text="🗑 Удалить профиль")],
        [KeyboardButton(text="📊 Назад в контроль")]
    ], resize_keyboard=True)

async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Главное меню / Общение с ИИ"),
        BotCommand(command="finance", description="📊 Финансовый контроль"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

# ==========================================
# ХЭНДЛЕРЫ
# ==========================================
@dp.message(Command("start"))
@dp.message(F.text.contains("Общение с ИИ"))
@dp.message(F.text == "🏠 Главное меню (Общение с ИИ)")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💬 Режим свободного общения с ИИ (По умолчанию)\n\n"
        "Задавайте любые вопросы текстом или голосом!\n"
        "Для управления бюджетом используйте меню «Финансовый контроль».",
        reply_markup=get_main_keyboard(), parse_mode="Markdown"
    )

@dp.message(Command("finance"))
@dp.message(F.text.contains("Финансовый контроль"))
@dp.message(F.text == "👥 Сменить профиль")
@dp.message(F.text == "📊 Назад в контроль")
async def start_finance(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    try:
        res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
        if not res.data:
            await state.set_state(ModeStates.waiting_for_name)
            await message.answer("👋 У вас пока нет профилей. Введите имя нового профиля:", reply_markup=ReplyKeyboardRemove())
            return
        
        data = await state.get_data()
        current_pid = data.get("profile_id")
        
        if current_pid and message.text != "👥 Сменить профиль":
            prof = next((p for p in res.data if p.get("id") == current_pid), res.data[0])
            await state.set_state(ModeStates.finance_menu)
            await message.answer(
                f"📊 **Финансовый контроль** (Профиль: {prof.get('name')})\n"
                f"💰 Баланс: {float(prof.get('balance', 0)):.2f} ₽ | План: {float(prof.get('monthly_budget', 0)):.2f} ₽\n\nВыберите действие:",
                reply_markup=get_finance_keyboard(), parse_mode="Markdown"
            )
            return
            
        text = "📂 **Выберите профиль для управления:**\n\n"
        buttons = []
        for p in res.data:
            text += f"• **{p.get('name')}** (Баланс: {p.get('balance', 0)} ₽)\n"
            buttons.append([KeyboardButton(text=f"👤 {p.get('name')}")])
        buttons.append([KeyboardButton(text="➕ Создать новый профиль")])
        buttons.append([KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")])
        
        await state.set_state(ModeStates.selecting_profile)
        await message.answer(text, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка входа в финансы: {e}")
        await message.answer("Ошибка при получении профилей из БД.")

@dp.message(ModeStates.selecting_profile, F.text.startswith("👤 "))
async def select_profile_choice(message: Message, state: FSMContext):
    name = message.text.replace("👤 ", "").strip()
    user_id = int(message.from_user.id)
    res = supabase.table("users").select("*").eq("telegram_id", user_id).eq("name", name).execute()
    if res.data:
        pid = res.data[0].get("id")
        await state.update_data(profile_id=pid)
        await message.answer(f"✅ Выбран профиль: **{name}**", parse_mode="Markdown")
    await start_finance(message, state)

@dp.message(ModeStates.selecting_profile, F.text == "➕ Создать новый профиль")
async def add_new_profile_start(message: Message, state: FSMContext):
    await message.answer("Введите Имя для нового профиля:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ModeStates.waiting_for_name)

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
        await message.answer("Пожалуйста, введите число.")

@dp.message(ModeStates.waiting_for_budget, F.text & ~F.text.startswith("/"))
async def process_budget(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(",", "."))
        data = await state.get_data()
        user_id = int(message.from_user.id)
        name = data.get("new_name", "Основной")
        balance = float(data.get("new_balance", 0.0))
        
        res = supabase.table("users").insert({
            "telegram_id": user_id, "name": name, "balance": balance, "monthly_budget": budget
        }).execute()
        
        if res.data:
            profile_id = res.data[0].get("id")
            await state.update_data(profile_id=profile_id)
            await message.answer(f"🎉 Профиль «{name}» успешно создан!")
            await start_finance(message, state)
    except Exception as e:
        logging.error(f"Ошибка создания профиля: {e}")
        await message.answer("Ошибка при сохранении профиля.")
        await state.clear()

@dp.message(ModeStates.finance_menu, F.text == "⚙️ Корректировка пользователя")
async def open_edit_profile(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_profile_menu)
    await message.answer("⚙️ Настройки профиля", reply_markup=get_edit_profile_keyboard(), parse_mode="Markdown")

@dp.message(ModeStates.edit_profile_menu, F.text == "✏️ Изменить имя")
async def edit_name_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_name)
    await message.answer("Введите новое имя:", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_name, F.text)
async def edit_name_save(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    supabase.table("users").update({"name": message.text.strip()}).eq("id", pid).execute()
    await message.answer("✅ Имя изменено!")
    await start_finance(message, state)

@dp.message(ModeStates.edit_profile_menu, F.text == "💰 Изменить баланс")
async def edit_balance_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_balance)
    await message.answer("Введите новый баланс:", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_balance, F.text)
async def edit_balance_save(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        data = await state.get_data()
        pid = data.get("profile_id")
        supabase.table("users").update({"balance": val}).eq("id", pid).execute()
        await message.answer("✅ Баланс обновлен!")
        await start_finance(message, state)
    except ValueError:
        await message.answer("Введите корректное число.")

@dp.message(ModeStates.edit_profile_menu, F.text == "🎯 Изменить план на месяц")
async def edit_budget_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_budget)
    await message.answer("Введите новый бюджет:", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_budget, F.text)
async def edit_budget_save(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        data = await state.get_data()
        pid = data.get("profile_id")
        supabase.table("users").update({"monthly_budget": val}).eq("id", pid).execute()
        await message.answer("✅ План обновлен!")
        await start_finance(message, state)
    except ValueError:
        await message.answer("Введите корректное число.")

@dp.message(ModeStates.edit_profile_menu, F.text == "🗑 Удалить профиль")
async def delete_profile_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    if pid:
        supabase.table("users").delete().eq("id", pid).execute()
        supabase.table("transactions").delete().eq("profile_id", pid).execute()
        await state.update_data(profile_id=None)
        await message.answer("🗑 Профиль удален.")
        await start_finance(message, state)

@dp.message(ModeStates.finance_menu, F.text == "➕ Внести расход/доход")
async def add_tx_prompt(message: Message, state: FSMContext):
    await state.set_state(ModeStates.waiting_for_tx)
    await message.answer(
        "📝 Отправьте текст, голос или фото чека.\nПример текста: `Обед 500`",
        reply_markup=get_finance_keyboard(), parse_mode="Markdown"
    )

# === ОБЛАЧНОЕ РАСПОЗНАВАНИЕ ФОТО (БЕЗ НАГРУЗКИ НА ПАМЯТЬ) ===
@dp.message(ModeStates.waiting_for_tx, F.photo)
@dp.message(ModeStates.finance_menu, F.photo)
async def process_tx_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    if not pid:
        await message.answer("⚠️ Сначала выберите профиль.")
        return
        
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    local_filename = f"photo_{message.message_id}.jpg"
    
    try:
        await bot.download_file(file_info.file_path, local_filename)
        status_msg = await message.answer("🔍 Распознаю чек через облачный ИИ...")
        
        with open(local_filename, "rb") as f:
            image_bytes = f.read()
            
        prompt = (
            "Проанализируй фото чека. Найди все финансовые операции. "
            "Верни СТРОГО JSON массив без markdown: "
            '[{"amount": 500, "type": "expense", "category": "Еда", "raw_text": "Обед"}]'
        )
        
        raw = safe_llm_completion(prompt, image_bytes=image_bytes, mime_type="image/jpeg")
        
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
            
        transactions = json.loads(raw)
        if not transactions:
            await status_msg.edit_text("❌ Не удалось найти операции на фото.")
            return
            
        total_change = 0.0
        summary_text = "✅ **Записано с фото:**\n"
        
        for item in transactions:
            amt = float(item.get("amount", 0))
            tx_type = item.get("type", "expense")
            cat = item.get("category", "Разное")
            item_name = item.get("raw_text", "Покупка")
            if amt <= 0: continue
                
            supabase.table("transactions").insert({
                "telegram_id": int(message.from_user.id), "profile_id": pid,
                "amount": amt, "type": tx_type, "category": cat, "raw_text": item_name
            }).execute()
            
            if tx_type == "expense":
                total_change -= amt
                summary_text += f"• 🔴 {item_name}: {amt:.2f} ₽ ({cat})\n"
            else:
                total_change += amt
                summary_text += f"• 🟢 {item_name}: {amt:.2f} ₽ ({cat})\n"
                
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            supabase.table("users").update({"balance": current_bal + total_change}).eq("id", pid).execute()
            
        await state.set_state(ModeStates.waiting_for_tx)
        await status_msg.edit_text(summary_text, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Ошибка парсинга фото: {e}")
        await message.answer("⚠️ Ошибка распознавания фото. Попробуйте четче.")
    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)

@dp.message(ModeStates.waiting_for_tx, F.text & ~F.text.startswith("/"))
async def process_tx_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    text = message.text
    
    if text in ["➕ Внести расход/доход", "📈 Статистика", "🧠 AI-Анализ", 
                "⚙️ Корректировка пользователя", "👥 Сменить профиль", "🏠 Главное меню (Общение с ИИ)"]:
        return
        
    prompt = f'Разбери запись: "{text}". Ответь ТОЛЬКО JSON: {{"amount": 500, "type": "expense", "category": "Еда"}}'
    try:
        raw = safe_llm_completion(prompt)
        if "```" in raw: raw = raw.split("```")[1].replace("json", "").strip()
        parsed = json.loads(raw)
        amt = float(parsed["amount"])
        tx_type = parsed.get("type", "expense")
        cat = parsed.get("category", "Разное")
        
        supabase.table("transactions").insert({
            "telegram_id": int(message.from_user.id), "profile_id": pid,
            "amount": amt, "type": tx_type, "category": cat, "raw_text": text
        }).execute()
        
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            new_bal = current_bal - amt if tx_type == "expense" else current_bal + amt
            supabase.table("users").update({"balance": new_bal}).eq("id", pid).execute()
            
        await state.set_state(ModeStates.waiting_for_tx)
        await message.answer(f"✅ Записано: **{amt:.2f} ₽** ({cat})", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка транзакции: {e}")
        await message.answer("Ошибка. Формат: «Название Сумма» (например, Ужин 400).")

@dp.message(F.voice)
async def process_voice(message: Message, state: FSMContext):
    if not groq_client:
        await message.answer("⚠️ Голосовой ввод недоступен.")
        return
    file_id = message.voice.file_id
    file_info = await bot.get_file(file_id)
    local_filename = f"voice_{uuid.uuid4().hex}.ogg"
    try:
        await bot.download_file(file_info.file_path, local_filename)
        with open(local_filename, "rb") as f:
            trans = groq_client.audio.transcriptions.create(file=(local_filename, f.read()), model="whisper-large-v3", language="ru")
        text = trans.text.strip()
        
        current_state = await state.get_state()
        if current_state == ModeStates.waiting_for_tx:
            msg_copy = type('obj', (object,), {'text': text, 'from_user': message.from_user, 'answer': message.answer})
            await process_tx_text(msg_copy, state)
        else:
            reply = safe_llm_completion(f"Ответь пользователю: {text}")
            await send_chunked_message(message, f"🗣 «{text}»\n\n🤖 {reply}")
    except Exception as e:
        logging.error(f"Ошибка голоса: {e}")
        await message.answer("Ошибка распознавания голоса.")
    finally:
        if os.path.exists(local_filename): os.remove(local_filename)

@dp.message(ModeStates.finance_menu, F.text == "🧠 AI-Анализ")
async def finance_audit(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    prof_res = supabase.table("users").select("*").eq("id", pid).execute()
    if not prof_res.data: return
    prof = prof_res.data[0]
    txs = supabase.table("transactions").select("*").eq("profile_id", pid).limit(20).execute().data
    prompt = f"Сделай краткий финансовый аудит. Бюджет: {prof.get('monthly_budget')} ₽, Баланс: {prof.get('balance')} ₽. Транзакции: {txs}"
    try:
        reply = safe_llm_completion(prompt)
        await send_chunked_message(message, f"💡 AI-Аудит:\n\n{reply}")
    except Exception:
        await message.answer("Не удалось сгенерировать аудит.")

@dp.message(F.text & ~F.text.startswith("/"))
async def default_ai_chat(message: Message):
    try:
        reply = safe_llm_completion(message.text)
        await send_chunked_message(message, reply)
    except Exception as e:
        logging.error(f"Ошибка ИИ-чата: {e}")
        await message.answer("Ошибка генерации ответа.")

# ==========================================
# ЗАПУСК
# ==========================================
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
    logging.info("Бот оптимизирован (без OCR) и запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())