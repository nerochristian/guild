"""Full AutoMod setup wizard.

The wizard is intentionally additive: it builds a complete settings payload in
memory and only writes it when the admin presses Finish Setup. It uses a private
temporary setup channel when possible, Components v2 layout cards, buttons,
selects, and modals for detailed input such as timeout durations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import discord

from config import Config
from utils.components_v2 import ensure_layout_view_action_rows
from .config import AUTOMOD_PRESETS, MODULE_SETTING_KEYS, apply_preset, default_settings
from .health import build_automod_health_report
from .utils import compact_duration, parse_duration


ACTION_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("log", "Log Only", "Only records a staff log entry."),
    ("delete", "Delete Only", "Deletes the message but does not warn/punish."),
    ("warn", "Delete + Warn", "Deletes and creates a warning case."),
    ("timeout", "Delete + Timeout/Mute", "Deletes and asks how long to timeout."),
    ("kick", "Delete + Kick", "Deletes and kicks the member."),
    ("ban", "Delete + Ban", "Deletes and bans; requires confirmation."),
)

SETUP_LEVELS: dict[str, str] = {
    "quick": "Quick Setup — fastest, recommended defaults",
    "full": "Full Setup — asks all important policy questions",
    "expert": "Expert Setup — exposes advanced per-rule controls",
}

ENFORCEMENT_MODES: dict[str, str] = {
    "monitor": "Monitor Only — detect and log first",
    "soft": "Soft — delete + warn most issues",
    "balanced": "Balanced — warn then timeout repeat offenders",
    "strict": "Strict — faster timeouts for dangerous behavior",
    "lockdown": "Lockdown — emergency raid/scam mode",
}

SECURITY_POLICIES: tuple[tuple[str, str, str], ...] = (
    ("scams", "Scams / phishing", "Nitro scams, fake verification, gift links, phishing language."),
    ("links", "Suspicious links", "Shorteners, grabify/iplogger style links, or allowlist mode."),
    ("invites", "Discord invites", "Normal and disguised invite links."),
    ("mentions", "Mass mentions", "Mention raids and ping spam."),
    ("new_accounts", "New accounts", "Users with very new Discord accounts."),
    ("raid", "Join raids", "Many accounts joining quickly."),
    ("attachments", "Attachment spam", "Too many files/images too quickly."),
    ("emoji_spam", "Emoji / wall / unicode spam", "Emoji floods, walls of text, Zalgo/unicode abuse."),
)

PROFILE_LABELS: dict[str, str] = {
    "starter": "Starter",
    "community": "Community",
    "gaming": "Gaming",
    "support": "Support",
    "marketplace": "Marketplace",
    "high_security": "High Security",
    "chill": "Chill",
    "lockdown": "Lockdown",
}


def _duration_label(seconds: int | None) -> str:
    return compact_duration(int(seconds or 0)) if seconds else "none"


def _clean_lines(value: str, *, limit: int = 100, min_length: int = 2, max_length: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (value or "").replace(",", "\n").splitlines():
        item = " ".join(raw.strip().split())
        folded = item.casefold()
        if min_length <= len(item) <= max_length and folded not in seen:
            seen.add(folded)
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _action_label(policy: dict[str, Any] | None) -> str:
    if not policy:
        return "not set"
    action = str(policy.get("action", "log"))
    if action == "log" and policy.get("delete"):
        return "delete only"
    if action == "timeout":
        return f"timeout {_duration_label(policy.get('duration'))}"
    if action == "ban":
        return f"ban / delete {int(policy.get('delete_days', 1) or 0)}d history"
    return action


@dataclass
class AutoModWizardState:
    settings: dict[str, Any] = field(default_factory=default_settings)
    setup_level: str = "quick"
    profile: str = "starter"
    enforcement: str = "balanced"
    safe_rollout: str = "off"
    current_policy_key: Optional[str] = None
    completed_pages: set[str] = field(default_factory=set)

    def apply_profile(self, profile: str) -> None:
        self.profile = profile
        preserved = self._portable_preserved_settings()
        self.settings = apply_preset(profile)
        self.settings.update(preserved)
        self.settings["automod_setup_profile"] = profile

    def apply_enforcement(self, mode: str) -> None:
        self.enforcement = mode
        self.settings["automod_enabled"] = True
        self.settings["automod_safe_mode"] = mode == "monitor"
        self.settings["automod_setup_level"] = self.setup_level
        if mode == "monitor":
            self.settings["automod_punishment"] = "log"
            self.settings["automod_security_punishment"] = "log"
            self.settings["automod_raid_punishment"] = "log"
        elif mode == "soft":
            self.settings["automod_punishment"] = "warn"
            self.settings["automod_security_punishment"] = "warn"
            self.settings["automod_raid_punishment"] = "timeout"
        elif mode == "balanced":
            self.settings["automod_punishment"] = "warn"
            self.settings["automod_security_punishment"] = "timeout"
            self.settings["automod_raid_punishment"] = "timeout"
        elif mode == "strict":
            self.settings["automod_punishment"] = "timeout"
            self.settings["automod_security_punishment"] = "timeout"
            self.settings["automod_raid_punishment"] = "timeout"
            self.settings["automod_spam_threshold"] = min(4, int(self.settings.get("automod_spam_threshold", 5)))
            self.settings["automod_fast_message_threshold"] = min(3, int(self.settings.get("automod_fast_message_threshold", 4)))
        elif mode == "lockdown":
            lockdown = apply_preset("lockdown")
            preserved = self._portable_preserved_settings()
            self.settings.update(lockdown)
            self.settings.update(preserved)
            self.settings["automod_lockdown_enabled"] = True

    def set_policy(self, key: str, action: str, *, duration: int | None = None, delete: bool = True, delete_days: int = 1) -> None:
        policies = dict(self.settings.get("automod_rule_actions", {}) or {})
        normalized = "timeout" if action == "mute" else action
        if normalized == "delete":
            normalized = "log"
            delete = True
        policy: dict[str, Any] = {"action": normalized, "delete": bool(delete)}
        if normalized == "timeout" and duration:
            policy["duration"] = int(duration)
        if normalized == "ban":
            policy["delete_days"] = max(0, min(7, int(delete_days)))
        policies[key] = policy
        self.settings["automod_rule_actions"] = policies

    def _portable_preserved_settings(self) -> dict[str, Any]:
        keys = (
            "automod_log_channel",
            "log_channel_automod",
            "automod_bypass_roles",
            "automod_bypass_users",
            "automod_bypass_channels",
            "automod_badwords",
            "automod_allowed_invites",
            "automod_whitelisted_domains",
            "automod_links_whitelist",
            "automod_rule_actions",
            "automod_appeal_instructions",
        )
        return {key: self.settings[key] for key in keys if self.settings.get(key)}


class AutoModWizardSession:
    def __init__(self, bot: discord.Client, guild: discord.Guild, user_id: int, storage: Any) -> None:
        self.bot = bot
        self.guild = guild
        self.user_id = int(user_id)
        self.storage = storage
        self.state = AutoModWizardState()
        self.channel: Optional[discord.TextChannel] = None
        self.message: Optional[discord.Message] = None
        self.finished = False
        self.started_at = datetime.now(timezone.utc)

    async def start(self, interaction: discord.Interaction) -> None:
        existing = await self.storage.get_settings(self.guild.id)
        self.state.settings.update(existing)
        self.state.apply_profile(str(self.state.settings.get("automod_setup_profile") or "starter"))
        self.state.apply_enforcement("balanced")
        target_channel = await self._create_setup_channel(interaction)
        self.channel = target_channel
        if target_channel is not None and target_channel != interaction.channel:
            await interaction.response.send_message(f"Created your private AutoMod setup room: {target_channel.mention}", ephemeral=True)
        else:
            await interaction.response.send_message("I could not create a private setup channel, so I opened the wizard here.", ephemeral=True)
            target_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
            self.channel = target_channel
        if target_channel is None:
            return
        view = AutoModWizardView(self, "welcome")
        self.message = await target_channel.send(view=view.build_layout())

    async def refresh(self, interaction: discord.Interaction, page: str) -> None:
        view = AutoModWizardView(self, page)
        if interaction.response.is_done():
            await interaction.edit_original_response(view=view.build_layout())
        else:
            await interaction.response.edit_message(view=view.build_layout())

    async def apply(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.state.settings["automod_enabled"] = True
        self.state.settings["automod_setup_level"] = self.state.setup_level
        self.state.settings["automod_setup_profile"] = self.state.profile
        await self.storage.update_settings(self.guild.id, self.state.settings)
        view = AutoModWizardView(self, "finished")
        await interaction.response.edit_message(view=view.build_layout())

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        view = AutoModWizardView(self, "cancelled")
        await interaction.response.edit_message(view=view.build_layout())

    async def delete_channel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        channel = self.channel
        if channel is None:
            await interaction.response.send_message("Setup channel is already gone.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting this setup channel in 3 seconds...", ephemeral=True)
        try:
            await channel.delete(reason="AutoMod setup wizard finished")
        except discord.HTTPException:
            pass

    async def _create_setup_channel(self, interaction: discord.Interaction) -> Optional[discord.TextChannel]:
        bot_member = self.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        user = interaction.user
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, manage_channels=True, read_message_history=True),
        }
        if isinstance(user, discord.Member):
            overwrites[user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        safe_name = "".join(ch for ch in getattr(user, "name", "admin").lower() if ch.isalnum() or ch in "-_")[:16] or "admin"
        try:
            return await self.guild.create_text_channel(
                name=f"automod-setup-{safe_name}",
                overwrites=overwrites,
                reason=f"AutoMod setup wizard started by {user}",
            )
        except discord.HTTPException:
            return interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None


class AutoModWizardView:
    """Stateless renderer for the wizard's current page."""

    def __init__(self, session: AutoModWizardSession, page: str) -> None:
        self.session = session
        self.page = page

    def build_layout(self) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=1800)

        async def check(layout_self: discord.ui.LayoutView, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.session.user_id:
                await interaction.response.send_message("Only the admin who started this wizard can use these controls.", ephemeral=True)
                return False
            return True

        view.interaction_check = check.__get__(view, discord.ui.LayoutView)  # type: ignore[method-assign]
        container = discord.ui.Container(accent_color=getattr(Config, "COLOR_MOD", 0x5865F2))
        container.add_item(discord.ui.TextDisplay(self._content()))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        for row in self._rows():
            container.add_item(discord.ui.ActionRow(*row))
        view.add_item(container)
        return ensure_layout_view_action_rows(view)

    def _content(self) -> str:
        title = "## 🛡️ AutoMod Setup Wizard"
        progress = self._progress_line()
        if self.page == "welcome":
            return f"{title}\nPrivate setup room for configuring AutoMod from start to finish.\n\n{progress}\n\nPick how detailed setup should be. Quick is easiest; Full/Expert unlocks more questions."
        if self.page == "profile":
            return f"{title}\n### Server Profile\nChoose the closest server type. This gives safe defaults before you customize.\n\nCurrent: **{PROFILE_LABELS.get(self.session.state.profile, self.session.state.profile)}**\n\n{self._profile_summary()}"
        if self.page == "enforcement":
            return f"{title}\n### Enforcement Style + Safe Rollout\nChoose how hard AutoMod acts. Monitor Only is safest for testing.\n\nCurrent: **{ENFORCEMENT_MODES.get(self.session.state.enforcement, self.session.state.enforcement)}**\nSafe mode: **{bool(self.session.state.settings.get('automod_safe_mode'))}**"
        if self.page == "modules":
            enabled = [module for module, key in MODULE_SETTING_KEYS.items() if self.session.state.settings.get(key, False)]
            return f"{title}\n### Protection Modules\nToggle major protections. Recommended defaults are already selected.\n\nEnabled: {', '.join(f'`{item}`' for item in enabled) or 'none'}"
        if self.page == "badwords":
            words = self.session.state.settings.get("automod_badwords", []) or []
            return f"{title}\n### Blocked Words / Phrases\nAdd server-specific words or phrases with a popup. I will not ship a default slur list; the server owner chooses exact terms.\n\nConfigured: **{len(words)}** phrases\nPreview: {', '.join(f'`{w}`' for w in words[:8]) or 'none'}"
        if self.page == "allowlists":
            domains = self.session.state.settings.get("automod_whitelisted_domains", []) or []
            invites = self.session.state.settings.get("automod_allowed_invites", []) or []
            return f"{title}\n### Allowlists\nChoose what should be allowed even when filters are strict.\n\nAllowed domains: {', '.join(f'`{d}`' for d in domains[:8]) or 'none'}\nAllowed invites: {', '.join(f'`{i}`' for i in invites[:8]) or 'none'}"
        if self.page == "normal_action":
            policy = (self.session.state.settings.get("automod_rule_actions", {}) or {}).get("default")
            return f"{title}\n### Normal Violation Action\nThis applies to common spam, caps, bad words, duplicates, and similar behavior unless a specific rule overrides it.\n\nCurrent normal policy: **{_action_label(policy) if policy else self.session.state.settings.get('automod_punishment', 'warn')}**"
        if self.page == "security":
            return f"{title}\n### Security Policy Builder\nConfigure each dangerous category separately. If you choose timeout/mute, a popup asks for duration. Ban requires typed confirmation.\n\n{self._security_summary()}"
        if self.page == "thresholds":
            s = self.session.state.settings
            return f"{title}\n### Warning Thresholds\nSet what happens after users collect warnings.\n\nEnabled: **{bool(s.get('warn_thresholds_enabled', True))}**\n{s.get('warn_threshold_mute', 3)} warnings → timeout **{_duration_label(s.get('warn_mute_duration'))}**\n{s.get('warn_threshold_kick', 5)} warnings → kick\n{s.get('warn_threshold_ban', 7)} warnings → ban"
        if self.page == "escalation":
            return f"{title}\n### Recent AutoMod Offense Escalation\nSeparate from warnings: repeated AutoMod hits in a time window can become stronger automatically.\n\n{self._escalation_summary()}"
        if self.page == "logs":
            log = self.session.state.settings.get("automod_log_channel")
            return f"{title}\n### Logs, Notifications, Appeals\nPick where staff logs go and what users see.\n\nLog channel: {f'<#{log}>' if log else '**not set**'}\nDM users: **{bool(self.session.state.settings.get('automod_notify_users', True))}**\nPublic feedback: **{bool(self.session.state.settings.get('automod_public_feedback', False))}**\nAppeal text: {self.session.state.settings.get('automod_appeal_instructions') or 'none'}"
        if self.page == "bypass":
            s = self.session.state.settings
            return f"{title}\n### Bypass Roles / Channels\nKeep staff, bot channels, logs, and special channels from being hit by AutoMod.\n\nBypass staff permissions: **{bool(s.get('automod_bypass_staff', True))}**\nRoles: {self._mention_ids(s.get('automod_bypass_roles', []), role=True)}\nChannels: {self._mention_ids(s.get('automod_bypass_channels', []), channel=True)}"
        if self.page == "emergency":
            s = self.session.state.settings
            return f"{title}\n### Emergency / Lockdown Settings\nConfigure raid-mode behavior that can be reused later.\n\nLockdown enabled: **{bool(s.get('automod_lockdown_enabled', False))}**\nLockdown duration: **{_duration_label(s.get('automod_lockdown_duration'))}**\nRaid action: **{s.get('automod_raid_punishment', 'timeout')}**"
        if self.page == "review":
            report = build_automod_health_report(self.session.guild, self.session.state.settings)
            return f"{title}\n### Final Review\nNothing is saved until you press **Finish Setup**.\n\n{self._final_summary()}\n\n**Health Score:** {report.score}/100 ({report.grade})\n{self._doctor_short(report)}"
        if self.page == "finished":
            report = build_automod_health_report(self.session.guild, self.session.state.settings)
            return f"{title}\n### ✅ AutoMod Setup Complete\nSaved the full server configuration.\n\nHealth Score: **{report.score}/100 ({report.grade})**\nUse `/automod status`, `/automod doctor`, or `/automod scan` next."
        if self.page == "cancelled":
            return f"{title}\n### Setup Cancelled\nNo settings were saved. You can run `/automod wizard` again anytime."
        return f"{title}\nUnknown page."

    def _rows(self) -> list[list[discord.ui.Item[Any]]]:
        if self.page == "welcome":
            return [[self._select("setup_level", "Choose setup depth", SETUP_LEVELS)], [self._button("Next", "profile", discord.ButtonStyle.primary), self._cancel_button()]]
        if self.page == "profile":
            return [[self._profile_select()], self._nav("welcome", "enforcement")]
        if self.page == "enforcement":
            return [[self._select("enforcement", "Choose enforcement style", ENFORCEMENT_MODES)], [self._button("Toggle Safe Mode", "toggle_safe", discord.ButtonStyle.secondary), self._button("Back", "profile", discord.ButtonStyle.secondary), self._button("Next", "modules", discord.ButtonStyle.primary), self._cancel_button()]]
        if self.page == "modules":
            return [[self._module_select()], [self._button("Enable Recommended", "recommended_modules", discord.ButtonStyle.success), self._button("Back", "enforcement", discord.ButtonStyle.secondary), self._button("Next", "badwords", discord.ButtonStyle.primary), self._cancel_button()]]
        if self.page == "badwords":
            return [[self._button("Add/Edit Bad Words", "badwords_modal", discord.ButtonStyle.primary), self._button("Clear Bad Words", "clear_badwords", discord.ButtonStyle.secondary)], self._nav("modules", "allowlists")]
        if self.page == "allowlists":
            return [[self._button("Allowed Domains", "domains_modal", discord.ButtonStyle.primary), self._button("Allowed Invites", "invites_modal", discord.ButtonStyle.primary)], self._nav("badwords", "normal_action")]
        if self.page == "normal_action":
            return [[self._action_select("default")], self._nav("allowlists", "security")]
        if self.page == "security":
            return [[self._security_select()], [self._button("Choose Action", "security_action", discord.ButtonStyle.primary), self._button("Back", "normal_action", discord.ButtonStyle.secondary), self._button("Next", "thresholds", discord.ButtonStyle.primary), self._cancel_button()]]
        if self.page == "thresholds":
            return [[self._button("Recommended Ladder", "threshold_recommended", discord.ButtonStyle.success), self._button("Custom Ladder", "threshold_modal", discord.ButtonStyle.primary), self._button("Toggle Escalation", "threshold_toggle", discord.ButtonStyle.secondary)], self._nav("security", "escalation")]
        if self.page == "escalation":
            return [[self._button("Recommended Escalation", "escalation_recommended", discord.ButtonStyle.success), self._button("Custom Escalation", "escalation_modal", discord.ButtonStyle.primary), self._button("Toggle Off/On", "escalation_toggle", discord.ButtonStyle.secondary)], self._nav("thresholds", "logs")]
        if self.page == "logs":
            return [[self._channel_select("log_channel", "Select AutoMod log channel")], [self._button("Toggle User DMs", "toggle_dm", discord.ButtonStyle.secondary), self._button("Toggle Public Feedback", "toggle_public", discord.ButtonStyle.secondary), self._button("Appeal Text", "appeal_modal", discord.ButtonStyle.primary)], self._nav("escalation", "bypass")]
        if self.page == "bypass":
            return [[self._role_select("bypass_roles", "Choose bypass roles")], [self._channel_select("bypass_channels", "Choose bypass channels")], [self._button("Auto-detect Staff Roles", "detect_staff", discord.ButtonStyle.success), self._button("Toggle Staff Perm Bypass", "toggle_staff_bypass", discord.ButtonStyle.secondary), self._button("Back", "logs", discord.ButtonStyle.secondary), self._button("Next", "emergency", discord.ButtonStyle.primary)]]
        if self.page == "emergency":
            return [[self._button("Toggle Lockdown", "toggle_lockdown", discord.ButtonStyle.secondary), self._button("Lockdown Duration", "lockdown_duration_modal", discord.ButtonStyle.primary), self._button("Apply Lockdown Preset", "apply_lockdown", discord.ButtonStyle.danger)], self._nav("bypass", "review")]
        if self.page == "review":
            return [[self._button("Run Doctor Refresh", "review", discord.ButtonStyle.secondary), self._button("Test Simulator", "simulator", discord.ButtonStyle.primary), self._button("Finish Setup", "finish", discord.ButtonStyle.success), self._cancel_button()], [self._button("Back", "emergency", discord.ButtonStyle.secondary)]]
        if self.page == "finished":
            return [[self._button("Open Status Tips", "status_tips", discord.ButtonStyle.primary), self._button("Delete Setup Channel", "delete_channel", discord.ButtonStyle.danger)]]
        if self.page == "cancelled":
            return [[self._button("Delete Setup Channel", "delete_channel", discord.ButtonStyle.danger)]]
        return [[self._cancel_button()]]

    def _button(self, label: str, action: str, style: discord.ButtonStyle) -> discord.ui.Button[Any]:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_action(interaction, action)

        button.callback = callback
        return button

    def _cancel_button(self) -> discord.ui.Button[Any]:
        return self._button("Cancel", "cancel", discord.ButtonStyle.danger)

    def _nav(self, previous: str, next_page: str) -> list[discord.ui.Item[Any]]:
        return [self._button("Back", previous, discord.ButtonStyle.secondary), self._button("Next", next_page, discord.ButtonStyle.primary), self._cancel_button()]

    def _select(self, action: str, placeholder: str, options: dict[str, str]) -> discord.ui.Select[Any]:
        select = discord.ui.Select(placeholder=placeholder, min_values=1, max_values=1)
        for value, label in options.items():
            select.add_option(label=label[:100], value=value, default=getattr(self.session.state, action, None) == value)

        async def callback(interaction: discord.Interaction) -> None:
            selected = select.values[0]
            if action == "setup_level":
                self.session.state.setup_level = selected
                self.session.state.settings["automod_setup_level"] = selected
                await self.session.refresh(interaction, "profile")
            elif action == "enforcement":
                self.session.state.apply_enforcement(selected)
                await self.session.refresh(interaction, "enforcement")

        select.callback = callback
        return select

    def _profile_select(self) -> discord.ui.Select[Any]:
        select = discord.ui.Select(placeholder="Choose server profile", min_values=1, max_values=1)
        for value, label in PROFILE_LABELS.items():
            description = str(AUTOMOD_PRESETS.get(value, {}).get("description", ""))[:100]
            select.add_option(label=label, value=value, description=description, default=self.session.state.profile == value)

        async def callback(interaction: discord.Interaction) -> None:
            self.session.state.apply_profile(select.values[0])
            self.session.state.apply_enforcement(self.session.state.enforcement)
            await self.session.refresh(interaction, "profile")

        select.callback = callback
        return select

    def _module_select(self) -> discord.ui.Select[Any]:
        modules = list(MODULE_SETTING_KEYS.items())
        select = discord.ui.Select(placeholder="Toggle modules", min_values=1, max_values=len(modules))
        for module, key in modules:
            select.add_option(label=module.replace("_", " ").title(), value=module, default=bool(self.session.state.settings.get(key, False)))

        async def callback(interaction: discord.Interaction) -> None:
            chosen = set(select.values)
            for module, key in MODULE_SETTING_KEYS.items():
                self.session.state.settings[key] = module in chosen
            await self.session.refresh(interaction, "modules")

        select.callback = callback
        return select

    def _action_select(self, key: str) -> discord.ui.Select[Any]:
        select = discord.ui.Select(placeholder="Choose action", min_values=1, max_values=1)
        for value, label, description in ACTION_CHOICES:
            select.add_option(label=label, value=value, description=description[:100])

        async def callback(interaction: discord.Interaction) -> None:
            await self._apply_action_choice(interaction, key, select.values[0])

        select.callback = callback
        return select

    def _security_select(self) -> discord.ui.Select[Any]:
        select = discord.ui.Select(placeholder="Choose security category to edit", min_values=1, max_values=1)
        for value, label, description in SECURITY_POLICIES:
            select.add_option(label=label, value=value, description=description[:100], default=self.session.state.current_policy_key == value)

        async def callback(interaction: discord.Interaction) -> None:
            self.session.state.current_policy_key = select.values[0]
            await self.session.refresh(interaction, "security")

        select.callback = callback
        return select

    def _role_select(self, action: str, placeholder: str) -> discord.ui.RoleSelect[Any]:
        select: discord.ui.RoleSelect[Any] = discord.ui.RoleSelect(placeholder=placeholder, min_values=0, max_values=10)

        async def callback(interaction: discord.Interaction) -> None:
            self.session.state.settings["automod_bypass_roles"] = [role.id for role in select.values]
            await self.session.refresh(interaction, "bypass")

        select.callback = callback
        return select

    def _channel_select(self, action: str, placeholder: str) -> discord.ui.ChannelSelect[Any]:
        select: discord.ui.ChannelSelect[Any] = discord.ui.ChannelSelect(placeholder=placeholder, channel_types=[discord.ChannelType.text], min_values=0, max_values=10 if action == "bypass_channels" else 1)

        async def callback(interaction: discord.Interaction) -> None:
            if action == "log_channel":
                channel = select.values[0] if select.values else None
                if channel is not None:
                    self.session.state.settings["automod_log_channel"] = channel.id
                    self.session.state.settings["log_channel_automod"] = channel.id
                await self.session.refresh(interaction, "logs")
            elif action == "bypass_channels":
                self.session.state.settings["automod_bypass_channels"] = [channel.id for channel in select.values]
                await self.session.refresh(interaction, "bypass")

        select.callback = callback
        return select

    async def _handle_action(self, interaction: discord.Interaction, action: str) -> None:
        if action in {"welcome", "profile", "enforcement", "modules", "badwords", "allowlists", "normal_action", "security", "thresholds", "escalation", "logs", "bypass", "emergency", "review"}:
            await self.session.refresh(interaction, action)
            return
        if action == "cancel":
            await self.session.cancel(interaction)
            return
        if action == "finish":
            await self.session.apply(interaction)
            return
        if action == "delete_channel":
            await self.session.delete_channel(interaction)
            return
        if action == "toggle_safe":
            self.session.state.settings["automod_safe_mode"] = not bool(self.session.state.settings.get("automod_safe_mode", False))
            await self.session.refresh(interaction, "enforcement")
            return
        if action == "recommended_modules":
            for key in MODULE_SETTING_KEYS.values():
                self.session.state.settings[key] = True
            await self.session.refresh(interaction, "modules")
            return
        if action == "badwords_modal":
            await interaction.response.send_modal(BadWordsModal(self.session))
            return
        if action == "clear_badwords":
            self.session.state.settings["automod_badwords"] = []
            await self.session.refresh(interaction, "badwords")
            return
        if action == "domains_modal":
            await interaction.response.send_modal(LineListModal(self.session, "Allowed Domains", "automod_whitelisted_domains", "allowlists", limit=50))
            return
        if action == "invites_modal":
            await interaction.response.send_modal(LineListModal(self.session, "Allowed Invite Codes", "automod_allowed_invites", "allowlists", limit=50))
            return
        if action == "security_action":
            key = self.session.state.current_policy_key or "scams"
            self.session.state.current_policy_key = key
            await interaction.response.send_message(f"Choose an action for **{key}** below.", view=AutoModActionChoiceView(self.session, key), ephemeral=True)
            return
        if action == "threshold_recommended":
            self.session.state.settings.update({"warn_thresholds_enabled": True, "warn_threshold_mute": 3, "warn_mute_duration": 3600, "warn_threshold_kick": 5, "warn_threshold_ban": 7})
            await self.session.refresh(interaction, "thresholds")
            return
        if action == "threshold_modal":
            await interaction.response.send_modal(WarningThresholdModal(self.session))
            return
        if action == "threshold_toggle":
            self.session.state.settings["warn_thresholds_enabled"] = not bool(self.session.state.settings.get("warn_thresholds_enabled", True))
            await self.session.refresh(interaction, "thresholds")
            return
        if action == "escalation_recommended":
            self.session.state.settings.update({"automod_escalation_enabled": True, "automod_escalation_window": 86400, "automod_escalation": [{"offenses": 2, "action": "timeout", "duration": 1800}, {"offenses": 4, "action": "kick", "duration": 0}, {"offenses": 6, "action": "ban", "duration": 0}]})
            await self.session.refresh(interaction, "escalation")
            return
        if action == "escalation_modal":
            await interaction.response.send_modal(EscalationModal(self.session))
            return
        if action == "escalation_toggle":
            self.session.state.settings["automod_escalation_enabled"] = not bool(self.session.state.settings.get("automod_escalation_enabled", True))
            await self.session.refresh(interaction, "escalation")
            return
        if action == "toggle_dm":
            self.session.state.settings["automod_notify_users"] = not bool(self.session.state.settings.get("automod_notify_users", True))
            await self.session.refresh(interaction, "logs")
            return
        if action == "toggle_public":
            self.session.state.settings["automod_public_feedback"] = not bool(self.session.state.settings.get("automod_public_feedback", False))
            await self.session.refresh(interaction, "logs")
            return
        if action == "appeal_modal":
            await interaction.response.send_modal(AppealModal(self.session))
            return
        if action == "detect_staff":
            staff_words = ("admin", "mod", "moderator", "staff", "manager", "helper", "trial", "supervisor")
            roles = [role.id for role in self.session.guild.roles if any(word in role.name.casefold() for word in staff_words)]
            self.session.state.settings["automod_bypass_roles"] = sorted(set(roles))[:20]
            await self.session.refresh(interaction, "bypass")
            return
        if action == "toggle_staff_bypass":
            self.session.state.settings["automod_bypass_staff"] = not bool(self.session.state.settings.get("automod_bypass_staff", True))
            await self.session.refresh(interaction, "bypass")
            return
        if action == "toggle_lockdown":
            self.session.state.settings["automod_lockdown_enabled"] = not bool(self.session.state.settings.get("automod_lockdown_enabled", False))
            await self.session.refresh(interaction, "emergency")
            return
        if action == "lockdown_duration_modal":
            await interaction.response.send_modal(DurationOnlyModal(self.session, "Lockdown Duration", "automod_lockdown_duration", "emergency"))
            return
        if action == "apply_lockdown":
            preserved = self.session.state._portable_preserved_settings()
            self.session.state.settings.update(apply_preset("lockdown"))
            self.session.state.settings.update(preserved)
            await self.session.refresh(interaction, "emergency")
            return
        if action == "simulator":
            await interaction.response.send_message(self._simulator_text(), ephemeral=True)
            return
        if action == "status_tips":
            await interaction.response.send_message("Next commands: `/automod status`, `/automod doctor`, `/automod scan channel:#general amount:100`.", ephemeral=True)
            return

    async def _apply_action_choice(self, interaction: discord.Interaction, key: str, choice: str) -> None:
        if choice == "timeout":
            await interaction.response.send_modal(ActionDurationModal(self.session, key))
            return
        if choice == "ban":
            await interaction.response.send_modal(BanConfirmModal(self.session, key))
            return
        delete = choice != "log"
        self.session.state.set_policy(key, choice, delete=delete)
        if key == "default":
            self.session.state.settings["automod_punishment"] = "log" if choice == "delete" else choice
        elif key in {"raid"}:
            self.session.state.settings["automod_raid_punishment"] = "log" if choice == "delete" else choice
        elif key in {"new_accounts"}:
            self.session.state.settings["automod_newaccount_join_action"] = "log" if choice == "delete" else choice
        else:
            self.session.state.settings["automod_security_punishment"] = "log" if choice == "delete" else choice
        await self.session.refresh(interaction, "normal_action" if key == "default" else "security")

    def _progress_line(self) -> str:
        return f"Profile: **{PROFILE_LABELS.get(self.session.state.profile, self.session.state.profile)}** • Mode: **{self.session.state.enforcement}** • Level: **{self.session.state.setup_level}**"

    def _profile_summary(self) -> str:
        rows = []
        for key, label in PROFILE_LABELS.items():
            rows.append(f"• **{label}** — {AUTOMOD_PRESETS.get(key, {}).get('description', '')}")
        return "\n".join(rows[:8])

    def _security_summary(self) -> str:
        policies = self.session.state.settings.get("automod_rule_actions", {}) or {}
        rows = []
        for key, label, _ in SECURITY_POLICIES:
            marker = "▶️" if self.session.state.current_policy_key == key else "•"
            rows.append(f"{marker} **{label}:** {_action_label(policies.get(key))}")
        return "\n".join(rows)

    def _escalation_summary(self) -> str:
        s = self.session.state.settings
        if not s.get("automod_escalation_enabled", True):
            return "Escalation is disabled."
        rows = [f"Window: **{_duration_label(s.get('automod_escalation_window'))}**"]
        for step in s.get("automod_escalation", []) or []:
            rows.append(f"• {step.get('offenses')} offenses → {step.get('action')} {_duration_label(step.get('duration')) if step.get('duration') else ''}".strip())
        return "\n".join(rows)

    def _final_summary(self) -> str:
        s = self.session.state.settings
        modules = [name for name, key in MODULE_SETTING_KEYS.items() if s.get(key, False)]
        return (
            f"Profile: **{PROFILE_LABELS.get(self.session.state.profile, self.session.state.profile)}**\n"
            f"Enforcement: **{self.session.state.enforcement}**\n"
            f"Modules: **{len(modules)}** enabled\n"
            f"Bad words: **{len(s.get('automod_badwords', []) or [])}**\n"
            f"Normal action: **{s.get('automod_punishment', 'warn')}**\n"
            f"Security action: **{s.get('automod_security_punishment', 'timeout')}**\n"
            f"Rule policies: **{len(s.get('automod_rule_actions', {}) or {})}**\n"
            f"Log channel: {f'<#{s.get('automod_log_channel')}>' if s.get('automod_log_channel') else '**not set**'}"
        )

    def _doctor_short(self, report: Any) -> str:
        items = report.failures[:2] or report.warnings[:2] or report.recommendations[:2]
        return "\n".join(f"• {item}" for item in items) if items else "• No major setup issues detected."

    def _mention_ids(self, values: Any, *, role: bool = False, channel: bool = False) -> str:
        ids = list(values or [])[:10]
        if not ids:
            return "none"
        if role:
            return ", ".join(f"<@&{int(value)}>" for value in ids)
        if channel:
            return ", ".join(f"<#{int(value)}>" for value in ids)
        return ", ".join(f"<@{int(value)}>" for value in ids)

    def _simulator_text(self) -> str:
        s = self.session.state.settings
        policies = s.get("automod_rule_actions", {}) or {}
        examples = [
            ("Scam link", "scams"),
            ("Blocked invite", "invites"),
            ("Mass mentions", "mentions"),
            ("Emoji spam", "emoji_spam"),
            ("Attachment burst", "attachments"),
        ]
        rows = ["**AutoMod Test Simulator**", "No punishments are executed here. This previews configured actions."]
        for label, key in examples:
            rows.append(f"• {label}: `{_action_label(policies.get(key))}`")
        return "\n".join(rows)


class AutoModActionChoiceView(discord.ui.View):
    def __init__(self, session: AutoModWizardSession, key: str) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.key = key
        select = discord.ui.Select(placeholder=f"Action for {key}", min_values=1, max_values=1)
        for value, label, description in ACTION_CHOICES:
            select.add_option(label=label, value=value, description=description[:100])
        select.callback = self._callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.session.user_id

    async def _callback(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        choice = select.values[0] if isinstance(select, discord.ui.Select) else "log"
        if choice == "timeout":
            await interaction.response.send_modal(ActionDurationModal(self.session, self.key))
            return
        if choice == "ban":
            await interaction.response.send_modal(BanConfirmModal(self.session, self.key))
            return
        delete = choice != "log"
        self.session.state.set_policy(self.key, choice, delete=delete)
        if self.key == "default":
            self.session.state.settings["automod_punishment"] = "log" if choice == "delete" else choice
        elif self.key == "raid":
            self.session.state.settings["automod_raid_punishment"] = "log" if choice == "delete" else choice
        elif self.key == "new_accounts":
            self.session.state.settings["automod_newaccount_join_action"] = "log" if choice == "delete" else choice
        elif self.key in {"scams", "links", "invites", "mentions", "attachments", "emoji_spam"}:
            self.session.state.settings["automod_security_punishment"] = "log" if choice == "delete" else choice
        await interaction.response.edit_message(content=f"Saved `{self.key}` policy as `{choice}`.", view=None)
        if self.session.message:
            view = AutoModWizardView(self.session, "security")
            await self.session.message.edit(view=view.build_layout())


class BadWordsModal(discord.ui.Modal, title="Blocked Words / Phrases"):
    words = discord.ui.TextInput(label="One word or phrase per line", style=discord.TextStyle.paragraph, required=False, max_length=3000)

    def __init__(self, session: AutoModWizardSession) -> None:
        super().__init__(timeout=300)
        self.session = session
        self.words.default = "\n".join((session.state.settings.get("automod_badwords", []) or [])[:40])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items = _clean_lines(str(self.words.value), limit=100)
        self.session.state.settings["automod_badwords"] = items
        self.session.state.settings["automod_badwords_enabled"] = bool(items) or self.session.state.settings.get("automod_badwords_enabled", True)
        await self.session.refresh(interaction, "badwords")


class LineListModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="One item per line", style=discord.TextStyle.paragraph, required=False, max_length=2000)

    def __init__(self, session: AutoModWizardSession, title: str, setting_key: str, return_page: str, *, limit: int = 50) -> None:
        super().__init__(title=title, timeout=300)
        self.session = session
        self.setting_key = setting_key
        self.return_page = return_page
        self.limit = limit
        self.value.default = "\n".join((session.state.settings.get(setting_key, []) or [])[:limit])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.state.settings[self.setting_key] = _clean_lines(str(self.value.value), limit=self.limit, min_length=2, max_length=100)
        await self.session.refresh(interaction, self.return_page)


class ActionDurationModal(discord.ui.Modal, title="Timeout Duration"):
    duration = discord.ui.TextInput(label="How long? Examples: 10m, 1h, 6h, 1d, 7d", default="1h", max_length=16)

    def __init__(self, session: AutoModWizardSession, policy_key: str) -> None:
        super().__init__(timeout=300)
        self.session = session
        self.policy_key = policy_key

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = parse_duration(str(self.duration.value), minimum=60, maximum=2419200)
        if seconds is None:
            await interaction.response.send_message("Invalid duration. Use `10m`, `1h`, `6h`, or `7d`.", ephemeral=True)
            return
        self.session.state.set_policy(self.policy_key, "timeout", duration=seconds, delete=True)
        if self.policy_key == "default":
            self.session.state.settings["automod_punishment"] = "timeout"
            self.session.state.settings["automod_mute_duration"] = seconds
        elif self.policy_key == "raid":
            self.session.state.settings["automod_raid_punishment"] = "timeout"
            self.session.state.settings["automod_mute_duration"] = seconds
        elif self.policy_key == "new_accounts":
            self.session.state.settings["automod_newaccount_join_action"] = "timeout"
            self.session.state.settings["automod_mute_duration"] = seconds
        else:
            self.session.state.settings["automod_security_punishment"] = "timeout"
            self.session.state.settings["automod_mute_duration"] = seconds
        await self.session.refresh(interaction, "normal_action" if self.policy_key == "default" else "security")


class BanConfirmModal(discord.ui.Modal, title="Confirm Ban Policy"):
    confirmation = discord.ui.TextInput(label="Type BAN to confirm", max_length=8)
    delete_days = discord.ui.TextInput(label="Delete message history days: 0, 1, 3, or 7", default="1", max_length=1)

    def __init__(self, session: AutoModWizardSession, policy_key: str) -> None:
        super().__init__(timeout=300)
        self.session = session
        self.policy_key = policy_key

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value).strip().upper() != "BAN":
            await interaction.response.send_message("Ban policy was not saved because confirmation did not match `BAN`.", ephemeral=True)
            return
        try:
            days = max(0, min(7, int(str(self.delete_days.value).strip() or "1")))
        except ValueError:
            days = 1
        self.session.state.set_policy(self.policy_key, "ban", delete=True, delete_days=days)
        if self.policy_key == "default":
            self.session.state.settings["automod_punishment"] = "ban"
        elif self.policy_key == "raid":
            self.session.state.settings["automod_raid_punishment"] = "ban"
        elif self.policy_key == "new_accounts":
            self.session.state.settings["automod_newaccount_join_action"] = "ban"
        else:
            self.session.state.settings["automod_security_punishment"] = "ban"
        self.session.state.settings["automod_ban_delete_days"] = days
        await self.session.refresh(interaction, "normal_action" if self.policy_key == "default" else "security")


