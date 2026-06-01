"""Пакет оркестрации асинхронных задач (Модули 4 и 7A).

Экспортирует публичный интерфейс слоя: DTO плеча маршрута, планировщики плеч,
генератор ссылок на бронирование, состояние прогресса и сам оркестратор очереди.
"""

from src.orchestration.booking import BookingLinkBuilder
from src.orchestration.dto import RouteLeg
from src.orchestration.orchestrator import ProgressState, TaskOrchestrator
from src.orchestration.planner import (
    AllPairsLegPlanner,
    LegPlanner,
    SequentialLegPlanner,
)

__all__ = [
    "AllPairsLegPlanner",
    "BookingLinkBuilder",
    "LegPlanner",
    "ProgressState",
    "RouteLeg",
    "SequentialLegPlanner",
    "TaskOrchestrator",
]
