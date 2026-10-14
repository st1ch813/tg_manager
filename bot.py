import html
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily-bot")

# ---------- налаштування (.env) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"]) if os.getenv("CHAT_ID") else None
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
SEND_TIME = os.getenv("SEND_TIME", "07:00")
CITY = os.getenv("CITY", "Білогородка")
STREET = os.getenv("STREET", "")
OUTAGE_GROUP = os.getenv("OUTAGE_GROUP", "")  # область: "4.2"; Київ: "42"
OUTAGE_SOURCE = os.getenv("OUTAGE_SOURCE", "prokyiv")  # prokyiv | yasno
OUTAGE_AREA = os.getenv("OUTAGE_AREA", "oblast")  # oblast | city (для prokyiv)
OUTAGE_URL = os.getenv("OUTAGE_URL", "https://pro.kyiv.ua/ru/grafik-vidklyuchennya-svitla-2/")
# 25 / 902 = ДТЕК Київські електромережі (Київ і великі населені пункти області)
YASNO_REGION_ID = os.getenv("YASNO_REGION_ID", "25")
YASNO_DSO_ID = os.getenv("YASNO_DSO_ID", "902")
YASNO_REGION_NAME = os.getenv("YASNO_REGION_NAME", "Київська")
YASNO_API = "https://app.yasno.ua/api/blackout-service/public/shutdowns"
_yasno_ids = None
OUTAGE_LINK = os.getenv("OUTAGE_LINK", "https://yasno.ua/schedule-turn-off-electricity")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_UA = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

WEATHER_UA = {
    0: "ясно ☀️", 1: "переважно ясно 🌤", 2: "мінлива хмарність ⛅", 3: "хмарно ☁️",
    45: "туман 🌫", 48: "паморозь 🌫",
    51: "легка мряка 🌦", 53: "мряка 🌦", 55: "сильна мряка 🌧",
    61: "невеликий дощ 🌦", 63: "дощ 🌧", 65: "сильний дощ 🌧",
    66: "крижаний дощ 🌧", 67: "сильний крижаний дощ 🌧",
    71: "невеликий сніг 🌨", 73: "сніг 🌨", 75: "сильний сніг ❄️", 77: "снігові зерна 🌨",
    80: "короткочасні дощі 🌦", 81: "зливи 🌧", 82: "сильні зливи ⛈",
    85: "снігопад 🌨", 86: "сильний снігопад ❄️",
    95: "гроза ⛈", 96: "гроза з градом ⛈", 99: "сильна гроза з градом ⛈",
}
RAIN_CODES = set(range(51, 68)) | {80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}

esc = html.escape
_geo_cache: dict = {}


# ---------- розклад уроків ----------
def _fmt_dur(seconds: float) -> str:
    total = max(1, int((seconds + 59) // 60))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def _lesson_bounds(lesson: dict, now: datetime):
    m = TIME_RE.search(lesson.get("time", ""))
    if not m:
        return None

    def to_dt(t: str) -> datetime:
        hh, mm = map(int, t.split(":"))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    return to_dt(m.group(1)), to_dt(m.group(2))


def lessons_text(now: datetime, with_remaining: bool = False) -> str:
    data = json.loads(Path(__file__).with_name("schedule.json").read_text(encoding="utf-8"))
    day = data.get(DAYS[now.weekday()], [])
    if not day:
        return "📚 <b>Розклад:</b> сьогодні уроків немає 🎉"

    nums = [lesson.get("n", i) for i, lesson in enumerate(day, 1)]
    bounds = [_lesson_bounds(lesson, now) for lesson in day]

    current, status = None, None
    if with_remaining:
        for i, b in enumerate(bounds):
            if b and b[0] <= now < b[1]:
                current = i
                status = (
                    f"⏱ Зараз {nums[i]}-й урок ({esc(day[i]['subject'])}). "
                    f"До кінця: <b>{_fmt_dur((b[1] - now).total_seconds())}</b>"
                )
                break
        else:
            upcoming = [(i, b) for i, b in enumerate(bounds) if b and now < b[0]]
            if not upcoming:
                status = "✅ Уроки на сьогодні вже закінчились."
            else:
                i, b = upcoming[0]
                first_started = any(x and x[0] <= now for x in bounds)
                label = "☕ Перерва" if first_started else "🕗 Уроки ще не почались"
                status = (
                    f"{label}. До {nums[i]}-го уроку ({esc(day[i]['subject'])}): "
                    f"<b>{_fmt_dur((b[0] - now).total_seconds())}</b>"
                )

    lines = []
    for i, lesson in enumerate(day):
        mark = "▶️ " if i == current else ""
        lines.append(f"{mark}{nums[i]}. {esc(lesson.get('time', ''))} — {esc(lesson['subject'])}")
    text = "📚 <b>Розклад на сьогодні:</b>\n" + "\n".join(lines)
    return text + ("\n\n" + status if status else "")


# ---------- погода + що вдягнути ----------
async def geocode(client: httpx.AsyncClient, city: str):
    if city not in _geo_cache:
        r = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 10, "language": "uk", "countryCode": "UA"},
        )
        r.raise_for_status()
        results = r.json().get("results")
        if not results:
            raise ValueError(f"Не знайшов місто: {city}")
        # перевага — населеному пункту саме Київської області
        best = next((x for x in results if "київ" in (x.get("admin1") or "").lower()), results[0])
        _geo_cache[city] = (best["latitude"], best["longitude"], best["name"])
    return _geo_cache[city]


