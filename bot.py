import asyncio
import logging
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import asyncpg
import os
import json

# ── НАСТРОЙКИ ─────────────────────────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
SPREADSHEET_ID    = os.getenv("SPREADSHEET_ID", "")
CALENDAR_ID       = os.getenv("CALENDAR_ID", "")   # ID вашего Google Calendar
DATABASE_URL      = os.getenv("DATABASE_URL", "")
PORTFOLIO_CHANNEL = -1004425193962  # ID канала с портфолио
CARE_PDF_PATH     = "/app/care.pdf"  # путь к PDF инструкции по уходу

ADDRESS     = "📍 Крыленко 14 стр3, домофон 116, этаж 8"
ADDRESS_LAT = 59.895772
ADDRESS_LON = 30.465483
YANDEX_URL  = f"https://yandex.ru/maps/?pt={ADDRESS_LON},{ADDRESS_LAT}&z=17&l=map"
# ENTRY_PHOTO = "https://..."  # ссылка на фото входа — добавьте позже

SCHEDULE = {0: 18, 1: 18, 2: 18, 3: 18, 4: 10, 5: 10, 6: 10}
DAY_END       = 24
SLOT_DURATION = 5

KERATIN_PRICES = {
    30: {"price": 4000, "hours": 3}, 35: {"price": 4500, "hours": 3},
    40: {"price": 5000, "hours": 3}, 45: {"price": 5500, "hours": 4},
    50: {"price": 6000, "hours": 4}, 55: {"price": 6500, "hours": 4},
    60: {"price": 7000, "hours": 4}, 65: {"price": 8000, "hours": 4},
    70: {"price": 9000, "hours": 4},
}
THICKNESS_PRICES = {
    "до 5 см": 0, "5–8 см": 500, "9–13 см": 1000,
    "более 13 см": 2000, "нарощенные волосы": 1000,
}
OTHER_SERVICES = {
    "cold_restore":  {"name": "Холодное восстановление",           "price": 4500, "hours": 2},
    "scalp_peeling": {"name": "Пилинг кожи головы",                "price": 1000, "hours": 1},
    "trim_only":     {"name": "Стрижка кончиков без процедуры",     "price": 800,  "hours": 1},
    "keratin_bangs": {"name": "Кератин чёлки",                     "price": 2000, "hours": 1},
    "root_zone":     {"name": "Прикорневая зона*",                  "price": 4000, "hours": 2},
}

class BookingStates(StatesGroup):
    choosing_service   = State()
    choosing_length    = State()
    choosing_thickness = State()
    choosing_date      = State()
    choosing_time      = State()
    entering_contact   = State()

