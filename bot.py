#!/usr/bin/env python3
"""
Ramadan Prayer Times Bot for Kazakhstan
Telegram: @qwerty1341848_bot

Features:
- /start — subscribe + choose city
- /today — today's prayer times
- /schedule — full Ramadan schedule
- /connect — link Google Calendar (auto-add events)
- /city — change city
- /stop — unsubscribe from notifications
"""

import asyncio
import json
import os
import logging
from datetime import datetime, time as dt_time, timedelta
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ── Config ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set! Check Railway environment variables.")
    raise SystemExit("BOT_TOKEN is required")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:8080/callback")
PORT = int(os.environ.get("PORT", 8080))

COUNTRY = "Kazakhstan"
METHOD = 3  # Muslim World League
HIJRI_YEAR = 1447
HIJRI_MONTH = 9  # Ramadan
TZ = ZoneInfo("Asia/Almaty")
API_BASE = "https://api.aladhan.com/v1"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DEFAULT_CITY = "astana"

# ── Cities of Kazakhstan ─────────────────────────────────────────────
CITIES = {
    "astana": {"name": "Астана", "api": "Astana"},
    "almaty": {"name": "Алматы", "api": "Almaty"},
    "shymkent": {"name": "Шымкент", "api": "Shymkent"},
    "karaganda": {"name": "Караганда", "api": "Karaganda"},
    "aktobe": {"name": "Актобе", "api": "Aktobe"},
    "taraz": {"name": "Тараз", "api": "Taraz"},
    "pavlodar": {"name": "Павлодар", "api": "Pavlodar"},
    "ust-kamenogorsk": {"name": "Усть-Каменогорск", "api": "Ust-Kamenogorsk"},
    "semey": {"name": "Семей", "api": "Semey"},
    "atyrau": {"name": "Атырау", "api": "Atyrau"},
    "kostanay": {"name": "Костанай", "api": "Kostanay"},
    "petropavlovsk": {"name": "Петропавловск", "api": "Petropavlovsk"},
    "aktau": {"name": "Актау", "api": "Aktau"},
    "oral": {"name": "Уральск", "api": "Oral"},
    "kyzylorda": {"name": "Кызылорда", "api": "Kyzylorda"},
    "turkestan": {"name": "Туркестан", "api": "Turkestan"},
}

DATA_DIR = Path(__file__).parent
USERS_FILE = DATA_DIR / "users.json"
TOKENS_FILE = DATA_DIR / "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Persistence ──────────────────────────────────────────────────────
def load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False))


def load_users() -> dict:
    """Load users dict: {chat_id_str: {"city": "astana"}}"""
    data = load_json(USERS_FILE)
    # Migrate from old subscribers.json format (list of ints)
    if isinstance(data, list):
        migrated = {str(cid): {"city": DEFAULT_CITY} for cid in data}
        save_json(USERS_FILE, migrated)
        return migrated
    return data if isinstance(data, dict) else {}


def save_users(users: dict):
    save_json(USERS_FILE, users)


def load_tokens() -> dict:
    data = load_json(TOKENS_FILE)
    return data if isinstance(data, dict) else {}


def save_tokens(tokens: dict):
    save_json(TOKENS_FILE, tokens)


# Also migrate old subscribers.json on startup
def migrate_old_subs():
    old_file = DATA_DIR / "subscribers.json"
    if old_file.exists() and not USERS_FILE.exists():
        data = json.loads(old_file.read_text())
        if isinstance(data, list):
            migrated = {str(cid): {"city": DEFAULT_CITY} for cid in data}
            save_json(USERS_FILE, migrated)
            logger.info(f"Migrated {len(migrated)} subscribers to users.json")


migrate_old_subs()
users = load_users()


def get_user_city(chat_id: int) -> str:
    """Get user's city key, default to astana."""
    return users.get(str(chat_id), {}).get("city", DEFAULT_CITY)


