import asyncio
import logging
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from google.oauth2.service_account import Credentials
import os
import json

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

SCHEDULE = {
    0: 18, 1: 18, 2: 18, 3: 18,
    4: 10, 5: 10, 6: 10,
}
DAY_END = 24
SLOT_DURATION = 5

ADDRESS = "📍 Крыленко 14 стр3, домофон 116, этаж 8"
ADDRESS_LAT = 59.895772
ADDRESS_LON = 30.465483
YANDEX_MAPS_URL = f"https://yandex.ru/maps/?pt={ADDRESS_LON},{ADDRESS_LAT}&z=17&l=map"
# ENTRY_PHOTO = "https://..." # сюда вставьте ссылку на фото входа позже

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
    "cold_restore":  {"name": "Холодное восстановление",             "price": 4500, "hours": 2},
    "scalp_peeling": {"name": "Пилинг кожи головы",                  "price": 1000, "hours": 1},
    "trim_after":    {"name": "Стрижка кончиков после процедуры",     "price": 0,    "hours": 1},
    "trim_only":     {"name": "Стрижка кончиков без процедуры",       "price": 800,  "hours": 1},
    "keratin_bangs": {"name": "Кератин чёлки",                       "price": 2000, "hours": 1},
    "root_zone":     {"name": "Прикорневая зона*",                   "price": 4000, "hours": 2},
}

class BookingStates(StatesGroup):
    choosing_service   = State()
    choosing_length    = State()
    choosing_thickness = State()
    choosing_date      = State()
    choosing_time      = State()
    entering_contact   = State()

bookings: dict = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheet():
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        return gc.open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        logging.error(f"Sheets error: {e}")
        return None

def save_to_sheets(b: dict):
    sheet = get_sheet()
    if not sheet:
        return
    try:
        if sheet.row_count == 0 or sheet.cell(1,1).value != "Дата":
            sheet.insert_row(["Дата","Время","Клиент","Контакт","Услуга","Густота","Стоимость","Статус"], 1)
        price = "уточняется" if b["price"] == -1 else ("бесплатно" if b["price"] == 0 else f"{b['price']} ₽")
        sheet.append_row([b["date"], b["time"], b["name"], b["contact"],
                          b["service"], b.get("thickness","—"), price, "Подтверждена"])
    except Exception as e:
        logging.error(f"Sheets write error: {e}")

# ── ХЕЛПЕРЫ ───────────────────────────────────────────────────────────────────
def get_dates(offset=0):
    dates = []
    today = datetime.now().date()
    count = 0
    i = 1
    while len(dates) < 14:
        d = today + timedelta(days=i)
        if d.weekday() in SCHEDULE:
            count += 1
            if count > offset:
                dates.append(d)
        i += 1
        if i > 120:
            break
    return dates

def get_available_slots(d: date) -> list:
    start_hour = SCHEDULE.get(d.weekday())
    if start_hour is None:
        return []
    booked_starts = set()
    for b in bookings.values():
        if b.get("date") == str(d):
            booked_starts.add(int(b["time"].split(":")[0]))
    last_start = DAY_END - SLOT_DURATION
    slots = []
    for hour in range(start_hour, last_start + 1):
        if not any(abs(hour - bs) < SLOT_DURATION for bs in booked_starts):
            slots.append(f"{hour:02d}:00")
    return slots

def fmt_date(d: date) -> str:
    months = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
    days   = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{d.day} {months[d.month-1]} ({days[d.weekday()]})"

def fmt_price(p: int) -> str:
    if p == -1: return "уточняется у мастера"
    return "бесплатно" if p == 0 else f"{p:,} ₽".replace(",", " ")

def schedule_reminders(booking_id: str, b: dict):
    visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    now = datetime.now()

    async def remind(uid, text):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logging.error(e)

    r24 = visit_dt - timedelta(hours=24)
    r2  = visit_dt - timedelta(hours=2)
    if r24 > now:
        scheduler.add_job(remind, "date", run_date=r24,
            args=[b["user_id"],
                  f"⏰ <b>Напоминание!</b>\nЗавтра:\n{b['service']}\n"
                  f"{fmt_date(visit_dt.date())} в {b['time']}\n\n{ADDRESS}"],
            id=f"r24_{booking_id}", replace_existing=True)
    if r2 > now:
        scheduler.add_job(remind, "date", run_date=r2,
            args=[b["user_id"],
                  f"⏰ <b>Через 2 часа</b> ваша процедура!\n{b['service']}\nв {b['time']}\n\n{ADDRESS}"],
            id=f"r2_{booking_id}", replace_existing=True)

# ── ПОСТОЯННОЕ МЕНЮ ───────────────────────────────────────────────────────────
def main_reply_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💆 Записаться")
    kb.button(text="💰 Прайс")
    kb.button(text="📋 Мои записи")
    kb.button(text="🗺 Как пройти")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

MAIN_TEXT = ("✨ <b>Keratin & Botox Studio</b>\n\n"
             "Привет! Я помогу вам записаться на процедуру.\n"
             "Выберите действие:")

