"""Thread cog — advanced thread management inspired by Dyno, ProBot, Arcane & MEE6.
────────────────────────────────────────────────────────────────────────────────
Commands
────────
  /thread info        — Rich metadata embed about any thread.
  /thread create      — Create a public or private thread via a modal
                        (optionally anchored to an existing message).
  /thread archive     — Archive a thread; optional auto-archive duration picker.
  /thread unarchive   — Unarchive / reopen a thread.
  /thread lock        — Lock a thread — only moderators can reply.
  /thread unlock      — Unlock a thread and post an in-thread notice.
  /thread rename      — Rename via a Discord modal (pre-filled with current name).
  /thread slowmode    — Set or clear slowmode (0 – 21 600 s).
  /thread pin         — Toggle the pinned flag on a thread.
  /thread delete      — Delete with an ephemeral Confirm / Cancel button prompt.
  /thread purge       — Bulk-delete up to 500 messages with optional filters.
  /thread add         — Add a member to a private thread.
  /thread remove      — Remove a member from a private thread.
  /thread members     — Paginated member list (10 per page, ◀ ▶ buttons).
  /thread manage      — Button-driven management panel (live state display).
  /thread stats       — Detailed statistics, including a live member fetch.
  /thread list        — Browse all threads in a channel via a select menu.
  /thread convert     — Change the thread type (public ↔ private / announcement).

Interactive components
──────────────────────
  RenameModal         — Single TextInput; pre-filled with the current name.
  CreateThreadModal   — Name + optional opening-message fields.
  ConfirmDeleteView   — "Delete 🗑️" / "Cancel" ephemeral buttons (30 s timeout).
  MemberListView      — ◀ page-indicator ▶ pagination (120 s timeout).
  ManagePanelView     — Lock / Archive / Pin / Rename / Refresh / Close
                        buttons with live state (180 s timeout).
  _ThreadSelectMenu   — Select menu showing up to 25 threads per page.
  ThreadListView      — Hosts _ThreadSelectMenu + optional ◀ / ▶ nav buttons
                        for channels with more than 25 threads (120 s timeout).

Thread API used
───────────────
  discord.Thread.edit()          — name, archived, locked, slowmode_delay,
                                   auto_archive_duration, pinned, invitable, reason
  discord.Thread.delete()        — permanent deletion
  discord.Thread.purge()         — bulk-delete with a check callback
  discord.Thread.add_user()      — add a member to a private thread
  discord.Thread.remove_user()   — remove a member from a private thread
  discord.Thread.fetch_members() — fetch all ThreadMember objects
  discord.Thread.send()          — post in-thread notices (lock / unlock)
  discord.Message.create_thread()           — anchor a thread to a message
  discord.TextChannel.create_thread()       — free-standing thread creation
  discord.TextChannel.archived_threads()    — async-iterate archived threads
  discord.TextChannel.threads              — cached active threads
  discord.HTTPClient.edit_channel()         — low-level PATCH for type conversion
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


# ── Constants ─────────────────────────────────────────────────────────────────

PURGE_LIMIT = 500  # absolute safety cap for /thread purge

_AUTO_ARCHIVE_LABELS: dict[int, str] = {
    60: "1 hour",
    1440: "24 hours (1 day)",
    4320: "3 days",
    10080: "1 week",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _colour() -> discord.Colour:
    return discord.Colour(random.randint(0, 0xFFFFFF))


def _embed(description: str, colour: discord.Colour, *, title: str = "") -> discord.Embed:
    e = discord.Embed(description=description, colour=colour)
    if title:
        e.title = title
    e.timestamp = datetime.now(timezone.utc)
    return e


def _ok(text: str) -> discord.Embed:
    return _embed(f"✅  {text}", discord.Colour.green())


def _err(text: str) -> discord.Embed:
    return _embed(f"❌  {text}", discord.Colour.red())


def _fmt_duration(seconds: int) -> str:
    """Convert a raw second count into a human-readable string, e.g. '1h 30m 5s'."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"


def _fmt_auto_archive(minutes: int | None) -> str:
    """Map an auto-archive duration (minutes) to a friendly label."""
    if minutes is None:
        return "Unknown"
    return _AUTO_ARCHIVE_LABELS.get(minutes, f"{minutes} min")


