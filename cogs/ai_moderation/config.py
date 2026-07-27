"""
Guild configuration models and defaults.

Each guild can configure rules, punishment limits, protected users,
authorization roles, AI thresholds, and more. This module defines the
data structures and sensible defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

from .models import ActionType, Rule, SeverityLevel


# =============================================================================
# Punishment matrix — maps severity + offense count to an action
# =============================================================================


@dataclass
class PunishmentMatrix:
    """Escalation ladder per severity level.

    The matrix maps (severity, offense_count) → (action, duration_seconds).
    Offense counts are 1-indexed: 1 = first offense, 2 = second, etc.
    """

    # Key: severity value, Value: list of (action, duration) tuples indexed by offense number.
    _matrix: Dict[str, List[tuple[ActionType, Optional[int]]]] = field(default_factory=lambda: {
        SeverityLevel.MINOR.value: [
            (ActionType.WARN, None),                    # 1st
            (ActionType.TIMEOUT, 600),                  # 2nd: 10 min
            (ActionType.TIMEOUT, 3600),                 # 3rd: 1 hour
            (ActionType.TIMEOUT, 21600),                # 4th: 6 hours
            (ActionType.KICK, None),                    # 5th+
        ],
        SeverityLevel.MODERATE.value: [
            (ActionType.TIMEOUT, 3600),                 # 1st: 1 hour
            (ActionType.TIMEOUT, 21600),                # 2nd: 6 hours
            (ActionType.TIMEOUT, 86400),                # 3rd: 1 day
            (ActionType.KICK, None),                    # 4th
            (ActionType.BAN, None),                     # 5th+
        ],
        SeverityLevel.SEVERE.value: [
            (ActionType.TIMEOUT, 86400),                # 1st: 1 day
            (ActionType.KICK, None),                    # 2nd
            (ActionType.TEMP_BAN, 604800),              # 3rd: 7 days
            (ActionType.BAN, None),                     # 4th+
        ],
        SeverityLevel.CRITICAL.value: [
            (ActionType.TEMP_BAN, 604800),              # 1st: 7 days
            (ActionType.BAN, None),                     # 2nd+
        ],
    })

    def get_punishment(
        self,
        severity: SeverityLevel,
        offense_count: int,
    ) -> tuple[ActionType, Optional[int]]:
        """Return (action, duration_seconds) for the given severity and offense count."""
        ladder = self._matrix.get(severity.value, [])
        if not ladder:
            return (ActionType.WARN, None)
        idx = min(offense_count - 1, len(ladder) - 1)
        return ladder[idx]


# =============================================================================
# Guild configuration
# =============================================================================


@dataclass
class GuildConfig:
    """Complete configuration for one guild's AI moderation.

    All fields have defaults so a newly-joined guild works out of the box
    with conservative settings. Server admins can override via the DB.
    """

    # --- Toggles ---
    enabled: bool = True
    chat_enabled: bool = True

    # --- Authorization ---
    authorized_role_ids: Set[int] = field(default_factory=set)
    senior_moderator_role_ids: Set[int] = field(default_factory=set)

    # --- Protected entities ---
    protected_user_ids: Set[int] = field(default_factory=set)
    protected_role_ids: Set[int] = field(default_factory=set)

    # --- Duration limits ---
    default_timeout_seconds: int = 3_600       # 1 hour
    max_timeout_seconds: int = 2_419_200       # 28 days (Discord max)
    max_temp_ban_seconds: int = 2_592_000      # 30 days

    # --- Requirements ---
    require_reason: bool = True
    require_evidence_for_severe: bool = True
    require_confirmation_for_severe: bool = True
    require_confirmation_threshold: float = 0.6  # confidence below this → confirm

    # --- AI confidence thresholds ---
    auto_action_threshold: float = 0.85          # above → auto-execute
    human_review_threshold: float = 0.5          # below → require human review

    # --- Context collection ---
    recent_message_limit: int = 50               # messages to scan for evidence
    investigation_lookback_minutes: int = 30

    # --- Conversation state ---
    conversation_ttl_seconds: int = 120          # 2 minutes to respond to a clarification
    confirmation_timeout_seconds: int = 60       # 1 minute to confirm

    # --- Channels ---
    log_channel_id: Optional[int] = None
    appeal_channel_id: Optional[int] = None

    # --- Data retention ---
    case_retention_days: int = 365
    evidence_retention_days: int = 90

    # --- Muted role (alternative to timeout) ---
    use_timeout_over_role: bool = True
    muted_role_id: Optional[int] = None

    # --- AI provider overrides (per-guild) ---
    ai_model: Optional[str] = None
    ai_temperature: float = 0.3

    # --- Rules ---
    rules: List[Rule] = field(default_factory=list)
    punishment_matrix: PunishmentMatrix = field(default_factory=PunishmentMatrix)

    # --- Per-guild lock ---
    # (not persisted; used to prevent concurrent actions in the same guild)
    _lock_held_by: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "chat_enabled": self.chat_enabled,
            "authorized_role_ids": sorted(self.authorized_role_ids),
            "senior_moderator_role_ids": sorted(self.senior_moderator_role_ids),
            "protected_user_ids": sorted(self.protected_user_ids),
            "protected_role_ids": sorted(self.protected_role_ids),
            "default_timeout_seconds": self.default_timeout_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
            "max_temp_ban_seconds": self.max_temp_ban_seconds,
            "require_reason": self.require_reason,
            "require_evidence_for_severe": self.require_evidence_for_severe,
            "require_confirmation_for_severe": self.require_confirmation_for_severe,
            "require_confirmation_threshold": self.require_confirmation_threshold,
            "auto_action_threshold": self.auto_action_threshold,
            "human_review_threshold": self.human_review_threshold,
            "recent_message_limit": self.recent_message_limit,
            "investigation_lookback_minutes": self.investigation_lookback_minutes,
            "conversation_ttl_seconds": self.conversation_ttl_seconds,
            "confirmation_timeout_seconds": self.confirmation_timeout_seconds,
            "log_channel_id": str(self.log_channel_id) if self.log_channel_id else None,
            "appeal_channel_id": str(self.appeal_channel_id) if self.appeal_channel_id else None,
            "case_retention_days": self.case_retention_days,
            "evidence_retention_days": self.evidence_retention_days,
            "use_timeout_over_role": self.use_timeout_over_role,
            "muted_role_id": str(self.muted_role_id) if self.muted_role_id else None,
            "ai_model": self.ai_model,
            "ai_temperature": self.ai_temperature,
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuildConfig":
        """Build a GuildConfig from a DB row, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known and not k.startswith("_")}

        # Convert string IDs back to int sets.
        for key in ("authorized_role_ids", "senior_moderator_role_ids",
                     "protected_user_ids", "protected_role_ids"):
            if key in filtered and isinstance(filtered[key], (list, tuple)):
                filtered[key] = {int(v) for v in filtered[key] if v}

        # Convert string IDs back to int.
        for key in ("log_channel_id", "appeal_channel_id", "muted_role_id"):
            if key in filtered and filtered[key] and isinstance(filtered[key], str):
                try:
                    filtered[key] = int(filtered[key])
                except ValueError:
                    filtered[key] = None

        # Parse rules.
        if "rules" in filtered and isinstance(filtered[key], list):
            rules = []
            for rd in filtered["rules"]:
                if isinstance(rd, dict):
                    rules.append(Rule(
                        name=rd.get("name", ""),
                        description=rd.get("description", ""),
                        severity=SeverityLevel(rd.get("severity", "minor")),
                        suggested_action=ActionType(rd.get("suggested_action", "warn")),
                        max_automatic_action=ActionType(rd.get("max_automatic_action", "timeout")),
                        escalation_steps=rd.get("escalation_steps", []),
                    ))
            filtered["rules"] = rules

        return cls(**filtered)

    @classmethod
    def default(cls) -> "GuildConfig":
        """Return a config with sensible default rules."""
        config = cls()
        config.rules = [
            Rule(
                name="spam",
                description="Sending repeated or duplicate messages rapidly.",
                severity=SeverityLevel.MINOR,
                suggested_action=ActionType.WARN,
                max_automatic_action=ActionType.TIMEOUT,
            ),
            Rule(
                name="harassment",
                description="Targeted insults, threats, or persistent unwanted attention.",
                severity=SeverityLevel.MODERATE,
                suggested_action=ActionType.TIMEOUT,
                max_automatic_action=ActionType.KICK,
            ),
            Rule(
                name="hate_speech",
                description="Slurs, discriminatory language, or incitement of hatred.",
                severity=SeverityLevel.SEVERE,
                suggested_action=ActionType.KICK,
                max_automatic_action=ActionType.BAN,
            ),
            Rule(
                name="threats",
                description="Threats of violence, self-harm, or illegal activity.",
                severity=SeverityLevel.CRITICAL,
                suggested_action=ActionType.TEMP_BAN,
                max_automatic_action=ActionType.BAN,
            ),
            Rule(
                name="advertising",
                description="Unsolicited promotional content or server invites.",
                severity=SeverityLevel.MINOR,
                suggested_action=ActionType.WARN,
                max_automatic_action=ActionType.TIMEOUT,
            ),
            Rule(
                name="nsfw",
                description="Not safe for work content in non-NSFW channels.",
                severity=SeverityLevel.SEVERE,
                suggested_action=ActionType.KICK,
                max_automatic_action=ActionType.BAN,
            ),
            Rule(
                name="evasion",
                description="Attempting to circumvent a previous ban or moderation action.",
                severity=SeverityLevel.SEVERE,
                suggested_action=ActionType.TEMP_BAN,
                max_automatic_action=ActionType.BAN,
            ),
        ]
        return config
