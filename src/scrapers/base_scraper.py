"""Абстрактный базовый класс скрапера.

Реализует общую инфраструктуру: запуск Playwright со stealth-патчами,
подключение резидентного прокси, агрессивную блокировку статических
ресурсов и точку расширения для перехвата фоновых JSON-ответов.
Подклассы описывают только специфику конкретного источника:
построение URL и нормализацию полезной нагрузки в :class:`TicketDTO`.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from datetime import date
from types import TracebackType
from typing import Final

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from src.config import Settings, get_settings
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Расширения, которые мы блокируем, чтобы не платить за бесполезный трафик
# через резидентный прокси. Картинки, шрифты, видео и стили не нужны для
# сбора JSON-ответов с ценами.
_BLOCKED_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {"image", "media", "font"}
)

# Чёрный список доменов трекинга и аналитики — также режется на лету.
_BLOCKED_TRACKING_HOSTS: Final[tuple[str, ...]] = (
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "doubleclick.net",
    "yandex.ru/metrika",
    "mc.yandex.ru",
    "facebook.com/tr",
    "hotjar.com",
    "amplitude.com",
    "sentry.io",
    "criteo.com",
    "adfox.ru",
)

# Реалистичный современный User-Agent. Подменяется stealth-плагином,
# но мы дополнительно фиксируем его на уровне контекста для предсказуемости.
_DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class ScraperError(RuntimeError):
    """Базовое исключение модуля скрапинга.

    Все ошибки конкретных скраперов должны наследоваться от него,
    чтобы вышестоящие слои (оркестратор, бот) могли единообразно их
    обрабатывать.
    """


class BaseScraper(abc.ABC):
    """Абстрактный скрапер билетов.

    Класс реализует паттерн `Template Method`: метод :meth:`fetch` фиксирует
    жизненный цикл (запуск браузера → открытие страницы → перехват ответов →
    нормализация → закрытие). Подклассы переопределяют только специфические
    шаги — построение URL и парсинг JSON-ответов.

    Поддерживается также протокол асинхронного контекстного менеджера для
    случаев, когда удобнее переиспользовать один экземпляр Playwright между
    несколькими вызовами `fetch`.
    """

    #: Человекочитаемое имя источника (используется в логах и DTO).
    source_name: str = ""

    def __init__(self, settings: Settings | None = None) -> None:
        """Инициализирует скрапер.

        Args:
            settings: Сводный объект конфигурации. Если не передан, берётся
                кэшированный синглтон через :func:`get_settings` — там же
                выполняется строгая проверка прокси.
        """
        if not self.source_name:
            raise ScraperError(
                f"{type(self).__name__} обязан определить атрибут `source_name`."
            )
        # Триггерит валидацию прокси при первой инициализации скрапера —
        # безопасный способ упасть быстро, до запуска браузера.
        self._settings: Settings = settings or get_settings()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ------------------------------------------------------------------
    # Точки расширения для подклассов
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def build_search_url(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> str:
        """Возвращает URL поисковой страницы источника.

        Подкласс отвечает за корректное кодирование IATA-кодов, дат и
        других параметров запроса, специфичных для источника.
        """

    @abc.abstractmethod
    def is_results_response(self, url: str) -> bool:
        """Определяет, относится ли URL ответа к интересующему JSON-эндпоинту.

        Используется в обработчике `page.on("response", ...)` для отбора
        нужных XHR/Fetch-запросов и игнорирования всего постороннего.
        """

    @abc.abstractmethod
    def parse_payload(
        self,
        payload: object,
        departure_city: str,
        arrival_city: str,
    ) -> list[TicketDTO]:
        """Преобразует распарсенный JSON-ответ источника в список DTO."""

    @abc.abstractmethod
    async def wait_for_results(self, page: Page) -> None:
        """Ожидает появления результатов на странице.

        Реализация зависит от источника: где-то это `page.wait_for_selector`,
        где-то — детект завершения SSE-стрима. Базовый класс намеренно не
        диктует конкретный механизм.
        """

    # ------------------------------------------------------------------
    # Жизненный цикл Playwright
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BaseScraper":
        await self._start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stop()

    async def _start(self) -> None:
        """Запускает Playwright и создаёт браузерный контекст с прокси."""
        if self._playwright is not None:
            return

        logger.info("Запуск Playwright для скрапера %s", self.source_name)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._settings.headless,
            # Передаём прокси на уровне процесса браузера — это самый
            # надёжный способ гарантировать, что все запросы пойдут через
            # резидентный канал, включая WebSocket и DNS prefetch.
            proxy=self._settings.proxy.as_playwright_proxy(),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=_DEFAULT_USER_AGENT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 800},
        )
        self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)
        self._context.set_default_timeout(self._settings.nav_timeout_ms)

    async def _stop(self) -> None:
        """Аккуратно закрывает все ресурсы Playwright."""
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright для скрапера %s остановлен", self.source_name)

    # ------------------------------------------------------------------
    # Публичный шаблонный метод
    # ------------------------------------------------------------------

    async def fetch(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> list[TicketDTO]:
        """Основная точка входа: возвращает нормализованный список билетов.

        Метод выполняет все стадии единым потоком: при необходимости
        поднимает Playwright, готовит страницу, перехватывает фоновые
        JSON-ответы и нормализует их через :meth:`parse_payload`.
        """
        owns_lifecycle = self._context is None
        if owns_lifecycle:
            await self._start()

        assert self._context is not None  # для статической типизации

        url = self.build_search_url(departure_city, arrival_city, travel_date)
        logger.info("[%s] Открываем %s", self.source_name, url)

        page = await self._context.new_page()
        await self._install_request_blocker(page)

        collected: list[TicketDTO] = []
        # Взводится, как только перехвачена первая полезная партия билетов.
        # Позволяет не дожидаться завершения длинного long-poll и не
        # оплачивать «хвост» лишнего трафика через резидентный прокси.
        first_results: asyncio.Event = asyncio.Event()

        async def _on_response(response):  # type: ignore[no-untyped-def]
            """Перехватчик фоновых JSON-ответов источника.

            Любые ошибки парсинга поглощаются и логируются — один битый
            ответ не должен ронять весь скрапинг.
            """
            try:
                url = response.url
                content_type = (response.headers or {}).get("content-type", "")
                # Логируем все JSON-ответы при DEBUG — помогает обновить URL-хинты.
                if logger.isEnabledFor(logging.DEBUG) and "json" in content_type.lower():
                    logger.debug("[%s] JSON: %s", self.source_name, url[:200])
                if not self.is_results_response(url):
                    return
                if response.status >= 400:
                    return
                if "json" not in content_type.lower():
                    return
                payload = await response.json()
            except Exception as exc:  # noqa: BLE001 — намеренно широкий улов
                logger.debug(
                    "[%s] Не удалось разобрать ответ %s: %s",
                    self.source_name,
                    getattr(response, "url", "<?>"),
                    exc,
                )
                return

            try:
                tickets = self.parse_payload(payload, departure_city, arrival_city)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] Ошибка нормализации полезной нагрузки: %s",
                    self.source_name,
                    exc,
                )
                return

            if tickets:
                logger.info(
                    "[%s] Перехвачено билетов: %d (URL: %s)",
                    self.source_name,
                    len(tickets),
                    response.url,
                )
                collected.extend(tickets)
                first_results.set()

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded")
            try:
                # Ждём первую партию результатов через сетевой перехватчик.
                await asyncio.wait_for(
                    first_results.wait(),
                    timeout=self._settings.nav_timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                # JSON так и не пришёл — пробуем источник-специфичное ожидание
                # как запасной вариант (best-effort).
                await self.wait_for_results(page)
            else:
                # Первая партия получена. Даём короткое окно добрать
                # запоздавшие пакеты и принудительно прекращаем загрузку
                # страницы ради экономии трафика (не ждём networkidle).
                await asyncio.sleep(self._settings.results_grace_ms / 1000)
        except Exception as exc:  # noqa: BLE001
            raise ScraperError(
                f"[{self.source_name}] Ошибка при навигации/ожидании результатов: {exc}"
            ) from exc
        finally:
            await page.close()
            if owns_lifecycle:
                await self._stop()

        return collected

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    async def _install_request_blocker(self, page: Page) -> None:
        """Регистрирует обработчик `page.route`, режущий ненужные ресурсы.

        Снижает потребление трафика через резидентный прокси и ускоряет
        отрисовку страницы (что косвенно помогает обходить эвристики
        антибота, ориентированные на тайминг).
        """

        async def _handler(route: Route) -> None:
            request = route.request
            if request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            url_lower = request.url.lower()
            if any(host in url_lower for host in _BLOCKED_TRACKING_HOSTS):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", _handler)