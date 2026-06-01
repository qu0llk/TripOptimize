"""Пакет репозиториев — слой доступа к данным (паттерн Repository).

Каждый репозиторий отвечает за одну таблицу и скрывает детали SQLAlchemy
от вышестоящих слоёв, работая в рамках переданной извне сессии.
"""

from src.database.repositories.admin_repository import AdminRepository
from src.database.repositories.task_repository import TaskRepository
from src.database.repositories.ticket_repository import TicketRepository

__all__ = [
    "AdminRepository",
    "TaskRepository",
    "TicketRepository",
]