def get_city_api(city_key: str) -> str:
    """Get API city name from key."""
    return CITIES.get(city_key, CITIES[DEFAULT_CITY])["api"]


def get_city_name(city_key: str) -> str:
    """Get display city name from key."""
    return CITIES.get(city_key, CITIES[DEFAULT_CITY])["name"]


# ── Prayer Times API ─────────────────────────────────────────────────
def clean_time(raw: str) -> str:
    return raw.split(" ")[0].strip()


async def fetch_today(city_key: str) -> dict | None:
    city_api = get_city_api(city_key)
    date_str = datetime.now(TZ).strftime("%d-%m-%Y")
    url = f"{API_BASE}/timingsByCity/{date_str}"
    params = {"city": city_api, "country": COUNTRY, "method": METHOD}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    d = data["data"]
    t = d["timings"]
    hijri = d["date"]["hijri"]

    return {
        "imsak": clean_time(t["Imsak"]),
        "fajr": clean_time(t["Fajr"]),
        "sunrise": clean_time(t["Sunrise"]),
        "dhuhr": clean_time(t["Dhuhr"]),
        "asr": clean_time(t["Asr"]),
        "maghrib": clean_time(t["Maghrib"]),
        "isha": clean_time(t["Isha"]),
        "hijri_day": hijri["day"],
        "hijri_month": hijri["month"]["en"],
        "hijri_month_number": int(hijri["month"]["number"]),
        "date": datetime.now(TZ).strftime("%d.%m.%Y"),
        "city_name": get_city_name(city_key),
    }


async def fetch_ramadan(city_key: str) -> list[dict]:
    city_api = get_city_api(city_key)
    url = f"{API_BASE}/hijriCalendarByCity/{HIJRI_YEAR}/{HIJRI_MONTH}"
    params = {"city": city_api, "country": COUNTRY, "method": METHOD}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

    results = []
    for day in data["data"]:
        t = day["timings"]
        g = day["date"]["gregorian"]
        h = day["date"]["hijri"]

        parts = g["date"].split("-")
        gdate = datetime(
            int(parts[2]), int(parts[1]), int(parts[0]), tzinfo=TZ
        )

        results.append({
            "date": gdate,
            "hijri_day": int(h["day"]),
            "imsak": clean_time(t["Imsak"]),
            "fajr": clean_time(t["Fajr"]),
            "maghrib": clean_time(t["Maghrib"]),
            "isha": clean_time(t["Isha"]),
        })

    return results


# ── Google Calendar ──────────────────────────────────────────────────
def make_oauth_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [CALLBACK_URL],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = CALLBACK_URL
    return flow


def add_events_to_calendar(creds: Credentials, days: list[dict], city_name: str) -> int:
    """Add Ramadan events to Google Calendar. Returns count of events added."""
    service = build("calendar", "v3", credentials=creds)
    count = 0

    for day in days:
        d = day["date"]
        hd = day["hijri_day"]

        # Suhoor
        ih, im = map(int, day["imsak"].split(":"))
        suhoor_start = d.replace(hour=ih, minute=im, second=0, microsecond=0)

        service.events().insert(calendarId="primary", body={
            "summary": f"Сухур (саһарлық) — день {hd}",
            "start": {"dateTime": suhoor_start.isoformat(), "timeZone": "Asia/Almaty"},
            "end": {"dateTime": (suhoor_start + timedelta(minutes=5)).isoformat(), "timeZone": "Asia/Almaty"},
            "description": f"г. {city_name}\nИмсак: {day['imsak']}\nФаджр: {day['fajr']}\nДень {hd} Рамадана",
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 30}],
            },
        }).execute()
        count += 1

        # Iftar
        mh, mm = map(int, day["maghrib"].split(":"))
        iftar_start = d.replace(hour=mh, minute=mm, second=0, microsecond=0)

        service.events().insert(calendarId="primary", body={
            "summary": f"Ифтар (ауызашар) — день {hd}",
            "start": {"dateTime": iftar_start.isoformat(), "timeZone": "Asia/Almaty"},
            "end": {"dateTime": (iftar_start + timedelta(minutes=30)).isoformat(), "timeZone": "Asia/Almaty"},
            "description": f"г. {city_name}\nМагриб: {day['maghrib']}\nИша: {day['isha']}\nДень {hd} Рамадана",
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 15}],
            },
        }).execute()
        count += 1

    return count


