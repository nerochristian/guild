"""
Confirmation views — Discord UI buttons for severe/uncertain actions.

When the AI recommends a severe action or confidence is below the
configured threshold, a confirmation view is sent with buttons:

- ✅ Confirm: Execute the action.
- ✏️ Change punishment: Ask the mod to specify a different action.
- 📋 View evidence: Show the evidence (ephemeral).
- ❌ Cancel: Cancel the action.
- ⬆️ Escalate: Escalate to a senior moderator.

Only the original moderator (or a senior moderator) can interact.
Confirmations expire after config.confirmation_timeout_seconds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

import discord

from .config import GuildConfig
from .models import ActionType, CaseRecord, CaseStatus, Evidence, ModerationRequest
from .utils import format_duration

logger = logging.getLogger("ModBot.AIModeration.Confirmations")


# =============================================================================
# Confirmation view
# =============================================================================


class ConfirmationView(discord.ui.View):
    """Button-based confirmation for a pending moderation action.

    The view is actor-bound: only the moderator who initiated the request
    (or a senior moderator) can interact with the buttons.

    All button handlers acquire a lock to prevent race conditions when
    multiple users click simultaneously.
    """

    def __init__(
        self,
        *,
        request: ModerationRequest,
        actor_id: int,
        senior_moderator_role_ids: set[int],
        evidence: List[Evidence],
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self.request = request
        self.actor_id = actor_id
        self.senior_moderator_role_ids = senior_moderator_role_ids
        self.evidence = evidence
        self._finished = False
        self._lock = asyncio.Lock()
        self._result: Optional[str] = None  # "confirm" | "cancel" | "escalate" | "change"

        # The confirmation message (set by the caller after sending).
        self.message: Optional[discord.Message] = None

    @property
    def result(self) -> Optional[str]:
        return self._result

    # ------------------------------------------------------------------
    # Authorization check
    # ------------------------------------------------------------------

    def _is_authorized(self, user: discord.abc.User) -> bool:
        """Check if a user is authorized to interact with this view."""
        if user.id == self.actor_id:
            return True
        if isinstance(user, discord.Member):
            user_role_ids = {r.id for r in user.roles}
            if user_role_ids & self.senior_moderator_role_ids:
                return True
            if user.guild_permissions.administrator:
                return True
        return False

    async def _reject_unauthorized(
        self, interaction: discord.Interaction
    ) -> bool:
        """Return True if the user was unauthorized (and told so)."""
        if self._is_authorized(interaction.user):
            return False
        await interaction.response.send_message(
            "Only the moderator who requested this action (or a senior "
            "moderator) can interact with these buttons.",
            ephemeral=True,
        )
        return True

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._reject_unauthorized(interaction):
            return
        async with self._lock:
            if self._finished:
                await interaction.response.send_message(
                    "This confirmation has already been handled.", ephemeral=True
                )
                return
            self._finished = True
            self._result = "confirm"
            self.stop()
        await interaction.response.edit_message(
            view=self._status_view("Executing...", "The action is being executed.", discord.Color.orange())
        )

    @discord.ui.button(label="Change", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def change_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._reject_unauthorized(interaction):
            return
        async with self._lock:
            if self._finished:
                await interaction.response.send_message(
                    "This confirmation has already been handled.", ephemeral=True
                )
                return
            self._finished = True
            self._result = "change"
            self.stop()
        await interaction.response.edit_message(
            view=self._status_view(
                "Change Requested",
                "Please reply with the new action or duration.",
                discord.Color.blurple(),
            )
        )

    @discord.ui.button(label="Evidence", style=discord.ButtonStyle.secondary, emoji="📋")
    async def evidence_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._reject_unauthorized(interaction):
            return
        # Show evidence as an ephemeral message.
        embed = self._build_evidence_embed()
        await interaction.response.send_message(
            embed=embed, ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._reject_unauthorized(interaction):
            return
        async with self._lock:
            if self._finished:
                await interaction.response.send_message(
                    "This confirmation has already been handled.", ephemeral=True
                )
                return
            self._finished = True
            self._result = "cancel"
            self.stop()
        await interaction.response.edit_message(
            view=self._status_view(
                "Cancelled", "The action was not executed.", discord.Color.greyple()
            )
        )

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.primary, emoji="⬆️")
    async def escalate_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._reject_unauthorized(interaction):
            return
        async with self._lock:
            if self._finished:
                await interaction.response.send_message(
                    "This confirmation has already been handled.", ephemeral=True
                )
                return
            self._finished = True
            self._result = "escalate"
            self.stop()
        await interaction.response.edit_message(
            view=self._status_view(
                "Escalated",
                "This case has been escalated for senior moderator review.",
                discord.Color.gold(),
            )
        )

    # ------------------------------------------------------------------
    # Timeout
    # ------------------------------------------------------------------

    async def on_timeout(self) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            self._result = "expired"
        if self.message:
            try:
                await self.message.edit(
                    view=self._status_view(
                        "Expired",
                        "Confirmation timed out. The action was not executed.",
                        discord.Color.dark_grey(),
                    )
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Embed builders
    # ------------------------------------------------------------------

    @staticmethod
    def _status_view(title: str, description: str, color: discord.Color) -> discord.ui.View:
        """Build a simple status view (no buttons, just an embed)."""
        view = discord.ui.View(timeout=None)
        embed = discord.Embed(title=title, description=description, color=color)
        view.add_item(embed)  # type: ignore[arg-type]  # LayoutView vs View compatibility
        return view

    def _build_evidence_embed(self) -> discord.Embed:
        """Build an embed showing the evidence for this confirmation."""
        embed = discord.Embed(
            title="📋 Evidence Review",
            color=discord.Color.blurple(),
        )
        if not self.evidence:
            embed.description = "No evidence was collected for this case."
            return embed

        for i, ev in enumerate(self.evidence[:10], 1):
            embed.add_field(
                name=f"Evidence {i} — {ev.author_name}",
                value=(
                    f"```{ev.content[:500]}```\n"
                    f"Message ID: `{ev.message_id}` • "
                    f"Channel: <#{ev.channel_id}>"
                ),
                inline=False,
            )
        if len(self.evidence) > 10:
            embed.set_footer(text=f"Showing 10 of {len(self.evidence)} evidence items.")
        return embed

    def build_confirmation_embed(self) -> discord.Embed:
        """Build the main confirmation embed shown with the buttons."""
        request = self.request
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            color=discord.Color.orange(),
        )

        # Summary.
        embed.add_field(
            name="AI Summary",
            value=request.summary[:1024] or "No summary provided.",
            inline=False,
        )

        # Targets and actions.
        for i, target in enumerate(request.targets, 1):
            action_name = target.action.value.replace("_", " ").title()
            target_str = f"<@{target.user_id}>"
            duration_str = (
                f" for {format_duration(target.duration_seconds)}"
                if target.duration_seconds
                else ""
            )
            embed.add_field(
                name=f"Action {i}: {action_name}",
                value=f"Target: {target_str}{duration_str}\nReason: {target.reason or 'not specified'}",
                inline=False,
            )

        # Confidence.
        if request.confidence > 0:
            embed.add_field(
                name="AI Confidence",
                value=f"{request.confidence * 100:.0f}%",
                inline=True,
            )

        # Evidence count.
        if self.evidence:
            embed.add_field(
                name="Evidence",
                value=f"{len(self.evidence)} message(s)",
                inline=True,
            )

        # Reasoning.
        if request.reasoning:
            embed.add_field(
                name="AI Reasoning",
                value=request.reasoning[:1024],
                inline=False,
            )

        embed.set_footer(text="Click a button below to proceed.")
        return embed
