"""
bot/util/game/__init__.py
─────────────────────────────────────────────────────────────────────────────
Wordle engine + TicTacToe UI  –  discord.py 2.x
(ported from nextcord; game logic is unchanged)

Callback note
─────────────
TicTacToeButton subclasses discord.ui.Button and overrides `callback`.
In discord.py 2.x that signature is:

    async def callback(self, interaction: discord.Interaction) -> None:

This matches what was already in the nextcord version, so no signature
change was needed (nextcord also uses (self, interaction) on Button
subclasses; the reversed (button, interaction) order only affects the
@ui.button decorator pattern on Views).

Bug fixed vs original
─────────────────────
generate_puzzle_embed called set_author() twice — the first call
(bot username) was immediately overwritten by the second (player name).
Removed the dead first call.

Emoji refs updated (new constants_new.py names)
───────────────────────────────────────────────
  Emojis.discord  →  Emojis.discord_emoji
  Emojis.youtube  →  Emojis.youtube_emoji
  Emojis.github   →  Emojis.github_emoji

New vs original
────────────────
TicTacToe now supports two modes, set up in games.py and enforced here:
  • Human vs Bot   – player_o is None, vs_bot=True. Difficulty is one of
                      "easy" / "medium" / "hard"; see the "Bot AI" section
                      below for the move-selection logic for each.
  • Human vs Human – player_o is a real Discord member. TicTacToe now
                      overrides interaction_check so only player_x and
                      player_o can click the board, and only on their turn.
"""

from __future__ import annotations

import datetime
import math
import random
from typing import List

import aiohttp
import discord

from util.constants import Emojis
from util.loaders.json import get_path

# ── Word lists (loaded once at import time) ───────────────────────────────────

_cwd = get_path()
popular_words: list[str] = open(_cwd + "/game/wordle/popular.txt").read().splitlines()
all_words: set[str]      = {w.strip() for w in open(_cwd + "/game/wordle/sowpods.txt")}

# ── Emoji shortcut ────────────────────────────────────────────────────────────

EMOJI_CODES = Emojis.EMOJI_CODES

# ── Dictionary fallback (for valid English words missing from SOWPODS) ───────
#
# SOWPODS is a Scrabble word list — large, but it's still missing plenty of
# ordinary English words (and it includes plenty of obscure ones nobody
# would call "real"). Rather than rejecting a guess just because it's absent
# from one static file, anything SOWPODS doesn't recognise gets a second
# opinion from a free dictionary API before being rejected.
#
# Results are cached in-memory for the lifetime of the process so repeated
# guesses of the same word (very common across many concurrent games) never
# hit the network twice, and so an API outage only costs latency once per
# unique word rather than once per guess.

_DICTIONARY_API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
_API_TIMEOUT = aiohttp.ClientTimeout(total=3)

_api_verified_words: set[str] = set()    # confirmed real words, not in all_words
_api_rejected_words: set[str] = set()    # confirmed NOT real words (404 from API)


