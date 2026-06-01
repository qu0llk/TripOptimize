"""Скрапер `ticket.rzd.ru` через перехват фоновых JSON-ответов.

Сайт РЖД — это SPA, которое подгружает расписание и цены асинхронным
POST/GET-запросом к внутреннему API (`apib2b`/`api`). Как и в случае с
Aviasales, мы не парсим DOM, а слушаем `page.on("response")` и забираем
готовый JSON с поездами.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Final, Iterable
from urllib.parse import urlparse

from patchright.async_api import Page

from src.scrapers.base_scraper import BaseScraper, ScraperError
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Соответствие "город → код станции РЖД" (express-код узла).
# Полноценный справочник станций будет вынесен в отдельный модуль; здесь —
# минимальный набор узловых городов, которыми оперирует пользовательский ввод.
_CITY_TO_RZD_CODE: Final[dict[str, str]] = {
    "москва": "2000000",
    "санкт-петербург": "2004000",
    "санкт петербург": "2004000",
    "питер": "2004000",
    "сочи": "2064001",
    "казань": "2060600",
    "екатеринбург": "2030000",
    "новосибирск": "2044000",
    "нижний новгород": "2060001",
    "калининград": "2030500",
    "минск": "2100000",
}

# Подстроки в URL, по которым опознаются JSON-ответы со списком поездов.
# РЖД периодически меняет имена эндпоинтов, поэтому держим набор вариантов.
_RESULTS_URL_HINTS: Final[tuple[str, ...]] = (
    "/apib2b/p/railway/v1/search/trainpricing",
    "/api/v1/railway/search",
    "/timetable/public",
    "/searchresults",
    "train_pricing",
    "trainpricing",
)


def _city_to_rzd_code(city: str) -> str:
    """Переводит русское название города в express-код станции РЖД."""
    key = city.strip().lower().replace("ё", "е")
    code = _CITY_TO_RZD_CODE.get(key)
    if not code:
        raise ScraperError(
            f"Город {city!r} не найден в таблице кодов станций РЖД. "
            "Дополните `_CITY_TO_RZD_CODE` или передайте код вручную."
        )
    return code


class RzdScraper(BaseScraper):
    """Скрапер железнодорожных билетов с `ticket.rzd.ru`."""

    source_name = "rzd"

    # Базовый URL поисковой страницы. Формат пути:
    # /searching-train/{from}/{to}?... — параметры дублируем в query-строке,
    # чтобы SPA гарантированно инициировал фоновый запрос к API.
    _SEARCH_URL_TEMPLATE: Final[str] = (
        "https://ticket.rzd.ru/searching-train/{origin}/{destination}"
        "?direction=forward&dir=0&tfl=3&checkSeats=1"
        "&code0={origin}&code1={destination}&dt0={date}"
    )

    def build_search_url(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> str:
        """Собирает URL поиска поездов на заданную дату.

        Дата в РЖД передаётся в формате ДД.ММ.ГГГГ.
        """
        origin = _city_to_rzd_code(departure_city)
        destination = _city_to_rzd_code(arrival_city)
        ddmmyyyy = travel_date.strftime("%d.%m.%Y")
        return self._SEARCH_URL_TEMPLATE.format(
            origin=origin,
            destination=destination,
            date=ddmmyyyy,
        )

    def is_results_response(self, url: str) -> bool:
        """Проверяет, относится ли URL к интересующему JSON-эндпоинту РЖД."""
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return False
        if "rzd.ru" not in host:
            return False
        url_lower = url.lower()
        return any(hint in url_lower for hint in _RESULTS_URL_HINTS)

    async def wait_for_results(self, page: Page) -> None:
        """Ждёт отрисовки списка поездов либо затихания сети.

        РЖД нередко показывает капчу/прелоадер, поэтому работаем по принципу
        best-effort: сначала пытаемся дождаться карточки поезда, затем —
        состояния `networkidle`, чтобы перехватчик успел добрать JSON.
        """
        try:
            await page.wait_for_selector(
                "[class*='train'], [data-test-id*='train'], .route-block",
                timeout=self._settings.nav_timeout_ms,
            )
        except Exception:  # noqa: BLE001 — допустимо: дальше есть фоллбэк
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[%s] networkidle не достигнут, продолжаем по таймауту",
                    self.source_name,
                )

    # ------------------------------------------------------------------
    # Нормализация ответа
    # ------------------------------------------------------------------

    def parse_payload(
        self,
        payload: object,
        departure_city: str,
        arrival_city: str,
    ) -> list[TicketDTO]:
        """Преобразует ответ РЖД в список :class:`TicketDTO`.

        Структура API РЖД меняется между версиями, поэтому парсер работает
        оборонительно и пропускает любые поезда без обязательных полей.
        """
        trains = self._extract_trains(payload)
        if not trains:
            return []

        tickets: list[TicketDTO] = []
        for train in trains:
            try:
                ticket = self._train_to_dto(train, departure_city, arrival_city)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] Пропуск поезда: %s", self.source_name, exc)
                continue
            if ticket is not None:
                tickets.append(ticket)
        return tickets

    @staticmethod
    def _extract_trains(payload: object) -> list[dict[str, Any]]:
        """Извлекает список поездов из любой известной структуры ответа."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        # Прямые ключи верхнего уровня, где встречается список поездов.
        for key in ("trains", "Trains", "TrainList", "list", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # Вложенный контейнер вида {"result": {"trains": [...]}}.
        for container_key in ("result", "Result", "tp"):
            container = payload.get(container_key)
            if isinstance(container, dict):
                nested = container.get("trains") or container.get("Trains")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            if isinstance(container, list):
                # Формат timetable: список объектов с полем `list` внутри.
                flattened: list[dict[str, Any]] = []
                for entry in container:
                    if isinstance(entry, dict) and isinstance(entry.get("list"), list):
                        flattened.extend(
                            item for item in entry["list"] if isinstance(item, dict)
                        )
                if flattened:
                    return flattened
        return []

    def _train_to_dto(
        self,
        train: dict[str, Any],
        departure_city: str,
        arrival_city: str,
    ) -> TicketDTO | None:
        """Конвертирует описание одного поезда в :class:`TicketDTO`."""
        departure_dt = self._parse_datetime(
            train,
            ("departureDateTime", "DepartureDateTime", "date0", "departure"),
            ("localDepartureTime", "time0"),
        )
        arrival_dt = self._parse_datetime(
            train,
            ("arrivalDateTime", "ArrivalDateTime", "date1", "arrival"),
            ("localArrivalTime", "time1"),
        )
        if departure_dt is None or arrival_dt is None:
            return None

        duration_minutes = self._extract_duration(train, departure_dt, arrival_dt)
        price_rub = self._extract_min_price(train)
        if price_rub is None:
            return None

        has_baggage = self._extract_baggage_flag(train)

        return TicketDTO(
            source=self.source_name,
            departure_city=departure_city,
            arrival_city=arrival_city,
            departure_time=departure_dt.isoformat(),
            arrival_time=arrival_dt.isoformat(),
            duration_minutes=duration_minutes,
            price=price_rub,
            has_baggage=has_baggage,
        )

    @staticmethod
    def _parse_datetime(
        train: dict[str, Any],
        datetime_keys: Iterable[str],
        time_keys: Iterable[str],
    ) -> datetime | None:
        """Извлекает дату-время из поезда по набору возможных имён полей.

        Поддерживаются два формата: единое ISO-поле (`departureDateTime`) и
        раздельные `date`+`time` строки (legacy timetable).
        """
        # Современный формат: одно ISO-поле.
        for key in datetime_keys:
            value = train.get(key)
            if isinstance(value, str) and value:
                parsed = RzdScraper._safe_fromiso(value)
                if parsed is not None:
                    return parsed

        # Legacy-формат: дата (ДД.ММ.ГГГГ) + время (ЧЧ:ММ) в раздельных полях.
        raw_date = train.get("date0") or train.get("date1") or train.get("date")
        for key in time_keys:
            raw_time = train.get(key)
            if isinstance(raw_date, str) and isinstance(raw_time, str):
                for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
                    try:
                        return datetime.strptime(f"{raw_date} {raw_time}", fmt)
                    except ValueError:
                        continue
        return None

    @staticmethod
    def _safe_fromiso(value: str) -> datetime | None:
        """Безопасно парсит ISO-8601, нормализуя суффикс `Z`."""
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _extract_duration(
        train: dict[str, Any],
        departure_dt: datetime,
        arrival_dt: datetime,
    ) -> int:
        """Возвращает длительность поездки в минутах.

        Сначала пытаемся использовать готовое поле длительности; если оно
        отсутствует — считаем разницу между прибытием и отправлением.
        """
        for key in ("tripDuration", "duration", "tripTime", "timeInWay"):
            value = train.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
            # Иногда длительность приходит строкой "ЧЧ:ММ".
            if isinstance(value, str) and ":" in value:
                try:
                    hours, minutes = (int(part) for part in value.split(":", 1))
                    return hours * 60 + minutes
                except ValueError:
                    continue
        delta: timedelta = arrival_dt - departure_dt
        return max(int(delta.total_seconds() // 60), 0)

    @staticmethod
    def _extract_min_price(train: dict[str, Any]) -> int | None:
        """Возвращает минимальную цену по всем категориям вагонов, в рублях."""
        prices: list[float] = []

        # Поле минимальной цены верхнего уровня.
        for key in ("minPrice", "MinPrice", "minprice"):
            value = train.get(key)
            if isinstance(value, (int, float)) and value > 0:
                prices.append(float(value))

        # Перебор групп вагонов с собственными ценами.
        for groups_key in ("carGroups", "CarGroups", "cars"):
            groups = train.get(groups_key)
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                price = (
                    group.get("minPrice")
                    or group.get("price")
                    or group.get("Price")
                )
                if isinstance(price, (int, float)) and price > 0:
                    prices.append(float(price))

        if not prices:
            return None
        return int(round(min(prices)))

    @staticmethod
    def _extract_baggage_flag(train: dict[str, Any]) -> bool:
        """Определяет возможность провоза багажа.

        В большинстве тарифов РЖД провоз ручной клади и багажа разрешён,
        поэтому при отсутствии явного флага считаем багаж доступным. Если же
        источник прямо указывает запрет — уважаем его.
        """
        for key in ("hasBaggage", "baggageAvailable", "baggage"):
            value = train.get(key)
            if isinstance(value, bool):
                return value
        # По умолчанию для поездов багаж доступен.
        return True
