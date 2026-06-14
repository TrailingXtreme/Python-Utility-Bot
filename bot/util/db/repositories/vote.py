"""VoteRepository — bot_votes + vote_records tables."""

import asyncpg
from config import settings

from util.db.models import BotVotes


class VoteRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_or_create(self) -> BotVotes:
        vote_id = settings.secret_id.get_secret_value()
        row = await self.pool.fetchrow(
            "SELECT * FROM bot_votes WHERE id = $1", vote_id
        )
        if row is None:
            row = await self.pool.fetchrow(
                "INSERT INTO bot_votes (id) VALUES ($1) RETURNING *", vote_id
            )
        return BotVotes.model_validate(dict(row))

    async def add_like(self) -> BotVotes:
        vote_id = settings.secret_id.get_secret_value()
        row = await self.pool.fetchrow(
            "UPDATE bot_votes SET likes = likes + 1 WHERE id = $1 RETURNING *",
            vote_id,
        )
        return BotVotes.model_validate(dict(row))

    async def add_dislike(self) -> BotVotes:
        vote_id = settings.secret_id.get_secret_value()
        row = await self.pool.fetchrow(
            "UPDATE bot_votes SET dislikes = dislikes + 1 WHERE id = $1 RETURNING *",
            vote_id,
        )
        return BotVotes.model_validate(dict(row))

    async def has_voted(self, user_id: int) -> bool:
        val = await self.pool.fetchval(
            "SELECT 1 FROM vote_records WHERE user_id = $1", user_id
        )
        return val is not None

    async def record_vote(self, user_id: int) -> None:
        await self.pool.execute(
            "INSERT INTO vote_records (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id,
        )