class WarningThresholdModal(discord.ui.Modal, title="Warning Thresholds"):
    mute_at = discord.ui.TextInput(label="Timeout after how many warnings?", default="3", max_length=3)
    mute_duration = discord.ui.TextInput(label="Timeout duration", default="1h", max_length=16)
    kick_at = discord.ui.TextInput(label="Kick after how many warnings?", default="5", max_length=3)
    ban_at = discord.ui.TextInput(label="Ban after how many warnings?", default="7", max_length=3)

    def __init__(self, session: AutoModWizardSession) -> None:
        super().__init__(timeout=300)
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            mute_at = max(0, min(100, int(str(self.mute_at.value).strip() or "0")))
            kick_at = max(0, min(100, int(str(self.kick_at.value).strip() or "0")))
            ban_at = max(0, min(100, int(str(self.ban_at.value).strip() or "0")))
        except ValueError:
            await interaction.response.send_message("Thresholds must be numbers.", ephemeral=True)
            return
        duration = parse_duration(str(self.mute_duration.value), minimum=60, maximum=2419200)
        if mute_at and duration is None:
            await interaction.response.send_message("Timeout duration must look like `30m`, `1h`, or `7d`.", ephemeral=True)
            return
        self.session.state.settings.update({"warn_thresholds_enabled": True, "warn_threshold_mute": mute_at, "warn_mute_duration": duration or 3600, "warn_threshold_kick": kick_at, "warn_threshold_ban": ban_at})
        await self.session.refresh(interaction, "thresholds")


