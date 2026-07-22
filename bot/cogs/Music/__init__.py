"""
Music cog — wavelink v3, built on the MusicPlayer/LoopMode/lyrics glue in
bot/util/music/__init__.py.

Commands
────────
  /play <query>            — hybrid, high-frequency (search or direct URL;
                              YouTube via youtube-plugin, Spotify/etc via
                              lavasrc — wavelink.Playable.search() picks the
                              right source automatically from the query)
  /skip /pause /resume /stop — hybrid, high-frequency. /skip is a
                              FredBoat-style *vote*-skip once 2+ listeners
                              are present — the requester or anyone with
                              manage_guild always force-skips instantly.
  /music queue              — paginated QueueView (10/page, prev/next +
                              jump-to-first/last, matches Thread.MemberListView)
  /music nowplaying          — snapshot embed with progress bar; the message
                              sent on track_start keeps that bar live via a
                              15s self-editing background task
  /music loop <off|track|queue>
  /music volume <0-150>
  /music remove <index>      — drop a single queued track (manage_guild or
                              the track's own requester)
  /music clear                — wipe the queue (manage_guild)
  /music move <from> <to>   — reorder the queue
  /music jump <index>        — skip straight to a queued track, discarding
                              everything before it
  /music previous            — replay the last played track (history stack)
  /music seek <mm:ss|secs>  — seek within the current track
  /music filter <preset>    — bassboost / nightcore / vaporwave / 8d / off
  /music autoplay             — toggle wavelink's related-track autoplay for
                              when the queue runs dry
  /music lyrics [query]      — defaults to the currently-playing track;
                              LRCLIB synced/plain lyrics, Genius search-URL
                              button fallback on a miss
  /music 247                 — toggle stay-connected-when-empty (manage_guild)

/play search results use a paginated SearchResultView (10 hits, 5/page,
prev/next buttons + a per-page select) instead of a flat top-5 list.

Persistent control block
─────────────────────────
MusicControlView is registered via bot.add_view() in cog_load() — per the
UI directive, all custom_ids are deterministic ("music:*") so the row of
buttons attached to each now-playing message keeps working across restarts.

Button appearance is refreshed per-interaction (dynamic emoji/label swap,
same pattern as Thread.ManagePanelView._sync()) rather than being fixed at
class-definition time, so e.g. the pause button actually flips to a resume
icon once the player is paused.

Auto-advance / loop
────────────────────
player.autoplay is left at AutoPlayMode.disabled — loop/queue advancement
is handled explicitly in on_wavelink_track_end so LoopMode.TRACK/QUEUE stay
simple and legible instead of fighting wavelink's own autoplay heuristics.

Empty-channel auto-disconnect
──────────────────────────────
on_voice_state_update starts a 120s per-guild asyncio.Task the moment the
bot is left alone in a voice channel (skipped entirely if /music 247 is on
for that guild), cancelling it the instant a non-bot member returns.
"""

from __future__ import annotations

import asyncio
import enum
import io
import logging

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from util.constants import Emojis
from util.music import (
    FILTER_NAMES,
    LoopMode,
    MusicPlayer,
    build_filter,
    fetch_lyrics,
    genius_search_url,
)

log = logging.getLogger(__name__)

_EMPTY_DISCONNECT_DELAY = 120.0  # seconds
_LYRICS_EMBED_LIMIT = 3800       # headroom under the 4096 description cap
_NOW_PLAYING_REFRESH_SECONDS = 15.0  # progress-bar self-edit interval
_SEEK_STEP_MS = 10_000               # ⏪ / ⏩ button step


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _ok(text: str) -> discord.Embed:
    return discord.Embed(description=f"{Emojis.confirmation}  {text}", colour=discord.Colour.green())


def _err(text: str) -> discord.Embed:
    return discord.Embed(description=f"{Emojis.cross_mark}  {text}", colour=discord.Colour.red())


def _format_duration(ms: int) -> str:
    total_seconds = ms // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _looks_like_url(query: str) -> bool:
    return query.startswith(("http://", "https://"))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _progress_bar(position_ms: int, length_ms: int, *, size: int = 20) -> str:
    if length_ms <= 0:
        return "─" * size
    filled = min(size, int((position_ms / length_ms) * size))
    return "▰" * filled + "▱" * (size - filled)


def _parse_seek_position(text: str) -> int | None:
    """Parse "mm:ss", "h:mm:ss", or a bare seconds count into milliseconds.

    Returns None (rather than raising) on anything unparsable so the caller
    can render a normal error embed instead of a stack trace.
    """
    text = text.strip()
    if ":" in text:
        parts = text.split(":")
        if not all(p.isdigit() for p in parts) or len(parts) > 3:
            return None
        values = [int(p) for p in parts]
        while len(values) < 3:
            values.insert(0, 0)
        h, m, s = values
        return ((h * 3600) + (m * 60) + s) * 1000
    if text.isdigit():
        return int(text) * 1000
    return None


