"""
Location: bot/cogs/Config/__init__.py
──────────────────────────────────────
Bot configuration — prefix management and user blacklisting.

Command tree:
  /config prefix set <prefix>        — requires Manage Guild
  /config prefix reset               — requires Manage Guild
  /config blacklist add <user>       — owner-only
  /config blacklist remove <user>    — owner-only
  /config blacklist list             — owner-only; Select to remove inline
  /config logout                     — owner-only; graceful shutdown

Modernisation vs the nextcord version:
  • All commands are slash (app_commands.Group hierarchy).
  • Blacklist listing shows a discord.ui.Select where selecting a user
    immediately unblacklists them — no extra slash invocation needed.
  • Paginated BlacklistView handles >25 entries with ◀️▶️ buttons.
  • Owner check uses a reusable app_commands predicate so Discord shows
    a proper ephemeral error on failure instead of a generic exception.
"""

import discord
from discord import app_commands
from discord.ext import commands
from util.db.models import BlacklistedUser

_MAX_PREFIX_LEN = 10


# ── Owner predicate ────────────────────────────────────────────────────────────

async def _is_owner(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)

owner_only = app_commands.check(_is_owner)


# ── Blacklist list UI ──────────────────────────────────────────────────────────

class BlacklistSelect(discord.ui.Select):
    """Select a user from the current page to unblacklist them instantly."""

    def __init__(self, page: list[BlacklistedUser]) -> None:
        options = [
            discord.SelectOption(
                label=e.user_name[:80],
                value=str(e.user_id),
                description=f"ID: {e.user_id} • blacklisted {discord.utils.format_dt(e.blacklisted_at, 'R')}",
            )
            for e in page
        ]
        super().__init__(
            placeholder="Select a user to unblacklist…",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        user_id = int(self.values[0])
        removed = await interaction.client.db.blacklist.remove(user_id)  # type: ignore[attr-defined]
        if removed:
            await interaction.response.send_message(
                f"✅ <@{user_id}> has been unblacklisted.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ That user was already removed.", ephemeral=True
            )


class BlacklistView(discord.ui.View):
    """
    Paginated view of all blacklisted users.
    Each page shows up to 25 entries in a Select.
    Navigation via ◀️ / ▶️ buttons.
    """

    def __init__(
        self,
        entries: list[BlacklistedUser],
        owner_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self.entries = entries
        self.owner_id = owner_id
        self.page = 0
        self.per_page = 25
        self._build()

    def _build(self) -> None:
        self.clear_items()
        start = self.page * self.per_page
        page_entries = self.entries[start : start + self.per_page]

        if page_entries:
            self.add_item(BlacklistSelect(page_entries))

        total_pages = max(1, -(-len(self.entries) // self.per_page))

        prev_btn = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=1,
        )
        next_btn = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= total_pages - 1,
            row=1,
        )
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        self.add_item(prev_btn)
        self.add_item(next_btn)

    def _embed(self) -> discord.Embed:
        total_pages = max(1, -(-len(self.entries) // self.per_page))
        embed = discord.Embed(
            title=f"🚫 Blacklisted Users ({len(self.entries)} total)",
            colour=discord.Colour.red(),
        )
        start = self.page * self.per_page
        lines = [
            f"`{e.user_id}` — **{e.user_name}** • {discord.utils.format_dt(e.blacklisted_at, 'R')}"
            for e in self.entries[start : start + self.per_page]
        ]
        embed.description = "\n".join(lines) or "No entries on this page."
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • Select a user to remove them")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                ":no_entry: This panel is not for you.", ephemeral=True
            )
            return False
        return True

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Config(commands.Cog, description="Configure the bot for this server."):
    COG_EMOJI = "⚙️"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /config group ─────────────────────────────────────────────────────────
    config_grp = app_commands.Group(
        name="config",
        description="Bot configuration.",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    prefix_grp = app_commands.Group(
        name="prefix",
        description="Manage this server's command prefix.",
        parent=config_grp,
    )
    blacklist_grp = app_commands.Group(
        name="blacklist",
        description="Manage users blocked from using the bot.",
        parent=config_grp,
        default_permissions=discord.Permissions(administrator=True),
    )

    # ── Prefix commands ────────────────────────────────────────────────────────

    @prefix_grp.command(name="set", description="Set a custom prefix for this server.")
    @app_commands.describe(prefix="New prefix (max 10 characters).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def prefix_set(self, interaction: discord.Interaction, prefix: str) -> None:
        if len(prefix) > _MAX_PREFIX_LEN:
            await interaction.response.send_message(
                f":no_entry: Prefix must be {_MAX_PREFIX_LEN} characters or fewer.",
                ephemeral=True,
            )
            return

        assert interaction.guild is not None
        cfg = await self.bot.db.config.set_prefix(interaction.guild.id, prefix)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            f"✅ Prefix set to `{cfg.prefix}`. "
            f"Use `{cfg.prefix}help` or </help:0> to get started.",
            ephemeral=True,
        )

    @prefix_grp.command(name="reset", description="Reset the prefix back to the default `t!`.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def prefix_reset(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await self.bot.db.config.reset_prefix(interaction.guild.id)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            "✅ Prefix reset to `t!`.", ephemeral=True
        )

    # ── Blacklist commands ─────────────────────────────────────────────────────

    @blacklist_grp.command(name="add", description="Block a user from using the bot. (Owner only)")
    @app_commands.describe(user="The member to blacklist.")
    @owner_only
    async def blacklist_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if await self.bot.is_owner(user):  # type: ignore[attr-defined]
            await interaction.response.send_message(
                ":no_entry: You cannot blacklist a bot owner.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message(
                ":no_entry: You cannot blacklist another bot.", ephemeral=True
            )
            return
        assert interaction.guild is not None
        await self.bot.db.blacklist.add(user, interaction.guild.id)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            f"🚫 **{user}** (`{user.id}`) has been blacklisted.", ephemeral=True
        )

    @blacklist_grp.command(name="remove", description="Unblacklist a user. (Owner only)")
    @app_commands.describe(user="The member to unblacklist.")
    @owner_only
    async def blacklist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        removed = await self.bot.db.blacklist.remove(user.id)  # type: ignore[attr-defined]
        if removed:
            await interaction.response.send_message(
                f"✅ **{user}** has been unblacklisted.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f":no_entry: **{user}** is not blacklisted.", ephemeral=True
            )

    @blacklist_grp.command(name="list", description="Show all blacklisted users. (Owner only)")
    @owner_only
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        entries = await self.bot.db.blacklist.list_all()  # type: ignore[attr-defined]

        if not entries:
            await interaction.followup.send(
                "✅ The blacklist is empty.", ephemeral=True
            )
            return

        view = BlacklistView(entries, interaction.user.id)
        await interaction.followup.send(
            embed=view._embed(), view=view, ephemeral=True
        )

    # ── /config logout ─────────────────────────────────────────────────────────

    @config_grp.command(name="logout", description="Gracefully shut the bot down. (Owner only)")
    @owner_only
    async def logout(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "👋 Logging out…", ephemeral=True
        )
        await self.bot.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Config(bot))