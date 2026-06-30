import asyncio
import logging
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import asyncpg
from config_loader import (load_config, cfg, get_services, get_schedule,
    get_keratin_prices, get_thickness_prices, get_slot_duration,
    get_slot_step, get_day_end, get_address, get_address_lat, get_address_lon,
    get_admin_ids, get_notify_id, get_extension_note_ids,
    get_metro, get_how_to_get_text, get_master_name,
    get_yandex_reviews, get_vk_reviews, get_knowledge)
import os
import json

# ── НАСТРОЙКИ ─────────────────────────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
SPREADSHEET_ID    = os.getenv("SPREADSHEET_ID", "")
CALENDAR_ID       = os.getenv("CALENDAR_ID", "")
DATABASE_URL      = os.getenv("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CARE_PDF_PATH     = "/app/care.pdf"

class BookingStates(StatesGroup):
    choosing_service   = State()
    choosing_length    = State()
    choosing_thickness = State()
    choosing_date      = State()
    choosing_period    = State()
    choosing_time      = State()
    entering_contact   = State()

class AdminStates(StatesGroup):
    pass  # резерв

class RescheduleStates(StatesGroup):
    choosing_date = State()
    choosing_time = State()

class AssistantStates(StatesGroup):
    waiting_question = State()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())



APPROXIMATE_THICKNESS = {"более 13 см", "уточняется у мастера"}

def _is_price_approximate(thickness: str) -> bool:
    """Цена приблизительная если густота неизвестна или в открытом диапазоне."""
    return thickness in APPROXIMATE_THICKNESS

def _get_price_prefix(service_id: str) -> str:
    """Возвращает префикс цены ('от ') для услуги по её id."""
    for svc in get_services():
        if svc["id"] == service_id:
            prefix = svc.get("price_prefix", "")
            return f"{prefix} " if prefix else ""
    return ""

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
        try:
            await conn.execute("ALTER TABLE bookings ADD COLUMN sent_reminders TEXT DEFAULT ''")
        except Exception:
            pass

async def db_save_booking(booking_id: str, b: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bookings (id, user_id, service, date, time, price, name, contact, thickness, date_display)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO NOTHING
        """, booking_id, b["user_id"], b["service"], b["date"], b["time"],
             b["price"], b["name"], b["contact"], b.get("thickness",""), b.get("date_display",""))

async def db_update_booking_time(booking_id: str, new_date: str, new_time: str, new_date_display: str):
    """Обновляет дату и время записи — перенос без удаления."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE bookings SET date=$1, time=$2, date_display=$3 WHERE id=$4 AND status='active' RETURNING *",
            new_date, new_time, new_date_display, booking_id
        )
    return dict(row) if row else None

