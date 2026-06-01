# TripOptimizer — Архитектурная спецификация

Документ описывает наиболее сложные, асинхронные и неочевидные узлы системы.
Установка и запуск — см. `README.md`.

---

## 1. Архитектурный каркас (OOP / SOLID)

Пакеты развязаны через абстракции; зависимости направлены к интерфейсам, а не к
реализациям (Dependency Inversion).

| Слой | Пакет | Ключевая абстракция | Реализации |
|------|-------|---------------------|------------|
| Скрапинг | `src/scrapers` | `BaseScraper` (ABC) | `AviasalesScraper`, `RzdScraper` |
| Координация сбора | `src/scrapers/manager.py` | `ScraperManager` зависит от `BaseScraper` | — |
| Доступ к данным | `src/database/repositories` | паттерн Repository | `TicketRepository`, `TaskRepository`, `AdminRepository` |
| Сессии БД | `src/database/manager.py` | `DatabaseSessionManager` | async-движок SQLAlchemy 2.0 |
| Оркестрация | `src/orchestration` | `LegPlanner` (ABC) | `SequentialLegPlanner`, `AllPairsLegPlanner` |
| Конфигурация | `src/config` | `Settings`, `CityCatalog` | env + `cities.json` |

Принципы в действии:

- **SRP** — каждый репозиторий обслуживает одну таблицу; презентация бота вынесена
  в `keyboards.py`, маршрутизация — в `handlers.py`.
- **OCP** — новый источник билетов = новый подкласс `BaseScraper`; `ScraperManager`
  и оркестратор не меняются. Новая стратегия дробления маршрута = новый `LegPlanner`.
- **DIP** — `TaskOrchestrator` принимает `DatabaseSessionManager`, фабрику
  `ScraperManager` и `LegPlanner` через конструктор (внедрение зависимостей);
  тяжёлый импорт Playwright выполняется **лениво** (PEP 562 в `src/scrapers/__init__.py`
  и фабрика по умолчанию), поэтому слой БД/оркестрации импортируется без Playwright.
- **LSP** — `AviasalesScraper`/`RzdScraper` взаимозаменяемы за `BaseScraper`
  (контекст-менеджер + `fetch(...)`).

---

## 2. Перехват сети Playwright (сетевой «wiretap»)

Целевые сайты — SPA: результаты приходят фоновыми XHR/Fetch в виде JSON. DOM
эфемерный и обфусцированный, поэтому он **не парсится** — слушается сеть.

### 2.1 Шаблонный метод `BaseScraper.fetch`
1. `_start()` поднимает Playwright + `playwright-stealth`, открывает контекст с
   прокси (`ProxySettings.as_playwright_proxy()`).
2. На страницу вешается **блокировщик ресурсов** `page.route("**/*", handler)`:
   запросы типов `image`, `media`, `font`, `stylesheet` и трекинг-домены —
   `route.abort()`; остальное — `route.continue_()`. Это главный рычаг экономии
   резидентного прокси (по сети идёт только полезный JSON и каркас SPA).
3. Вешается перехватчик `page.on("response", _on_response)`.
4. Открывается `build_search_url(...)` (подкласс кодирует IATA/express-коды и дату).

### 2.2 Отбор полезных ответов
`_on_response` для каждого ответа вызывает абстрактный `is_results_response(url)`:
- Aviasales — хосты `aviasales`/`travelpayouts` + подстроки (`/v3/search`,
  `ticket_proposals`, …);
- РЖД — хост `rzd.ru` + подстроки (`trainpricing`, `/searchresults`, …).

При совпадении тело декодируется (`response.json()`) и нормализуется
`parse_payload(...)` → список `TicketDTO` (единый формат для всех источников).

### 2.3 Ранний выход (экономия «хвоста» long-poll)
- `first_results: asyncio.Event` взводится, как только перехвачена первая полезная
  партия билетов.
- Ожидание: `asyncio.wait_for(first_results.wait(), timeout=nav_timeout_ms)`.
  При успехе — короткое окно добора `RESULTS_GRACE_MS`, затем страница
  закрывается, не оплачивая «хвост» long-poll. При таймауте — фоллбэк
  `wait_for_results(page)` (best-effort: селектор карточки → `networkidle`).

### 2.4 Тёплый кэш на пакет (`collect_iter`)
`ScraperManager.collect_iter(routes, health_sink=...)` открывает контексты всех
скраперов **один раз** на весь пакет (`AsyncExitStack`); маршруты обходятся
последовательно (HTTP-кэш браузера остаётся тёплым — тяжёлые JS-бандлы качаются
единожды), источники по маршруту — параллельно (`asyncio.gather`). Метод —
async-генератор: `yield (маршрут, билеты)` сразу по готовности плеча (точный
прогресс), `health_sink` фиксирует здоровье источника (Модуль 7C).

---

## 3. Жизненный цикл async-задачи и SSE-конвейер

Модель конкурентности: один процесс, один event loop, одна нативная
`asyncio.Queue`, один фоновый воркер (целевой масштаб — ≤ 2 пользователей, без
Celery/Redis).

### 3.1 Постановка
1. `POST /api/tasks` (`src/api.py`) валидирует тело Pydantic-схемой `SearchRequest`.
2. `TaskOrchestrator.submit(user_inputs)`:
   - `TaskRepository.create_task(...)` → строка `SearchTask` в статусе `PENDING`
     (UUID генерируется приложением, известен сразу);
   - заводится `ProgressState` (in-memory): `total_legs = промежуточные + 1`;
   - `task_id` кладётся в `asyncio.Queue`.
3. Ответ `202 Accepted` с `task_id` — мгновенно, не дожидаясь обработки.

