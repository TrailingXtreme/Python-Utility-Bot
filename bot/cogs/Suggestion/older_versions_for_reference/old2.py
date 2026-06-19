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

Suggestion embeds carry a persistent SuggestionActionView (Approve / Deny
buttons, custom_id-based). Clicking either opens a ReviewModal. After review
the buttons are replaced with a single disabled status button.

IMPORTANT — interaction acknowledgement:
  Every modal's on_submit() defers the interaction immediately, before any
  DB or Discord work happens. Modal and component interactions must be
  acknowledged within 3 seconds; once that window passes Discord tears
  down the interaction's webhook, and a later `interaction.followup.send()`
  fails with a 404 "Unknown Webhook" — that was the source of the errors
  this file used to throw on basically every command.

Uses: bot.db.suggestion  (SuggestionRepository)
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Colours, Emojis
from util.db.models import ReviewStatus, Suggestion, SuggestionSettings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _suggestion_embed(
    suggestion: str,
    serial: int,
    author: discord.User | discord.Member,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{Emojis.idea}  Suggestion #{serial}",
        description=f">>> {suggestion}",
        colour=discord.Colour(int(Colours.DISCORD_COLOURS["blurple"], 16)),
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
    approved = status == ReviewStatus.ACCEPTED
    status_emoji = Emojis.check_mark if approved else Emojis.cross_mark
    embed.colour = discord.Colour(int(Colours.DISCORD_COLOURS["green"], 16)) if approved else discord.Colour(int(Colours.DISCORD_COLOURS["red"], 16))

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
    async def approve_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ReviewModal(action=ReviewStatus.ACCEPTED)
        )

    @discord.ui.button(
        label="Deny", emoji=Emojis.cross_mark,
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
        emoji = Emojis.check_mark if status == ReviewStatus.ACCEPTED else Emojis.cross_mark
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
        # Ack immediately — channel auto-creation + DB writes below can
        # easily take longer than Discord's 3-second response window.
        await interaction.response.defer(ephemeral=True)
        await self._cog._post_suggestion(interaction, str(self.suggestion.value))


class ReviewModal(discord.ui.Modal):
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
        message: discord.Message | None = None,
        suggestion: Suggestion | None = None,
    ) -> None:
        """
        `message` / `suggestion` are only passed by the slash-command path,
        where the target message has already been looked up. The button
        flow leaves them as None — `_apply_review` resolves both from
        `interaction.message` instead.
        """
        verb = "Approve" if action == ReviewStatus.ACCEPTED else "Deny"
        super().__init__(title=f"{verb} Suggestion")
        self.action = action
        self._message = message
        self._suggestion = suggestion

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or "No reason provided."
        await _apply_review(
            interaction, self.action, reason,
            message=self._message, suggestion=self._suggestion,
        )


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
        self.add_item(_ChannelSelect(f"{Emojis.envelope} Suggestion channel", "suggestion_channel", 0, self))
        self.add_item(_ChannelSelect(f"{Emojis.check_mark} Approved channel",  "approve_channel",    1, self))
        self.add_item(_ChannelSelect(f"{Emojis.cross_mark} Denied channel",    "deny_channel",       2, self))

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
        embed = discord.Embed(
            title=f"{Emojis.check_mark}  Suggestion system configured",
            description="Here's where suggestions will flow from now on:",
            colour=discord.Colour(int(Colours.DISCORD_COLOURS["green"], 16)),
        )
        embed.add_field(
            name=f"{Emojis.envelope} Suggestion channel",
            value=f"<#{self.suggestion_channel}>",
            inline=True,
        )
        embed.add_field(
            name=f"{Emojis.check_mark} Approve channel",
            value=f"<#{self.approve_channel}>",
            inline=True,
        )
        embed.add_field(
            name=f"{Emojis.cross_mark} Deny channel",
            value=f"<#{self.deny_channel}>",
            inline=True,
        )
        self.stop()
        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=f"{Emojis.cross_mark} Setup cancelled.", embed=None, view=None
        )


# ── Shared review logic (used by both button and slash flows) ─────────────────

