"""
bot/bot.py
──────────
Main entry point.

discord.py 2.x architecture changes vs the old nextcord bot:
  • Subclass commands.AutoShardedBot instead of instantiating it directly.
  • All async setup (DB pool, cog loading, tree sync) lives in setup_hook(),
    which runs after login but before the gateway READY event.
  • Cog loading is now async: await self.load_extension(...)
  • Slash-command errors are handled via a subclassed CommandTree (BotTree).
  • on_application_command_error is gone — use tree.on_error instead.
  • bot.pool replaces bot.db / bot.config (MongoDB Document).

Run:
    uv run python bot/bot.py
"""

import logging
import time
from pathlib import Path

import asyncpg
import discord
from bot.util.constants import Emojis
from config import settings
from discord import app_commands
from discord.ext import commands
from util.db.repositories import Repositories

log = logging.getLogger(__name__)

_DEFAULT_PREFIX = "t!"


# ── Prefix resolver ───────────────────────────────────────────────────────────

async def get_prefix(bot: "DiscordBot", message: discord.Message) -> list[str]:
    """
    Return the command prefix for the given message.

    DMs always use the default prefix.  In guilds, the prefix is fetched
    from the guild_config table; falls back to the default if not set or
    if the DB is temporarily unavailable.
    """
    if not message.guild:
        return commands.when_mentioned_or(_DEFAULT_PREFIX)(bot, message)

    try:
        prefix = await bot.db.config.get_prefix(message.guild.id)
    except Exception:
        prefix = _DEFAULT_PREFIX

    return commands.when_mentioned_or(prefix)(bot, message)


# ── Custom CommandTree — centralises slash-command error handling ──────────────

