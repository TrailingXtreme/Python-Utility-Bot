"""
Calculator cog
━━━━━━━━━━━━━━
Casio fx-991MS style scientific calculator for Discord.

Commands
────────
/calc  <expression>
    One-shot evaluation — responds with a colour-coded embed.
    Also available as ``!calc`` / ``!calculate`` via text prefix.

    Examples::

        /calc sin(45) + √(16) * 2
        /calc nCr(10, 3)
        /calc (1 + 1/100)^100
        /calc gcd(48, 18)

/calculator
    Opens a persistent interactive calculator panel (5-minute timeout).
    Only the user who opened it can press its buttons.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Interactive calculator layout
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Discord Views are hard-capped at 25 child components across five rows
(rows 0–4).  A row holding a Select cannot also hold buttons.

    Row 0  ╔══════════════════════════════════════╗  ← CalculatorSelect
           ║  ƒ(x) — functions · constants · tools ║    (25 options, see below)
           ╚══════════════════════════════════════╝

    Row 1  [ 7 ] [ 8 ] [ 9 ] [ ÷ ] [DEL]
    Row 2  [ 4 ] [ 5 ] [ 6 ] [ × ] [ AC]
    Row 3  [ 1 ] [ 2 ] [ 3 ] [ANS] [ ( ]
    Row 4  [ 0 ] [ . ] [ + ] [ − ] [ = ]
    ─────────────────────────────────────
    Total  1 select + 20 buttons = 21 / 25 components used

    Note: ')' is available via the ƒ(x) menu → ")" option, or the
    📝 Type expression modal.  The ANS button inserts the last result
    directly — identical to picking Ans from the ƒ(x) menu but one
    tap faster.

CalculatorSelect options  (25 / 25 — the Discord maximum)
─────────────────────────────────────────────────────────
  Trig (6)         sin  cos  tan  sin⁻¹  cos⁻¹  tan⁻¹
  Log/Exp (4)      log  log₂  ln  exp
  Roots (2)        √ (sqrt)   ∛ (cbrt)
  Rounding (3)     abs   floor   ceil
  Combinatorics(3) nCr   nPr   ! (postfix factorial)
  Integer (2)      gcd   lcm
  Constants (2)    π   e
  Brackets (1)     )
  Utility (2)      ⟳ Toggle DEG/RAD · 📝 Type expression…

= key behaviour
──────────────────────────
On the interactive panel, pressing = evaluates the expression and
*replaces* the input display with the formatted result — matching real
Casio behaviour.  The original expression is saved to the history list.
Pressing a digit after = continues from the result naturally.

For /calc the original input is always echoed alongside the result.

Error display
─────────────
Errors show a short description of what went wrong plus a 💡 hint
suggesting how to fix it, both on the interactive panel and in /calc.

History & embed
───────────────
The embed shows the active angle mode, the current expression, the last
result (or an error), and up to 5 previous calculations (newest first).

DEG / RAD mode
──────────────
Toggled via "⟳ Toggle DEG ↔ RAD" in CalculatorSelect.  The current
mode is shown in the embed description and footer.
"""

from __future__ import annotations
from tkinter.font import BOLD

import discord
from discord.ext import commands

from util.calculator import CalcError, evaluate, expr_str, format_result

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_DISPLAY = 55   # chars shown in the expression line before truncation
MAX_HISTORY = 5    # past evaluations kept and displayed in the embed


# ══════════════════════════════════════════════════════════════════════════════
# CalculatorSelect option table  (25 options — Discord maximum)
# ══════════════════════════════════════════════════════════════════════════════
#
# Regular option values (e.g. "sin(", "pi") are appended directly to the
# expression string.
#
# Special sentinel values (prefixed "__") trigger calculator actions:
#   __mode__  → toggle angle mode between DEG and RAD
#   __type__  → open ExpressionModal for free-form text entry
#
# ─────────────────────────────────────────────────────────────────────────────