def _thread_status(thread: discord.Thread) -> str:
    """Compose a short status string, e.g. '🔒 Locked · 📌 Pinned'."""
    parts: list[str] = []
    if thread.locked:
        parts.append("🔒 Locked")
    if thread.archived:
        parts.append("📁 Archived")
    if not parts:
        parts.append("💬 Active")
    if getattr(thread.flags, "pinned", False):
        parts.append("📌 Pinned")
    return " · ".join(parts)


def _thread_type_label(thread: discord.Thread) -> str:
    return {
        discord.ChannelType.public_thread: "Public Thread",
        discord.ChannelType.private_thread: "Private Thread",
        discord.ChannelType.news_thread: "Announcement Thread",
    }.get(thread.type, str(thread.type).replace("_", " ").title())


def _build_info_embed(
    thread: discord.Thread,
    requester: discord.User | discord.Member,
) -> discord.Embed:
    """Shared rich-info embed used by /thread info and the ThreadListView callback."""
    embed = discord.Embed(
        title=f"Thread Info — {thread.name}",
        colour=_colour(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="ID", value=f"`{thread.id}`")
    embed.add_field(name="Type", value=_thread_type_label(thread))
    embed.add_field(name="Status", value=_thread_status(thread))
    embed.add_field(name="Parent", value=f"<#{thread.parent_id}>")
    embed.add_field(
        name="Owner",
        value=f"<@{thread.owner_id}>" if thread.owner_id else "*Unknown*",
    )
    embed.add_field(
        name="Auto-archive After",
        value=_fmt_auto_archive(thread.auto_archive_duration),
    )
    embed.add_field(
        name="Slowmode",
        value=_fmt_duration(thread.slowmode_delay) if thread.slowmode_delay else "Off",
    )
    embed.add_field(name="≈ Messages", value=str(thread.message_count))
    embed.add_field(name="≈ Members", value=str(thread.member_count))
    if thread.created_at:
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(thread.created_at, "F"),
            inline=False,
        )
        embed.add_field(
            name="Age",
            value=discord.utils.format_dt(thread.created_at, "R"),
        )
    if thread.archive_timestamp:
        embed.add_field(
            name="Last Archived / Unarchived",
            value=discord.utils.format_dt(thread.archive_timestamp, "R"),
        )
    embed.set_footer(text=f"Requested by {requester}")
    return embed


# ── Modals ────────────────────────────────────────────────────────────────────


