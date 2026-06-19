# Location: bot/cogs/Suggestion/__init__.py
"""
Suggestion cog — fully slash-command driven.

This module merges two earlier implementations of the same cog, keeping
whichever approach was best in each area:

Commands
────────
/suggest
    Opens SuggestModal (paragraph TextInput, 10–1000 chars). No arguments;
    the full suggestion is typed inside a Discord popup.

/suggestion setup
    Ephemeral SetupView wizard with three ChannelSelect menus
    (suggestion / approve / deny) + Save / Cancel buttons. Requires
    manage_guild. The embed live-updates after every channel pick so the
    moderator can see all three choices before saving.

/suggestion approve <serial>   [autocomplete — pending only]
/suggestion deny    <serial>   [autocomplete — pending only]
    Both require manage_messages. The slash interaction is NOT deferred
    before opening ReviewModal — Discord's API forbids `send_modal()` on an
    interaction that has already been acknowledged via `defer()`, so this
    flow opens the modal directly from the still-fresh command interaction.
    ReviewModal.on_submit defers its own (fresh) interaction before doing
    any DB/Discord work.

Persistent views
─────────────────
SuggestionActionView  custom_id: suggest:approve / suggest:deny
    Registered in `cog_load()` (the correct async hook for this) so the
    Approve / Deny buttons on suggestion messages survive bot restarts
    without manual re-syncing.

    At button-press time `interaction.message` IS the suggestion message,
    so it's passed straight into ReviewModal — no secondary DB lookup
    needed for the message object itself.

Unified review path  (_apply_review)
──────────────────────────────────────
Both the persistent-button flow and the slash-command flow converge on one
coroutine, `_apply_review`, which always receives the target message
explicitly (never relies on guessing it from `interaction.message`), so it
behaves identically regardless of call site:
  1. DB lookup + double-review guard.
  2. DB status update.
  3. Original message edit (embed + ReviewedView), tolerant of the message
     having been deleted or otherwise inaccessible.
  4. Result card posted to the approve/deny channel + Jump button.
  5. Ephemeral confirmation to the reviewer.

IMPORTANT — interaction acknowledgement:
  Every modal's on_submit() defers the interaction immediately, before any
  DB or Discord work happens. Modal and component interactions must be
  acknowledged within 3 seconds; once that window passes Discord tears
  down the interaction's webhook, and a later `interaction.followup.send()`
  fails with a 404 "Unknown Webhook".

Uses: bot.db.suggestion  (SuggestionRepository)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Colours, Emojis
from util.db.models import ReviewStatus

if TYPE_CHECKING:
    from util.db.models import Suggestion as SuggestionRow
    from util.db.models import SuggestionSettings


def _colour(name: str) -> discord.Colour:
    """Resolve a themed colour from util.constants, e.g. _colour('green')."""
    return discord.Colour(int(Colours.DISCORD_COLOURS[name], 16))


# ── Embed helpers ────────────────────────────────────────────────────────────

def _suggestion_embed(
    suggestion: str,
    serial: int,
    author: discord.User | discord.Member,
) -> discord.Embed:
    """Build the initial suggestion embed posted to the suggestion channel."""
    embed = discord.Embed(
        title=f"{Emojis.idea}  Suggestion #{serial}",
        description=f">>> {suggestion}",
        colour=_colour("blurple"),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.add_field(name=f"{Emojis.hourglass} Status", value="Pending review", inline=True)
    embed.add_field(name=f"{Emojis.hand_raised} Suggested by", value=author.mention, inline=True)
    embed.set_footer(text=f"Author ID: {author.id}")
    return embed


def _reviewed_embed(
    embed: discord.Embed,
    status: ReviewStatus,
    reviewer: discord.Member | discord.User,
    reason: str,
) -> discord.Embed:
    """Mutate *embed* in-place to reflect the review decision and return it."""
    approved = status == ReviewStatus.ACCEPTED
    status_emoji = Emojis.check_mark if approved else Emojis.cross_mark
    embed.colour = _colour("green") if approved else _colour("red")

    # Refresh the "Status" field in place instead of leaving it stuck on
    # "Pending review" forever and stacking a second, contradictory field.
    for index, field in enumerate(embed.fields):
        if field.name and "Status" in field.name:
            embed.set_field_at(
                index,
                name=f"{status_emoji} Status",
                value="Approved" if approved else "Denied",
                inline=True,
            )
            break

    embed.add_field(
        name=f"{status_emoji} {'Approved' if approved else 'Denied'} by {reviewer}",
        value=f">>> {reason}",
        inline=False,
    )
    return embed


# ── Core review executor ───────────────────────────────────────────────────────

async def _apply_review(
    interaction: discord.Interaction,
    message: discord.Message,
    status: ReviewStatus,
    reason: str,
) -> None:
    """Shared review logic for both the button path and the slash-command path.

    The caller **must** have deferred the interaction before calling this
    function — both call sites are ReviewModal.on_submit, which defers
    first thing.

    Parameters
    ----------
    interaction:
        The modal-submit interaction. Must already be deferred so multiple
        async operations can be performed before responding.
    message:
        The original suggestion Discord message (the one with the Approve /
        Deny buttons). Passed explicitly so this function behaves the same
        regardless of what interaction.message would have resolved to.
    status:
        ReviewStatus.ACCEPTED or ReviewStatus.REJECTED.
    reason:
        Moderator-supplied reason; already stripped and defaulted to
        "No reason provided." by the caller.
    """
    suggestion: SuggestionRow | None = await interaction.client.db.suggestion.get(message.id)  # type: ignore[attr-defined]
    if suggestion is None:
        await interaction.followup.send(
            f"{Emojis.warning} Could not find that suggestion in the database.",
            ephemeral=True,
        )
        return

    if suggestion.is_reviewed is not None:
        already = "approved" if suggestion.is_reviewed == ReviewStatus.ACCEPTED else "denied"
        await interaction.followup.send(
            f"{Emojis.no_entry} Suggestion **#{suggestion.serial_no}** was already **{already}**.",
            ephemeral=True,
        )
        return

    await interaction.client.db.suggestion.set_status(message.id, status)  # type: ignore[attr-defined]

    # Swap the action buttons for the disabled status button and refresh
    # the embed's colour + status field. Tolerant of HTTPException broadly
    # (deleted message, lost channel access, etc.) — DB is already updated
    # by this point, so we just skip the message edit on failure.
    base_embed = message.embeds[0] if message.embeds else discord.Embed()
    try:
        await message.edit(
            embed=_reviewed_embed(base_embed, status, interaction.user, reason),
            view=ReviewedView(status, str(interaction.user)),
        )
    except discord.HTTPException:
        await interaction.followup.send(
            f"{Emojis.warning} The original suggestion message could not be updated "
            "(it may have been deleted), but this review has still been recorded.",
            ephemeral=True,
        )

    # Post the result to the configured approve/deny channel, if any.
    settings: SuggestionSettings | None = await interaction.client.db.suggestion.get_settings(  # type: ignore[attr-defined]
        interaction.guild_id  # type: ignore[arg-type]
    )
    if settings and interaction.guild:
        approved = status == ReviewStatus.ACCEPTED
        target_id = settings.approve_channel_id if approved else settings.deny_channel_id
        if target_id:
            result_channel = interaction.guild.get_channel(target_id)
            if isinstance(result_channel, discord.TextChannel):
                emoji = Emojis.check_mark if approved else Emojis.cross_mark
                result_embed = discord.Embed(
                    title=f"{emoji}  Suggestion #{suggestion.serial_no} "
                    f"{'Approved' if approved else 'Denied'}",
                    description=f">>> {suggestion.suggestion}",
                    colour=_colour("green") if approved else _colour("red"),
                    timestamp=discord.utils.utcnow(),
                )
                result_embed.add_field(name="Reviewed by", value=interaction.user.mention, inline=True)
                result_embed.add_field(name="Suggested by", value=f"<@{suggestion.suggestor_id}>", inline=True)
                result_embed.add_field(name="Reason", value=reason, inline=False)
                jump_view = discord.ui.View()
                jump_view.add_item(
                    discord.ui.Button(
                        label="Jump to suggestion",
                        style=discord.ButtonStyle.link,
                        url=message.jump_url,
                    )
                )
                await result_channel.send(embed=result_embed, view=jump_view)

    verb = "approved" if status == ReviewStatus.ACCEPTED else "denied"
    emoji = Emojis.check_mark if status == ReviewStatus.ACCEPTED else Emojis.cross_mark
    await interaction.followup.send(
        f"{emoji} Suggestion **#{suggestion.serial_no}** has been {verb}.",
        ephemeral=True,
    )


# ── Persistent action view ─────────────────────────────────────────────────────

class SuggestionActionView(discord.ui.View):
    """Persistent Approve / Deny buttons attached to every suggestion message.

    Registered with `bot.add_view()` in `cog_load()` so the buttons survive
    bot restarts without re-syncing.

    At button-press time `interaction.message` IS the suggestion message,
    so it's passed directly into ReviewModal — no secondary lookup needed.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                f"{Emojis.no_entry} You need **Manage Messages** to review suggestions.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Approve", emoji=Emojis.check_mark,
        style=discord.ButtonStyle.success,
        custom_id="suggest:approve",
    )
    async def approve_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ReviewModal(action=ReviewStatus.ACCEPTED, message=interaction.message)  # type: ignore[arg-type]
        )

    @discord.ui.button(
        label="Deny", emoji=Emojis.cross_mark,
        style=discord.ButtonStyle.danger,
        custom_id="suggest:deny",
    )
    async def deny_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ReviewModal(action=ReviewStatus.REJECTED, message=interaction.message)  # type: ignore[arg-type]
        )