_CALC_SELECT_OPTIONS: list[discord.SelectOption] = [
    # ── Trigonometric functions ────────────────────────────────────────────
    discord.SelectOption(
        label="sin  (Sine)", value="sin(",
        description="sin(x) — sine in the active angle mode",
    ),
    discord.SelectOption(
        label="cos  (Cosine)", value="cos(",
        description="cos(x) — cosine in the active angle mode",
    ),
    discord.SelectOption(
        label="tan  (Tangent)", value="tan(",
        description="tan(x) — tangent in the active angle mode",
    ),
    discord.SelectOption(
        label="sin⁻¹  (Arcsine)", value="sin⁻¹(",
        description="Angle whose sine = x (result in active mode units)",
    ),
    discord.SelectOption(
        label="cos⁻¹  (Arccosine)", value="cos⁻¹(",
        description="Angle whose cosine = x (result in active mode units)",
    ),
    discord.SelectOption(
        label="tan⁻¹  (Arctangent)", value="tan⁻¹(",
        description="Angle whose tangent = x (result in active mode units)",
    ),

    # ── Logarithms & exponential ──────────────────────────────────────────
    discord.SelectOption(
        label="log  (Base-10 logarithm)", value="log(",
        description="log(x) — common logarithm",
    ),
    discord.SelectOption(
        label="log₂  (Base-2 logarithm)", value="log2(",
        description="log₂(x) — binary logarithm",
    ),
    discord.SelectOption(
        label="ln  (Natural logarithm)", value="ln(",
        description="ln(x) — logarithm base e",
    ),
    discord.SelectOption(
        label="exp  (eˣ)", value="exp(",
        description="exp(x) = eˣ — exponential function",
    ),

    # ── Roots ─────────────────────────────────────────────────────────────
    discord.SelectOption(
        label="√  (Square root)", value="√(",
        description="√(x) — principal square root",
    ),
    discord.SelectOption(
        label="∛  (Cube root)", value="cbrt(",
        description="cbrt(x) — real cube root (works for negative inputs)",
    ),

    # ── Rounding & magnitude ──────────────────────────────────────────────
    discord.SelectOption(
        label="abs  (Absolute value)", value="abs(",
        description="|x| — magnitude / distance from zero",
    ),
    discord.SelectOption(
        label="floor  (Round down)", value="floor(",
        description="floor(x) — largest integer ≤ x",
    ),
    discord.SelectOption(
        label="ceil  (Round up)", value="ceil(",
        description="ceil(x) — smallest integer ≥ x",
    ),
    discord.SelectOption(
        label="round  (Standard round)", value="round(",
        description="round(x) or round(x, digits) — rounds to nearest integer/decimal",
    ),

    # ── PAGE FLIP NAVIGATION SENTINEL ──────────────────────────────────────
    discord.SelectOption(
        label="➡️ Switch to Tools & Constants Menu", value="__page_tools__",
        description="View combinatorics, algebra operators, constants, and utilities",
    ),
]

