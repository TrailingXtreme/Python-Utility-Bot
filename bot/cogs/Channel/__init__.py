"""Channel cog — advanced channel management inspired by Dyno, ProBot, Arcane & MEE6.
────────────────────────────────────────────────────────────────────────────────
What's new
──────────────────────────
  • /channel info          — Rich metadata embed (type, topic, permissions summary).
  • /channel slowmode      — Set / clear slowmode (0 – 21 600 s) with friendly output.
  • /channel topic         — Set or clear the channel topic.
  • /channel rename        — Rename any text or voice channel.
  • /channel nsfw          — Toggle NSFW flag with confirmation.
  • /channel hide/unhide   — Per-role view_channel overwrite (default @everyone).
  • /channel lock/unlock   — Explicit send-message lockdown with in-channel notice embeds.
  • /channel archive       — Read-only mode: locks + kills reactions + optional prefix rename.
  • /channel clone         — Duplicate a channel (name, topic, perms, slowmode, NSFW, category).
  • /channel purge         — Bulk-delete up to 1 000 messages; filters by user / bots /
                             contains / attachments / embeds.
  • /channel create voice  — Create voice channels with bitrate & user-limit params.
  • /channel delete voice  — Delete voice channels.
  • /channel create text   — Now accepts topic, slowmode, and nsfw at creation time.
  • /channel create category — Role param made optional.
  • cog_app_command_error  — User-friendly embeds for missing-permission errors.
  • Helper functions       — Consistent green / red / blurple embeds throughout.
"""

import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


# ── Helpers ──────────────────────────────────────────────────────────────────

PURGE_LIMIT = 1_000  # absolute safety cap for /channel purge


def _colour() -> discord.Colour:
    return discord.Colour(random.randint(0, 0xFFFFFF))


def _embed(description: str, colour: discord.Colour, *, title: str = "") -> discord.Embed:
    e = discord.Embed(description=description, colour=colour)
    if title:
        e.title = title
    e.timestamp = datetime.now(timezone.utc)
    return e


def _ok(text: str) -> discord.Embed:
    return _embed(f"✅  {text}", discord.Colour.green())


def _err(text: str) -> discord.Embed:
    return _embed(f"❌  {text}", discord.Colour.red())


