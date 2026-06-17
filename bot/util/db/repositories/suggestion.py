"""SuggestionRepository — suggestion_settings + suggestions tables."""

import asyncpg

from util.db.models import ReviewStatus, Suggestion, SuggestionSettings


class SuggestionRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Settings ─────────────────────────────────────────────────────────────

    async def get_settings(self, guild_id: int) -> SuggestionSettings | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM suggestion_settings WHERE guild_id = $1", guild_id
        )
        return SuggestionSettings.model_validate(dict(row)) if row else None

    async def create_settings(
        self,
        guild_id: int,
        channel_id: int | None = None,
        approve_channel_id: int | None = None,
        deny_channel_id: int | None = None,
    ) -> SuggestionSettings:
        row = await self.pool.fetchrow(
            """
            INSERT INTO suggestion_settings
                (guild_id, channel_id, approve_channel_id, deny_channel_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id) DO UPDATE SET
                channel_id = COALESCE(EXCLUDED.channel_id, suggestion_settings.channel_id),
                approve_channel_id = COALESCE(EXCLUDED.approve_channel_id, suggestion_settings.approve_channel_id),
                deny_channel_id = COALESCE(EXCLUDED.deny_channel_id, suggestion_settings.deny_channel_id)
            RETURNING *
            """,
            guild_id, channel_id, approve_channel_id, deny_channel_id,
        )
        return SuggestionSettings.model_validate(dict(row))

    async def set_channel(self, guild_id: int, field: str, channel_id: int) -> None:
        """field must be one of: channel_id, approve_channel_id, deny_channel_id."""
        if field not in {"channel_id", "approve_channel_id", "deny_channel_id"}:
            raise ValueError(f"Invalid field: {field}")
        await self.pool.execute(
            f"UPDATE suggestion_settings SET {field} = $2 WHERE guild_id = $1",
            guild_id, channel_id,
        )

    async def next_serial(self, guild_id: int) -> int:
        """Atomically increments and returns the new suggestion_count."""
        row = await self.pool.fetchrow(
            """
            UPDATE suggestion_settings
            SET suggestion_count = suggestion_count + 1
            WHERE guild_id = $1
            RETURNING suggestion_count
            """,
            guild_id,
        )
        return row["suggestion_count"]

    # ── Suggestions ──────────────────────────────────────────────────────────

    async def create(
        self, message_id: int, guild_id: int, suggestor_id: int,
        serial_no: int, suggestion: str,
    ) -> Suggestion:
        row = await self.pool.fetchrow(
            """
            INSERT INTO suggestions (message_id, guild_id, suggestor_id, serial_no, suggestion)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            message_id, guild_id, suggestor_id, serial_no, suggestion,
        )
        return Suggestion.model_validate(dict(row))

    async def get(self, message_id: int) -> Suggestion | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM suggestions WHERE message_id = $1", message_id
        )
        return Suggestion.model_validate(dict(row)) if row else None

    async def set_status(self, message_id: int, status: ReviewStatus) -> Suggestion:
        row = await self.pool.fetchrow(
            """
            UPDATE suggestions
            SET is_reviewed = $2, reviewed_at = NOW()
            WHERE message_id = $1
            RETURNING *
            """,
            message_id, status,
        )
        return Suggestion.model_validate(dict(row))

    async def get_pending(self, guild_id: int) -> list[Suggestion]:
        """All unreviewed suggestions for a guild — used by autocomplete."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM suggestions
            WHERE guild_id = $1 AND is_reviewed IS NULL
            ORDER BY serial_no
            """,
            guild_id,
        )
        return [Suggestion.model_validate(dict(r)) for r in rows]

    async def get_by_serial(self, guild_id: int, serial_no: int) -> Suggestion | None:
        """Fetch one suggestion by its human-readable serial number."""
        row = await self.pool.fetchrow(
            "SELECT * FROM suggestions WHERE guild_id = $1 AND serial_no = $2",
            guild_id, serial_no,
        )
        return Suggestion.model_validate(dict(row)) if row else None