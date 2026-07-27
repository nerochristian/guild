"""
Pydantic schemas for validating AI output.

The AI must return JSON that matches these schemas. Any deviation is
rejected — the AI never directly calls Discord functions, and its output
is always validated before execution.
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# =============================================================================
# Allowed values (mirrors models.py enums but as plain strings for the AI)
# =============================================================================

ALLOWED_ACTIONS = {
    "warn", "delete_message", "timeout", "untimeout", "kick", "ban",
    "temp_ban", "unban", "add_muted_role", "remove_muted_role",
    "slowmode", "lock_channel", "unlock_channel", "purge",
    "add_role", "remove_role", "escalate_human", "no_action", "undo",
}

ALLOWED_INTENTS = {
    "moderation_action", "question", "clarification", "investigation",
    "undo", "cancel", "conversation", "bot_selected_punishment",
}

ALLOWED_SEVERITIES = {"none", "minor", "moderate", "severe", "critical"}


# =============================================================================
# Pydantic models
# =============================================================================


class AITarget(BaseModel):
    """One target user and the action to apply."""

    user_id: str = Field(..., description="Discord user ID as a string")
    action: str = Field(..., description="One of the allowed action names")
    duration_seconds: Optional[int] = Field(
        None, ge=1, le=2419200, description="Duration in seconds (1 to 28 days)"
    )
    reason: str = Field("", max_length=500, description="Reason for the action")
    evidence_message_ids: List[str] = Field(
        default_factory=list, description="Message IDs that serve as evidence"
    )
    role_name: Optional[str] = Field(None, description="Role name for add/remove role actions")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError(f"user_id must be a numeric string, got {v!r}")
        return v

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ALLOWED_ACTIONS:
            raise ValueError(f"Unknown action: {v!r}. Allowed: {sorted(ALLOWED_ACTIONS)}")
        return v

    @field_validator("evidence_message_ids")
    @classmethod
    def validate_evidence_ids(cls, v: List[str]) -> List[str]:
        for eid in v:
            if not eid.isdigit():
                raise ValueError(f"evidence_message_id must be numeric, got {eid!r}")
        return v

    @model_validator(mode="after")
    def validate_duration_for_action(self) -> "AITarget":
        """Duration must only be set for timed actions."""
        timed_actions = {"timeout", "temp_ban", "slowmode"}
        if self.action not in timed_actions and self.duration_seconds is not None:
            raise ValueError(
                f"duration_seconds should not be set for action {self.action!r}"
            )
        if self.action in timed_actions and self.duration_seconds is None:
            raise ValueError(
                f"duration_seconds is required for action {self.action!r}"
            )
        return self


class AIInvestigation(BaseModel):
    """Investigation result for context-based requests."""

    detected_violation: Optional[str] = Field(None, max_length=200)
    severity: str = Field("none", description="One of: none, minor, moderate, severe, critical")
    evidence_message_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    recommended_action: Optional[str] = Field(None)
    alternative_action: Optional[str] = Field(None)
    requires_human_review: bool = False
    summary: str = Field("", max_length=1000)
    reasoning: str = Field("", max_length=2000)

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in ALLOWED_SEVERITIES:
            raise ValueError(f"Unknown severity: {v!r}")
        return v

    @field_validator("recommended_action", "alternative_action")
    @classmethod
    def validate_actions(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALLOWED_ACTIONS:
            raise ValueError(f"Unknown action: {v!r}")
        return v


class AIResponse(BaseModel):
    """The complete validated AI response.

    This is the single entry point through which all AI output flows.
    If the AI returns anything that doesn't match this schema, it is
    rejected and an error is logged.
    """

    intent: str = Field(..., description="One of the allowed intent types")
    targets: List[AITarget] = Field(default_factory=list, max_length=10)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    needs_clarification: bool = False
    clarification_question: Optional[str] = Field(None, max_length=500)
    requires_confirmation: bool = False
    summary: str = Field("", max_length=1000)
    reasoning: str = Field("", max_length=2000)
    investigation: Optional[AIInvestigation] = None
    channel_id: Optional[str] = Field(None, description="Channel ID for channel-scoped actions")
    amount: Optional[int] = Field(None, ge=1, le=500, description="Message count for purge")

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, v: str) -> str:
        if v not in ALLOWED_INTENTS:
            raise ValueError(f"Unknown intent: {v!r}. Allowed: {sorted(ALLOWED_INTENTS)}")
        return v

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.isdigit():
            raise ValueError(f"channel_id must be numeric, got {v!r}")
        return v

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "AIResponse":
        """Cross-field validation rules."""
        # Clarification requires a question.
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification is True but clarification_question is empty")

        # Moderation actions require at least one target (unless it's a channel action).
        channel_actions = {"slowmode", "lock_channel", "unlock_channel", "purge"}
        if (
            self.intent == "moderation_action"
            and not self.needs_clarification
            and not self.targets
        ):
            # Allow channel-scoped actions without a user target.
            has_channel_action = any(t.action in channel_actions for t in self.targets)
            if not has_channel_action and not self.channel_id:
                raise ValueError(
                    "moderation_action intent requires at least one target "
                    "or a channel-scoped action"
                )

        # No-action and escalate don't need targets.
        if self.intent in {"cancel", "conversation", "question"} and self.targets:
            raise ValueError(f"{self.intent!r} intent should not have targets")

        return self


# =============================================================================
# Parsing / validation entrypoint
# =============================================================================


def validate_ai_response(raw_json: str) -> AIResponse:
    """Parse and validate a raw JSON string from the AI.

    Args:
        raw_json: The raw text returned by the AI model.

    Returns:
        A validated AIResponse.

    Raises:
        InvalidAIResponseError: If the JSON is malformed or fails validation.
    """
    from .exceptions import InvalidAIResponseError

    # Strip markdown code fences if present.
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_json.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    # Extract JSON object if there's surrounding text.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise InvalidAIResponseError(raw_json, f"JSON decode error: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidAIResponseError(raw_json, "Top-level JSON must be an object")

    try:
        return AIResponse.model_validate(data)
    except Exception as exc:
        raise InvalidAIResponseError(raw_json, str(exc)) from exc
