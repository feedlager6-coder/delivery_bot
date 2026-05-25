import os
import re
import math
import signal
import asyncio
import sqlite3
import logging
import random
import json
import urllib.request
import urllib.parse
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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

AVG_SPEED_KMH = 30

geocode_cache: dict[str, str] = {}


def cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"


def get_address(lat: float, lon: float, yandex_key: str | None = None) -> str:
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

    # Яндекс (запасной)
    if yandex_key:
        try:
            url = (
                "https://geocode-maps.yandex.ru/1.x/?"
                f"apikey={yandex_key}&geocode={lon},{lat}"
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

    # Запасной — координаты
    result = f"{lat:.5f}, {lon:.5f}"
    geocode_cache[key] = result
    logger.info("Address for %s: %s (coords)", key, result)
    return result


WAITING_FOR_START = 1
WAITING_FOR_DELIVERY = 2
CONFIRM_START = 3
WAITING_FOR_COURIERS = 4


def route_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Новый маршрут", callback_data="new"),
                InlineKeyboardButton("🏠 Изменить старт", callback_data="changehome"),
            ],
            [
                InlineKeyboardButton("📊 Статистика", callback_data="stats"),
                InlineKeyboardButton("📖 Помощь", callback_data="help"),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки маршрута", callback_data="open_route_prefs"),
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


HOW_TO_GET_LINK = (
    "Как получить ссылку из Яндекс Карт:\n"
    "1️⃣ Открой Яндекс Карты\n"
    "2️⃣ Найди нужное место\n"
    "3️⃣ Зажми палец на точке\n"
    "4️⃣ Нажми Поделиться\n"
    "5️⃣ Скопируй ссылку и отправь мне"
)


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

        # Запасной вариант: ссылка на организацию — извлекаем координаты из HTML
        if "/maps/org/" in url or "/maps/org/" in decoded_url:
            return extract_coords_from_org_page(url)

        return None
    except Exception as e:
        logger.error(f"Link parse error for '{url}': {e}")
        return None


def haversine_meters(c1: tuple[float, float], c2: tuple[float, float]) -> int:
    R = 6371000
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def build_distance_matrix(coords: list[tuple[float, float]]) -> list[list[int]]:
    n = len(coords)
    return [
        [0 if i == j else haversine_meters(coords[i], coords[j]) for j in range(n)]
        for i in range(n)
    ]


def solve_tsp_with_start(
    all_coords: list[tuple[float, float]],
) -> tuple[list[int], int]:
    n = len(all_coords)
    if n == 2:
        dist = haversine_meters(all_coords[0], all_coords[1])
        return [0, 1, 0], dist * 2

    matrix = build_distance_matrix(all_coords)
    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [0])
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
    params.time_limit.seconds = 5

    solution = routing.SolveWithParameters(params)
    if not solution:
        route = list(range(n)) + [0]
        total = sum(matrix[route[i]][route[i + 1]] for i in range(len(route) - 1))
        return route, total

    route = []
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        route.append(manager.IndexToNode(idx))
        idx = solution.Value(routing.NextVar(idx))
    route.append(0)
    return route, solution.ObjectiveValue()


def random_route_distance(all_coords: list[tuple[float, float]]) -> float:
    delivery_indices = list(range(1, len(all_coords)))
    random.shuffle(delivery_indices)
    order = [0] + delivery_indices + [0]
    total = sum(
        haversine_meters(all_coords[order[i]], all_coords[order[i + 1]])
        for i in range(len(order) - 1)
    )
    return total / 1000


def coords_key(coord: tuple[float, float]) -> str:
    """Ключ координаты с точностью до 3 знаков — для дедупликации."""
    return f"{round(coord[0], 3)},{round(coord[1], 3)}"


