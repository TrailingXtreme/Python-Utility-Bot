# Location: bot/cogs/About/__init__.py
"""
About cog — bot information and the global like/dislike vote system.

Commands
────────
/about    — version, git SHA, uptime, guild/user counts, and link buttons
            (GitHub always shown; top.gg only if settings.topgg_token is set).
/vote     — current like/dislike tally with interactive Like/Dislike buttons.
            Backed by VoteRepository.get_or_create() / add_like() /
            add_dislike() / has_voted() / record_vote().

Uses: bot.db.vote  (VoteRepository)
"""

from __future__ import annotations

import time

import discord
from config import settings
from discord import app_commands
from discord.ext import commands

from util.constants import Client, Emojis


# ── Embed helpers ──────────────────────────────────────────────────────────────

def _format_uptime(start_time: float) -> str:
    """Human-readable uptime, e.g. '2d 4h 13m'."""
    if start_time <= 0:
        return "Unknown"
    seconds = int(time.time() - start_time)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _about_embed(bot: commands.Bot) -> discord.Embed:
    total_members = sum(g.member_count or 0 for g in bot.guilds)

    embed = discord.Embed(
        title=f"ℹ️ About {Client.name}",
        description=(
            "A modern, modular Discord bot built on `discord.py` "
            "and backed by PostgreSQL."
        ),
        colour=discord.Colour.blurple(),
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(name="Version", value=f"`{Client.bot_version}`", inline=True)
    embed.add_field(name="Commit", value=f"`{settings.git_sha}`", inline=True)
    embed.add_field(name="Uptime", value=_format_uptime(bot.start_time), inline=True)

    embed.add_field(name="Servers", value=f"{len(bot.guilds):,}", inline=True)
    embed.add_field(name="Users", value=f"{total_members:,}", inline=True)
    shard_count = getattr(bot, "shard_count", None) or 1
    embed.add_field(name="Shards", value=str(shard_count), inline=True)

    embed.add_field(
        name="Library",
        value="[discord.py](https://github.com/Rapptz/discord.py)",
        inline=True,
    )
    embed.add_field(name="Default Prefix", value=f"`{Client.default_prefix}`", inline=True)
    embed.set_footer(text="Use /vote to support development!")

    return embed


def _vote_embed(likes: int, dislikes: int, approval_pct: float) -> discord.Embed:
    embed = discord.Embed(
        title=f"💌 Support {Client.name}",
        description=(
            "If you enjoy using this bot, consider leaving a vote below — "
            "it helps others discover it!"
        ),
        colour=discord.Colour.gold(),
    )
    embed.add_field(name="👍 Likes", value=str(likes), inline=True)
    embed.add_field(name="👎 Dislikes", value=str(dislikes), inline=True)
    embed.add_field(name="Approval", value=f"{approval_pct}%", inline=True)
    return embed


# ── Vote view ──────────────────────────────────────────────────────────────────

class VoteView(discord.ui.View):
    """Like / Dislike buttons backed by VoteRepository.

    Fixed custom_ids are used for consistency with the rest of the codebase's
    persistent-view convention, even though the embed is always freshly
    fetched (tallies are global, not per-message-state, so there's no strict
    restart-persistence requirement here — but using fixed IDs costs nothing
    and avoids "this interaction failed" weirdness if the view object itself
    were ever reconstructed across a restart for some other reason).

    A user who has already voted (per vote_records) is blocked from voting
    again — has_voted() is checked at click-time, not just at view-creation
    time, since the view may remain visible long after creation.
    """

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=180)
        self.bot = bot

    async def _cast(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
        *,
        like: bool,
    ) -> None:
        already = await self.bot.db.vote.has_voted(interaction.user.id)  # type: ignore[attr-defined]
        if already:
            await interaction.response.send_message(
                ":no_entry: You've already cast your vote — thank you!",
                ephemeral=True,
            )
            return

        if like:
            tally = await self.bot.db.vote.add_like()  # type: ignore[attr-defined]
        else:
            tally = await self.bot.db.vote.add_dislike()  # type: ignore[attr-defined]
        await self.bot.db.vote.record_vote(interaction.user.id)  # type: ignore[attr-defined]

        await interaction.response.edit_message(
            embed=_vote_embed(tally.likes, tally.dislikes, tally.approval_pct),
            view=self,
        )

    @discord.ui.button(
        label="Like",
        emoji="👍",
        style=discord.ButtonStyle.success,
        custom_id="about:vote_like",
    )
    async def like_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cast(interaction, button, like=True)

    @discord.ui.button(
        label="Dislike",
        emoji="👎",
        style=discord.ButtonStyle.danger,
        custom_id="about:vote_dislike",
    )
    async def dislike_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cast(interaction, button, like=False)


# ── Cog ────────────────────────────────────────────────────────────────────────

class About(commands.Cog, description="Bot information and the vote system."):
    COG_EMOJI = Emojis.info

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /about ────────────────────────────────────────────────────────────────

    @app_commands.command(name="about", description="Show information about the bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        embed = _about_embed(self.bot)

        link_view = discord.ui.View()
        link_view.add_item(
            discord.ui.Button(
                label="GitHub",
                emoji="🔗",
                style=discord.ButtonStyle.link,
                url=Client.github_bot_repo,
            )
        )
        if settings.topgg_token:
            # A bot listing on top.gg can be safely assumed to exist at this
            # conventional URL once a token is configured for that integration.
            if self.bot.user:
                link_view.add_item(
                    discord.ui.Button(
                        label="Vote on top.gg",
                        emoji="⭐",
                        style=discord.ButtonStyle.link,
                        url=f"https://top.gg/bot/{self.bot.user.id}",
                    )
                )

        await interaction.response.send_message(embed=embed, view=link_view)

    # ── /vote ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="vote", description="Like or dislike the bot.")
    async def vote(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        tally = await self.bot.db.vote.get_or_create()  # type: ignore[attr-defined]
        view = VoteView(self.bot)
        await interaction.followup.send(
            embed=_vote_embed(tally.likes, tally.dislikes, tally.approval_pct),
            view=view,
        )


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(About(bot))