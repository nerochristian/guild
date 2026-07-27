"""Compatibility helpers for AI-assisted AutoMod setup tests.

This module predates the interactive `cogs.automod.wizard` implementation in
some tests. The helpers below keep that public API available while reusing the
new deterministic AutoMod settings schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import discord

from cogs.automod.config import PUNISHMENTS, default_settings
from cogs.automod.utils import normalize_domain


@dataclass(frozen=True)
class SetupProfile:
    name: str
    description: str
    focus: str = ""


@dataclass(frozen=True)
class SetupQuestion:
    key: str
    question: str
    type: str = "choice"
    options: tuple[str, ...] = ()

    @property
    def is_closed(self) -> bool:
        return self.type.casefold() in {"choice", "select", "multi_choice", "boolean"} or bool(self.options)


class SetupReviewView(discord.ui.View):
    """Small review view used by the legacy AI setup flow/tests."""

    def __init__(self, user_id: int, settings: dict[str, Any], raw_payload: dict[str, Any], summary: str) -> None:
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        self.settings = settings
        self.raw_payload = raw_payload
        self.summary = summary
        for label in ("Blocked Words", "Links", "Invites", "Limits", "Actions"):
            self.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary))
        self.add_item(discord.ui.Button(label="Save Setup", style=discord.ButtonStyle.success))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _normalize_invite(value: Any) -> str:
    text = str(value or "").strip().casefold().rstrip("/.,; ")
    if not text:
        return ""
    text = text.rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9_-]", "", text)[:64]


def _normalize_list(key: str, values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if key == "automod_allowed_invites":
            item = _normalize_invite(raw)
            min_len = 2
        elif key in {"automod_whitelisted_domains", "automod_links_whitelist"}:
            item = normalize_domain(str(raw)) or str(raw or "").strip().casefold()
            min_len = 2
        else:
            item = " ".join(str(raw or "").strip().casefold().split())
            min_len = 2
        if item in {"", "none", "null", "n/a"} or len(item) < min_len:
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= 100:
            break
    return out


def validate_automod_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitize an AI/generated AutoMod settings payload.

    Unknown keys are dropped, booleans and lists are normalized, punishments are
    made Discord-compatible (`mute` -> `timeout`), and risky numeric values are
    bounded by the default schema type.
    """

    data = payload.get("settings", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        raise ValueError("Invalid AutoMod setup payload")
    defaults = default_settings()
    out: dict[str, Any] = {}
    int_ranges: dict[str, tuple[int, int]] = {
        "automod_spam_threshold": (2, 50),
        "automod_spam_window": (2, 60),
        "automod_duplicate_threshold": (2, 20),
        "automod_duplicate_window": (5, 300),
        "automod_fast_message_threshold": (2, 20),
        "automod_fast_message_window": (1, 15),
        "automod_max_mentions": (1, 50),
        "automod_caps_percentage": (50, 100),
        "automod_mute_duration": (60, 2419200),
        "warn_mute_duration": (60, 2419200),
        "warn_threshold_mute": (0, 100),
        "warn_threshold_kick": (0, 100),
        "warn_threshold_ban": (0, 100),
    }
    for key, value in data.items():
        if key not in defaults:
            continue
        default = defaults[key]
        if isinstance(default, bool):
            if isinstance(value, str):
                out[key] = value.strip().casefold() in {"1", "true", "yes", "on", "enabled", "enable"}
            else:
                out[key] = bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            low, high = int_ranges.get(key, (0, 2592000))
            if low <= number <= high:
                out[key] = number
        elif isinstance(default, list):
            out[key] = _normalize_list(key, value)
        elif isinstance(default, dict):
            if isinstance(value, dict):
                out[key] = value
        elif key.endswith("punishment") or key.endswith("action") or key == "automod_punishment":
            action = str(value or "").strip().casefold()
            action = "timeout" if action == "mute" else action
            if action in PUNISHMENTS:
                out[key] = action
        elif isinstance(value, str):
            out[key] = value.strip()[:800]
        else:
            out[key] = value
    if not out:
        raise ValueError("No valid AutoMod settings found")
    return out


def _parse_profiles(payload: dict[str, Any]) -> list[SetupProfile]:
    raw_profiles = payload.get("profiles", []) if isinstance(payload, dict) else []
    profiles: list[SetupProfile] = []
    seen: set[str] = set()
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        focus = str(item.get("focus") or "").strip()
        folded = name.casefold()
        if name and description and folded not in seen:
            profiles.append(SetupProfile(name=name[:60], description=description[:200], focus=focus[:160]))
            seen.add(folded)
    if len(profiles) < 3:
        raise ValueError("Expected at least three distinct setup profiles")
    return profiles


def _parse_generated_questions(payload: dict[str, Any]) -> list[SetupQuestion]:
    raw_questions = payload.get("questions", []) if isinstance(payload, dict) else []
    questions: list[SetupQuestion] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or f"q{len(questions) + 1}").strip()[:50]
        question = str(item.get("question") or "").strip()
        qtype = str(item.get("type") or "choice").strip().casefold()
        options = tuple(str(option).strip()[:80] for option in item.get("options", []) or [] if str(option).strip())
        if question:
            questions.append(SetupQuestion(key=key, question=question[:200], type=qtype, options=options[:10]))
    if len(questions) < 3:
        raise ValueError("Expected multiple setup questions")
    closed = sum(question.is_closed for question in questions)
    if closed < max(1, len(questions) - 1):
        raise ValueError("Setup should mostly use closed questions")
    return questions


