# Location: bot/cogs/Dev/__init__.py
"""
Dev cog — owner-only extension management: load / unload / reload cogs and
resync the slash-command tree without a full process restart.

Why this exists
────────────────
The dependency-guard work in cog_load() (see Moderation/Docs) means a cog can
now fail to load with a clear, actionable message instead of a silent skip —
but "fix the missing package, then restart the whole bot" is still a heavy
loop for something as small as a missing pip package. This cog closes that
loop: fix the dependency, then /dev reload <extension>, no restart needed.

Ownership check
────────────────
app_commands has no built-in equivalent of commands.is_owner() — that's a
prefix-command-only check. DevGroup overrides interaction_check once at the
Group level (rather than repeating a decorator on every command) so every
subcommand is gated identically. A failing check raises app_commands.CheckFailure,
which BotTree.on_error already renders as "🚫 Check Failed" — no new error
handling needed here.

Extension name resolution
───────────────────────────
Mirrors the discovery logic in bot.py's setup_hook(): walks bot/cogs/ for
either a package (dir with __init__.py) or a standalone .py module, and maps
each to its "cogs.<name>" extension string. Used for autocomplete on every
command that takes an `extension` parameter.

Reload safety
─────────────
discord.py's Bot.reload_extension() automatically restores the previous
module state if the new load fails (it snapshots sys.modules before
attempting the reload), so a bad edit doesn't leave the extension unloaded —
this cog surfaces the resulting ExtensionError but doesn't need to hand-roll
any rollback itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Client, Emojis

log = logging.getLogger(__name__)


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _ok(text: str) -> discord.Embed:
    return discord.Embed(description=f"{Emojis.confirmation}  {text}", colour=discord.Colour.green())


def _err(text: str) -> discord.Embed:
    return discord.Embed(description=f"{Emojis.cross_mark}  {text}", colour=discord.Colour.red())


# ── Extension discovery (mirrors bot.py's setup_hook loader) ────────────────

def _discover_extensions() -> list[str]:
    """Return every loadable extension string under bot/cogs/, sorted.

    Kept in sync with the discovery logic in bot.py's setup_hook — both walk
    the same directory with the same package-vs-module rules, so an
    extension visible to one is visible to the other.
    """
    cogs_dir = Path(__file__).parents[1]  # bot/cogs/Dev/__init__.py -> bot/cogs/
    extensions: list[str] = []
    for path in sorted(cogs_dir.iterdir()):
        if path.is_dir() and (path / "__init__.py").exists():
            extensions.append(f"cogs.{path.name}")
        elif path.suffix == ".py" and not path.name.startswith("_"):
            extensions.append(f"cogs.{path.stem}")
    return extensions


# ── Owner-gated command group ────────────────────────────────────────────────

class DevGroup(app_commands.Group):
    """Every subcommand shares one owner-only gate via this override, instead
    of decorating each command individually."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await interaction.client.is_owner(interaction.user):  # type: ignore[attr-defined]
            raise app_commands.CheckFailure("This command is restricted to the bot owner.")
        return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class Dev(commands.Cog, description="Owner-only extension management."):
    COG_EMOJI = "🛠️"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _extension_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        all_ext = _discover_extensions()
        loaded = set(self.bot.extensions.keys())
        matches = [ext for ext in all_ext if current.lower() in ext.lower()] if current else all_ext
        return [
            app_commands.Choice(name=f"{ext}{' (loaded)' if ext in loaded else ''}"[:100], value=ext)
            for ext in matches[:25]
        ]

    dev_grp = DevGroup(name="dev", description="Owner-only extension management commands.")

    # ── /dev extensions ───────────────────────────────────────────────────────

    @dev_grp.command(name="extensions", description="List all discovered extensions and their load state.")
    async def dev_extensions(self, interaction: discord.Interaction) -> None:
        all_ext = _discover_extensions()
        loaded = set(self.bot.extensions.keys())

        lines = [
            f"{Emojis.confirmation if ext in loaded else Emojis.cross_mark} `{ext}`"
            for ext in all_ext
        ]
        embed = discord.Embed(
            title="🛠️ Extensions",
            description="\n".join(lines) or "*No extensions found.*",
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{len(loaded)}/{len(all_ext)} loaded")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /dev load ─────────────────────────────────────────────────────────────

    @dev_grp.command(name="load", description="Load an extension that isn't currently loaded.")
    @app_commands.describe(extension="The extension to load.")
    @app_commands.autocomplete(extension=_extension_autocomplete)
    async def dev_load(self, interaction: discord.Interaction, extension: str) -> None:
        if extension in self.bot.extensions:
            await interaction.response.send_message(
                embed=_err(f"`{extension}` is already loaded — use `/dev reload` instead."), ephemeral=True
            )
            return
        try:
            await self.bot.load_extension(extension)
        except commands.ExtensionError as exc:
            log.warning("Dev: failed to load %s: %s", extension, exc)
            await interaction.response.send_message(
                embed=_err(f"Failed to load `{extension}`:\n```py\n{exc}\n```"), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=_ok(f"Loaded `{extension}`."), ephemeral=True)

    # ── /dev unload ───────────────────────────────────────────────────────────

    @dev_grp.command(name="unload", description="Unload a currently-loaded extension.")
    @app_commands.describe(extension="The extension to unload.")
    @app_commands.autocomplete(extension=_extension_autocomplete)
    async def dev_unload(self, interaction: discord.Interaction, extension: str) -> None:
        if extension == f"cogs.{Path(__file__).parent.name}":
            await interaction.response.send_message(
                embed=_err("Refusing to unload the Dev cog itself — you'd lose access to `/dev load` to bring it back."),
                ephemeral=True,
            )
            return
        if extension not in self.bot.extensions:
            await interaction.response.send_message(
                embed=_err(f"`{extension}` isn't currently loaded."), ephemeral=True
            )
            return
        try:
            await self.bot.unload_extension(extension)
        except commands.ExtensionError as exc:
            log.warning("Dev: failed to unload %s: %s", extension, exc)
            await interaction.response.send_message(
                embed=_err(f"Failed to unload `{extension}`:\n```py\n{exc}\n```"), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=_ok(f"Unloaded `{extension}`."), ephemeral=True)

    # ── /dev reload ───────────────────────────────────────────────────────────

    @dev_grp.command(name="reload", description="Reload a single extension (e.g. after a code fix).")
    @app_commands.describe(extension="The extension to reload.")
    @app_commands.autocomplete(extension=_extension_autocomplete)
    async def dev_reload(self, interaction: discord.Interaction, extension: str) -> None:
        if extension not in self.bot.extensions:
            await interaction.response.send_message(
                embed=_err(f"`{extension}` isn't loaded — use `/dev load` instead."), ephemeral=True
            )
            return
        try:
            await self.bot.reload_extension(extension)
        except commands.ExtensionError as exc:
            # discord.py restores the previous working module on a failed
            # reload, so the extension stays usable even though this
            # particular attempt failed.
            log.warning("Dev: failed to reload %s: %s", extension, exc)
            await interaction.response.send_message(
                embed=_err(
                    f"Failed to reload `{extension}` — the previous version is still active:\n"
                    f"```py\n{exc}\n```"
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=_ok(f"Reloaded `{extension}`."), ephemeral=True)

    # ── /dev reload-all ───────────────────────────────────────────────────────

    @dev_grp.command(name="reload-all", description="Reload every currently-loaded extension.")
    async def dev_reload_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        loaded = sorted(self.bot.extensions.keys())
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []

        for ext in loaded:
            try:
                await self.bot.reload_extension(ext)
                succeeded.append(ext)
            except commands.ExtensionError as exc:
                failed.append((ext, str(exc)))
                log.warning("Dev: failed to reload %s during reload-all: %s", ext, exc)

        embed = discord.Embed(
            title="🛠️ Reload All",
            colour=discord.Colour.green() if not failed else discord.Colour.orange(),
        )
        embed.add_field(
            name=f"{Emojis.confirmation} Succeeded ({len(succeeded)})",
            value="\n".join(f"`{e}`" for e in succeeded) or "*None.*",
            inline=False,
        )
        if failed:
            embed.add_field(
                name=f"{Emojis.cross_mark} Failed ({len(failed)})",
                value="\n".join(f"`{e}` — {msg[:150]}" for e, msg in failed)[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /dev sync ─────────────────────────────────────────────────────────────

    @dev_grp.command(name="sync", description="Resync the slash-command tree.")
    @app_commands.describe(scope="Sync to this guild only (instant) or globally (~1hr propagation).")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This guild (instant)", value="guild"),
            app_commands.Choice(name="Global (~1hr propagation)", value="global"),
        ]
    )
    async def dev_sync(self, interaction: discord.Interaction, scope: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True)

        if scope.value == "guild":
            if interaction.guild is None:
                await interaction.followup.send(embed=_err("Run this in a guild to sync locally."), ephemeral=True)
                return
            self.bot.tree.copy_global_to(guild=interaction.guild)
            synced = await self.bot.tree.sync(guild=interaction.guild)
            await interaction.followup.send(
                embed=_ok(f"Synced **{len(synced)}** command(s) to **{interaction.guild.name}**."), ephemeral=True
            )
        else:
            synced = await self.bot.tree.sync()
            await interaction.followup.send(
                embed=_ok(f"Synced **{len(synced)}** command(s) globally (propagation may take up to an hour)."),
                ephemeral=True,
            )


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dev(bot))