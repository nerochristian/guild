"""
Custom exception hierarchy for the AI moderation system.

All exceptions inherit from AIModerationError so callers can catch the
entire domain with a single except clause if needed.
"""
from __future__ import annotations

from typing import Any, Optional


class AIModerationError(Exception):
    """Base exception for all AI moderation errors."""

    def __init__(self, message: str, *, user_facing: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.user_facing = user_facing


# =============================================================================
# Configuration errors
# =============================================================================


class ConfigError(AIModerationError):
    """Guild is not configured or configuration is invalid."""


class MissingConfiguration(ConfigError):
    """A required configuration value is missing for this guild."""


# =============================================================================
# Permission errors
# =============================================================================


class PermissionError(AIModerationError):
    """Moderator lacks the required Discord or bot-specific permission."""


class UnauthorizedModerator(PermissionError):
    """User is not on the authorized-moderator list for this guild."""

    def __init__(self, user_id: int) -> None:
        super().__init__(
            f"User {user_id} is not authorized to use AI moderation commands.",
            user_facing=True,
        )


class HierarchyError(PermissionError):
    """Role hierarchy prevents the action."""

    def __init__(self, actor_name: str, target_name: str) -> None:
        super().__init__(
            f"{actor_name} cannot moderate {target_name} due to role hierarchy.",
            user_facing=True,
        )


class ProtectedUserError(PermissionError):
    """Target is on the protected-users list."""

    def __init__(self, target_name: str) -> None:
        super().__init__(
            f"{target_name} is a protected user and cannot be moderated.",
            user_facing=True,
        )


class ProtectedRoleError(PermissionError):
    """Target holds a protected role."""

    def __init__(self, role_name: str) -> None:
        super().__init__(
            f"Users with the {role_name} role are protected.",
            user_facing=True,
        )


class SelfTargetError(PermissionError):
    """Moderator tried to target themselves with a non-safe action."""

    def __init__(self) -> None:
        super().__init__(
            "You cannot target yourself with this action.",
            user_facing=True,
        )


class OwnerTargetError(PermissionError):
    """Moderator tried to target the server owner."""

    def __init__(self) -> None:
        super().__init__(
            "The server owner cannot be moderated by the bot.",
            user_facing=True,
        )


class BotMissingPermissionError(PermissionError):
    """The bot itself lacks the required Discord permission."""

    def __init__(self, permission_name: str) -> None:
        super().__init__(
            f"I need the `{permission_name}` permission to do that.",
            user_facing=True,
        )


# =============================================================================
# Validation errors
# =============================================================================


class ValidationError(AIModerationError):
    """Input failed validation."""


class InvalidAIResponseError(ValidationError):
    """AI returned a response that failed Pydantic validation."""

    def __init__(self, raw: str, errors: Any) -> None:
        super().__init__(
            f"AI response failed validation: {errors}",
        )
        self.raw = raw
        self.errors = errors


class InvalidDurationError(ValidationError):
    """Duration could not be parsed or is out of bounds."""

    def __init__(self, raw: str) -> None:
        super().__init__(
            f"Could not parse duration: {raw!r}",
            user_facing=True,
        )


class AmbiguousDurationError(ValidationError):
    """A number was given without a unit and no default is configured."""

    def __init__(self, number: int) -> None:
        super().__init__(
            f"Did you mean {number} minutes, {number} hours, or another duration?",
            user_facing=True,
        )
        self.number = number


class UnknownActionError(ValidationError):
    """AI requested an action that doesn't exist."""

    def __init__(self, action: str) -> None:
        super().__init__(f"Unknown action: {action!r}")


class InvalidTargetError(ValidationError):
    """Target user ID is invalid or not in the guild."""

    def __init__(self, target: Any) -> None:
        super().__init__(
            f"Invalid or unknown target: {target!r}",
            user_facing=True,
        )


class ExceedsMaxDurationError(ValidationError):
    """Requested duration exceeds the configured maximum."""

    def __init__(self, max_seconds: int) -> None:
        super().__init__(
            f"Duration exceeds the configured maximum "
            f"({max_seconds // 3600}h).",
            user_facing=True,
        )


class ContradictoryRequestError(ValidationError):
    """Request contains contradictory actions (e.g. ban AND unban)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, user_facing=True)


class DestructiveRequestError(ValidationError):
    """Request is destructively dangerous (e.g. delete all channels)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, user_facing=True)


