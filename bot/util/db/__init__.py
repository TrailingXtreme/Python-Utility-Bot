"""
bot.util.db
───────────
PostgreSQL data-access layer.

Current contents (Phase 2):
  models.py  — Pydantic v2 row models for every table

Phase 4 will add:
  pool.py          — asyncpg connection pool setup/teardown
  repositories/    — one module per domain (config, blacklist, afk, …)
"""

from util.db.models import (
    AfkStatus,
    BlacklistedUser,
    BotVotes,
    GuildConfig,
    InviteTracking,
    ReviewStatus,
    Suggestion,
    SuggestionSettings,
    VoteRecord,
)

__all__ = [
    "AfkStatus",
    "BlacklistedUser",
    "BotVotes",
    "GuildConfig",
    "InviteTracking",
    "ReviewStatus",
    "Suggestion",
    "SuggestionSettings",
    "VoteRecord",
]