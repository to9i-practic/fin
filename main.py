import os
import sys
import json
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

# Логирование
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
    logging.error("TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Инициализация клиентов ИИ
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

CASCADE_MODELS = [
    {"provider": "groq", "name": "openai/gpt-oss-20b"},
    {"provider": "groq", "name": "openai/gpt-oss-120b"},
    {"provider": "groq", "name": "qwen/qwen3.6-27b"},
    {"provider": "gemini", "name": "gemini-2.0-flash"}, 
    {"provider": "gemini", "name": "gemini-1.5-flash"}
]

def safe_llm_completion(prompt: str) -> str:
    errors = []
    for model_info in CASCADE_MODELS:
        provider = model_info["provider"]
        model_name = model_info["name"]
        try:
            if provider == "gemini":
                if not gemini_client:
                    continue
                response = gemini_client.models.generate_content(
                    model=model_name, 
                    contents=prompt
                )
                if response.text:
                    return response.text.strip()

            elif provider == "groq":
                if not groq_client:
                    continue
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    timeout=8
                )
                if response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content.strip()

        except Exception as e:
            err_details = f"[{provider}:{model_name}] Ошибка -> {e}"
            logging.warning(f"Каскад ИИ: {err_details}")
            errors.append(err_details)
            continue

    logging.error(f"❌ Все модели в каскаде недоступны: {errors}")
    raise Exception("Ни одна из ИИ-моделей не ответила.")

# ==========================================
# 2. СОСТОЯНИЯ FSM И КЛАВИАТУРЫ
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
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Общение с ИИ (По умолчанию)")],
            [KeyboardButton(text="📊 Финансовый контроль")]
        ],
        resize_keyboard=True
    )

def get_finance_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Внести расход/доход"), KeyboardButton(text="📈 Статистика")],
            [KeyboardButton(text="🧠 AI-Анализ"), KeyboardButton(text="⚙️ Корректировка пользователя")],
            [KeyboardButton(text="👥 Сменить профиль"), KeyboardButton(text="🏠 Главное меню (Общение с ИИ)")]
        ],
        resize_keyboard=True
    )

def get_edit_profile_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить имя"), KeyboardButton(text="💰 Изменить баланс")],
            [KeyboardButton(text="🎯 Изменить план на месяц")],
            [KeyboardButton(text="🗑 Удалить профиль")],
            [KeyboardButton(text="📊 Назад в контроль")]
        ],
        resize_keyboard=True
    )

