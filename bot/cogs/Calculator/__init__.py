"""
Calculator cog
──────────────
Modernised replacement for the TagScriptEngine-based calculator.

Two ways to calculate:
  /calc <expression>   — instant one-shot result (hybrid command)
  /calculator          — opens an interactive Casio fx-991MS style keypad:
                            • a Select menu for scientific functions/constants
                            • a digit/operator button grid
                            • a Modal ("Type…") for typing full expressions

discord.py Views allow max 25 children. A Select counts as 1, leaving
24 buttons — exactly enough for digits 0-9, ( ) ! + - × ÷ . = AC DEL Ans
plus Type…  (24 total).
"""

import discord
from discord.ext import commands
from util.calculator import CalcError, evaluate, format_result

MAX_DISPLAY = 60  # truncate long expressions in the embed

# (label shown in select, token inserted into the expression)
_FUNCTION_OPTIONS: list[tuple[str, str, str]] = [
    ("sin",   "sin(",   "Sine (degrees)"),
    ("cos",   "cos(",   "Cosine (degrees)"),
    ("tan",   "tan(",   "Tangent (degrees)"),
    ("sin⁻¹", "sin⁻¹(", "Inverse sine"),
    ("cos⁻¹", "cos⁻¹(", "Inverse cosine"),
    ("tan⁻¹", "tan⁻¹(", "Inverse tangent"),
    ("log",   "log(",   "Log base 10"),
    ("ln",    "ln(",    "Natural log"),
    ("√",     "√(",     "Square root"),
    ("^",     "^",      "Power (x^y)"),
    ("nCr",   "nCr(",   "Combinations nCr(n, r)"),
    ("nPr",   "nPr(",   "Permutations nPr(n, r)"),
    ("π",     "pi",     "Pi constant"),
    ("e",     "e",      "Euler's number"),
]


class ExpressionModal(discord.ui.Modal, title="Enter expression"):
    """Lets the user type/paste a full expression instead of tapping buttons."""

    expression: discord.ui.TextInput = discord.ui.TextInput(
        label="Expression",
        placeholder="e.g. sin(45) + sqrt(16) * 2",
        style=discord.TextStyle.short,
        max_length=200,
        required=True,
    )

    def __init__(self, view: "CalculatorView") -> None:
        super().__init__()
        self.view_ref = view
        self.expression.default = view.expr or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view_ref.expr = str(self.expression.value)
        await self.view_ref.refresh(interaction)


