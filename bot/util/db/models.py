"""
bot/util/db/models.py
─────────────────────
Pydantic v2 row models — one class per PostgreSQL table.

Each model represents a single row returned by asyncpg and provides:
  • Full type safety for every column.
  • Computed properties for common derived values.
  • Direct compatibility with asyncpg.Record objects.

Usage
─────
    record = await pool.fetchrow(
        "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
    )
    if record:
        cfg = GuildConfig.model_validate(dict(record))
        print(cfg.prefix)

Why dict(record) rather than model_validate(record) directly?
  asyncpg.Record supports attribute-access, but converting to dict first
  is explicit, always works, and avoids any edge-cases with Pydantic's
  from_attributes introspection on non-standard mapping types.

NOTE: do NOT add 'from __future__ import annotations' to this file.
  Deferred annotation evaluation breaks Pydantic v2's runtime type
  inspection (same reason as bot/config.py).
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field


# ── Discord snowflake type alias ──────────────────────────────────────────────
# All Discord IDs are unsigned 64-bit integers that are always > 0.
# The Field(gt=0) constraint catches accidental zero-values at parse time.
DiscordId = Annotated[int, Field(gt=0)]


# ── Review status enum ────────────────────────────────────────────────────────
class ReviewStatus(StrEnum):
    """
    Maps to the CHECK constraint on suggestions.is_reviewed.
    StrEnum means the value IS the string — no .value needed in queries.

        await pool.execute(
            "UPDATE suggestions SET is_reviewed = $1 WHERE message_id = $2",
            ReviewStatus.ACCEPTED,   # passes "accepted" to the driver
            message_id,
        )
    """
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# ── Moderation log event enum ─────────────────────────────────────────────────
class LogEvent(StrEnum):
    """
    The subset of moderation events that get their own dedicated log
    channel. Anything not listed here (e.g. warns, note-only actions)
    falls back to ``moderation_settings.mod_log_channel_id`` instead of
    getting a column of its own — keeps the settings panel from sprawling
    into one column per possible action, per the "limited custom
    channels" scope agreed for this feature.
    """
    KICK = "kick"
    BAN = "ban"
    TIMEOUT = "timeout"
    AUTOMOD = "automod"


# ── Shared base ───────────────────────────────────────────────────────────────
class _Row(BaseModel):
    """
    Base for all row models.

    from_attributes=True: allows model_validate() to accept objects with
    attribute-style access (asyncpg.Record, dataclasses, ORM rows, etc.)
    in addition to plain dicts.

    frozen=True: row models are immutable — mutating a row object would
    not persist anything to the database, so making them immutable prevents
    accidental bugs.  To modify a row, fetch it, build a new model or call
    the relevant repository method.
    """
    model_config = ConfigDict(from_attributes=True, frozen=True)


# ── 1. guild_config ───────────────────────────────────────────────────────────
class GuildConfig(_Row):
    """
    One row per Discord guild.
    Source: MongoDB `config` collection → table `guild_config`.
    """
    guild_id:   DiscordId
    prefix:     str = Field(default="t!", max_length=10)
    created_at: datetime
    updated_at: datetime


# ── 2. blacklisted_users ──────────────────────────────────────────────────────
class BlacklistedUser(_Row):
    """
    Users banned from interacting with the bot.
    Source: MongoDB `blacklisted_users` → table `blacklisted_users`.
    """
    user_id:        DiscordId
    guild_id:       DiscordId
    user_name:      str = Field(max_length=100)
    blacklisted_at: datetime


# ── 3. afk_status ────────────────────────────────────────────────────────────
class AfkStatus(_Row):
    """
    Active AFK entries.  A row exists while the user is AFK; it is
    deleted when the user sends their next message.

    Source: MongoDB `afk_user_db` → table `afk_status`.

    original_name — the Discord username stored before the AFK nickname
    ("[AFK] name") is applied.  The cog restores it on return.
    Phase 5 TODO: store the actual *nickname* (not username) here so
    users with custom nicknames have them correctly restored.
    """
    user_id:       DiscordId
    guild_id:      DiscordId
    original_name: str = Field(max_length=100)
    reason:        str = Field(default="No reason provided")
    created_at:    datetime

    @computed_field  # type: ignore[misc]
    @property
    def display_reason(self) -> str:
        """Reason truncated to 100 chars for safe embedding in Discord messages."""
        return self.reason[:100] + "…" if len(self.reason) > 100 else self.reason


# ── 4. bot_votes ──────────────────────────────────────────────────────────────
class BotVotes(_Row):
    """
    Global like/dislike counters for the bot (typically one row).
    Source: MongoDB `votes` → table `bot_votes`.

    `id` is the opaque secret_id string from settings.SECRET_ID.
    """
    id:       str
    likes:    int = Field(default=0, ge=0)
    dislikes: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[misc]
    @property
    def total(self) -> int:
        """Total number of votes cast."""
        return self.likes + self.dislikes

    @computed_field  # type: ignore[misc]
    @property
    def approval_pct(self) -> float:
        """Like percentage rounded to one decimal place; 0.0 if no votes."""
        if self.total == 0:
            return 0.0
        return round(self.likes / self.total * 100, 1)


# ── 5. vote_records ───────────────────────────────────────────────────────────
class VoteRecord(_Row):
    """
    One row per user who has voted.  Existence of the row = voted.
    Source: MongoDB `users` (vote tracking) → table `vote_records`.
    """
    user_id:  DiscordId
    voted_at: datetime


# ── 6. suggestion_settings ───────────────────────────────────────────────────
class SuggestionSettings(_Row):
    """
    Per-guild suggestion channel configuration.
    Source: MongoDB `suggestion_db` → table `suggestion_settings`.

    Channel IDs are nullable — the cog creates the row first and
    configures channels separately, so some may be None initially.
    """
    guild_id:           DiscordId
    channel_id:         DiscordId | None = None
    approve_channel_id: DiscordId | None = None
    deny_channel_id:    DiscordId | None = None
    suggestion_count:   int = Field(default=1, ge=1)

    @computed_field  # type: ignore[misc]
    @property
    def is_fully_configured(self) -> bool:
        """True only when all three channel IDs have been set."""
        return (
            self.channel_id is not None
            and self.approve_channel_id is not None
            and self.deny_channel_id is not None
        )


# ── 7. suggestions ────────────────────────────────────────────────────────────
class Suggestion(_Row):
    """
    Individual suggestion records.
    Source: MongoDB `suggestion_status_db` → table `suggestions`.

    guild_id is nullable — the original MongoDB document did not store it.
    Phase 5: populate via a JOIN on suggestion_settings when porting the cog.

    is_reviewed uses ReviewStatus enum; None means still pending.
    """
    message_id:   DiscordId
    guild_id:     DiscordId | None = None
    suggestor_id: DiscordId
    serial_no:    int = Field(ge=1)
    suggestion:   str
    is_reviewed:  ReviewStatus | None = None
    created_at:   datetime
    reviewed_at:  datetime | None = None

    @computed_field  # type: ignore[misc]
    @property
    def is_pending(self) -> bool:
        """True if the suggestion has not yet been reviewed."""
        return self.is_reviewed is None

    @computed_field  # type: ignore[misc]
    @property
    def status_emoji(self) -> str:
        """Single emoji representing the current review state."""
        match self.is_reviewed:
            case ReviewStatus.ACCEPTED:
                return "✅"
            case ReviewStatus.REJECTED:
                return "❌"
            case _:
                return "⏳"


# ── 8. invite_tracking ────────────────────────────────────────────────────────
class InviteTracking(_Row):
    """
    Per-guild, per-inviter invite statistics.
    Source: MongoDB `invites` → table `invite_tracking`.

    invited_users — asyncpg returns PostgreSQL BIGINT[] as list[int] natively.
    log_channel   — nullable; may not be configured yet.
    """
    id:            int              # BIGSERIAL surrogate PK
    guild_id:      DiscordId
    inviter_id:    DiscordId
    count:         int = Field(default=0, ge=0)
    invited_users: list[int] = Field(default_factory=list)
    log_channel:   DiscordId | None = None

    @computed_field  # type: ignore[misc]
    @property
    def unique_invites(self) -> int:
        """Number of distinct users invited (deduplicates the array)."""
        return len(set(self.invited_users))


# ── 9. moderation_settings ────────────────────────────────────────────────────
class ModerationSettings(_Row):
    """
    Per-guild moderation configuration — one row per guild, created lazily
    on first use (see ModerationRepository.get_or_create).

    Modelled after the kind of settings panel Dyno / Arcane expose for
    moderation: a general mod-log channel that acts as the fallback, a
    handful of dedicated per-event channels (kick / ban / timeout /
    AutoMod — see LogEvent), a configured mute role, a DM-on-punishment
    toggle, and an incrementing case counter for numbered mod-log entries.

    All *_channel_id / muted_role_id columns are nullable — a guild may
    only configure some of them, or none at all.
    """
    guild_id:                DiscordId
    mod_log_channel_id:      DiscordId | None = None
    kick_log_channel_id:     DiscordId | None = None
    ban_log_channel_id:      DiscordId | None = None
    timeout_log_channel_id:  DiscordId | None = None
    automod_log_channel_id:  DiscordId | None = None
    muted_role_id:           DiscordId | None = None
    dm_on_punishment:        bool = True
    case_count:               int = Field(default=0, ge=0)
    created_at:               datetime
    updated_at:               datetime

    def log_channel_for(self, event: LogEvent) -> int | None:
        """
        Resolve the channel a given event should be logged to: the
        event's dedicated channel if one is set, otherwise the general
        mod_log_channel_id fallback (which may itself be None).
        """
        specific = {
            LogEvent.KICK: self.kick_log_channel_id,
            LogEvent.BAN: self.ban_log_channel_id,
            LogEvent.TIMEOUT: self.timeout_log_channel_id,
            LogEvent.AUTOMOD: self.automod_log_channel_id,
        }[event]
        return specific if specific is not None else self.mod_log_channel_id

    @computed_field  # type: ignore[misc]
    @property
    def is_logging_configured(self) -> bool:
        """True if at least one log channel (general or per-event) is set."""
        return any((
            self.mod_log_channel_id,
            self.kick_log_channel_id,
            self.ban_log_channel_id,
            self.timeout_log_channel_id,
            self.automod_log_channel_id,
        ))


# ── Public surface ────────────────────────────────────────────────────────────
__all__ = [
    "DiscordId",
    "ReviewStatus",
    "LogEvent",
    # Row models
    "GuildConfig",
    "BlacklistedUser",
    "AfkStatus",
    "BotVotes",
    "VoteRecord",
    "SuggestionSettings",
    "Suggestion",
    "InviteTracking",
    "ModerationSettings",
]