"""BlacklistRepository — blacklisted_users table."""

import asyncpg
import discord


class BlacklistRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def is_blacklisted(self, user_id: int) -> bool:
        val = await self.pool.fetchval(
            "SELECT 1 FROM blacklisted_users WHERE user_id = $1", user_id
        )
        return val is not None

    async def add(self, user: discord.abc.User, guild_id: int) -> None:
        await self.pool.execute(
            """
            INSERT INTO blacklisted_users (user_id, guild_id, user_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user.id, guild_id, user.name,
        )

    async def remove(self, user_id: int) -> bool:
        """Returns True if a row was deleted."""
        result = await self.pool.execute(
            "DELETE FROM blacklisted_users WHERE user_id = $1", user_id
        )
        return result != "DELETE 0"