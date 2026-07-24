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
  _TimeoutView         — Base view: disables + edits itself on view timeout.
  _ConfirmViewBase     — Shared Kick/Ban confirm scaffolding: requester-lock
                          + an "Edit Reason" button (opens _ReasonModal).
  _ReasonModal         — Modal used to add/change the reason before confirming.
  _KickConfirmView     — Kick / Cancel confirm (30 s timeout).
  _BanConfirmView      — Ban / Cancel confirm (30 s timeout).
  _ModLogPanelView     — /modlog panel: Select to pick a log slot, a
                          ChannelSelect / RoleSelect to assign it, and a
                          button to flip the DM-on-punishment toggle.

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
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Colours, Emojis
from util.db.models import LogEvent, ModerationSettings
from util.db.repositories.moderation import ModerationRepository

try:
    import humanfriendly
    _HUMANFRIENDLY_AVAILABLE = True
except ImportError:
    humanfriendly = None  # type: ignore[assignment]
    _HUMANFRIENDLY_AVAILABLE = False


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
    embed.add_field(
        name="Member", value=f"{target} ({target.id})", inline=False)
    embed.add_field(name="Moderator",
                    value=f"{moderator} ({moderator.id})", inline=False)
    embed.add_field(
        name="Reason", value=reason or "No reason provided.", inline=False)
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

class _ReasonModal(discord.ui.Modal, title="Edit Reason"):
    """Lets the requester add or change the reason without cancelling and
    re-running the command. Opened from the shared 'Edit Reason' button."""

    def __init__(self, view: "_ConfirmViewBase") -> None:
        super().__init__()
        self._view = view
        self.reason_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            placeholder="No reason provided.",
            required=False,
            max_length=512,
            default=view.reason,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._view.reason = self.reason_input.value or None
        await interaction.response.edit_message(embed=self._view.build_embed())


class _ConfirmViewBase(_TimeoutView):
    """Shared scaffolding for the Kick/Ban confirm views: locks interaction
    to the requester and provides the 'Edit Reason' button (+ modal) so
    subclasses only need to add their action-specific buttons and an embed.
    """

    def __init__(
        self,
        requester: discord.User | discord.Member,
        *,
        reason: str | None,
        repo: ModerationRepository,
    ) -> None:
        super().__init__(timeout=30)
        self.requester = requester
        self.reason = reason
        self.repo = repo

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        raise NotImplementedError

    @discord.ui.button(label="Edit Reason", emoji=Emojis.pencil, style=discord.ButtonStyle.secondary, row=1)
    async def edit_reason(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_ReasonModal(self))