# ── OAuth Callback (Web Server) ─────────────────────────────────────
async def oauth_callback(request):
    """Handle Google OAuth2 callback."""
    code = request.query.get("code")
    state = request.query.get("state")  # format: "chat_id:city_key"

    if not code or not state:
        return web.Response(
            text="<h2>Ошибка</h2><p>Попробуйте снова через /connect в боте.</p>",
            content_type="text/html",
        )

    # Parse state
    parts = state.split(":", 1)
    chat_id_str = parts[0]
    city_key = parts[1] if len(parts) > 1 else DEFAULT_CITY

    try:
        flow = make_oauth_flow()
        await asyncio.to_thread(flow.fetch_token, code=code)
        creds = flow.credentials

        # Save token
        tokens = load_tokens()
        tokens[chat_id_str] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        save_tokens(tokens)

        # Fetch remaining Ramadan days for user's city
        days = await fetch_ramadan(city_key)
        today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = [d for d in days if d["date"] >= today]
        target = remaining if remaining else days

        city_name = get_city_name(city_key)

        # Add events (sync call in thread)
        count = await asyncio.to_thread(add_events_to_calendar, creds, target, city_name)

        # Notify via Telegram
        bot = Bot(BOT_TOKEN)
        async with bot:
            await bot.send_message(
                int(chat_id_str),
                f"Google Calendar подключен!\n\n"
                f"Город: {city_name}\n"
                f"Добавлено {count} событий в ваш календарь:\n"
                f"- Сухур с напоминанием за 30 мин\n"
                f"- Ифтар с напоминанием за 15 мин\n\n"
                f"Откройте Google Calendar — всё уже там!"
            )

        return web.Response(
            text=(
                "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                "<h2>Готово!</h2>"
                f"<p>Все события Рамадана ({city_name}) добавлены в ваш Google Calendar.</p>"
                "<p>Можете закрыть эту страницу и вернуться в Telegram.</p>"
                "</body></html>"
            ),
            content_type="text/html",
        )

    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        bot = Bot(BOT_TOKEN)
        async with bot:
            await bot.send_message(
                int(chat_id_str),
                "Произошла ошибка при подключении. Попробуйте /connect ещё раз."
            )
        return web.Response(
            text="<h2>Ошибка</h2><p>Попробуйте снова.</p>",
            content_type="text/html",
        )


# ── Scheduled Notifications ──────────────────────────────────────────
async def send_morning(context):
    if not users:
        return

    # Group users by city
    by_city = defaultdict(list)
    for cid, data in users.items():
        by_city[data.get("city", DEFAULT_CITY)].append(int(cid))

    for city_key, chat_ids in by_city.items():
        times = await fetch_today(city_key)
        if not times or times["hijri_month_number"] != 9:
            continue

        city_name = get_city_name(city_key)
        text = (
            f"Доброе утро! День {times['hijri_day']} Рамадана\n"
            f"г. {city_name}\n\n"
            f"Саһарлық (имсак): {times['imsak']}\n"
            f"Фаджр: {times['fajr']}\n\n"
            f"Ифтар сегодня в {times['maghrib']}\n\n"
            f"Хорошего дня и лёгкого поста!"
        )

        for chat_id in chat_ids:
            try:
                await context.bot.send_message(chat_id, text)
            except Exception:
                users.pop(str(chat_id), None)
                save_users(users)

    logger.info(f"Morning notification sent to {len(users)} users")


