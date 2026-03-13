import asyncio
import json
import os
import uuid
import random
import aiohttp
import logging

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from typing import Callable, Dict, Any, Awaitable

# Настройки
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "config.env"))

TOKEN = os.getenv("aiogramBot_TOKEN")
COMFY_URL = "http://127.0.0.1:8000"

logging.basicConfig(level=logging.INFO)

def load_allowed_users() -> list[int]:
    raw = os.getenv("ALLOWED_USERS", "")
    return [int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()]

def is_allowed(user_id: int) -> bool:
    return user_id in load_allowed_users()

def is_admin(user_id: int) -> bool:
    users = load_allowed_users()
    return len(users) > 0 and user_id == users[0]

# ─────────────────────────────────────────────
# Middleware — проверка доступа
# ─────────────────────────────────────────────

class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return

        if not is_allowed(user.id):
            if isinstance(event, Message):
                await event.answer(
                    "⛔ У вас нет доступа к этому боту.\n"
                    f"Ваш ID: <code>{user.id}</code>\n\n"
                    "Обратитесь к владельцу бота.",
                    parse_mode="HTML"
                )
            return

        return await handler(event, data)

bot = Bot(token=TOKEN)
dp = Dispatcher()

dp.message.middleware(AuthMiddleware())
dp.callback_query.middleware(AuthMiddleware())

class Generation(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_count = State()

# Клавиатуры

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎨 Генерировать изображение")],
        [KeyboardButton(text="ℹ️ Информация")]
    ],
    resize_keyboard=True
)

cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ]
)

count_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣", callback_data="count_1"),
            InlineKeyboardButton(text="2️⃣", callback_data="count_2"),
            InlineKeyboardButton(text="4️⃣", callback_data="count_4"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ]
)


# Прогресс-бар
def make_progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = int(100 * current / total) if total > 0 else 0
    return f"[{bar}] {percent}%"

STAGES = [
    (0,  10, "🔄 Подготовка..."),
    (10, 40, "🎨 Сэмплинг..."),
    (40, 70, "✨ Апскейл..."),
    (70, 90, "👁 Детейлинг..."),
    (90, 99, "💾 Сохранение..."),
]

# ComfyUI — генерация изображения
async def generate_image(prompt: str, progress_callback, seed: int = None) -> str:
    with open(os.path.join(BASE_DIR, "workflow.json"), "r", encoding="utf-8") as f:
        workflow = json.load(f)

    # Подставляем промпт в нод 38 (позитивный промпт)
    workflow["38"]["inputs"]["text"] = prompt

    new_seed = seed if seed else random.randint(1, 999999999999999)

    # Нод 46 — Seed Generator
    workflow["46"]["inputs"]["seed"] = new_seed

    # Нод 31 — KSampler
    workflow["31"]["inputs"]["seed"] = new_seed

    # Нод 45 — UltimateSDUpscale  
    workflow["45"]["inputs"]["seed"] = new_seed

    # Детейлеры
    workflow["55:82"]["inputs"]["seed"] = new_seed
    workflow["53:71"]["inputs"]["seed"] = new_seed

    client_id = str(uuid.uuid4())

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{COMFY_URL}/prompt",
            json={"prompt": workflow, "client_id": client_id}
    ) as resp:
            data = await resp.json()
            logging.info(f"ComfyUI response: {data}")  # <-- добавь это
            if "prompt_id" not in data:
                raise ValueError(f"ComfyUI вернул ошибку: {data}")
            prompt_id = data["prompt_id"]

        logging.info(f"Задача отправлена, prompt_id: {prompt_id}")

        stage_index = 0
        fake_progress = 0

        while True:
            async with session.get(f"{COMFY_URL}/history/{prompt_id}") as resp:
                history = await resp.json()
                if prompt_id in history:
                    await progress_callback(100, 100, "✅ Готово!")
                    break

            if stage_index < len(STAGES):
                start, end, name = STAGES[stage_index]
                fake_progress = min(fake_progress + 3, end)
                await progress_callback(fake_progress, 100, name)
                if fake_progress >= end:
                    stage_index += 1

            await asyncio.sleep(3)

        outputs = history[prompt_id]["outputs"]
        logging.info(f"Outputs: {json.dumps(outputs, ensure_ascii=False)}")
        if "43" not in outputs:
            status = history[prompt_id].get("status", {})
            raise ValueError(f"Нода 43 не выполнилась. Статус: {status}")
        images = outputs["43"]["images"]
        filename = images[0]["filename"]
        subfolder = images[0]["subfolder"]

        async with session.get(
            f"{COMFY_URL}/view?filename={filename}&subfolder={subfolder}&type=output"
        ) as resp:
            image_data = await resp.read()

    temp_path = os.path.join(BASE_DIR, f"temp_{prompt_id}.png")
    with open(temp_path, "wb") as f:
        f.write(image_data)

    return temp_path

