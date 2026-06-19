"""InviteRepository — invite_tracking table.

Methods
───────
get                  — one inviter row (or None)
record_invite        — upsert: increment count + append user; create if missing
set_log_channel      — bulk-update log_channel for all rows in a guild
guild_has_entries    — cheap existence check
get_leaderboard      — top-N inviters by count for a guild
get_log_channel      — fetch the log channel for a guild
get_guild_log_channels — all guilds → their log channel
get_inviter_of       — which inviter_id invited a given user
"""

import asyncpg

from util.db.models import InviteTracking


class InviteRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Existing methods ──────────────────────────────────────────────────────

    async def get(self, guild_id: int, inviter_id: int) -> InviteTracking | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM invite_tracking WHERE guild_id = $1 AND inviter_id = $2",
            guild_id, inviter_id,
        )
        return InviteTracking.model_validate(dict(row)) if row else None

    async def record_invite(
        self,
        guild_id: int,
        inviter_id: int,
        invited_user_id: int,
        log_channel: int | None = None,
    ) -> InviteTracking:
        """Increment count and append invited_user_id.

        Creates the row if it doesn't exist.  log_channel is only written
        on the initial INSERT; subsequent conflicts leave it unchanged so a
        later set_log_channel() call is not silently overwritten.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO invite_tracking
                (guild_id, inviter_id, count, invited_users, log_channel)
            VALUES ($1, $2, 1, ARRAY[$3::BIGINT], $4)
            ON CONFLICT (guild_id, inviter_id) DO UPDATE SET
                count         = invite_tracking.count + 1,
                invited_users = invite_tracking.invited_users
                                || EXCLUDED.invited_users[1]
            RETURNING *
            """,
            guild_id, inviter_id, invited_user_id, log_channel,
        )
        return InviteTracking.model_validate(dict(row))

    async def set_log_channel(self, guild_id: int, channel_id: int) -> bool:
        """Set log_channel on every row for this guild.

        Returns True if at least one row was updated (guild has entries),
        False if the guild has no rows yet.
        """
        result = await self.pool.execute(
            "UPDATE invite_tracking SET log_channel = $2 WHERE guild_id = $1",
            guild_id, channel_id,
        )
        return result != "UPDATE 0"

    async def guild_has_entries(self, guild_id: int) -> bool:
        val = await self.pool.fetchval(
            "SELECT 1 FROM invite_tracking WHERE guild_id = $1 LIMIT 1",
            guild_id,
        )
        return val is not None

    # ── New methods ───────────────────────────────────────────────────────────

    async def get_leaderboard(
        self, guild_id: int, limit: int = 10
    ) -> list[InviteTracking]:
        """Return the top *limit* inviters for a guild, ordered by count DESC."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM invite_tracking
            WHERE guild_id = $1
            ORDER BY count DESC
            LIMIT $2
            """,
            guild_id, limit,
        )
        return [InviteTracking.model_validate(dict(r)) for r in rows]

    async def get_log_channel(self, guild_id: int) -> int | None:
        """Return the log_channel configured for this guild, or None.

        Reads the first row that has a non-null log_channel.  All rows for
        the same guild share the same channel value (enforced by
        set_log_channel), so the row chosen is arbitrary.
        """
        return await self.pool.fetchval(
            """
            SELECT log_channel
            FROM invite_tracking
            WHERE guild_id = $1 AND log_channel IS NOT NULL
            LIMIT 1
            """,
            guild_id,
        )

    async def get_guild_log_channels(self) -> dict[int, int]:
        """Return {guild_id: log_channel_id} for every guild with a log channel set.

        Used during cog startup to pre-populate the in-memory log-channel map
        from the database so the correct channel is used immediately after a
        bot restart, even before the first new invite is recorded.
        """
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (guild_id) guild_id, log_channel
            FROM invite_tracking
            WHERE log_channel IS NOT NULL
            ORDER BY guild_id, id DESC
            """
        )
        return {int(r["guild_id"]): int(r["log_channel"]) for r in rows}

    async def get_inviter_of(
        self, guild_id: int, user_id: int
    ) -> int | None:
        """Return the inviter_id who invited *user_id* in *guild_id*, or None.

        Searches the invited_users array across all rows for the guild.
        Useful for attributing a member-leave event back to the inviter.

        Note: PostgreSQL's ``= ANY(array_column)`` performs a sequential scan
        on the column.  This is acceptable for typical Discord server sizes;
        add a GIN index on invited_users if the table grows very large.
        """
        return await self.pool.fetchval(
            """
            SELECT inviter_id
            FROM invite_tracking
            WHERE guild_id = $1
              AND $2 = ANY(invited_users)
            LIMIT 1
            """,
            guild_id, user_id,
        )