async def _apply_review(
    interaction: discord.Interaction,
    status: ReviewStatus,
    reason: str,
    *,
    message: discord.Message | None = None,
    suggestion: Suggestion | None = None,
) -> None:
    """
    Update DB, edit the original suggestion message, and post to the
    approve/deny channel. Used by both the persistent-button flow
    (message/suggestion left as None → resolved from interaction.message)
    and the slash-command flow (both passed in explicitly, since a slash
    command's own interaction has no `.message` to resolve them from).

    This always runs as a modal submission, which must be acknowledged
    within 3 seconds — so we defer first, before any DB call or Discord
    edit. Skipping that step is what previously made every
    `interaction.followup.send()` below fail with a 404 "Unknown Webhook":
    once the unacknowledged interaction's 3-second window lapsed, Discord
    considered it failed and tore down its webhook before we ever got
    around to using it.
    """
    await interaction.response.defer(ephemeral=True)

    target_message = message or interaction.message
    if target_message is None:
        await interaction.followup.send(
            f"{Emojis.warning} Could not locate the suggestion message.",
            ephemeral=True,
        )
        return

    if suggestion is None:
        suggestion = await interaction.client.db.suggestion.get(target_message.id)  # type: ignore[attr-defined]
    if suggestion is None:
        await interaction.followup.send(
            f"{Emojis.warning} Could not find that suggestion in the database.",
            ephemeral=True,
        )
        return

    if suggestion.is_reviewed is not None:
        await interaction.followup.send(
            f"{Emojis.no_entry} This suggestion was already **{suggestion.is_reviewed}**.",
            ephemeral=True,
        )
        return

    await interaction.client.db.suggestion.set_status(target_message.id, status)  # type: ignore[attr-defined]
    settings = await interaction.client.db.suggestion.get_settings(interaction.guild_id)  # type: ignore[attr-defined]

    # Swap the action buttons for the disabled status button and refresh
    # the embed's colour + status field.
    reviewed_view = ReviewedView(status, str(interaction.user))
    original_embed = target_message.embeds[0] if target_message.embeds else discord.Embed()
    updated_embed = _reviewed_embed(original_embed, status, interaction.user, reason)
    try:
        await target_message.edit(embed=updated_embed, view=reviewed_view)
    except discord.NotFound:
        await interaction.followup.send(
            f"{Emojis.warning} The original suggestion message was deleted, "
            "but this review has still been recorded.",
            ephemeral=True,
        )

    # Post the result to the configured approve/deny channel, if any.
    if settings:
        target_id = (
            settings.approve_channel_id
            if status == ReviewStatus.ACCEPTED
            else settings.deny_channel_id
        )
        if target_id:
            channel = interaction.guild.get_channel(target_id)  # type: ignore[union-attr]
            if isinstance(channel, discord.TextChannel):
                approved = status == ReviewStatus.ACCEPTED
                emoji = Emojis.check_mark if approved else Emojis.cross_mark
                result_embed = discord.Embed(
                    title=f"{emoji}  Suggestion #{suggestion.serial_no} "
                    f"{'Approved' if approved else 'Denied'}",
                    description=f">>> {suggestion.suggestion}",
                    colour=discord.Colour(int(Colours.DISCORD_COLOURS["green"], 16)) if approved else discord.Colour(int(Colours.DISCORD_COLOURS["red"], 16)),
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
                        url=target_message.jump_url,
                    )
                )
                await channel.send(embed=result_embed, view=jump_view)

    verb = "approved" if status == ReviewStatus.ACCEPTED else "denied"
    emoji = Emojis.check_mark if status == ReviewStatus.ACCEPTED else Emojis.cross_mark
    await interaction.followup.send(
        f"{emoji} Suggestion **#{suggestion.serial_no}** has been {verb}.",
        ephemeral=True,
    )


# ── Cog ───────────────────────────────────────────────────────────────────────

class Suggestion(commands.Cog, description="Submit and manage server suggestions."):
    COG_EMOJI = Emojis.idea

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
            s_ch = await interaction.guild.create_text_channel(f"{Emojis.envelope}｜suggestions")
            a_ch = await interaction.guild.create_text_channel(f"{Emojis.check_mark}｜approved-suggestions")
            d_ch = await interaction.guild.create_text_channel(f"{Emojis.cross_mark}｜denied-suggestions")
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
                f"{Emojis.warning} The suggestion channel is no longer valid. "
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
        existing = await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        view = SetupView(owner_id=interaction.user.id, existing=existing)

        embed = discord.Embed(
            title=f"{Emojis.gear}  Suggestion Setup",
            description=(
                "Select a text channel for each role below, then press **Save**.\n"
                "Current settings are pre-filled where available."
            ),
            colour=discord.Colour(int(Colours.DISCORD_COLOURS["blurple"], 16)),
        )
        if existing:
            embed.add_field(
                name=f"{Emojis.clipboard} Current channels",
                value=(
                    f"{Emojis.envelope} Suggestions: <#{existing.channel_id}>\n"
                    f"{Emojis.check_mark} Approved: <#{existing.approve_channel_id}>\n"
                    f"{Emojis.cross_mark} Denied: <#{existing.deny_channel_id}>"
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
        message, then opens the same ReviewModal used by the button flow
        so the reviewer can leave an optional reason.

        The message and suggestion are passed straight into the modal
        because a slash command's own interaction has no `.message` —
        that only exists on component interactions — so there'd be
        nothing for `_apply_review` to resolve them from later.
        """
        assert interaction.guild is not None
        suggestion = await self.bot.db.suggestion.get_by_serial(  # type: ignore[attr-defined]
            interaction.guild.id, serial
        )
        if suggestion is None:
            await interaction.response.send_message(
                f"{Emojis.no_entry} Suggestion **#{serial}** not found.", ephemeral=True
            )
            return
        if suggestion.is_reviewed is not None:
            await interaction.response.send_message(
                f"{Emojis.no_entry} Suggestion **#{serial}** was already "
                f"**{suggestion.is_reviewed}**.",
                ephemeral=True,
            )
            return

        settings = await self.bot.db.suggestion.get_settings(interaction.guild.id)  # type: ignore[attr-defined]
        if not settings or not settings.channel_id:
            await interaction.response.send_message(
                f"{Emojis.warning} Suggestion channel not configured.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                f"{Emojis.warning} Suggestion channel is invalid.", ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(suggestion.message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                f"{Emojis.warning} Original suggestion message was deleted.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ReviewModal(action=status, message=message, suggestion=suggestion)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestion(bot))