async def db_cancel_booking(booking_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE bookings SET status='cancelled' WHERE id=$1 RETURNING *", booking_id)
    return dict(row) if row else None

async def db_get_user_bookings(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM bookings WHERE user_id=$1 AND status='active' AND date >= CURRENT_DATE ORDER BY date,time", user_id)
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

async def db_get_booked_slots_minutes(d: date):
    """Возвращает множество занятых слотов в минутах от полуночи"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT time FROM bookings WHERE date=$1 AND status='active'", str(d))
    result = set()
    for r in rows:
        h, m = map(int, r["time"].split(":"))
        result.add(h * 60 + m)
    return result

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


async def db_mark_reminder_sent(booking_id: str, reminder_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sent_reminders FROM bookings WHERE id=$1", booking_id)
        if row:
            sent = row["sent_reminders"] or ""
            if reminder_type not in sent.split(","):
                new_val = f"{sent},{reminder_type}" if sent else reminder_type
                await conn.execute("UPDATE bookings SET sent_reminders=$1 WHERE id=$2", new_val, booking_id)

def _was_reminder_sent(booking: dict, reminder_type: str) -> bool:
    sent = booking.get("sent_reminders", "") or ""
    return reminder_type in sent.split(",")

async def db_get_all_booked_slots_range(date_from: date, date_to: date) -> dict:
    """Один запрос к БД — все занятые слоты за период. Возвращает {date_str: set(minutes)}"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, time FROM bookings WHERE date >= $1 AND date <= $2 AND status='active'",
            str(date_from), str(date_to)
        )
    result = {}
    for r in rows:
        d_str = r["date"]
        h, m = map(int, r["time"].split(":"))
        result.setdefault(d_str, set()).add(h * 60 + m)
    return result


async def db_get_blocked_dates_set(date_from: date, date_to: date) -> set:
    """Один запрос — все заблокированные даты за период."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date FROM blocked_dates WHERE date >= $1 AND date <= $2",
            str(date_from), str(date_to)
        )
    return {r["date"] for r in rows}

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
        end_dt = (datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=get_slot_duration())).strftime("%Y-%m-%dT%H:%M:%S")
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

def _has_available_slots(d: date, booked_minutes: set) -> bool:
    """Проверяет есть ли хотя бы один свободный слот — без запроса к БД."""
    start_hour = get_schedule().get(d.weekday())
    if start_hour is None:
        return False
    slot_dur = get_slot_duration() * 60
    slot_step = get_slot_step()
    last_start = get_day_end() * 60  # day_end = последний возможный час НАЧАЛА процедуры
    t = start_hour * 60
    while t <= last_start:
        if not any(abs(t - bm) < slot_dur for bm in booked_minutes):
            return True
        t += slot_step
    return False

async def get_dates(offset=0):
    today = datetime.now().date()
    date_from = today + timedelta(days=1)
    date_to   = today + timedelta(days=120)

    # Два запроса к БД — всё сразу
    booked_cache  = await db_get_all_booked_slots_range(date_from, date_to)
    blocked_cache = await db_get_blocked_dates_set(date_from, date_to)

    dates, count, i = [], 0, 1
    while len(dates) < 14:
        d = today + timedelta(days=i)
        if d.weekday() in get_schedule() and str(d) not in blocked_cache:
            booked = booked_cache.get(str(d), set())
            if _has_available_slots(d, booked):
                count += 1
                if count > offset:
                    dates.append(d)
        i += 1
        if i > 120:
            break
    return dates

async def get_available_slots(d: date, period: str = None):
    """period: 'morning' 10-13, 'day' 13-16, 'evening' 16-19:30"""
    start_hour = get_schedule().get(d.weekday())
    if start_hour is None or await db_is_blocked(d):
        return []
    SLOT_DURATION = get_slot_duration()
    SLOT_STEP = get_slot_step()
    booked = await db_get_booked_slots_minutes(d)  # множество занятых минут от полуночи

    # Диапазон периода
    if period == "morning":
        p_start, p_end = max(start_hour * 60, 10 * 60), 13 * 60
    elif period == "day":
        p_start, p_end = max(start_hour * 60, 13 * 60), 16 * 60
    elif period == "evening":
        p_start, p_end = max(start_hour * 60, 16 * 60), 20 * 60
    else:
        p_start = start_hour * 60
        p_end = get_day_end() * 60

    slots = []
    t = p_start
    while t <= p_end:
        # Проверяем что слот не пересекается с занятыми
        conflict = any(
            abs(t - bm) < SLOT_DURATION * 60
            for bm in booked
        )
        if not conflict:
            h, m = divmod(t, 60)
            if h < 24:
                slots.append(f"{h:02d}:{m:02d}")
        t += SLOT_STEP
    return slots

async def get_available_periods(d: date):
    """Возвращает периоды где есть хотя бы один свободный слот"""
    periods = []
    for p, label in [("morning", "🌅 Утро (10:00–13:00)"),
                     ("day",     "☀️ День (13:00–16:00)"),
                     ("evening", "🌆 Вечер (16:00–20:00)")]:
        slots = await get_available_slots(d, period=p)
        if slots:
            periods.append((p, label))
    return periods

def fmt_date(d: date):
    months = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
    days   = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{d.day} {months[d.month-1]} ({days[d.weekday()]})"

def fmt_price(p: int):
    if p == -1: return "уточняется у мастера"
    return "бесплатно" if p == 0 else f"{p:,} ₽".replace(",", " ")

# ── НАПОМИНАНИЯ ───────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


def _cancel_reminder_jobs(booking_id: str):
    """Удаляет все напоминания для записи."""
    for job_id in [f"r24_{booking_id}", f"r2_{booking_id}",
                   f"care_{booking_id}", f"r23_{booking_id}", f"review_{booking_id}"]:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

async def _send_confirmation(uid, bk_id, name, service, date_display, time_str):
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
        await db_mark_reminder_sent(bk_id, "r24")
    except Exception as e:
        logging.error(e)

async def _send_remind(uid, text, booking_id, reminder_type):
    try:
        await bot.send_message(uid, text, parse_mode="HTML")
        await db_mark_reminder_sent(booking_id, reminder_type)
    except Exception as e:
        logging.error(e)

async def _send_care_instructions(uid, booking_id):
    import os
    try:
        if os.path.exists(CARE_PDF_PATH):
            from aiogram.types import FSInputFile
            await bot.send_document(uid, FSInputFile(CARE_PDF_PATH),
                caption="📋 <b>Рекомендации по уходу после процедуры</b>\n\nСохраните этот документ!", parse_mode="HTML")
        else:
            logging.warning("care.pdf не найден")
        await db_mark_reminder_sent(booking_id, "care")
    except Exception as e:
        logging.error(f"Care PDF error: {e}")

async def _send_review_request(uid, name, booking_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Отзыв на Яндекс.Картах", url=cfg("yandex_reviews"))
    kb.button(text="⭐ Отзыв ВКонтакте", url=cfg("vk_reviews"))
    kb.button(text="👍 Всё отлично, спасибо!", callback_data="review_skip")
    kb.adjust(1)
    try:
        await bot.send_message(uid,
            f"✨ Надеемся, процедура прошла отлично!\n\n"
            f"Если вам всё понравилось — будем очень благодарны за отзыв. "
            f"Это занимает 1 минуту и очень помогает нам 🙏",
            reply_markup=kb.as_markup())
        await db_mark_reminder_sent(booking_id, "review")
    except Exception as e:
        logging.error(e)

async def _notify_admin_no_confirm(name, service, date_display, time_str, booking_id):
    try:
        await bot.send_message(get_notify_id(),
            f"⚠️ <b>Клиент не подтвердил запись!</b>\n\n"
            f"👤 {name}\n💆 {service}\n📅 {date_display} в {time_str}\n\n"
            f"Рекомендуем связаться с клиентом.", parse_mode="HTML")
        await db_mark_reminder_sent(booking_id, "r23")
    except Exception as e:
        logging.error(e)

def schedule_reminders(booking_id: str, b: dict):
    visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    now = datetime.now()

    r24 = visit_dt - timedelta(hours=24)
    if r24 > now:
        scheduler.add_job(_send_confirmation, "date", run_date=r24,
            args=[b["user_id"], booking_id, b["name"], b["service"], b.get("date_display",""), b["time"]],
            id=f"r24_{booking_id}", replace_existing=True)

    r2 = visit_dt - timedelta(hours=2)
    if r2 > now:
        scheduler.add_job(_send_remind, "date", run_date=r2,
            args=[b["user_id"], f"⏰ <b>Через 2 часа</b> ваша процедура!\n{b['service']}\nв {b['time']}\n\n{get_address()}", booking_id, "r2"],
            id=f"r2_{booking_id}", replace_existing=True)

    care_time = visit_dt + timedelta(hours=2)
    if care_time > now:
        scheduler.add_job(_send_care_instructions, "date", run_date=care_time,
            args=[b["user_id"], booking_id],
            id=f"care_{booking_id}", replace_existing=True)

    r23 = visit_dt - timedelta(hours=23)
    if r23 > now:
        scheduler.add_job(_notify_admin_no_confirm, "date", run_date=r23,
            args=[b["name"], b["service"], b.get("date_display",""), b["time"], booking_id],
            id=f"r23_{booking_id}", replace_existing=True)

    review_time = visit_dt + timedelta(hours=24)
    if review_time > now:
        scheduler.add_job(_send_review_request, "date", run_date=review_time,
            args=[b["user_id"], b["name"], booking_id],
            id=f"review_{booking_id}", replace_existing=True)

# ── ИИ-АССИСТЕНТ ─────────────────────────────────────────────────────────────
EXIT_PHRASES = {"стоп", "выход", "хватит", "всё", "все", "закрыть", "выйти", "конец", "пока", "спасибо"}

def _build_kb_text():
    kb_lines = []
    for i, item in enumerate(get_knowledge()):
        kb_lines.append(
            str(i+1) + ". Вопрос: " + item["question"] +
            "\n   Ответ: " + item["answer"] +
            "\n   Действие: " + item["action"]
        )
    return "\n\n".join(kb_lines)

async def ask_assistant(user_message: str) -> list:
    """
    Принимает сообщение клиента (может быть несколько вопросов).
    Возвращает список ответов: [{"answer": str, "action": str}, ...]
    """
    knowledge = get_knowledge()
    if not knowledge or not ANTHROPIC_API_KEY:
        return [{"answer": None, "action": ""}]

    kb_text = _build_kb_text()

    prompt = (
        "Ты помощник мастера по кератиновому выпрямлению волос.\n"
        "Отвечай ТОЛЬКО на основе базы знаний ниже. Не придумывай и не добавляй ничего от себя.\n\n"
        "База знаний:\n" + kb_text + "\n\n"
        "Сообщение клиента может содержать один или несколько вопросов.\n"
        "Для КАЖДОГО вопроса найди подходящий ответ в базе и верни массив JSON:\n"
        '[{"answer": "текст ответа", "action": "действие или пустая строка"}, ...]\n\n'
        'Если на какой-то вопрос ответа нет — {"answer": null, "action": ""}.\n'
        "Отвечай только JSON-массивом, без пояснений.\n\n"
        "Сообщение клиента: " + user_message
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        if isinstance(result, dict):
            result = [result]
        return result
    except Exception as e:
        logging.error(f"Assistant error: {e}")
        return [{"answer": None, "action": ""}]

# ── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────────
def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💆 Записаться")
    kb.button(text="💰 Прайс")
    kb.button(text="📋 Мои записи")
    kb.button(text="🗺 Как пройти")
    kb.button(text="🤖 Спросить ассистента")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def admin_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💆 Записаться")
    kb.button(text="💰 Прайс")
    kb.button(text="📋 Мои записи")
    kb.button(text="🗺 Как пройти")
    kb.button(text="🤖 Спросить ассистента")
    kb.button(text="👑 Админ-панель")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_kb(user_id: int):
    return admin_kb() if user_id in get_admin_ids() else main_kb()

def get_main_text():
    return f"✨ <b>{cfg('salon_name', 'Кератин&Ботокс')}</b>\n\n{cfg('welcome_text', 'Привет! Я помогу вам записаться на процедуру.')}\nВыберите действие:"

# ── СТАРТ ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(get_main_text(), reply_markup=get_kb(message.from_user.id), parse_mode="HTML")

# ── ПРАЙС ─────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💰 Прайс", StateFilter("*"))
@dp.message(Command("price"))
async def show_price(message: Message):
    import os
    from aiogram.types import FSInputFile

    mode = cfg("price_display", "both")  # image / text / both

    if mode in ("image", "both"):
        if os.path.exists("/app/price1.jpg"):
            await message.answer_photo(FSInputFile("/app/price1.jpg"))
        if os.path.exists("/app/price2.jpg"):
            await message.answer_photo(FSInputFile("/app/price2.jpg"))

    if mode in ("text", "both"):
        lines = ["💰 <b>Прайс-лист</b>\n"]
        for svc in get_services():
            prefix = svc.get("price_prefix", "")
            if svc["price"] > 0:
                price_str = f"{prefix + ' ' if prefix else ''}{svc['price']:,} ₽".replace(",", " ")
            else:
                price_str = "уточняется у мастера"
            lines.append(f"{svc['name']} — {price_str}")
        await message.answer("\n".join(lines), reply_markup=get_kb(message.from_user.id), parse_mode="HTML")
    else:
        # Только картинки — клавиатура уже видна внизу, ничего не пишем
        pass

# ── КАК ПРОЙТИ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "🗺 Как пройти", StateFilter("*"))
async def how_to_get(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺 Открыть в Яндекс.Картах", url=f"https://yandex.ru/maps/?pt={get_address_lon()},{get_address_lat()}&z=17&l=map")
    await message.answer_location(latitude=get_address_lat(), longitude=get_address_lon())
    metro = get_metro()
    metro_line = f"\n🚇 {metro}" if metro else ""
    how_text = get_how_to_get_text()
    how_line = f"\n\n{how_text}" if how_text else ""
    await message.answer(
        f"<b>Как нас найти:</b>\n\n{get_address()}{metro_line}{how_line}",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    # await message.answer_photo(photo=ENTRY_PHOTO, caption="Вход в подъезд")


# ── ПОДТВЕРЖДЕНИЕ ЗАПИСИ ──────────────────────────────────────────────────────
@dp.callback_query(F.data == "review_skip")
async def review_skip(callback: CallbackQuery):
    await callback.message.edit_text("Спасибо! Всегда рады вас видеть 😊")

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
            f"💆 {b['service']}\n📅 {b.get('date_display','')} в {b['time']}\n\n{get_address()}",
            parse_mode="HTML")
        try:
            await bot.send_message(get_notify_id(),
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
        _cancel_reminder_jobs(booking_id)
        await callback.message.edit_text(
            "❌ Ваша запись отменена. Будем рады видеть вас в другой раз!\n\n"
            "Для новой записи нажмите «💆 Записаться»")
        try:
            await bot.send_message(get_notify_id(),
                f"❌ <b>{b['name']} отменил запись!</b>\n"
                f"💆 {b['service']}\n📅 {b.get('date_display','')} в {b['time']}", parse_mode="HTML")
        except Exception as e: logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

# ── МОИ ЗАПИСИ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "📋 Мои записи", StateFilter("*"))
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
        try:
            date_val = b["date"]
            d = date_val if not isinstance(date_val, str) else datetime.strptime(date_val, "%Y-%m-%d").date()
            text += f"{i+1}. {b['service']}\n   📅 {fmt_date(d)} в {b['time']} — {fmt_price(b['price'])}\n\n"
            kb.button(text=f"🔄 Перенести №{i+1}", callback_data=f"reschedule_{b['id']}")
            kb.button(text=f"❌ Отменить №{i+1}", callback_data=f"cancel_{b['id']}")
        except Exception as e:
            logging.error(f"my_bookings error: {e}, booking: {b}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_") & ~F.data.startswith("cancel_admin_"))
async def cancel_booking(callback: CallbackQuery):
    booking_id = callback.data[7:]
    b = await db_cancel_booking(booking_id)
    if b:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        gcal_delete_event(b)
        _cancel_reminder_jobs(booking_id)
        await callback.message.edit_text(
            f"✅ Запись отменена:\n{b['service']}\n{fmt_date(d)} в {b['time']}", parse_mode="HTML")
        try:
            await bot.send_message(get_notify_id(),
                f"❌ <b>Клиент отменил запись!</b>\n\n"
                f"👤 {b['name']}\n📱 {b['contact']}\n"
                f"💆 {b['service']}\n📅 {fmt_date(d)} в {b['time']}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    else:
        await callback.answer("Запись не найдена.", show_alert=True)

# ── ИИ-АССИСТЕНТ ОБРАБОТЧИКИ ─────────────────────────────────────────────────
def _assistant_session_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔚 Завершить")
    return kb.as_markup(resize_keyboard=True)

@dp.message(F.text == "🤖 Спросить ассистента")
async def assistant_start(message: Message, state: FSMContext):
    await state.set_state(AssistantStates.waiting_question)
    await message.answer(
        "🤖 <b>Ассистент</b>\n\n"
        "Задавайте любые вопросы о процедурах — отвечу на каждый.\n\n"
        "<i>Чтобы выйти — нажмите «🔚 Завершить» или напишите «стоп».</i>",
        reply_markup=_assistant_session_kb(), parse_mode="HTML")

@dp.message(F.text == "🔚 Завершить")
async def assistant_finish_btn(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо, если появятся вопросы - я всегда здесь 😊",
                         reply_markup=get_kb(message.from_user.id))

@dp.message(AssistantStates.waiting_question)
async def assistant_answer(message: Message, state: FSMContext):
    text = message.text.strip()

    # Выход по фразе
    if text.lower() in EXIT_PHRASES:
        await state.clear()
        await message.answer("Хорошо, если появятся вопросы - я всегда здесь 😊",
                             reply_markup=get_kb(message.from_user.id))
        return

    thinking = await message.answer("⏳ Ищу ответ...")
    results = await ask_assistant(text)
    await thinking.delete()

    # Разделяем ответы на обычные (текст) и с действием (прайс, запись)
    text_parts = []
    action_items = []
    for item in results:
        action = item.get("action", "")
        if action in ("show_price", "suggest_contact"):
            action_items.append(item)
        else:
            text_parts.append(item)

    # Склеиваем текстовые ответы в одно нумерованное сообщение
    if text_parts:
        if len(text_parts) == 1:
            answer = text_parts[0].get("answer")
            if answer:
                reply = answer
            else:
                reply = "😔 На этот вопрос не нашла ответа в своей базе.\nПопробуйте переформулировать или спросите мастера напрямую: @Kseniartemka"
        else:
            lines = []
            for i, item in enumerate(text_parts, 1):
                answer = item.get("answer")
                if answer:
                    lines.append(f"<b>{i}.</b> {answer}")
                else:
                    lines.append(f"<b>{i}.</b> 😔 На этот вопрос не нашла ответа в базе. Спросите мастера: @Kseniartemka")
            reply = "\n\n".join(lines)
        await message.answer(reply, reply_markup=_assistant_session_kb(), parse_mode="HTML")

    # Ответы с действием — каждый отдельно (там прайс или кнопки)
    import os
    from aiogram.types import FSInputFile
    for item in action_items:
        answer = item.get("answer")
        action = item.get("action", "")

        if action == "show_price":
            if answer:
                await message.answer(answer, reply_markup=_assistant_session_kb())
            mode = cfg("price_display", "both")
            if mode in ("image", "both"):
                if os.path.exists("/app/price1.jpg"):
                    await message.answer_photo(FSInputFile("/app/price1.jpg"))
                if os.path.exists("/app/price2.jpg"):
                    await message.answer_photo(FSInputFile("/app/price2.jpg"))
            kb = InlineKeyboardBuilder()
            kb.button(text="💆 Записаться", callback_data="book")
            await message.answer("Хотите записаться?", reply_markup=kb.as_markup())

        elif action == "suggest_contact":
            kb = InlineKeyboardBuilder()
            kb.button(text="💆 Записаться", callback_data="book")
            await message.answer(
                answer + "\n\nЧтобы мастер дал персональные рекомендации — запишитесь:",
                reply_markup=kb.as_markup())

    # Автовыход через 10 минут — планируем задачу
    uid = message.from_user.id
    job_id = f"assistant_timeout_{uid}"
    async def _timeout_exit(user_id):
        try:
            data = await dp.fsm.get_context(bot, user_id, user_id).get_data()
        except Exception:
            return
        try:
            await bot.send_message(user_id,
                "⏱ Сессия ассистента завершена по таймауту.",
                reply_markup=get_kb(user_id))
            await dp.fsm.get_context(bot, user_id, user_id).clear()
        except Exception as e:
            logging.error(f"Timeout exit error: {e}")

    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    scheduler.add_job(_timeout_exit, "date",
        run_date=datetime.now() + timedelta(minutes=10),
        args=[uid], id=job_id, replace_existing=True)

# ── ЗАПИСЬ ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💆 Записаться", StateFilter("*"))
async def btn_book(message: Message, state: FSMContext):
    await show_services(message, state)

@dp.callback_query(F.data == "book")
async def cb_book(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.choosing_service)
    kb = InlineKeyboardBuilder()
    for svc in get_services():
        kb.button(text=svc["name"], callback_data=f"svc_{svc['id']}")
    kb.adjust(1)
    await callback.message.edit_text("💆 <b>Выберите услугу:</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

async def show_services(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for svc in get_services():
        kb.button(text=svc["name"], callback_data=f"svc_{svc['id']}")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_service)
    await message.answer("💆 <b>Выберите услугу:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "svc_keratin")
async def choose_length(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for l in get_keratin_prices():
        kb.button(text=f"{l} см", callback_data=f"len_{l}")
    kb.button(text="🤷 Не знаю свою длину", callback_data="len_unknown")
    kb.button(text="◀️ Назад", callback_data="book")
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
    for name, extra in get_thickness_prices().items():
        kb.button(text=name if extra == 0 else f"{name} (+{extra} ₽)", callback_data=f"thick_{name}")
    kb.button(text="🤷 Не знаю", callback_data="thick_unknown")
    kb.button(text="◀️ Назад", callback_data="svc_keratin")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_thickness)
    await callback.message.edit_text(
        "💇 <b>Густота волос:</b>\n\n<i>Сечение хвоста у основания</i>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "thick_unknown")
async def thickness_unknown(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    length = data["length"]
    price  = get_keratin_prices()[length]["price"]
    await state.update_data(thickness="уточняется у мастера", price=price,
                            hours=get_keratin_prices()[length]["hours"],
                            service_id="keratin",
                            service_name=f"Кератиновое выпрямление {length} см")
    await show_dates(callback, state)

@dp.callback_query(F.data.startswith("thick_") & ~F.data.endswith("unknown"))
async def after_thickness(callback: CallbackQuery, state: FSMContext):
    thickness = callback.data[6:]
    data = await state.get_data()
    length = data["length"]
    price  = get_keratin_prices()[length]["price"] + get_thickness_prices()[thickness]
    await state.update_data(thickness=thickness, price=price,
                            hours=get_keratin_prices()[length]["hours"],
                            service_id="keratin",
                            service_name=f"Кератиновое выпрямление {length} см")
    await show_dates(callback, state)

@dp.callback_query(F.data.startswith("svc_") & ~F.data.endswith("keratin"))
async def choose_other(callback: CallbackQuery, state: FSMContext):
    svc_id = callback.data[4:]
    svc = next((s for s in get_services() if s["id"] == svc_id), None)
    if not svc: return
    await state.update_data(service_id=svc["id"], service_name=svc["name"], price=svc["price"],
                            hours=get_slot_duration(), thickness="")
    await show_dates(callback, state)

async def show_dates(callback: CallbackQuery, state: FSMContext, offset: int = 0):
    dates = await get_dates(offset=offset)
    kb    = InlineKeyboardBuilder()
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

@dp.callback_query(F.data == "show_dates")
async def cb_show_dates(callback: CallbackQuery, state: FSMContext):
    await show_dates(callback, state, offset=0)

@dp.callback_query(F.data == "dates_next")
async def dates_next(callback: CallbackQuery, state: FSMContext):
    await show_dates(callback, state, offset=14)

@dp.callback_query(F.data.startswith("date_"))
async def choose_period(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[5:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await state.update_data(date=date_str, date_display=fmt_date(d))
    periods = await get_available_periods(d)
    if not periods:
        await callback.answer("На этот день нет свободного времени.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p, label in periods:
        kb.button(text=label, callback_data=f"period_{p}")
    kb.button(text="◀️ Назад", callback_data="show_dates")
    kb.adjust(1)
    await state.set_state(BookingStates.choosing_period)
    await callback.message.edit_text(
        f"⏰ <b>Когда вам удобно {fmt_date(d)}?</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("period_"))
async def choose_time(callback: CallbackQuery, state: FSMContext):
    period = callback.data[7:]
    data = await state.get_data()
    d = datetime.strptime(data["date"], "%Y-%m-%d").date()
    slots = await get_available_slots(d, period=period)
    if not slots:
        await callback.answer("В это время нет свободных слотов.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=s, callback_data=f"time_{s}")
    kb.button(text="◀️ Назад", callback_data=f"date_{data['date']}")
    kb.adjust(3)
    await state.set_state(BookingStates.choosing_time)
    await callback.message.edit_text(
        f"⏰ <b>Выберите время:</b>",
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
        f"Стоимость: {'от ' if _is_price_approximate(data.get('thickness','')) else ''}{fmt_price(data['price'])}\n"
        f"{f'<i>⚠️ {get_master_name()} свяжется с вами для уточнения суммы</i>' + chr(10) if _is_price_approximate(data.get('thickness','')) else ''}\n"
        f"{'<i>ℹ️ При наличии нарощенных волос доплата +1 000 ₽</i>\n' if data.get('service_id','') in get_extension_note_ids() else ''}"
        f"\nКак с вами связаться?\n"
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
    import time as _time
    booking_id = f"{b['date']}_{b['time']}_{b['user_id']}_{int(_time.time())}"
    try:
        await db_save_booking(booking_id, b)
        logging.info(f"Запись сохранена: {booking_id}")
    except Exception as e:
        logging.error(f"ОШИБКА сохранения записи: {e}")
        await message.answer("❌ Произошла ошибка при сохранении. Попробуйте ещё раз.")
        await state.clear()
        return
    schedule_reminders(booking_id, b)
    save_to_sheets(b)
    gcal_add_event(b)

    thick = f"\nГустота: {b['thickness']}" if b.get("thickness") else ""
    await message.answer(
        f"🎉 <b>Вы записаны!</b>\n\n"
        f"Услуга: {b['service']}{thick}\n"
        f"Дата: {b['date_display']}\n"
        f"Время: {b['time']}\n"
        f"Стоимость: {'от ' if _is_price_approximate(b.get('thickness','')) else ''}{fmt_price(b['price'])}\n"
        f"{f'<i>⚠️ {get_master_name()} свяжется с вами для уточнения суммы</i>' + chr(10) if _is_price_approximate(b.get('thickness','')) else ''}\n"
        f"{get_address()}\n\n"
        f"Напомню за 24 ч и за 2 ч до визита 🔔",
        reply_markup=get_kb(message.from_user.id), parse_mode="HTML")
    notify = get_notify_id()
    if notify:
        try:
            await bot.send_message(notify,
                f"🆕 <b>Новая запись!</b>\n\n"
                f"👤 {b['name']}\n📱 {contact}\n"
                f"💆 {b['service']}{thick}\n"
                f"📅 {b['date_display']} в {b['time']}\n"
                f"💰 {fmt_price(b['price'])}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    await state.clear()


# ── ПЕРЕНОС ЗАПИСИ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("reschedule_") & ~F.data.startswith("reschedule_admin_"))
async def reschedule_start(callback: CallbackQuery, state: FSMContext):
    booking_id = callback.data[11:]
    b = await db_get_booking_by_id(booking_id)
    if not b:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(
        reschedule_id=booking_id,
        service_name=b["service"],
        price=b["price"],
        hours=2,
        thickness=b.get("thickness","")
    )
    dates = await get_dates(offset=0)
    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=fmt_date(d), callback_data=f"redate_{d}")
    kb.button(text="📅 Позднее →", callback_data="redate_next")
    kb.adjust(2)
    await state.set_state(RescheduleStates.choosing_date)
    await callback.message.edit_text(
        f"🔄 <b>Перенос записи</b>\n{b['service']}\n\nВыберите новую дату:",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "redate_next")
async def redate_next(callback: CallbackQuery, state: FSMContext):
    dates = await get_dates(offset=14)
    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=fmt_date(d), callback_data=f"redate_{d}")
    kb.adjust(2)
    await callback.message.edit_text("📅 <b>Следующие 2 недели:</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("redate_") & ~F.data.endswith("next"))
async def reschedule_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[7:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await state.update_data(new_date=date_str, new_date_display=fmt_date(d))
    periods = await get_available_periods(d)
    if not periods:
        await callback.answer("На этот день нет свободного времени.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p, label in periods:
        kb.button(text=label, callback_data=f"reperiod_{p}")
    kb.adjust(1)
    await state.set_state(RescheduleStates.choosing_time)
    await callback.message.edit_text(f"⏰ <b>Когда вам удобно {fmt_date(d)}?</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("reperiod_"))
async def reschedule_period(callback: CallbackQuery, state: FSMContext):
    period = callback.data[9:]
    data = await state.get_data()
    d = datetime.strptime(data["new_date"], "%Y-%m-%d").date()
    slots = await get_available_slots(d, period=period)
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=s, callback_data=f"retime_{s}")
    kb.button(text="◀️ Другое время", callback_data=f"redate_{data['new_date']}")
    kb.adjust(3)
    await callback.message.edit_text("⏰ <b>Выберите время:</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("retime_"))
async def reschedule_time(callback: CallbackQuery, state: FSMContext):
    new_time = callback.data[7:]
    data = await state.get_data()
    booking_id = data["reschedule_id"]

    # Получаем старую запись
    old = await db_get_booking_by_id(booking_id)
    if not old:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Удаляем старые напоминания
    _cancel_reminder_jobs(booking_id)

    # Обновляем дату/время — ID и user_id не меняются!
    updated = await db_update_booking_time(booking_id, data["new_date"], new_time, data["new_date_display"])
    if not updated:
        await callback.answer("Ошибка обновления записи.", show_alert=True)
        return

    # Создаём новые напоминания на новое время
    updated["date_display"] = data["new_date_display"]
    schedule_reminders(booking_id, updated)

    old_date_display = old.get("date_display", "")
    old_time = old["time"]
    await callback.message.edit_text(
        f"\u2705 <b>\u0417\u0430\u043f\u0438\u0441\u044c \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0435\u043d\u0430!</b>\n\n"
        f"\U0001f48e {updated['service']}\n"
        f"\U0001f4c5 {data['new_date_display']} \u0432 {new_time}\n"
        f"\U0001f4b0 {fmt_price(updated['price'])}\n\n{get_address()}",
        parse_mode="HTML")

    if callback.from_user.id in get_admin_ids() and callback.from_user.id != updated["user_id"]:
        try:
            await bot.send_message(updated["user_id"],
                f"\U0001f504 <b>\u0412\u0430\u0448\u0430 \u0437\u0430\u043f\u0438\u0441\u044c \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0435\u043d\u0430 \u043c\u0430\u0441\u0442\u0435\u0440\u043e\u043c</b>\n\n"
                f"\U0001f48e {updated['service']}\n"
                f"\u0411\u044b\u043b\u043e: {old_date_display} \u0432 {old_time}\n"
                f"\u0421\u0442\u0430\u043b\u043e: {data['new_date_display']} \u0432 {new_time}\n\n"
                f"{get_address()}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)

    notify = get_notify_id()
    if notify:
        try:
            await bot.send_message(notify,
                f"\U0001f504 <b>\u041f\u0435\u0440\u0435\u043d\u043e\u0441 \u0437\u0430\u043f\u0438\u0441\u0438!</b>\n\n"
                f"\U0001f464 {updated['name']} \u00b7 {updated['contact']}\n"
                f"\U0001f48e {updated['service']}\n"
                f"\u0411\u044b\u043b\u043e: {old_date_display} \u0432 {old_time}\n"
                f"\u0421\u0442\u0430\u043b\u043e: {data['new_date_display']} \u0432 {new_time}",
                parse_mode="HTML")
        except Exception as e:
            logging.error(e)
    await state.clear()


@dp.callback_query(F.data.startswith("reschedule_admin_"))
async def admin_reschedule_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    booking_id = callback.data[17:]
    b = await db_get_booking_by_id(booking_id)
    if not b:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(
        reschedule_id=booking_id,
        service_name=b["service"],
        price=b["price"],
        hours=get_slot_duration(),
        thickness=b.get("thickness",""),
        is_admin_reschedule=True
    )
    dates = await get_dates(offset=0)
    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=fmt_date(d), callback_data=f"redate_{d}")
    kb.button(text="📅 Позднее →", callback_data="redate_next")
    kb.adjust(2)
    await state.set_state(RescheduleStates.choosing_date)
    await callback.message.edit_text(
        f"🔄 <b>Перенос записи (админ)</b>\n{b['service']}\n"
        f"👤 {b['name']}\n\nВыберите новую дату:",
        reply_markup=kb.as_markup(), parse_mode="HTML")

# ── АДМИН-ПАНЕЛЬ ──────────────────────────────────────────────────────────────
def is_admin(message: Message):
    return message.from_user.id in get_admin_ids()

@dp.message(F.text == "👑 Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все записи",         callback_data="admin_all_bookings")
    kb.button(text="🚫 Заблокировать день",  callback_data="admin_block_pick")
    kb.button(text="✅ Разблокировать день",  callback_data="admin_unblock_pick")
    kb.button(text="📅 Заблокированные дни", callback_data="admin_blocked_list")
    kb.adjust(1)
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_all_bookings")
async def admin_all_bookings(callback: CallbackQuery):
    if callback.from_user.id not in get_admin_ids(): return
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
        kb.button(text=f"🔄 Перенести №{i+1}", callback_data=f"reschedule_admin_{b['id']}")
        kb.button(text=f"❌ Отменить №{i+1}", callback_data=f"cancel_admin_{b['id']}")
    kb.adjust(1)
    # Telegram ограничивает длину — если записей много, обрезаем
    if len(text) > 4000:
        text = text[:3900] + "\n\n<i>...и ещё записи</i>"
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_admin_"))
async def admin_cancel_booking(callback: CallbackQuery):
    if callback.from_user.id not in get_admin_ids(): return
    booking_id = callback.data[13:]
    b = await db_cancel_booking(booking_id)
    if b:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        gcal_delete_event(b)
        _cancel_reminder_jobs(booking_id)
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

@dp.callback_query(F.data == "admin_block_pick")
async def admin_block_pick(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    await state.update_data(block_action="block")
    await _show_admin_date_picker(callback, state, "block")

@dp.callback_query(F.data == "admin_unblock_pick")
async def admin_unblock_pick(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    # Показываем только уже заблокированные дни
    blocked = await db_get_blocked_dates()
    if not blocked:
        await callback.answer("Нет заблокированных дней.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for b in blocked:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        label = fmt_date(d)
        if b.get("reason"):
            label += f" ({b['reason']})"
        kb.button(text=label, callback_data=f"admin_do_unblock_{b['date']}")
    kb.button(text="◀️ Назад", callback_data="admin_panel")
    kb.adjust(1)
    await callback.message.edit_text(
        "✅ <b>Выберите день для разблокировки:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

async def _show_admin_date_picker(callback: CallbackQuery, state: FSMContext, action: str, offset: int = 0):
    today = datetime.now().date()
    dates = []
    i = 0
    while len(dates) < 14:
        i += 1
        d = today + timedelta(days=i)
        dates.append(d)
        if i > 60:
            break
    kb = InlineKeyboardBuilder()
    start = offset
    end = min(offset + 14, len(dates))
    for d in dates[start:end]:
        kb.button(text=fmt_date(d), callback_data=f"admin_do_block_{d}")
    if end < len(dates):
        kb.button(text="📅 Ещё →", callback_data=f"admin_block_more_{offset+14}")
    kb.button(text="◀️ Назад", callback_data="admin_panel")
    kb.adjust(2)
    await callback.message.edit_text(
        "🚫 <b>Выберите день для блокировки:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_block_more_"))
async def admin_block_more(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    offset = int(callback.data.split("_")[-1])
    await _show_admin_date_picker(callback, state, "block", offset)

@dp.callback_query(F.data.startswith("admin_do_block_"))
async def admin_do_block(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    date_str = callback.data[15:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await db_block_date(date_str, "")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В админ-панель", callback_data="admin_panel")
    kb.adjust(1)
    await callback.message.edit_text(
        f"🚫 День <b>{fmt_date(d)}</b> заблокирован.\nКлиенты не смогут записаться на этот день.",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_do_unblock_"))
async def admin_do_unblock(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in get_admin_ids(): return
    date_str = callback.data[17:]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    await db_unblock_date(date_str)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В админ-панель", callback_data="admin_panel")
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ День <b>{fmt_date(d)}</b> разблокирован.",
        reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_panel")
async def admin_panel_cb(callback: CallbackQuery):
    if callback.from_user.id not in get_admin_ids(): return
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все записи",         callback_data="admin_all_bookings")
    kb.button(text="🚫 Заблокировать день",  callback_data="admin_block_pick")
    kb.button(text="✅ Разблокировать день",  callback_data="admin_unblock_pick")
    kb.button(text="📅 Заблокированные дни", callback_data="admin_blocked_list")
    kb.adjust(1)
    await callback.message.edit_text("👑 <b>Админ-панель</b>",
                                     reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_blocked_list")
async def admin_blocked_list(callback: CallbackQuery):
    if callback.from_user.id not in get_admin_ids(): return
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
    restored = 0
    caught_up = 0
    for b in all_b:
        try:
            visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")

            # Будущие напоминания — планируем как обычно
            if visit_dt > now:
                schedule_reminders(b["id"], b)
                restored += 1
                continue

            # Прошедшие записи — досылаем пропущенные напоминания
            # Памятка по уходу (должна была уйти через 2ч после визита)
            care_time = visit_dt + timedelta(hours=2)
            if care_time <= now and not _was_reminder_sent(b, "care"):
                # Не старше 3 дней — иначе уже неактуально
                if (now - care_time).days < 3:
                    await _send_care_instructions(b["user_id"], b["id"])
                    caught_up += 1

            # Отзыв (должен был уйти через 24ч после визита)
            review_time = visit_dt + timedelta(hours=24)
            if review_time <= now and not _was_reminder_sent(b, "review"):
                if (now - review_time).days < 3:
                    await _send_review_request(b["user_id"], b["name"], b["id"])
                    caught_up += 1

        except Exception as e:
            logging.error(f"Restore reminder error: {e}")
    logging.info(f"Reminders: {restored} scheduled, {caught_up} caught up")

async def main():
    await init_db()
    scheduler.start()
    await restore_reminders()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception(f"Критическая ошибка запуска: {e}")
        raise
# patch main
