from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
from typing import Any, Optional

import discord

from config import Config

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_BRAILLE_BLANK = "\u2800"
_INVALID_EMOJI_NAME_RE = re.compile(r"[^a-z0-9_]")
_DUP_UNDERSCORE_RE = re.compile(r"_+")
_VERSION_SUFFIX_RE = re.compile(r"_v\d+$", re.IGNORECASE)
_SHORTCODE_RE = re.compile(r":([A-Za-z0-9_]{2,32}):")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d+)>")

_EMOJI_META = {
    "success": {
        "config_icon": "EMOJI_SUCCESS",
        "default_icon": "\u2705",
        "config_name": "STATUS_SUCCESS_EMOJI_NAME",
        "default_name": "mod_success",
        "asset": "emoji_success.gif",
    },
    "error": {
        "config_icon": "EMOJI_ERROR",
        "default_icon": "\u274c",
        "config_name": "STATUS_ERROR_EMOJI_NAME",
        "default_name": "mod_error",
        "asset": "emoji_error.gif",
    },
    "warning": {
        "config_icon": "EMOJI_WARNING",
        "default_icon": "\u26a0\ufe0f",
        "config_name": "STATUS_WARNING_EMOJI_NAME",
        "default_name": "mod_warn",
        "asset": "emoji_warn.gif",
    },
    "info": {
        "config_icon": "EMOJI_INFO",
        "default_icon": "\u2139\ufe0f",
        "config_name": "STATUS_INFO_EMOJI_NAME",
        "default_name": "mod_info",
        "asset": "emoji_info.gif",
    },
    "lock": {
        "config_icon": "EMOJI_LOCK",
        "default_icon": "\U0001f512",
        "config_name": "STATUS_LOCK_EMOJI_NAME",
        "default_name": "mod_lock",
        "asset": "emoji_lock.gif",
    },
    "unlock": {
        "config_icon": "EMOJI_UNLOCK",
        "default_icon": "\U0001f513",
        "config_name": "STATUS_UNLOCK_EMOJI_NAME",
        "default_name": "mod_unlock",
        "asset": "emoji_unlock.gif",
    },
    "loading": {
        "config_icon": "EMOJI_LOADING",
        "default_icon": "\u23f3",
        "config_name": "STATUS_LOADING_EMOJI_NAME",
        "default_name": "mod_loading",
        "asset": "emoji_loading.gif",
    },
    "ban": {
        "config_icon": "EMOJI_BAN",
        "default_icon": "\U0001f528",
        "config_name": "STATUS_BAN_EMOJI_NAME",
        "default_name": "mod_ban",
        "asset": "emoji_ban.gif",
    },
    "kick": {
        "config_icon": "EMOJI_KICK",
        "default_icon": "\U0001f462",
        "config_name": "STATUS_KICK_EMOJI_NAME",
        "default_name": "mod_kick",
        "asset": "emoji_kick.gif",
    },
    "mute": {
        "config_icon": "EMOJI_MUTE",
        "default_icon": "\U0001f507",
        "config_name": "STATUS_MUTE_EMOJI_NAME",
        "default_name": "mod_mute",
        "asset": "emoji_mute.gif",
    },
    "warn": {
        "config_icon": "EMOJI_WARN",
        "default_icon": "\u26a0\ufe0f",
        "config_name": "STATUS_WARN_EMOJI_NAME",
        "default_name": "mod_warn",
        "asset": "emoji_warn.gif",
    },
}

_emoji_cache: dict[tuple[int, str, str], str] = {}
_emoji_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
_emoji_locks_guard = asyncio.Lock()
_application_emoji_cache: dict[tuple[str, str], str] = {}
_application_kind_cache: dict[str, str] = {}
_application_name_cache: dict[str, str] = {}
_application_sync_lock = asyncio.Lock()


def _bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _get_lock(key: tuple[int, str, str]) -> asyncio.Lock:
    lock = _emoji_locks.get(key)
    if lock is not None:
        return lock
    async with _emoji_locks_guard:
        lock = _emoji_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _emoji_locks[key] = lock
        return lock


def _looks_custom_emoji(value: str) -> bool:
    text = (value or "").strip()
    return text.startswith("<:") or text.startswith("<a:")


