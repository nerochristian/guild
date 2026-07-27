from __future__ import annotations

import importlib.util
import asyncio
import html
import io
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "groups.sqlite3"
COMPONENTS_V2_PATH = ROOT / "components_v2.py"
TRANSCRIPT_TEMPLATE_PATH = ROOT / "template.html" if (ROOT / "template.html").exists() else ROOT / "transcript_template.html"
ASSETS_DIR = ROOT / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"
BANNER_PATH = ASSETS_DIR / "banner.png"

load_dotenv(ROOT / ".env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_GUILD_ID = (os.getenv("COMMAND_GUILD_ID") or "").strip()
AUTO_SET_BOT_AVATAR = os.getenv("AUTO_SET_BOT_AVATAR", "true").lower() in {"1", "true", "yes", "on"}
GROUP_CATEGORY_ANCHOR_ID = int(os.getenv("GROUP_CATEGORY_ANCHOR_ID", "1388268039773884588"))

log = logging.getLogger("group_bot")


def load_components_v2() -> None:
    spec = importlib.util.spec_from_file_location("components_v2_universal", COMPONENTS_V2_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {COMPONENTS_V2_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


@dataclass(slots=True)
class GroupRecord:
    id: int
    guild_id: int
    name: str
    category_id: Optional[int]
    announcements_channel_id: Optional[int]
    general_channel_id: Optional[int]
    group_role_id: Optional[int]
    leader_role_id: Optional[int]
    leader_user_id: Optional[int]
    created_at: str
    status: str
    broken_reason: Optional[str]
    needs_new_leader: bool


class GroupStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category_id TEXT,
                announcements_channel_id TEXT,
                general_channel_id TEXT,
                group_role_id TEXT,
                leader_role_id TEXT,
                leader_user_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_groups_guild_name
                ON groups(guild_id, lower(name));

            CREATE UNIQUE INDEX IF NOT EXISTS ux_group_members_group_user
                ON group_members(group_id, user_id);
            """
        )
        self._ensure_column("groups", "status", "TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column("groups", "broken_reason", "TEXT")
        self._ensure_column("groups", "needs_new_leader", "INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, now_iso()),
        )
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def create_group(
        self,
        *,
        guild_id: int,
        name: str,
        category_id: int,
        announcements_channel_id: int,
        general_channel_id: int,
        group_role_id: int,
        leader_role_id: int,
        leader_user_id: int,
        member_user_ids: Iterable[int],
    ) -> GroupRecord:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO groups (
                    guild_id, name, category_id, announcements_channel_id, general_channel_id,
                    group_role_id, leader_role_id, leader_user_id, created_at, status,
                    broken_reason, needs_new_leader
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0)
                """,
                (
                    str(guild_id),
                    name,
                    str(category_id),
                    str(announcements_channel_id),
                    str(general_channel_id),
                    str(group_role_id),
                    str(leader_role_id),
                    str(leader_user_id),
                    now_iso(),
                ),
            )
            group_id = int(cur.lastrowid)
            for user_id in set(member_user_ids):
                self.conn.execute(
                    "INSERT OR IGNORE INTO group_members(group_id, user_id) VALUES (?, ?)",
                    (group_id, str(user_id)),
                )
        record = self.get_group(guild_id, name)
        if record is None:
            raise RuntimeError("Created group could not be loaded from database")
        return record

    def get_group(self, guild_id: int, name: str) -> Optional[GroupRecord]:
        row = self.conn.execute(
            "SELECT * FROM groups WHERE guild_id = ? AND lower(name) = lower(?)",
            (str(guild_id), name),
        ).fetchone()
        return self._record(row) if row else None

    def list_groups(self, guild_id: Optional[int] = None) -> list[GroupRecord]:
        if guild_id is None:
            rows = self.conn.execute("SELECT * FROM groups ORDER BY guild_id, name").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM groups WHERE guild_id = ? ORDER BY name",
                (str(guild_id),),
            ).fetchall()
        return [self._record(row) for row in rows]

    def member_ids(self, group_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ? ORDER BY id",
            (group_id,),
        ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def add_member(self, group_id: int, user_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO group_members(group_id, user_id) VALUES (?, ?)",
                (group_id, str(user_id)),
            )

    def remove_member(self, group_id: int, user_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, str(user_id)),
            )

    def update_discord_ids(
        self,
        group_id: int,
        *,
        category_id: int,
        announcements_channel_id: int,
        general_channel_id: int,
        group_role_id: int,
        leader_role_id: int,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE groups
                SET category_id = ?, announcements_channel_id = ?, general_channel_id = ?,
                    group_role_id = ?, leader_role_id = ?, status = 'active',
                    broken_reason = NULL
                WHERE id = ?
                """,
                (
                    str(category_id),
                    str(announcements_channel_id),
                    str(general_channel_id),
                    str(group_role_id),
                    str(leader_role_id),
                    group_id,
                ),
            )

    def set_leader(self, group_id: int, leader_user_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE groups SET leader_user_id = ?, needs_new_leader = 0 WHERE id = ?",
                (str(leader_user_id), group_id),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO group_members(group_id, user_id) VALUES (?, ?)",
                (group_id, str(leader_user_id)),
            )

    def mark_broken(self, group_id: int, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE groups SET status = 'broken', broken_reason = ? WHERE id = ?",
                (reason, group_id),
            )

    def mark_needs_leader(self, group_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE groups SET needs_new_leader = 1 WHERE id = ?",
                (group_id,),
            )

    def clear_broken_flags(self, group_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE groups SET status = 'active', broken_reason = NULL WHERE id = ?",
                (group_id,),
            )

    def cleanup(self, guild_id: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM groups WHERE guild_id = ? AND status IN ('broken', 'finished')",
                (str(guild_id),),
            )
        return int(cur.rowcount)

    def delete_group(self, group_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def close(self) -> None:
        self.conn.close()

    def _record(self, row: sqlite3.Row) -> GroupRecord:
        return GroupRecord(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            category_id=to_optional_int(row["category_id"]),
            announcements_channel_id=to_optional_int(row["announcements_channel_id"]),
            general_channel_id=to_optional_int(row["general_channel_id"]),
            group_role_id=to_optional_int(row["group_role_id"]),
            leader_role_id=to_optional_int(row["leader_role_id"]),
            leader_user_id=to_optional_int(row["leader_user_id"]),
            created_at=str(row["created_at"]),
            status=str(row["status"]),
            broken_reason=row["broken_reason"],
            needs_new_leader=bool(row["needs_new_leader"]),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def admin_only() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    return app_commands.check(predicate)


def group_embed(title: str, description: str, *, color: int = 0x2F80ED) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))


def discord_file(path: Path, filename: str) -> Optional[discord.File]:
    if not path.exists():
        return None
    return discord.File(path, filename=filename)


def html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_transcript_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%b %d, %Y (%H:%M:%S)")


def message_component_texts(component: object) -> list[str]:
    if hasattr(component, "to_component_dict"):
        data = component.to_component_dict()  # type: ignore[attr-defined]
    elif isinstance(component, dict):
        data = component
    else:
        data = {}

    texts: list[str] = []
    for key in ("content", "text", "label"):
        value = data.get(key)
        if value:
            texts.append(str(value))
    for child in data.get("components", []) or []:
        texts.extend(message_component_texts(child))
    return texts


def render_message_html(message: discord.Message) -> str:
    body_parts: list[str] = []
    if message.clean_content:
        body_parts.append(html_escape(message.clean_content))

    for embed in message.embeds:
        embed_lines = [value for value in (embed.title, embed.description) if value]
        embed_lines.extend(f"{field.name}: {field.value}" for field in embed.fields)
        if embed_lines:
            body_parts.append(html_escape("\n".join(embed_lines)))

    component_lines: list[str] = []
    for component in message.components:
        component_lines.extend(message_component_texts(component))
    if component_lines:
        body_parts.append(html_escape("\n".join(component_lines)))

    for attachment in message.attachments:
        body_parts.append(f'<a href="{html_escape(attachment.url)}">{html_escape(attachment.filename)}</a>')

    body = "<br>".join(part.replace("\n", "<br>") for part in body_parts) or "<span class=\"chatlog__markdown\">[no text]</span>"
    created = message.created_at.astimezone(timezone.utc)
    timestamp = created.strftime("%A, %B %d, %Y %H:%M:%S")
    short_timestamp = created.strftime("%H:%M")
    avatar_url = html_escape(message.author.display_avatar.url)
    author_name = html_escape(message.author.display_name)

    return f"""
        <div class="chatlog__message-container" id="chatlog__message-container-{message.id}">
            <div class="chatlog__message">
                <div class="chatlog__message-aside">
                    <img class="chatlog__avatar" src="{avatar_url}" alt="Avatar">
                </div>
                <div class="chatlog__message-primary">
                    <div>
                        <span class="chatlog__author-name" data-user-id="{message.author.id}">{author_name}</span>
                        <span class="chatlog__timestamp" data-timestamp="{timestamp} UTC">{short_timestamp}</span>
                    </div>
                    <div class="chatlog__content">
                        <div class="chatlog__markdown chatlog__markdown-preserve">{body}</div>
                    </div>
                </div>
            </div>
        </div>
    """


async def build_transcript_file(channel: discord.TextChannel) -> tuple[discord.File, int, int]:
    template = TRANSCRIPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    messages = [message async for message in channel.history(limit=None, oldest_first=True)]
    participants = {message.author.id for message in messages}
    generated_at = datetime.now(timezone.utc)
    guild_icon = channel.guild.icon.url if channel.guild.icon else ""
    created_at = format_transcript_time(channel.created_at)
    message_html = "\n".join(render_message_html(message) for message in messages)
    replacements = {
        "{guild_name}": html_escape(channel.guild.name),
        "{guild_id}": str(channel.guild.id),
        "{guild_icon}": html_escape(guild_icon),
        "{channel_name}": html_escape(channel.name),
        "{channel_id}": str(channel.id),
        "{created_at}": html_escape(created_at),
        "{message_count}": str(len(messages)),
        "{participant_count}": str(len(participants)),
        "{generated_at}": html_escape(generated_at.strftime("%B %d, %Y at %H:%M:%S")),
        "{messages}": message_html,
        "{user_popouts}": "",
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    fp = io.BytesIO(template.encode("utf-8"))
    filename = f"setup-transcript-{channel.id}.html"
    return discord.File(fp, filename=filename), len(messages), len(participants)


async def dm_setup_transcript(channel: discord.TextChannel, leader: discord.Member) -> None:
    if not TRANSCRIPT_TEMPLATE_PATH.exists():
        await channel.send(embed=group_embed("Transcript Missing", "`transcript_template.html` was not found.", color=0xE67E22))
        return

    try:
        transcript, message_count, participant_count = await build_transcript_file(channel)
        await leader.send(
            content=(
                f"Here is your setup transcript for `#{channel.name}` "
                f"with {message_count} messages from {participant_count} participants."
            ),
            file=transcript,
        )
    except discord.Forbidden:
        await channel.send(embed=group_embed("Transcript DM Failed", f"{leader.mention}, I could not DM you the transcript.", color=0xE67E22))
    except discord.HTTPException:
        log.exception("Could not send setup transcript for channel %s", channel.id)
        await channel.send(embed=group_embed("Transcript Failed", "I could not send the transcript file.", color=0xE67E22))


async def delete_setup_channel_later(channel: discord.TextChannel, delay_seconds: int = 600) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await channel.delete(reason="Setup complete; deleting setup channel after transcript delivery")
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.exception("Missing permission to delete setup channel %s", channel.id)
    except discord.HTTPException:
        log.exception("Discord failed to delete setup channel %s", channel.id)


async def finish_setup_channel(channel: discord.TextChannel, leader: discord.Member) -> None:
    await dm_setup_transcript(channel, leader)
    await channel.send(embed=group_embed("Setup Channel Cleanup", "Transcript sent. This setup channel will be deleted in 10 minutes."))
    asyncio.create_task(delete_setup_channel_later(channel))


async def send_admin_notice(guild: discord.Guild, embed: discord.Embed) -> None:
    channels: list[discord.TextChannel] = []
    if guild.system_channel:
        channels.append(guild.system_channel)
    channels.extend(channel for channel in guild.text_channels if channel not in channels)

    me = guild.me
    for channel in channels:
        permissions = channel.permissions_for(me) if me else None
        if permissions and permissions.send_messages:
            await channel.send(embed=embed)
            return
    log.warning("Could not send admin notice in guild %s", guild.id)


async def fetch_member_or_none(guild: discord.Guild, user_id: Optional[int]) -> Optional[discord.Member]:
    if user_id is None:
        return None
    cached = guild.get_member(user_id)
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None


async def delete_if_exists(target: object, reason: str) -> bool:
    if target is None:
        return False
    try:
        await target.delete(reason=reason)  # type: ignore[attr-defined]
        return True
    except discord.NotFound:
        return False
    except discord.Forbidden:
        log.exception("Missing permission to delete %r", target)
        return False
    except discord.HTTPException:
        log.exception("Discord failed to delete %r", target)
        return False


async def delete_group_discord_objects(guild: discord.Guild, group: GroupRecord) -> list[str]:
    reason = f"Group cleanup after saved category was deleted: {group.name}"
    deleted: list[str] = []

    general = guild.get_channel(group.general_channel_id) if group.general_channel_id else None
    announcements = guild.get_channel(group.announcements_channel_id) if group.announcements_channel_id else None
    if await delete_if_exists(general, reason):
        deleted.append("general channel")
    if await delete_if_exists(announcements, reason):
        deleted.append("announcements channel")

    group_role = guild.get_role(group.group_role_id) if group.group_role_id else None
    leader_role = guild.get_role(group.leader_role_id) if group.leader_role_id else None
    if await delete_if_exists(group_role, reason):
        deleted.append("group role")
    if await delete_if_exists(leader_role, reason):
        deleted.append("leader role")

    return deleted


async def move_category_to_position(category: discord.CategoryChannel, target_position: int) -> bool:
    if category.position == target_position:
        return False
    try:
        await category.edit(position=target_position, reason="Keep group category under configured anchor")
        return True
    except discord.Forbidden:
        log.exception("Missing permission to move category %s", category.id)
    except discord.HTTPException:
        log.exception("Discord failed to move category %s", category.id)
    return False


async def move_category_under_anchor(category: discord.CategoryChannel) -> bool:
    anchor = category.guild.get_channel(GROUP_CATEGORY_ANCHOR_ID)
    if not isinstance(anchor, discord.CategoryChannel) or anchor.id == category.id:
        return False

    return await move_category_to_position(category, anchor.position + 1)


async def enforce_group_channel_locations(guild: discord.Guild, store: GroupStore) -> None:
    groups = [group for group in store.list_groups(guild.id) if group.status == "active"]
    categories: list[discord.CategoryChannel] = []

    for group in groups:
        category = guild.get_channel(group.category_id) if group.category_id else None
        if isinstance(category, discord.CategoryChannel):
            categories.append(category)

            for channel_id in (group.announcements_channel_id, group.general_channel_id):
                channel = guild.get_channel(channel_id) if channel_id else None
                if isinstance(channel, discord.TextChannel) and channel.category_id != category.id:
                    try:
                        await channel.edit(category=category, reason="Keep group channel in saved group category")
                    except discord.Forbidden:
                        log.exception("Missing permission to move channel %s into category %s", channel.id, category.id)
                    except discord.HTTPException:
                        log.exception("Discord failed to move channel %s into category %s", channel.id, category.id)

    anchor = guild.get_channel(GROUP_CATEGORY_ANCHOR_ID)
    if not isinstance(anchor, discord.CategoryChannel):
        return

    for offset, category in enumerate(
        sorted({category.id: category for category in categories}.values(), key=lambda item: item.name.lower()),
        start=1,
    ):
        await move_category_to_position(category, anchor.position + offset)


async def validate_group(bot: commands.Bot, store: GroupStore, group: GroupRecord, *, notify: bool) -> None:
    guild = bot.get_guild(group.guild_id)
    if guild is None:
        return

    missing: list[str] = []
    if group.category_id and guild.get_channel(group.category_id) is None:
        deleted = await delete_group_discord_objects(guild, group)
        store.delete_group(group.id)
        if notify:
            detail = ", ".join(deleted) if deleted else "no remaining saved Discord objects were found"
            await send_admin_notice(
                guild,
                group_embed(
                    "Group Deleted",
                    (
                        f"`{group.name}` was removed because its saved category was deleted. "
                        f"I also cleaned up: {detail}. The database record was removed."
                    ),
                    color=0xE67E22,
                ),
            )
        return
    if group.announcements_channel_id and guild.get_channel(group.announcements_channel_id) is None:
        missing.append("announcements channel")
    if group.general_channel_id and guild.get_channel(group.general_channel_id) is None:
        missing.append("general channel")
    if group.group_role_id and guild.get_role(group.group_role_id) is None:
        missing.append("group role")
    if group.leader_role_id and guild.get_role(group.leader_role_id) is None:
        missing.append("leader role")

    for user_id in store.member_ids(group.id):
        if await fetch_member_or_none(guild, user_id) is None:
            store.remove_member(group.id, user_id)

    leader_left = bool(group.leader_user_id and await fetch_member_or_none(guild, group.leader_user_id) is None)
    if leader_left:
        store.mark_needs_leader(group.id)

    if missing:
        reason = ", ".join(missing)
        store.mark_broken(group.id, reason)
        if notify:
            await send_admin_notice(
                guild,
                group_embed(
                    "Group Needs Repair",
                    (
                        f"`{group.name}` is marked broken because the saved {reason} "
                        "could not be found. Use `/group repair` to recreate missing Discord objects."
                    ),
                    color=0xE67E22,
                ),
            )
    elif group.status == "broken":
        store.clear_broken_flags(group.id)

    if leader_left and notify:
        await send_admin_notice(
            guild,
            group_embed(
                "Group Needs New Leader",
                f"`{group.name}` has no valid leader in this server. Use `/group setleader`.",
                color=0xE67E22,
            ),
        )


async def create_group_discord_objects(
    guild: discord.Guild,
    name: str,
    leader: discord.Member,
    *,
    members: Iterable[discord.Member] = (),
    existing: Optional[GroupRecord] = None,
) -> tuple[discord.CategoryChannel, discord.TextChannel, discord.TextChannel, discord.Role, discord.Role]:
    group_role = guild.get_role(existing.group_role_id) if existing and existing.group_role_id else None
    leader_role = guild.get_role(existing.leader_role_id) if existing and existing.leader_role_id else None
    category = guild.get_channel(existing.category_id) if existing and existing.category_id else None
    announcements = guild.get_channel(existing.announcements_channel_id) if existing and existing.announcements_channel_id else None
    general = guild.get_channel(existing.general_channel_id) if existing and existing.general_channel_id else None

    if group_role is None:
        group_role = await guild.create_role(name=name, reason="Group repair/create")
    if leader_role is None:
        leader_role = await guild.create_role(name=f"{name} Leader", reason="Group repair/create")

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        group_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        leader_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

    if not isinstance(category, discord.CategoryChannel):
        category = await guild.create_category(name=name, overwrites=overwrites, reason="Group repair/create")
    if not isinstance(announcements, discord.TextChannel):
        announcements = await guild.create_text_channel(
            "announcements",
            category=category,
            overwrites=overwrites,
            reason="Group repair/create",
        )
    if not isinstance(general, discord.TextChannel):
        general = await guild.create_text_channel(
            "general",
            category=category,
            overwrites=overwrites,
            reason="Group repair/create",
        )
    await move_category_under_anchor(category)

    member_set = {member for member in members}
    member_set.add(leader)
    for member in member_set:
        await member.add_roles(group_role, reason="Group membership assignment")
    await leader.add_roles(leader_role, reason="Group leader assignment")
    return category, announcements, general, group_role, leader_role


async def send_group_welcome(
    group: GroupRecord,
    announcements: discord.TextChannel,
    general: discord.TextChannel,
    leader: discord.Member,
    members: Iterable[discord.Member],
) -> None:
    member_mentions = [member.mention for member in members if member.id != leader.id]
    ping_line = " ".join([leader.mention, *member_mentions]).strip()
    member_line = ", ".join(member_mentions) if member_mentions else "No extra members were added."
    embed = group_embed(
        "Welcome to Your Group",
        (
            f"**Group:** {group.name}\n"
            f"**Created by:** {leader.mention}\n"
            f"**Leader:** {leader.mention}\n"
            f"**Members:** {member_line}\n"
            f"**General chat:** {general.mention}\n\n"
            "This private space is ready. Use the general channel to work together, plan, and keep the group organized."
        ),
    )
    await announcements.send(content=ping_line or None, embed=embed)


class PanelApplyButton(discord.ui.Button):
    def __init__(self, store: GroupStore) -> None:
        super().__init__(
            label="Create",
            style=discord.ButtonStyle.secondary,
            custom_id="group_panel:create_group",
        )
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            setup_channel = await create_setup_channel(interaction.guild, interaction.user)
        except discord.Forbidden:
            await interaction.followup.send("I need permission to create setup channels.", ephemeral=True)
            return

        await interaction.followup.send(f"Setup started in {setup_channel.mention}.", ephemeral=True)
        asyncio.create_task(run_setup_flow(interaction.client, self.store, setup_channel, interaction.user))


class PanelView(discord.ui.LayoutView):
    def __init__(self, store: GroupStore) -> None:
        super().__init__(timeout=None)
        self.store = store
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                "## Group Creator\n"
                "Create a private project space where you and your friends can work together peacefully without distractions."
            )
        ]
        if BANNER_PATH.exists():
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://banner.png")))
        children.append(discord.ui.TextDisplay("Build your private chatroom for your group"))
        children.append(discord.ui.ActionRow(PanelApplyButton(store)))
        self.add_item(discord.ui.Container(*children, accent_color=0x2F80ED))


async def create_setup_channel(guild: discord.Guild, leader: discord.Member) -> discord.TextChannel:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        leader: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )
    return await guild.create_text_channel("setup", overwrites=overwrites, reason="Group setup started")


async def run_setup_flow(
    bot: discord.Client,
    store: GroupStore,
    channel: discord.TextChannel,
    leader: discord.Member,
) -> None:
    def message_from_leader(message: discord.Message) -> bool:
        return message.channel.id == channel.id and message.author.id == leader.id

    should_finish_channel = False
    try:
        await channel.send(embed=group_embed("Group Setup", f"{leader.mention}, what is the name of this group?"))
        name_message = await bot.wait_for("message", check=message_from_leader, timeout=600)
        group_name = name_message.content.strip()
        while not group_name or store.get_group(channel.guild.id, group_name) is not None:
            prompt = "That name is already used. Send a different group name." if group_name else "Send a group name."
            await channel.send(embed=group_embed("Group Setup", prompt, color=0xE67E22))
            name_message = await bot.wait_for("message", check=message_from_leader, timeout=600)
            group_name = name_message.content.strip()

        await channel.send(
            embed=group_embed(
                "Group Setup",
                "Who are the members of this group? Ping every member in one message, or type `none`.",
            )
        )
        members_message = await bot.wait_for("message", check=message_from_leader, timeout=600)
        members = list(members_message.mentions)
        members = [member for member in members if isinstance(member, discord.Member) and not member.bot]
        members_by_id = {member.id: member for member in members}
        members_by_id[leader.id] = leader

        category, announcements, general, group_role, leader_role = await create_group_discord_objects(
            channel.guild,
            group_name[:100],
            leader,
            members=members_by_id.values(),
        )
        group = store.create_group(
            guild_id=channel.guild.id,
            name=group_name[:100],
            category_id=category.id,
            announcements_channel_id=announcements.id,
            general_channel_id=general.id,
            group_role_id=group_role.id,
            leader_role_id=leader_role.id,
            leader_user_id=leader.id,
            member_user_ids=members_by_id.keys(),
        )
        await send_group_welcome(group, announcements, general, leader, members_by_id.values())
        await channel.send(
            embed=group_embed(
                "Group Created",
                (
                    f"`{group.name}` is ready.\n"
                    f"Leader: {leader.mention}\n"
                    f"Members saved: `{len(members_by_id)}`\n"
                    f"General: {general.mention}\n"
                    f"Announcements: {announcements.mention}"
                ),
            )
        )
        should_finish_channel = True
    except asyncio.TimeoutError:
        await channel.send(embed=group_embed("Setup Expired", "No response was received in time.", color=0xE67E22))
        should_finish_channel = True
    except Exception:
        log.exception("Setup flow failed in channel %s", channel.id)
        await channel.send(embed=group_embed("Setup Failed", "Something went wrong. Ask an admin to check the bot logs.", color=0xE74C3C))
        should_finish_channel = True
    finally:
        if should_finish_channel:
            await finish_setup_channel(channel, leader)


class PanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: GroupStore) -> None:
        self.bot = bot
        self.store = store

    @app_commands.command(name="panel", description="Post the group application panel.")
    @admin_only()
    async def panel(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message("I cannot send the panel in this channel.", ephemeral=True)
            return

        files: list[discord.File] = []
        if BANNER_PATH.exists():
            files.append(discord.File(BANNER_PATH, filename="banner.png"))
        if LOGO_PATH.exists():
            files.append(discord.File(LOGO_PATH, filename="logo.png"))

        await interaction.response.send_message("Sent.", ephemeral=True)
        await interaction.channel.send(files=files, view=PanelView(self.store))


class GroupCog(commands.GroupCog, name="group"):
    def __init__(self, bot: commands.Bot, store: GroupStore) -> None:
        self.bot = bot
        self.store = store

    @app_commands.command(name="list", description="Show all active groups.")
    @admin_only()
    async def list_groups(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        groups = [group for group in self.store.list_groups(interaction.guild.id) if group.status == "active"]
        if not groups:
            await interaction.response.send_message(embed=group_embed("Groups", "No active groups are saved."), ephemeral=True)
            return

        lines = []
        for group in groups[:25]:
            member_count = len(self.store.member_ids(group.id))
            leader = f"<@{group.leader_user_id}>" if group.leader_user_id else "None"
            lines.append(f"**{group.name}** - leader: {leader} - members: `{member_count}`")

        await interaction.response.send_message(embed=group_embed("Groups", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="add", description="Add a member to a saved group.")
    @admin_only()
    @app_commands.describe(name="Group name", user="Member to add")
    async def add(self, interaction: discord.Interaction, name: str, user: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        group = self.store.get_group(interaction.guild.id, name)
        if group is None:
            await interaction.response.send_message("No saved group exists with that name.", ephemeral=True)
            return
        if group.status != "active":
            await interaction.response.send_message("That group is not active.", ephemeral=True)
            return

        self.store.add_member(group.id, user.id)
        group_role = interaction.guild.get_role(group.group_role_id) if group.group_role_id else None
        if group_role is not None:
            await user.add_roles(group_role, reason=f"Added to group {group.name}")

        await interaction.response.send_message(
            embed=group_embed("Member Added", f"{user.mention} was added to `{group.name}`."),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Remove a member from a saved group.")
    @admin_only()
    @app_commands.describe(name="Group name", user="Member to remove")
    async def remove(self, interaction: discord.Interaction, name: str, user: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        group = self.store.get_group(interaction.guild.id, name)
        if group is None:
            await interaction.response.send_message("No saved group exists with that name.", ephemeral=True)
            return

        self.store.remove_member(group.id, user.id)
        roles_to_remove = [
            role
            for role in (
                interaction.guild.get_role(group.group_role_id) if group.group_role_id else None,
                interaction.guild.get_role(group.leader_role_id) if group.leader_role_id and user.id == group.leader_user_id else None,
            )
            if role is not None
        ]
        if roles_to_remove:
            await user.remove_roles(*roles_to_remove, reason=f"Removed from group {group.name}")

        if user.id == group.leader_user_id:
            self.store.mark_needs_leader(group.id)

        await interaction.response.send_message(
            embed=group_embed("Member Removed", f"{user.mention} was removed from `{group.name}`."),
            ephemeral=True,
        )


class GroupBot(commands.Bot):
    def __init__(self, store: GroupStore) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = store
        self._startup_validation_done = False
        self._avatar_sync_done = False
        self._channel_enforce_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.add_view(PanelView(self.store))
        await self.add_cog(PanelCog(self, self.store))
        await self.add_cog(GroupCog(self, self.store))
        if COMMAND_GUILD_ID:
            guild_obj = discord.Object(id=int(COMMAND_GUILD_ID))
            self.tree.copy_global_to(guild=guild_obj)
            try:
                await self.tree.sync(guild=guild_obj)
            except discord.Forbidden:
                log.exception(
                    "Could not sync commands to COMMAND_GUILD_ID=%s. "
                    "The bot must be in that server and invited with the applications.commands scope. "
                    "Falling back to global command sync.",
                    COMMAND_GUILD_ID,
                )
                await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        if AUTO_SET_BOT_AVATAR and not self._avatar_sync_done and self.user is not None and LOGO_PATH.exists():
            self._avatar_sync_done = True
            try:
                await self.user.edit(avatar=LOGO_PATH.read_bytes())
            except discord.HTTPException:
                log.exception("Could not update bot avatar from %s", LOGO_PATH)
        if self._startup_validation_done:
            return
        self._startup_validation_done = True
        for group in self.store.list_groups():
            await validate_group(self, self.store, group, notify=True)
        for guild in self.guilds:
            async with self._channel_enforce_lock:
                await enforce_group_channel_locations(guild, self.store)

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if before.position == after.position and getattr(before, "category_id", None) == getattr(after, "category_id", None):
            return

        async with self._channel_enforce_lock:
            await enforce_group_channel_locations(after.guild, self.store)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_components_v2()
    if not DISCORD_TOKEN or DISCORD_TOKEN == "put-your-bot-token-here":
        raise RuntimeError("Set DISCORD_TOKEN before starting the bot.")
    store = GroupStore(DB_PATH)
    bot = GroupBot(store)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
