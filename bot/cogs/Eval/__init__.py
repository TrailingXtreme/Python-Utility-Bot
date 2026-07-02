"""
Eval cog — a single /eval command, split by TRUST not by heuristics.

Design note (read before changing this file)
----------------------------------------------
An earlier version of this cog had two commands: `/eval run` (owner-only,
full process access, no sandboxing) and `/eval safe` (public, restricted to
simpleeval expressions). This version merges them into one `/eval` command
with one modal, but the safety boundary is still enforced by WHO you are,
not by scanning the code for "harmful-looking" patterns.

A content-based harm check (regex, AST denylist, even an LLM call) is not a
security boundary — arbitrary Python has too many ways to reach the same
capability through indirection (string building, getattr chains, walking
`().__class__.__mro__[...]__subclasses__()`, base64'd payloads, etc.).
Anyone motivated enough routes around a "does this look bad" filter. Real
isolation has to come from where the code actually executes:

    /eval  [Owner]     -> runs in-process, full bot access, same as before.
                          You're trusted, so no sandbox needed.
    /eval  [Everyone]  -> code is shipped to Judge0's execution API and runs
                          in a namespaced, cgroup-limited container with NO
                          access to this process, its memory, its token, the
                          filesystem, or the network beyond what Judge0
                          itself allows inside the sandbox. Whatever the code
                          does, it cannot touch the bot.

Judge0 (https://github.com/judge0/judge0) is the same category of tool as
snekbox — it uses `isolate` (Linux namespaces + cgroups) to run untrusted
code safely — but unlike snekbox it doesn't require privileged/NsJail-style
container capabilities to operate, which is why it has a free, public,
no-signup instance at ce.judge0.com. That's also *why* self-hosting snekbox
crashed on Railway: snekbox's NsJail sandbox needs CAP_SYS_ADMIN-equivalent
privileges that free PaaS tiers (Railway, Render, Vercel, Netlify) do not
grant to containers. Judge0's public instance sidesteps that problem
entirely — you call it over HTTPS, you don't host the sandbox yourself.

If you outgrow the public instance's rate limits, the same client code here
works against:
  - Judge0 CE on RapidAPI (free tier, higher/more predictable limits,
    requires a RapidAPI key — set JUDGE0_API_KEY)
  - A self-hosted Judge0 (`docker-compose up`) on any host that allows
    privileged containers (a real VPS, not most free serverless PaaS)
Just change JUDGE0_BASE_URL / JUDGE0_API_KEY; nothing else in this file
needs to change.

Output overflow handling: input/output/traceback are each measured
independently. Any section that exceeds the embed field cap is NOT
truncated — it is instead attached to the response as a standalone file
(input.py / output.py / traceback.py), and the embed field shows a short
pointer to the attachment.

Uses: aiohttp (via discord.py dependency) for the Judge0 HTTP calls.
No persisted state beyond the in-memory `_last_result` convenience variable
for the owner path, and an in-memory per-user cooldown for the sandboxed path.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import textwrap
import time
import traceback
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Emojis
from config import settings

# ── Owner-path execution limits ─────────────────────────────────────────────

_OWNER_TIMEOUT_SECONDS = 30.0
_EMBED_FIELD_LIMIT = 1000  # leaves headroom under Discord's 1024-char field cap

# ── Sandboxed-path (Judge0) configuration ───────────────────────────────────

# Public, free, no-signup Judge0 CE instance. No credit card, no hosting.
# Swap to a RapidAPI-issued host (and set JUDGE0_API_KEY) or a self-hosted instance if you outgrow this — see module docstring.
JUDGE0_BASE_URL = settings.JUDGE0_BASE_URL
JUDGE0_API_KEY = settings.judge0_token

_SANDBOX_CPU_TIME_LIMIT = 8.0      # seconds, enforced BY Judge0 inside the sandbox
_SANDBOX_WALL_TIME_LIMIT = 12.0    # seconds, hard wall clock cap inside the sandbox
_SANDBOX_MEMORY_LIMIT_KB = 128_000  # ~128MB
_SANDBOX_HTTP_TIMEOUT = 20.0        # our own timeout on the HTTP round trip
_SANDBOX_MAX_CODE_LENGTH = 4000
_SANDBOX_MAX_STDIN_LENGTH = 4000

# Cooldown applied only to the sandboxed (non-owner) path, so we're a
# considerate citizen of the shared public Judge0 instance.
_SANDBOX_COOLDOWN_USES = 3
_SANDBOX_COOLDOWN_SECONDS = 30.0


# ── Formatting helpers ───────────────────────────────────────────────────────

def _prepare_field(
    text: str, filename: str, *, limit: int = _EMBED_FIELD_LIMIT
) -> tuple[str, discord.File | None]:
    """Render *text* for an embed field, falling back to a file attachment.

    If the codeblock-escaped text fits within *limit*, it's returned as an
    embed-ready codeblock string and no file is produced. If it's too long,
    the FULL, unescaped, untruncated text is attached as *filename*, and the
    returned field value becomes a short pointer to that attachment instead
    of a lossy truncation.

    Returns
    -------
    (field_value, file)
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
    stdin: str,
    stdout: str,
    result: Any,
    elapsed: float,
    error: str | None,
) -> tuple[discord.Embed, list[discord.File]]:
    """Build the result embed AND any overflow attachments for the owner path."""
    embed = discord.Embed(
        title=f"{Emojis.snekbox} Eval Error" if error else f"{Emojis.snekbox} Eval Result",
        colour=discord.Colour.red() if error else discord.Colour.green(),
    )
    files: list[discord.File] = []

    input_value, input_file = _prepare_field(code, "input.py")
    embed.add_field(name="Input", value=input_value, inline=False)
    if input_file:
        files.append(input_file)

    if stdin:
        stdin_value, stdin_file = _prepare_field(stdin, "stdin.txt")
        embed.add_field(name="stdin", value=stdin_value, inline=False)
        if stdin_file:
            files.append(stdin_file)

    if error:
        error_value, error_file = _prepare_field(error, "traceback.py")
        embed.add_field(name="Traceback", value=error_value, inline=False)
        if error_file:
            files.append(error_file)
    else:
        if stdout:
            stdout_value, stdout_file = _prepare_field(stdout, "output.py")
            embed.add_field(name="stdout", value=stdout_value, inline=False)
            if stdout_file:
                files.append(stdout_file)
        result_value, result_file = _prepare_field(repr(result), "result.py")
        embed.add_field(name="Returned", value=result_value, inline=False)
        if result_file:
            files.append(result_file)
        embed.add_field(name="Type", value=f"`{type(result).__name__}`", inline=True)

    embed.set_footer(text=f"Owner path · in-process · {elapsed * 1000:.1f}ms")
    return embed, files


