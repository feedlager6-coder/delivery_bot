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

# Логирование
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Настройки из переменных окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Поддержка обоих имен переменной для Яндекса
YANDEX_API_KEY = os.environ.get("YANDEX_GEOCODER_API_KEY") or os.environ.get("YANDEX_KEY")
GRAPHHOPPER_API_KEY = os.environ.get("GRAPHHOPPER_API_KEY")

# Параметры экономики (Дагестан)
FUEL_CONSUMPTION = 10.0  # л/100км
FUEL_PRICE = 75.0        # руб/л
AVG_SPEED = 30           # км/ч

# Состояния диалога
WAITING_FOR_START, WAITING_FOR_DELIVERY, CONFIRM_START, WAITING_FOR_COURIERS, WAITING_FOR_PREFS = range(1, 6)

# --- База данных ---
DB_NAME = 'delivery_bot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''CREATE TABLE IF NOT EXISTS routes 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, timestamp TEXT, 
        dist REAL, saved_rub REAL, count INTEGER)''')
    conn.commit()
    conn.close()

# --- Геокодирование (с защитой от зависаний) ---
geocode_cache = {}

def get_address(lat, lon):
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in geocode_cache: return geocode_cache[key]

    # 1. Пробуем Яндекс (быстро и точно)
    if YANDEX_API_KEY:
        try:
            url = f"https://geocode-maps.yandex.ru/1.x/?apikey={YANDEX_API_KEY}&geocode={lon},{lat}&format=json&results=1"
            with urllib.request.urlopen(url, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
                addr = data['response']['GeoObjectCollection']['featureMember'][0]['GeoObject']['metaDataProperty']['GeocoderMetaData']['text']
                res = ", ".join(addr.split(", ")[-2:])
                geocode_cache[key] = res
                return res
        except: pass

    # 2. Запасной Nominatim (с коротким тайм-аутом)
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            res = data.get("display_name", "").split(",")[0]
            geocode_cache[key] = res
            return res
    except: pass

    return f"{lat:.5f}, {lon:.5f}"

# --- Маршрутизация GraphHopper ---
async def get_gh_route(points, prefs):
    if not GRAPHHOPPER_API_KEY: return None
    
    gh_points = [[p[1], p[0]] for p in points]
    custom_model = {"priority": [], "turn_penalty": []}
    
    if prefs.get('right_turn'):
        custom_model["turn_penalty"].append({"if": "change_angle >= 60", "add": "120"}) # Штраф за левый поворот
    if prefs.get('avoid_bad'):
        custom_model["priority"].append({"if": "surface == UNPAVED || road_class == TRACK", "multiply_by": "0.1"})

    body = {
        "points": gh_points, "profile": "car", "locale": "ru",
        "instructions": False, "calc_points": False, "ch.disable": True,
        "custom_model": custom_model
    }
    
    try:
        url = f"https://graphhopper.com/api/1/route?key={GRAPHHOPPER_API_KEY}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            path = data["paths"][0]
            return path["distance"]/1000, path["time"]/60000
    except: return None

# --- Парсинг ссылок ---
def parse_link(url):
    try:
        if "maps/-/" in url: # Раскрываем короткие ссылки
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            url = urllib.request.build_opener(urllib.request.HTTPRedirectHandler()).open(req, timeout=5).geturl()
        
        decoded = urllib.parse.unquote(url)
        # Ищем координаты в разных форматах
        m = re.search(r'point=([\d\.]+),([\d\.]+)', decoded) or re.search(r'll=([\d\.]+),([\d\.]+)', decoded)
        if m: return float(m.group(2)), float(m.group(1))
        
        m = re.search(r'whatshere%5Bpoint%5D=([\d\.]+),([\d\.]+)', url) or re.search(r'whatshere<LaTex>\[point\]</LaTex>=([\d\.]+),([\d\.]+)', decoded)
        if m: return float(m.group(2)), float(m.group(1))
    except: pass
    return None

# --- Обработчики команд ---
async def start(update, context):
    context.user_data.clear()
    # Настройки по умолчанию
    context.user_data['prefs'] = {'right_turn': True, 'avoid_bad': True}
    await update.message.reply_text(
        "🚀 <b>Бот-логист готов!</b>\n\n"
        "Я приоритизирую правые повороты и избегаю плохих дорог.\n"
        "Отправь ссылку на <b>Старт</b> (склад/дом).",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Настройки", callback_data="prefs")]])
    )
    return WAITING_FOR_START

async def handle_start(update, context):
    coord = parse_link(update.message.text)
    if coord:
        context.user_data["start"] = coord
        addr = get_address(*coord)
        await update.message.reply_text(f"📍 Старт: <b>{addr}</b>\nТеперь кидай точки доставки по одной. В конце напиши <b>Готово</b>.", parse_mode="HTML")
        return WAITING_FOR_DELIVERY
    await update.message.reply_text("❌ Не вижу координат в ссылке. Попробуй еще раз.")
    return WAITING_FOR_START

async def handle_delivery(update, context):
    text = update.message.text.strip().lower()
    if text in ("готово", "го", "done"):
        if not context.user_data.get("dels"):
            await update.message.reply_text("Сначала добавь точки!")
            return WAITING_FOR_DELIVERY
        await update.message.reply_text("Сколько курьеров? (1-10)")
        return WAITING_FOR_COURIERS
    
    coord = parse_link(update.message.text)
    if coord:
        context.user_data.setdefault("dels", []).append(coord)
        addr = get_address(*coord)
        context.user_data.setdefault("addrs", []).append(addr)
        await update.message.reply_text(f"✅ Добавлено: {addr}\nЕще или <b>Готово</b>?")
    else:
        await update.message.reply_text("❌ Ошибка в ссылке. Попробуй другую.")
    return WAITING_FOR_DELIVERY

async def finish(update, context):
    try:
        num = int(update.message.text)
    except:
        await update.message.reply_text("Введи число.")
        return WAITING_FOR_COURIERS
    
    msg = await update.message.reply_text("⏳ Оптимизирую маршрут...")
    start_pt = context.user_data["start"]
    dels = context.user_data["dels"]
    addrs = context.user_data["addrs"]
    prefs = context.user_data["prefs"]

    # Простая нарезка на курьеров
    chunk = len(dels) // num + (1 if len(dels)%num > 0 else 0)
    results = []
    total_dist = 0

    for i in range(num):
        batch = dels[i*chunk : (i+1)*chunk]
        batch_addrs = addrs[i*chunk : (i+1)*chunk]
        if not batch: break
        
        # TSP через Haversine (т.к. GH лимит 5 точек)
        pts = [start_pt] + batch
        n = len(pts)
        manager = pywrapcp.RoutingIndexManager(n, 1, 0)
        routing = pywrapcp.RoutingModel(manager)
        def dist_fn(f, t):
            p1, p2 = pts[manager.IndexToNode(f)], pts[manager.IndexToNode(t)]
            return int(math.hypot(p1[0]-p2[0], p1[1]-p2[1]) * 100000)
        routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(dist_fn))
        sol = routing.SolveWithParameters(pywrapcp.DefaultRoutingSearchParameters())
        
        order = []
        if sol:
            idx = routing.Start(0)
            while not routing.IsEnd(idx):
                order.append(manager.IndexToNode(idx))
                idx = sol.Value(routing.NextVar(idx))
        
        # Финальный расчет через GraphHopper
        ordered_pts = [pts[idx] for idx in order]
        gh_res = await get_gh_route(ordered_pts, prefs)
        d, m = gh_res if gh_res else (len(batch)*2, len(batch)*5)
        total_dist += d
        
        points_txt = "\n".join([f"{j}. {batch_addrs[idx-1]}" for j, idx in enumerate(order[1:], 1)])
        y_url = f"https://yandex.ru/maps/?rtext={'~'.join([f'{p[0]},{p[1]}' for p in ordered_pts])}&rtt=auto"
        results.append(f"🚗 <b>Курьер {i+1}</b> ({d:.1f} км):\n{points_txt}\n🔗 <a href='{y_url}'>Навигатор</a>")

    saved_rub = (len(dels) * 3 - total_dist) * (FUEL_CONSUMPTION/100) * FUEL_PRICE
    report = "\n\n".join(results) + f"\n\n💰 Экономия: <b>~{max(0, int(saved_rub))} руб</b>"
    
    await msg.edit_text(report, parse_mode="HTML", disable_web_page_preview=True)
    
    # Сохраняем статистику
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO routes (user_id, timestamp, dist, saved_rub, count) VALUES (?, ?, ?, ?, ?)",
                 (update.effective_user.id, datetime.now().isoformat(), total_dist, saved_rub, len(dels)))
    conn.commit()
    conn.close()
    
    return ConversationHandler.END

async def show_reports(update, context):
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT SUM(dist), SUM(saved_rub), SUM(count) FROM routes WHERE user_id = ?", (update.effective_user.id,)).fetchone()
    conn.close()
    
    txt = "📊 <b>Твоя статистика:</b>\n\n"
    if res[0]:
        txt += f"• Пробег: {res[0]:.1f} км\n• Экономия: {int(res[1])} руб\n• Доставок: {res[2]}"
    else:
        txt += "Данных пока нет."
    
    if update.callback_query: await update.callback_query.message.reply_text(txt, parse_mode="HTML")
    else: await update.message.reply_text(txt, parse_mode="HTML")

async def prefs_handler(update, context):
    query = update.callback_query
    await query.answer()
    p = context.user_data.setdefault('prefs', {'right_turn': True, 'avoid_bad': True})
    
    if query.data == "toggle_rt": p['right_turn'] = not p['right_turn']
    if query.data == "toggle_bad": p['avoid_bad'] = not p['avoid_bad']
    
    kb = [
        [InlineKeyboardButton(f"Правые повороты: {'✅' if p['right_turn'] else '❌'}", callback_data="toggle_rt")],
        [InlineKeyboardButton(f"Избегать плохих дорог: {'✅' if p['avoid_bad'] else '❌'}", callback_data="toggle_bad")],
        [InlineKeyboardButton("📊 Мои отчеты", callback_data="reports")]
    ]
    await query.message.edit_text("⚙️ <b>Настройки и отчеты:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    return WAITING_FOR_PREFS

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).persistence(PicklePersistence(filepath="bot_persistence.pkl")).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(prefs_handler, pattern="^prefs$")],
        states={
            WAITING_FOR_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start)],
            WAITING_FOR_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delivery)],
            WAITING_FOR_COURIERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish)],
            WAITING_FOR_PREFS: [CallbackQueryHandler(prefs_handler, pattern="^toggle_"), CallbackQueryHandler(show_reports, pattern="^reports$")],
        },
        fallbacks=[CommandHandler("start", start)],
        name="main_conv", persistent=True
    )
    
    app.add_handler(conv)
    app.add_handler(CommandHandler("reports", show_reports))
    app.run_polling()

if __name__ == "__main__":
    main()
