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
from util.constants import Emojis
from config import settings
from discord import app_commands
from discord.ext import commands
from util.db.repositories import Repositories
from util.constants import Emojis
from util.music import connect_nodes

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


# ── Shared (title, description) describers ─────────────────────────────────────
# Both the pure-slash error path (BotTree.on_error, for app_commands.Command)
# and the prefix/hybrid error path (DiscordBot.on_command_error, for
# commands.ext Command / HybridCommand) end up needing to turn an exception
# into a user-facing (title, description) pair. The two exception hierarchies
# (app_commands.AppCommandError vs commands.CommandError) are unrelated, so
# they get their own describer each — but a hybrid command invoked as a slash
# command raises commands.HybridCommandError wrapping the *app_commands*
# error, so on_command_error calls back into describe_app_command_error for
# that case instead of duplicating the mapping.

def describe_app_command_error(error: app_commands.AppCommandError) -> tuple[str, str]:
    """Return (title, description) for an app_commands error, most-specific first."""

    if isinstance(error, app_commands.MissingPermissions):
        perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
        return f"{Emojis.prohibited} Missing Permissions", f"You need the {perms} permission(s) to use this command."

    if isinstance(error, app_commands.BotMissingPermissions):
        perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
        return f"{Emojis.prohibited} I'm Missing Permissions", f"I need the {perms} permission(s) to do that."

    if isinstance(error, app_commands.CommandOnCooldown):
        return (
            f"{Emojis.alarm_clock} On Cooldown",
            f"This command is on cooldown — try again in **{error.retry_after:.1f}s**.",
        )

    if isinstance(error, app_commands.MissingRole):
        role = error.missing_role
        role_str = f"<@&{role}>" if isinstance(role, int) else f"`{role}`"
        return f"{Emojis.prohibited} Missing Role", f"You need the {role_str} role to use this command."

    if isinstance(error, app_commands.MissingAnyRole):
        roles = ", ".join(
            f"<@&{r}>" if isinstance(r, int) else f"`{r}`" for r in error.missing_roles
        )
        return f"{Emojis.prohibited} Missing Role", f"You need at least one of these roles: {roles}."

    if isinstance(error, app_commands.NoPrivateMessage):
        return f"{Emojis.prohibited} Server Only", "This command can't be used in DMs — try it in a server."

    if isinstance(error, app_commands.CommandNotFound):
        # Rare for app_commands (Discord usually filters stale commands
        # client-side), but can surface right after a command is renamed
        # and the tree hasn't finished re-syncing everywhere yet.
        return (
            f"{Emojis.question_mark} Command Not Found",
            "This command no longer exists — it may have just been renamed or removed.",
        )

    if isinstance(error, app_commands.TransformerError):
        return (
            f"{Emojis.warning} Invalid Input",
            f"Couldn't convert `{error.value}` to the expected type (`{error.type.name}`).",
        )

    if isinstance(error, app_commands.CommandInvokeError):
        # The real exception is `.original` — unwrap it here instead of
        # showing the user the wrapper's generic repr.
        return (
            f"{Emojis.decline} Error",
            f"Something went wrong while running this command:\n```py\n{error.original}\n```",
        )

    if isinstance(error, app_commands.CheckFailure):
        # Generic fallback for custom `@app_commands.check(...)` predicates
        # that don't raise one of the more specific subclasses above.
        return f"{Emojis.prohibited} Check Failed", str(error) or "You don't meet the requirements to use this command."

    # Anything else — CommandSignatureMismatch, translation errors, etc.
    return f"{Emojis.decline} Error", f"An unexpected error occurred:\n```py\n{error}\n```"