def _is_dj(member: discord.Member) -> bool:
    """Anyone who can manage the guild is treated as a DJ — bypasses vote-skip
    and can force-manage the queue. No separate configurable DJ role yet;
    manage_guild is the same bar /music 247 already gates on.
    """
    return member.guild_permissions.manage_guild


def _volume_emoji(level: int) -> str:
    """Pick the closest volume icon for the current level."""
    if level == 0:
        return Emojis.mute
    if level < 60:
        return Emojis.halfvolume
    return Emojis.fullvolume


def _now_playing_embed(
    track: wavelink.Playable,
    player: MusicPlayer,
    requester_id: int | None,
) -> discord.Embed:
    title_icon = Emojis.loop if player.loop_mode != LoopMode.OFF else Emojis.track_note
    embed = discord.Embed(
        title=f"{title_icon}  Now Playing",
        description=f"**[{track.title}]({track.uri})**\n{track.author}",
        colour=discord.Colour.blurple(),
    )
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)

    bar = _progress_bar(player.position, track.length)
    embed.add_field(
        name="Progress",
        value=f"{bar}\n`{_format_duration(player.position)} / {_format_duration(track.length)}`",
        inline=False,
    )
    embed.add_field(name="Requested by", value=f"<@{requester_id}>" if requester_id else "Autoplay", inline=True)
    embed.add_field(name=f"{Emojis.loop} Loop", value=player.loop_mode.value.title(), inline=True)
    embed.add_field(
        name=f"{_volume_emoji(player.volume)} Volume", value=f"{player.volume}%", inline=True
    )
    if player.active_filter != "off":
        embed.add_field(name="🎛️ Filter", value=player.active_filter.title(), inline=True)
    footer = f"{len(player.queue)} track(s) in queue"
    if player.autoplay == wavelink.AutoPlayMode.enabled:
        footer += " • Autoplay on"
    embed.set_footer(text=footer)
    return embed