def outfit_advice(feels_min: float, feels_max: float, rain_prob: float, wind: float, code: int) -> str:
    t = (feels_min + feels_max) / 2
    if t <= -10:
        base = "дуже тепла зимова куртка, шапка, шарф, рукавиці, термобілизна"
    elif t <= 0:
        base = "зимова куртка, шапка, шарф і рукавиці"
    elif t <= 8:
        base = "тепла куртка або пальто, шапка/капюшон, закрите взуття"
    elif t <= 14:
        base = "легка куртка або вітровка + светр чи худі"
    elif t <= 19:
        base = "худі або кофта, легка куртка про всяк випадок"
    elif t <= 25:
        base = "футболка/сорочка, можна взяти тонку кофту на вечір"
    else:
        base = "легкий одяг, головний убір, вода з собою"

    extras = []
    if code in SNOW_CODES:
        extras.append("взуття, яке не промокає й не ковзає")
    if code in RAIN_CODES or rain_prob >= 50:
        extras.append("парасолька або дощовик")
    if wind >= 10:
        extras.append("щось вітрозахисне")
    if feels_max - feels_min >= 10:
        extras.append("одягайся шарами — вдень значно тепліше, ніж зранку")

    text = "👕 <b>Що вдягнути:</b> " + base
    if extras:
        text += "\n➕ " + "; ".join(extras)
    return text


async def weather_text(client: httpx.AsyncClient) -> str:
    try:
        lat, lon, name = await geocode(client, CITY)
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "timezone": str(TZ.key),
                "forecast_days": 1,
                "wind_speed_unit": "ms",
                "current": "temperature_2m,apparent_temperature,weather_code",
                "daily": ",".join([
                    "temperature_2m_max", "temperature_2m_min",
                    "apparent_temperature_max", "apparent_temperature_min",
                    "precipitation_probability_max", "wind_speed_10m_max", "weather_code",
                ]),
            },
        )
        r.raise_for_status()
        d = r.json()
        cur, day = d["current"], d["daily"]
        code = int(day["weather_code"][0])
        rain = day["precipitation_probability_max"][0] or 0
        wind = day["wind_speed_10m_max"][0] or 0
        tmin, tmax = day["temperature_2m_min"][0], day["temperature_2m_max"][0]

        text = (
            f"🌡 <b>Погода — {esc(name)}:</b>\n"
            f"Зараз: {cur['temperature_2m']:+.0f}°C (відчувається як {cur['apparent_temperature']:+.0f}°C)\n"
            f"Протягом дня: від {tmin:+.0f}°C до {tmax:+.0f}°C, {WEATHER_UA.get(code, 'без опису')}\n"
            f"Ймовірність опадів: {rain:.0f}%, вітер до {wind:.0f} м/с\n\n"
        )
        text += outfit_advice(
            day["apparent_temperature_min"][0], day["apparent_temperature_max"][0], rain, wind, code
        )
        return text
    except Exception:
        log.exception("weather failed")
        return "🌡 <b>Погода:</b> не вдалося отримати прогноз ⚠️"


# ---------- графік відключень світла ----------
def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _name(item: dict) -> str:
    return str(item.get("value") or item.get("name") or item.get("title") or "")


async def fetch_yasno_regions(client: httpx.AsyncClient) -> list:
    r = await client.get(f"{YASNO_API}/addresses/v2/regions")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("regions", [])


