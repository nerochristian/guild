import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import commands
import psycopg2
import psycopg2.extras

LOGGER = logging.getLogger("enzo-bot.afk-system")


class AFKStore:
    def __init__(self, db_url: str) -> None:
        if not db_url:
            raise RuntimeError(
                "DATABASE_URL is required; configure it before starting the bot."
            )
        self.db_url = db_url

    def _connect(self):
        return psycopg2.connect(self.db_url, cursor_factory=psycopg2.extras.DictCursor)

    def initialize(self):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS afk_status (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            reason TEXT,
                            original_name TEXT,
                            since_timestamp BIGINT NOT NULL,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
        finally:
            conn.close()

    def set_afk(
        self,
        guild_id: int,
        user_id: int,
        reason: str,
        original_name: str,
        since_timestamp: int,
    ):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO afk_status (guild_id, user_id, reason, original_name, since_timestamp)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            reason = EXCLUDED.reason,
                            original_name = EXCLUDED.original_name,
                            since_timestamp = EXCLUDED.since_timestamp
                        """,
                        (guild_id, user_id, reason, original_name, since_timestamp),
                    )
        finally:
            conn.close()

    def remove_afk(self, guild_id: int, user_id: int) -> Optional[dict]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM afk_status 
                        WHERE guild_id = %s AND user_id = %s
                        RETURNING reason, original_name, since_timestamp
                        """,
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    return dict(row) if row else None
        finally:
            conn.close()

    def get_afk(self, guild_id: int, user_id: int) -> Optional[dict]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT reason, original_name, since_timestamp FROM afk_status WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def get_afk_bulk(self, guild_id: int, user_ids: list[int]) -> dict[int, dict]:
        if not user_ids:
            return {}
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, reason, original_name, since_timestamp FROM afk_status WHERE guild_id = %s AND user_id = ANY(%s)",
                    (guild_id, user_ids),
                )
                return {int(row["user_id"]): dict(row) for row in cursor.fetchall()}
        finally:
            conn.close()


class AFKSystem(commands.Cog):
    def __init__(self, bot: commands.Bot, db_url: str):
        self.bot = bot
        self.store = AFKStore(db_url)
        self.store.initialize()

    @commands.command(name="afk", help="Set your AFK status")
    async def set_afk(self, ctx: commands.Context, *, reason: str = "AFK"):
        if ctx.guild is None:
            return

        original_name = ctx.author.display_name
        timestamp = int(time.time())

        await asyncio.to_thread(
            self.store.set_afk,
            ctx.guild.id,
            ctx.author.id,
            reason,
            original_name,
            timestamp,
        )

        # Try to rename
        try:
            if (
                ctx.author.id != ctx.guild.owner_id
                and ctx.guild.me.top_role > ctx.author.top_role
            ):
                new_name = f"[AFK] {original_name}"
                if len(new_name) > 32:
                    new_name = new_name[:32]
                await ctx.author.edit(nick=new_name)
        except Exception as exc:
            LOGGER.warning("Could not change nickname for AFK: %s", exc)

        embed = discord.Embed(
            description=f"✅ **{ctx.author.display_name}** is now AFK: {reason}",
            color=0x2ECC71,
        )
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        # Ignore bot commands so they don't remove AFK status (and to prevent race conditions with .afk)
        if message.content.startswith("."):
            return

        # Check if the author is returning from AFK
        afk_data = await asyncio.to_thread(
            self.store.remove_afk, message.guild.id, message.author.id
        )
        if afk_data:
            # Revert nickname if possible
            try:
                if (
                    message.author.id != message.guild.owner_id
                    and message.guild.me.top_role > message.author.top_role
                ):
                    # Only revert if it still has [AFK] to be safe
                    if message.author.display_name.startswith("[AFK]"):
                        original_name = afk_data["original_name"]
                        new_nick = (
                            None
                            if original_name == message.author.name
                            else original_name
                        )
                        await message.author.edit(nick=new_nick)
            except Exception:
                pass

            duration = max(0, int(time.time()) - afk_data["since_timestamp"])
            mins = duration // 60
            hours = mins // 60
            mins %= 60

            time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            if duration < 60:
                time_str = f"{duration}s"

            welcome_msg = await message.channel.send(
                f"👋 Welcome back {message.author.mention}! You were AFK for {time_str}."
            )
            await asyncio.sleep(10)
            try:
                await welcome_msg.delete()
            except Exception:
                pass

        # Check if any mentioned users are AFK
        if message.mentions:
            mentioned_users = [
                user for user in message.mentions if user.id != message.author.id
            ]
            afk_by_user = await asyncio.to_thread(
                self.store.get_afk_bulk,
                message.guild.id,
                [user.id for user in mentioned_users],
            )
            for mentioned_user in mentioned_users:
                mentioned_afk = afk_by_user.get(mentioned_user.id)
                if mentioned_afk:
                    timestamp = mentioned_afk["since_timestamp"]
                    reason = mentioned_afk["reason"]
                    reply = await message.reply(
                        f"💤 **{mentioned_user.display_name}** is AFK: {reason} (<t:{timestamp}:R>)"
                    )
                    # Delete after 30 seconds as requested
                    asyncio.create_task(self._delete_later(reply, 30))

    async def _delete_later(self, message: discord.Message, delay: int):
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass


def init_afk_system(bot: commands.Bot, *, db_url: str) -> AFKSystem:
    return AFKSystem(bot, db_url)