def _fmt_duration(seconds: int) -> str:
    """Turn a raw second count into a human-readable string, e.g. '1h 30m 5s'."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"


# ── Cog ──────────────────────────────────────────────────────────────────────


class Channels(commands.Cog, description="Advanced channel management — like Dyno, ProBot, Arcane & MEE6."):
    COG_EMOJI = "📺"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── App-command groups ───────────────────────────────────────────────────

    channel_group = app_commands.Group(
        name="channel", description="Channel management commands."
    )
    create_group = app_commands.Group(
        name="create", description="Create categories, text, or voice channels.",
        parent=channel_group,
    )
    delete_group = app_commands.Group(
        name="delete", description="Delete categories, text, or voice channels.",
        parent=channel_group,
    )

    # ── Centralised error handler ────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"You need the {perms} permission(s) to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"I need the {perms} permission(s) to do that."
        else:
            raise error  # bubble up unexpected errors

        if interaction.response.is_done():
            await interaction.followup.send(embed=_err(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=_err(msg), ephemeral=True)

    # ── /channelstats (hybrid, keep prefix fallback) ─────────────────────────

    @commands.hybrid_command(
        name="channelstats",
        aliases=["cs"],
        description="Show stats for the current channel.",
    )
    async def channelstats(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        channel = ctx.channel

        embed = discord.Embed(
            title=f"#{channel.name}",
            colour=_colour(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon)
        embed.add_field(name="ID", value=f"`{channel.id}`")
        embed.add_field(name="Guild", value=ctx.guild.name)
        embed.add_field(
            name="Category",
            value=(
                channel.category.name
                if isinstance(channel, discord.abc.GuildChannel) and channel.category
                else "*None*"
            ),
        )
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="Topic", value=channel.topic or "*None*")
            embed.add_field(
                name="Slowmode",
                value=_fmt_duration(channel.slowmode_delay) if channel.slowmode_delay else "Off",
            )
            embed.add_field(name="NSFW", value="Yes" if channel.is_nsfw() else "No")
            embed.add_field(name="Announcement", value="Yes" if channel.is_news() else "No")
            embed.add_field(name="Threads", value=str(len(channel.threads)))
        embed.add_field(name="Position", value=str(channel.position))
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(channel.created_at, "R"),
        )
        embed.set_footer(text=f"Requested by {ctx.author}")
        await ctx.send(embed=embed)

    # ── /channel info ─────────────────────────────────────────────────────────

    @channel_group.command(
        name="info",
        description="Detailed info embed for any channel.",
    )
    @app_commands.describe(channel="Channel to inspect (defaults to current)")
    async def channel_info(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel

        embed = discord.Embed(
            title=f"Channel Info — #{target.name}",
            colour=_colour(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=interaction.guild.name, icon_url=interaction.guild.icon)
        embed.add_field(name="ID", value=f"`{target.id}`")
        embed.add_field(
            name="Type",
            value=str(target.type).replace("_", " ").title(),
        )
        embed.add_field(name="Position", value=str(target.position))
        embed.add_field(
            name="Category",
            value=(
                target.category.name
                if isinstance(target, discord.abc.GuildChannel) and target.category
                else "*None*"
            ),
        )
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(target.created_at, "F"),
            inline=False,
        )
        embed.add_field(
            name="Age",
            value=discord.utils.format_dt(target.created_at, "R"),
        )

        if isinstance(target, discord.TextChannel):
            embed.add_field(
                name="Topic",
                value=target.topic or "*None*",
                inline=False,
            )
            embed.add_field(
                name="Slowmode",
                value=_fmt_duration(target.slowmode_delay) if target.slowmode_delay else "Off",
            )
            embed.add_field(name="NSFW", value="Yes" if target.is_nsfw() else "No")
            embed.add_field(
                name="Announcement",
                value="Yes" if target.is_news() else "No",
            )
            embed.add_field(
                name="Active Threads",
                value=str(len(target.threads)),
            )

            # Permission summary
            locked_for = [
                r.name
                for r, ow in target.overwrites.items()
                if isinstance(r, discord.Role) and ow.send_messages is False
            ]
            hidden_from = [
                r.name
                for r, ow in target.overwrites.items()
                if isinstance(r, discord.Role) and ow.view_channel is False
            ]
            embed.add_field(
                name="🔒 Locked For",
                value=", ".join(locked_for) or "*Nobody*",
                inline=False,
            )
            embed.add_field(
                name="👁‍🗨 Hidden From",
                value=", ".join(hidden_from) or "*Nobody*",
            )

        elif isinstance(target, discord.VoiceChannel):
            embed.add_field(
                name="Bitrate",
                value=f"{target.bitrate // 1000} kbps",
            )
            embed.add_field(
                name="User Limit",
                value=str(target.user_limit) if target.user_limit else "Unlimited",
            )
            embed.add_field(
                name="Currently Connected",
                value=str(len(target.members)),
            )

        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    # ── /channel slowmode ─────────────────────────────────────────────────────

    @channel_group.command(
        name="slowmode",
        description="Set slowmode on a channel. 0 = disabled, max 21600 s (6 h).",
    )
    @app_commands.describe(
        seconds="Delay in seconds (0 to disable)",
        channel="Channel to apply slowmode to (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        await target.edit(slowmode_delay=seconds)
        if seconds == 0:
            embed = _ok(f"Slowmode **disabled** in {target.mention}.")
        else:
            embed = _ok(
                f"Slowmode set to **{_fmt_duration(seconds)}** in {target.mention}."
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /channel topic ────────────────────────────────────────────────────────

    @channel_group.command(
        name="topic",
        description="Set or clear the channel topic.",
    )
    @app_commands.describe(
        topic="New topic text (leave blank to clear)",
        channel="Channel to update (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def topic(
        self,
        interaction: discord.Interaction,
        topic: str = "",
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        await target.edit(topic=topic or None)
        if topic:
            embed = _ok(f"Topic for {target.mention} updated.")
        else:
            embed = _ok(f"Topic for {target.mention} cleared.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /channel rename ───────────────────────────────────────────────────────

    @channel_group.command(
        name="rename",
        description="Rename a text or voice channel.",
    )
    @app_commands.describe(
        name="New channel name",
        channel="Channel to rename (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def rename(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, (discord.TextChannel, discord.VoiceChannel))
        old_name = target.name
        await target.edit(name=name)
        await interaction.response.send_message(
            embed=_ok(f"Renamed **#{old_name}** → **#{name}**."),
            ephemeral=True,
        )

    # ── /channel nsfw ─────────────────────────────────────────────────────────

    @channel_group.command(
        name="nsfw",
        description="Toggle the NSFW flag on a text channel.",
    )
    @app_commands.describe(channel="Channel to toggle (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def nsfw(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        new_state = not target.is_nsfw()
        await target.edit(nsfw=new_state)
        label = "🔞 **enabled**" if new_state else "✅ **disabled**"
        await interaction.response.send_message(
            embed=_ok(f"NSFW {label} for {target.mention}."),
            ephemeral=True,
        )

    # ── /channel hide ─────────────────────────────────────────────────────────

    @channel_group.command(
        name="hide",
        description="Hide a channel from a role (@everyone by default).",
    )
    @app_commands.describe(
        role="Role to hide the channel from (default: @everyone)",
        channel="Channel to hide (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def hide(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        role = role or interaction.guild.default_role
        overwrite = target.overwrites_for(role)
        overwrite.view_channel = False
        await target.set_permissions(role, overwrite=overwrite)
        await interaction.response.send_message(
            embed=_ok(f"{target.mention} is now **hidden** from **{role.name}**."),
            ephemeral=True,
        )

    # ── /channel unhide ───────────────────────────────────────────────────────

    @channel_group.command(
        name="unhide",
        description="Reveal a hidden channel to a role (@everyone by default).",
    )
    @app_commands.describe(
        role="Role to reveal the channel to (default: @everyone)",
        channel="Channel to unhide (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unhide(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        role = role or interaction.guild.default_role
        overwrite = target.overwrites_for(role)
        overwrite.view_channel = None  # resets to neutral / inherited
        await target.set_permissions(role, overwrite=overwrite)
        await interaction.response.send_message(
            embed=_ok(f"{target.mention} is now **visible** to **{role.name}**."),
            ephemeral=True,
        )

    # ── /channel lock ─────────────────────────────────────────────────────────

    @channel_group.command(
        name="lock",
        description="Prevent a role (@everyone default) from sending messages.",
    )
    @app_commands.describe(
        channel="Channel to lock (defaults to current)",
        role="Role to lock out (default: @everyone)",
        reason="Reason shown in the audit log and channel notice",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        role = role or interaction.guild.default_role

        overwrite = target.overwrites_for(role)
        overwrite.send_messages = False
        await target.set_permissions(role, overwrite=overwrite, reason=reason)

        # Post a visible notice inside the channel itself
        notice = discord.Embed(
            title="🔒 Channel Locked",
            description=(
                f"{target.mention} has been locked by {interaction.user.mention}."
                + (f"\n\n**Reason:** {reason}" if reason else "")
            ),
            colour=discord.Colour.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)

        if target.id != interaction.channel_id:
            await interaction.response.send_message(
                embed=_ok(f"🔒 {target.mention} is now **locked** for **{role.name}**."),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("✅ Done.", ephemeral=True)

    # ── /channel unlock ───────────────────────────────────────────────────────

    @channel_group.command(
        name="unlock",
        description="Lift a send-message lockdown on a channel.",
    )
    @app_commands.describe(
        channel="Channel to unlock (defaults to current)",
        role="Role to unlock (default: @everyone)",
        reason="Reason shown in the audit log",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        role = role or interaction.guild.default_role

        overwrite = target.overwrites_for(role)
        overwrite.send_messages = None  # reset to inherited
        await target.set_permissions(role, overwrite=overwrite, reason=reason)

        notice = discord.Embed(
            title="🔓 Channel Unlocked",
            description=(
                f"{target.mention} has been unlocked by {interaction.user.mention}."
            ),
            colour=discord.Colour.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)

        if target.id != interaction.channel_id:
            await interaction.response.send_message(
                embed=_ok(f"🔓 {target.mention} is now **unlocked** for **{role.name}**."),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("✅ Done.", ephemeral=True)

    # ── /channel archive ──────────────────────────────────────────────────────

    @channel_group.command(
        name="archive",
        description="Archive a channel: lock it for everyone, kill reactions, and optionally rename it.",
    )
    @app_commands.describe(
        channel="Channel to archive (defaults to current)",
        prefix="Prefix to prepend to the name (e.g. 'archived' → 'archived-general')",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def archive(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        prefix: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)

        overwrite = target.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        overwrite.add_reactions = False
        await target.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason="Channel archived",
        )

        if prefix:
            await target.edit(name=f"{prefix}-{target.name}")

        notice = discord.Embed(
            title="📁 Channel Archived",
            description=(
                f"This channel has been archived by {interaction.user.mention} "
                "and is now **read-only**."
            ),
            colour=discord.Colour.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)

        await interaction.response.send_message(
            embed=_ok(f"📁 {target.mention} has been archived."),
            ephemeral=True,
        )

    # ── /channel clone ────────────────────────────────────────────────────────

    @channel_group.command(
        name="clone",
        description="Clone a channel — copies name, topic, slowmode, NSFW flag, category, and permission overwrites.",
    )
    @app_commands.describe(
        name="Name for the clone (default: copy-of-<original>)",
        channel="Channel to clone (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def clone(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, (discord.TextChannel, discord.VoiceChannel))
        new_name = name or f"copy-of-{target.name}"
        cloned = await target.clone(
            name=new_name, reason=f"Cloned by {interaction.user}"
        )
        await interaction.response.send_message(
            embed=_ok(f"Cloned **#{target.name}** → {cloned.mention}."),
            ephemeral=True,
        )

    # ── /channel purge ────────────────────────────────────────────────────────

    @channel_group.command(
        name="purge",
        description=f"Bulk-delete up to {PURGE_LIMIT} messages with optional filters.",
    )
    @app_commands.describe(
        amount=f"Number of messages to scan (1 – {PURGE_LIMIT})",
        user="Only delete messages from this member",
        bots_only="Only delete messages sent by bots",
        contains="Only delete messages whose content contains this text",
        attachments_only="Only delete messages that have attachments",
        embeds_only="Only delete messages that have embeds",
        channel="Channel to purge (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        user: discord.Member | None = None,
        bots_only: bool = False,
        contains: str | None = None,
        attachments_only: bool = False,
        embeds_only: bool = False,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)

        await interaction.response.defer(ephemeral=True)

        def _check(m: discord.Message) -> bool:
            if user and m.author != user:
                return False
            if bots_only and not m.author.bot:
                return False
            if contains and contains.lower() not in m.content.lower():
                return False
            if attachments_only and not m.attachments:
                return False
            if embeds_only and not m.embeds:
                return False
            return True

        deleted = await target.purge(limit=amount, check=_check, bulk=True)

        # Describe the active filters for the confirmation embed
        filters: list[str] = []
        if user:
            filters.append(f"from {user.mention}")
        if bots_only:
            filters.append("by bots")
        if contains:
            filters.append(f"containing `{contains}`")
        if attachments_only:
            filters.append("with attachments")
        if embeds_only:
            filters.append("with embeds")
        filter_str = " " + " & ".join(filters) if filters else ""

        result = _ok(
            f"Deleted **{len(deleted)}** message(s){filter_str} in {target.mention}."
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=result, ephemeral=True)

    # ── /channel create category ──────────────────────────────────────────────

    @create_group.command(
        name="category",
        description="Create a new category, optionally visible only to a specific role.",
    )
    @app_commands.describe(
        name="Category name",
        role="Role that can view this category (optional — omit for public)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def create_category(
        self,
        interaction: discord.Interaction,
        name: str,
        role: discord.Role | None = None,
    ) -> None:
        assert interaction.guild is not None
        overwrites: dict = {}
        if role:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
                role: discord.PermissionOverwrite(view_channel=True),
            }
        category = await interaction.guild.create_category(name=name, overwrites=overwrites)
        msg = f"Created category **{category.name}**"
        if role:
            msg += f", visible only to {role.mention}"
        msg += "."
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)

    # ── /channel create text ──────────────────────────────────────────────────

    @create_group.command(
        name="text",
        description="Create a new text channel with optional role restriction, topic, slowmode, and NSFW.",
    )
    @app_commands.describe(
        name="Channel name",
        role="Role that can view this channel (optional)",
        category="Category to place the channel in (optional)",
        topic="Channel topic (optional)",
        slowmode="Slowmode delay in seconds (0 = off)",
        nsfw="Mark channel as NSFW",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def create_text(
        self,
        interaction: discord.Interaction,
        name: str,
        role: discord.Role | None = None,
        category: discord.CategoryChannel | None = None,
        topic: str | None = None,
        slowmode: app_commands.Range[int, 0, 21600] = 0,
        nsfw: bool = False,
    ) -> None:
        assert interaction.guild is not None
        overwrites: dict = {}
        if role:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
                role: discord.PermissionOverwrite(view_channel=True),
            }
        new_channel = await interaction.guild.create_text_channel(
            name=name,
            overwrites=overwrites,
            category=category,
            topic=topic,
            slowmode_delay=slowmode,
            nsfw=nsfw,
        )
        msg = f"Created {new_channel.mention}"
        if role:
            msg += f", visible to {role.mention}"
        msg += "."
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)

    # ── /channel create voice ─────────────────────────────────────────────────

    @create_group.command(
        name="voice",
        description="Create a new voice channel.",
    )
    @app_commands.describe(
        name="Channel name",
        role="Role that can view this channel (optional)",
        category="Category to place the channel in (optional)",
        bitrate="Bitrate in kbps (8 – 384, default 64)",
        user_limit="Max users (0 = unlimited)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def create_voice(
        self,
        interaction: discord.Interaction,
        name: str,
        role: discord.Role | None = None,
        category: discord.CategoryChannel | None = None,
        bitrate: app_commands.Range[int, 8, 384] = 64,
        user_limit: app_commands.Range[int, 0, 99] = 0,
    ) -> None:
        assert interaction.guild is not None
        overwrites: dict = {}
        if role:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
                role: discord.PermissionOverwrite(view_channel=True),
            }
        vc = await interaction.guild.create_voice_channel(
            name=name,
            overwrites=overwrites,
            category=category,
            bitrate=bitrate * 1000,
            user_limit=user_limit,
        )
        msg = f"Created voice channel **{vc.name}**"
        if role:
            msg += f", visible to {role.mention}"
        msg += "."
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)

    # ── /channel delete category ──────────────────────────────────────────────

    @delete_group.command(
        name="category",
        description="Delete a category and report how many channels it held.",
    )
    @app_commands.describe(
        category="The category to delete",
        reason="Audit log reason",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def delete_category(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        reason: str | None = None,
    ) -> None:
        channel_count = len(category.channels)
        name = category.name
        await category.delete(reason=reason)
        await interaction.response.send_message(
            embed=_ok(
                f"Deleted category **{name}** "
                f"({channel_count} channel(s) removed with it)."
            ),
            ephemeral=True,
        )

    # ── /channel delete text ──────────────────────────────────────────────────

    @delete_group.command(
        name="text",
        description="Delete a text channel.",
    )
    @app_commands.describe(
        channel="Channel to delete (defaults to current)",
        reason="Audit log reason",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def delete_text(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        name = target.name
        await target.delete(reason=reason)
        if target.id != interaction.channel_id:
            await interaction.response.send_message(
                embed=_ok(f"Deleted **#{name}**."),
                ephemeral=True,
            )
        # Deleting the current channel naturally drops the interaction.

    # ── /channel delete voice ─────────────────────────────────────────────────

    @delete_group.command(
        name="voice",
        description="Delete a voice channel.",
    )
    @app_commands.describe(
        channel="Voice channel to delete",
        reason="Audit log reason",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def delete_voice(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        reason: str | None = None,
    ) -> None:
        name = channel.name
        await channel.delete(reason=reason)
        await interaction.response.send_message(
            embed=_ok(f"Deleted voice channel **{name}**."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Channels(bot))