class BotTree(app_commands.CommandTree):
    """
    Subclassing CommandTree is the discord.py 2.x equivalent of
    nextcord's on_application_command_error event.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        embed = discord.Embed(
            title="❌ Error",
            description="An error occurred while running this command.",
            colour=0xFF5733,
        )
        embed.add_field(
            name="Details",
            value=f"```py\n{error}\n```",
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user}")

        # interaction.response may already be used if the error is deferred.
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Bot class ─────────────────────────────────────────────────────────────────

class DiscordBot(commands.AutoShardedBot):
    """
    The main bot class.

    Attributes set before any cog sees the bot:
        pool        asyncpg.Pool    — PostgreSQL connection pool
        start_time  float           — time.time() from on_ready
        bot_version str             — human-readable version string
    """

    pool: asyncpg.Pool  # annotated here; assigned in setup_hook
    db: Repositories    # assigned immediately after pool
    db: Repositories    # annotated here; assigned in setup_hook

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.emojis = True
        intents.emojis_and_stickers = True
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.presences = False  # we don't use presence data, so disable it.

        super().__init__(
            command_prefix=get_prefix,
            case_insensitive=True,
            activity=discord.Game(name="Please interact with me!"),
            intents=intents,
            # Plug our custom error handler in at construction time.
            tree_cls=BotTree,
        )

        self.start_time: float = 0.0
        self.bot_version: str = "5.0.0"

    # ── Async setup ───────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """
        Called by discord.py after login, before the gateway READY event.

        This is the correct place for:
          1. Async resource acquisition (DB pool).
          2. Cog / extension loading (now async in discord.py 2.x).
          3. Slash-command tree sync.

        Do NOT do any of this in on_ready — that event can fire multiple
        times on reconnect, which would duplicate pool connections and
        double-register slash commands.
        """

        # 1. PostgreSQL connection pool ───────────────────────────────────────
        self.pool = await asyncpg.create_pool(
            settings.asyncpg_dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        self.db = Repositories(self.pool)
        log.info("PostgreSQL pool + repositories ready.")

        self.db = Repositories(self.pool)

        # 2. Load application emojis and map them by name for easy access.
        app_emojis = await self.fetch_application_emojis()
        emoji_map = {e.name: str(e) for e in app_emojis}
        # Patch Emojis with resolved strings e.g. "<:dustbin:1234567890>"
        from util.constants import Emojis
        for attr in vars(Emojis):
            if not attr.startswith("_") and attr in emoji_map:
                setattr(Emojis, attr, emoji_map[attr])
        log.info("Loaded %d application emojis.", len(app_emojis))

        # 3. Extension / cog loading ──────────────────────────────────────────
        # Phase 5 will port each cog from nextcord to discord.py.
        # Until then, cogs still import nextcord and will fail to load.
        # Failures are caught individually so a single broken cog cannot
        # prevent the rest from loading.
        cwd = Path(__file__).parent
        for path in sorted((cwd / "cogs").iterdir()):
                if path.is_dir() and (path / "__init__.py").exists():
                    ext = f"cogs.{path.name}"
                elif path.suffix == ".py" and not path.name.startswith("_"):
                    ext = f"cogs.{path.stem}"
                else:
                    continue

                try:
                    await self.load_extension(ext)
                    log.info("Loaded  %s", ext)
                except Exception as exc:  # noqa: BLE001
                    # Expected until Phase 5 — cogs still use nextcord imports.
                    log.warning("Skipped %s: %s", ext, exc)

        # 4. Slash-command tree sync ──────────────────────────────────────────
        # Debug mode: sync to the dev guild for instant registration (no delay).
        # Production: global sync (Discord propagates within ~1 hour).
        if settings.bot_debug:
            from util.constants import Client  # avoid circular import at module level
            dev_guild = discord.Object(id=Client.guild_id)
            self.tree.copy_global_to(guild=dev_guild)
            await self.tree.sync(guild=dev_guild)
            log.info("Slash commands synced to dev guild %d.", Client.guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally.")

    # ── Gateway events ────────────────────────────────────────────────────────

    async def on_ready(self) -> None:
        """
        Fires when the bot has finished connecting to all shards.

        on_ready can fire more than once (e.g. on reconnect after a
        network drop).  Keep this handler lightweight and idempotent.
        """
        self.start_time = time.time()
        assert self.user is not None
        log.info(
            "Ready: %s (%d) | Guilds: %d | Shards: %d",
            self.user,
            self.user.id,
            len(self.guilds),
            self.shard_count or 1,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # ── Blacklist check ───────────────────────────────────────────────────
        # Direct query here; Phase 4 will move this into BlacklistRepository.
        try:
            if await self.db.blacklist.is_blacklisted(message.author.id):
                embed = discord.Embed(
                    title=f"{Emojis.prohibited} You are not allowed to use my commands.",
                    colour=0x00FFFF,
                )
                await message.author.send(embed=embed)
                return
        except Exception:
            # If DB is down, let the message through rather than blocking.
            pass

        # ── Mention-as-prefix: reply when the bot is @mentioned alone ─────────
        if (
            message.guild
            and self.user is not None
            and message.content.strip() == self.user.mention
        ):
            try:
                prefix = await self.db.config.get_prefix(message.guild.id)
            except Exception:
                prefix = _DEFAULT_PREFIX
            await message.channel.send(
                f"My prefix here is `{prefix}`", delete_after=20
            )

        await self.process_commands(message)

    async def on_command_error(
        self,
        ctx: commands.Context,  # type: ignore[type-arg]
        error: commands.CommandError,
    ) -> None:
        # Silently ignore unknown commands — avoids spam when bots interact.
        if isinstance(error, commands.CommandNotFound):
            return

        assert self.user is not None
        embed = discord.Embed(
            title=f"{Emojis.cross_mark} Error",
            description="An error occurred while running this command.",
            colour=0xFF5733,
        )
        embed.set_author(
            name=str(self.user),
            icon_url=self.user.display_avatar.url,
        )

        if isinstance(error, commands.MissingRequiredArgument):
            embed.add_field(
                name="Missing argument",
                value=f"```{error}```",
                inline=False,
            )
        elif isinstance(error, commands.MissingPermissions):
            embed.add_field(
                name="Missing permissions",
                value=f"```{error}```",
                inline=False,
            )
        else:
            embed.add_field(
                name="Details",
                value=f"```py\n{error}\n```",
                inline=False,
            )

        if ctx.author.avatar:
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.avatar.url,
            )

        await ctx.send(embed=embed)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Gracefully close the DB pool before disconnecting from Discord."""
        await super().close()
        if hasattr(self, "pool") and self.pool:
            await self.pool.close()
            log.info("PostgreSQL pool closed.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.bot_debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # discord.py's own log is very verbose at DEBUG; keep it at INFO unless
    # you're debugging the library internals.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    bot = DiscordBot()
    # log_handler=None: we've already configured logging above.
    bot.run(settings.bot_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    main()
