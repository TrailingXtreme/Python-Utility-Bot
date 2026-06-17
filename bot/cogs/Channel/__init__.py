"""Channel cog — advanced channel management (Dyno / ProBot / Arcane / MEE6 style).
────────────────────────────────────────────────────────────────────────────────
Commands
────────
  /channel info          — Rich metadata embed with permission summary.
  /channel slowmode      — Set / clear slowmode (0 – 21 600 s).
  /channel topic         — Set or clear the channel topic.
  /channel rename        — Rename any text or voice channel.
  /channel nsfw          — Toggle NSFW flag.
  /channel hide          — Hide from one or more roles (RoleSelect multi-pick).
  /channel unhide        — Unhide for one or more roles (RoleSelect multi-pick).
  /channel lock          — Lock one or more roles out of sending (RoleSelect).
  /channel unlock        — Lift lockdown for one or more roles (RoleSelect).
  /channel archive       — Read-only mode (lock + reactions off + optional rename).
  /channel clone         — Clone a channel (confirmation required).
  /channel purge         — Bulk-delete up to 1,000 msgs (confirmation required).
  /channel create builder — Interactive creator (text/voice/forum/stage/category).
  /channel delete builder — Interactive deleter (text/voice/category/forum/stage).
  /channel manage        — Ephemeral 13-button interactive dashboard.
  /channel perms         — Multi-role + multi-member permission overwrite editor.
  /channelstats          — Hybrid: stats for current channel.

Interactive components
──────────────────────
  ChannelRenameModal    — Pre-filled single-field modal.
  ChannelTopicModal     — Paragraph TextInput, up to 1 024 chars.
  ChannelSlowmodeModal  — Validated 0 – 21 600 input.
  _PurgeAmountModal     — Amount + optional user/contains → PurgeConfirmView.
  PermOverwriteModal    — 5-permission fine-grained editor (allow/deny/inherit).
  _PermPresetSelect     — Named preset dropdown for ChannelPermsView.
  _CloneConfirmView     — Clone / Cancel confirm (30 s timeout).
  PurgeConfirmView      — Purge / Cancel; executes bulk-delete on confirm.
  ChannelPermsView      — RoleSelect + UserSelect + preset select + action btns.
  ChannelManageView     — 13-button ephemeral dashboard (180 s timeout).
  _ChannelCreateView    — Type select + category/role pickers → creation modal.
  _ChannelDeleteView    — Single ChannelSelect (any type) → _DeleteConfirmView.
  _DeleteConfirmView    — Delete / Cancel confirm (30 s timeout).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import random

import discord
from discord import app_commands
from discord.ext import commands

from util.constants import Colours, Emojis


# ── Constants ─────────────────────────────────────────────────────────────────

PURGE_LIMIT = 1_000

_SKIP = object()  # sentinel: "leave this permission unchanged"

# Status colours pulled from Colours.DISCORD_COLOURS instead of calling
# discord.Colour.green() / .red() / .yellow() / .blurple() / .dark_grey().
_SUCCESS_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["green"], 16))
_ERROR_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["red"], 16))
_WARNING_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["yellow"], 16))
_INFO_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["blurple"], 16))
_ARCHIVE_COLOUR = discord.Colour(int(Colours.DISCORD_COLOURS["dark_grey"], 16))

_PERMISSION_PRESETS: dict[str, dict[str, bool | None]] = {
    "lock":          {"send_messages": False},
    "unlock":        {"send_messages": None},
    "hide":          {"view_channel": False},
    "unhide":        {"view_channel": None},
    "read_only":     {"send_messages": False, "add_reactions": False,
                      "attach_files": False},
    "allow_all":     {"view_channel": True, "send_messages": True,
                      "add_reactions": True, "attach_files": True},
    "mute":          {"send_messages": False, "add_reactions": False},
    "full_restrict": {"view_channel": False, "send_messages": False},
    "mod_access":    {"manage_messages": True, "manage_channels": True},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _colour() -> discord.Colour:
    return discord.Colour(int(random.choice(list(Colours.HTML5_COLOR_CODES.values())), 16))


def _embed(description: str, colour: discord.Colour, *, title: str = "") -> discord.Embed:
    e = discord.Embed(description=description, colour=colour)
    if title:
        e.title = title
    e.timestamp = datetime.now(timezone.utc)
    return e


def _ok(text: str) -> discord.Embed:
    return _embed(f"{Emojis.confirmation}  {text}", _SUCCESS_COLOUR)


def _err(text: str) -> discord.Embed:
    return _embed(f"{Emojis.decline}  {text}", _ERROR_COLOUR)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"


# ── Modals ────────────────────────────────────────────────────────────────────

class ChannelRenameModal(discord.ui.Modal, title="Rename Channel"):
    name: discord.ui.TextInput = discord.ui.TextInput(
        label="New channel name", placeholder="Enter a new name…",
        min_length=1, max_length=100,
    )

    def __init__(self, channel: discord.TextChannel | discord.VoiceChannel) -> None:
        super().__init__()
        self.channel = channel
        self.name.default = channel.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        old = self.channel.name
        await self.channel.edit(name=self.name.value)
        await interaction.response.send_message(
            embed=_ok(f"Renamed **#{old}** → **#{self.name.value}**."), ephemeral=True,
        )


class ChannelTopicModal(discord.ui.Modal, title="Set Channel Topic"):
    topic: discord.ui.TextInput = discord.ui.TextInput(
        label="Topic (leave blank to clear)",
        style=discord.TextStyle.paragraph,
        placeholder="Enter a topic…", required=False, max_length=1024,
    )

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel
        if channel.topic:
            self.topic.default = channel.topic

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.topic.value.strip()
        await self.channel.edit(topic=value or None)
        msg = (f"Topic for {self.channel.mention} updated."
               if value else f"Topic for {self.channel.mention} cleared.")
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)


class ChannelSlowmodeModal(discord.ui.Modal, title="Set Slowmode"):
    seconds: discord.ui.TextInput = discord.ui.TextInput(
        label="Delay in seconds (0 to disable, max 21 600)",
        placeholder="e.g. 5", min_length=1, max_length=5,
    )

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel
        self.seconds.default = str(channel.slowmode_delay)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            val = int(self.seconds.value)
        except ValueError:
            await interaction.response.send_message(
                embed=_err("Please enter a valid integer."), ephemeral=True)
            return
        if not 0 <= val <= 21600:
            await interaction.response.send_message(
                embed=_err("Value must be between 0 and 21 600."), ephemeral=True)
            return
        await self.channel.edit(slowmode_delay=val)
        msg = (f"Slowmode **disabled** in {self.channel.mention}."
               if val == 0
               else f"Slowmode set to **{_fmt_duration(val)}** in {self.channel.mention}.")
        await interaction.response.send_message(embed=_ok(msg), ephemeral=True)


class _PurgeAmountModal(discord.ui.Modal, title="Purge Messages"):
    """Collect purge parameters, then show PurgeConfirmView."""

    amount: discord.ui.TextInput = discord.ui.TextInput(
        label=f"Messages to scan (1 – {PURGE_LIMIT})",
        placeholder="e.g. 100", min_length=1, max_length=4,
    )
    filter_user: discord.ui.TextInput = discord.ui.TextInput(
        label="From user (ID or display name — optional)",
        placeholder="Leave blank for all users", required=False, max_length=100,
    )
    contains: discord.ui.TextInput = discord.ui.TextInput(
        label="Content contains (optional)",
        placeholder="Leave blank for no text filter", required=False, max_length=200,
    )

    def __init__(
        self,
        channel: discord.TextChannel,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.requester = requester

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amt = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message(
                embed=_err("Amount must be a whole number."), ephemeral=True)
            return
        if not 1 <= amt <= PURGE_LIMIT:
            await interaction.response.send_message(
                embed=_err(f"Amount must be between 1 and {PURGE_LIMIT}."),
                ephemeral=True)
            return

        target_user: discord.Member | None = None
        raw_user = self.filter_user.value.strip()
        if raw_user and interaction.guild:
            try:
                uid = int(raw_user)
                target_user = interaction.guild.get_member(uid)
            except ValueError:
                lower = raw_user.lower()
                target_user = discord.utils.find(
                    lambda m: m.name.lower() == lower or m.display_name.lower() == lower,
                    interaction.guild.members,
                )
            if target_user is None:
                await interaction.response.send_message(
                    embed=_err(f"No member found matching `{raw_user}`."),
                    ephemeral=True)
                return

        contains_text = self.contains.value.strip() or None

        def _check(m: discord.Message) -> bool:
            if target_user and m.author != target_user:
                return False
            if contains_text and contains_text.lower() not in m.content.lower():
                return False
            return True

        filters: list[str] = []
        if target_user:
            filters.append(f"from {target_user.mention}")
        if contains_text:
            filters.append(f"containing `{contains_text}`")
        filter_str = " " + " & ".join(filters) if filters else ""

        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Confirm Purge",
            description=(
                f"About to scan **{amt}** message(s){filter_str} "
                f"in {self.channel.mention}.\n\nThis **cannot be undone**. Proceed?"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=PurgeConfirmView(self.channel, amt, _check,
                                  self.requester, filter_str),
            ephemeral=True,
        )


class PermOverwriteModal(discord.ui.Modal, title="Custom Permission Overwrite"):
    """5-flag per-target editor. Accepts: allow/a  deny/d  inherit/i  (blank=skip)."""

    view_channel: discord.ui.TextInput = discord.ui.TextInput(
        label="view_channel  (allow / deny / inherit / blank=skip)",
        placeholder="allow", required=False, max_length=8,
    )
    send_messages: discord.ui.TextInput = discord.ui.TextInput(
        label="send_messages  (allow / deny / inherit / blank=skip)",
        placeholder="allow", required=False, max_length=8,
    )
    add_reactions: discord.ui.TextInput = discord.ui.TextInput(
        label="add_reactions  (allow / deny / inherit / blank=skip)",
        placeholder="allow", required=False, max_length=8,
    )
    attach_files: discord.ui.TextInput = discord.ui.TextInput(
        label="attach_files  (allow / deny / inherit / blank=skip)",
        placeholder="allow", required=False, max_length=8,
    )
    manage_messages: discord.ui.TextInput = discord.ui.TextInput(
        label="manage_messages  (allow / deny / inherit / blank=skip)",
        placeholder="deny", required=False, max_length=8,
    )

    def __init__(
        self,
        channel: discord.TextChannel,
        targets: list[discord.Role | discord.Member],
    ) -> None:
        super().__init__()
        self.channel = channel
        self.targets = targets

    @staticmethod
    def _parse(raw: str) -> bool | None | object:
        v = raw.strip().lower()
        if not v:
            return _SKIP
        if v in ("allow", "a", "true", "yes", "1"):
            return True
        if v in ("deny", "d", "false", "no", "0"):
            return False
        return None  # inherit / i / reset

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mapping: dict[str, object] = {
            "view_channel":    self._parse(self.view_channel.value),
            "send_messages":   self._parse(self.send_messages.value),
            "add_reactions":   self._parse(self.add_reactions.value),
            "attach_files":    self._parse(self.attach_files.value),
            "manage_messages": self._parse(self.manage_messages.value),
        }
        for target in self.targets:
            ow = self.channel.overwrites_for(target)
            for attr, val in mapping.items():
                if val is not _SKIP:
                    setattr(ow, attr, val)
            await self.channel.set_permissions(target, overwrite=ow)
        names = ", ".join(
            (f"@{t.name}" if isinstance(t, discord.Role) else t.display_name)
            for t in self.targets
        )
        await interaction.response.send_message(
            embed=_ok(
                f"Custom permissions applied for **{names}** in {self.channel.mention}."),
            ephemeral=True,
        )

# ── View Components ───────────────────────────────────────────────────────────


class _PermPresetSelect(discord.ui.Select):
    """Dropdown for ChannelPermsView — named presets + Custom… option."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a permission preset…",
            options=[
                discord.SelectOption(
                    label="Lock  (deny send_messages)", value="lock", emoji=Emojis.lock),
                discord.SelectOption(
                    label="Unlock  (inherit send_messages)", value="unlock", emoji=Emojis.unlock),
                discord.SelectOption(
                    label="Hide  (deny view_channel)", value="hide", emoji=Emojis.eye),
                discord.SelectOption(
                    label="Unhide  (inherit view_channel)", value="unhide", emoji=Emojis.eye_speech_bubble),
                discord.SelectOption(
                    label="Read Only  (no send / react / attach)", value="read_only", emoji=Emojis.book),
                discord.SelectOption(
                    label="Allow All  (view + send + react + attach)", value="allow_all", emoji=Emojis.confirmation),
                discord.SelectOption(
                    label="Mute  (no send + no react)", value="mute", emoji=Emojis.mute_channel),
                discord.SelectOption(
                    label="Full Restrict  (hide + lock)", value="full_restrict", emoji=Emojis.no_entry),
                discord.SelectOption(
                    label="Mod Access  (manage_messages + manage_channels)", value="mod_access", emoji=Emojis.shield),
                discord.SelectOption(
                    label="Custom…  (opens 5-field modal)", value="custom", emoji=Emojis.settings),
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ChannelPermsView = self.view  # type: ignore[assignment]
        view.selected_preset = self.values[0]
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _CloneConfirmView(discord.ui.View):
    """Ephemeral Clone / Cancel confirmation."""

    def __init__(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        requester: discord.User | discord.Member,
        *,
        new_name: str,
    ) -> None:
        super().__init__(timeout=30)
        self.channel = channel
        self.requester = requester
        self.new_name = new_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Clone", emoji=Emojis.clipboard, style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        cloned = await self.channel.clone(
            name=self.new_name, reason=f"Cloned by {interaction.user}")
        await interaction.response.edit_message(
            embed=_ok(f"Cloned **#{self.channel.name}** → {cloned.mention}."), view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Clone cancelled."), view=None)

    async def on_timeout(self) -> None:
        self.stop()


class PurgeConfirmView(discord.ui.View):
    """Ephemeral Purge / Cancel — runs bulk-delete only on confirm."""

    def __init__(
        self,
        channel: discord.TextChannel,
        amount: int,
        check: Callable[[discord.Message], bool],
        requester: discord.User | discord.Member,
        filter_str: str,
    ) -> None:
        super().__init__(timeout=30)
        self.channel = channel
        self.amount = amount
        self.check = check
        self.requester = requester
        self.filter_str = filter_str

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Purge", emoji=Emojis.trashcan, style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"{Emojis.hourglass}  Purging messages…", colour=_WARNING_COLOUR),
            view=None,
        )
        deleted = await self.channel.purge(limit=self.amount, check=self.check, bulk=True)
        result = _ok(
            f"Deleted **{len(deleted)}** message(s){self.filter_str} "
            f"in {self.channel.mention}.")
        result.set_footer(text=f"Requested by {self.requester}")
        await interaction.edit_original_response(embed=result)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Purge cancelled."), view=None)

    async def on_timeout(self) -> None:
        self.stop()


