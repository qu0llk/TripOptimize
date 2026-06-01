# TripOptimizer

Сервис планирования путешествий: на вход — город отправления, город назначения и
до 8 промежуточных городов с числом дней (суммарно ≤ 31). Решает вариацию задачи
коммивояжёра (TSP) и подбирает оптимальный маршрут по стоимости либо по времени с
учётом фильтров (транспорт, багаж, бюджет). Данные собираются вживую с
`aviasales.ru` и `ticket.rzd.ru` через Playwright.

Архитектура и сложные узлы (перехват сети, async-конвейер, SSE, FSM бота)
описаны в **[`ARCHITECTURE_SPEC.md`](ARCHITECTURE_SPEC.md)**.

## Модули

| № | Модуль | Расположение |
|---|--------|--------------|
| 1 | Скрапинг (Playwright + перехват JSON) | `src/scrapers` |
| 2 | Конфигурация + справочник городов мира | `src/config` |
| 3 | БД и модели (PostgreSQL/SQLAlchemy) | `src/database` |
| 4 | Очередь задач и оркестратор (`asyncio.Queue`) | `src/orchestration` |
| 5 | Сборка маршрута (предварительная, точка входа TSP) | `src/orchestration/orchestrator.py` |
| 6 | Telegram-бот (aiogram 3.x) | `src/bot` |
| 7A/7B/7C | API + SSE / веб-интерфейс / админ-панель | `src/api.py`, `templates`, `static` |

---

## 1. Системные требования

- **Python 3.12+**
- **PostgreSQL 14+** (доступный экземпляр)
- **Резидентный прокси** — обязателен: без валидного прокси скрапер немедленно
  завершается с ошибкой (дата-центровые IP мгновенно банятся целевыми сайтами).
- ОС с поддержкой Playwright Chromium (Linux/macOS/Windows).

Поднять PostgreSQL локально через Docker:

```bash
docker run --name tripoptimize-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=tripoptimize -p 5432:5432 -d postgres:16
```

---

## 2. Установка

```bash
# 1. Клонирование
git clone <repository-url> TripOptimize
cd TripOptimize

# 2. Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Зависимости
pip install --upgrade pip
pip install -r requirements.txt
```

### Инициализация Playwright (headless Chromium)

```bash
playwright install chromium
```

> На Linux могут потребоваться системные библиотеки: `playwright install-deps chromium`.

---

## 3. Конфигурация окружения

Скопируйте образец и заполните реальными значениями:

```bash
cp .env.example .env
```

Содержимое `.env.example`:

```dotenv
# --- База данных (Модуль 3) — ОБЯЗАТЕЛЬНО ---
# Только асинхронный драйвер asyncpg: URL обязан начинаться с postgresql+asyncpg://
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/tripoptimize

# --- Резидентный прокси (Модуль 1) — ОБЯЗАТЕЛЬНО ---
# Требуется именно РЕЗИДЕНТНЫЙ прокси; дата-центровые IP блокируются сайтами.
PROXY_HOST=your.residential.proxy.host
PROXY_PORT=8000
PROXY_USER=proxy_user
PROXY_PASS=proxy_password

# --- Telegram-бот (Модуль 6) — обязателен только для run_bot.py ---
TELEGRAM_BOT_TOKEN=123456:ABC-DEF_your_bot_token

# --- Параметры запуска веб-приложения ---
APP_HOST=0.0.0.0
APP_PORT=8000

# --- Опционально (Playwright / экономия трафика) ---
HEADLESS=true            # запуск браузера без GUI
NAV_TIMEOUT_MS=45000     # таймаут навигации Playwright
RESULTS_GRACE_MS=3000    # окно добора после первой партии JSON, затем закрытие
```

> `TELEGRAM_BOT_TOKEN` валидируется отдельно (`require_telegram_token`) — только при
> запуске бота; API и скрапер работают без него. `APP_HOST`/`APP_PORT` используются
> в командах запуска `uvicorn` (см. ниже).

---

## 4. Протоколы запуска

### Шаг 1. Инициализация схемы БД

Создаёт таблицы (`SearchTask`, `TicketCache`) и прогоняет sanity-check репозиториев:

```bash
python init_db.py
```

Коды выхода: `1` — ошибка конфигурации, `2` — БД недоступна/неверный `DATABASE_URL`.

### Шаг 2. Веб-приложение + фоновый воркер очереди

Один процесс `uvicorn` поднимает FastAPI **и** через `lifespan` запускает фоновый
воркер оркестратора (очередь `asyncio.Queue`) — отдельный демон не нужен:

```bash
uvicorn src.api:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}
# разработка: добавьте --reload
```

Доступно после старта:
- Веб-интерфейс — `http://<APP_HOST>:<APP_PORT>/`
- Админ-панель — `http://<APP_HOST>:<APP_PORT>/admin`
- SSE-поток статуса — `GET /api/tasks/{task_id}/stream`

### Шаг 3. Telegram-бот (отдельный процесс)

Бот работает по long-polling и поднимает собственный воркер оркестратора поверх
той же PostgreSQL:

```bash
python run_bot.py
```

---

## 5. Структура проекта

```
TripOptimize/
├── ARCHITECTURE_SPEC.md      # архитектура и сложные узлы
├── README.md
├── requirements.txt
├── .env.example
├── init_db.py                # инициализация схемы БД
├── run_bot.py                # точка входа Telegram-бота
├── main.py                   # демо-прогон скрапера
├── templates/                # index.html, admin.html (Jinja2)
├── static/                   # main.js, admin.js, styles.css (Vanilla)
└── src/
    ├── api.py                # FastAPI: API, SSE, веб, админ-роуты
    ├── schemas.py            # Pydantic SearchRequest
    ├── config/               # settings.py, cities.py, cities.json
    ├── database/             # base, models, manager, repositories/
    ├── orchestration/        # orchestrator, planner, booking, dto
    ├── bot/                  # states, keyboards, service, handlers, app
    └── scrapers/             # base, aviasales, rzd, manager, dto
```

---

## 6. Замечания по эксплуатации

- **Экономия прокси:** блокировка `image/CSS/font/media` + трекеров, ранний выход
  по первой партии JSON, тёплый HTTP-кэш в `collect_iter`, кэш билетов с TTL 24 ч.
  Подробнее — `ARCHITECTURE_SPEC.md` §2.
- **Здоровье скраперов:** админ-панель (`/admin`) флагует «ВНИМАНИЕ / БЛОКИРОВКА»
  при ошибке прокси/капче или серии пустых ответов — индикатор здоровья прокси.
- **Очистка кэша:** кнопка в админке → `POST /api/admin/cache/purge` (удаляет
  записи `TicketCache` старше 24 ч).
