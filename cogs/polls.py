"""
Poll System - Custom implementation that mimics Discord's native poll UI
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Dict, Set
from datetime import datetime, timezone, timedelta
import asyncio

from utils.embeds import ModEmbed
from utils.checks import is_mod


class PollButton(discord.ui.Button):
    """Button for poll options"""
    
    def __init__(self, poll_id: int, option_index: int, option_text: str, is_selected: bool):
        # Style to look like Discord's poll options
        super().__init__(
            label=option_text[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=f"poll_{poll_id}_{option_index}",
            row=option_index if option_index < 5 else 4
        )
        self.poll_id = poll_id
        self.option_index = option_index
    
    async def callback(self, interaction: discord.Interaction):
        poll_cog = interaction.client.get_cog("Polls")
        if poll_cog:
            await poll_cog._handle_vote(interaction, self.poll_id, self.option_index)


class RemoveVoteButton(discord.ui.Button):
    """Remove vote button"""
    
    def __init__(self, poll_id: int):
        super().__init__(
            label="Remove Vote",
            style=discord.ButtonStyle.secondary,
            custom_id=f"poll_remove_{poll_id}",
            row=4
        )
        self.poll_id = poll_id
    
    async def callback(self, interaction: discord.Interaction):
        poll_cog = interaction.client.get_cog("Polls")
        if poll_cog:
            await poll_cog._remove_vote(interaction, self.poll_id)


class ShowResultsButton(discord.ui.Button):
    """Show results button"""
    
    def __init__(self, poll_id: int):
        super().__init__(
            label="Show results",
            style=discord.ButtonStyle.secondary,
            custom_id=f"poll_results_{poll_id}",
            row=4
        )
        self.poll_id = poll_id
    
    async def callback(self, interaction: discord.Interaction):
        poll_cog = interaction.client.get_cog("Polls")
        if poll_cog:
            await poll_cog._show_results(interaction, self.poll_id)


class VoteButton(discord.ui.Button):
    """Vote button (appears when results are shown)"""
    
    def __init__(self, poll_id: int):
        super().__init__(
            label="Vote",
            style=discord.ButtonStyle.primary,
            custom_id=f"poll_vote_{poll_id}",
            row=4
        )
        self.poll_id = poll_id
    
    async def callback(self, interaction: discord.Interaction):
        poll_cog = interaction.client.get_cog("Polls")
        if poll_cog:
            await poll_cog._back_to_voting(interaction, self.poll_id)


class PollView(discord.ui.View):
    """View for poll - dynamically updates based on user's vote status"""
    
    def __init__(self, poll_data: dict, user_id: int, user_vote: Optional[int], show_results: bool = False):
        super().__init__(timeout=None)
        
        poll_id = poll_data["id"]
        options = poll_data["options"]
        
        if show_results:
            # Just show "Vote" button when viewing results
            self.add_item(VoteButton(poll_id))
        elif user_vote is not None:
            # User has voted - show Remove Vote button
            self.add_item(RemoveVoteButton(poll_id))
        else:
            # User hasn't voted - show Show Results button
            self.add_item(ShowResultsButton(poll_id))