class ChannelPermsView(discord.ui.View):
    """Multi-target permission overwrite editor.

    Row 0  RoleSelect  (0 – 10 roles)
    Row 1  UserSelect  (0 – 10 members)
    Row 2  _PermPresetSelect  (named preset or Custom…)
    Row 3  [Apply ✅]  [Clear Overwrites 🗑]  [Close ✖]

    Workflow:
      1. Pick roles / members from the selects.
      2. Pick a preset (or "Custom…" → opens PermOverwriteModal with 5 flags).
      3. Click Apply — preset applied to every selected target.
      "Clear Overwrites" wipes ALL permission entries for selected targets.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.channel = channel
        self.requester = requester
        self.selected_targets: list[discord.Role | discord.Member] = []
        self.selected_preset: str | None = None
        self.add_item(_PermPresetSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        roles = [t for t in self.selected_targets if isinstance(
            t, discord.Role)]
        members = [t for t in self.selected_targets if isinstance(
            t, discord.Member)]
        embed = discord.Embed(
            title=f"{Emojis.shield}  Permission Editor — #{self.channel.name}",
            colour=_INFO_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Roles",
            value=", ".join(f"@{r.name}" for r in roles) or "*None selected*",
            inline=False,
        )
        embed.add_field(
            name="Members",
            value=", ".join(
                m.display_name for m in members) or "*None selected*",
            inline=False,
        )
        embed.add_field(
            name="Preset", value=self.selected_preset or "*None selected*")
        embed.set_footer(text=(
            "1. Pick roles and/or members.  "
            "2. Choose a preset (Custom opens a 5-field modal).  "
            "3. Press Apply."
        ))
        return embed

    # Row 0: role multi-select
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles…",
        min_values=0, max_values=10, row=0,
    )
    async def role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        members = [t for t in self.selected_targets if isinstance(
            t, discord.Member)]
        self.selected_targets = list(select.values) + members
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 1: member multi-select
    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select members…",
        min_values=0, max_values=10, row=1,
    )
    async def member_select(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        roles = [t for t in self.selected_targets if isinstance(
            t, discord.Role)]
        new_members = [
            m for m in select.values if isinstance(m, discord.Member)]
        self.selected_targets = roles + new_members
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 3: action buttons (row 2 is occupied by _PermPresetSelect)
    @discord.ui.button(label="Apply", emoji=Emojis.confirmation, style=discord.ButtonStyle.success, row=3)
    async def apply_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_targets:
            await interaction.response.send_message(
                embed=_err("Select at least one role or member first."), ephemeral=True)
            return
        if not self.selected_preset:
            await interaction.response.send_message(
                embed=_err("Choose a permission preset first."), ephemeral=True)
            return

        if self.selected_preset == "custom":
            await interaction.response.send_modal(
                PermOverwriteModal(self.channel, self.selected_targets))
            return

        preset = _PERMISSION_PRESETS[self.selected_preset]
        for target in self.selected_targets:
            ow = self.channel.overwrites_for(target)
            for perm_name, val in preset.items():
                setattr(ow, perm_name, val)
            await self.channel.set_permissions(target, overwrite=ow)

        names = ", ".join(
            (f"@{t.name}" if isinstance(t, discord.Role) else t.display_name)
            for t in self.selected_targets
        )
        await interaction.response.edit_message(
            embed=_ok(
                f"Applied **{self.selected_preset}** to **{names}** in {self.channel.mention}."),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Clear Overwrites", emoji=Emojis.trashcan, style=discord.ButtonStyle.danger, row=3)
    async def clear_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_targets:
            await interaction.response.send_message(
                embed=_err("Select at least one role or member first."), ephemeral=True)
            return
        for target in self.selected_targets:
            await self.channel.set_permissions(target, overwrite=None)
        names = ", ".join(
            (f"@{t.name}" if isinstance(t, discord.Role) else t.display_name)
            for t in self.selected_targets
        )
        await interaction.response.edit_message(
            embed=_ok(
                f"Cleared all overwrites for **{names}** in {self.channel.mention}."),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Close", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=3)
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Permission editor closed."), view=None)


class ChannelManageView(discord.ui.View):
    """Ephemeral 13-button interactive dashboard for a text channel.

    Row 0  [Lock/Unlock 🔒]  [Hide 👁]  [Unhide 👁‍🗨]  [Archive 📁]  [NSFW 🔞]
    Row 1  [Slowmode ⏱]  [Rename ✏️]  [Topic 📝]  [Clone 📋]  [Purge 🗑]
    Row 2  [Permissions 🛡]  [Refresh 🔄]  [Close ✖]

    • Slowmode / Rename / Topic / Purge  → open modals.
    • Clone  → ephemeral _CloneConfirmView.
    • Permissions  → ephemeral ChannelPermsView.
    • Lock / Hide / Unhide / Archive / NSFW  → edit @everyone overwrite in-place.
    • Purge and Permissions buttons disabled if requester lacks the required perms.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.channel = channel
        self.requester = requester
        if isinstance(requester, discord.Member):
            gp = requester.guild_permissions
            if not gp.manage_messages:
                self.purge_btn.disabled = True
            if not gp.manage_roles:
                self.perms_btn.disabled = True
        self._sync()

    # ── state helpers ─────────────────────────────────────────────────────────

    def _ow(self, interaction: discord.Interaction) -> discord.PermissionOverwrite:
        assert interaction.guild is not None
        return self.channel.overwrites_for(interaction.guild.default_role)

    def _is_locked(self) -> bool:
        assert self.channel.guild
        ow = self.channel.overwrites_for(self.channel.guild.default_role)
        return ow.send_messages is False

    def _is_hidden(self) -> bool:
        assert self.channel.guild
        ow = self.channel.overwrites_for(self.channel.guild.default_role)
        return ow.view_channel is False

    def _sync(self) -> None:
        locked = self._is_locked()
        self.toggle_lock.label = f"Unlock {Emojis.unlock}" if locked else f"Lock {Emojis.lock}"
        self.toggle_lock.style = (
            discord.ButtonStyle.success if locked else discord.ButtonStyle.danger)
        nsfw = self.channel.is_nsfw()
        self.toggle_nsfw.label = f"Disable NSFW {Emojis.confirmation}" if nsfw else f"Enable NSFW {Emojis.nsfw_emoji}"
        self.toggle_nsfw.style = (
            discord.ButtonStyle.secondary if nsfw else discord.ButtonStyle.danger)

    def _fresh(self, interaction: discord.Interaction) -> discord.TextChannel | None:
        if not interaction.guild:
            return None
        ch = interaction.guild.get_channel(self.channel.id)
        return ch if isinstance(ch, discord.TextChannel) else None

    def build_embed(self) -> discord.Embed:
        c = self.channel
        embed = discord.Embed(
            title=f"{Emojis.settings}  Channel Management — #{c.name}",
            colour=_INFO_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="ID", value=f"`{c.id}`")
        embed.add_field(name="Category",
                        value=c.category.name if c.category else "*None*")
        embed.add_field(name="Slowmode",
                        value=_fmt_duration(c.slowmode_delay) if c.slowmode_delay else "Off")
        embed.add_field(name="NSFW", value=f"{Emojis.nsfw_emoji} Yes" if c.is_nsfw() else "No")
        embed.add_field(name="Locked (@everyone)",
                        value=f"{Emojis.lock} Yes" if self._is_locked() else "No")
        embed.add_field(name="Hidden (@everyone)",
                        value=f"{Emojis.eye} Yes" if self._is_hidden() else "No")
        embed.add_field(name="Topic", value=c.topic or "*None*", inline=False)
        embed.set_footer(text=(
            "Slowmode / Rename / Topic / Purge open modals.  •  "
            "Clone & Purge have confirmation windows."))
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    # ── Row 0: quick state toggles ────────────────────────────────────────────

    @discord.ui.button(label="Lock", emoji=Emojis.lock, style=discord.ButtonStyle.danger, row=0)
    async def toggle_lock(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        assert interaction.guild is not None
        ow = self.channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = None if self._is_locked() else False
        await self.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        if fresh := self._fresh(interaction):
            self.channel = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Hide", emoji=Emojis.eye, style=discord.ButtonStyle.secondary, row=0)
    async def hide_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        assert interaction.guild is not None
        ow = self.channel.overwrites_for(interaction.guild.default_role)
        ow.view_channel = False
        await self.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        if fresh := self._fresh(interaction):
            self.channel = fresh
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Unhide", emoji=Emojis.eye_speech_bubble, style=discord.ButtonStyle.success, row=0)
    async def unhide_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        assert interaction.guild is not None
        ow = self.channel.overwrites_for(interaction.guild.default_role)
        ow.view_channel = None
        await self.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        if fresh := self._fresh(interaction):
            self.channel = fresh
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Archive", emoji=Emojis.folder, style=discord.ButtonStyle.secondary, row=0)
    async def archive_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        assert interaction.guild is not None
        ow = self.channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        ow.add_reactions = False
        await self.channel.set_permissions(
            interaction.guild.default_role, overwrite=ow, reason="Archived via manage panel")
        if fresh := self._fresh(interaction):
            self.channel = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Enable NSFW", emoji=Emojis.nsfw_emoji, style=discord.ButtonStyle.danger, row=0)
    async def toggle_nsfw(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.channel.edit(nsfw=not self.channel.is_nsfw())
        if fresh := self._fresh(interaction):
            self.channel = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # ── Row 1: modal and confirm actions ─────────────────────────────────────

    @discord.ui.button(label="Slowmode", emoji=Emojis.timer, style=discord.ButtonStyle.secondary, row=1)
    async def slowmode_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ChannelSlowmodeModal(self.channel))

    @discord.ui.button(label="Rename", emoji=Emojis.pencil, style=discord.ButtonStyle.secondary, row=1)
    async def rename_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ChannelRenameModal(self.channel))

    @discord.ui.button(label="Topic", emoji=Emojis.memo, style=discord.ButtonStyle.secondary, row=1)
    async def topic_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ChannelTopicModal(self.channel))

    @discord.ui.button(label="Clone", emoji=Emojis.clipboard, style=discord.ButtonStyle.secondary, row=1)
    async def clone_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        new_name = f"copy-of-{self.channel.name}"
        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Clone Channel?",
            description=(
                f"Create **#{new_name}** as a copy of {self.channel.mention}?\n"
                "Copies name, topic, slowmode, NSFW flag, category, and overwrites."
            ),
            colour=_WARNING_COLOUR,
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=_CloneConfirmView(
                self.channel, interaction.user, new_name=new_name),
            ephemeral=True,
        )

    @discord.ui.button(label="Purge", emoji=Emojis.trashcan, style=discord.ButtonStyle.danger, row=1)
    async def purge_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            _PurgeAmountModal(self.channel, interaction.user))

    # ── Row 2: utility ────────────────────────────────────────────────────────

    @discord.ui.button(label="Permissions", emoji=Emojis.shield, style=discord.ButtonStyle.primary, row=2)
    async def perms_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = ChannelPermsView(self.channel, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Refresh", emoji=Emojis.refresh, style=discord.ButtonStyle.secondary, row=2)
    async def refresh_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if fresh := self._fresh(interaction):
            self.channel = fresh
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=2)
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Management panel closed."), view=None)


