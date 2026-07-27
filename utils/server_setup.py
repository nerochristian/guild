from __future__ import annotations

from typing import Any, Dict, List, Optional

import discord

DEFAULT_RULES: List[str] = [
    "Be respectful to all members",
    "No spam or self-promotion",
    "No NSFW content",
    "No harassment or bullying",
    "Follow Discord's Terms of Service",
    "Listen to staff members",
    "Use channels for their intended purpose",
    "No excessive caps or emojis",
    "English only in main channels",
    "Have fun!",
]

DEFAULT_STAFF_GUIDE: Dict[str, Any] = {
    "welcome": "Welcome to the staff team. This guide covers the basics for your moderation team.",
    "sections": [
        {
            "title": "General Guidelines",
            "content": [
                "Stay professional and respectful.",
                "Document major moderation actions.",
                "Escalate edge cases when unsure.",
                "Do not use staff tools for personal disputes.",
            ],
        },
        {
            "title": "Warnings",
            "content": [
                "Use warnings consistently.",
                "Explain the reason clearly.",
                "Escalate repeated behavior.",
            ],
        },
        {
            "title": "Tickets and Modmail",
            "content": [
                "Respond clearly and promptly.",
                "Keep conversations on-topic.",
                "Leave transcripts for important cases.",
            ],
        },
    ],
}

