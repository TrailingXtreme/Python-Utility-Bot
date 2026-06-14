"""
bot/util/db/pool.py
────────────────────
asyncpg pool creation/teardown. bot.py calls these in setup_hook()/close().
"""

import asyncpg
from config import settings


async def create_pool() -> asyncpg.Pool:
    """Create the shared connection pool. Call once in setup_hook()."""
    return await asyncpg.create_pool(
        settings.asyncpg_dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    """Gracefully close the pool. Call in bot.close()."""
    await pool.close()