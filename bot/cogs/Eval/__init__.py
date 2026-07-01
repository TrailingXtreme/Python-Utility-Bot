# Location: bot/cogs/Eval/__init__.py
"""
Eval cog — two independent evaluation surfaces under one command group.

/eval run   [Owner Only]
    Opens EvalModal — a paragraph TextInput for arbitrary Python code.
    Code runs inside an async wrapper function with full access to the bot
    instance, discord.py, and the invoking interaction. stdout is captured;
    the wrapper's return value (if any) is stored in a session-persistent
    `_` variable, mirroring the classic REPL convention.

    Gated by app_commands.check(_is_owner_check), which calls
    bot.is_owner() — this correctly resolves team-owned applications, not
    just a single hardcoded user ID. A redundant check runs inside the
    modal's on_submit as defense-in-depth, since a modal is a distinct
    interaction from the command that spawned it.

    DANGER: this command grants full code-execution privileges over the
    bot's process. It must never be exposed to non-owners. There is no
    sandboxing on this path by design — that is the whole point of "safe"
    living in the other subcommand instead.

    Output overflow handling: input/output/traceback are each measured
    independently. Any section that exceeds the embed field cap is NOT
    truncated — it is instead attached to the response as a standalone
    file (input.py / output.py / traceback.py), and the embed field shows
    a short pointer to the attachment. Multiple sections can overflow in
    the same response; each gets its own file.

/eval safe  [Everyone]
    Takes a single `expression` argument and evaluates it through a
    dedicated simpleeval.EvalWithCompoundTypes instance restricted to an
    explicit allow-list of names and functions. simpleeval only parses
    expressions (no exec, no statements, no imports, no attribute access
    by default), so this is safe to expose publicly. Execution still runs
    off the event loop via asyncio.to_thread under a short timeout, since
    pathological inputs (e.g. very large exponents) can still be slow even
    though they can never hang forever or execute arbitrary code.

Uses: none (stateless — no bot.db access, no persisted state beyond the
in-memory `_last_result` convenience variable for the owner path).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import textwrap
import time
import traceback
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from simpleeval import (
    EvalWithCompoundTypes,
    FunctionNotDefined,
    InvalidExpression,
    IterableTooLong,
    NameNotDefined,
    NumberTooHigh,
)

from util.constants import Emojis

# ── Owner-eval execution limits ─────────────────────────────────────────────────

_OWNER_TIMEOUT_SECONDS = 30.0
_EMBED_FIELD_LIMIT = 1000  # leaves headroom under Discord's 1024-char field cap

# ── Safe-eval sandbox configuration ─────────────────────────────────────────────

_SAFE_TIMEOUT_SECONDS = 3.0
_SAFE_STRING_LIMIT = 200
_SAFE_COMPREHENSION_LIMIT = 100
_SAFE_POWER_LIMIT = 1_000_000

_SAFE_NAMES: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "true": True,
    "false": False,
    "none": None,
}

_SAFE_FUNCTIONS: dict[str, Any] = {
    # Builtins
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "sorted": sorted,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "tuple": tuple,
    # math module — deliberately excludes anything with side effects
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "hypot": math.hypot,
    "degrees": math.degrees,
    "radians": math.radians,
    "floor": math.floor,
    "ceil": math.ceil,
}


def _build_safe_evaluator() -> EvalWithCompoundTypes:
    """Construct a fresh, tightly-bounded simpleeval instance.

    EvalWithCompoundTypes (rather than plain SimpleEval) allows list/dict/set
    literals and comprehensions, which makes the sandbox meaningfully useful
    for everyday expressions without opening up statement execution, imports,
    or attribute access — none of which simpleeval's expression-only parser
    supports regardless of which Eval subclass is used.
    """
    evaluator = EvalWithCompoundTypes(functions=_SAFE_FUNCTIONS, names=_SAFE_NAMES)
    evaluator.MAX_STRING_LENGTH = _SAFE_STRING_LIMIT
    evaluator.MAX_COMPREHENSION_LENGTH = _SAFE_COMPREHENSION_LIMIT
    evaluator.MAX_POWER = _SAFE_POWER_LIMIT
    return evaluator


# ── Formatting helpers ───────────────────────────────────────────────────────────

def _safe_codeblock(text: str, limit: int) -> str:
    """Escape stray triple-backticks and truncate to fit an embed field.

    Still used by the public /eval safe path, where inputs are naturally
    short (a single expression) and truncation is an acceptable, cheap
    safeguard rather than the file-attachment path used for owner output.
    """
    text = text.replace("```", "'''") if text else ""
    if not text:
        return "\u200b"  # zero-width space — embed fields can't be empty
    if len(text) > limit:
        text = text[:limit] + "\n… (truncated)"
    return text


def _prepare_field(
    text: str, filename: str, *, limit: int = _EMBED_FIELD_LIMIT
) -> tuple[str, discord.File | None]:
    """Render *text* for an embed field, falling back to a file attachment.

    If the codeblock-escaped text fits within *limit*, it's returned as an
    embed-ready codeblock string and no file is produced. If it's too long,
    the FULL, unescaped, untruncated text is attached as *filename*, and the
    returned field value becomes a short pointer to that attachment instead
    of a lossy truncation. This means a long traceback or a large print()
    dump is always fully recoverable from the response, never silently cut.

    Returns
    -------
    (field_value, file)
        field_value is ready to drop straight into embed.add_field(value=...).
        file is None when no attachment was needed, otherwise a discord.File
        that the caller must include in the followup.send(files=...) list.
    """
    if not text:
        return "\u200b", None  # zero-width space — embed fields can't be empty

    escaped = text.replace("```", "'''")
    if len(escaped) <= limit:
        return f"```py\n{escaped}\n```", None

    file = discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)
    pointer = f"*Too long for an embed field — see `{filename}` attached below.*"
    return pointer, file


def _build_owner_response(
    *,
    code: str,
    stdout: str,
    result: Any,
    elapsed: float,
    error: str | None,
) -> tuple[discord.Embed, list[discord.File]]:
    """Build the result embed AND any overflow attachments for /eval run.

    Input, output, and traceback are measured independently — any one of
    them overflowing does not affect the others, and multiple overflows in
    the same response each produce their own file.
    """
    embed = discord.Embed(
        title=f"{Emojis.ERROR} Eval Error" if error else f"{Emojis.snekbox} Eval Result",
        colour=discord.Colour.red() if error else discord.Colour.green(),
    )
    files: list[discord.File] = []

    input_value, input_file = _prepare_field(code, "input.py")
    embed.add_field(name="Input", value=input_value, inline=False)
    if input_file:
        files.append(input_file)

    if error:
        tb_value, tb_file = _prepare_field(error, "traceback.py")
        embed.add_field(name="Traceback", value=tb_value, inline=False)
        if tb_file:
            files.append(tb_file)
    else:
        output = stdout or ""
        if result is not None:
            output = f"{output}\n{result!r}" if output else repr(result)
        output_value, output_file = _prepare_field(output, "output.py")
        embed.add_field(name="Output", value=output_value, inline=False)
        if output_file:
            files.append(output_file)
        embed.add_field(name="Return type", value=f"`{type(result).__name__}`", inline=True)

    embed.add_field(name="Time", value=f"{elapsed * 1000:.2f} ms", inline=True)
    return embed, files


def _safe_result_embed(expression: str, result: Any) -> discord.Embed:
    """Build the result embed for the public /eval safe path."""
    embed = discord.Embed(title=f"{Emojis.snekbox} Safe Eval", colour=discord.Colour.blurple())
    embed.add_field(
        name="Expression",
        value=f"```py\n{_safe_codeblock(expression, 500)}\n```",
        inline=False,
    )
    embed.add_field(
        name="Result",
        value=f"```py\n{_safe_codeblock(repr(result), 500)}\n```",
        inline=False,
    )
    embed.add_field(name="Type", value=f"`{type(result).__name__}`", inline=True)
    return embed


# ── Owner-check predicate ─────────────────────────────────────────────────────────

async def _is_owner_check(interaction: discord.Interaction) -> bool:
    """app_commands.check predicate gating /eval run to the application owner(s).

    Uses bot.is_owner() rather than a hardcoded ID comparison so that
    team-owned applications (where multiple Discord accounts are legitimate
    owners) are resolved correctly.
    """
    is_owner = await interaction.client.is_owner(interaction.user)  # type: ignore[attr-defined]
    if not is_owner:
        raise app_commands.CheckFailure("Owner-only command.")
    return True


# ── Owner-eval modal ───────────────────────────────────────────────────────────────

class EvalModal(discord.ui.Modal, title="Evaluate Python Code"):
    """Paragraph TextInput for arbitrary Python — owner path only.

    The command that opens this modal is already gated by _is_owner_check,
    but that check applies to the *command* interaction, not this modal's
    own on_submit interaction. The redundant check here is defense-in-depth
    against any code path that might construct/reuse this modal outside the
    guarded command in the future.
    """

    code: discord.ui.TextInput = discord.ui.TextInput(
        label="Python code",
        style=discord.TextStyle.paragraph,
        placeholder="await bot.fetch_user(123456789012345678)\nprint('hello')\n_  # last result",
        min_length=1,
        max_length=4000,
    )

    def __init__(self, cog: Eval) -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        # Defense-in-depth — see class docstring.
        if not await interaction.client.is_owner(interaction.user):  # type: ignore[attr-defined]
            await interaction.followup.send(
                ":no_entry: This is an owner-only command.", ephemeral=True
            )
            return

        source = str(self.code.value)
        try:
            stdout, result, error, elapsed = await asyncio.wait_for(
                self._cog._execute_owner(interaction, source),
                timeout=_OWNER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f":alarm_clock: Execution exceeded the {_OWNER_TIMEOUT_SECONDS:.0f}s limit "
                "and was abandoned. Note: the task may still be running in the "
                "background if it didn't respect cancellation.",
            )
            return

        embed, files = _build_owner_response(
            code=source, stdout=stdout, result=result, elapsed=elapsed, error=error
        )
        await interaction.followup.send(embed=embed, files=files)


# ── Cog ────────────────────────────────────────────────────────────────────────────

class Eval(commands.Cog, description="Owner-only Python execution and a public safe-eval sandbox."):
    COG_EMOJI = Emojis.snekbox

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_result: Any = None
        self._safe_evaluator = _build_safe_evaluator()

    # ── Owner-path execution ─────────────────────────────────────────────────────

    async def _execute_owner(
        self, interaction: discord.Interaction, code: str
    ) -> tuple[str, Any, str | None, float]:
        """Compile and run *code* inside an async wrapper.

        Returns (stdout, result, error_traceback_or_None, elapsed_seconds).
        A SyntaxError during compilation is treated identically to a runtime
        exception for the caller's purposes — both surface as `error`.
        """
        env: dict[str, Any] = {
            "bot": self.bot,
            "interaction": interaction,
            "guild": interaction.guild,
            "channel": interaction.channel,
            "author": interaction.user,
            "discord": discord,
            "commands": commands,
            "app_commands": app_commands,
            "asyncio": asyncio,
            "_": self._last_result,
        }

        body = textwrap.indent(code, "    ")
        wrapper_src = f"async def __eval_wrapper():\n{body}"

        start = time.perf_counter()
        try:
            exec(compile(wrapper_src, "<eval>", "exec"), env)  # noqa: S102
        except SyntaxError:
            elapsed = time.perf_counter() - start
            return "", None, traceback.format_exc(), elapsed

        wrapper = env["__eval_wrapper"]
        stdout_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buf):
                result = await wrapper()
        except Exception:  # noqa: BLE001 — owner explicitly wants to see any failure
            elapsed = time.perf_counter() - start
            return stdout_buf.getvalue(), None, traceback.format_exc(), elapsed

        elapsed = time.perf_counter() - start
        if result is not None:
            self._last_result = result
        return stdout_buf.getvalue(), result, None, elapsed

    # ── Error handler ─────────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f":alarm_clock: Slow down — try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.CheckFailure):
            msg = ":no_entry: You don't have permission to use this command."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── Command group ─────────────────────────────────────────────────────────────

    eval_grp = app_commands.Group(
        name="eval", description="Evaluate Python code or safe expressions."
    )

    # ── /eval run  [Owner Only] ──────────────────────────────────────────────────

    @eval_grp.command(
        name="run",
        description="[Owner Only] Execute arbitrary Python code.",
    )
    @app_commands.check(_is_owner_check)
    async def eval_run(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EvalModal(self))

    # ── /eval safe  [Everyone] ───────────────────────────────────────────────────

    @eval_grp.command(
        name="safe",
        description="Evaluate a safe mathematical/logical expression — no code execution.",
    )
    @app_commands.describe(
        expression="e.g. sqrt(16) + 2 ** 3, or [x * 2 for x in range(5) if x % 2 == 0]"
    )
    @app_commands.checks.cooldown(3, 10.0, key=lambda i: (i.guild_id, i.user.id))
    async def eval_safe(self, interaction: discord.Interaction, expression: str) -> None:
        await interaction.response.defer()

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._safe_evaluator.eval, expression),
                timeout=_SAFE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f":alarm_clock: Evaluation exceeded the {_SAFE_TIMEOUT_SECONDS:.0f}s limit."
            )
            return
        except (
            InvalidExpression,
            FunctionNotDefined,
            NameNotDefined,
            NumberTooHigh,
            IterableTooLong,
        ) as exc:
            await interaction.followup.send(f":no_entry: {exc}")
            return
        except ZeroDivisionError:
            await interaction.followup.send(":no_entry: Division by zero.")
            return
        except Exception as exc:  # noqa: BLE001 — surfaced as a generic sandbox error
            await interaction.followup.send(f":no_entry: Could not evaluate: `{exc}`")
            return

        await interaction.followup.send(embed=_safe_result_embed(expression, result))


# ── Extension entry-point ─────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Eval(bot))