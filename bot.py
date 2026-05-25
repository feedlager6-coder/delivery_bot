import os
import re
import math
import signal
import asyncio
import logging
import json
import urllib.request
import urllib.parse
import sqlite3
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    PicklePersistence,
    filters,
    ContextTypes,
)
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment & Constants ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
YANDEX_KEY = os.environ.get("YANDEX_GEOCODER_API_KEY") or os.environ.get("YANDEX_KEY")
GRAPHHOPPER_KEY = os.environ.get("GRAPHHOPPER_API_KEY")

# Economics (Дагестан)
AVG_FUEL_CONSUMPTION = 10.0
FUEL_PRICE = 75.0
AVG_SPEED_KMH = 30

# States
WAITING_FOR_START, WAITING_FOR_DELIVERY, CONFIRM_START, WAITING_FOR_COURIERS, WAITING_FOR_PREFS = range(1, 6)

# --- DB Setup ---
DB_NAME = 'delivery_bot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''CREATE TABLE IF NOT EXISTS routes 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, timestamp TEXT, 
        dist REAL, saved_rub REAL, count INTEGER)''')
    conn.commit()
    conn.close()

# --- Geocoding & Parser ---
geocode_cache = {}

def get_address(lat, lon):
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in geocode_cache: return geocode_cache[key]

    if YANDEX_KEY:
        try:
            url = f"https://geocode-maps.yandex.ru/1.x/?apikey={YANDEX_KEY}&geocode={lon},{lat}&format=json&results=1&lang=ru_RU"
            with urllib.request.urlopen(url, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
                members = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
                if members:
                    name = members[0].get("GeoObject", {}).get("metaDataProperty", {}).get("GeocoderMetaData", {}).get("text", "")
                    res = ", ".join(name.split(", ")[-2:]) if len(name.split(", ")) >= 2 else name
                    geocode_cache[key] = res
                    return res
        except: pass

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language=ru"
        req = urllib.request.Request(url, headers={"User-Agent": "DeliveryBot/2.0"})
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            addr = data.get("address", {})
            res = f"{addr.get('road', '')}, {addr.get('house_number', '')}".strip(", ")
            if not res: res = data.get("display_name", "").split(",")[0]
            geocode_cache[key] = res
            return res
    except: pass
    return f"{lat:.5f}, {lon:.5f}"

def expand_short_url(url):
    if "maps/-/" not in url: return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.build_opener(urllib.request.HTTPRedirectHandler()).open(req, timeout=5).geturl()
    except: return url

def parse_yandex_link(url):
    try:
        url = expand_short_url(url)
        decoded = urllib.parse.unquote(url)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(decoded).query)
        for key in ["whatshere[point]", "ll"]:
            if key in params:
                lon, lat = params[key][0].split(",")
                return float(lat), float(lon)
        m = re.search(r'point=([\d\.]+),([\d\.]+)', decoded)
        if m: return float(m.group(2)), float(m.group(1))
        if "/maps/org/" in url:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read(500000).decode("utf-8", errors="ignore")
                m = re.search(r'"center":\[(-?\d+\.?\d*),(-?\d+\.?\d*)\]', body)
                if m: return float(m.group(2)), float(m.group(1))
    except: pass
    return None

# --- Routing Engine ---
async def get_gh_route(points, prefs):
    if not GRAPHHOPPER_KEY: return None
    gh_points = [[p[1], p[0]] for p in points]
    custom_model = {"priority": [], "turn_penalty": []}
    if prefs.get('right_turn', True):
        custom_model["turn_penalty"].append({"if": "change_angle >= 60", "add": "150"})
    if prefs.get('avoid_bad', True):
        custom_model["priority"].append({"if": "surface == UNPAVED || road_class == TRACK", "multiply_by": "0.1"})
    body = {
        "points": gh_points, "profile": "car", "locale": "ru",
        "instructions": False, "calc_points": False, "ch.disable": True,
        "custom_model": custom_model
    }
    try:
        url = f"https://graphhopper.com/api/1/route?key={GRAPHHOPPER_KEY}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            path = data["paths"][0]
            return path["distance"]/1000, path["time"]/60000
    except: return None

# --- Keyboards ---
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Начать маршрут", callback_data="start_route")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="prefs"), InlineKeyboardButton("📊 Отчеты", callback_data="reports")],
        [InlineKeyboardButton("📖 Помощь", callback_data="how_to")]
    ])

def route_done_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Новый маршрут", callback_data="new"), InlineKeyboardButton("🏠 Изменить старт", callback_data="changehome")],
        [InlineKeyboardButton("📊 Статистика", callback_data="reports")]
    ])

def prefs_kb(p):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Правые повороты: {'✅' if p.get('right_turn') else '❌'}", callback_data="toggle_rt")],
        [InlineKeyboardButton(f"Избегать плохих дорог: {'✅' if p.get('avoid_bad') else '❌'}", callback_data="toggle_bad")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ])

# --- Handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['prefs'] = {'right_turn': True, 'avoid_bad': True}
    await update.message.reply_text(
        "👋 <b>Привет! Я профессиональный бот-логист.</b>\n\n"
        "Я экономлю ваши деньги, используя алгоритмы UPS:\n"
        "• <b>Приоритизация правых поворотов</b>\n"
        "• <b>Исключение плохих дорог</b>\n"
        "• <b>Бизнес-аналитика</b>\n\n"
        "Нажми кнопку ниже, чтобы начать! 👇",
        parse_mode="HTML", reply_markup=main_menu_kb()
    )
    return WAITING_FOR_START

async def handle_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coord = parse_yandex_link(update.message.text)
    if coord:
        context.user_data["старт"] = coord
        addr = get_address(*coord)
        await update.message.reply_text(f"📍 Старт: <b>{addr}</b>\nТеперь отправляй точки доставки. Когда закончишь — напиши <b>Готово</b>.", parse_mode="HTML")
        return WAITING_FOR_DELIVERY
    await update.message.reply_text("❌ Не распознал ссылку. Попробуй еще раз.")
    return WAITING_FOR_START

async def handle_delivery_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("готово", "готов", "done", "го"):
        if not context.user_data.get("deliveries"):
            await update.message.reply_text("⚠️ Добавь хотя бы одну точку!")
            return WAITING_FOR_DELIVERY
        await update.message.reply_text("👥 Сколько курьеров? (1-10)")
        return WAITING_FOR_COURIERS
    coord = parse_yandex_link(update.message.text)
    if coord:
        context.user_data.setdefault("deliveries", []).append(coord)
        addr = get_address(*coord)
        context.user_data.setdefault("delivery_addresses", []).append(addr)
        await update.message.reply_text(f"✅ Добавлено: <b>{addr}</b>\nЕще или <b>Готово</b>?", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Ошибка в ссылке.")
    return WAITING_FOR_DELIVERY

async def finish_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        num = int(update.message.text)
        if not (1 <= num <= 10): raise ValueError
    except:
        await update.message.reply_text("Напиши число от 1 до 10.")
        return WAITING_FOR_COURIERS
    msg = await update.message.reply_text("⚙️ <b>Оптимизирую маршрут...</b>", parse_mode="HTML")
    start_pt, dels, addrs, prefs = context.user_data["старт"], context.user_data["deliveries"], context.user_data["delivery_addresses"], context.user_data["prefs"]
    chunk = len(dels) // num + (1 if len(dels)%num > 0 else 0)
    total_km_all, blocks = 0, []
    for i in range(num):
        batch = dels[i*chunk : (i+1)*chunk]
        batch_addrs = addrs[i*chunk : (i+1)*chunk]
        if not batch: break
        pts = [start_pt] + batch
        n = len(pts)
        manager = pywrapcp.RoutingIndexManager(n, 1, 0)
        routing = pywrapcp.RoutingModel(manager)
        def d_fn(f, t):
            p1, p2 = pts[manager.IndexToNode(f)], pts[manager.IndexToNode(t)]
            return int(math.hypot(p1[0]-p2[0], p1[1]-p2[1]) * 100000)
        routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(d_fn))
        sol = routing.SolveWithParameters(pywrapcp.DefaultRoutingSearchParameters())
        order = []
        if sol:
            idx = routing.Start(0)
            while not routing.IsEnd(idx):
                order.append(manager.IndexToNode(idx)); idx = sol.Value(routing.NextVar(idx))
        ordered_pts = [pts[idx] for idx in order]
        gh_res = await get_gh_route(ordered_pts, prefs)
        dist, _ = gh_res if gh_res else (len(batch)*3, 0)
        total_km_all += dist
        numbered = "\n".join([f"{j}. {batch_addrs[idx-1]}" for j, idx in enumerate(order[1:], 1)])
        y_url = f"https://yandex.ru/maps/?rtext={'~'.join([f'{p[0]},{p[1]}' for p in ordered_pts])}&rtt=auto"
        blocks.append(f"🚗 <b>Курьер {i+1}</b> ({dist:.1f} км):\n{numbered}\n🗺 <a href='{y_url}'>Навигатор</a>")
    saved_rub = (len(dels) * 3.5 - total_km_all) * (AVG_FUEL_CONSUMPTION/100) * FUEL_PRICE
    res_txt = "🚀 <b>Готово!</b>\n\n" + "\n\n".join(blocks) + f"\n\n💰 Экономия: <b>~{max(0, int(saved_rub))} руб.</b>"
    await msg.edit_text(res_txt, parse_mode="HTML", reply_markup=route_done_kb(), disable_web_page_preview=True)
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO routes (user_id, timestamp, dist, saved_rub, count) VALUES (?, ?, ?, ?, ?)", (update.effective_user.id, datetime.now().isoformat(), total_km_all, saved_rub, len(dels)))
        conn.commit(); conn.close()
    except: pass
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "start_route":
        await query.message.reply_text("📍 Отправь ссылку на <b>Старт</b>.", parse_mode="HTML"); return WAITING_FOR_START
    elif query.data == "prefs":
        p = context.user_data.setdefault('prefs', {'right_turn': True, 'avoid_bad': True})
        await query.message.edit_text("⚙️ <b>Настройки:</b>", reply_markup=prefs_kb(p), parse_mode="HTML"); return WAITING_FOR_PREFS
    elif query.data.startswith("toggle_"):
        p = context.user_data['prefs']
        if query.data == "toggle_rt": p['right_turn'] = not p['right_turn']
        if query.data == "toggle_bad": p['avoid_bad'] = not p['avoid_bad']
        await query.message.edit_reply_markup(reply_markup=prefs_kb(p)); return WAITING_FOR_PREFS
    elif query.data == "reports":
        conn = sqlite3.connect(DB_NAME); res = conn.execute("SELECT SUM(dist), SUM(saved_rub), SUM(count) FROM routes WHERE user_id = ?", (update.effective_user.id,)).fetchone(); conn.close()
        txt = f"📊 <b>Отчеты:</b>\n\n• Пробег: {res[0]:.1f} км\n• Экономия: {int(res[1])} руб\n• Доставок: {res[2]}" if res[0] else "Нет данных."
        await query.message.reply_text(txt, parse_mode="HTML"); return ConversationHandler.END
    elif query.data == "back_to_main":
        await query.message.edit_text("Меню:", reply_markup=main_menu_kb()); return WAITING_FOR_START
    return ConversationHandler.END

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).persistence(PicklePersistence(filepath="bot_persistence.pkl")).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CallbackQueryHandler(button_handler)],
        states={
            WAITING_FOR_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_link), CallbackQueryHandler(button_handler)],
            WAITING_FOR_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delivery_link), CallbackQueryHandler(button_handler)],
            WAITING_FOR_COURIERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_route)],
            WAITING_FOR_PREFS: [CallbackQueryHandler(button_handler)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        name="premium_conv", persistent=True
    )
    app.add_handler(conv); app.run_polling()

if __name__ == "__main__":
    main()