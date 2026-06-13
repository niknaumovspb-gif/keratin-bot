import asyncio
import logging
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_СЮДА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "ВАШ_TELEGRAM_ID"))
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")   # JSON-строка из сервисного аккаунта
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")         # ID Google Sheets таблицы

# ─── ГРАФИК РАБОТЫ ───────────────────────────────────────────────────────────
# weekday(): 0=пн 1=вт 2=ср 3=чт 4=пт 5=сб 6=вс
SCHEDULE = {
    0: 18,   # пн с 18:00
    1: 18,   # вт с 18:00
    2: 18,   # ср с 18:00
    3: 18,   # чт с 18:00
    4: 10,   # пт с 10:00
    5: 10,   # сб с 10:00
    6: 10,   # вс с 10:00
}
DAY_END = 24        # работаем до 24:00
SLOT_DURATION = 5   # процедура занимает 5 часов

# ─── ПРАЙС ───────────────────────────────────────────────────────────────────
KERATIN_PRICES = {
    30: {"price": 4000, "hours": 3},
    35: {"price": 4500, "hours": 3},
    40: {"price": 5000, "hours": 3},
    45: {"price": 5500, "hours": 4},
    50: {"price": 6000, "hours": 4},
    55: {"price": 6500, "hours": 4},
    60: {"price": 7000, "hours": 4},
    65: {"price": 8000, "hours": 4},
    70: {"price": 9000, "hours": 4},
}

THICKNESS_PRICES = {
    "до 5 см": 0,
    "5–8 см": 500,
    "9–13 см": 1000,
    "более 13 см": 2000,
    "нарощенные волосы": 1000,
}

OTHER_SERVICES = {
    "cold_restore":   {"name": "Холодное восстановление",              "price": 4500, "hours": 2},
    "scalp_peeling":  {"name": "Пилинг кожи головы",                   "price": 1000, "hours": 1},
    "trim_after":     {"name": "Стрижка кончиков после процедуры",      "price": 0,    "hours": 1},
    "trim_only":      {"name": "Стрижка кончиков без процедуры",        "price": 800,  "hours": 1},
    "keratin_bangs":  {"name": "Кератин чёлки",                        "price": 2000, "hours": 1},
    "root_zone":      {"name": "Прикорневая зона*",                    "price": 4000, "hours": 2},
}

# ─── СОСТОЯНИЯ ───────────────────────────────────────────────────────────────
class BookingStates(StatesGroup):
    choosing_service  = State()
    choosing_length   = State()
    choosing_thickness = State()
    choosing_date     = State()
    choosing_time     = State()
    entering_name     = State()
    entering_phone    = State()

# ─── ХРАНИЛИЩЕ ЗАПИСЕЙ ───────────────────────────────────────────────────────
bookings: dict = {}   # booking_id -> dict

# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────
def get_sheet():
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        return gc.open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        logging.error(f"Google Sheets error: {e}")
        return None

def save_to_sheets(booking: dict):
    sheet = get_sheet()
    if not sheet:
        return
    try:
        # Если нет заголовков — добавить
        if sheet.row_count == 0 or sheet.cell(1, 1).value != "Дата":
            sheet.insert_row(["Дата", "Время", "Клиент", "Телефон",
                               "Услуга", "Густота", "Стоимость", "Статус"], 1)
        price_text = "бесплатно" if booking["price"] == 0 else f"{booking['price']} ₽"
        sheet.append_row([
            booking["date"],
            booking["time"],
            booking["name"],
            booking["phone"],
            booking["service"],
            booking.get("thickness", "—"),
            price_text,
            "Подтверждена"
        ])
    except Exception as e:
        logging.error(f"Ошибка записи в Sheets: {e}")

# ─── ИНИЦИАЛИЗАЦИЯ ───────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# ─── ХЕЛПЕРЫ ─────────────────────────────────────────────────────────────────
def get_available_dates():
    """Ближайшие ~30 дней включая воскресенье."""
    dates = []
    today = datetime.now().date()
    for i in range(1, 45):
        d = today + timedelta(days=i)
        if d.weekday() in SCHEDULE:
            dates.append(d)
        if len(dates) == 30:
            break
    return dates