def _normalize_emoji_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if not name:
        return ""
    name = _INVALID_EMOJI_NAME_RE.sub("_", name)
    name = _DUP_UNDERSCORE_RE.sub("_", name).strip("_")
    name = _VERSION_SUFFIX_RE.sub("", name)
    return name[:32]


def _emoji_name(meta: dict[str, Any]) -> str:
    configured = str(
        getattr(Config, meta["config_name"], meta["default_name"]) or meta["default_name"]
    ).strip()
    normalized = _normalize_emoji_name(configured)
    if normalized:
        return normalized

    fallback = _normalize_emoji_name(str(meta.get("default_name", "") or "").strip())
    if fallback:
        return fallback

    return "mod_status"


def _is_legacy_version_name(name: str, base_name: str) -> bool:
    if not base_name:
        return False
    return bool(re.fullmatch(rf"{re.escape(base_name)}_v\d+", str(name), flags=re.IGNORECASE))


def _asset_candidates(meta: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    assets = meta.get("assets")
    if isinstance(assets, (list, tuple, set, frozenset)):
        for value in assets:
            name = str(value or "").strip()
            if name and name not in candidates:
                candidates.append(name)
    asset = str(meta.get("asset", "") or "").strip()
    if asset and asset not in candidates:
        candidates.append(asset)
    return candidates


def _pick_existing_emoji(
    emojis: list[discord.Emoji],
    *,
    name: str,
    prefer_animated: bool = False,
) -> Optional[discord.Emoji]:
    same_name = [emoji for emoji in emojis if emoji.name == name]
    if not same_name:
        return None
    if prefer_animated:
        animated = next((emoji for emoji in same_name if getattr(emoji, "animated", False)), None)
        if animated is not None:
            return animated
    return same_name[0]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _auto_render_from_svg(asset_name: str) -> bool:
    """If a GIF asset is missing or its SVG source is newer, render it."""
    asset_path = _ASSETS_DIR / asset_name
    stem = asset_path.stem  # e.g. "emoji_success"
    svg_path = _ASSETS_DIR / f"{stem}.svg"

    if not svg_path.exists():
        return asset_path.exists()

    # Regenerate if GIF is missing OR SVG was modified after GIF was created
    needs_render = not asset_path.exists()
    if not needs_render and asset_path.exists():
        svg_mtime = svg_path.stat().st_mtime
        gif_mtime = asset_path.stat().st_mtime
        if svg_mtime > gif_mtime:
            needs_render = True

    if not needs_render:
        return True

    try:
        from scripts.convert_emoji_assets import render_svg_sync
        gif_path = _ASSETS_DIR / f"{stem}.gif"
        render_svg_sync(svg_path, gif_path)
        return asset_path.exists()
    except Exception:
        return False


async def _pick_asset_payload(
    meta: dict[str, Any],
) -> Optional[tuple[str, bytes, bool, str]]:
    for asset_name in _asset_candidates(meta):
        # Auto-render from SVG if the asset file doesn't exist yet
        if not (_ASSETS_DIR / asset_name).exists():
            await asyncio.to_thread(_auto_render_from_svg, asset_name)

        asset_path = _ASSETS_DIR / asset_name
        if not asset_path.exists():
            continue

        emoji_bytes = await asyncio.to_thread(asset_path.read_bytes)
        if len(emoji_bytes) > 256 * 1024:
            continue

        return (
            asset_name,
            emoji_bytes,
            asset_name.lower().endswith(".gif"),
            _sha256_bytes(emoji_bytes),
        )
    return None


async def _read_emoji_bytes(emoji: Any) -> Optional[bytes]:
    reader = getattr(emoji, "read", None)
    if callable(reader):
        try:
            return await reader()
        except Exception:
            pass

    url = getattr(emoji, "url", None)
    url_reader = getattr(url, "read", None) if url is not None else None
    if callable(url_reader):
        try:
            return await url_reader()
        except Exception:
            return None

    return None


async def _emoji_matches_payload(
    emoji: Any,
    *,
    payload_hash: str,
    require_animated: bool = False,
) -> bool:
    if require_animated and not bool(getattr(emoji, "animated", False)):
        return False

    # Discord transcodes uploaded emojis (e.g. compressing GIFs), which changes their bytes
    # and invalidates the SHA256 hash. If we strictly check the hash, the bot will delete
    # and recreate the emoji on every startup, breaking all previously sent messages.
    # Therefore, if the emoji exists and matches the animated requirement, we consider it valid.
    return True


async def _delete_application_emoji(bot: discord.Client, emoji: Any) -> bool:
    deleter = getattr(emoji, "delete", None)
    if callable(deleter):
        try:
            await deleter()
            return True
        except Exception:
            pass

    emoji_id = getattr(emoji, "id", None)
    bot_deleter = getattr(bot, "delete_application_emoji", None)
    if emoji_id is not None and callable(bot_deleter):
        try:
            await bot_deleter(emoji_id)
            return True
        except Exception:
            return False

    return False


async def _delete_guild_emoji(
    emoji: discord.Emoji,
    *,
    reason: Optional[str] = None,
) -> bool:
    try:
        await emoji.delete(reason=reason)
        return True
    except Exception:
        return False


def _cached_application_mention(kind: str, meta: dict[str, Any]) -> Optional[str]:
    key = (kind, _emoji_name(meta))
    return _application_emoji_cache.get(key) or _application_kind_cache.get(kind)


def _cached_application_mention_by_name(emoji_name: str) -> Optional[str]:
    normalized = _normalize_emoji_name(emoji_name)
    if not normalized:
        return None
    return _application_name_cache.get(normalized)


def _extract_shortcode_name(value: str) -> Optional[str]:
    match = _SHORTCODE_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    normalized = _normalize_emoji_name(match.group(1))
    return normalized or None


def _extract_custom_emoji_name(value: str) -> Optional[str]:
    match = _CUSTOM_EMOJI_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    normalized = _normalize_emoji_name(match.group(1))
    return normalized or None


def _cache_application_mention(
    *,
    kind: Optional[str],
    emoji_name: str,
    mention: str,
) -> None:
    normalized_name = _normalize_emoji_name(emoji_name)
    normalized_mention = str(mention or "").strip()
    if not normalized_name or not normalized_mention:
        return

    _application_name_cache[normalized_name] = normalized_mention
    if kind is not None:
        _application_kind_cache[kind] = normalized_mention
        _application_emoji_cache[(kind, normalized_name)] = normalized_mention


def _get_client_from_guild(guild: discord.Guild) -> Optional[discord.Client]:
    explicit_client = getattr(guild, "client", None)
    if explicit_client is not None:
        return explicit_client

    state = getattr(guild, "_state", None)
    if state is None:
        return None

    explicit_client = getattr(state, "_client", None) or getattr(state, "client", None)
    if explicit_client is not None:
        return explicit_client

    getter = getattr(state, "_get_client", None)
    if callable(getter):
        try:
            client = getter()
            if client is not None:
                return client
        except Exception:
            return None
    return None


async def _prime_application_cache_from_client(client: Optional[discord.Client]) -> None:
    if client is None:
        return
    if not hasattr(client, "fetch_application_emojis"):
        return

    try:
        emojis = list(await client.fetch_application_emojis())
    except Exception:
        return

    for emoji in emojis:
        emoji_name = _normalize_emoji_name(str(getattr(emoji, "name", "") or ""))
        mention = str(emoji)
        if not emoji_name or not mention:
            continue

        _cache_application_mention(kind=None, emoji_name=emoji_name, mention=mention)
        for kind, meta in _EMOJI_META.items():
            if emoji_name == _emoji_name(meta):
                _cache_application_mention(kind=kind, emoji_name=emoji_name, mention=mention)


async def _resolve_shortcode_application_mention(
    guild: Optional[discord.Guild],
    *,
    shortcode_token: str,
    kind: Optional[str] = None,
) -> Optional[str]:
    emoji_name = _extract_shortcode_name(shortcode_token)
    if not emoji_name:
        return None

    if kind is not None:
        meta = _EMOJI_META.get(kind)
        if meta is not None:
            cached = _cached_application_mention(kind, meta)
            if cached:
                return cached

    cached_by_name = _cached_application_mention_by_name(emoji_name)
    if cached_by_name:
        return cached_by_name

    if guild is None:
        return None

    client = _get_client_from_guild(guild)
    await _prime_application_cache_from_client(client)
    return _cached_application_mention_by_name(emoji_name)


def get_app_emoji(kind: str) -> str:
    """
    Return the application emoji mention for the given kind.

    Looks up the cached mention set by sync_status_emojis_to_application().
    Falls back to the configured icon or unicode default if not cached.
    """
    meta = _EMOJI_META.get(kind)
    if meta is None:
        return ""
    mention = _cached_application_mention(kind, meta)
    if mention:
        return mention
    # Fall back to configured or default unicode icon
    configured = str(getattr(Config, meta["config_icon"], meta["default_icon"]) or "").strip()
    if not configured:
        return meta["default_icon"]

    # Do not leak raw shortcode tokens like :mod_warn: into embeds.
    shortcode_name = _extract_shortcode_name(configured)
    if shortcode_name:
        cached_by_name = _cached_application_mention_by_name(shortcode_name)
        if cached_by_name:
            return cached_by_name
        return meta["default_icon"]

    return configured


async def sync_status_emojis_to_application(bot: discord.Client) -> dict[str, str]:
    """
    Ensure all status emojis exist in the application's emoji inventory.

    This populates a process-local cache used by embed/reaction rendering so
    status icons come from the app emoji set (Developer Portal > Emojis).
    """
    synced: dict[str, str] = {}

    try:
        existing = list(await bot.fetch_application_emojis())
    except Exception:
        return synced

    auto_create = _bool(getattr(Config, "AUTO_CREATE_STATUS_EMOJIS", True), default=True)
    for emoji in existing:
        emoji_name = _normalize_emoji_name(str(getattr(emoji, "name", "") or ""))
        mention = str(emoji)
        if emoji_name and mention:
            _cache_application_mention(kind=None, emoji_name=emoji_name, mention=mention)

    async with _application_sync_lock:
        for kind, meta in _EMOJI_META.items():
            configured_icon = str(getattr(Config, meta["config_icon"], meta["default_icon"]) or "").strip()
            emoji_name = _emoji_name(meta)
            cache_key = (kind, emoji_name)

            # If explicitly configured as a custom emoji mention, honor it directly.
            if _looks_custom_emoji(configured_icon):
                _application_emoji_cache[cache_key] = configured_icon
                _application_kind_cache[kind] = configured_icon
                explicit_name = _extract_custom_emoji_name(configured_icon) or emoji_name
                _cache_application_mention(kind=kind, emoji_name=explicit_name, mention=configured_icon)
                synced[kind] = configured_icon
                continue

            cached = _application_emoji_cache.get(cache_key)
            if cached:
                _cache_application_mention(kind=kind, emoji_name=emoji_name, mention=cached)
                synced[kind] = cached
                continue

            payload = await _pick_asset_payload(meta) if auto_create else None
            prefer_animated = True
            require_animated = bool(payload and prefer_animated and payload[2])
            payload_hash = payload[3] if payload is not None else ""

            same_name = [emoji for emoji in existing if emoji.name == emoji_name]
            selected = _pick_existing_emoji(
                same_name,
                name=emoji_name,
                prefer_animated=prefer_animated,
            )

            up_to_date: Optional[Any] = None
            if payload is not None:
                for emoji in same_name:
                    if await _emoji_matches_payload(
                        emoji,
                        payload_hash=payload_hash,
                        require_animated=require_animated,
                    ):
                        up_to_date = emoji
                        break
            elif selected is not None:
                up_to_date = selected

            stale: list[Any] = []
            if auto_create and payload is not None:
                keep_id = getattr(up_to_date, "id", None)
                for emoji in existing:
                    if emoji.name == emoji_name or _is_legacy_version_name(emoji.name, emoji_name):
                        if keep_id is not None and getattr(emoji, "id", None) == keep_id:
                            continue
                        stale.append(emoji)

                if stale:
                    deleted_ids: set[int] = set()
                    for emoji in stale:
                        if await _delete_application_emoji(bot, emoji):
                            emoji_id = getattr(emoji, "id", None)
                            if isinstance(emoji_id, int):
                                deleted_ids.add(emoji_id)
                    if deleted_ids:
                        existing = [
                            emoji
                            for emoji in existing
                            if getattr(emoji, "id", None) not in deleted_ids
                        ]

                selected = up_to_date
                if selected is None:
                    try:
                        selected = await bot.create_application_emoji(
                            name=emoji_name,
                            image=payload[1],
                        )
                    except discord.Forbidden:
                        selected = None
                    except discord.HTTPException:
                        selected = None

                    if selected is not None:
                        existing.append(selected)

            if selected is not None:
                mention = str(selected)
                _cache_application_mention(kind=kind, emoji_name=emoji_name, mention=mention)
                synced[kind] = mention

    return synced


def _member_can_manage_emojis(member: Optional[discord.Member]) -> bool:
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(
        getattr(perms, "manage_emojis_and_stickers", False)
        or getattr(perms, "manage_emojis", False)
        or getattr(perms, "manage_expressions", False)
    )


async def _ensure_custom_status_emoji(guild: discord.Guild, kind: str) -> Optional[str]:
    meta = _EMOJI_META.get(kind)
    if meta is None:
        return None

    emoji_name = _emoji_name(meta)
    key = (guild.id, kind, emoji_name)

    cached = _emoji_cache.get(key)
    if cached:
        return cached

    auto_create = _bool(getattr(Config, "AUTO_CREATE_STATUS_EMOJIS", True), default=True)
    prefer_animated = True
    payload = await _pick_asset_payload(meta) if auto_create else None

    bot_member = guild.me
    if bot_member is None:
        bot_user = getattr(guild, "_state", None)
        bot_id = getattr(getattr(bot_user, "user", None), "id", None)
        if bot_id is not None:
            bot_member = guild.get_member(bot_id)

    lock = await _get_lock(key)
    async with lock:
        cached = _emoji_cache.get(key)
        if cached:
            return cached

        guild_emojis = list(guild.emojis)
        same_name = [emoji for emoji in guild_emojis if emoji.name == emoji_name]
        selected = _pick_existing_emoji(
            same_name,
            name=emoji_name,
            prefer_animated=prefer_animated,
        )

        up_to_date: Optional[discord.Emoji] = None
        if payload is not None:
            require_animated = bool(prefer_animated and payload[2])
            for emoji in same_name:
                if await _emoji_matches_payload(
                    emoji,
                    payload_hash=payload[3],
                    require_animated=require_animated,
                ):
                    up_to_date = emoji
                    break
        elif selected is not None:
            up_to_date = selected

        if not auto_create:
            if selected is None:
                return None
            mention = str(selected)
            _emoji_cache[key] = mention
            return mention

        reason = str(
            getattr(
                Config,
                "STATUS_EMOJI_CREATE_REASON",
                "Auto-create status emojis for moderation responses.",
            )
        )

        can_manage = _member_can_manage_emojis(bot_member)
        if payload is not None and can_manage:
            keep_id = getattr(up_to_date, "id", None)
            stale = [
                emoji
                for emoji in guild_emojis
                if (emoji.name == emoji_name or _is_legacy_version_name(emoji.name, emoji_name))
                and (keep_id is None or emoji.id != keep_id)
            ]
            for emoji in stale:
                await _delete_guild_emoji(emoji, reason=reason)

            selected = up_to_date
            if selected is None:
                try:
                    selected = await guild.create_custom_emoji(
                        name=emoji_name,
                        image=payload[1],
                        reason=reason,
                    )
                except discord.Forbidden:
                    selected = None
                except discord.HTTPException:
                    selected = None
        elif up_to_date is not None:
            selected = up_to_date

        if selected is None:
            return None

        mention = str(selected)
        _emoji_cache[key] = mention
        return mention


async def apply_status_emoji_overrides(
    embed: discord.Embed,
    guild: Optional[discord.Guild],
) -> discord.Embed:
    """
    Replace unicode status icons with app emojis when available.
    Falls back to guild custom emojis if app-level sync is unavailable.
    """
    if guild is None:
        return embed

    description = getattr(embed, "description", None)
    title = getattr(embed, "title", None)
    if not description and not title:
        return embed

    updated_description = str(description) if description else None
    updated_title = str(title) if title else None
    changed = False

    for kind, meta in _EMOJI_META.items():
        configured_icon = str(getattr(Config, meta["config_icon"], meta["default_icon"]) or "").strip()
        if not configured_icon:
            continue
        if _looks_custom_emoji(configured_icon):
            continue

        prefix = f"{configured_icon} "
        mention = _cached_application_mention(kind, meta)
        if mention is None:
            mention = await _resolve_shortcode_application_mention(
                guild,
                shortcode_token=configured_icon,
                kind=kind,
            )

        if updated_description and updated_description.startswith(prefix):
            if mention is None:
                try:
                    mention = await _ensure_custom_status_emoji(guild, kind)
                except Exception:
                    mention = None
            replacement = mention or str(meta.get("default_icon") or "").strip()
            if replacement:
                updated_description = updated_description.replace(prefix, f"{replacement} ", 1)
                changed = True

        if updated_title and updated_title.startswith(prefix):
            if mention is None:
                try:
                    mention = await _ensure_custom_status_emoji(guild, kind)
                except Exception:
                    mention = None
            replacement = mention or str(meta.get("default_icon") or "").strip()
            if replacement:
                updated_title = updated_title.replace(prefix, f"{replacement} ", 1)
                changed = True

    if changed:
        if updated_description is not None:
            embed.description = updated_description
        if updated_title is not None:
            embed.title = updated_title

    return embed


async def get_loading_emoji_for_guild(
    guild: Optional[discord.Guild],
    *,
    configured_emoji: Optional[str] = None,
) -> str:
    """
    Resolve loading emoji with app-emoji preference and guild fallback.

    Returns a unicode/custom emoji token suitable for message content or reactions.
    """
    configured = str(
        configured_emoji
        if configured_emoji is not None
        else getattr(Config, "EMOJI_LOADING", "\u23f3")
    ).strip()
    if not configured:
        configured = "\u23f3"

    if guild is None:
        return configured
    if _looks_custom_emoji(configured):
        return configured

    mention_from_shortcode = await _resolve_shortcode_application_mention(
        guild,
        shortcode_token=configured,
        kind="loading",
    )
    if mention_from_shortcode:
        return mention_from_shortcode

    loading_meta = _EMOJI_META.get("loading")
    if loading_meta is not None:
        cached_loading = _cached_application_mention("loading", loading_meta)
        if cached_loading:
            return cached_loading

    mention = await _ensure_custom_status_emoji(guild, "loading")
    if mention:
        return mention
    return configured


async def get_status_emoji_for_guild(
    guild: Optional[discord.Guild],
    *,
    kind: str,
    configured_emoji: Optional[str] = None,
) -> str:
    """
    Resolve a status emoji token for a specific kind (success/error/warning/info/etc).

    Returns a unicode/custom emoji token suitable for button emoji/content usage.
    """
    meta = _EMOJI_META.get(kind)
    if meta is None:
        raw = str(configured_emoji or "").strip()
        return raw or "?"

    configured = str(
        configured_emoji
        if configured_emoji is not None
        else getattr(Config, meta["config_icon"], meta["default_icon"])
    ).strip()
    if not configured:
        configured = str(meta["default_icon"])

    if guild is None:
        return configured
    if _looks_custom_emoji(configured):
        return configured

    mention_from_shortcode = await _resolve_shortcode_application_mention(
        guild,
        shortcode_token=configured,
        kind=kind,
    )
    if mention_from_shortcode:
        return mention_from_shortcode

    shortcode_name = _extract_shortcode_name(configured)
    if shortcode_name:
        named = discord.utils.get(guild.emojis, name=shortcode_name)
        if named is not None:
            return str(named)

    cached = _cached_application_mention(kind, meta)
    if cached:
        return cached

    mention = await _ensure_custom_status_emoji(guild, kind)
    if mention:
        return mention
    if shortcode_name:
        return str(meta["default_icon"])
    return configured


def status_embed_pad_line(pad_chars: int) -> str:
    if pad_chars <= 0:
        return ""
    return _BRAILLE_BLANK * pad_chars
