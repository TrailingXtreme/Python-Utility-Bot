# Location: bot/cogs/InviteTracker/__init__.py
"""
InviteTracker cog — invite attribution, join/leave logging, and leaderboard.

How invite tracking works
─────────────────────────
Discord does not tell you *which* invite a member used to join.  The only
reliable technique is to snapshot the invite list before each join, then
compare it with the fresh list fetched after the join event fires.  The
invite whose use-count increased by one is the one that was used.

To make that comparison possible, this cog maintains an in-memory cache:

    _cache: dict[guild_id, dict[invite_code, discord.Invite]]

The cache is populated on cog load (after the bot is ready) and kept up to
date via the on_invite_create / on_invite_delete listeners.

Edge cases acknowledged
───────────────────────
• Concurrent joins  — two people joining within the same event dispatch may
  cause misattribution; this is a fundamental Discord API limitation.
• Vanity URL joins  — not tracked (requires a separate guild.vanity_invite()
  call and is excluded here for simplicity).
• Missing permissions — if the bot lacks Manage Guild, guild.invites() raises
  HTTPException; the cog gracefully skips tracking for that guild.

Log-channel persistence
───────────────────────
The log_channel is stored on every invite_tracking row.  On cog load the
cog calls InviteRepository.get_guild_log_channels() to pre-fill
self._log_channels from the DB.  When /invite setup is called for a guild
with no rows yet, the channel is held in self._log_channels and written to
the DB the first time record_invite() creates a row for that guild.

Commands
────────
/invite stats [member]     — invite count + unique count for a member
/invite leaderboard        — top-10 inviters embed (public)
/invite setup <channel>    — set the join/leave log channel (manage_guild)

Intents required
────────────────
• Intents.members          — on_member_join / on_member_remove
• Intents.guilds           — on_guild_join / on_invite_create / on_invite_delete
Both are enabled in bot.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from util.db.models import InviteTracking

log = logging.getLogger(__name__)

# Medal emoji for leaderboard top-3 positions.
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ── Embed helpers ──────────────────────────────────────────────────────────────

def _join_embed(
    member: discord.Member,
    invite: discord.Invite,
    record: InviteTracking,
) -> discord.Embed:
    """Rich embed sent to the log channel when a member joins."""
    embed = discord.Embed(
        title="📨 Member Joined",
        colour=discord.Colour.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member", value=f"{member.mention} `{member.id}`", inline=False)

    inviter = invite.inviter
    if inviter:
        embed.add_field(
            name="Invited by",
            value=f"{inviter.mention} `{inviter.id}`",
            inline=True,
        )
        embed.add_field(
            name="Their invite count",
            value=f"**{record.count}** total · **{record.unique_invites}** unique",
            inline=True,
        )
    else:
        embed.add_field(name="Invited by", value="*Unknown (vanity / widget)*", inline=True)

    embed.add_field(name="Invite code", value=f"`{invite.code}`", inline=True)

    if invite.expires_at:
        embed.add_field(
            name="Invite expires",
            value=discord.utils.format_dt(
                invite.expires_at.replace(tzinfo=timezone.utc)
                if invite.expires_at.tzinfo is None
                else invite.expires_at,
                "R",
            ),
            inline=True,
        )

    embed.set_footer(
        text=f"Account created {discord.utils.format_dt(member.created_at, 'R')}"
    )
    return embed


def _leave_embed(
    member: discord.Member,
    inviter_id: int | None,
) -> discord.Embed:
    """Embed sent to the log channel when a member leaves."""
    embed = discord.Embed(
        title="📤 Member Left",
        colour=discord.Colour.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Member",
        value=f"`{member}` · `{member.id}`",
        inline=False,
    )
    if inviter_id:
        embed.add_field(
            name="Originally invited by",
            value=f"<@{inviter_id}> `{inviter_id}`",
            inline=True,
        )
    else:
        embed.add_field(
            name="Originally invited by",
            value="*Unknown*",
            inline=True,
        )
    if member.joined_at:
        embed.add_field(
            name="Was here for",
            value=discord.utils.format_dt(member.joined_at, "R"),
            inline=True,
        )
    return embed


def _stats_embed(
    target: discord.Member | discord.User,
    record: InviteTracking | None,
    guild_name: str,
) -> discord.Embed:
    """Invite statistics embed for a single member."""
    embed = discord.Embed(
        title=f"💌 Invite Stats — {target.display_name}",
        colour=discord.Colour.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=guild_name)

    if record is None or record.count == 0:
        embed.description = f"{target.mention} hasn't invited anyone yet."
        return embed

    embed.add_field(name="Total invites", value=str(record.count), inline=True)
    embed.add_field(name="Unique invites", value=str(record.unique_invites), inline=True)

    # Show up to 15 invited users; truncate the rest.
    users = record.invited_users
    MAX_SHOWN = 15
    mentions = " ".join(f"<@{uid}>" for uid in users[:MAX_SHOWN])
    if len(users) > MAX_SHOWN:
        mentions += f"\n*…and {len(users) - MAX_SHOWN} more*"
    if mentions:
        embed.add_field(name="Invited", value=mentions, inline=False)

    return embed


def _leaderboard_embed(
    rows: list[InviteTracking],
    guild: discord.Guild,
) -> discord.Embed:
    """Top-10 inviters embed."""
    embed = discord.Embed(
        title=f"🏆 Invite Leaderboard — {guild.name}",
        colour=discord.Colour.gold(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    if not rows:
        embed.description = "*No invite data recorded yet.*"
        return embed

    lines: list[str] = []
    for i, record in enumerate(rows, start=1):
        medal = _MEDALS.get(i, f"`{i}.`")
        lines.append(
            f"{medal} <@{record.inviter_id}> — "
            f"**{record.count}** invite(s) · "
            f"**{record.unique_invites}** unique"
        )

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Showing top {len(rows)} inviters")
    return embed


# ── Cog ────────────────────────────────────────────────────────────────────────

class InviteTracker(commands.Cog, description="Track invites, log joins/leaves, and view leaderboards."):
    COG_EMOJI = "📨"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # guild_id -> {invite_code: discord.Invite}
        # Populated after READY and kept live via invite create/delete listeners.
        self._cache: dict[int, dict[str, discord.Invite]] = {}

        # guild_id -> log_channel_id
        # Primary in-memory source.  Populated from DB on ready; updated by
        # /invite setup.  New guilds with no rows yet are stored here until
        # their first record_invite() writes the channel to the DB.
        self._log_channels: dict[int, int] = {}

        # Task handle so we can cancel it cleanly on cog_unload.
        self._populate_task: asyncio.Task[None] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """Schedule the cache+log-channel population for after READY fires.

        guild.invites() is unavailable before the gateway READY event, so we
        cannot call it synchronously here.  asyncio.ensure_future schedules
        _populate_on_ready() to run in the background; it awaits
        bot.wait_until_ready() before touching any guild state.
        """
        self._populate_task = asyncio.ensure_future(self._populate_on_ready())

    async def cog_unload(self) -> None:
        if self._populate_task and not self._populate_task.done():
            self._populate_task.cancel()

    # ── Internal cache helpers ────────────────────────────────────────────────

    async def _populate_on_ready(self) -> None:
        """Wait for READY, then load invites and log channels for all guilds."""
        await self.bot.wait_until_ready()

        # Pre-fill log channels from DB (survives restarts).
        try:
            self._log_channels = await self.bot.db.invite.get_guild_log_channels()  # type: ignore[attr-defined]
            log.info("InviteTracker: loaded %d log channel(s) from DB.", len(self._log_channels))
        except Exception as exc:  # noqa: BLE001
            log.warning("InviteTracker: could not load log channels from DB: %s", exc)

        await self._populate_all()

    async def _populate(self, guild: discord.Guild) -> None:
        """Fetch and cache all active invites for *guild*.

        Silently skips guilds where the bot lacks Manage Guild permission.
        """
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {inv.code: inv for inv in invites}
            log.debug("InviteTracker: cached %d invite(s) for %s.", len(invites), guild.name)
        except discord.Forbidden:
            self._cache.setdefault(guild.id, {})
            log.debug("InviteTracker: missing Manage Guild in %s — skipping.", guild.name)
        except discord.HTTPException as exc:
            self._cache.setdefault(guild.id, {})
            log.warning("InviteTracker: failed to fetch invites for %s: %s", guild.name, exc)

    async def _populate_all(self) -> None:
        """Populate the invite cache for every guild the bot is in."""
        for guild in self.bot.guilds:
            await self._populate(guild)
        log.info("InviteTracker: invite cache populated for %d guild(s).", len(self.bot.guilds))

    async def _find_used_invite(
        self, guild: discord.Guild
    ) -> discord.Invite | None:
        """Diff the cached invite list against a fresh fetch to identify which
        invite was just used.

        Algorithm
        ─────────
        1.  Snapshot the old cache for this guild.
        2.  Fetch the current invite list from Discord.
        3.  Update the cache with the fresh list.
        4.  Walk the fresh list: the invite whose use-count is higher than the
            cached count is the one used.
        5.  Walk the old list for codes no longer present: a missing invite
            with max_uses == 1 was auto-deleted after use.

        Limitations
        ───────────
        • Concurrent joins can cause misattribution (Discord API limitation).
        • Vanity-URL joins are not attributed (excluded for simplicity).
        """
        old: dict[str, discord.Invite] = dict(self._cache.get(guild.id, {}))

        try:
            current_invites = await guild.invites()
        except discord.Forbidden:
            log.debug("InviteTracker: can't fetch invites for %s (Forbidden).", guild.name)
            return None
        except discord.HTTPException as exc:
            log.warning("InviteTracker: HTTPException fetching invites for %s: %s", guild.name, exc)
            return None

        current: dict[str, discord.Invite] = {inv.code: inv for inv in current_invites}
        # Update cache *after* we have the snapshot so the next join event
        # will compare against this fresh state.
        self._cache[guild.id] = current

        # Case 1 — invite still in list; use count went up.
        for code, inv in current.items():
            cached_inv = old.get(code)
            if cached_inv is None:
                # Created between our last cache refresh and this join.
                if inv.uses and inv.uses > 0:
                    return inv
            elif inv.uses > cached_inv.uses:
                return inv

        # Case 2 — single-use invite auto-deleted the moment it was consumed.
        for code, inv in old.items():
            if code not in current and inv.max_uses == 1:
                return inv

        return None

    # ── Guild / invite listeners ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._populate(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._cache.pop(guild.id, None)
        self._log_channels.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        guild_cache = self._cache.setdefault(invite.guild.id, {})
        guild_cache[invite.code] = invite

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self._cache.get(invite.guild.id, {}).pop(invite.code, None)

    # ── Member join ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Record the invite used and post a log embed when a member joins."""
        if member.bot:
            return  # bots are added via OAuth, not invite links

        guild = member.guild
        invite = await self._find_used_invite(guild)

        if invite is None or invite.inviter is None:
            # Can't attribute the join; still log the event without invite info.
            await self._send_unattributed_join(member)
            return

        log_channel_id = self._log_channels.get(guild.id)

        try:
            record = await self.bot.db.invite.record_invite(  # type: ignore[attr-defined]
                guild_id=guild.id,
                inviter_id=invite.inviter.id,
                invited_user_id=member.id,
                log_channel=log_channel_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("InviteTracker: DB error recording invite for %s: %s", member, exc)
            return

        if log_channel_id:
            channel = guild.get_channel(log_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(embed=_join_embed(member, invite, record))
                except discord.HTTPException as exc:
                    log.warning("InviteTracker: failed to send join log: %s", exc)

    async def _send_unattributed_join(self, member: discord.Member) -> None:
        """Send a minimal join log when the used invite cannot be determined."""
        log_channel_id = self._log_channels.get(member.guild.id)
        if not log_channel_id:
            return
        channel = member.guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title="📨 Member Joined",
            colour=discord.Colour.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} `{member.id}`", inline=False)
        embed.add_field(name="Invited by", value="*Could not be determined*", inline=True)
        embed.set_footer(
            text=f"Account created {discord.utils.format_dt(member.created_at, 'R')}"
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("InviteTracker: failed to send unattributed join log: %s", exc)

    # ── Member leave ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Post a leave log embed, attributing the original inviter if known."""
        if member.bot:
            return

        log_channel_id = self._log_channels.get(member.guild.id)
        if not log_channel_id:
            return

        channel = member.guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        inviter_id: int | None = None
        try:
            inviter_id = await self.bot.db.invite.get_inviter_of(  # type: ignore[attr-defined]
                member.guild.id, member.id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("InviteTracker: DB error looking up inviter for %s: %s", member, exc)

        try:
            await channel.send(embed=_leave_embed(member, inviter_id))
        except discord.HTTPException as exc:
            log.warning("InviteTracker: failed to send leave log: %s", exc)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f":no_entry: You need the {perms} permission(s) to use this command."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── Command group ─────────────────────────────────────────────────────────

    invite_grp = app_commands.Group(
        name="invite",
        description="Invite tracking commands.",
    )

    # ── /invite stats ─────────────────────────────────────────────────────────

    @invite_grp.command(
        name="stats",
        description="Show how many members a user has invited.",
    )
    @app_commands.describe(member="Member to look up (defaults to you).")
    async def invite_stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = member or interaction.user

        await interaction.response.defer()

        record: InviteTracking | None = None
        if isinstance(target, discord.Member):
            try:
                record = await self.bot.db.invite.get(  # type: ignore[attr-defined]
                    interaction.guild.id, target.id
                )
            except Exception as exc:  # noqa: BLE001
                log.error("InviteTracker: DB error fetching stats for %s: %s", target, exc)

        await interaction.followup.send(
            embed=_stats_embed(target, record, interaction.guild.name)
        )

    # ── /invite leaderboard ───────────────────────────────────────────────────

    @invite_grp.command(
        name="leaderboard",
        description="Show the top 10 inviters in this server.",
    )
    async def invite_leaderboard(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer()

        try:
            rows: list[InviteTracking] = await self.bot.db.invite.get_leaderboard(  # type: ignore[attr-defined]
                interaction.guild.id, limit=10
            )
        except Exception as exc:  # noqa: BLE001
            log.error("InviteTracker: DB error fetching leaderboard: %s", exc)
            await interaction.followup.send(
                ":warning: Could not fetch leaderboard data.", ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=_leaderboard_embed(rows, interaction.guild)
        )

    # ── /invite setup ─────────────────────────────────────────────────────────

    @invite_grp.command(
        name="setup",
        description="Set the channel where join and leave events are logged.",
    )
    @app_commands.describe(channel="Text channel to send join/leave log messages to.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(send_messages=True, embed_links=True)
    async def invite_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        # Always update the in-memory map immediately.
        self._log_channels[interaction.guild.id] = channel.id

        try:
            updated = await self.bot.db.invite.set_log_channel(  # type: ignore[attr-defined]
                interaction.guild.id, channel.id
            )
        except Exception as exc:  # noqa: BLE001
            log.error("InviteTracker: DB error setting log channel: %s", exc)
            await interaction.followup.send(
                ":warning: Failed to persist the log channel to the database. "
                "It has been set in memory for this session.",
                ephemeral=True,
            )
            return

        note = (
            ""
            if updated
            else "\n-# No invite records exist yet — the channel will be "
                 "stored automatically when the first invite is recorded."
        )

        embed = discord.Embed(
            title="✅ Invite log channel set",
            description=(
                f"Join and leave events will now be logged in {channel.mention}.{note}"
            ),
            colour=discord.Colour.green(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Send a confirmation ping in the log channel itself.
        try:
            await channel.send(
                embed=discord.Embed(
                    description=(
                        f"✅ This channel has been set as the invite log by "
                        f"{interaction.user.mention}."
                    ),
                    colour=discord.Colour.blurple(),
                )
            )
        except discord.HTTPException:
            pass  # Non-critical; the setup is already complete.


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    print("Invite Cog will be implemented in a future update. Stay tuned!")
    # await bot.add_cog(InviteTracker(bot))