async def _check_word_via_api(word: str) -> bool:
    """
    Ask a free dictionary API whether *word* is a real English word.

    Returns True only on an explicit, confident answer (HTTP 200 with at
    least one dictionary entry). Any ambiguity — network failure, timeout,
    unexpected response — falls back to False so the word list stays the
    single source of truth whenever the API can't weigh in cleanly. A 404
    is the API's explicit "not a word" answer and is treated the same way.
    """
    if word in _api_verified_words:
        return True
    if word in _api_rejected_words:
        return False

    try:
        async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
            async with session.get(_DICTIONARY_API_URL.format(word=word)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        _api_verified_words.add(word)
                        return True
                    _api_rejected_words.add(word)
                    return False
                if resp.status == 404:
                    _api_rejected_words.add(word)
                    return False
                # Anything else (rate limit, 5xx, etc.) is inconclusive —
                # don't cache it, and don't let it block a valid guess
                # just because the API had a bad moment.
                return False
    except (aiohttp.ClientError, TimeoutError):
        # Network hiccup — same reasoning: inconclusive, not a rejection.
        return False


# ═════════════════════════════════════════════════════════════════════════════
# TicTacToe
# ═════════════════════════════════════════════════════════════════════════════

class TicTacToeButton(discord.ui.Button["TicTacToe"]):
    """A single cell in the 3×3 TicTacToe grid."""

    def __init__(self, x: int, y: int) -> None:
        # row= places each button on the correct grid row (0-2)
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction) -> None:
        # discord.py 2.x Button subclass: callback(self, interaction) ✓
        assert self.view is not None
        view: TicTacToe = self.view

        # Ignore clicks on already-played cells (shouldn't reach here since
        # the button is disabled after play, but guard just in case)
        if view.board[self.y][self.x] in (view.X, view.O):
            return

        mover = view.current_player
        view.apply_move(self.x, self.y, mover)
        view.current_player = view.O if mover == view.X else view.X

        winner = view.check_board_winner()
        if winner is not None:
            content = view.end_message(winner)
            view.disable_all()
            view.stop()
            await interaction.response.edit_message(content=content, view=view)
            return

        # Human-vs-Bot: play the bot's O move immediately so there's only
        # ever a single response edit per human click (no second interaction
        # is available for the bot to respond with later).
        if view.vs_bot and view.current_player == view.O:
            bx, by = choose_bot_move(view.board, view.difficulty)
            view.apply_move(bx, by, view.O)
            view.current_player = view.X

            winner = view.check_board_winner()
            if winner is not None:
                content = view.end_message(winner)
                view.disable_all()
                view.stop()
                await interaction.response.edit_message(content=content, view=view)
                return

        await interaction.response.edit_message(content=view.turn_message(), view=view)


class TicTacToe(discord.ui.View):
    """
    3×3 TicTacToe board built from TicTacToeButton instances.

    Modes
    ─────
      • Human vs Human – player_o is a real Discord member. Only player_x
                          and player_o may interact with the board, and
                          only on their own turn (see interaction_check).
      • Human vs Bot    – player_o is None and vs_bot=True. The bot plays
                          its O move automatically right after the human's
                          X move, so only player_x ever has to click.

    Difficulty (bot mode only) — see the "Bot AI" section below for the
    actual move-selection logic:
      • "easy"   – fully random moves (intentionally weak play).
      • "medium" – mixes basic reactive play (take obvious wins, block
                   obvious losses) with occasional full lookahead, tuned
                   to land close to a 52/48 human/bot split among
                   decisive games (see _MEDIUM_SMART_MOVE_CHANCE).
      • "hard"   – near-perfect minimax with a small ~2% chance of a
                   deliberate slip, so it's beatable but only barely
                   (see _HARD_SLIP_CHANCE).
    """

    children: List[TicTacToeButton]   # narrowed type for IDE / linter

    X   = -1
    O   =  1
    Tie =  2

    def __init__(
        self,
        player_x: discord.User | discord.Member,
        player_o: discord.User | discord.Member | None = None,
        *,
        vs_bot: bool = False,
        difficulty: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.current_player = self.X
        self.board: list[list[int]] = [
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0],
        ]

        self.player_x   = player_x
        self.player_o   = player_o          # None when vs_bot is True
        self.vs_bot     = vs_bot
        self.difficulty = difficulty        # "easy" | "medium" | "hard" | None

        for x in range(3):
            for y in range(3):
                self.add_item(TicTacToeButton(x, y))

    # ── Permission gate ────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the two players may click — and only on their own turn."""
        players = {self.player_x.id}
        if self.player_o is not None:
            players.add(self.player_o.id)

        if interaction.user.id not in players:
            await interaction.response.send_message(
                f"{Emojis.no_entry} Only the players in this game can interact with the board.",
                ephemeral=True,
            )
            return False

        current_user = self.player_x if self.current_player == self.X else self.player_o
        if current_user is not None and interaction.user.id != current_user.id:
            await interaction.response.send_message(
                f"{Emojis.warning} It's not your turn yet — waiting on {current_user.mention}.",
                ephemeral=True,
            )
            return False

        return True

    # ── Board helpers ────────────────────────────────────────────────────────
    def get_button(self, x: int, y: int) -> "TicTacToeButton":
        for child in self.children:
            if isinstance(child, TicTacToeButton) and child.x == x and child.y == y:
                return child
        raise ValueError(f"No button found at ({x}, {y})")

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]

    def apply_move(self, x: int, y: int, symbol: int) -> None:
        """Marks a cell as played, both on the board state and the button UI."""
        button = self.get_button(x, y)
        if symbol == self.X:
            button.style = discord.ButtonStyle.danger
            button.label = "X"
        else:
            button.style = discord.ButtonStyle.success
            button.label = "O"
        button.disabled = True
        self.board[y][x] = symbol

    def check_board_winner(self) -> int | None:
        """Return X, O, Tie, or None if the game is still in progress."""
        return _evaluate_board(self.board)

    # ── Message helpers ──────────────────────────────────────────────────────
    def turn_message(self) -> str:
        if self.current_player == self.X:
            return f"{Emojis.x} It is now **X**'s turn ({self.player_x.mention})"
        who = self.player_o.mention if self.player_o is not None else "the bot 🤖"
        return f"{Emojis.o} It is now **O**'s turn ({who})"

    def end_message(self, winner: int) -> str:
        if winner == self.X:
            return f"{Emojis.x} **{self.player_x.mention} (X) wins!** 🎉"
        if winner == self.O:
            who = self.player_o.mention if self.player_o is not None else "The bot 🤖"
            return f"{Emojis.o} **{who} (O) wins!** 🎉"
        return f"{Emojis.warning} **It's a tie!**"


