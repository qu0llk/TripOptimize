"""Оркестратор асинхронных задач подбора маршрута (Модули 4 и 7A).

:class:`TaskOrchestrator` — связующее звено между скрапинг-движком (Модуль 1)
и слоем БД (Модуль 3). Он принимает задачи из внутренней ``asyncio.Queue`` и
прогоняет каждую через жизненный цикл:

    PENDING → SCRAPING → OPTIMIZING → COMPLETED
                                    ↘ FAILED (любая ошибка)

В Модуле 7A добавлено отслеживание прогресса в реальном времени: оркестратор
ведёт in-memory реестр :class:`ProgressState` по каждой задаче (всего плеч,
сколько собрано, статус, результат). SSE-эндпоинт читает этот реестр и
транслирует прогресс в браузер.

Целевой масштаб — не более двух одновременных пользователей, поэтому тяжёлая
инфраструктура (Celery, Redis) избыточна: достаточно одного фонового воркера
на нативной ``asyncio.Queue``.

Принципы SOLID соблюдены через инверсию зависимостей: оркестратор зависит от
абстракций (планировщик плеч, менеджер БД, фабрика скрапинг-координатора), а
не от конкретных реализаций. Тяжёлый импорт ``ScraperManager`` (тянущий
Playwright) выполняется лениво — создать оркестратор и слой БД можно без
установленного Playwright.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from datetime import datetime, time, timezone
from typing import Any, Callable

from src.config import Settings
from src.database import (
    DatabaseSessionManager,
    TaskRepository,
    TaskStatus,
    TicketRepository,
)
from src.orchestration.booking import BookingLinkBuilder
from src.orchestration.dto import RouteLeg
from src.orchestration.planner import (
    AllPairsLegPlanner,
    LegPlanner,
    SequentialLegPlanner,
    _extract_cities,
    _extract_days_to_stay,
)
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ProgressState:
    """Снимок прогресса одной задачи (хранится в оперативной памяти воркера)."""

    status: TaskStatus
    total_legs: int
    completed_legs: int = 0
    result: dict[str, Any] | None = None

    @property
    def percentage(self) -> int:
        """Процент готовности: (собрано плеч / всего) * 100."""
        if self.total_legs <= 0:
            return 0
        return int(self.completed_legs / self.total_legs * 100)


class Telemetry:
    """Оперативная телеметрия для админ-панели (Модуль 7C).

    Хранит счётчик кэш-хитов (экономия прокси) и здоровье скраперов. Данные
    in-memory: живут в процессе API, обнуляются при рестарте — этого достаточно
    для real-time мониторинга проксей и анти-бот блокировок.
    """

    #: Сколько подряд пустых ответов трактуем как вероятную блокировку.
    EMPTY_STREAK_THRESHOLD = 3

    def __init__(self) -> None:
        self.cache_hits = 0
        self._scrapers: dict[str, dict[str, Any]] = {}

    def record_cache_hit(self, count: int = 1) -> None:
        """Учитывает плечи, отданные из кэша вместо живого прокси."""
        self.cache_hits += count

    def record_scraper(
        self, source: str, *, count: int, error: str | None = None
    ) -> None:
        """Фиксирует исход одного запроса скрапера для трекера здоровья."""
        entry = self._scrapers.setdefault(
            source,
            {"status": "ok", "last_error": None, "last_error_at": None, "empty_streak": 0},
        )
        now = datetime.now(timezone.utc).isoformat()
        if error:
            entry.update(status="warn", last_error=error[:200], last_error_at=now)
            return
        if count == 0:
            entry["empty_streak"] += 1
            if entry["empty_streak"] >= self.EMPTY_STREAK_THRESHOLD:
                entry.update(
                    status="warn",
                    last_error="Подряд пустые ответы — вероятна блокировка/капча",
                    last_error_at=now,
                )
        else:
            entry.update(status="ok", empty_streak=0)

    def snapshot(self) -> dict[str, Any]:
        """Возвращает копию телеметрии для отдачи в админ-API."""
        return {
            "cache_hits": self.cache_hits,
            "scrapers": {k: dict(v) for k, v in self._scrapers.items()},
        }


class TaskOrchestrator:
    """Фоновый исполнитель задач подбора маршрута на базе ``asyncio.Queue``.

    Жизненный цикл воркера управляется методами :meth:`start` и :meth:`stop`,
    что удобно встраивается в события ``startup``/``shutdown`` FastAPI.
    """

    def __init__(
        self,
        db_manager: DatabaseSessionManager,
        *,
        leg_planner: LegPlanner | None = None,
        scraper_manager_factory: Callable[[], Any] | None = None,
        settings: Settings | None = None,
        queue_maxsize: int = 0,
    ) -> None:
        """Создаёт оркестратор.

        Args:
            db_manager: Менеджер сессий БД (Модуль 3).
            leg_planner: Стратегия построения плеч. По умолчанию —
                :class:`SequentialLegPlanner` (плеч = промежуточных + 1).
            scraper_manager_factory: Фабрика ``ScraperManager`` на каждую
                задачу. По умолчанию импортируется лениво и проверяет
                конфигурацию прокси при вызове. Подменяется в тестах.
            settings: Конфигурация приложения (опционально).
            queue_maxsize: Ограничение очереди (0 — без ограничения).
        """
        self._db = db_manager
        self._settings = settings
        self._leg_planner = leg_planner or AllPairsLegPlanner()
        self._scraper_manager_factory: Callable[..., Any] = (
            scraper_manager_factory or self._default_scraper_manager_factory
        )
        self._booking = BookingLinkBuilder()
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker_task: asyncio.Task[None] | None = None
        #: Реестр прогресса задач (in-memory). Доступ — из одного event loop.
        self._progress: dict[uuid.UUID, ProgressState] = {}
        #: Оперативная телеметрия для админ-панели (Модуль 7C).
        self._telemetry = Telemetry()

    @property
    def telemetry(self) -> Telemetry:
        """Оперативная телеметрия (кэш-хиты, здоровье скраперов)."""
        return self._telemetry

    # ------------------------------------------------------------------ #
    # Управление жизненным циклом фонового воркера
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Запускает фоновый воркер (idempotent — повторный вызов безопасен)."""
        if self._worker_task is not None and not self._worker_task.done():
            logger.debug("Воркер оркестратора уже запущен")
            return
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="task-orchestrator-worker"
        )
        logger.info("Фоновый воркер оркестратора запущен")

    async def stop(self, *, drain: bool = False) -> None:
        """Останавливает воркер, корректно завершая текущую работу.

        Args:
            drain: Если ``True`` — дождаться опустошения очереди перед остановкой.
        """
        if self._worker_task is None:
            return
        if drain:
            await self._queue.join()
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None
        logger.info("Фоновый воркер оркестратора остановлен")

    # ------------------------------------------------------------------ #
    # Постановка задач в очередь
    # ------------------------------------------------------------------ #
    async def submit(self, user_inputs: dict[str, Any]) -> uuid.UUID:
        """Создаёт задачу в БД (``PENDING``), ставит в очередь, заводит прогресс.

        Returns:
            Идентификатор задачи — клиент может сразу подписаться на её поток.
        """
        async with self._db.session() as session:
            task = await TaskRepository(session).create_task(user_inputs)
            task_id = task.id

        # Всего плеч известно сразу из ввода (промежуточные + 1) — заводим
        # запись прогресса ещё до обработки, чтобы поток отдавал данные тут же.
        total_legs = max(len(_extract_cities(user_inputs)) - 1, 0)
        self._progress[task_id] = ProgressState(
            status=TaskStatus.PENDING, total_legs=total_legs
        )

        await self._queue.put(task_id)
        logger.info("Задача %s в очереди (плеч=%d)", task_id, total_legs)
        return task_id

    def get_progress(self, task_id: uuid.UUID) -> ProgressState | None:
        """Возвращает снимок прогресса задачи или ``None``, если он неизвестен."""
        return self._progress.get(task_id)

    @property
    def queue_size(self) -> int:
        """Текущее число задач, ожидающих обработки."""
        return self._queue.qsize()

    # ------------------------------------------------------------------ #
    # Внутренняя логика воркера
    # ------------------------------------------------------------------ #
    async def _worker_loop(self) -> None:
        """Бесконечный цикл: извлекает задачи из очереди и обрабатывает их."""
        logger.info("Цикл воркера запущен, ожидание задач…")
        while True:
            task_id = await self._queue.get()
            try:
                await self._process_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — воркер не должен падать
                logger.exception("Необработанный сбой воркера на задаче %s", task_id)
            finally:
                self._queue.task_done()

    async def _process_task(self, task_id: uuid.UUID) -> None:
        """Прогоняет одну задачу через весь жизненный цикл."""
        try:
            user_inputs = await self._mark_scraping(task_id)
            legs = self._leg_planner.plan(user_inputs)
            # Прогресс считается по уникальным парам городов, а не по числу
            # дат-плеч (каждая пара скрапится за один «шаг» с точки зрения UX).
            unique_pairs = len(
                dict.fromkeys((leg.departure_city, leg.arrival_city) for leg in legs)
            )
            self._set_progress(task_id, status=TaskStatus.SCRAPING, total=unique_pairs)

            transport_type = (user_inputs.get("filters") or {}).get("transport_type", "both")
            leg_tickets = await self._gather_tickets(task_id, legs, transport_type=transport_type)

            await self._update_status(task_id, TaskStatus.OPTIMIZING)
            self._set_progress(task_id, status=TaskStatus.OPTIMIZING)

            # ПРЕДВАРИТЕЛЬНАЯ сборка маршрута: без переупорядочивания (полноценный
            # TSP — Модуль 5). Нужна, чтобы фронтенд уже сейчас показывал результат.
            result = self._assemble_itinerary(legs, leg_tickets, user_inputs)

            await self._save_result(task_id, result)
            self._set_progress(
                task_id, status=TaskStatus.COMPLETED, result=result
            )
            logger.info("Задача %s завершена", task_id)
        except Exception as exc:  # noqa: BLE001 — фиксируем причину в БД и прогрессе
            logger.exception("Задача %s завершилась ошибкой", task_id)
            await self._update_status(
                task_id, TaskStatus.FAILED, error_message=str(exc)
            )
            self._set_progress(task_id, status=TaskStatus.FAILED)

    async def _mark_scraping(self, task_id: uuid.UUID) -> dict[str, Any]:
        """Переводит задачу в ``SCRAPING`` и возвращает её пользовательский ввод."""
        async with self._db.session() as session:
            task = await TaskRepository(session).update_task_status(
                task_id, TaskStatus.SCRAPING
            )
            if task is None:
                raise LookupError(f"Задача {task_id} не найдена в БД.")
            return dict(task.user_inputs)

    async def _gather_tickets(
        self, task_id: uuid.UUID, legs: list[RouteLeg], *, transport_type: str = "both"
    ) -> dict[tuple[str, str], list[TicketDTO]]:
        """Собирает билеты по плечам, переиспользуя кэш ради экономии прокси.

        Плечи со свежими (моложе TTL) билетами в кэше не скрапятся повторно —
        главный рычаг экономии трафика. Живой сбор идёт потоково
        (``collect_iter``): прогресс инкрементируется по мере готовности
        каждого плеча. Возвращает билеты, сгруппированные по паре городов.
        """
        result: dict[tuple[str, str], list[TicketDTO]] = {}
        to_scrape: list[RouteLeg] = []
        # Пары, по которым прогресс уже учтён (чтобы не инкрементировать дважды).
        pairs_incremented: set[tuple[str, str]] = set()

        async with self._db.session() as session:
            repo = TicketRepository(session)
            for leg in legs:
                day_start, day_end = self._day_bounds(leg)
                cached = await repo.get_cached_tickets(
                    departure_city=leg.departure_city,
                    arrival_city=leg.arrival_city,
                    departure_from=day_start,
                    departure_to=day_end,
                )
                pair = (leg.departure_city, leg.arrival_city)
                if cached:
                    if pair not in result:
                        result[pair] = []
                    result[pair].extend(self._cache_to_dto(c) for c in cached)
                    self._telemetry.record_cache_hit()
                    if pair not in pairs_incremented:
                        pairs_incremented.add(pair)
                        self._inc_completed(task_id)
                else:
                    to_scrape.append(leg)

        if not to_scrape:
            logger.info("Все плечи найдены в кэше (%d дат-запросов)", len(legs))
            return result

        logger.info(
            "Кэш-промах по %d дат-плечам (%d уникальных пар из %d) — живой сбор",
            len(to_scrape),
            len(dict.fromkeys((l.departure_city, l.arrival_city) for l in to_scrape)),
            len(legs),
        )
        manager = self._scraper_manager_factory(transport_type)
        async for (dep, arr, _date), tickets in manager.collect_iter(
            [leg.as_tuple() for leg in to_scrape],
            health_sink=self._telemetry.record_scraper,
        ):
            pair = (dep, arr)
            if pair not in result:
                result[pair] = []
            result[pair].extend(tickets)
            if tickets:
                async with self._db.session() as session:
                    await TicketRepository(session).bulk_save_tickets(tickets)
            if pair not in pairs_incremented:
                pairs_incremented.add(pair)
                self._inc_completed(task_id)

        return result

    def _assemble_itinerary(
        self,
        legs: list[RouteLeg],
        leg_tickets: dict[tuple[str, str], list[TicketDTO]],
        user_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Собирает маршрут через оптимизатор с учётом дней стоя и запаса."""
        from src.optimization.optimizer import find_optimal_route

        filters = user_inputs.get("filters") or {}
        metric = filters.get("optimization_metric", "money")

        cities = _extract_cities(user_inputs)
        origin = cities[0]
        destination = cities[-1]
        intermediates = list(dict.fromkeys(cities[1:-1]))

        # Словарь: город → минимальный стой (для передачи в TSP-перебор).
        intermediate_input = (
            user_inputs.get("intermediate_cities")
            or user_inputs.get("intermediate")
            or []
        )
        days_by_city = {
            item["city"].strip(): int(item.get("days_to_stay") or item.get("days") or 0)
            for item in intermediate_input
            if (item.get("city") or "").strip()
        }

        surplus_days = int(user_inputs.get("surplus_days") or 0)

        # Фильтруем билеты по всем собранным парам.
        all_pairs = set(leg_tickets.keys())
        filtered_by_pair = {
            pair: self._filter_candidates(leg_tickets[pair], filters)
            for pair in all_pairs
        }

        best_path, best_sequence, all_permutations = find_optimal_route(
            origin=origin,
            destination=destination,
            intermediates=intermediates,
            days_by_city=days_by_city,
            tickets_by_pair=filtered_by_pair,
            start_date_str=user_inputs.get("start_date") or "",
            surplus_days=surplus_days,
            metric=metric,
            collect_all=True,
        )

        chosen: list[dict[str, Any] | None] = []
        total_price = 0
        total_duration = 0
        for idx, ticket in enumerate(best_path):
            if ticket is None:
                # Пытаемся дать пользователю понятную причину, чтобы «Билеты
                # не найдены» не выглядело как сбой системы. Смотрим на пару
                # городов для текущего плеча в выбранной последовательности.
                from_city = best_sequence[idx] if idx < len(best_sequence) else "?"
                to_city = (
                    best_sequence[idx + 1]
                    if idx + 1 < len(best_sequence)
                    else "?"
                )
                # Передаём ОБА среза: сырой (после скрапера) и отфильтрованный
                # (после фильтров пользователя). Это позволяет точно отличить
                # «билетов нет в принципе» от «все билеты отсеяны фильтрами».
                raw_tickets = leg_tickets.get((from_city, to_city), [])
                filtered_tickets = filtered_by_pair.get((from_city, to_city), [])
                # Если до этого плеча УЖЕ есть успешный билет — это
                # «последующее» плечо, и частая причина провала — оптимизатор
                # не нашёл стыковки (билеты есть, но стартуют раньше, чем
                # прилетает предыдущий поезд). Это надо объяснить отдельно.
                is_follow_on = any(
                    isinstance(c, dict) and not c.get("__empty__")
                    for c in chosen[:idx]
                )
                reason = self._explain_empty_leg(
                    from_city,
                    to_city,
                    raw_tickets,
                    filtered_tickets,
                    filters,
                    is_follow_on=is_follow_on,
                )
                chosen.append({"__empty__": True, "reason": reason})
                continue
            # Скрапер мог уже проставить рабочий deep-link (например, РЖД отдаёт
            # ссылку на страницу расписания tutu для любого города). Если ссылки
            # нет — строим её через BookingLinkBuilder (путь Aviasales).
            booking_url = ticket.booking_url or self._booking.build(ticket)
            ticket = dataclasses.replace(ticket, booking_url=booking_url)
            chosen.append(ticket.to_dict())
            total_price += ticket.price
            total_duration += ticket.duration_minutes

        empty_count = sum(1 for c in chosen if isinstance(c, dict) and c.get("__empty__"))
        result: dict[str, Any] = {
            "order": best_sequence,
            "optimization_metric": metric,
            "legs": chosen,
            "total_price": total_price,
            "total_duration_minutes": total_duration,
            "tree": self._build_tree(all_permutations, metric),
        }
        # Если ВСЕ плечи пустые — даём общую причину, чтобы пользователь
        # понял, что проблема не в одной паре, а в маршруте целиком (например,
        # у оптимизатора не нашлось стыковок в окне дат).
        if chosen and empty_count == len(chosen):
            result["global_reason"] = self._explain_global_empty(
                chosen, best_sequence, surplus_days
            )
        return result

    @staticmethod
    def _build_tree(
        permutations: list[dict[str, Any]], metric: str
    ) -> dict[str, Any]:
        """Строит древовидную структуру перестановок для визуализации.

        Корень дерева — город отправления. Каждый уровень — один шаг (город
        в порядке перестановки). Листья — финальные города. Узлы сортируются
        по стоимости/длительности (по выбранной метрике), выбранный маршрут
        помечается флагом ``is_chosen``.

        Returns:
            Словарь с ``root`` (узел-дерево) и ``metric``.
        """
        if not permutations:
            return {"root": None, "metric": metric, "total": 0}

        def cost_key(node: dict[str, Any]) -> float:
            """Ключ сортировки по активной метрике (неполные пути — в конец)."""
            if not node["is_complete"]:
                # Большое значение, но согласованное для tie-break по цене.
                return float("inf")
            if metric == "time":
                return float(node["total_duration_minutes"])
            return float(node["total_price"])

        # Группируем по первому промежуточному городу (после origin).
        # Уровни дерева: 0 = origin, 1 = первый промежуточный, 2 = второй, …
        # Последний «лист» = destination.
        origin = permutations[0]["sequence"][0]
        destination = permutations[0]["sequence"][-1]

        def make_node(city: str, depth: int) -> dict[str, Any]:
            """Рекурсивно строит узел дерева, начиная с ``city`` на уровне ``depth``."""
            # Собираем все перестановки, у которых путь на позиции ``depth`` — ``city``.
            # position[depth] = city, далее идёт суффикс.
            subseqs = []
            for p in permutations:
                seq = p["sequence"]
                if depth < len(seq) and seq[depth] == city:
                    subseqs.append(p)
            # Сортируем потомков (по суффиксу на следующем уровне) по метрике.
            subseqs.sort(key=cost_key)

            is_leaf = depth + 1 >= len(subseqs[0]["sequence"]) if subseqs else True
            # Метрики самого узла — берём минимум по его потомкам (если есть),
            # иначе минимум по текущему уровню.
            if subseqs:
                best_metric_value = min(
                    cost_key(s) for s in subseqs if s["is_complete"]
                ) if any(s["is_complete"] for s in subseqs) else float("inf")
                best_incomplete = min(
                    (s for s in subseqs if not s["is_complete"]),
                    key=lambda s: (s["total_price"], s["total_duration_minutes"]),
                    default=None,
                )
                is_chosen = any(s.get("is_chosen") for s in subseqs)
                # Лучший полный потомок (для tooltip'а).
                best_complete = next(
                    (s for s in subseqs if s["is_complete"]), None
                )
            else:
                best_metric_value = float("inf")
                best_incomplete = None
                is_chosen = False
                best_complete = None

            children = []
            if not is_leaf:
                # Группируем по следующему городу.
                next_cities = sorted({s["sequence"][depth + 1] for s in subseqs})
                for nc in next_cities:
                    children.append(make_node(nc, depth + 1))

            return {
                "city": city,
                "is_chosen": is_chosen,
                "is_complete": bool(best_complete) or (is_leaf and bool(subseqs) and subseqs[0]["is_complete"]),
                "metric_value": best_metric_value,
                "preview": (
                    {
                        "total_price": best_complete["total_price"],
                        "total_duration_minutes": best_complete["total_duration_minutes"],
                        "sequence": best_complete["sequence"],
                    }
                    if best_complete
                    else (
                        {
                            "total_price": best_incomplete["total_price"],
                            "total_duration_minutes": best_incomplete["total_duration_minutes"],
                            "sequence": best_incomplete["sequence"],
                        }
                        if best_incomplete
                        else None
                    )
                ),
                "children": children,
            }

        root = make_node(origin, 0)
        return {
            "root": root,
            "destination": destination,
            "metric": metric,
            "total": len(permutations),
        }

    @staticmethod
    def _explain_global_empty(
        chosen: list[dict[str, Any]],
        best_sequence: list[str],
        surplus_days: int,
    ) -> str:
        """Объясняет, почему ВСЕ плечи оказались пустыми.

        Сейчас самая частая причина — оптимизатор не смог «разложить» билеты
        по дням в пределах запаса. Сообщаем пользователю, что можно попробовать
        увеличить surplus_days или снять промежуточные города.
        """
        # Если ВСЕ причины — "не попадают в окно дат", подсказка про запас.
        reasons = [c.get("reason", "") for c in chosen]
        if all("окно дат" in r for r in reasons):
            return (
                f"Билеты есть, но оптимизатор не смог уложить маршрут из "
                f"{len(best_sequence) - 1} плеч в окно "
                f"start_date + {surplus_days} дн. запаса. "
                "Попробуйте увеличить surplus_days или убрать промежуточные города."
            )
        if all("Нет прямых" in r for r in reasons):
            return (
                "Ни на одном плече нет прямого сообщения выбранным видом транспорта."
            )
        return "Не удалось собрать полный маршрут — см. причины по каждому плечу."

    @staticmethod
    def _explain_empty_leg(
        from_city: str,
        to_city: str,
        raw_tickets: list[TicketDTO],
        filtered_tickets: list[TicketDTO],
        filters: dict[str, Any],
        *,
        is_follow_on: bool = False,
    ) -> str:
        """Возвращает человекочитаемую причину отсутствия билета на плече.

        Используется в финальном результате как ``reason`` для пустого плеча —
        чтобы фронтенд мог показать «нет прямых поездов X→Y» вместо сухого
        «Билеты не найдены». Не выбрасывает исключений и всегда возвращает
        строку — это best-effort диагностика.

        Args:
            from_city / to_city: Концы плеча в выбранной последовательности.
            raw_tickets: Билеты, отданные скрапером (без фильтров пользователя).
            filtered_tickets: Те же билеты после применения фильтров.
                Если raw непуст, а filtered пуст — причина в фильтрах; иначе —
                в самом маршруте (нет прямого сообщения).
            is_follow_on: ``True``, если это плечо идёт ПОСЛЕ уже найденного
                билета. Тогда частая причина провала — билеты есть, но
                отправляются раньше, чем прилетает предыдущий поезд, и
                оптимизатор не смог найти стыковку.
        """
        # 1) Скрапер не вернул ни одного билета — нет прямого сообщения между
        #    городами (либо город не резолвится в код РЖД / IATA).
        if not raw_tickets:
            return f"Нет прямых поездов {from_city} → {to_city}."
        # 2) Сырые билеты есть, но все отсеяны фильтрами пользователя.
        if not filtered_tickets:
            if (filters.get("transport_type") or "both") != "both":
                if filters.get("transport_type") == "train":
                    return (
                        f"Все билеты {from_city} → {to_city} — не поезд "
                        "(фильтр: только поезд)."
                    )
                if filters.get("transport_type") == "plane":
                    return (
                        f"Все билеты {from_city} → {to_city} — не самолёт "
                        "(фильтр: только самолёт)."
                    )
            if filters.get("require_baggage"):
                return (
                    f"Все билеты {from_city} → {to_city} отсеяны фильтром «багаж»."
                )
            max_budget = filters.get("max_budget")
            if isinstance(max_budget, int) and max_budget > 0:
                return (
                    f"Все билеты {from_city} → {to_city} дороже {max_budget} ₽ "
                    "(фильтр бюджета)."
                )
            return f"Все билеты {from_city} → {to_city} отсеяны фильтрами."
        # 3) Билеты есть и прошли фильтры, но оптимизатор не смог их
        #    встроить в маршрут. Две принципиально разные причины —
        #    объясняем пользователю честно.
        if is_follow_on:
            return (
                f"Поезд {from_city} → {to_city} не успевает на стыковку с "
                "предыдущим плечом: либо нет рейсов после прилёта, либо "
                "предыдущий поезд прибывает слишком поздно."
            )
        return (
            f"Билеты {from_city} → {to_city} есть, но оптимизатор не смог "
            "уложить их в выбранное окно дат. Попробуйте увеличить surplus_days."
        )

    @staticmethod
    def _filter_candidates(
        tickets: list[TicketDTO], filters: dict[str, Any]
    ) -> list[TicketDTO]:
        """Отсеивает билеты по фильтрам пользователя (транспорт, багаж, бюджет)."""
        transport = filters.get("transport_type", "both")
        require_baggage = bool(filters.get("require_baggage", False))
        max_budget = filters.get("max_budget")

        source_by_transport = {"plane": "aviasales", "train": "rzd"}
        wanted_source = source_by_transport.get(transport)

        out: list[TicketDTO] = []
        for t in tickets:
            if wanted_source and t.source != wanted_source:
                continue
            if require_baggage and not t.has_baggage:
                continue
            if max_budget is not None and t.price > int(max_budget):
                continue
            out.append(t)
        return out

    @staticmethod
    def _pick_best(tickets: list[TicketDTO], metric: str) -> TicketDTO | None:
        """Выбирает лучший билет: по цене (``money``) либо длительности (``time``)."""
        if not tickets:
            return None
        if metric == "time":
            return min(tickets, key=lambda t: t.duration_minutes)
        return min(tickets, key=lambda t: t.price)

    async def _update_status(
        self,
        task_id: uuid.UUID,
        status: TaskStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        """Атомарно обновляет статус задачи в отдельной сессии."""
        async with self._db.session() as session:
            await TaskRepository(session).update_task_status(
                task_id, status, error_message=error_message
            )

    async def _save_result(
        self, task_id: uuid.UUID, result: dict[str, Any]
    ) -> None:
        """Сохраняет итоговый маршрут и переводит задачу в ``COMPLETED``."""
        async with self._db.session() as session:
            await TaskRepository(session).save_task_result(task_id, result)

    # ------------------------------------------------------------------ #
    # Работа с реестром прогресса
    # ------------------------------------------------------------------ #
    def _set_progress(
        self,
        task_id: uuid.UUID,
        *,
        status: TaskStatus | None = None,
        total: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Обновляет (или создаёт) запись прогресса задачи."""
        state = self._progress.get(task_id)
        if state is None:
            state = ProgressState(
                status=status or TaskStatus.PENDING, total_legs=total or 0
            )
            self._progress[task_id] = state
            return
        if status is not None:
            state.status = status
        if total is not None:
            state.total_legs = total
        if result is not None:
            state.result = result

    def _inc_completed(self, task_id: uuid.UUID) -> None:
        """Увеличивает счётчик собранных плеч (не превышая общего числа)."""
        state = self._progress.get(task_id)
        if state is None:
            return
        state.completed_legs = min(state.completed_legs + 1, state.total_legs)

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    @staticmethod
    def _day_bounds(leg: RouteLeg) -> tuple[datetime, datetime]:
        """Возвращает границы суток (UTC) для даты плеча — фильтр кэша по дате."""
        day_start = datetime.combine(leg.travel_date, time.min, tzinfo=timezone.utc)
        day_end = datetime.combine(leg.travel_date, time.max, tzinfo=timezone.utc)
        return day_start, day_end

    @staticmethod
    def _cache_to_dto(cache: Any) -> TicketDTO:
        """Преобразует ORM-запись кэша :class:`TicketCache` обратно в DTO."""
        return TicketDTO(
            source=cache.source,
            departure_city=cache.departure_city,
            arrival_city=cache.arrival_city,
            departure_time=cache.departure_time.isoformat(),
            arrival_time=cache.arrival_time.isoformat(),
            duration_minutes=cache.duration_minutes,
            price=cache.price,
            has_baggage=cache.has_baggage,
        )

    def _default_scraper_manager_factory(self, transport_type: str = "both") -> Any:
        """Ленивая фабрика ``ScraperManager`` (импорт Playwright только тут)."""
        from src.scrapers import ScraperManager

        return ScraperManager(settings=self._settings, transport_type=transport_type)
