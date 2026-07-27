"""
Parser — converts natural-language instructions into validated ModerationRequests.

The parser is the bridge between the moderator's message and the AI.
It:
1. Cleans the instruction (strips bot mention, normalizes text).
2. Resolves reply-chain context (replied-to message author = potential target).
3. Resolves mentioned users (excluding the bot).
4. Builds the AI prompt with evidence and conversation context.
5. Calls the AI client.
6. Converts the validated AIResponse into a ModerationRequest.

The parser NEVER executes actions — it only produces a parsed request
that the listener then validates and executes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import discord

from .ai_client import AIClient
from .config import GuildConfig
from .context_collector import ContextCollector
from .database import Database
from .exceptions import (
    AmbiguousDurationError,
    InvalidTargetError,
)
from .models import (
    ActionType,
    Evidence,
    IntentType,
    InvestigationResult,
    ModerationRequest,
    ModerationTarget,
    SeverityLevel,
)
from .prompts import (
    CLARIFICATION_SYSTEM_PROMPT,
    CONVERSATION_SYSTEM_PROMPT,
    INVESTIGATION_SYSTEM_PROMPT,
    ROUTING_SYSTEM_PROMPT,
)
from .rule_engine import RuleEngine
from .schemas import AIResponse
from .utils import (
    build_evidence_block,
    extract_user_mentions,
    sanitize_for_prompt,
)

logger = logging.getLogger("ModBot.AIModeration.Parser")


# =============================================================================
# Mention patterns
# =============================================================================

_MENTION_RE = re.compile(r"<@!?(\d+)>")


class InstructionParser:
    """Parses natural-language moderation instructions via AI.

    The parser is stateless — it takes a message + context and returns
    a ModerationRequest. Conversation state (for follow-ups) is handled
    by the ConversationManager, not the parser.
    """

    def __init__(
        self,
        ai: AIClient,
        db: Database,
        context_collector: ContextCollector,
        rule_engine: RuleEngine,
    ) -> None:
        self.ai = ai
        self.db = db
        self.context_collector = context_collector
        self.rule_engine = rule_engine

    # ------------------------------------------------------------------
    # Parse a moderation instruction
    # ------------------------------------------------------------------

    async def parse(
        self,
        *,
        message: discord.Message,
        content: str,
        guild: discord.Guild,
        actor: discord.Member,
        config: GuildConfig,
        recent_messages: List[discord.Message],
        is_reply_to_bot: bool = False,
    ) -> ModerationRequest:
        """Parse a natural-language instruction into a ModerationRequest.

        This is the main entry point. It:
        1. Collects context (reply chain, mentioned users, evidence).
        2. Builds the AI prompt.
        3. Calls the AI.
        4. Converts the AIResponse to a ModerationRequest.
        5. Enriches the request with rule-engine recommendations.

        Returns a ModerationRequest that may need clarification, confirmation,
        or may be ready for immediate execution.
        """
        # --- Step 1: Collect context ---
        mentioned_user_ids = self._extract_mentioned_users(message, guild)
        reply_target_id = await self._extract_reply_target(message)

        # Determine if this is an investigation request ("deal with @user").
        is_investigation = self._is_investigation_request(content)

        # Collect evidence if this is an investigation or context-based request.
        evidence: List[Evidence] = []
        infraction_history: List[Dict[str, Any]] = []
        if is_investigation or "what they did" in content.lower() or "what happened" in content.lower():
            # Determine the target for investigation.
            investigate_target = mentioned_user_ids[0] if mentioned_user_ids else reply_target_id
            if investigate_target:
                evidence = await self.context_collector.collect_investigation_context(
                    guild, message.channel, investigate_target, config,
                )
                infraction_history = await self.context_collector.collect_infraction_history(
                    guild.id, investigate_target,
                )

        # Always collect the reply chain for context.
        if not evidence and message.reference:
            evidence = await self.context_collector.collect_reply_chain(message)

        # --- Step 2: Build the AI prompt ---
        evidence_block = ContextCollector.build_evidence_prompt(
            evidence, infraction_history,
        )
        conversation_block = ContextCollector.build_conversation_prompt(
            recent_messages,
            bot_user_id=self.ai.bot.user.id if self.ai.bot.user else None,
        )

        # Build the user instruction with resolved context.
        instruction_text = self._build_instruction_text(
            content, mentioned_user_ids, reply_target_id, guild,
        )

        # Choose the system prompt based on the request type.
        system_prompt = ROUTING_SYSTEM_PROMPT
        if is_investigation:
            system_prompt = INVESTIGATION_SYSTEM_PROMPT

        # --- Step 3: Call the AI ---
        try:
            ai_response = await self.ai.analyze_instruction(
                system_prompt=system_prompt,
                user_prompt=sanitize_for_prompt(instruction_text, max_length=2000),
                evidence_block=evidence_block,
                conversation_block=conversation_block,
                guild_id=guild.id,
                author_id=actor.id,
                model=config.ai_model,
                temperature=config.ai_temperature,
            )
        except Exception as exc:
            logger.exception("AI analysis failed")
            return ModerationRequest(
                intent=IntentType.CONVERSATION,
                summary="I couldn't analyze that request. The AI provider may be unavailable.",
                raw_instruction=content,
                reasoning=str(exc),
            )

        # --- Step 4: Convert AIResponse to ModerationRequest ---
        request = self._ai_response_to_request(
            ai_response, content, mentioned_user_ids, reply_target_id, guild, config,
        )

        # --- Step 5: Enrich with rule engine ---
        if (
            request.intent in {IntentType.BOT_SELECTED_PUNISHMENT, IntentType.INVESTIGATION}
            and request.investigation
            and request.targets
        ):
            target_id = request.targets[0].user_id
            request.investigation = await self.rule_engine.enrich_investigation(
                request.investigation, guild.id, target_id, config,
            )

        # --- Step 6: Determine if confirmation is needed ---
        request.requires_confirmation = self._needs_confirmation(request, config)

        return request

    # ------------------------------------------------------------------
    # Context extraction
    # ------------------------------------------------------------------

    def _extract_mentioned_users(
        self, message: discord.Message, guild: discord.Guild
    ) -> List[int]:
        """Extract mentioned user IDs, excluding the bot."""
        bot_id = self.ai.bot.user.id if self.ai.bot.user else 0
        result: List[int] = []
        for user in message.mentions:
            if user.id != bot_id and not getattr(user, "bot", False):
                result.append(user.id)
        # Also extract from raw text (in case mentions are text-only).
        for uid_str in _MENTION_RE.findall(message.content or ""):
            uid = int(uid_str)
            if uid != bot_id and uid not in result:
                result.append(uid)
        return result

    async def _extract_reply_target(
        self, message: discord.Message
    ) -> Optional[int]:
        """Get the user ID of the replied-to message's author."""
        if not message.reference or not message.reference.message_id:
            return None
        ref = message.reference.resolved
        if not isinstance(ref, discord.Message):
            fetch = getattr(message.channel, "fetch_message", None)
            if callable(fetch):
                try:
                    ref = await fetch(message.reference.message_id)
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    return None
        if isinstance(ref, discord.Message) and not ref.author.bot:
            return ref.author.id
        return None

    # ------------------------------------------------------------------
    # Request type detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_investigation_request(content: str) -> bool:
        """Detect if the moderator wants an investigation (not a direct action)."""
        lower = content.lower()
        investigation_phrases = [
            "deal with", "handle this", "check what happened",
            "investigate", "review what", "punish whoever",
            "punish the person", "who started", "is this against the rules",
            "what rule did", "should i ban", "should i kick",
            "what punishment", "choose a fair", "according to the rules",
            "deal with them", "handle @",
        ]
        return any(phrase in lower for phrase in investigation_phrases)

    # ------------------------------------------------------------------
    # Build the instruction text for the AI
    # ------------------------------------------------------------------

    def _build_instruction_text(
        self,
        content: str,
        mentioned_user_ids: List[int],
        reply_target_id: Optional[int],
        guild: discord.Guild,
    ) -> str:
        """Build the instruction text with resolved context.

        The AI needs to know:
        - What the moderator said.
        - Who was mentioned (resolved to user IDs).
        - Who was replied to (if applicable).
        """
        parts: List[str] = [f"Moderator instruction: {content}"]

        if mentioned_user_ids:
            user_list = ", ".join(f"<@{uid}> (ID: {uid})" for uid in mentioned_user_ids)
            parts.append(f"Mentioned users: {user_list}")

        if reply_target_id:
            parts.append(f"Reply target: <@{reply_target_id}> (ID: {reply_target_id})")

        # Include guild context.
        parts.append(f"Guild: {guild.name} (ID: {guild.id}, Members: {guild.member_count})")

        # Include configured rules summary.
        # (The AI uses this to classify violations and recommend punishments.)
        # This is passed in the system prompt, not the user message, to keep
        # the user message clean.

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Convert AIResponse → ModerationRequest
    # ------------------------------------------------------------------

    def _ai_response_to_request(
        self,
        ai_response: AIResponse,
        raw_instruction: str,
        mentioned_user_ids: List[int],
        reply_target_id: Optional[int],
        guild: discord.Guild,
        config: GuildConfig,
    ) -> ModerationRequest:
        """Convert a validated AIResponse into a ModerationRequest.

        This is where we resolve any "them" / "that user" references
        to actual user IDs using the mentioned users and reply target.
        """
        # Map intent string to enum.
        intent_map = {
            "moderation_action": IntentType.MODERATION_ACTION,
            "question": IntentType.QUESTION,
            "clarification": IntentType.CLARIFICATION,
            "investigation": IntentType.INVESTIGATION,
            "undo": IntentType.UNDO,
            "cancel": IntentType.CANCEL,
            "conversation": IntentType.CONVERSATION,
            "bot_selected_punishment": IntentType.BOT_SELECTED_PUNISHMENT,
        }
        intent = intent_map.get(ai_response.intent, IntentType.CONVERSATION)

        # Build targets.
        targets: List[ModerationTarget] = []
        for ai_target in ai_response.targets:
            # Resolve the user ID.
            try:
                user_id = int(ai_target.user_id)
            except ValueError:
                continue

            # If the AI didn't specify a user ID but we have mentioned
            # users or a reply target, use the first available.
            if user_id == 0:
                user_id = mentioned_user_ids[0] if mentioned_user_ids else (reply_target_id or 0)
                if user_id == 0:
                    raise InvalidTargetError(ai_target.user_id)

            # Validate the user exists in the guild (for non-ban actions).
            action = ActionType(ai_target.action)
            if action not in {ActionType.BAN, ActionType.TEMP_BAN, ActionType.UNBAN}:
                member = guild.get_member(user_id)
                if member is None and action != ActionType.NO_ACTION:
                    raise InvalidTargetError(user_id)

            # Parse evidence message IDs.
            evidence_ids = []
            for eid_str in ai_target.evidence_message_ids:
                try:
                    evidence_ids.append(int(eid_str))
                except ValueError:
                    pass

            targets.append(ModerationTarget(
                user_id=user_id,
                action=action,
                duration_seconds=ai_target.duration_seconds,
                reason=ai_target.reason,
                evidence_message_ids=evidence_ids,
                role_name=ai_target.role_name,
            ))

        # Build investigation result if present.
        investigation: Optional[InvestigationResult] = None
        if ai_response.investigation:
            inv = ai_response.investigation
            investigation = InvestigationResult(
                detected_violation=inv.detected_violation,
                severity=SeverityLevel(inv.severity),
                evidence=[],  # Evidence is collected separately; not all evidence has message IDs from the AI
                confidence=inv.confidence,
                recommended_action=ActionType(inv.recommended_action) if inv.recommended_action else None,
                alternative_action=ActionType(inv.alternative_action) if inv.alternative_action else None,
                requires_human_review=inv.requires_human_review,
                summary=inv.summary,
                reasoning=inv.reasoning,
            )

        # Determine channel_id for channel-scoped actions.
        channel_id = None
        if ai_response.channel_id:
            try:
                channel_id = int(ai_response.channel_id)
            except ValueError:
                pass

        return ModerationRequest(
            intent=intent,
            targets=targets,
            confidence=ai_response.confidence,
            needs_clarification=ai_response.needs_clarification,
            clarification_question=ai_response.clarification_question,
            requires_confirmation=ai_response.requires_confirmation,
            summary=ai_response.summary,
            reasoning=ai_response.reasoning,
            investigation=investigation,
            channel_id=channel_id,
            amount=ai_response.amount,
            raw_instruction=raw_instruction,
        )

    # ------------------------------------------------------------------
    # Confirmation logic
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_confirmation(request: ModerationRequest, config: GuildConfig) -> bool:
        """Determine if this request requires confirmation before execution.

        Confirmation is required when:
        1. The action is severe (ban, kick, purge).
        2. Confidence is below the configured threshold.
        3. The investigation requires human review.
        4. The config requires confirmation for severe actions.
        """
        if request.needs_clarification:
            return False  # Clarification comes before confirmation.

        if not request.targets:
            return False

        # Any severe action requires confirmation.
        for target in request.targets:
            if target.action.is_severe:
                if config.require_confirmation_for_severe:
                    return True

        # Low confidence requires confirmation.
        if request.confidence < config.require_confirmation_threshold:
            return True

        # Human review required.
        if request.investigation and request.investigation.requires_human_review:
            return True

        return request.requires_confirmation  # AI's own recommendation