_TOOLS_CONSTANTS_OPTIONS: list[discord.SelectOption] = [
    # ── Combinatorics ─────────────────────────────────────────────────────
    discord.SelectOption(
        label="nCr  (Combinations)", value="nCr(",
        description="nCr(n, r) — ways to choose r items from n",
    ),
    discord.SelectOption(
        label="nPr  (Permutations)", value="nPr(",
        description="nPr(n, r) — ordered arrangements of r from n",
    ),
    discord.SelectOption(
        label="!  (Factorial)", value="!",
        description="Appends postfix factorial (!) operator to your value",
    ),

    # ── Integer math & Extra Operators ────────────────────────────────────
    discord.SelectOption(
        label="gcd  (Greatest Common Divisor)", value="gcd(",
        description="gcd(a, b)",
    ),
    discord.SelectOption(
        label="lcm  (Least Common Multiple)", value="lcm(",
        description="lcm(a, b)",
    ),
    discord.SelectOption(
        label="^  (Power / Exponent)", value="^",
        description="Raises base to a power, e.g. 2^10",
    ),
    discord.SelectOption(
        label="%  (Modulo / Remainder)", value="%",
        description="Returns the remainder of integer division, e.g. 10 % 3",
    ),
    discord.SelectOption(
        label="//  (Integer Division)", value="//",
        description="Divides and truncates the decimal part, e.g. 7 // 2 = 3",
    ),

    # ── Constants ─────────────────────────────────────────────────────────
    discord.SelectOption(
        label="π  (Pi ≈ 3.14159…)", value="pi",
        description="Ratio of a circle's circumference to its diameter",
    ),
    discord.SelectOption(
        label="e  (Euler's number ≈ 2.71828…)", value="e",
        description="Base of the natural logarithm",
    ),
    discord.SelectOption(
        label="τ  (Tau ≈ 6.28318…)", value="τ",
        description="Circumference-to-radius ratio (equal to 2π)",
    ),

    # ── Extra Symbols ─────────────────────────────────────────────────────
    discord.SelectOption(
        label=",  (Comma delimiter)", value=",",
        description="Separates arguments inside functions like nCr(10, 3)",
    ),

    # ── System Utilities ──────────────────────────────────────────────────
    discord.SelectOption(
        label="⟳ Toggle DEG ↔ RAD", value="__mode__",
        description="Switch trig angle unit between Degrees and Radians",
    ),
    discord.SelectOption(
        label="📝 Type full expression…", value="__type__",
        description="Open a text box to type or paste any expression",
    ),

    # ── PAGE FLIP NAVIGATION SENTINEL ──────────────────────────────────────
    discord.SelectOption(
        label="⬅️ Switch to Advanced Functions Menu", value="__page_math__",
        description="View trigonometric, logarithms, roots, and rounding operations",
    ),
]

# ══════════════════════════════════════════════════════════════════════════════
# Modal
# ══════════════════════════════════════════════════════════════════════════════


class ExpressionModal(discord.ui.Modal, title="Enter expression"):
    """
    Full-keyboard expression entry modal.

    Triggered by selecting "📝 Type full expression…" from CalculatorSelect.
    Pre-populated with the current expression so it acts as an in-place
    editor — the user can append to, rewrite, or paste over it entirely.

    On submit the view's ``expr`` is updated and the message is refreshed.
    """

    expression: discord.ui.TextInput = discord.ui.TextInput(
        label="Expression",
        placeholder="e.g.  sin(45) + √(16) * 2   or   nCr(10, 3)   or   5!",
        style=discord.TextStyle.short,
        max_length=200,
        required=True,
    )

    def __init__(self, view: CalculatorView) -> None:
        super().__init__()
        self.view_ref = view
        self.expression.default = view.expr or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view_ref.expr = str(self.expression.value).strip()
        self.view_ref.error = None
        await self.view_ref.refresh(interaction)


# ══════════════════════════════════════════════════════════════════════════════
# Select component
# ══════════════════════════════════════════════════════════════════════════════

