import os
import math
import logging
import random
import urllib.request
import urllib.parse
from telegram import Update
from telegram.ext import (
    Application,
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

WAITING_FOR_START = 1
WAITING_FOR_DELIVERY = 2
CONFIRM_START = 3

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deliveries", None)
    saved = context.user_data.get("старт")

    if saved:
        await update.message.reply_text(
            "🚀 Начинаем маршрут.\n\n"
            "Стартовая точка сохранена с прошлого раза.\n"
            "Стартуем снова отсюда? (да/нет)",
        )
        return CONFIRM_START
    else:
        await update.message.reply_text(
            "Привет! 🚀 Начинаем маршрут.\n\n"
            "Сначала отправь ссылку на место старта\n"
            "(склад, офис или дом)\n\n" + HOW_TO_GET_LINK,
            parse_mode="HTML",
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
        logger.info("User %s confirmed start, moving to WAITING_FOR_DELIVERY", update.effective_user.id)
        await _ask_for_delivery(update, context, first=True)
        return WAITING_FOR_DELIVERY
    else:
        context.user_data.pop("старт", None)
        logger.info("User %s declined start, moving to WAITING_FOR_START", update.effective_user.id)
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

    coord = parse_yandex_link(text)
    if not coord:
        await update.message.reply_text(
            "❌ Не смог прочитать ссылку.\n"
            "Попробуй ещё раз — зажми место на карте,\n"
            "нажми Поделиться и скопируй ссылку.",
        )
        return WAITING_FOR_START

    context.user_data["старт"] = coord
    context.user_data["deliveries"] = []
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
        await update.message.reply_text(
            f"📍 Точка {n} добавлена!\n"
            "Отправь следующую ссылку или напиши <b>Готово</b>",
            parse_mode="HTML",
        )


async def handle_delivery_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()

    if text.lower() in ("готово", "готов", "go", "done"):
        return await finish_route(update, context)

    coord = parse_yandex_link(text)
    if not coord:
        await update.message.reply_text(
            "❌ Не смог прочитать ссылку.\n"
            "Попробуй ещё раз — зажми место на карте,\n"
            "нажми Поделиться и скопируй ссылку.\n\n"
            "Или напиши <b>Готово</b> чтобы построить маршрут из уже добавленных точек.",
            parse_mode="HTML",
        )
        return WAITING_FOR_DELIVERY

    deliveries = context.user_data.setdefault("deliveries", [])
    if len(deliveries) >= 50:
        await update.message.reply_text(
            "⚠️ Максимум 50 точек доставки. Напиши <b>Готово</b> для расчёта.",
            parse_mode="HTML",
        )
        logger.info("User %s hit delivery limit (50)", update.effective_user.id)
        return WAITING_FOR_DELIVERY

    deliveries.append(coord)
    logger.info(
        "User %s added delivery point #%d, state=WAITING_FOR_DELIVERY",
        update.effective_user.id,
        len(deliveries),
    )
    await _ask_for_delivery(update, context, first=False)
    return WAITING_FOR_DELIVERY


async def finish_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    deliveries: list[tuple[float, float]] = context.user_data.get("deliveries", [])

    if not deliveries:
        await update.message.reply_text(
            "⚠️ Ты не добавил ни одной точки доставки.\nОтправь ссылку из Яндекс Карт."
        )
        return WAITING_FOR_DELIVERY

    status_msg = await update.message.reply_text("⚙️ Оптимизирую маршрут...")

    start_coord = context.user_data["старт"]
    all_coords = [start_coord] + deliveries

    route_order, total_meters = solve_tsp_with_start(all_coords)
    random_km = random_route_distance(all_coords)

    total_km = total_meters / 1000
    savings_km = max(0.0, random_km - total_km)
    time_min = int(total_km / AVG_SPEED_KMH * 60)

    seen = set()
    delivery_steps = []
    for i in route_order:
        if i != 0 and i not in seen:
            delivery_steps.append(i)
            seen.add(i)

    numbered = "\n".join(
        f"{idx + 1}. Точка {i}" for idx, i in enumerate(delivery_steps)
    )

    yandex_url = build_yandex_nav_url(all_coords, route_order)

    if savings_km > 0:
        day_savings = round(savings_km * 12)
        month_savings = round(day_savings * 30)
        year_savings = round(month_savings * 12)
        savings_block = (
            f"💰 Экономия: <b>{savings_km:.1f} км</b>\n"
            f"⛽️ Экономия топлива:\n"
            f"   ~{day_savings} руб. в день\n"
            f"   ~{month_savings} руб. в месяц\n"
            f"   ~{year_savings} руб. в год\n\n"
        )
    else:
        savings_block = ""

    result = (
        "🚀 Старт сохранён\n\n"
        f"📍 <b>Оптимальный маршрут:</b>\n{numbered}\n"
        "🏁 Возврат на старт\n\n"
        f"📏 Расстояние: <b>{total_km:.1f} км</b>\n"
        f"⏱ Время: <b>{time_min} мин</b> (30 км/ч)\n"
        + savings_block +
        f"🗺 <b>Открыть в навигаторе:</b>\n{yandex_url}"
    )

    await status_msg.edit_text(result, parse_mode="HTML")
    context.user_data.pop("deliveries", None)
    return ConversationHandler.END


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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    persistence = PicklePersistence(filepath="bot_persistence.pkl")
    app = Application.builder().token(token).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
        ],
        states={
            CONFIRM_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm_start)
            ],
            WAITING_FOR_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_link)
            ],
            WAITING_FOR_DELIVERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delivery_link),
                # перехватываем любые команды внутри состояния — не выпадаем из диалога
                MessageHandler(filters.COMMAND, delivery_state_unknown_cmd),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("new", cmd_new),
            CommandHandler("changehome", cmd_changehome),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_handler),
        ],
        allow_reentry=True,
        name="route_conversation",
        persistent=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