class EscalationModal(discord.ui.Modal, title="AutoMod Offense Escalation"):
    window = discord.ui.TextInput(label="Tracking window", default="1d", max_length=16)
    steps = discord.ui.TextInput(label="Steps: 2:timeout:30m, 4:kick, 6:ban", style=discord.TextStyle.paragraph, default="2:timeout:30m\n4:kick\n6:ban", max_length=1000)

    def __init__(self, session: AutoModWizardSession) -> None:
        super().__init__(timeout=300)
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        window = parse_duration(str(self.window.value), minimum=60, maximum=2592000)
        if window is None:
            await interaction.response.send_message("Window must look like `1h`, `1d`, or `7d`.", ephemeral=True)
            return
        parsed_steps: list[dict[str, Any]] = []
        for raw in str(self.steps.value).replace(",", "\n").splitlines():
            parts = [part.strip().lower() for part in raw.split(":") if part.strip()]
            if len(parts) < 2:
                continue
            try:
                offenses = max(1, min(100, int(parts[0])))
            except ValueError:
                continue
            action = "timeout" if parts[1] == "mute" else parts[1]
            if action not in {"log", "warn", "timeout", "kick", "ban"}:
                continue
            duration = parse_duration(parts[2], minimum=60, maximum=2419200) if len(parts) >= 3 else None
            parsed_steps.append({"offenses": offenses, "action": action, "duration": duration or 0})
        if not parsed_steps:
            await interaction.response.send_message("No valid escalation steps found. Example: `2:timeout:30m`.", ephemeral=True)
            return
        self.session.state.settings.update({"automod_escalation_enabled": True, "automod_escalation_window": window, "automod_escalation": parsed_steps[:8]})
        await self.session.refresh(interaction, "escalation")


class AppealModal(discord.ui.Modal, title="Appeal / Modmail Instructions"):
    text = discord.ui.TextInput(label="Optional DM appeal instructions", style=discord.TextStyle.paragraph, required=False, max_length=800)

    def __init__(self, session: AutoModWizardSession) -> None:
        super().__init__(timeout=300)
        self.session = session
        self.text.default = str(session.state.settings.get("automod_appeal_instructions") or "")[:800]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.state.settings["automod_appeal_instructions"] = str(self.text.value).strip()[:800]
        await self.session.refresh(interaction, "logs")


class DurationOnlyModal(discord.ui.Modal):
    duration = discord.ui.TextInput(label="Duration", default="30m", max_length=16)

    def __init__(self, session: AutoModWizardSession, title: str, setting_key: str, return_page: str) -> None:
        super().__init__(title=title, timeout=300)
        self.session = session
        self.setting_key = setting_key
        self.return_page = return_page

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = parse_duration(str(self.duration.value), minimum=60, maximum=2592000)
        if seconds is None:
            await interaction.response.send_message("Invalid duration. Use `10m`, `1h`, `1d`, etc.", ephemeral=True)
            return
        self.session.state.settings[self.setting_key] = seconds
        await self.session.refresh(interaction, self.return_page)