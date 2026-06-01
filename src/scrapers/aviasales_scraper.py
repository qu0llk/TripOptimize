"""Скрапер `aviasales.ru` через перехват фоновых JSON-ответов.

Aviasales рендерит страницу как SPA и подгружает результаты порциями через
long-poll на внутренний эндпоинт. Мы не парсим DOM (он эфемерный и
обфусцированный), а слушаем `page.on("response")` и забираем готовый JSON.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Final, Iterable
from urllib.parse import urlparse

from patchright.async_api import Page

from src.config.cities import get_city_catalog
from src.scrapers.base_scraper import BaseScraper, ScraperError
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Минимальное соответствие "город → IATA-код города (метакод)" для городов,
# которыми чаще всего оперирует пользовательский ввод. Полноценная
# геолокация будет реализована отдельным модулем; здесь — только то, что
# нужно скраперу прямо сейчас.
_CITY_TO_IATA: Final[dict[str, str]] = {
    "москва": "MOW",
    "санкт-петербург": "LED",
    "санкт петербург": "LED",
    "питер": "LED",
    "сочи": "AER",
    "казань": "KZN",
    "екатеринбург": "SVX",
    "новосибирск": "OVB",
    "нижний новгород": "GOJ",
    "калининград": "KGD",
    "минск": "MSQ",
    "стамбул": "IST",
    "ереван": "EVN",
    "тбилиси": "TBS",
    "дубай": "DXB",
}

# Подстроки в URL, по которым опознаются JSON-ответы со списком билетов.
_RESULTS_URL_HINTS: Final[tuple[str, ...]] = (
    "/search/v3.2/results",
    "/search/v3/results",
    "/v3/search",
    "/search/v3",
    "lyssa.aviasales",
    "/results",
)


def _city_to_iata(city: str) -> str:
    """Переводит русское название города в IATA-код метаполиса.

    Сначала проверяется локальная таблица алиасов (быстрые синонимы вроде
    «питер»), затем — полный всемирный справочник городов (`cities.json`).
    """
    key = city.strip().lower().replace("ё", "е")
    code = _CITY_TO_IATA.get(key) or get_city_catalog().resolve_iata(city)
    if not code:
        raise ScraperError(
            f"Город {city!r} не найден в справочнике IATA-кодов. "
            "Проверьте название или передайте код вручную."
        )
    return code


class AviasalesScraper(BaseScraper):
    """Скрапер авиабилетов с `aviasales.ru`."""

    source_name = "aviasales"

    # Базовый URL поисковой страницы.
    _SEARCH_URL_TEMPLATE: Final[str] = "https://www.aviasales.ru/search/{query}"

    def build_search_url(
        self,
        departure_city: str,
        arrival_city: str,
        travel_date: date,
    ) -> str:
        """Собирает URL вида `MOW0106LED1` (1 взрослый, без обратного билета)."""
        origin = _city_to_iata(departure_city)
        destination = _city_to_iata(arrival_city)
        # Формат даты у aviasales: ДДММ.
        ddmm = travel_date.strftime("%d%m")
        query = f"{origin}{ddmm}{destination}1"
        return self._SEARCH_URL_TEMPLATE.format(query=query)

    def is_results_response(self, url: str) -> bool:
        """Проверяет, относится ли URL к интересующему JSON-эндпоинту."""
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return False
        if "aviasales" not in host and "travelpayouts" not in host:
            return False
        return any(hint in url for hint in _RESULTS_URL_HINTS)

    async def wait_for_results(self, page: Page) -> None:
        """Ждёт появления первых результатов либо завершения сетевой активности.

        Aviasales использует long-poll: первая партия предложений приходит
        в течение нескольких секунд, дальше DOM просто дозаполняется.
        """
        try:
            # Самый надёжный сигнал — первая отрисованная карточка билета.
            await page.wait_for_selector(
                "[data-test-id='ticket'], [class*='ticket']",
                timeout=self._settings.nav_timeout_ms,
            )
        except Exception:  # noqa: BLE001 — работаем по принципу best-effort
            # Фоллбэк: ждём затихания сети, чтобы дать перехватчику добрать
            # запоздавшие пакеты предложений.
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[%s] networkidle не достигнут, продолжаем по таймауту",
                    self.source_name,
                )

    # ------------------------------------------------------------------
    # Нормализация ответа — v3.2/results (current) + старые форматы
    # ------------------------------------------------------------------

    def parse_payload(
        self,
        payload: object,
        departure_city: str,
        arrival_city: str,
    ) -> list[TicketDTO]:
        """Преобразует ответ Aviasales в список :class:`TicketDTO`.

        Поддерживает текущий формат v3.2/results (чанк с ``tickets`` и
        ``flight_legs``) и исторические форматы (legacy ``proposals``).
        """
        # Новый формат: список из одного чанка с полями tickets + flight_legs.
        if isinstance(payload, list):
            for chunk in payload:
                if isinstance(chunk, dict) and "tickets" in chunk and "flight_legs" in chunk:
                    return self._parse_v3_chunk(chunk, departure_city, arrival_city)

        # Legacy-форматы.
        proposals = self._extract_proposals_legacy(payload)
        tickets: list[TicketDTO] = []
        for proposal in proposals:
            try:
                ticket = self._legacy_proposal_to_dto(proposal, departure_city, arrival_city)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] Пропуск предложения: %s", self.source_name, exc)
                continue
            if ticket is not None:
                tickets.append(ticket)
        return tickets

    # ------------------------------------------------------------------
    # Новый формат v3.2/results
    # ------------------------------------------------------------------

    def _parse_v3_chunk(
        self,
        chunk: dict[str, Any],
        departure_city: str,
        arrival_city: str,
    ) -> list[TicketDTO]:
        """Парсит чанк ответа формата v3.2/results."""
        flight_legs: list[dict[str, Any]] = chunk.get("flight_legs") or []
        raw_tickets: list[dict[str, Any]] = chunk.get("tickets") or []
        result: list[TicketDTO] = []

        for raw in raw_tickets:
            try:
                dto = self._v3_ticket_to_dto(raw, flight_legs, departure_city, arrival_city)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] Пропуск билета v3: %s", self.source_name, exc)
                continue
            if dto is not None:
                result.append(dto)

        logger.info("[%s] v3.2 парсер: %d/%d билетов", self.source_name, len(result), len(raw_tickets))
        return result

    @staticmethod
    def _v3_ticket_to_dto(
        raw: dict[str, Any],
        flight_legs: list[dict[str, Any]],
        departure_city: str,
        arrival_city: str,
    ) -> TicketDTO | None:
        """Конвертирует один билет из формата v3.2 в DTO."""
        segments = raw.get("segments") or []
        if not segments:
            return None

        # Собираем все индексы рейсов по всем сегментам.
        indices: list[int] = []
        for seg in segments:
            for idx in seg.get("flights") or []:
                if isinstance(idx, int):
                    indices.append(idx)
        if not indices:
            return None

        try:
            first_leg = flight_legs[indices[0]]
            last_leg = flight_legs[indices[-1]]
        except IndexError:
            return None

        dep_raw = first_leg.get("local_departure_date_time") or ""
        arr_raw = last_leg.get("local_arrival_date_time") or ""
        dep_unix = first_leg.get("departure_unix_timestamp")
        arr_unix = last_leg.get("arrival_unix_timestamp")

        if not dep_raw and not dep_unix:
            return None

        dep_iso = dep_raw.replace(" ", "T") if dep_raw else datetime.fromtimestamp(dep_unix).isoformat()  # type: ignore[arg-type]
        arr_iso = arr_raw.replace(" ", "T") if arr_raw else datetime.fromtimestamp(arr_unix).isoformat()  # type: ignore[arg-type]

        if dep_unix and arr_unix:
            duration = max(0, (arr_unix - dep_unix) // 60)
        else:
            try:
                dt_dep = datetime.fromisoformat(dep_iso)
                dt_arr = datetime.fromisoformat(arr_iso)
                duration = max(0, int((dt_arr - dt_dep).total_seconds() // 60))
            except ValueError:
                duration = 0

        proposals: list[dict[str, Any]] = raw.get("proposals") or []
        if not proposals:
            return None

        best_price = min(
            (p.get("price") or {}).get("value", float("inf"))
            for p in proposals
            if isinstance(p.get("price"), dict)
        )
        if best_price == float("inf"):
            return None

        has_baggage = False
        for proposal in proposals:
            for ft in (proposal.get("flight_terms") or {}).values():
                if not isinstance(ft, dict):
                    continue
                if ft.get("total_baggage_count", 0) > 0 or (ft.get("baggage") or {}).get("count", 0) > 0:
                    has_baggage = True
                    break
            if has_baggage:
                break

        return TicketDTO(
            source="aviasales",
            departure_city=departure_city,
            arrival_city=arrival_city,
            departure_time=dep_iso,
            arrival_time=arr_iso,
            duration_minutes=duration,
            price=int(round(best_price)),
            has_baggage=has_baggage,
        )

    # ------------------------------------------------------------------
    # Legacy-форматы (сохранены для обратной совместимости)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_proposals_legacy(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        for key in ("proposals", "tickets", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("proposals") or value.get("tickets")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        return []

    def _legacy_proposal_to_dto(
        self,
        proposal: dict[str, Any],
        departure_city: str,
        arrival_city: str,
    ) -> TicketDTO | None:
        segments = proposal.get("segment") or proposal.get("segments") or []
        if not segments:
            return None
        flights = self._collect_flights(segments)
        if not flights:
            return None

        first_flight = flights[0]
        last_flight = flights[-1]
        dep_dt = self._iso_from_flight(first_flight, "departure")
        arr_dt = self._iso_from_flight(last_flight, "arrival")
        if dep_dt is None or arr_dt is None:
            return None

        delta: timedelta = arr_dt - dep_dt
        duration = max(int(delta.total_seconds() // 60), 0)
        price_rub = self._legacy_min_price(proposal)
        if price_rub is None:
            return None
        has_baggage = self._legacy_baggage_flag(proposal)

        return TicketDTO(
            source=self.source_name,
            departure_city=departure_city,
            arrival_city=arrival_city,
            departure_time=dep_dt.isoformat(),
            arrival_time=arr_dt.isoformat(),
            duration_minutes=duration,
            price=price_rub,
            has_baggage=has_baggage,
        )

    @staticmethod
    def _collect_flights(segments: Iterable[Any]) -> list[dict[str, Any]]:
        flights: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            for flight in segment.get("flight") or segment.get("flights") or []:
                if isinstance(flight, dict):
                    flights.append(flight)
        return flights

    @staticmethod
    def _iso_from_flight(flight: dict[str, Any], prefix: str) -> datetime | None:
        date_str = flight.get(f"{prefix}_date")
        time_str = flight.get(f"{prefix}_time")
        single = flight.get(prefix)
        try:
            if isinstance(single, str) and single:
                return datetime.fromisoformat(single.replace("Z", "+00:00"))
            if date_str and time_str:
                return datetime.fromisoformat(f"{date_str}T{time_str}")
        except ValueError:
            return None
        return None

    @staticmethod
    def _legacy_min_price(proposal: dict[str, Any]) -> int | None:
        prices: list[float] = []
        terms = proposal.get("terms")
        if isinstance(terms, dict):
            for term in terms.values():
                if not isinstance(term, dict):
                    continue
                price = term.get("unified_price") or term.get("price")
                if isinstance(price, (int, float)) and price > 0:
                    prices.append(float(price))
        top_price = proposal.get("price")
        if isinstance(top_price, dict):
            value = top_price.get("value") or top_price.get("amount")
            if isinstance(value, (int, float)) and value > 0:
                prices.append(float(value))
        elif isinstance(top_price, (int, float)) and top_price > 0:
            prices.append(float(top_price))
        return int(round(min(prices))) if prices else None

    @staticmethod
    def _legacy_baggage_flag(proposal: dict[str, Any]) -> bool:
        terms = proposal.get("terms")
        if isinstance(terms, dict):
            for term in terms.values():
                if not isinstance(term, dict):
                    continue
                codes = term.get("flights_baggage") or term.get("baggage") or []
                if isinstance(codes, str):
                    codes = [codes]
                for code in codes:
                    if isinstance(code, str) and code and not code.startswith("0"):
                        return True
        return False