def _queue_page_embed(player: MusicPlayer, page: int, per_page: int = 10) -> discord.Embed:
    tracks = list(player.queue)
    start = page * per_page
    chunk = tracks[start : start + per_page]

    lines: list[str] = []
    if player.current:
        lines.append(f"{Emojis.track_note} **Now Playing:** {player.current.title}")
        lines.append("")
    for i, track in enumerate(chunk, start=start + 1):
        requester_id = player.requester_for(track)
        who = f" — <@{requester_id}>" if requester_id else ""
        lines.append(f"`{i}.` {track.title}{who}")

    embed = discord.Embed(
        title=f"{Emojis.list_emoji} Queue",
        description="\n".join(lines) or "*Queue is empty.*",
        colour=discord.Colour.blurple(),
    )
    max_page = max(0, (len(tracks) - 1) // per_page)
    embed.set_footer(text=f"Page {page + 1}/{max_page + 1} • {len(tracks)} track(s) queued")
    return embed


class _SkipOutcome(enum.Enum):
    SKIPPED = "skipped"          # force-skipped immediately
    VOTE_REGISTERED = "vote"     # vote counted, threshold not yet met
    ALREADY_VOTED = "already"    # this member already voted for this track


async def _register_skip_vote(player: MusicPlayer, member: discord.Member) -> _SkipOutcome:
    """Shared vote-skip logic for both the button and the /skip command.

    The requester of the current track and anyone with manage_guild always
    force-skip instantly — FredBoat's rule of thumb that whoever queued a
    song (or a mod) shouldn't have to wait on a vote. Everyone else needs a
    simple majority of the members actually listening in voice.
    """
    is_requester = player.current is not None and player.requester_for(player.current) == member.id
    if is_requester or _is_dj(member):
        player.skip_votes.clear()
        await player.skip(force=True)
        return _SkipOutcome.SKIPPED

    if member.id in player.skip_votes:
        return _SkipOutcome.ALREADY_VOTED

    player.skip_votes.add(member.id)
    if len(player.skip_votes) >= player.votes_required_to_skip():
        player.skip_votes.clear()
        await player.skip(force=True)
        return _SkipOutcome.SKIPPED
    return _SkipOutcome.VOTE_REGISTERED


async def _queue_push_front(queue: wavelink.Queue, track: wavelink.Playable) -> None:
    """Insert `track` at the front of `queue`.

    wavelink.Queue doesn't expose a direct put_at(0, ...), so this drains
    and rebuilds it — fine for the queue sizes this bot deals with, and
    kept as a helper so /music previous, /music jump, and /music move all
    share one (tested) implementation instead of three near-duplicates.
    """
    rest = list(queue)
    queue.clear()
    await queue.put_wait(track)
    for item in rest:
        await queue.put_wait(item)


# ── Persistent control view ──────────────────────────────────────────────────

class MusicControlView(discord.ui.View):
    """Deterministic custom_ids — registered in cog_load() so buttons attached
    to old now-playing messages keep working after a bot restart.

    _sync() refreshes emoji/label/style against the live player state right
    before every edit, so e.g. the pause button flips to Emojis.resume once
    playback is actually paused, instead of staying visually static.
    """

    def __init__(self, player: MusicPlayer | None = None) -> None:
        super().__init__(timeout=None)
        if player is not None:
            self._sync(player)

    def _sync(self, player: MusicPlayer) -> None:
        self.toggle_pause.emoji = Emojis.resume if player.paused else Emojis.pause
        self.loop_btn.style = (
            discord.ButtonStyle.success if player.loop_mode != LoopMode.OFF else discord.ButtonStyle.secondary
        )

    @staticmethod
    def _player(interaction: discord.Interaction) -> MusicPlayer | None:
        if interaction.guild is None:
            return None
        vc = interaction.guild.voice_client
        return vc if isinstance(vc, MusicPlayer) else None

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="music:previous", row=0)
    async def previous_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None:
            await interaction.response.send_message(embed=_err("Not connected."), ephemeral=True)
            return
        track = player.pop_previous()
        if track is None:
            await interaction.response.send_message(embed=_err("No previous track."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if player.current is not None:
            await _queue_push_front(player.queue, player.current)
        await player.play(track)
        await interaction.followup.send(embed=_ok(f"{Emojis.track_note} Playing **{track.title}**."), ephemeral=True)

    @discord.ui.button(emoji=Emojis.pause, style=discord.ButtonStyle.primary, custom_id="music:toggle_pause", row=0)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing."), ephemeral=True)
            return
        # player.pause() below is a Lavalink round-trip. Ack with a deferred
        # update *before* it, then edit the original response, instead of
        # editing directly afterwards — otherwise a slow node blows the 3s
        # ack window and edit_message() dies with the same "Unknown
        # interaction" failure /play had.
        await interaction.response.defer()
        await player.pause(not player.paused)
        self._sync(player)
        await interaction.edit_original_response(view=self)

    @discord.ui.button(emoji=Emojis.skip, style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing."), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            return
        result = await _register_skip_vote(player, interaction.user)
        if result is _SkipOutcome.SKIPPED:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(embed=_ok(f"{Emojis.skip} Skipped."), ephemeral=True)
        elif result is _SkipOutcome.ALREADY_VOTED:
            await interaction.response.send_message(embed=_err("You've already voted to skip."), ephemeral=True)
        else:
            have, need = len(player.skip_votes), player.votes_required_to_skip()
            await interaction.response.send_message(
                embed=_ok(f"{Emojis.skip} Vote to skip registered ({have}/{need})."), ephemeral=True
            )

    @discord.ui.button(emoji=Emojis.stop, style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None:
            await interaction.response.send_message(embed=_err("Not connected."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        player.queue.clear()
        await player.stop()
        await player.disconnect()
        await interaction.followup.send(
            embed=_ok(f"Stopped and {Emojis.closeConnection} disconnected."), ephemeral=True
        )

    @discord.ui.button(emoji=Emojis.loop, style=discord.ButtonStyle.secondary, custom_id="music:loop", row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None:
            await interaction.response.send_message(embed=_err("Not connected."), ephemeral=True)
            return
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        player.loop_mode = order[(order.index(player.loop_mode) + 1) % len(order)]
        self._sync(player)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=_ok(f"{Emojis.loop} Loop mode set to **{player.loop_mode.value}**."), ephemeral=True
        )

    @discord.ui.button(emoji=Emojis.shuffle, style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None or not player.queue:
            await interaction.response.send_message(embed=_err("Queue is empty."), ephemeral=True)
            return
        player.queue.shuffle()
        await interaction.response.send_message(embed=_ok(f"{Emojis.shuffle} Queue shuffled."), ephemeral=True)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="music:seek_back", row=1)
    async def seek_back_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await player.seek(max(0, player.position - _SEEK_STEP_MS))
        await interaction.followup.send(embed=_ok("⏪ Rewound 10s."), ephemeral=True)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="music:seek_fwd", row=1)
    async def seek_fwd_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_pos = min(player.current.length, player.position + _SEEK_STEP_MS)
        await player.seek(new_pos)
        await interaction.followup.send(embed=_ok("⏩ Skipped ahead 10s."), ephemeral=True)

    @discord.ui.button(emoji="🎛️", label="Filter", style=discord.ButtonStyle.secondary, custom_id="music:filter", row=1)
    async def filter_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player(interaction)
        if player is None:
            await interaction.response.send_message(embed=_err("Not connected."), ephemeral=True)
            return
        order = list(FILTER_NAMES)
        next_name = order[(order.index(player.active_filter) + 1) % len(order)]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await player.set_filters(build_filter(next_name))
        player.active_filter = next_name
        await interaction.followup.send(embed=_ok(f"🎛️ Filter set to **{next_name.title()}**."), ephemeral=True)


# ── Queue view (prev/next pagination) ────────────────────────────────────────

class QueueView(discord.ui.View):
    def __init__(self, player: MusicPlayer, requester: discord.abc.User) -> None:
        super().__init__(timeout=120)
        self.player = player
        self.requester = requester
        self.page = 0
        self._sync()

    def _max_page(self) -> int:
        return max(0, (len(self.player.queue) - 1) // 10)

    def _sync(self) -> None:
        at_start = self.page == 0
        at_end = self.page >= self._max_page()
        self.first_btn.disabled = at_start
        self.prev_btn.disabled = at_start
        self.next_btn.disabled = at_end
        self.last_btn.disabled = at_end
        self.clear_btn.disabled = len(self.player.queue) == 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(embed=_err("This queue view isn't for you."), ephemeral=True)
            return False
        return True

    async def _goto(self, interaction: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self._max_page()))
        self._sync()
        await interaction.response.edit_message(embed=_queue_page_embed(self.player, self.page), view=self)

    @discord.ui.button(label="«", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._goto(interaction, 0)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._goto(interaction, self.page - 1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._goto(interaction, self.page + 1)

    @discord.ui.button(label="»", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._goto(interaction, self._max_page())

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, row=1)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not (
            _is_dj(interaction.user) or interaction.user.id == self.requester.id
        ):
            await interaction.response.send_message(
                embed=_err("Only the requester or a manager can clear the queue."), ephemeral=True
            )
            return
        self.player.queue.clear()
        self.page = 0
        self._sync()
        await interaction.response.edit_message(embed=_queue_page_embed(self.player, self.page), view=self)


# ── Lyrics view (Genius fallback button) ─────────────────────────────────────

class LyricsView(discord.ui.View):
    def __init__(self, *, query: str) -> None:
        super().__init__(timeout=120)
        self.add_item(
            discord.ui.Button(
                label="Search on Genius", emoji="🔗", style=discord.ButtonStyle.link, url=genius_search_url(query)
            )
        )


# ── Search result picker (select menu, /play text searches only) ────────────

def _search_results_embed(query: str, results: list[wavelink.Playable], page: int, per_page: int) -> discord.Embed:
    start = page * per_page
    chunk = results[start : start + per_page]
    lines = [
        f"`{start + i + 1}.` **[{t.title}]({t.uri})** — {t.author} `({_format_duration(t.length)})`"
        for i, t in enumerate(chunk)
    ]
    embed = discord.Embed(
        title=f"{Emojis.list_emoji} Search results for “{_truncate(query, 60)}”",
        description="\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    max_page = max(0, (len(results) - 1) // per_page)
    embed.set_footer(text=f"Page {page + 1}/{max_page + 1} • pick a track below, or Cancel")
    return embed


class SearchResultView(discord.ui.View):
    """Lets the requester pick which search hit to queue.

    Only shown for plain-text /play queries (see _looks_like_url) — direct
    links and playlists still auto-queue immediately, there's nothing to
    pick there. Only the original requester can use the menu, matching the
    QueueView.interaction_check pattern above.

    Results are paginated (5/page, up to 10 total) with prev/next buttons
    alongside a select that's rebuilt for whichever page is showing, so a
    plain-text search doesn't just silently truncate to the first 5 hits.
    """

    _MAX_RESULTS = 10
    _PER_PAGE = 5

    def __init__(
        self, query: str, results: list[wavelink.Playable], player: MusicPlayer, requester: discord.abc.User
    ) -> None:
        super().__init__(timeout=60)
        self.query = query
        self.results = results[: self._MAX_RESULTS]
        self.player = player
        self.requester = requester
        self.page = 0
        self.message: discord.Message | None = None
        self._sync()

    def _max_page(self) -> int:
        return max(0, (len(self.results) - 1) // self._PER_PAGE)

    def _page_slice(self) -> list[wavelink.Playable]:
        start = self.page * self._PER_PAGE
        return self.results[start : start + self._PER_PAGE]

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self._max_page()

        start = self.page * self._PER_PAGE
        self.select_track.options = [
            discord.SelectOption(
                label=_truncate(track.title, 100),
                description=_truncate(f"{track.author} • {_format_duration(track.length)}", 100),
                value=str(start + i),
            )
            for i, track in enumerate(self._page_slice())
        ]

    def current_embed(self) -> discord.Embed:
        return _search_results_embed(self.query, self.results, self.page, self._PER_PAGE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(embed=_err("This search isn't for you."), ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(embed=_err("Search timed out."), view=self)
            except discord.HTTPException:
                pass
        await self._disconnect_if_idle()

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    async def _disconnect_if_idle(self) -> None:
        # If this connection was made just for this search and nothing got
        # picked, don't leave the bot idling in voice — the empty-channel
        # timer only kicks in once the channel is actually empty.
        if self.player.connected and not self.player.playing and self.player.queue.is_empty:
            await self.player.disconnect()

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.select(placeholder="Choose a track to queue…")
    async def select_track(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        track = self.results[int(select.values[0])]
        self.player.remember_requester(track, self.requester.id)
        await self.player.queue.put_wait(track)

        self._disable_all()
        await interaction.response.edit_message(
            embed=_ok(f"{Emojis.track_note} Queued **{track.title}**."), view=self
        )
        self.stop()

        if not self.player.playing:
            next_track = await self.player.queue.get_wait()
            await self.player.play(next_track)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_all()
        await interaction.response.edit_message(embed=_err("Search cancelled."), view=self)
        self.stop()
        await self._disconnect_if_idle()


# ── Cog ────────────────────────────────────────────────────────────────────────

class Music(commands.Cog, description="Play music from YouTube, Spotify, SoundCloud and more."):
    COG_EMOJI = Emojis.track_note

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._empty_timers: dict[int, asyncio.Task] = {}
        # Keeps the now-playing progress bar honest between track_start and
        # track_end — one self-editing task per guild, same registry shape
        # as _empty_timers above.
        self._np_refresh_tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self.bot.add_view(MusicControlView())

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()
        for task in self._empty_timers.values():
            task.cancel()
        for task in self._np_refresh_tasks.values():
            task.cancel()

    def _stop_np_refresh(self, guild_id: int) -> None:
        task = self._np_refresh_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def _start_np_refresh(self, guild_id: int, player: MusicPlayer) -> None:
        self._stop_np_refresh(guild_id)
        self._np_refresh_tasks[guild_id] = asyncio.ensure_future(self._np_refresh_loop(guild_id, player))

    async def _np_refresh_loop(self, guild_id: int, player: MusicPlayer) -> None:
        try:
            while True:
                await asyncio.sleep(_NOW_PLAYING_REFRESH_SECONDS)
                if player.now_playing_message is None or player.current is None or not player.playing:
                    continue
                embed = _now_playing_embed(player.current, player, player.requester_for(player.current))
                try:
                    await player.now_playing_message.edit(embed=embed)
                except discord.HTTPException:
                    return  # message deleted or otherwise unreachable — stop trying
        except asyncio.CancelledError:
            pass
        finally:
            self._np_refresh_tasks.pop(guild_id, None)

    # ── Shared guards ────────────────────────────────────────────────────────

    @staticmethod
    async def _require_player(ctx: commands.Context) -> MusicPlayer | None:
        vc = ctx.guild.voice_client if ctx.guild else None
        if not isinstance(vc, MusicPlayer) or vc.current is None:
            await ctx.send(embed=_err("Nothing is playing right now."), ephemeral=True)
            return None
        if not isinstance(ctx.author, discord.Member) or ctx.author.voice is None or ctx.author.voice.channel != vc.channel:
            await ctx.send(embed=_err(f"You need to be in {vc.channel.mention} to do that."), ephemeral=True)
            return None
        return vc

    def _cancel_empty_timer(self, guild_id: int) -> None:
        task = self._empty_timers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _disconnect_after_delay(self, guild_id: int, player: MusicPlayer) -> None:
        try:
            await asyncio.sleep(_EMPTY_DISCONNECT_DELAY)
            if player.connected:
                await player.disconnect()
                log.info("Music: auto-disconnected from guild %d (empty channel).", guild_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._empty_timers.pop(guild_id, None)

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("wavelink: node %s ready (session %s).", payload.node.identifier, payload.session_id)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not isinstance(player, MusicPlayer):
            return
        player.skip_votes.clear()
        if player.guild is not None:
            self._start_np_refresh(player.guild.id, player)
        if player.text_channel is None:
            return
        embed = _now_playing_embed(payload.track, player, player.requester_for(payload.track))
        view = MusicControlView(player)
        try:
            player.now_playing_message = await player.text_channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("Music: failed to send now-playing message: %s", exc)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if not isinstance(player, MusicPlayer):
            return
        if player.guild is not None:
            self._stop_np_refresh(player.guild.id)

        if player.loop_mode == LoopMode.TRACK and payload.track is not None:
            await player.play(payload.track)
            return

        if payload.track is not None:
            player.record_played(payload.track)

        if player.loop_mode == LoopMode.QUEUE and payload.track is not None:
            await player.queue.put_wait(payload.track)

        if player.queue.is_empty:
            return

        next_track = await player.queue.get_wait()
        await player.play(next_track)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        guild = member.guild
        vc = guild.voice_client
        if not isinstance(vc, MusicPlayer) or vc.channel is None:
            return

        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if not non_bot_members and not vc.stay_connected:
            if guild.id not in self._empty_timers:
                self._empty_timers[guild.id] = asyncio.ensure_future(
                    self._disconnect_after_delay(guild.id, vc)
                )
        else:
            self._cancel_empty_timer(guild.id)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"You need the {perms} permission(s) to use this command."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(embed=_err(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=_err(msg), ephemeral=True)

    # ── /play (hybrid, high-frequency) ──────────────────────────────────────

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add it to the queue.")
    @app_commands.describe(query="Search term, or a YouTube/Spotify/SoundCloud URL.")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        # wavelink.Playable.search() below is a network call to Lavalink and can
        # easily take >3s (playlists especially). Without an ack, the slash
        # interaction token expires at the 3s mark; every ctx.send() after that
        # -- including the one in the global on_command_error handler if search
        # raises -- then fails with 404 "Unknown interaction" (10062). Deferring
        # up front is a no-op for prefix invocation and acks the interaction for
        # slash invocation.
        await ctx.defer()

        if not isinstance(ctx.author, discord.Member) or ctx.author.voice is None:
            await ctx.send(embed=_err("Join a voice channel first."), ephemeral=True)
            return

        vc = ctx.guild.voice_client if ctx.guild else None

        if vc is None:
            try:
                player: MusicPlayer = await ctx.author.voice.channel.connect(cls=MusicPlayer, self_deaf=True)
            except discord.ClientException:
                await ctx.send(embed=_err("Could not join your voice channel."))
                return
            player.autoplay = wavelink.AutoPlayMode.disabled
        elif isinstance(vc, MusicPlayer):
            if ctx.author.voice.channel != vc.channel:
                await ctx.send(embed=_err(f"I'm already playing in {vc.channel.mention}."))
                return
            player = vc
        else:
            await ctx.send(embed=_err("I'm already connected to voice in an unexpected state."))
            return

        player.text_channel = ctx.channel

        try:
            results = await wavelink.Playable.search(query)
        except wavelink.LavalinkLoadException as exc:
            await ctx.send(embed=_err(f"Could not load that: {exc}"))
            return
        except (wavelink.exceptions.NodeException, asyncio.TimeoutError) as exc:
            # Lavalink node unreachable/slow — surface a clean message instead
            # of letting this bubble to the global handler.
            log.warning("Music: search failed for %r: %s", query, exc)
            await ctx.send(embed=_err("Couldn't reach the music server. Try again in a moment."))
            return

        if not results:
            await ctx.send(embed=_err(f"No results found for **{query}**."))
            return

        if isinstance(results, wavelink.Playlist):
            for track in results.tracks:
                player.remember_requester(track, ctx.author.id)
            await player.queue.put_wait(results)
            embed = _ok(f"{Emojis.list_emoji} Queued playlist **{results.name}** — {len(results.tracks)} track(s).")
        elif _looks_like_url(query):
            # A direct link is already a specific choice — nothing to pick.
            track = results[0]
            player.remember_requester(track, ctx.author.id)
            await player.queue.put_wait(track)
            embed = _ok(f"{Emojis.track_note} Queued **{track.title}**.")
        else:
            # Plain-text search — let the requester pick from the top hits
            # instead of guessing with results[0]. Queuing happens inside
            # SearchResultView once they choose (or is skipped on cancel/timeout).
            view = SearchResultView(query, results, player, ctx.author)
            view.message = await ctx.send(embed=view.current_embed(), view=view)
            return

        await ctx.send(embed=embed)

        if not player.playing:
            next_track = await player.queue.get_wait()
            await player.play(next_track)

    # ── /skip /pause /resume /stop (hybrid, high-frequency) ────────────────

    @commands.hybrid_command(name="skip", description="Skip the current track (vote-skip once 2+ people are listening).")
    async def skip(self, ctx: commands.Context) -> None:
        # player.skip() below round-trips to the Lavalink node; defer before it
        # (not after) so a slow/laggy node can never eat the 3s ack window and
        # produce the same "Unknown interaction" failure /play had.
        await ctx.defer()
        player = await self._require_player(ctx)
        if player is None or not isinstance(ctx.author, discord.Member):
            return
        title = player.current.title if player.current else "track"
        outcome = await _register_skip_vote(player, ctx.author)
        if outcome is _SkipOutcome.SKIPPED:
            await ctx.send(embed=_ok(f"{Emojis.skip} Skipped **{title}**."))
        elif outcome is _SkipOutcome.ALREADY_VOTED:
            await ctx.send(embed=_err("You've already voted to skip."), ephemeral=True)
        else:
            have, need = len(player.skip_votes), player.votes_required_to_skip()
            await ctx.send(embed=_ok(f"{Emojis.skip} Vote to skip registered ({have}/{need})."))

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    async def pause(self, ctx: commands.Context) -> None:
        await ctx.defer()
        player = await self._require_player(ctx)
        if player is None:
            return
        if player.paused:
            await ctx.send(embed=_err("Already paused."), ephemeral=True)
            return
        await player.pause(True)
        await ctx.send(embed=_ok(f"{Emojis.pause} Paused."))

    @commands.hybrid_command(name="resume", description="Resume the current track.")
    async def resume(self, ctx: commands.Context) -> None:
        await ctx.defer()
        player = await self._require_player(ctx)
        if player is None:
            return
        if not player.paused:
            await ctx.send(embed=_err("Not paused."), ephemeral=True)
            return
        await player.pause(False)
        await ctx.send(embed=_ok(f"{Emojis.resume} Resumed."))

    @commands.hybrid_command(name="stop", description="Stop playback, clear the queue, and disconnect.")
    async def stop(self, ctx: commands.Context) -> None:
        await ctx.defer()
        player = await self._require_player(ctx)
        if player is None:
            return
        self._cancel_empty_timer(ctx.guild.id)
        player.queue.clear()
        await player.stop()
        await player.disconnect()
        await ctx.send(embed=_ok(f"{Emojis.closeConnection} Stopped and disconnected."))

    # ── /music group ─────────────────────────────────────────────────────────

    music_grp = app_commands.Group(name="music", description="Music playback controls.")

    @music_grp.command(name="queue", description="View the current queue.")
    async def music_queue(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Nothing is playing right now."), ephemeral=True)
            return
        view = QueueView(vc, interaction.user)
        await interaction.response.send_message(embed=_queue_page_embed(vc, 0), view=view)

    @music_grp.command(name="nowplaying", description="Show what's currently playing.")
    async def music_nowplaying(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer) or vc.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing right now."), ephemeral=True)
            return
        embed = _now_playing_embed(vc.current, vc, vc.requester_for(vc.current))
        await interaction.response.send_message(embed=embed, view=MusicControlView(vc))

    @music_grp.command(name="loop", description="Set the loop mode.")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Track", value="track"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def music_loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        vc.loop_mode = LoopMode(mode.value)
        await interaction.response.send_message(embed=_ok(f"{Emojis.loop} Loop mode set to **{mode.name}**."))

    @music_grp.command(name="volume", description="Set the playback volume (0–150).")
    @app_commands.describe(level="Volume percentage, 0 to 150.")
    async def music_volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]) -> None:
        # set_volume() is a Lavalink round-trip; ack first, same reasoning as
        # the hybrid commands above.
        await interaction.response.defer()
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.followup.send(embed=_err("Not connected to voice."), ephemeral=True)
            return
        await vc.set_volume(level)
        await interaction.followup.send(
            embed=_ok(f"{_volume_emoji(level)} Volume set to **{level}%**.")
        )

    @music_grp.command(name="remove", description="Remove a single track from the queue by its position.")
    @app_commands.describe(index="Position in /music queue to remove (1 = next up).")
    async def music_remove(self, interaction: discord.Interaction, index: app_commands.Range[int, 1, 500]) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        items = list(vc.queue)
        if index > len(items):
            await interaction.response.send_message(embed=_err(f"Queue only has {len(items)} track(s)."), ephemeral=True)
            return
        removed = items.pop(index - 1)
        if not isinstance(interaction.user, discord.Member) or not (
            _is_dj(interaction.user) or vc.requester_for(removed) == interaction.user.id
        ):
            await interaction.response.send_message(
                embed=_err("Only the requester or a manager can remove that track."), ephemeral=True
            )
            return
        vc.queue.clear()
        for item in items:
            await vc.queue.put_wait(item)
        await interaction.response.send_message(embed=_ok(f"{Emojis.cross_mark} Removed **{removed.title}**."))

    @music_grp.command(name="clear", description="Clear the entire queue.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_clear(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        count = len(vc.queue)
        vc.queue.clear()
        await interaction.response.send_message(embed=_ok(f"{Emojis.list_emoji} Cleared {count} track(s) from the queue."))

    @music_grp.command(name="move", description="Move a queued track to a new position.")
    @app_commands.describe(from_position="Current position (1 = next up).", to_position="Where it should end up.")
    async def music_move(
        self,
        interaction: discord.Interaction,
        from_position: app_commands.Range[int, 1, 500],
        to_position: app_commands.Range[int, 1, 500],
    ) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        items = list(vc.queue)
        if from_position > len(items) or to_position > len(items):
            await interaction.response.send_message(embed=_err(f"Queue only has {len(items)} track(s)."), ephemeral=True)
            return
        track = items.pop(from_position - 1)
        items.insert(to_position - 1, track)
        vc.queue.clear()
        for item in items:
            await vc.queue.put_wait(item)
        await interaction.response.send_message(
            embed=_ok(f"{Emojis.list_emoji} Moved **{track.title}** to position {to_position}.")
        )

    @music_grp.command(name="jump", description="Skip straight to a track in the queue, dropping everything before it.")
    @app_commands.describe(index="Position in /music queue to jump to (1 = next up).")
    async def music_jump(self, interaction: discord.Interaction, index: app_commands.Range[int, 1, 500]) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        items = list(vc.queue)
        if index > len(items):
            await interaction.response.send_message(embed=_err(f"Queue only has {len(items)} track(s)."), ephemeral=True)
            return
        # player.skip.below round-trips to the node; ack first.
        await interaction.response.defer()
        target = items[index - 1]
        vc.queue.clear()
        for item in items[index:]:
            await vc.queue.put_wait(item)
        await vc.play(target)
        await interaction.followup.send(embed=_ok(f"{Emojis.skip} Jumped to **{target.title}**."))

    @music_grp.command(name="previous", description="Replay the last played track.")
    async def music_previous(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        track = vc.pop_previous()
        if track is None:
            await interaction.response.send_message(embed=_err("No previous track to go back to."), ephemeral=True)
            return
        await interaction.response.defer()
        if vc.current is not None:
            await _queue_push_front(vc.queue, vc.current)
        await vc.play(track)
        await interaction.followup.send(embed=_ok(f"{Emojis.track_note} Playing **{track.title}**."))

    @music_grp.command(name="seek", description="Seek within the current track.")
    @app_commands.describe(position="Timestamp like 1:30, or a plain number of seconds.")
    async def music_seek(self, interaction: discord.Interaction, position: str) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer) or vc.current is None:
            await interaction.response.send_message(embed=_err("Nothing is playing right now."), ephemeral=True)
            return
        ms = _parse_seek_position(position)
        if ms is None:
            await interaction.response.send_message(
                embed=_err("Couldn't parse that — try `1:30` or a plain number of seconds."), ephemeral=True
            )
            return
        if ms > vc.current.length:
            await interaction.response.send_message(embed=_err("That's past the end of the track."), ephemeral=True)
            return
        await interaction.response.defer()
        await vc.seek(ms)
        await interaction.followup.send(embed=_ok(f"{Emojis.track_note} Seeked to `{_format_duration(ms)}`."))

    @music_grp.command(name="filter", description="Apply an audio filter preset.")
    @app_commands.choices(
        preset=[app_commands.Choice(name=name.title(), value=name) for name in FILTER_NAMES]
    )
    async def music_filter(self, interaction: discord.Interaction, preset: app_commands.Choice[str]) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        await interaction.response.defer()
        await vc.set_filters(build_filter(preset.value))
        vc.active_filter = preset.value
        await interaction.followup.send(embed=_ok(f"🎛️ Filter set to **{preset.name}**."))

    @music_grp.command(name="autoplay", description="Toggle auto-queuing related tracks once the queue runs out.")
    async def music_autoplay(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        enabling = vc.autoplay != wavelink.AutoPlayMode.enabled
        vc.autoplay = wavelink.AutoPlayMode.enabled if enabling else wavelink.AutoPlayMode.disabled
        state = "enabled" if enabling else "disabled"
        await interaction.response.send_message(embed=_ok(f"{Emojis.loop} Autoplay **{state}**."))

    @music_grp.command(name="lyrics", description="Show lyrics for the current or a specified track.")
    @app_commands.describe(query="Track to search for (defaults to what's currently playing).")
    @app_commands.checks.cooldown(2, 20.0, key=lambda i: i.user.id)
    async def music_lyrics(self, interaction: discord.Interaction, query: str | None = None) -> None:
        assert self.session is not None
        vc = interaction.guild.voice_client if interaction.guild else None

        if query:
            title, artist, duration = query, None, None
        elif isinstance(vc, MusicPlayer) and vc.current is not None:
            title, artist, duration = vc.current.title, vc.current.author, vc.current.length // 1000
        else:
            await interaction.response.send_message(
                embed=_err("Nothing is playing — provide a `query` to search instead."), ephemeral=True
            )
            return

        await interaction.response.defer()
        result = await fetch_lyrics(self.session, title=title, artist=artist, duration_seconds=duration)

        search_term = f"{title} {artist}" if artist else title
        if result is None:
            await interaction.followup.send(
                embed=_err(f"No lyrics found for **{title}**."), view=LyricsView(query=search_term)
            )
            return

        body = "\n".join(line.text for line in result.synced) if result.synced else (result.plain or "")
        header = f"{Emojis.track_note} {result.track_name} — {result.artist_name}"

        if len(body) <= _LYRICS_EMBED_LIMIT:
            embed = discord.Embed(title=header, description=body, colour=discord.Colour.blurple())
            await interaction.followup.send(embed=embed, view=LyricsView(query=search_term))
        else:
            file = discord.File(io.BytesIO(body.encode("utf-8")), filename="lyrics.txt")
            embed = discord.Embed(
                title=header,
                description="*Lyrics too long for an embed — see `lyrics.txt` attached below.*",
                colour=discord.Colour.blurple(),
            )
            await interaction.followup.send(embed=embed, file=file, view=LyricsView(query=search_term))

    @music_grp.command(name="247", description="Toggle staying connected when the voice channel is empty.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_247(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(vc, MusicPlayer):
            await interaction.response.send_message(embed=_err("Not connected to voice."), ephemeral=True)
            return
        vc.stay_connected = not vc.stay_connected
        if vc.stay_connected:
            self._cancel_empty_timer(interaction.guild.id)
            msg = f"{Emojis.closeConnection} 24/7 mode **enabled** — I'll stay connected even if the channel empties."
        else:
            msg = "24/7 mode **disabled**."
        await interaction.response.send_message(embed=_ok(msg))


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))