ROLE_SPECS: List[Dict[str, Any]] = [
    {
        "name": "Owner",
        "setting_key": "owner_role",
        "color": discord.Color.dark_red(),
        "permissions": discord.Permissions(administrator=True),
        "hoist": True,
    },
    {
        "name": "Manager",
        "setting_key": "manager_role",
        "color": discord.Color.from_rgb(114, 0, 0),
        "permissions": discord.Permissions(administrator=True),
        "hoist": True,
    },
    {
        "name": "Admin",
        "setting_key": "admin_role",
        "color": discord.Color.red(),
        "permissions": discord.Permissions(administrator=True),
        "hoist": True,
    },
    {
        "name": "Supervisor",
        "setting_key": "supervisor_role",
        "color": discord.Color.from_rgb(204, 0, 0),
        "permissions": discord.Permissions(
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            manage_channels=True,
            manage_nicknames=True,
            moderate_members=True,
            view_audit_log=True,
            manage_threads=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        ),
        "hoist": True,
    },
    {
        "name": "Senior Moderator",
        "setting_key": "senior_mod_role",
        "color": discord.Color.from_rgb(255, 0, 0),
        "permissions": discord.Permissions(
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            manage_channels=True,
            manage_nicknames=True,
            moderate_members=True,
            view_audit_log=True,
            manage_threads=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        ),
        "hoist": True,
    },
    {
        "name": "Moderator",
        "setting_key": "mod_role",
        "color": discord.Color.from_rgb(255, 77, 77),
        "permissions": discord.Permissions(
            kick_members=True,
            manage_messages=True,
            manage_nicknames=True,
            moderate_members=True,
            view_audit_log=True,
            manage_threads=True,
            move_members=True,
            mute_members=True,
        ),
        "hoist": True,
    },
    {
        "name": "Trial Moderator",
        "setting_key": "trial_mod_role",
        "color": discord.Color.from_rgb(255, 128, 128),
        "permissions": discord.Permissions(
            manage_messages=True,
            manage_nicknames=True,
            moderate_members=True,
            mute_members=True,
        ),
        "hoist": True,
    },
    {
        "name": "Staff",
        "setting_key": "staff_role",
        "color": discord.Color.from_rgb(255, 179, 179),
        "permissions": discord.Permissions(
            view_audit_log=True,
            manage_messages=True,
            mute_members=True,
        ),
        "hoist": True,
    },
    {
        "name": "Muted",
        "setting_key": "muted_role",
        "color": discord.Color.dark_gray(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Quarantined",
        "setting_key": "automod_quarantine_role_id",
        "color": discord.Color.darker_grey(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Unverified",
        "setting_key": "unverified_role",
        "color": discord.Color.darker_grey(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Verified",
        "setting_key": "verified_role",
        "color": discord.Color.green(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Logs Access",
        "setting_key": "logs_access_role",
        "color": discord.Color.light_grey(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Bypass",
        "setting_key": "automod_bypass_role_id",
        "color": discord.Color.teal(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Whitelisted",
        "setting_key": "whitelisted_role",
        "color": discord.Color.gold(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
    {
        "name": "Support",
        "setting_key": "ticket_support_role",
        "color": discord.Color.blurple(),
        "permissions": discord.Permissions.none(),
        "hoist": False,
    },
]

CORE_SETUP_ROLE_KEYS = {
    "muted_role",
    "automod_quarantine_role_id",
    "unverified_role",
    "verified_role",
    "logs_access_role",
    "automod_bypass_role_id",
    "whitelisted_role",
}

QUICKSTART_ROLE_SPECS: List[Dict[str, Any]] = [
    spec for spec in ROLE_SPECS
    if spec["setting_key"] in CORE_SETUP_ROLE_KEYS
]

CATEGORY_SPECS: List[Dict[str, Any]] = [
    {"name": "Moderation Logs", "setting_key": "logs_category", "prefer_name": True},
    {"name": "Support Tickets", "setting_key": "ticket_category"},
]

CHANNEL_SPECS: List[Dict[str, Any]] = [
    {"name": "welcome", "setting_key": "welcome_channel", "topic": "Welcome and onboarding messages."},
    {"name": "audit-logs", "setting_key": "audit_log_channel", "category": "logs_category", "topic": "Server audit events and configuration changes.", "prefer_name": True},
    {"name": "mod-logs", "setting_key": "mod_log_channel", "category": "logs_category", "topic": "Moderation actions.", "prefer_name": True},
    {"name": "message-logs", "setting_key": "message_log_channel", "category": "logs_category", "topic": "Message edits, deletes, and purge logs.", "prefer_name": True},
    {"name": "automod-logs", "setting_key": "automod_log_channel", "category": "logs_category", "topic": "Automod and AI moderation events.", "prefer_name": True},
    {"name": "voice-logs", "setting_key": "voice_log_channel", "category": "logs_category", "topic": "Voice channel moderation and state changes.", "prefer_name": True},
    {"name": "jail", "setting_key": "quarantine_channel", "topic": "Channel for quarantined users."},
]

FEATURE_DEFAULTS: Dict[str, bool] = {
    "logging_enabled": True,
    "automod_enabled": True,
    "antiraid_enabled": False,
    "verification_enabled": False,
    "tickets_enabled": False,
    "modmail_enabled": False,
    "aimod_enabled": False,
    "whitelist_enabled": False,
}


def _coerce_int(value: Any) -> Optional[int]:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _modules_blob(settings: Dict[str, Any]) -> Dict[str, Any]:
    modules = settings.get("modules", {})
    return modules if isinstance(modules, dict) else {}


def module_enabled(settings: Dict[str, Any], module_id: str, default: bool) -> bool:
    flat_key = f"{module_id}_enabled"
    if flat_key in settings:
        flat_value = settings.get(flat_key)
        if isinstance(flat_value, bool):
            return flat_value
        if flat_value is not None:
            return bool(flat_value)

    modules = _modules_blob(settings)
    module = modules.get(module_id, {})
    if isinstance(module, dict) and isinstance(module.get("enabled"), bool):
        return bool(module.get("enabled"))
    return bool(settings.get(flat_key, default))


def module_setting(
    settings: Dict[str, Any],
    module_id: str,
    key: str,
    *,
    fallback_key: Optional[str] = None,
) -> Optional[int]:
    modules = _modules_blob(settings)
    module = modules.get(module_id, {})
    if isinstance(module, dict):
        module_settings = module.get("settings", {})
        if isinstance(module_settings, dict):
            module_value = _coerce_int(module_settings.get(key))
            if module_value:
                return module_value
    if fallback_key:
        return _coerce_int(settings.get(fallback_key))
    return None


def sync_setup_aliases(settings: Dict[str, Any]) -> None:
    if settings.get("muted_role"):
        settings["mute_role"] = settings["muted_role"]
    elif settings.get("mute_role"):
        settings["muted_role"] = settings["mute_role"]

    if settings.get("automod_quarantine_role_id"):
        settings["antiraid_quarantine_role"] = settings["automod_quarantine_role_id"]
    elif settings.get("antiraid_quarantine_role"):
        settings["automod_quarantine_role_id"] = settings["antiraid_quarantine_role"]

    if settings.get("verified_role"):
        settings["verification_role"] = settings["verified_role"]
    elif settings.get("verification_role"):
        settings["verified_role"] = settings["verification_role"]

    if settings.get("verify_channel"):
        settings["verification_channel"] = settings["verify_channel"]
    elif settings.get("verification_channel"):
        settings["verify_channel"] = settings["verification_channel"]

    if settings.get("verify_log_channel"):
        settings["verification_log_channel"] = settings["verify_log_channel"]
    elif settings.get("verification_log_channel"):
        settings["verify_log_channel"] = settings["verification_log_channel"]

    log_aliases = {
        "mod_log_channel": "log_channel_mod",
        "audit_log_channel": "log_channel_audit",
        "message_log_channel": "log_channel_message",
        "voice_log_channel": "log_channel_voice",
        "automod_log_channel": "log_channel_automod",
        "report_log_channel": "log_channel_report",
        "ticket_log_channel": "log_channel_ticket",
    }
    for primary, alias in log_aliases.items():
        if settings.get(primary):
            settings[alias] = settings[primary]
        elif settings.get(alias):
            settings[primary] = settings[alias]

    if settings.get("forum_alerts_channel"):
        settings["forum_alert_channel"] = settings["forum_alerts_channel"]
    elif settings.get("forum_alert_channel"):
        settings["forum_alerts_channel"] = settings["forum_alert_channel"]


def apply_compact_log_routing(settings: Dict[str, Any]) -> None:
    """
    Route logging into the standard Moderation Logs channel layout.

    - audit/server events go to audit-logs
    - mod/report/ticket-style logs go to mod-logs
    - message logs go to message-logs
    - automod logs go to automod-logs
    - voice logs go to voice-logs
    """
    sync_setup_aliases(settings)

    mod_log_id = _coerce_int(settings.get("mod_log_channel")) or _coerce_int(settings.get("log_channel_mod"))
    audit_log_id = _coerce_int(settings.get("audit_log_channel")) or _coerce_int(settings.get("log_channel_audit"))
    message_log_id = _coerce_int(settings.get("message_log_channel")) or _coerce_int(settings.get("log_channel_message"))
    automod_log_id = _coerce_int(settings.get("automod_log_channel")) or _coerce_int(settings.get("log_channel_automod"))
    voice_log_id = _coerce_int(settings.get("voice_log_channel")) or _coerce_int(settings.get("log_channel_voice"))

    if mod_log_id:
        for key in (
            "mod_log_channel",
            "log_channel_mod",
            "report_log_channel",
            "log_channel_report",
            "ticket_log_channel",
            "log_channel_ticket",
            "modmail_log_channel",
            "court_log_channel",
            "emoji_log_channel",
            "forum_alerts_channel",
            "forum_alert_channel",
            "ai_confirmation_channel",
        ):
            settings[key] = mod_log_id

    if audit_log_id:
        settings["audit_log_channel"] = audit_log_id
        settings["log_channel_audit"] = audit_log_id

    if message_log_id:
        settings["message_log_channel"] = message_log_id
        settings["log_channel_message"] = message_log_id

    if automod_log_id:
        settings["automod_log_channel"] = automod_log_id
        settings["log_channel_automod"] = automod_log_id

    if voice_log_id:
        settings["voice_log_channel"] = voice_log_id
        settings["log_channel_voice"] = voice_log_id

    modules = settings.get("modules")
    if not isinstance(modules, dict):
        modules = {}
        settings["modules"] = modules
    logging_module = modules.get("logging")
    if not isinstance(logging_module, dict):
        logging_module = {}
        modules["logging"] = logging_module
    logging_settings = logging_module.get("settings")
    if not isinstance(logging_settings, dict):
        logging_settings = {}
        logging_module["settings"] = logging_settings

    if mod_log_id:
        logging_settings["modChannel"] = mod_log_id
    if audit_log_id:
        logging_settings["auditChannel"] = audit_log_id
    if message_log_id:
        logging_settings["messageChannel"] = message_log_id
    if automod_log_id:
        logging_settings["automodChannel"] = automod_log_id
    if voice_log_id:
        logging_settings["voiceChannel"] = voice_log_id

    sync_setup_aliases(settings)


def sync_staff_role_groups(settings: Dict[str, Any]) -> None:
    admin_role = _coerce_int(settings.get("admin_role"))
    supervisor_role = _coerce_int(settings.get("supervisor_role"))
    mod_role_ids = [
        _coerce_int(settings.get(key))
        for key in (
            "admin_role",
            "supervisor_role",
            "senior_mod_role",
            "mod_role",
            "trial_mod_role",
            "staff_role",
        )
    ]
    settings["mod_roles"] = [role_id for role_id in mod_role_ids if role_id]
    settings["admin_roles"] = [admin_role] if admin_role else []
    settings["supervisor_roles"] = [supervisor_role] if supervisor_role else []


def hydrate_setup_settings_from_guild(
    guild: discord.Guild,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Backfill missing setup IDs from guild resources that already exist.

    This keeps setup/status output aligned with roles/channels the bot created earlier,
    even when the exact flat setting key was never written or drifted to an alias.
    """
    hydrated = dict(settings)
    sync_setup_aliases(hydrated)

    for spec in ROLE_SPECS:
        key = spec["setting_key"]
        if _coerce_int(hydrated.get(key)):
            continue
        role = _find_role(guild, None, spec["name"])
        if role is not None:
            hydrated[key] = role.id

    for spec in CATEGORY_SPECS:
        key = spec["setting_key"]
        if _coerce_int(hydrated.get(key)) and not spec.get("prefer_name"):
            continue
        category = _find_category(
            guild,
            hydrated.get(key),
            spec["name"],
            prefer_name=bool(spec.get("prefer_name")),
        )
        if category is not None:
            hydrated[key] = category.id
        elif spec.get("prefer_name"):
            hydrated.pop(key, None)

    for spec in CHANNEL_SPECS:
        key = spec["setting_key"]
        if _coerce_int(hydrated.get(key)) and not spec.get("prefer_name"):
            continue
        channel = _find_text_channel(
            guild,
            hydrated.get(key),
            spec["name"],
            prefer_name=bool(spec.get("prefer_name")),
        )
        if channel is not None:
            hydrated[key] = channel.id
        elif spec.get("prefer_name"):
            hydrated.pop(key, None)
            alias = {
                "mod_log_channel": "log_channel_mod",
                "audit_log_channel": "log_channel_audit",
                "message_log_channel": "log_channel_message",
                "automod_log_channel": "log_channel_automod",
                "voice_log_channel": "log_channel_voice",
            }.get(key)
            if alias:
                hydrated.pop(alias, None)

    apply_compact_log_routing(hydrated)
    sync_setup_aliases(hydrated)
    sync_staff_role_groups(hydrated)
    return hydrated


def build_setup_summary(
    guild: discord.Guild,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    settings = hydrate_setup_settings_from_guild(guild, settings)

    def role_item(label: str, key: str) -> Dict[str, Any]:
        role = guild.get_role(_coerce_int(settings.get(key)) or 0)
        return {
            "key": key,
            "label": label,
            "configured": role is not None,
            "value": f"@{role.name}" if role else None,
        }

    def channel_item(label: str, key: str) -> Dict[str, Any]:
        channel = guild.get_channel(_coerce_int(settings.get(key)) or 0)
        return {
            "key": key,
            "label": label,
            "configured": channel is not None,
            "value": f"#{getattr(channel, 'name', 'unknown')}" if channel else None,
        }

    def category_item(label: str, key: str) -> Dict[str, Any]:
        category = guild.get_channel(_coerce_int(settings.get(key)) or 0)
        configured = isinstance(category, discord.CategoryChannel)
        return {
            "key": key,
            "label": label,
            "configured": configured,
            "value": category.name if configured else None,
        }

    def module_channel_item(label: str, module_id: str, field_key: str, fallback_key: str) -> Dict[str, Any]:
        channel = guild.get_channel(module_setting(settings, module_id, field_key, fallback_key=fallback_key) or 0)
        return {
            "key": f"{module_id}.{field_key}",
            "label": label,
            "configured": channel is not None,
            "value": f"#{getattr(channel, 'name', 'unknown')}" if channel else None,
        }

    sections = [
        {
            "id": "staff_roles",
            "label": "Staff Roles",
            "items": [
                role_item("Owner Role", "owner_role"),
                role_item("Manager Role", "manager_role"),
                role_item("Admin Role", "admin_role"),
                role_item("Supervisor Role", "supervisor_role"),
                role_item("Senior Moderator Role", "senior_mod_role"),
                role_item("Moderator Role", "mod_role"),
                role_item("Trial Moderator Role", "trial_mod_role"),
                role_item("Staff Role", "staff_role"),
            ],
        },
        {
            "id": "system_roles",
            "label": "System Roles",
            "items": [
                role_item("Muted Role", "muted_role"),
                role_item("Quarantine Role", "automod_quarantine_role_id"),
                role_item("Logs Access Role", "logs_access_role"),
                role_item("Bypass Role", "automod_bypass_role_id"),
                role_item("Whitelisted Role", "whitelisted_role"),
                role_item("Auto Join Role", "auto_role"),
                role_item("Verified Role", "verified_role"),
                role_item("Unverified Role", "unverified_role"),
            ],
        },
        {
            "id": "core_channels",
            "label": "Core Channels",
            "items": [
                channel_item("Welcome Channel", "welcome_channel"),
            ],
        },
        {
            "id": "routing",
            "label": "Routing",
            "items": [
                module_channel_item("Audit Log Channel", "logging", "auditChannel", "audit_log_channel"),
                module_channel_item("Mod Log Channel", "logging", "modChannel", "mod_log_channel"),
                module_channel_item("Message Log Channel", "logging", "messageChannel", "message_log_channel"),
                module_channel_item("AutoMod Log Channel", "logging", "automodChannel", "automod_log_channel"),
                module_channel_item("Voice Log Channel", "logging", "voiceChannel", "voice_log_channel"),
                category_item("Ticket Category", "ticket_category"),
                module_channel_item("Ticket/Modmail Log Channel", "tickets", "logChannel", "ticket_log_channel"),
            ],
        },
        {
            "id": "features",
            "label": "Features",
            "items": [
                {"key": "logging_enabled", "label": "Logging", "configured": True, "value": "Enabled" if module_enabled(settings, "logging", True) else "Disabled"},
                {"key": "automod_enabled", "label": "AutoMod", "configured": True, "value": "Enabled" if module_enabled(settings, "automod", True) else "Disabled"},
                {"key": "antiraid_enabled", "label": "Anti-Raid", "configured": True, "value": "Enabled" if module_enabled(settings, "antiraid", False) else "Disabled"},
                {"key": "verification_enabled", "label": "Verification", "configured": True, "value": "Enabled" if module_enabled(settings, "verification", False) else "Disabled"},
                {"key": "tickets_enabled", "label": "Tickets", "configured": True, "value": "Enabled" if module_enabled(settings, "tickets", False) else "Disabled"},
                {"key": "modmail_enabled", "label": "Modmail", "configured": True, "value": "Enabled" if module_enabled(settings, "modmail", False) else "Disabled"},
                {"key": "aimod_enabled", "label": "AI Moderation", "configured": True, "value": "Enabled" if module_enabled(settings, "aimod", False) else "Disabled"},
            ],
        },
    ]

    total = 0
    complete = 0
    for section in sections:
        section_total = len(section["items"])
        section_complete = sum(1 for item in section["items"] if item["configured"])
        section["total"] = section_total
        section["complete"] = section_complete
        total += section_total
        complete += section_complete

    percent = int(round((complete / total) * 100)) if total else 100
    return {
        "guildId": str(guild.id),
        "setupComplete": bool(settings.get("setup_complete")),
        "complete": complete,
        "total": total,
        "percent": percent,
        "sections": sections,
    }


def _find_role(guild: discord.Guild, setting_value: Any, name: str) -> Optional[discord.Role]:
    role_id = _coerce_int(setting_value)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
    lowered = name.casefold()
    for role in guild.roles:
        if role.name.casefold() == lowered:
            return role
    return None


def _find_category(
    guild: discord.Guild,
    setting_value: Any,
    name: str,
    *,
    prefer_name: bool = False,
) -> Optional[discord.CategoryChannel]:
    lowered = name.casefold()
    matching_category = next((category for category in guild.categories if category.name.casefold() == lowered), None)
    if prefer_name and matching_category is not None:
        return matching_category

    category_id = _coerce_int(setting_value)
    if category_id:
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category

    for category in guild.categories:
        if category.name.casefold() == lowered:
            return category
    return None


def _find_text_channel(
    guild: discord.Guild,
    setting_value: Any,
    name: str,
    *,
    prefer_name: bool = False,
) -> Optional[discord.TextChannel]:
    lowered = name.casefold()
    matching_channel = next((channel for channel in guild.text_channels if channel.name.casefold() == lowered), None)
    if prefer_name and matching_channel is not None:
        return matching_channel

    channel_id = _coerce_int(setting_value)
    if channel_id:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            if prefer_name and channel.name.casefold() != lowered:
                return None
            return channel

    for channel in guild.text_channels:
        if channel.name.casefold() == lowered:
            return channel
    return None


def _verification_exempt_channel_ids(settings: Dict[str, Any]) -> set[int]:
    exempt_ids: set[int] = set()
    for key in ("welcome_channel", "verify_channel", "verification_channel"):
        channel_id = _coerce_int(settings.get(key))
        if channel_id:
            exempt_ids.add(channel_id)
    return exempt_ids


async def apply_verification_gate(
    guild: discord.Guild,
    settings: Dict[str, Any],
    *,
    previous_unverified_role_id: Optional[int] = None,
) -> Dict[str, Any]:
    sync_setup_aliases(settings)

    enabled = module_enabled(settings, "verification", False)
    unverified_role = guild.get_role(_coerce_int(settings.get("unverified_role")) or 0)
    if unverified_role is None:
        return {
            "enabled": enabled,
            "updated": 0,
            "errors": ["Unverified role is not configured."] if enabled else [],
        }

    exempt_channel_ids = _verification_exempt_channel_ids(settings)
    updated = 0
    errors: List[str] = []
    stale_unverified_role = None
    stale_role_id = _coerce_int(previous_unverified_role_id)
    if stale_role_id and stale_role_id != unverified_role.id:
        stale_unverified_role = guild.get_role(stale_role_id)

    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue

        role_targets = [(unverified_role, True if enabled and channel.id in exempt_channel_ids else (False if enabled else None))]
        if stale_unverified_role is not None:
            role_targets.append((stale_unverified_role, None))

        for role, desired_view in role_targets:
            overwrite = channel.overwrites_for(role)
            if overwrite.view_channel == desired_view:
                continue

            overwrite.view_channel = desired_view
            try:
                await channel.set_permissions(
                    role,
                    overwrite=overwrite,
                    reason="ModBot verification access sync",
                )
                updated += 1
            except Exception as exc:
                errors.append(f"{channel.name} ({role.name}): {exc}")

    return {
        "enabled": enabled,
        "updated": updated,
        "errors": errors,
    }


async def _sync_log_channel_order(
    category: Optional[discord.CategoryChannel],
    settings: Dict[str, Any],
    errors: List[str],
) -> None:
    if category is None:
        return

    ordered_keys = (
        "audit_log_channel",
        "mod_log_channel",
        "message_log_channel",
        "automod_log_channel",
        "voice_log_channel",
    )
    channels: list[discord.TextChannel] = []
    for key in ordered_keys:
        channel = category.guild.get_channel(_coerce_int(settings.get(key)) or 0)
        if isinstance(channel, discord.TextChannel) and channel.category_id == category.id:
            channels.append(channel)

    if len(channels) < 2:
        return

    start_position = min(channel.position for channel in channels)
    for offset, channel in enumerate(channels):
        desired_position = start_position + offset
        if channel.position == desired_position:
            continue
        try:
            await channel.edit(
                position=desired_position,
                reason="ModBot setup log channel order sync",
            )
        except Exception as exc:
            errors.append(f"Channel {channel.name} order sync: {exc}")


async def quickstart_server(guild: discord.Guild, settings: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(settings)
    created_roles: List[str] = []
    created_channels: List[str] = []
    reused: List[str] = []
    errors: List[str] = []

    for spec in QUICKSTART_ROLE_SPECS:
        role = _find_role(guild, updated.get(spec["setting_key"]), spec["name"])
        if role is None:
            try:
                role = await guild.create_role(
                    name=spec["name"],
                    color=spec["color"],
                    permissions=spec["permissions"],
                    hoist=spec["hoist"],
                    reason="ModBot setup quickstart",
                )
                created_roles.append(spec["name"])
            except Exception as exc:
                errors.append(f"Role {spec['name']}: {exc}")
                continue
        else:
            reused.append(f"role:{spec['name']}")

        updated[spec["setting_key"]] = role.id

    category_lookup: Dict[str, discord.CategoryChannel] = {}
    for spec in CATEGORY_SPECS:
        category = _find_category(
            guild,
            updated.get(spec["setting_key"]),
            spec["name"],
            prefer_name=bool(spec.get("prefer_name")),
        )
        if category is None:
            try:
                category = await guild.create_category(
                    name=spec["name"],
                    reason="ModBot setup quickstart",
                )
                created_channels.append(spec["name"])
            except Exception as exc:
                errors.append(f"Category {spec['name']}: {exc}")
                continue
        else:
            reused.append(f"category:{spec['name']}")

        if spec.get("prefer_name") and category.name != spec["name"]:
            try:
                await category.edit(name=spec["name"], reason="ModBot setup log category sync")
            except Exception as exc:
                errors.append(f"Category {spec['name']} sync: {exc}")

        updated[spec["setting_key"]] = category.id
        category_lookup[spec["setting_key"]] = category

    for spec in CHANNEL_SPECS:
        channel = _find_text_channel(
            guild,
            updated.get(spec["setting_key"]),
            spec["name"],
            prefer_name=bool(spec.get("prefer_name")),
        )
        category = None
        category_key = spec.get("category")
        if isinstance(category_key, str):
            category = category_lookup.get(category_key)
            if category is None:
                found_category = guild.get_channel(_coerce_int(updated.get(category_key)) or 0)
                if isinstance(found_category, discord.CategoryChannel):
                    category = found_category
        if channel is None:
            try:
                channel = await guild.create_text_channel(
                    name=spec["name"],
                    category=category,
                    topic=spec.get("topic"),
                    reason="ModBot setup quickstart",
                )
                created_channels.append(spec["name"])
            except Exception as exc:
                errors.append(f"Channel {spec['name']}: {exc}")
                continue
        else:
            reused.append(f"channel:{spec['name']}")

        if category is not None and channel.category_id != category.id and spec.get("category") == "logs_category":
            try:
                await channel.edit(
                    category=category,
                    sync_permissions=True,
                    reason="ModBot setup log channel sync",
                )
            except Exception as exc:
                errors.append(f"Channel {spec['name']} sync: {exc}")

        updated[spec["setting_key"]] = channel.id

    await _sync_log_channel_order(category_lookup.get("logs_category"), updated, errors)

    for key, default in FEATURE_DEFAULTS.items():
        updated.setdefault(key, default)

    updated.setdefault("server_rules", DEFAULT_RULES)
    updated.setdefault("staff_guide", DEFAULT_STAFF_GUIDE)
    updated["setup_complete"] = True

    apply_compact_log_routing(updated)
    sync_setup_aliases(updated)
    sync_staff_role_groups(updated)
    verification_sync = await apply_verification_gate(guild, updated)
    errors.extend(verification_sync.get("errors", []))

    return {
        "settings": updated,
        "createdRoles": created_roles,
        "createdChannels": created_channels,
        "reused": reused,
        "errors": errors,
        "permissionUpdates": verification_sync.get("updated", 0),
    }
