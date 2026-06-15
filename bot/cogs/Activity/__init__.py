"""Activity cog for Discord embedded voice-channel activities.
─────────────────────────────────────────
Discord embedded voice-channel activities (Watch Together, Sketch Heads, …).

Modernisation vs the nextcord version:
  • nextcord-ext-activities is REMOVED entirely. discord.py (Rapptz) has no
    `create_activity_invite` shortcut (that's Pycord-only) — we build the
    invite directly via:
        channel.create_invite(
            target_type=discord.InviteTarget.embedded_application,
            target_application_id=<app id>,
            max_age=86400,
        )
  • Single `/activity` slash command with an `activity` choice parameter
    (app_commands.Choice — native Discord dropdown, no extra Select needed)
    and a `channel` parameter typed as discord.VoiceChannel.
  • Result is delivered as a link-style Button (discord.ui.View), which
    Discord renders as a clickable "Join Activity" button — modern UI.
  • Checks discord.Permissions.use_embedded_activities on both the bot and
    invoking member before attempting the invite.

NOTE ON APPLICATION IDS:
  These IDs come from Discord's officially documented embedded activities
  (https://discord.com/blog/server-activities-games-voice-watch-together
  and community-maintained trackers). Discord adds/removes activities and
  may require the server to be Boosted (Level 1+) for some of them without
  warning. If `create_invite` raises HTTPException for a given activity,
  it has likely been deprecated server-side — remove it from _ACTIVITIES.
"""

import discord
from discord import app_commands
from discord.ext import commands
from util.constants import Activities

class JoinActivityView(discord.ui.View):
    """A single link-style button pointing to the activity invite."""

    def __init__(self, invite_url: str, activity_name: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label=f"Join {activity_name}",
                style=discord.ButtonStyle.link,
                url=invite_url,
            )
        )


class Activity(commands.Cog, description="Start Discord voice-channel activities."):
    COG_EMOJI = "🚀"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="activity", description="Start a voice-channel activity (Watch Together, Sketch Heads, …).")
    @app_commands.describe(
        channel="The voice channel to start the activity in",
        activity="Which activity to launch",
    )
    @app_commands.choices(
        activity=[
            app_commands.Choice(name=f"{emoji} {name}", value=name)
            for name, (_, emoji, _, _) in Activities._ACTIVITIES.items()
        ]
    )
    @app_commands.checks.has_permissions(use_embedded_activities=True)
    @app_commands.checks.bot_has_permissions(
        use_embedded_activities=True, create_instant_invite=True
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        activity: app_commands.Choice[str],
    ) -> None:
        app_id, emoji, min_boost, desc = Activities._ACTIVITIES[activity.value]

        assert interaction.guild is not None
        if interaction.guild.premium_tier < min_boost:
            await interaction.response.send_message(
                f":no_entry: **{activity.value}** requires the server to be "
                f"Boosted to Level {min_boost} or higher.",
                ephemeral=True,
            )
            return

        try:
            invite = await channel.create_invite(
                target_type=discord.InviteTarget.embedded_application,
                target_application_id=app_id,
                max_age=86400,  # 24h
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f":warning: Discord rejected this activity (it may have been "
                f"discontinued): `{exc}`",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{emoji} {activity.value}",
            description=desc,
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Started by", value=interaction.user.mention)
        embed.add_field(name="Voice channel", value=channel.mention)

        view = JoinActivityView(invite.url, activity.value)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Activity(bot))