class FunctionSelect(discord.ui.Select):
    """Dropdown of scientific functions/constants — inserts a token on select."""

    def __init__(self, view: "CalculatorView") -> None:
        self.view_ref = view
        options = [
            discord.SelectOption(label=label, value=token, description=desc)
            for label, token, desc in _FUNCTION_OPTIONS
        ]
        super().__init__(
            placeholder="ƒ(x) — scientific functions & constants",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view_ref.error = None
        self.view_ref.expr += self.values[0]
        await self.view_ref.refresh(interaction)


class CalculatorView(discord.ui.View):
    """
    Interactive scientific calculator.

    Row 0: Select menu — scientific functions & constants (sin, log, π, …)
    Row 1: 7 8 9 ÷ DEL
    Row 2: 4 5 6 × AC
    Row 3: 1 2 3 - (
    Row 4: 0 . + = )  plus Ans / Type… via overflow handled below
    """

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.expr: str = ""
        self.ans: float = 0.0
        self.error: str | None = None
        self.add_item(FunctionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                ":no_entry: This calculator isn't yours.", ephemeral=True
            )
            return False
        return True

    def _embed(self) -> discord.Embed:
        display = self.expr or "0"
        if len(display) > MAX_DISPLAY:
            display = "…" + display[-(MAX_DISPLAY - 1):]

        embed = discord.Embed(title="🧮 Scientific Calculator", colour=discord.Colour.blurple())
        embed.add_field(name="Expression", value=f"```{display}```", inline=False)
        if self.error:
            embed.add_field(name="Error", value=f"```{self.error}```", inline=False)
        else:
            embed.add_field(name="Result", value=f"```{format_result(self.ans)}```", inline=False)
        embed.set_footer(text="DEG mode • Ans = last result • Type… for full expressions")
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self._embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _append(self, interaction: discord.Interaction, token: str) -> None:
        self.error = None
        self.expr += token
        await self.refresh(interaction)

    # ── Row 1: digits 7-9 + divide + DEL ─────────────────────────────────────
    @discord.ui.button(label="7", style=discord.ButtonStyle.primary, row=1)
    async def b7(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "7")

    @discord.ui.button(label="8", style=discord.ButtonStyle.primary, row=1)
    async def b8(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "8")

    @discord.ui.button(label="9", style=discord.ButtonStyle.primary, row=1)
    async def b9(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "9")

    @discord.ui.button(label="÷", style=discord.ButtonStyle.secondary, row=1)
    async def div_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "/")

    @discord.ui.button(label="DEL", style=discord.ButtonStyle.danger, row=1)
    async def del_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.error = None
        self.expr = self.expr[:-1]
        await self.refresh(interaction)

    # ── Row 2: digits 4-6 + multiply + AC ────────────────────────────────────
    @discord.ui.button(label="4", style=discord.ButtonStyle.primary, row=2)
    async def b4(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "4")

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary, row=2)
    async def b5(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "5")

    @discord.ui.button(label="6", style=discord.ButtonStyle.primary, row=2)
    async def b6(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "6")

    @discord.ui.button(label="×", style=discord.ButtonStyle.secondary, row=2)
    async def mul_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "*")

    @discord.ui.button(label="AC", style=discord.ButtonStyle.danger, row=2)
    async def ac_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.expr = ""
        self.ans = 0
        self.error = None
        await self.refresh(interaction)

    # ── Row 3: digits 1-3 + subtract + Ans ───────────────────────────────────
    @discord.ui.button(label="1", style=discord.ButtonStyle.primary, row=3)
    async def b1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "1")

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary, row=3)
    async def b2(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "2")

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary, row=3)
    async def b3(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "3")

    @discord.ui.button(label="-", style=discord.ButtonStyle.secondary, row=3)
    async def sub_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "-")

    @discord.ui.button(label="Ans", style=discord.ButtonStyle.success, row=3)
    async def ans_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "ans")

    # ── Row 4: 0, ., +, =, Type… ─────────────────────────────────────────────
    @discord.ui.button(label="0", style=discord.ButtonStyle.primary, row=4)
    async def b0(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "0")

    @discord.ui.button(label=".", style=discord.ButtonStyle.primary, row=4)
    async def dot_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, ".")

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=4)
    async def add_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._append(interaction, "+")

    @discord.ui.button(label="=", style=discord.ButtonStyle.success, row=4)
    async def eq_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            self.ans = evaluate(self.expr, ans=self.ans)
            self.error = None
        except CalcError as exc:
            self.error = str(exc)
        await self.refresh(interaction)

    @discord.ui.button(label="Type…", style=discord.ButtonStyle.secondary, row=4)
    async def type_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ExpressionModal(self))


class Calculator(
    commands.Cog,
    name="Calculator System",
    description="A scientific calculator — type an expression or use the interactive keypad.",
):
    COG_EMOJI = "🧮"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="calc",
        aliases=["calculate"],
        description="Evaluate a scientific expression, e.g. sin(45) + sqrt(16) * 2",
    )
    async def calc(self, ctx: commands.Context, *, expression: str) -> None:
        try:
            result = evaluate(expression)
            embed = discord.Embed(title="🧮 Result", colour=discord.Colour.green())
            embed.add_field(name="Input", value=f"```{expression}```", inline=False)
            embed.add_field(name="Output", value=f"```{format_result(result)}```", inline=False)
        except CalcError as exc:
            embed = discord.Embed(
                title="🧮 Error", description=f"```{exc}```", colour=discord.Colour.red()
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="calculator", description="Open an interactive scientific calculator.")
    async def calculator(self, ctx: commands.Context) -> None:
        view = CalculatorView(ctx.author.id)
        await ctx.send(embed=view._embed(), view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Calculator(bot))