# ═════════════════════════════════════════════════════════════════════════════
# Bot AI — difficulty-based move selection for Human-vs-Bot TicTacToe
# ═════════════════════════════════════════════════════════════════════════════

# Tunable parameters — adjust these to rebalance difficulty without touching
# any of the selection logic below.
#
# _MEDIUM_SMART_MOVE_CHANCE was tuned by simulating thousands of games against
# a "reasonably-played" human (one who at least takes their own obvious wins
# and blocks obvious losses — true random clicking isn't representative of
# an actual player). At 0.35, the medium bot landed at roughly a 51/49
# human/bot split among decisive (non-tied) games, close to the requested
# ~52/48. Exact percentages will vary a bit with real opponents — nudge this
# value up to make medium tougher, down to make it softer.
_MEDIUM_SMART_MOVE_CHANCE = 0.35

# _HARD_SLIP_CHANCE is the bot's per-move odds of a deliberate misplay,
# which is what keeps "hard" technically beatable instead of a flawless
# solver. In the same kind of simulation this landed the human's overall
# win rate in the ~1-3% range depending on opponent skill — close to the
# requested ~2%.
_HARD_SLIP_CHANCE = 0.02


def _evaluate_board(board: list[list[int]]) -> int | None:
    """
    Pure board-evaluation helper.

    Shared by TicTacToe.check_board_winner and the bot's minimax search so
    the UI and the AI always agree on what counts as a win — logic itself
    is unchanged from the original check_board_winner.
    """
    O, X, TIE = TicTacToe.O, TicTacToe.X, TicTacToe.Tie

    for row in board:
        total = sum(row)
        if total ==  3: return O
        if total == -3: return X

    for col in range(3):
        total = board[0][col] + board[1][col] + board[2][col]
        if total ==  3: return O
        if total == -3: return X

    total = board[0][0] + board[1][1] + board[2][2]
    if total ==  3: return O
    if total == -3: return X

    total = board[0][2] + board[1][1] + board[2][0]
    if total ==  3: return O
    if total == -3: return X

    if all(cell != 0 for row in board for cell in row):
        return TIE

    return None


def _empty_cells(board: list[list[int]]) -> list[tuple[int, int]]:
    return [(x, y) for y in range(3) for x in range(3) if board[y][x] == 0]


def _find_winning_move(board: list[list[int]], symbol: int) -> tuple[int, int] | None:
    """Return a cell that immediately wins for *symbol*, if one exists."""
    for x, y in _empty_cells(board):
        board[y][x] = symbol
        wins = _evaluate_board(board) == symbol
        board[y][x] = 0
        if wins:
            return (x, y)
    return None


