"""AfkRepository — afk_status table."""

import asyncpg

from util.db.models import AfkStatus


class AfkRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get(self, user_id: int) -> AfkStatus | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM afk_status WHERE user_id = $1", user_id
        )
        return AfkStatus.model_validate(dict(row)) if row else None

    async def set(
        self, user_id: int, guild_id: int, original_name: str, reason: str
    ) -> AfkStatus:
        row = await self.pool.fetchrow(
            """
            INSERT INTO afk_status (user_id, guild_id, original_name, reason)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
                SET guild_id = EXCLUDED.guild_id,
                    original_name = EXCLUDED.original_name,
                    reason = EXCLUDED.reason
            RETURNING *
            """,
            user_id, guild_id, original_name, reason,
        )
        return AfkStatus.model_validate(dict(row))

    async def clear(self, user_id: int) -> bool:
        """Returns True if the user was AFK (row existed and was deleted)."""
        result = await self.pool.execute(
            "DELETE FROM afk_status WHERE user_id = $1", user_id
        )
        return result != "DELETE 0"