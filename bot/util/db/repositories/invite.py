"""InviteRepository — invite_tracking table."""

import asyncpg

from util.db.models import InviteTracking


class InviteRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get(self, guild_id: int, inviter_id: int) -> InviteTracking | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM invite_tracking WHERE guild_id = $1 AND inviter_id = $2",
            guild_id, inviter_id,
        )
        return InviteTracking.model_validate(dict(row)) if row else None

    async def record_invite(
        self, guild_id: int, inviter_id: int, invited_user_id: int,
        log_channel: int | None = None,
    ) -> InviteTracking:
        """
        Increments count and appends invited_user_id. Creates the row if it
        doesn't exist. log_channel is only set on first insert.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO invite_tracking (guild_id, inviter_id, count, invited_users, log_channel)
            VALUES ($1, $2, 1, ARRAY[$3::BIGINT], $4)
            ON CONFLICT (guild_id, inviter_id) DO UPDATE SET
                count = invite_tracking.count + 1,
                invited_users = invite_tracking.invited_users || EXCLUDED.invited_users[1]
            RETURNING *
            """,
            guild_id, inviter_id, invited_user_id, log_channel,
        )
        return InviteTracking.model_validate(dict(row))

    async def set_log_channel(self, guild_id: int, channel_id: int) -> bool:
        """Returns True if a row existed for this guild (any inviter)."""
        result = await self.pool.execute(
            "UPDATE invite_tracking SET log_channel = $2 WHERE guild_id = $1",
            guild_id, channel_id,
        )
        return result != "UPDATE 0"

    async def guild_has_entries(self, guild_id: int) -> bool:
        val = await self.pool.fetchval(
            "SELECT 1 FROM invite_tracking WHERE guild_id = $1 LIMIT 1", guild_id
        )
        return val is not None