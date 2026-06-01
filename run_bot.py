"""Точка входа Telegram-бота TripOptimizer (Модуль 6).

Запуск::

    python run_bot.py

Требуются валидный `.env` (прокси, DATABASE_URL, TELEGRAM_BOT_TOKEN) и
доступный экземпляр PostgreSQL.
"""

from src.bot.app import run

if __name__ == "__main__":
    run()
