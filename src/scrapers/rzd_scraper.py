"""Скрапер железнодорожных билетов через агрегатор `tutu.ru`.

Официальный сайт РЖД (`ticket.rzd.ru`) закрыт агрессивной антибот-защитой
(`sifting`/`kfp`/`dmcsfw`), которую невозможно стабильно пройти автоматически в
рамках бюджета прокси. Поэтому источником данных служит официальный агрегатор
ж/д билетов **tutu.ru**, который оперирует теми же express-кодами станций РЖД.

Ключевое отличие от Aviasales: tutu — это Next.js-приложение, и страница
расписания direction `/poezda/<Откуда>/<Куда>/` **server-side-рендерит** весь
список поездов прямо в `__NEXT_DATA__` (узел `pageData.routes`). Никакого
фонового XHR/WebSocket ждать не нужно — достаточно одного GET и разбора JSON из
HTML. Это и дёшево по трафику (~1 МБ на маршрут, без JS-бандлов), и устойчиво.

Поток сбора:

1. ``train-gateway.tutu.ru/api/seo/schedule/link`` по паре express-кодов РЖД
   возвращает slug страницы расписания (``{"url": "/poezda/Sankt-Peterburg/Moskva/"}``).
2. GET этой страницы → из ``<script id="__NEXT_DATA__">`` достаётся ``pageData.routes``.
3. Каждый ``route`` (время отправления/прибытия в UTC, длительность, цены по
   типам вагонов) нормализуется в :class:`TicketDTO`.

Важно про даты: страница `/poezda/.../` — это **недатированное** расписание
(CDN-бандл так и называется, ``schedule-undated``): каждый поезд показан с
ближайшим отправлением. Для оптимизатора нужны билеты на конкретную дату плеча,
поэтому время каждого регулярного поезда **проецируется** на запрошенную дату с
сохранением времени суток и длительности. Поезда с заметно более поздним
ближайшим отправлением (сезонные/нерегулярные) при проекции отбрасываются —
см. :data:`_SCHEDULE_WINDOW_DAYS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from patchright.async_api import Page

from src.scrapers.base_scraper import BaseScraper, ScraperError
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Офлайн-фолбэк "город → код станции РЖД" на случай недоступности suggest-API
# tutu. Основной путь резолва — динамический (см. `_resolve_code`): он покрывает
# любой город России, поэтому таблицу руками расширять не нужно. tutu использует
# те же express-коды в качестве `departure_rzd_code`/`arrival_rzd_code`.
_CITY_TO_RZD_CODE: Final[dict[str, str]] = {
    "москва": "2000000",
    "санкт-петербург": "2004000",
    "санкт петербург": "2004000",
    "питер": "2004000",
    "казань": "2060600",
    "екатеринбург": "2030000",
    "новосибирск": "2044000",
    "нижний новгород": "2060001",
    "калининград": "2030500",
    "минск": "2100000",
}

# Suggest-API tutu: по названию города возвращает список станций с express-кодами
# РЖД (`{"suggest":[{"value":"2040000","label":"Челябинск"}],"name":"..."}`).
# Параметр фильтра — именно `name` (а не `query`).
_SUGGEST_TEMPLATE: Final[str] = "https://www.tutu.ru/suggest/railway/?name={name}"

# Часовой пояс, в котором tutu отдаёт «настенное» время (расписание РЖД ведётся
# по Москве). Время в `__NEXT_DATA__` приходит в UTC — переводим в МСК и отдаём
# наивное локальное время, как это делает скрапер Aviasales.
_MOSCOW_TZ: Final[ZoneInfo] = ZoneInfo("Europe/Moscow")

# Насколько ближайшее отправление поезда может отстоять от «ближайшего дня»
# расписания, чтобы поезд считался регулярным и проецировался на дату плеча.
# Ежедневные поезда кластеризуются на ближайших сутках; сезонные показывают
# ближайшее отправление через недели — их при проекции отбрасываем.
_SCHEDULE_WINDOW_DAYS: Final[int] = 3

# Извлечение полезной нагрузки Next.js из HTML страницы расписания.
_NEXT_DATA_RE: Final[re.Pattern[str]] = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _norm_city(city: str) -> str:
    """Нормализует название города для сравнения/поиска по таблице."""
    return city.strip().lower().replace("ё", "е")


def _city_to_rzd_code(city: str) -> str:
    """Офлайн-резолв названия города в express-код по таблице-фолбэку."""
    code = _CITY_TO_RZD_CODE.get(_norm_city(city))
    if not code:
        raise ScraperError(
            f"Город {city!r} не найден в офлайн-таблице кодов станций РЖД."
        )
    return code


class RzdScraper(BaseScraper):
    """Скрапер железнодорожных билетов через агрегатор tutu.ru."""

    source_name = "rzd"

    _TUTU_BASE: Final[str] = "https://www.tutu.ru"
    # Резолвер slug-страницы расписания по паре express-кодов РЖД.
    _SEO_LINK_TEMPLATE: Final[str] = (
        "https://train-gateway.tutu.ru/api/seo/schedule/link"
        "?departure_rzd_code={origin}&arrival_rzd_code={destination}"
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Кэш «город → express-код» на время жизни экземпляра: при пакетном
        # сборе (collect_iter) один и тот же город не резолвится повторно.
        self._code_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Реализация абстрактных точек расширения BaseScraper
    # ------------------------------------------------------------------

    def build_search_url(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> str:
        """Собирает URL SEO-резолвера по офлайн-таблице кодов (для интерфейса).

        Основной путь сбора (:meth:`fetch`) резолвит города динамически через
        suggest-API и этот метод не использует; он сохранён для совместимости с
        абстрактным контрактом :class:`BaseScraper` и как синхронный фолбэк.
        Дата не участвует: страница расписания недатированная.
        """
        origin = _city_to_rzd_code(departure_city)
        destination = _city_to_rzd_code(arrival_city)
        return self._SEO_LINK_TEMPLATE.format(origin=origin, destination=destination)

    def is_results_response(self, url: str) -> bool:
        """Опознаёт документ страницы расписания tutu (`/poezda/...`)."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        return parsed.netloc.lower().endswith("tutu.ru") and parsed.path.startswith(
            "/poezda/"
        )

    async def wait_for_results(self, page: Page) -> None:  # noqa: D401
        """Не используется: данные приходят SSR-ом, ждать нечего."""
        return None

    def parse_payload(
        self,
        payload: object,
        departure_city: str,
        arrival_city: str,
    ) -> list[TicketDTO]:
        """Разбирает `__NEXT_DATA__`/`pageData` в список билетов (без проекции).

        Без даты возвращает поезда с их «родными» датами ближайшего отправления.
        Проекция на конкретную дату выполняется в :meth:`fetch`.
        """
        return self._routes_to_tickets(
            payload, departure_city, arrival_city, target_date=None, booking_url=None
        )

    # ------------------------------------------------------------------
    # Переопределённый шаблонный метод: tutu отдаёт данные через SSR,
    # поэтому вместо перехвата XHR делаем два лёгких HTTP-GET.
    # ------------------------------------------------------------------

    async def fetch(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> list[TicketDTO]:
        """Собирает билеты на дату плеча со страницы расписания tutu."""
        owns_lifecycle = self._context is None
        if owns_lifecycle:
            await self._start()

        assert self._context is not None  # для статической типизации
        request = self._context.request
        timeout_ms = float(self._settings.nav_timeout_ms)

        try:
            slug = await self._resolve_slug(
                request, departure_city, arrival_city, timeout_ms
            )
            page_url = f"{self._TUTU_BASE}{slug}"
            logger.info("[%s] Загружаем расписание %s", self.source_name, page_url)
            html = await self._fetch_text(request, page_url, timeout_ms)
            payload = self._extract_next_data(html)
            tickets = self._routes_to_tickets(
                payload,
                departure_city,
                arrival_city,
                target_date=travel_date,
                booking_url=page_url,
            )
            logger.info(
                "[%s] Поездов после нормализации: %d (%s → %s на %s)",
                self.source_name,
                len(tickets),
                departure_city,
                arrival_city,
                travel_date.isoformat(),
            )
            return tickets
        except ScraperError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ScraperError(
                f"[{self.source_name}] Ошибка при сборе расписания tutu: {exc}"
            ) from exc
        finally:
            if owns_lifecycle:
                await self._stop()

    # ------------------------------------------------------------------
    # Сетевые шаги
    # ------------------------------------------------------------------

    async def _get(
        self, request: Any, url: str, timeout_ms: float, *, attempts: int = 2
    ) -> Any:
        """GET с повтором на транзиентных сбоях (5xx и сетевые исключения).

        tutu периодически отвечает 502/504 от шлюза; один повтор делает сбор
        устойчивым. Повторов мало (бюджет прокси), бэкофф короткий.
        """
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await request.get(url, timeout=timeout_ms)
                if response.status < 500:
                    return response
                logger.warning(
                    "[%s] %s ответил %s (попытка %d/%d)",
                    self.source_name,
                    url,
                    response.status,
                    attempt,
                    attempts,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "[%s] ошибка запроса %s (попытка %d/%d): %s",
                    self.source_name,
                    url,
                    attempt,
                    attempts,
                    exc,
                )
            if attempt < attempts:
                await asyncio.sleep(0.8 * attempt)
        if last_exc is not None:
            raise last_exc
        # Все попытки вернули 5xx — отдаём последний ответ, разбор статуса выше.
        return response

    async def _resolve_code(
        self, request: Any, city: str, timeout_ms: float
    ) -> str:
        """Резолвит город в express-код станции РЖД через suggest-API tutu.

        Порядок: кэш экземпляра → suggest-API → офлайн-таблица. Из ответа
        suggest берётся станция с точным совпадением названия, иначе — первая
        (как правило, узловой код «все вокзалы города»).
        """
        key = _norm_city(city)
        cached = self._code_cache.get(key)
        if cached:
            return cached

        url = _SUGGEST_TEMPLATE.format(name=quote(city.strip()))
        code: str | None = None
        try:
            response = await self._get(request, url, timeout_ms)
            if response.status == 200:
                data = await response.json()
                suggestions = data.get("suggest") if isinstance(data, dict) else None
                if isinstance(suggestions, list) and suggestions:
                    exact = next(
                        (
                            s
                            for s in suggestions
                            if isinstance(s, dict)
                            and _norm_city(str(s.get("label", ""))) == key
                        ),
                        None,
                    )
                    chosen = exact or suggestions[0]
                    value = chosen.get("value") if isinstance(chosen, dict) else None
                    if isinstance(value, str) and value.isdigit():
                        code = value
        except Exception as exc:  # noqa: BLE001 — падаем в офлайн-фолбэк
            logger.warning(
                "[%s] suggest-API недоступен для %r: %s", self.source_name, city, exc
            )

        if code is None:
            # Офлайн-фолбэк: вдруг город есть в таблице (бросит ScraperError, если нет).
            code = _city_to_rzd_code(city)

        self._code_cache[key] = code
        return code

    async def _resolve_slug(
        self,
        request: Any,
        departure_city: str,
        arrival_city: str,
        timeout_ms: float,
    ) -> str:
        """Резолвит slug страницы расписания по паре городов.

        Города переводятся в express-коды РЖД динамически (suggest-API), затем
        SEO-резолвер tutu отдаёт человекочитаемый slug направления.
        """
        origin = await self._resolve_code(request, departure_city, timeout_ms)
        destination = await self._resolve_code(request, arrival_city, timeout_ms)
        url = self._SEO_LINK_TEMPLATE.format(origin=origin, destination=destination)
        response = await self._get(request, url, timeout_ms)
        if response.status != 200:
            raise ScraperError(
                f"[{self.source_name}] SEO-резолвер ответил {response.status} для {url}"
            )
        try:
            data = await response.json()
        except Exception as exc:  # noqa: BLE001
            raise ScraperError(
                f"[{self.source_name}] Не удалось разобрать ответ SEO-резолвера: {exc}"
            ) from exc
        slug = data.get("url") if isinstance(data, dict) else None
        if not (isinstance(slug, str) and slug.startswith("/")):
            raise ScraperError(
                f"[{self.source_name}] SEO-резолвер не вернул slug: {data!r}"
            )
        return slug

    async def _fetch_text(self, request: Any, url: str, timeout_ms: float) -> str:
        """Скачивает HTML страницы расписания (один GET, без под-ресурсов)."""
        response = await self._get(request, url, timeout_ms)
        if response.status != 200:
            raise ScraperError(
                f"[{self.source_name}] Страница расписания ответила {response.status}: {url}"
            )
        return await response.text()

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any]:
        """Достаёт и парсит JSON из `<script id="__NEXT_DATA__">`."""
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise ScraperError("На странице расписания tutu нет блока __NEXT_DATA__.")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ScraperError(f"Некорректный JSON в __NEXT_DATA__: {exc}") from exc
        if not isinstance(payload, dict):
            raise ScraperError("__NEXT_DATA__ не является объектом JSON.")
        return payload

    # ------------------------------------------------------------------
    # Нормализация
    # ------------------------------------------------------------------

    def _routes_to_tickets(
        self,
        payload: object,
        departure_city: str,
        arrival_city: str,
        target_date: date | None,
        booking_url: str | None,
    ) -> list[TicketDTO]:
        """Преобразует список `routes` в билеты, проецируя на дату при наличии."""
        routes = self._extract_routes(payload)

        # Заранее разбираем время отправления/прибытия (UTC) — нужно и для
        # фильтра регулярности, и для DTO.
        prepared: list[tuple[dict[str, Any], datetime, datetime]] = []
        for route in routes:
            dep_dt = self._parse_iso_utc(
                (route.get("departure") or {}).get("departureDateTime")
            )
            arr_dt = self._parse_iso_utc(
                (route.get("arrival") or {}).get("arrivalDateTime")
            )
            if dep_dt is None or arr_dt is None:
                continue
            prepared.append((route, dep_dt, arr_dt))

        if not prepared:
            return []

        # «Ближайший день» расписания — минимальная дата отправления (по МСК).
        baseline = min(
            dep_dt.astimezone(_MOSCOW_TZ).date() for _, dep_dt, _ in prepared
        )

        tickets: list[TicketDTO] = []
        for route, dep_dt, arr_dt in prepared:
            if target_date is not None:
                native_date = dep_dt.astimezone(_MOSCOW_TZ).date()
                if (native_date - baseline).days > _SCHEDULE_WINDOW_DAYS:
                    # Сезонный/нерегулярный поезд — не проецируем на дату плеча.
                    continue
            try:
                ticket = self._route_to_dto(
                    route,
                    dep_dt,
                    arr_dt,
                    departure_city,
                    arrival_city,
                    target_date,
                    booking_url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] Пропуск поезда: %s", self.source_name, exc)
                continue
            if ticket is not None:
                tickets.append(ticket)
        return tickets

    @staticmethod
    def _extract_routes(payload: object) -> list[dict[str, Any]]:
        """Извлекает список `routes` из `__NEXT_DATA__`, `pageData` или списка."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        # Полный __NEXT_DATA__: props → pageProps → pageData → routes.
        node: Any = payload
        for key in ("props", "pageProps", "pageData"):
            if isinstance(node, dict) and key in node:
                node = node[key]
        if isinstance(node, dict) and isinstance(node.get("routes"), list):
            return [item for item in node["routes"] if isinstance(item, dict)]
        # Уже переданный pageData (или иной объект с routes на верхнем уровне).
        if isinstance(payload.get("routes"), list):
            return [item for item in payload["routes"] if isinstance(item, dict)]
        return []

    def _route_to_dto(
        self,
        route: dict[str, Any],
        dep_dt: datetime,
        arr_dt: datetime,
        departure_city: str,
        arrival_city: str,
        target_date: date | None,
        booking_url: str | None,
    ) -> TicketDTO | None:
        """Конвертирует один `route` в :class:`TicketDTO`."""
        price = self._route_min_price(route)
        if price is None:
            return None

        duration_minutes = self._route_duration_minutes(route, dep_dt, arr_dt)

        # UTC → МСК; затем — проекция на дату плеча с сохранением времени суток.
        dep_local = dep_dt.astimezone(_MOSCOW_TZ)
        arr_local = arr_dt.astimezone(_MOSCOW_TZ)
        if target_date is not None:
            shift = (target_date - dep_local.date()).days
            if shift:
                dep_local += timedelta(days=shift)
                arr_local += timedelta(days=shift)

        return TicketDTO(
            source=self.source_name,
            departure_city=departure_city,
            arrival_city=arrival_city,
            # Наивное локальное (МСК) время — как у скрапера Aviasales.
            departure_time=dep_local.replace(tzinfo=None).isoformat(),
            arrival_time=arr_local.replace(tzinfo=None).isoformat(),
            duration_minutes=duration_minutes,
            price=price,
            has_baggage=True,
            booking_url=booking_url,
        )

    @staticmethod
    def _parse_iso_utc(value: object) -> datetime | None:
        """Парсит ISO-8601 (с суффиксом `Z`) в aware-datetime UTC."""
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _route_min_price(route: dict[str, Any]) -> int | None:
        """Минимальная цена поездки в рублях (сумма минимумов по сегментам).

        Для прямого поезда — минимум по типам вагонов; для составного маршрута —
        сумма минимумов каждого сегмента. Цена приходит в минорных единицах с
        полем ``precision`` (обычно 2 → копейки).
        """
        segments = route.get("segments")
        if not isinstance(segments, list) or not segments:
            return None

        total = 0.0
        for segment in segments:
            if not isinstance(segment, dict):
                return None
            car_types = segment.get("carTypes")
            if not isinstance(car_types, list):
                return None
            segment_prices: list[float] = []
            for car_type in car_types:
                if not isinstance(car_type, dict):
                    continue
                min_price = car_type.get("minPrice")
                if not isinstance(min_price, dict):
                    continue
                amount = min_price.get("amount")
                precision = min_price.get("precision", 2)
                if isinstance(amount, (int, float)) and amount > 0:
                    segment_prices.append(amount / (10 ** int(precision)))
            if not segment_prices:
                return None
            total += min(segment_prices)

        return int(round(total))

    @staticmethod
    def _route_duration_minutes(
        route: dict[str, Any],
        dep_dt: datetime,
        arr_dt: datetime,
    ) -> int:
        """Длительность поездки в минутах (из `travelTime`, иначе по разнице)."""
        travel_time = route.get("travelTime")
        if isinstance(travel_time, dict):
            seconds = travel_time.get("seconds")
            if isinstance(seconds, (int, float)) and seconds > 0:
                return int(seconds // 60)

        # Сумма длительностей сегментов как запасной вариант.
        segments = route.get("segments")
        if isinstance(segments, list):
            total = 0
            found = False
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                seg_tt = segment.get("travelTime")
                if isinstance(seg_tt, dict) and isinstance(
                    seg_tt.get("seconds"), (int, float)
                ):
                    total += int(seg_tt["seconds"] // 60)
                    found = True
            if found:
                return total

        return max(int((arr_dt - dep_dt).total_seconds() // 60), 0)
