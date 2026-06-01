"""Координатор скраперов — единая точка запуска сбора билетов.

`ScraperManager` инкапсулирует список конкретных скраперов и параллельно
запускает их для одного маршрута, агрегируя результаты в общий список
:class:`TicketDTO`. Менеджер ничего не знает о внутренностях источников —
он зависит только от абстракции :class:`BaseScraper` (принцип инверсии
зависимостей из SOLID).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import date
from typing import AsyncIterator, Callable, Sequence

from src.config import Settings, get_settings
from src.scrapers.aviasales_scraper import AviasalesScraper
from src.scrapers.base_scraper import BaseScraper, ScraperError
from src.scrapers.dto import TicketDTO
from src.scrapers.rzd_scraper import RzdScraper

logger = logging.getLogger(__name__)


class ScraperManager:
    """Управляет набором скраперов и агрегирует их результаты.

    По умолчанию подключает все доступные источники (`aviasales`, `rzd`),
    но допускает передачу произвольного набора скраперов — это упрощает
    тестирование и расширение новыми источниками без изменения кода
    менеджера (принцип открытости/закрытости).
    """

    def __init__(
        self,
        scrapers: Sequence[BaseScraper] | None = None,
        settings: Settings | None = None,
        transport_type: str = "both",
    ) -> None:
        """Создаёт менеджер.

        Args:
            scrapers: Явный список скраперов. Если не передан — создаются
                реализации по умолчанию.
            settings: Конфигурация приложения. Если не передана — берётся
                кэшированный синглтон (с попутной валидацией прокси).
            transport_type: ``"plane"`` — только Aviasales, ``"train"`` —
                только РЖД, ``"both"`` — оба источника.
        """
        # Триггерим валидацию прокси на старте — лучше упасть здесь, чем
        # после запуска нескольких браузеров.
        self._settings: Settings = settings or get_settings()
        if scrapers is not None:
            self._scrapers: list[BaseScraper] = list(scrapers)
        else:
            candidates: list[BaseScraper] = [
                AviasalesScraper(self._settings),
                RzdScraper(self._settings),
            ]
            if transport_type == "plane":
                self._scrapers = [s for s in candidates if s.source_name == "aviasales"]
            elif transport_type == "train":
                self._scrapers = [s for s in candidates if s.source_name == "rzd"]
            else:
                self._scrapers = candidates
        if not self._scrapers:
            raise ScraperError("ScraperManager инициализирован без скраперов.")

    @property
    def scrapers(self) -> tuple[BaseScraper, ...]:
        """Возвращает неизменяемый кортеж подключённых скраперов."""
        return tuple(self._scrapers)

    async def collect(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> list[TicketDTO]:
        """Параллельно запускает все скраперы и возвращает общий список билетов.

        Сбои отдельных источников не прерывают общий процесс: исключение
        одного скрапера логируется, а результаты остальных возвращаются как
        есть. Это критично для отказоустойчивости — если, например, РЖД
        показал капчу, авиабилеты всё равно должны вернуться.
        """
        logger.info(
            "Запуск сбора билетов %s → %s на %s по %d источникам",
            departure_city,
            arrival_city,
            travel_date.isoformat(),
            len(self._scrapers),
        )

        tasks = [
            self._run_single(scraper, departure_city, arrival_city, travel_date)
            for scraper in self._scrapers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tickets: list[TicketDTO] = []
        for scraper, result in zip(self._scrapers, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Скрапер %s завершился с ошибкой: %s",
                    scraper.source_name,
                    result,
                )
                continue
            tickets.extend(result)

        logger.info("Всего собрано билетов: %d", len(tickets))
        return tickets

    @staticmethod
    async def _run_single(
        scraper: BaseScraper,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> list[TicketDTO]:
        """Запускает один скрапер в собственном контексте Playwright."""
        async with scraper:
            return await scraper.fetch(departure_city, arrival_city, travel_date)

    async def collect_many(
        self,
        routes: Sequence[tuple[str, str, date]],
    ) -> list[TicketDTO]:
        """Прогоняет множество маршрутов, переиспользуя браузерный контекст.

        Главный рычаг экономии трафика при тонком бюджете прокси: контекст
        каждого скрапера открывается ОДИН раз на весь пакет. Тяжёлые
        JS-бандлы и HTML скачиваются единожды и на всех последующих
        маршрутах берутся из HTTP-кэша браузера — по сети идёт практически
        только полезный JSON.

        Внутри одного источника маршруты обходятся последовательно (чтобы
        кэш оставался «тёплым»), а разные источники работают параллельно.
        Сбой отдельного маршрута логируется и не прерывает остальные.
        """
        if not routes:
            return []

        logger.info(
            "Пакетный сбор: %d маршрут(ов) по %d источникам",
            len(routes),
            len(self._scrapers),
        )

        async def _run_scraper(scraper: BaseScraper) -> list[TicketDTO]:
            collected: list[TicketDTO] = []
            # Контекст открывается единожды и переиспользуется на все маршруты.
            async with scraper:
                for departure_city, arrival_city, travel_date in routes:
                    try:
                        collected.extend(
                            await scraper.fetch(
                                departure_city, arrival_city, travel_date
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "[%s] Маршрут %s → %s (%s) пропущен: %s",
                            scraper.source_name,
                            departure_city,
                            arrival_city,
                            travel_date.isoformat(),
                            exc,
                        )
            return collected

        results = await asyncio.gather(
            *(_run_scraper(scraper) for scraper in self._scrapers),
            return_exceptions=True,
        )

        tickets: list[TicketDTO] = []
        for scraper, result in zip(self._scrapers, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Скрапер %s упал на пакете: %s", scraper.source_name, result
                )
                continue
            tickets.extend(result)

        logger.info("Пакет завершён, всего билетов: %d", len(tickets))
        return tickets

    async def collect_iter(
        self,
        routes: Sequence[tuple[str, str, date]],
        *,
        health_sink: "Callable[..., None] | None" = None,
    ) -> AsyncIterator[tuple[tuple[str, str, date], list[TicketDTO]]]:
        """Потоковая версия пакетного сбора: отдаёт результат по каждому плечу.

        Контексты всех скраперов открываются ОДИН раз на весь пакет (через
        :class:`~contextlib.AsyncExitStack`) — тот же рычаг экономии трафика,
        что и в :meth:`collect_many`: тяжёлые JS-бандлы качаются единожды, далее
        берутся из HTTP-кэша браузера. Маршруты обходятся последовательно (кэш
        остаётся «тёплым»), а источники по каждому маршруту работают параллельно.

        В отличие от :meth:`collect_many`, метод не копит всё в один список, а
        ``yield``-ит пару ``(маршрут, билеты)`` сразу по завершении каждого
        плеча — это даёт вышестоящему слою точный прогресс в реальном времени.
        Сбой отдельного источника на плече логируется и не прерывает остальные.
        """
        if not routes:
            return

        async with AsyncExitStack() as stack:
            for scraper in self._scrapers:
                await stack.enter_async_context(scraper)

            for departure_city, arrival_city, travel_date in routes:
                results = await asyncio.gather(
                    *(
                        scraper.fetch(departure_city, arrival_city, travel_date)
                        for scraper in self._scrapers
                    ),
                    return_exceptions=True,
                )
                tickets: list[TicketDTO] = []
                for scraper, result in zip(self._scrapers, results):
                    if isinstance(result, BaseException):
                        logger.error(
                            "[%s] Маршрут %s → %s (%s) пропущен: %s",
                            scraper.source_name,
                            departure_city,
                            arrival_city,
                            travel_date.isoformat(),
                            result,
                        )
                        # Трекер здоровья: фиксируем ошибку источника (Модуль 7C).
                        if health_sink is not None:
                            health_sink(scraper.source_name, count=0, error=str(result))
                        continue
                    tickets.extend(result)
                    if health_sink is not None:
                        health_sink(scraper.source_name, count=len(result), error=None)
                yield (departure_city, arrival_city, travel_date), tickets
