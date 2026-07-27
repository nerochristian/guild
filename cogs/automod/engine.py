"""AutoMod rule engine and runtime telemetry."""

from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Optional

import discord

from .config import MODULE_SETTING_KEYS
from .models import Action, Category, RuleMatch, Severity, ViolationRecord
from .rules import ALL_RULES, Rule
from .utils import id_list
from utils.checks import is_bot_owner_id


class AutoModEngine:
    def __init__(self) -> None:
        self.rules: list[Rule] = sorted((factory() for factory in ALL_RULES), key=lambda rule: rule.priority, reverse=True)
        self.stats: Counter[str] = Counter()
        self.rule_hits: Counter[str] = Counter()
        self.recent: Deque[ViolationRecord] = deque(maxlen=300)
        self._last_trigger: dict[tuple[int, int, str], float] = {}
        self._joins: dict[int, Deque[tuple[float, int]]] = defaultdict(deque)
        self._offenses: dict[tuple[int, int], Deque[float]] = defaultdict(deque)

    async def evaluate(self, message: discord.Message, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        if not dry_run:
            self.stats["messages_checked"] += 1
        for rule in self.rules:
            if not settings.get(rule.setting_key, False):
                continue
            try:
                match = await rule.check(message, settings, dry_run=dry_run)
            except Exception:
                self.stats[f"{rule.name}_errors"] += 1
                continue
            if match is None:
                continue
            if dry_run:
                return match
            if self._is_cooling_down(message, match, settings):
                return None
            self.stats["violations_detected"] += 1
            self.rule_hits[match.rule] += 1
            return match
        return None

    def evaluate_join(self, member: discord.Member, settings: dict[str, Any]) -> list[RuleMatch]:
        matches: list[RuleMatch] = []
        now = time.monotonic()
        if settings.get("automod_newaccount_enabled", False):
            age_days = max(0, int((discord.utils.utcnow() - member.created_at).total_seconds() // 86400))
            threshold = max(0, min(365, int(settings.get("automod_newaccount_days", 7))))
            if threshold and age_days < threshold:
                matches.append(
                    RuleMatch(
                        "new_accounts",
                        f"New member account is only {age_days} day{'s' if age_days != 1 else ''} old",
                        Severity.INFO,
                        Category.IDENTITY,
                        delete_message=False,
                        metadata={"age_days": age_days},
                    )
                )
        if settings.get("automod_raid_enabled", False):
            window = max(5, min(300, int(settings.get("automod_raid_join_window", 20))))
            threshold = max(2, min(100, int(settings.get("automod_raid_join_threshold", 8))))
            entries = self._joins[member.guild.id]
            while entries and now - entries[0][0] > window:
                entries.popleft()
            entries.append((now, member.id))
            if len(entries) >= threshold:
                matches.append(
                    RuleMatch(
                        "raid",
                        f"Possible raid: {len(entries)} joins in {window}s",
                        Severity.CRITICAL,
                        Category.RAID,
                        delete_message=False,
                        metadata={"count": len(entries), "window": window, "member_ids": [user_id for _, user_id in entries]},
                    )
                )
        return matches

    def bypass_reason(self, message: discord.Message, settings: dict[str, Any]) -> Optional[str]:
        author = message.author
        if not isinstance(author, discord.Member) or message.guild is None:
            return "not a guild member"
        if author.bot:
            return "bot account"
        if is_bot_owner_id(author.id):
            return "bot owner"
        if author.id in id_list(settings.get("automod_bypass_users", [])):
            return "whitelisted user"
        if settings.get("automod_bypass_staff", True):
            perms = author.guild_permissions
            if perms.administrator or perms.manage_guild or perms.manage_messages:
                return "staff permissions"
        role_ids = {role.id for role in author.roles}
        bypass_roles = id_list(settings.get("automod_bypass_roles", [])) | id_list(settings.get("ignored_roles", []))
        if role_ids & bypass_roles:
            return "whitelisted role"
        channel = message.channel
        channel_ids = id_list(settings.get("automod_bypass_channels", [])) | id_list(settings.get("ignored_channels", []))
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        if channel_id in channel_ids or parent_id in channel_ids:
            return "whitelisted channel"
        return None

    def resolve_action(self, match: RuleMatch, settings: dict[str, Any]) -> Action:
        if settings.get("automod_safe_mode", False):
            return Action.LOG
        override = self._rule_policy(match, settings)
        if override:
            raw = str(override.get("action", "") or "").strip().lower()
            if raw:
                raw = {"mute": "timeout", "delete": "log", "delete_only": "log", "none": "none"}.get(raw, raw)
                try:
                    return Action(raw)
                except ValueError:
                    pass
        if match.category is Category.IDENTITY:
            raw = str(settings.get("automod_newaccount_join_action", "log"))
        elif match.category is Category.RAID:
            raw = str(settings.get("automod_raid_punishment", "timeout"))
        elif match.category is Category.SECURITY:
            raw = str(settings.get("automod_security_punishment", "timeout"))
        else:
            raw = str(settings.get("automod_punishment", "warn"))
        raw = {"mute": "timeout", "none": "none"}.get(raw.strip().lower(), raw.strip().lower())
        try:
            return Action(raw)
        except ValueError:
            return Action.TIMEOUT if match.category in {Category.SECURITY, Category.RAID} else Action.WARN

    def resolve_duration(self, match: RuleMatch, settings: dict[str, Any]) -> Optional[int]:
        override = self._rule_policy(match, settings)
        if not override:
            return None
        try:
            duration = int(override.get("duration") or 0)
        except (TypeError, ValueError):
            return None
        return max(60, min(2419200, duration)) if duration else None

    def should_delete_message(self, match: RuleMatch, settings: dict[str, Any]) -> bool:
        if not match.delete_message or not settings.get("automod_delete_violations", True):
            return False
        override = self._rule_policy(match, settings)
        if override and "delete" in override:
            return bool(override.get("delete"))
        return True

    def _rule_policy(self, match: RuleMatch, settings: dict[str, Any]) -> Optional[dict[str, Any]]:
        policies = settings.get("automod_rule_actions", {}) or {}
        if not isinstance(policies, dict):
            return None
        candidates = (
            match.rule,
            match.category.value,
            "security" if match.category is Category.SECURITY else "",
            "raid" if match.category is Category.RAID else "",
            "identity" if match.category is Category.IDENTITY else "",
            "default",
        )
        for key in candidates:
            if not key:
                continue
            value = policies.get(key)
            if isinstance(value, dict):
                return value
        return None

    def escalated_action(self, guild_id: int, user_id: int, base_action: Action, settings: dict[str, Any]) -> tuple[Action, Optional[int], int]:
        window = max(60, min(2592000, int(settings.get("automod_escalation_window", 86400))))
        now = time.monotonic()
        entries = self._offenses[(int(guild_id), int(user_id))]
        while entries and now - entries[0] > window:
            entries.popleft()
        entries.append(now)
        count = len(entries)
        if not settings.get("automod_escalation_enabled", True):
            return base_action, None, count
        selected_action = base_action
        selected_duration: Optional[int] = None
        for step in sorted(settings.get("automod_escalation", []) or [], key=lambda item: int(item.get("offenses", 0))):
            try:
                if count >= int(step.get("offenses", 0)):
                    selected_action = Action(str(step.get("action", base_action.value)).lower())
                    selected_duration = int(step.get("duration") or 0) or None
            except (TypeError, ValueError):
                continue
        return selected_action, selected_duration, count

    def record_action(self, message: discord.Message, match: RuleMatch, action: Action) -> None:
        self.recent.append(
            ViolationRecord(
                guild_id=message.guild.id if message.guild else 0,
                user_id=message.author.id,
                channel_id=getattr(message.channel, "id", None),
                rule=match.rule,
                reason=match.reason,
                action=action.value,
                severity=match.severity.name,
                created_at=time.time(),
            )
        )

    def recent_for(self, guild_id: int, user_id: Optional[int] = None) -> list[ViolationRecord]:
        return [item for item in reversed(self.recent) if item.guild_id == guild_id and (user_id is None or item.user_id == user_id)]

    def enabled_modules(self, settings: dict[str, Any]) -> list[str]:
        return [module for module, key in MODULE_SETTING_KEYS.items() if settings.get(key, False)]

    def _is_cooling_down(self, message: discord.Message, match: RuleMatch, settings: dict[str, Any]) -> bool:
        guild_id = message.guild.id if message.guild else 0
        user_id = message.author.id
        cooldown = 3600 if match.rule == "new_accounts" else max(1, min(300, int(settings.get("automod_violation_cooldown", 8))))
        key = (guild_id, user_id, match.rule)
        now = time.monotonic()
        if now - self._last_trigger.get(key, 0) < cooldown:
            return True
        self._last_trigger[key] = now
        return False

    def prune(self) -> None:
        now = time.monotonic()
        for rule in self.rules:
            rule.prune(now)
        for key, created_at in list(self._last_trigger.items()):
            if now - created_at > 3600:
                self._last_trigger.pop(key, None)
        for key, entries in list(self._offenses.items()):
            while entries and now - entries[0] > 2592000:
                entries.popleft()
            if not entries:
                self._offenses.pop(key, None)
        for guild_id, entries in list(self._joins.items()):
            while entries and now - entries[0][0] > 300:
                entries.popleft()
            if not entries:
                self._joins.pop(guild_id, None)

    async def close(self) -> None:
        return None