def _build_sandbox_response(
    *,
    code: str,
    stdin: str,
    stdout: str,
    stderr: str,
    compile_output: str,
    status: str,
    elapsed_ms: float | None,
) -> tuple[discord.Embed, list[discord.File]]:
    """Build the result embed AND any overflow attachments for the sandboxed path."""
    is_error = status != "Accepted"
    embed = discord.Embed(
        title=f"{Emojis.snekbox} Eval Error" if is_error else f"{Emojis.snekbox} Eval Result",
        colour=discord.Colour.red() if is_error else discord.Colour.green(),
    )
    files: list[discord.File] = []

    input_value, input_file = _prepare_field(code, "input.py")
    embed.add_field(name="Input", value=input_value, inline=False)
    if input_file:
        files.append(input_file)

    if stdin:
        stdin_value, stdin_file = _prepare_field(stdin, "stdin.txt")
        embed.add_field(name="stdin", value=stdin_value, inline=False)
        if stdin_file:
            files.append(stdin_file)

    if compile_output:
        cval, cfile = _prepare_field(compile_output, "compile.txt")
        embed.add_field(name="Compile output", value=cval, inline=False)
        if cfile:
            files.append(cfile)

    if stdout:
        oval, ofile = _prepare_field(stdout, "output.py")
        embed.add_field(name="stdout", value=oval, inline=False)
        if ofile:
            files.append(ofile)

    if stderr:
        eval_, efile = _prepare_field(stderr, "stderr.txt")
        embed.add_field(name="stderr", value=eval_, inline=False)
        if efile:
            files.append(efile)

    if not stdout and not stderr and not compile_output:
        embed.add_field(name="Output", value="\u200b(no output)", inline=False)

    footer = f"Sandboxed via Judge0 · status: {status}"
    if elapsed_ms is not None:
        footer += f" · {elapsed_ms:.1f}ms"
    embed.set_footer(text=footer)
    return embed, files