def get_available_slots(d: date) -> list[str]:
    """
    Слоты с шагом 1 час. Начало — из расписания, конец — DAY_END - SLOT_DURATION.
    Занятые слоты: если чужая запись начинается в пределах ±SLOT_DURATION.
    """
    start_hour = SCHEDULE.get(d.weekday())
    if start_hour is None:
        return []

    # Занятые часы начала на эту дату
    booked_starts = set()
    for b in bookings.values():
        if b.get("date") == str(d):
            booked_starts.add(int(b["time"].split(":")[0]))

    last_start = DAY_END - SLOT_DURATION  # последний слот: 19:00 (19+5=24)
    slots = []
    for hour in range(start_hour, last_start + 1):
        # Проверяем: не пересекается ли с уже занятыми
        conflict = any(
            abs(hour - bs) < SLOT_DURATION
            for bs in booked_starts
        )
        if not conflict:
            slots.append(f"{hour:02d}:00")
    return slots

def format_date_ru(d: date) -> str:
    months = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
    days   = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{d.day} {months[d.month-1]} ({days[d.weekday()]})"

def price_text(price: int) -> str:
    return "бесплатно" if price == 0 else f"{price:,} ₽".replace(",", " ")

# ─── НАПОМИНАНИЯ ─────────────────────────────────────────────────────────────
def schedule_reminders(booking_id: str, booking: dict):
    dt_str = f"{booking['date']} {booking['time']}"
    visit_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    remind_24 = visit_dt - timedelta(hours=24)
    remind_2  = visit_dt - timedelta(hours=2)
    now = datetime.now()

    async def send_reminder(uid, text):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Напоминание не отправлено: {e}")

    if remind_24 > now:
        scheduler.add_job(
            send_reminder,
            "date", run_date=remind_24,
            args=[booking["user_id"],
                  f"⏰ <b>Напоминание!</b>\nЗавтра у вас запись:\n"
                  f"{booking['service']}\n{format_date_ru(visit_dt.date())} в {booking['time']}"],
            id=f"r24_{booking_id}", replace_existing=True
        )
    if remind_2 > now:
        scheduler.add_job(
            send_reminder,
            "date", run_date=remind_2,
            args=[booking["user_id"],
                  f"⏰ <b>Через 2 часа</b> ваша процедура!\n"
                  f"{booking['service']}\n{format_date_ru(visit_dt.date())} в {booking['time']}"],
            id=f"r2_{booking_id}", replace_existing=True
        )