def _blocked_link_summary(settings: dict[str, Any]) -> str:
    if not settings.get("automod_links_enabled", True):
        return "Link filtering is disabled."
    mode = str(settings.get("automod_links_mode", "dangerous")).casefold()
    if mode == "allowlist":
        domains = list(settings.get("automod_links_whitelist", []) or []) + list(settings.get("automod_whitelisted_domains", []) or [])
        suffix = f" Allowed domains include: {', '.join(domains[:8])}." if domains else " No custom domains are currently allowed."
        return "Blocks **every link domain that is not allowed**." + suffix
    return "Blocks suspicious shortened links and tracking domains like bit.ly, tinyurl.com, grabify.link, and iplogger.org."


def _blocked_invite_summary(settings: dict[str, Any]) -> str:
    if not settings.get("automod_invites_enabled", True):
        return "Discord invite filtering is disabled."
    allowed = list(settings.get("automod_allowed_invites", []) or [])
    if not allowed:
        return "Blocks **all Discord invite links**."
    return f"Blocks every Discord invite link except these invite codes: {', '.join(allowed[:12])}."


def _review_description(settings: dict[str, Any], summary: str) -> str:
    words = list(settings.get("automod_badwords", []) or [])
    return (
        "Review what this setup will block before saving.\n\n"
        f"{summary}\n\n"
        f"**Blocked Words**\n{', '.join(words[:20]) if words else 'No custom blocked words configured.'}\n\n"
        f"**Links**\n{_blocked_link_summary(settings)}\n\n"
        f"**Invites and Security**\n{_blocked_invite_summary(settings)}\n"
        f"Scam protection: **{bool(settings.get('automod_scam_protection', True))}**\n\n"
        "**Limits and Actions**\n"
        f"Spam: `{settings.get('automod_spam_threshold', 5)}/{settings.get('automod_spam_window', 5)}s`\n"
        f"Mentions: `{settings.get('automod_max_mentions', 5)}`\n"
        f"Default action: `{settings.get('automod_punishment', 'warn')}`\n"
        f"Security action: `{settings.get('automod_security_punishment', 'timeout')}`"
    )[:3900]


def _setup_user_prompt(guild: Any, goal: str, profile: SetupProfile, answers: list[dict[str, Any]]) -> str:
    payload = {
        "guild": {
            "id": getattr(guild, "id", None),
            "name": getattr(guild, "name", "Unknown"),
            "member_count": getattr(guild, "member_count", None),
        },
        "goal": goal,
        "selected_profile": profile.__dict__,
        "answers": answers,
        "schema": {
            "settings": sorted(default_settings().keys()),
            "notes": "Return JSON only. Do not invent default bad-word/slur lists; only include words explicitly provided by the admin.",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def call_deepseek_json(
    cog: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 1200,
    session_key: str | None = None,
    session_name: str | None = None,
) -> dict[str, Any]:
    ai_cog = cog.bot.get_cog("AIModeration") if getattr(cog, "bot", None) is not None else None
    ai = getattr(ai_cog, "ai", None)
    web = getattr(ai, "_deepseek_web", None)
    if web is not None and getattr(web, "enabled", False):
        timeout = float(os.environ.get("DEEPSEEK_WEB_PRIMARY_TIMEOUT", "8"))
        try:
            result = await asyncio.wait_for(
                web.chat(user_prompt, system_prompt=system_prompt, session_key=session_key, session_name=session_name),
                timeout=timeout,
            )
            return _extract_json_object(str(result))
        except Exception:
            pass
    if ai is not None and hasattr(ai, "_call_digitalocean"):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        result = await ai._call_digitalocean(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        return _extract_json_object(str(result))
    raise RuntimeError("No DeepSeek provider is available")
