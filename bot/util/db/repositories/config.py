"""ConfigRepository — guild_config table (prefix per guild)."""

import asyncpg

from util.db.models import GuildConfig

_DEFAULT_PREFIX = "t!"


class ConfigRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_prefix(self, guild_id: int) -> str:
        row = await self.pool.fetchrow(
            "SELECT prefix FROM guild_config WHERE guild_id = $1", guild_id
        )
        return row["prefix"] if row else _DEFAULT_PREFIX

    async def set_prefix(self, guild_id: int, prefix: str) -> GuildConfig:
        row = await self.pool.fetchrow(
            """
            INSERT INTO guild_config (guild_id, prefix)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET prefix = EXCLUDED.prefix
            RETURNING *
            """,
            guild_id, prefix,
        )
        return GuildConfig.model_validate(dict(row))

    async def reset_prefix(self, guild_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM guild_config WHERE guild_id = $1", guild_id
        )