class _HideUnhideView(discord.ui.View):
    """Ephemeral Hide/Unhide role selector.

    Row 0  RoleSelect  (0 – 10 roles)
    Row 1  [Apply ✅]  [Cancel ✖]

    Workflow:
      1. Pick one or more roles to hide/unhide.
      2. Click Apply — permission applied to every selected role.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        requester: discord.User | discord.Member,
        action: str,
    ) -> None:
        super().__init__(timeout=180)
        self.channel = channel
        self.requester = requester
        self.action = action  # "hide" or "unhide"
        self.selected_roles: list[discord.Role] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        action_label = "Hidden From" if self.action == "hide" else "Unhidden For"
        embed = discord.Embed(
            title=f"{Emojis.shield}  {self.action.title()} Channel — #{self.channel.name}",
            colour=_INFO_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Roles",
            value=", ".join(
                f"@{r.name}" for r in self.selected_roles) or "*None selected*",
            inline=False,
        )
        embed.set_footer(text="1. Pick one or more roles.  2. Press Apply.")
        return embed

    # Row 0: role multi-select
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles…",
        min_values=0, max_values=10, row=0,
    )
    async def role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.selected_roles = list(select.values)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 1: action buttons
    @discord.ui.button(label="Apply", emoji=Emojis.confirmation, style=discord.ButtonStyle.success, row=1)
    async def apply_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_roles:
            await interaction.response.send_message(
                embed=_err("Select at least one role first."), ephemeral=True)
            return

        # Apply hide/unhide to each selected role
        for role in self.selected_roles:
            ow = self.channel.overwrites_for(role)
            if self.action == "hide":
                ow.view_channel = False
            else:  # unhide
                ow.view_channel = None
            await self.channel.set_permissions(role, overwrite=ow)

        names = ", ".join(f"@{r.name}" for r in self.selected_roles)
        action_past = "hidden from" if self.action == "hide" else "unhidden for"
        await interaction.response.edit_message(
            embed=_ok(
                f"Channel **{action_past}** **{names}** in {self.channel.mention}."),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Cancelled."), view=None)


class _LockUnlockView(discord.ui.View):
    """Ephemeral Lock/Unlock role selector.

    Row 0  RoleSelect  (0 – 10 roles)
    Row 1  [Apply ✅]  [Cancel ✖]

    Workflow:
      1. Pick one or more roles to lock/unlock.
      2. Click Apply — permission applied to every selected role.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        requester: discord.User | discord.Member,
        action: str,
        reason: str | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.channel = channel
        self.requester = requester
        self.action = action  # "lock" or "unlock"
        self.reason = reason
        self.selected_roles: list[discord.Role] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        action_label = "Locked For" if self.action == "lock" else "Unlocked For"
        embed = discord.Embed(
            title=f"{Emojis.shield}  {self.action.title()} Channel — #{self.channel.name}",
            colour=_INFO_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Roles",
            value=", ".join(
                f"@{r.name}" for r in self.selected_roles) or "*None selected*",
            inline=False,
        )
        if self.reason:
            embed.add_field(name="Reason", value=self.reason, inline=False)
        embed.set_footer(text="1. Pick one or more roles.  2. Press Apply.")
        return embed

    # Row 0: role multi-select
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles…",
        min_values=0, max_values=10, row=0,
    )
    async def role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.selected_roles = list(select.values)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 1: action buttons
    @discord.ui.button(label="Apply", emoji=Emojis.confirmation, style=discord.ButtonStyle.success, row=1)
    async def apply_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_roles:
            await interaction.response.send_message(
                embed=_err("Select at least one role first."), ephemeral=True)
            return

        # Apply lock/unlock to each selected role
        for role in self.selected_roles:
            ow = self.channel.overwrites_for(role)
            if self.action == "lock":
                ow.send_messages = False
            else:  # unlock
                ow.send_messages = None
            await self.channel.set_permissions(role, overwrite=ow, reason=self.reason)

        names = ", ".join(f"@{r.name}" for r in self.selected_roles)
        action_past = "locked for" if self.action == "lock" else "unlocked for"
        await interaction.response.edit_message(
            embed=_ok(
                f"Channel **{action_past}** **{names}** in {self.channel.mention}."),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Cancelled."), view=None)


# ── Channel Creation Modals ───────────────────────────────────────────────────


class _TextChannelCreateModal(discord.ui.Modal, title="Create Text Channel"):
    """Modal to create a text channel with optional parameters."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name", placeholder="e.g. general", min_length=1, max_length=100,
    )
    topic: discord.ui.TextInput = discord.ui.TextInput(
        label="Topic (optional)", placeholder="Leave blank for no topic",
        style=discord.TextStyle.short, required=False, max_length=1024,
    )
    slowmode: discord.ui.TextInput = discord.ui.TextInput(
        label="Slowmode in seconds (0 = off, max 21600)",
        placeholder="e.g. 5", required=False, max_length=5,
    )
    nsfw: discord.ui.TextInput = discord.ui.TextInput(
        label="NSFW? (yes/no)", placeholder="no",
        required=False, max_length=3,
    )

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        category: discord.CategoryChannel | None = None,
        roles: list[discord.Role] | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.requester = requester
        self.category = category
        self.roles = roles or []

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse inputs
        channel_name = self.name.value.strip()
        topic_text = self.topic.value.strip() or None
        slowmode_input = self.slowmode.value.strip() or "0"
        nsfw_input = self.nsfw.value.strip().lower() or "no"

        # Validate slowmode
        try:
            slowmode_delay = int(slowmode_input)
            if not 0 <= slowmode_delay <= 21600:
                await interaction.response.send_message(
                    embed=_err("Slowmode must be between 0 and 21600."), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(
                embed=_err("Slowmode must be a valid integer."), ephemeral=True)
            return

        # Parse NSFW
        is_nsfw = nsfw_input in ("yes", "y", "true", "1")

        # Create channel
        try:
            overwrites: dict = {}
            if self.roles:
                overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False)
                overwrites[self.guild.me] = discord.PermissionOverwrite(
                    view_channel=True)
                for role in self.roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True)

            new_channel = await self.guild.create_text_channel(
                name=channel_name, overwrites=overwrites, category=self.category,
                topic=topic_text, slowmode_delay=slowmode_delay, nsfw=is_nsfw,
            )
            msg = f"Created {new_channel.mention}"
            if self.roles:
                role_names = ", ".join(r.mention for r in self.roles)
                msg += f", visible to {role_names}"
            msg += "."
            await interaction.response.send_message(embed=_ok(msg), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err("I don't have permission to create channels."), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_err(f"Failed to create channel: {e}"), ephemeral=True)


class _VoiceChannelCreateModal(discord.ui.Modal, title="Create Voice Channel"):
    """Modal to create a voice channel with optional parameters."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name", placeholder="e.g. General", min_length=1, max_length=100,
    )
    bitrate: discord.ui.TextInput = discord.ui.TextInput(
        label="Bitrate in kbps (8 – 384, default 64)",
        placeholder="64", required=False, max_length=3,
    )
    user_limit: discord.ui.TextInput = discord.ui.TextInput(
        label="User limit (0 = unlimited)",
        placeholder="0", required=False, max_length=2,
    )

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        category: discord.CategoryChannel | None = None,
        roles: list[discord.Role] | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.requester = requester
        self.category = category
        self.roles = roles or []

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse inputs
        channel_name = self.name.value.strip()
        bitrate_input = self.bitrate.value.strip() or "64"
        user_limit_input = self.user_limit.value.strip() or "0"

        # Validate bitrate
        try:
            bitrate = int(bitrate_input)
            if not 8 <= bitrate <= 384:
                await interaction.response.send_message(
                    embed=_err("Bitrate must be between 8 and 384 kbps."), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(
                embed=_err("Bitrate must be a valid integer."), ephemeral=True)
            return

        # Validate user limit
        try:
            user_limit = int(user_limit_input)
            if not 0 <= user_limit <= 99:
                await interaction.response.send_message(
                    embed=_err("User limit must be between 0 and 99."), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(
                embed=_err("User limit must be a valid integer."), ephemeral=True)
            return

        # Create channel
        try:
            overwrites: dict = {}
            if self.roles:
                overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False)
                overwrites[self.guild.me] = discord.PermissionOverwrite(
                    view_channel=True)
                for role in self.roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True)

            vc = await self.guild.create_voice_channel(
                name=channel_name, overwrites=overwrites, category=self.category,
                bitrate=bitrate * 1000, user_limit=user_limit,
            )
            msg = f"Created voice channel {vc.mention}"
            if self.roles:
                role_names = ", ".join(r.mention for r in self.roles)
                msg += f", visible to {role_names}"
            msg += "."
            await interaction.response.send_message(embed=_ok(msg), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err("I don't have permission to create channels."), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_err(f"Failed to create channel: {e}"), ephemeral=True)


