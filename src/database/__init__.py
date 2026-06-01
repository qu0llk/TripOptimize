"""Пакет работы с базой данных (Модуль 3).

Экспортирует публичный интерфейс слоя БД: декларативную базу, ORM-модели,
перечисление статусов задач, менеджер сессий и репозитории.

Порядок импортов важен: сначала :mod:`base`, затем :mod:`models` (регистрация
таблиц в ``Base.metadata``), и лишь потом менеджер и репозитории — это
исключает циклические импорты.
"""

from src.database.base import Base
from src.database.manager import DatabaseSessionManager
from src.database.models import SearchTask, TaskStatus, TicketCache
from src.database.repositories import (
    AdminRepository,
    TaskRepository,
    TicketRepository,
)

__all__ = [
    "AdminRepository",
    "Base",
    "DatabaseSessionManager",
    "SearchTask",
    "TaskRepository",
    "TaskStatus",
    "TicketCache",
    "TicketRepository",
]
