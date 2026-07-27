"""
Conversation manager — multi-turn clarification state.

When the bot asks "Did you mean 10 minutes?" or "What is the reason?",
the conversation manager stores the pending request and matches the
moderator's next message to complete it.

Conversation state is:
- Per-user, per-channel (so a mod can have separate conversations in
  different channels).
- Time-limited (expires after config.conversation_ttl_seconds).
- Stored in the database (so it survives bot restarts).
- Cleared on completion, cancellation, or expiry.

The manager also handles "correction" messages like "actually make it 30
minutes" or "use a warning instead" by updating the pending request.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord

from .config import GuildConfig
from .database import Database
from .exceptions import (
    ExpiredConversationError,
    NoActiveConversationError,
)
from .models import ActionType, IntentType, ModerationRequest, ModerationTarget
from .utils import parse_duration

logger = logging.getLogger("ModBot.AIModeration.Conversation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# Correction patterns — detect when a mod is amending a pending request
# =============================================================================


# "Actually make it 30 minutes" / "Change it to 1 hour"
_CHANGE_DURATION_RE = re.compile(
    r"(?:actually|change|make\s+it|update)\s+(?:it\s+)?(?:to\s+)?(.+)",
    re.IGNORECASE,
)

# "Use a warning instead" / "Change to a kick"
_CHANGE_ACTION_RE = re.compile(
    r"(?:use|change\s+to|switch\s+to|make\s+it\s+a)\s+(?:a\s+)?(warn\w*|timeout|mute|kick|ban|unban|untimeout|unmute)",
    re.IGNORECASE,
)

# "The reason is X" / "Reason: X"
_PROVIDE_REASON_RE = re.compile(
    r"(?:the\s+reason\s+is|reason\s*[:\-]|because)\s+(.+)",
    re.IGNORECASE,
)

# Affirmative responses.
_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|confirm|do\s+it|go\s+ahead|that'?s\s+right|correct)\b",
    re.IGNORECASE,
)

# Negative responses.
_NO_RE = re.compile(
    r"^(?:no|nope|cancel|never\s+mind|nvm|stop|abort)\b",
    re.IGNORECASE,
)


class ConversationManager:
    """Manages multi-turn clarification conversations.

    Flow:
    1. Bot asks a clarification question → stores pending request in DB.
    2. Moderator responds → manager retrieves the pending request.
    3. Manager classifies the response (answer, correction, cancel).
    4. Manager updates or completes the pending request.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Save / retrieve pending state
    # ------------------------------------------------------------------

    async def save_pending_request(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        request: ModerationRequest,
        clarification_question: str,
        config: GuildConfig,
    ) -> None:
        """Save a pending request that's waiting for clarification."""
        await self.db.save_conversation_state(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            pending_request=request.to_dict(),
            clarification_question=clarification_question,
            ttl_seconds=config.conversation_ttl_seconds,
        )

    async def get_pending_request(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Get the pending request for a user/channel, if not expired."""
        return await self.db.get_conversation_state(guild_id, channel_id, user_id)

    async def clear_pending_request(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> None:
        """Clear the pending request (after completion or cancellation)."""
        await self.db.clear_conversation_state(guild_id, channel_id, user_id)

    # ------------------------------------------------------------------
    # Classify a follow-up response
    # ------------------------------------------------------------------

    def classify_response(
        self,
        response_text: str,
        pending_question: Optional[str],
    ) -> str:
        """Classify a moderator's follow-up message.

        Returns one of:
        - "cancel": The mod wants to cancel the pending action.
        - "change_action": The mod wants to change the action type.
        - "change_duration": The mod wants to change the duration.
        - "provide_reason": The mod is providing a reason.
        - "affirmative": The mod confirmed (e.g. "yes, 10 minutes").
        - "answer": The mod is answering the clarification question.
        """
        text = response_text.strip()

        # Check for cancellation first.
        if _NO_RE.match(text):
            return "cancel"

        # Check for action change.
        if _CHANGE_ACTION_RE.match(text):
            return "change_action"

        # Check for duration change.
        if _CHANGE_DURATION_RE.match(text):
            # Try to parse a duration from the text.
            try:
                parsed = parse_duration(text, default_unit="minutes")
                if parsed is not None or "permanently" in text.lower():
                    return "change_duration"
            except Exception:
                pass

        # Check for reason provision.
        if _PROVIDE_REASON_RE.match(text):
            return "provide_reason"

        # Check for affirmative.
        if _YES_RE.match(text):
            return "affirmative"

        # Default: treat as an answer to the question.
        return "answer"

    # ------------------------------------------------------------------
    # Apply a follow-up response to a pending request
    # ------------------------------------------------------------------

    async def apply_response(
        self,
        response_text: str,
        pending: Dict[str, Any],
        config: GuildConfig,
    ) -> ModerationRequest:
        """Apply a moderator's follow-up response to a pending request.

        Returns the updated ModerationRequest.

        Raises:
            ExpiredConversationError: If the conversation has expired.
            NoActiveConversationError: If there's no pending request.
        """
        if not pending:
            raise NoActiveConversationError()

        # Reconstruct the request from stored data.
        request = self._reconstruct_request(pending["pending_request"])
        question = pending.get("clarification_question", "")

        classification = self.classify_response(response_text, question)

        if classification == "cancel":
            request.intent = IntentType.CANCEL
            request.needs_clarification = False
            request.clarification_question = None
            return request

        if classification == "change_action":
            return self._apply_action_change(response_text, request)

        if classification == "change_duration":
            return self._apply_duration_change(response_text, request, config)

        if classification == "provide_reason":
            return self._apply_reason(response_text, request)

        if classification == "affirmative":
            # The mod confirmed the suggested value. Clear the clarification.
            request.needs_clarification = False
            request.clarification_question = None
            return request

        # Default: treat as an answer to the question.
        return self._apply_answer(response_text, request, question, config)

    # ------------------------------------------------------------------
    # Apply specific response types
    # ------------------------------------------------------------------

    def _apply_action_change(
        self, text: str, request: ModerationRequest
    ) -> ModerationRequest:
        """Change the action type in the pending request."""
        match = _CHANGE_ACTION_RE.match(text)
        if not match:
            return request

        action_word = match.group(1).lower().strip()
        action_map = {
            "warn": ActionType.WARN,
            "warning": ActionType.WARN,
            "timeout": ActionType.TIMEOUT,
            "mute": ActionType.TIMEOUT,
            "kick": ActionType.KICK,
            "ban": ActionType.BAN,
            "unban": ActionType.UNBAN,
            "untimeout": ActionType.UNTIMEOUT,
            "unmute": ActionType.UNTIMEOUT,
        }
        new_action = action_map.get(action_word)
        if new_action and request.targets:
            for target in request.targets:
                target.action = new_action
            request.needs_clarification = False
            request.clarification_question = None
            request.summary = f"Action changed to {new_action.value}."
        return request

    def _apply_duration_change(
        self, text: str, request: ModerationRequest, config: GuildConfig
    ) -> ModerationRequest:
        """Change the duration in the pending request."""
        try:
            seconds = parse_duration(
                text,
                default_unit="minutes",
                max_seconds=config.max_timeout_seconds,
            )
        except Exception:
            return request

        if request.targets:
            for target in request.targets:
                if target.action in {ActionType.TIMEOUT, ActionType.TEMP_BAN}:
                    target.duration_seconds = seconds
            request.needs_clarification = False
            request.clarification_question = None
            request.summary = f"Duration changed to {seconds}s."
        return request

    def _apply_reason(
        self, text: str, request: ModerationRequest
    ) -> ModerationRequest:
        """Apply a provided reason to the pending request."""
        match = _PROVIDE_REASON_RE.match(text)
        if not match:
            return request

        reason = match.group(1).strip()
        if request.targets:
            for target in request.targets:
                target.reason = reason
            request.needs_clarification = False
            request.clarification_question = None
            request.summary = f"Reason set to: {reason}"
        return request

    def _apply_answer(
        self,
        text: str,
        request: ModerationRequest,
        question: str,
        config: GuildConfig,
    ) -> ModerationRequest:
        """Apply a free-form answer to the clarification question."""
        text_lower = text.lower().strip()

        # If the question was about duration, try to parse it.
        if question and any(
            word in question.lower()
            for word in ("duration", "long", "minutes", "hours", "time")
        ):
            try:
                seconds = parse_duration(
                    text,
                    default_unit="minutes",
                    max_seconds=config.max_timeout_seconds,
                )
                if seconds is not None and request.targets:
                    for target in request.targets:
                        target.duration_seconds = seconds
                    request.needs_clarification = False
                    request.clarification_question = None
                    return request
            except Exception:
                pass

        # If the question was about the reason, use the text as the reason.
        if question and "reason" in question.lower():
            if request.targets:
                for target in request.targets:
                    target.reason = text.strip()
                request.needs_clarification = False
                request.clarification_question = None
                return request

        # If the question was about the target (who), try to extract a user ID.
        if question and any(
            word in question.lower() for word in ("who", "target", "user")
        ):
            # The listener should have already resolved mentions.
            # Just clear the clarification.
            request.needs_clarification = False
            request.clarification_question = None
            return request

        # Default: clear the clarification and use the text as-is.
        request.needs_clarification = False
        request.clarification_question = None
        return request

    # ------------------------------------------------------------------
    # Reconstruct a ModerationRequest from stored dict
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_request(data: Dict[str, Any]) -> ModerationRequest:
        """Rebuild a ModerationRequest from its serialized form."""
        targets = []
        for td in data.get("targets", []):
            targets.append(ModerationTarget(
                user_id=int(td["user_id"]),
                action=ActionType(td["action"]),
                duration_seconds=td.get("duration_seconds"),
                reason=td.get("reason", ""),
                evidence_message_ids=[
                    int(mid) for mid in td.get("evidence_message_ids", [])
                ],
                role_name=td.get("role_name"),
            ))

        return ModerationRequest(
            intent=IntentType(data.get("intent", "conversation")),
            targets=targets,
            confidence=data.get("confidence", 0.0),
            needs_clarification=data.get("needs_clarification", False),
            clarification_question=data.get("clarification_question"),
            requires_confirmation=data.get("requires_confirmation", False),
            summary=data.get("summary", ""),
            reasoning=data.get("reasoning", ""),
            channel_id=int(data["channel_id"]) if data.get("channel_id") else None,
            amount=data.get("amount"),
            raw_instruction=data.get("raw_instruction", ""),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup_expired(self) -> int:
        """Delete all expired conversation states. Returns count deleted."""
        return await self.db.cleanup_expired_conversations()