class Polls(commands.Cog):
    """Poll management system with Discord-native-like UI"""
    
    def __init__(self, bot):
        self.bot = bot
        self.active_polls: Dict[int, dict] = {}
        self.poll_votes: Dict[int, Dict[int, int]] = {}  # poll_id -> {user_id -> option_index}
        self._poll_counter = 1
    
    poll_group = app_commands.Group(name="poll", description="📊 Poll commands")
    
    @poll_group.command(name="create", description="📊 Create a poll")
    @app_commands.describe(
        question="The poll question",
        option1="First option",
        option2="Second option",
        option3="Third option (optional)",
        option4="Fourth option (optional)",
        option5="Fifth option (optional)",
        option6="Sixth option (optional)",
        option7="Seventh option (optional)",
        option8="Eighth option (optional)",
        option9="Ninth option (optional)",
        option10="Tenth option (optional)",
        duration="How long the poll runs in hours (1-168, default: 24)",
        channel="Which channel to post the poll in",
        multiple_choice="Allow users to vote for multiple options"
    )
    @is_mod()
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: str,
        option1: str,
        option2: str,
        option3: Optional[str] = None,
        option4: Optional[str] = None,
        option5: Optional[str] = None,
        option6: Optional[str] = None,
        option7: Optional[str] = None,
        option8: Optional[str] = None,
        option9: Optional[str] = None,
        option10: Optional[str] = None,
        duration: Optional[int] = 24,
        channel: Optional[discord.TextChannel] = None,
        multiple_choice: bool = False
    ):
        """Create a new poll"""
        
        # Collect options
        options = [option1, option2]
        for opt in [option3, option4, option5, option6, option7, option8, option9, option10]:
            if opt:
                options.append(opt)
        
        if len(options) < 2:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Enough Options", "Polls need at least 2 options."),
                ephemeral=True
            )
        
        # Validate duration
        if duration:
            duration = max(1, min(168, duration))
        else:
            duration = 24
        
        # Default to current channel
        if channel is None:
            channel = interaction.channel
        
        # Create poll data
        poll_id = self._poll_counter
        self._poll_counter += 1
        
        ends_at = datetime.now(timezone.utc) + timedelta(hours=duration)
        
        poll_data = {
            "id": poll_id,
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "question": question,
            "options": options,
            "creator_id": interaction.user.id,
            "created_at": datetime.now(timezone.utc),
            "ends_at": ends_at,
            "multiple_choice": multiple_choice,
            "ended": False
        }
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Create the initial message
            embed = self._create_poll_embed(poll_data, 0, show_results=False)
            view = PollView(poll_data, 0, None, show_results=False)
            
            msg = await channel.send(embed=embed, view=view)
            
            # Store poll data
            poll_data["message_id"] = msg.id
            self.active_polls[poll_id] = poll_data
            self.poll_votes[poll_id] = {}
            
            # Start auto-end task
            asyncio.create_task(self._auto_end_poll(poll_id))
            
            await interaction.followup.send(
                embed=ModEmbed.success(
                    "Poll Created",
                    f"Poll ID: `{poll_id}`\nPosted in {channel.mention}\n{msg.jump_url}"
                ),
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(
                embed=ModEmbed.error("Failed", f"Failed to create poll: {e}"),
                ephemeral=True
            )
    
    def _create_poll_embed(self, poll_data: dict, user_id: int, show_results: bool = False, user_vote: Optional[int] = None) -> discord.Embed:
        """Create poll embed that looks like Discord's native polls"""
        
        question = poll_data["question"]
        options = poll_data["options"]
        multiple = poll_data.get("multiple_choice", False)
        
        votes = self.poll_votes.get(poll_data["id"], {})
        total_votes = len(votes)
        
        # Count votes per option
        vote_counts = {}
        for option_idx in range(len(options)):
            vote_counts[option_idx] = sum(1 for v in votes.values() if v == option_idx)
        
        # Build description
        description = f"**{question}**\n"
        description += f"*Select {'multiple answers' if multiple else 'one answer'}*\n\n"
        
        # Add options
        for i, option in enumerate(options):
            count = vote_counts.get(i, 0)
            percentage = (count / total_votes * 100) if total_votes > 0 else 0
            
            # Check if this option is selected by the user
            is_selected = user_vote == i
            
            if show_results or (user_vote is not None):
                # Show results
                # Use a checkmark if user voted for this
                check = "✅ " if is_selected else ""
                description += f"{check}**{option}**\n"
                description += f"`{count} vote{'s' if count != 1 else ''} - {percentage:.0f}%`\n\n"
            else:
                # Just show option text
                description += f"**{option}**\n\n"
        
        # Calculate time left
        ends_at = poll_data.get("ends_at")
        if ends_at:
            time_left = ends_at - datetime.now(timezone.utc)
            hours_left = int(time_left.total_seconds() / 3600)
            if hours_left > 0:
                time_text = f"{hours_left}h left"
            else:
                mins_left = int(time_left.total_seconds() / 60)
                time_text = f"{mins_left}m left" if mins_left > 0 else "Ending soon"
        else:
            time_text = "No time limit"
        
        # Add vote count and time
        if show_results or user_vote is not None:
            description += f"\n{total_votes} vote{'s' if total_votes != 1 else ''} • {time_text}"
        else:
            description += f"\n0 votes • {time_text}"
        
        # Create embed with Discord's dark theme colors
        embed = discord.Embed(
            description=description,
            color=0x2B2D31  # Discord's dark background color
        )
        
        return embed
    
    async def _handle_vote(self, interaction: discord.Interaction, poll_id: int, option_index: int):
        """Handle a user voting"""
        poll_data = self.active_polls.get(poll_id)
        
        if not poll_data:
            return await interaction.response.send_message(
                "This poll is no longer active.",
                ephemeral=True
            )
        
        if poll_data.get("ended"):
            return await interaction.response.send_message(
                "This poll has ended.",
                ephemeral=True
            )
        
        user_id = interaction.user.id
        votes = self.poll_votes.get(poll_id, {})
        
        # Record vote
        votes[user_id] = option_index
        self.poll_votes[poll_id] = votes
        
        # Update message
        await self._update_poll_for_user(interaction, poll_id, user_id)
    
    async def _remove_vote(self, interaction: discord.Interaction, poll_id: int):
        """Remove a user's vote"""
        poll_data = self.active_polls.get(poll_id)
        
        if not poll_data:
            return await interaction.response.send_message(
                "This poll is no longer active.",
                ephemeral=True
            )
        
        user_id = interaction.user.id
        votes = self.poll_votes.get(poll_id, {})
        
        if user_id in votes:
            del votes[user_id]
            self.poll_votes[poll_id] = votes
        
        # Update message
        await self._update_poll_for_user(interaction, poll_id, user_id)
    
    async def _show_results(self, interaction: discord.Interaction, poll_id: int):
        """Show results without voting"""
        await self._update_poll_for_user(interaction, poll_id, interaction.user.id, show_results=True)
    
    async def _back_to_voting(self, interaction: discord.Interaction, poll_id: int):
        """Go back to voting view"""
        await self._update_poll_for_user(interaction, poll_id, interaction.user.id, show_results=False)
    
    async def _update_poll_for_user(self, interaction: discord.Interaction, poll_id: int, user_id: int, show_results: bool = False):
        """Update the poll view for a specific user"""
        poll_data = self.active_polls.get(poll_id)
        if not poll_data:
            return
        
        votes = self.poll_votes.get(poll_id, {})
        user_vote = votes.get(user_id)
        
        # Create updated embed and view
        embed = self._create_poll_embed(poll_data, user_id, show_results=show_results, user_vote=user_vote)
        view = PollView(poll_data, user_id, user_vote, show_results=show_results)
        
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.InteractionResponded:
            # Already responded, use followup
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=view)
        except Exception as e:
            print(f"[Polls] Error updating poll: {e}")
    
    async def _auto_end_poll(self, poll_id: int):
        """Auto-end poll after duration"""
        poll_data = self.active_polls.get(poll_id)
        if not poll_data:
            return
        
        ends_at = poll_data.get("ends_at")
        if not ends_at:
            return
        
        now = datetime.now(timezone.utc)
        wait_seconds = (ends_at - now).total_seconds()
        
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        
        # Mark as ended
        poll_data["ended"] = True
        
        # Post results
        try:
            channel = self.bot.get_channel(poll_data["channel_id"])
            if channel:
                msg = await channel.fetch_message(poll_data["message_id"])
                
                votes = self.poll_votes.get(poll_id, {})
                vote_counts = {}
                for option_idx in range(len(poll_data["options"])):
                    vote_counts[option_idx] = sum(1 for v in votes.values() if v == option_idx)
                
                max_votes = max(vote_counts.values()) if vote_counts.values() else 0
                winners = [poll_data["options"][i] for i, count in vote_counts.items() if count == max_votes]
                
                result_text = f"📊 **Poll Ended!**\n🏆 Winner: **{winners[0]}**" if winners and max_votes > 0 else "📊 **Poll Ended!** (No votes)"
                
                await msg.reply(result_text)
        except Exception as e:
            print(f"[Polls] Error ending poll {poll_id}: {e}")
    
    @poll_group.command(name="end", description="🛑 End a poll early")
    @app_commands.describe(poll_id="The poll ID")
    @is_mod()
    async def poll_end(self, interaction: discord.Interaction, poll_id: int):
        """Manually end a poll"""
        poll_data = self.active_polls.get(poll_id)
        
        if not poll_data:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that poll."),
                ephemeral=True
            )
        
        if poll_data.get("ended"):
            return await interaction.response.send_message(
                embed=ModEmbed.info("Already Ended", f"Poll ID: `{poll_id}`"),
                ephemeral=True
            )
        
        poll_data["ended"] = True
        
        await interaction.response.send_message(
            embed=ModEmbed.success("Poll Ended", f"Poll ID: `{poll_id}` has been ended."),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Polls(bot))
