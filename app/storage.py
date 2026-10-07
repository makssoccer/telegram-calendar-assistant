from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import DatabaseSettings


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    # Engine управляет пулом соединений, аналогично DataSource в Java.
    # Это не одно постоянно открытое соединение. asyncpg — драйвер PostgreSQL.
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 10, "command_timeout": 15},
    )


async def check_database(engine: AsyncEngine) -> None:
    # Короткая проверка доступности при старте. async with возвращает
    # соединение в пул после завершения блока.
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
