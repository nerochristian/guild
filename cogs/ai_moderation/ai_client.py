"""
AI client — provider abstraction with circuit breaker, retry, and
Pydantic schema validation.

The AI client NEVER lets the model execute actions directly. Instead:
1. The system prompt is sent as an immutable system message.
2. User content + evidence are sent as user messages, clearly delimited.
3. The model's response is parsed as JSON and validated against the
   Pydantic schema in schemas.py.
4. If validation fails, an error is returned — the model's output is
   never trusted blindly.

Supports two providers:
- DeepSeek Web (browser-based, no API key needed).
- DigitalOcean inference API (fallback, requires DO_API_KEY).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import aiohttp
import discord
from discord.ext import commands

from .exceptions import (
    AITimeoutError,
    AIUnavailableError,
    CircuitBreakerOpenError,
    InvalidAIResponseError,
    AIRateLimitError,
)
from .schemas import AIResponse, validate_ai_response

logger = logging.getLogger("ModBot.AIModeration.AIClient")


_DO_API_KEY: str = os.getenv("DO_API_KEY", "").strip()
_DO_BASE_URL: str = (
    os.getenv("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
    .strip()
    .rstrip("/")
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _deepseek_web_timeout() -> float:
    raw = os.getenv("DEEPSEEK_WEB_PRIMARY_TIMEOUT", "30").strip()
    try:
        return min(90.0, max(5.0, float(raw)))
    except ValueError:
        return 30.0


# =============================================================================
# Circuit breaker
# =============================================================================


class CircuitBreaker:
    """Simple failure-count circuit breaker with auto-reset."""

    def __init__(self, *, failure_threshold: int = 5, reset_seconds: int = 300) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._reset_seconds = max(1, reset_seconds)
        self._failures = 0
        self._open_until: Optional[datetime] = None

    @property
    def is_open(self) -> bool:
        if self._open_until is None:
            return False
        if _now() >= self._open_until:
            self._open_until = None
            return False
        return True

    def remaining_seconds(self) -> int:
        if not self.is_open or self._open_until is None:
            return 0
        return max(1, int((self._open_until - _now()).total_seconds()))

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open_until = _now() + timedelta(seconds=self._reset_seconds)
            logger.warning(
                "Circuit breaker opened for %ds after %d failures",
                self._reset_seconds, self._failures,
            )


# =============================================================================
# AI client
# =============================================================================


class AIClient:
    """Provider-agnostic AI client with validation and resilience.

    Architecture:
      1. Provider layer: _call() → DeepSeek web → DigitalOcean fallback
      2. Resilience layer: circuit breaker + retry with backoff
      3. Validation layer: every response validated via Pydantic
      4. Rate limiting: per-user rate limiter
    """

    _MAX_RETRIES = 2
    _RETRY_BASE_DELAY = 1.5
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Rate limiting.
        from utils.cache import RateLimiter  # type: ignore[import-not-found]
        self._rate_limiter = RateLimiter(
            max_calls=int(os.getenv("AI_RATE_LIMIT_CALLS", "30")),
            window_seconds=int(os.getenv("AI_RATE_LIMIT_WINDOW", "60")),
        )

        # Circuit breaker.
        self._circuit = CircuitBreaker()

        # Provider state.
        self._block_until: Optional[datetime] = None
        self._block_reason: Optional[str] = None

        # Try to load DeepSeek web client.
        self._deepseek_web = None
        try:
            from utils.deepseek_web import DeepSeekWebClient  # type: ignore[import-not-found]
            self._deepseek_web = DeepSeekWebClient()
        except ImportError:
            logger.info("DeepSeek web client not available; using DigitalOcean only.")

        # Background tasks.
        self._background_tasks: Set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        if self._deepseek_web and getattr(self._deepseek_web, "enabled", False):
            return True
        return bool(_DO_API_KEY)

    def availability_message(self) -> str:
        if self._deepseek_web and getattr(self._deepseek_web, "enabled", False):
            if _DO_API_KEY:
                return "DeepSeek web is enabled with DigitalOcean fallback."
            return "DeepSeek web is enabled (no fallback configured)."
        if _DO_API_KEY:
            return "Using DigitalOcean inference API."
        return "No AI provider is configured. Set DO_API_KEY or enable DeepSeek web."

    def diagnostic_lines(self) -> List[str]:
        lines = []
        if self._deepseek_web:
            lines.append(
                f"DeepSeek web: {'enabled' if getattr(self._deepseek_web, 'enabled', False) else 'disabled'}"
            )
            lines.append(f"  Timeout: {getattr(self._deepseek_web, 'timeout_seconds', '?')}s")
        lines.append(f"DigitalOcean API key: {'configured' if _DO_API_KEY else 'missing'}")
        lines.append(f"Circuit breaker: {'open' if self._circuit.is_open else 'closed'}")
        if self._circuit.is_open:
            lines.append(f"  Resets in ~{self._circuit.remaining_seconds()}s")
        lines.append(f"Available: {'yes' if self.is_available else 'no'}")
        return lines

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._deepseek_web:
            await self._deepseek_web.close()

    async def prewarm(self) -> None:
        if self._deepseek_web:
            await self._deepseek_web.prewarm()

    # ------------------------------------------------------------------
    # Service-block helpers
    # ------------------------------------------------------------------

    def _set_block(self, *, seconds: int, reason: str) -> None:
        self._block_until = _now() + timedelta(seconds=max(1, seconds))
        self._block_reason = reason
        logger.warning("AI service blocked for %ds: %s", seconds, reason)

    def _get_block_message(self) -> Optional[str]:
        if not self._block_until:
            return None
        remaining = (self._block_until - _now()).total_seconds()
        if remaining <= 0:
            self._block_until = None
            self._block_reason = None
            return None
        mins = max(1, int(remaining // 60))
        return f"{self._block_reason} Try again in ~{mins}m."

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    async def _preflight(self, user_id: int) -> Optional[str]:
        """Return an error string if the call should be blocked, else None."""
        blocked = self._get_block_message()
        if blocked:
            return blocked
        if self._circuit.is_open:
            raise CircuitBreakerOpenError(self._circuit.remaining_seconds())
        is_limited, retry_after = await self._rate_limiter.is_rate_limited(user_id)
        if is_limited:
            raise AIRateLimitError(retry_after)
        return None

    # ------------------------------------------------------------------
    # HTTP session
    # ------------------------------------------------------------------

    def _get_http_session(self, *, timeout: int) -> tuple[aiohttp.ClientSession, bool]:
        session: Optional[aiohttp.ClientSession] = getattr(self.bot, "session", None)
        if not session or getattr(session, "closed", False):
            return (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)),
                True,
            )
        return session, False

    # ------------------------------------------------------------------
    # Main call — routes to DeepSeek web or DigitalOcean
    # ------------------------------------------------------------------

    async def _call(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        model: Optional[str] = None,
        json_mode: bool = False,
        session_key: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> Optional[str]:
        """Call the AI provider with retry and circuit breaker.

        Returns the raw text response, or None if no response.
        """
        if not self.is_available:
            raise AIUnavailableError()

        # Try DeepSeek web first.
        if self._deepseek_web and getattr(self._deepseek_web, "enabled", False):
            try:
                prompt_parts: List[str] = []
                for msg in messages:
                    role = str(msg.get("role") or "user").upper()
                    content = self._stringify_content(msg.get("content"))
                    if content:
                        prompt_parts.append(f"[{role}]\n{content}")
                if json_mode:
                    prompt_parts.append(
                        "[OUTPUT FORMAT]\nReturn exactly one valid JSON object and no other text."
                    )
                result = await asyncio.wait_for(
                    self._deepseek_web.chat(
                        "\n\n".join(prompt_parts),
                        session_key=session_key,
                        session_name=session_name,
                    ),
                    timeout=_deepseek_web_timeout(),
                )
                self._circuit.record_success()
                return result
            except asyncio.TimeoutError:
                logger.warning("DeepSeek web timed out; falling back to DigitalOcean.")
                self._circuit.record_failure()
            except Exception as exc:
                logger.warning("DeepSeek web failed (%s); falling back.", exc)
                self._circuit.record_failure()

        # Fallback to DigitalOcean.
        return await self._call_digitalocean(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            json_mode=json_mode,
        )

    async def _call_digitalocean(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        model: Optional[str] = None,
        json_mode: bool = False,
    ) -> Optional[str]:
        """Call the DigitalOcean inference API with retry."""
        if not _DO_API_KEY:
            raise AIUnavailableError()

        selected_model = (model or os.getenv("DO_PROFILE_MODEL", "deepseek-4-flash")).strip()
        payload: Dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Optional[Exception] = None
        for attempt in range(self._MAX_RETRIES + 1):
            session, owned = self._get_http_session(timeout=60)
            data: Optional[Any] = None
            try:
                async with session.post(
                    f"{_DO_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_DO_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        detail = (
                            data.get("error", data)
                            if isinstance(data, dict) else data
                        )
                        if resp.status in {401, 403}:
                            self._set_block(
                                seconds=900,
                                reason="DigitalOcean auth failed.",
                            )
                            raise AIUnavailableError()
                        if resp.status == 429:
                            self._set_block(
                                seconds=60,
                                reason="DigitalOcean rate limit reached.",
                            )
                            raise AIRateLimitError()
                        if resp.status in self._RETRYABLE_STATUS and attempt < self._MAX_RETRIES:
                            delay = self._RETRY_BASE_DELAY * (2 ** attempt)
                            await asyncio.sleep(delay)
                            continue
                        raise AIUnavailableError()

                self._circuit.record_success()
                if not isinstance(data, dict):
                    return None
                choices = data.get("choices") or []
                if not choices:
                    return None
                content = (choices[0] or {}).get("message", {}).get("content")
                return content if isinstance(content, str) else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise
            finally:
                if owned:
                    await session.close()

        self._circuit.record_failure()
        if last_error:
            raise AITimeoutError(60.0) from last_error
        return None

    # ------------------------------------------------------------------
    # Content normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _stringify_content(content: Any) -> str:
        """Convert message content to a string (handles multimodal lists)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text = str(item.get("text") or "").strip()
                        if text:
                            parts.append(text)
                    elif item.get("type") == "image_url":
                        parts.append("[Image omitted from text-only request]")
                else:
                    text = str(item).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts)
        return str(content or "")

    # ------------------------------------------------------------------
    # Public API: analyze moderation instruction
    # ------------------------------------------------------------------

    async def analyze_instruction(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        evidence_block: str = "",
        conversation_block: str = "",
        guild_id: int,
        author_id: int,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> AIResponse:
        """Send a moderation instruction to the AI and get a validated response.

        This is the main entry point. The system prompt is immutable;
        user content is clearly delimited to prevent prompt injection.

        Args:
            system_prompt: The immutable system prompt (from prompts.py).
            user_prompt: The moderator's instruction.
            evidence_block: Pre-formatted evidence (from context_collector).
            conversation_block: Pre-formatted recent conversation.
            guild_id: Guild ID (for rate limiting).
            author_id: Moderator ID (for rate limiting).
            model: Optional model override.
            temperature: Generation temperature.

        Returns:
            A validated AIResponse.

        Raises:
            AIUnavailableError: No provider available.
            AITimeoutError: Provider timed out.
            CircuitBreakerOpenError: Too many failures.
            InvalidAIResponseError: AI returned invalid JSON.
            AIRateLimitError: Rate limit hit.
        """
        # Pre-flight checks.
        await self._preflight(author_id)

        # Build the user message with clear section delimiters.
        user_content_parts: List[str] = []
        if conversation_block:
            user_content_parts.append(conversation_block)
        if evidence_block:
            user_content_parts.append(evidence_block)
        user_content_parts.append(f"=== MODERATOR INSTRUCTION ===\n{user_prompt}\n=== END INSTRUCTION ===")

        user_content = "\n\n".join(user_content_parts)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        await self._rate_limiter.record_call(author_id)

        try:
            raw = await self._call(
                messages,
                temperature=temperature,
                max_tokens=2000,
                model=model,
                json_mode=True,
                session_key=f"{guild_id}:moderation",
                session_name=f"Guild {guild_id} moderation",
            )
        except (AIUnavailableError, AITimeoutError, CircuitBreakerOpenError, AIRateLimitError):
            raise
        except Exception as exc:
            logger.exception("Unexpected AI call error")
            self._circuit.record_failure()
            raise AIUnavailableError() from exc

        if not raw:
            raise InvalidAIResponseError("(empty response)", "AI returned no content")

        # Validate the response against the Pydantic schema.
        return validate_ai_response(raw)

    # ------------------------------------------------------------------
    # Background task management
    # ------------------------------------------------------------------

    def _spawn_background(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