class _KickConfirmView(_ConfirmViewBase):
    """Ephemeral Kick / Cancel confirmation, with reason editing."""

    def __init__(
        self,
        member: discord.Member,
        requester: discord.User | discord.Member,
        *,
        reason: str | None,
        repo: ModerationRepository,
    ) -> None:
        super().__init__(requester, reason=reason, repo=repo)
        self.member = member

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{Emojis.warning}  Confirm Kick",
            description=(
                f"Kick **{self.member}** ({self.member.mention}) from the server?\n"
                f"**Reason:** {self.reason or 'No reason provided.'}"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

    @discord.ui.button(label="Kick", emoji=Emojis.hammer, style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        # Ack immediately — kick() + _log_case() below can be slow enough
        # (Discord API + DB round-trips) that the interaction token dies
        # before we get to responding, causing a "404 Unknown interaction".
        await interaction.response.defer()
        try:
            await self.member.kick(reason=f"By {interaction.user} — {self.reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.edit_original_response(
                embed=_err(f"I don't have permission to kick **{self.member}**."), view=None)
            return
        except discord.HTTPException:
            # e.g. the member already left/was removed out from under us.
            await interaction.edit_original_response(
                embed=_err(f"Failed to kick **{self.member}** — they may no longer be in the server."), view=None)
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
        await interaction.edit_original_response(embed=result, view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Kick cancelled."), view=None)


class _BanConfirmView(_ConfirmViewBase):
    """Ephemeral Ban / Cancel confirmation, with reason editing."""

    def __init__(
        self,
        member: discord.Member,
        requester: discord.User | discord.Member,
        *,
        reason: str | None,
        delete_message_seconds: int,
        repo: ModerationRepository,
    ) -> None:
        super().__init__(requester, reason=reason, repo=repo)
        self.member = member
        self.delete_message_seconds = delete_message_seconds

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{Emojis.warning}  Confirm Ban",
            description=(
                f"Ban **{self.member}** ({self.member.mention}) from the server?\n"
                f"**Reason:** {self.reason or 'No reason provided.'}\n"
                f"**Delete message history:** last {self.delete_message_seconds // 86_400} day(s)"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

    @discord.ui.button(label="Ban", emoji=Emojis.hammer, style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        # Same fix as _KickConfirmView.confirm: ack before the slow work.
        await interaction.response.defer()
        try:
            await self.member.ban(
                reason=f"By {interaction.user} — {self.reason or 'No reason provided.'}",
                delete_message_seconds=self.delete_message_seconds,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                embed=_err(f"I don't have permission to ban **{self.member}**."), view=None)
            return
        except discord.HTTPException:
            await interaction.edit_original_response(
                embed=_err(f"Failed to ban **{self.member}** — they may no longer be in the server."), view=None)
            return
        assert interaction.guild is not None
        await _log_case(
            interaction.guild, self.repo, LogEvent.BAN,
            target=self.member, moderator=interaction.user, reason=self.reason,
        )
        result = _ok(
            f"**{self.member}** has been banned {Emojis.animated_ban}.\n"
            f"**Reason:** {self.reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {self.requester}")
        await interaction.edit_original_response(embed=result, view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Ban cancelled."), view=None)


# ── Modlog settings panel ─────────────────────────────────────────────────────

_EVENT_LABELS: dict[str, str] = {
    "general": "General (fallback)",
    "kick": "Kick",
    "ban": "Ban",
    "timeout": "Timeout",
    "automod": "AutoMod",
}


class _ModLogPanelView(_TimeoutView):
    """Interactive alternative to `/modlog set` / `clear` / `muted-role` /
    `dm-toggle`: pick a log slot with the dropdown, then assign it with the
    channel select below; the role select and DM button apply immediately.
    The embed refreshes in place after every action.
    """

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.abc.User,
        repo: ModerationRepository,
        settings: ModerationSettings,
    ) -> None:
        super().__init__(timeout=120)
        self.guild = guild
        self.requester = requester
        self.repo = repo
        self.settings = settings
        self.selected_event: str = "general"
        self._sync_dm_button()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        s = self.settings

        def fmt(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "*Not set*"

        embed = discord.Embed(
            title=f"{Emojis.mod}  Moderation Settings",
            description=f"Editing slot: **{_EVENT_LABELS[self.selected_event]}**",
            colour=_SUCCESS_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="General (fallback)", value=fmt(
            s.mod_log_channel_id), inline=True)
        embed.add_field(name="Kick", value=fmt(
            s.kick_log_channel_id), inline=True)
        embed.add_field(name="Ban", value=fmt(
            s.ban_log_channel_id), inline=True)
        embed.add_field(name="Timeout", value=fmt(
            s.timeout_log_channel_id), inline=True)
        embed.add_field(name="AutoMod", value=fmt(
            s.automod_log_channel_id), inline=True)
        embed.add_field(
            name="Muted role",
            value=f"<@&{s.muted_role_id}>" if s.muted_role_id else "*Not set*",
            inline=True,
        )
        embed.add_field(name="DM on punishment",
                        value="✅ On" if s.dm_on_punishment else "❌ Off", inline=True)
        embed.add_field(name="Case count", value=str(
            s.case_count), inline=True)
        return embed

    def _sync_dm_button(self) -> None:
        self.dm_toggle.label = "Turn DM Off" if self.settings.dm_on_punishment else "Turn DM On"
        self.dm_toggle.style = (
            discord.ButtonStyle.secondary if self.settings.dm_on_punishment else discord.ButtonStyle.success
        )

    @discord.ui.select(
        placeholder="Choose a log slot to edit…",
        options=[discord.SelectOption(label=label, value=key)
                 for key, label in _EVENT_LABELS.items()],
        row=0,
    )
    async def event_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.selected_event = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Set the channel for the selected slot…",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        row=1,
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await interaction.response.defer()
        channel_id = select.values[0].id
        if self.selected_event == "general":
            self.settings = await self.repo.set_mod_log_channel(self.guild.id, channel_id)
        else:
            self.settings = await self.repo.set_log_channel(
                self.guild.id, LogEvent(self.selected_event), channel_id)
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Set the muted role…", row=2)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        await interaction.response.defer()
        self.settings = await self.repo.set_muted_role(self.guild.id, select.values[0].id)
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Turn DM Off", style=discord.ButtonStyle.secondary, row=3)
    async def dm_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.settings = await self.repo.set_dm_on_punishment(self.guild.id, not self.settings.dm_on_punishment)
        self._sync_dm_button()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Clear Selected Slot", emoji=Emojis.close, style=discord.ButtonStyle.danger, row=3)
    async def clear_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        if self.selected_event == "general":
            self.settings = await self.repo.set_mod_log_channel(self.guild.id, None)
        else:
            self.settings = await self.repo.set_log_channel(self.guild.id, LogEvent(self.selected_event), None)
        await interaction.edit_original_response(embed=self.build_embed(), view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Moderation(commands.Cog, description="Kick, ban, and timeout members."):
    COG_EMOJI = Emojis.mod

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.repo = ModerationRepository(
            bot.pool)  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        if not _HUMANFRIENDLY_AVAILABLE:
            raise commands.ExtensionFailed(
                self.__class__.__module__,
                ImportError(
                    "Moderation cog requires 'humanfriendly' for /timeout duration "
                    "parsing (e.g. '10m', '1h', '3d'). Install it with "
                    "`uv add humanfriendly` and reload this cog."
                ),
            )

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
        view = _KickConfirmView(member, interaction.user,
                                reason=reason, repo=self.repo)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
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
        view = _BanConfirmView(
            member, interaction.user,
            reason=reason,
            delete_message_seconds=delete_message_days * 86_400,
            repo=self.repo,
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
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
                embed=_err(
                    "Pick a user from the autocomplete list, or pass a raw user ID."),
                ephemeral=True,
            )
            return

        # Ack now — fetch_ban() and unban() below are both Discord API round-trips,
        # and doing two of them before the first response risks the same
        # "10062 Unknown interaction" timeout as the kick/ban confirm views.
        await interaction.response.defer(ephemeral=True)

        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            await interaction.followup.send(
                embed=_err("That user isn't currently banned."), ephemeral=True)
            return

        try:
            await interaction.guild.unban(
                ban_entry.user, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_err(f"I don't have permission to unban **{ban_entry.user}**."), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send(
                embed=_err(f"Failed to unban **{ban_entry.user}**."), ephemeral=True)
            return

        result = _ok(
            f"**{ban_entry.user}** has been unbanned.\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=result, ephemeral=True)

    @unban.autocomplete("user")
    async def unban_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        assert interaction.guild is not None
        choices: list[app_commands.Choice[str]] = []
        async for entry in interaction.guild.bans(limit=1000):
            if current.lower() in str(entry.user).lower():
                choices.append(app_commands.Choice(
                    name=str(entry.user), value=str(entry.user.id)))
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
                embed=_err(
                    f"`{duration}` isn't a valid duration. Try something like `10m`, `1h`, or `3d`."),
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

        # Ack before the slow work — member.timeout() (Discord API) plus
        # _log_case() (DB + possibly another Discord send) before the first
        # response risks the same interaction-timeout bug as kick/ban.
        await interaction.response.defer(ephemeral=True)

        try:
            await member.timeout(
                delta, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_err(f"I don't have permission to timeout **{member}**."), ephemeral=True)
            return

        until = discord.utils.format_dt(
            datetime.now(timezone.utc) + delta, style="R")
        assert interaction.guild is not None
        await _log_case(
            interaction.guild, self.repo, LogEvent.TIMEOUT,
            target=member, moderator=interaction.user, reason=reason,
            extra=f"Until {until}",
        )
        result = _ok(
            f"**{member}** has been timed out until {until}.\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=result, ephemeral=True)

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

        await interaction.response.defer(ephemeral=True)

        try:
            await member.timeout(
                None, reason=f"By {interaction.user} — {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_err(f"I don't have permission to untimeout **{member}**."), ephemeral=True)
            return

        assert interaction.guild is not None
        await _log_case(
            interaction.guild, self.repo, LogEvent.TIMEOUT,
            target=member, moderator=interaction.user, reason=reason,
            extra="Timeout removed",
        )
        result = _ok(f"**{member}**'s timeout has been removed.")
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=result, ephemeral=True)

    # ── /modlog ───────────────────────────────────────────────────────────────
    # Admin-only settings panel for the moderation_settings row — Dyno/Arcane- style: a general fallback log channel, dedicated per-event channels,
    # a mute role, and a DM-on-punishment toggle.

    modlog = app_commands.Group(
        name="modlog",
        description="Configure moderation log channels and related settings.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @modlog.command(name="set", description="Set the log channel for an event (or the general fallback).")
    @app_commands.describe(event="Which event to log — 'general' is the fallback used when a specific one isn't set", channel="The channel to send log entries to")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_set(
        self,
        interaction: discord.Interaction,
        event: Literal["general", "kick", "ban", "timeout", "automod"],
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild is not None
        if event == "general":
            await self.repo.set_mod_log_channel(interaction.guild.id, channel.id)
        else:
            await self.repo.set_log_channel(interaction.guild.id, LogEvent(event), channel.id)
        await interaction.response.send_message(
            embed=_ok(f"**{event.title()}** log channel set to {channel.mention}."), ephemeral=True)

    @modlog.command(name="clear", description="Clear a previously-set log channel.")
    @app_commands.describe(event="Which event's channel to clear")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_clear(
        self,
        interaction: discord.Interaction,
        event: Literal["general", "kick", "ban", "timeout", "automod"],
    ) -> None:
        assert interaction.guild is not None
        if event == "general":
            await self.repo.set_mod_log_channel(interaction.guild.id, None)
        else:
            await self.repo.set_log_channel(interaction.guild.id, LogEvent(event), None)
        await interaction.response.send_message(
            embed=_ok(
                f"**{event.title()}** log channel cleared. Falls back to the general channel if set."),
            ephemeral=True,
        )

    @modlog.command(name="view", description="View the current moderation log settings.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_view(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        settings: ModerationSettings = await self.repo.get_or_create(interaction.guild.id)

        def _fmt(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "*Not set*"

        embed = discord.Embed(
            title=f"{Emojis.mod}  Moderation Settings",
            colour=_SUCCESS_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="General (fallback)", value=_fmt(
            settings.mod_log_channel_id), inline=True)
        embed.add_field(name="Kick", value=_fmt(
            settings.kick_log_channel_id), inline=True)
        embed.add_field(name="Ban", value=_fmt(
            settings.ban_log_channel_id), inline=True)
        embed.add_field(name="Timeout", value=_fmt(
            settings.timeout_log_channel_id), inline=True)
        embed.add_field(name="AutoMod", value=_fmt(
            settings.automod_log_channel_id), inline=True)
        embed.add_field(
            name="Muted role",
            value=f"<@&{settings.muted_role_id}>" if settings.muted_role_id else "*Not set*",
            inline=True,
        )
        embed.add_field(name="DM on punishment",
                        value="✅ On" if settings.dm_on_punishment else "❌ Off", inline=True)
        embed.add_field(name="Case count", value=str(
            settings.case_count), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @modlog.command(name="muted-role", description="Set (or clear) the role used for manual mutes.")
    @app_commands.describe(role="The mute role — omit to clear")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_muted_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        assert interaction.guild is not None
        await self.repo.set_muted_role(interaction.guild.id, role.id if role else None)
        await interaction.response.send_message(
            embed=_ok(
                f"Muted role set to {role.mention}." if role else "Muted role cleared."),
            ephemeral=True,
        )

    @modlog.command(name="dm-toggle", description="Toggle whether punished members are DMed.")
    @app_commands.describe(enabled="Whether to DM members when kicked/banned/timed out")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_dm_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.repo.set_dm_on_punishment(interaction.guild.id, enabled)
        await interaction.response.send_message(
            embed=_ok(f"DM-on-punishment is now **{'on' if enabled else 'off'}**."), ephemeral=True)

    @modlog.command(name="panel", description="Open an interactive panel to configure log channels, mute role, and DM toggle.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_panel(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        settings = await self.repo.get_or_create(interaction.guild.id)
        view = _ModLogPanelView(
            interaction.guild, interaction.user, self.repo, settings)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
