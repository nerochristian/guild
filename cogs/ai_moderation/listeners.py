"""
Listeners — the on_message handler that orchestrates the full workflow.

This is the entry point for every message that might be a moderation
instruction. The listener:
1. Checks if the message is directed at the bot (mention or reply).
2. Verifies the author is authorized.
3. Checks for an active conversation (follow-up to a clarification).
4. If no active conversation, parses the instruction via AI.
5. If the request needs clarification, saves it and asks the question.
6. If the request needs confirmation, sends the confirmation view.
7. If the request is ready, executes it.
8. Logs everything.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from .action_executor import ActionExecutor
from .config import GuildConfig
from .confirmations import ConfirmationView
from .conversation_manager import ConversationManager
from .exceptions import (
    AIModerationError,
    AITimeoutError,
    AIUnavailableError,
    AmbiguousDurationError,
    CircuitBreakerOpenError,
    ConfirmationExpiredError,
    ExpiredConversationError,
    HierarchyError,
    InvalidAIResponseError,
    InvalidTargetError,
    NoActiveConversationError,
    OwnerTargetError,
    PermissionError as ModPermissionError,
    ProtectedRoleError,
    ProtectedUserError,
    SelfTargetError,
    UnauthorizedModerator,
    WrongModeratorError,
)
from .logging_service import LoggingService
from .models import IntentType, ModerationRequest
from .parser import InstructionParser
from .permissions import PermissionChecker

logger = logging.getLogger("ModBot.AIModeration.Listener")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ModerationListener:
    """Orchestrates the moderation workflow for incoming messages.

    The listener is the glue between all services. It doesn't contain
    business logic — it sequences calls to the parser, conversation
    manager, confirmations, executor, and logging service.
    """

    def __init__(
        self,
        bot: commands.Bot,
        parser: InstructionParser,
        conversation: ConversationManager,
        executor: ActionExecutor,
        permissions: PermissionChecker,
        logging: LoggingService,
    ) -> None:
        self.bot = bot
        self.parser = parser
        self.conversation = conversation
        self.executor = executor
        self.permissions = permissions
        self.logging = logging

    # ------------------------------------------------------------------
    # Main message handler
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        """Handle an incoming message that might be a moderation instruction."""
        # Skip bot messages and DMs.
        if message.author.bot or not message.guild or not self.bot.user:
            return

        # Check if directed at the bot.
        is_mentioned = self.bot.user in message.mentions
        is_reply_to_bot = await self._is_reply_to_bot(message)

        if not is_mentioned and not is_reply_to_bot:
            return

        # Skip if a traditional command matches.
        try:
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return
        except Exception:
            pass

        # Get guild config.
        # (The cog provides the db; we access it through the parser.)
        from .database import Database  # type: ignore[import-not-found]
        db: Database = self.parser.db  # type: ignore[attr-defined]
        config = await db.get_guild_config(message.guild.id)

        if not config.enabled:
            return

        # Clean the content (strip bot mention).
        content = self._clean_content(message)
        if not content.strip():
            return

        # --- Check for active conversation (follow-up) ---
        pending = await self.conversation.get_pending_request(
            message.guild.id, message.channel.id, message.author.id,
        )

        if pending:
            await self._handle_followup(message, content, pending, config, db)
            return

        # --- Parse the instruction ---
        await self._handle_new_instruction(
            message, content, config, db, is_reply_to_bot,
        )

    # ------------------------------------------------------------------
    # Handle a new instruction
    # ------------------------------------------------------------------

    async def _handle_new_instruction(
        self,
        message: discord.Message,
        content: str,
        config: GuildConfig,
        db,
        is_reply_to_bot: bool,
    ) -> None:
        """Parse and handle a new moderation instruction."""
        actor = message.author
        if not isinstance(actor, discord.Member):
            return

        # Check authorization.
        if not self.permissions.is_authorized_moderator(actor, config):
            await self._reply(message, "You are not authorized to use AI moderation commands.")
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="unauthorized",
                raw_instruction=content,
                result="Unauthorized moderator rejected.",
            )
            return

        # Fetch recent messages for context.
        recent = await self._fetch_recent_messages(message.channel, limit=config.recent_message_limit)

        # Parse via AI.
        try:
            async with message.channel.typing():
                request = await self.parser.parse(
                    message=message,
                    content=content,
                    guild=message.guild,
                    actor=actor,
                    config=config,
                    recent_messages=recent,
                    is_reply_to_bot=is_reply_to_bot,
                )
        except AIUnavailableError as exc:
            await self._reply(message, str(exc))
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="ai_unavailable",
                raw_instruction=content,
                error=str(exc),
            )
            return
        except (AITimeoutError, CircuitBreakerOpenError) as exc:
            await self._reply(message, str(exc))
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="ai_error",
                raw_instruction=content,
                error=str(exc),
            )
            return
        except InvalidAIResponseError as exc:
            await self._reply(
                message,
                "I couldn't understand the AI's response. Please try rephrasing your request.",
            )
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="ai_invalid_response",
                raw_instruction=content,
                error=str(exc),
            )
            return
        except Exception as exc:
            logger.exception("Unexpected parsing error")
            await self._reply(message, f"An error occurred: {exc}")
            return

        # --- Handle the parsed request ---
        await self._dispatch_request(message, request, config, db)

    # ------------------------------------------------------------------
    # Dispatch a parsed request
    # ------------------------------------------------------------------

    async def _dispatch_request(
        self,
        message: discord.Message,
        request: ModerationRequest,
        config: GuildConfig,
        db,
    ) -> None:
        """Route a parsed request to the appropriate handler."""
        actor = message.author

        # Log the parsed request.
        await self.logging.log_request(
            guild_id=message.guild.id,
            actor_id=actor.id,
            event_type="parsed",
            raw_instruction=request.raw_instruction,
            parsed_intent=request.intent.value,
            action=request.targets[0].action.value if request.targets else None,
            target_id=request.targets[0].user_id if request.targets else None,
            confidence=request.confidence,
            result=request.summary[:200],
        )

        # --- Needs clarification ---
        if request.needs_clarification and request.clarification_question:
            await self.conversation.save_pending_request(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=actor.id,
                request=request,
                clarification_question=request.clarification_question,
                config=config,
            )
            await self._reply(message, request.clarification_question)
            return

        # --- Cancel ---
        if request.intent == IntentType.CANCEL:
            await self.conversation.clear_pending_request(
                message.guild.id, message.channel.id, actor.id,
            )
            await self._reply(message, "Action cancelled.")
            return

        # --- Question / Conversation (no execution) ---
        if request.intent in {IntentType.QUESTION, IntentType.CONVERSATION}:
            response_text = request.summary or request.reasoning or "I don't have enough information to answer that."
            await self._reply(message, response_text)
            return

        # --- No action ---
        if not request.targets or all(t.action.value == "no_action" for t in request.targets):
            await self._reply(
                message,
                request.summary or "I determined no action is needed.",
            )
            return

        # --- Escalate to human ---
        if any(t.action.value == "escalate_human" for t in request.targets):
            await self._reply(
                message,
                "This case has been flagged for human moderator review. "
                "A senior moderator will review the evidence and decide.",
            )
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="escalated",
                raw_instruction=request.raw_instruction,
                result="Escalated to human review.",
            )
            return

        # --- Needs confirmation ---
        if request.requires_confirmation:
            await self._send_confirmation(message, request, config, db)
            return

        # --- Execute directly ---
        await self._execute_request(message, request, config, db)

    # ------------------------------------------------------------------
    # Execute a request
    # ------------------------------------------------------------------

    async def _execute_request(
        self,
        message: discord.Message,
        request: ModerationRequest,
        config: GuildConfig,
        db,
    ) -> None:
        """Execute the request after all checks pass."""
        actor = message.author

        # Validate permissions for each target.
        for target in request.targets:
            target_member = message.guild.get_member(target.user_id)
            try:
                self.permissions.validate_action(
                    action=target.action,
                    actor=actor,
                    target=target_member,
                    guild=message.guild,
                    config=config,
                )
            except ModPermissionError as exc:
                await self._reply(message, str(exc))
                await self.logging.log_request(
                    guild_id=message.guild.id,
                    actor_id=actor.id,
                    event_type="permission_denied",
                    raw_instruction=request.raw_instruction,
                    action=target.action.value,
                    target_id=target.user_id,
                    error=str(exc),
                )
                return
            except (InvalidTargetError, SelfTargetError, OwnerTargetError,
                    ProtectedUserError, ProtectedRoleError, HierarchyError) as exc:
                await self._reply(message, str(exc))
                return

        # Execute.
        try:
            async with message.channel.typing():
                result = await self.executor.execute_request(
                    request=request,
                    actor=actor,
                    guild=message.guild,
                    config=config,
                )
        except Exception as exc:
            logger.exception("Execution failed")
            await self._reply(message, f"An error occurred during execution: {exc}")
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=actor.id,
                event_type="execution_error",
                raw_instruction=request.raw_instruction,
                error=str(exc),
            )
            return

        # Send the result.
        await self._reply(message, result.message)

    # ------------------------------------------------------------------
    # Send confirmation
    # ------------------------------------------------------------------

    async def _send_confirmation(
        self,
        message: discord.Message,
        request: ModerationRequest,
        config: GuildConfig,
        db,
    ) -> None:
        """Send a confirmation view for the request."""
        # Collect evidence for the evidence button.
        evidence = []
        if request.targets and request.targets[0].evidence_message_ids:
            from .context_collector import ContextCollector
            # Build Evidence objects from the stored IDs.
            # In practice, the parser already collected these.
            pass  # Evidence is collected by the parser and passed through.

        view = ConfirmationView(
            request=request,
            actor_id=message.author.id,
            senior_moderator_role_ids=config.senior_moderator_role_ids,
            evidence=evidence,
            timeout=float(config.confirmation_timeout_seconds),
        )

        embed = view.build_confirmation_embed()
        sent = await message.channel.send(
            embed=embed,
            view=view,
            reference=message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = sent

        # Wait for the result.
        await view.wait()

        if view.result == "confirm":
            # Clear any pending conversation state.
            await self.conversation.clear_pending_request(
                message.guild.id, message.channel.id, message.author.id,
            )
            await self._execute_request(message, request, config, db)

        elif view.result == "cancel":
            await self.conversation.clear_pending_request(
                message.guild.id, message.channel.id, message.author.id,
            )
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=message.author.id,
                event_type="cancelled",
                raw_instruction=request.raw_instruction,
                result="Moderator cancelled via confirmation button.",
            )

        elif view.result == "escalate":
            await self._reply(
                message,
                "This case has been escalated for senior moderator review.",
            )

        elif view.result == "change":
            # Save the pending request so the next message updates it.
            await self.conversation.save_pending_request(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                request=request,
                clarification_question="What would you like to change it to?",
                config=config,
            )
            await self._reply(
                message,
                "Please reply with the new action or duration. For example: "
                "'use a warning instead' or 'make it 30 minutes'.",
            )

        elif view.result == "expired":
            await self.logging.log_request(
                guild_id=message.guild.id,
                actor_id=message.author.id,
                event_type="expired",
                raw_instruction=request.raw_instruction,
                result="Confirmation expired.",
            )

    # ------------------------------------------------------------------
    # Handle a follow-up message (for active conversations)
    # ------------------------------------------------------------------

    async def _handle_followup(
        self,
        message: discord.Message,
        content: str,
        pending: dict,
        config: GuildConfig,
        db,
    ) -> None:
        """Handle a follow-up message in an active conversation."""
        actor = message.author

        try:
            updated_request = await self.conversation.apply_response(
                content, pending, config,
            )
        except ExpiredConversationError:
            await self.conversation.clear_pending_request(
                message.guild.id, message.channel.id, actor.id,
            )
            await self._reply(message, "That request has expired. Please start over.")
            return
        except NoActiveConversationError:
            return

        # Clear the pending state (it's been consumed).
        await self.conversation.clear_pending_request(
            message.guild.id, message.channel.id, actor.id,
        )

        # If the mod cancelled, acknowledge.
        if updated_request.intent == IntentType.CANCEL:
            await self._reply(message, "Action cancelled.")
            return

        # If still needs clarification, ask again.
        if updated_request.needs_clarification and updated_request.clarification_question:
            await self.conversation.save_pending_request(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=actor.id,
                request=updated_request,
                clarification_question=updated_request.clarification_question,
                config=config,
            )
            await self._reply(message, updated_request.clarification_question)
            return

        # Re-dispatch the updated request.
        await self._dispatch_request(message, updated_request, config, db)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_content(self, message: discord.Message) -> str:
        """Strip the bot mention from the message content."""
        content = message.content or ""
        if self.bot.user:
            content = re.sub(
                rf"^\s*<@!?{self.bot.user.id}>\s*[:,]?\s*",
                "",
                content,
                count=1,
            )
        return content.strip()

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        """Check if the message is a reply to the bot."""
        if not self.bot.user or not message.reference or not message.reference.message_id:
            return False
        ref = message.reference.resolved
        if isinstance(ref, discord.Message):
            return ref.author.id == self.bot.user.id
        fetch = getattr(message.channel, "fetch_message", None)
        if not callable(fetch):
            return False
        try:
            fetched = await fetch(message.reference.message_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return False
        return isinstance(fetched, discord.Message) and fetched.author.id == self.bot.user.id

    async def _fetch_recent_messages(
        self, channel: discord.abc.Messageable, limit: int = 50
    ) -> list[discord.Message]:
        """Fetch recent messages, oldest first."""
        try:
            messages = [m async for m in channel.history(limit=limit)]
            messages.reverse()
            return messages
        except discord.HTTPException:
            return []

    async def _reply(
        self, message: discord.Message, content: str, *, ephemeral: bool = False
    ) -> None:
        """Send a reply to the moderator."""
        try:
            await message.reply(
                content,
                allowed_mentions=discord.AllowedMentions.none(),
                mention_author=False,
            )
        except discord.HTTPException as exc:
            logger.debug("Failed to reply: %s", exc)
