"""
Help cog — native discord.py UI replacing discord-ext-menus pagination.

Design
──────
Pure introspection over `self.bot` — no database dependency.

  /help                → category overview (HelpView: Select + Delete button)
  /help command:<name> → detailed embed for one command (autocompleted)
  helpme / h            → hybrid prefix alias for the overview, for parity
                          with prefix-command users

Dedup strategy for hybrid commands
───────────────────────────────────
`Cog.walk_commands()` yields prefix/HybridCommand objects; `Cog.get_app_commands()`
yields pure app_commands.Command/Group objects — and for any HybridCommand also
registers an auto-generated app-command view with the SAME qualified_name.
`_list_cog_commands` walks prefix commands FIRST (so hybrids are represented by
their richer HybridCommand/HybridGroup object, tagged `[hybrid]`), then adds only
the pure app-commands not already covered, tagged `[slash]`. Anything left over
from `walk_commands()` that isn't hybrid is tagged `[prefix]`.

Visibility filtering
─────────────────────
Unlike `commands.MinimalHelpCommand.filter_commands`, discord.py gives us no
built-in equivalent for a from-scratch help UI, so `_can_run` reimplements the
important bits: commands flagged `hidden=True` are always excluded, and any
command's `.checks` are evaluated against the invoker (via a `Context` built
with `Bot.get_context()` for prefix/hybrid commands, or the raw `Interaction`
for pure app_commands checks) so permission-gated commands (owner-only, etc.)
don't leak into the menu for people who can't run them. Visibility is computed
once per invocation and shared between the overview embed and the category
Select so the two never disagree. When a check can't be evaluated (e.g. a
pure-slash check tested from a prefix invocation with no Interaction), we
fail open rather than silently hiding commands the caller might genuinely be
allowed to run.

Autocomplete
────────────
`command=` is fuzzy-matched (via util.docs.fuzzy.finder, same engine the Docs
cog uses) against a flattened `{qualified_name: command}` map built lazily on
first use and cached for the process lifetime — the command tree is static
once `setup_hook` finishes syncing. Autocomplete only strips `hidden` commands
(cheap, sync); the full permission check runs once, on resolution, so a
command the user can't run is reported as "not found" rather than confirming
it exists.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Emojis
from util.docs.fuzzy import finder

AnyCommand = app_commands.Command | app_commands.Group | commands.Command


# ── Introspection helpers ────────────────────────────────────────────────────

def _list_cog_commands(cog: commands.Cog) -> list[AnyCommand]:
    """All commands owned by *cog*, deduped so hybrids appear exactly once."""
    seen: set[str] = set()
    result: list[AnyCommand] = []

    # Prefix / hybrid commands first — HybridCommand carries the richest info
    # (aliases, signature) and lets us tag it correctly below.
    for cmd in cog.walk_commands():
        if cmd.qualified_name in seen:
            continue
        result.append(cmd)
        seen.add(cmd.qualified_name)

    # Pure slash commands/groups not already covered by a hybrid above.
    for cmd in cog.get_app_commands():
        if cmd.qualified_name in seen:
            continue
        result.append(cmd)
        seen.add(cmd.qualified_name)

    result.sort(key=lambda c: c.qualified_name)
    return result


def _command_tag(cmd: AnyCommand) -> str:
    if isinstance(cmd, (commands.HybridCommand, commands.HybridGroup)):
        return " `[hybrid]`"
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        return " `[slash]`"
    return " `[prefix]`"


def _command_signature(cmd: AnyCommand) -> str:
    if isinstance(cmd, (commands.HybridCommand, commands.HybridGroup)):
        return f"/{cmd.qualified_name}"
    if isinstance(cmd, app_commands.Group):
        return f"/{cmd.qualified_name} <subcommand>"
    if isinstance(cmd, app_commands.Command):
        return f"/{cmd.qualified_name}"
    return f"{cmd.qualified_name} {cmd.signature}".strip()


def _command_description(cmd: AnyCommand) -> str:
    desc = getattr(cmd, "description", None)
    if desc:
        return desc
    return getattr(cmd, "help", None) or getattr(cmd, "short_doc", None) or "*No description provided.*"


def _command_cog(cmd: AnyCommand) -> commands.Cog | None:
    """Best-effort cog lookup across app_commands.Command and commands.Command."""
    return getattr(cmd, "binding", None) or getattr(cmd, "cog", None)


async def _can_run(
    cmd: AnyCommand,
    *,
    ctx: commands.Context | None,
    interaction: discord.Interaction | None,
) -> bool:
    """Best-effort visibility filter for the help UI.

    Always excludes commands flagged `hidden=True`. Otherwise evaluates the
    command's checks against the invoker where we can, and fails open (shows
    the command) when we genuinely can't evaluate — see module docstring.
    """
    if getattr(cmd, "hidden", False):
        return False

    try:
        if isinstance(cmd, commands.Command):  # covers HybridCommand/HybridGroup too
            if ctx is None:
                return True
            return await cmd.can_run(ctx)

        if isinstance(cmd, app_commands.Group):
            # Groups have no .checks of their own (only individual Commands do);
            # a Group can only gate access via an overridden interaction_check.
            if interaction is None:
                return True
            checker = getattr(cmd, "interaction_check", None)
            if checker is None:
                return True
            result = checker(interaction)
            if hasattr(result, "__await__"):
                result = await result
            return bool(result)

        if isinstance(cmd, app_commands.Command):
            if interaction is None or not cmd.checks:
                return True
            for check in cmd.checks:
                result = check(interaction)
                if hasattr(result, "__await__"):
                    result = await result
                if not result:
                    return False
            return True
    except (commands.CommandError, app_commands.AppCommandError, discord.DiscordException):
        return False

    return True


async def _visible_commands_by_cog(
    bot: commands.Bot,
    *,
    ctx: commands.Context | None,
    interaction: discord.Interaction | None,
) -> dict[str, list[AnyCommand]]:
    """cog_name → commands the invoker can actually see/run, for cogs that opt into help."""
    visible: dict[str, list[AnyCommand]] = {}
    for cog_name, cog in bot.cogs.items():
        if getattr(cog, "COG_EMOJI", None) is None:
            continue
        cmds = [
            cmd for cmd in _list_cog_commands(cog)
            if await _can_run(cmd, ctx=ctx, interaction=interaction)
        ]
        if cmds:
            visible[cog_name] = cmds
    return visible


# ── Embed builders ────────────────────────────────────────────────────────────

def _overview_embed(
    visible: dict[str, list[AnyCommand]], bot: commands.Bot, *, requester: discord.abc.User
) -> discord.Embed:
    embed = discord.Embed(
        title="📖 Help Menu",
        description=(
            "Use the select menu below to browse commands by category.\n"
            "Run `/help command:<name>` for details on a specific command."
        ),
        colour=discord.Colour.blurple(),
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    for cog_name in sorted(visible):
        cog = bot.cogs.get(cog_name)
        emoji = getattr(cog, "COG_EMOJI", "") if cog else ""
        cmds = visible[cog_name]
        embed.add_field(
            name=f"{emoji} {cog_name}".strip(),
            value=f"{(cog.description if cog else None) or '*No description.*'}\n`{len(cmds)}` command(s)",
            inline=True,
        )

    if not visible:
        embed.add_field(
            name="No commands available",
            value="*You don't currently have access to any commands here.*",
            inline=False,
        )

    embed.set_footer(text=f"Requested by {requester}")
    return embed


def _cog_embed(
    cog: commands.Cog, cmds: list[AnyCommand], *, requester: discord.abc.User
) -> discord.Embed:
    emoji = getattr(cog, "COG_EMOJI", "📁")
    embed = discord.Embed(
        title=f"{emoji} {cog.qualified_name}",
        description=cog.description or "*No description.*",
        colour=discord.Colour.blurple(),
    )

    if not cmds:
        embed.add_field(name="Commands", value="*No commands available to you.*", inline=False)
    else:
        for cmd in cmds:
            embed.add_field(
                name=f"`{_command_signature(cmd)}`{_command_tag(cmd)}",
                value=_command_description(cmd)[:100],
                inline=False,
            )

    embed.set_footer(text=f"Requested by {requester} • Use /help command:<name> for full details")
    return embed


def _command_detail_embed(cmd: AnyCommand, *, requester: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(
        title=f"`{_command_signature(cmd)}`{_command_tag(cmd)}",
        description=_command_description(cmd),
        colour=discord.Colour.blurple(),
    )

    if isinstance(cmd, app_commands.Group):
        subs = "\n".join(
            f"`/{sub.qualified_name}` — {sub.description or 'No description.'}"
            for sub in cmd.commands
        )
        embed.add_field(name="Subcommands", value=subs or "*None.*", inline=False)

    elif isinstance(cmd, app_commands.Command):
        if cmd.parameters:
            lines: list[str] = []
            for param in cmd.parameters:
                req = "required" if param.required else "optional"
                lines.append(f"`{param.name}` ({req}) — {param.description or 'No description.'}")
                if param.choices:
                    lines.append(f"　└ Options: {', '.join(c.name for c in param.choices)}")
            embed.add_field(name="Parameters", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Parameters", value="*None.*", inline=False)

    else:  # commands.Command / HybridCommand / HybridGroup (prefix-capable)
        if isinstance(cmd, commands.Group):
            subs = "\n".join(
                f"`{sub.qualified_name}` — {_command_description(sub)}" for sub in cmd.commands
            )
            embed.add_field(name="Subcommands", value=subs or "*None.*", inline=False)
        elif cmd.clean_params:
            lines = []
            for name, param in cmd.clean_params.items():
                required = param.default is param.empty
                lines.append(f"`{name}` ({'required' if required else 'optional'})")
            embed.add_field(name="Parameters", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Parameters", value="*None.*", inline=False)

        if getattr(cmd, "aliases", None):
            embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)

    cog = _command_cog(cmd)
    if cog is not None:
        embed.add_field(
            name="Category",
            value=f"{getattr(cog, 'COG_EMOJI', '')} {cog.qualified_name}".strip(),
            inline=True,
        )

    embed.set_footer(text=f"Requested by {requester}")
    return embed


# ── Shared components ─────────────────────────────────────────────────────────

class DeleteButton(discord.ui.Button):
    def __init__(self, *, row: int = 1) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, emoji=Emojis.trashcan, label="Delete", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.message.delete()


class HelpSelect(discord.ui.Select):
    """One option per visible cog, plus a Home option to return to the overview."""

    def __init__(self, bot: commands.Bot, visible: dict[str, list[AnyCommand]]) -> None:
        self.bot = bot
        options = [
            discord.SelectOption(label="Home", value="__home__", emoji="🏠", description="Back to the overview")
        ]
        for cog_name in sorted(visible):
            cog = bot.cogs.get(cog_name)
            emoji = getattr(cog, "COG_EMOJI", None) if cog else None
            options.append(
                discord.SelectOption(
                    label=cog_name,
                    value=cog_name,
                    emoji=emoji,
                    description=(cog.description[:100] if cog and cog.description else None),
                )
            )
        super().__init__(placeholder="Browse a category…", options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        view: HelpView = self.view  # type: ignore[assignment]

        if value == "__home__":
            embed = _overview_embed(view.visible, self.bot, requester=interaction.user)
        else:
            cog = self.bot.cogs.get(value)
            cmds = view.visible.get(value)
            if cog is None or not cmds:
                await interaction.response.send_message(
                    f"{Emojis.warning} That category is no longer available.", ephemeral=True
                )
                return
            embed = _cog_embed(cog, cmds, requester=interaction.user)

        await interaction.response.edit_message(embed=embed, view=view)


class HelpView(discord.ui.View):
    """Select (row 0) + Delete button (row 1). Restricted to the invoker; 180s timeout."""

    def __init__(self, bot: commands.Bot, visible: dict[str, list[AnyCommand]], *, author_id: int) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.visible = visible
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.add_item(HelpSelect(bot, visible))
        self.add_item(DeleteButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"{Emojis.prohibited} This help menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Cog ────────────────────────────────────────────────────────────────────────

class Help(commands.Cog, description="Browse commands and get detailed usage help."):
    COG_EMOJI = Emojis.book

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Built lazily on first use — the command tree is static per-process
        # once setup_hook's tree.sync() has run, so this never needs invalidating.
        # NOTE: this is the raw (unfiltered) tree, used only for name lookup and
        # autocomplete — per-user visibility is computed separately in _can_run.
        self._flat_cache: dict[str, AnyCommand] | None = None

    def _flatten_commands(self) -> dict[str, AnyCommand]:
        if self._flat_cache is not None:
            return self._flat_cache

        flat: dict[str, AnyCommand] = {}
        for cog in self.bot.cogs.values():
            for cmd in _list_cog_commands(cog):
                flat[cmd.qualified_name] = cmd
                if isinstance(cmd, (app_commands.Group, commands.Group)):
                    for sub in cmd.commands:
                        flat[sub.qualified_name] = sub

        self._flat_cache = flat
        return flat

    async def _command_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        flat = self._flatten_commands()
        candidates = [name for name, cmd in flat.items() if not getattr(cmd, "hidden", False)]
        if not current:
            sample = candidates[:25]
        else:
            sample = finder(current, candidates, lazy=False)[:25]
        return [app_commands.Choice(name=name[:100], value=name[:100]) for name in sample]

    # ── /help ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="Browse bot commands, or view details for a specific one.")
    @app_commands.describe(command="A specific command to look up (optional) — start typing to search.")
    @app_commands.autocomplete(command=_command_autocomplete)
    async def help_(self, interaction: discord.Interaction, command: str | None = None) -> None:
        ctx = await self.bot.get_context(interaction)

        if command:
            flat = self._flatten_commands()
            target = flat.get(command)
            if target is None:
                # Free-text fallback if the user bypassed autocomplete.
                matches = finder(command, flat.keys(), lazy=False)
                target = flat.get(matches[0]) if matches else None
            if target is None or not await _can_run(target, ctx=ctx, interaction=interaction):
                # Same message whether the command is unknown or just hidden
                # from this user — don't confirm existence of gated commands.
                await interaction.response.send_message(
                    f"{Emojis.warning} No command found matching `{command}`.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                embed=_command_detail_embed(target, requester=interaction.user)
            )
            return

        visible = await _visible_commands_by_cog(self.bot, ctx=ctx, interaction=interaction)
        view = HelpView(self.bot, visible, author_id=interaction.user.id)
        embed = _overview_embed(visible, self.bot, requester=interaction.user)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    # ── helpme / h — prefix parity alias ────────────────────────────────────────

    @commands.hybrid_command(name="helpme", aliases=["h"], description="Browse bot commands (alias for /help).")
    async def helpme(self, ctx: commands.Context) -> None:
        visible = await _visible_commands_by_cog(self.bot, ctx=ctx, interaction=ctx.interaction)
        view = HelpView(self.bot, visible, author_id=ctx.author.id)
        embed = _overview_embed(visible, self.bot, requester=ctx.author)
        view.message = await ctx.send(embed=embed, view=view)


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))