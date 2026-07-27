"""
Rule engine — evaluates violations against guild rules and the punishment matrix.

Given a detected violation + the user's infraction history, the rule engine
determines:
- Which rule was violated
- The severity
- The recommended action (from the punishment matrix, escalating by offense count)
- Whether the action exceeds the rule's max_automatic_action
- Whether human review is required
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .config import GuildConfig, PunishmentMatrix
from .database import Database
from .models import (
    ActionType,
    InvestigationResult,
    Rule,
    SeverityLevel,
)

logger = logging.getLogger("ModBot.AIModeration.RuleEngine")


class RuleEngine:
    """Evaluates violations against configured guild rules.

    The rule engine is the bridge between the AI's investigation result
    and the configured punishment system. It ensures the AI never exceeds
    configured limits.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Find matching rule
    # ------------------------------------------------------------------

    @staticmethod
    def find_rule(
        violation_description: str,
        rules: List[Rule],
    ) -> Optional[Rule]:
        """Find the rule that best matches a detected violation.

        Matching is keyword-based: the rule whose name appears in the
        violation description wins. If no match, returns None.
        """
        if not violation_description:
            return None

        violation_lower = violation_description.lower()

        # Exact name match.
        for rule in rules:
            if rule.name.lower() in violation_lower:
                return rule

        # Partial match on keywords in the description.
        for rule in rules:
            rule_keywords = rule.name.lower().replace("_", " ").split()
            if all(kw in violation_lower for kw in rule_keywords):
                return rule

        return None

    # ------------------------------------------------------------------
    # Get recommended punishment
    # ------------------------------------------------------------------

    async def get_recommended_punishment(
        self,
        guild_id: int,
        target_id: int,
        severity: SeverityLevel,
        rule: Optional[Rule],
        config: GuildConfig,
    ) -> tuple[ActionType, Optional[int], bool]:
        """Determine the recommended punishment for a violation.

        Returns (action, duration_seconds, requires_human_review).

        The punishment is determined by:
        1. Count the target's active infractions for this rule.
        2. Use the punishment matrix to escalate.
        3. If the matrix's recommendation exceeds the rule's
           max_automatic_action, require human review.
        """
        if severity == SeverityLevel.NONE:
            return (ActionType.NO_ACTION, None, False)

        # Count prior infractions (all actions, not just for this rule —
        # repeat offenders escalate faster).
        offense_count = await self.db.count_active_infractions(
            guild_id, target_id
        )
        offense_count += 1  # Include the current offense.

        # Get the punishment from the matrix.
        action, duration = config.punishment_matrix.get_punishment(
            severity, offense_count
        )

        # Check if the action exceeds the rule's max_automatic_action.
        requires_human = False
        if rule:
            max_auto = rule.max_automatic_action
            if self._action_exceeds_max(action, max_auto):
                # Clamp to the max automatic action and require human review.
                action = max_auto
                duration = self._default_duration_for_action(action, config)
                requires_human = True

        # Also require human review for critical severity.
        if severity == SeverityLevel.CRITICAL:
            requires_human = True

        return (action, duration, requires_human)

    # ------------------------------------------------------------------
    # Validate an AI-recommended action against the rules
    # ------------------------------------------------------------------

    def validate_action_against_rules(
        self,
        action: ActionType,
        severity: SeverityLevel,
        rule: Optional[Rule],
        config: GuildConfig,
    ) -> tuple[bool, str]:
        """Validate that the AI's recommended action is within bounds.

        Returns (is_valid, reason). If is_valid is False, the action
        should be clamped or rejected.
        """
        # No-action is always valid.
        if action == ActionType.NO_ACTION:
            return (True, "")

        # If there's a rule, check against its max_automatic_action.
        if rule:
            max_auto = rule.max_automatic_action
            if self._action_exceeds_max(action, max_auto):
                return (
                    False,
                    f"Action {action.value} exceeds the maximum automatic "
                    f"action ({max_auto.value}) for rule {rule.name!r}.",
                )

        # Ban and temp_ban are only valid for severe+ violations.
        if action in {ActionType.BAN, ActionType.TEMP_BAN}:
            if severity.numeric < SeverityLevel.SEVERE.numeric:
                return (
                    False,
                    f"Action {action.value} is too severe for "
                    f"{severity.value} violations.",
                )

        # Kick is only valid for moderate+ violations.
        if action == ActionType.KICK:
            if severity.numeric < SeverityLevel.MODERATE.numeric:
                return (
                    False,
                    f"Kick is too severe for {severity.value} violations.",
                )

        return (True, "")

    # ------------------------------------------------------------------
    # Enrich an investigation result with rule information
    # ------------------------------------------------------------------

    async def enrich_investigation(
        self,
        investigation: InvestigationResult,
        guild_id: int,
        target_id: Optional[int],
        config: GuildConfig,
    ) -> InvestigationResult:
        """Enrich an investigation result with rule-based recommendations."""
        if not investigation.detected_violation:
            return investigation

        # Find the matching rule.
        rule = self.find_rule(investigation.detected_violation, config.rules)
        if rule:
            investigation.severity = rule.severity

        # Get recommended punishment.
        if target_id:
            action, duration, requires_human = await self.get_recommended_punishment(
                guild_id, target_id, investigation.severity, rule, config
            )
            investigation.recommended_action = action
            investigation.requires_human_review = requires_human

            # If the AI's recommendation differs from the rule engine's,
            # prefer the rule engine (it enforces configured limits).
            if investigation.recommended_action != action:
                logger.info(
                    "Rule engine overrode AI recommendation: AI=%s, Rule=%s",
                    investigation.recommended_action, action,
                )

        return investigation

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _action_exceeds_max(action: ActionType, max_action: ActionType) -> bool:
        """Check if an action is more severe than the max allowed.

        Severity ordering (ascending):
        no_action < warn < timeout < untimeout < delete_message < purge <
        add_muted_role < remove_muted_role < add_role < remove_role <
        slowmode < lock_channel < unlock_channel < kick < temp_ban < ban <
        unban < escalate_human < undo
        """
        severity_order = {
            ActionType.NO_ACTION: 0,
            ActionType.WARN: 1,
            ActionType.UNTIMEOUT: 2,
            ActionType.REMOVE_MUTED_ROLE: 2,
            ActionType.REMOVE_ROLE: 2,
            ActionType.DELETE_MESSAGE: 3,
            ActionType.ADD_MUTED_ROLE: 3,
            ActionType.ADD_ROLE: 3,
            ActionType.PURGE: 4,
            ActionType.SLOWMODE: 4,
            ActionType.UNLOCK_CHANNEL: 4,
            ActionType.LOCK_CHANNEL: 5,
            ActionType.TIMEOUT: 6,
            ActionType.KICK: 7,
            ActionType.TEMP_BAN: 8,
            ActionType.BAN: 9,
            ActionType.UNBAN: 9,
            ActionType.ESCALATE_HUMAN: 10,
            ActionType.UNDO: 10,
        }
        return severity_order.get(action, 0) > severity_order.get(max_action, 0)

    @staticmethod
    def _default_duration_for_action(
        action: ActionType, config: GuildConfig
    ) -> Optional[int]:
        """Get a default duration for a timed action."""
        if action == ActionType.TIMEOUT:
            return config.default_timeout_seconds
        if action == ActionType.TEMP_BAN:
            return 604800  # 7 days
        if action == ActionType.SLOWMODE:
            return 10  # 10 seconds
        return None
