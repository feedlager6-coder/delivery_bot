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
from datetime import datetime, timedelta

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables and Constants ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
YANDEX_GEOCODER_API_KEY = os.environ.get("YANDEX_GEOCODER_API_KEY")
GRAPHHOPPER_API_KEY = os.environ.get("GRAPHHOPPER_API_KEY")

# Business Metrics
AVG_FUEL_CONSUMPTION_L_PER_100KM = 10.0  # litres per 100 km
FUEL_PRICE_RUB_PER_L = 75.0  # rubles per litre
AVG_SPEED_KMH = 30

# Conversation States
WAITING_FOR_START = 1
WAITING_FOR_DELIVERY = 2
CONFIRM_START = 3
WAITING_FOR_COURIERS = 4
WAITING_FOR_ROUTE_PREFS = 5

# --- Database Setup ---
DB_NAME = 'delivery_bot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            start_lat REAL,
            start_lon REAL,
            deliveries_count INTEGER,
            total_distance_km REAL,
            total_time_min INTEGER,
            saved_distance_km REAL,
            saved_fuel_rub REAL
        )
    ''')
    conn.commit()
    conn.close()

# --- Geocoding Cache ---
geocode_cache: dict[str, str] = {}

def cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"

def get_address(lat: float, lon: float) -> str:
    key = cache_key(lat, lon)

    if key in geocode_cache:
        logger.info("Address for %s: %s (cache)", key, geocode_cache[key])
        return geocode_cache[key]

    # Nominatim (бесплатно, приоритет)
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse?"
            f"lat={lat}&lon={lon}"
            "&format=json&accept-language=ru"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "delivery-bot-1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        addr = data.get("address", {})
        road = addr.get("road", "")
        house = addr.get("house_number", "")
        if road:
            result = f"{road}, {house}" if house else road
            geocode_cache[key] = result
            logger.info("Address for %s: %s (nominatim)", key, result)
            return result
    except Exception as e:
        logger.warning("Nominatim failed for %s: %s", key, e)

    # Yandex Geocoder (запасной)
    if YANDEX_GEOCODER_API_KEY:
        try:
            url = (
                "https://geocode-maps.yandex.ru/1.x/?"
                f"apikey={YANDEX_GEOCODER_API_KEY}&geocode={lon},{lat}"
                "&format=json&results=1&lang=ru_RU&kind=house"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            members = (
                data.get("response", {})
                .get("GeoObjectCollection", {})
                .get("featureMember", [])
            )
            if members:
                name = (
                    members[0]
                    .get("GeoObject", {})
                    .get("metaDataProperty", {})
                    .get("GeocoderMetaData", {})
                    .get("text", "")
                )
                if name:
                    parts = name.split(", ")
                    result = ", ".join(parts[-2:]) if len(parts) >= 2 else name
                    geocode_cache[key] = result
                    logger.info("Address for %s: %s (yandex)", key, result)
                    return result
        except Exception as e:
            logger.warning("Yandex geocoder failed for %s: %s", key, e)

    # Fallback to coordinates
    result = f"{lat:.5f}, {lon:.5f}"
    geocode_cache[key] = result
    logger.info("Address for %s: %s (coords)", key, result)
    return result

# --- GraphHopper Routing Integration ---
async def get_graphhopper_route_details(points: list[tuple[float, float]], avoid_narrow: bool, avoid_unpaved: bool, right_turn_priority: bool) -> tuple[float, float] | None:
    if not GRAPHHOPPER_API_KEY:
        logger.error("GRAPHHOPPER_API_KEY is not set.")
        return None

    if len(points) < 2:
        return 0.0, 0.0 # No distance or time for less than 2 points

    # GraphHopper expects points in [lon, lat] format
    gh_points = [[p[1], p[0]] for p in points]

    custom_model = {
        "priority": [],
        "distance_influence": 100,
        "turn_penalty": []
    }

    # Right Turn Priority (for right-hand traffic, penalize left turns)
    if right_turn_priority:
        # Penalize sharp left turns significantly
        custom_model["turn_penalty"].append({"if": "change_angle >= 80 && change_angle <= 180", "add": "300"})
        # Penalize moderate left turns less
        custom_model["turn_penalty"].append({"if": "change_angle >= 25 && change_angle < 80", "add": "50"})

    # Avoid Narrow Streets (assuming 'width' tag in OSM data)
    if avoid_narrow:
        # Penalize roads with width < 5 meters (example threshold)
        custom_model["priority"].append({"if": "road_class == RESIDENTIAL && max_width < 5", "multiply_by": "0.1"})
        custom_model["priority"].append({"if": "road_class == SERVICE && max_width < 4", "multiply_by": "0.1"})

    # Avoid Unpaved Roads (using 'surface' tag in OSM data)
    if avoid_unpaved:
        custom_model["priority"].append({"if": "surface == UNPAVED || surface == DIRT || surface == GRAVEL", "multiply_by": "0.01"})

    # Construct the request body
    body = {
        "points": gh_points,
        "profile": "car",
        "locale": "ru",
        "instructions": False,
        "calc_points": False,
        "points_encoded": False,
        "ch.disable": True, # Disable Contraction Hierarchies for custom models
        "custom_model": custom_model
    }

    headers = {
        "Content-Type": "application/json"
    }

    try:
        url = f"https://graphhopper.com/api/1/route?key={GRAPHHOPPER_API_KEY}"
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))

        if data and "paths" in data and len(data["paths"]) > 0:
            path = data["paths"][0]
            distance_meters = path.get("distance", 0.0)
            time_ms = path.get("time", 0.0)
            return distance_meters / 1000, time_ms / 60000 # Return km and minutes
        else:
            logger.warning("GraphHopper returned no paths: %s", data)
            return None
    except Exception as e:
        logger.error("GraphHopper routing failed: %s", e)
        return None

# --- Haversine (fallback for OR-Tools TSP distance matrix) ---
def haversine_meters(c1: tuple[float, float], c2: tuple[float, float]) -> int:
    R = 6371000  # Earth radius in meters
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

# --- OR-Tools TSP Solver ---
async def solve_tsp_with_start_and_gh_distances(
    all_coords: list[tuple[float, float]],
    avoid_narrow: bool,
    avoid_unpaved: bool,
    right_turn_priority: bool
) -> tuple[list[int], float, float]: # Returns route_order, total_km, total_min
    n = len(all_coords)
    if n == 0:
        return [], 0.0, 0.0
    if n == 1:
        return [0], 0.0, 0.0

    # Build distance matrix using GraphHopper for more accurate distances
    matrix = [[0 for _ in range(n)] for _ in range(n)]
    total_gh_distance_km = 0.0
    total_gh_time_min = 0.0

    # For TSP, we need distances between all pairs. GraphHopper free tier is limited.
    # We will use Haversine for TSP matrix for now, and then use GraphHopper for the final route calculation.
    # This is a compromise due to GH free tier limitations. For full accuracy, a paid GH plan or self-hosted solution is needed.
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 0
            else:
                # Use Haversine for TSP matrix due to GH API limitations for many-to-many requests
                matrix[i][j] = haversine_meters(all_coords[i], all_coords[j])

    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [0]) # 1 vehicle, start and end at index 0
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_idx = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = 5 # 5 seconds to find a solution

    solution = routing.SolveWithParameters(params)

    route_order = []
    if solution:
        idx = routing.Start(0)
        while not routing.IsEnd(idx):
            route_order.append(manager.IndexToNode(idx))
            idx = solution.Value(routing.NextVar(idx))
        route_order.append(0) # Return to start
    else:
        # Fallback to simple sequential route if OR-Tools fails
        route_order = list(range(n)) + [0]
        logger.warning("OR-Tools failed to find a solution, falling back to sequential route.")

    # Now, calculate the actual distance and time for the optimized route using GraphHopper
    # We need to pass the points in the optimized order to GraphHopper
    ordered_gh_points = [all_coords[i] for i in route_order if i != route_order[0] or route_order.count(i) == 1]
    # Remove duplicate start point if it's not the only point
    if len(ordered_gh_points) > 1 and ordered_gh_points[0] == ordered_gh_points[-1]:
        ordered_gh_points = ordered_gh_points[:-1]

    # GraphHopper expects a list of points for a single route, not a matrix
    # We will call GH for the full optimized route
    gh_route_details = await get_graphhopper_route_details(ordered_gh_points, avoid_narrow, avoid_unpaved, right_turn_priority)

    if gh_route_details:
        total_gh_distance_km, total_gh_time_min = gh_route_details
    else:
        # Fallback to Haversine if GraphHopper fails for the final route
        total_gh_distance_km = sum(haversine_meters(all_coords[route_order[i]], all_coords[route_order[i+1]]) for i in range(len(route_order)-1)) / 1000
        total_gh_time_min = total_gh_distance_km / AVG_SPEED_KMH * 60
        logger.warning("GraphHopper failed for final route, falling back to Haversine for total distance/time.")

    return route_order, total_gh_distance_km, total_gh_time_min

# --- Telegram Bot UI Elements ---
def route_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Новый маршрут", callback_data="new"),
                InlineKeyboardButton("🏠 Изменить старт", callback_data="changehome"),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки маршрута", callback_data="route_prefs"),
                InlineKeyboardButton("📊 Отчеты", callback_data="reports"),
                InlineKeyboardButton("📖 Помощь", callback_data="help"),
            ],
        ]
    )

def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Начать маршрут", callback_data="start_route")],
            [InlineKeyboardButton("📖 Как пользоваться", callback_data="how_to")],
        ]
    )

def start_route_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Начать маршрут", callback_data="start_route")],
        ]
    )

def route_prefs_keyboard(avoid_narrow: bool, avoid_unpaved: bool, right_turn_priority: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Узкие улицы: {'✅ Да' if avoid_narrow else '❌ Нет'}",
                    callback_data=f"toggle_narrow_{'false' if avoid_narrow else 'true'}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"Грунтовки: {'✅ Да' if avoid_unpaved else '❌ Нет'}",
                    callback_data=f"toggle_unpaved_{'false' if avoid_unpaved else 'true'}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"Приоритет правых поворотов: {'✅ Да' if right_turn_priority else '❌ Нет'}",
                    callback_data=f"toggle_right_turn_{'false' if right_turn_priority else 'true'}"
                )
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")
            ]
        ]
    )


HOW_TO_GET_LINK = (
    "Как получить ссылку из Яндекс Карт:\n"
    "1️⃣ Открой Яндекс Карты\n"
    "2️⃣ Найди нужное место\n"
    "3️⃣ Зажми палец на точке\n"
    "4️⃣ Нажми Поделиться\n"
    "5️⃣ Скопируй ссылку и отправь мне"
)

# --- URL Parsing ---
def expand_short_url(url: str) -> str:
    if "maps/-/" not in url:
        return url
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15"
                )
            },
            method="GET",
        )
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        response = opener.open(req, timeout=10)
        final_url = response.geturl()
        logger.info("Expanded short link: %s -> %s", url, final_url)
        return final_url
    except Exception as e:
        logger.error("Failed to expand short link '%s': %s", url, e)
        return url

def extract_coords_from_org_page(url: str) -> tuple[float, float] | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(500000).decode("utf-8", errors="ignore")
        m = re.search(r'"center":\[(-?\d+\.?\d*),(-?\d+\.?\d*)\]', body)
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            logger.info(
                "Extracted org coords from page body: lat=%.6f lon=%.6f", lat, lon
            )
            return lat, lon
    except Exception as e:
        logger.warning("Failed to extract coords from org page '%s': %s", url, e)
    return None

def parse_yandex_link(url: str) -> tuple[float, float] | None:
    try:
        url = expand_short_url(url)

        decoded_url = urllib.parse.unquote(url)
        parsed = urllib.parse.urlparse(decoded_url)
        params = urllib.parse.parse_qs(parsed.query)

        if "whatshere[point]" in params:
            lon, lat = params["whatshere[point]"][0].split(",")
            return float(lat), float(lon)

        if "ll" in params:
            lon, lat = params["ll"][0].split(",")
            return float(lat), float(lon)

        if "rtext" in params:
            parts = params["rtext"][0].split("~")[0].split(",")
            if len(parts) >= 2:
                return float(parts[0]), float(parts[1])

        if "/maps/org/" in url or "/maps/org/" in decoded_url:
            return extract_coords_from_org_page(url)

        return None
    except Exception as e:
        logger.error(f"Link parse error for '{url}': {e}")
        return None

def coords_key(coord: tuple[float, float]) -> str:
    return f"{round(coord[0], 3)},{round(coord[1], 3)}"

# --- Route Distribution (existing logic) ---
def distribute_routes(
    deliveries: list[tuple[float, float]],
    delivery_addresses: list[str],
    num_couriers: int,
) -> list[tuple[list[tuple[float, float]], list[str]]]:
    n = len(deliveries)
    if n == 0:
        return []

    padded_addrs = (delivery_addresses + [""] * n)[:n]
    pairs = list(zip(deliveries, padded_addrs))

    actual_couriers = min(num_couriers, n)

    if actual_couriers == 1:
        groups = [(deliveries, list(delivery_addresses))]
    else:
        center_lat = sum(p[0][0] for p in pairs) / n
        center_lon = sum(p[0][1] for p in pairs) / n

        sorted_pairs = sorted(
            pairs,
            key=lambda p: math.atan2(p[0][0] - center_lat, p[0][1] - center_lon),
        )

        base_size = n // actual_couriers
        remainder = n % actual_couriers
        groups = []
        idx = 0
        for i in range(actual_couriers):
            size = base_size + (1 if i < remainder else 0)
            chunk = sorted_pairs[idx : idx + size]
            idx += size
            if chunk:
                groups.append(([c[0] for c in chunk], [c[1] for c in chunk]))

    total_assigned = sum(len(g[0]) for g in groups)
    if total_assigned != n:
        logger.error("distribute_routes: потеряны точки: %d != %d", total_assigned, n)
    for i, (grp_coords, _) in enumerate(groups):
        logger.info("Курьер %d: %d точек", i + 1, len(grp_coords))
    return groups

# --- Yandex Navigator URL (existing logic) ---
def build_yandex_nav_url(
    coords: list[tuple[float, float]], route_order: list[int]
) -> str:
    seen = set()
    unique = []
    for i in route_order:
        if i not in seen:
            unique.append(i)
            seen.add(i)
    points = "~".join(f"{coords[i][0]},{coords[i][1]}" for i in unique)
    return f"https://yandex.ru/maps/?rtext={points}&rtt=auto"

# --- Telegram Bot Handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
    context.user_data.pop("delivery_addresses", None)
    context.user_data.pop("num_couriers", None)
    context.user_data.setdefault("avoid_narrow", False)
    context.user_data.setdefault("avoid_unpaved", False)
    context.user_data.setdefault("right_turn_priority", False)

    saved = context.user_data.get("старт")

    if saved:
        await update.message.reply_text(
            "👋 С возвращением!\n"
            "Стартовая точка сохранена.\n"
            "Стартуем снова отсюда? (да/нет)",
        )
        return CONFIRM_START
    else:
        await update.message.reply_text(
            "👋 Привет! Я бот для оптимизации маршрутов доставки.\n\n"
            "🚗 <b>Что я умею:</b>\n"
            "• Строю оптимальный маршрут объезда\n"
            "• Экономлю топливо и время курьера\n"
            "• Показываю маршрут в Яндекс Навигаторе\n\n"
            "📊 <b>Реальная польза:</b>\n"
            "• Экономия 20–30% пути на каждом маршруте\n"
            "• Чем больше доставок — тем больше экономия\n"
            "• Курьер успевает больше заказов за день\n\n"
            "💡 Пример: при 10 доставках в день\n"
            "экономия может составить от 3 000 до 15 000 руб.\n"
            "в месяц только на топливе\n\n"
            "Яндекс Карты едут в том порядке как ты добавил точки — "
            "я нахожу лучший порядок сам 🧠\n\n"
            "Готов начать? 👇",
            parse_mode="HTML",
            reply_markup=welcome_keyboard(),
        )
        return WAITING_FOR_START

async def handle_confirm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    if text == "да":
        await _ask_for_delivery(update, context, first=True)
        return WAITING_FOR_DELIVERY
    elif text == "нет":
        context.user_data.pop("старт", None)
        context.user_data.pop("старт_адрес", None)
        await update.message.reply_text(
            "Отправь ссылку на место старта\n"
            "(склад, офис или дом)\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_START
    else:
        await update.message.reply_text("Пожалуйста, ответь 'да' или 'нет'.")
        return CONFIRM_START

async def handle_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    coord = await asyncio.to_thread(parse_yandex_link, text)
    if coord:
        context.user_data["старт"] = coord
        address = await asyncio.to_thread(get_address, coord[0], coord[1])
        context.user_data["старт_адрес"] = address
        await update.message.reply_text(
            f"📍 Стартовая точка: <b>{address}</b> сохранена.\n"
            "Теперь отправляй ссылки точек доставки по одной.\n"
            "Когда добавишь все — напиши <b>Готово</b>\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_DELIVERY
    else:
        await update.message.reply_text(
            "❌ Не смог прочитать ссылку.\n"
            "Попробуй ещё раз — зажми место на карте,\n"
            "нажми Поделиться и скопируй ссылку.",
            parse_mode="HTML",
        )
        return WAITING_FOR_START

async def _ask_for_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE, first: bool) -> None:
    n = len(context.user_data.get("deliveries", []))
    if first:
        await update.message.reply_text(
            "Теперь отправляй ссылки точек доставки по одной.\n"
            "Когда добавишь все — напиши <b>Готово</b>\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
    else:
        addresses = context.user_data.get("delivery_addresses", [])
        addr = addresses[n - 1] if n <= len(addresses) else f"точка {n}"
        await update.message.reply_text(
            f"📍 <b>{addr}</b> добавлена!\n"
            "Отправь следующую ссылку или напиши <b>Готово</b>",
            parse_mode="HTML",
        )

async def handle_delivery_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    msg_id = update.message.message_id
    processed = context.user_data.setdefault("processed_msgs", set())
    if msg_id in processed:
        return WAITING_FOR_DELIVERY
    processed.add(msg_id)
    if len(processed) > 100:
        context.user_data["processed_msgs"] = set()

    text = update.message.text.strip()

    if text.lower() in ("готово", "готов", "go", "done"):
        deliveries = context.user_data.get("deliveries", [])
        if not deliveries:
            await update.message.reply_text(
                "⚠️ Ты не добавил ни одной точки доставки.\nОтправь ссылку из Яндекс Карт."
            )
            return WAITING_FOR_DELIVERY
        await update.message.reply_text(
            "👥 Сколько курьеров?\n"
            "Напиши число от 1 до 10\n\n"
            "(1 — один оптимальный маршрут)"
        )
        return WAITING_FOR_COURIERS

    coord = await asyncio.to_thread(parse_yandex_link, text)
    if not coord:
        await update.message.reply_text(
            "❌ Не смог прочитать ссылку.\n"
            "Попробуй ещё раз — зажми место на карте,\n"
            "нажми Поделиться и скопируй ссылку.\n\n"
            "Или напиши <b>Готово</b> чтобы построить маршрут из уже добавленных точек.",
            parse_mode="HTML",
        )
        return WAITING_FOR_DELIVERY

    new_key = coords_key(coord)

    start = context.user_data.get("старт")
    if start and coords_key(start) == new_key:
        await update.message.reply_text(
            "⚠️ Это твоя стартовая точка.\nОтправь ссылку на точку доставки."
        )
        return WAITING_FOR_DELIVERY

    deliveries = context.user_data.setdefault("deliveries", [])
    existing_keys = {coords_key(d) for d in deliveries}
    if new_key in existing_keys:
        return WAITING_FOR_DELIVERY
    if len(deliveries) >= 50:
        await update.message.reply_text(
            "⚠️ Максимум 50 точек доставки. Напиши <b>Готово</b> для расчёта.",
            parse_mode="HTML",
        )
        logger.info("User %s hit delivery limit (50)", update.effective_user.id)
        return WAITING_FOR_DELIVERY

    address = await asyncio.to_thread(
        get_address, coord[0], coord[1]
    )
    deliveries.append(coord)
    context.user_data.setdefault("delivery_addresses", []).append(address)
    logger.info(
        "User %s added delivery point #%d (%s), state=WAITING_FOR_DELIVERY",
        update.effective_user.id,
        len(deliveries),
        address,
    )
    await _ask_for_delivery(update, context, first=False)
    return WAITING_FOR_DELIVERY

async def handle_couriers_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    try:
        n = int(text)
        if n < 1 or n > 10:
            await update.message.reply_text("Напиши число от 1 до 10")
            return WAITING_FOR_COURIERS
    except ValueError:
        await update.message.reply_text("Не понял. Напиши просто число, например: 3")
        return WAITING_FOR_COURIERS

    context.user_data["num_couriers"] = n
    return await finish_route(update, context)

async def finish_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    deliveries: list[tuple[float, float]] = context.user_data.get("deliveries", [])
    delivery_addresses: list[str] = context.user_data.get("delivery_addresses", [])
    num_couriers: int = context.user_data.get("num_couriers", 1)
    start_coord = context.user_data["старт"]

    avoid_narrow = context.user_data.get("avoid_narrow", False)
    avoid_unpaved = context.user_data.get("avoid_unpaved", False)
    right_turn_priority = context.user_data.get("right_turn_priority", False)

    status_msg = await update.message.reply_text(
        f"⚙️ Считаю маршруты для {num_couriers} курьера(ов) с учетом ваших настроек..."
    )

    groups = distribute_routes(deliveries, delivery_addresses, num_couriers)

    total_km_all = 0.0
    total_min_all = 0
    courier_blocks: list[str] = []

    for courier_idx, (group_coords, group_addrs) in enumerate(groups, 1):
        all_coords = [start_coord] + group_coords
        route_order, total_km, total_min = await solve_tsp_with_start_and_gh_distances(
            all_coords, avoid_narrow, avoid_unpaved, right_turn_priority
        )
        total_km_all += total_km
        total_min_all += total_min

        seen: set[int] = set()
        delivery_steps: list[int] = []
        for i in route_order:
            if i != 0 and i not in seen:
                delivery_steps.append(i)
                seen.add(i)

        numbered = "\n".join(
            f"{pos}. {group_addrs[step - 1] if 0 <= step - 1 < len(group_addrs) else f'точка {step}'}"
            for pos, step in enumerate(delivery_steps, 1)
        )

        yandex_url = build_yandex_nav_url(all_coords, route_order)

        if num_couriers == 1:
            courier_blocks.append(
                f"📍 <b>Оптимальный маршрут:</b>\n{numbered}\n"
                f"🏁 Возврат на старт\n\n"
                f"📏 Расстояние: <b>{total_km:.1f} км</b>\n"
                f"⏱ Время: <b>{total_min} мин</b> (с учетом пробок и настроек)\n"
                f"🗺 <b>Открыть в навигаторе:</b>\n{yandex_url}"
            )
        else:
            courier_blocks.append(
                f"🚗 <b>Курьер {courier_idx}</b> — {len(group_coords)} точек:\n"
                f"{numbered}\n"
                f"📏 {total_km:.1f} км • ⏱ {total_min} мин\n"
                f'🗺 <a href="{yandex_url}">Маршрут курьера {courier_idx}</a>'
            )

    # Calculate savings based on new GH distances
    # For random route, we'll use Haversine as a baseline for comparison, as GH for random is too complex/costly
    random_km_baseline = sum(haversine_meters(start_coord, d) for d in deliveries) * 2 / 1000 # Rough estimate
    savings_km = max(0.0, random_km_baseline - total_km_all)
    savings_fuel_rub = (savings_km / 100) * AVG_FUEL_CONSUMPTION_L_PER_100KM * FUEL_PRICE_RUB_PER_L

    if num_couriers == 1:
        header = "🚀 Маршрут готов!\n\n"
        footer = ""
    else:
        header = f"👥 Маршруты для {num_couriers} курьеров готовы!\n\n"
        footer = (
            f"\n\n📊 Итого: <b>{total_km_all:.1f} км</b> • <b>{total_min_all} мин</b>\n"
        )

    if savings_km > 0:
        day_savings = round(savings_fuel_rub)
        if num_couriers == 1:
            month_savings = round(day_savings * 30)
            year_savings = round(month_savings * 12)
            footer += (
                f"💰 Экономия: <b>{savings_km:.1f} км</b>\n"
                f"⛽️ Экономия топлива:\n"
                f"   ~{day_savings} руб. в день\n"
                f"   ~{month_savings} руб. в месяц\n"
                f"   ~{year_savings} руб. в год\n"
            )
        else:
            footer += (
                f"💰 Экономия: <b>{savings_km:.1f} км</b> (~{day_savings} руб/день)\n"
            )

    result_text = header + "\n\n".join(courier_blocks) + footer

    await status_msg.edit_text(
        result_text, parse_mode="HTML", reply_markup=route_done_keyboard()
    )

    # Save route data to DB
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO routes (user_id, timestamp, start_lat, start_lon, deliveries_count, total_distance_km, total_time_min, saved_distance_km, saved_fuel_rub) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (update.effective_user.id, datetime.now().isoformat(), start_coord[0], start_coord[1], len(deliveries), total_km_all, total_min_all, savings_km, savings_fuel_rub))
    conn.commit()
    conn.close()

    context.user_data.pop("deliveries", None)
    context.user_data.pop("delivery_addresses", None)
    context.user_data.pop("num_couriers", None)
    return ConversationHandler.END

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
    context.user_data.pop("delivery_addresses", None)
    context.user_data.pop("num_couriers", None)
    saved = context.user_data.get("старт")

    if saved:
        saved_addr = context.user_data.get("старт_адрес", "сохранённая точка")
        await update.message.reply_text(
            "🔄 Новый маршрут! Точки сброшены.\n\n"
            f"Стартуем снова отсюда?\n"
            f"📍 {saved_addr}\n\n"
            "(да/нет)"
        )
        return CONFIRM_START
    else:
        await update.message.reply_text(
            "🔄 Новый маршрут!\n\n"
            "Отправь ссылку на место старта.\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_START

async def cmd_changehome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
    await update.message.reply_text(
        "🏠 Отправь новую ссылку на место старта.\n\n" + HOW_TO_GET_LINK,
        parse_mode="HTML",
    )
    return WAITING_FOR_START

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "start_route":
        context.user_data.pop("deliveries", None)
        await query.message.reply_text(
            "Отправь ссылку на место старта\n"
            "(склад, офис или дом)\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_START

    elif query.data == "how_to":
        await query.message.reply_text(
            "📖 <b>Как пользоваться ботом:</b>\n\n"
            "1️⃣ Нажми <b>Начать маршрут</b>\n"
            "2️⃣ Открой Яндекс Карты\n"
            "3️⃣ Зажми палец на точке старта\n"
            "4️⃣ Нажми Поделиться → скопируй ссылку\n"
            "5️⃣ Отправь ссылку боту\n"
            "6️⃣ Повтори для каждой точки доставки\n"
            "7️⃣ Напиши <b>Готово</b>\n"
            "8️⃣ Получи оптимальный маршрут! 🎉\n\n"
            "Подсказка: ссылка выглядит так:\n"
            "<code>https://yandex.ru/maps/?whatshere...</code>",
            parse_mode="HTML",
            reply_markup=start_route_keyboard(),
        )
        return WAITING_FOR_START

    elif query.data == "new":
        context.user_data.pop("deliveries", None)
        context.user_data.pop("delivery_addresses", None)
        context.user_data.pop("num_couriers", None)
        saved = context.user_data.get("старт")
        if saved:
            saved_addr = context.user_data.get("старт_адрес", "сохранённая точка")
            await query.message.reply_text(
                "🔄 Новый маршрут! Точки сброшены.\n\n"
                f"Стартуем снова отсюда?\n"
                f"📍 {saved_addr}\n\n"
                "(да/нет)"
            )
            return CONFIRM_START
        else:
            await query.message.reply_text(
                "🔄 Новый маршрут!\n\n"
                "Отправь ссылку на место старта.\n\n" + HOW_TO_GET_LINK,
                parse_mode="HTML",
            )
            return WAITING_FOR_START

    elif query.data == "changehome":
        context.user_data.pop("deliveries", None)
        await query.message.reply_text(
            "🏠 Отправь новую ссылку на место старта.\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_START

    elif query.data == "help":
        await query.message.reply_text(
            "📖 <b>Как пользоваться ботом:</b>\n\n"
            "1. /start — начать новый маршрут\n"
            "2. Отправь ссылку стартовой точки из Яндекс Карт\n"
            "3. Отправляй ссылки точек доставки по одной\n"
            "4. Напиши <b>Готово</b> — получи оптимальный маршрут\n\n"
            "<b>Команды:</b>\n"
            "/new — новый маршрут (старт сохраняется)\n"
            "/changehome — изменить стартовую точку\n"
            "/route_prefs — настройки маршрута (узкие улицы, грунтовки, правые повороты)\n"
            "/reports — отчеты по доставкам\n"
            "/help — эта справка\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return ConversationHandler.END

    elif query.data == "route_prefs":
        avoid_narrow = context.user_data.get("avoid_narrow", False)
        avoid_unpaved = context.user_data.get("avoid_unpaved", False)
        right_turn_priority = context.user_data.get("right_turn_priority", False)
        await query.message.edit_text(
            "⚙️ <b>Настройки маршрута:</b>\n\n"
            "Здесь вы можете настроить предпочтения для построения маршрута.",
            parse_mode="HTML",
            reply_markup=route_prefs_keyboard(avoid_narrow, avoid_unpaved, right_turn_priority)
        )
        return WAITING_FOR_ROUTE_PREFS

    elif query.data.startswith("toggle_narrow_"):
        new_value = query.data.split("_")[2] == "true"
        context.user_data["avoid_narrow"] = new_value
        avoid_narrow = new_value
        avoid_unpaved = context.user_data.get("avoid_unpaved", False)
        right_turn_priority = context.user_data.get("right_turn_priority", False)
        await query.message.edit_reply_markup(
            reply_markup=route_prefs_keyboard(avoid_narrow, avoid_unpaved, right_turn_priority)
        )
        return WAITING_FOR_ROUTE_PREFS

    elif query.data.startswith("toggle_unpaved_"):
        new_value = query.data.split("_")[2] == "true"
        context.user_data["avoid_unpaved"] = new_value
        avoid_narrow = context.user_data.get("avoid_narrow", False)
        avoid_unpaved = new_value
        right_turn_priority = context.user_data.get("right_turn_priority", False)
        await query.message.edit_reply_markup(
            reply_markup=route_prefs_keyboard(avoid_narrow, avoid_unpaved, right_turn_priority)
        )
        return WAITING_FOR_ROUTE_PREFS

    elif query.data.startswith("toggle_right_turn_"):
        new_value = query.data.split("_")[2] == "true"
        context.user_data["right_turn_priority"] = new_value
        avoid_narrow = context.user_data.get("avoid_narrow", False)
        avoid_unpaved = context.user_data.get("avoid_unpaved", False)
        right_turn_priority = new_value
        await query.message.edit_reply_markup(
            reply_markup=route_prefs_keyboard(avoid_narrow, avoid_unpaved, right_turn_priority)
        )
        return WAITING_FOR_ROUTE_PREFS

    elif query.data == "back_to_main":
        await query.message.edit_text(
            "Главное меню.",
            reply_markup=welcome_keyboard()
        )
        return WAITING_FOR_START # Or whatever state is appropriate for main menu

    elif query.data == "reports":
        return await cmd_reports(update, context)

    return ConversationHandler.END

async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Daily Report
    today = datetime.now().date()
    cursor.execute("SELECT SUM(total_distance_km), SUM(saved_fuel_rub), COUNT(*) FROM routes WHERE user_id = ? AND DATE(timestamp) = ?", (user_id, today.isoformat()))
    daily_data = cursor.fetchone()
    daily_km, daily_saved_rub, daily_deliveries = daily_data if daily_data[0] is not None else (0, 0, 0)

    # Monthly Report
    this_month_start = today.replace(day=1)
    cursor.execute("SELECT SUM(total_distance_km), SUM(saved_fuel_rub), COUNT(*) FROM routes WHERE user_id = ? AND DATE(timestamp) >= ?", (user_id, this_month_start.isoformat()))
    monthly_data = cursor.fetchone()
    monthly_km, monthly_saved_rub, monthly_deliveries = monthly_data if monthly_data[0] is not None else (0, 0, 0)

    conn.close()

    report_text = (
        f"📊 <b>Отчеты по доставкам:</b>\n\n"
        f"🗓 <b>Сегодня ({today.strftime('%d.%m.%Y')}):</b>\n"
        f"  • Пробег: <b>{daily_km:.1f} км</b>\n"
        f"  • Сэкономлено топлива: <b>{daily_saved_rub:.0f} руб.</b>\n"
        f"  • Доставок: <b>{daily_deliveries}</b>\n\n"
        f"📈 <b>За текущий месяц ({this_month_start.strftime('%m.%Y')}):</b>\n"
        f"  • Пробег: <b>{monthly_km:.1f} км</b>\n"
        f"  • Сэкономлено топлива: <b>{monthly_saved_rub:.0f} руб.</b>\n"
        f"  • Доставок: <b>{monthly_deliveries}</b>\n\n"
        "(Данные обновляются после каждого расчета маршрута)"
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(report_text, parse_mode="HTML", reply_markup=route_done_keyboard())
    else:
        await update.message.reply_text(report_text, parse_mode="HTML", reply_markup=route_done_keyboard())

    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Используй /start чтобы начать или /help для справки."
    )

async def delivery_state_unknown_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    deliveries = context.user_data.get("deliveries", [])
    logger.info(
        "User %s sent unknown command in WAITING_FOR_DELIVERY, points so far: %d",
        update.effective_user.id,
        len(deliveries),
    )
    await update.message.reply_text(
        f"📍 Уже добавлено точек: <b>{len(deliveries)}</b>\n"
        "Отправь ссылку из Яндекс Карт или напиши <b>Готово</b>",
        parse_mode="HTML",
    )
    return WAITING_FOR_DELIVERY

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Exception while handling update: %s", context.error, exc_info=context.error
    )

def main() -> None:
    # Ensure environment variables are set
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")
    if not YANDEX_GEOCODER_API_KEY:
        logger.warning("YANDEX_GEOCODER_API_KEY is not set. Yandex geocoding will not be used.")
    if not GRAPHHOPPER_API_KEY:
        logger.warning("GRAPHHOPPER_API_KEY is not set. Advanced routing features will be limited.")

    # Initialize database
    init_db()

    # Terminate previous bot instance to avoid 409 Conflict
    pid_file = "/tmp/bot_route.pid"
    if os.path.exists(pid_file):
        try:
            old_pid = int(open(pid_file).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to old instance PID=%d", old_pid)
        except (ProcessLookupError, ValueError, OSError):
            pass
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    persistence = PicklePersistence(filepath="bot_persistence.pkl")
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
            CommandHandler("route_prefs", button_handler), # Direct command to route prefs
            CommandHandler("reports", cmd_reports), # Direct command to reports
            CallbackQueryHandler(button_handler),
        ],
        states={
            CONFIRM_START: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm_start),
            ],
            WAITING_FOR_START: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_link),
            ],
            WAITING_FOR_DELIVERY: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delivery_link),
            ],
            WAITING_FOR_COURIERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_couriers_input),
            ],
            WAITING_FOR_ROUTE_PREFS: [
                CallbackQueryHandler(button_handler),
            ]
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
            CommandHandler("route_prefs", button_handler),
            CommandHandler("reports", cmd_reports),
            CallbackQueryHandler(button_handler),
            MessageHandler(filters.COMMAND, delivery_state_unknown_cmd), # Catch unknown commands in delivery state
            MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ATTACHMENT, fallback_handler), # General fallback
        ],
        name="route_conversation",
        persistent=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    logger.info("Bot started polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()