async def resolve_yasno_ids(client: httpx.AsyncClient):
    """Повертає (region_id, dso_id): з .env або автоматично за назвою області."""
    global _yasno_ids
    if YASNO_REGION_ID and YASNO_DSO_ID:
        return YASNO_REGION_ID, YASNO_DSO_ID
    if _yasno_ids:
        return _yasno_ids
    for region in await fetch_yasno_regions(client):
        if YASNO_REGION_NAME.lower() in _name(region).lower():
            dsos = region.get("dsos") or []
            if not dsos:
                continue
            dso = next((d for d in dsos if "регіон" in _name(d).lower()), dsos[0])
            _yasno_ids = (region["id"], dso["id"])
            log.info("Yasno: region=%s dso=%s", *_yasno_ids)
            return _yasno_ids
    raise RuntimeError(f"Yasno: не знайшов регіон «{YASNO_REGION_NAME}» (дивись команду /regions)")


LABEL = r"(?:Очередь|Черга|Группа|Група)"
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})")
CITY_RE = re.compile(
    r"(?:отключений\s+в\s+Киеве|відключень\s+(?:у|в)\s+Києві)\s+на\s+([^\n]+)", re.I)
OBL_RE = re.compile(
    r"(?:отключений\s+в\s+Киевской\s+области|відключень\s+(?:у|в)\s+Київській\s+області)\s+на\s+([^\n]+)",
    re.I)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"<[^>]+>", "\n", raw))


async def prokyiv_outages_text(client: httpx.AsyncClient) -> str:
    """Графік ДТЕК зі сторінки pro.kyiv.ua (Київ — групи 1-60, область — черги 1.1-6.2)."""
    head = "💡 <b>Світло" + (f" — {esc(STREET)}" if STREET else "") + "</b>"
    if not OUTAGE_GROUP:
        return head + "\nЧерга не вказана (змінна OUTAGE_GROUP у .env)."
    area = "Київська область" if OUTAGE_AREA == "oblast" else "Київ"
    try:
        r = await client.get(OUTAGE_URL, headers={"User-Agent": BROWSER_UA}, follow_redirects=True)
        r.raise_for_status()
        text = _page_text(r.text)
        city, obl = CITY_RE.search(text), OBL_RE.search(text)
        if OUTAGE_AREA == "oblast":
            m, section = obl, (text[obl.start():] if obl else "")
        else:
            m = city
            end = obl.start() if obl and city and obl.start() > city.start() else len(text)
            section = text[city.start():end] if city else ""
        if not m:
            raise ValueError("не знайшов потрібний розділ на сторінці")
        day = m.group(1).strip(" *")

        grp = re.search(rf"{LABEL}\s+{re.escape(OUTAGE_GROUP)}\s*:", section)
        head += f" ({esc(area)}, черга {esc(OUTAGE_GROUP)}, {esc(day)})"
        if not grp:
            return head + "\n⚠️ Такої черги не знайшов на сторінці. Перевір OUTAGE_GROUP та OUTAGE_AREA у .env."
        tail = section[grp.end():]
        nxt = re.search(rf"{LABEL}\s+[\d.]+", tail)
        chunk = tail[: nxt.start()] if nxt else tail[:300]
        slots = [f"{a}–{b}" for a, b in TIME_RE.findall(chunk)]
        upd = re.search(r"(?:Обновлено|Оновлено)\s*([\d.]+\s+[\d:]+)", section)
        footer = f"\n🕒 Дані ДТЕК, оновлено {esc(upd.group(1))}" if upd else ""
        if not slots:
            return head + "\n✅ За графіком відключень немає." + footer
        return head + "\n" + "\n".join(f"🔌 {s}" for s in slots) + footer
    except Exception as e:
        log.exception("prokyiv outages failed")
        reason = f"{type(e).__name__}: {str(e)[:150]}"
        return head + f"\n⚠️ Не вдалося отримати графік ({esc(reason)}). Перевір вручну: {esc(OUTAGE_URL)}"


async def outages_text(client: httpx.AsyncClient) -> str:
    if OUTAGE_SOURCE == "yasno":
        return await yasno_outages_text(client)
    return await prokyiv_outages_text(client)