# =============================================================================
# AI provider errors
# =============================================================================


class AIProviderError(AIModerationError):
    """Base error for AI provider failures."""


class AIUnavailableError(AIProviderError):
    """No AI provider is available."""

    def __init__(self) -> None:
        super().__init__(
            "The AI provider is not available right now. Try again later.",
            user_facing=True,
        )


class AITimeoutError(AIProviderError):
    """AI request timed out."""

    def __init__(self, timeout: float) -> None:
        super().__init__(
            f"The AI took too long to respond ({timeout:.0f}s). Try again.",
            user_facing=True,
        )


class AIRateLimitError(AIProviderError):
    """AI provider returned a rate-limit error."""

    def __init__(self, retry_after: Optional[float] = None) -> None:
        msg = "The AI provider is rate-limiting requests."
        if retry_after:
            msg += f" Try again in {int(retry_after)}s."
        super().__init__(msg, user_facing=True)


class CircuitBreakerOpenError(AIProviderError):
    """Circuit breaker is open due to repeated failures."""

    def __init__(self, remaining_seconds: int) -> None:
        super().__init__(
            f"The AI provider is temporarily unavailable due to repeated "
            f"failures. Try again in ~{remaining_seconds}s.",
            user_facing=True,
        )


# =============================================================================
# Conversation / clarification errors
# =============================================================================


class ConversationError(AIModerationError):
    """Base error for conversation-state issues."""


class NoActiveConversationError(ConversationError):
    """No pending clarification exists for this user/channel."""

    def __init__(self) -> None:
        super().__init__(
            "There is no pending action to update.",
            user_facing=True,
        )


class ExpiredConversationError(ConversationError):
    """Conversation state expired before the moderator responded."""

    def __init__(self) -> None:
        super().__init__(
            "That request has expired. Please start over.",
            user_facing=True,
        )


# =============================================================================
# Confirmation errors
# =============================================================================


class ConfirmationError(AIModerationError):
    """Base error for confirmation-flow issues."""


class ConfirmationExpiredError(ConfirmationError):
    """Confirmation view timed out."""

    def __init__(self) -> None:
        super().__init__(
            "Confirmation expired. The action was not executed.",
            user_facing=True,
        )


class WrongModeratorError(ConfirmationError):
    """A different user tried to interact with a confirmation view."""

    def __init__(self) -> None:
        super().__init__(
            "Only the moderator who requested this action can confirm it.",
            user_facing=True,
        )


class ConfirmationAlreadyHandledError(ConfirmationError):
    """Confirmation view was already confirmed or cancelled."""

    def __init__(self) -> None:
        super().__init__(
            "This confirmation has already been handled.",
            user_facing=True,
        )


# =============================================================================
# Execution errors
# =============================================================================


class ExecutionError(AIModerationError):
    """Base error for action execution failures."""


class DiscordHTTPError(ExecutionError):
    """Discord API returned an HTTP error."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(
            f"Discord API error ({status}): {text}",
            user_facing=True,
        )


class MemberNotFoundError(ExecutionError):
    """Target member left the guild before the action could execute."""

    def __init__(self, user_id: int) -> None:
        super().__init__(
            f"That user (<@{user_id}>) is no longer in the server.",
            user_facing=True,
        )


class AlreadyInStateError(ExecutionError):
    """User is already banned/timed out/etc."""

    def __init__(self, state: str) -> None:
        super().__init__(
            f"That user is already {state}.",
            user_facing=True,
        )


class IrreversibleActionError(ExecutionError):
    """The action cannot be undone."""

    def __init__(self, action: str) -> None:
        super().__init__(
            f"The action '{action}' cannot be undone.",
            user_facing=True,
        )


class PartialFailureError(ExecutionError):
    """Some steps in a multi-action plan succeeded but others failed."""

    def __init__(self, succeeded: list[str], failed: list[str]) -> None:
        super().__init__(
            f"Partial failure — succeeded: {succeeded}, failed: {failed}",
            user_facing=True,
        )


# =============================================================================
# Database errors
# =============================================================================


class DatabaseError(AIModerationError):
    """Database operation failed."""


class CaseNotFoundError(DatabaseError):
    """No case with the given ID exists."""

    def __init__(self, case_id: str) -> None:
        super().__init__(f"No case found with ID {case_id}.")