class AdminStates(StatesGroup):
    blocking_date = State()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── БАЗА ДАННЫХ (asyncpg) ─────────────────────────────────────────────────────
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        url = DATABASE_URL.replace("postgres://", "postgresql://")
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                user_id BIGINT,
                service TEXT,
                date TEXT,
                time TEXT,
                price INTEGER,
                name TEXT,
                contact TEXT,
                thickness TEXT,
                date_display TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS blocked_dates (
                date TEXT PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

async def db_save_booking(booking_id: str, b: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bookings (id, user_id, service, date, time, price, name, contact, thickness, date_display)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO NOTHING
        """, booking_id, b["user_id"], b["service"], b["date"], b["time"],
             b["price"], b["name"], b["contact"], b.get("thickness",""), b.get("date_display",""))

async def db_cancel_booking(booking_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE bookings SET status='cancelled' WHERE id=$1 RETURNING *", booking_id)
    return dict(row) if row else None

async def db_get_user_bookings(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM bookings WHERE user_id=$1 AND status='active' ORDER BY date,time", user_id)
    return [dict(r) for r in rows]

async def db_get_all_bookings():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM bookings WHERE status='active' ORDER BY date,time")
    return [dict(r) for r in rows]

async def db_get_booked_slots(d: date):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT time FROM bookings WHERE date=$1 AND status='active'", str(d))
    return {int(r["time"].split(":")[0]) for r in rows}

async def db_is_blocked(d: date):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM blocked_dates WHERE date=$1", str(d))
    return row is not None

async def db_block_date(d: str, reason: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO blocked_dates (date,reason) VALUES ($1,$2) ON CONFLICT DO NOTHING", d, reason)

async def db_unblock_date(d: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM blocked_dates WHERE date=$1", d)

async def db_get_booking_by_id(booking_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bookings WHERE id=$1 AND status='active'", booking_id)
    return dict(row) if row else None

async def db_get_blocked_dates():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT date, reason FROM blocked_dates ORDER BY date")
    return [dict(r) for r in rows]

# ── GOOGLE CALENDAR ───────────────────────────────────────────────────────────
def get_gcal_service():
    if not GOOGLE_CREDS_JSON or not CALENDAR_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logging.error(f"Calendar error: {e}")
        return None

def gcal_add_event(b: dict):
    svc = get_gcal_service()
    if not svc:
        return
    try:
        dt_str = f"{b['date']}T{b['time']}:00"
        end_dt = (datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=SLOT_DURATION)).strftime("%Y-%m-%dT%H:%M:%S")
        price  = "уточняется" if b["price"] == -1 else fmt_price(b["price"])
        thick  = f", густота {b['thickness']}" if b.get("thickness") else ""
        svc.events().insert(calendarId=CALENDAR_ID, body={
            "summary": f"💆 {b['service']}{thick}",
            "description": f"Клиент: {b['name']}\nКонтакт: {b['contact']}\nСтоимость: {price}",
            "start": {"dateTime": dt_str, "timeZone": "Europe/Moscow"},
            "end":   {"dateTime": end_dt,  "timeZone": "Europe/Moscow"},
        }).execute()
    except Exception as e:
        logging.error(f"Calendar add error: {e}")

def gcal_delete_event(b: dict):
    svc = get_gcal_service()
    if not svc:
        return
    try:
        dt_str = f"{b['date']}T{b['time']}:00"
        events = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=dt_str + "+03:00",
            timeMax=dt_str + "+03:00",
            singleEvents=True
        ).execute().get("items", [])
        for e in events:
            svc.events().delete(calendarId=CALENDAR_ID, eventId=e["id"]).execute()
    except Exception as e:
        logging.error(f"Calendar delete error: {e}")

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def save_to_sheets(b: dict):
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        return
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
        if sheet.row_count == 0 or sheet.cell(1,1).value != "Дата":
            sheet.insert_row(["Дата","Время","Клиент","Контакт","Услуга","Густота","Стоимость","Статус"], 1)
        price = "уточняется" if b["price"] == -1 else ("бесплатно" if b["price"] == 0 else f"{b['price']} ₽")
        sheet.append_row([b["date"], b["time"], b["name"], b["contact"],
                          b["service"], b.get("thickness","—"), price, "Подтверждена"])
    except Exception as e:
        logging.error(f"Sheets error: {e}")

# ── ХЕЛПЕРЫ ───────────────────────────────────────────────────────────────────
async def get_dates(offset=0):
    dates, count, i = [], 0, 1
    today = datetime.now().date()
    while len(dates) < 14:
        d = today + timedelta(days=i)
        if d.weekday() in SCHEDULE and not await db_is_blocked(d):
            count += 1
            if count > offset:
                dates.append(d)
        i += 1
        if i > 120:
            break
    return dates

async def get_available_slots(d: date):
    start_hour = SCHEDULE.get(d.weekday())
    if start_hour is None or await db_is_blocked(d):
        return []
    booked = await db_get_booked_slots(d)
    last_start = DAY_END - SLOT_DURATION
    return [f"{h:02d}:00" for h in range(start_hour, last_start + 1)
            if not any(abs(h - bs) < SLOT_DURATION for bs in booked)]

def fmt_date(d: date):
    months = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
    days   = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{d.day} {months[d.month-1]} ({days[d.weekday()]})"

def fmt_price(p: int):
    if p == -1: return "уточняется у мастера"
    return "бесплатно" if p == 0 else f"{p:,} ₽".replace(",", " ")

# ── НАПОМИНАНИЯ ───────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

def schedule_reminders(booking_id: str, b: dict):
    visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    now = datetime.now()

    async def remind(uid, text):
        try: await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e: logging.error(e)

    async def send_confirmation(uid, bk_id, name, service, date_display, time_str):
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтверждаю запись", callback_data=f"confirm_yes_{bk_id}")
        kb.button(text="❌ Отменить запись",    callback_data=f"confirm_no_{bk_id}")
        kb.adjust(1)
        try:
            await bot.send_message(uid,
                f"👋 <b>Напоминаем о вашей записи завтра!</b>\n\n"
                f"💆 {service}\n📅 {date_display} в {time_str}\n\n"
                f"Пожалуйста, подтвердите визит:",
                reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception as e:
            logging.error(e)

    async def send_care_instructions(uid):
        import os
        try:
            if os.path.exists(CARE_PDF_PATH):
                from aiogram.types import FSInputFile
                await bot.send_document(uid, FSInputFile(CARE_PDF_PATH),
                    caption="📋 <b>Рекомендации по уходу после процедуры</b>\n\nСохраните этот документ!", parse_mode="HTML")
            else:
                logging.warning("care.pdf не найден")
        except Exception as e:
            logging.error(f"Care PDF error: {e}")

    async def notify_admin_no_confirm(name, service, date_display, time_str):
        try:
            await bot.send_message(ADMIN_ID,
                f"⚠️ <b>Клиент не подтвердил запись!</b>\n\n"
                f"👤 {name}\n💆 {service}\n📅 {date_display} в {time_str}\n\n"
                f"Рекомендуем связаться с клиентом.", parse_mode="HTML")
        except Exception as e:
            logging.error(e)

    # За 24 часа — запрос подтверждения
    r24 = visit_dt - timedelta(hours=24)
    if r24 > now:
        scheduler.add_job(send_confirmation, "date", run_date=r24,
            args=[b["user_id"], booking_id, b["name"], b["service"], b.get("date_display",""), b["time"]],
            id=f"r24_{booking_id}", replace_existing=True)

    # За 2 часа — напоминание
    r2 = visit_dt - timedelta(hours=2)
    if r2 > now:
        scheduler.add_job(remind, "date", run_date=r2,
            args=[b["user_id"], f"⏰ <b>Через 2 часа</b> ваша процедура!\n{b['service']}\nв {b['time']}\n\n{ADDRESS}"],
            id=f"r2_{booking_id}", replace_existing=True)

    # Через 2 часа после СТАРТА — инструкция по уходу
    care_time = visit_dt + timedelta(hours=2)
    if care_time > now:
        scheduler.add_job(send_care_instructions, "date", run_date=care_time,
            args=[b["user_id"]],
            id=f"care_{booking_id}", replace_existing=True)

    # Если не подтвердил — уведомить админа (за 23 часа, т.е. через 1 час после запроса)
    r23 = visit_dt - timedelta(hours=23)
    if r23 > now:
        scheduler.add_job(notify_admin_no_confirm, "date", run_date=r23,
            args=[b["name"], b["service"], b.get("date_display",""), b["time"]],
            id=f"r23_{booking_id}", replace_existing=True)

# ── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────────
def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💆 Записаться")
    kb.button(text="💰 Прайс")
    kb.button(text="📋 Мои записи")
    kb.button(text="🗺 Как пройти")
    kb.button(text="📸 Портфолио")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def admin_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💆 Записаться")
    kb.button(text="💰 Прайс")
    kb.button(text="📋 Мои записи")
    kb.button(text="🗺 Как пройти")
    kb.button(text="📸 Портфолио")
    kb.button(text="👑 Админ-панель")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_kb(user_id: int):
    return admin_kb() if user_id == ADMIN_ID else main_kb()

MAIN_TEXT = "✨ <b>Кератин&Ботокс</b>\n\nПривет! Я помогу вам записаться на процедуру.\nВыберите действие:"

# ── СТАРТ ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(MAIN_TEXT, reply_markup=get_kb(message.from_user.id), parse_mode="HTML")

# ── ПРАЙС ─────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💰 Прайс")
@dp.message(Command("price"))
async def show_price(message: Message):
    from aiogram.types import FSInputFile
    await message.answer_photo(FSInputFile("/app/price1.jpg"))
    await message.answer_photo(FSInputFile("/app/price2.jpg"), reply_markup=get_kb(message.from_user.id))

# ── КАК ПРОЙТИ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "🗺 Как пройти")
async def how_to_get(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺 Открыть в Яндекс.Картах", url=YANDEX_URL)
    await message.answer_location(latitude=ADDRESS_LAT, longitude=ADDRESS_LON)
    await message.answer(
        f"<b>Как нас найти:</b>\n\n{ADDRESS}\n\n"
        f"🚇 Ближайшее метро: Улица Дыбенко\n"
        f"🚶 От метро ~10 минут пешком\n\n"
        f"Можно войти с обеих сторон дома, домофон 116, лифт на 8 этаж.",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    # await message.answer_photo(photo=ENTRY_PHOTO, caption="Вход в подъезд")


# ── ПОРТФОЛИО ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "📸 Портфолио")
async def show_portfolio(message: Message):
    try:
        await message.answer("📸 <b>Портфолио работ</b>\n\nЗагружаю...", parse_mode="HTML")
        count = 0
        # Перебираем последние 30 сообщений канала по ID (ищем фото)
        # Получаем последнее сообщение чтобы узнать максимальный ID
        chat = await bot.get_chat(PORTFOLIO_CHANNEL)
        # Пробуем переслать последние сообщения
        for msg_id in range(3, 53):
            try:
                await bot.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=PORTFOLIO_CHANNEL,
                    message_id=msg_id
                )
                count += 1
                if count >= 10:
                    break
            except Exception:
                continue
        if count == 0:
            await message.answer("📸 Портфолио пока пустое — скоро добавим работы!")
    except Exception as e:
        logging.error(f"Portfolio error: {e}")
        await message.answer("📸 Портфолио временно недоступно.")

# ── ПОДТВЕРЖДЕНИЕ ЗАПИСИ ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("confirm_yes_"))
async def confirm_yes(callback: CallbackQuery):
    booking_id = callback.data[12:]
    b = await db_get_booking_by_id(booking_id)
    if b:
        # Отменяем уведомление "не подтвердил"
        try: scheduler.remove_job(f"r23_{booking_id}")
        except: pass
        await callback.message.edit_text(
            f"✅ <b>Отлично, ждём вас!</b>\n\n"
            f"💆 {b['service']}\n📅 {b.get('date_display','')} в {b['time']}\n\n{ADDRESS}",
            parse_mode="HTML")
        try:
            await bot.send_message(ADMIN_ID,
                f"✅ <b>{b['name']} подтвердил запись!</b>\n"
                f"📅 {b.get('date_display','')} в {b['time']}", parse_mode="HTML")
        except Exception as e: logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("confirm_no_"))
async def confirm_no(callback: CallbackQuery):
    booking_id = callback.data[11:]
    b = await db_cancel_booking(booking_id)
    if b:
        try: scheduler.remove_job(f"r2_{booking_id}")
        except: pass
        try: scheduler.remove_job(f"r23_{booking_id}")
        except: pass
        await callback.message.edit_text(
            "❌ Ваша запись отменена. Будем рады видеть вас в другой раз!\n\n"
            "Для новой записи нажмите «💆 Записаться»")
        try:
            await bot.send_message(ADMIN_ID,
                f"❌ <b>{b['name']} отменил запись!</b>\n"
                f"💆 {b['service']}\n📅 {b.get('date_display','')} в {b['time']}", parse_mode="HTML")
        except Exception as e: logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

# ── МОИ ЗАПИСИ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "📋 Мои записи")
@dp.message(Command("mybookings"))
async def my_bookings(message: Message):
    uid = message.from_user.id
    ub  = await db_get_user_bookings(uid)
    if not ub:
        await message.answer("У вас пока нет записей.", reply_markup=get_kb(uid))
        return
    text = "📋 <b>Ваши записи:</b>\n\n"
    kb   = InlineKeyboardBuilder()
    for i, b in enumerate(ub):
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        text += f"{i+1}. {b['service']}\n   📅 {fmt_date(d)} в {b['time']} — {fmt_price(b['price'])}\n\n"
        kb.button(text=f"❌ Отменить №{i+1}", callback_data=f"cancel_{b['id']}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_") & ~F.data.startswith("cancel_admin_"))
async def cancel_booking(callback: CallbackQuery):
    booking_id = callback.data[7:]
    b = await db_cancel_booking(booking_id)
    if b:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        gcal_delete_event(b)
        await callback.message.edit_text(
            f"✅ Запись отменена:\n{b['service']}\n{fmt_date(d)} в {b['time']}", parse_mode="HTML")
        try:
            await bot.send_message(ADMIN_ID,
                f"❌ <b>Клиент отменил запись!</b>\n\n"
                f"👤 {b['name']}\n📱 {b['contact']}\n"
                f"💆 {b['service']}\n📅 {fmt_date(d)} в {b['time']}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

# ── ЗАПИСЬ ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💆 Записаться")
async def btn_book(message: Message, state: FSMContext):
    await show_services(message, state)

async def show_services(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Кератиновое выпрямление",            callback_data="svc_keratin")
    kb.button(text="❄️ Холодное восстановление",            callback_data="svc_cold_restore")
    kb.button(text="🌿 Пилинг кожи головы",                 callback_data="svc_scalp_peeling")
    kb.button(text="✂️ Стрижка кончиков (без процедуры)",   callback_data="svc_trim_only")
    kb.button(text="💫 Кератин чёлки",                      callback_data="svc_keratin_bangs")
    kb.button(text="🔄 Прикорневая зона",                   callback_data="svc_root_zone")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_service)
    await message.answer("💆 <b>Выберите услугу:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "svc_keratin")
async def choose_length(callback: CallbackQuery, state: FSMContext):
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
    await state.update_data(price=-1, hours=4,
                            service_name="Кератиновое выпрямление (длина уточняется)", thickness="")
    await show_dates(callback, state)

@dp.callback_query(F.data.startswith("len_") & ~F.data.endswith("unknown"))
async def choose_thickness(callback: CallbackQuery, state: FSMContext):
    length = int(callback.data.split("_")[1])
    await state.update_data(length=length)
    kb = InlineKeyboardBuilder()
    for name, extra in THICKNESS_PRICES.items():
        kb.button(text=name if extra == 0 else f"{name} (+{extra} ₽)", callback_data=f"thick_{name}")
    kb.button(text="🤷 Не знаю", callback_data="thick_unknown")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_thickness)
    await callback.message.edit_text(
        "💇 <b>Густота волос:</b>\n\n<i>Сечение хвоста у основания</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "thick_unknown")
async def thickness_unknown(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    length = data["length"]
    price  = KERATIN_PRICES[length]["price"]
    await state.update_data(thickness="уточняется у мастера", price=price,
                            hours=KERATIN_PRICES[length]["hours"],
                            service_name=f"Кератиновое выпрямление {length} см")
    await show_dates(callback, state)

@dp.callback_query(F.data.startswith("thick_") & ~F.data.endswith("unknown"))
async def after_thickness(callback: CallbackQuery, state: FSMContext):
    thickness = callback.data[6:]
    data = await state.get_data()
    length = data["length"]
    price  = KERATIN_PRICES[length]["price"] + THICKNESS_PRICES[thickness]
    await state.update_data(thickness=thickness, price=price,
                            hours=KERATIN_PRICES[length]["hours"],
                            service_name=f"Кератиновое выпрямление {length} см")
    await show_dates(callback, state)

@dp.callback_query(F.data.startswith("svc_") & ~F.data.endswith("keratin"))
async def choose_other(callback: CallbackQuery, state: FSMContext):
    svc = OTHER_SERVICES.get(callback.data[4:])
    if not svc: return
    await state.update_data(service_name=svc["name"], price=svc["price"],
                            hours=svc["hours"], thickness="")
    await show_dates(callback, state)

async def show_dates(callback: CallbackQuery, state: FSMContext, offset: int = 0):
    dates = await get_dates(offset=offset)
    kb    = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=fmt_date(d), callback_data=f"date_{d}")
    if offset == 0:
        kb.button(text="📅 Позднее →", callback_data="dates_next")
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
    slots = await get_available_slots(d)
    if not slots:
        await callback.answer("На этот день нет свободного времени.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=s, callback_data=f"time_{s}")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_time)
    await callback.message.edit_text(f"⏰ <b>Время на {fmt_date(d)}:</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("time_"))
async def ask_contact(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data[5:]
    await state.update_data(time=time_str)
    data  = await state.get_data()
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
    data    = await state.get_data()
    b = {
        "user_id":      message.from_user.id,
        "service":      data["service_name"],
        "date":         data["date"],
        "time":         data["time"],
        "price":        data["price"],
        "name":         message.from_user.full_name,
        "contact":      contact,
        "thickness":    data.get("thickness", ""),
        "date_display": data.get("date_display", ""),
    }
    booking_id = f"{b['date']}_{b['time']}_{b['user_id']}"
    await db_save_booking(booking_id, b)
    schedule_reminders(booking_id, b)
    save_to_sheets(b)
    gcal_add_event(b)

    thick = f"\nГустота: {b['thickness']}" if b.get("thickness") else ""
    await message.answer(
        f"🎉 <b>Вы записаны!</b>\n\n"
        f"Услуга: {b['service']}{thick}\n"
        f"Дата: {b['date_display']}\n"
        f"Время: {b['time']}\n"
        f"Стоимость: {fmt_price(b['price'])}\n\n"
        f"{ADDRESS}\n\n"
        f"Напомню за 24 ч и за 2 ч до визита 🔔",
        reply_markup=get_kb(message.from_user.id), parse_mode="HTML")
    try:
        await bot.send_message(ADMIN_ID,
            f"🆕 <b>Новая запись!</b>\n\n"
            f"👤 {b['name']}\n📱 {contact}\n"
            f"💆 {b['service']}{thick}\n"
            f"📅 {b['date_display']} в {b['time']}\n"
            f"💰 {fmt_price(b['price'])}",
            parse_mode="HTML")
    except Exception as e:
        logging.error(e)
    await state.clear()

# ── АДМИН-ПАНЕЛЬ ──────────────────────────────────────────────────────────────
def is_admin(message: Message):
    return message.from_user.id == ADMIN_ID

@dp.message(F.text == "👑 Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все записи",       callback_data="admin_all_bookings")
    kb.button(text="🚫 Заблокировать день", callback_data="admin_block")
    kb.button(text="✅ Разблокировать день", callback_data="admin_unblock")
    kb.button(text="📅 Заблокированные дни", callback_data="admin_blocked_list")
    kb.adjust(1)
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_all_bookings")
async def admin_all_bookings(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    bookings = await db_get_all_bookings()
    if not bookings:
        await callback.message.edit_text("Записей нет.")
        return
    text = "📋 <b>Все активные записи:</b>\n\n"
    kb   = InlineKeyboardBuilder()
    for i, b in enumerate(bookings):
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        thick = f", {b['thickness']}" if b.get("thickness") else ""
        text += (f"{i+1}. <b>{fmt_date(d)} в {b['time']}</b>\n"
                 f"   {b['service']}{thick}\n"
                 f"   👤 {b['name']} · {b['contact']}\n"
                 f"   💰 {fmt_price(b['price'])}\n\n")
        kb.button(text=f"❌ Отменить №{i+1}", callback_data=f"cancel_admin_{b['id']}")
    kb.adjust(1)
    # Telegram ограничивает длину — если записей много, обрезаем
    if len(text) > 4000:
        text = text[:3900] + "\n\n<i>...и ещё записи</i>"
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_admin_"))
async def admin_cancel_booking(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    booking_id = callback.data[13:]
    b = await db_cancel_booking(booking_id)
    if b:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        gcal_delete_event(b)
        await callback.message.edit_text(
            f"✅ Запись отменена:\n{b['service']}\n{fmt_date(d)} в {b['time']}")
        try:
            await bot.send_message(b["user_id"],
                f"❌ <b>Ваша запись отменена мастером</b>\n\n"
                f"{b['service']}\n{fmt_date(d)} в {b['time']}\n\n"
                f"Для новой записи нажмите «💆 Записаться»",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

@dp.callback_query(F.data == "admin_block")
async def admin_block_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.blocking_date)
    await state.update_data(block_action="block")
    await callback.message.edit_text(
        "🚫 Введите дату для блокировки в формате <b>ДД.ММ.ГГГГ</b>\n"
        "Например: <code>25.06.2025</code>\n\n"
        "Можно добавить причину через пробел:\n"
        "<code>25.06.2025 Отпуск</code>",
        parse_mode="HTML")

@dp.callback_query(F.data == "admin_unblock")
async def admin_unblock_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.blocking_date)
    await state.update_data(block_action="unblock")
    await callback.message.edit_text(
        "✅ Введите дату для разблокировки в формате <b>ДД.ММ.ГГГГ</b>\n"
        "Например: <code>25.06.2025</code>",
        parse_mode="HTML")

@dp.message(AdminStates.blocking_date)
async def admin_process_date(message: Message, state: FSMContext):
    if not is_admin(message): return
    data   = await state.get_data()
    action = data.get("block_action")
    parts  = message.text.strip().split(None, 1)
    reason = parts[1] if len(parts) > 1 else ""
    try:
        d = datetime.strptime(parts[0], "%d.%m.%Y").date()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ")
        return
    if action == "block":
        await db_block_date(str(d), reason)
        await message.answer(f"🚫 День {fmt_date(d)} заблокирован{' — ' + reason if reason else ''}.",
                             reply_markup=admin_kb())
    else:
        await db_unblock_date(str(d))
        await message.answer(f"✅ День {fmt_date(d)} разблокирован.", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_blocked_list")
async def admin_blocked_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    blocked = await db_get_blocked_dates()
    if not blocked:
        await callback.message.edit_text("Нет заблокированных дней.")
        return
    text = "🚫 <b>Заблокированные дни:</b>\n\n"
    for b in blocked:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        reason = f" — {b['reason']}" if b.get("reason") else ""
        text += f"• {fmt_date(d)}{reason}\n"
    await callback.message.edit_text(text, parse_mode="HTML")

# ── ЗАПУСК ────────────────────────────────────────────────────────────────────
async def restore_reminders():
    all_b = await db_get_all_bookings()
    now = datetime.now()
    for b in all_b:
        try:
            visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
            if visit_dt > now:
                schedule_reminders(b["id"], b)
        except Exception as e:
            logging.error(f"Restore reminder error: {e}")
    logging.info(f"Restored {len(all_b)} reminders")

async def main():
    await init_db()
    scheduler.start()
    await restore_reminders()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
# patch main
