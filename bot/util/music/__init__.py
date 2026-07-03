"""
bot/util/music/__init__.py
────────────────────────────
wavelink v3 glue layer — everything the Music cog needs that ISN'T a
Discord command/UI concern lives here, mirroring the util/game and
util/db split used elsewhere in this codebase.

Contents
────────
  connect_nodes(bot)   — establishes the wavelink.Pool connection to the
                          Lavalink node defined in docker-compose.yml /
                          application.yml. Called ONCE from bot.py's
                          setup_hook(), never from inside a cog — same
                          "no raw resources inside cogs" principle that
                          keeps asyncpg.Pool out of cog bodies.

  MusicPlayer           — wavelink.Player subclass. wavelink instantiates
                          this automatically (via the `cls=` kwarg on
                          channel.connect()) instead of the base Player,
                          giving each guild's voice connection a place to
                          hang loop-mode / now-playing state without a
                          separate in-memory dict keyed by guild_id.

  LoopMode               — StrEnum: OFF | TRACK | QUEUE.

  build_filter()          — name -> wavelink.Filters factory for the
                          /music filter command (bassboost/nightcore/
                          vaporwave/8d/off), so the cog never hand-rolls
                          equalizer/timescale/rotation numbers inline.

  fetch_lyrics()         — LRCLIB (https://lrclib.net) lookup, no auth.
                          Parses synced LRC into timestamped lines when
                          available, falls back to plain lyrics, and
                          returns None on a clean miss so the caller can
                          fall back to a Genius search-URL button.

  genius_search_url()    — builds a Genius *search* URL only — this file
                          never scrapes Genius. Matches the same
                          link-button-not-scraper discipline already used
                          for the Docs cog's dictionary/Discord-docs
                          fallbacks.

Why a custom Player subclass instead of a bare dict[guild_id, state]?
──────────────────────────────────────────────────────────────────────
wavelink already keys wavelink.Pool's node routing and Player lifecycle
by guild via the voice client machinery discord.py provides. Piggy-backing
our state directly onto that same object means it's automatically cleaned
up when the player disconnects (VoiceProtocol.cleanup()) — no separate
"did we forget to evict this guild_id" bookkeeping to maintain.
"""

import logging
import re
from collections import deque
from enum import StrEnum
from typing import NamedTuple
from urllib.parse import urlencode

import aiohttp
import discord
import wavelink
from discord.ext import commands

from config import settings

log = logging.getLogger(__name__)


# ── Node connection (called once from bot.py setup_hook) ────────────────────

async def connect_nodes(bot: commands.Bot) -> None:
    """Connect to the Lavalink node described by docker-compose.yml.

    Must be awaited from setup_hook() — wavelink.Pool.connect() needs the
    bot's event loop and gateway connection to already be starting up, and
    calling it more than once (e.g. accidentally from a cog's cog_load())
    would attempt duplicate node registrations under the same identifier.
    """
    node = wavelink.Node(
        uri=settings.lavalink_uri,
        password=settings.lavalink_password.get_secret_value(),
        identifier="MAIN",
    )
    await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
    log.info("wavelink: connecting to Lavalink node at %s", settings.lavalink_uri)


# ── Loop mode ─────────────────────────────────────────────────────────────────

