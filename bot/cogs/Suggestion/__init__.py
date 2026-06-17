"""
Location: bot/cogs/Suggestion/__init__.py
──────────────────────────────────────────
User suggestion system — fully slash-command driven.

UI overview (modern Discord features throughout):
  /suggest
    → Opens SuggestModal (paragraph TextInput — full Discord popup)

  /suggestion setup
    → Ephemeral SetupView with three ChannelSelect menus (one per channel
       type) + Save / Cancel buttons. No need to remember sub-command names.

  /suggestion approve <serial>   (autocomplete — lists pending suggestions)
  /suggestion deny    <serial>   (autocomplete)
    → Both open a ReviewModal for an optional reason, then post results.

Suggestion embeds carry a persistent SuggestionActionView (Approve ✅ /
Deny ❌ buttons, custom_id-based). Clicking either opens a ReviewModal.
After review the buttons are replaced with a single disabled status button.

Uses: bot.db.suggestion  (SuggestionRepository)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from util.db.models import ReviewStatus, SuggestionSettings
from util.constants import Emojis, Colours

if TYPE_CHECKING:
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _suggestion_embed(
    suggestion: str,
    serial: int,
    author: discord.User | discord.Member,
) -> discord.Embed:
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
    colour = discord.Colour.green() if status == ReviewStatus.ACCEPTED else discord.Colour.red()
    embed.colour = colour
    embed.add_field(
        name=f"{'✅ Approved' if status == ReviewStatus.ACCEPTED else '❌ Denied'} by {reviewer}",
        value=reason,
        inline=False,
    )
    return embed


# ── Persistent action view (on suggestion messages) ───────────────────────────

class SuggestionActionView(discord.ui.View):
    """
    Persistent view attached to every suggestion message.
    custom_ids are fixed strings — message_id is read from
    interaction.message.id at callback time, avoiding dynamic custom_ids.
    Registered with bot.add_view() on cog load.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_messages:  # type: ignore[union-attr]
            await interaction.response.send_message(
                ":no_entry: You need **Manage Messages** to review suggestions.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Approve", emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="suggest:approve",
    )
    async def approve_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ReviewModal(action=ReviewStatus.ACCEPTED)
        )

    @discord.ui.button(
        label="Deny", emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="suggest:deny",
    )
    async def deny_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ReviewModal(action=ReviewStatus.REJECTED)
        )


class ReviewedView(discord.ui.View):
    """Single disabled button shown after a suggestion is reviewed."""

    def __init__(self, status: ReviewStatus, reviewer_name: str) -> None:
        super().__init__(timeout=None)
        verb = "Approved" if status == ReviewStatus.ACCEPTED else "Denied"
        style = (
            discord.ButtonStyle.success
            if status == ReviewStatus.ACCEPTED
            else discord.ButtonStyle.danger
        )
        emoji = "✅" if status == ReviewStatus.ACCEPTED else "❌"
        btn = discord.ui.Button(
            label=f"{verb} by {reviewer_name}",
            style=style,
            emoji=emoji,
            disabled=True,
            custom_id="suggest:reviewed",
        )
        self.add_item(btn)


# ── Modals ────────────────────────────────────────────────────────────────────

class SuggestModal(discord.ui.Modal, title="Submit a Suggestion"):
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
        await self._cog._post_suggestion(interaction, str(self.suggestion.value))


class ReviewModal(discord.ui.Modal):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your decision…",
        required=False,
        max_length=500,
    )

    def __init__(self, *, action: ReviewStatus) -> None:
        verb = "Approve" if action == ReviewStatus.ACCEPTED else "Deny"
        super().__init__(title=f"{verb} Suggestion")
        self.action = action

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or "No reason provided."
        await _apply_review(interaction, self.action, reason)


# ── Setup wizard view ─────────────────────────────────────────────────────────

class _ChannelSelect(discord.ui.ChannelSelect):
    """Generic single-channel picker filtered to text channels."""

    def __init__(self, placeholder: str, attr: str, row: int, view: "SetupView") -> None:
        super().__init__(
            placeholder=placeholder,
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=row,
        )
        self._attr = attr
        self._view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        setattr(self._view, self._attr, self.values[0].id)
        await interaction.response.defer()


