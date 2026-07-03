"""Ping cog — latency check. hybrid_command works as both !ping and /ping."""

import discord
from discord.ext import commands


class Ping(commands.Cog, description="Get the latency of the bot."):
    COG_EMOJI = "🏓"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="ping",
        description="Returns the latency of the bot.",
        aliases=["latency","pingpong"],
    )
    async def ping(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Latency: `{round(self.bot.latency * 1000)}ms`",
            colour=discord.Colour.green(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ping(bot))