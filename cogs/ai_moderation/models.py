"""
Core domain models — enums and dataclasses used across the system.

These are the internal representations of moderation concepts. They are
intentionally plain (no Discord or AI dependencies) so they can be used
in tests, the database layer, and the AI schemas without circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# Enums
# =============================================================================


class ActionType(str, Enum):
    """Every moderation action the system can execute."""

    WARN = "warn"
    DELETE_MESSAGE = "delete_message"
    TIMEOUT = "timeout"
    UNTIMEOUT = "untimeout"
    KICK = "kick"
    BAN = "ban"
    TEMP_BAN = "temp_ban"
    UNBAN = "unban"
    ADD_MUTED_ROLE = "add_muted_role"
    REMOVE_MUTED_ROLE = "remove_muted_role"
    SLOWMODE = "slowmode"
    LOCK_CHANNEL = "lock_channel"
    UNLOCK_CHANNEL = "unlock_channel"
    PURGE = "purge"
    ADD_ROLE = "add_role"
    REMOVE_ROLE = "remove_role"
    ESCALATE_HUMAN = "escalate_human"
    NO_ACTION = "no_action"
    UNDO = "undo"

    @property
    def is_reversible(self) -> bool:
        """Whether this action can be undone by the undo system."""
        return self in {
            ActionType.TIMEOUT,
            ActionType.BAN,
            ActionType.TEMP_BAN,
            ActionType.ADD_MUTED_ROLE,
            ActionType.ADD_ROLE,
            ActionType.SLOWMODE,
            ActionType.LOCK_CHANNEL,
            ActionType.PURGE,  # reversible via transcript restore (limited)
        }

    @property
    def is_severe(self) -> bool:
        """Whether this action should require confirmation by default."""
        return self in {
            ActionType.KICK,
            ActionType.BAN,
            ActionType.TEMP_BAN,
            ActionType.PURGE,
            ActionType.DELETE_MESSAGE,
            ActionType.LOCK_CHANNEL,
        }


class IntentType(str, Enum):
    """High-level intent the moderator expressed."""

    MODERATION_ACTION = "moderation_action"
    QUESTION = "question"
    CLARIFICATION = "clarification"
    INVESTIGATION = "investigation"
    UNDO = "undo"
    CANCEL = "cancel"
    CONVERSATION = "conversation"
    BOT_SELECTED_PUNISHMENT = "bot_selected_punishment"


class SeverityLevel(str, Enum):
    """Severity of a rule violation."""

    NONE = "none"
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    CRITICAL = "critical"

    @property
    def numeric(self) -> int:
        return {"none": 0, "minor": 1, "moderate": 2, "severe": 3, "critical": 4}[self.value]


class ConfidenceLevel(str, Enum):
    """Confidence bucket derived from the numeric score."""

    LOW = "low"       # < 0.5
    MEDIUM = "medium"  # 0.5 – 0.8
    HIGH = "high"      # > 0.8

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceLevel":
        if score < 0.5:
            return cls.LOW
        if score <= 0.8:
            return cls.MEDIUM
        return cls.HIGH


class CaseStatus(str, Enum):
    """Status of a moderation case."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNDONE = "undone"
    APPEALED = "appealed"


# =============================================================================
# Evidence
# =============================================================================


@dataclass(frozen=True)
class Evidence:
    """A piece of evidence from a Discord message."""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str  # truncated + sanitized
    timestamp: datetime
    attachment_descriptions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": str(self.message_id),
            "channel_id": str(self.channel_id),
            "author_id": str(self.author_id),
            "author_name": self.author_name,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "attachments": self.attachment_descriptions,
        }


# =============================================================================
# Investigation result
# =============================================================================


@dataclass
class InvestigationResult:
    """Structured result of investigating a context-based request."""

    detected_violation: Optional[str] = None
    severity: SeverityLevel = SeverityLevel.NONE
    evidence: List[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    recommended_action: Optional[ActionType] = None
    alternative_action: Optional[ActionType] = None
    requires_human_review: bool = False
    summary: str = ""
    reasoning: str = ""

    @property
    def confidence_level(self) -> ConfidenceLevel:
        return ConfidenceLevel.from_score(self.confidence)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected_violation": self.detected_violation,
            "severity": self.severity.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "confidence": self.confidence,
            "confidence_level": self.confidence_level.value,
            "recommended_action": self.recommended_action.value if self.recommended_action else None,
            "alternative_action": self.alternative_action.value if self.alternative_action else None,
            "requires_human_review": self.requires_human_review,
            "summary": self.summary,
            "reasoning": self.reasoning,
        }


