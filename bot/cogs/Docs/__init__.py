"""
Docs cog — unified documentation search.

This merges two previous implementations into one discord.py-native cog:

  * The *old* engine (``docs_old_engine.py``, nextcord-based) which parsed
    Sphinx ``objects.inv`` inventories for discord.py / nextcord / disnake /
    py-cord / their extensions / and the Python stdlib, fuzzy-matched the
    query against the inventory, and replied with a link.

  * The *new* engine (``docs.py``, discord.py) which searches the Discord
    Developer Documentation and replies with an autocomplete-driven embed
    + link button.

Both data sources now live behind a single ``/docs`` slash command with a
``library`` choice. UI has been rebuilt with modern discord.py components:

  * ``DocsResultSelect``  — a Select so a user can browse every fuzzy match
    returned for a library search, not just the top hit (the old engine
    only ever showed the #1 match).
  * ``DocsResultView`` / ``ApiDocsResultView`` — Views wrapping the Select,
    a link Button to the resolved page, and a Delete Button restricted to
    the original invoker via ``interaction_check``.
  * Autocomplete on the ``query`` parameter, branching on whichever
    ``library`` the user already picked (read via ``interaction.namespace``).

Discord Developer Docs (API) source
──────────────────────────────────────────────────────────────
This library choice is deliberately *not* backed by algoliasearch — algolia
is untouched everywhere else it's used, but the Discord docs are fetched
straight from Discord's own official ``llms.txt`` index (see
``API_DOCS_LLMS_TXT_URL``), a flat Markdown list of every developer-docs
page with a title, URL, and one-line description. ``parse_llms_txt`` turns
that into a list of ``ApiDocMatch`` entries, cached on the cog after the
first fetch (see ``_get_api_docs_table`` / ``docscache``), and searched with
the same ``util.docs.fuzzy`` finder the Sphinx-based libraries use below —
no external search service or API key required for this source at all.

Sphinx inventory entries (``objects.inv``) only ever carry a name, a URL,
and a role (``py:function``, ``c:macro``, ``std:label``, …) — there is no
prose description in the inventory itself. To still show *something*
useful in the embed beyond a bare link, ``_get_symbol_description`` does a
best-effort scrape of the first paragraph following the symbol's anchor on
the actual docs page (cached per-URL). This only works reliably against
standard Sphinx HTML output (``<dt id=...><dd><p>...``); if a given page's
markup doesn't match, the scrape just returns ``None`` and the embed falls
back to showing the role label only.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import zlib
from typing import NamedTuple

import aiohttp
import bs4
import discord
from discord import app_commands
from discord.ext import commands

from util.docs import fuzzy
from util.constants import Emojis

log = logging.getLogger(__name__)

# lxml's C parser is dramatically faster than the stdlib html.parser on large
# readthedocs pages (sidebar/search-index markup adds up). Fall back cleanly
# if lxml isn't installed rather than hard-requiring it.
try:
    import lxml  # noqa: F401

    _BS4_PARSER = "lxml"
except ImportError:
    _BS4_PARSER = "html.parser"

# Sentinel "library" choice value that routes the query through the
# llms.txt-backed Discord Developer Docs search instead of a Sphinx
# objects.inv lookup.
API_DOCS_KEY = "discord-api"

# Discord's official llms.txt index — a flat Markdown list of every
# developer-docs page (title, URL, one-line description). This is fetched
# and parsed instead of using algoliasearch for this particular source.
API_DOCS_LLMS_TXT_URL = "https://docs.discord.com/llms.txt"

LIBRARY_SOURCES: dict[str, str] = {
    "discord.py": "https://discordpy.readthedocs.io/en/latest/",
    "nextcord": "https://docs.nextcord.dev/en/latest/",
    "disnake": "https://docs.disnake.dev/en/latest/",
    "py-cord": "https://docs.pycord.dev/en/master/",
    "nextcord-ext-menus": "https://menus.docs.nextcord.dev/en/latest/",
    "nextcord-ext-ipc": "https://ipc.docs.nextcord.dev/en/latest/",
    "python": "https://docs.python.org/3/",
}


def _library_emoji(library: str) -> str:
    """Returns the branded emoji for a library key, or a fallback."""
    return Emojis.library_icons.get(library, Emojis.book)


_PREFIX_STRIP = (
    "nextcord.ext.commands.",
    "nextcord.ext.menus.",
    "nextcord.ext.ipc.",
    "nextcord.",
    "discord.ext.commands.",
    "discord.",
    "disnake.ext.commands.",
    "disnake.",
)

# Human-readable labels for Sphinx inventory roles (domain:subdirective).
_ROLE_LABELS: dict[str, str] = {
    "py:function": "Python function",
    "py:method": "Python method",
    "py:class": "Python class",
    "py:exception": "Python exception",
    "py:attribute": "Python attribute",
    "py:property": "Python property",
    "py:data": "Python data",
    "py:module": "Python module",
    "py:decorator": "Python decorator",
    "c:function": "C function",
    "c:macro": "C macro",
    "c:member": "C struct member",
    "c:type": "C type",
    "c:var": "C variable",
    "std:label": "Documentation section",
    "std:term": "Glossary term",
    "std:doc": "Documentation page",
}


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role)


class DocMatch(NamedTuple):
    name: str
    url: str
    kind: str = ""
    qualname: str = ""


# ── Sphinx objects.inv parsing (ported from the old engine) ────────────────────

class SphinxObjectFileReader:
    """Inspired by Sphinx's InventoryFileReader."""

    BUFSIZE = 16 * 1024

    def __init__(self, buffer: bytes) -> None:
        self.stream = io.BytesIO(buffer)

    def readline(self) -> str:
        return self.stream.readline().decode("utf-8")

    def read_compressed_chunks(self):
        decompressor = zlib.decompressobj()
        while True:
            chunk = self.stream.read(self.BUFSIZE)
            if len(chunk) == 0:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()

    def read_compressed_lines(self):
        buf = b""
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b"\n")
            while pos != -1:
                yield buf[:pos].decode("utf-8")
                buf = buf[pos + 1:]
                pos = buf.find(b"\n")