def expand_short_url(url: str) -> str:
    """Раскрывает короткие ссылки yandex.ru/maps/-/... через GET-запрос."""
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
    """Извлекает координаты из страницы организации Яндекс Карт по содержимому HTML."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(500000).decode("utf-8", errors="ignore")
        # "center":[lon, lat] — стандартный паттерн в JSON страницы организации
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


def distribute_routes(
    deliveries: list[tuple[float, float]],
    delivery_addresses: list[str],
    num_couriers: int,
) -> list[tuple[list[tuple[float, float]], list[str]]]:
    """Делит точки доставки на N групп по углу от центра (равномерно)."""
    n = len(deliveries)
    if n == 0:
        return []

    padded_addrs = (delivery_addresses + [""] * n)[:n]
    pairs = list(zip(deliveries, padded_addrs))

    # Не создаём больше групп, чем точек
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


def build_graphhopper_route(
    points: list[tuple[float, float]], preferences: dict
) -> dict | None:
    """Request a real road route from GraphHopper for an already-ordered list of coords.

    OR-Tools determines the optimal visit order; this function only computes
    accurate road distance/duration for that fixed sequence.

    Args:
        points: (lat, lon) tuples in optimised order (including return to start).
        preferences: dict from get_user_preferences().

    Returns:
        {"distance_km": float, "duration_minutes": float, "raw_response": dict}
        or None on any failure (caller must fall back to haversine estimate).
    """
    api_key = os.environ.get("GRAPHHOPPER_API_KEY")
    if not api_key:
        logger.warning("GRAPHHOPPER_API_KEY not set — skipping GraphHopper routing")
        return None

    # GraphHopper API expects [lon, lat] (GeoJSON order)
    gh_points = [[lon, lat] for lat, lon in points]

    body: dict = {
        "points": gh_points,
        "profile": "car",
        "locale": "ru",
        "calc_points": False,
        "instructions": False,
    }

    priority_rules: list[dict] = []

    if preferences.get("avoid_bad_roads"):
        priority_rules.append({
            "if": (
                "surface == UNPAVED || surface == GRAVEL || "
                "surface == DIRT || surface == GROUND || road_class == TRACK"
            ),
            "multiply_by": "0.1",
        })

    if preferences.get("avoid_narrow_roads"):
        priority_rules.append({
            "if": (
                "road_class == RESIDENTIAL || "
                "road_class == SERVICE || road_class == LIVING_STREET"
            ),
            "multiply_by": "0.7",
        })

    # TODO: prefer_right_turns — GraphHopper has no native turn-preference primitive
    # in its custom model. Proper implementation requires post-processing turn-by-turn
    # instructions and re-scoring segments at junctions. Architectural placeholder:
    # preferences["prefer_right_turns"] is stored in SQLite and passed here, but is
    # not applied to routing until a suitable hook in the GH API is identified.

    if priority_rules:
        body["custom_model"] = {"priority": priority_rules, "distance_influence": 0}
        body["ch.disable"] = True

    url = f"https://graphhopper.com/api/1/route?key={api_key}"

    try:
        raw = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "delivery-bot-1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            response = json.loads(r.read().decode("utf-8"))

        path = response.get("paths", [{}])[0]
        distance_km = path.get("distance", 0) / 1000
        duration_minutes = path.get("time", 0) / 60000

        logger.info(
            "GraphHopper: %.1f km, %.0f min (prefs: bad_roads=%s narrow=%s right=%s)",
            distance_km,
            duration_minutes,
            preferences.get("avoid_bad_roads"),
            preferences.get("avoid_narrow_roads"),
            preferences.get("prefer_right_turns"),
        )
        return {
            "distance_km": distance_km,
            "duration_minutes": duration_minutes,
            "raw_response": response,
        }
    except Exception as e:
        logger.warning("GraphHopper API error: %s — falling back to haversine", e)
        return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
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


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
    saved = context.user_data.get("старт")

    if saved:
        await update.message.reply_text(
            "🔄 Новый маршрут! Точки доставки сброшены.\n\n"
            "Стартуем снова отсюда? (да/нет)",
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


async def handle_confirm_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip().lower()
    logger.info("Confirm start: got '%s' (repr: %r)", text, text)
    if text in ("да", "yes", "д", "+", "y", "ага", "ок", "ok", "1"):
        context.user_data["deliveries"] = []
        logger.info(
            "User %s confirmed start, moving to WAITING_FOR_DELIVERY",
            update.effective_user.id,
        )
        await _ask_for_delivery(update, context, first=True)
        return WAITING_FOR_DELIVERY
    else:
        context.user_data.pop("старт", None)
        logger.info(
            "User %s declined start, moving to WAITING_FOR_START",
            update.effective_user.id,
        )
        await update.message.reply_text(
            "Хорошо! Отправь новую ссылку на место старта.\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return WAITING_FOR_START


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 <b>Как пользоваться ботом:</b>\n\n"
        "1. /start — начать новый маршрут\n"
        "2. Отправь ссылку на стартовую точку\n"
        "3. Отправляй ссылки точек доставки по одной\n"
        "4. Напиши <b>Готово</b> когда добавишь все точки\n"
        "5. Получи оптимальный маршрут и ссылку в навигатор\n\n"
        "<b>Команды:</b>\n"
        "/new — новый маршрут (старт сохраняется)\n"
        "/changehome — изменить стартовую точку\n"
        "/help — эта справка\n\n" + HOW_TO_GET_LINK,
        parse_mode="HTML",
    )


async def handle_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    coord = await asyncio.to_thread(parse_yandex_link, text)
    if not coord:
        await update.message.reply_text(
            "❌ Не смог прочитать ссылку.\n"
            "Попробуй ещё раз — зажми место на карте,\n"
            "нажми Поделиться и скопируй ссылку.",
        )
        return WAITING_FOR_START

    context.user_data["старт"] = coord
    context.user_data["старт_адрес"] = await asyncio.to_thread(
        get_address, coord[0], coord[1], os.environ.get("YANDEX_KEY")
    )
    context.user_data["deliveries"] = []
    context.user_data["delivery_addresses"] = []
    context.user_data["processed_msgs"] = set()
    await update.message.reply_text("✅ Стартовая точка сохранена!")
    await _ask_for_delivery(update, context, first=True)
    return WAITING_FOR_DELIVERY


async def _ask_for_delivery(
    update: Update, context: ContextTypes.DEFAULT_TYPE, first: bool = False
) -> None:
    deliveries = context.user_data.get("deliveries", [])
    n = len(deliveries)
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
        get_address, coord[0], coord[1], os.environ.get("YANDEX_KEY")
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

    prefs = get_user_preferences(update.effective_user.id)
    prefs_lines = [
        ("✅" if prefs["avoid_bad_roads"] else "❌") + " Избегать грунтовок",
        ("✅" if prefs["avoid_narrow_roads"] else "❌") + " Избегать узких улиц",
        ("✅" if prefs["prefer_right_turns"] else "❌") + " Приоритет правых поворотов",
    ]
    await update.message.reply_text(
        "⚙️ <b>Активные настройки:</b>\n" + "\n".join(prefs_lines),
        parse_mode="HTML",
    )

    return await finish_route(update, context)


async def finish_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    deliveries: list[tuple[float, float]] = context.user_data.get("deliveries", [])
    delivery_addresses: list[str] = context.user_data.get("delivery_addresses", [])
    num_couriers: int = context.user_data.get("num_couriers", 1)
    start_coord = context.user_data["старт"]

    prefs = get_user_preferences(update.effective_user.id)

    status_msg = await update.message.reply_text(
        f"⚙️ Считаю маршруты для {num_couriers} курьера(ов)..."
    )

    groups = distribute_routes(deliveries, delivery_addresses, num_couriers)

    total_km_all = 0.0
    total_min_all = 0
    courier_blocks: list[str] = []

    for courier_idx, (group_coords, group_addrs) in enumerate(groups, 1):
        all_coords = [start_coord] + group_coords
        route_order, total_meters = solve_tsp_with_start(all_coords)

        # Haversine baseline (always computed as fallback)
        total_km = total_meters / 1000
        time_min = int(total_km / AVG_SPEED_KMH * 60)

        # Real road distance/duration via GraphHopper (fallback-safe)
        ordered_coords = [all_coords[i] for i in route_order]
        gh = await asyncio.to_thread(build_graphhopper_route, ordered_coords, prefs)
        if gh:
            total_km = gh["distance_km"]
            time_min = int(gh["duration_minutes"])

        total_km_all += total_km
        total_min_all += time_min

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
                f"⏱ Время: <b>{time_min} мин</b> (30 км/ч)\n"
                f"🗺 <b>Открыть в навигаторе:</b>\n{yandex_url}"
            )
        else:
            courier_blocks.append(
                f"🚗 <b>Курьер {courier_idx}</b> — {len(group_coords)} точек:\n"
                f"{numbered}\n"
                f"📏 {total_km:.1f} км • ⏱ {time_min} мин\n"
                f'🗺 <a href="{yandex_url}">Маршрут курьера {courier_idx}</a>'
            )

    # Экономия относительно случайного порядка всех точек
    random_km = random_route_distance([start_coord] + deliveries)
    savings_km = max(0.0, random_km - total_km_all)

    if num_couriers == 1:
        header = "🚀 Маршрут готов!\n\n"
        footer = ""
    else:
        header = f"👥 Маршруты для {num_couriers} курьеров готовы!\n\n"
        footer = (
            f"\n\n📊 Итого: <b>{total_km_all:.1f} км</b> • <b>{total_min_all} мин</b>\n"
        )

    if savings_km > 0:
        day_savings = round(savings_km * 12)
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

    result = header + "\n\n".join(courier_blocks) + footer

    await status_msg.edit_text(
        result, parse_mode="HTML", reply_markup=route_done_keyboard()
    )

    if savings_km > 0:
        saved_rub = round(savings_km * 12)
    else:
        saved_rub = 0
    save_route(
        user_id=update.effective_user.id,
        num_couriers=num_couriers,
        num_points=len(deliveries),
        total_km=round(total_km_all, 2),
        saved_km=round(savings_km, 2),
        saved_rub=saved_rub,
    )

    context.user_data.pop("deliveries", None)
    context.user_data.pop("delivery_addresses", None)
    context.user_data.pop("num_couriers", None)
    return ConversationHandler.END


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

    elif query.data == "stats":
        await cmd_stats_inner(query.message, update.effective_user.id)
        return

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
            "/help — эта справка\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
        )
        return ConversationHandler.END

    elif query.data == "open_route_prefs":
        await cmd_route_prefs(update, context)
        return ConversationHandler.END

    return ConversationHandler.END


def init_db() -> None:
    conn = sqlite3.connect("routes.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            num_couriers INTEGER,
            num_points INTEGER,
            total_km REAL,
            saved_km REAL,
            saved_rub INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS route_preferences (
            user_id INTEGER PRIMARY KEY,
            avoid_bad_roads INTEGER DEFAULT 0,
            avoid_narrow_roads INTEGER DEFAULT 0,
            prefer_right_turns INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def get_user_preferences(user_id: int) -> dict:
    conn = sqlite3.connect("routes.db")
    c = conn.cursor()
    c.execute("SELECT avoid_bad_roads, avoid_narrow_roads, prefer_right_turns FROM route_preferences WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO route_preferences (user_id) VALUES (?)", (user_id,))
        conn.commit()
        row = (0, 0, 0)
    conn.close()
    return {
        "avoid_bad_roads": bool(row[0]),
        "avoid_narrow_roads": bool(row[1]),
        "prefer_right_turns": bool(row[2]),
    }


def update_user_preference(user_id: int, field: str, value: int) -> None:
    allowed = {"avoid_bad_roads", "avoid_narrow_roads", "prefer_right_turns"}
    if field not in allowed:
        return
    conn = sqlite3.connect("routes.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO route_preferences (user_id) VALUES (?)", (user_id,))
    c.execute(f"UPDATE route_preferences SET {field}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()


def save_route(
    user_id: int,
    num_couriers: int,
    num_points: int,
    total_km: float,
    saved_km: float,
    saved_rub: int,
) -> None:
    conn = sqlite3.connect("routes.db")
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO routes
        (user_id, date, num_couriers, num_points, total_km, saved_km, saved_rub)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, date.today().isoformat(), num_couriers, num_points, total_km, saved_km, saved_rub),
    )
    conn.commit()
    conn.close()


async def cmd_stats_inner(message, user_id: int) -> None:
    today = date.today().isoformat()
    month = today[:7]

    conn = sqlite3.connect("routes.db")
    c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(*), SUM(num_points), SUM(total_km), SUM(saved_km), SUM(saved_rub)
        FROM routes WHERE user_id=? AND date=?
        """,
        (user_id, today),
    )
    today_stats = c.fetchone()
    c.execute(
        """
        SELECT COUNT(*), SUM(num_points), SUM(total_km), SUM(saved_km), SUM(saved_rub)
        FROM routes WHERE user_id=? AND date LIKE ?
        """,
        (user_id, f"{month}%"),
    )
    month_stats = c.fetchone()
    conn.close()

    def fmt(stats) -> str:
        if not stats or stats[0] == 0:
            return "Нет данных"
        return (
            f"Маршрутов: {stats[0]}\n"
            f"Точек доставки: {stats[1] or 0}\n"
            f"Пробег: {(stats[2] or 0):.1f} км\n"
            f"Экономия: {(stats[3] or 0):.1f} км\n"
            f"В рублях: ~{stats[4] or 0} руб"
        )

    await message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"📅 <b>Сегодня:</b>\n{fmt(today_stats)}\n\n"
        f"📆 <b>За месяц:</b>\n{fmt(month_stats)}",
        parse_mode="HTML",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_stats_inner(update.message, update.effective_user.id)


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Используй /start чтобы начать или /help для справки."
    )


