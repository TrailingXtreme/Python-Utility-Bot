<div align="center">

# Discord Utility Bot

A modern, modular Discord bot built on **discord.py** and backed by **PostgreSQL** —
migrated from a legacy `nextcord` + `MongoDB` stack to a fully async, type-safe,
slash-command-first architecture.

[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/postgresql-17-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

## Features

- **Music** — full voice playback via `wavelink` + Lavalink (YouTube, Spotify,
  SoundCloud), persistent playback controls that survive restarts, synced
  lyrics via LRCLIB with a Genius fallback.
- **Moderation** — kick/ban/timeout with confirmation prompts, permission
  hierarchy checks, and a configurable per-event mod-log system.
- **Games** — Wordle (daily/random/by-ID) and Tic-Tac-Toe (vs. bot with three
  difficulty levels, or vs. another player).
- **Calculator** — scientific calculator with an interactive button/select UI,
  powered by a sandboxed expression evaluator (no `eval()` on raw input).
- **Suggestions** — a full suggest → review → approve/deny pipeline with
  persistent buttons and configurable channels.
- **Invite tracking** — attributes joins to the invite that was used, with a
  server leaderboard and per-member stats.
- **Thread management** — create, archive, lock, purge, and browse threads
  entirely through slash commands and interactive panels.
- **AFK status** — automatic AFK nickname tagging with mention replies and
  clean recovery on return.
- **Documentation search** — fuzzy search across discord.py/nextcord/disnake/
  py-cord/Python docs plus the official Discord Developer Docs.
- **Eval** — a sandboxed `/eval` for everyone (runs in an isolated Judge0
  container) and full in-process access for the bot owner.
- **Fun & utility** — memes, jokes, 8-ball, emoji tools, server/channel
  configuration, and a self-service help menu.

All commands are slash-command-first, with a handful of high-frequency hybrid
commands (`/play`, `/skip`, `/ping`, etc.) also usable with a text prefix.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Discord library | [discord.py](https://github.com/Rapptz/discord.py) `>=2.7.1` (voice extra) |
| Language | Python `>=3.12` |
| Database | PostgreSQL `17` via `asyncpg` |
| Migrations | Alembic (raw SQL, no ORM) |
| Validation | Pydantic v2 + pydantic-settings |
| Music | `wavelink` v3 + Lavalink `4.2.2` (youtube-plugin, lavasrc-plugin) |
| Sandboxed execution | `simpleeval` (calculator), Judge0 (public `/eval`) |
| Package management | [`uv`](https://github.com/astral-sh/uv) |

---

## Self-Hosting Guide

### Prerequisites

- Python **3.12+**
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed
- [Docker](https://docs.docker.com/get-docker/) + Docker Compose (for
  PostgreSQL and Lavalink — everything runs in containers except the bot
  process itself)
- A [Discord bot application](https://discord.com/developers/applications)
  with the **Message Content**, **Server Members**, and **Presence** (if you
  enable it) privileged intents toggled on

### 1. Clone and install dependencies

```bash
git clone https://github.com/abindent/Python-Utility-Bot.git
cd Python-Utility-Bot
uv sync
```

### 2. Configure environment variables

Copy the example file and fill in your own values:

```bash
cp .env.example .env
```

```env
# ── Discord ──────────────────────────────────────────────────────────────
BOT_TOKEN=your-bot-token-here
BOT_DEBUG=false                 # true = sync slash commands to a single dev guild instantly

# Short git commit SHA injected at deploy time (e.g. by CI).
# Leave as "master" for local development.
GIT_SHA=master

# ── PostgreSQL ───────────────────────────────────────────────────────────
DATABASE_URL=postgresql://botuser:botpassword@localhost:5432/discordbot

# ── Lavalink ─────────────────────────────────────────────────────────────
LAVALINK_HOST=localhost
LAVALINK_PORT=2333
LAVALINK_PASSWORD=youshallnotpass
LAVALINK_HTTPS=false

# ── Spotify (optional — enables Spotify link resolution via LavaSrc) ─────
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=

# Notice: Obsolete keys (not used in code anymore) are kept here for reference, but can be removed if you want to clean up the file.
# ── Algolia (Discord Developer Docs search) ───────────────────
# These are the public read-only keys for the discord.dev docs index.
# Only change if you run your own Algolia index.
ALGOLIA_SEARCH_APP_ID=BH4D9OD16A
ALGOLIA_SEARCH_API_KEY=f37d91bd900bbb124c8210cca9efcc01

# ── Misc / optional integrations ─────────────────────────────────────────
SECRET_ID=some-random-opaque-string   # used as the PK for the global vote counter
JUDGE0_API_KEY=                        # optional — only needed for RapidAPI-hosted Judge0
TOPGGTOKEN=                            # optional — enables the "Vote on top.gg" button
GITHUB_TOKEN=                          # optional
```

> **Never commit your real `.env` file.** `BOT_TOKEN` and `DATABASE_URL`
> grant full control over your bot and database respectively.

### 3. Start PostgreSQL and Lavalink

```bash
docker compose up -d postgres lavalink
docker compose logs -f lavalink   # wait for "Lavalink is ready to accept connections"
```

Sanity-check Lavalink came up correctly:

```bash
curl -H "Authorization: youshallnotpass" http://localhost:2333/version
```

### 4. Run database migrations

```bash
uv run alembic upgrade head
```

This creates all tables (`guild_config`, `blacklisted_users`, `afk_status`,
`bot_votes`, `vote_records`, `suggestion_settings`, `suggestions`,
`invite_tracking`, `moderation_settings`) against the database in
`DATABASE_URL`.

### 5. Run the bot

```bash
uv run python bot/bot.py
```

On first run with `BOT_DEBUG=true`, slash commands sync instantly to your
configured dev guild. With `BOT_DEBUG=false`, commands sync globally, which
can take up to an hour to propagate across all servers.

### 6. (Optional) Run via Docker

A `Dockerfile` is provided for running the bot itself in a container
alongside `postgres`/`lavalink`:

```bash
docker compose up -d
```

See [`Dockerfile`](Dockerfile) for build details.

---

## Development

```bash
uv sync --group dev          # install dev dependencies (ruff, mypy, pytest)
uv run ruff check .          # lint
uv run mypy bot/             # type-check
uv run pytest                # run tests
```

New Alembic migrations:

```bash
uv run alembic revision -m "description of change"
```

Migrations use raw `op.execute()` SQL rather than autogenerated ORM diffs —
write the `upgrade()`/`downgrade()` SQL by hand for full control over the
schema.

---

## Project Structure

```
bot/
├── bot.py                 # Entry point — DiscordBot, BotTree, setup_hook
├── config.py               # pydantic-settings environment configuration
├── cogs/                   # One directory per feature area
│   ├── Music/
│   ├── Moderation/
│   ├── Games/
│   ├── Calculator/
│   ├── Suggestion/
│   ├── Invite/
│   ├── Thread/
│   ├── Afk/
│   ├── Docs/
│   ├── Eval/
│   ├── Help/
│   ├── Dev/               # Owner-only extension management
│   └── ...                # Add more as per your choice
└── util/
    ├── db/                 # Repository pattern — asyncpg pool + Pydantic models
    ├── music/              # wavelink glue (player subclass, lyrics fetcher)
    ├── game/               # Wordle + Tic-Tac-Toe engine
    ├── docs/               # Fuzzy search helpers
    └── constants/          # Emojis, colours, static metadata
```

---

## License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for the full
text.