async def send_evening(context):
    if not users:
        return

    by_city = defaultdict(list)
    for cid, data in users.items():
        by_city[data.get("city", DEFAULT_CITY)].append(int(cid))

    for city_key, chat_ids in by_city.items():
        times = await fetch_today(city_key)
        if not times or times["hijri_month_number"] != 9:
            continue

        city_name = get_city_name(city_key)
        text = (
            f"Скоро ифтар! День {times['hijri_day']} Рамадана\n"
            f"г. {city_name}\n\n"
            f"Ауызашар (магриб): {times['maghrib']}\n"
            f"Иша: {times['isha']}\n\n"
            f"Приятного ифтара!"
        )

        for chat_id in chat_ids:
            try:
                await context.bot.send_message(chat_id, text)
            except Exception:
                users.pop(str(chat_id), None)
                save_users(users)

    logger.info(f"Evening notification sent to {len(users)} users")


# ── City Selection Keyboard ──────────────────────────────────────────
def city_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard with Kazakhstan cities (4 per row)."""
    keys = list(CITIES.keys())
    rows = []
    for i in range(0, len(keys), 4):
        row = []
        for k in keys[i:i + 4]:
            row.append(InlineKeyboardButton(
                CITIES[k]["name"],
                callback_data=f"city:{k}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ── Bot Handlers ─────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Subscribe with default city (will be updated on city selection)
    if str(chat_id) not in users:
        users[str(chat_id)] = {"city": DEFAULT_CITY}
        save_users(users)

    await update.message.reply_text(
        "Ассаламу алейкум!\n"
        "Я — бот расписания Рамадана для Казахстана.\n\n"
        "Выберите ваш город:",
        reply_markup=city_keyboard(),
    )


async def city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle city selection from inline keyboard."""
    query = update.callback_query
    await query.answer()

    city_key = query.data.split(":", 1)[1]
    chat_id = query.from_user.id

    if city_key not in CITIES:
        return

    users[str(chat_id)] = {"city": city_key}
    save_users(users)

    city_name = get_city_name(city_key)
    logger.info(f"User {chat_id} selected city: {city_name}")

    await query.edit_message_text(
        f"Город: {city_name}\n"
        f"Вы подписаны на ежедневные уведомления!\n\n"
        f"Команды:\n"
        f"/today — время на сегодня\n"
        f"/schedule — расписание всего Рамадана\n"
        f"/connect — подключить Google Calendar\n"
        f"/city — сменить город\n"
        f"/stop — отключить уведомления\n\n"
        f"Рамадан мубарак!"
    )


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_city_name(get_user_city(chat_id))
    await update.message.reply_text(
        f"Текущий город: {current}\n\nВыберите новый город:",
        reply_markup=city_keyboard(),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users.pop(chat_id, None)
    save_users(users)
    await update.message.reply_text(
        "Уведомления отключены.\nЧтобы включить снова — /start"
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_CLIENT_ID:
        await update.message.reply_text("Google Calendar пока не настроен.")
        return

    chat_id = update.effective_chat.id
    city_key = get_user_city(chat_id)
    city_name = get_city_name(city_key)

    # Encode chat_id and city in state
    state = f"{chat_id}:{city_key}"
    flow = make_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )

    keyboard = [[InlineKeyboardButton("Подключить Google Calendar", url=auth_url)]]

    await update.message.reply_text(
        f"Город: {city_name}\n\n"
        "Нажмите кнопку ниже — откроется страница Google.\n"
        "Разрешите доступ к календарю.\n\n"
        "После этого все события Рамадана автоматически\n"
        "добавятся в ваш Google Calendar с напоминаниями!",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city_key = get_user_city(chat_id)
    times = await fetch_today(city_key)

    if not times:
        await update.message.reply_text("Не удалось получить данные. Попробуйте позже.")
        return

    is_ramadan = times["hijri_month_number"] == 9
    day_info = f" (день {times['hijri_day']} Рамадана)" if is_ramadan else ""

    lines = [
        f"г. {times['city_name']} | {times['date']}{day_info}",
        "",
        f"Саһарлық (Имсак): {times['imsak']}",
        f"Фаджр: {times['fajr']}",
        f"Восход: {times['sunrise']}",
        f"Зухр: {times['dhuhr']}",
        f"Аср: {times['asr']}",
        f"Ауызашар (Магриб): {times['maghrib']}",
        f"Иша: {times['isha']}",
    ]

    if is_ramadan:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━",
            f"Прекращение еды: {times['imsak']}",
            f"Разговение: {times['maghrib']}",
        ]

    await update.message.reply_text("\n".join(lines))


WEEKDAYS_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
}
MONTHS_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city_key = get_user_city(chat_id)
    city_name = get_city_name(city_key)

    msg = await update.message.reply_text("Загружаю расписание...")

    days = await fetch_ramadan(city_key)
    if not days:
        await msg.edit_text("Не удалось получить данные. Попробуйте позже.")
        return

    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    chunks = []
    current = f"РАСПИСАНИЕ РАМАДАНА 2026\nг. {city_name}\n"

    for day in days:
        d = day["date"]
        wd = WEEKDAYS_RU[d.weekday()]
        mn = MONTHS_RU[d.month]
        marker = " <<< сегодня" if d.date() == today.date() else ""

        line = (
            f"\nДень {day['hijri_day']} | {wd}, {d.day} {mn}\n"
            f"  Сухур:  {day['imsak']}  |  Ифтар: {day['maghrib']}"
            f"{marker}\n"
        )

        if len(current) + len(line) > 4000:
            chunks.append(current)
            current = ""
        current += line

    if current:
        chunks.append(current)

    await msg.delete()
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city_name = get_city_name(get_user_city(chat_id))
    await update.message.reply_text(
        f"Бот расписания Рамадана для Казахстана\n"
        f"Ваш город: {city_name}\n\n"
        f"Команды:\n"
        f"/today — время на сегодня\n"
        f"/schedule — расписание всего Рамадана\n"
        f"/connect — подключить Google Calendar\n"
        f"/city — сменить город\n"
        f"/stop — отключить уведомления\n\n"
        f"Рамадан мубарак!"
    )