#Хэндлеры — команды и кнопки
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n\n"
        f"Я генерирую изображения через ComfyUI 🎨\n"
        f"Твой ID: <code>{message.from_user.id}</code>\n\n"
        f"Нажми кнопку чтобы начать!",
        parse_mode="HTML",
        reply_markup=main_kb
    )

@dp.message(F.text == "ℹ️ Информация")
async def btn_info(message: Message):
    users = load_allowed_users()
    users_list = "\n".join([
        f"• <code>{uid}</code>{'  👑 админ' if i == 0 else ''}"
        for i, uid in enumerate(users)
    ])
    await message.answer(
        "🤖 <b>ComfyUI Bot</b>\n\n"
        "Генерирует изображения через ComfyUI на локальном ПК.\n\n"
        "<b>Как использовать:</b>\n"
        "1. Нажми 🎨 Генерировать изображение\n"
        "2. Введи промпт на английском\n"
        "3. Выбери количество изображений\n"
        "4. Жди результата!\n\n"
        "<b>Советы для промпта:</b>\n"
        "• Пиши на английском\n"
        "• Добавляй стиль: <i>anime style, detailed, high quality</i>\n"
        "• Описывай сцену подробно\n\n"
        f"<b>Пользователи с доступом:</b>\n{users_list}",
        parse_mode="HTML"
    )

# Хэндлеры — генерация
@dp.message(F.text == "🎨 Генерировать изображение")
async def btn_generate(message: Message, state: FSMContext):
    await message.answer(
        "✏️ Введи промпт для генерации:\n\n"
        "<i>Пример: anime girl, sunset, detailed, high quality</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb
    )
    await state.set_state(Generation.waiting_for_prompt)

@dp.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()

# Шаг 1 — получаем промпт, спрашиваем количество
@dp.message(Generation.waiting_for_prompt)
async def get_prompt(message: Message, state: FSMContext):
    await state.update_data(prompt=message.text)
    await message.answer(
        "Сколько изображений сгенерировать?",
        reply_markup=count_kb
    )
    await state.set_state(Generation.waiting_for_count)

# Шаг 2 — получаем количество, запускаем генерацию
@dp.callback_query(Generation.waiting_for_count, F.data.startswith("count_"))
async def get_count(callback: CallbackQuery, state: FSMContext):
    count = int(callback.data.split("_")[1])
    data = await state.get_data()
    prompt = data["prompt"]
    await state.clear()

    await callback.message.edit_text(f"🚀 Запускаю генерацию {count} изображений...")
    await callback.answer()

    for i in range(count):
        progress_msg = await callback.message.answer(
            f"🖼 Изображение {i + 1}/{count}\n\n"
            f"{make_progress_bar(0, 100)}\n"
            f"🔄 Подготовка..."
        )

        # Создаём функцию обновления прогресса для текущей итерации
        current_i = i
        async def update_progress(current: int, total: int, stage: str, msg=progress_msg, idx=current_i):
            try:
                bar = make_progress_bar(current, total)
                await msg.edit_text(
                    f"🖼 Изображение {idx + 1}/{count}\n\n"
                    f"{bar}\n"
                    f"{stage}"
                )
            except Exception:
                pass

        image_path = None
        try:
            image_path = await generate_image(
                prompt,
                update_progress,
                seed=random.randint(1, 999999999999999)
            )
            photo = FSInputFile(image_path)
            await progress_msg.delete()
            await callback.message.answer_photo(
                photo,
                caption=f"🖼 {i + 1}/{count}\n\n<b>Промпт:</b> <i>{prompt}</i>",
                parse_mode="HTML"
            )

        except Exception as e:
            logging.error(f"Ошибка генерации {i + 1}: {e}")
            await progress_msg.edit_text(
                f"❌ Ошибка при генерации {i + 1}/{count}:\n<code>{e}</code>\n\n"
                f"Убедись что ComfyUI запущен на {COMFY_URL}",
                parse_mode="HTML"
            )

        finally:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)

# Запуск бота
async def main():
    print("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