async def delivery_state_unknown_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Перехватывает неизвестные команды внутри WAITING_FOR_DELIVERY — не даёт выпасть из диалога."""
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


def _prefs_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    def label(val: bool) -> str:
        return "✅ ON" if val else "❌ OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🚧 Избегать грунтовок: {label(prefs['avoid_bad_roads'])}",
            callback_data="pref_bad_roads",
        )],
        [InlineKeyboardButton(
            f"🏘 Избегать узких улиц: {label(prefs['avoid_narrow_roads'])}",
            callback_data="pref_narrow",
        )],
        [InlineKeyboardButton(
            f"↪️ Приоритет правых поворотов: {label(prefs['prefer_right_turns'])}",
            callback_data="pref_right_turns",
        )],
    ])


async def cmd_route_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_user_preferences(update.effective_user.id)
    await update.message.reply_text(
        "⚙️ <b>Настройки маршрута</b>\n\nВыбери параметры, которые будут учитываться при построении маршрута:",
        parse_mode="HTML",
        reply_markup=_prefs_keyboard(prefs),
    )


async def pref_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    field_map = {
        "pref_bad_roads": "avoid_bad_roads",
        "pref_narrow": "avoid_narrow_roads",
        "pref_right_turns": "prefer_right_turns",
    }

    field = field_map.get(data)
    if not field:
        return

    prefs = get_user_preferences(user_id)
    new_val = 0 if prefs[field] else 1
    update_user_preference(user_id, field, new_val)
    prefs[field] = bool(new_val)

    await query.edit_message_text(
        "⚙️ <b>Настройки маршрута</b>\n\nВыбери параметры, которые будут учитываться при построении маршрута:",
        parse_mode="HTML",
        reply_markup=_prefs_keyboard(prefs),
    )
    raise ApplicationHandlerStop


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Exception while handling update: %s", context.error, exc_info=context.error
    )


def main() -> None:
    # Завершаем предыдущий экземпляр бота, чтобы избежать 409 Conflict
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

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    init_db()

    persistence = PicklePersistence(filepath="bot_persistence.pkl")
    app = Application.builder().token(token).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
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
                # перехватываем любые команды внутри состояния — не выпадаем из диалога
                MessageHandler(filters.COMMAND, delivery_state_unknown_cmd),
            ],
            WAITING_FOR_COURIERS: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_couriers_input),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
            CallbackQueryHandler(button_handler),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_handler),
        ],
        allow_reentry=True,
        name="route_conversation",
        persistent=True,
    )

    app.add_handler(CallbackQueryHandler(pref_callback_handler, pattern="^pref_"), group=-1)
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("route_prefs", cmd_route_prefs))
    app.add_error_handler(error_handler)

    # Force-close any stale Telegram long-poll session from a previous instance.
    # Without this, the new process gets 409 Conflict until the old session times out.
    try:
        _url = f"https://api.telegram.org/bot{token}/getUpdates"
        _data = json.dumps({"offset": -1, "timeout": 0}).encode("utf-8")
        _req = urllib.request.Request(
            _url, data=_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(_req, timeout=5) as _r:
            logger.info("Pre-start session evict: ok=%s", json.loads(_r.read()).get("ok"))
    except Exception as _e:
        logger.warning("Pre-start session evict failed (non-fatal): %s", _e)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