# ── Webhook Handler ──────────────────────────────────────────────────
async def webhook_handler(request):
    """Handle Telegram webhook updates via aiohttp."""
    app = request.app["telegram_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(status=200)


async def health_check(request):
    return web.Response(text="OK")


# ── Main ─────────────────────────────────────────────────────────────
async def main_async():
    """Run bot with webhook (no polling = no Conflict errors)."""
    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("city", cmd_city))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(city_callback, pattern=r"^city:"))

    # Initialize and start the application
    await app.initialize()
    await app.start()

    # Register bot commands
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Запуск и подписка"),
        BotCommand("today", "Время намаза на сегодня"),
        BotCommand("schedule", "Расписание всего Рамадана"),
        BotCommand("connect", "Подключить Google Calendar"),
        BotCommand("city", "Сменить город"),
        BotCommand("stop", "Отключить уведомления"),
        BotCommand("help", "Список команд"),
    ])
    logger.info("Bot commands registered")

    # Set webhook (this kills any polling connections!)
    base_url = CALLBACK_URL.rsplit("/callback", 1)[0]
    webhook_url = f"{base_url}/webhook"
    await app.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info(f"Webhook set to {webhook_url}")

    # Daily notifications
    app.job_queue.run_daily(
        send_morning,
        time=dt_time(hour=4, minute=0, tzinfo=TZ),
        name="morning",
    )
    app.job_queue.run_daily(
        send_evening,
        time=dt_time(hour=17, minute=0, tzinfo=TZ),
        name="evening",
    )

    # Start aiohttp web server (handles both webhook and OAuth callback)
    web_app = web.Application()
    web_app["telegram_app"] = app
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/callback", oauth_callback)
    web_app.router.add_get("/", health_check)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Bot started with webhook! Users: {len(users)}, Port: {PORT}")

    # Keep running forever
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
