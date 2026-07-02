"""
bot/util/db/repositories/__init__.py
─────────────────────────────────────
Aggregates all repositories into one object.

In bot.py setup_hook():
    self.pool = await create_pool()
    self.db = Repositories(self.pool)

In cogs:
    await self.bot.db.config.get_prefix(guild_id)
    await self.bot.db.afk.get(user_id)
"""

import asyncpg

from util.db.repositories.afk import AfkRepository
from util.db.repositories.blacklist import BlacklistRepository
from util.db.repositories.config import ConfigRepository
from util.db.repositories.invite import InviteRepository
from util.db.repositories.suggestion import SuggestionRepository
from util.db.repositories.vote import VoteRepository
from util.db.repositories.moderation import ModerationRepository

class Repositories:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.config = ConfigRepository(pool)
        self.blacklist = BlacklistRepository(pool)
        self.afk = AfkRepository(pool)
        self.vote = VoteRepository(pool)
        self.suggestion = SuggestionRepository(pool)
        self.invite = InviteRepository(pool)
        self.moderation = ModerationRepository(pool)


__all__ = ["Repositories"]