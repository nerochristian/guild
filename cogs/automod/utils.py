"""Utility helpers for the AutoMod package."""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import discord

from utils.checks import is_bot_owner_id


ZERO_WIDTH_TRANSLATION = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")
LEET_TRANSLATION = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
    }
)
URL_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>]+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?)")
INVITE_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/([\w-]+)")
DURATION_RE = re.compile(r"(?P<count>\d+)(?P<unit>[smhd])", re.IGNORECASE)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").translate(ZERO_WIDTH_TRANSLATION)
    value = value.casefold().translate(LEET_TRANSLATION)
    value = "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip()


def keyword_pattern(keyword: str) -> Optional[re.Pattern[str]]:
    normalized = normalize_text(keyword)
    characters = [re.escape(char) for char in normalized if char.isalnum()]
    if not characters:
        return None
    return re.compile(rf"(?<!\w){r'[\W_]*'.join(characters)}(?!\w)", re.IGNORECASE)


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value or "").strip().casefold()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_domain(value: str) -> str:
    raw = (value or "").strip().casefold().rstrip(".")
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        host = urlsplit(candidate).hostname or ""
        return host.encode("idna").decode("ascii").casefold().rstrip(".")
    except (UnicodeError, ValueError):
        return ""


def domain_matches(domain: str, configured_domain: str) -> bool:
    configured = normalize_domain(configured_domain)
    return bool(configured and (domain == configured or domain.endswith(f".{configured}")))


def extract_domains(content: str) -> list[str]:
    domains: list[str] = []
    for raw in URL_RE.findall(content or ""):
        domain = normalize_domain(raw.rstrip(".,;:!?)]}\""))
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def parse_duration(value: str, *, minimum: int = 1, maximum: int = 2419200) -> Optional[int]:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    total = 0
    position = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for match in DURATION_RE.finditer(raw):
        if match.start() != position:
            return None
        total += int(match.group("count")) * multipliers[match.group("unit")]
        position = match.end()
    if position != len(raw) or not minimum <= total <= maximum:
        return None
    return total


def compact_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    parts: list[str] = []
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{suffix}")
    return "".join(parts) or "0s"


def parse_threshold_pair(value: str, *, count_range: tuple[int, int], window_range: tuple[int, int]) -> Optional[tuple[int, int]]:
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value or "")
    if not match:
        return None
    count, window = int(match.group(1)), int(match.group(2))
    if not count_range[0] <= count <= count_range[1]:
        return None
    if not window_range[0] <= window <= window_range[1]:
        return None
    return count, window


def id_list(values: Iterable[Any]) -> set[int]:
    ids: set[int] = set()
    for value in values or []:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


async def can_manage_automod(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    user = interaction.user
    if is_bot_owner_id(user.id) or user.id == interaction.guild.owner_id:
        return True
    perms = user.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_messages:
        return True
    settings = await interaction.client.db.get_settings(interaction.guild.id)
    role_ids = {role.id for role in user.roles}
    return bool(role_ids & id_list(settings.get("mod_roles", [])) or role_ids & id_list(settings.get("admin_roles", [])))


def bot_can_act_on(member: discord.Member, action: str) -> tuple[bool, str]:
    guild = member.guild
    bot_member = guild.me
    if bot_member is None:
        return False, "Bot member is unavailable."
    if member.id == guild.owner_id:
        return False, "Discord does not allow moderating the server owner."
    if member.top_role >= bot_member.top_role:
        return False, "Target has a role equal to or higher than the bot."
    permissions = bot_member.guild_permissions
    if action in {"timeout", "mute"} and not permissions.moderate_members:
        return False, "Bot needs Moderate Members."
    if action == "kick" and not permissions.kick_members:
        return False, "Bot needs Kick Members."
    if action == "ban" and not permissions.ban_members:
        return False, "Bot needs Ban Members."
    return True, ""


def timeout_delta(seconds: int) -> timedelta:
    return timedelta(seconds=max(1, min(int(seconds), 2419200)))
