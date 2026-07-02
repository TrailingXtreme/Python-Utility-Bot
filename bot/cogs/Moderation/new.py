"""Moderation cog — member moderation (kick / ban / timeout).
────────────────────────────────────────────────────────────────────────────────
Ported from the nextcord-era ``old_moderation.py`` + ``old_moderation_slash.py``
duo (Phase 5). Those two files each implemented kick/ban as a *separate*
prefix command and a *separate* slash command with duplicated embed-building
code. This cog consolidates both into a single slash-only implementation.

What moved elsewhere and is intentionally NOT reimplemented here:
  /afk                → cogs/Afk           (already split out — see that cog)
  clear / bulk-delete  → /channel purge     (cogs/Channel — filters + confirm)
  toggle command       → not yet ported; command-enable/disable is an owner
                         utility, not member moderation, so it doesn't belong
                         in this cog. Flag for Config/Eval if still wanted.

Commands
────────
  /kick      — Kick a member. Confirmation required.
  /ban       — Ban a member/user, optional message-history deletion window.
               Confirmation required.
  /unban     — Unban a previously-banned user (autocompletes recent bans).
  /timeout   — Apply a Discord timeout ("mute") for a duration.
  /untimeout — Clear an active timeout ("unmute").

Interactive components
──────────────────────
  _TimeoutView       — Base view: disables + edits itself on view timeout.
  _KickConfirmView   — Kick / Cancel confirm (30 s timeout).
  _BanConfirmView    — Ban / Cancel confirm (30 s timeout).

Notes
─────
  • All destructive actions (kick, ban) require an explicit confirmation
    click from the *same user* who invoked the command before anything
    happens — mirroring the pattern used by /channel purge and /channel
    clone in cogs/Channel.
  • A permission-hierarchy guard (`_check_hierarchy`) blocks moderating the
    server owner, the bot itself, or any member whose top role is not
    strictly below both the invoker's and the bot's top role — Discord
    would reject these anyway, but this fails fast with a clear message
    instead of a 403 from the API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord
import humanfriendly
from discord import app_commands
from discord.ext import commands

from util.constants import Colours, Emojis
from util.db.models import LogEvent, ModerationSettings
from util.db.moderation import ModerationRepository

# NOTE: adjust the import path above if your repositories live under
# util/db/repositories/ rather than flat in util/db/ — placed to match
# where suggestion.py's SuggestionRepository resolves from.

# ── Constants ─────────────────────────────────────────────────────────────────

# Discord's hard cap on timeout duration.
_MAX_TIMEOUT = timedelta(days=28)

# Status colours pulled from Colours.DISCORD_COLOURS instead of calling
# discord.Colour.green() / .red() / .yellow().
_SUCCESS_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["green"], 16))
_ERROR_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["red"], 16))
_WARNING_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["yellow"], 16))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _embed(description: str, colour: discord.Colour, *, title: str = "") -> discord.Embed:
    e = discord.Embed(description=description, colour=colour)
    if title:
        e.title = title
    e.timestamp = datetime.now(timezone.utc)
    return e


def _ok(text: str) -> discord.Embed:
    return _embed(f"{Emojis.confirmation}  {text}", _SUCCESS_COLOUR)


def _err(text: str) -> discord.Embed:
    return _embed(f"{Emojis.decline}  {text}", _ERROR_COLOUR)


async def _check_hierarchy(
    interaction: discord.Interaction,
    target: discord.Member,
    *,
    action: str,
) -> bool:
    """Guard against moderating the owner, the bot, or an equal/higher member.

    Returns ``False`` (after sending an ephemeral error) if ``target`` can't
    legally/sensibly be moderated by the invoker, so the caller can just
    ``return``.
    """
    guild = interaction.guild
    assert guild is not None
    invoker = interaction.user
    assert isinstance(invoker, discord.Member)

    if target.id == invoker.id:
        await interaction.response.send_message(
            embed=_err(f"You can't {action} yourself."), ephemeral=True)
        return False

    if target.id == guild.me.id:
        await interaction.response.send_message(
            embed=_err(f"I can't {action} myself."), ephemeral=True)
        return False

    if target.id == guild.owner_id:
        await interaction.response.send_message(
            embed=_err(f"You can't {action} the server owner."), ephemeral=True)
        return False

    if invoker.id != guild.owner_id and target.top_role >= invoker.top_role:
        await interaction.response.send_message(
            embed=_err(
                f"You can't {action} **{target}** — their top role is "
                "equal to or higher than yours."
            ),
            ephemeral=True,
        )
        return False

    if target.top_role >= guild.me.top_role:
        await interaction.response.send_message(
            embed=_err(
                f"I can't {action} **{target}** — their top role is "
                "equal to or higher than mine."
            ),
            ephemeral=True,
        )
        return False

    return True


async def _log_case(
    guild: discord.Guild,
    repo: ModerationRepository,
    event: LogEvent,
    *,
    target: discord.Member | discord.User,
    moderator: discord.User | discord.Member,
    reason: str | None,
    extra: str | None = None,
) -> None:
    """
    Write a numbered case entry to the event's configured log channel
    (falling back to the general mod-log channel), if one is set.
    Silently does nothing if no channel is configured or the configured
    channel no longer exists/isn't sendable — logging must never break
    the moderation action itself.
    """
    settings = await repo.get_or_create(guild.id)
    channel_id = settings.log_channel_for(event)
    if channel_id is None:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    case_no = await repo.next_case(guild.id)
    embed = discord.Embed(
        title=f"Case #{case_no} — {event.value.title()}",
        colour=_WARNING_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Member", value=f"{target} ({target.id})", inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} ({moderator.id})", inline=False)
    embed.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
    if extra:
        embed.add_field(name="Details", value=extra, inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass  # Never let a broken log channel take down the moderation action.


class _TimeoutView(discord.ui.View):
    """Base view that disables its own components and edits the message when
    it times out, instead of leaving dead, clickable-looking buttons behind.
    """

    def __init__(self, *, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self.message: discord.Message | discord.InteractionMessage | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Confirmation views ───────────────────────────────────────────────────────

class _KickConfirmView(_TimeoutView):
    """Ephemeral Kick / Cancel confirmation."""

    def __init__(
        self,
        member: discord.Member,
        requester: discord.User | discord.Member,
        *,
        reason: str | None,
        repo: ModerationRepository,
    ) -> None:
        super().__init__(timeout=30)
        self.member = member
        self.requester = requester
        self.reason = reason
        self.repo = repo

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Kick", emoji=Emojis.hammer, style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        try:
            await self.member.kick(reason=f"By {interaction.user} — {self.reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.response.edit_message(
                embed=_err(f"I don't have permission to kick **{self.member}**."), view=None)
            return
        assert interaction.guild is not None
        await _log_case(
            interaction.guild, self.repo, LogEvent.KICK,
            target=self.member, moderator=interaction.user, reason=self.reason,
        )
        result = _ok(
            f"**{self.member}** has been kicked.\n"
            f"**Reason:** {self.reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {self.requester}")
        await interaction.response.edit_message(embed=result, view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Kick cancelled."), view=None)


class _BanConfirmView(_TimeoutView):
    """Ephemeral Ban / Cancel confirmation."""

    def __init__(
        self,
        member: discord.Member,
        requester: discord.User | discord.Member,
        *,
        reason: str | None,
        delete_message_seconds: int,
    ) -> None:
        super().__init__(timeout=30)
        self.member = member
        self.requester = requester
        self.reason = reason
        self.delete_message_seconds = delete_message_seconds

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ban", emoji=Emojis.hammer, style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        try:
            await self.member.ban(
                reason=f"By {interaction.user} — {self.reason or 'No reason provided.'}",
                delete_message_seconds=self.delete_message_seconds,
            )
        except discord.Forbidden:
            await interaction.response.edit_message(
                embed=_err(f"I don't have permission to ban **{self.member}**."), view=None)
            return
        result = _ok(
            f"**{self.member}** has been banned {Emojis.animated_ban}.\n"
            f"**Reason:** {self.reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {self.requester}")
        await interaction.response.edit_message(embed=result, view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Ban cancelled."), view=None)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Moderation(commands.Cog, description="Kick, ban, and timeout members."):
    COG_EMOJI = Emojis.mod

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /kick ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="kick", description="Kick a member from the server — shows a confirmation first.")
    @app_commands.describe(member="The member to kick", reason="Why you're kicking them")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        if not await _check_hierarchy(interaction, member, action="kick"):
            return
        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Confirm Kick",
            description=(
                f"Kick **{member}** ({member.mention}) from the server?\n"
                f"**Reason:** {reason or 'No reason provided.'}"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        view = _KickConfirmView(member, interaction.user, reason=reason)
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # ── /ban ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Ban a member from the server — shows a confirmation first.")
    @app_commands.describe(
        member="The member to ban",
        reason="Why you're banning them",
        delete_message_days="Delete this member's messages from the last N days (0 – 7)",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        if not await _check_hierarchy(interaction, member, action="ban"):
            return
        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Confirm Ban",
            description=(
                f"Ban **{member}** ({member.mention}) from the server?\n"
                f"**Reason:** {reason or 'No reason provided.'}\n"
                f"**Delete message history:** last {delete_message_days} day(s)"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        view = _BanConfirmView(
            member, interaction.user,
            reason=reason,
            delete_message_seconds=delete_message_days * 86_400,
        )
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # ── /unban ────────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Unban a previously-banned user.")
    @app_commands.describe(user="The user to unban (start typing a name to search recent bans)", reason="Why you're unbanning them")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user: str,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        try:
            user_id = int(user)
        except ValueError:
            await interaction.response.send_message(
                embed=_err("Pick a user from the autocomplete list, or pass a raw user ID."),
                ephemeral=True,
            )
            return

        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            await interaction.response.send_message(
                embed=_err("That user isn't currently banned."), ephemeral=True)
            return

        await interaction.guild.unban(
            ban_entry.user, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        result = _ok(
            f"**{ban_entry.user}** has been unbanned.\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=result, ephemeral=True)

    @unban.autocomplete("user")
    async def unban_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        assert interaction.guild is not None
        choices: list[app_commands.Choice[str]] = []
        async for entry in interaction.guild.bans(limit=1000):
            if current.lower() in str(entry.user).lower():
                choices.append(app_commands.Choice(name=str(entry.user), value=str(entry.user.id)))
            if len(choices) >= 25:
                break
        return choices

    # ── /timeout ──────────────────────────────────────────────────────────────

    @app_commands.command(name="timeout", description="Timeout (mute) a member for a duration.")
    @app_commands.describe(
        member="The member to timeout",
        duration="e.g. 10m, 1h, 3d — max 28 days",
        reason="Why you're timing them out",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str | None = None,
    ) -> None:
        if not await _check_hierarchy(interaction, member, action="timeout"):
            return

        try:
            seconds = humanfriendly.parse_timespan(duration)
        except humanfriendly.InvalidTimespan:
            await interaction.response.send_message(
                embed=_err(f"`{duration}` isn't a valid duration. Try something like `10m`, `1h`, or `3d`."),
                ephemeral=True,
            )
            return

        delta = timedelta(seconds=seconds)
        if delta <= timedelta(seconds=0):
            await interaction.response.send_message(
                embed=_err("Duration must be greater than zero."), ephemeral=True)
            return
        if delta > _MAX_TIMEOUT:
            await interaction.response.send_message(
                embed=_err("Timeouts can't be longer than 28 days."), ephemeral=True)
            return

        try:
            await member.timeout(
                delta, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err(f"I don't have permission to timeout **{member}**."), ephemeral=True)
            return

        until = discord.utils.format_dt(datetime.now(timezone.utc) + delta, style="R")
        result = _ok(
            f"**{member}** has been timed out until {until}.\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=result, ephemeral=True)

    # ── /untimeout ────────────────────────────────────────────────────────────

    @app_commands.command(name="untimeout", description="Remove an active timeout (unmute) from a member.")
    @app_commands.describe(member="The member to remove the timeout from", reason="Why you're removing it")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        if member.timed_out_until is None:
            await interaction.response.send_message(
                embed=_err(f"**{member}** isn't currently timed out."), ephemeral=True)
            return

        try:
            await member.timeout(
                None, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err(f"I don't have permission to untimeout **{member}**."), ephemeral=True)
            return

        result = _ok(f"**{member}**'s timeout has been removed.")
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=result, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))