# ── Judge0 client ────────────────────────────────────────────────────────────

class Judge0Error(RuntimeError):
    """Raised on any Judge0 transport/HTTP failure (not a code error)."""


class Judge0Client:
    """Thin async client for the Judge0 execute endpoint.

    Talks to whichever host JUDGE0_BASE_URL points at — the free public
    instance by default. Caches the numeric language id for "Python (3.x)"
    after the first successful lookup, since it's static per-instance.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._python_language_id: int | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if JUDGE0_API_KEY:
            # RapidAPI-hosted Judge0 expects these two headers.
            headers["X-RapidAPI-Key"] = JUDGE0_API_KEY
            headers["X-RapidAPI-Host"] = JUDGE0_BASE_URL.replace("https://", "")
        return headers

    async def _resolve_python_language_id(self) -> int:
        if self._python_language_id is not None:
            return self._python_language_id

        url = f"{JUDGE0_BASE_URL}/languages"
        try:
            async with self._session.get(
                url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise Judge0Error(f"Judge0 /languages returned HTTP {resp.status}")
                languages = await resp.json()
        except aiohttp.ClientError as exc:
            raise Judge0Error(f"Could not reach Judge0: {exc}") from exc

        # Prefer the newest "Python (3.x.y)" entry available on this instance.
        candidates = [
            lang for lang in languages
            if lang.get("name", "").startswith("Python (3")
        ]
        if not candidates:
            raise Judge0Error("No Python 3 runtime found on this Judge0 instance.")
        candidates.sort(key=lambda lang: lang["name"])
        self._python_language_id = candidates[-1]["id"]
        return self._python_language_id

    async def run_python(self, source: str, stdin: str | None = None) -> dict[str, Any]:
        """Submit *source* for sandboxed execution and return the raw result dict.

        Uses wait=true for a synchronous round trip — simpler than polling,
        and well within our own HTTP timeout for short scripts. *stdin*, if
        given, is fed to the process exactly as if it had been piped in —
        each input() call in the submitted code consumes one line of it.
        """
        language_id = await self._resolve_python_language_id()

        payload = {
            "language_id": language_id,
            "source_code": source,
            "cpu_time_limit": _SANDBOX_CPU_TIME_LIMIT,
            "wall_time_limit": _SANDBOX_WALL_TIME_LIMIT,
            "memory_limit": _SANDBOX_MEMORY_LIMIT_KB,
            # No network access — this is a code sandbox, not a shell.
            "enable_network": False,
            "stdin": stdin or "",
        }
        url = f"{JUDGE0_BASE_URL}/submissions?wait=true&base64_encoded=false"
        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=_SANDBOX_HTTP_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    raise Judge0Error(
                        "The public Judge0 instance is rate-limited right now — try again shortly."
                    )
                if resp.status not in (200, 201):
                    raise Judge0Error(f"Judge0 returned HTTP {resp.status}")
                return await resp.json()
        except asyncio.TimeoutError as exc:
            raise Judge0Error("Timed out waiting for the sandbox to respond.") from exc
        except aiohttp.ClientError as exc:
            raise Judge0Error(f"Could not reach Judge0: {exc}") from exc


# ── Owner-check + per-user cooldown for the sandboxed path ──────────────────

async def _is_owner(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)  # type: ignore[attr-defined]


class _SandboxCooldown:
    """Manual sliding-window cooldown, applied ONLY to non-owner submissions.

    A decorator-based cooldown on the command itself would also throttle the
    owner, which isn't the point — the owner isn't hitting a shared public
    resource on someone else's free tier, only non-owners are.
    """

    def __init__(self, uses: int, per_seconds: float) -> None:
        self._uses = uses
        self._per_seconds = per_seconds
        self._hits: dict[int, list[float]] = {}

    def check(self, user_id: int) -> float | None:
        """Returns None if allowed, or seconds-to-wait if rate limited."""
        now = time.monotonic()
        window_start = now - self._per_seconds
        hits = [t for t in self._hits.get(user_id, []) if t > window_start]
        if len(hits) >= self._uses:
            retry_after = hits[0] + self._per_seconds - now
            self._hits[user_id] = hits
            return max(retry_after, 0.1)
        hits.append(now)
        self._hits[user_id] = hits
        return None


# ── Result view (delete button) ──────────────────────────────────────────────

class DeleteButton(discord.ui.Button):
    """Deletes the message it's attached to — but only for whoever's allowed to.

    Permission is enforced by ResultView.interaction_check, not here: a bare
    interaction.message.delete() on click would let *anyone* who can see the
    message delete someone else's eval output, since the button itself has
    no idea who's clicking it.
    """

    def __init__(self, *, row: int = 1) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, emoji=Emojis.trashcan, label="Delete", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.message.delete()


class ResultView(discord.ui.View):
    """Attached to every eval result — currently just the delete button.

    Only the person who ran the eval (or the bot owner, for cleanup) can hit
    delete. Times out after 5 minutes so the button doesn't linger forever;
    the message itself is untouched on timeout, only the button disables.
    """

    def __init__(self, *, author_id: int) -> None:
        super().__init__(timeout=300)
        self._author_id = author_id
        self.add_item(DeleteButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._author_id or await _is_owner(interaction):
            return True
        await interaction.response.send_message(
            ":no_entry: Only the person who ran this eval can delete it.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        # Best-effort — the message may already be gone (e.g. user deleted it).
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


# ── Eval modal (shared by owner and non-owner submissions) ──────────────────

class EvalModal(discord.ui.Modal, title="Evaluate Python Code"):
    """Single paragraph TextInput for Python code — routing happens on submit.

    Owners get full in-process access; everyone else gets the same modal but
    their code is executed in an isolated Judge0 sandbox with no bot access
    whatsoever. The modal itself doesn't know or care which path it's on.
    """

    code: discord.ui.TextInput = discord.ui.TextInput(
        label="Python code",
        style=discord.TextStyle.paragraph,
        placeholder="print('hello')\n1 + 1  # last expression's repr is shown as the result",
        min_length=1,
        max_length=4000,
    )

    stdin: discord.ui.TextInput = discord.ui.TextInput(
        label="Standard input (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="One value per line — each input() call consumes the next line",
        required=False,
        max_length=4000,
    )

    def __init__(self, cog: Eval) -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        source = str(self.code.value)
        stdin_text = str(self.stdin.value) if self.stdin.value else ""

        if await _is_owner(interaction):
            await self._run_owner_path(interaction, source, stdin_text)
        else:
            await self._run_sandboxed_path(interaction, source, stdin_text)

    # -- Owner path: in-process, full trust, no sandbox -----------------------

    async def _run_owner_path(
        self, interaction: discord.Interaction, source: str, stdin_text: str
    ) -> None:
        try:
            stdout, result, error, elapsed = await asyncio.wait_for(
                self._cog.execute_owner(interaction, source, stdin_text),
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
            code=source,
            stdin=stdin_text,
            stdout=stdout,
            result=result,
            elapsed=elapsed,
            error=error,
        )
        view = ResultView(author_id=interaction.user.id)
        view.message = await interaction.followup.send(embed=embed, files=files, view=view)

    # -- Non-owner path: routed to the Judge0 sandbox, zero bot access --------

    async def _run_sandboxed_path(
        self, interaction: discord.Interaction, source: str, stdin_text: str
    ) -> None:
        if len(source) > _SANDBOX_MAX_CODE_LENGTH:
            await interaction.followup.send(
                f":no_entry: Code is too long for the sandbox "
                f"({len(source)} > {_SANDBOX_MAX_CODE_LENGTH} characters).",
                ephemeral=True,
            )
            return

        if len(stdin_text) > _SANDBOX_MAX_STDIN_LENGTH:
            await interaction.followup.send(
                f":no_entry: stdin is too long for the sandbox "
                f"({len(stdin_text)} > {_SANDBOX_MAX_STDIN_LENGTH} characters).",
                ephemeral=True,
            )
            return

        retry_after = self._cog.sandbox_cooldown.check(interaction.user.id)
        if retry_after is not None:
            await interaction.followup.send(
                f":alarm_clock: Slow down — try again in {retry_after:.1f}s. "
                "(This limit only applies to the shared public sandbox, not the bot itself.)",
                ephemeral=True,
            )
            return

        try:
            raw = await self._cog.judge0.run_python(source, stdin=stdin_text)
        except Judge0Error as exc:
            await interaction.followup.send(f":no_entry: {exc}", ephemeral=True)
            return

        status = raw.get("status", {}).get("description", "Unknown")
        stdout = raw.get("stdout") or ""
        stderr = raw.get("stderr") or raw.get("message") or ""
        compile_output = raw.get("compile_output") or ""
        elapsed_ms = None
        if raw.get("time") is not None:
            with contextlib.suppress(ValueError, TypeError):
                elapsed_ms = float(raw["time"]) * 1000

        embed, files = _build_sandbox_response(
            code=source,
            stdin=stdin_text,
            stdout=stdout,
            stderr=stderr,
            compile_output=compile_output,
            status=status,
            elapsed_ms=elapsed_ms,
        )
        view = ResultView(author_id=interaction.user.id)
        view.message = await interaction.followup.send(embed=embed, files=files, view=view)


# ── Cog ────────────────────────────────────────────────────────────────────

class Eval(commands.Cog, description="Evaluate Python — full access for the owner, sandboxed for everyone else."):
    COG_EMOJI = Emojis.snekbox
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_result: Any = None
        self._session: aiohttp.ClientSession | None = None
        self.judge0: Judge0Client | None = None
        self.sandbox_cooldown = _SandboxCooldown(
            _SANDBOX_COOLDOWN_USES, _SANDBOX_COOLDOWN_SECONDS
        )

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self.judge0 = Judge0Client(self._session)

    async def cog_unload(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ── Owner-path execution ─────────────────────────────────────────────────

    async def execute_owner(
        self, interaction: discord.Interaction, code: str, stdin_text: str = ""
    ) -> tuple[str, Any, str | None, float]:
        """Compile and run *code* inside an async wrapper with full bot access.

        *stdin_text*, if given, backs a substitute ``input()`` so code that
        calls it behaves like it's reading piped stdin — one line consumed
        per call, ``EOFError`` once exhausted (matching real ``input()``
        semantics rather than blocking forever, which would hang the
        interaction).

        Returns (stdout, result, error_traceback_or_None, elapsed_seconds).
        """
        stdin_lines = iter(stdin_text.splitlines())

        def _fake_input(prompt: object = "") -> str:
            if prompt:
                stdout_buf.write(str(prompt))
            try:
                return next(stdin_lines)
            except StopIteration:
                raise EOFError("EOF when reading a line") from None

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
            "input": _fake_input,
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

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"{Emojis.alarm_clock} Slow down — try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.CheckFailure):
            msg = f"{Emojis.no_entry} You don't have permission to use this command."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── The single /eval command ─────────────────────────────────────────────

    @app_commands.command(
        name="eval",
        description="Evaluate Python code. Full access if you're the bot owner; sandboxed otherwise.",
    )
    async def eval_(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EvalModal(self))


# ── Extension entry-point ─────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Eval(bot))