"""Пакет скрапинга билетов (Модуль 1).

Экспортирует публичный интерфейс модуля: DTO, абстрактный базовый класс
скрапера, две конкретные реализации (`aviasales.ru` и `ticket.rzd.ru`) и
координатор запусков `ScraperManager`.
"""

from typing import TYPE_CHECKING, Any

# Ленивая загрузка (PEP 562): тяжёлые символы, тянущие за собой Playwright,
# импортируются только при первом обращении. Это разрывает связность —
# слой БД может использовать лёгкий :class:`TicketDTO` (`src.scrapers.dto`),
# не требуя установленного Playwright и playwright-stealth.
if TYPE_CHECKING:  # для статических анализаторов — обычные импорты
    from src.scrapers.aviasales_scraper import AviasalesScraper
    from src.scrapers.base_scraper import BaseScraper, ScraperError
    from src.scrapers.dto import TicketDTO
    from src.scrapers.manager import ScraperManager
    from src.scrapers.rzd_scraper import RzdScraper

# Карта "имя символа → модуль", из которого он подгружается по требованию.
_LAZY_EXPORTS: dict[str, str] = {
    "AviasalesScraper": "src.scrapers.aviasales_scraper",
    "BaseScraper": "src.scrapers.base_scraper",
    "ScraperError": "src.scrapers.base_scraper",
    "TicketDTO": "src.scrapers.dto",
    "ScraperManager": "src.scrapers.manager",
    "RzdScraper": "src.scrapers.rzd_scraper",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    """Подгружает публичный символ из подмодуля при первом обращении."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)