def describe_command_error(error: commands.CommandError) -> tuple[str, str]:
    """Return (title, description) for a commands.ext error, most-specific first.

    Mirrors describe_app_command_error, but for the completely separate
    commands.CommandError hierarchy used by prefix commands and by hybrid
    commands when invoked as text. Ordering follows the same rule: subclasses
    before their parents (e.g. BadUnionArgument before BadArgument,
    MissingPermissions before CheckFailure) or the parent's branch would
    swallow the more specific one.
    """

    # ── UserInputError family ────────────────────────────────────────────────
    if isinstance(error, commands.MissingRequiredArgument):
        return (
            f"{Emojis.warning} Missing Argument",
            f"You're missing the `{error.param.name}` argument.\n"
            f"Check the command's help for the correct usage.",
        )

    if isinstance(error, commands.MissingRequiredAttachment):
        return (
            f"{Emojis.warning} Missing Attachment",
            f"You need to attach a file for the `{error.param.name}` argument.",
        )

    if isinstance(error, commands.TooManyArguments):
        return f"{Emojis.warning} Too Many Arguments", "You passed too many arguments to this command."

    if isinstance(error, commands.BadUnionArgument):
        types_str = ", ".join(t.__name__ for t in error.converters)
        return (
            f"{Emojis.warning} Invalid Input",
            f"Couldn't convert `{error.param.name}` to any of: {types_str}.",
        )

    if isinstance(error, commands.BadLiteralArgument):
        options = ", ".join(f"`{a}`" for a in error.literals)
        return (
            f"{Emojis.warning} Invalid Input",
            f"`{error.param.name}` must be one of: {options}.",
        )

    if isinstance(error, (commands.MissingFlagArgument, commands.TooManyFlags, commands.MissingRequiredFlag, commands.BadFlagArgument)):
        return f"{Emojis.warning} Invalid Flags", str(error)

    if isinstance(error, commands.BadArgument):
        # Catches ChannelNotFound, RoleNotFound, MemberNotFound, BadColourArgument, etc.
        return f"{Emojis.warning} Invalid Input", str(error)

    if isinstance(error, commands.ConversionError):
        # Raised when a converter itself raises something other than
        # BadArgument — .original is the real underlying exception.
        return f"{Emojis.warning} Invalid Input", f"Couldn't process one of your arguments: {error.original}"

    # ── CheckFailure family (most specific first) ───────────────────────────
    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
        return f"{Emojis.prohibited} Missing Permissions", f"You need the {perms} permission(s) to use this command."

    if isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
        return f"{Emojis.prohibited} I'm Missing Permissions", f"I need the {perms} permission(s) to do that."

    if isinstance(error, commands.MissingRole):
        role = error.missing_role
        role_str = f"<@&{role}>" if isinstance(role, int) else f"`{role}`"
        return f"{Emojis.prohibited} Missing Role", f"You need the {role_str} role to use this command."

    if isinstance(error, commands.BotMissingRole):
        role = error.missing_role
        role_str = f"<@&{role}>" if isinstance(role, int) else f"`{role}`"
        return f"{Emojis.prohibited} I'm Missing a Role", f"I need the {role_str} role to do that."

    if isinstance(error, commands.MissingAnyRole):
        roles = ", ".join(
            f"<@&{r}>" if isinstance(r, int) else f"`{r}`" for r in error.missing_roles
        )
        return f"{Emojis.prohibited} Missing Role", f"You need at least one of these roles: {roles}."

    if isinstance(error, commands.BotMissingAnyRole):
        roles = ", ".join(
            f"<@&{r}>" if isinstance(r, int) else f"`{r}`" for r in error.missing_roles
        )
        return f"{Emojis.prohibited} I'm Missing a Role", f"I need at least one of these roles: {roles}."

    if isinstance(error, commands.NotOwner):
        return f"{Emojis.prohibited} Owner Only", "Only the bot owner can use this command."

    if isinstance(error, commands.PrivateMessageOnly):
        return f"{Emojis.prohibited} DM Only", "This command can only be used in DMs."

    if isinstance(error, commands.NoPrivateMessage):
        return f"{Emojis.prohibited} Server Only", "This command can't be used in DMs — try it in a server."

    if isinstance(error, commands.NSFWChannelRequired):
        return f"{Emojis.nsfw_emoji} NSFW Only", "This command can only be used in an NSFW channel."

    # ── Rate limiting / concurrency ──────────────────────────────────────────
    if isinstance(error, commands.CommandOnCooldown):
        return (
            f"{Emojis.alarm_clock} On Cooldown",
            f"This command is on cooldown — try again in **{error.retry_after:.1f}s**.",
        )

    if isinstance(error, commands.MaxConcurrencyReached):
        plural = "time" if error.number == 1 else "times"
        return (
            f"{Emojis.traffic_signal} Slow Down",
            f"This command can only be used **{error.number}** {plural} at once per "
            f"{error.per.name} — wait for it to finish first.",
        )

    if isinstance(error, commands.DisabledCommand):
        return f"{Emojis.prohibited} Disabled", "This command is currently disabled."

    # ── Invocation-time failures ─────────────────────────────────────────────
    if isinstance(error, commands.CommandInvokeError):
        # The real exception is `.original` — unwrap it here instead of
        # showing the user the wrapper's generic repr.
        return (
            f"{Emojis.decline} Error",
            f"Something went wrong while running this command:\n```py\n{error.original}\n```",
        )

    if isinstance(error, commands.CheckAnyFailure):
        return f"{Emojis.prohibited} Check Failed", "You don't meet the requirements to use this command."

    if isinstance(error, commands.CheckFailure):
        # Generic fallback for custom `@commands.check(...)` predicates that
        # don't raise one of the more specific subclasses above.
        return f"{Emojis.prohibited} Check Failed", str(error) or "You don't meet the requirements to use this command."

    # Anything else — ExtensionError, ArgumentParsingError, etc.
    return f"{Emojis.decline} Error", f"An unexpected error occurred:\n```py\n{error}\n```"


