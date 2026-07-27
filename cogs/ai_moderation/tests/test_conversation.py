"""
Unit tests for the conversation manager.

Tests clarification state, follow-up classification, and correction handling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ..config import GuildConfig
from ..conversation_manager import ConversationManager
from ..database import Database
from ..models import ActionType, IntentType, ModerationRequest, ModerationTarget


def _make_request(
    action: ActionType = ActionType.TIMEOUT,
    user_id: int = 123456789012345678,
    duration: int = 600,
    reason: str = "spamming",
) -> ModerationRequest:
    return ModerationRequest(
        intent=IntentType.MODERATION_ACTION,
        targets=[ModerationTarget(
            user_id=user_id,
            action=action,
            duration_seconds=duration if action in {ActionType.TIMEOUT, ActionType.TEMP_BAN} else None,
            reason=reason,
        )],
        confidence=0.8,
        needs_clarification=True,
        clarification_question="What duration?",
        summary="Pending clarification.",
        raw_instruction="timeout @user for spamming",
    )


class TestResponseClassification:
    def test_cancel(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("cancel", None) == "cancel"
        assert manager.classify_response("never mind", None) == "cancel"
        assert manager.classify_response("no", None) == "cancel"

    def test_change_action(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("use a warning instead", None) == "change_action"
        assert manager.classify_response("change to a kick", None) == "change_action"

    def test_change_duration(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("make it 30 minutes", None) == "change_duration"
        assert manager.classify_response("change to 1 hour", None) == "change_duration"

    def test_provide_reason(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("the reason is repeated spam", None) == "provide_reason"
        assert manager.classify_response("reason: harassment", None) == "provide_reason"

    def test_affirmative(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("yes", None) == "affirmative"
        assert manager.classify_response("yes, 10 minutes", None) == "affirmative"
        assert manager.classify_response("that's right", None) == "affirmative"

    def test_answer_default(self):
        manager = ConversationManager(MagicMock())
        assert manager.classify_response("10 minutes", None) == "answer"


class TestApplyResponse:
    @pytest.mark.asyncio
    async def test_cancel_clears_clarification(self):
        manager = ConversationManager(MagicMock())
        request = _make_request()
        pending = {
            "pending_request": request.to_dict(),
            "clarification_question": "What duration?",
        }
        result = await manager.apply_response("cancel", pending, GuildConfig())
        assert result.intent == IntentType.CANCEL
        assert not result.needs_clarification

    @pytest.mark.asyncio
    async def test_change_action(self):
        manager = ConversationManager(MagicMock())
        request = _make_request(action=ActionType.TIMEOUT)
        pending = {
            "pending_request": request.to_dict(),
            "clarification_question": "Confirm?",
        }
        result = await manager.apply_response(
            "use a warning instead", pending, GuildConfig(),
        )
        assert result.targets[0].action == ActionType.WARN
        assert not result.needs_clarification

    @pytest.mark.asyncio
    async def test_change_duration(self):
        manager = ConversationManager(MagicMock())
        request = _make_request(action=ActionType.TIMEOUT, duration=600)
        pending = {
            "pending_request": request.to_dict(),
            "clarification_question": "What duration?",
        }
        result = await manager.apply_response(
            "make it 30 minutes", pending, GuildConfig(),
        )
        assert result.targets[0].duration_seconds == 1800
        assert not result.needs_clarification

    @pytest.mark.asyncio
    async def test_provide_reason(self):
        manager = ConversationManager(MagicMock())
        request = _make_request()
        request.targets[0].reason = ""
        pending = {
            "pending_request": request.to_dict(),
            "clarification_question": "What is the reason?",
        }
        result = await manager.apply_response(
            "the reason is repeated spam", pending, GuildConfig(),
        )
        assert result.targets[0].reason == "repeated spam"
        assert not result.needs_clarification

    @pytest.mark.asyncio
    async def test_affirmative_clears(self):
        manager = ConversationManager(MagicMock())
        request = _make_request()
        pending = {
            "pending_request": request.to_dict(),
            "clarification_question": "Did you mean 10 minutes?",
        }
        result = await manager.apply_response("yes", pending, GuildConfig())
        assert not result.needs_clarification

    @pytest.mark.asyncio
    async def test_no_pending_raises(self):
        from ..exceptions import NoActiveConversationError
        manager = ConversationManager(MagicMock())
        with pytest.raises(NoActiveConversationError):
            await manager.apply_response("yes", {}, GuildConfig())