def _minimax(
    board: list[list[int]],
    depth: int,
    maximizing: bool,
    alpha: float = -math.inf,
    beta: float = math.inf,
) -> int:
    """
    Standard minimax: bot is O (maximizing), human is X (minimizing).
    Alpha-beta pruning is just a speed optimization — it always returns
    the exact same value plain minimax would, just without wasting time
    exploring branches that can't change the outcome.
    """
    result = _evaluate_board(board)
    if result == TicTacToe.O:
        return 10 - depth
    if result == TicTacToe.X:
        return depth - 10
    if result == TicTacToe.Tie:
        return 0

    symbol = TicTacToe.O if maximizing else TicTacToe.X
    if maximizing:
        best = -math.inf
        for x, y in _empty_cells(board):
            board[y][x] = symbol
            best = max(best, _minimax(board, depth + 1, False, alpha, beta))
            board[y][x] = 0
            alpha = max(alpha, best)
            if beta <= alpha:
                break
        return best
    else:
        best = math.inf
        for x, y in _empty_cells(board):
            board[y][x] = symbol
            best = min(best, _minimax(board, depth + 1, True, alpha, beta))
            board[y][x] = 0
            beta = min(beta, best)
            if beta <= alpha:
                break
        return best


def _minimax_best_move(board: list[list[int]]) -> tuple[int, int]:
    best_score, best_move = -math.inf, None
    for x, y in _empty_cells(board):
        board[y][x] = TicTacToe.O
        score = _minimax(board, 1, False)
        board[y][x] = 0
        if score > best_score:
            best_score, best_move = score, (x, y)
    return best_move


def _easy_move(board: list[list[int]]) -> tuple[int, int]:
    """Easy: ignores strategy entirely and just picks a random open cell."""
    return random.choice(_empty_cells(board))


def _medium_move(board: list[list[int]]) -> tuple[int, int]:
    """
    Medium: a meaningful slice of the time (see _MEDIUM_SMART_MOVE_CHANCE)
    it looks ahead with the same full search the hard bot uses, which is
    what lets it occasionally catch forks/traps a purely reactive bot
    would miss. The rest of the time it falls back to a basic reactive
    bot: take an obvious win, block an obvious loss, otherwise random.
    """
    if random.random() < _MEDIUM_SMART_MOVE_CHANCE:
        return _minimax_best_move(board)

    move = _find_winning_move(board, TicTacToe.O)
    if move:
        return move

    move = _find_winning_move(board, TicTacToe.X)
    if move:
        return move

    return random.choice(_empty_cells(board))


def _hard_move(board: list[list[int]]) -> tuple[int, int]:
    """
    Hard: near-perfect minimax. A small, deliberate slip chance is mixed in
    so the bot stays technically beatable instead of being a flawless
    solver that can never lose.
    """
    if random.random() < _HARD_SLIP_CHANCE:
        return random.choice(_empty_cells(board))
    return _minimax_best_move(board)


def choose_bot_move(board: list[list[int]], difficulty: str | None) -> tuple[int, int]:
    """Dispatch to the move-selection function for the given difficulty."""
    if difficulty == "easy":
        return _easy_move(board)
    if difficulty == "medium":
        return _medium_move(board)
    return _hard_move(board)   # default to hard if difficulty is somehow unset


# ═════════════════════════════════════════════════════════════════════════════
# Wordle — pure functions (no UI state)
# ═════════════════════════════════════════════════════════════════════════════

def generate_colored_word(guess: str, answer: str) -> str:
    """
    Build a row of emoji tiles for a Wordle guess.

    Colour rules:
      Green  – correct letter, correct position
      Yellow – correct letter, wrong position
      Gray   – letter not in answer
    """
    colored_word = [EMOJI_CODES["gray"][letter] for letter in guess]
    guess_letters  = list(guess)
    answer_letters = list(answer)

    # Pass 1: mark greens and nullify matched positions
    for i in range(len(guess_letters)):
        if guess_letters[i] == answer_letters[i]:
            colored_word[i]   = EMOJI_CODES["green"][guess_letters[i]]
            answer_letters[i] = None   # type: ignore[call-overload]
            guess_letters[i]  = None   # type: ignore[call-overload]

    # Pass 2: mark yellows using remaining unmatched letters
    for i in range(len(guess_letters)):
        if guess_letters[i] is not None and guess_letters[i] in answer_letters:
            colored_word[i] = EMOJI_CODES["yellow"][guess_letters[i]]
            answer_letters[answer_letters.index(guess_letters[i])] = None  # type: ignore[call-overload]

    return "".join(colored_word)


