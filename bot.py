#!/usr/bin/env python3
"""
Ramadan Prayer Times Bot for Astana, Kazakhstan
Telegram: @qwerty1341848_bot

Features:
- /start — subscribe + auto-notifications
- /today — today's prayer times
- /schedule — full Ramadan schedule
- /connect — link Google Calendar (auto-add events)
- /stop — unsubscribe from notifications
"""

import asyncio
import json
import os
import logging
from datetime import datetime, time as dt_time, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Config ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:8080/callback")
PORT = int(os.environ.get("PORT", 8080))

CITY = "Astana"
COUNTRY = "Kazakhstan"
METHOD = 3  # Muslim World League
HIJRI_YEAR = 1447
HIJRI_MONTH = 9  # Ramadan
TZ = ZoneInfo("Asia/Almaty")
API_BASE = "https://api.aladhan.com/v1"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

DATA_DIR = Path(__file__).parent
SUBS_FILE = DATA_DIR / "subscribers.json"
TOKENS_FILE = DATA_DIR / "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Persistence (subscribers + tokens) ───────────────────────────────
def load_json(path: Path) -> dict | list | set:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False))


def load_subs() -> set[int]:
    data = load_json(SUBS_FILE)
    return set(data) if isinstance(data, list) else set()


def save_subs(subs: set[int]):
    save_json(SUBS_FILE, list(subs))


def load_tokens() -> dict:
    data = load_json(TOKENS_FILE)
    return data if isinstance(data, dict) else {}


def save_tokens(tokens: dict):
    save_json(TOKENS_FILE, tokens)


subscribers = load_subs()


# ── Prayer Times API ─────────────────────────────────────────────────
def clean_time(raw: str) -> str:
    return raw.split(" ")[0].strip()


async def fetch_today() -> dict | None:
    date_str = datetime.now(TZ).strftime("%d-%m-%Y")
    url = f"{API_BASE}/timingsByCity/{date_str}"
    params = {"city": CITY, "country": COUNTRY, "method": METHOD}

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
    }


async def fetch_ramadan() -> list[dict]:
    url = f"{API_BASE}/hijriCalendarByCity/{HIJRI_YEAR}/{HIJRI_MONTH}"
    params = {"city": CITY, "country": COUNTRY, "method": METHOD}

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


