"""
Advanced Logging System
Comprehensive event logging for messages, members, voice, moderation actions, and more
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal, Any
import logging
import io
import re
import tempfile
import asyncio
import time
from pathlib import Path
from discord.ext import tasks

from utils.embeds import ModEmbed, Colors, compact_kv_lines
from utils.checks import is_admin
from utils.cache import ChannelCache
from utils.transcript import generate_html_transcript, EphemeralTranscriptView
from utils.logging import prepare_log_embed
from utils.components_v2 import ensure_layout_view_action_rows, layout_view_from_embeds

CACHE_DIR = Path(tempfile.gettempdir()) / "modbot_images"
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

logger = logging.getLogger(__name__)


class DeletedMessageImageView(discord.ui.View):
    CUSTOM_ID = "deleted_message_images:view"

    def __init__(self, bot: commands.Bot, fallback_images: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.fallback_images = (fallback_images or [])[:10]

        label = "View image" if len(self.fallback_images) <= 1 else "View images"
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID,
        )

        async def _callback(interaction: discord.Interaction) -> None:
            await self._send_images(interaction)

        button.callback = _callback
        self.add_item(button)

    @staticmethod
    def _extract_deleted_message_id(interaction: discord.Interaction) -> Optional[int]:
        message = interaction.message
        if not message:
            return None

        chunks: list[str] = []
        for embed in message.embeds:
            if embed.description:
                chunks.append(embed.description)
            for field in embed.fields:
                chunks.append(str(field.name))
                chunks.append(str(field.value))

        text = "\n".join(chunks)
        match = re.search(r"\*\*Message ID:\*\*\s*\[(\d{15,22})\]", text)
        if not match:
            match = re.search(r"/(\d{15,22})(?:\)|\s|$)", text)
        if not match:
            match = re.search(r"\b(\d{15,22})\b", text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _safe_filename(raw: object, index: int) -> str:
        filename = str(raw or f"image-{index}.png").strip() or f"image-{index}.png"
        filename = filename.replace("\\", "_").replace("/", "_")
        filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
        if "." not in filename:
            filename = f"{filename}.png"
        return f"{index}-{filename}"[:96]

    async def _load_persisted_images(self, interaction: discord.Interaction) -> list[dict[str, Any]]:
        db = getattr(self.bot, "db", None)
        guild_id = interaction.guild_id
        message_id = self._extract_deleted_message_id(interaction)
        if not db or not guild_id or not message_id:
            return []
        try:
            return await db.get_deleted_message_attachments(int(guild_id), int(message_id))
        except Exception:
            logger.exception("Failed to load persisted deleted-message images")
            return []

    async def _send_images(self, interaction: discord.Interaction) -> None:
        images = await self._load_persisted_images(interaction)
        if not images:
            images = self.fallback_images

        if not images:
            await interaction.response.send_message("No stored image data is available for this deleted message.", ephemeral=True)
            return

        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        for index, image in enumerate(images[:10], start=1):
            data = image.get("data")
            filename = self._safe_filename(image.get("filename"), index)
            embed = discord.Embed(
                title=image.get("filename") or "Deleted message image",
                color=Colors.INFO,
            )
            if data:
                if isinstance(data, memoryview):
                    data = data.tobytes()
                file = discord.File(io.BytesIO(bytes(data)), filename=filename)
                files.append(file)
                embed.set_image(url=f"attachment://{filename}")
            elif image.get("url"):
                embed.set_image(url=str(image.get("url")))
            else:
                continue
            embeds.append(embed)

        if not embeds:
            await interaction.response.send_message("No stored image data is available for this deleted message.", ephemeral=True)
            return

        kwargs: dict[str, Any] = {"embeds": embeds[:10], "ephemeral": True}
        if files:
            kwargs["files"] = files[:10]
        await interaction.response.send_message(**kwargs)


class Logging(commands.Cog):
    """Event logging system with configurable channels"""
    
    # Command group for logging configuration
    log_group = app_commands.Group(name="log", description="📝 Logging configuration")
    _LOG_CHANNEL_KEY_ALIASES: dict[str, tuple[str, ...]] = {
        "mod": ("mod_log_channel", "log_channel_mod"),
        "audit": ("audit_log_channel", "log_channel_audit"),
        "message": ("message_log_channel", "log_channel_message"),
        "voice": ("voice_log_channel", "log_channel_voice"),
        "automod": ("automod_log_channel", "log_channel_automod"),
        "report": ("report_log_channel", "log_channel_report"),
        "ticket": ("ticket_log_channel", "log_channel_ticket"),
    }
    _MOD_LOG_FALLBACK_TYPES: set[str] = {"automod"}
    _AUDIT_LOG_FALLBACK_TYPES: set[str] = {"message", "voice", "report", "ticket"}

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_cache = ChannelCache(ttl=300)  # 5 minute TTL
        self._audit_search_window_seconds = 15
        self._seen_generic_audit_entries: dict[int, datetime] = {}
        self._suppress_message_delete_until: dict[int, datetime] = {}
        self._message_snapshots: dict[int, discord.Message] = {}
        self._snapshot_lock = asyncio.Lock()

    async def cog_load(self):
        self._cleanup_temp_cache.start()

    async def cog_unload(self):
        self._cleanup_temp_cache.cancel()

    @tasks.loop(hours=1)
    async def _cleanup_temp_cache(self):
        try:
            now = time.time()
            for f in CACHE_DIR.glob("*.bin"):
                if f.is_file() and now - f.stat().st_mtime > 86400:
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        self._suppress_bulk_delete_until: dict[int, datetime] = {}
        self._suppress_timeout_change_until: dict[tuple[int, int], datetime] = {}
        self._seen_webhook_create_entries: dict[int, datetime] = {}
        self._recent_message_snapshots: dict[int, dict[str, Any]] = {}
        self._recent_message_snapshot_ttl = timedelta(hours=3)
        self._recent_message_snapshot_max = 20000
        try:
            self.bot.add_view(DeletedMessageImageView(self.bot))
        except Exception:
            logger.debug("Deleted-message image persistent view was already registered", exc_info=True)

    def _log_channel_setting_keys(self, log_type: str) -> tuple[str, ...]:
        aliases = self._LOG_CHANNEL_KEY_ALIASES.get(log_type)
        if aliases:
            return aliases
        return (f"log_channel_{log_type}", f"{log_type}_log_channel")

    @staticmethod
    def _coerce_channel_id(value: Any) -> Optional[int]:
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return None
        return channel_id if channel_id > 0 else None

    def _resolve_log_channel_id(self, settings: dict[str, Any], log_type: str) -> Optional[int]:
        for key in self._log_channel_setting_keys(log_type):
            channel_id = self._coerce_channel_id(settings.get(key))
            if channel_id:
                return channel_id
        return None

    def _clear_log_channel_settings(self, settings: dict[str, Any], log_type: str) -> bool:
        removed = False
        keys = self._log_channel_setting_keys(log_type)
        for key in keys:
            if key in settings:
                settings[key] = None
                removed = True

        modules = settings.get("modules")
        logging_module = modules.get("logging") if isinstance(modules, dict) else None
        logging_settings = logging_module.get("settings") if isinstance(logging_module, dict) else None
        if isinstance(logging_settings, dict):
            for module_key, aliases in {
                "modChannel": ("mod_log_channel", "log_channel_mod"),
                "auditChannel": ("audit_log_channel", "log_channel_audit"),
                "messageChannel": ("message_log_channel", "log_channel_message"),
                "voiceChannel": ("voice_log_channel", "log_channel_voice"),
                "automodChannel": ("automod_log_channel", "log_channel_automod"),
                "reportChannel": ("report_log_channel", "log_channel_report"),
                "ticketChannel": ("ticket_log_channel", "log_channel_ticket"),
            }.items():
                if any(alias in keys for alias in aliases):
                    logging_settings[module_key] = None
                    removed = True

        return removed

    def suppress_message_delete_log(self, channel_id: int, seconds: int = 6) -> None:
        self._suppress_message_delete_until[channel_id] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def suppress_bulk_delete_log(self, channel_id: int, seconds: int = 8) -> None:
        self._suppress_bulk_delete_until[channel_id] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def _is_message_delete_suppressed(self, channel_id: int) -> bool:
        until = self._suppress_message_delete_until.get(channel_id)
        if not until:
            return False
        if datetime.now(timezone.utc) >= until:
            self._suppress_message_delete_until.pop(channel_id, None)
            return False
        return True

    def _is_bulk_delete_suppressed(self, channel_id: int) -> bool:
        until = self._suppress_bulk_delete_until.get(channel_id)
        if not until:
            return False
        if datetime.now(timezone.utc) >= until:
            self._suppress_bulk_delete_until.pop(channel_id, None)
            return False
        return True

    def suppress_timeout_change_log(self, guild_id: int, user_id: int, seconds: int = 8) -> None:
        key = (guild_id, user_id)
        self._suppress_timeout_change_until[key] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def _is_timeout_change_suppressed(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        until = self._suppress_timeout_change_until.get(key)
        if not until:
            return False
        if datetime.now(timezone.utc) >= until:
            self._suppress_timeout_change_until.pop(key, None)
            return False
        return True

    def _remember_webhook_entry(self, entry_id: int) -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=5)
        stale_ids = [seen_id for seen_id, seen_at in self._seen_webhook_create_entries.items() if seen_at < cutoff]
        for seen_id in stale_ids:
            self._seen_webhook_create_entries.pop(seen_id, None)

        if entry_id in self._seen_webhook_create_entries:
            return False

        self._seen_webhook_create_entries[entry_id] = now
        return True

    def _prune_recent_message_snapshots(self) -> None:
        if not self._recent_message_snapshots:
            return
        now = datetime.now(timezone.utc)
        cutoff = now - self._recent_message_snapshot_ttl
        stale_ids = [
            message_id
            for message_id, data in self._recent_message_snapshots.items()
            if data.get("stored_at", now) < cutoff
        ]
        for message_id in stale_ids:
            self._recent_message_snapshots.pop(message_id, None)

        while len(self._recent_message_snapshots) > self._recent_message_snapshot_max:
            oldest_id = min(
                self._recent_message_snapshots,
                key=lambda mid: self._recent_message_snapshots[mid].get("stored_at", now),
            )
            self._recent_message_snapshots.pop(oldest_id, None)

    def _cache_message_snapshot(self, message: discord.Message) -> None:
        if not message.guild:
            return
        avatar_url = getattr(getattr(message.author, "display_avatar", None), "url", None)
        attachments = self._attachment_records(message.attachments or [])
        self._recent_message_snapshots[message.id] = {
            "stored_at": datetime.now(timezone.utc),
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "author_id": getattr(message.author, "id", None),
            "author_name": getattr(message.author, "name", ""),
            "author_display": getattr(message.author, "display_name", ""),
            "author_avatar_url": str(avatar_url) if avatar_url else None,
            "content": message.content or "",
            "created_ts": int(message.created_at.timestamp()) if getattr(message, "created_at", None) else None,
            "attachments": attachments[:10],
            "attachment_count": len(attachments),
        }
        if len(self._recent_message_snapshots) % 250 == 0:
            self._prune_recent_message_snapshots()

    def _pop_message_snapshot(
        self,
        message_id: int,
        *,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        data = self._recent_message_snapshots.pop(message_id, None)
        if not data:
            return None

        if guild_id is not None and data.get("guild_id") != guild_id:
            return None
        if channel_id is not None and data.get("channel_id") != channel_id:
            return None

        stored_at = data.get("stored_at")
        if isinstance(stored_at, datetime):
            if datetime.now(timezone.utc) - stored_at > self._recent_message_snapshot_ttl:
                return None
        return data

    async def get_log_channel(
        self, 
        guild: discord.Guild, 
        log_type: str,
        *,
        allow_audit_fallback: bool = False,
    ) -> Optional[discord.TextChannel]:
        """
        Get the appropriate log channel for a log type
        Uses advanced caching with TTL to reduce DB queries
        """
        # Check cache first
        channel_id = await self._channel_cache.get(guild.id, log_type)
        
        if channel_id is None:
            # Fetch from DB
            try:
                settings = await self.bot.db.get_settings(guild.id)
                channel_id = self._resolve_log_channel_id(settings, log_type)

                if not channel_id and log_type in self._MOD_LOG_FALLBACK_TYPES:
                    channel_id = self._resolve_log_channel_id(settings, "mod")
                elif not channel_id and log_type in self._AUDIT_LOG_FALLBACK_TYPES:
                    channel_id = self._resolve_log_channel_id(settings, "audit")

                # Global audit fallback: if a type-specific channel is not configured,
                # route logs into the audit channel so events are never dropped.
                if allow_audit_fallback and not channel_id and log_type != "audit":
                    channel_id = self._resolve_log_channel_id(settings, "audit")

                # Keep mod logs strictly moderation-only.
                # If a non-mod log type resolves to the mod log channel, drop it.
                if log_type != "mod" and channel_id:
                    mod_channel_id = self._resolve_log_channel_id(settings, "mod")
                    if mod_channel_id and channel_id == mod_channel_id and log_type not in self._MOD_LOG_FALLBACK_TYPES:
                        # For audit specifically, prefer any alternate audit key first.
                        if log_type == "audit":
                            for key in self._log_channel_setting_keys("audit"):
                                candidate_id = self._coerce_channel_id(settings.get(key))
                                if candidate_id and candidate_id != mod_channel_id:
                                    channel_id = candidate_id
                                    break
                        if channel_id == mod_channel_id:
                            channel_id = None
                 
                # Cache the result (even if None)
                await self._channel_cache.set(guild.id, log_type, channel_id)
            except Exception as e:
                logger.error(f"Failed to get log channel for {guild.name}: {e}")
                return None
        
        # Validate channel exists and is accessible
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel is None:
                # Channel was deleted, remove from cache and DB
                await self._remove_invalid_channel(guild.id, log_type)
                return None
            return channel
        
        return None
    
    async def _remove_invalid_channel(self, guild_id: int, log_type: str) -> None:
        """Remove invalid channel from cache and database"""
        try:
            # Invalidate cache
            await self._channel_cache.invalidate(guild_id, log_type)
            
            # Remove from database
            settings = await self.bot.db.get_settings(guild_id)
            removed = self._clear_log_channel_settings(settings, log_type)
            if removed:
                await self.bot.db.update_settings(guild_id, settings)
            
            logger.warning(f"Removed invalid {log_type} log channel for guild {guild_id}")
        except Exception as e:
            logger.error(f"Error removing invalid channel: {e}")

    async def safe_send_log(
        self, 
        channel: Optional[discord.TextChannel], 
        embed: discord.Embed,
        *,
        use_v2: bool = False,
        view: Optional[discord.ui.View] = None,
        mirror_to_audit: bool = False,
    ) -> bool:
        """
        Safely send a log embed with enhanced error handling
        Returns: bool indicating success
        
        Args:
            channel: The channel to send to
            embed: The embed to send
            use_v2: Retained for API compatibility; logs always use Components V2
            view: Optional view to attach to the log message
            mirror_to_audit: Also send this log to the configured audit log channel
        """
        if not channel:
            return False

        # Hard routing guard: if an audit/message card is about to be posted in the
        # wrong channel, reroute before sending.
        routed_channel = channel
        try:
            destination_type = self._classify_misrouted_log_embed(embed)
            if destination_type:
                settings = await self.bot.db.get_settings(channel.guild.id)
                mod_channel_id = self._resolve_log_channel_id(settings, "mod")
                source_is_mod = bool(mod_channel_id and channel.id == mod_channel_id)
                destination_id = self._resolve_log_channel_id(settings, destination_type)
                if destination_id and destination_id != channel.id:
                    destination_channel = channel.guild.get_channel(destination_id)
                    if isinstance(destination_channel, discord.TextChannel):
                        routed_channel = destination_channel
                    elif source_is_mod:
                        logger.warning(
                            "Dropping %s log in %s: destination channel is invalid.",
                            destination_type,
                            channel.guild.name,
                        )
                        return False
                elif not destination_id and source_is_mod:
                    logger.warning(
                        "Dropping %s log in %s: destination channel is not configured.",
                        destination_type,
                        channel.guild.name,
                    )
                    return False
        except Exception:
            routed_channel = channel

        sent_primary = False
        try:
            normalized = await prepare_log_embed(routed_channel, embed)
            layout = await layout_view_from_embeds(
                embed=normalized,
                existing_view=view,
            )
            await routed_channel.send(
                view=ensure_layout_view_action_rows(layout),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent_primary = True
        except discord.Forbidden:
            logger.warning(f"Missing permissions to log in {routed_channel.guild.name} #{routed_channel.name}")
            # Invalidate cache for this channel
            await self._channel_cache.invalidate(routed_channel.guild.id, "unknown")
        except discord.NotFound:
            logger.warning(f"Log channel not found: {routed_channel.guild.name} #{routed_channel.name}")
            # Channel was deleted
            await self._channel_cache.invalidate(routed_channel.guild.id, "unknown")
        except discord.HTTPException as e:
            logger.error(f"Failed to send log in {routed_channel.guild.name}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending log: {e}")
        sent_audit = False
        if mirror_to_audit:
            try:
                audit_channel = await self.get_log_channel(routed_channel.guild, "audit")
            except Exception:
                audit_channel = None

            if audit_channel and audit_channel.id != routed_channel.id:
                try:
                    # Do not reuse the same View object across multiple messages.
                    normalized_audit = await prepare_log_embed(audit_channel, embed)
                    layout = await layout_view_from_embeds(embed=normalized_audit)
                    await audit_channel.send(
                        view=ensure_layout_view_action_rows(layout),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    sent_audit = True
                except discord.Forbidden:
                    logger.warning(f"Missing permissions to log in {audit_channel.guild.name} #{audit_channel.name}")
                    await self._channel_cache.invalidate(audit_channel.guild.id, "audit")
                except discord.NotFound:
                    logger.warning(f"Audit log channel not found: {audit_channel.guild.name} #{audit_channel.name}")
                    await self._channel_cache.invalidate(audit_channel.guild.id, "audit")
                except discord.HTTPException as e:
                    logger.error(f"Failed to mirror log to audit in {audit_channel.guild.name}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error mirroring audit log: {e}")

        return sent_primary or sent_audit

    def _shorten(self, text: Optional[str], limit: int) -> str:
        if not text:
            return "*None*"
        text = str(text).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _attachment_records(self, attachments: list[discord.Attachment]) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for attachment in attachments:
            filename = str(getattr(attachment, "filename", "") or "attachment")
            url = str(getattr(attachment, "url", "") or getattr(attachment, "proxy_url", "") or "")
            content_type = str(getattr(attachment, "content_type", "") or "")
            records.append(
                {
                    "filename": filename,
                    "url": url,
                    "content_type": content_type,
                }
            )
        return records

    async def _attachment_records_with_data(
        self,
        attachments: list[discord.Attachment],
        *,
        max_bytes_each: int = 8_000_000,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for attachment in attachments:
            filename = str(getattr(attachment, "filename", "") or "attachment")
            url = str(getattr(attachment, "url", "") or getattr(attachment, "proxy_url", "") or "")
            content_type = str(getattr(attachment, "content_type", "") or "")
            record: dict[str, Any] = {
                "filename": filename,
                "url": url,
                "content_type": content_type,
            }
            if self._is_image_attachment(record):
                size = int(getattr(attachment, "size", 0) or 0)
                if size <= max_bytes_each:
                    try:
                        data = await attachment.read(use_cached=True)
                    except Exception:
                        logger.debug("Failed to read deleted-message image attachment %s", filename, exc_info=True)
                    else:
                        if data and len(data) <= max_bytes_each:
                            record["data"] = data
            records.append(record)
        return records

    async def _persist_deleted_image_attachments(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        attachment_records: list[dict[str, Any]],
    ) -> None:
        images = [
            attachment
            for attachment in attachment_records
            if self._is_image_attachment(attachment) and attachment.get("data")
        ]
        if not images:
            return
        db = getattr(self.bot, "db", None)
        if not db:
            return
        try:
            await db.save_deleted_message_attachments(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                attachments=images,
            )
        except Exception:
            pass

    async def _process_message_attachments_on_send(self, message: discord.Message) -> None:
        """Downloads attachments to local temp storage when a message is sent."""
        if not message.attachments:
            return
        try:
            records = await self._attachment_records_with_data(message.attachments)
            for i, record in enumerate(records):
                data = record.get("data")
                if data:
                    cache_file = CACHE_DIR / f"{message.id}_{i}.bin"
                    def _write(cf, d):
                        cf.write_bytes(d)
                    await asyncio.to_thread(_write, cache_file, data)
        except Exception:
            logger.exception("Failed to cache message image attachments locally")

    @staticmethod
    def _attachment_name(attachment: object) -> str:
        if isinstance(attachment, dict):
            return str(attachment.get("filename") or "attachment")
        return str(attachment or "attachment")

    @staticmethod
    def _is_image_attachment(attachment: object) -> bool:
        if not isinstance(attachment, dict):
            return False
        filename = str(attachment.get("filename") or "").casefold()
        content_type = str(attachment.get("content_type") or "").casefold()
        return content_type.startswith("image/") or filename.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        )

    def _image_attachments(self, attachments: list[object]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        for attachment in attachments:
            if isinstance(attachment, dict) and self._is_image_attachment(attachment):
                url = attachment.get("url") or attachment.get("original_url")
                if url:
                    images.append(
                        {
                            "filename": str(attachment.get("filename") or "image"),
                            "url": str(url),
                        }
                    )
        return images

    def _format_attachment_names(self, attachments: list[object], attachment_count: int) -> str:
        names = [self._attachment_name(attachment) for attachment in attachments[:10]]
        attachments_text = ", ".join(names)
        if attachment_count > len(names):
            attachments_text += f", +{attachment_count - len(names)} more"
        return attachments_text or "*Unknown attachment*"

    def _yn(self, value: Optional[bool]) -> str:
        if value is None:
            return "*N/A*"
        return "Yes" if value else "No"

    async def _find_recent_audit_entry(
        self,
        guild: discord.Guild,
        *,
        action: discord.AuditLogAction,
        target_id: int,
    ) -> Optional[discord.AuditLogEntry]:
        now = datetime.now(timezone.utc)
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                entry_target_id = getattr(getattr(entry, "target", None), "id", None)
                if entry_target_id != target_id:
                    continue
                age = (now - entry.created_at).total_seconds()
                if age <= self._audit_search_window_seconds:
                    return entry
        except discord.Forbidden:
            return None
        except Exception as e:
            logger.error(f"Failed to query audit logs in {guild.name}: {e}")
            return None
        return None

    async def _find_recent_message_delete_entry(
        self,
        guild: discord.Guild,
        *,
        channel_id: int,
        author_id: Optional[int] = None,
    ) -> Optional[discord.AuditLogEntry]:
        now = datetime.now(timezone.utc)
        try:
            async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.message_delete):
                age = (now - entry.created_at).total_seconds()
                if age > self._audit_search_window_seconds:
                    continue

                extra = getattr(entry, "extra", None)
                entry_channel = getattr(extra, "channel", None)
                entry_channel_id = getattr(entry_channel, "id", None) or getattr(extra, "channel_id", None)
                if entry_channel_id is not None and entry_channel_id != channel_id:
                    continue

                if author_id is not None:
                    target_id = getattr(getattr(entry, "target", None), "id", None)
                    if target_id is not None and target_id != author_id:
                        continue

                return entry
        except discord.Forbidden:
            return None
        except Exception as e:
            logger.error(f"Failed to query message delete audit logs in {guild.name}: {e}")
            return None
        return None

    def _format_user_reference(self, user: Optional[discord.abc.User]) -> str:
        if user is None:
            return "*Unknown*"
        mention = getattr(user, "mention", None)
        primary = str(user)
        if mention:
            return f"{primary} ({mention})"
        return primary

    @staticmethod
    def _is_emoji_audit_action(action: discord.AuditLogAction) -> bool:
        return action in {
            discord.AuditLogAction.emoji_create,
            discord.AuditLogAction.emoji_update,
            discord.AuditLogAction.emoji_delete,
        }

    def _emoji_audit_metadata(self, entry: discord.AuditLogEntry) -> tuple[Optional[int], Optional[str], Optional[bool]]:
        target = getattr(entry, "target", None)
        emoji_id = getattr(target, "id", None)
        emoji_name = getattr(target, "name", None)
        animated = getattr(target, "animated", None)

        before = getattr(entry, "before", None)
        after = getattr(entry, "after", None)
        if not emoji_name:
            emoji_name = getattr(after, "name", None) or getattr(before, "name", None)
        if animated is None:
            animated = getattr(after, "animated", None)
            if animated is None:
                animated = getattr(before, "animated", None)

        if emoji_id is not None:
            try:
                emoji_id = int(emoji_id)
            except (TypeError, ValueError):
                emoji_id = None

        if emoji_name is not None:
            emoji_name = str(emoji_name).strip() or None

        if animated is not None:
            animated = bool(animated)

        return emoji_id, emoji_name, animated

    def _emoji_token(self, emoji_id: Optional[int], emoji_name: Optional[str], animated: Optional[bool]) -> Optional[str]:
        if not emoji_id or not emoji_name:
            return None
        prefix = "a" if animated else ""
        return f"<{prefix}:{emoji_name}:{emoji_id}>"

    def _emoji_cdn_url(self, emoji_id: Optional[int], animated: Optional[bool]) -> Optional[str]:
        if not emoji_id:
            return None
        ext = "gif" if animated else "png"
        return f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"

    def _audit_thumbnail_url(self, entry: discord.AuditLogEntry) -> Optional[str]:
        action = getattr(entry, "action", None)
        if action and self._is_emoji_audit_action(action):
            emoji_id, _, animated = self._emoji_audit_metadata(entry)
            return self._emoji_cdn_url(emoji_id, animated)

        target = getattr(entry, "target", None)
        avatar = getattr(getattr(target, "display_avatar", None), "url", None)
        if avatar:
            return avatar
        return None

    def _format_channel_reference(self, channel: Optional[object], *, fallback_id: Optional[int] = None) -> str:
        if channel is None:
            if fallback_id is not None:
                return f"<#{fallback_id}>"
            return "*Unknown*"
        channel_name = getattr(channel, "name", "unknown")
        mention = getattr(channel, "mention", None)
        if mention:
            return f"{channel_name} ({mention})"
        return channel_name

    def _format_role_reference(self, role: Optional[discord.Role]) -> str:
        if role is None:
            return "*Unknown*"
        role_name = getattr(role, "name", "unknown") or "unknown"
        mention = getattr(role, "mention", None)
        if mention:
            # Keep plain role name first so any custom emoji in the role name renders.
            return f"{role_name} ({mention})"
        role_id = getattr(role, "id", None)
        if role_id is not None:
            return f"{role_name} (`{role_id}`)"
        return role_name

    def _remember_generic_audit_entry(self, entry_id: int) -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=30)
        stale_ids = [
            seen_id
            for seen_id, seen_at in self._seen_generic_audit_entries.items()
            if seen_at < cutoff
        ]
        for seen_id in stale_ids:
            self._seen_generic_audit_entries.pop(seen_id, None)

        if entry_id in self._seen_generic_audit_entries:
            return False

        self._seen_generic_audit_entries[entry_id] = now
        return True

    @staticmethod
    def _audit_channel_kind(target: Optional[object]) -> str:
        if isinstance(target, discord.TextChannel):
            return "Text channel"
        if isinstance(target, discord.VoiceChannel):
            return "Voice channel"
        if isinstance(target, discord.StageChannel):
            return "Stage channel"
        if isinstance(target, discord.CategoryChannel):
            return "Category channel"
        if isinstance(target, discord.ForumChannel):
            return "Forum channel"
        if isinstance(target, discord.Thread):
            return "Thread"
        return "Channel"

    def _audit_action_title(self, entry: discord.AuditLogEntry) -> str:
        action = entry.action
        target = getattr(entry, "target", None)
        names: dict[discord.AuditLogAction, str] = {
            discord.AuditLogAction.guild_update: "Server updated",
            discord.AuditLogAction.channel_update: f"{self._audit_channel_kind(target)} updated",
            discord.AuditLogAction.overwrite_create: f"{self._audit_channel_kind(target)} permissions created",
            discord.AuditLogAction.overwrite_update: f"{self._audit_channel_kind(target)} permissions updated",
            discord.AuditLogAction.overwrite_delete: f"{self._audit_channel_kind(target)} permissions deleted",
            discord.AuditLogAction.role_update: "Role updated",
            discord.AuditLogAction.webhook_update: "Webhook updated",
            discord.AuditLogAction.webhook_delete: "Webhook deleted",
            discord.AuditLogAction.invite_create: "Invite created",
            discord.AuditLogAction.invite_update: "Invite updated",
            discord.AuditLogAction.invite_delete: "Invite deleted",
            discord.AuditLogAction.emoji_create: "Emoji created",
            discord.AuditLogAction.emoji_update: "Emoji updated",
            discord.AuditLogAction.emoji_delete: "Emoji deleted",
            discord.AuditLogAction.sticker_create: "Sticker created",
            discord.AuditLogAction.sticker_update: "Sticker updated",
            discord.AuditLogAction.sticker_delete: "Sticker deleted",
            discord.AuditLogAction.integration_create: "Integration created",
            discord.AuditLogAction.integration_update: "Integration updated",
            discord.AuditLogAction.integration_delete: "Integration deleted",
            discord.AuditLogAction.stage_instance_create: "Stage instance created",
            discord.AuditLogAction.stage_instance_update: "Stage instance updated",
            discord.AuditLogAction.stage_instance_delete: "Stage instance deleted",
            discord.AuditLogAction.scheduled_event_create: "Scheduled event created",
            discord.AuditLogAction.scheduled_event_update: "Scheduled event updated",
            discord.AuditLogAction.scheduled_event_delete: "Scheduled event deleted",
            discord.AuditLogAction.thread_create: "Thread created",
            discord.AuditLogAction.thread_update: "Thread updated",
            discord.AuditLogAction.thread_delete: "Thread deleted",
            discord.AuditLogAction.app_command_permission_update: "Application command permissions updated",
            discord.AuditLogAction.soundboard_sound_create: "Soundboard sound created",
            discord.AuditLogAction.soundboard_sound_update: "Soundboard sound updated",
            discord.AuditLogAction.soundboard_sound_delete: "Soundboard sound deleted",
            discord.AuditLogAction.automod_rule_create: "AutoMod rule created",
            discord.AuditLogAction.automod_rule_update: "AutoMod rule updated",
            discord.AuditLogAction.automod_rule_delete: "AutoMod rule deleted",
            discord.AuditLogAction.onboarding_prompt_create: "Onboarding prompt created",
            discord.AuditLogAction.onboarding_prompt_update: "Onboarding prompt updated",
            discord.AuditLogAction.onboarding_prompt_delete: "Onboarding prompt deleted",
            discord.AuditLogAction.onboarding_create: "Onboarding created",
            discord.AuditLogAction.onboarding_update: "Onboarding updated",
            discord.AuditLogAction.home_settings_create: "Home settings created",
            discord.AuditLogAction.home_settings_update: "Home settings updated",
            discord.AuditLogAction.member_prune: "Member prune executed",
            discord.AuditLogAction.bot_add: "Bot added",
        }
        if action in names:
            return names[action]
        return action.name.replace("_", " ").title()

    @staticmethod
    def _audit_action_color(action: discord.AuditLogAction) -> int:
        name = action.name
        if name.endswith("_create") or name in {"bot_add"}:
            return Colors.SUCCESS
        if name.endswith("_delete") or name in {"kick", "ban", "member_disconnect"}:
            return Colors.ERROR
        if name.endswith("_update") or name in {"guild_update"}:
            return Colors.INFO
        if name.startswith("automod_"):
            return Colors.WARNING
        return Colors.INFO

    def _format_audit_value(self, value: Any) -> str:
        if value is None:
            return "*None*"
        if isinstance(value, bool):
            return self._yn(value)
        if isinstance(value, datetime):
            return f"<t:{int(value.timestamp())}:F>"
        if isinstance(value, discord.Role):
            return self._format_role_reference(value)
        if isinstance(value, (discord.Member, discord.User)):
            return self._format_user_reference(value)
        if isinstance(value, discord.abc.GuildChannel):
            return self._format_channel_reference(value)
        if isinstance(value, discord.Object):
            return f"`{value.id}`"
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if not values:
                return "*None*"
            rendered = [self._format_audit_value(v) for v in values[:5]]
            text = ", ".join(rendered)
            if len(values) > 5:
                text += f", +{len(values) - 5} more"
            return self._shorten(text, 300)

        text = str(value).strip()
        if not text:
            return "*None*"
        return self._shorten(text, 300)

    def _audit_target_lines(self, entry: discord.AuditLogEntry) -> list[str]:
        target = getattr(entry, "target", None)
        lines: list[str] = []

        if self._is_emoji_audit_action(entry.action):
            emoji_id, emoji_name, animated = self._emoji_audit_metadata(entry)
            lines.append(f"**Name:** {emoji_name or '*Unknown*'}")
            lines.append(f"**ID:** `{emoji_id}`" if emoji_id is not None else "**ID:** *Unknown*")
            lines.append(f"**Animated:** {self._yn(animated)}")
            emoji_token = self._emoji_token(emoji_id, emoji_name, animated)
            if emoji_token:
                lines.append(f"**Emoji:** {emoji_token}")
            return lines

        if isinstance(target, discord.abc.GuildChannel):
            lines.append(f"**Name:** {self._format_channel_reference(target)}")
            lines.append(f"**ID:** `{target.id}`")
            return lines

        if isinstance(target, discord.Role):
            lines.append(f"**Role:** {self._format_role_reference(target)}")
            lines.append(f"**ID:** `{target.id}`")
            return lines

        if isinstance(target, (discord.Member, discord.User)):
            lines.append(f"**User:** {self._format_user_reference(target)}")
            lines.append(f"**ID:** `{target.id}`")
            return lines

        if isinstance(target, discord.Emoji):
            lines.append(f"**Name:** {target.name}")
            lines.append(f"**ID:** `{target.id}`")
            lines.append(f"**Animated:** {self._yn(getattr(target, 'animated', None))}")
            return lines

        if isinstance(target, discord.Invite):
            code = getattr(target, "code", None)
            lines.append(f"**Invite:** `{code}`" if code else "**Invite:** *Unknown*")
            invite_channel = getattr(target, "channel", None)
            if invite_channel is not None:
                lines.append(f"**Channel:** {self._format_channel_reference(invite_channel)}")
            return lines

        target_name = getattr(target, "name", None)
        target_id = getattr(target, "id", None)
        if target_name:
            lines.append(f"**Target:** {target_name}")
        elif target is not None:
            lines.append(f"**Target:** {self._shorten(str(target), 180)}")
        else:
            lines.append("**Target:** *Unknown*")
        if target_id is not None:
            lines.append(f"**ID:** `{target_id}`")

        return lines

    def _audit_extra_lines(self, entry: discord.AuditLogEntry) -> list[str]:
        extra = getattr(entry, "extra", None)
        if extra is None:
            return []

        lines: list[str] = []

        extra_channel = getattr(extra, "channel", None)
        if extra_channel is not None:
            lines.append(f"**Channel:** {self._format_channel_reference(extra_channel)}")

        extra_role = getattr(extra, "role", None)
        if isinstance(extra_role, discord.Role):
            lines.append(f"**Role:** {self._format_role_reference(extra_role)}")

        count = getattr(extra, "count", None)
        if count is not None:
            lines.append(f"**Count:** {count}")

        members_removed = getattr(extra, "members_removed", None)
        if members_removed is not None:
            lines.append(f"**Members removed:** {members_removed}")

        delete_member_days = getattr(extra, "delete_member_days", None)
        if delete_member_days is not None:
            lines.append(f"**Delete message days:** {delete_member_days}")

        return lines

    def _audit_change_lines(self, entry: discord.AuditLogEntry, *, max_items: int = 8) -> list[str]:
        raw_changes = getattr(entry, "changes", None)
        if raw_changes is None:
            return []

        before_values = self._audit_diff_mapping(getattr(raw_changes, "before", None))
        after_values = self._audit_diff_mapping(getattr(raw_changes, "after", None))
        if not before_values and not after_values:
            return []

        ordered_keys = list(dict.fromkeys([*before_values, *after_values]))
        changes: list[tuple[str, Any, Any]] = []
        for key in ordered_keys:
            before = before_values.get(key)
            after = after_values.get(key)
            if before != after:
                changes.append((key, before, after))

        lines: list[str] = []
        for key, before, after in changes[:max_items]:
            label = self._audit_change_label(key)
            if isinstance(before, discord.Permissions) and isinstance(after, discord.Permissions):
                value = self._format_permission_delta(before, after)
                lines.append(f"**{label}:** {value}")
                continue
            before_text = self._format_audit_value(before)
            after_text = self._format_audit_value(after)
            lines.append(f"**{label}:** {before_text} → {after_text}")

        if len(changes) > max_items:
            lines.append(f"**Additional changes:** +{len(changes) - max_items} more")

        return lines

    @staticmethod
    def _audit_diff_mapping(diff: Any) -> dict[str, Any]:
        if diff is None:
            return {}
        try:
            values = dict(diff)
        except (TypeError, ValueError):
            try:
                values = dict(vars(diff))
            except TypeError:
                return {}

        aliases = {
            "colour": "color",
            "secondary_colour": "secondary_color",
            "tertiary_colour": "tertiary_color",
            "expire_behaviour": "expire_behavior",
        }
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            normalized.setdefault(aliases.get(str(key), str(key)), value)
        return normalized

    @staticmethod
    def _audit_change_label(key: str) -> str:
        labels = {
            "color": "Color",
            "secondary_color": "Secondary color",
            "tertiary_color": "Tertiary color",
            "hoist": "Displayed separately",
            "mentionable": "Mentionable",
            "permissions": "Permissions changed",
            "icon": "Role icon",
            "unicode_emoji": "Role emoji",
            "position": "Position",
        }
        return labels.get(key, key.replace("_", " ").title())

    def _format_permission_delta(
        self,
        before: discord.Permissions,
        after: discord.Permissions,
    ) -> str:
        before_flags = dict(before)
        after_flags = dict(after)
        enabled: list[str] = []
        disabled: list[str] = []
        for name in dict.fromkeys([*before_flags, *after_flags]):
            old_value = before_flags.get(name, False)
            new_value = after_flags.get(name, False)
            if old_value == new_value:
                continue
            label = {
                "read_messages": "View Channels",
                "external_emojis": "Use External Emoji",
                "external_stickers": "Use External Stickers",
            }.get(name, name.replace("_", " ").title())
            (enabled if new_value else disabled).append(label)

        parts: list[str] = []
        if enabled:
            parts.append(f"Enabled: {', '.join(enabled)}")
        if disabled:
            parts.append(f"Disabled: {', '.join(disabled)}")
        return self._shorten("; ".join(parts) or "No effective permission changes", 900)

    @staticmethod
    def _should_skip_generic_audit_action(action: discord.AuditLogAction) -> bool:
        # Skip actions already logged in other channels (or by dedicated handlers).
        skip_actions = {
            # Message log channel
            discord.AuditLogAction.message_delete,
            discord.AuditLogAction.message_bulk_delete,
            # Mod log channel
            discord.AuditLogAction.kick,
            discord.AuditLogAction.ban,
            discord.AuditLogAction.unban,
            discord.AuditLogAction.member_update,
            # Existing dedicated audit/member handlers
            discord.AuditLogAction.member_role_update,
            discord.AuditLogAction.channel_create,
            discord.AuditLogAction.channel_delete,
            discord.AuditLogAction.role_create,
            discord.AuditLogAction.role_delete,
            discord.AuditLogAction.webhook_create,
            # Voice log channel
            discord.AuditLogAction.member_move,
            discord.AuditLogAction.member_disconnect,
            # AutoMod channel already captures execution-level events
            discord.AuditLogAction.automod_block_message,
            discord.AuditLogAction.automod_flag_message,
            discord.AuditLogAction.automod_timeout_member,
            discord.AuditLogAction.automod_quarantine_user,
        }
        return action in skip_actions

    @staticmethod
    def _classify_misrouted_log_embed(embed: discord.Embed) -> Optional[str]:
        """Return target log type for known misplaced cards, otherwise None."""
        title = (getattr(embed, "title", "") or "").strip().lower()
        if not title:
            return None

        # Message delete/edit cards should live in message logs only.
        if (
            title == "message deleted"
            or title.endswith("messages deleted")
            or title == "bulk message delete"
            or title == "message edited"
        ):
            return "message"

        # Moderation action cards belong in mod logs, not audit.
        mod_markers = (
            "user kicked",
            "user banned",
            "user softbanned",
            "user temporarily banned",
            "user unbanned",
            "member kicked",
            "member banned",
            "member unbanned",
            "user timed out",
            "user timeout removed",
            "user warned",
            "user muted",
            "user unmuted",
            "user quarantined",
            "quarantine lifted",
            "mass ban",
            "moderator purge",
        )
        if any(marker in title for marker in mod_markers):
            return "mod"

        # Audit/server event cards should not appear in mod logs.
        audit_markers = (
            "permissions updated",
            "channel created",
            "channel deleted",
            "role created",
            "role deleted",
            "role updated",
            "roles updated",
            "role name update",
            "webhook created",
            "emoji created",
            "emoji deleted",
            "emoji updated",
            "emoji removed",
            "sticker created",
            "sticker deleted",
            "sticker updated",
            "invite created",
            "invite deleted",
            "nickname changed",
            "member joined",
            "member left",
        )
        if any(marker in title for marker in audit_markers):
            return "audit"

        return None

    async def _reroute_misplaced_log_message(self, message: discord.Message) -> None:
        """
        Move known misrouted log cards out of mod logs.

        This is a safety net for emitters that still post audit/message cards to
        the mod log channel.
        """
        if not message.guild or not message.embeds:
            return

        # Accept bot/webhook-authored cards, not only this exact user ID.
        # Some paths may post via alternate bot identity/webhook wrappers.
        if not getattr(message.author, "bot", False) and message.webhook_id is None:
            return

        settings = await self.bot.db.get_settings(message.guild.id)
        mod_channel_id = self._resolve_log_channel_id(settings, "mod")
        if not mod_channel_id or message.channel.id != mod_channel_id:
            return

        destination_type = self._classify_misrouted_log_embed(message.embeds[0])
        if not destination_type:
            return

        destination_id = self._resolve_log_channel_id(settings, destination_type)
        if not destination_id or destination_id == message.channel.id:
            return

        destination_channel = message.guild.get_channel(destination_id)
        if destination_channel is None:
            return

        try:
            normalized_embeds = [
                await prepare_log_embed(destination_channel, embed)
                for embed in message.embeds[:10]
            ]
            layout = await layout_view_from_embeds(
                content=message.content or None,
                embeds=normalized_embeds,
            )
            await destination_channel.send(
                view=ensure_layout_view_action_rows(layout),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await message.delete()
        except discord.Forbidden:
            logger.warning(
                "Missing permissions while rerouting misplaced log in %s",
                message.guild.name,
            )
        except discord.HTTPException as e:
            logger.error(
                "Failed to reroute misplaced log in %s: %s",
                message.guild.name,
                e,
            )
        except Exception as e:
            logger.error(
                "Unexpected error rerouting misplaced log in %s: %s",
                message.guild.name,
                e,
            )

    @staticmethod
    def _quote_lines(lines: list[str]) -> str:
        return "\n".join(f"> {line}" for line in lines if line)

    def _build_sapphire_log_embed(
        self,
        *,
        title: str,
        color: int,
        details_lines: list[str],
        message_text: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        footer_user: Optional[discord.abc.User] = None,
        footer_text: Optional[str] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        if details_lines:
            embed.description = self._quote_lines(details_lines)

        if message_text is not None:
            value = (message_text or "").strip() or "*No content*"
            if len(value) > 1024:
                value = value[:1021].rstrip() + "..."
            embed.add_field(name="Message", value=value, inline=False)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        if footer_user is not None:
            footer_name = f"@{getattr(footer_user, 'name', str(footer_user))}"
            footer_icon = getattr(getattr(footer_user, "display_avatar", None), "url", None)
            embed.set_footer(text=footer_name, icon_url=footer_icon)
        elif footer_text:
            embed.set_footer(text=footer_text)

        return embed

    def _add_channel_details_fields(self, embed: discord.Embed, channel: discord.abc.GuildChannel) -> None:
        category = getattr(channel, "category", None)
        category_value = category.mention if category else "*None*"
        embed.add_field(name="Category", value=category_value, inline=True)
        embed.add_field(name="Position", value=str(getattr(channel, "position", "*N/A*")), inline=True)
        embed.add_field(
            name="Overwrites",
            value=str(len(getattr(channel, "overwrites", {}) or {})),
            inline=True,
        )

        nsfw = getattr(channel, "nsfw", None)
        if nsfw is not None:
            embed.add_field(name="NSFW", value=self._yn(nsfw), inline=True)
        else:
            embed.add_field(name="NSFW", value="*N/A*", inline=True)

        slowmode_delay = getattr(channel, "slowmode_delay", None)
        if slowmode_delay is not None:
            embed.add_field(name="Slowmode", value=f"{slowmode_delay}s", inline=True)
        else:
            embed.add_field(name="Slowmode", value="*N/A*", inline=True)

        permissions_synced = getattr(channel, "permissions_synced", None)
        if permissions_synced is not None:
            embed.add_field(name="Synced", value=self._yn(permissions_synced), inline=True)
        else:
            embed.add_field(name="Synced", value="*N/A*", inline=True)

        topic = getattr(channel, "topic", None)
        if topic is not None:
            embed.add_field(name="Topic", value=self._shorten(topic, 200), inline=False)

        bitrate = getattr(channel, "bitrate", None)
        user_limit = getattr(channel, "user_limit", None)
        if bitrate is not None or user_limit is not None:
            bitrate_text = f"{int(bitrate/1000)}kbps" if bitrate else "*N/A*"
            limit_text = str(user_limit) if user_limit else "∞"
            embed.add_field(name="Voice", value=f"Bitrate: {bitrate_text} | Limit: {limit_text}", inline=False)

    def _add_role_details_fields(
        self,
        embed: discord.Embed,
        role: discord.Role,
        *,
        include_members: bool = True,
    ) -> None:
        color_value = role.color.value
        color_text = f"#{color_value:06x}" if color_value else "Default"
        embed.add_field(name="Color", value=color_text, inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)
        if include_members:
            embed.add_field(name="Members", value=str(len(role.members)), inline=True)
        embed.add_field(name="Hoist", value=self._yn(role.hoist), inline=True)
        embed.add_field(name="Mentionable", value=self._yn(role.mentionable), inline=True)
        embed.add_field(name="Managed", value=self._yn(role.managed), inline=True)

        perms = role.permissions
        key_perms = [
            ("Administrator", perms.administrator),
            ("Manage Server", perms.manage_guild),
            ("Manage Roles", perms.manage_roles),
            ("Manage Channels", perms.manage_channels),
            ("Ban Members", perms.ban_members),
            ("Kick Members", perms.kick_members),
        ]
        perms_text = " | ".join([f"{name}: {self._yn(val)}" for name, val in key_perms])
        embed.add_field(name="Key Perms", value=perms_text, inline=False)

    def _channel_detail_lines(self, channel: discord.abc.GuildChannel) -> list[str]:
        category = getattr(channel, "category", None)
        category_value = category.mention if category else "*None*"
        lines = [
            f"**Category:** {category_value}",
            f"**Position:** {getattr(channel, 'position', '*N/A*')}",
            f"**Overwrites:** {len(getattr(channel, 'overwrites', {}) or {})}",
        ]

        nsfw = getattr(channel, "nsfw", None)
        lines.append(f"**NSFW:** {self._yn(nsfw) if nsfw is not None else '*N/A*'}")

        slowmode_delay = getattr(channel, "slowmode_delay", None)
        lines.append(
            f"**Slowmode:** {f'{slowmode_delay}s' if slowmode_delay is not None else '*N/A*'}"
        )

        permissions_synced = getattr(channel, "permissions_synced", None)
        lines.append(
            f"**Synced:** {self._yn(permissions_synced) if permissions_synced is not None else '*N/A*'}"
        )

        topic = getattr(channel, "topic", None)
        if topic:
            lines.append(f"**Topic:** {self._shorten(topic, 180)}")

        bitrate = getattr(channel, "bitrate", None)
        user_limit = getattr(channel, "user_limit", None)
        if bitrate is not None or user_limit is not None:
            bitrate_text = f"{int(bitrate/1000)}kbps" if bitrate else "*N/A*"
            limit_text = str(user_limit) if user_limit else "∞"
            lines.append(f"**Voice:** Bitrate {bitrate_text}, Limit {limit_text}")

        return lines

    def _role_detail_lines(self, role: discord.Role, *, include_members: bool = True) -> list[str]:
        color_value = role.color.value
        color_text = f"#{color_value:06x}" if color_value else "Default"
        lines = [
            f"**Color:** {color_text}",
            f"**Position:** {role.position}",
            f"**Hoist:** {self._yn(role.hoist)}",
            f"**Mentionable:** {self._yn(role.mentionable)}",
            f"**Managed:** {self._yn(role.managed)}",
        ]
        if include_members:
            lines.insert(2, f"**Members:** {len(role.members)}")

        perms = role.permissions
        key_perms = [
            ("Administrator", perms.administrator),
            ("Manage Server", perms.manage_guild),
            ("Manage Roles", perms.manage_roles),
            ("Manage Channels", perms.manage_channels),
            ("Ban Members", perms.ban_members),
            ("Kick Members", perms.kick_members),
        ]
        enabled = [name for name, enabled in key_perms if enabled]
        lines.append(f"**Key perms:** {', '.join(enabled) if enabled else 'None'}")
        return lines

    # ==================== MESSAGE LOGGING ====================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Cache recent messages so raw delete fallback can still show author/content."""
        if not message.guild:
            return
        try:
            await self._reroute_misplaced_log_message(message)
        except Exception:
            pass
        try:
            self._cache_message_snapshot(message)
        except Exception:
            pass
            
        if message.attachments:
            self.bot.loop.create_task(self._process_message_attachments_on_send(message))
    
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Log deleted messages"""
        # Ignore DMs
        if not message.guild:
            return

        if message.author.id == self.bot.user.id:
            return

        self._pop_message_snapshot(
            message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
        )

        if self._is_message_delete_suppressed(message.channel.id):
            return

        channel = await self.get_log_channel(message.guild, 'message', allow_audit_fallback=False)
        if not channel:
            return

        delete_entry = await self._find_recent_message_delete_entry(
            message.guild,
            channel_id=message.channel.id,
            author_id=getattr(message.author, "id", None),
        )
        deleter = getattr(delete_entry, "user", None) or message.author
        delete_reason = getattr(delete_entry, "reason", None)

        message_link = (
            f"https://discord.com/channels/"
            f"{message.guild.id}/{message.channel.id}/{message.id}"
        )
        details_lines = [
            f"**Source channel:** {self._format_channel_reference(message.channel)}",
            f"**Message ID:** [{message.id}]({message_link})",
            f"**Message author:** {self._format_user_reference(message.author)}",
            f"**Message created:** <t:{int(message.created_at.timestamp())}:R>",
        ]
        if deleter is not None:
            details_lines.append(f"**Deleted by:** {self._format_user_reference(deleter)}")
        if delete_reason:
            details_lines.append(f"**Reason:** {self._shorten(delete_reason, 250)}")

        db = getattr(self.bot, "db", None)
        attachment_records = []
        if db:
            attachment_records = await db.get_deleted_message_attachments(
                guild_id=message.guild.id,
                message_id=message.id,
            )
            
        if not attachment_records:
            attachment_records = self._attachment_records(message.attachments or [])
            has_data = False
            for i, record in enumerate(attachment_records):
                cache_file = CACHE_DIR / f"{message.id}_{i}.bin"
                if cache_file.exists():
                    try:
                        def _read(cf):
                            return cf.read_bytes()
                        record["data"] = await asyncio.to_thread(_read, cache_file)
                        has_data = True
                        def _unlink(cf):
                            cf.unlink(missing_ok=True)
                        await asyncio.to_thread(_unlink, cache_file)
                    except Exception:
                        pass
            
            if has_data:
                await self._persist_deleted_image_attachments(
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    attachment_records,
                )
        image_attachments = self._image_attachments(attachment_records)
        content_value = (message.content or "").strip()
        message_text = content_value or (None if attachment_records else "*No content*")

        embed = self._build_sapphire_log_embed(
            title="Message deleted",
            color=Colors.ERROR,
            details_lines=details_lines,
            message_text=message_text,
            thumbnail_url=message.author.display_avatar.url,
        )

        if attachment_records:
            embed.add_field(
                name=f"Attachments ({len(attachment_records)})",
                value=self._format_attachment_names(attachment_records, len(attachment_records)),
                inline=False,
            )

        view = DeletedMessageImageView(self.bot, image_attachments) if image_attachments else None
        if view is not None:
            embed.timestamp = None
            embed.set_footer(text=None)

        # Single-message deletes do not include transcript downloads. Image
        # attachments are exposed through an ephemeral button instead.
        await self.safe_send_log(channel, embed, use_v2=False, view=view, mirror_to_audit=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """
        Fallback delete logger for uncached messages.

        `on_message_delete` only fires for cached messages. This raw event ensures
        we still emit logs when Discord does not have a cached Message object.
        """
        # Cached deletes are already handled by on_message_delete.
        if payload.cached_message is not None:
            return

        guild_id = payload.guild_id
        if guild_id is None:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel_id = payload.channel_id
        if self._is_message_delete_suppressed(channel_id):
            return

        log_channel = await self.get_log_channel(guild, "message", allow_audit_fallback=False)
        if not log_channel:
            return

        source_channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)

        message_id = payload.message_id
        snapshot = self._pop_message_snapshot(
            message_id,
            guild_id=guild.id,
            channel_id=channel_id,
        )
        message_link = (
            f"https://discord.com/channels/"
            f"{guild.id}/{channel_id}/{message_id}"
        )

        if snapshot:
            author_id = snapshot.get("author_id")
            if author_id == self.bot.user.id:
                return
            author_display = snapshot.get("author_display") or snapshot.get("author_name") or "unknown"
            author_ref = f"<@{author_id}> (`@{author_display}`)" if author_id else f"`@{author_display}`"
            created_ts = snapshot.get("created_ts")
            author_avatar_url = snapshot.get("author_avatar_url")
            content_text = str(snapshot.get("content", "") or "").strip()
            attachment_count = int(snapshot.get("attachment_count", 0) or 0)
            attachments = list(snapshot.get("attachments") or [])
            message_text = content_text or (None if attachments else "*No content*")

            delete_entry = await self._find_recent_message_delete_entry(
                guild,
                channel_id=channel_id,
                author_id=author_id if isinstance(author_id, int) else None,
            )
            deleter = getattr(delete_entry, "user", None)
            if deleter is None and isinstance(author_id, int):
                deleter = guild.get_member(author_id) or self.bot.get_user(author_id)
            delete_reason = getattr(delete_entry, "reason", None)

            details_lines = [
                f"**Source channel:** {self._format_channel_reference(source_channel, fallback_id=channel_id)}",
                f"**Message ID:** [{message_id}]({message_link})",
                f"**Message author:** {author_ref}",
                f"**Message created:** <t:{created_ts}:R>" if created_ts else "**Message created:** *Unknown*",
            ]
            if deleter is not None:
                details_lines.append(f"**Deleted by:** {self._format_user_reference(deleter)}")
            if delete_reason:
                details_lines.append(f"**Reason:** {self._shorten(delete_reason, 250)}")
            embed = self._build_sapphire_log_embed(
                title="Message deleted",
                color=Colors.ERROR,
                details_lines=details_lines,
                message_text=message_text,
                thumbnail_url=author_avatar_url,
            )

            if attachments:
                embed.add_field(
                    name=f"Attachments ({attachment_count})",
                    value=self._format_attachment_names(attachments, attachment_count),
                    inline=False,
                )
        else:
            details_lines = [
                f"**Source channel:** {self._format_channel_reference(source_channel, fallback_id=channel_id)}",
                f"**Message ID:** [{message_id}]({message_link})",
                "**Message author:** *Unknown (message not cached)*",
                "**Message created:** *Unknown*",
            ]
            embed = self._build_sapphire_log_embed(
                title="Message deleted",
                color=Colors.ERROR,
                details_lines=details_lines,
                message_text="*Content unavailable (message was not cached by the bot).*",
            )

        db = getattr(self.bot, "db", None)
        attachment_records = []
        if db:
            attachment_records = await db.get_deleted_message_attachments(
                guild_id=guild.id,
                message_id=message_id,
            )
            
        image_attachments = self._image_attachments(attachment_records)
        if not attachment_records and snapshot:
            # Fallback to metadata from snapshot if no DB records exist
            image_attachments = self._image_attachments(list(snapshot.get("attachments") or []))
        view = DeletedMessageImageView(self.bot, image_attachments) if image_attachments else None
        if view is not None:
            embed.timestamp = None
            embed.set_footer(text=None)
        await self.safe_send_log(log_channel, embed, use_v2=False, view=view, mirror_to_audit=False)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Log edited messages"""
        # Ignore DMs, bots, and non-content changes
        if not before.guild or before.author.bot:
            return
        if before.content == after.content:
            return

        # Ignore if both empty
        if not before.content and not after.content:
            return

        channel = await self.get_log_channel(before.guild, 'message')
        if not channel:
            return

        embed = discord.Embed(
            title="Message edited",
            color=Colors.WARNING,
            timestamp=datetime.now(timezone.utc),
        )

        channel_name = getattr(before.channel, "name", "unknown")
        created_ts = int(before.created_at.timestamp())
        details_lines = [
            f"**Channel:** {before.channel.mention} (`#{channel_name}`)",
            f"**Message ID:** `{before.id}`",
            f"**Message author:** {before.author.mention} (`@{before.author.name}`)",
            f"**Message created:** <t:{created_ts}:R>",
            f"**Jump:** [Open message]({after.jump_url})",
        ]
        embed.description = "> " + "\n> ".join(details_lines)

        before_content = (before.content or "").strip() or "*Empty*"
        if len(before_content) > 1024:
            before_content = before_content[:1021].rstrip() + "..."

        after_content = (after.content or "").strip() or "*Empty*"
        if len(after_content) > 1024:
            after_content = after_content[:1021].rstrip() + "..."

        embed.add_field(name="Before", value=before_content, inline=True)
        embed.add_field(name="After", value=after_content, inline=True)

        footer_name = before.author.global_name or before.author.name
        embed.set_thumbnail(url=before.author.display_avatar.url)
        embed.set_footer(text=f"@{footer_name}", icon_url=before.author.display_avatar.url)

        await self.safe_send_log(channel, embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        """Log bulk message deletions"""
        if not messages:
            return

        if self._is_bulk_delete_suppressed(messages[0].channel.id):
            return

        # Get guild from first message
        guild = messages[0].guild
        if not guild:
            return

        channel = await self.get_log_channel(guild, 'message', allow_audit_fallback=False)
        if not channel:
            return

        deleted_count = len(messages)
        source_channel = messages[0].channel
        details_lines = [
            f"**Source channel:** {self._format_channel_reference(source_channel)}",
        ]
        preview_lines: list[str] = []
        for msg in reversed(messages):
            raw = (msg.content or "").strip()
            if not raw:
                if msg.attachments:
                    raw = f"[{len(msg.attachments)} attachment(s)]"
                elif msg.embeds:
                    raw = "[embed]"
                else:
                    continue
            raw = " ".join(raw.split())
            if len(raw) > 60:
                raw = raw[:57].rstrip() + "..."
            author_name = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "unknown")
            preview_lines.append(f"`{author_name}`: {raw}")
            if len(preview_lines) >= 3:
                break
        preview_text = "\n".join(preview_lines) if preview_lines else "*No text content available*"

        actor = None
        action_reason = None
        now = datetime.now(timezone.utc)
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.message_bulk_delete):
                age = (now - entry.created_at).total_seconds()
                if age > self._audit_search_window_seconds:
                    continue
                entry_channel = getattr(getattr(entry, "extra", None), "channel", None)
                if entry_channel and getattr(entry_channel, "id", None) != source_channel.id:
                    continue
                actor = getattr(entry, "user", None)
                action_reason = getattr(entry, "reason", None)
                break
        except discord.Forbidden:
            actor = None
        except Exception as e:
            logger.error(f"Failed to query bulk delete audit logs in {guild.name}: {e}")

        if actor:
            details_lines.append(f"**Moderator:** {self._format_user_reference(actor)}")
        if action_reason:
            details_lines.append(f"**Reason:** {self._shorten(action_reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title=f"{deleted_count} messages deleted",
            color=Colors.ERROR,
            details_lines=details_lines,
            message_text=preview_text,
            footer_user=actor,
        )
        embed.timestamp = None

        transcript_bytes = None
        transcript_name = None
        try:
            transcript_file = generate_html_transcript(
                guild,
                messages[0].channel,
                [],
                purged_messages=messages,
            )
            transcript_bytes = transcript_file.getvalue()
            transcript_name = f"purge-transcript-{guild.id}-{int(datetime.now(timezone.utc).timestamp())}.html"
        except Exception:
            transcript_bytes = None
            transcript_name = None

        view = None
        if transcript_bytes and transcript_name:
            view = EphemeralTranscriptView(io.BytesIO(transcript_bytes), filename=transcript_name)

        await self.safe_send_log(channel, embed, view=view, mirror_to_audit=False)

        mod_channel = await self.get_log_channel(guild, "mod", allow_audit_fallback=False)
        if mod_channel:
            mod_details = [
                f"**Source channel:** {self._format_channel_reference(source_channel)}",
                f"**Messages:** {deleted_count}",
            ]
            if actor:
                mod_details.append(f"**Moderator:** {self._format_user_reference(actor)}")
            if action_reason:
                mod_details.append(f"**Reason:** {self._shorten(action_reason, 250)}")

            mod_embed = self._build_sapphire_log_embed(
                title="Moderator Purge",
                color=Colors.ERROR,
                details_lines=mod_details,
                message_text=preview_text,
                footer_user=actor,
            )
            mod_view = None
            if transcript_bytes and transcript_name:
                mod_view = EphemeralTranscriptView(io.BytesIO(transcript_bytes), filename=transcript_name)
            await self.safe_send_log(mod_channel, mod_embed, view=mod_view, mirror_to_audit=False)

    # ==================== MEMBER LOGGING ====================
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Log member joins"""
        channel = await self.get_log_channel(member.guild, 'audit')
        if not channel:
            return

        account_age = (datetime.now(timezone.utc) - member.created_at).days
        details_lines = [
            f"**User:** {self._format_user_reference(member)}",
            f"**Account age:** {account_age} day(s)",
            f"**Created:** <t:{int(member.created_at.timestamp())}:R>",
            f"**Member count:** #{member.guild.member_count}",
        ]

        color = Colors.SUCCESS
        suspicious_tokens = ("discord", "nitro", "mod", "admin", "support")
        if account_age < 7:
            details_lines.append("**Warning:** New account")
            color = Colors.WARNING
        if any(token in member.name.lower() for token in suspicious_tokens):
            details_lines.append("**Warning:** Username contains common scam keywords")
            color = Colors.WARNING

        embed = self._build_sapphire_log_embed(
            title="Member joined",
            color=color,
            details_lines=details_lines,
            thumbnail_url=member.display_avatar.url,
        )
        await self.safe_send_log(channel, embed)
        return

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Log member leaves/kicks"""
        if member.joined_at:
            days_in_server = (datetime.now(timezone.utc) - member.joined_at).days
            tenure = f"{days_in_server} day(s)"
        else:
            tenure = "Unknown"

        details_lines = [
            f"**User:** {self._format_user_reference(member)}",
            f"**Time in server:** {tenure}",
            f"**Member count:** #{member.guild.member_count}",
        ]

        roles = [r.mention for r in member.roles[1:] if r.name != "@everyone"]
        if roles:
            roles_text = ", ".join(roles[:10])
            if len(roles) > 10:
                roles_text += f" +{len(roles) - 10} more"
            details_lines.append(f"**Roles:** {roles_text}")

        title = "Member left"
        footer_user = None
        was_kicked = False
        try:
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id != member.id:
                    continue
                if (datetime.now(timezone.utc) - entry.created_at).seconds < 5:
                    title = "Member kicked"
                    footer_user = getattr(entry, "user", None)
                    reason = getattr(entry, "reason", None)
                    if reason:
                        details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")
                    was_kicked = True
                    break
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Failed to check audit log for kick: {e}")

        # Kicks are mod actions → mod logs; voluntary leaves → audit logs
        log_type = "mod" if was_kicked else "audit"
        channel = await self.get_log_channel(member.guild, log_type)
        if not channel:
            return

        embed = self._build_sapphire_log_embed(
            title=title,
            color=Colors.ERROR,
            details_lines=details_lines,
            thumbnail_url=member.display_avatar.url,
            footer_user=footer_user,
        )
        await self.safe_send_log(channel, embed)
        return

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Log member updates (nickname, roles, etc.)"""
        audit_channel = await self.get_log_channel(before.guild, 'audit')

        if before.nick != after.nick:
            if audit_channel:
                entry = await self._find_recent_audit_entry(
                    before.guild,
                    action=discord.AuditLogAction.member_update,
                    target_id=after.id,
                )
                moderator = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
                details_lines = [
                    f"**User:** {self._format_user_reference(after)}",
                    f"**Before:** {before.nick or '*None*'}",
                    f"**After:** {after.nick or '*None*'}",
                ]
                if reason:
                    details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

                embed = self._build_sapphire_log_embed(
                    title="Nickname changed",
                    color=Colors.INFO,
                    details_lines=details_lines,
                    thumbnail_url=after.display_avatar.url,
                    footer_user=moderator,
                )
                await self.safe_send_log(audit_channel, embed)

        if before.roles != after.roles:
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]

            if (added or removed) and audit_channel:
                added_text = "*None*"
                if added:
                    added_text = ", ".join([r.mention for r in added[:10]])
                    if len(added) > 10:
                        added_text += f" +{len(added) - 10} more"

                removed_text = "*None*"
                if removed:
                    removed_text = ", ".join([r.mention for r in removed[:10]])
                    if len(removed) > 10:
                        removed_text += f" +{len(removed) - 10} more"

                entry = await self._find_recent_audit_entry(
                    before.guild,
                    action=discord.AuditLogAction.member_role_update,
                    target_id=after.id,
                )
                moderator = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)

                details_lines = [
                    f"**User:** {self._format_user_reference(after)}",
                    f"**Added:** {added_text}",
                    f"**Removed:** {removed_text}",
                ]
                if reason:
                    details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

                embed = self._build_sapphire_log_embed(
                    title="Roles updated",
                    color=Colors.INFO,
                    details_lines=details_lines,
                    thumbnail_url=after.display_avatar.url,
                    footer_user=moderator,
                )
                await self.safe_send_log(audit_channel, embed)
        
        # ===== TIMEOUT CHANGES =====
        # Timeouts are moderation actions → send to mod logs, not audit
        if before.timed_out_until != after.timed_out_until:
            if not self._is_timeout_change_suppressed(before.guild.id, after.id):
                mod_channel = await self.get_log_channel(before.guild, 'mod')
                if mod_channel:
                    if after.timed_out_until and after.timed_out_until > datetime.now(timezone.utc):
                        # User was timed out
                        entry = await self._find_recent_audit_entry(
                            before.guild,
                            action=discord.AuditLogAction.member_update,
                            target_id=after.id,
                        )
                        moderator = getattr(entry, "user", None)
                        reason = getattr(entry, "reason", None)

                        until_ts = int(after.timed_out_until.timestamp())
                        details_lines = [
                            f"**User:** {self._format_user_reference(after)}",
                            f"**Until:** <t:{until_ts}:F>",
                            f"**Duration:** <t:{until_ts}:R>",
                        ]
                        if reason:
                            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

                        embed = self._build_sapphire_log_embed(
                            title="User timed out",
                            color=Colors.ERROR,
                            details_lines=details_lines,
                            thumbnail_url=after.display_avatar.url,
                            footer_user=moderator,
                        )
                        
                        await self.safe_send_log(mod_channel, embed)
                    elif before.timed_out_until and not after.timed_out_until:
                        # Timeout removed
                        entry = await self._find_recent_audit_entry(
                            before.guild,
                            action=discord.AuditLogAction.member_update,
                            target_id=after.id,
                        )
                        moderator = getattr(entry, "user", None)
                        reason = getattr(entry, "reason", None)

                        details_lines = [
                            f"**User:** {self._format_user_reference(after)}",
                        ]
                        if reason:
                            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

                        embed = self._build_sapphire_log_embed(
                            title="User timeout removed",
                            color=Colors.SUCCESS,
                            details_lines=details_lines,
                            thumbnail_url=after.display_avatar.url,
                            footer_user=moderator,
                        )
                        
                        await self.safe_send_log(mod_channel, embed)

    # ==================== VOICE LOGGING ====================

    def _build_voice_log_embed(
        self,
        *,
        title: str,
        color: int,
        member: discord.Member,
        channel_line: str,
        detail_line: str,
    ) -> discord.Embed:
        description = (
            f"**User:** {member.mention}\n"
            f"**Channel:** {channel_line}\n"
            f"**Details:** {detail_line}"
        )
        return discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, 
        member: discord.Member, 
        before: discord.VoiceState, 
        after: discord.VoiceState
    ):
        """Log voice channel activity"""
        channel = await self.get_log_channel(member.guild, 'voice')
        if not channel:
            return
        
        embed = None
        
        # ===== JOINED VOICE =====
        if before.channel is None and after.channel is not None:
            embed = self._build_voice_log_embed(
                title="Joined Voice Channel",
                color=Colors.SUCCESS,
                member=member,
                channel_line=after.channel.mention,
                detail_line=f"{len(after.channel.members)} member(s)",
            )
        
        # ===== LEFT VOICE =====
        elif before.channel is not None and after.channel is None:
            embed = self._build_voice_log_embed(
                title="Left Voice Channel",
                color=Colors.ERROR,
                member=member,
                channel_line=before.channel.mention,
                detail_line=f"{len(before.channel.members)} member(s) remaining",
            )
        
        # ===== SWITCHED VOICE =====
        elif before.channel != after.channel and before.channel and after.channel:
            embed = self._build_voice_log_embed(
                title="Switched Voice Channel",
                color=Colors.INFO,
                member=member,
                channel_line=f"{before.channel.mention} -> {after.channel.mention}",
                detail_line=f"{len(after.channel.members)} member(s) in destination",
            )
        
        # ===== VOICE MUTE/UNMUTE =====
        elif before.self_mute != after.self_mute:
            action = "Muted" if after.self_mute else "Unmuted"
            voice_channel = after.channel or before.channel
            embed = self._build_voice_log_embed(
                title=f"Self {action}",
                color=Colors.INFO if after.self_mute else Colors.SUCCESS,
                member=member,
                channel_line=voice_channel.mention if voice_channel else "*None*",
                detail_line=f"Self mute {'enabled' if after.self_mute else 'disabled'}",
            )
        
        # ===== VOICE DEAFEN/UNDEAFEN =====
        elif before.self_deaf != after.self_deaf:
            action = "Deafened" if after.self_deaf else "Undeafened"
            voice_channel = after.channel or before.channel
            embed = self._build_voice_log_embed(
                title=f"Self {action}",
                color=Colors.INFO if after.self_deaf else Colors.SUCCESS,
                member=member,
                channel_line=voice_channel.mention if voice_channel else "*None*",
                detail_line=f"Self deafen {'enabled' if after.self_deaf else 'disabled'}",
            )

        # ===== STREAMING START/STOP =====
        elif before.self_stream != after.self_stream:
            action = "Started Streaming" if after.self_stream else "Stopped Streaming"
            voice_channel = after.channel or before.channel
            embed = self._build_voice_log_embed(
                title=f"{action}",
                color=Colors.INFO if after.self_stream else Colors.ERROR,
                member=member,
                channel_line=voice_channel.mention if voice_channel else "*None*",
                detail_line=f"Streaming {'started' if after.self_stream else 'stopped'}",
            )

        # ===== VIDEO START/STOP =====
        elif before.self_video != after.self_video:
            action = "Started Video" if after.self_video else "Stopped Video"
            voice_channel = after.channel or before.channel
            embed = self._build_voice_log_embed(
                title=f"{action}",
                color=Colors.INFO if after.self_video else Colors.ERROR,
                member=member,
                channel_line=voice_channel.mention if voice_channel else "*None*",
                detail_line=f"Video {'started' if after.self_video else 'stopped'}",
            )

        if embed:
            await self.safe_send_log(channel, embed)

    # ==================== BAN/UNBAN LOGGING ====================
    
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        """Log bans — moderation action, goes to mod logs"""
        channel = await self.get_log_channel(guild, 'mod')
        if not channel:
            return

        moderator = None
        ban_reason = None
        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id == user.id:
                    moderator = getattr(entry, "user", None)
                    ban_reason = getattr(entry, "reason", None)
                    break
        except discord.Forbidden:
            logger.warning(f"Missing audit log permissions in {guild.name}")
        except Exception as e:
            logger.error(f"Failed to check audit log for ban: {e}")

        details_lines = [
            f"**User:** {self._format_user_reference(user)}",
            f"**Bot:** {self._yn(getattr(user, 'bot', False))}",
            f"**Reason:** {self._shorten(ban_reason, 250)}",
        ]
        if getattr(user, "created_at", None):
            details_lines.insert(1, f"**Account created:** <t:{int(user.created_at.timestamp())}:R>")

        embed = self._build_sapphire_log_embed(
            title="Member banned",
            color=Colors.DARK_RED,
            details_lines=details_lines,
            thumbnail_url=user.display_avatar.url,
            footer_user=moderator,
        )
        await self.safe_send_log(channel, embed)
        return

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        """Log unbans — moderation action, goes to mod logs"""
        channel = await self.get_log_channel(guild, 'mod')
        if not channel:
            return

        moderator = None
        unban_reason = None
        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id == user.id:
                    moderator = getattr(entry, "user", None)
                    unban_reason = getattr(entry, "reason", None)
                    break
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Failed to check audit log for unban: {e}")

        details_lines = [
            f"**User:** {self._format_user_reference(user)}",
            f"**Bot:** {self._yn(getattr(user, 'bot', False))}",
            f"**Reason:** {self._shorten(unban_reason, 250)}",
        ]
        if getattr(user, "created_at", None):
            details_lines.insert(1, f"**Account created:** <t:{int(user.created_at.timestamp())}:R>")

        embed = self._build_sapphire_log_embed(
            title="Member unbanned",
            color=Colors.SUCCESS,
            details_lines=details_lines,
            thumbnail_url=user.display_avatar.url,
            footer_user=moderator,
        )
        await self.safe_send_log(channel, embed)
        return

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        """Log webhook creation events."""
        log_channel = await self.get_log_channel(channel.guild, "audit")
        if not log_channel:
            return

        now = datetime.now(timezone.utc)
        entry = None
        try:
            async for candidate in channel.guild.audit_logs(limit=8, action=discord.AuditLogAction.webhook_create):
                age = (now - candidate.created_at).total_seconds()
                if age > self._audit_search_window_seconds:
                    continue

                webhook = getattr(candidate, "target", None)
                webhook_channel = getattr(webhook, "channel", None)
                webhook_channel_id = getattr(webhook_channel, "id", None) or getattr(webhook, "channel_id", None)
                if webhook_channel_id is not None and webhook_channel_id != channel.id:
                    continue

                entry = candidate
                break
        except discord.Forbidden:
            return
        except Exception as e:
            logger.error(f"Failed to query webhook audit logs in {channel.guild.name}: {e}")
            return

        if entry is None:
            return

        entry_id = getattr(entry, "id", None)
        if entry_id is None or not self._remember_webhook_entry(entry_id):
            return

        webhook = getattr(entry, "target", None)
        webhook_name = getattr(webhook, "name", "Unknown")
        webhook_id = getattr(webhook, "id", "Unknown")
        webhook_type = str(getattr(getattr(webhook, "type", None), "name", "incoming")).upper()

        details_lines = [
            f"**Webhook:** {webhook_name} (`{webhook_id}`)",
            f"**Channel:** {self._format_channel_reference(channel)}",
            f"**Type:** {webhook_type}",
        ]
        actor = getattr(entry, "user", None)
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")
        reason = getattr(entry, "reason", None)
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title="Webhook created",
            color=Colors.SUCCESS,
            details_lines=details_lines,
            thumbnail_url=channel.guild.icon.url if channel.guild.icon else None,
        )

        await self.safe_send_log(log_channel, embed)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        """
        Generic audit stream for actions that do not already have dedicated log paths.
        """
        guild = getattr(entry, "guild", None)
        if guild is None:
            return

        action = entry.action
        if self._should_skip_generic_audit_action(action):
            return

        entry_id = getattr(entry, "id", None)
        if not isinstance(entry_id, int):
            return
        if not self._remember_generic_audit_entry(entry_id):
            return

        channel = await self.get_log_channel(guild, "audit")
        if not channel:
            return

        details_lines = []
        details_lines.extend(self._audit_target_lines(entry))
        details_lines.extend(self._audit_extra_lines(entry))
        details_lines.extend(self._audit_change_lines(entry))

        actor = getattr(entry, "user", None)
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")

        reason = getattr(entry, "reason", None)
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title=self._audit_action_title(entry),
            color=self._audit_action_color(action),
            details_lines=details_lines or ["**Details:** *No additional fields*"],
            thumbnail_url=self._audit_thumbnail_url(entry),
        )

        await self.safe_send_log(channel, embed)
    
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Log channel creation"""
        log_channel = await self.get_log_channel(channel.guild, 'audit')
        if not log_channel:
            return

        entry = await self._find_recent_audit_entry(
            channel.guild,
            action=discord.AuditLogAction.channel_create,
            target_id=channel.id,
        )
        actor = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        created_at = getattr(channel, "created_at", None)
        details_lines = [
            f"**Channel:** {self._format_channel_reference(channel)}",
            f"**Type:** {str(channel.type).title()}",
            f"**ID:** `{channel.id}`",
            f"**Created:** {f'<t:{int(created_at.timestamp())}:R>' if created_at else '*N/A*'}",
        ]
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")
        details_lines.extend(self._channel_detail_lines(channel))
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title="Channel created",
            color=Colors.SUCCESS,
            details_lines=details_lines,
            thumbnail_url=channel.guild.icon.url if channel.guild.icon else None,
        )
        await self.safe_send_log(log_channel, embed)
        return

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Log channel deletion"""
        log_channel = await self.get_log_channel(channel.guild, 'audit')
        if not log_channel:
            return

        entry = await self._find_recent_audit_entry(
            channel.guild,
            action=discord.AuditLogAction.channel_delete,
            target_id=channel.id,
        )
        actor = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        created_at = getattr(channel, "created_at", None)
        details_lines = [
            f"**Channel:** {self._format_channel_reference(channel)}",
            f"**Type:** {str(channel.type).title()}",
            f"**ID:** `{channel.id}`",
            f"**Created:** {f'<t:{int(created_at.timestamp())}:R>' if created_at else '*N/A*'}",
        ]
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")
        details_lines.extend(self._channel_detail_lines(channel))
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title="Channel deleted",
            color=Colors.ERROR,
            details_lines=details_lines,
            thumbnail_url=channel.guild.icon.url if channel.guild.icon else None,
        )
        await self.safe_send_log(log_channel, embed)
        return

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        """Log role creation"""
        channel = await self.get_log_channel(role.guild, 'audit')
        if not channel:
            return

        entry = await self._find_recent_audit_entry(
            role.guild,
            action=discord.AuditLogAction.role_create,
            target_id=role.id,
        )
        actor = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        created_at = getattr(role, "created_at", None)
        details_lines = [
            f"**Role:** {self._format_role_reference(role)}",
            f"**ID:** `{role.id}`",
            f"**Created:** {f'<t:{int(created_at.timestamp())}:R>' if created_at else '*N/A*'}",
        ]
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")
        details_lines.extend(self._role_detail_lines(role, include_members=True))
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title="Role created",
            color=role.color if role.color.value != 0 else Colors.SUCCESS,
            details_lines=details_lines,
            thumbnail_url=role.guild.icon.url if role.guild.icon else None,
        )
        await self.safe_send_log(channel, embed)
        return

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        """Log role deletion"""
        channel = await self.get_log_channel(role.guild, 'audit')
        if not channel:
            return

        entry = await self._find_recent_audit_entry(
            role.guild,
            action=discord.AuditLogAction.role_delete,
            target_id=role.id,
        )
        actor = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        created_at = getattr(role, "created_at", None)
        details_lines = [
            f"**Role:** @{role.name}",
            f"**ID:** `{role.id}`",
            f"**Members at deletion:** {len(role.members)}",
            f"**Created:** {f'<t:{int(created_at.timestamp())}:R>' if created_at else '*N/A*'}",
        ]
        if actor:
            details_lines.append(f"**Actor:** {self._format_user_reference(actor)}")
        details_lines.extend(self._role_detail_lines(role, include_members=False))
        if reason:
            details_lines.append(f"**Reason:** {self._shorten(reason, 250)}")

        embed = self._build_sapphire_log_embed(
            title="Role deleted",
            color=Colors.ERROR,
            details_lines=details_lines,
            thumbnail_url=role.guild.icon.url if role.guild.icon else None,
        )
        await self.safe_send_log(channel, embed)
        return

    # ==================== CONFIGURATION COMMAND ====================
    
    @log_group.command(name="set", description="⚙️ Configure logging channels")
    @app_commands.describe(
        log_type="Type of log to configure",
        channel="Channel to send logs to (leave empty to disable)"
    )
    @is_admin()
    async def log_set(
        self,
        interaction: discord.Interaction,
        log_type: Literal['mod', 'audit', 'message', 'voice', 'automod', 'report', 'ticket'],
        channel: Optional[discord.TextChannel] = None
    ):
        """Configure logging channels for different event types"""
        try:
            settings = await self.bot.db.get_settings(interaction.guild_id)
            
            if channel:
                # Set channel
                for key in self._log_channel_setting_keys(log_type):
                    settings[key] = channel.id
                await self.bot.db.update_settings(interaction.guild_id, settings)
                
                # Update cache
                await self._channel_cache.set(interaction.guild_id, log_type, channel.id)
                
                embed = ModEmbed.success(
                    "Logging Configured",
                    f"**{log_type.title()}** logs will now be sent to {channel.mention}"
                )
            else:
                # Disable logging for this type
                removed = self._clear_log_channel_settings(settings, log_type)
                if removed:
                    await self.bot.db.update_settings(interaction.guild_id, settings)
                
                # Clear cache
                await self._channel_cache.invalidate(interaction.guild_id, log_type)
                
                embed = ModEmbed.success(
                    "Logging Disabled",
                    f"**{log_type.title()}** logging has been disabled"
                )
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to configure logging: {e}")
            await interaction.response.send_message(
                embed=ModEmbed.error("Configuration Failed", f"An error occurred: {str(e)}"),
                ephemeral=True
            )

    @log_group.command(name="config", description="📋 View current logging configuration")
    @is_admin()
    async def log_config(self, interaction: discord.Interaction):
        """View all configured logging channels"""
        try:
            settings = await self.bot.db.get_settings(interaction.guild_id)
            
            log_types = ['mod', 'audit', 'message', 'voice', 'automod', 'report', 'ticket']
            rows: list[tuple[str, object]] = []
            for log_type in log_types:
                channel_id = self._resolve_log_channel_id(settings, log_type)
                if channel_id:
                    channel = interaction.guild.get_channel(channel_id)
                    value = channel.mention if channel else "❌ Channel not found"
                else:
                    value = "*Not configured*"

                rows.append((f"{log_type.title()} Logs", value))

            embed = discord.Embed(
                title="📋 Logging Configuration",
                description=compact_kv_lines(rows),
                color=Colors.INFO,
                timestamp=datetime.now(timezone.utc)
            )
            mod_embed.timestamp = None

            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to get log config: {e}")
            await interaction.response.send_message(
                embed=ModEmbed.error("Failed", f"An error occurred: {str(e)}"),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Load the Logging cog"""
    await bot.add_cog(Logging(bot))