class CalculatorSelect(discord.ui.Select):
    """
    Row 0 — Scientific functions, constants, and calculator tools.

    Regular option values (e.g. ``"sin("``, ``"pi"``) are appended to
    ``view.expr`` directly, exactly like a button press.

    Sentinel values trigger actions instead of token insertion:

    +-----------+----------------------------------------------------------+
    | Value     | Effect                                                   |
    +===========+==========================================================+
    | __mode__  | Toggle ``view.angle_mode`` between "deg" and "rad"      |
    | __type__  | Send ExpressionModal for free-form text / paste entry   |
    +-----------+----------------------------------------------------------+
    """

    def __init__(self, view: CalculatorView) -> None:
        self.view_ref = view
        super().__init__(
            placeholder="ƒ(x)  —  Advanced Functions Menu",
            options=_CALC_SELECT_OPTIONS,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        token = self.values[0]

        # ── Handle Page Transitions ───────────────────────────────────────────
        if token == "__page_tools__":
            self.options = _TOOLS_CONSTANTS_OPTIONS
            self.placeholder = "⚙️  —  Tools & Constants Menu"
            await self.view_ref.refresh(interaction)
            return

        if token == "__page_math__":
            self.options = _CALC_SELECT_OPTIONS
            self.placeholder = "ƒ(x)  —  Advanced Functions Menu"
            await self.view_ref.refresh(interaction)
            return

        # ── Handle Core System Sentinels ──────────────────────────────────────
        if token == "__type__":
            await interaction.response.send_modal(ExpressionModal(self.view_ref))
            return

        if token == "__mode__":
            self.view_ref.angle_mode = (
                "rad" if self.view_ref.angle_mode == "deg" else "deg"
            )
            await self.view_ref.refresh(interaction)
            return

        # ── Standard Token Injection ──────────────────────────────────────────
        self.view_ref.error = None
        self.view_ref.expr += token
        await self.view_ref.refresh(interaction)

# ══════════════════════════════════════════════════════════════════════════════
# Main view
# ══════════════════════════════════════════════════════════════════════════════


class CalculatorView(discord.ui.View):
    """
    Owner-only interactive scientific calculator panel.

    State
    ─────
    expr        Expression string currently in the input display.
    ans         Result of the last successful evaluation (``Ans`` token).
    angle_mode  Active trig unit: ``"deg"`` (default) or ``"rad"``.
    history     List of ``(expression, result)`` pairs, oldest first.
                Capped at MAX_HISTORY entries.
    error       Most recent CalcError, or ``None``.

    Timeout
    ───────
    300 seconds (5 minutes) of inactivity.  discord.py automatically
    disables all children on timeout so the buttons go grey.

    Component budget  (21 of 25 slots used)
    ────────────────────────────────────────
    Row 0  CalculatorSelect        1
    Row 1  [7] [8] [9] [×] [DEL]   5
    Row 2  [4] [5] [6] [÷] [(]     5
    Row 3  [1] [2] [3] [+] [-]     5
    Row 4  [0] [.] [)] [AC] [=]    5
                              ────
                              21 total
    """

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id:   int = owner_id
        self.expr:       str = ""
        self.ans:        float = 0.0
        self.angle_mode: str = "deg"
        self.history:    list[tuple[str, float]] = []
        self.error:      CalcError | None = None

        self.add_item(CalculatorSelect(self))

    # ── Owner guard ───────────────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Silently reject interactions from anyone other than the owner."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🚫 This calculator belongs to someone else.", ephemeral=True
            )
            return False
        return True

    # ── Embed builder ─────────────────────────────────────────────────────────

    def _embed(self) -> discord.Embed:
        """
        Build the current-state embed.

        Layout
        ------
        Description   Active angle mode badge.
        Fields (row)  Expression (inline) | Result or Error (inline).
        Error hint    If an error has a hint, shown as a separate field.
        History field All past evaluations, newest first (if any).
        Footer        Quick-reference tips + active mode reminder.
        """
        mode = self.angle_mode.upper()
        display = self.expr or "0"
        if len(display) > MAX_DISPLAY:
            display = "…" + display[-(MAX_DISPLAY - 1):]

        colour = discord.Colour.red() if self.error else discord.Colour.blurple()
        embed = discord.Embed(title="🧮 Scientific Calculator", colour=colour)

        # Status bar
        embed.description = f"**{mode}** mode"

        # Tips field (full-width, below description, above expression/result)
        embed.add_field(
            name="\u200b",
            value="**Calculator tips:**\n\n▸ Use the **ƒ(x) menu** for scientific functions, constants, and tools like the `DEG/RAD` toggle and free-form expression entry.\n\n▸ The **ANS** button inserts the last result in one tap, and :heavy_equals_sign: stores the answer and resets the input display, matching real Casio behaviour.\n\n▸ Errors show a description and a :bulb:",
            inline=False,
        )
        embed.add_field(
            name="\u200b",
            value=":bulb: **Remember:**\n\n1) **Use ƒ(x) menu to access** `Type Full Expression` (Please ➡️ Switch to Tools & Constants Menu before using) **for free-form input**\n\n2) **AC clears**\n",
            inline=False,
        )
        # Expression + result/error
        embed.add_field(
            name="Expression",
            value=f"```\n{display}\n```",
            inline=False,
        )
        if self.error:
            embed.add_field(
                name="⚠️ Error",
                value=f"```\n{self.error.message}\n```",
                inline=True,
            )
            # Hint shown as its own full-width field so it's easy to read
            if self.error.hint:
                embed.add_field(
                    name="💡 Suggestion",
                    value=self.error.hint,
                    inline=False,
                )
        else:
            embed.add_field(
                name="Result",
                value=f"```\n{format_result(self.ans)}\n```",
                inline=False,
            )

        # Calculation history (newest first)
        if self.history:
            lines = [
                f"`{e[:40]}` → **{format_result(r)}**"
                for e, r in reversed(self.history)
            ]
            embed.add_field(
                name="📜 History",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(
            text=(f"{mode} trig active"),
            icon_url="https://cdn.discordapp.com/emojis/1514827759577464943.gif"
        )
        return embed

    # ── Refresh helper ────────────────────────────────────────────────────────

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Edit the calculator message in-place to reflect current state."""
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self._embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _append(self, interaction: discord.Interaction, token: str) -> None:
        """Append *token* to ``self.expr``, clear any error, then refresh."""
        self.error = None
        self.expr += token
        await self.refresh(interaction)

    # ── Row 1 — 7  8  9 × ÷  DEL ──────────────────────────────────────────────

    @discord.ui.button(label="7", style=discord.ButtonStyle.primary, row=1)
    async def b7(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "7")

    @discord.ui.button(label="8", style=discord.ButtonStyle.primary, row=1)
    async def b8(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "8")

    @discord.ui.button(label="9", style=discord.ButtonStyle.primary, row=1)
    async def b9(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "9")

    @discord.ui.button(label="×", style=discord.ButtonStyle.secondary, row=1)
    async def mul_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "*")

    @discord.ui.button(label="DEL", style=discord.ButtonStyle.danger, row=1)
    async def del_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Delete the rightmost character from the expression."""
        self.error = None
        self.expr = self.expr[:-1]
        await self.refresh(interaction)

    # ── Row 2 —  4  5  6 + - AC ───────────────────────────────────────────────

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary, row=2)
    async def b4(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "4")

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary, row=2)
    async def b5(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "5")

    @discord.ui.button(label="6", style=discord.ButtonStyle.primary, row=2)
    async def b6(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "6")

    @discord.ui.button(label="÷", style=discord.ButtonStyle.secondary, row=2)
    async def div_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "/")

    @discord.ui.button(label="(", style=discord.ButtonStyle.secondary, row=2)
    async def open_paren(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "(")


    # ── Row 3 — 1  2  3  ANS  = ──────────────────────────────────────────────

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary, row=3)
    async def b1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "1")

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary, row=3)
    async def b2(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "2")

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary, row=3)
    async def b3(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "3")

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=3)
    async def add_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "+")

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=3)
    async def sub_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "-")

    # ── Row 4 — 0  . ( ) ────────────────────────────────────────────────

    @discord.ui.button(label="0", style=discord.ButtonStyle.primary, row=4)
    async def b0(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "0")

    @discord.ui.button(label=".", style=discord.ButtonStyle.secondary, row=4)
    async def dot_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, ".")

    @discord.ui.button(label=")", style=discord.ButtonStyle.secondary, row=4)
    async def close_paren(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, ")")


    @discord.ui.button(label="AC", style=discord.ButtonStyle.danger, row=4)
    async def ac_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """
        All-Clear: wipe the expression and any error.

        Deliberately preserves ``ans`` and ``history`` so the user can
        still reference the last result via Ans and review past work.
        Matches Casio behaviour (AC ≠ power-off reset).
        """
        self.expr = ""
        self.error = None
        await self.refresh(interaction)
        
    @discord.ui.button(label="=", style=discord.ButtonStyle.success, row=4)
    async def eq_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """
        Evaluate the current expression.

        On **success**
            * The result is stored in ``self.ans`` (Ans token).
            * The original expression is appended to ``self.history``.
            * ``self.expr`` is **replaced** with the bare numeric result
              (no commas) so that pressing digit buttons afterwards
              continues from a valid numeric state.  This matches the
              behaviour of physical Casio calculators where the display
              shows the answer after = and digits start a new entry.

        On **error**
            * ``self.error`` is set to the CalcError instance, which
              carries both a short message and an optional hint.
            * ``self.expr`` is preserved so the user can correct it.
        """
        try:
            original = self.expr
            result = evaluate(self.expr, ans=self.ans, mode=self.angle_mode)
            self.ans = result
            self.error = None
            self.history.append((original, result))
            if len(self.history) > MAX_HISTORY:
                self.history.pop(0)
        except CalcError as exc:
            self.error = exc   # preserve full CalcError (message + hint)

        await self.refresh(interaction)

# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════


class Calculator(
    commands.Cog,
    name="Calculator System",
    description="Scientific calculator — one-shot /calc or interactive /calculator.",
):
    """
    Calculator commands cog.

    Registers two hybrid commands that share the same engine
    (``util.calculator.engine``), so both accept identical expression
    syntax — see that module's docstring for the full reference.

    /calc <expression>
        Evaluates an expression immediately and replies with a
        colour-coded embed.  Great for quick one-off calculations.
        Accepts the same text-prefix aliases ``!calc`` and ``!calculate``.

    /calculator
        Opens a full interactive panel with:
          • a 25-option scientific-function dropdown (CalculatorSelect)
          • a complete numeric keypad + arithmetic operators + brackets
          • a dedicated ANS button for one-tap last-result insertion
          • a free-form text-input modal via the "📝 Type…" option
          • DEG / RAD mode toggle in the same dropdown
          • calculation history shown live in the embed
          • human-friendly error messages with fix suggestions
        The panel is private to the invoking user and auto-disables
        after 5 minutes of inactivity.
    """

    COG_EMOJI = "🧮"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="calc",
        aliases=["calculate", "math"],
        description="Evaluate a scientific expression instantly.",
    )
    async def calc(self, ctx: commands.Context, *, expression: str) -> None:
        """
        Evaluate *expression* and post a colour-coded result embed.

        A green embed indicates success; red indicates an error.
        Errors include a short description and a 💡 fix suggestion.

        Supports the full engine syntax: trig (DEG mode), log/ln, sqrt,
        factorial, nCr/nPr, gcd/lcm, floor/ceil, π, e, and more.
        See ``util.calculator.engine`` for the complete reference.

        Parameters
        ----------
        expression:
            Any Casio-style expression string, e.g. ``sin(45) + √16``.
        """
        try:
            result = evaluate(expression)
            embed = discord.Embed(
                title="🧮 Result", colour=discord.Colour.green())
            embed.add_field(
                name="Input",  value=f"```{expression}```",            inline=False)
            embed.add_field(
                name="Output", value=f"```{format_result(result)}```", inline=False)
        except CalcError as exc:
            embed = discord.Embed(
                title="🧮 Error",
                colour=discord.Colour.red(),
            )
            embed.add_field(
                name="⚠️ Problem",
                value=f"```{exc.message}```",
                inline=False,
            )
            if exc.hint:
                embed.add_field(
                    name="💡 Suggestion",
                    value=exc.hint,
                    inline=False,
                )
            embed.set_footer(text=f"Expression: {expression[:80]}")

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="calculator",
        aliases=["calc_panel", "calcpad"],
        description="Open an interactive scientific calculator pad.",
    )
    async def calculator(self, ctx: commands.Context) -> None:
        """
        Open a personal interactive scientific calculator panel.

        The panel is private — only the invoking user can press its
        buttons.  It auto-disables after 5 minutes of inactivity.

        Use the **ƒ(x) dropdown** (top row) to access scientific
        functions, constants, the DEG / RAD toggle, and the expression
        text-input modal.  The dedicated **ANS** button inserts the last
        result in one tap.  The **=** button stores the answer and resets
        the input display to the numeric result, matching real Casio
        behaviour.  Errors show a plain-English description with a fix hint.
        """
        view = CalculatorView(ctx.author.id)
        await ctx.send(embed=view._embed(), view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Calculator(bot))
