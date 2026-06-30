"""
Модуль загрузки конфига из Google Sheets.
Читает листы: config, services, schedule, keratin_prices, thickness
Кешируется при старте бота.
"""
import logging
import gspread
from google.oauth2.service_account import Credentials
import json
import os

_config = {}
_services = []
_schedule = {}
_keratin_prices = {}
_thickness_prices = {}
_knowledge = []

def get_gspread_client():
    creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
    if not creds_json:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def load_config():
    global _config, _services, _schedule, _keratin_prices, _thickness_prices, _knowledge

    spreadsheet_id = os.getenv("CONFIG_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        logging.error("CONFIG_SPREADSHEET_ID не задан — бот не может работать без конфига")
        return

    try:
        gc = get_gspread_client()
        if not gc:
            logging.error("GOOGLE_CREDS_JSON не задан — бот не может работать без конфига")
            return

        wb = gc.open_by_key(spreadsheet_id)

        # ── CONFIG ────────────────────────────────────────────────────────────
        sheet = wb.worksheet("config")
        rows = sheet.get_all_values()
        _config = {r[0]: r[1] for r in rows if len(r) >= 2 and r[0]}
        logging.info(f"Config загружен: {len(_config)} параметров")

        # ── SERVICES ──────────────────────────────────────────────────────────
        sheet = wb.worksheet("services")
        rows = sheet.get_all_values()
        _services = []
        for r in rows:
            if len(r) >= 4 and r[0]:
                _services.append({
                    "id":           r[0].strip(),
                    "name":         r[1].strip(),
                    "price":        int(r[2]) if r[2].strip().isdigit() else 0,
                    "type":         r[3].strip(),
                    "price_prefix":    r[4].strip() if len(r) >= 5 else "",
                    "extension_note":  r[5].strip() if len(r) >= 6 else "",
                })
        logging.info(f"Services загружено: {len(_services)} услуг")

        # ── SCHEDULE ──────────────────────────────────────────────────────────
        sheet = wb.worksheet("schedule")
        rows = sheet.get_all_values()
        _schedule = {}
        for r in rows:
            if len(r) >= 2 and r[0].strip().replace(".", "").isdigit():
                _schedule[int(float(r[0]))] = int(float(r[1]))
        logging.info(f"Schedule загружен: {len(_schedule)} дней")

        # ── KERATIN_PRICES ────────────────────────────────────────────────────
        try:
            sheet = wb.worksheet("keratin_prices")
            rows = sheet.get_all_values()
            _keratin_prices = {}
            for r in rows:
                if len(r) >= 3 and r[0].strip().isdigit():
                    _keratin_prices[int(r[0])] = {
                        "price": int(r[1]),
                        "hours": int(r[2]),
                    }
            logging.info(f"Keratin prices загружено: {len(_keratin_prices)} вариантов")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Лист keratin_prices не найден — кератин будет без цен по длине")
            _keratin_prices = {}

        # ── THICKNESS ─────────────────────────────────────────────────────────
        try:
            sheet = wb.worksheet("thickness")
            rows = sheet.get_all_values()
            _thickness_prices = {}
            for r in rows:
                if len(r) >= 2 and r[0].strip() and r[1].strip().isdigit():
                    _thickness_prices[r[0].strip()] = int(r[1])
            logging.info(f"Thickness загружено: {len(_thickness_prices)} вариантов")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Лист thickness не найден — густота не будет учитываться")
            _thickness_prices = {}

        # ── KNOWLEDGE ─────────────────────────────────────────────────────────
        try:
            sheet = wb.worksheet("knowledge")
            rows = sheet.get_all_values()
            _knowledge = []
            for r in rows:
                if len(r) >= 1 and r[0].strip():
                    _knowledge.append({
                        "question": r[0].strip(),
                        "answer":   r[1].strip() if len(r) >= 2 else "",
                        "action":   r[2].strip() if len(r) >= 3 else "",
                    })
            logging.info(f"Knowledge загружено: {len(_knowledge)} записей")
            if _knowledge:
                logging.info(f"Knowledge первая запись: {_knowledge[0]}")
            else:
                logging.warning(f"Knowledge пустой! Сырые строки: {rows[:3]}")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Лист knowledge не найден")
            _knowledge = []

    except Exception as e:
        logging.error(f"Ошибка загрузки конфига: {e}")

# ── ГЕТТЕРЫ ───────────────────────────────────────────────────────────────────
def cfg(key, default=""):
    return _config.get(key, default)

def get_services():
    return _services

def get_schedule():
    return _schedule

def get_keratin_prices():
    return _keratin_prices

def get_thickness_prices():
    return _thickness_prices

def get_slot_duration():
    return int(cfg("slot_duration", "5"))

def get_slot_step():
    return int(cfg("slot_step", "30"))

def get_day_end():
    return int(cfg("day_end", "20"))

def get_address():
    return f"📍 {cfg('address')}"

def get_address_lat():
    return float(cfg("address_lat", "0"))

def get_address_lon():
    return float(cfg("address_lon", "0"))

def get_metro():
    return cfg("metro", "")

def get_how_to_get_text():
    return cfg("how_to_get_text", "")

def get_master_name():
    return cfg("master_name", "Мастер")

def get_admin_ids() -> list:
    ids_str = cfg("admin_ids", "")
    if ids_str:
        return [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    admin_id = os.getenv("ADMIN_ID", "0")
    return [int(admin_id)] if admin_id.isdigit() and int(admin_id) > 0 else []

def get_extension_note_ids() -> set:
    return {s["id"] for s in _services if s.get("extension_note")}

def get_notify_id() -> int:
    notify_str = cfg("notify_id", "")
    if notify_str.strip().isdigit():
        return int(notify_str.strip())
    ids = get_admin_ids()
    return ids[0] if ids else 0

def get_knowledge():
    return _knowledge

def get_yandex_reviews():
    return cfg("yandex_reviews", "")

def get_vk_reviews():
    return cfg("vk_reviews", "")

# Загружаем конфиг при импорте модуля
load_config()
