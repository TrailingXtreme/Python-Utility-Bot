"""
cogs/fun.py
───────────────────────────────────────────────────────────────────────────────
Fun Cog  –  discord.py v2.7.1+  (migrated from nextcord)

Commands
────────
  /echo        – repeat a message in the channel
  /8ball       – magic 8-ball with re-ask button
  /meme        – random Reddit meme with Next / Close buttons
  /joke        – JokeAPI v2 (jokeapi.dev) with category selector + re-roll
  /emoji add   – upload an external image as a custom server emoji
  /emojify     – convert plain text into regional-indicator emojis

API changes vs old cog
──────────────────────
  dadjoke (RapidAPI)  →  JokeAPI v2  (https://v2.jokeapi.dev, no key needed)
  meme-api.herokuapp  →  https://meme-api.com/gimme  (updated host)
  nextcord UI         →  discord.ui.View / Button / Select  (discord.py v2)
"""

from __future__ import annotations

import random
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Emojis, MemeAPIs

# ── Static data ───────────────────────────────────────────────────────────────

# (response text, discord.Color) – grouped by sentiment so the embed colour
# gives an immediate visual hint before the user even reads the answer.
_8BALL_POSITIVE: list[tuple[str, discord.Color]] = [
    ("It is certain.",           discord.Color.green()),
    ("It is decidedly so.",      discord.Color.green()),
    ("Without a doubt.",         discord.Color.green()),
    ("Yes – definitely.",        discord.Color.green()),
    ("You may rely on it.",      discord.Color.green()),
    ("As I see it, yes.",        discord.Color.green()),
    ("Most likely.",             discord.Color.green()),
    ("Outlook good.",            discord.Color.green()),
    ("Yes.",                     discord.Color.green()),
    ("Signs point to yes.",      discord.Color.green()),
]
_8BALL_NEUTRAL: list[tuple[str, discord.Color]] = [
    ("Reply hazy, try again.",   discord.Color.yellow()),
    ("Ask again later.",         discord.Color.yellow()),
    ("Better not tell you now.", discord.Color.yellow()),
    ("Concentrate and ask again.", discord.Color.yellow()),
    ("Cannot predict now.",      discord.Color.yellow()),
]
_8BALL_NEGATIVE: list[tuple[str, discord.Color]] = [
    ("Don't count on it.",       discord.Color.red()),
    ("My reply is no.",          discord.Color.red()),
    ("My sources say no.",       discord.Color.red()),
    ("Outlook not so good.",     discord.Color.red()),
    ("Very doubtful.",           discord.Color.red()),
]
_8BALL_RESPONSES = _8BALL_POSITIVE + _8BALL_NEUTRAL + _8BALL_NEGATIVE

_JOKE_CHOICES = [
    app_commands.Choice(name="🎲  Any",          value="Any"),
    app_commands.Choice(name="💻  Programming",  value="Programming"),
    app_commands.Choice(name="🙂  Misc",         value="Misc"),
    app_commands.Choice(name="😈  Dark",         value="Dark"),
    app_commands.Choice(name="😄  Pun",          value="Pun"),
    app_commands.Choice(name="👻  Spooky",       value="Spooky"),
    app_commands.Choice(name="🎄  Christmas",    value="Christmas"),
]

_NUM_WORDS = {
    "0": "zero", "1": "one",   "2": "two",   "3": "three", "4": "four",
    "5": "five", "6": "six",   "7": "seven", "8": "eight", "9": "nine",
}
_SPECIAL_CHARS = {
    "!": ":grey_exclamation:",
    "?": ":grey_question:",
    "*": ":asterisk:",
}


# ── Shared helper ─────────────────────────────────────────────────────────────

def _owner_only_check(
    author: discord.User | discord.Member,
    interaction: discord.Interaction,
) -> bool:
    """Return True if the interacting user is the original command author."""
    return interaction.user.id == author.id


# ═════════════════════════════════════════════════════════════════════════════
# Views
# ═════════════════════════════════════════════════════════════════════════════