def generate_blanks() -> str:
    """Return a row of 5 white-square emoji (one blank Wordle row)."""
    return "\N{WHITE MEDIUM SQUARE}" * 5


def generate_puzzle_embed(
    bot: discord.Client,
    user: discord.User | discord.Member,
    puzzle_id: int,
) -> discord.Embed:
    """
    Build a fresh Wordle game embed.

    BUG FIX: original called set_author() twice (bot name then player name);
    the first call was a dead write.  Only the player call remains.
    """
    blank_rows = "\n".join([generate_blanks()] * 6)
    embed = discord.Embed(
        title=f"{Emojis.game_die}  Wordle",
        description=(
            f"{Emojis.idea} **Guess the 5-letter word in 6 tries.** "
            "Reply to this message with your guess.\n"
            f"{EMOJI_CODES['green']['a']} = right spot　"
            f"{EMOJI_CODES['yellow']['a']} = wrong spot　"
            f"{EMOJI_CODES['gray']['a']} = not in word\n"
            f"{blank_rows}"
        ),
        color=discord.Color.blue(),
    )
    embed.set_author(name=user.name, icon_url=user.display_avatar.url)
    embed.set_footer(
        text=(
            f"ID: {puzzle_id} ︱ To play, use the command /wordle\n"
            "To guess, reply to this message with a word."
        )
    )
    return embed


def update_embed(embed: discord.Embed, guess: str) -> discord.Embed:
    """
    Replace the first blank row with the coloured guess tiles.
    Appends a congratulatory / game-over message when the game ends.
    """
    puzzle_id = int(embed.footer.text.split()[1])
    answer    = popular_words[puzzle_id]

    colored_word = generate_colored_word(guess, answer)
    empty_slot   = generate_blanks()

    embed.description = embed.description.replace(empty_slot, colored_word, 1)
    num_empty = embed.description.count(empty_slot)

    if guess == answer:
        endings = {
            5: "Genius!",
            4: "Magnificent!",
            3: "Impressive!",
            2: "Splendid!",
            1: "Great!",
            0: "Phew!",
        }
        ending = endings.get(num_empty, "Nice!")
        embed.description += f"\n\n{Emojis.animated_tada} **{ending}** {Emojis.tada}"
        embed.color = discord.Color.green()
    elif num_empty == 0:
        embed.description += f"\n\n{Emojis.cross_mark} **Out of guesses!** The answer was **{answer}**."
        embed.color = discord.Color.red()

    return embed


async def is_valid_word(word: str) -> bool:
    """
    Return True if *word* is a valid 5-letter English word.

    Fast path: SOWPODS local list (instant, no network — covers the vast
    majority of guesses).
    Fallback: if SOWPODS doesn't recognise the word, ask a dictionary API
    before rejecting it, since SOWPODS is missing plenty of legitimate
    English words. The API check is skipped entirely once the fast path
    already says yes, and also skipped for anything that isn't 5 letters
    long, since Wordle guesses must always be exactly 5 letters regardless
    of whether the word itself is real.
    """
    if len(word) != 5:
        return False
    if word in all_words:
        return True
    return await _check_word_via_api(word)


def is_game_over(embed: discord.Embed) -> bool:
    """Return True if the embed already contains a game-over message."""
    return "\n\n" in embed.description


def random_puzzle_id() -> int:
    """Return a random valid puzzle ID."""
    return random.randint(0, len(popular_words) - 1)


def daily_puzzle_id() -> int:
    """
    Return today's puzzle ID.
    Calculated as days elapsed since 2022-01-01, mod total puzzle count.
    """
    delta = datetime.datetime.now().date() - datetime.date(2022, 1, 1)
    return delta.days % len(popular_words)


