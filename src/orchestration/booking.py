"""Генератор прямых ссылок на покупку билета (Модуль 7A).

:class:`BookingLinkBuilder` формирует deep-link на поисковую выдачу Aviasales
или РЖД с предзаполненными параметрами (города + дата), по которому
пользователь сразу попадает на нужный билет.

Сервис намеренно НЕ импортирует модули скраперов: те тянут за собой Playwright,
а слой API/оркестрации должен оставаться лёгким. Поэтому таблицы кодов городов
продублированы здесь в минимальном объёме (принцип слабой связанности).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from src.config.cities import get_city_catalog
from src.scrapers.dto import TicketDTO

# Город → IATA-метакод (для deep-link Aviasales).
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

# Город → express-код станции РЖД (для deep-link РЖД).
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


def _norm(city: str) -> str:
    """Нормализует название города для поиска в таблицах кодов."""
    return city.strip().lower().replace("ё", "е")


class BookingLinkBuilder:
    """Строит deep-link на бронирование по DTO билета.

    Возвращает ссылку «best-effort»: если город отсутствует в таблице кодов,
    отдаётся ссылка на главную страницу источника — сборка маршрута при этом
    не падает (устойчивость важнее точности ссылки).
    """

    _AVIASALES_TPL: Final[str] = "https://www.aviasales.ru/search/{query}"
    _AVIASALES_HOME: Final[str] = "https://www.aviasales.ru/"
    _RZD_TPL: Final[str] = (
        "https://ticket.rzd.ru/searching-train/{origin}/{destination}"
        "?dir=0&tfl=3&checkSeats=1&code0={origin}&code1={destination}&dt0={date}"
    )
    _RZD_HOME: Final[str] = "https://ticket.rzd.ru/"

    def build(self, ticket: TicketDTO) -> str:
        """Возвращает прямую ссылку на покупку для данного билета."""
        try:
            travel_date = datetime.fromisoformat(ticket.departure_time)
        except ValueError:
            travel_date = None

        if ticket.source == "aviasales":
            return self._aviasales(ticket, travel_date)
        if ticket.source == "rzd":
            return self._rzd(ticket, travel_date)
        return ""

    def _aviasales(self, ticket: TicketDTO, travel_date: datetime | None) -> str:
        # Алиасы (локальная таблица) → полный всемирный справочник городов.
        catalog = get_city_catalog()
        origin = _CITY_TO_IATA.get(_norm(ticket.departure_city)) or catalog.resolve_iata(
            ticket.departure_city
        )
        destination = _CITY_TO_IATA.get(_norm(ticket.arrival_city)) or catalog.resolve_iata(
            ticket.arrival_city
        )
        if not (origin and destination and travel_date):
            return self._AVIASALES_HOME
        # Формат запроса Aviasales: ORIGIN + ДДММ + DESTINATION + кол-во пассажиров.
        query = f"{origin}{travel_date.strftime('%d%m')}{destination}1"
        return self._AVIASALES_TPL.format(query=query)

    def _rzd(self, ticket: TicketDTO, travel_date: datetime | None) -> str:
        origin = _CITY_TO_RZD_CODE.get(_norm(ticket.departure_city))
        destination = _CITY_TO_RZD_CODE.get(_norm(ticket.arrival_city))
        if not (origin and destination and travel_date):
            return self._RZD_HOME
        return self._RZD_TPL.format(
            origin=origin,
            destination=destination,
            date=travel_date.strftime("%d.%m.%Y"),
        )