class _CategoryCreateModal(discord.ui.Modal, title="Create Category"):
    """Modal to create a category with optional role restriction."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Category name", placeholder="e.g. General", min_length=1, max_length=100,
    )

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        roles: list[discord.Role] | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.requester = requester
        self.roles = roles or []

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse input
        category_name = self.name.value.strip()

        # Create category
        try:
            overwrites: dict = {}
            if self.roles:
                overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False)
                overwrites[self.guild.me] = discord.PermissionOverwrite(
                    view_channel=True)
                for role in self.roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True)

            category = await self.guild.create_category(name=category_name, overwrites=overwrites)
            msg = f"Created category {category.mention}"
            if self.roles:
                role_names = ", ".join(r.mention for r in self.roles)
                msg += f", visible only to {role_names}"
            msg += "."
            await interaction.response.send_message(embed=_ok(msg), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err("I don't have permission to create categories."), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_err(f"Failed to create category: {e}"), ephemeral=True)


class _ForumChannelCreateModal(discord.ui.Modal, title="Create Forum Channel"):
    """Modal to create a forum channel with optional parameters."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name", placeholder="e.g. general-discussion", min_length=1, max_length=100,
    )
    topic: discord.ui.TextInput = discord.ui.TextInput(
        label="Topic (optional)", placeholder="Leave blank for no topic",
        style=discord.TextStyle.short, required=False, max_length=1024,
    )
    nsfw: discord.ui.TextInput = discord.ui.TextInput(
        label="NSFW? (yes/no)", placeholder="no",
        required=False, max_length=3,
    )

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        category: discord.CategoryChannel | None = None,
        roles: list[discord.Role] | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.requester = requester
        self.category = category
        self.roles = roles or []

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse inputs
        channel_name = self.name.value.strip()
        topic_text = self.topic.value.strip() or None
        nsfw_input = self.nsfw.value.strip().lower() or "no"
        nsfw_value = nsfw_input in ("yes", "y", "true", "1")

        # Create forum channel
        try:
            overwrites: dict = {}
            if self.roles:
                overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False)
                overwrites[self.guild.me] = discord.PermissionOverwrite(
                    view_channel=True)
                for role in self.roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True)

            forum = await self.guild.create_forum(
                name=channel_name, overwrites=overwrites, category=self.category,
                topic=topic_text, nsfw=nsfw_value
            )
            msg = f"Created forum {forum.mention}"
            if self.roles:
                role_names = ", ".join(r.mention for r in self.roles)
                msg += f", visible to {role_names}"
            msg += "."
            await interaction.response.send_message(embed=_ok(msg), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err("I don't have permission to create channels."), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_err(f"Failed to create forum: {e}"), ephemeral=True)


