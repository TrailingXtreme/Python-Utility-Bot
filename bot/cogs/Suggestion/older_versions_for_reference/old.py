# Location: bot/cogs/Suggestion/__init__.py
"""
Suggestion cog — fully slash-command driven.

Commands
────────
/suggest
    Opens SuggestModal (paragraph TextInput, 10–1000 chars).
    No arguments; the full suggestion is typed inside a Discord popup.

/suggestion setup
    Ephemeral SetupView wizard with three ChannelSelect menus
    (suggestion / approve / deny) and Save / Cancel buttons.
    Requires manage_guild.

/suggestion approve <serial>   [autocomplete — pending only]
/suggestion deny    <serial>   [autocomplete — pending only]
    Both require manage_messages.
    Defer → validate → fetch original message → show _SlashReviewView
    (single "Add Reason & Confirm" button) → ReviewModal opens →
    on_submit calls _apply_review().  This chain avoids the Discord
    restriction that prevents opening a modal after a deferred response.

Persistent views
────────────────
SuggestionActionView  custom_id: suggest:approve / suggest:deny
    Registered unconditionally in cog_load() via bot.add_view() so the
    Approve / Deny buttons on suggestion messages survive bot restarts.

    At button-press time, interaction.message IS the suggestion message.
    It is passed directly into ReviewModal so no secondary DB lookup for
    the message object is required, and there are zero sentinel hacks.

Unified review path  (_apply_review)
──────────────────────────────────────
Both the persistent-button flow and the slash-command flow converge on
a single free coroutine _apply_review(interaction, message, status, reason)
that accepts the suggestion message explicitly rather than relying on
interaction.message.  This keeps both paths readable and identical in
their side-effects:
  1. DB lookup + double-review guard.
  2. DB status update.
  3. Original message edit (embed + ReviewedView).
  4. Result card to the approve/deny channel + Jump button.
  5. Ephemeral confirmation to the reviewer.

Uses: bot.db.suggestion  (SuggestionRepository)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from util.db.models import ReviewStatus

if TYPE_CHECKING:
    from util.db.models import Suggestion as SuggestionRow
    from util.db.models import SuggestionSettings


# ── Embed helpers ──────────────────────────────────────────────────────────────

def _suggestion_embed(
    suggestion: str,
    serial: int,
    author: discord.User | discord.Member,
) -> discord.Embed:
    """Build the initial suggestion embed posted to the suggestion channel."""
    embed = discord.Embed(
        title=f"💡 Suggestion #{serial}",
        description=suggestion,
        colour=discord.Colour.blurple(),
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.set_footer(text=f"Author ID: {author.id}")
    return embed


def _reviewed_embed(
    embed: discord.Embed,
    status: ReviewStatus,
    reviewer: discord.Member | discord.User,
    reason: str,
) -> discord.Embed:
    """Mutate *embed* in-place to reflect the review decision and return it."""
    embed.colour = (
        discord.Colour.green()
        if status == ReviewStatus.ACCEPTED
        else discord.Colour.red()
    )
    verb = "✅ Approved" if status == ReviewStatus.ACCEPTED else "❌ Denied"
    embed.add_field(
        name=f"{verb} by {reviewer}",
        value=reason,
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
    function (either via interaction.response.defer() or via send_modal, which
    in the button path keeps the original message interaction alive for followup
    after the modal submit defers).

    Parameters
    ----------
    interaction:
        The modal-submit interaction.  Must already be deferred so that
        multiple async operations can be performed before responding.
    message:
        The original suggestion Discord message (the one with the Approve /
        Deny buttons).  Passed explicitly so this function is safe regardless
        of what interaction.message resolves to at the call site.
    status:
        ReviewStatus.ACCEPTED or ReviewStatus.REJECTED.
    reason:
        Moderator-supplied reason; already stripped and defaulted to
        "No reason provided." by the caller.
    """
    # 1. Retrieve the DB record.
    suggestion: SuggestionRow | None = await interaction.client.db.suggestion.get(message.id)  # type: ignore[attr-defined]
    if suggestion is None:
        await interaction.followup.send(
            "⚠️ Could not find that suggestion in the database.", ephemeral=True
        )
        return

    # 2. Guard against double-review.
    if suggestion.is_reviewed is not None:
        already = (
            "approved"
            if suggestion.is_reviewed == ReviewStatus.ACCEPTED
            else "denied"
        )
        await interaction.followup.send(
            f":no_entry: Suggestion **#{suggestion.serial_no}** was already **{already}**.",
            ephemeral=True,
        )
        return

    # 3. Persist the new status.
    await interaction.client.db.suggestion.set_status(message.id, status)  # type: ignore[attr-defined]

    # 4. Update the original suggestion message.
    base_embed = message.embeds[0] if message.embeds else discord.Embed()
    try:
        await message.edit(
            embed=_reviewed_embed(base_embed, status, interaction.user, reason),
            view=ReviewedView(status, str(interaction.user)),
        )
    except discord.HTTPException:
        # Original message deleted between review being triggered and now.
        # Continue — DB is already updated; just skip the message edit.
        pass

    # 5. Post result card to the approve / deny channel.
    settings: SuggestionSettings | None = await interaction.client.db.suggestion.get_settings(  # type: ignore[attr-defined]
        interaction.guild_id  # type: ignore[arg-type]
    )
    if settings and interaction.guild:
        target_id = (
            settings.approve_channel_id
            if status == ReviewStatus.ACCEPTED
            else settings.deny_channel_id
        )
        if target_id:
            result_channel = interaction.guild.get_channel(target_id)
            if isinstance(result_channel, discord.TextChannel):
                verb_title = (
                    "approved" if status == ReviewStatus.ACCEPTED else "denied"
                )
                result_embed = discord.Embed(
                    title=f"💡 Suggestion #{suggestion.serial_no} {verb_title}",
                    description=suggestion.suggestion,
                    colour=(
                        discord.Colour.green()
                        if status == ReviewStatus.ACCEPTED
                        else discord.Colour.red()
                    ),
                )
                result_embed.add_field(name="Reason", value=reason, inline=False)
                result_embed.add_field(
                    name="Reviewed by", value=interaction.user.mention, inline=True
                )
                jump_view = discord.ui.View()
                jump_view.add_item(
                    discord.ui.Button(
                        label="Jump to suggestion",
                        style=discord.ButtonStyle.link,
                        url=message.jump_url,
                    )
                )
                await result_channel.send(embed=result_embed, view=jump_view)

    # 6. Confirm to the reviewer.
    emoji = "✅" if status == ReviewStatus.ACCEPTED else "❌"
    verb_confirm = "approved" if status == ReviewStatus.ACCEPTED else "denied"
    await interaction.followup.send(
        f"{emoji} Suggestion **#{suggestion.serial_no}** has been {verb_confirm}.",
        ephemeral=True,
    )