# ─── ГЛАВНОЕ МЕНЮ ─────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="💆 Записаться",   callback_data="book")
    kb.button(text="💰 Прайс",        callback_data="price")
    kb.button(text="📋 Мои записи",   callback_data="my_bookings")
    kb.adjust(1)
    await message.answer(
        "✨ <b>Keratin & Botox Studio</b>\n\n"
        "Привет! Я помогу вам записаться на процедуру.\n"
        "Выберите действие:",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

# ─── ПРАЙС ───────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "price")
async def show_price(callback: CallbackQuery):
    text = (
        "💰 <b>Прайс-лист</b>\n\n"
        "<b>Кератиновое выпрямление:</b>\n"
        "30 см — 4 000 ₽ · 35 см — 4 500 ₽\n"
        "40 см — 5 000 ₽ · 45 см — 5 500 ₽\n"
        "50 см — 6 000 ₽ · 55 см — 6 500 ₽\n"
        "60 см — 7 000 ₽ · 65 см — 8 000 ₽\n"
        "70 см — 9 000 ₽\n\n"
        "<b>Доплаты за густоту:</b>\n"
        "до 5 см — без доплат · 5–8 см — +500 ₽\n"
        "9–13 см — +1 000 ₽ · более 13 см — +2 000 ₽\n"
        "нарощенные — +1 000 ₽\n\n"
        "<b>Другие услуги:</b>\n"
        "Холодное восстановление — 4 500 ₽\n"
        "Пилинг кожи головы — 1 000 ₽\n"
        "Стрижка кончиков после процедуры — бесплатно\n"
        "Стрижка кончиков без процедуры — 800 ₽\n"
        "Кератин чёлки — 2 000 ₽\n"
        "Прикорневая зона — 4 000 ₽"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="💆 Записаться", callback_data="book")
    kb.button(text="◀️ Назад",      callback_data="back_start")
    kb.adjust(1)
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

# ─── ЗАПИСЬ: ВЫБОР УСЛУГИ ────────────────────────────────────────────────────
@dp.callback_query(F.data == "book")
async def start_booking(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Кератиновое выпрямление",             callback_data="svc_keratin")
    kb.button(text="❄️ Холодное восстановление",             callback_data="svc_cold_restore")
    kb.button(text="🌿 Пилинг кожи головы",                  callback_data="svc_scalp_peeling")
    kb.button(text="✂️ Стрижка кончиков (после процедуры)",  callback_data="svc_trim_after")
    kb.button(text="✂️ Стрижка кончиков (без процедуры)",    callback_data="svc_trim_only")
    kb.button(text="💫 Кератин чёлки",                       callback_data="svc_keratin_bangs")
    kb.button(text="🔄 Прикорневая зона",                    callback_data="svc_root_zone")
    kb.button(text="◀️ Назад",                               callback_data="back_start")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_service)
    await callback.message.edit_text(
        "💆 <b>Выберите услугу:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

# ─── КЕРАТИН: ДЛИНА ──────────────────────────────────────────────────────────
@dp.callback_query(F.data == "svc_keratin")
async def choose_length(callback: CallbackQuery, state: FSMContext):
    await state.update_data(service="keratin")
    kb = InlineKeyboardBuilder()
    for length in KERATIN_PRICES:
        kb.button(text=f"{length} см", callback_data=f"len_{length}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_length)
    await callback.message.edit_text(
        "📏 <b>Длина ваших волос:</b>\n\n<i>От корней до кончиков</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("len_"))
async def choose_thickness(callback: CallbackQuery, state: FSMContext):
    length = int(callback.data.split("_")[1])
    await state.update_data(length=length)
    kb = InlineKeyboardBuilder()
    for name, extra in THICKNESS_PRICES.items():
        label = name if extra == 0 else f"{name} (+{extra} ₽)"
        kb.button(text=label, callback_data=f"thick_{name}")
    kb.button(text="◀️ Назад", callback_data="svc_keratin")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_thickness)
    await callback.message.edit_text(
        "💇 <b>Густота волос:</b>\n\n<i>Сечение хвоста у основания</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("thick_"))
async def after_thickness(callback: CallbackQuery, state: FSMContext):
    thickness = callback.data[6:]
    data = await state.get_data()
    length = data["length"]
    p = KERATIN_PRICES[length]["price"] + THICKNESS_PRICES[thickness]
    await state.update_data(
        thickness=thickness,
        price=p,
        hours=KERATIN_PRICES[length]["hours"],
        service_name=f"Кератиновое выпрямление {length} см"
    )
    await show_dates(callback, state)

# ─── ДРУГИЕ УСЛУГИ ───────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("svc_") & ~F.data.endswith("keratin"))
async def choose_other_service(callback: CallbackQuery, state: FSMContext):
    svc_key = callback.data[4:]
    svc = OTHER_SERVICES.get(svc_key)
    if not svc:
        return
    await state.update_data(
        service=svc_key,
        service_name=svc["name"],
        price=svc["price"],
        hours=svc["hours"],
        thickness=""
    )
    await show_dates(callback, state)

# ─── ВЫБОР ДАТЫ ──────────────────────────────────────────────────────────────
async def show_dates(callback: CallbackQuery, state: FSMContext):
    dates = get_available_dates()
    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=format_date_ru(d), callback_data=f"date_{d}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(2)
    await state.set_state(BookingStates.choosing_date)
    await callback.message.edit_text(
        "📅 <b>Выберите дату:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("date_"))
async def choose_time(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[5:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await state.update_data(date=date_str, date_display=format_date_ru(d))

    slots = get_available_slots(d)
    if not slots:
        await callback.answer("На этот день нет свободного времени — выберите другой.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for slot in slots:
        kb.button(text=slot, callback_data=f"time_{slot}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_time)
    await callback.message.edit_text(
        f"⏰ <b>Время на {format_date_ru(d)}:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

# ─── СБОР ИМЕНИ ──────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("time_"))
async def ask_name(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data[5:]
    await state.update_data(time=time_str)
    data = await state.get_data()
    thickness_line = f"\nГустота: {data['thickness']}" if data.get("thickness") else ""
    await state.set_state(BookingStates.entering_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data="back_start")
    await callback.message.edit_text(
        f"✅ <b>Почти готово!</b>\n\n"
        f"Услуга: {data['service_name']}{thickness_line}\n"
        f"Дата: {data['date_display']}\n"
        f"Время: {time_str}\n"
        f"Стоимость: {price_text(data['price'])}\n\n"
        f"Как вас зовут?",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

# ─── СБОР ТЕЛЕФОНА ───────────────────────────────────────────────────────────
@dp.message(BookingStates.entering_name)
async def ask_phone(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await state.set_state(BookingStates.entering_phone)
    await message.answer("📱 Введите ваш номер телефона:")

# ─── ФИНАЛИЗАЦИЯ ЗАПИСИ ──────────────────────────────────────────────────────
@dp.message(BookingStates.entering_phone)
async def finalize_booking(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(client_phone=phone)
    data = await state.get_data()

    booking_id = f"{data['date']}_{data['time']}_{message.from_user.id}"
    booking = {
        "user_id":  message.from_user.id,
        "service":  data["service_name"],
        "date":     data["date"],
        "time":     data["time"],
        "price":    data["price"],
        "name":     data["client_name"],
        "phone":    phone,
        "thickness": data.get("thickness", ""),
    }
    bookings[booking_id] = booking

    # Напоминания
    schedule_reminders(booking_id, booking)

    # Запись в Google Sheets
    save_to_sheets(booking)

    thickness_line = f"\nГустота: {data['thickness']}" if data.get("thickness") else ""

    # Клиенту
    await message.answer(
        f"🎉 <b>Вы записаны!</b>\n\n"
        f"Услуга: {data['service_name']}{thickness_line}\n"
        f"Дата: {data['date_display']}\n"
        f"Время: {data['time']}\n"
        f"Стоимость: {price_text(data['price'])}\n\n"
        f"За 24 ч и за 2 ч до визита я напомню вам 🔔",
        parse_mode="HTML"
    )

    # Мастеру
    admin_text = (
        f"🆕 <b>Новая запись!</b>\n\n"
        f"👤 {data['client_name']}\n"
        f"📱 {phone}\n"
        f"💆 {data['service_name']}{thickness_line}\n"
        f"📅 {data['date_display']} в {data['time']}\n"
        f"💰 {price_text(data['price'])}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Уведомление мастеру не отправлено: {e}")

    await state.clear()

# ─── МОИ ЗАПИСИ ──────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_bookings")
async def my_bookings(callback: CallbackQuery):
    user_id = callback.from_user.id
    ub = sorted(
        [b for b in bookings.values() if b["user_id"] == user_id],
        key=lambda x: (x["date"], x["time"])
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="💆 Записаться", callback_data="book")
    kb.button(text="◀️ Назад",      callback_data="back_start")
    kb.adjust(1)

    if not ub:
        await callback.message.edit_text(
            "У вас пока нет записей.",
            reply_markup=kb.as_markup()
        )
        return

    text = "📋 <b>Ваши записи:</b>\n\n"
    for b in ub:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        text += f"• {b['service']}\n  📅 {format_date_ru(d)} в {b['time']} — {price_text(b['price'])}\n\n"

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

# ─── ОТМЕНА ЗАПИСИ ───────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_bookings")
async def cancel_booking_menu(callback: CallbackQuery):
    # Расширить позже: кнопка «Отменить» рядом с каждой записью
    pass

# ─── НАЗАД В МЕНЮ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "back_start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="💆 Записаться", callback_data="book")
    kb.button(text="💰 Прайс",      callback_data="price")
    kb.button(text="📋 Мои записи", callback_data="my_bookings")
    kb.adjust(1)
    await callback.message.edit_text(
        "✨ <b>Keratin & Botox Studio</b>\n\n"
        "Привет! Я помогу вам записаться на процедуру.\n"
        "Выберите действие:",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
