"""SubCommands cog — example of nested command groups."""

import discord
from discord.ext import commands


class Groups(commands.Cog, name="Sub Commands", description="A simple test group for sub commands"):
    COG_EMOJI = "🧑‍🎓"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
    
    @commands.hybrid_group(with_app_command=True)
    async def first(self, ctx: commands.Context) -> None:
        """Executed when running the parent command alone."""
        await ctx.send("This is the first command layer")

    @first.group(invoke_without_command=True)
    async def second(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Executed when using this child subcommand with the parent."""
        target = channel or ctx.channel
        await target.send(
            "Hey! This is a message from me the bot. Bet you didn't see who ran the command?",
            delete_after=15,
        )

    @second.command()
    async def third(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Executed when using this deeply-nested subcommand."""
        if channel:
            await channel.send("Hey! Did this come through clearly?")
            await ctx.channel.send(
                f"Message sent to {channel.mention} by {ctx.author.mention}"
            )
        else:
            await ctx.author.send("Hey! Did this come through clearly?")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Groups(bot))