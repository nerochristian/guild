"""
AI Moderation — production-quality AI-powered moderation system for Discord.py

A modular, secure, configurable AI moderation cog that understands natural-
language moderation instructions, validates them against guild rules and
permissions, and safely executes them with full audit logging.

## Architecture

    cogs/ai_moderation/
    ├── __init__.py          — Package exports
    ├── cog.py               — Discord cog + slash commands
    ├── listeners.py         — on_message orchestrator
    ├── parser.py            — NL → ModerationRequest via AI
    ├── ai_client.py         — Provider abstraction (DeepSeek + DigitalOcean)
    ├── prompts.py           — System prompts (prompt-injection-resistant)
    ├── schemas.py           — Pydantic validation for AI output
    ├── context_collector.py — Evidence gathering
    ├── rule_engine.py       — Guild rules + punishment matrix
    ├── action_executor.py   — Safe Discord API execution + undo
    ├── permissions.py       — Auth + hierarchy + protected users
    ├── confirmations.py     — Button-based confirmation views
    ├── conversation_manager.py — Multi-turn clarification state
    ├── logging_service.py   — Structured logs + Discord embeds
    ├── database.py          — Async SQLite layer
    ├── models.py            — Enums + dataclasses
    ├── exceptions.py        — Domain exceptions
    ├── utils.py             — Duration parsing, sanitization, helpers
    ├── config.py            — Guild configuration + punishment matrix
    └── tests/               — Unit + integration tests

## Workflow

    1. Moderator mentions the bot with an instruction.
    2. Listener checks authorization and conversation state.
    3. Parser collects context (reply chain, evidence, infractions).
    4. Parser calls the AI with a prompt-injection-safe prompt.
    5. AI returns JSON validated against the Pydantic schema.
    6. Parser converts the validated response to a ModerationRequest.
    7. Rule engine enriches the request with punishment recommendations.
    8. Permission checker validates hierarchy and authorization.
    9. If clarification is needed, the conversation manager saves state.
    10. If confirmation is needed, a button view is sent.
    11. Action executor safely calls the Discord API.
    12. Everything is logged to the DB and Discord log channel.

## Security

- AI output is NEVER trusted directly — it's validated via Pydantic.
- Investigated messages are wrapped in delimiters that tell the AI to
  treat them as data, not instructions.
- Permission checks run before EVERY action.
- Protected users/roles/owner are never moderated.
- The AI cannot execute arbitrary code or API calls.
"""
from __future__ import annotations

# Core exports.
from .config import GuildConfig, PunishmentMatrix
from .models import (
    ActionType,
    CaseRecord,
    CaseStatus,
    ConfidenceLevel,
    Evidence,
    IntentType,
    InvestigationResult,
    ModerationRequest,
    ModerationTarget,
    ReversibleAction,
    Rule,
    SeverityLevel,
)
from .exceptions import AIModerationError
from .schemas import AIResponse, AITarget, AIInvestigation, validate_ai_response

__all__ = [
    # Config
    "GuildConfig",
    "PunishmentMatrix",
    # Models
    "ActionType",
    "CaseRecord",
    "CaseStatus",
    "ConfidenceLevel",
    "Evidence",
    "IntentType",
    "InvestigationResult",
    "ModerationRequest",
    "ModerationTarget",
    "ReversibleAction",
    "Rule",
    "SeverityLevel",
    # Exceptions
    "AIModerationError",
    # Schemas
    "AIResponse",
    "AITarget",
    "AIInvestigation",
    "validate_ai_response",
]


async def setup(bot) -> None:
    """Add the AI Moderation cog to the bot.

    Usage in your bot's main file::

        await bot.load_extension("cogs.ai_moderation")
    """
    from .cog import AIModerationCog
    await bot.add_cog(AIModerationCog(bot))