def parse_object_inv(stream: SphinxObjectFileReader, url: str) -> dict[str, tuple[str, str, str]]:
    """Parse a Sphinx ``objects.inv`` stream into a ``{name: (page_url, role, qualname)}`` mapping.

    ``role`` is the raw Sphinx directive (e.g. ``py:function``, ``c:macro``)
    and ``qualname`` is the original inventory entry name *before*
    library-prefix stripping, kept around to give a touch more context in
    embeds (e.g. distinguishing ``cmath.sinh`` from a bare ``sinh``).
    """
    result: dict[str, tuple[str, str, str]] = {}
    inv_version = stream.readline().rstrip()

    if inv_version != "# Sphinx inventory version 2":
        raise RuntimeError("Invalid objects.inv file version.")

    stream.readline()  # project name — unused
    stream.readline()  # version — unused

    line = stream.readline()
    if "zlib" not in line:
        raise RuntimeError("Invalid objects.inv file, not zlib compatible.")

    entry_regex = re.compile(r"(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)")
    for line in stream.read_compressed_lines():
        match = entry_regex.match(line.rstrip())
        if not match:
            continue

        name, directive, _prio, location, dispname = match.groups()
        domain, _, subdirective = directive.partition(":")
        if directive == "py:module" and name in result:
            continue

        role = directive  # keep the original role before std:doc relabels subdirective below
        if directive == "std:doc":
            subdirective = "label"

        if location.endswith("$"):
            location = location[:-1] + name

        key = name if dispname == "-" else dispname
        prefix = f"{subdirective}:" if domain == "std" else ""

        for old in _PREFIX_STRIP:
            key = key.replace(old, "")

        result[f"{prefix}{key}"] = (os.path.join(url, location), role, name)

    return result


# ── Discord Developer Docs (llms.txt) parsing ───────────────────────────────

class ApiDocMatch(NamedTuple):
    title: str
    url: str
    description: str = ""


# Matches a Markdown list entry of the form:
#   - [Title](https://example.com/page.md): Optional description text.
# The trailing "``: description``" is optional — some entries in Discord's
# llms.txt are bare links with no description.
_LLMS_TXT_ENTRY_RE = re.compile(
    r"^-\s*\[(?P<title>[^\]]+)]\((?P<url>[^)]+)\)(?::\s*(?P<description>.+))?\s*$")