class RenameModal(discord.ui.Modal, title="Rename Thread"):
    """Single-field modal — pre-filled with the thread's current name."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="New thread name",
        placeholder="Enter a new name…",
        min_length=1,
        max_length=100,
    )

    def __init__(self, thread: discord.Thread) -> None:
        super().__init__()
        self.thread = thread
        self.name.default = thread.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        old = self.thread.name
        await self.thread.edit(name=self.name.value)
        await interaction.response.send_message(
            embed=_ok(f"Renamed **{old}** → **{self.name.value}**."),
            ephemeral=True,
        )


class CreateThreadModal(discord.ui.Modal, title="Create Thread"):
    """Name + optional opening-message modal for /thread create."""

    thread_name: discord.ui.TextInput = discord.ui.TextInput(
        label="Thread name",
        placeholder="Enter a thread name…",
        min_length=1,
        max_length=100,
    )
    opening_message: discord.ui.TextInput = discord.ui.TextInput(
        label="Opening message (optional)",
        placeholder="First message to send inside the thread…",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
    )

    def __init__(
        self,
        *,
        channel: discord.TextChannel,
        private: bool,
        message: discord.Message | None,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.private = private
        self.message = message  # anchor message, or None for a free-standing thread

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if self.message:
                # Anchor the thread to an existing message (always public)
                thread = await self.message.create_thread(
                    name=self.thread_name.value,
                    auto_archive_duration=1440,
                )
            else:
                thread = await self.channel.create_thread(
                    name=self.thread_name.value,
                    type=(
                        discord.ChannelType.private_thread
                        if self.private
                        else discord.ChannelType.public_thread
                    ),
                    auto_archive_duration=1440,
                )
            if self.opening_message.value:
                await thread.send(self.opening_message.value)
            await interaction.followup.send(
                embed=_ok(f"Thread {thread.mention} created."), ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=_err(f"Could not create thread: {exc.text}"), ephemeral=True
            )


# ── Views ─────────────────────────────────────────────────────────────────────


class ConfirmDeleteView(discord.ui.View):
    """Ephemeral Confirm / Cancel prompt for destructive thread deletion."""

    def __init__(
        self,
        thread: discord.Thread,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=30)
        self.thread = thread
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        name = self.thread.name
        self.stop()
        await self.thread.delete()
        await interaction.response.edit_message(
            embed=_ok(f"Thread **{name}** permanently deleted."), view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_ok("Deletion cancelled."), view=None
        )

    async def on_timeout(self) -> None:
        self.stop()


# ─────────────────────────────────────────────────────────────────────────────


class MemberListView(discord.ui.View):
    """Paginated thread-member list — 10 members per page, ◀ indicator ▶ buttons."""

    PER_PAGE = 10

    def __init__(
        self,
        thread: discord.Thread,
        members: list[discord.ThreadMember],
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=120)
        self.thread = thread
        self.members = members
        self.requester = requester
        self.page = 0
        self.max_page = max(0, math.ceil(len(members) / self.PER_PAGE) - 1)
        self._sync()

    # ── internal helpers ──

    def _sync(self) -> None:
        """Update button disabled-states and the page-indicator label."""
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.max_page
        self.page_btn.label = f"{self.page + 1} / {self.max_page + 1}"

    def build_embed(self) -> discord.Embed:
        start = self.page * self.PER_PAGE
        chunk = self.members[start : start + self.PER_PAGE]
        lines = [f"`{i + start + 1}.` <@{m.id}>" for i, m in enumerate(chunk)]
        e = discord.Embed(
            title=f"👥 Members — {self.thread.name}",
            description="\n".join(lines) or "*No members on this page.*",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        e.set_footer(text=f"{len(self.members)} total member(s) • Page {self.page + 1}/{self.max_page + 1}")
        return e

    # ── guard ──

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This list is not for you."), ephemeral=True
            )
            return False
        return True

    # ── buttons ──

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass  # display-only indicator — never fires

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ─────────────────────────────────────────────────────────────────────────────


class _ThreadSelectMenu(discord.ui.Select):
    """One page of up to 25 threads shown as select-menu options.

    Stores a reference to the full thread list so archived threads
    (which may not be in the guild cache) can still be looked up by ID.
    """

    def __init__(
        self,
        all_threads: list[discord.Thread],
        page: int,
        total_pages: int,
    ) -> None:
        self._all = all_threads
        page_threads = all_threads[page * 25 : (page + 1) * 25]
        options = [
            discord.SelectOption(
                label=t.name[:100],
                value=str(t.id),
                description=(
                    f"{'🔒 Locked' if t.locked else '📁 Archived' if t.archived else '💬 Active'}"
                    f"  •  {_thread_type_label(t)}"
                )[:100],
                emoji="🔒" if t.locked else ("📁" if t.archived else "💬"),
            )
            for t in page_threads
        ]
        super().__init__(
            placeholder=f"Pick a thread to inspect…  ({page + 1} / {total_pages})",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        tid = int(self.values[0])
        # Prefer our stored copy (covers archived threads not in guild cache)
        thread: discord.Thread | None = next(
            (t for t in self._all if t.id == tid), None
        )
        if thread is None and interaction.guild:
            thread = interaction.guild.get_thread(tid)
        if thread is None:
            await interaction.response.send_message(
                embed=_err("Thread no longer accessible."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=_build_info_embed(thread, interaction.user), ephemeral=True
        )


class ThreadListView(discord.ui.View):
    """Hosts _ThreadSelectMenu with optional ◀ / ▶ page navigation for channels
    that have more than 25 threads."""

    def __init__(
        self,
        threads: list[discord.Thread],
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=120)
        self.threads = threads
        self.requester = requester
        self.page = 0
        self.total_pages = max(1, math.ceil(len(threads) / 25))
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the select menu and (if needed) nav buttons."""
        self.clear_items()
        self.add_item(_ThreadSelectMenu(self.threads, self.page, self.total_pages))
        if self.total_pages > 1:
            self._add_nav()

    def _add_nav(self) -> None:
        """Dynamically build ◀ label ▶ nav buttons on row 1."""

        async def _prev(interaction: discord.Interaction) -> None:
            if not await self._guard(interaction):
                return
            self.page -= 1
            self._rebuild()
            await interaction.response.edit_message(view=self)

        async def _next(interaction: discord.Interaction) -> None:
            if not await self._guard(interaction):
                return
            self.page += 1
            self._rebuild()
            await interaction.response.edit_message(view=self)

        prev = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=1,
        )
        lbl = discord.ui.Button(
            label=f"Page {self.page + 1} / {self.total_pages}",
            style=discord.ButtonStyle.primary,
            disabled=True,
            row=1,
        )
        nxt = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1,
            row=1,
        )
        prev.callback = _prev
        nxt.callback = _next
        self.add_item(prev)
        self.add_item(lbl)
        self.add_item(nxt)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This menu is not for you."), ephemeral=True
            )
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._guard(interaction)


