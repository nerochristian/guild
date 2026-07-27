"""AutoMod configuration health checks.

The doctor is intentionally deterministic and local. It does not need API calls
or AI, so it can run quickly during setup, from `/automod doctor`, and inside
the wizard final review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import discord

from config import Config
from .config import MODULE_SETTING_KEYS, PUNISHMENTS


@dataclass(slots=True)
class AutoModHealthReport:
    score: int
    passed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        if self.score >= 90:
            return "Excellent"
        if self.score >= 75:
            return "Good"
        if self.score >= 55:
            return "Needs Work"
        return "Broken"

    def to_embed(self) -> discord.Embed:
        color = getattr(Config, "COLOR_SUCCESS", 0x22C55E)
        if self.score < 75:
            color = getattr(Config, "COLOR_WARNING", 0xF59E0B)
        if self.score < 55:
            color = getattr(Config, "COLOR_ERROR", 0xEF4444)
        embed = discord.Embed(
            title=f"AutoMod Doctor — {self.score}/100 ({self.grade})",
            description="Configuration health, permission safety, and setup recommendations.",
            color=color,
        )
        embed.add_field(name="Passing", value=_bullet(self.passed, "No passing checks yet."), inline=False)
        if self.failures:
            embed.add_field(name="Fix Now", value=_bullet(self.failures), inline=False)
        if self.warnings:
            embed.add_field(name="Warnings", value=_bullet(self.warnings), inline=False)
        if self.recommendations:
            embed.add_field(name="Recommended Next Steps", value=_bullet(self.recommendations), inline=False)
        return embed


def _bullet(items: Iterable[str], empty: str = "None") -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return empty
    return "\n".join(f"• {item}" for item in values[:8])[:1024]


def _valid_action(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in PUNISHMENTS or raw == "delete_only"


def _uses_action(settings: dict[str, Any], actions: set[str]) -> bool:
    keys = ("automod_punishment", "automod_security_punishment", "automod_raid_punishment", "automod_newaccount_join_action")
    for key in keys:
        raw = str(settings.get(key, "") or "").lower()
        if raw in actions:
            return True
    for policy in (settings.get("automod_rule_actions", {}) or {}).values():
        if isinstance(policy, dict) and str(policy.get("action", "") or "").lower() in actions:
            return True
    for step in settings.get("automod_escalation", []) or []:
        if isinstance(step, dict) and str(step.get("action", "") or "").lower() in actions:
            return True
    return False


def build_automod_health_report(guild: discord.Guild, settings: dict[str, Any]) -> AutoModHealthReport:
    score = 100
    passed: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []
    recommendations: list[str] = []

    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        score -= 25
        failures.append("Bot member is unavailable, so permission checks cannot run.")
    else:
        perms = bot_member.guild_permissions
        if perms.manage_messages:
            passed.append("Bot can delete violating messages.")
        elif settings.get("automod_delete_violations", True):
            score -= 16
            failures.append("Bot is missing Manage Messages, so message deletion will fail.")

        if _uses_action(settings, {"timeout", "mute"}):
            if perms.moderate_members:
                passed.append("Bot can timeout members.")
            else:
                score -= 15
                failures.append("Timeout/mute is configured but bot is missing Moderate Members.")

        if _uses_action(settings, {"kick"}):
            if perms.kick_members:
                passed.append("Bot can kick members when escalation requires it.")
            else:
                score -= 10
                failures.append("Kick is configured but bot is missing Kick Members.")

        if _uses_action(settings, {"ban"}):
            if perms.ban_members:
                passed.append("Bot can ban members when configured.")
            else:
                score -= 14
                failures.append("Ban is configured but bot is missing Ban Members.")

    log_channel_id = settings.get("automod_log_channel") or settings.get("log_channel_automod")
    log_channel = None
    try:
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None
    except (TypeError, ValueError):
        log_channel = None
    if log_channel is not None:
        passed.append(f"AutoMod logs are configured for {getattr(log_channel, 'mention', '#logs')}.")
    else:
        score -= 12
        warnings.append("No AutoMod log channel is configured.")

    if settings.get("automod_enabled", True):
        passed.append("AutoMod is globally enabled.")
    else:
        score -= 20
        failures.append("AutoMod is globally disabled.")

    enabled_modules = [name for name, key in MODULE_SETTING_KEYS.items() if settings.get(key, False)]
    if len(enabled_modules) >= 8:
        passed.append(f"{len(enabled_modules)} protection modules are enabled.")
    elif enabled_modules:
        score -= 10
        warnings.append(f"Only {len(enabled_modules)} protection modules are enabled.")
    else:
        score -= 25
        failures.append("No AutoMod protection modules are enabled.")

    if settings.get("automod_badwords_enabled", True) and not settings.get("automod_badwords"):
        score -= 3
        recommendations.append("Add server-specific blocked words/phrases in the wizard or `/automod badwords add`.")

    if not settings.get("automod_bypass_roles") and settings.get("automod_bypass_staff", True):
        recommendations.append("Add staff/mod roles as explicit bypass roles for clearer multi-server portability.")

    if not settings.get("automod_allowed_invites") and settings.get("automod_invites_enabled", True):
        recommendations.append("Allowlist your own server invite if members are allowed to share it.")

    if not settings.get("automod_whitelisted_domains") and str(settings.get("automod_links_mode", "dangerous")) == "allowlist":
        score -= 8
        warnings.append("Links are in allowlist mode but no custom domains are allowlisted.")

    if settings.get("automod_safe_mode", False):
        warnings.append("Safe mode is enabled: AutoMod will log instead of punishing.")

    rule_actions = settings.get("automod_rule_actions", {}) or {}
    if isinstance(rule_actions, dict):
        invalid = [name for name, policy in rule_actions.items() if isinstance(policy, dict) and not _valid_action(policy.get("action"))]
        if invalid:
            score -= 6
            warnings.append(f"Invalid per-rule actions: {', '.join(invalid[:5])}.")
        elif rule_actions:
            passed.append(f"{len(rule_actions)} per-rule action policies configured.")

    try:
        mute_at = int(settings.get("warn_threshold_mute") or 0)
        kick_at = int(settings.get("warn_threshold_kick") or 0)
        ban_at = int(settings.get("warn_threshold_ban") or 0)
        if settings.get("warn_thresholds_enabled", True) and mute_at and kick_at and ban_at and mute_at < kick_at < ban_at:
            passed.append("Warning escalation ladder is ordered safely.")
        elif settings.get("warn_thresholds_enabled", True):
            score -= 4
            warnings.append("Warning thresholds should usually increase from timeout → kick → ban.")
    except (TypeError, ValueError):
        score -= 6
        warnings.append("Warning thresholds contain invalid numbers.")

    if _uses_action(settings, {"ban"}):
        recommendations.append("Review ban policies carefully; the wizard requires confirmation before adding new ban rules.")

    score = max(0, min(100, score))
    return AutoModHealthReport(score=score, passed=passed, warnings=warnings, failures=failures, recommendations=recommendations)