class ReviewedView(discord.ui.View):
    """Single disabled status button shown after a suggestion is reviewed."""

    def __init__(self, status: ReviewStatus, reviewer_name: str) -> None:
        super().__init__(timeout=None)
        verb = "Approved" if status == ReviewStatus.ACCEPTED else "Denied"
        style = discord.ButtonStyle.success if status == ReviewStatus.ACCEPTED else discord.ButtonStyle.danger
        emoji = Emojis.check_mark if status == ReviewStatus.ACCEPTED else Emojis.cross_mark
        self.add_item(
            discord.ui.Button(
                label=f"{verb} by {reviewer_name}",
                style=style,
                emoji=emoji,
                disabled=True,
                custom_id="suggest:reviewed",
            )
        )


# ── Modals ────────────────────────────────────────────────────────────────────

class SuggestModal(discord.ui.Modal, title="Submit a Suggestion"):
    """Paragraph text-input popup for submitting a suggestion."""

    suggestion: discord.ui.TextInput = discord.ui.TextInput(
        label="Your suggestion",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your idea in detail…",
        min_length=10,
        max_length=1000,
        required=True,
    )

    def __init__(self, cog: "Suggestion") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Ack immediately — channel auto-creation + DB writes below can
        # easily take longer than Discord's 3-second response window.
        await interaction.response.defer(ephemeral=True)
        await self._cog._post_suggestion(interaction, str(self.suggestion.value).strip())