class MemeView(discord.ui.View):
    """
    Meme browser with a 'Next Meme' and a 'Close' button.
    Only the user who ran /meme can interact with it.

    Fetch strategy
    ──────────────
    1. Try the Reddit meme API (meme-api.com).
    2. If that fails for any reason, fall back to memegen.link's random
       endpoint (api.memegen.link/images/random) – free, no key, always up.
    The source that served the current meme is stored in ``self.source`` so
    ``build_embed`` can render the right footer / metadata.
    """

    #: Possible values for ``self.source``
    SOURCE_REDDIT  = "reddit"
    SOURCE_MEMEGEN = "memegen"

    def __init__(self, author: discord.User | discord.Member, source: str) -> None:
        super().__init__(timeout=180)
        self.author  = author
        self.source  = source                             # which API is active
        self.message: Optional[discord.Message] = None   # set after send

    # ── guard ────────────────────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _owner_only_check(self.author, interaction):
            await interaction.response.send_message(
                f"{Emojis.no_entry} This interaction belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    # ── fetch helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _fetch_reddit() -> dict:
        """Raises on any non-2xx status or network error."""
        async with aiohttp.ClientSession() as session:
            async with session.get(MemeAPIs._MEME_REDDIT_API) as resp:
                resp.raise_for_status()
                return await resp.json()

    @staticmethod
    async def _fetch_memegen() -> dict:
        """
        Returns a random generated meme from memegen.link.
        Response shape: { url, template: { name, id }, lines: [...] }
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(MemeAPIs._MEME_MEMEGEN_API) as resp:
                resp.raise_for_status()
                return await resp.json()

    @staticmethod
    async def fetch() -> tuple[dict, str]:
        """
        Try Reddit first; silently fall back to memegen.link if it fails.

        Returns
        ───────
        (data, source) where source is SOURCE_REDDIT or SOURCE_MEMEGEN.
        Raises only if *both* APIs fail.
        """
        try:
            data = await MemeView._fetch_reddit()
            return data, MemeView.SOURCE_REDDIT
        except Exception:
            pass  # Reddit is down / rate-limited → try memegen

        data = await MemeView._fetch_memegen()
        return data, MemeView.SOURCE_MEMEGEN

    # ── embed builders ───────────────────────────────────────────────────────

    @staticmethod
    def _embed_reddit(data: dict, bot_avatar: discord.Asset) -> discord.Embed:
        embed = discord.Embed(
            title=data["title"],
            url=data["postLink"],
            color=0x14CCCC,
        )
        embed.set_author(name="Meme Browser • Reddit", icon_url=bot_avatar.url)
        embed.set_image(url=data["url"])
        embed.set_footer(text=f"👤 {data['author']}  •  r/{data['subreddit']}")
        return embed

    @staticmethod
    def _embed_memegen(data: dict, bot_avatar: discord.Asset) -> discord.Embed:
        template_name = data.get("template", {}).get("name", "Random Meme")
        lines         = data.get("lines", [])
        caption       = "  /  ".join(str(l) for l in lines if l and l != "_") or None

        embed = discord.Embed(
            title=template_name,
            color=0xFF6B35,        # memegen brand orange
        )
        embed.set_author(
            name="Meme Browser • memegen.link (Reddit unavailable)",
            icon_url=bot_avatar.url,
        )
        embed.set_image(url=data["url"])
        if caption:
            embed.description = f"*{caption}*"
        embed.set_footer(text="🔗 memegen.link  •  Reddit API unavailable")
        return embed

    @staticmethod
    def build_embed(
        data: dict,
        source: str,
        bot_avatar: discord.Asset,
    ) -> discord.Embed:
        if source == MemeView.SOURCE_REDDIT:
            return MemeView._embed_reddit(data, bot_avatar)
        return MemeView._embed_memegen(data, bot_avatar)

    # ── buttons ──────────────────────────────────────────────────────────────

    @discord.ui.button(label="Next Meme", style=discord.ButtonStyle.green, emoji="⏩")
    async def next_meme(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        try:
            data, source  = await self.fetch()
            self.source   = source
            embed = self.build_embed(data, source, interaction.client.user.display_avatar)
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            await interaction.followup.send(
                f"{Emojis.warning} Both meme APIs are unreachable right now. Try again later!",
                ephemeral=True,
            )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self._disable_all()
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(
            f"{Emojis.confirmation} Meme browser closed.", ephemeral=True
        )
        self.stop()

    # ── timeout ──────────────────────────────────────────────────────────────

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]


# ─────────────────────────────────────────────────────────────────────────────

class EightBallView(discord.ui.View):
    """
    Shows a 🎱 Ask Again button so the user can re-roll the 8-ball
    on the same question without typing the command again.
    """

    def __init__(
        self,
        author: discord.User | discord.Member,
        question: str,
    ) -> None:
        super().__init__(timeout=60)
        self.author   = author
        self.question = question
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _owner_only_check(self.author, interaction):
            await interaction.response.send_message(
                f"{Emojis.no_entry} This is not your 8-ball.",
                ephemeral=True,
            )
            return False
        return True

    @staticmethod
    def build_embed(question: str, bot_avatar: discord.Asset) -> discord.Embed:
        text, color = random.choice(_8BALL_RESPONSES)
        embed = discord.Embed(color=color)
        embed.set_author(name="🎱 Magic 8-Ball", icon_url=bot_avatar.url)
        embed.add_field(name="❓ Question", value=question,          inline=False)
        embed.add_field(name="🎱 Answer",   value=f"**{text}**",    inline=False)
        return embed

    @discord.ui.button(label="Ask Again", style=discord.ButtonStyle.blurple, emoji="🎱")
    async def ask_again(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        embed = self.build_embed(
            self.question, interaction.client.user.display_avatar
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


# ─────────────────────────────────────────────────────────────────────────────

class JokeView(discord.ui.View):
    """
    Joke browser.
    Row 0 – Select menu to switch category.
    Row 1 – 'Another Joke' re-roll button  +  'Close' button.
    """

    _SELECT_OPTIONS = [
        discord.SelectOption(label="Any",          emoji="🎲", value="Any",         description="Any category"),
        discord.SelectOption(label="Programming",  emoji="💻", value="Programming", description="Programmer humour"),
        discord.SelectOption(label="Misc",         emoji="🙂", value="Misc",        description="Miscellaneous jokes"),
        discord.SelectOption(label="Dark",         emoji="😈", value="Dark",        description="Dark humour"),
        discord.SelectOption(label="Pun",          emoji="😄", value="Pun",         description="Puns and wordplay"),
        discord.SelectOption(label="Spooky",       emoji="👻", value="Spooky",      description="Spooky jokes"),
        discord.SelectOption(label="Christmas",    emoji="🎄", value="Christmas",   description="Christmas jokes"),
    ]

    def __init__(
        self,
        author: discord.User | discord.Member,
        category: str,
        safe: bool,
    ) -> None:
        super().__init__(timeout=180)
        self.author   = author
        self.category = category
        self.safe     = safe
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _owner_only_check(self.author, interaction):
            await interaction.response.send_message(
                f"{Emojis.no_entry} This joke belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    async def fetch(category: str, safe: bool) -> dict:
        url = MemeAPIs._JOKE_API.format(category=category)
        if safe:
            url += "?safe-mode"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.json()

    @staticmethod
    def build_embed(data: dict, category: str, safe: bool) -> discord.Embed:
        # JokeAPI can return an error object if no joke matches the filters
        if data.get("error"):
            return discord.Embed(
                description=f"{Emojis.warning} No joke found for this category/filter combo.",
                color=discord.Color.red(),
            )

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_author(name="😂 Joke Time!")

        if data.get("type") == "twopart":
            embed.add_field(name="Setup",     value=data["setup"],              inline=False)
            embed.add_field(name="Punchline", value=f"||{data['delivery']}||", inline=False)
        else:
            embed.description = data.get("joke", "…")

        safe_label = f"{Emojis.confirmation} Safe" if safe else f"{Emojis.warning} May contain adult content"
        embed.set_footer(text=f"Category: {category}  •  {safe_label}  •  jokeapi.dev")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            data  = await self.fetch(self.category, self.safe)
            embed = self.build_embed(data, self.category, self.safe)
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            await interaction.followup.send(
                f"{Emojis.warning} Couldn't reach JokeAPI right now. Try again!",
                ephemeral=True,
            )

    # ── components ───────────────────────────────────────────────────────────

    @discord.ui.select(
        placeholder="Switch category…",
        options=_SELECT_OPTIONS,
        row=0,
    )
    async def category_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.category = select.values[0]
        await self._refresh(interaction)

    @discord.ui.button(
        label="Another Joke",
        style=discord.ButtonStyle.green,
        emoji="😂",
        row=1,
    )
    async def another_joke(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._refresh(interaction)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
        row=1,
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self._disable_all()
        await interaction.response.edit_message(view=None)
        self.stop()

    # ── timeout ──────────────────────────────────────────────────────────────

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]


# ═════════════════════════════════════════════════════════════════════════════
# Cog
# ═════════════════════════════════════════════════════════════════════════════

class Fun(commands.Cog, description="😄 Fun and entertainment commands."):
    """Silly, light-hearted commands for when you just want to have fun."""

    COG_EMOJI = Emojis.happy_face

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /echo ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="echo",
        description="Repeat a message in the channel.",
    )
    @app_commands.describe(message="The text to send.")
    async def echo(self, interaction: discord.Interaction, message: str) -> None:
        # Acknowledge privately so the trigger message is invisible
        await interaction.response.send_message(
            f"{Emojis.confirmation} Message sent!", ephemeral=True
        )
        await interaction.channel.send(message)  # type: ignore[union-attr]

    # ── /8ball ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="8ball",
        description="Consult the Magic 8-Ball.",
    )
    @app_commands.describe(question="The question you want answered.")
    async def eightball(
        self, interaction: discord.Interaction, question: str
    ) -> None:
        view  = EightBallView(interaction.user, question)
        embed = EightBallView.build_embed(question, self.bot.user.display_avatar)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    # ── /meme ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="meme",
        description="Fetch a random Reddit meme.",
    )
    @app_commands.checks.cooldown(3, 10.0, key=lambda i: i.user.id)
    async def meme(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            data, source = await MemeView.fetch()
        except Exception:
            await interaction.followup.send(
                f"{Emojis.warning} Both meme APIs are unreachable right now. Try again later.",
                ephemeral=True,
            )
            return

        view    = MemeView(interaction.user, source)
        embed   = MemeView.build_embed(data, source, self.bot.user.display_avatar)
        msg     = await interaction.followup.send(embed=embed, view=view)
        view.message = msg

    # ── /joke ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="joke",
        description="Fetch a joke from JokeAPI (jokeapi.dev).",
    )
    @app_commands.describe(
        category="Joke category — defaults to Any.",
        safe="Filter out adult / offensive jokes (default: on).",
    )
    @app_commands.choices(category=_JOKE_CHOICES)
    @app_commands.checks.cooldown(3, 10.0, key=lambda i: i.user.id)
    async def joke(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
        safe: bool = True,
    ) -> None:
        await interaction.response.defer()
        cat = category.value if category else "Any"

        try:
            data = await JokeView.fetch(cat, safe)
        except Exception:
            await interaction.followup.send(
                f"{Emojis.warning} Could not reach JokeAPI. Try again later.",
                ephemeral=True,
            )
            return

        view    = JokeView(interaction.user, cat, safe)
        embed   = JokeView.build_embed(data, cat, safe)
        msg     = await interaction.followup.send(embed=embed, view=view)
        view.message = msg

    # ── /emoji group ──────────────────────────────────────────────────────────

    emoji_group = app_commands.Group(
        name="emoji",
        description="Server emoji utilities.",
    )

    @emoji_group.command(
        name="add",
        description="Upload an external image as a custom server emoji.",
    )
    @app_commands.describe(
        url="Direct link to the image (PNG, JPG, or GIF, max 256 KB).",
        name="Name for the emoji – spaces become underscores.",
    )
    @app_commands.checks.has_permissions(manage_expressions=True)
    async def emoji_add(
        self,
        interaction: discord.Interaction,
        url: str,
        name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        name = name.strip().replace(" ", "_")

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if not (200 <= resp.status < 300):
                    await interaction.followup.send(
                        f"{Emojis.cross_mark} Download failed (HTTP {resp.status}). "
                        "Check the URL and try again.",
                        ephemeral=True,
                    )
                    return
                img_bytes = await resp.read()

        try:
            emoji = await interaction.guild.create_custom_emoji(  # type: ignore[union-attr]
                name=name,
                image=img_bytes,
                reason=f"Added via /emoji add by {interaction.user} ({interaction.user.id})",
            )
            await interaction.followup.send(
                f"{Emojis.confirmation} {emoji} **:{name}:** has been added to the server!",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            # 50138 = file size too large; 30008 = max emoji limit reached
            reasons = {
                50138: "The image file is too large (max 256 KB). Try compressing it first.",
                30008: "This server has reached its maximum custom emoji limit.",
            }
            msg = reasons.get(exc.code, f"Discord returned an error: {exc.text}")
            await interaction.followup.send(
                f"{Emojis.cross_mark} {msg}", ephemeral=True
            )

    @emoji_add.error
    async def emoji_add_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                f"{Emojis.no_entry} You need the **Manage Expressions** permission "
                "to add emojis.",
                ephemeral=True,
            )

    # ── /emojify ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="emojify",
        description="Convert text into regional-indicator emojis.",
    )
    @app_commands.describe(text="The text to emojify (letters, numbers, ! ? * supported).")
    async def emojify(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        parts: list[str] = []
        for char in text:
            if char.isdecimal():
                parts.append(f":{_NUM_WORDS[char]}:")
            elif char.isalpha():
                parts.append(f":regional_indicator_{char.lower()}:")
            elif char in _SPECIAL_CHARS:
                parts.append(_SPECIAL_CHARS[char])
            else:
                parts.append(char)          # spaces, punctuation – pass through

        result = " ".join(parts)

        if len(result) > 2000:
            await interaction.response.send_message(
                f"{Emojis.warning} The emojified text is too long to send. "
                "Try a shorter input!",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(result)


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))