class _StageChannelCreateModal(discord.ui.Modal, title="Create Stage Channel"):
    """Modal to create a stage channel."""

    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name", placeholder="e.g. Stage", min_length=1, max_length=100,
    )

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        category: discord.CategoryChannel | None = None,
        roles: list[discord.Role] | None = None,
    ) -> None:
        super().__init__()
        self.guild = guild
        self.requester = requester
        self.category = category
        self.roles = roles or []

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse inputs
        channel_name = self.name.value.strip()

        # Create stage channel
        try:
            overwrites: dict = {}
            if self.roles:
                overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False)
                overwrites[self.guild.me] = discord.PermissionOverwrite(
                    view_channel=True)
                for role in self.roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True)

            stage = await self.guild.create_stage_channel(
                name=channel_name, overwrites=overwrites, category=self.category,
            )
            msg = f"Created stage channel {stage.mention}"
            if self.roles:
                role_names = ", ".join(r.mention for r in self.roles)
                msg += f", visible to {role_names}"
            msg += "."
            await interaction.response.send_message(embed=_ok(msg), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_err("I don't have permission to create channels."), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_err(f"Failed to create stage channel: {e}"), ephemeral=True)


# ── Channel Creation Views ────────────────────────────────────────────────────


class _ChannelCreateTypeSelect(discord.ui.Select):
    """Dropdown for selecting channel type in _ChannelCreateView."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Select channel type…",
            options=[
                discord.SelectOption(
                    label="Text Channel", value="text", emoji=Emojis.text_channel, description="Create a text channel"),
                discord.SelectOption(
                    label="Voice Channel", value="voice", emoji=Emojis.voice_channel, description="Create a voice channel"),
                discord.SelectOption(
                    label="Forum Channel", value="forum", emoji=Emojis.forum_channel, description="Create a forum channel"),
                discord.SelectOption(
                    label="Stage Channel", value="stage", emoji=Emojis.stage_channel, description="Create a stage channel"),
                discord.SelectOption(
                    label="Category", value="category", emoji=Emojis.folder, description="Create a category"),
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _ChannelCreateView = self.view  # type: ignore[assignment]
        view.selected_type = self.values[0]
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _ChannelCreateView(discord.ui.View):
    """Interactive channel creation view.

    Row 0  _ChannelCreateTypeSelect  (text / voice / forum / stage / category)
    Row 1  [Select Category]
    Row 2  [Select Role]
    Row 3  [Create ✅]  [Cancel ✖]

    Workflow:
      1. Choose channel type from dropdown.
      2. Optionally select a category and role.
      3. Click Create to open type-specific modal.
    """

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        self.requester = requester
        self.selected_type: str | None = None
        self.selected_category: discord.CategoryChannel | None = None
        self.selected_roles: list[discord.Role] = []
        self.add_item(_ChannelCreateTypeSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{Emojis.tv} Channel Creator",
            colour=_INFO_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Type",
            value=self.selected_type.title() if self.selected_type else "*None selected*",
        )
        embed.add_field(
            name="Category",
            value=self.selected_category.name if self.selected_category else "*None selected*",
        )
        embed.add_field(
            name="Restrict to Roles",
            value=", ".join(
                r.mention for r in self.selected_roles) if self.selected_roles else "*Public*",
        )
        embed.set_footer(
            text="1. Choose a type.  2. Optionally set category & roles.  3. Click Create.")
        return embed

    # Row 1: category selection
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select category (optional)…",
        min_values=0, max_values=1, channel_types=[discord.ChannelType.category], row=1,
    )
    async def category_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.selected_category = select.values[0] if select.values else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 2: role selection (multiple)
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select role restrictions (optional, up to 10)…",
        min_values=0, max_values=10, row=2,
    )
    async def role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.selected_roles = list(select.values)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 3: action buttons
    @discord.ui.button(label="Create", emoji=Emojis.confirmation, style=discord.ButtonStyle.success, row=3)
    async def create_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_type:
            await interaction.response.send_message(
                embed=_err("Select a channel type first."), ephemeral=True)
            return

        modal: discord.ui.Modal
        if self.selected_type == "text":
            modal = _TextChannelCreateModal(
                self.guild, interaction.user,
                category=self.selected_category, roles=self.selected_roles,
            )
        elif self.selected_type == "voice":
            modal = _VoiceChannelCreateModal(
                self.guild, interaction.user,
                category=self.selected_category, roles=self.selected_roles,
            )
        elif self.selected_type == "forum":
            modal = _ForumChannelCreateModal(
                self.guild, interaction.user,
                category=self.selected_category, roles=self.selected_roles,
            )
        elif self.selected_type == "stage":
            modal = _StageChannelCreateModal(
                self.guild, interaction.user,
                category=self.selected_category, roles=self.selected_roles,
            )
        elif self.selected_type == "category":
            if self.selected_category:
                await interaction.response.send_message(
                    embed=_err(
                        "Cannot create category inside another category."),
                    ephemeral=True)
                return
            modal = _CategoryCreateModal(
                self.guild, interaction.user, roles=self.selected_roles,
            )
        else:
            return

        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=_err(
                        "Couldn't open the creation form. Please try again "
                        "or report this to the bot owner."),
                    ephemeral=True)
            return
        
        self.stop()

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=3)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Cancelled."), view=None)


# ── Channel Deletion Views ────────────────────────────────────────────────────

# Every channel/category type the delete builder is willing to touch.
_DELETABLE_TYPES: list[discord.ChannelType] = [
    discord.ChannelType.text,
    discord.ChannelType.voice,
    discord.ChannelType.category,
    discord.ChannelType.news,
    discord.ChannelType.forum,
    discord.ChannelType.stage_voice,
]


class _DeleteConfirmView(discord.ui.View):
    """Ephemeral Delete / Cancel — runs the actual delete only on confirm."""

    def __init__(
        self,
        channel: discord.abc.GuildChannel,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=30)
        self.channel = channel
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This confirmation is not for you."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Delete", emoji=Emojis.trashcan, style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        name = self.channel.name
        is_category = isinstance(self.channel, discord.CategoryChannel)
        held = len(self.channel.channels) if is_category else None
        try:
            await self.channel.delete(
                reason=f"Deleted via channel delete builder by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.edit_message(
                embed=_err("I don't have permission to delete that channel."), view=None)
            return
        except discord.HTTPException as e:
            await interaction.response.edit_message(
                embed=_err(f"Failed to delete **#{name}**: {e}"), view=None)
            return
        msg = f"Deleted **#{name}**"
        if held is not None:
            msg += f" ({held} channel(s) removed with it)"
        msg += "."
        await interaction.response.edit_message(embed=_ok(msg), view=None)

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Delete cancelled."), view=None)

    async def on_timeout(self) -> None:
        self.stop()


class _ChannelDeleteView(discord.ui.View):
    """Interactive channel/category deletion builder — one panel instead of
    separate /channel delete text|voice|category|forum|stage commands.

    Row 0  ChannelSelect  (text / voice / category / announcement / forum / stage)
    Row 1  [Delete 🗑]  [Cancel ✖]

    Workflow:
      1. Pick the channel or category to remove from the select menu.
      2. Click Delete — a confirmation prompt appears before anything is removed.
    """

    def __init__(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        self.requester = requester
        self.selected_channel: discord.abc.GuildChannel | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                embed=_err("This panel is not for you."), ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{Emojis.trashcan} Channel Deleter",
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        if self.selected_channel:
            kind = str(self.selected_channel.type).replace("_", " ").title()
            value = f"**#{self.selected_channel.name}**  ({kind})"
            if isinstance(self.selected_channel, discord.CategoryChannel):
                value += f"\n{len(self.selected_channel.channels)} channel(s) live inside it."
        else:
            value = "*None selected*"
        embed.add_field(name="Target", value=value, inline=False)
        embed.set_footer(
            text="1. Pick a channel/category.  2. Click Delete to confirm.")
        return embed

    # Row 0: channel/category picker (any deletable type)
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel or category to delete…",
        min_values=1, max_values=1, channel_types=_DELETABLE_TYPES, row=0,
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.selected_channel = select.values[0] if select.values else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # Row 1: action buttons
    @discord.ui.button(label="Delete", emoji=Emojis.trashcan, style=discord.ButtonStyle.danger, row=1)
    async def delete_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_channel:
            await interaction.response.send_message(
                embed=_err("Select a channel or category first."), ephemeral=True)
            return

        target = self.selected_channel
        extra = ""
        if isinstance(target, discord.CategoryChannel):
            extra = (f"\n**{len(target.channels)}** channel(s) inside it "
                     "will be deleted too.")
        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Confirm Delete",
            description=(
                f"Delete **#{target.name}**?{extra}\n\n"
                "This **cannot be undone**. Proceed?"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=_DeleteConfirmView(target, interaction.user),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", emoji=Emojis.close, style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(embed=_ok("Cancelled."), view=None)


# ── Cog ──────────────────────────────────────────────────────────────────────


class Channels(
    commands.Cog,
    description="Advanced channel management — like Dyno, ProBot, Arcane & MEE6.",
):
    COG_EMOJI = Emojis.tv

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    channel_group = app_commands.Group(
        name="channel", description="Channel management commands.")
    create_group = app_commands.Group(
        name="create", description="Create categories, text, or voice channels.",
        parent=channel_group)
    delete_group = app_commands.Group(
        name="delete", description="Delete a channel or category via the interactive builder.",
        parent=channel_group)

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
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(embed=_err(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=_err(msg), ephemeral=True)

    # ── /channelstats ─────────────────────────────────────────────────────────

    @commands.hybrid_command(name="channelstats", aliases=["cs"],
                             description="Show stats for the current channel.")
    async def channelstats(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        channel = ctx.channel
        embed = discord.Embed(title=f"#{channel.name}", colour=_colour(),
                              timestamp=datetime.now(timezone.utc))
        embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon)
        embed.add_field(name="ID", value=f"`{channel.id}`")
        embed.add_field(name="Guild", value=ctx.guild.name)
        embed.add_field(
            name="Category",
            value=(channel.category.name
                   if isinstance(channel, discord.abc.GuildChannel) and channel.category
                   else "*None*"),
        )
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="Topic", value=channel.topic or "*None*")
            embed.add_field(
                name="Slowmode",
                value=_fmt_duration(
                    channel.slowmode_delay) if channel.slowmode_delay else "Off",
            )
            embed.add_field(
                name="NSFW", value="Yes" if channel.is_nsfw() else "No")
            embed.add_field(name="Announcement",
                            value="Yes" if channel.is_news() else "No")
            embed.add_field(name="Threads", value=str(len(channel.threads)))
        embed.add_field(name="Position", value=str(channel.position))
        embed.add_field(name="Created", value=discord.utils.format_dt(
            channel.created_at, "R"))
        embed.set_footer(text=f"Requested by {ctx.author}")
        await ctx.send(embed=embed)

    # ── /channel info ─────────────────────────────────────────────────────────

    @channel_group.command(name="info", description="Detailed info embed for any channel.")
    @app_commands.describe(channel="Channel to inspect (defaults to current)")
    async def channel_info(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        embed = discord.Embed(
            title=f"Channel Info — #{target.name}", colour=_colour(),
            timestamp=datetime.now(timezone.utc))
        embed.set_author(name=interaction.guild.name,
                         icon_url=interaction.guild.icon)
        embed.add_field(name="ID", value=f"`{target.id}`")
        embed.add_field(name="Type", value=str(
            target.type).replace("_", " ").title())
        embed.add_field(name="Position", value=str(target.position))
        embed.add_field(
            name="Category",
            value=(target.category.name
                   if isinstance(target, discord.abc.GuildChannel) and target.category
                   else "*None*"),
        )
        embed.add_field(
            name="Created", value=discord.utils.format_dt(target.created_at, "F"), inline=False)
        embed.add_field(name="Age", value=discord.utils.format_dt(
            target.created_at, "R"))
        if isinstance(target, discord.TextChannel):
            embed.add_field(
                name="Topic", value=target.topic or "*None*", inline=False)
            embed.add_field(
                name="Slowmode",
                value=_fmt_duration(target.slowmode_delay) if target.slowmode_delay else "Off")
            embed.add_field(
                name="NSFW", value="Yes" if target.is_nsfw() else "No")
            embed.add_field(name="Announcement",
                            value="Yes" if target.is_news() else "No")
            embed.add_field(name="Active Threads",
                            value=str(len(target.threads)))
            locked_for = [r.name for r, ow in target.overwrites.items()
                          if isinstance(r, discord.Role) and ow.send_messages is False]
            hidden_from = [r.name for r, ow in target.overwrites.items()
                           if isinstance(r, discord.Role) and ow.view_channel is False]
            embed.add_field(name=f"{Emojis.lock} Locked For",
                            value=", ".join(locked_for) or "*Nobody*", inline=False)
            embed.add_field(name=f"{Emojis.eye_speech_bubble} Hidden From",
                            value=", ".join(hidden_from) or "*Nobody*")
        elif isinstance(target, discord.VoiceChannel):
            embed.add_field(
                name="Bitrate", value=f"{target.bitrate // 1000} kbps")
            embed.add_field(
                name="User Limit",
                value=str(target.user_limit) if target.user_limit else "Unlimited")
            embed.add_field(name="Currently Connected",
                            value=str(len(target.members)))
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    # ── /channel slowmode ─────────────────────────────────────────────────────

    @channel_group.command(name="slowmode",
                           description="Set slowmode on a channel. 0 = disabled, max 21 600 s.")
    @app_commands.describe(seconds="Delay in seconds (0 to disable)",
                           channel="Channel to apply slowmode to (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self, interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        await target.edit(slowmode_delay=seconds)
        embed = (_ok(f"Slowmode **disabled** in {target.mention}.")
                 if seconds == 0
                 else _ok(f"Slowmode set to **{_fmt_duration(seconds)}** in {target.mention}."))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /channel topic ────────────────────────────────────────────────────────

    @channel_group.command(name="topic", description="Set or clear the channel topic.")
    @app_commands.describe(topic="New topic text (leave blank to clear)",
                           channel="Channel to update (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def topic(
        self, interaction: discord.Interaction,
        topic: str = "",
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        await target.edit(topic=topic or None)
        embed = (_ok(f"Topic for {target.mention} updated.")
                 if topic else _ok(f"Topic for {target.mention} cleared."))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /channel rename ───────────────────────────────────────────────────────

    @channel_group.command(name="rename", description="Rename a text or voice channel.")
    @app_commands.describe(name="New channel name",
                           channel="Channel to rename (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def rename(
        self, interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, (discord.TextChannel, discord.VoiceChannel))
        old_name = target.name
        await target.edit(name=name)
        await interaction.response.send_message(
            embed=_ok(f"Renamed **#{old_name}** → **#{name}**."), ephemeral=True)

    # ── /channel nsfw ─────────────────────────────────────────────────────────

    @channel_group.command(name="nsfw", description="Toggle the NSFW flag on a text channel.")
    @app_commands.describe(channel="Channel to toggle (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def nsfw(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        new_state = not target.is_nsfw()
        await target.edit(nsfw=new_state)
        label = f"{Emojis.nsfw_emoji} **enabled**" if new_state else f"{Emojis.confirmation} **disabled**"
        await interaction.response.send_message(
            embed=_ok(f"NSFW {label} for {target.mention}."), ephemeral=True)

    # ── /channel hide ─────────────────────────────────────────────────────────

    @channel_group.command(name="hide",
                           description="Hide a channel from one or more roles via a select menu.")
    @app_commands.describe(channel="Channel to hide (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def hide(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = _HideUnhideView(target, interaction.user, action="hide")
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel unhide ───────────────────────────────────────────────────────

    @channel_group.command(name="unhide",
                           description="Reveal a channel to one or more roles via a select menu.")
    @app_commands.describe(channel="Channel to unhide (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unhide(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = _HideUnhideView(target, interaction.user, action="unhide")
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel lock ─────────────────────────────────────────────────────────

    @channel_group.command(name="lock",
                           description="Lock one or more roles out of sending messages.")
    @app_commands.describe(
        channel="Channel to lock (defaults to current)",
        reason="Reason shown in audit log and channel notice",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = _LockUnlockView(target, interaction.user,
                               action="lock", reason=reason)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel unlock ───────────────────────────────────────────────────────

    @channel_group.command(name="unlock",
                           description="Lift the send-message lock for one or more roles.")
    @app_commands.describe(
        channel="Channel to unlock (defaults to current)",
        reason="Reason shown in audit log",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = _LockUnlockView(target, interaction.user,
                               action="unlock", reason=reason)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel archive ──────────────────────────────────────────────────────

    @channel_group.command(name="archive",
                           description="Archive a channel: lock for everyone, kill reactions.")
    @app_commands.describe(
        channel="Channel to archive (defaults to current)",
        prefix="Prefix to prepend to the name (e.g. 'archived')",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def archive(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        prefix: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        overwrite = target.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        overwrite.add_reactions = False
        await target.set_permissions(
            interaction.guild.default_role, overwrite=overwrite, reason="Channel archived")
        if prefix:
            await target.edit(name=f"{prefix}-{target.name}")
        notice = discord.Embed(
            title=f"{Emojis.folder} Channel Archived",
            description=(f"This channel has been archived by {interaction.user.mention} "
                         "and is now **read-only**."),
            colour=_ARCHIVE_COLOUR, timestamp=datetime.now(timezone.utc),
        )
        await target.send(embed=notice)
        await interaction.response.send_message(
            embed=_ok(f"{Emojis.folder} {target.mention} has been archived."), ephemeral=True)

    # ── /channel clone ────────────────────────────────────────────────────────

    @channel_group.command(name="clone",
                           description="Clone a channel — shows a confirmation first.")
    @app_commands.describe(
        name="Name for the clone (default: copy-of-<original>)",
        channel="Channel to clone (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def clone(
        self, interaction: discord.Interaction,
        name: str | None = None,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, (discord.TextChannel, discord.VoiceChannel))
        new_name = name or f"copy-of-{target.name}"
        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Clone Channel?",
            description=(
                f"Create **#{new_name}** as a copy of {target.mention}?\n"
                "Copies name, topic, slowmode, NSFW flag, category, and permission overwrites."
            ),
            colour=_WARNING_COLOUR,
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=_CloneConfirmView(
                target, interaction.user, new_name=new_name),
            ephemeral=True,
        )

    # ── /channel purge ────────────────────────────────────────────────────────

    @channel_group.command(
        name="purge",
        description=f"Bulk-delete up to {PURGE_LIMIT} messages — shows confirmation first.",
    )
    @app_commands.describe(
        amount=f"Number of messages to scan (1 – {PURGE_LIMIT})",
        user="Only delete messages from this member",
        bots_only="Only delete messages sent by bots",
        contains="Only delete messages whose content contains this text",
        attachments_only="Only delete messages that have attachments",
        embeds_only="Only delete messages that have embeds",
        channel="Channel to purge (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        user: discord.Member | None = None,
        bots_only: bool = False,
        contains: str | None = None,
        attachments_only: bool = False,
        embeds_only: bool = False,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)

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

        confirm_embed = discord.Embed(
            title=f"{Emojis.warning}  Confirm Purge",
            description=(
                f"About to scan **{amount}** message(s){filter_str} "
                f"in {target.mention}.\n\nThis **cannot be undone**. Proceed?"
            ),
            colour=_WARNING_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=PurgeConfirmView(target, amount, _check,
                                  interaction.user, filter_str),
            ephemeral=True,
        )

    # ── /channel manage ───────────────────────────────────────────────────────

    @channel_group.command(name="manage",
                           description="Open the interactive channel management panel.")
    @app_commands.describe(channel="Channel to manage (defaults to current)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def manage(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = ChannelManageView(target, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel perms ────────────────────────────────────────────────────────

    @channel_group.command(name="perms",
                           description="Multi-role + multi-member permission overwrite editor.")
    @app_commands.describe(channel="Channel to edit permissions for (defaults to current)")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def perms(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)
        view = ChannelPermsView(target, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /channel create (interactive) ─────────────────────────────────────────

    @create_group.command(name="builder",
                          description="Interactive channel builder (select type, category, role).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def create_builder(self, interaction: discord.Interaction) -> None:
        """Open interactive channel creation interface."""
        assert interaction.guild is not None
        view = _ChannelCreateView(interaction.guild, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)


    # ── /channel delete builder ───────────────────────────────────────────────

    @delete_group.command(name="builder",
                          description="Interactive deleter — pick any text, voice, category, forum or stage channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def delete_builder(self, interaction: discord.Interaction) -> None:
        """Open interactive channel deletion interface."""
        assert interaction.guild is not None
        view = _ChannelDeleteView(interaction.guild, interaction.user)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Channels(bot))