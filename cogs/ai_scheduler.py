import asyncio
import json
import logging
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks

from utils.checks import is_bot_owner_id

logger = logging.getLogger("ModBot.AIScheduler")


class AIScheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ready = False
        self._task_loop.start()

    def cog_unload(self):
        self._task_loop.cancel()

    @tasks.loop(seconds=30.0)
    async def _task_loop(self):
        if not self._ready:
            return
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            async with self.bot.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT id, guild_id, task_type, payload, execute_at, author_id "
                    "FROM scheduled_tasks "
                    "WHERE status = 'pending' AND execute_at <= ?",
                    (now,),
                )
                records = await cursor.fetchall()

            if not records:
                return

            for row in records:
                task_id = row[0]
                guild_id = row[1]
                task_type = row[2]
                payload_str = row[3]
                author_id = row[5] if len(row) > 5 else None

                try:
                    payload = json.loads(payload_str) if payload_str else {}
                    await self._execute_task(guild_id, task_type, payload, author_id)
                    async with self.bot.db.get_connection() as db:
                        await db.execute(
                            "UPDATE scheduled_tasks SET status = 'completed' WHERE id = ?",
                            (task_id,),
                        )
                        await db.commit()
                except Exception as e:
                    logger.error(f"Failed to execute task {task_id}: {e}")
                    try:
                        async with self.bot.db.get_connection() as db:
                            await db.execute(
                                "UPDATE scheduled_tasks SET status = 'failed' WHERE id = ?",
                                (task_id,),
                            )
                            await db.commit()
                    except Exception:
                        pass
        except Exception as e:
            # Silently ignore "table does not exist" during early startup
            if "scheduled_tasks" in str(e) and "does not exist" in str(e):
                return
            logger.exception("AI Scheduler loop failed")

    async def _execute_task(self, guild_id: int, task_type: str, payload: dict, author_id: int | None = None):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        if task_type == "reminder":
            user_id = payload.get("user_id")
            message = payload.get("message")
            if user_id and message:
                try:
                    member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                    if member:
                        await member.send(f"⏰ **Reminder:** {message}")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"Reminder delivery failed for user {user_id}: {e}")

        elif task_type == "execute_python":
            # SECURITY: scheduled Python runs arbitrary code with full bot
            # privileges. Re-authorize at execution time — the task's original
            # author must STILL be a bot owner. This closes the privilege-
            # escalation gap where a non-owner could persist a task that later
            # ran unchecked. Never trust the row alone.
            if author_id is None or not is_bot_owner_id(int(author_id)):
                logger.warning(
                    "Blocked scheduled execute_python for guild %s: author %s is not a bot owner.",
                    guild_id, author_id,
                )
                raise PermissionError("Scheduled execute_python author is not an authorized bot owner.")

            code = payload.get("code")
            if code:
                env = {
                    "bot": self.bot,
                    "guild": guild,
                    "discord": discord,
                    "asyncio": asyncio,
                }
                wrapped = "async def _scheduled_task():\n"
                for line in code.splitlines():
                    wrapped += f"    {line}\n"
                logger.info("Running scheduled execute_python authored by owner %s in guild %s.", author_id, guild_id)
                exec(wrapped, env)
                await asyncio.wait_for(env["_scheduled_task"](), timeout=60)

    @_task_loop.before_loop
    async def _before_task_loop(self):
        await self.bot.wait_until_ready()
        # Wait a bit for guild init to create the scheduled_tasks table
        await asyncio.sleep(15)
        self._ready = True
        logger.info("AI Scheduler is now active.")


async def setup(bot):
    await bot.add_cog(AIScheduler(bot))
