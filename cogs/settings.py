"""
Unified Settings Dashboard
Centralized configuration system for ALL bot features
Follows the same rich-panel pattern as AutoMod dashboard
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List, Literal, Any
import datetime
from datetime import timezone, timedelta

from utils.embeds import ModEmbed, Colors
from utils.checks import is_admin
from utils.server_setup import apply_compact_log_routing, apply_verification_gate, sync_setup_aliases, sync_staff_role_groups
from config import Config

# ==============================================================================
# HELPERS
# ==============================================================================

def _r(guild, settings, key):
    """Role mention or Not Set"""
    rid = settings.get(key)
    r = guild.get_role(rid) if rid else None
    return r.mention if r else "`Not Set`"

def _c(guild, settings, key, fallback="`Not Set`"):
    """Channel mention or fallback"""
    cid = settings.get(key)
    ch = guild.get_channel(cid) if cid else None
    return ch.mention if ch else fallback

def _b(val):
    """Bool icon"""
    return "✅" if val else "❌"

async def _save(cog, guild_id, settings):
    """Save and return"""
    sync_setup_aliases(settings)
    sync_staff_role_groups(settings)
    await cog.bot.db.update_settings(guild_id, settings)

# ==============================================================================
# CATEGORY SELECT
# ==============================================================================

class SettingsCategorySelect(discord.ui.Select):
    def __init__(self, parent_view, current: str = None):
        options = [
            discord.SelectOption(label="General",      description="Prefix, Core Channels, Branding",  emoji="⚙️",  value="general",      default=current == "general"),
            discord.SelectOption(label="Roles",        description="Full Role Hierarchy (7 roles)",     emoji="👥",  value="roles",        default=current == "roles"),
            discord.SelectOption(label="Moderation",   description="Warn Thresholds & Auto-Punish",    emoji="🔨",  value="moderation",   default=current == "moderation"),
            discord.SelectOption(label="Logging",      description="Compact log routing",              emoji="📝",  value="logging",      default=current == "logging"),
            discord.SelectOption(label="Voice & AFK",  description="AFK Detection, Timeouts",          emoji="🔊",  value="voice",        default=current == "voice"),
            discord.SelectOption(label="Tickets",      description="Support Ticket System",             emoji="🎫",  value="tickets",      default=current == "tickets"),
            discord.SelectOption(label="Anti-Raid",    description="Raid Detection & Response",         emoji="🚨",  value="antiraid",     default=current == "antiraid"),
            discord.SelectOption(label="Verification", description="Member Verification Gate",          emoji="✅",  value="verification", default=current == "verification"),
        ]
        super().__init__(placeholder="📂 Switch settings category...", min_values=1, max_values=1, options=options, row=0)
        self._parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self._parent_view.switch_category(interaction, self.values[0])


class BaseSettingsView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, category: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.category = category
        self.add_item(SettingsCategorySelect(self, current=category))

    async def switch_category(self, interaction: discord.Interaction, new_category: str):
        if new_category == self.category:
            return await interaction.response.defer()
        settings = await self.cog.bot.db.get_settings(self.guild.id)
        VIEW_MAP = {
            "general": GeneralSettingsView,
            "roles": RolesSettingsView,
            "moderation": ModerationSettingsView,
            "logging": LoggingSettingsView,
            "voice": VoiceSettingsView,
            "tickets": TicketsSettingsView,
            "antiraid": AntiRaidSettingsView,
            "verification": VerificationSettingsView,
        }
        view_cls = VIEW_MAP.get(new_category)
        if not view_cls:
            return
        view = view_cls(self.cog, self.guild, settings)
        await interaction.response.edit_message(embed=view.get_embed(), view=view)


# ==============================================================================
# ⚙️ GENERAL SETTINGS
# ==============================================================================

class GeneralSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "general")
        self.settings = settings

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        embed = discord.Embed(
            title="⚙️ General Settings",
            description="Core bot configuration for this server.\nUse the selects and buttons below to change settings.",
            color=Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        # ── Core Settings ──
        prefix = s.get("prefix", ",") or ","
        core = (
            f"**Prefix:** `{prefix}`\n"
            f"**Mute Role:** {_r(g, s, 'mute_role')}\n"
            f"**Manager Role:** {_r(g, s, 'manager_role')}"
        )
        embed.add_field(name="🔧 Core", value=core, inline=True)

        # ── Channels ──
        channels = (
            f"**Mod Log:** {_c(g, s, 'mod_log_channel')}\n"
            f"**Forum Alerts:** {_c(g, s, 'forum_alert_channel', '`Using Mod Log`')}\n"
            f"**Welcome:** {_c(g, s, 'welcome_channel')}"
        )
        embed.add_field(name="📍 Channels", value=channels, inline=True)

        # ── Features ──
        features = (
            f"{_b(s.get('automod_enabled', True))} **AutoMod**\n"
            f"{_b(s.get('verification_enabled', True))} **Verification**\n"
            f"{_b(s.get('antiraid_enabled', True))} **Anti-Raid**\n"
            f"{_b(s.get('warn_thresholds_enabled', True))} **Warn Thresholds**\n"
            f"{_b(s.get('afk_detection_enabled', True))} **AFK Detection**"
        )
        embed.add_field(name="📦 Features", value=features, inline=True)

        embed.set_footer(text="Config Dashboard • Changes saved automatically")
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🔇 Set Mute Role", min_values=0, max_values=1, row=1)
    async def mute_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.settings["mute_role"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="👔 Set Manager Role", min_values=0, max_values=1, row=2)
    async def manager_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.settings["manager_role"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📜 Set Mod Log Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=3)
    async def mod_log_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel_id = select.values[0].id if select.values else None
        self.settings["mod_log_channel"] = channel_id
        self.settings["log_channel_mod"] = channel_id
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="🚨 Set Forum Alert Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=4)
    async def forum_alerts_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["forum_alert_channel"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==============================================================================
# 👥 ROLES / HIERARCHY  (Page 1: Owner/Admin/Mod  |  Page 2: Helper/WL/Bypass/Quarantine)
# ==============================================================================

ROLE_PAGES = [
    # (setting_key, label, emoji)
    [("owner_role", "Owner", "👑"), ("admin_role", "Admin", "⚡"), ("moderator_role", "Moderator", "🛡️")],
    [("helper_role", "Helper", "🤝"), ("whitelisted_role", "Whitelisted", "✅"), ("automod_bypass_role_id", "Bypass", "🔓")],
]

class RolesSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "roles")
        self.settings = settings
        self.page = 0
        self._update_placeholders()

    def _update_placeholders(self):
        page = ROLE_PAGES[self.page]
        self.role_1.placeholder = f"{page[0][2]} Set {page[0][1]} Role"
        self.role_2.placeholder = f"{page[1][2]} Set {page[1][1]} Role"
        self.role_3.placeholder = f"{page[2][2]} Set {page[2][1]} Role"
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(ROLE_PAGES) - 1

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        embed = discord.Embed(
            title="👥 Roles & Hierarchy",
            description="Configure the server's role hierarchy.\nHigher roles have more permissions in the bot.",
            color=Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        # ── Full hierarchy at a glance ──
        hierarchy = (
            f"👑 **Owner:** {_r(g, s, 'owner_role')}\n"
            f"⚡ **Admin:** {_r(g, s, 'admin_role')}\n"
            f"🛡️ **Moderator:** {_r(g, s, 'moderator_role')}\n"
            f"🤝 **Helper:** {_r(g, s, 'helper_role')}"
        )
        embed.add_field(name="📊 Hierarchy", value=hierarchy, inline=True)

        # ── Special roles ──
        special = (
            f"✅ **Whitelisted:** {_r(g, s, 'whitelisted_role')}\n"
            f"🔓 **Bypass:** {_r(g, s, 'automod_bypass_role_id')}\n"
            f"🔒 **Quarantine:** {_r(g, s, 'automod_quarantine_role_id')}\n"
            f"🔇 **Muted:** {_r(g, s, 'muted_role')}"
        )
        embed.add_field(name="🏷️ Special", value=special, inline=True)

        # ── Page info ──
        page = ROLE_PAGES[self.page]
        editing = ", ".join(f"**{lbl}**" for _, lbl, _ in page)
        embed.add_field(name="✏️ Editing (Page " + str(self.page + 1) + "/" + str(len(ROLE_PAGES)) + ")", value=editing, inline=False)

        embed.set_footer(text="Config Dashboard • Use ◀ ▶ to switch pages")
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Set Role 1", min_values=0, max_values=1, row=1)
    async def role_1(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        key = ROLE_PAGES[self.page][0][0]
        self.settings[key] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Set Role 2", min_values=0, max_values=1, row=2)
    async def role_2(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        key = ROLE_PAGES[self.page][1][0]
        self.settings[key] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Set Role 3", min_values=0, max_values=1, row=3)
    async def role_3(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        key = ROLE_PAGES[self.page][2][0]
        self.settings[key] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=4)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_placeholders()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=4)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(ROLE_PAGES) - 1, self.page + 1)
        self._update_placeholders()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==============================================================================
# 🔨 MODERATION (Warn Thresholds + Auto-Punishment)
# ==============================================================================

class ModerationSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "moderation")
        self.settings = settings
        self._sync_buttons()

    def _sync_buttons(self):
        on = self.settings.get("warn_thresholds_enabled", True)
        self.thresh_btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.danger
        self.thresh_btn.label = "Thresholds: ON" if on else "Thresholds: OFF"

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        enabled = s.get("warn_thresholds_enabled", True)

        embed = discord.Embed(
            title="🔨 Moderation Settings",
            description=(
                "Configure automatic punishment escalation based on warning count.\n"
                "Applies to **both** manual `/warn` and AutoMod warns."
            ),
            color=Colors.WARNING if enabled else Colors.ERROR,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        # ── Threshold Status ──
        mute_at = s.get("warn_threshold_mute", 3)
        kick_at = s.get("warn_threshold_kick", 5)
        ban_at = s.get("warn_threshold_ban", 7)
        mute_dur = s.get("warn_mute_duration", 3600)

        thresholds = (
            f"**Status:** {_b(enabled)} {'Active' if enabled else 'Disabled'}\n"
            f"🔇 **Mute at** `{mute_at}` warnings → timeout `{mute_dur}s` ({mute_dur // 60}m)\n"
            f"👢 **Kick at** `{kick_at}` warnings\n"
            f"🔨 **Ban at** `{ban_at}` warnings"
        )
        embed.add_field(name="⚠️ Warning Thresholds", value=thresholds, inline=False)

        # ── AutoMod Punishment ──
        punishment = s.get("automod_punishment", "warn").upper()
        am_mute_dur = s.get("automod_mute_duration", 3600)
        automod = (
            f"**Default Punishment:** `{punishment}`\n"
            f"**Mute Duration:** `{am_mute_dur}s` ({am_mute_dur // 60}m)\n"
            f"**Notify Users:** {_b(s.get('automod_notify_users', True))}"
        )
        embed.add_field(name="🛡️ AutoMod Punishment", value=automod, inline=True)

        # ── Infrastructure ──
        infra = (
            f"**Mute Role:** {_r(g, s, 'mute_role')}\n"
            f"**Quarantine Role:** {_r(g, s, 'automod_quarantine_role_id')}\n"
            f"**AutoMod Log:** {_c(g, s, 'automod_log_channel', '`Disabled`')}"
        )
        embed.add_field(name="⚙️ Infrastructure", value=infra, inline=True)

        embed.set_footer(text="Config Dashboard • Toggle thresholds or click Edit to change values")
        return embed

    @discord.ui.button(label="Thresholds: OFF", style=discord.ButtonStyle.danger, row=1)
    async def thresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settings["warn_thresholds_enabled"] = not self.settings.get("warn_thresholds_enabled", True)
        await _save(self.cog, self.guild.id, self.settings)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Edit Thresholds", emoji="📊", style=discord.ButtonStyle.primary, row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WarnThresholdsModal(self))

    @discord.ui.button(label="Edit Punishment", emoji="⚡", style=discord.ButtonStyle.primary, row=1)
    async def punishment_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PunishmentSettingsModal(self))

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🔒 Set Quarantine Role", min_values=0, max_values=1, row=2)
    async def quarantine_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.settings["automod_quarantine_role_id"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📝 Set AutoMod Log Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=3)
    async def log_ch_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["automod_log_channel"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


class WarnThresholdsModal(discord.ui.Modal, title="📊 Warning Thresholds"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.mute_at = discord.ui.TextInput(label="Mute at X warnings", placeholder="3", default=str(view.settings.get("warn_threshold_mute", 3)), min_length=1, max_length=3)
        self.kick_at = discord.ui.TextInput(label="Kick at X warnings", placeholder="5", default=str(view.settings.get("warn_threshold_kick", 5)), min_length=1, max_length=3)
        self.ban_at = discord.ui.TextInput(label="Ban at X warnings", placeholder="7", default=str(view.settings.get("warn_threshold_ban", 7)), min_length=1, max_length=3)
        self.mute_dur = discord.ui.TextInput(label="Mute duration (seconds)", placeholder="3600", default=str(view.settings.get("warn_mute_duration", 3600)), min_length=1, max_length=7)
        self.add_item(self.mute_at)
        self.add_item(self.kick_at)
        self.add_item(self.ban_at)
        self.add_item(self.mute_dur)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            m, k, b, d = int(self.mute_at.value), int(self.kick_at.value), int(self.ban_at.value), int(self.mute_dur.value)
            if not (1 <= m < k < b <= 50):
                return await interaction.response.send_message("Thresholds must be: mute < kick < ban (1-50)", ephemeral=True)
            if d < 60:
                return await interaction.response.send_message("Mute duration must be ≥60 seconds", ephemeral=True)
            self.view.settings.update({"warn_threshold_mute": m, "warn_threshold_kick": k, "warn_threshold_ban": b, "warn_mute_duration": d})
            await _save(self.view.cog, interaction.guild_id, self.view.settings)
            await interaction.response.edit_message(embed=self.view.get_embed(), view=self.view)
        except ValueError:
            await interaction.response.send_message("All fields must be numbers.", ephemeral=True)


class PunishmentSettingsModal(discord.ui.Modal, title="⚡ Punishment Settings"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.punishment = discord.ui.TextInput(label="Default punishment (warn/mute/kick/ban)", placeholder="warn", default=view.settings.get("automod_punishment", "warn"), min_length=2, max_length=10)
        self.mute_dur = discord.ui.TextInput(label="AutoMod mute duration (seconds)", placeholder="3600", default=str(view.settings.get("automod_mute_duration", 3600)), min_length=1, max_length=7)
        self.add_item(self.punishment)
        self.add_item(self.mute_dur)

    async def on_submit(self, interaction: discord.Interaction):
        p = self.punishment.value.lower().strip()
        if p not in ("warn", "mute", "kick", "ban", "delete", "quarantine"):
            return await interaction.response.send_message("Valid: warn, mute, kick, ban, delete, quarantine", ephemeral=True)
        try:
            d = int(self.mute_dur.value)
        except ValueError:
            return await interaction.response.send_message("Duration must be a number.", ephemeral=True)
        self.view.settings["automod_punishment"] = p
        self.view.settings["automod_mute_duration"] = max(60, d)
        await _save(self.view.cog, interaction.guild_id, self.view.settings)
        await interaction.response.edit_message(embed=self.view.get_embed(), view=self.view)


# ==============================================================================
# 📝 LOGGING (compact routing)
# ==============================================================================

LOG_PAGES = [
    [("audit_log_channel", "Audit Logs", "Audit"), ("mod_log_channel", "Mod Logs", "Mod"), ("message_log_channel", "Message Logs", "Message")],
    [("automod_log_channel", "AutoMod Logs", "AutoMod"), ("voice_log_channel", "Voice Logs", "Voice"), ("verify_log_channel", "Verify Logs", "Verify")],
]

class LoggingSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "logging")
        self.settings = settings
        self.page = 0
        self._update_placeholders()

    def _set_log_channel_setting(self, key: str, channel_id: Optional[int]) -> None:
        self.settings[key] = channel_id
        if key == "mod_log_channel":
            self.settings["log_channel_mod"] = channel_id
        elif key == "audit_log_channel":
            self.settings["log_channel_audit"] = channel_id
        elif key == "message_log_channel":
            self.settings["log_channel_message"] = channel_id
        elif key == "automod_log_channel":
            self.settings["log_channel_automod"] = channel_id
        elif key == "voice_log_channel":
            self.settings["log_channel_voice"] = channel_id
        elif key == "ticket_log_channel":
            self.settings["log_channel_ticket"] = channel_id
        elif key == "verify_log_channel":
            self.settings["verification_log_channel"] = channel_id
        apply_compact_log_routing(self.settings)

    def _update_placeholders(self):
        page = LOG_PAGES[self.page]
        self.ch_1.placeholder = f"{page[0][2]} Set {page[0][1]} Channel"
        self.ch_2.placeholder = f"{page[1][2]} Set {page[1][1]} Channel"
        self.ch_3.placeholder = f"{page[2][2]} Set {page[2][1]} Channel"
        self.log_prev.disabled = self.page <= 0
        self.log_next.disabled = self.page >= len(LOG_PAGES) - 1

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        embed = discord.Embed(
            title="📝 Logging Configuration",
            description=(
                "Configure the standard `Moderation Logs` layout.\n"
                "Setup syncs `#audit-logs`, `#mod-logs`, `#message-logs`, `#automod-logs`, and `#voice-logs`."
            ),
            color=Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        # ── All channels at a glance ──
        col1 = (
            f"**Audit Logs:** {_c(g, s, 'audit_log_channel', '`Disabled`')}\n"
            f"**Mod Logs:** {_c(g, s, 'mod_log_channel', '`Disabled`')}\n"
            f"**Message Logs:** {_c(g, s, 'message_log_channel', '`Disabled`')}"
        )
        embed.add_field(name="Main Channels", value=col1, inline=True)

        col2 = (
            f"**AutoMod Logs:** {_c(g, s, 'automod_log_channel', '`Disabled`')}\n"
            f"**Voice Logs:** {_c(g, s, 'voice_log_channel', '`Disabled`')}\n"
            f"**Verify Logs:** {_c(g, s, 'verify_log_channel', '`Disabled`')}"
        )
        embed.add_field(name="Other Routing", value=col2, inline=True)

        embed.add_field(
            name="Routing Rules",
            value=(
                "`audit-logs`: server audit and configuration changes\n"
                "`mod-logs`: moderation, reports, tickets, modmail, forum alerts\n"
                "`message-logs`: message edits/deletes and purge transcripts\n"
                "`automod-logs`: automod and AI moderation events\n"
                "`voice-logs`: voice state and voice moderation events\n"
                "`verify-logs`: verification actions only"
            ),
            inline=False,
        )

        embed.set_footer(text="Config Dashboard • Compact logging keeps setup simple")
        return embed

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Set Channel 1", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=1)
    async def ch_1(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        key = LOG_PAGES[self.page][0][0]
        channel_id = select.values[0].id if select.values else None
        self._set_log_channel_setting(key, channel_id)
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Set Channel 2", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=2)
    async def ch_2(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        key = LOG_PAGES[self.page][1][0]
        channel_id = select.values[0].id if select.values else None
        self._set_log_channel_setting(key, channel_id)
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Set Channel 3", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=3)
    async def ch_3(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        key = LOG_PAGES[self.page][2][0]
        channel_id = select.values[0].id if select.values else None
        self._set_log_channel_setting(key, channel_id)
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=4)
    async def log_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_placeholders()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=4)
    async def log_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(LOG_PAGES) - 1, self.page + 1)
        self._update_placeholders()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==============================================================================
# 🔊 VOICE & AFK
# ==============================================================================

class VoiceSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "voice")
        self.settings = settings
        self._sync_buttons()

    def _sync_buttons(self):
        on = self.settings.get("afk_detection_enabled", True)
        self.afk_btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.danger
        self.afk_btn.label = "AFK: ON" if on else "AFK: OFF"

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        enabled = s.get("afk_detection_enabled", True)

        embed = discord.Embed(
            title="🔊 Voice & AFK Settings",
            description="Configure voice channel monitoring and AFK detection.",
            color=Colors.SUCCESS if enabled else Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        # ── Status ──
        status = (
            f"**AFK Detection:** {_b(enabled)} {'Active' if enabled else 'Disabled'}\n"
            f"**Inactive Timeout:** `{s.get('afk_timeout_minutes', 15)}` minutes\n"
            f"**Response Time:** `{s.get('afk_response_timeout', 30)}` seconds"
        )
        embed.add_field(name="⏱️ Timers", value=status, inline=True)

        # ── Channels ──
        ignored = s.get("afk_ignored_channels", [])
        voice_log = _c(g, s, "log_channel_voice", "`Disabled`")
        channels = (
            f"**Voice Log:** {voice_log}\n"
            f"**Ignored VCs:** `{len(ignored)}` channels"
        )
        embed.add_field(name="📍 Channels", value=channels, inline=True)

        embed.set_footer(text="Config Dashboard • Toggle AFK or click Edit to change timeouts")
        return embed

    @discord.ui.button(label="AFK: OFF", style=discord.ButtonStyle.danger, row=1)
    async def afk_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settings["afk_detection_enabled"] = not self.settings.get("afk_detection_enabled", True)
        await _save(self.cog, self.guild.id, self.settings)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Edit Timeouts", emoji="⏱️", style=discord.ButtonStyle.primary, row=1)
    async def timeout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VoiceTimeoutModal(self))

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="🔇 Select Ignored Voice Channels", min_values=0, max_values=25, channel_types=[discord.ChannelType.voice], row=2)
    async def ignored_vc(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["afk_ignored_channels"] = [c.id for c in select.values]
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="🔊 Set Voice Log Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=3)
    async def voice_log_ch(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["log_channel_voice"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


class VoiceTimeoutModal(discord.ui.Modal, title="⏱️ AFK Timeouts"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.afk_time = discord.ui.TextInput(label="Inactive Timeout (minutes, 1-120)", placeholder="15", default=str(view.settings.get("afk_timeout_minutes", 15)), min_length=1, max_length=3)
        self.response_time = discord.ui.TextInput(label="Response Time (seconds, 10-300)", placeholder="30", default=str(view.settings.get("afk_response_timeout", 30)), min_length=1, max_length=3)
        self.add_item(self.afk_time)
        self.add_item(self.response_time)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mins = int(self.afk_time.value)
            secs = int(self.response_time.value)
            if not (1 <= mins <= 120): raise ValueError("Minutes must be 1-120")
            if not (10 <= secs <= 300): raise ValueError("Seconds must be 10-300")
            self.view.settings["afk_timeout_minutes"] = mins
            self.view.settings["afk_response_timeout"] = secs
            await _save(self.view.cog, interaction.guild_id, self.view.settings)
            await interaction.response.edit_message(embed=self.view.get_embed(), view=self.view)
        except ValueError as e:
            await interaction.response.send_message(f"Invalid: {e}", ephemeral=True)


# ==============================================================================
# 🎫 TICKETS
# ==============================================================================

class TicketsSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "tickets")
        self.settings = settings

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        embed = discord.Embed(
            title="🎫 Ticket Settings",
            description="Configure your support ticket system.\nTickets are created in a dedicated category with staff access.",
            color=Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        config = (
            f"**Support Role:** {_r(g, s, 'ticket_support_role')}\n"
            f"**Log Channel:** {_c(g, s, 'ticket_log_channel')}\n"
            f"**Category:** {_c(g, s, 'ticket_category', '`Default`')}"
        )
        embed.add_field(name="⚙️ Configuration", value=config, inline=True)

        # Stats preview
        embed.add_field(
            name="📊 Usage",
            value="Use `/ticket create` to create tickets\nUse `/ticket close` to close\nPrefix: `,ticket create`",
            inline=True,
        )

        embed.set_footer(text="Config Dashboard • Changes saved automatically")
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="👥 Set Support Role", min_values=0, max_values=1, row=1)
    async def support_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.settings["ticket_support_role"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📝 Set Ticket Log Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=2)
    async def log_ch(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["ticket_log_channel"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📁 Set Ticket Category", min_values=0, max_values=1, channel_types=[discord.ChannelType.category], row=3)
    async def category_sel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["ticket_category"] = select.values[0].id if select.values else None
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==============================================================================
# 🚨 ANTI-RAID
# ==============================================================================

class AntiRaidSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "antiraid")
        self.settings = settings
        self._sync_buttons()

    def _sync_buttons(self):
        on = self.settings.get("antiraid_enabled", True)
        self.raid_btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.danger
        self.raid_btn.label = "Anti-Raid: ON" if on else "Anti-Raid: OFF"

    def get_embed(self) -> discord.Embed:
        s = self.settings
        enabled = s.get("antiraid_enabled", True)
        threshold = s.get("antiraid_join_threshold", 10)
        interval = s.get("antiraid_join_interval", 10)
        action = s.get("antiraid_action", "kick").upper()

        embed = discord.Embed(
            title="🚨 Anti-Raid Settings",
            description=f"Triggers when **{threshold}+ users** join within **{interval}s**.",
            color=Colors.ERROR if enabled else Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        config = (
            f"**Status:** {_b(enabled)} {'Active' if enabled else 'Disabled'}\n"
            f"**Action:** `{action}`\n"
            f"**Join Threshold:** `{threshold}` users\n"
            f"**Time Window:** `{interval}` seconds"
        )
        embed.add_field(name="⚙️ Configuration", value=config, inline=True)

        actions_help = (
            "`KICK` — Kick suspected raiders\n"
            "`BAN` — Ban suspected raiders\n"
            "`LOCKDOWN` — Lock all channels"
        )
        embed.add_field(name="📋 Available Actions", value=actions_help, inline=True)

        embed.set_footer(text="Config Dashboard • Toggle or click Edit to configure")
        return embed

    @discord.ui.button(label="Anti-Raid: OFF", style=discord.ButtonStyle.danger, row=1)
    async def raid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settings["antiraid_enabled"] = not self.settings.get("antiraid_enabled", True)
        await _save(self.cog, self.guild.id, self.settings)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Edit Settings", emoji="📊", style=discord.ButtonStyle.primary, row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AntiRaidModal(self))

    @discord.ui.select(
        placeholder="⚡ Select Raid Action",
        options=[
            discord.SelectOption(label="Kick", value="kick", emoji="👢"),
            discord.SelectOption(label="Ban", value="ban", emoji="🔨"),
            discord.SelectOption(label="Lockdown", value="lockdown", emoji="🔒"),
        ],
        row=2,
    )
    async def action_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.settings["antiraid_action"] = select.values[0]
        await _save(self.cog, self.guild.id, self.settings)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


class AntiRaidModal(discord.ui.Modal, title="🚨 Anti-Raid Configuration"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.threshold = discord.ui.TextInput(label="Join threshold (users, 2-50)", placeholder="10", default=str(view.settings.get("antiraid_join_threshold", 10)), min_length=1, max_length=3)
        self.interval = discord.ui.TextInput(label="Time window (seconds, 5-120)", placeholder="10", default=str(view.settings.get("antiraid_join_interval", 10)), min_length=1, max_length=4)
        self.add_item(self.threshold)
        self.add_item(self.interval)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            t, i = int(self.threshold.value), int(self.interval.value)
            if not (2 <= t <= 50): raise ValueError("Threshold must be 2-50")
            if not (5 <= i <= 120): raise ValueError("Interval must be 5-120")
            self.view.settings["antiraid_join_threshold"] = t
            self.view.settings["antiraid_join_interval"] = i
            await _save(self.view.cog, interaction.guild_id, self.view.settings)
            await interaction.response.edit_message(embed=self.view.get_embed(), view=self.view)
        except ValueError as e:
            await interaction.response.send_message(f"Invalid: {e}", ephemeral=True)


# ==============================================================================
# ✅ VERIFICATION
# ==============================================================================

class VerificationSettingsView(BaseSettingsView):
    def __init__(self, cog, guild, settings):
        super().__init__(cog, guild, "verification")
        self.settings = settings
        sync_setup_aliases(self.settings)
        self._sync_buttons()

    async def _persist(self, *, previous_unverified_role_id: Optional[int] = None):
        await _save(self.cog, self.guild.id, self.settings)
        await apply_verification_gate(
            self.guild,
            self.settings,
            previous_unverified_role_id=previous_unverified_role_id,
        )

    def _sync_buttons(self):
        on = self.settings.get("verification_enabled", True)
        self.verify_btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.danger
        self.verify_btn.label = "Verification: ON" if on else "Verification: OFF"

    def get_embed(self) -> discord.Embed:
        s = self.settings
        g = self.guild
        enabled = s.get("verification_enabled", True)

        embed = discord.Embed(
            title="✅ Verification Settings",
            description="New members must verify in the designated channel to get the verified role.\nUnverified members are restricted until they complete verification.",
            color=Colors.SUCCESS if enabled else Colors.INFO,
            timestamp=datetime.datetime.now(timezone.utc),
        )

        config = (
            f"**Status:** {_b(enabled)} {'Active' if enabled else 'Disabled'}\n"
            f"**Channel:** {_c(g, s, 'verify_channel')}\n"
            f"**Verified Role:** {_r(g, s, 'verified_role')}\n"
            f"**Unverified Role:** {_r(g, s, 'unverified_role')}"
        )
        embed.add_field(name="⚙️ Configuration", value=config, inline=True)

        embed.set_footer(text="Config Dashboard • Toggle verification and set channel/roles below")
        return embed

    @discord.ui.button(label="Verification: OFF", style=discord.ButtonStyle.danger, row=1)
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settings["verification_enabled"] = not self.settings.get("verification_enabled", True)
        await self._persist()
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📍 Set Verification Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text], row=2)
    async def ver_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.settings["verify_channel"] = select.values[0].id if select.values else None
        await self._persist()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="✅ Set Verified Role", min_values=0, max_values=1, row=3)
    async def ver_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.settings["verified_role"] = select.values[0].id if select.values else None
        await self._persist()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🚫 Set Unverified Role", min_values=0, max_values=1, row=4)
    async def unver_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        previous_unverified_role_id = self.settings.get("unverified_role")
        self.settings["unverified_role"] = select.values[0].id if select.values else None
        await self._persist(previous_unverified_role_id=previous_unverified_role_id)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==============================================================================
# SETTINGS COG
# ==============================================================================

class Settings(commands.Cog):
    """Unified Settings Command"""
    def __init__(self, bot):
        self.bot = bot

    config_group = app_commands.Group(name="config", description="⚙️ Bot configuration")

    @config_group.command(name="dashboard", description="Open the interactive settings dashboard")
    @is_admin()
    async def dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        settings = await self.bot.db.get_settings(interaction.guild_id)
        view = GeneralSettingsView(self, interaction.guild, settings)
        await interaction.followup.send(embed=view.get_embed(), view=view)

    @app_commands.command(name="settings", description="Open the interactive settings dashboard")
    @is_admin()
    async def settings_alias(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        settings = await self.bot.db.get_settings(interaction.guild_id)
        view = GeneralSettingsView(self, interaction.guild, settings)
        await interaction.followup.send(embed=view.get_embed(), view=view)

async def setup(bot):
    await bot.add_cog(Settings(bot))
