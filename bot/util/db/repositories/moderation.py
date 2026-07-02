"""ModerationRepository — moderation_settings table.

Per-guild moderation log channels + related settings (mute role,
DM-on-punishment toggle, case counter). See models.ModerationSettings
for the row shape and models.LogEvent for the set of events that get
their own dedicated channel column.
"""

import asyncpg

from util.db.models import LogEvent, ModerationSettings

# Whitelist mapping LogEvent -> column name. Never interpolate a raw
# user-controlled string into the column position of a query — this is
# the only thing allowed to choose the column, same guard pattern as
# SuggestionRepository.set_channel.
_EVENT_COLUMNS: dict[LogEvent, str] = {
    LogEvent.KICK: "kick_log_channel_id",
    LogEvent.BAN: "ban_log_channel_id",
    LogEvent.TIMEOUT: "timeout_log_channel_id",
    LogEvent.AUTOMOD: "automod_log_channel_id",
}


class ModerationRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Reads ────────────────────────────────────────────────────────────────

    async def get(self, guild_id: int) -> ModerationSettings | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM moderation_settings WHERE guild_id = $1", guild_id
        )
        return ModerationSettings.model_validate(dict(row)) if row else None

    async def get_or_create(self, guild_id: int) -> ModerationSettings:
        """Fetch a guild's settings, creating an all-defaults row if none exists yet."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO moderation_settings (guild_id)
            VALUES ($1)
            ON CONFLICT (guild_id) DO UPDATE SET guild_id = EXCLUDED.guild_id
            RETURNING *
            """,
            guild_id,
        )
        return ModerationSettings.model_validate(dict(row))

    # ── Log channels ─────────────────────────────────────────────────────────

    async def set_mod_log_channel(self, guild_id: int, channel_id: int | None) -> ModerationSettings:
        """Set (or clear, with channel_id=None) the general fallback mod-log channel."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO moderation_settings (guild_id, mod_log_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET
                mod_log_channel_id = $2,
                updated_at = NOW()
            RETURNING *
            """,
            guild_id, channel_id,
        )
        return ModerationSettings.model_validate(dict(row))

    async def set_log_channel(
        self, guild_id: int, event: LogEvent, channel_id: int | None,
    ) -> ModerationSettings:
        """Set (or clear, with channel_id=None) the dedicated channel for one LogEvent."""
        if event not in _EVENT_COLUMNS:
            raise ValueError(f"Invalid log event: {event}")
        column = _EVENT_COLUMNS[event]
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO moderation_settings (guild_id, {column})
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET
                {column} = $2,
                updated_at = NOW()
            RETURNING *
            """,
            guild_id, channel_id,
        )
        return ModerationSettings.model_validate(dict(row))

    # ── Other settings ───────────────────────────────────────────────────────

    async def set_muted_role(self, guild_id: int, role_id: int | None) -> ModerationSettings:
        row = await self.pool.fetchrow(
            """
            INSERT INTO moderation_settings (guild_id, muted_role_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET
                muted_role_id = $2,
                updated_at = NOW()
            RETURNING *
            """,
            guild_id, role_id,
        )
        return ModerationSettings.model_validate(dict(row))

    async def set_dm_on_punishment(self, guild_id: int, enabled: bool) -> ModerationSettings:
        row = await self.pool.fetchrow(
            """
            INSERT INTO moderation_settings (guild_id, dm_on_punishment)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET
                dm_on_punishment = $2,
                updated_at = NOW()
            RETURNING *
            """,
            guild_id, enabled,
        )
        return ModerationSettings.model_validate(dict(row))

    async def next_case(self, guild_id: int) -> int:
        """
        Atomically increments and returns the new case_count — use this to
        number mod-log entries (Case #N) the same way the row is created
        lazily if it doesn't exist yet.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO moderation_settings (guild_id, case_count)
            VALUES ($1, 1)
            ON CONFLICT (guild_id) DO UPDATE SET
                case_count = moderation_settings.case_count + 1,
                updated_at = NOW()
            RETURNING case_count
            """,
            guild_id,
        )
        return row["case_count"]