### 3.2 Обработка (фоновый воркер)
`_worker_loop` бесконечно `await queue.get()` → `_process_task`:

```
PENDING ─► SCRAPING ─► OPTIMIZING ─► COMPLETED
                    └─────────────► FAILED   (любое исключение)
```

- `SCRAPING`: статус пишется в БД (`update_task_status`), план плеч строится
  `LegPlanner.plan(...)`.
- Сбор `_gather_tickets`: для каждого плеча — проверка кэша
  (`TicketRepository.get_cached_tickets`, TTL 24 ч, фильтр по дате); **попадание**
  засчитывается мгновенно (`Telemetry.record_cache_hit`, прокси не трогается),
  **промах** уходит в `collect_iter`. По готовности каждого плеча
  `completed_legs += 1`, новые билеты пишутся в `TicketCache` (`bulk_save_tickets`).
- `OPTIMIZING`: предварительная сборка маршрута (лучший билет на плечо по метрике
  `money`/`time` + фильтры + `booking_url`). *(Полноценный TSP — отдельный модуль.)*
- `COMPLETED`: `save_task_result(result_route)`; `ProgressState.result` заполняется.
- Исключения: `logger.exception` + `update_task_status(FAILED, error_message=...)`.
- Воркер не падает на ошибке задачи; `CancelledError` пробрасывается для штатной
  остановки (`stop(drain=...)`).

Источники истины: **БД** — статус/результат (переживает рестарт), **in-memory
`ProgressState`** — live-проценты и плечи.

### 3.3 Поток статуса (SSE)
`GET /api/tasks/{task_id}/stream` → `StreamingResponse(media_type="text/event-stream")`.
`_event_generator` каждые `0.5 c` собирает `_snapshot` (реестр прогресса + добор из
БД) и шлёт пакет **только при изменении**:

```json
{ "status": "SCRAPING", "progress_percentage": 50,
  "total_legs": 2, "completed_legs": 1, "result": null }
```

`progress_percentage = int(completed_legs / total_legs * 100)`. Поток закрывается
на терминальном статусе (`COMPLETED`/`FAILED`); при `COMPLETED` в `result` лежит
итоговый маршрут. Веб-UI (`static/main.js`) рисует бар/проценты/скелетоны;
админ-панель использует HTTP-поллинг `/api/admin/overview`.

### 3.4 Композиция ресурсов
FastAPI `lifespan`: на старте — `DatabaseSessionManager` + `TaskOrchestrator`,
`orchestrator.start()` (воркер). На остановке — `stop(drain=True)` (дренаж
очереди) + `dispose()` (пул соединений).

---

## 4. Конечный автомат Telegram-бота (aiogram 3.x)

Бот — самостоятельный процесс (`run_bot.py` → `src/bot/app.py`), поднимает
**собственный** `TaskOrchestrator` + `DatabaseSessionManager` поверх **той же**
PostgreSQL. Зависимости внедряются через `TripOptimizerService` и
`dispatcher["service"]`.

### 4.1 Состояния и линейный сценарий
`StateMachine` (`src/bot/states.py`):
`waiting_for_origin → destination → start_date → surplus → transport → filters`.
Порядок зафиксирован в `STATE_ORDER`.

### 4.2 Навигация и откат «Назад»
- Презентация шага: `keyboards.view_for(state, data)` → `(текст, инлайн-клавиатура)`.
- Каждый шаг, кроме первого, содержит «⬅️ Назад» и «🏠 Главное меню».
- **«Назад»**: `previous_state(current)` ищет текущее состояние в `STATE_ORDER` и
  возвращает предыдущее → `_render(prev)`. На первом шаге кнопки «Назад» нет.
- **«Главное меню»**: `FSMContext.clear()` (сброс данных и состояния) → возврат к
  `waiting_for_origin`.

### 4.3 Зацикленный экран фильтров (тумблеры одной кнопкой)
Состояние `waiting_for_filters` — самоповторяющееся: call-back не двигает сценарий,
а мутирует память диалога и **перерисовывает то же сообщение** (`message.edit_text`):
- 🧳/🎒 — тумблер `require_baggage`;
- 💰/⏱️ — тумблер `optimization_metric` (по умолчанию `money`);
- 🚀 «Оптимизировать маршрут!» — финал.

### 4.4 Подключение к слою задач
На «Оптимизировать»: `_build_payload(data)` (та же схема, что у API,
`intermediate_cities=[]`) → `service.submit(payload)` → квитанция «Мы сообщим…» →
фоновый `asyncio.create_task(_report_when_ready(...))`. Сервис в активном цикле
`wait_for_result` опрашивает `orchestrator.get_progress(task_id)` до `COMPLETED`
(или `FAILED`/`TIMEOUT`) и присылает карточки билетов с кликабельными
Markdown-ссылками (`booking_url`).

---

## 5. Модель данных (PostgreSQL, SQLAlchemy 2.0)

- **`SearchTask`** — UUID PK, `status` (нативный enum `task_status`:
  PENDING/SCRAPING/OPTIMIZING/COMPLETED/FAILED), `user_inputs` (JSONB),
  `result_route` (JSONB, nullable), `error_message`, `created_at`/`updated_at`.
- **`TicketCache`** — BigInteger PK, источник/города/времена/длительность/цена/
  багаж, `created_at` (`server_default=now()`); композитный индекс
  `ix_ticket_cache_lookup (departure_city, arrival_city, departure_time, source)`.
  TTL контролируется на уровне репозитория (24 ч): `get_cached_tickets` игнорирует
  протухшее, `purge_expired` физически удаляет (кнопка админки →
  `POST /api/admin/cache/purge`).