def parse_llms_txt(text: str) -> list[ApiDocMatch]:
    """Parse Discord's ``llms.txt`` into a flat list of ``ApiDocMatch``.

    The file is a plain Markdown bullet list (grouped under ``##`` headings
    we don't need to care about), one page per line. Entries under an
    ``## Optional`` heading (site-level links like the developer portal
    homepage, not actual docs pages) are skipped since they're not useful
    search results.

    Each entry's URL points at the ``.md`` (raw Markdown) version of the
    page; we strip that suffix so the link we hand back to the user opens
    the normal, human-readable docs page instead.
    """
    entries: list[ApiDocMatch] = []
    in_optional_section = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            in_optional_section = stripped.lstrip(
                "#").strip().lower() == "optional"
            continue
        if in_optional_section:
            continue

        match = _LLMS_TXT_ENTRY_RE.match(stripped)
        if not match:
            continue

        title = match.group("title").strip()
        url = match.group("url").strip().removesuffix(".md")
        description = (match.group("description") or "").strip()
        entries.append(ApiDocMatch(
            title=title, url=url, description=description))

    return entries


# ── Shared view scaffolding ──────────────────────────────────────────────────

class DocsAuthorCheckView(discord.ui.View):
    """Base view that restricts every interaction to the original invoker."""

    def __init__(self, *, author_id: int, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(f"{Emojis.prohibited} This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class DeleteButton(discord.ui.Button):
    def __init__(self, *, row: int = 1) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         emoji=Emojis.trashcan, label="Delete", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.message.delete()


# ── Sphinx library docs view ─────────────────────────────────────────────────

def _build_library_description(*, kind: str, description: str | None, url: str) -> str:
    bits: list[str] = []
    if kind:
        bits.append(f"*{kind}*")
    if description:
        bits.append(description)
    bits.append(f"[Jump to documentation]({url})")
    return "\n".join(bits)


def _build_api_docs_description(match: "ApiDocMatch") -> str:
    bits: list[str] = []
    if match.description:
        bits.append(match.description)
    bits.append(f"[Jump to documentation]({match.url})")
    return "\n".join(bits)


class DocsResultSelect(discord.ui.Select):
    """Lets the user browse every fuzzy match, not just the top hit."""

    def __init__(self, matches: list[DocMatch], *, library: str, cog: "Docs") -> None:
        self.cog = cog
        self._lookup = {match.name[:100]: (
            match.url, match.kind) for match in matches}
        options = [discord.SelectOption(
            label=match.name[:100], value=match.name[:100]) for match in matches[:25]]
        plural = "es" if len(matches) != 1 else ""
        super().__init__(
            placeholder=f"Browse {len(matches)} match{plural} for {library}…",
            options=options,
            row=0,
        )
        self.library = library

    async def callback(self, interaction: discord.Interaction) -> None:
        name = self.values[0]
        url, kind = self._lookup[name]

        await interaction.response.defer()

        description = await self.cog._get_symbol_description(url)

        embed = discord.Embed(
            title=f"{_library_emoji(self.library)} {name}",
            description=_build_library_description(
                kind=kind, description=description, url=url),
            colour=discord.Colour.blurple(),
            url=url,
        )
        embed.set_footer(text=f"{self.library} documentation")

        view: DocsResultView = self.view  # type: ignore[assignment]
        view.link_button.url = url
        view.link_button.disabled = False
        await interaction.edit_original_response(embed=embed, view=view)


class DocsResultView(DocsAuthorCheckView):
    def __init__(self, matches: list[DocMatch], *, library: str, author_id: int, cog: "Docs") -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        first_url = matches[0].url if matches else None
        self.link_button = discord.ui.Button(
            label="Open Page",
            style=discord.ButtonStyle.link,
            url=first_url or "https://discord.com",
            row=1,
            disabled=first_url is None,
        )
        if len(matches) > 1:
            self.add_item(DocsResultSelect(matches, library=library, cog=cog))
        self.add_item(self.link_button)
        self.add_item(DeleteButton(row=1))


# ── Discord Developer Docs (llms.txt) view ──────────────────────────────────

class ApiDocsResultView(DocsAuthorCheckView):
    def __init__(self, url: str, *, author_id: int) -> None:
        super().__init__(author_id=author_id)
        self.add_item(
            discord.ui.Button(label="Open in Developer Docs", emoji=Emojis.book,
                              style=discord.ButtonStyle.link, url=url, row=0)
        )
        self.add_item(DeleteButton(row=0))


# ── Cog ────────────────────────────────────────────────────────────────────────

class Docs(commands.Cog, description="Search Discord API wrapper docs, the Python stdlib docs, and the Discord Developer Documentation."):
    COG_EMOJI = Emojis.book

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._lib_cache: dict[str, dict[str, tuple[str, str, str]]] = {}
        self._desc_cache: dict[str, str] = {}
        self._api_docs_cache: list[ApiDocMatch] | None = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()

    # ── Sphinx inventory cache ───────────────────────────────────────────────

    async def _get_library_table(self, library: str) -> dict[str, tuple[str, str, str]]:
        if library in self._lib_cache:
            return self._lib_cache[library]

        base_url = LIBRARY_SOURCES[library]
        assert self.session is not None
        async with self.session.get(base_url + "objects.inv") as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Could not fetch the docs inventory for **{library}** (HTTP {resp.status}). Try again later.")
            data = await resp.read()

        table = parse_object_inv(SphinxObjectFileReader(data), base_url)
        self._lib_cache[library] = table
        return table

    # ── Best-effort description scraping for Sphinx-based libraries ────────────

    async def _get_symbol_description(self, url: str) -> str | None:
        """Best-effort scrape of the first paragraph following a Sphinx anchor.

        Sphinx ``objects.inv`` files don't carry any prose, only name/url/role,
        so to show something more useful than a bare link we fetch the actual
        page and pull the first ``<p>`` inside the ``<dd>`` that follows the
        symbol's ``<dt id="...">`` anchor. Cached per-URL since pages don't
        change between requests in a single process lifetime. Returns ``None``
        on any failure or markup mismatch rather than raising — this is purely
        cosmetic and should never break the actual docs lookup.
        """
        if url in self._desc_cache:
            return self._desc_cache[url]
        if "#" not in url:
            return None

        page_url, _, anchor = url.partition("#")
        assert self.session is not None
        try:
            async with self.session.get(page_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.debug(
                "Docs cog: failed to fetch %s for description scrape: %s", page_url, exc)
            return None

        try:
            soup = await asyncio.to_thread(bs4.BeautifulSoup, html, _BS4_PARSER)
        except Exception as exc:  # noqa: BLE001 — purely cosmetic, never break the lookup
            log.debug(
                "Docs cog: failed to parse %s for description scrape: %s", page_url, exc)
            return None
        target = soup.find(id=anchor)
        if target is None:
            return None

        dd = target.find_next_sibling("dd")
        if dd is None:
            parent = target.find_parent("dl")
            dd = parent.find("dd") if parent else None
        if dd is None:
            return None

        paragraph = dd.find("p")
        if paragraph is None:
            return None

        text = paragraph.get_text(" ", strip=True)[:300]
        if not text:
            return None

        self._desc_cache[url] = text
        return text

    # ── Discord Developer Docs (llms.txt) table + search ────────────────────

    async def _get_api_docs_table(self) -> list[ApiDocMatch]:
        if self._api_docs_cache is not None:
            return self._api_docs_cache

        assert self.session is not None
        async with self.session.get(API_DOCS_LLMS_TXT_URL) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Could not fetch the Discord Developer Docs index (HTTP {resp.status}). Try again later."
                )
            text = await resp.text()

        table = parse_llms_txt(text)
        self._api_docs_cache = table
        return table

    @staticmethod
    def _search_api_docs(table: list[ApiDocMatch], query: str, *, limit: int = 10) -> list[ApiDocMatch]:
        """Fuzzy-searches the llms.txt table by title and description."""
        if not query:
            return []
        return fuzzy.finder(query, table, key=lambda m: f"{m.title} {m.description}", lazy=False)[:limit]

    # ── Autocomplete (branches on the already-chosen `library`) ────────────────

    async def _query_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        library = interaction.namespace.library

        if library == API_DOCS_KEY:
            if not current:
                return []
            try:
                table = await self._get_api_docs_table()
            except RuntimeError:
                return []
            matches = self._search_api_docs(table, current, limit=25)
            return [app_commands.Choice(name=m.title[:100], value=m.url[:100]) for m in matches]

        if not library or library not in LIBRARY_SOURCES:
            return []

        try:
            table = await self._get_library_table(library)
        except RuntimeError:
            return []

        items = list(table.items())
        if not current:
            sample = items[:25]
        else:
            sample = fuzzy.finder(
                current, items, key=lambda t: t[0], lazy=False)[:25]
        return [app_commands.Choice(name=name[:100], value=name[:100]) for name, (_url, _kind, _qualname) in sample]

    # ── /docs ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="docs", description="Search documentation for a Discord API wrapper, the Python stdlib, or the Discord Developer Docs.")
    @app_commands.describe(
        library="Which documentation source to search.",
        query="What to search for — start typing to see live suggestions.",
    )
    @app_commands.choices(library=[
        app_commands.Choice(name="discord.py", value="discord.py"),
        app_commands.Choice(name="nextcord", value="nextcord"),
        app_commands.Choice(name="disnake", value="disnake"),
        app_commands.Choice(name="py-cord", value="py-cord"),
        app_commands.Choice(name="nextcord-ext-menus",
                            value="nextcord-ext-menus"),
        app_commands.Choice(name="nextcord-ext-ipc", value="nextcord-ext-ipc"),
        app_commands.Choice(name="python", value="python"),
        app_commands.Choice(
            name="Discord Developer Docs (API)", value=API_DOCS_KEY),
    ])
    @app_commands.autocomplete(query=_query_autocomplete)
    async def docs(self, interaction: discord.Interaction, library: str, query: str) -> None:
        await interaction.response.defer()

        if library == API_DOCS_KEY:
            await self._respond_api_docs(interaction, query)
        else:
            await self._respond_library_docs(interaction, library, query)

    async def _respond_api_docs(self, interaction: discord.Interaction, query: str) -> None:
        try:
            table = await self._get_api_docs_table()
        except RuntimeError as exc:
            await interaction.followup.send(f"{Emojis.custom_warning} {exc}", ephemeral=True)
            return

        # `query` is normally the autocomplete-selected page URL. If the user
        # bypassed autocomplete with free text, treat it as a search term instead.
        if query.startswith("http://") or query.startswith("https://"):
            match = next((m for m in table if m.url == query), None)
            if match is None:
                # The exact URL wasn't found verbatim (e.g. it got truncated by
                # Discord's 100-char autocomplete choice-value limit) — fall
                # back to searching by the page's slug.
                slug = query.rsplit(
                    "/", 1)[-1].removesuffix(".md").replace("-", " ")
                candidates = self._search_api_docs(table, slug, limit=1)
                match = candidates[0] if candidates else None
        else:
            candidates = self._search_api_docs(table, query, limit=1)
            match = candidates[0] if candidates else None

        if not match:
            await interaction.followup.send(f"{Emojis.search} No documentation results found for **{query}**.")
            return

        embed = discord.Embed(
            title=f"{_library_emoji(API_DOCS_KEY)} {match.title}",
            description=_build_api_docs_description(match),
            colour=discord.Colour.blurple(),
            url=match.url,
        )
        embed.set_footer(text="Discord Developer Documentation")

        view = ApiDocsResultView(match.url, author_id=interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()

    async def _respond_library_docs(self, interaction: discord.Interaction, library: str, query: str) -> None:
        try:
            table = await self._get_library_table(library)
        except RuntimeError as exc:
            await interaction.followup.send(f"{Emojis.custom_warning} {exc}")
            return

        if not query:
            await interaction.followup.send(LIBRARY_SOURCES[library])
            return

        items = list(table.items())
        results = fuzzy.finder(
            query, items, key=lambda t: t[0], lazy=False)[:25]
        matches = [
            DocMatch(name=name, url=url, kind=_role_label(
                kind), qualname=qualname)
            for name, (url, kind, qualname) in results
        ]

        if not matches:
            await interaction.followup.send(f"{Emojis.search} Could not find anything for **{query}** in the {library} docs. Sorry.")
            return

        first = matches[0]
        description = await self._get_symbol_description(first.url)

        embed = discord.Embed(
            title=f"{_library_emoji(library)} {first.name}",
            description=_build_library_description(
                kind=first.kind, description=description, url=first.url),
            colour=discord.Colour.blurple(),
            url=first.url,
        )
        embed.set_footer(text=f"{library} documentation")

        view = DocsResultView(matches, library=library,
                              author_id=interaction.user.id, cog=self)
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()

    # ── Owner cache management ──────────────────────────────────────────────

    @commands.command(name="docscache", aliases=["purge-docs", "deldocs"], description="Purge cached documentation inventories (owner only).")
    @commands.is_owner()
    async def docscache(self, ctx: commands.Context, library: str | None = None) -> None:
        if library == API_DOCS_KEY:
            self._api_docs_cache = None
            title = "Purged docs cache for **Discord Developer Docs (API)**."
        elif library:
            self._lib_cache.pop(library, None)
            title = f"Purged docs cache for **{library}**."
        else:
            self._lib_cache.clear()
            self._desc_cache.clear()
            self._api_docs_cache = None
            title = "Purged all docs caches."
        embed = discord.Embed(title=title, colour=discord.Colour.blurple())
        await ctx.send(embed=embed)


# ── Extension entry-point ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Docs(bot))
