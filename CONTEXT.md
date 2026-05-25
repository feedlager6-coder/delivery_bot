# Контекст проекта для AI-агента

## Что это
Telegram бот для оптимизации маршрутов доставки.
Основной файл: bot.py
Язык: Python 3.11+

## Где запущен
Railway.app — облачный хостинг.
Код хранится на GitHub, Railway автоматически
деплоит при каждом коммите в ветку main.

## Важные особенности Railway
- Файловая система сбрасывается при деплое
- /tmp не сохраняется между деплоями
- Переменные окружения задаются в Railway dashboard
- Логи доступны в Railway → Deployments → Logs
- PID файлы НЕ работают на Railway

## Переменные окружения (уже настроены в Railway)
BOT_TOKEN — токен Telegram бота
YANDEX_GEOCODER_KEY — ключ Яндекс Геокодера
GRAPHHOPPER_API_KEY — ключ GraphHopper API

## Архитектура
- python-telegram-bot 20.x (async)
- OR-Tools для оптимизации маршрутов
- SQLite для статистики (routes.db)
- PicklePersistence для состояний диалога
- Nominatim + Яндекс Геокодер для адресов
- geocode_cache — кэш в памяти

## Правила при внесении изменений
1. НЕ трогать логику парсинга ссылок Яндекс Карт
2. НЕ трогать ConversationHandler states без крайней нужды
3. Все HTTP запросы только через asyncio.to_thread()
4. Все обращения к SQLite через asyncio.to_thread()
5. НЕ использовать PID файлы — не работает на Railway
6. drop_pending_updates=True уже стоит — не убирать
7. После изменений обновить requirements.txt если нужно

## Известные проблемы которые нужно исправить
1. get_address() синхронная — блокирует event loop
   Нужно обернуть в asyncio.to_thread()
2. SQLite вызовы синхронные — то же самое
3. geocode_cache сбрасывается при рестарте
   Лучше хранить в SQLite таблице address_cache
4. Настройки prefer_right_turns и avoid_bad_roads
   сохраняются но не применяются в алгоритме
   Нужен GraphHopper API для их реализации

## Структура файлов
bot.py — основной код бота
routes.db — SQLite база (создаётся автоматически)
bot_persistence.pkl — состояния диалогов
requirements.txt — зависимости
Procfile — команда запуска для Railway
CONTEXT.md — этот файл
