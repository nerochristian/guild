"""AutoMod detection rules.

Rules keep bounded in-memory state for fast abuse patterns. They do not delete
messages or punish users.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Iterable, Optional

from .models import Category, RuleMatch, Severity
from .utils import INVITE_RE, domain_matches, extract_domains, keyword_pattern, normalize_text, unique_strings


CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>")
DISGUISED_INVITE_RE = re.compile(
    r"(?ix)"
    r"\b(?:discord|d\s*i\s*s\s*c\s*o\s*r\s*d)\s*"
    r"(?:\.|\(dot\)|\[dot\]|dot|\s)+\s*"
    r"(?:gg|com\s*/\s*invite|app\s*\.\s*com\s*/\s*invite)\s*"
    r"(?:/|\\|\s)+\s*([A-Za-z0-9-]{2,32})"
)


class Rule:
    name = "base"
    setting_key = ""
    priority = 0

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        raise NotImplementedError

    def prune(self, now: float) -> None:
        return None


class BadWordsRule(Rule):
    name = "badwords"
    setting_key = "automod_badwords_enabled"
    priority = 100

    def __init__(self) -> None:
        self._cache_key: tuple[str, ...] = ()
        self._patterns: list[tuple[str, Any]] = []

    def _patterns_for(self, words: Iterable[Any]) -> list[tuple[str, Any]]:
        normalized = tuple(unique_strings(words))
        if normalized != self._cache_key:
            compiled: list[tuple[str, Any]] = []
            for word in normalized:
                pattern = keyword_pattern(word)
                if pattern is not None:
                    compiled.append((word, pattern))
            self._cache_key = normalized
            self._patterns = compiled
        return self._patterns

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = normalize_text(getattr(message, "content", ""))
        if not content:
            return None
        hits = [word for word, pattern in self._patterns_for(settings.get("automod_badwords", [])) if pattern.search(content)]
        if not hits:
            return None
        return RuleMatch(
            rule=self.name,
            reason="Blocked word or phrase",
            severity=Severity.HIGH if len(hits) > 1 else Severity.MEDIUM,
            category=Category.CONTENT,
            evidence=tuple(hits[:3]),
        )


class ScamRule(Rule):
    name = "scams"
    setting_key = "automod_scam_protection"
    priority = 110
    scam_phrases = (
        "free nitro",
        "nitro free",
        "claim your prize",
        "claim reward",
        "claim your reward",
        "airdrop",
        "discord gift",
        "discord staff",
        "discord moderator",
        "steam community gift",
        "steam gift",
        "steam wallet",
        "verify your account",
        "verify account",
        "account flagged",
        "account disabled",
        "qr code login",
        "crypto giveaway",
        "limited giveaway",
        "click this link",
        "password reset",
    )

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = normalize_text(getattr(message, "content", ""))
        if not content:
            return None
        domains = extract_domains(getattr(message, "content", ""))
        has_invite_or_link = bool(domains or INVITE_RE.search(getattr(message, "content", "") or ""))
        if not has_invite_or_link:
            return None
        if any(phrase in content for phrase in self.scam_phrases):
            return RuleMatch(
                self.name,
                "Likely scam or phishing message",
                Severity.CRITICAL if "@everyone" in content or "@here" in content else Severity.HIGH,
                Category.SECURITY,
                evidence=tuple(domains[:3]),
            )
        return None


class LinkRule(Rule):
    name = "links"
    setting_key = "automod_links_enabled"
    priority = 80
    suspicious_domains = ("bit.ly", "tinyurl.com", "tiny.one", "cutt.ly", "rb.gy", "is.gd", "grabify.link", "iplogger.org")

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        domains = extract_domains(getattr(message, "content", ""))
        if not domains:
            return None
        allowlist = list(settings.get("automod_links_whitelist", []) or []) + list(settings.get("automod_whitelisted_domains", []) or [])
        mode = str(settings.get("automod_links_mode", "dangerous")).strip().lower()
        blocked: list[str] = []
        for domain in domains:
            if any(domain_matches(domain, allowed) for allowed in allowlist):
                continue
            if mode == "allowlist" or any(domain_matches(domain, suspicious) for suspicious in self.suspicious_domains):
                blocked.append(domain)
        if not blocked:
            return None
        return RuleMatch(
            rule=self.name,
            reason="Domain is not allowed" if mode == "allowlist" else "Shortened or suspicious link",
            severity=Severity.HIGH if mode == "dangerous" else Severity.MEDIUM,
            category=Category.SECURITY if mode == "dangerous" else Category.CONTENT,
            evidence=tuple(blocked[:3]),
        )


class InviteRule(Rule):
    name = "invites"
    setting_key = "automod_invites_enabled"
    priority = 75

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = getattr(message, "content", "") or ""
        codes = [match.group(1).casefold() for match in INVITE_RE.finditer(content)]
        codes.extend(match.group(1).casefold() for match in DISGUISED_INVITE_RE.finditer(content))
        if not codes:
            return None
        allowed = {str(value).rsplit("/", 1)[-1].casefold() for value in settings.get("automod_allowed_invites", []) or []}
        blocked = [code for code in codes if code not in allowed]
        if not blocked:
            return None
        reason = "Discord invite is not allowed"
        if any(DISGUISED_INVITE_RE.search(content) for _ in (0,)):
            reason = "Disguised Discord invite is not allowed"
        return RuleMatch(self.name, reason, Severity.MEDIUM, Category.CONTENT, evidence=tuple(blocked[:3]))


class MentionRule(Rule):
    name = "mentions"
    setting_key = "automod_mentions_enabled"
    priority = 70

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        limit = max(1, min(50, int(settings.get("automod_max_mentions", 5))))
        user_ids = {getattr(item, "id", item) for item in getattr(message, "mentions", [])}
        role_ids = {getattr(item, "id", item) for item in getattr(message, "role_mentions", [])}
        total = len(user_ids) + len(role_ids)
        if total < limit:
            return None
        return RuleMatch(
            self.name,
            f"Mass mention spam ({total} unique mentions)",
            Severity.CRITICAL if total >= limit * 2 else Severity.HIGH,
            Category.BEHAVIOR,
            metadata={"count": total},
        )


class CapsRule(Rule):
    name = "caps"
    setting_key = "automod_caps_enabled"
    priority = 60

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = getattr(message, "content", "") or ""
        letters = [char for char in content if char.isalpha()]
        minimum = max(5, min(500, int(settings.get("automod_caps_min_length", 12))))
        if len(letters) < minimum:
            return None
        percentage = round(sum(1 for char in letters if char.isupper()) * 100 / len(letters))
        threshold = max(50, min(100, int(settings.get("automod_caps_percentage", 70))))
        if percentage < threshold:
            return None
        return RuleMatch(self.name, f"Excessive capital letters ({percentage}%)", Severity.LOW, Category.BEHAVIOR, metadata={"percentage": percentage})


class SpamRule(Rule):
    name = "spam"
    setting_key = "automod_spam_enabled"
    priority = 90

    def __init__(self) -> None:
        self._messages: dict[tuple[int, int], Deque[float]] = defaultdict(deque)

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = normalize_text(getattr(message, "content", ""))
        if not content:
            return None
        compact = "".join(content.split())
        if len(compact) >= 16:
            most_common = Counter(compact).most_common(1)[0][1]
            if most_common / len(compact) >= 0.82:
                return RuleMatch(self.name, "Repeated-character spam", Severity.LOW, Category.BEHAVIOR)
        if dry_run:
            return None
        guild_id = int(getattr(getattr(message, "guild", None), "id", 0))
        user_id = int(getattr(getattr(message, "author", None), "id", 0))
        now = time.monotonic()
        window = max(2, min(60, int(settings.get("automod_spam_window", 5))))
        limit = max(2, min(50, int(settings.get("automod_spam_threshold", 5))))
        entries = self._messages[(guild_id, user_id)]
        while entries and now - entries[0] > window:
            entries.popleft()
        entries.append(now)
        if len(entries) >= limit:
            return RuleMatch(self.name, f"Message flood ({len(entries)} messages in {window}s)", Severity.HIGH, Category.BEHAVIOR, metadata={"count": len(entries), "window": window})
        return None

    def prune(self, now: float) -> None:
        for key, entries in list(self._messages.items()):
            while entries and now - entries[0] > 120:
                entries.popleft()
            if not entries:
                self._messages.pop(key, None)


class DuplicateRule(Rule):
    name = "duplicates"
    setting_key = "automod_duplicates_enabled"
    priority = 85

    def __init__(self) -> None:
        self._messages: dict[tuple[int, int], Deque[tuple[float, str]]] = defaultdict(deque)

    @staticmethod
    def fingerprint(content: str) -> str:
        normalized = normalize_text(content)
        return hashlib.blake2s(normalized.encode("utf-8"), digest_size=8).hexdigest()

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = normalize_text(getattr(message, "content", ""))
        if not content or dry_run:
            return None
        guild_id = int(getattr(getattr(message, "guild", None), "id", 0))
        user_id = int(getattr(getattr(message, "author", None), "id", 0))
        now = time.monotonic()
        window = max(5, min(300, int(settings.get("automod_duplicate_window", 30))))
        limit = max(2, min(20, int(settings.get("automod_duplicate_threshold", 3))))
        fingerprint = self.fingerprint(content)
        entries = self._messages[(guild_id, user_id)]
        while entries and now - entries[0][0] > window:
            entries.popleft()
        entries.append((now, fingerprint))
        count = sum(1 for created_at, existing in entries if existing == fingerprint and now - created_at <= window)
        if count >= limit:
            return RuleMatch(self.name, f"Duplicate message spam ({count} copies in {window}s)", Severity.MEDIUM, Category.BEHAVIOR, metadata={"count": count, "window": window})
        return None

    def prune(self, now: float) -> None:
        for key, entries in list(self._messages.items()):
            while entries and now - entries[0][0] > 300:
                entries.popleft()
            if not entries:
                self._messages.pop(key, None)


class FastMessageRule(Rule):
    name = "fast_messages"
    setting_key = "automod_fast_messages_enabled"
    priority = 88

    def __init__(self) -> None:
        self._messages: dict[tuple[int, int], Deque[float]] = defaultdict(deque)

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        if dry_run or not normalize_text(getattr(message, "content", "")):
            return None
        guild_id = int(getattr(getattr(message, "guild", None), "id", 0))
        user_id = int(getattr(getattr(message, "author", None), "id", 0))
        now = time.monotonic()
        window = max(1, min(15, int(settings.get("automod_fast_message_window", 3))))
        limit = max(2, min(20, int(settings.get("automod_fast_message_threshold", 4))))
        entries = self._messages[(guild_id, user_id)]
        while entries and now - entries[0] > window:
            entries.popleft()
        entries.append(now)
        if len(entries) >= limit:
            return RuleMatch(self.name, f"Messages sent too quickly ({len(entries)} in {window}s)", Severity.MEDIUM, Category.BEHAVIOR, metadata={"count": len(entries), "window": window})
        return None

    def prune(self, now: float) -> None:
        for key, entries in list(self._messages.items()):
            while entries and now - entries[0] > 60:
                entries.popleft()
            if not entries:
                self._messages.pop(key, None)


class EmojiSpamRule(Rule):
    name = "emoji_spam"
    setting_key = "automod_emoji_spam_enabled"
    priority = 65

    @staticmethod
    def _emoji_count(content: str) -> int:
        custom = len(CUSTOM_EMOJI_RE.findall(content or ""))
        unicode_emoji = 0
        for char in content or "":
            if unicodedata.category(char) == "So" and not char.isalnum():
                unicode_emoji += 1
        return custom + unicode_emoji

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = getattr(message, "content", "") or ""
        if not content:
            return None
        emoji_count = self._emoji_count(content)
        threshold = max(4, min(100, int(settings.get("automod_emoji_spam_threshold", 14))))
        if emoji_count < threshold:
            return None
        non_space_length = max(1, len("".join(content.split())))
        ratio = round(emoji_count * 100 / non_space_length)
        ratio_threshold = max(20, min(100, int(settings.get("automod_emoji_spam_ratio", 65))))
        if ratio < ratio_threshold and emoji_count < threshold * 2:
            return None
        return RuleMatch(
            self.name,
            f"Emoji spam ({emoji_count} emojis, {ratio}% of message)",
            Severity.LOW if emoji_count < threshold * 2 else Severity.MEDIUM,
            Category.BEHAVIOR,
            metadata={"count": emoji_count, "ratio": ratio},
        )


class WallSpamRule(Rule):
    name = "wall_spam"
    setting_key = "automod_wall_spam_enabled"
    priority = 64

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = getattr(message, "content", "") or ""
        if not content:
            return None
        max_chars = max(200, min(4000, int(settings.get("automod_wall_spam_max_chars", 1800))))
        max_lines = max(3, min(80, int(settings.get("automod_wall_spam_max_lines", 12))))
        max_newlines = max(3, min(120, int(settings.get("automod_wall_spam_max_newlines", 10))))
        line_count = len(content.splitlines())
        newline_count = content.count("\n")
        if len(content) < max_chars and line_count < max_lines and newline_count < max_newlines:
            return None
        reason_parts: list[str] = []
        if len(content) >= max_chars:
            reason_parts.append(f"{len(content)} chars")
        if line_count >= max_lines:
            reason_parts.append(f"{line_count} lines")
        if newline_count >= max_newlines:
            reason_parts.append(f"{newline_count} newlines")
        return RuleMatch(
            self.name,
            f"Wall/spacer spam ({', '.join(reason_parts)})",
            Severity.LOW if len(content) < max_chars * 2 else Severity.MEDIUM,
            Category.BEHAVIOR,
            metadata={"chars": len(content), "lines": line_count, "newlines": newline_count},
        )


class AttachmentSpamRule(Rule):
    name = "attachments"
    setting_key = "automod_attachments_enabled"
    priority = 63

    def __init__(self) -> None:
        self._attachments: dict[tuple[int, int], Deque[tuple[float, int]]] = defaultdict(deque)

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        attachments = getattr(message, "attachments", []) or []
        count = len(attachments)
        if count <= 0:
            return None
        limit = max(2, min(25, int(settings.get("automod_attachment_threshold", 4))))
        if count >= limit:
            return RuleMatch(self.name, f"Attachment spam ({count} files in one message)", Severity.MEDIUM, Category.BEHAVIOR, metadata={"count": count})
        if dry_run:
            return None
        guild_id = int(getattr(getattr(message, "guild", None), "id", 0))
        user_id = int(getattr(getattr(message, "author", None), "id", 0))
        now = time.monotonic()
        window = max(5, min(120, int(settings.get("automod_attachment_window", 15))))
        entries = self._attachments[(guild_id, user_id)]
        while entries and now - entries[0][0] > window:
            entries.popleft()
        entries.append((now, count))
        total = sum(item_count for _, item_count in entries)
        if total >= limit:
            return RuleMatch(self.name, f"Attachment burst ({total} files in {window}s)", Severity.MEDIUM, Category.BEHAVIOR, metadata={"count": total, "window": window})
        return None

    def prune(self, now: float) -> None:
        for key, entries in list(self._attachments.items()):
            while entries and now - entries[0][0] > 180:
                entries.popleft()
            if not entries:
                self._attachments.pop(key, None)


class UnicodeSpamRule(Rule):
    name = "unicode_spam"
    setting_key = "automod_unicode_spam_enabled"
    priority = 62

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        content = getattr(message, "content", "") or ""
        if len(content) < 8:
            return None
        combining = sum(1 for char in content if unicodedata.combining(char))
        combining_limit = max(3, min(100, int(settings.get("automod_unicode_combining_threshold", 8))))
        if combining >= combining_limit:
            return RuleMatch(self.name, f"Unicode/Zalgo abuse ({combining} combining marks)", Severity.MEDIUM, Category.BEHAVIOR, metadata={"combining": combining})
        visible = [char for char in content if not char.isspace()]
        if len(visible) < 12:
            return None
        symbols = sum(1 for char in visible if unicodedata.category(char).startswith(("S", "M", "C")))
        ratio = round(symbols * 100 / max(1, len(visible)))
        ratio_limit = max(40, min(100, int(settings.get("automod_unicode_symbol_ratio", 70))))
        if ratio < ratio_limit:
            return None
        return RuleMatch(self.name, f"Unicode symbol spam ({ratio}% symbols/marks)", Severity.LOW, Category.BEHAVIOR, metadata={"ratio": ratio})


class NewAccountRule(Rule):
    name = "new_accounts"
    setting_key = "automod_newaccount_enabled"
    priority = 40

    async def check(self, message: Any, settings: dict[str, Any], *, dry_run: bool = False) -> Optional[RuleMatch]:
        author = getattr(message, "author", None)
        created_at = getattr(author, "created_at", None)
        message_time = getattr(message, "created_at", None)
        if created_at is None or message_time is None:
            return None
        threshold = max(0, min(365, int(settings.get("automod_newaccount_days", 7))))
        if threshold <= 0:
            return None
        age_days = max(0, int((message_time - created_at).total_seconds() // 86400))
        if age_days >= threshold:
            return None
        return RuleMatch(self.name, f"Account is only {age_days} day{'s' if age_days != 1 else ''} old", Severity.INFO, Category.IDENTITY, delete_message=False, metadata={"age_days": age_days})


ALL_RULES: tuple[type[Rule], ...] = (
    ScamRule,
    BadWordsRule,
    SpamRule,
    FastMessageRule,
    DuplicateRule,
    LinkRule,
    InviteRule,
    MentionRule,
    CapsRule,
    EmojiSpamRule,
    WallSpamRule,
    AttachmentSpamRule,
    UnicodeSpamRule,
    NewAccountRule,
)
