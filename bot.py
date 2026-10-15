"""Progress and Result xususiy maktabi — qabul boti.

Oqim: /start -> ism -> telefon -> yosh/sinf -> taom -> tekshirish -> tasdiqlash.
Tasdiqlangan arizalar SQLite bazaga yoziladi va adminlarga yuboriladi.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Arizalar tushadigan chat(lar): shaxsiy ID yoki guruh ID, vergul bilan ajratiladi
ADMIN_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()]
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "@progress_2021").strip()
CONTACT_PHONE = os.getenv("CONTACT_PHONE", "+998 93 131 49 01").strip()
BRANCH_NAME = os.getenv("BRANCH_NAME", "Zafar").strip()
SCHOOL_NAME = os.getenv("SCHOOL_NAME", "Progress and Result").strip()
DB_PATH = os.getenv("DB_PATH", "applications.db")

router = Router()


class Form(StatesGroup):
    name = State()
    phone = State()
    grade = State()
    meal = State()
    confirm = State()


# ---------- Baza ----------
def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                tg_id INTEGER,
                tg_username TEXT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                grade TEXT NOT NULL,
                meal INTEGER NOT NULL
            )"""
        )


def db_save(tg_id: int, tg_username: str | None, data: dict) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO applications (created_at, tg_id, tg_username, full_name, phone, grade, meal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tg_id,
                tg_username,
                data["name"],
                data["phone"],
                data["grade"],
                1 if data["meal"] else 0,
            ),
        )
        return cur.lastrowid


# ---------- Yordamchilar ----------
def esc(text: str) -> str:
    return html.escape(text, quote=False)


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:  # 901234567
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    return None


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni avtomatik yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def meal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, qo'shilsin", callback_data="meal:yes"),
                InlineKeyboardButton(text="❌ Yo'q", callback_data="meal:no"),
            ]
        ]
    )


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm:yes"),
                InlineKeyboardButton(text="🚫 Bekor qilish", callback_data="confirm:no"),
            ]
        ]
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Ariza qoldirish", callback_data="menu:apply")],
            [
                InlineKeyboardButton(text="💰 To'lov haqida", callback_data="menu:pay"),
                InlineKeyboardButton(text="🍽️ Taom haqida", callback_data="menu:meal"),
            ],
            [InlineKeyboardButton(text="📚 Darslar haqida", callback_data="menu:lessons")],
        ]
    )


def info_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Ariza qoldirish", callback_data="menu:apply")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:home")],
        ]
    )


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="menu:home")]]
    )


def welcome_text() -> str:
    return (
        f"👋 Salom! <b>{esc(SCHOOL_NAME)}</b> xususiy maktabimizga xush kelibsiz!\n\n"
        "Quyidagi bo'limlardan birini tanlang:"
    )


INFO_TEXTS = {
    "pay": (
        "💰 <b>To'lov haqida</b>\n\n"
        "🎒 1–6 sinflar: <b>952 000</b> so'm\n"
        "🎒 7–8 sinflar: <b>1 224 000</b> so'm\n"
        "🎒 9–11 sinflar: <b>1 496 000</b> so'm"
    ),
    "meal": (
        "🍽️ <b>Taom haqida</b>\n\n"
        "5 kun issiq taom, non va choy.\n"
        "To'lov: <b>310 000</b> so'm (maktab to'lovidan tashqari)."
    ),
    "lessons": (
        "📚 <b>Darslar haqida</b>\n\n"
        "Chuqurlashtirilgan holatda mukammal darslar:\n\n"
        "• Ingliz tili\n"
        "• SAT\n"
        "• Matematika\n"
        "• Tarix\n"
        "• Huquq\n"
        "• Ona tili va adabiyot\n\n"
        "<b>Qo'shimcha:</b> turk tili, arab tili"
    ),
}


def summary_text(d: dict) -> str:
    meal = "Taomlanish ham" if d["meal"] else "Taomlanishsiz"
    return (
        "📋 <b>Ma'lumotlaringizni tekshiring:</b>\n\n"
        f"👤 Ism: {esc(d['name'])}\n"
        f"📱 Telefon: {esc(d['phone'])}\n"
        f"🎒 Yosh/sinf: {esc(d['grade'])}\n"
        f"🍽️ {meal}\n\n"
        "Yuqoridagi ma'lumotlar to'g'rimi?"
    )