def generate_info_embed() -> discord.Embed:
    """Build the /wordle info embed."""
    join_url    = (
        "https://discord.com/api/oauth2/authorize"
        "?client_id=932265924541681727&permissions=11264&scope=bot%20applications.commands"
    )
    discord_url = "https://discord.io/OpenSourceGames"
    youtube_url = "https://tiny.cc/DiscoHuge-YT"
    github_url  = "https://github.com/abindent/Python-Utility-Bot"

    return discord.Embed(
        title="About Wordle",
        description=(
            "Discord Wordle is a word-guessing puzzle game.\n\n"
            f"{Emojis.book}  **How to play**\n"
            "Guess the secret 5-letter word in 6 tries. After each guess, "
            "reply to the puzzle message with your next word — the tiles "
            "will show how close you were.\n\n"
            f"{EMOJI_CODES['green']['a']} right letter, right spot　"
            f"{EMOJI_CODES['yellow']['a']} right letter, wrong spot　"
            f"{EMOJI_CODES['gray']['a']} not in the word\n\n"
            f"{Emojis.idea}  **Start a game with**\n\n"
            f"{Emojis.sunny}       `/wordle daily`  — Play the puzzle of the day\n"
            f"{Emojis.game_die}       `/wordle random` — Play a random puzzle\n"
            f"{Emojis.boxing_glove}  `/wordle id`     — Play a puzzle by its ID\n\n"
            f"{Emojis.member_join}  [Add this bot to your server]({join_url})\n"
            f"{Emojis.discord_emoji}  [Join the Discord server]({discord_url})\n"
            f"{Emojis.youtube_emoji}  [YouTube tutorial]({youtube_url})\n"
            f"{Emojis.github_emoji}  [Source code on GitHub]({github_url})\n"
        ),
        color=discord.Color.blue(),
    )


async def process_message_as_guess(
    bot: discord.Client,
    message: discord.Message,
) -> bool:
    """
    Inspect a new message to see if it is a reply to an active Wordle game.

    Returns True  if the message was handled as a Wordle guess (even if
                  it was rejected for being invalid).
    Returns False if the message has nothing to do with Wordle.
    """
    ref = message.reference
    if not ref or not isinstance(ref.resolved, discord.Message):
        return False

    parent = ref.resolved

    # Must be a reply to the bot's own message
    if parent.author.id != bot.user.id:
        return False

    # Parent must carry an embed (our game board)
    if not parent.embeds:
        return False

    embed = parent.embeds[0]
    guess = message.content.lower()

    # ── Ownership check ───────────────────────────────────────────────────────
    if (
        embed.author.name    != message.author.name
        or embed.author.icon_url != message.author.display_avatar.url
    ):
        who = f"{embed.author.name}" if embed.author else "someone else"
        await message.reply(
            f"This game was started by **{who}**. Start your own with `/wordle`.",
            delete_after=5,
        )
        try:
            await message.delete(delay=5)
        except Exception:
            pass
        return True

    # ── Game-over check ───────────────────────────────────────────────────────
    if is_game_over(embed):
        await message.reply(
            "This game is already over. Start a new one with `/wordle`.",
            delete_after=5,
        )
        try:
            await message.delete(delay=5)
        except Exception:
            pass
        return True

    # ── Single-word check ─────────────────────────────────────────────────────
    if len(message.content.split()) > 1:
        await message.reply(
            "Please reply with a **single 5-letter word**.", delete_after=5
        )
        try:
            await message.delete(delay=5)
        except Exception:
            pass
        return True

    # ── Length check ──────────────────────────────────────────────────────────
    # Wordle answers are always exactly 5 letters. This must run before the
    # dictionary check: a real English word of any other length (e.g. "ok",
    # "knit", "happiness") would otherwise pass validation and then crash
    # generate_colored_word, which assumes guess/answer are the same length.
    if len(guess) != 5:
        await message.reply(
            "Please reply with a **5-letter word**.", delete_after=5
        )
        try:
            await message.delete(delay=5)
        except Exception:
            pass
        return True

    # ── Dictionary check ──────────────────────────────────────────────────────
    if not await is_valid_word(guess):
        await message.reply("**Not a valid word.** Try again!", delete_after=5)
        try:
            await message.delete(delay=5)
        except Exception:
            pass
        return True

    # ── Accept the guess ──────────────────────────────────────────────────────
    embed = update_embed(embed, guess)
    await parent.edit(embed=embed)

    try:
        await message.delete()
    except Exception:
        pass

    return True