def add_events_to_calendar(creds: Credentials, days: list[dict]) -> int:
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
            "description": f"Имсак: {day['imsak']}\nФаджр: {day['fajr']}\nДень {hd} Рамадана",
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
            "description": f"Магриб: {day['maghrib']}\nИша: {day['isha']}\nДень {hd} Рамадана",
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
    state = request.query.get("state")

    if not code or not state:
        return web.Response(
            text="<h2>Ошибка</h2><p>Попробуйте снова через /connect в боте.</p>",
            content_type="text/html",
        )

    try:
        flow = make_oauth_flow()
        await asyncio.to_thread(flow.fetch_token, code=code)
        creds = flow.credentials

        # Save token
        tokens = load_tokens()
        tokens[state] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        save_tokens(tokens)

        # Fetch remaining Ramadan days
        days = await fetch_ramadan()
        today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = [d for d in days if d["date"] >= today]
        target = remaining if remaining else days

        # Add events (sync call in thread)
        count = await asyncio.to_thread(add_events_to_calendar, creds, target)

        # Notify via Telegram
        bot = Bot(BOT_TOKEN)
        async with bot:
            await bot.send_message(
                int(state),
                f"Google Calendar подключен!\n\n"
                f"Добавлено {count} событий в ваш календарь:\n"
                f"- Сухур с напоминанием за 30 мин\n"
                f"- Ифтар с напоминанием за 15 мин\n\n"
                f"Откройте Google Calendar — всё уже там!"
            )

        return web.Response(
            text=(
                "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                "<h2>Готово!</h2>"
                "<p>Все события Рамадана добавлены в ваш Google Calendar.</p>"
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
                int(state),
                "Произошла ошибка при подключении. Попробуйте /connect ещё раз."
            )
        return web.Response(
            text="<h2>Ошибка</h2><p>Попробуйте снова.</p>",
            content_type="text/html",
        )


# ── Scheduled Notifications ──────────────────────────────────────────
async def send_morning(context):
    times = await fetch_today()
    if not times or times["hijri_month_number"] != 9:
        return

    text = (
        f"Доброе утро! День {times['hijri_day']} Рамадана\n\n"
        f"Саһарлық (имсак): {times['imsak']}\n"
        f"Фаджр: {times['fajr']}\n\n"
        f"Ифтар сегодня в {times['maghrib']}\n\n"
        f"Хорошего дня и лёгкого поста!"
    )

    for chat_id in list(subscribers):
        try:
            await context.bot.send_message(chat_id, text)
        except Exception:
            subscribers.discard(chat_id)
            save_subs(subscribers)

    logger.info(f"Morning notification sent to {len(subscribers)} subscribers")


async def send_evening(context):
    times = await fetch_today()
    if not times or times["hijri_month_number"] != 9:
        return

    text = (
        f"Скоро ифтар! День {times['hijri_day']} Рамадана\n\n"
        f"Ауызашар (магриб): {times['maghrib']}\n"
        f"Иша: {times['isha']}\n\n"
        f"Приятного ифтара!"
    )

    for chat_id in list(subscribers):
        try:
            await context.bot.send_message(chat_id, text)
        except Exception:
            subscribers.discard(chat_id)
            save_subs(subscribers)

    logger.info(f"Evening notification sent to {len(subscribers)} subscribers")


# ── Bot Handlers ─────────────────────────────────────────────────────
WELCOME = (
    "Ассаламу алейкум!\n"
    "Я — бот расписания Рамадана для Астаны.\n\n"
    "Вы подписаны на ежедневные уведомления!\n\n"
    "Команды:\n"
    "/today — время на сегодня\n"
    "/schedule — расписание всего Рамадана\n"
    "/connect — подключить Google Calendar\n"
    "/stop — отключить уведомления\n\n"
    "Рамадан мубарак!"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_subs(subscribers)
    logger.info(f"New subscriber: {chat_id} (total: {len(subscribers)})")
    await update.message.reply_text(WELCOME)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.discard(chat_id)
    save_subs(subscribers)
    await update.message.reply_text(
        "Уведомления отключены.\nЧтобы включить снова — /start"
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_CLIENT_ID:
        await update.message.reply_text("Google Calendar пока не настроен.")
        return

    chat_id = str(update.effective_chat.id)
    flow = make_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=chat_id,
    )

    keyboard = [[InlineKeyboardButton("Подключить Google Calendar", url=auth_url)]]

    await update.message.reply_text(
        "Нажмите кнопку ниже — откроется страница Google.\n"
        "Разрешите доступ к календарю.\n\n"
        "После этого все события Рамадана автоматически\n"
        "добавятся в ваш Google Calendar с напоминаниями!",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    times = await fetch_today()
    if not times:
        await update.message.reply_text("Не удалось получить данные. Попробуйте позже.")
        return

    is_ramadan = times["hijri_month_number"] == 9
    day_info = f" (день {times['hijri_day']} Рамадана)" if is_ramadan else ""

    lines = [
        f"{times['date']}{day_info}",
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
    msg = await update.message.reply_text("Загружаю расписание...")

    days = await fetch_ramadan()
    if not days:
        await msg.edit_text("Не удалось получить данные. Попробуйте позже.")
        return

    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    chunks = []
    current = "РАСПИСАНИЕ РАМАДАНА 2026\nг. Астана\n"

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
    await update.message.reply_text(WELCOME)


# ── Main ─────────────────────────────────────────────────────────────
async def post_init(app: Application):
    """Start the OAuth callback web server."""
    web_app = web.Application()
    web_app.router.add_get("/callback", oauth_callback)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    app.bot_data["web_runner"] = runner
    logger.info(f"OAuth web server started on port {PORT}")


async def post_shutdown(app: Application):
    """Stop the web server."""
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("help", cmd_help))

    # Daily notifications (Astana time)
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

    logger.info(f"Bot started! Subscribers: {len(subscribers)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
