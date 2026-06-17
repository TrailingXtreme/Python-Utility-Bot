"""
Location: bot/cogs/Afk/__init__.py
────────────────────────────────────
AFK status management — split out from the old Moderation cog.

Commands:
  /afk [reason]   — Set AFK. Opens a Modal if reason is omitted.
  /afk clear      — Manually clear your own AFK (alternative to sending a msg).

on_message listener:
  • If the message author is AFK → clear their status, restore nickname,
    send a "welcome back" notice (auto-deletes after 8 s).
  • If any mentioned user is AFK → inform the channel (auto-deletes after 10 s).

Nickname behaviour:
  Before going AFK  →  stores member.nick (or "" if no custom nick).
  On return         →  restores stored nick; if stored value is empty
                        the custom nick is cleared (member.edit(nick=None)).
  Nickname edit failures are silently ignored (Forbidden on admins, etc.).

Uses: bot.db.afk  (AfkRepository)
"""

import contextlib

import discord
from discord import app_commands
from discord.ext import commands

# ── Modal ─────────────────────────────────────────────────────────────────────

class AfkModal(discord.ui.Modal, title="Set AFK Status"):
    """Opens when /afk is called without an inline reason."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason",
        placeholder="e.g. Having lunch, back in 30 mins.",
        style=discord.TextStyle.short,
        max_length=200,
        required=False,
    )

    def __init__(self, cog: "Afk") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or "No reason provided."
        await self._cog._apply_afk(interaction, reason)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Afk(commands.Cog, description="Set or clear an AFK status."):
    COG_EMOJI = "💤"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Shared helper ─────────────────────────────────────────────────────────

    async def _apply_afk(
        self, interaction: discord.Interaction, reason: str
    ) -> None:
        """Write the AFK entry and set the [AFK] nickname prefix."""
        assert interaction.guild is not None
        member = interaction.guild.get_member(interaction.user.id)

        # Store the current nickname so we can restore it on return.
        original_nick = (member.nick if member and member.nick else "") if member else ""

        await self.bot.db.afk.set(  # type: ignore[attr-defined]
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            original_name=original_nick,
            reason=reason,
        )

        # Try to prefix the nickname with [AFK].
        if member:
            display = member.nick or member.name
            with contextlib.suppress(discord.Forbidden):
                await member.edit(nick=f"[AFK] {display}"[:32])

        embed = discord.Embed(
            title="💤 You are now AFK",
            description=f"Reason: *{reason}*",
            colour=discord.Colour.greyple(),
        )
        embed.set_footer(text="Send any message to automatically clear your AFK.")

        # Modal submissions use response; slash with inline reason uses followup
        # pattern handled by the caller — respond/followup is unified here.
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /afk group ────────────────────────────────────────────────────────────

    afk_group = app_commands.Group(
        name="afk",
        description="Manage your AFK status.",
    )

    @afk_group.command(name="set", description="Set your AFK status.")
    @app_commands.describe(
        reason="Why you're going AFK — leave blank to type it in a popup."
    )
    async def afk_set(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
    ) -> None:
        if reason is None:
            # No inline reason — open the Modal.
            await interaction.response.send_modal(AfkModal(self))
        else:
            await self._apply_afk(interaction, reason.strip() or "No reason provided.")

    @afk_group.command(name="clear", description="Manually clear your AFK status.")
    async def afk_clear(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        entry = await self.bot.db.afk.get(interaction.user.id)  # type: ignore[attr-defined]

        if not entry:
            await interaction.response.send_message(
                "You are not currently AFK.", ephemeral=True
            )
            return

        await self.bot.db.afk.clear(interaction.user.id)  # type: ignore[attr-defined]
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            await self._restore_nick(member, entry.original_name)

        await interaction.response.send_message(
            "✅ Your AFK status has been cleared.", ephemeral=True
        )

    # ── on_message listener ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # ── Returning from AFK ───────────────────────────────────────────────
        entry = await self.bot.db.afk.get(message.author.id)  # type: ignore[attr-defined]
        if entry:
            cleared = await self.bot.db.afk.clear(message.author.id)  # type: ignore[attr-defined]
            if cleared:
                member = message.guild.get_member(message.author.id)
                if member:
                    await self._restore_nick(member, entry.original_name)

                await message.channel.send(
                    f"👋 Welcome back, {message.author.mention}! "
                    f"You were AFK: *{entry.display_reason}*",
                    delete_after=8,
                )

        # ── Mentioned AFK users ──────────────────────────────────────────────
        for mentioned in message.mentions:
            # Skip bots, the author themselves, and the just-returned user.
            if mentioned.bot or mentioned.id == message.author.id:
                continue
            if entry and mentioned.id == message.author.id:
                continue

            afk_entry = await self.bot.db.afk.get(mentioned.id)  # type: ignore[attr-defined]
            if afk_entry:
                await message.channel.send(
                    f"💤 **{mentioned.display_name}** is AFK: "
                    f"*{afk_entry.display_reason}*",
                    delete_after=10,
                )

    # ── Private helper ────────────────────────────────────────────────────────

    @staticmethod
    async def _restore_nick(member: discord.Member, original_nick: str) -> None:
        """Restore the member's nickname to what it was before going AFK."""
        with contextlib.suppress(discord.Forbidden):
            if original_nick:
                await member.edit(nick=original_nick)
            else:
                await member.edit(nick=None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Afk(bot))