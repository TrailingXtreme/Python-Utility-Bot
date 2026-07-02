"""Games Cog

Commands
────────
  /tictactoe          – start a 3×3 TicTacToe game
                         mode=bot   → requires difficulty (easy/medium/hard)
                         mode=human → requires opponent (who you're challenging)
  /wordle <mode>      – Wordle: random | daily | id | info
                        (mode is an app_commands.Choice; puzzle_id is an
                         optional int parameter, only used with mode=id)

Listeners
─────────
  on_message         – intercepts replies to active Wordle boards and
                       processes them as guesses (no change in behaviour
                       vs the original)

Removed vs original
───────────────────
  @commands.command / @commands.group  (prefix commands)
  @nextcord.slash_command / nextcord.SlashOption
  util.messages.DeleteMessageSlash / DeleteMessage
    → replaced by the inline _WordleDeleteView defined here
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Emojis
from util.game import (
    TicTacToe,
    daily_puzzle_id,
    generate_info_embed,
    generate_puzzle_embed,
    process_message_as_guess,
    random_puzzle_id,
)

# ── Slash-command choices for /tictactoe ──────────────────────────────────────

_TICTACTOE_MODE_CHOICES = [
    app_commands.Choice(name="🤖 Vs Bot",      value="bot"),
    app_commands.Choice(name="🧑‍🤝‍🧑 Vs Human", value="human"),
]

_TICTACTOE_DIFFICULTY_CHOICES = [
    app_commands.Choice(name="🟢Easy",   value="easy"),
    app_commands.Choice(name="🟡Medium", value="medium"),
    app_commands.Choice(name="🔴Hard",   value="hard"),
]


# ── Slash-command choices for /wordle ─────────────────────────────────────────

_WORDLE_CHOICES = [
    app_commands.Choice(name=f"{Emojis.game_die} Random",  value="random"),
    app_commands.Choice(name=f"{Emojis.calendar} Daily",   value="daily"),
    app_commands.Choice(name=f"{Emojis._emoji_1234} By ID",   value="id"),
    app_commands.Choice(name=f"{Emojis.info} Info",    value="info"),
]


# ── Shared delete-button view ─────────────────────────────────────────────────

class _WordleDeleteView(discord.ui.View):
    """
    Attaches a single 🗑️ Delete button to a Wordle board message.
    Only the user who started the game can use it.
    """

    def __init__(self, author: discord.User | discord.Member) -> None:
        super().__init__(timeout=None)   # live for the whole game session
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                f"{Emojis.no_entry} Only the player who started this game can delete it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji=Emojis.trashcan)
    async def delete(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Delete the board message entirely; the view stops automatically
        await interaction.message.delete()
        self.stop()


# ═════════════════════════════════════════════════════════════════════════════
# Cog
# ═════════════════════════════════════════════════════════════════════════════

class Games(commands.Cog, description="🎮 Play games directly in Discord."):

    COG_EMOJI = Emojis.game_controller

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Wordle on_message listener ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Route reply-messages to the Wordle engine.

        Only acts when:
          • the message is a reply to another message
          • the parent is a bot message with a Wordle embed
          • the replying user is the game owner
        Everything else is ignored and falls through to normal command processing.
        """
        # Ignore bot messages and non-reply messages early
        if message.author.bot:
            return

        ref = message.reference
        if not ref or not isinstance(ref.resolved, discord.Message):
            return

        await process_message_as_guess(self.bot, message)

    # ── /tictactoe ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="tictactoe",
        description="Start a Tic Tac Toe game — vs the bot or vs another player.",
    )
    @app_commands.describe(
        mode="Play against the bot or challenge another player.",
        difficulty="Bot difficulty — required when mode is 'Vs Bot'.",
        opponent="Who to challenge — required when mode is 'Vs Human'.",
    )
    @app_commands.choices(
        mode=_TICTACTOE_MODE_CHOICES,
        difficulty=_TICTACTOE_DIFFICULTY_CHOICES,
    )
    async def tictactoe(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        difficulty: Optional[app_commands.Choice[str]] = None,
        opponent: Optional[discord.Member] = None,
    ) -> None:
        """
        Creates a fresh 3×3 TicTacToe board.

          • mode = Vs Bot   – you play X, the bot plays O and moves
                              automatically right after you, at the chosen
                              `difficulty`.
          • mode = Vs Human – you play X, `opponent` plays O. Only the two
                              of you can click the board, and only on your
                              own turn (enforced in TicTacToe.interaction_check).
        """
        if mode.value == "bot":
            if difficulty is None:
                await interaction.response.send_message(
                    f"{Emojis.warning} Please choose a `difficulty` when playing against the bot.",
                    ephemeral=True,
                )
                return

            view = TicTacToe(
                player_x=interaction.user,
                vs_bot=True,
                difficulty=difficulty.value,
            )
            await interaction.response.send_message(
                f"🎮 **Tic Tac Toe** — {interaction.user.mention} vs 🤖 **Bot** "
                f"({difficulty.name}) — {Emojis.x} **X** goes first!",
                view=view,
            )

        else:  # mode.value == "human"
            if opponent is None:
                await interaction.response.send_message(
                    f"{Emojis.warning} Please choose an `opponent` when playing against another player.",
                    ephemeral=True,
                )
                return

            if opponent.id == interaction.user.id:
                await interaction.response.send_message(
                    f"{Emojis.warning} You can't challenge yourself — try `mode: Vs Bot` instead.",
                    ephemeral=True,
                )
                return

            if opponent.bot:
                await interaction.response.send_message(
                    f"{Emojis.warning} You can't challenge a bot account — use `mode: Vs Bot` instead.",
                    ephemeral=True,
                )
                return

            view = TicTacToe(player_x=interaction.user, player_o=opponent)
            await interaction.response.send_message(
                f"🎮 **Tic Tac Toe** — {interaction.user.mention} vs {opponent.mention} "
                f"— {Emojis.x} **X** goes first!",
                view=view,
            )

    @tictactoe.error
    async def tictactoe_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Catch-all for unexpected /tictactoe errors."""
        await interaction.response.send_message(
            f"{Emojis.warning} Something went wrong: `{error}`",
            ephemeral=True,
        )

    # ── /wordle ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="wordle",
        description="Play Wordle — guess the 5-letter word in 6 tries.",
    )
    @app_commands.describe(
        mode="Game mode: random, daily, by ID, or info.",
        puzzle_id="Puzzle ID — only used when mode is 'id'.",
    )
    @app_commands.choices(mode=_WORDLE_CHOICES)
    async def wordle(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        puzzle_id: Optional[int] = None,
    ) -> None:
        view = _WordleDeleteView(interaction.user)

        match mode.value:

            case "random":
                embed = generate_puzzle_embed(
                    self.bot, interaction.user, random_puzzle_id()
                )
                await interaction.response.send_message(embed=embed, view=view)

            case "daily":
                embed = generate_puzzle_embed(
                    self.bot, interaction.user, daily_puzzle_id()
                )
                await interaction.response.send_message(embed=embed, view=view)

            case "id":
                if puzzle_id is None:
                    await interaction.response.send_message(
                        f"{Emojis.warning} Please provide a `puzzle_id` when using "
                        "the **id** mode.",
                        ephemeral=True,
                    )
                    return
                embed = generate_puzzle_embed(self.bot, interaction.user, puzzle_id)
                await interaction.response.send_message(embed=embed, view=view)

            case "info":
                # Info embed has no ongoing game; no delete button needed
                await interaction.response.send_message(embed=generate_info_embed())

            case _:
                # Unreachable — Discord validates Choice values before dispatch
                await interaction.response.send_message(
                    f"{Emojis.warning} Unknown mode. This should never happen.",
                    ephemeral=True,
                )

    @wordle.error
    async def wordle_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Catch-all for unexpected /wordle errors."""
        await interaction.response.send_message(
            f"{Emojis.warning} Something went wrong: `{error}`",
            ephemeral=True,
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))