# ---------- Handlerlar ----------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(welcome_text(), reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:home")
async def menu_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(welcome_text(), reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data.in_({"menu:pay", "menu:meal", "menu:lessons"}))
async def menu_info(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(INFO_TEXTS[call.data.split(":", 1)[1]], reply_markup=info_kb())
    await call.answer()


@router.callback_query(F.data == "menu:apply")
async def menu_apply(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Form.name)
    await call.message.answer(
        "Ro'yxatdan o'tish uchun <b>to'liq ismingizni</b> kiriting:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await call.answer()


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Admin yoki guruh ID sini bilish uchun (shaxsiy chatda ham, guruhda ham ishlaydi)."""
    await message.answer(f"Bu chatning ID raqami: <code>{message.chat.id}</code>")


@router.message(Form.name, F.text)
async def get_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Iltimos, to'liq ismingizni to'g'ri kiriting:")
        return
    await state.update_data(name=name)
    await state.set_state(Form.phone)
    await message.answer(
        "📱 <b>Telefon raqamingizni</b> yuboring:\n\n"
        "🔘 Quyidagi tugmani bosing — Telegram raqamingizni o'zi yuboradi\n"
        "✏️ Yoki qo'lda kiriting: <code>+998901234567</code>",
        reply_markup=phone_kb(),
    )


@router.message(Form.phone, F.contact)
async def get_phone_contact(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.contact.phone_number)
    if not phone:
        phone = "+" + re.sub(r"\D", "", message.contact.phone_number)
    await _ask_grade(message, state, phone)


@router.message(Form.phone, F.text)
async def get_phone_text(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(
            "❗ Raqam noto'g'ri. Iltimos, <code>+998901234567</code> ko'rinishida kiriting "
            "yoki tugmani bosing:",
            reply_markup=phone_kb(),
        )
        return
    await _ask_grade(message, state, phone)


async def _ask_grade(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(phone=phone)
    await state.set_state(Form.grade)
    await message.answer(
        "🎒 <b>Yoshingiz yoki nechanchi sinfda o'qishingizni</b> kiriting:\n"
        "Masalan: <i>14 yosh, 7-sinf</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Form.grade, F.text)
async def get_grade(message: Message, state: FSMContext) -> None:
    grade = message.text.strip()
    if len(grade) < 1 or len(grade) > 60:
        await message.answer("Iltimos, yosh yoki sinfni kiriting. Masalan: 14 yosh, 7-sinf")
        return
    await state.update_data(grade=grade)
    await state.set_state(Form.meal)
    await message.answer("🍽️ <b>Abetgi taom ham berilsinmi?</b>\n(To'lovga qo'shilsinmi?)", reply_markup=meal_kb())


@router.callback_query(Form.meal, F.data.in_({"meal:yes", "meal:no"}))
async def get_meal(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(meal=(call.data == "meal:yes"))
    await state.set_state(Form.confirm)
    data = await state.get_data()
    await call.message.edit_text(summary_text(data), reply_markup=confirm_kb())
    await call.answer()


@router.callback_query(Form.confirm, F.data == "confirm:yes")
async def confirm_yes(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    app_id = db_save(call.from_user.id, call.from_user.username, data)
    await state.clear()

    await call.message.edit_text(summary_text(data).rsplit("\n\n", 1)[0])  # tugmalarsiz, savolsiz
    await call.message.answer(
        "🎉 <b>Murojaatingiz muvaffaqiyatli qabul qilindi!</b>\n\n"
        f"📍 {esc(BRANCH_NAME)} filialimiz xodimlari tez orada siz bilan bog'lanishadi.\n\n"
        f"Savollar bo'lsa: {esc(CONTACT_USERNAME)}\n"
        f"📞 {esc(CONTACT_PHONE)}\n"
        "Ko'rishguncha! 👋",
        reply_markup=home_kb(),
    )
    await call.answer()

    # Adminlarga xabar
    meal = "Taomlanish ham" if data["meal"] else "Taomlanishsiz"
    uname = f"@{call.from_user.username}" if call.from_user.username else "—"
    text = (
        f"🆕 <b>Yangi ariza #{app_id}</b>\n\n"
        f"👤 Ism: {esc(data['name'])}\n"
        f"📱 Telefon: {esc(data['phone'])}\n"
        f"🎒 Yosh/sinf: {esc(data['grade'])}\n"
        f"🍽️ {meal}\n"
        f"💬 Telegram: {esc(uname)} (ID: <code>{call.from_user.id}</code>)"
    )
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:  # admin botni boshlamagan bo'lishi mumkin
            logging.warning("Adminga yuborib bo'lmadi (%s): %s", chat_id, exc)


@router.callback_query(Form.confirm, F.data == "confirm:no")
async def confirm_no(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("🚫 Ariza bekor qilindi.", reply_markup=home_kb())
    await call.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Boshlash uchun /start ni bosing.")


async def start_health_server() -> None:
    """Hosting (Render) uchun kichik veb-sahifa: tashqi ping botni uyg'oq ushlab turadi.
    Faqat PORT o'zgaruvchisi berilganda (hostingda) ishga tushadi, kompyuterda ishlamaydi."""
    port = int(os.getenv("PORT", "0") or 0)
    if not port:
        return

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Health server %s-portda ishga tushdi", port)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini to'ldiring.")
    logging.basicConfig(level=logging.INFO)
    await start_health_server()
    db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
