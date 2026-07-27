"""
Unit tests for the rule engine.

Tests rule matching, punishment matrix escalation, and action validation
against configured limits.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ..config import GuildConfig, PunishmentMatrix
from ..database import Database
from ..models import ActionType, Rule, SeverityLevel
from ..rule_engine import RuleEngine


def _make_rule(name: str, severity: SeverityLevel, max_auto: ActionType = ActionType.TIMEOUT) -> Rule:
    return Rule(
        name=name,
        description=f"Test rule: {name}",
        severity=severity,
        suggested_action=ActionType.WARN,
        max_automatic_action=max_auto,
    )


class TestRuleMatching:
    def test_exact_name_match(self):
        rules = [_make_rule("spam", SeverityLevel.MINOR)]
        result = RuleEngine.find_rule("user is spamming", rules)
        assert result is not None
        assert result.name == "spam"

    def test_no_match(self):
        rules = [_make_rule("spam", SeverityLevel.MINOR)]
        result = RuleEngine.find_rule("user is being friendly", rules)
        assert result is None

    def test_empty_description(self):
        rules = [_make_rule("spam", SeverityLevel.MINOR)]
        assert RuleEngine.find_rule("", rules) is None


class TestPunishmentMatrix:
    def test_first_minor_offense(self):
        matrix = PunishmentMatrix()
        action, duration = matrix.get_punishment(SeverityLevel.MINOR, 1)
        assert action == ActionType.WARN
        assert duration is None

    def test_second_minor_offense(self):
        matrix = PunishmentMatrix()
        action, duration = matrix.get_punishment(SeverityLevel.MINOR, 2)
        assert action == ActionType.TIMEOUT
        assert duration == 600

    def test_third_minor_offense(self):
        matrix = PunishmentMatrix()
        action, duration = matrix.get_punishment(SeverityLevel.MINOR, 3)
        assert action == ActionType.TIMEOUT
        assert duration == 3600

    def test_critical_first_offense(self):
        matrix = PunishmentMatrix()
        action, duration = matrix.get_punishment(SeverityLevel.CRITICAL, 1)
        assert action == ActionType.TEMP_BAN
        assert duration == 604800

    def test_high_offense_count_clamps(self):
        """Offense count beyond the ladder returns the last (most severe) entry."""
        matrix = PunishmentMatrix()
        action, _ = matrix.get_punishment(SeverityLevel.MINOR, 100)
        assert action == ActionType.KICK

    def test_none_severity(self):
        matrix = PunishmentMatrix()
        action, duration = matrix.get_punishment(SeverityLevel.NONE, 1)
        assert action == ActionType.WARN


class TestActionValidation:
    def test_ban_for_minor_rejected(self):
        engine = RuleEngine(MagicMock())
        rule = _make_rule("spam", SeverityLevel.MINOR)
        valid, _ = engine.validate_action_against_rules(
            ActionType.BAN, SeverityLevel.MINOR, rule, GuildConfig(),
        )
        assert not valid

    def test_warn_always_valid(self):
        engine = RuleEngine(MagicMock())
        valid, _ = engine.validate_action_against_rules(
            ActionType.WARN, SeverityLevel.MINOR, None, GuildConfig(),
        )
        assert valid

    def test_kick_for_minor_rejected(self):
        engine = RuleEngine(MagicMock())
        valid, _ = engine.validate_action_against_rules(
            ActionType.KICK, SeverityLevel.MINOR, None, GuildConfig(),
        )
        assert not valid

    def test_kick_for_moderate_allowed(self):
        engine = RuleEngine(MagicMock())
        valid, _ = engine.validate_action_against_rules(
            ActionType.KICK, SeverityLevel.MODERATE, None, GuildConfig(),
        )
        assert valid

    def test_action_exceeds_rule_max(self):
        engine = RuleEngine(MagicMock())
        rule = _make_rule("spam", SeverityLevel.MINOR, max_auto=ActionType.WARN)
        valid, _ = engine.validate_action_against_rules(
            ActionType.TIMEOUT, SeverityLevel.MINOR, rule, GuildConfig(),
        )
        assert not valid  # timeout exceeds warn max


class TestRecommendedPunishment:
    @pytest.mark.asyncio
    async def test_first_offense_no_prior(self):
        db = AsyncMock(spec=Database)
        db.count_active_infractions = AsyncMock(return_value=0)
        engine = RuleEngine(db)
        rule = _make_rule("spam", SeverityLevel.MINOR)
        config = GuildConfig()
        action, duration, human_review = await engine.get_recommended_punishment(
            guild_id=100, target_id=200, severity=SeverityLevel.MINOR,
            rule=rule, config=config,
        )
        assert action == ActionType.WARN
        assert duration is None
        assert not human_review

    @pytest.mark.asyncio
    async def test_repeat_offense_escalates(self):
        db = AsyncMock(spec=Database)
        db.count_active_infractions = AsyncMock(return_value=2)  # 2 prior → 3rd offense
        engine = RuleEngine(db)
        rule = _make_rule("spam", SeverityLevel.MINOR)
        config = GuildConfig()
        action, duration, _ = await engine.get_recommended_punishment(
            guild_id=100, target_id=200, severity=SeverityLevel.MINOR,
            rule=rule, config=config,
        )
        # 3rd minor offense = 1 hour timeout.
        assert action == ActionType.TIMEOUT
        assert duration == 3600

    @pytest.mark.asyncio
    async def test_critical_requires_human_review(self):
        db = AsyncMock(spec=Database)
        db.count_active_infractions = AsyncMock(return_value=0)
        engine = RuleEngine(db)
        rule = _make_rule("threats", SeverityLevel.CRITICAL)
        config = GuildConfig()
        _, _, human_review = await engine.get_recommended_punishment(
            guild_id=100, target_id=200, severity=SeverityLevel.CRITICAL,
            rule=rule, config=config,
        )
        assert human_review