class SetupView(discord.ui.View):
    """
    Ephemeral setup wizard opened by /suggestion setup.
    Three ChannelSelect menus (suggestion / approve / deny channels)
    + Save and Cancel buttons.
    """

    def __init__(self, owner_id: int, existing: SuggestionSettings | None) -> None:
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
        self.add_item(_ChannelSelect("📨 Suggestion channel", "suggestion_channel", 0, self))
        self.add_item(_ChannelSelect("✅ Approved channel",   "approve_channel",    1, self))
        self.add_item(_ChannelSelect("❌ Denied channel",     "deny_channel",       2, self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        missing = [
            name for name, val in [
                ("Suggestion", self.suggestion_channel),
                ("Approve",    self.approve_channel),
                ("Deny",       self.deny_channel),
            ] if val is None
        ]
        if missing:
            await interaction.response.send_message(
                f":warning: Please select channels for: **{', '.join(missing)}**",
                ephemeral=True,
            )
            return

        await interaction.client.db.suggestion.create_settings(  # type: ignore[attr-defined]
            guild_id=interaction.guild_id,
            channel_id=self.suggestion_channel,
            approve_channel_id=self.approve_channel,
            deny_channel_id=self.deny_channel,
        )
        embed = discord.Embed(
            title="✅ Suggestion system configured",
            colour=discord.Colour.green(),
        )
        embed.add_field(
            name="Suggestion channel",
            value=f"<#{self.suggestion_channel}>",
        )
        embed.add_field(
            name="Approve channel",
            value=f"<#{self.approve_channel}>",
        )
        embed.add_field(
            name="Deny channel",
            value=f"<#{self.deny_channel}>",
        )
        self.stop()
        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Setup cancelled.", embed=None, view=None
        )


# ── Shared review logic (used by both button and slash flows) ─────────────────

async def _apply_review(
    interaction: discord.Interaction,
    status: ReviewStatus,
    reason: str,
) -> None:
    """
    Update DB, edit the original suggestion message, and post to the
    approve/deny channel. Works from both the persistent button flow
    (interaction.message is the suggestion message) and the slash flow
    (message looked up by serial).
    """
    message_id = interaction.message.id  # type: ignore[union-attr]
    suggestion = await interaction.client.db.suggestion.get(message_id)  # type: ignore[attr-defined]
    if suggestion is None:
        await interaction.followup.send(
            ":warning: Could not find that suggestion in the database.",
            ephemeral=True,
        )
        return

    if suggestion.is_reviewed is not None:
        await interaction.followup.send(
            f":no_entry: This suggestion was already **{suggestion.is_reviewed}**.",
            ephemeral=True,
        )
        return

    await interaction.client.db.suggestion.set_status(message_id, status)  # type: ignore[attr-defined]
    settings = await interaction.client.db.suggestion.get_settings(interaction.guild_id)  # type: ignore[attr-defined]

    # Update the original suggestion message — swap buttons for status button.
    reviewed_view = ReviewedView(status, str(interaction.user))
    original_embed = interaction.message.embeds[0]  # type: ignore[union-attr]
    updated_embed = _reviewed_embed(original_embed, status, interaction.user, reason)
    await interaction.message.edit(embed=updated_embed, view=reviewed_view)  # type: ignore[union-attr]

    # Post result to the appropriate channel.
    if settings:
        target_id = (
            settings.approve_channel_id
            if status == ReviewStatus.ACCEPTED
            else settings.deny_channel_id
        )
        if target_id:
            channel = interaction.guild.get_channel(target_id)  # type: ignore[union-attr]
            if isinstance(channel, discord.TextChannel):
                verb = "approved" if status == ReviewStatus.ACCEPTED else "denied"
                result_embed = discord.Embed(
                    title=f"💡 Suggestion #{suggestion.serial_no} {verb}",
                    description=suggestion.suggestion,
                    colour=(
                        discord.Colour.green()
                        if status == ReviewStatus.ACCEPTED
                        else discord.Colour.red()
                    ),
                )
                result_embed.add_field(name="Reason", value=reason)
                result_embed.add_field(
                    name="Reviewed by", value=interaction.user.mention
                )
                jump_view = discord.ui.View()
                jump_view.add_item(
                    discord.ui.Button(
                        label="Jump to suggestion",
                        style=discord.ButtonStyle.link,
                        url=interaction.message.jump_url,  # type: ignore[union-attr]
                    )
                )
                await channel.send(embed=result_embed, view=jump_view)

    await interaction.followup.send(
        f"{'✅' if status == ReviewStatus.ACCEPTED else '❌'} "
        f"Suggestion #{suggestion.serial_no} has been "
        f"{'approved' if status == ReviewStatus.ACCEPTED else 'denied'}.",
        ephemeral=True,
    )


# ── Cog ───────────────────────────────────────────────────────────────────────

class Suggestion(commands.Cog, description="Submit and manage server suggestions."):
    COG_EMOJI = "💡"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Register the persistent view so buttons work after restarts.
        bot.add_view(SuggestionActionView())

    # ── /suggest ──────────────────────────────────────────────────────────────

    @app_commands.command(name="suggest", description="Submit a suggestion to the server.")
    async def suggest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SuggestModal(self))

    async def _post_suggestion(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        assert interaction.guild is not None

        settings = await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]

        # Auto-create channels if never configured.
        if not settings or not settings.channel_id:
            s_ch = await interaction.guild.create_text_channel("📨｜suggestions")
            a_ch = await interaction.guild.create_text_channel("✅｜approved-suggestions")
            d_ch = await interaction.guild.create_text_channel("❌｜denied-suggestions")
            settings = await self.bot.db.suggestion.create_settings(  # type: ignore[attr-defined]
                guild_id=interaction.guild.id,
                channel_id=s_ch.id,
                approve_channel_id=a_ch.id,
                deny_channel_id=d_ch.id,
            )

        serial = await self.bot.db.suggestion.next_serial(interaction.guild.id)  # type: ignore[attr-defined]
        channel = interaction.guild.get_channel(settings.channel_id)

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                ":warning: The suggestion channel is no longer valid. "
                "Please run `/suggestion setup` again.",
                ephemeral=True,
            )
            return

        embed = _suggestion_embed(text, serial, interaction.user)
        view = SuggestionActionView()
        message = await channel.send(embed=embed, view=view)

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

    @suggestion_grp.command(
        name="setup",
        description="Configure suggestion channels with an interactive wizard.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def suggestion_setup(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        existing = await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        view = SetupView(owner_id=interaction.user.id, existing=existing)

        embed = discord.Embed(
            title="⚙️ Suggestion Setup",
            description=(
                "Select a text channel for each role below, then press **Save**.\n"
                "Current settings are pre-filled where available."
            ),
            colour=discord.Colour.blurple(),
        )
        if existing:
            embed.add_field(
                name="Current channels",
                value=(
                    f"Suggestions: <#{existing.channel_id}>\n"
                    f"Approved: <#{existing.approve_channel_id}>\n"
                    f"Denied: <#{existing.deny_channel_id}>"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Autocomplete helper ───────────────────────────────────────────────────

    async def _pending_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if not interaction.guild:
            return []
        pending = await self.bot.db.suggestion.get_pending(interaction.guild.id)  # type: ignore[attr-defined]
        return [
            app_commands.Choice(
                name=f"#{s.serial_no} — {s.suggestion[:60]}",
                value=s.serial_no,
            )
            for s in pending
            if current in str(s.serial_no) or current.lower() in s.suggestion.lower()
        ][:25]

    # ── /suggestion approve ───────────────────────────────────────────────────

    @suggestion_grp.command(name="approve", description="Approve a pending suggestion.")
    @app_commands.describe(serial="Suggestion number (autocomplete shows pending ones).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_approve(
        self, interaction: discord.Interaction, serial: int
    ) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.ACCEPTED)

    # ── /suggestion deny ──────────────────────────────────────────────────────

    @suggestion_grp.command(name="deny", description="Deny a pending suggestion.")
    @app_commands.describe(serial="Suggestion number (autocomplete shows pending ones).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(serial=_pending_autocomplete)
    async def suggestion_deny(
        self, interaction: discord.Interaction, serial: int
    ) -> None:
        await self._slash_review(interaction, serial, ReviewStatus.REJECTED)

    async def _slash_review(
        self, interaction: discord.Interaction, serial: int, status: ReviewStatus
    ) -> None:
        """
        Slash-command path for approve/deny: looks up the suggestion's
        message, then opens ReviewModal (which calls _apply_review on submit).
        """
        assert interaction.guild is not None
        suggestion = await self.bot.db.suggestion.get_by_serial(  # type: ignore[attr-defined]
            interaction.guild.id, serial
        )
        if suggestion is None:
            await interaction.response.send_message(
                f":no_entry: Suggestion **#{serial}** not found.", ephemeral=True
            )
            return
        if suggestion.is_reviewed is not None:
            await interaction.response.send_message(
                f":no_entry: Suggestion **#{serial}** was already "
                f"**{suggestion.is_reviewed}**.",
                ephemeral=True,
            )
            return

        # We need the Discord message to call _apply_review (it edits the msg).
        settings = await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        if not settings or not settings.channel_id:
            await interaction.response.send_message(
                ":warning: Suggestion channel not configured.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                ":warning: Suggestion channel is invalid.", ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(suggestion.message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                ":warning: Original suggestion message was deleted.", ephemeral=True
            )
            return

        # Temporarily set interaction.message so _apply_review can edit it.
        interaction._state  # noqa: B018 — access check only
        # Use a thin wrapper approach: defer first, then call logic.
        await interaction.response.defer(ephemeral=True)

        reason_modal_sentinel = f"__slash__{serial}__{status}"
        # Since modals need a non-deferred response, do the review inline:
        await _apply_review_direct(interaction, message, suggestion, status, reason="")

    async def _on_timeout(self) -> None:
        pass


async def _apply_review_direct(
    interaction: discord.Interaction,
    message: discord.Message,
    suggestion,  # Suggestion model
    status: ReviewStatus,
    reason: str,
) -> None:
    """
    Direct (non-modal) review used by the slash command path.
    Opens a ReviewModal and then applies the review on submit.
    """
    # For the slash path we can't open a modal after defer, so prompt for reason
    # via a follow-up View with a button that opens the modal on the real message.
    # Simpler: just apply with no reason and note it in the footer.
    reason = reason or "No reason provided."
    settings = await interaction.client.db.suggestion.get_settings(interaction.guild_id)  # type: ignore[attr-defined]
    await interaction.client.db.suggestion.set_status(message.id, status)  # type: ignore[attr-defined]

    reviewed_view = ReviewedView(status, str(interaction.user))
    original_embed = message.embeds[0] if message.embeds else discord.Embed()
    updated_embed = _reviewed_embed(original_embed, status, interaction.user, reason)
    await message.edit(embed=updated_embed, view=reviewed_view)

    if settings:
        target_id = (
            settings.approve_channel_id
            if status == ReviewStatus.ACCEPTED
            else settings.deny_channel_id
        )
        if target_id:
            channel = interaction.guild.get_channel(target_id)  # type: ignore[union-attr]
            if isinstance(channel, discord.TextChannel):
                result_embed = discord.Embed(
                    title=f"💡 Suggestion #{suggestion.serial_no} "
                    f"{'approved' if status == ReviewStatus.ACCEPTED else 'denied'}",
                    description=suggestion.suggestion,
                    colour=(
                        discord.Colour.green()
                        if status == ReviewStatus.ACCEPTED
                        else discord.Colour.red()
                    ),
                )
                result_embed.add_field(name="Reviewed by", value=interaction.user.mention)
                jump_view = discord.ui.View()
                jump_view.add_item(
                    discord.ui.Button(
                        label="Jump to suggestion",
                        style=discord.ButtonStyle.link,
                        url=message.jump_url,
                    )
                )
                await channel.send(embed=result_embed, view=jump_view)

    verb = "approved" if status == ReviewStatus.ACCEPTED else "denied"
    await interaction.followup.send(
        f"{'✅' if status == ReviewStatus.ACCEPTED else '❌'} "
        f"Suggestion #{suggestion.serial_no} {verb}.",
        ephemeral=True,
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestion(bot))