class LoopMode(StrEnum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


# ── Audio filter presets ──────────────────────────────────────────────────────

FILTER_NAMES = ("off", "bassboost", "nightcore", "vaporwave", "8d")


def build_filter(name: str) -> wavelink.Filters:
    """Return a wavelink.Filters preset by name (see FILTER_NAMES).

    Each call starts from a fresh wavelink.Filters() rather than mutating
    the player's existing filter object, so switching presets never leaves
    stray equalizer/timescale/rotation state from a previous preset behind
    — "off" is a real reset, not just "undo the last thing applied".
    """
    filters = wavelink.Filters()

    if name == "bassboost":
        # Boost the low end, mirroring the classic Lavalink bassboost EQ curve.
        filters.equalizer.set(bands=[
            {"band": 0, "gain": 0.25}, {"band": 1, "gain": 0.20}, {"band": 2, "gain": 0.15},
            {"band": 3, "gain": 0.10}, {"band": 4, "gain": 0.05},
        ])
    elif name == "nightcore":
        filters.timescale.set(speed=1.15, pitch=1.2, rate=1.0)
    elif name == "vaporwave":
        filters.timescale.set(speed=0.85, pitch=0.85, rate=1.0)
    elif name == "8d":
        filters.rotation.set(rotation_hz=0.2)
    elif name != "off":
        raise ValueError(f"Unknown filter preset: {name!r}")

    return filters


# ── Custom player ─────────────────────────────────────────────────────────────

class MusicPlayer(wavelink.Player):
    """wavelink.Player subclass carrying per-guild state for the Music cog.

    Instantiated automatically by wavelink when the cog calls
    `channel.connect(cls=MusicPlayer)` — nothing else needs to construct
    this directly.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loop_mode: LoopMode = LoopMode.OFF
        self.text_channel: discord.abc.Messageable | None = None
        self.now_playing_message: discord.Message | None = None
        # identifier -> requester user id, so /nowplaying and /queue can
        # show "requested by" without a DB round-trip.
        self.requesters: dict[str, int] = {}
        # Reversible per-guild toggle for /247 — checked by the empty-voice-
        # channel auto-disconnect timer in the cog before it tears down.
        self.stay_connected: bool = False
        # Bounded "played" stack for /music previous — most-recent last,
        # capped so a long session doesn't grow this unbounded.
        self.history: deque[wavelink.Playable] = deque(maxlen=25)
        # user ids that have voted to skip the *current* track. Cleared on
        # every track_start (see on_wavelink_track_start in the cog) so a
        # stale vote from the previous song can never carry over.
        self.skip_votes: set[int] = set()
        # Active filter preset name ("bassboost", "off", ...) purely for
        # display in the now-playing embed — the actual DSP state lives on
        # player.filters via wavelink.
        self.active_filter: str = "off"

    def requester_for(self, track: wavelink.Playable) -> int | None:
        return self.requesters.get(track.identifier)

    def remember_requester(self, track: wavelink.Playable, user_id: int) -> None:
        self.requesters[track.identifier] = user_id

    def record_played(self, track: wavelink.Playable) -> None:
        """Push a finished/skipped track onto the history stack."""
        self.history.append(track)

    def pop_previous(self) -> wavelink.Playable | None:
        """Pop the most recently played track for /music previous, or None."""
        return self.history.pop() if self.history else None

    def listening_members(self) -> list[discord.Member]:
        """Non-bot humans currently in the player's voice channel."""
        if self.channel is None:
            return []
        return [m for m in self.channel.members if not m.bot]

    def votes_required_to_skip(self) -> int:
        """Simple majority of listeners (minimum 1) — FredBoat-style vote-skip."""
        return max(1, (len(self.listening_members()) // 2) + 1)


# ── Lyrics (LRCLIB primary, Genius search-link fallback) ────────────────────

class LyricsLine(NamedTuple):
    timestamp_ms: int
    text: str


class LyricsResult(NamedTuple):
    track_name: str
    artist_name: str
    synced: list[LyricsLine] | None
    plain: str | None


_LRCLIB_GET_URL = "https://lrclib.net/api/get"
_LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
_LRC_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Matches "[mm:ss.xx]lyric text" — LRCLIB's synced-lyrics line format.
_LRC_LINE_RE = re.compile(r"^\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)$")


def _parse_synced_lyrics(raw: str) -> list[LyricsLine]:
    """Parse LRC-format synced lyrics into (timestamp_ms, text) tuples.

    Malformed lines are skipped rather than raising — a partially-parsed
    lyric sync is far more useful to the caller than no lyrics at all.
    """
    lines: list[LyricsLine] = []
    for raw_line in raw.splitlines():
        match = _LRC_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        minutes, seconds, frac, text = match.groups()
        # frac may be 2 or 3 digits (centiseconds or milliseconds) — normalise to ms.
        frac_ms = int(frac.ljust(3, "0")) if len(frac) < 3 else int(frac)
        timestamp_ms = (int(minutes) * 60_000) + (int(seconds) * 1000) + frac_ms
        lines.append(LyricsLine(timestamp_ms=timestamp_ms, text=text.strip()))
    return lines


async def fetch_lyrics(
    session: aiohttp.ClientSession,
    *,
    title: str,
    artist: str | None = None,
    duration_seconds: int | None = None,
) -> LyricsResult | None:
    """Look up lyrics on LRCLIB.

    Tries the exact-match `/api/get` endpoint first (fastest, most accurate
    when artist + duration are known), then falls back to `/api/search`
    for a fuzzy title-only lookup. Returns None on a clean miss — the
    caller should render a Genius search-URL button in that case rather
    than treating it as an error.
    """
    params: dict[str, str] = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if duration_seconds is not None:
        params["duration"] = str(duration_seconds)

    try:
        async with session.get(_LRCLIB_GET_URL, params=params, timeout=_LRC_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                return _result_from_payload(data)
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.debug("LRCLIB /get failed for %r: %s", title, exc)

    # Fuzzy fallback — search endpoint returns a list; take the top hit.
    try:
        async with session.get(
            _LRCLIB_SEARCH_URL, params={"q": title}, timeout=_LRC_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None
            results = await resp.json()
            if not results:
                return None
            return _result_from_payload(results[0])
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.debug("LRCLIB /search failed for %r: %s", title, exc)
        return None


def _result_from_payload(data: dict) -> LyricsResult | None:
    synced_raw = data.get("syncedLyrics")
    plain = data.get("plainLyrics")
    if not synced_raw and not plain:
        return None
    return LyricsResult(
        track_name=data.get("trackName", ""),
        artist_name=data.get("artistName", ""),
        synced=_parse_synced_lyrics(synced_raw) if synced_raw else None,
        plain=plain,
    )


def genius_search_url(query: str) -> str:
    """Build a Genius *search* URL — link-button fallback only, never scraped."""
    return f"https://genius.com/search?{urlencode({'q': query})}"