# ── Persistent action view ─────────────────────────────────────────────────────

class SuggestionActionView(discord.ui.View):
    """Persistent Approve / Deny buttons attached to every suggestion message.

    Registered with ``bot.add_view()`` in ``cog_load()`` so buttons survive
    bot restarts without re-syncing.

    At button-press time ``interaction.message`` IS the suggestion message.
    It is passed directly into ReviewModal — avoiding any secondary lookup.
    The ReviewModal on_submit defers the modal interaction, then calls
    _apply_review() with the stored message reference.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                ":no_entry: You need **Manage Messages** to review suggestions.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Approve",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="suggest:approve",
    )
    async def approve_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        # interaction.message is the suggestion message at button-press time.
        await interaction.response.send_modal(
            ReviewModal(
                action=ReviewStatus.ACCEPTED,
                message=interaction.message,  # type: ignore[arg-type]
            )
        )

    @discord.ui.button(
        label="Deny",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="suggest:deny",
    )
    async def deny_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ReviewModal(
                action=ReviewStatus.REJECTED,
                message=interaction.message,  # type: ignore[arg-type]
            )
        )


class ReviewedView(discord.ui.View):
    """Single disabled status button shown after a suggestion is reviewed.

    All buttons are permanently disabled — no timeout or registration needed.
    """

    def __init__(self, status: ReviewStatus, reviewer_name: str) -> None:
        super().__init__(timeout=None)
        verb = "Approved" if status == ReviewStatus.ACCEPTED else "Denied"
        emoji = "✅" if status == ReviewStatus.ACCEPTED else "❌"
        self.add_item(
            discord.ui.Button(
                label=f"{verb} by {reviewer_name}",
                emoji=emoji,
                style=(
                    discord.ButtonStyle.success
                    if status == ReviewStatus.ACCEPTED
                    else discord.ButtonStyle.danger
                ),
                disabled=True,
                custom_id="suggest:reviewed",
            )
        )


# ── Modals ─────────────────────────────────────────────────────────────────────

class SuggestModal(discord.ui.Modal, title="Submit a Suggestion"):
    """Paragraph text-input popup for submitting a suggestion.

    Triggered by /suggest.  The cog reference is stored so _post_suggestion()
    can be called after the user submits without needing a separate slash step.
    """

    suggestion: discord.ui.TextInput = discord.ui.TextInput(
        label="Your suggestion",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your idea in detail…",
        min_length=10,
        max_length=1000,
    )

    def __init__(self, cog: Suggestion) -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer immediately — _post_suggestion performs multiple async operations.
        await interaction.response.defer(ephemeral=True)
        await self._cog._post_suggestion(interaction, str(self.suggestion.value).strip())


class ReviewModal(discord.ui.Modal):
    """Optional reason input shown before approving or denying a suggestion.

    Used by both the persistent-button flow (SuggestionActionView) and the
    slash-command flow (_SlashReviewView).  The suggestion message is passed
    explicitly so the correct target is always available at on_submit time,
    regardless of what ``interaction.message`` resolves to.

    Parameters
    ----------
    action:
        ReviewStatus.ACCEPTED or ReviewStatus.REJECTED.
    message:
        The original suggestion Discord message.
    """

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your decision…",
        required=False,
        max_length=500,
    )

    def __init__(
        self,
        *,
        action: ReviewStatus,
        message: discord.Message,
    ) -> None:
        verb = "Approve" if action == ReviewStatus.ACCEPTED else "Deny"
        super().__init__(title=f"{verb} Suggestion")
        self.action = action
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or "No reason provided."
        # Defer the fresh modal-submit interaction before running async work.
        await interaction.response.defer(ephemeral=True)
        await _apply_review(interaction, self.message, self.action, reason)


# ── Slash-command review confirmation view ─────────────────────────────────────

class _SlashReviewView(discord.ui.View):
    """Ephemeral followup view shown after /suggestion approve|deny.

    Because /suggestion approve|deny defers the interaction immediately,
    a modal cannot be opened from that deferred response.  This view works
    around that restriction:

      1. The command defers and posts this ephemeral view as a followup.
      2. The user clicks "Add Reason & Confirm" — that is a fresh button
         interaction, so a modal CAN be opened from it.
      3. ReviewModal on_submit defers its own interaction and calls
         _apply_review() with the stored suggestion_message.

    Layout (row 0)
    ──────────────
      [📝 Add Reason & Confirm]  [✖ Cancel]

    Parameters
    ----------
    status:
        The review decision to apply.
    suggestion_message:
        The original Discord message from the suggestion channel.
    suggestion:
        The DB model row (for the embed preview).
    requester_id:
        Only the moderator who triggered the slash command may use the buttons.
    """

    def __init__(
        self,
        *,
        status: ReviewStatus,
        suggestion_message: discord.Message,
        suggestion: SuggestionRow,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=60)
        self.status = status
        self.suggestion_message = suggestion_message
        self.suggestion = suggestion
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                ":no_entry: This panel is not for you.", ephemeral=True
            )
            return False
        return True

    def _embed(self) -> discord.Embed:
        emoji = "✅" if self.status == ReviewStatus.ACCEPTED else "❌"
        verb = "Approve" if self.status == ReviewStatus.ACCEPTED else "Deny"
        colour = (
            discord.Colour.green()
            if self.status == ReviewStatus.ACCEPTED
            else discord.Colour.red()
        )
        embed = discord.Embed(
            title=f"{emoji} {verb} Suggestion #{self.suggestion.serial_no}?",
            description=(
                self.suggestion.suggestion[:500]
                + ("…" if len(self.suggestion.suggestion) > 500 else "")
            ),
            colour=colour,
        )
        embed.set_footer(
            text='Click "Add Reason & Confirm" to provide a reason and apply the review.'
        )
        return embed

    @discord.ui.button(
        label="Add Reason & Confirm",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def confirm_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.send_modal(
            ReviewModal(action=self.status, message=self.suggestion_message)
        )

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def cancel_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Review cancelled.", embed=None, view=None
        )

    async def on_timeout(self) -> None:
        self.stop()


# ── Setup wizard view ──────────────────────────────────────────────────────────

class SetupView(discord.ui.View):
    """Ephemeral three-step channel configuration wizard.

    Layout
    ──────
    Row 0  ChannelSelect — suggestion channel
    Row 1  ChannelSelect — approved-suggestions channel
    Row 2  ChannelSelect — denied-suggestions channel
    Row 3  [✅ Save]  [✖ Cancel]

    The embed is updated live on every select change so the moderator can
    see all three choices before pressing Save.  Existing settings are
    reflected in the embed on first load so the current state is always
    visible.

    Parameters
    ----------
    owner_id:
        Only the moderator who invoked /suggestion setup may interact.
    existing:
        Current SuggestionSettings row if one exists, or None.
    """

    def __init__(
        self,
        owner_id: int,
        existing: SuggestionSettings | None,
    ) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.suggestion_channel: int | None = (
            existing.channel_id if existing else None
        )
        self.approve_channel: int | None = (
            existing.approve_channel_id if existing else None
        )
        self.deny_channel: int | None = (
            existing.deny_channel_id if existing else None
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                ":no_entry: This panel is not for you.", ephemeral=True
            )
            return False
        return True

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Suggestion System Setup",
            description=(
                "Select a text channel for each role below, then press **Save**.\n"
                "Current settings are reflected below."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="📨 Suggestion channel",
            value=(
                f"<#{self.suggestion_channel}>"
                if self.suggestion_channel
                else "*Not set*"
            ),
        )
        embed.add_field(
            name="✅ Approve channel",
            value=(
                f"<#{self.approve_channel}>"
                if self.approve_channel
                else "*Not set*"
            ),
        )
        embed.add_field(
            name="❌ Deny channel",
            value=(
                f"<#{self.deny_channel}>"
                if self.deny_channel
                else "*Not set*"
            ),
        )
        return embed

    # ── Row 0: suggestion channel ─────────────────────────────────────────────

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="📨 Where should suggestions be posted?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=0,
    )
    async def suggestion_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
    ) -> None:
        if select.values:
            self.suggestion_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # ── Row 1: approve channel ────────────────────────────────────────────────

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="✅ Where should approved suggestions appear?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=1,
    )
    async def approve_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
    ) -> None:
        if select.values:
            self.approve_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # ── Row 2: deny channel ───────────────────────────────────────────────────

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="❌ Where should denied suggestions appear?",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=2,
    )
    async def deny_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
    ) -> None:
        if select.values:
            self.deny_channel = select.values[0].id
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # ── Row 3: action buttons ─────────────────────────────────────────────────

    @discord.ui.button(
        label="Save",
        emoji="✅",
        style=discord.ButtonStyle.success,
        row=3,
    )
    async def save(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        missing = [
            name
            for name, val in [
                ("Suggestion", self.suggestion_channel),
                ("Approve", self.approve_channel),
                ("Deny", self.deny_channel),
            ]
            if val is None
        ]
        if missing:
            await interaction.response.send_message(
                f"⚠️ Please select channels for: **{', '.join(missing)}**",
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
            title="✅ Suggestion system configured",
            colour=discord.Colour.green(),
        )
        confirm_embed.add_field(
            name="📨 Suggestion channel",
            value=f"<#{self.suggestion_channel}>",
        )
        confirm_embed.add_field(
            name="✅ Approve channel",
            value=f"<#{self.approve_channel}>",
        )
        confirm_embed.add_field(
            name="❌ Deny channel",
            value=f"<#{self.deny_channel}>",
        )
        self.stop()
        self.clear_items()
        await interaction.response.edit_message(embed=confirm_embed, view=self)

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Setup cancelled.", embed=None, view=None
        )


# ── Cog ────────────────────────────────────────────────────────────────────────

class Suggestion(commands.Cog, description="Submit and manage server suggestions."):
    COG_EMOJI = "💡"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Register the persistent SuggestionActionView on every cog load.

        This ensures Approve / Deny buttons on old suggestion messages are
        correctly routed after bot restarts without manual re-syncing.
        """
        self.bot.add_view(SuggestionActionView())

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

    # ── /suggest ──────────────────────────────────────────────────────────────

    @app_commands.command(name="suggest", description="Submit a suggestion to the server.")
    async def suggest(self, interaction: discord.Interaction) -> None:
        """Open the suggestion modal — no arguments required."""
        await interaction.response.send_modal(SuggestModal(self))

    async def _post_suggestion(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """Create the DB record and post the suggestion embed.

        Called from SuggestModal.on_submit after the interaction is deferred,
        so interaction.followup must be used for all responses here.

        Auto-creates the three suggestion channels if no settings row exists
        for this guild, making the system self-configuring on first use.
        """
        assert interaction.guild is not None

        settings: SuggestionSettings | None = (
            await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        )

        if not settings or not settings.channel_id:
            # First use — create the three channels automatically.
            s_ch = await interaction.guild.create_text_channel("📨｜suggestions")
            a_ch = await interaction.guild.create_text_channel("✅｜approved-suggestions")
            d_ch = await interaction.guild.create_text_channel("❌｜denied-suggestions")
            settings = await self.bot.db.suggestion.create_settings(  # type: ignore[attr-defined]
                guild_id=interaction.guild.id,
                channel_id=s_ch.id,
                approve_channel_id=a_ch.id,
                deny_channel_id=d_ch.id,
            )

        channel = interaction.guild.get_channel(settings.channel_id)  # type: ignore[arg-type]
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                ":warning: The suggestion channel is no longer accessible. "
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
            f"✅ Your suggestion has been submitted as **#{serial}** in {channel.mention}.",
            ephemeral=True,
        )

    # ── /suggestion group ─────────────────────────────────────────────────────

    suggestion_grp = app_commands.Group(
        name="suggestion",
        description="Manage the suggestion system.",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    # ── /suggestion setup ─────────────────────────────────────────────────────

    @suggestion_grp.command(
        name="setup",
        description="Configure suggestion channels with an interactive wizard.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def suggestion_setup(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        existing: SuggestionSettings | None = (
            await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        )
        view = SetupView(owner_id=interaction.user.id, existing=existing)
        await interaction.response.send_message(
            embed=view._embed(), view=view, ephemeral=True
        )

    # ── Pending suggestions autocomplete ──────────────────────────────────────

    async def _pending_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return up to 25 pending suggestions matching *current*."""
        if not interaction.guild:
            return []
        pending = await self.bot.db.suggestion.get_pending(interaction.guild.id)  # type: ignore[attr-defined]
        return [
            app_commands.Choice(
                name=f"#{s.serial_no} — {s.suggestion[:60]}",
                value=s.serial_no,
            )
            for s in pending
            if not current
            or current in str(s.serial_no)
            or current.lower() in s.suggestion.lower()
        ][:25]

    # ── /suggestion approve ───────────────────────────────────────────────────

    @suggestion_grp.command(name="approve", description="Approve a pending suggestion.")
    @app_commands.describe(serial="Suggestion number — autocomplete shows pending ones only.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_approve(
        self, interaction: discord.Interaction, serial: int
    ) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.ACCEPTED)

    # ── /suggestion deny ──────────────────────────────────────────────────────

    @suggestion_grp.command(name="deny", description="Deny a pending suggestion.")
    @app_commands.describe(serial="Suggestion number — autocomplete shows pending ones only.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_deny(
        self, interaction: discord.Interaction, serial: int
    ) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.REJECTED)

    # ── Shared slash review path ──────────────────────────────────────────────

    async def _slash_review(
        self,
        interaction: discord.Interaction,
        serial: int,
        status: ReviewStatus,
    ) -> None:
        """Slash-command review path for both approve and deny.

        Flow
        ────
        1. Defer immediately (ephemeral) — allows multiple async ops below.
        2. Validate the suggestion exists and is still pending.
        3. Fetch the original Discord message from the suggestion channel.
        4. Post an ephemeral _SlashReviewView as a followup.
        5. User clicks "Add Reason & Confirm" → ReviewModal opens (fresh
           button interaction, so send_modal() is legal here).
        6. ReviewModal.on_submit → _apply_review() → done.
        """
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        # Validate suggestion.
        suggestion: SuggestionRow | None = (
            await self.bot.db.suggestion.get_by_serial(interaction.guild.id, serial)  # type: ignore[attr-defined]
        )
        if suggestion is None:
            await interaction.followup.send(
                f":no_entry: Suggestion **#{serial}** not found.", ephemeral=True
            )
            return
        if suggestion.is_reviewed is not None:
            already = (
                "approved"
                if suggestion.is_reviewed == ReviewStatus.ACCEPTED
                else "denied"
            )
            await interaction.followup.send(
                f":no_entry: Suggestion **#{serial}** was already **{already}**.",
                ephemeral=True,
            )
            return

        # Locate the suggestion channel.
        settings: SuggestionSettings | None = (
            await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        )
        if not settings or not settings.channel_id:
            await interaction.followup.send(
                ":warning: Suggestion channel is not configured. "
                "Run `/suggestion setup` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                ":warning: Suggestion channel is invalid or no longer accessible.",
                ephemeral=True,
            )
            return

        # Fetch the original Discord message.
        try:
            message = await channel.fetch_message(suggestion.message_id)
        except discord.NotFound:
            await interaction.followup.send(
                ":warning: The original suggestion message has been deleted.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f":warning: Could not fetch the suggestion message: `{exc}`",
                ephemeral=True,
            )
            return

        # Present the confirmation view.
        view = _SlashReviewView(
            status=status,
            suggestion_message=message,
            suggestion=suggestion,
            requester_id=interaction.user.id,
        )
        await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestion(bot))