# ─────────────────────────────────────────────────────────────────────────────


class ManagePanelView(discord.ui.View):
    """Interactive management panel with live state:
    Lock / Archive / Pin (row 0)  •  Rename / Refresh / Close (row 1).
    """

    def __init__(
        self,
        thread: discord.Thread,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.thread = thread
        self.requester = requester
        self._sync()

    # ── helpers ──

    def _sync(self) -> None:
        """Update button labels and styles to reflect current thread state."""
        t = self.thread

        self.toggle_lock.label = "Unlock 🔓" if t.locked else "Lock 🔒"
        self.toggle_lock.style = (
            discord.ButtonStyle.success if t.locked else discord.ButtonStyle.danger
        )

        self.toggle_archive.label = "Unarchive 📂" if t.archived else "Archive 📁"
        self.toggle_archive.style = (
            discord.ButtonStyle.success if t.archived else discord.ButtonStyle.secondary
        )

        pinned = getattr(t.flags, "pinned", False)
        self.toggle_pin.label = "Unpin 📌" if pinned else "Pin 📌"
        self.toggle_pin.style = (
            discord.ButtonStyle.secondary if pinned else discord.ButtonStyle.primary
        )

    def build_embed(self) -> discord.Embed:
        t = self.thread
        embed = discord.Embed(
            title=f"⚙️  Thread Management — {t.name}",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status", value=_thread_status(t))
        embed.add_field(name="Type", value=_thread_type_label(t))
        embed.add_field(
            name="Slowmode",
            value=_fmt_duration(t.slowmode_delay) if t.slowmode_delay else "Off",
        )
        embed.add_field(
            name="Auto-archive After",
            value=_fmt_auto_archive(t.auto_archive_duration),
        )
        embed.add_field(name="≈ Messages", value=str(t.message_count))
        embed.add_field(name="≈ Members", value=str(t.member_count))
        embed.set_footer(
            text="Rename opens a modal.  •  Refresh re-syncs state from Discord."
        )
        return embed

    def _fresh(self, interaction: discord.Interaction) -> discord.Thread | None:
        """Pull the latest Thread object from the guild cache."""
        return interaction.guild.get_thread(self.thread.id) if interaction.guild else None

    # ── guard ──

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True
            )
            return False
        return True

    # ── row 0: state toggles ──

    @discord.ui.button(label="Lock 🔒", style=discord.ButtonStyle.danger, row=0)
    async def toggle_lock(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        new_locked = not self.thread.locked
        kwargs: dict = {"locked": new_locked}
        if not new_locked:
            # Discord requires unarchiving to unlock
            kwargs["archived"] = False
        await self.thread.edit(**kwargs)
        if fresh := self._fresh(interaction):
            self.thread = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Archive 📁", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_archive(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.thread.edit(archived=not self.thread.archived)
        if fresh := self._fresh(interaction):
            self.thread = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Pin 📌", style=discord.ButtonStyle.primary, row=0)
    async def toggle_pin(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            pinned = getattr(self.thread.flags, "pinned", False)
            await self.thread.edit(pinned=not pinned)
            if fresh := self._fresh(interaction):
                self.thread = fresh
            self._sync()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_err(f"Could not toggle pin: {exc.text}"), ephemeral=True
            )

    # ── row 1: utility buttons ──

    @discord.ui.button(label="Rename ✏️", style=discord.ButtonStyle.secondary, row=1)
    async def rename_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Modal response; click Refresh afterwards to see the new name.
        await interaction.response.send_modal(RenameModal(self.thread))

    @discord.ui.button(label="Refresh 🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if fresh := self._fresh(interaction):
            self.thread = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close ✖", style=discord.ButtonStyle.secondary, row=1)
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_ok("Management panel closed."), view=None
        )


# ── Cog ──────────────────────────────────────────────────────────────────────


class Threads(
    commands.Cog,
    description="Advanced thread management — lock, archive, purge, manage & more.",
):
    COG_EMOJI = "🧵"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Command group ────────────────────────────────────────────────────────

    thread_group = app_commands.Group(
        name="thread", description="Thread management commands."
    )

    # ── Centralised error handler ────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"You need the {perms} permission(s) to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            msg = f"I need the {perms} permission(s) to do that."
        else:
            raise error  # bubble up unexpected errors

        if interaction.response.is_done():
            await interaction.followup.send(embed=_err(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=_err(msg), ephemeral=True)

    # ── Shared resolve helper ────────────────────────────────────────────────

    @staticmethod
    def _resolve(
        thread: discord.Thread | None,
        interaction: discord.Interaction,
    ) -> discord.Thread | None:
        """Return the given thread, or the current channel if it's a thread."""
        if thread:
            return thread
        if isinstance(interaction.channel, discord.Thread):
            return interaction.channel
        return None

    # ── /thread info ──────────────────────────────────────────────────────────

    @thread_group.command(
        name="info",
        description="Detailed info embed about any thread.",
    )
    @app_commands.describe(thread="Thread to inspect (defaults to current if inside one)")
    async def thread_info(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this command inside one."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=_build_info_embed(target, interaction.user)
        )

    # ── /thread create ────────────────────────────────────────────────────────

    @thread_group.command(
        name="create",
        description="Create a public or private thread via a modal "
        "(optionally anchored to an existing message).",
    )
    @app_commands.describe(
        private="Create a private thread instead of a public one",
        message_id="Message ID to anchor the thread to (public threads only)",
        channel="Parent channel (defaults to current text channel)",
    )
    @app_commands.checks.has_permissions(create_public_threads=True)
    @app_commands.checks.bot_has_permissions(create_public_threads=True)
    async def thread_create(
        self,
        interaction: discord.Interaction,
        private: bool = False,
        message_id: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        # Resolve the parent text channel
        parent: discord.TextChannel
        if channel:
            parent = channel
        elif isinstance(interaction.channel, discord.TextChannel):
            parent = interaction.channel
        elif isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent  # type: ignore[assignment]
        else:
            await interaction.response.send_message(
                embed=_err("Run this inside a text channel or specify one."),
                ephemeral=True,
            )
            return

        if message_id and private:
            await interaction.response.send_message(
                embed=_err(
                    "Private threads cannot be anchored to a message.\n"
                    "Remove `message_id` **or** set `private` to **False**."
                ),
                ephemeral=True,
            )
            return

        message: discord.Message | None = None
        if message_id:
            try:
                message = await parent.fetch_message(int(message_id))
            except (ValueError, discord.NotFound, discord.HTTPException):
                await interaction.response.send_message(
                    embed=_err("Message not found in that channel. Double-check the ID."),
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(
            CreateThreadModal(channel=parent, private=private, message=message)
        )

    # ── /thread archive ───────────────────────────────────────────────────────

    @thread_group.command(
        name="archive",
        description="Archive a thread, with an optional auto-archive duration.",
    )
    @app_commands.describe(
        thread="Thread to archive (defaults to current)",
        duration="Set the auto-archive duration at the same time",
    )
    @app_commands.choices(
        duration=[
            app_commands.Choice(name="1 hour", value=60),
            app_commands.Choice(name="24 hours", value=1440),
            app_commands.Choice(name="3 days", value=4320),
            app_commands.Choice(name="1 week", value=10080),
        ]
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_archive(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
        duration: int | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        kwargs: dict = {"archived": True}
        if duration:
            kwargs["auto_archive_duration"] = duration
        await target.edit(**kwargs)
        note = f" (auto-archive: {_fmt_auto_archive(duration)})" if duration else ""
        await interaction.response.send_message(
            embed=_ok(f"**{target.name}** archived{note}."), ephemeral=True
        )

    # ── /thread unarchive ─────────────────────────────────────────────────────

    @thread_group.command(
        name="unarchive",
        description="Unarchive / reopen a thread.",
    )
    @app_commands.describe(thread="Thread to unarchive")
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_unarchive(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await target.edit(archived=False)
        await interaction.response.send_message(
            embed=_ok(f"**{target.name}** is now unarchived and active."),
            ephemeral=True,
        )

    # ── /thread lock ──────────────────────────────────────────────────────────

    @thread_group.command(
        name="lock",
        description="Lock a thread — only moderators can send messages.",
    )
    @app_commands.describe(
        thread="Thread to lock (defaults to current)",
        reason="Reason shown in the audit log and the in-thread notice",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_lock(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
        reason: str | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await target.edit(locked=True, reason=reason)
        notice = discord.Embed(
            title="🔒 Thread Locked",
            description=(
                f"This thread was locked by {interaction.user.mention}."
                + (f"\n\n**Reason:** {reason}" if reason else "")
            ),
            colour=discord.Colour.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)
        await interaction.response.send_message(
            embed=_ok(f"🔒 **{target.name}** is now locked."), ephemeral=True
        )

    # ── /thread unlock ────────────────────────────────────────────────────────

    @thread_group.command(
        name="unlock",
        description="Unlock a thread and post an in-thread notice.",
    )
    @app_commands.describe(
        thread="Thread to unlock (defaults to current)",
        reason="Reason shown in the audit log",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_unlock(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
        reason: str | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        # Must unarchive alongside unlocking so the thread becomes active again
        await target.edit(locked=False, archived=False, reason=reason)
        notice = discord.Embed(
            title="🔓 Thread Unlocked",
            description=f"This thread was unlocked by {interaction.user.mention}.",
            colour=discord.Colour.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)
        await interaction.response.send_message(
            embed=_ok(f"🔓 **{target.name}** is now unlocked."), ephemeral=True
        )

    # ── /thread rename ────────────────────────────────────────────────────────

    @thread_group.command(
        name="rename",
        description="Rename a thread via a modal (pre-filled with the current name).",
    )
    @app_commands.describe(thread="Thread to rename (defaults to current)")
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_rename(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await interaction.response.send_modal(RenameModal(target))

    # ── /thread slowmode ──────────────────────────────────────────────────────

    @thread_group.command(
        name="slowmode",
        description="Set or clear slowmode on a thread (0 = off, max 21 600 s / 6 h).",
    )
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 = disabled)",
        thread="Thread to apply slowmode to (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await target.edit(slowmode_delay=seconds)
        msg = (
            f"Slowmode **disabled** on **{target.name}**."
            if seconds == 0
            else f"Slowmode set to **{_fmt_duration(seconds)}** on **{target.name}**."
        )
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)

    # ── /thread pin ───────────────────────────────────────────────────────────

    @thread_group.command(
        name="pin",
        description="Toggle the pinned flag on a thread.",
    )
    @app_commands.describe(thread="Thread to pin / unpin (defaults to current)")
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_pin(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        try:
            pinned = getattr(target.flags, "pinned", False)
            await target.edit(pinned=not pinned)
            state = "📌 **pinned**" if not pinned else "**unpinned**"
            await interaction.response.send_message(
                embed=_ok(f"**{target.name}** is now {state}."), ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_err(f"Could not change pin state: {exc.text}"), ephemeral=True
            )

    # ── /thread delete ────────────────────────────────────────────────────────

    @thread_group.command(
        name="delete",
        description="Permanently delete a thread — shows a Confirm / Cancel prompt first.",
    )
    @app_commands.describe(thread="Thread to delete (defaults to current)")
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_delete(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        prompt = _embed(
            f"⚠️  You are about to permanently delete **{target.name}**.\n"
            "This action **cannot** be undone.",
            discord.Colour.orange(),
            title="Confirm Thread Deletion",
        )
        await interaction.response.send_message(
            embed=prompt,
            view=ConfirmDeleteView(target, interaction.user),
            ephemeral=True,
        )

    # ── /thread purge ─────────────────────────────────────────────────────────

    @thread_group.command(
        name="purge",
        description=f"Bulk-delete up to {PURGE_LIMIT} messages in a thread with optional filters.",
    )
    @app_commands.describe(
        amount=f"Number of messages to scan (1 – {PURGE_LIMIT})",
        user="Only delete messages from this member",
        bots_only="Only delete messages sent by bots",
        contains="Only delete messages whose content contains this text",
        attachments_only="Only delete messages that have attachments",
        embeds_only="Only delete messages that have embeds",
        thread="Thread to purge (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def thread_purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 500],
        user: discord.Member | None = None,
        bots_only: bool = False,
        contains: str | None = None,
        attachments_only: bool = False,
        embeds_only: bool = False,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _check(m: discord.Message) -> bool:
            if user and m.author != user:
                return False
            if bots_only and not m.author.bot:
                return False
            if contains and contains.lower() not in m.content.lower():
                return False
            if attachments_only and not m.attachments:
                return False
            if embeds_only and not m.embeds:
                return False
            return True

        deleted = await target.purge(limit=amount, check=_check, bulk=True)

        # Describe active filters for the confirmation embed
        filters: list[str] = []
        if user:
            filters.append(f"from {user.mention}")
        if bots_only:
            filters.append("by bots")
        if contains:
            filters.append(f"containing `{contains}`")
        if attachments_only:
            filters.append("with attachments")
        if embeds_only:
            filters.append("with embeds")
        filter_str = " " + " & ".join(filters) if filters else ""

        result = _ok(
            f"Deleted **{len(deleted)}** message(s){filter_str} in **{target.name}**."
        )
        result.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=result, ephemeral=True)

    # ── /thread add ───────────────────────────────────────────────────────────

    @thread_group.command(
        name="add",
        description="Add a member to a private thread.",
    )
    @app_commands.describe(
        member="Member to add",
        thread="Thread to add them to (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await target.add_user(member)
        await interaction.response.send_message(
            embed=_ok(f"Added {member.mention} to **{target.name}**."),
            ephemeral=True,
        )

    # ── /thread remove ────────────────────────────────────────────────────────

    @thread_group.command(
        name="remove",
        description="Remove a member from a private thread.",
    )
    @app_commands.describe(
        member="Member to remove",
        thread="Thread to remove them from (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        await target.remove_user(member)
        await interaction.response.send_message(
            embed=_ok(f"Removed {member.mention} from **{target.name}**."),
            ephemeral=True,
        )

    # ── /thread members ───────────────────────────────────────────────────────

    @thread_group.command(
        name="members",
        description="Paginated list of all thread members (10 per page).",
    )
    @app_commands.describe(thread="Thread to inspect (defaults to current)")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def thread_members(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        members = await target.fetch_members()

        if not members:
            await interaction.followup.send(
                embed=_err("No members found in this thread."), ephemeral=True
            )
            return

        view = MemberListView(target, members, interaction.user)
        await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    # ── /thread manage ────────────────────────────────────────────────────────

    @thread_group.command(
        name="manage",
        description="Open the interactive management panel for a thread.",
    )
    @app_commands.describe(thread="Thread to manage (defaults to current)")
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_manage(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return
        view = ManagePanelView(target, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    # ── /thread stats ─────────────────────────────────────────────────────────

    @thread_group.command(
        name="stats",
        description="Detailed statistics about a thread, including a live member fetch.",
    )
    @app_commands.describe(thread="Thread to inspect (defaults to current)")
    async def thread_stats(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return

        await interaction.response.defer()
        members = await target.fetch_members()  # live fetch for accuracy

        embed = discord.Embed(
            title=f"📊 Thread Stats — {target.name}",
            colour=_colour(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="ID", value=f"`{target.id}`")
        embed.add_field(name="Type", value=_thread_type_label(target))
        embed.add_field(name="Status", value=_thread_status(target))
        embed.add_field(name="Parent", value=f"<#{target.parent_id}>")
        embed.add_field(
            name="Owner",
            value=f"<@{target.owner_id}>" if target.owner_id else "*Unknown*",
        )
        embed.add_field(
            name="Slowmode",
            value=_fmt_duration(target.slowmode_delay) if target.slowmode_delay else "Off",
        )
        embed.add_field(
            name="Auto-archive After",
            value=_fmt_auto_archive(target.auto_archive_duration),
        )
        embed.add_field(
            name="Messages (API cap ≈50)",
            value=str(target.message_count),
        )
        embed.add_field(
            name="Members (fetched live)",
            value=str(len(members)),
        )
        if target.created_at:
            embed.add_field(
                name="Created",
                value=discord.utils.format_dt(target.created_at, "F"),
                inline=False,
            )
            embed.add_field(
                name="Age",
                value=discord.utils.format_dt(target.created_at, "R"),
            )
        if target.archive_timestamp:
            embed.add_field(
                name="Last Archived / Unarchived",
                value=discord.utils.format_dt(target.archive_timestamp, "R"),
            )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed)

    # ── /thread list ──────────────────────────────────────────────────────────

    @thread_group.command(
        name="list",
        description="Browse all threads in a channel via a select menu (25 per page).",
    )
    @app_commands.describe(
        channel="Channel whose threads to list (defaults to current or parent channel)",
        include_archived="Also include archived threads (fetches up to 100)",
    )
    async def thread_list(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        include_archived: bool = False,
    ) -> None:
        # Resolve a parent text channel even when invoked from inside a thread
        parent: discord.TextChannel
        if channel:
            parent = channel
        elif isinstance(interaction.channel, discord.TextChannel):
            parent = interaction.channel
        elif isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent  # type: ignore[assignment]
        else:
            await interaction.response.send_message(
                embed=_err("Please specify a text channel."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        threads: list[discord.Thread] = list(parent.threads)
        if include_archived:
            seen = {t.id for t in threads}
            async for t in parent.archived_threads(limit=100):
                if t.id not in seen:
                    threads.append(t)
                    seen.add(t.id)

        threads.sort(key=lambda t: t.name.lower())

        if not threads:
            suffix = " (including archived)" if include_archived else ""
            await interaction.followup.send(
                embed=_err(f"No threads found in {parent.mention}{suffix}."),
                ephemeral=True,
            )
            return

        header = discord.Embed(
            title=f"🧵 Threads in #{parent.name}",
            description=(
                f"**{len(threads)}** thread(s) found. "
                "Pick one from the menu below to view its details."
            ),
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(
            embed=header, view=ThreadListView(threads, interaction.user), ephemeral=True
        )

    # ── /thread convert ───────────────────────────────────────────────────────

    @thread_group.command(
        name="convert",
        description="Change the thread type: public ↔ private, or promote to announcement.",
    )
    @app_commands.describe(
        new_type="Target thread type",
        thread="Thread to convert (defaults to current)",
    )
    @app_commands.choices(
        new_type=[
            app_commands.Choice(name="Public Thread", value="public"),
            app_commands.Choice(name="Private Thread", value="private"),
            app_commands.Choice(name="Announcement Thread", value="news"),
        ]
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    @app_commands.checks.bot_has_permissions(manage_threads=True)
    async def thread_convert(
        self,
        interaction: discord.Interaction,
        new_type: str,
        thread: discord.Thread | None = None,
    ) -> None:
        target = self._resolve(thread, interaction)
        if target is None:
            await interaction.response.send_message(
                embed=_err("Specify a thread or run this inside one."), ephemeral=True
            )
            return

        type_map: dict[str, discord.ChannelType] = {
            "public": discord.ChannelType.public_thread,
            "private": discord.ChannelType.private_thread,
            "news": discord.ChannelType.news_thread,
        }
        desired = type_map[new_type]

        if target.type == desired:
            await interaction.response.send_message(
                embed=_err(f"**{target.name}** is already a {_thread_type_label(target)}."),
                ephemeral=True,
            )
            return

        # Announcement threads require the parent to be a news channel
        if desired is discord.ChannelType.news_thread:
            parent = target.parent
            if not isinstance(parent, discord.TextChannel) or not parent.is_news():
                await interaction.response.send_message(
                    embed=_err(
                        "Announcement threads can only exist inside **Announcement channels**.\n"
                        "Convert the parent channel to an Announcement channel first."
                    ),
                    ephemeral=True,
                )
                return

        # Use the low-level HTTP endpoint — discord.py's Thread.edit() doesn't expose `type`
        try:
            await interaction.client.http.edit_channel(
                target.id,
                type=desired.value,
                reason=f"Thread type converted by {interaction.user}",
            )
            label = {
                "public": "Public Thread",
                "private": "Private Thread",
                "news": "Announcement Thread",
            }[new_type]
            await interaction.response.send_message(
                embed=_ok(f"**{target.name}** successfully converted to a **{label}**."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_err(
                    f"Discord rejected the type change: `{exc.text}`\n\n"
                    "Some conversions are restricted by Discord depending on guild "
                    "settings or Nitro boost level (e.g. private threads require Tier 2+)."
                ),
                ephemeral=True,
            )


# ── Extension entry-point ─────────────────────────────────────────────────────


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Threads(bot))