class ReviewModal(discord.ui.Modal):
    """Optional reason input shown before approving or denying a suggestion.

    Used by both the persistent-button flow and the slash-command flow.
    The suggestion message is always passed in explicitly so the correct
    target is available at on_submit time.
    """

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your decision…",
        required=False,
        max_length=500,
    )

    def __init__(self, *, action: ReviewStatus, message: discord.Message) -> None:
        verb = "Approve" if action == ReviewStatus.ACCEPTED else "Deny"
        super().__init__(title=f"{verb} Suggestion")
        self.action = action
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or "No reason provided."
        await interaction.response.defer(ephemeral=True)
        await _apply_review(interaction, self.message, self.action, reason)


# ── Setup wizard view ───────────────────────────────────────────────────────────

class SetupView(discord.ui.View):
    """Ephemeral three-step channel configuration wizard.

    Layout
    ──────
    Row 0  ChannelSelect — suggestion channel
    Row 1  ChannelSelect — approved-suggestions channel
    Row 2  ChannelSelect — denied-suggestions channel
    Row 3  [Save]  [Cancel]

    The embed live-updates on every select change so the moderator can see
    all three current choices before pressing Save.
    """

    def __init__(self, owner_id: int, existing: "SuggestionSettings | None") -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.suggestion_channel: int | None = existing.channel_id if existing else None
        self.approve_channel: int | None = existing.approve_channel_id if existing else None
        self.deny_channel: int | None = existing.deny_channel_id if existing else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"{Emojis.no_entry} This panel is not for you.", ephemeral=True
            )
            return False
        return True

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{Emojis.gear}  Suggestion System Setup",
            description=(
                "Select a text channel for each role below, then press **Save**.\n"
                "Current settings are reflected below."
            ),
            colour=_colour("blurple"),
        )
        embed.add_field(
            name=f"{Emojis.envelope} Suggestion channel",
            value=f"<#{self.suggestion_channel}>" if self.suggestion_channel else "*Not set*",
        )
        embed.add_field(
            name=f"{Emojis.check_mark} Approve channel",
            value=f"<#{self.approve_channel}>" if self.approve_channel else "*Not set*",
        )
        embed.add_field(
            name=f"{Emojis.cross_mark} Deny channel",
            value=f"<#{self.deny_channel}>" if self.deny_channel else "*Not set*",
        )
        return embed

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder=f"{Emojis.envelope} Where should suggestions be posted?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=0,
    )
    async def suggestion_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if select.values:
            self.suggestion_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder=f"{Emojis.check_mark} Where should approved suggestions appear?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=1,
    )
    async def approve_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if select.values:
            self.approve_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder=f"{Emojis.cross_mark} Where should denied suggestions appear?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=2,
    )
    async def deny_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if select.values:
            self.deny_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Save", emoji=Emojis.check_mark, style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        missing = [
            name for name, val in [
                ("Suggestion", self.suggestion_channel),
                ("Approve", self.approve_channel),
                ("Deny", self.deny_channel),
            ] if val is None
        ]
        if missing:
            await interaction.response.send_message(
                f"{Emojis.warning} Please select channels for: **{', '.join(missing)}**",
                ephemeral=True,
            )
            return

        await interaction.client.db.suggestion.create_settings(  # type: ignore[attr-defined]
            guild_id=interaction.guild_id,
            channel_id=self.suggestion_channel,
            approve_channel_id=self.approve_channel,
            deny_channel_id=self.deny_channel,
        )

        confirm_embed = discord.Embed(
            title=f"{Emojis.check_mark}  Suggestion system configured",
            colour=_colour("green"),
        )
        confirm_embed.add_field(name=f"{Emojis.envelope} Suggestion channel", value=f"<#{self.suggestion_channel}>")
        confirm_embed.add_field(name=f"{Emojis.check_mark} Approve channel", value=f"<#{self.approve_channel}>")
        confirm_embed.add_field(name=f"{Emojis.cross_mark} Deny channel", value=f"<#{self.deny_channel}>")
        self.stop()
        self.clear_items()
        await interaction.response.edit_message(embed=confirm_embed, view=self)

    @discord.ui.button(label="Cancel", emoji=Emojis.cross_mark, style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=f"{Emojis.cross_mark} Setup cancelled.", embed=None, view=None
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class Suggestion(commands.Cog, description="Submit and manage server suggestions."):
    COG_EMOJI = Emojis.idea

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Register the persistent SuggestionActionView on every cog load.

        Using the async `cog_load()` hook (rather than doing this in
        `__init__`) ensures buttons on old suggestion messages keep working
        after bot restarts without manual re-syncing, and keeps this setup
        step alongside the rest of the cog's async lifecycle.
        """
        self.bot.add_view(SuggestionActionView())

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"{Emojis.no_entry} You need the {perms} permission(s) to use this command."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── /suggest ──────────────────────────────────────────────────────────────

    @app_commands.command(name="suggest", description="Submit a suggestion to the server.")
    async def suggest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SuggestModal(self))

    async def _post_suggestion(self, interaction: discord.Interaction, text: str) -> None:
        """Create the DB record and post the suggestion embed.

        Auto-creates the three suggestion channels if no settings row exists
        for this guild yet, making the system self-configuring on first use.
        """
        assert interaction.guild is not None

        settings: "SuggestionSettings | None" = await self.bot.db.suggestion.get_settings(  # type: ignore[attr-defined]
            interaction.guild.id
        )

        if not settings or not settings.channel_id:
            s_ch = await interaction.guild.create_text_channel(f"{Emojis.envelope}｜suggestions")
            a_ch = await interaction.guild.create_text_channel(f"{Emojis.check_mark}｜approved-suggestions")
            d_ch = await interaction.guild.create_text_channel(f"{Emojis.cross_mark}｜denied-suggestions")
            settings = await self.bot.db.suggestion.create_settings(  # type: ignore[attr-defined]
                guild_id=interaction.guild.id,
                channel_id=s_ch.id,
                approve_channel_id=a_ch.id,
                deny_channel_id=d_ch.id,
            )

        channel = interaction.guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"{Emojis.warning} The suggestion channel is no longer accessible. "
                "Please run `/suggestion setup` to reconfigure it.",
                ephemeral=True,
            )
            return

        serial = await self.bot.db.suggestion.next_serial(interaction.guild.id)  # type: ignore[attr-defined]
        embed = _suggestion_embed(text, serial, interaction.user)
        message = await channel.send(embed=embed, view=SuggestionActionView())

        await self.bot.db.suggestion.create(  # type: ignore[attr-defined]
            message_id=message.id,
            guild_id=interaction.guild.id,
            suggestor_id=interaction.user.id,
            serial_no=serial,
            suggestion=text,
        )

        await interaction.followup.send(
            f"{Emojis.check_mark} Your suggestion has been submitted as **#{serial}** in {channel.mention}.",
            ephemeral=True,
        )

    # ── /suggestion group ─────────────────────────────────────────────────────

    suggestion_grp = app_commands.Group(
        name="suggestion",
        description="Manage the suggestion system.",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @suggestion_grp.command(
        name="setup",
        description="Configure suggestion channels with an interactive wizard.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def suggestion_setup(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        existing: "SuggestionSettings | None" = await self.bot.db.suggestion.get_settings(  # type: ignore[attr-defined]
            interaction.guild.id
        )
        view = SetupView(owner_id=interaction.user.id, existing=existing)
        await interaction.response.send_message(embed=view._embed(), view=view, ephemeral=True)

    async def _pending_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Return up to 25 pending suggestions matching *current*."""
        if not interaction.guild:
            return []
        pending = await self.bot.db.suggestion.get_pending(interaction.guild.id)  # type: ignore[attr-defined]
        return [
            app_commands.Choice(name=f"#{s.serial_no} — {s.suggestion[:60]}", value=s.serial_no)
            for s in pending
            if not current or current in str(s.serial_no) or current.lower() in s.suggestion.lower()
        ][:25]

    # ── /suggestion approve ───────────────────────────────────────────────────

    @suggestion_grp.command(name="approve", description="Approve a pending suggestion.")
    @app_commands.describe(serial="Suggestion number — autocomplete shows pending ones only.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_approve(self, interaction: discord.Interaction, serial: int) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.ACCEPTED)

    # ── /suggestion deny ──────────────────────────────────────────────────────

    @suggestion_grp.command(name="deny", description="Deny a pending suggestion.")
    @app_commands.describe(serial="Suggestion number — autocomplete shows pending ones only.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_deny(self, interaction: discord.Interaction, serial: int) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.REJECTED)

    async def _slash_review(
        self, interaction: discord.Interaction, serial: int, status: ReviewStatus
    ) -> None:
        """Slash-command review path for both approve and deny.

        Deliberately does NOT defer the interaction: ReviewModal is opened
        directly from this still-fresh command interaction, since Discord's
        API forbids opening a modal on an interaction that has already been
        acknowledged via `defer()`. All validation below uses
        `interaction.response.send_message()` (not `.followup`) precisely
        because the interaction is not yet deferred.
        """
        assert interaction.guild is not None

        suggestion: "SuggestionRow | None" = await self.bot.db.suggestion.get_by_serial(  # type: ignore[attr-defined]
            interaction.guild.id, serial
        )
        if suggestion is None:
            await interaction.response.send_message(
                f"{Emojis.no_entry} Suggestion **#{serial}** not found.", ephemeral=True
            )
            return
        if suggestion.is_reviewed is not None:
            already = "approved" if suggestion.is_reviewed == ReviewStatus.ACCEPTED else "denied"
            await interaction.response.send_message(
                f"{Emojis.no_entry} Suggestion **#{serial}** was already **{already}**.",
                ephemeral=True,
            )
            return

        settings: "SuggestionSettings | None" = await self.bot.db.suggestion.get_settings(  # type: ignore[attr-defined]
            interaction.guild.id
        )
        if not settings or not settings.channel_id:
            await interaction.response.send_message(
                f"{Emojis.warning} Suggestion channel is not configured. Run `/suggestion setup` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                f"{Emojis.warning} Suggestion channel is invalid or no longer accessible.",
                ephemeral=True,
            )
            return

        try:
            message = await channel.fetch_message(suggestion.message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                f"{Emojis.warning} The original suggestion message has been deleted.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"{Emojis.warning} Could not fetch the suggestion message: `{exc}`", ephemeral=True
            )
            return

        await interaction.response.send_modal(ReviewModal(action=status, message=message))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestion(bot))