# =============================================================================
# Parsed moderation request
# =============================================================================


@dataclass
class ModerationTarget:
    """One target user and the action to apply to them."""

    user_id: int
    action: ActionType
    duration_seconds: Optional[int] = None
    reason: str = ""
    evidence_message_ids: List[int] = field(default_factory=list)
    role_name: Optional[str] = None  # for add_role / remove_role

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "action": self.action.value,
            "duration_seconds": self.duration_seconds,
            "reason": self.reason,
            "evidence_message_ids": [str(mid) for mid in self.evidence_message_ids],
            "role_name": self.role_name,
        }


@dataclass
class ModerationRequest:
    """A fully parsed moderation request ready for validation/execution."""

    intent: IntentType
    targets: List[ModerationTarget] = field(default_factory=list)
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    requires_confirmation: bool = False
    summary: str = ""
    reasoning: str = ""
    investigation: Optional[InvestigationResult] = None
    channel_id: Optional[int] = None  # for channel-scoped actions
    amount: Optional[int] = None  # for purge
    raw_instruction: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.targets and self.intent not in {
            IntentType.QUESTION,
            IntentType.CANCEL,
            IntentType.CONVERSATION,
            IntentType.INVESTIGATION,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.value,
            "targets": [t.to_dict() for t in self.targets],
            "confidence": self.confidence,
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "requires_confirmation": self.requires_confirmation,
            "summary": self.summary,
            "reasoning": self.reasoning,
            "investigation": self.investigation.to_dict() if self.investigation else None,
            "channel_id": str(self.channel_id) if self.channel_id else None,
            "amount": self.amount,
        }


# =============================================================================
# Reversible action record (for undo)
# =============================================================================


@dataclass
class ReversibleAction:
    """Record of a reversible action, stored for potential undo."""

    case_id: str
    action: ActionType
    target_id: int
    actor_id: int
    guild_id: int
    timestamp: datetime = field(default_factory=_now)
    # Action-specific metadata needed to reverse the action.
    undo_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "action": self.action.value,
            "target_id": str(self.target_id),
            "actor_id": str(self.actor_id),
            "guild_id": str(self.guild_id),
            "timestamp": self.timestamp.isoformat(),
            "undo_data": self.undo_data,
        }


# =============================================================================
# Rule definition
# =============================================================================


@dataclass
class Rule:
    """A configurable guild rule."""

    name: str
    description: str
    severity: SeverityLevel
    suggested_action: ActionType = ActionType.WARN
    escalation_steps: List[Dict[str, Any]] = field(default_factory=list)
    max_automatic_action: ActionType = ActionType.TIMEOUT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "severity": self.severity.value,
            "suggested_action": self.suggested_action.value,
            "escalation_steps": self.escalation_steps,
            "max_automatic_action": self.max_automatic_action.value,
        }


# =============================================================================
# Case record
# =============================================================================


@dataclass
class CaseRecord:
    """A complete moderation case record."""

    case_id: str
    guild_id: int
    actor_id: int
    target_id: Optional[int]
    action: ActionType
    status: CaseStatus
    reason: str = ""
    duration_seconds: Optional[int] = None
    confidence: float = 0.0
    ai_summary: str = ""
    ai_reasoning: str = ""
    evidence_message_ids: List[int] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    resolved_at: Optional[datetime] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "guild_id": str(self.guild_id),
            "actor_id": str(self.actor_id),
            "target_id": str(self.target_id) if self.target_id else None,
            "action": self.action.value,
            "status": self.status.value,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "confidence": self.confidence,
            "ai_summary": self.ai_summary,
            "ai_reasoning": self.ai_reasoning,
            "evidence_message_ids": [str(mid) for mid in self.evidence_message_ids],
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "error": self.error,
        }
