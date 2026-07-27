"""Shared AutoMod types.

These dataclasses keep rule detection separate from Discord actions. Rules only
return facts; the cog decides whether to delete, warn, timeout, kick, or ban.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class Severity(Enum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Category(str, Enum):
    CONTENT = "content"
    BEHAVIOR = "behavior"
    SECURITY = "security"
    IDENTITY = "identity"
    RAID = "raid"


class Action(str, Enum):
    NONE = "none"
    LOG = "log"
    WARN = "warn"
    MUTE = "mute"
    TIMEOUT = "timeout"
    KICK = "kick"
    BAN = "ban"


@dataclass(frozen=True)
class RuleMatch:
    rule: str
    reason: str
    severity: Severity
    category: Category
    delete_message: bool = True
    evidence: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ViolationRecord:
    guild_id: int
    user_id: int
    channel_id: Optional[int]
    rule: str
    reason: str
    action: str
    severity: str
    created_at: float