# ── СТАРТ ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(MAIN_TEXT, reply_markup=main_reply_kb(), parse_mode="HTML")

@dp.message(F.text == "💆 Записаться")
async def btn_book(message: Message, state: FSMContext):
    await start_booking_msg(message, state)

@dp.message(F.text == "💰 Прайс")
@dp.message(Command("price"))
async def btn_price(message: Message):
    await message.answer(
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
        "Прикорневая зона — 4 000 ₽",
        reply_markup=main_reply_kb(), parse_mode="HTML"
    )

@dp.message(F.text == "📋 Мои записи")
@dp.message(Command("mybookings"))
async def btn_mybookings(message: Message):
    await show_my_bookings(message)

@dp.message(F.text == "🗺 Как пройти")
async def btn_how_to_get(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺 Открыть в Яндекс.Картах", url=YANDEX_MAPS_URL)
    await message.answer_location(latitude=ADDRESS_LAT, longitude=ADDRESS_LON)
    await message.answer(
        f"<b>Как нас найти:</b>\n\n"
        f"{ADDRESS}\n\n"
        f"🚇 Ближайшее метро: Улица Дыбенко\n"
        f"🚶 От метро ~10 минут пешком\n\n"
        f"Войдите во двор, домофон 116, лифт на 8 этаж.",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )
    # Когда будет фото — раскомментируйте:
    # await message.answer_photo(photo=ENTRY_PHOTO, caption="Вход в подъезд")

# ── ЗАПИСЬ: ВЫБОР УСЛУГИ ──────────────────────────────────────────────────────
async def start_booking_msg(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Кератиновое выпрямление",            callback_data="svc_keratin")
    kb.button(text="❄️ Холодное восстановление",            callback_data="svc_cold_restore")
    kb.button(text="🌿 Пилинг кожи головы",                 callback_data="svc_scalp_peeling")
    kb.button(text="✂️ Стрижка кончиков (после процедуры)", callback_data="svc_trim_after")
    kb.button(text="✂️ Стрижка кончиков (без процедуры)",   callback_data="svc_trim_only")
    kb.button(text="💫 Кератин чёлки",                      callback_data="svc_keratin_bangs")
    kb.button(text="🔄 Прикорневая зона",                   callback_data="svc_root_zone")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_service)
    await message.answer("💆 <b>Выберите услугу:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "book")
async def cb_book(callback: CallbackQuery, state: FSMContext):
    await start_booking_msg(callback.message, state)
    await callback.answer()

# ── КЕРАТИН: ДЛИНА ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "svc_keratin")
async def choose_length(callback: CallbackQuery, state: FSMContext):
    await state.update_data(service="keratin")
    kb = InlineKeyboardBuilder()
    for l in KERATIN_PRICES:
        kb.button(text=f"{l} см", callback_data=f"len_{l}")
    kb.button(text="🤷 Не знаю свою длину", callback_data="len_unknown")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_length)
    await callback.message.edit_text(
        "📏 <b>Длина ваших волос:</b>\n\n<i>От корней до кончиков</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "len_unknown")
async def length_unknown(callback: CallbackQuery, state: FSMContext):
    await state.update_data(length=0, price=-1, hours=4,
                            service_name="Кератиновое выпрямление (длина уточняется)",
                            thickness="")
    await show_dates(callback, state, offset=0)

@dp.callback_query(F.data.startswith("len_") & ~F.data.endswith("unknown"))
async def choose_thickness(callback: CallbackQuery, state: FSMContext):
    length = int(callback.data.split("_")[1])
    await state.update_data(length=length)
    kb = InlineKeyboardBuilder()
    for name, extra in THICKNESS_PRICES.items():
        label = name if extra == 0 else f"{name} (+{extra} ₽)"
        kb.button(text=label, callback_data=f"thick_{name}")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_thickness)
    await callback.message.edit_text(
        "💇 <b>Густота волос:</b>\n\n<i>Сечение хвоста у основания</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("thick_"))
async def after_thickness(callback: CallbackQuery, state: FSMContext):
    thickness = callback.data[6:]
    data = await state.get_data()
    length = data["length"]
    price = KERATIN_PRICES[length]["price"] + THICKNESS_PRICES[thickness]
    await state.update_data(thickness=thickness, price=price,
                            hours=KERATIN_PRICES[length]["hours"],
                            service_name=f"Кератиновое выпрямление {length} см")
    await show_dates(callback, state, offset=0)

# ── ДРУГИЕ УСЛУГИ ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("svc_") & ~F.data.endswith("keratin"))
async def choose_other(callback: CallbackQuery, state: FSMContext):
    key = callback.data[4:]
    svc = OTHER_SERVICES.get(key)
    if not svc:
        return
    await state.update_data(service=key, service_name=svc["name"],
                            price=svc["price"], hours=svc["hours"], thickness="")
    await show_dates(callback, state, offset=0)

# ── ДАТЫ ──────────────────────────────────────────────────────────────────────
async def show_dates(callback: CallbackQuery, state: FSMContext, offset: int = 0):
    dates = get_dates(offset=offset)
    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=fmt_date(d), callback_data=f"date_{d}")
    if offset == 0:
        kb.button(text="📅 Позднее →", callback_data="dates_next")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(2)
    await state.update_data(dates_offset=offset)
    await state.set_state(BookingStates.choosing_date)
    title = "📅 <b>Выберите дату:</b>" if offset == 0 else "📅 <b>Следующие 2 недели:</b>"
    await callback.message.edit_text(title, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "dates_next")
async def dates_next(callback: CallbackQuery, state: FSMContext):
    await show_dates(callback, state, offset=14)

@dp.callback_query(F.data.startswith("date_"))
async def choose_time(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[5:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await state.update_data(date=date_str, date_display=fmt_date(d))
    slots = get_available_slots(d)
    if not slots:
        await callback.answer("На этот день нет свободного времени.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=s, callback_data=f"time_{s}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_time)
    await callback.message.edit_text(f"⏰ <b>Время на {fmt_date(d)}:</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

# ── КОНТАКТ ───────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("time_"))
async def ask_contact(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data[5:]
    await state.update_data(time=time_str)
    data = await state.get_data()
    thick = f"\nГустота: {data['thickness']}" if data.get("thickness") else ""
    await state.set_state(BookingStates.entering_contact)
    await callback.message.edit_text(
        f"✅ <b>Почти готово!</b>\n\n"
        f"Услуга: {data['service_name']}{thick}\n"
        f"Дата: {data['date_display']}\n"
        f"Время: {time_str}\n"
        f"Стоимость: {fmt_price(data['price'])}\n\n"
        f"Как с вами связаться?\n"
        f"<i>Напишите номер телефона или @username в Telegram</i>",
        parse_mode="HTML")

@dp.message(BookingStates.entering_contact)
async def finalize(message: Message, state: FSMContext):
    contact = message.text.strip()
    data = await state.get_data()
    booking_id = f"{data['date']}_{data['time']}_{message.from_user.id}"
    b = {
        "user_id":   message.from_user.id,
        "service":   data["service_name"],
        "date":      data["date"],
        "time":      data["time"],
        "price":     data["price"],
        "name":      message.from_user.full_name,
        "contact":   contact,
        "thickness": data.get("thickness", ""),
    }
    bookings[booking_id] = b
    schedule_reminders(booking_id, b)
    save_to_sheets(b)

    thick = f"\nГустота: {b['thickness']}" if b.get("thickness") else ""
    await message.answer(
        f"🎉 <b>Вы записаны!</b>\n\n"
        f"Услуга: {b['service']}{thick}\n"
        f"Дата: {data['date_display']}\n"
        f"Время: {b['time']}\n"
        f"Стоимость: {fmt_price(b['price'])}\n\n"
        f"{ADDRESS}\n\n"
        f"Напомню за 24 ч и за 2 ч до визита 🔔",
        reply_markup=main_reply_kb(), parse_mode="HTML")

    try:
        await bot.send_message(ADMIN_ID,
            f"🆕 <b>Новая запись!</b>\n\n"
            f"👤 {b['name']}\n"
            f"📱 {contact}\n"
            f"💆 {b['service']}{thick}\n"
            f"📅 {data['date_display']} в {b['time']}\n"
            f"💰 {fmt_price(b['price'])}",
            parse_mode="HTML")
    except Exception as e:
        logging.error(e)
    await state.clear()

# ── МОИ ЗАПИСИ ────────────────────────────────────────────────────────────────
async def show_my_bookings(message: Message):
    uid = message.from_user.id
    ub = sorted([b for b in bookings.values() if b["user_id"] == uid],
                key=lambda x: (x["date"], x["time"]))
    if not ub:
        await message.answer("У вас пока нет записей.", reply_markup=main_reply_kb())
        return

    text = "📋 <b>Ваши записи:</b>\n\n"
    kb = InlineKeyboardBuilder()
    for i, b in enumerate(ub):
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        text += f"{i+1}. {b['service']}\n   📅 {fmt_date(d)} в {b['time']} — {fmt_price(b['price'])}\n\n"
        kb.button(text=f"❌ Отменить запись №{i+1}", callback_data=f"cancel_{b['date']}_{b['time']}_{uid}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_booking(callback: CallbackQuery):
    parts = callback.data[7:].rsplit("_", 1)
    booking_id = f"{parts[0]}_{callback.from_user.id}"
    if booking_id in bookings:
        b = bookings.pop(booking_id)
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        await callback.message.edit_text(
            f"✅ Запись отменена:\n{b['service']}\n{fmt_date(d)} в {b['time']}",
            parse_mode="HTML")
        try:
            await bot.send_message(ADMIN_ID,
                f"❌ <b>Отмена записи!</b>\n\n"
                f"👤 {b['name']}\n📱 {b['contact']}\n"
                f"💆 {b['service']}\n📅 {fmt_date(d)} в {b['time']}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

# ── ЗАПУСК ────────────────────────────────────────────────────────────────────
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
