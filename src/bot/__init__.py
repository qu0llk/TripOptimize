"""Пакет Telegram-бота TripOptimizer (Модуль 6).

Экспортирует сервисный фасад, роутер хендлеров и точку запуска бота.
"""

from src.bot.service import TripOptimizerService
from src.bot.states import StateMachine

__all__ = ["TripOptimizerService", "StateMachine"]
