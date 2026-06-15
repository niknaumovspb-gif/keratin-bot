"""
Модуль загрузки конфига из Google Sheets.
Читает листы: config, services, schedule
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

KERATIN_PRICES_DEFAULT = {
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

THICKNESS_PRICES_DEFAULT = {
    "до 5 см": 0,
    "5–8 см": 500,
    "9–13 см": 1000,
    "более 13 см": 2000,
    "нарощенные волосы": 1000,
}

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
    global _config, _services, _schedule, _keratin_prices, _thickness_prices

    spreadsheet_id = os.getenv("CONFIG_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        logging.warning("CONFIG_SPREADSHEET_ID не задан — используются значения по умолчанию")
        _set_defaults()
        return

    try:
        gc = get_gspread_client()
        if not gc:
            _set_defaults()
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
                    "type":         r[3].strip(),  # simple / complex
                    "price_prefix": r[4].strip() if len(r) >= 5 else "",  # "от" или пусто
                })
        logging.info(f"Services загружено: {len(_services)} услуг")

        # ── SCHEDULE ──────────────────────────────────────────────────────────
        sheet = wb.worksheet("schedule")
        rows = sheet.get_all_values()
        _schedule = {}
        for r in rows:
            if len(r) >= 2 and r[0].strip().isdigit():
                _schedule[int(r[0])] = int(r[1])
        logging.info(f"Schedule загружен: {len(_schedule)} дней")

        # Кератин и густота — пока из дефолтов (можно вынести в отдельный лист позже)
        _keratin_prices = KERATIN_PRICES_DEFAULT
        _thickness_prices = THICKNESS_PRICES_DEFAULT

    except Exception as e:
        logging.error(f"Ошибка загрузки конфига: {e}")
        _set_defaults()

def _set_defaults():
    global _config, _services, _schedule, _keratin_prices, _thickness_prices
    _config = {
        "salon_name":     "Кератин&Ботокс",
        "welcome_text":   "Привет! Я помогу вам записаться на процедуру.",
        "address":        "Крыленко 14 стр3, домофон 116, этаж 8",
        "address_lat":    "59.895772",
        "address_lon":    "30.465483",
        "yandex_reviews": "",
        "vk_reviews":     "",
        "slot_duration":  "5",
        "slot_step":      "30",
        "day_end":        "20",
    }
    _services = [
        {"id": "keratin",       "name": "Кератиновое выпрямление",         "price": 0,    "type": "complex", "price_prefix": "от"},
        {"id": "cold_restore",  "name": "Холодное восстановление",          "price": 4500, "type": "simple",  "price_prefix": ""},
        {"id": "scalp_peeling", "name": "Пилинг кожи головы",               "price": 1000, "type": "simple",  "price_prefix": ""},
        {"id": "trim_only",     "name": "Стрижка кончиков без процедуры",   "price": 800,  "type": "simple",  "price_prefix": ""},
        {"id": "keratin_bangs", "name": "Кератин чёлки",                    "price": 2000, "type": "simple",  "price_prefix": ""},
        {"id": "root_zone",     "name": "Прикорневая зона*",                "price": 4000, "type": "simple",  "price_prefix": ""},
    ]
    _schedule = {0: 18, 1: 18, 2: 18, 3: 18, 4: 10, 5: 10, 6: 10}
    _keratin_prices = KERATIN_PRICES_DEFAULT
    _thickness_prices = THICKNESS_PRICES_DEFAULT

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
    return float(cfg("address_lat", "59.895772"))

def get_address_lon():
    return float(cfg("address_lon", "30.465483"))

def get_admin_ids() -> list:
    """Список ID администраторов."""
    ids_str = cfg("admin_ids", "")
    if ids_str:
        return [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    # Фолбек на ADMIN_ID из переменных окружения
    import os
    admin_id = os.getenv("ADMIN_ID", "0")
    return [int(admin_id)] if admin_id.isdigit() and int(admin_id) > 0 else []

def get_notify_id() -> int:
    """ID кому приходят уведомления о новых записях."""
    notify_str = cfg("notify_id", "")
    if notify_str.strip().isdigit():
        return int(notify_str.strip())
    # Фолбек на первого админа
    ids = get_admin_ids()
    return ids[0] if ids else 0

# Загружаем конфиг при импорте модуля
load_config()