async def yasno_outages_text(client: httpx.AsyncClient) -> str:
    head = "💡 <b>Світло" + (f" — {esc(STREET)}" if STREET else "") + "</b>"
    if not OUTAGE_GROUP:
        return head + "\nЧерга не вказана (змінна OUTAGE_GROUP у .env)."
    try:
        region, dso = await resolve_yasno_ids(client)
        r = await client.get(f"{YASNO_API}/regions/{region}/dsos/{dso}/planned-outages")
        r.raise_for_status()
        data = r.json()
        if OUTAGE_GROUP not in data:
            known = ", ".join(sorted(data)[:12])
            return head + f"\n⚠️ Черги {esc(OUTAGE_GROUP)} немає у відповіді Yasno. Є, наприклад: {esc(known)}…"
        day = data[OUTAGE_GROUP]["today"]
        status = day.get("status")
        if status == "EmergencyShutdowns":
            return head + "\n🚨 Екстрені відключення, графіки сьогодні не діють."
        if status == "WaitingForSchedule":
            return head + "\n⏳ Графік на сьогодні ще не опублікований."
        slots = [s for s in day.get("slots", []) if s.get("type") == "Definite"]
        head += f" (черга {esc(OUTAGE_GROUP)})"
        if not slots:
            return head + "\n✅ За графіком відключень сьогодні немає."
        rows = [f"🔌 {fmt_min(s['start'])}–{fmt_min(s['end'])}" for s in slots]
        return head + "\n" + "\n".join(rows)
    except Exception as e:
        log.exception("outages failed")
        reason = f"{type(e).__name__}: {str(e)[:150]}"
        return head + f"\n⚠️ Не вдалося отримати графік ({esc(reason)}). Перевір вручну: {esc(OUTAGE_LINK)}"


# ---------- збірка повідомлень ----------
async def build_messages() -> list:
    """Повертає два повідомлення: [розклад + світло, погода + що вдягнути]."""
    now = datetime.now(TZ)
    async with httpx.AsyncClient(timeout=15) as client:
        first = "\n\n".join([
            f"☀️ <b>Доброго ранку! {now:%d.%m.%Y}, {WEEKDAY_UA[now.weekday()]}</b>",
            lessons_text(now),
            await outages_text(client),
        ])
        second = await weather_text(client)
    return [first, second]


def allowed(update: Update) -> bool:
    ok = CHAT_ID is None or update.effective_chat.id == CHAT_ID
    if not ok:
        log.warning("Ігнорую чат %s: не збігається з CHAT_ID=%s", update.effective_chat.id, CHAT_ID)
    return ok


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Помилка в обробнику", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(f"⚠️ Помилка: {ctx.error}")


# ---------- команди ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привіт! Твій chat id: {update.effective_chat.id}\n"
        "Встав його в .env як CHAT_ID і перезапусти бота.\n\n"
        "Команди: /today — зведення зараз, /schedule — розклад і скільки до кінця уроку, "
        "/weather — погода, /light — світло."
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        for part in await build_messages():
            await update.message.reply_text(part, parse_mode=ParseMode.HTML)


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        text = lessons_text(datetime.now(TZ), with_remaining=True)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_weather(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        async with httpx.AsyncClient(timeout=15) as c:
            await update.message.reply_text(await weather_text(c), parse_mode=ParseMode.HTML)


async def cmd_light(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        async with httpx.AsyncClient(timeout=15) as c:
            await update.message.reply_text(await outages_text(c), parse_mode=ParseMode.HTML)


async def cmd_regions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показує регіони та оператори, які віддає Yasno (щоб підібрати ID вручну)."""
    if not allowed(update):
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            regions = await fetch_yasno_regions(c)
        lines = []
        for rg in regions:
            lines.append(f"{rg.get('id')}: {_name(rg)}")
            for d in rg.get("dsos") or []:
                lines.append(f"    dso {d.get('id')}: {_name(d)}")
        await update.message.reply_text("\n".join(lines) or "Порожня відповідь")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID is None:
        log.warning("CHAT_ID не заданий — нікуди надсилати")
        return
    for part in await build_messages():
        await ctx.bot.send_message(CHAT_ID, part, parse_mode=ParseMode.HTML)


class _Ping(BaseHTTPRequestHandler):
    """Мінімальний веб-сервер: відповідає "ok", щоб безкоштовний Render не присипляв бота."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_ping_server():
    port = os.getenv("PORT")  # Render виставляє PORT сам; на комп'ютері змінної немає
    if not port:
        return
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _Ping)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Ping-сервер слухає порт %s", port)


def main():
    start_ping_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler(["schedule", "rozklad", "lessons"], cmd_schedule))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("light", cmd_light))
    app.add_handler(CommandHandler("regions", cmd_regions))

    hh, mm = map(int, SEND_TIME.split(":"))
    app.job_queue.run_daily(daily_job, time=time(hh, mm, tzinfo=TZ), name="daily-digest")
    log.info("Бот запущено, щоденна відправка о %s (%s)", SEND_TIME, TZ.key)
    app.run_polling()


if __name__ == "__main__":
    main()