# ── Custom CommandTree — centralises slash-command error handling ──────────────
class BotTree(app_commands.CommandTree):
    """
    Subclassing CommandTree is the discord.py 2.x equivalent of
    nextcord's on_application_command_error event.

    Dispatch order matters: subclasses must be checked before their parents
    (e.g. MissingRole is a subclass of CheckFailure — if CheckFailure were
    checked first, MissingRole would never be reached). isinstance checks
    below are ordered most-specific-first for exactly this reason.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        title, description = describe_app_command_error(error)

        embed = discord.Embed(title=title, description=description, colour=0xFF5733)
        embed.set_footer(text=f"Requested by {interaction.user}")

        # Unwrap CommandInvokeError to log the real traceback, not the wrapper.
        if isinstance(error, app_commands.CommandInvokeError):
            log.error(
                "Unhandled error in /%s",
                interaction.command.qualified_name if interaction.command else "?",
                exc_info=error.original,
            )
        elif not isinstance(
            error,
            (
                app_commands.MissingPermissions,
                app_commands.BotMissingPermissions,
                app_commands.CommandOnCooldown,
                app_commands.MissingRole,
                app_commands.MissingAnyRole,
                app_commands.NoPrivateMessage,
                app_commands.TransformerError,
            ),
        ):
            # Anything outside the "expected, user-facing" set is worth a
            # server-side log line even though it's still shown to the user.
            log.warning("Unhandled AppCommandError type: %r", error)

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

        # 5. Music node connection ───────────────────────────────────────────────
        await connect_nodes(self)
        log.info("Lavalink node connection initiated.")

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
        """
        Central error handler for every "normal" command — text-prefix,
        pure-slash-via-hybrid-wrapper, and hybrid commands invoked either
        way all funnel through here as long as they're commands.ext Commands
        (registered with @commands.command / @commands.hybrid_command),
        as opposed to pure app_commands.Command objects registered directly
        on the tree, which go through BotTree.on_error instead.

        ctx.send(...) is used throughout rather than branching on
        ctx.interaction: Context.send() already knows how to route to
        interaction.response / interaction.followup when the command was
        invoked as a slash command, and falls back to a normal channel
        message otherwise — including honouring `ephemeral=True` only when
        there's actually an interaction to be ephemeral on.
        """

        # Silently ignore unknown commands — avoids spam when bots interact,
        # or a message just happens to start with the prefix by coincidence.
        if isinstance(error, commands.CommandNotFound):
            return

        # A hybrid command invoked as a slash command raises its errors
        # wrapped in HybridCommandError, with `.original` holding the real
        # app_commands.AppCommandError (e.g. a TransformerError from bad
        # slash-option input, or that command's own CheckFailure). Reuse the
        # app-command describer for those instead of falling through to the
        # commands.ext one, which wouldn't recognise the wrapped type.
        if isinstance(error, commands.HybridCommandError):
            title, description = describe_app_command_error(error.original)
            log_error = (
                error.original
                if isinstance(error.original, app_commands.CommandInvokeError)
                else None
            )
        elif isinstance(error, commands.CommandInvokeError):
            title, description = describe_command_error(error)
            log_error = error.original
        else:
            title, description = describe_command_error(error)
            log_error = None

        if log_error is not None:
            log.error(
                "Unhandled error in %s",
                ctx.command.qualified_name if ctx.command else "?",
                exc_info=log_error,
            )
        elif not isinstance(
            error,
            (
                commands.MissingPermissions,
                commands.BotMissingPermissions,
                commands.CommandOnCooldown,
                commands.MissingRole,
                commands.MissingAnyRole,
                commands.NoPrivateMessage,
                commands.UserInputError,
                commands.CheckFailure,
            ),
        ):
            # Anything outside the "expected, user-facing" set is worth a
            # server-side log line even though it's still shown to the user.
            log.warning("Unhandled CommandError type: %r", error)

        embed = discord.Embed(title=title, description=description, colour=0xFF5733)
        embed.set_footer(text=f"Requested by {ctx.author}")

        try:
            await ctx.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            # Interaction token expired / message send failed for some other
            # reason — don't let error-handling itself raise an unhandled
            # exception up into the event loop's default handler.
            log.warning(
                "Failed to deliver error message for %s",
                ctx.command.qualified_name if ctx.command else "?",
            )

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