async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Главное меню / Общение с ИИ"),
        BotCommand(command="finance", description="📊 Финансовый контроль"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

# ==========================================
# 3. ОСНОВНЫЕ ХЭНДЛЕРЫ
# ==========================================
@dp.message(Command("start"))
@dp.message(F.text.contains("Общение с ИИ"))
@dp.message(F.text == "🏠 Главное меню (Общение с ИИ)")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💬 **Режим свободного общения с ИИ (По умолчанию)**\n\n"
        "Задавайте любые вопросы текстом или голосом!\n"
        "Для управления бюджетом используйте меню «Финансовый контроль».",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# ==========================================
# 4. ФИНАНСОВЫЙ КОНТРОЛЬ И ПРОФИЛИ
# ==========================================
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
            await message.answer("👋 У вас пока нет профилей. Введите имя нового профиля (например: Антон):", reply_markup=ReplyKeyboardRemove())
            return
            
        data = await state.get_data()
        current_pid = data.get("profile_id")
        
        if current_pid and message.text != "👥 Сменить профиль":
            prof = next((p for p in res.data if p.get("id") == current_pid), res.data[0])
            await state.set_state(ModeStates.finance_menu)
            await message.answer(
                f"📊 **Финансовый контроль** (Профиль: {prof.get('name')})\n"
                f"💰 Баланс: {float(prof.get('balance', 0)):.2f} ₽ | План: {float(prof.get('monthly_budget', 0)):.2f} ₽\n\n"
                f"Выберите действие:",
                reply_markup=get_finance_keyboard(),
                parse_mode="Markdown"
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

# --- СОЗДАНИЕ ПРОФИЛЯ ---
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
            profile_id = res.data[0].get("id")
            await state.update_data(profile_id=profile_id)
            await message.answer(f"🎉 Профиль «{name}» успешно создан!")
            await start_finance(message, state)
        else:
            raise Exception("Пустой ответ от Supabase")
            
    except ValueError:
        await message.answer("Введите числовое значение для бюджета.")
    except Exception as e:
        logging.error(f"Ошибка создания профиля: {e}")
        await message.answer("Ошибка при сохранении профиля в БД.")
        await state.clear()

# ==========================================
# 5. КОРРЕКТИРОВКА ПОЛЬЗОВАТЕЛЯ
# ==========================================
@dp.message(ModeStates.finance_menu, F.text == "⚙️ Корректировка пользователя")
async def open_edit_profile(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_profile_menu)
    await message.answer("⚙️ **Настройки профиля**\nВыберите параметр для изменения:", reply_markup=get_edit_profile_keyboard(), parse_mode="Markdown")

@dp.message(ModeStates.edit_profile_menu, F.text == "✏️ Изменить имя")
async def edit_name_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_name)
    await message.answer("Введите новое имя профиля:", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_name, F.text)
async def edit_name_save(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    supabase.table("users").update({"name": message.text.strip()}).eq("id", pid).execute()
    await message.answer("✅ Имя успешно изменено!")
    await start_finance(message, state)

@dp.message(ModeStates.edit_profile_menu, F.text == "💰 Изменить баланс")
async def edit_balance_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_balance)
    await message.answer("Введите новый текущий баланс (числом):", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_balance, F.text)
async def edit_balance_save(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        data = await state.get_data()
        pid = data.get("profile_id")
        supabase.table("users").update({"balance": val}).eq("id", pid).execute()
        await message.answer("✅ Баланс успешно обновлен!")
        await start_finance(message, state)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")

@dp.message(ModeStates.edit_profile_menu, F.text == "🎯 Изменить план на месяц")
async def edit_budget_start(message: Message, state: FSMContext):
    await state.set_state(ModeStates.edit_budget)
    await message.answer("Введите новый месячный план/бюджет (числом):", reply_markup=ReplyKeyboardRemove())

@dp.message(ModeStates.edit_budget, F.text)
async def edit_budget_save(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        data = await state.get_data()
        pid = data.get("profile_id")
        supabase.table("users").update({"monthly_budget": val}).eq("id", pid).execute()
        await message.answer("✅ Месячный план успешно обновлен!")
        await start_finance(message, state)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")

@dp.message(ModeStates.edit_profile_menu, F.text == "🗑 Удалить профиль")
async def delete_profile_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    if pid:
        supabase.table("users").delete().eq("id", pid).execute()
        await state.update_data(profile_id=None)
        await message.answer("🗑 Профиль успешно удален.")
    await start_finance(message, state)

# ==========================================
# 6. ВНЕСЕНИЕ РАСХОДОВ И ТРАНЗАКЦИЙ
# ==========================================
@dp.message(ModeStates.finance_menu, F.text == "➕ Внести расход/доход")
async def add_tx_prompt(message: Message, state: FSMContext):
    await state.set_state(ModeStates.waiting_for_tx)
    await message.answer(
        "📝 **Способы внесения данных:**\n\n"
        "• **Текст:** `Обед 500` или `Такси 300`\n"
        "• **Голос:** Надиктуйте список трат\n"
        "• **Фото:** Отправьте фото чека или рукописного списка на бумаге\n\n"
        "Ожидаю ввод (можно отправлять несколько записей подряд):",
        reply_markup=get_finance_keyboard(),
        parse_mode="Markdown"
    )
@dp.message(ModeStates.waiting_for_tx, F.photo)
@dp.message(ModeStates.finance_menu, F.photo)
async def process_tx_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    
    if not pid:
        await message.answer("⚠️ Сначала выберите профиль в меню «Финансовый контроль».")
        return

    # Скачиваем изображение наивысшего качества
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    local_filename = f"photo_{message.message_id}.jpg"
    await bot.download_file(file_info.file_path, local_filename)

    status_msg = await message.answer("🔍 Распознаю записи на фото...")

    try:
        if not gemini_client:
            await status_msg.edit_text("⚠️ Ошибка: GEMINI_API_KEY не настроен для распознавания фото.")
            return

        # Загружаем фото в Gemini API
        uploaded_file = gemini_client.files.upload(file=local_filename)
        
        prompt = (
            "Проанализируй фото (это может быть чек, список покупок или рукописная запись на бумаге). "
            "Найди все финансовые операции (расходы или доходы). "
            "Верни ответ STRICTLY в формате JSON массива объектов без markdown разметки:\n"
            '[{"amount": 500, "type": "expense", "category": "Еда", "raw_text": "Обед"}, ...]\n'
            "Если ничего не найдено, верни пустой массив []."
        )

        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[uploaded_file, prompt]
        )

        raw = response.text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()

        transactions = json.loads(raw)

        if not transactions:
            await status_msg.edit_text("❌ Не удалось распознать суммовые записи на фото.")
            return

        # Записываем все найденные позиции в Supabase
        total_change = 0.0
        summary_text = "✅ **Записано с фото:**\n"

        for item in transactions:
            amt = float(item.get("amount", 0))
            tx_type = item.get("type", "expense")
            cat = item.get("category", "Разное")
            item_name = item.get("raw_text", "Покупка по фото")

            if amt <= 0:
                continue

            supabase.table("transactions").insert({
                "telegram_id": int(message.from_user.id),
                "profile_id": pid,
                "amount": amt,
                "type": tx_type,
                "category": cat,
                "raw_text": item_name
            }).execute()

            if tx_type == "expense":
                total_change -= amt
                summary_text += f"• 🔴 {item_name}: {amt:.2f} ₽ ({cat})\n"
            else:
                total_change += amt
                summary_text += f"• 🟢 {item_name}: {amt:.2f} ₽ ({cat})\n"

        # Обновляем текущий баланс пользователя
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            supabase.table("users").update({"balance": current_bal + total_change}).eq("id", pid).execute()

        await state.set_state(ModeStates.waiting_for_tx)
        await status_msg.edit_text(summary_text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Ошибка парсинга фото: {e}")
        await status_msg.edit_text(" Ошибка распознавания изображения. Попробуйте сделать более четкое фото.")
    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)


# --- ОБРАБОТКА ТЕКСТА (НЕ СБРАСЫВАЕТ СОСТОЯНИЕ) ---
@dp.message(ModeStates.waiting_for_tx, F.text & ~F.text.startswith("/"))
async def process_tx_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    text = message.text

    if text in ["➕ Внести расход/доход", "📈 Статистика", "🧠 AI-Анализ", 
                "⚙️ Корректировка пользователя", "👥 Сменить профиль", "🏠 Главное меню (Общение с ИИ)"]:
        return

    prompt = (
        f'Разбери запись: "{text}". '
        f'Ответь ТОЛЬКО JSON без markdown. Формат: {{"amount": 500, "type": "expense", "category": "Еда"}}'
    )
    
    try:
        raw = safe_llm_completion(prompt)
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
            
        parsed = json.loads(raw)
        amt = float(parsed["amount"])
        tx_type = parsed.get("type", "expense")
        cat = parsed.get("category", "Разное")
        
        supabase.table("transactions").insert({
            "telegram_id": int(message.from_user.id),
            "profile_id": pid,
            "amount": amt,
            "type": tx_type,
            "category": cat,
            "raw_text": text
        }).execute()
        
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            new_bal = current_bal - amt if tx_type == "expense" else current_bal + amt
            supabase.table("users").update({"balance": new_bal}).eq("id", pid).execute()
        
        # Сохраняем состояние waiting_for_tx для ввода следующих покупок
        await state.set_state(ModeStates.waiting_for_tx)
        await message.answer(f"✅ Записано: **{amt:.2f} ₽** ({cat})", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Ошибка обработки транзакции: {e}")
        await message.answer("Ошибка распознавания. Попробуйте ввести: «Название Сумма» (например, «Ужин 400»).")
            
@dp.message(ModeStates.waiting_for_tx, F.text & ~F.text.startswith("/"))
async def process_tx_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("profile_id")
    text = message.text
    
    prompt = (
        f'Разбери запись: "{text}". '
        f'Ответь ТОЛЬКО JSON без markdown. Формат: {{"amount": 500, "type": "expense", "category": "Еда"}}'
    )
    
    try:
        raw = safe_llm_completion(prompt)
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
            
        parsed = json.loads(raw)
        amt = float(parsed["amount"])
        tx_type = parsed.get("type", "expense")
        cat = parsed.get("category", "Разное")
        
        supabase.table("transactions").insert({
            "telegram_id": int(message.from_user.id),
            "profile_id": pid,
            "amount": amt,
            "type": tx_type,
            "category": cat,
            "raw_text": text
        }).execute()
        
        prof_res = supabase.table("users").select("balance").eq("id", pid).execute()
        if prof_res.data:
            current_bal = float(prof_res.data[0].get("balance", 0))
            new_bal = current_bal - amt if tx_type == "expense" else current_bal + amt
            supabase.table("users").update({"balance": new_bal}).eq("id", pid).execute()
        
        await message.answer(f"✅ Записано: {amt:.2f} ₽ ({cat})", reply_markup=get_finance_keyboard())
        await state.set_state(ModeStates.waiting_for_tx)
        await message.answer(
        f"✅ Записано: {amt:.2f} ₽ ({cat})\n\n"
        "Отправьте следующую запись или выберите действие в меню ниже:", 
        reply_markup=get_finance_keyboard()
        )
    except Exception as e:
        logging.error(f"Ошибка обработки транзакции: {e}")
        await message.answer("Ошибка распознавания. Попробуйте ввести: «Название Сумма» (например, «Пиво 500»).")

@dp.message(F.voice)
async def process_voice(message: Message, state: FSMContext):
    file_id = message.voice.file_id
    file_info = await bot.get_file(file_id)
    local_filename = f"voice_{message.message_id}.ogg"
    await bot.download_file(file_info.file_path, local_filename)

    try:
        if gemini_client:
            uploaded_file = gemini_client.files.upload(file=local_filename)
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[uploaded_file, "Расшифруй и ответь на это голосовое сообщение."]
            )
            await message.answer(response.text if response.text else "Не удалось распознать звук.")
        else:
            await message.answer("Сервис обработки голоса недоступен.")
    except Exception as e:
        logging.error(f"Ошибка голоса: {e}")
        await message.answer("Ошибка при обработке голосового сообщения.")
    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)

@dp.message(F.text & ~F.text.startswith("/"))
async def default_ai_chat(message: Message):
    try:
        reply = safe_llm_completion(message.text)
        await message.answer(reply)
    except Exception as e:
        logging.error(f"Ошибка ИИ-чата: {e}")
        await message.answer("Ошибка генерации ответа.")

# ==========================================
# 8. ЗАПУСК И HEALTH CHECK
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
    logging.info("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())