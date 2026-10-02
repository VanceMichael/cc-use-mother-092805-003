"""资格核验引擎规则测试：休息间隔、地域签证、岗位重叠、利益关系、名额等。"""

import unittest
from datetime import date, datetime

from src import rules
from src.model import (
    CertificationLevel,
    Event,
    InvitationStatus,
    LanguageLevel,
    OfficialProfile,
    RelationKind,
    RelationRecord,
)
from tests.helpers import make_position


def context_for(
    profile: OfficialProfile,
    position,
    *,
    event=None,
    assignments=(),
    quota_held: int = 0,
    exceptions=(),
    association_id: str = "ASSN-A",
    official_id: str = "OFF-A1",
    as_of: datetime = datetime(2026, 9, 1, 9),
    rest_hours: int = 24,
):
    return rules.EvaluationContext(
        official_id=official_id,
        association_id=association_id,
        profile=profile,
        profile_version=1,
        profile_revised_on=date(2026, 1, 1),
        position=position,
        event=event or Event(id="E1", name="赛", host_association_id="H", territory="SGP",
                             participating_team_ids={"TEAM-A"},
                             travel_quotas={"ASSN-A": 2}),
        as_of=as_of,
        assignments=list(assignments),
        quota_held=quota_held,
        exceptions=list(exceptions),
        rest_hours=rest_hours,
    )


def assignment(position_id, start, end, status=InvitationStatus.CONFIRMED, invitation_id=None):
    from src.model import ServiceWindow

    return rules.AssignmentView(
        invitation_id=invitation_id or f"inv-{position_id}",
        position_id=position_id,
        window=ServiceWindow(start, end),
        status=status,
    )


class CertificationRuleTest(unittest.TestCase):
    def test_level_and_validity_checked_on_service_day(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(level=CertificationLevel.L2)
        position = make_position(required_level=CertificationLevel.L3)
        snapshot = rules.evaluate(context_for(profile, position))
        self.assertIn("CERT_LEVEL_VALID", [f.split(":")[0] for f in snapshot.failures])

    def test_certificate_expiring_before_event_fails_even_if_valid_at_issue(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(cert_until=date(2026, 11, 9))
        position = make_position(start=datetime(2026, 11, 10, 9))
        snapshot = rules.evaluate(
            context_for(profile, position, as_of=datetime(2026, 9, 1))
        )
        self.assertIn("CERT_LEVEL_VALID", [f.split(":")[0] for f in snapshot.failures])

    def test_perpetual_certificate_passes(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(cert_until=None)
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertTrue(snapshot.rule_results["CERT_LEVEL_VALID"])


class OverlapAndRestTest(unittest.TestCase):
    def test_overlapping_positions_are_rejected(self) -> None:
        from tests.helpers import make_profile

        other = assignment(
            "P2", datetime(2026, 11, 10, 14), datetime(2026, 11, 10, 22)
        )
        snapshot = rules.evaluate(
            context_for(make_profile(), make_position(), assignments=[other])
        )
        self.assertIn("POSITION_OVERLAP", [f.split(":")[0] for f in snapshot.failures])

    def test_rest_interval_too_short_is_rejected(self) -> None:
        from tests.helpers import make_profile

        # 前一岗位 11-10 18:00 结束，本岗位 11-11 12:00 开始：间隔 18 小时
        prior = assignment(
            "P2", datetime(2026, 11, 10, 9), datetime(2026, 11, 10, 18)
        )
        position = make_position(
            start=datetime(2026, 11, 11, 12), end=datetime(2026, 11, 11, 20)
        )
        snapshot = rules.evaluate(
            context_for(make_profile(), position, assignments=[prior])
        )
        self.assertIn("REST_INTERVAL", [f.split(":")[0] for f in snapshot.failures])

    def test_exactly_meeting_rest_interval_passes(self) -> None:
        from tests.helpers import make_profile

        prior = assignment(
            "P2", datetime(2026, 11, 9, 9), datetime(2026, 11, 9, 18)
        )
        # 前一岗位 11-09 18:00 结束，本岗位 11-10 18:00 开始：恰好 24 小时
        position = make_position(
            start=datetime(2026, 11, 10, 18), end=datetime(2026, 11, 10, 22)
        )
        snapshot = rules.evaluate(
            context_for(make_profile(), position, assignments=[prior])
        )
        self.assertTrue(snapshot.rule_results["REST_INTERVAL"])
        self.assertFalse(
            any(f.startswith("POSITION_OVERLAP") for f in snapshot.failures)
        )

    def test_cancelled_assignment_does_not_block(self) -> None:
        from tests.helpers import make_profile

        other = assignment(
            "P2", datetime(2026, 11, 10, 14), datetime(2026, 11, 10, 22),
            status=InvitationStatus.CANCELLED,
        )
        snapshot = rules.evaluate(
            context_for(make_profile(), make_position(), assignments=[other])
        )
        self.assertTrue(snapshot.rule_results["POSITION_OVERLAP"])


class TerritoryTest(unittest.TestCase):
    def test_missing_visa_fails(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(visas=())
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertIn("TERRITORY_VISA", [f.split(":")[0] for f in snapshot.failures])

    def test_expired_visa_fails(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(visas=(("SGP", date(2026, 11, 9)),))
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertIn("TERRITORY_VISA", [f.split(":")[0] for f in snapshot.failures])

    def test_restricted_territory_fails_even_with_visa(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(restricted=("SGP",))
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertIn("TERRITORY_VISA", [f.split(":")[0] for f in snapshot.failures])

    def test_home_territory_needs_no_visa(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(home_territory="SGP", visas=())
        snapshot = rules.evaluate(context_for(profile, make_position(territory="SGP")))
        self.assertTrue(snapshot.rule_results["TERRITORY_VISA"])


class TeamRelationTest(unittest.TestCase):
    def test_active_relation_to_participating_team_fails(self) -> None:
        from tests.helpers import make_profile

        relation = RelationRecord(RelationKind.COACH, "TEAM-A")
        profile = make_profile(relations=[relation])
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertIn("TEAM_RELATION", [f.split(":")[0] for f in snapshot.failures])

    def test_expired_relation_is_ignored(self) -> None:
        from tests.helpers import make_profile

        relation = RelationRecord(
            RelationKind.COACH, "TEAM-A", until=date(2026, 11, 9)
        )
        profile = make_profile(relations=[relation])
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertTrue(snapshot.rule_results["TEAM_RELATION"])

    def test_relation_to_non_participating_team_is_ignored(self) -> None:
        from tests.helpers import make_profile

        relation = RelationRecord(RelationKind.FAMILY, "TEAM-Z")
        profile = make_profile(relations=[relation])
        snapshot = rules.evaluate(
            context_for(
                profile,
                make_position(),
                event=Event(id="E1", name="赛", host_association_id="H",
                            territory="SGP", participating_team_ids={"TEAM-A"}),
            )
        )
        self.assertTrue(snapshot.rule_results["TEAM_RELATION"])


class QuotaAndLanguageTest(unittest.TestCase):
    def test_quota_exhaustion_fails(self) -> None:
        from tests.helpers import make_profile

        snapshot = rules.evaluate(
            context_for(make_profile(), make_position(), quota_held=2)
        )
        self.assertIn("TRAVEL_QUOTA", [f.split(":")[0] for f in snapshot.failures])

    def test_language_below_requirement_fails(self) -> None:
        from tests.helpers import make_profile

        profile = make_profile(languages=(("en", LanguageLevel.A2),))
        snapshot = rules.evaluate(context_for(profile, make_position()))
        self.assertIn("LANGUAGE_REQUIREMENT", [f.split(":")[0] for f in snapshot.failures])

    def test_no_language_requirement_passes(self) -> None:
        from tests.helpers import make_profile

        snapshot = rules.evaluate(
            context_for(make_profile(), make_position(required_language=None))
        )
        self.assertTrue(snapshot.rule_results["LANGUAGE_REQUIREMENT"])


class ExceptionCoverageTest(unittest.TestCase):
    def test_valid_exception_covers_only_its_rule(self) -> None:
        from tests.helpers import make_profile
        from src.model import RuleException

        profile = make_profile(visas=())  # 无签证
        grant = RuleException(
            id="exc1",
            rule_code="TERRITORY_VISA",
            official_id="OFF-A1",
            position_id="P1",
            reason="特案入境安排",
            granted_by="委员会",
            granted_by_association_id="ASSN-B",
            granted_on=datetime(2026, 8, 1),
        )
        snapshot = rules.evaluate(
            context_for(profile, make_position(), exceptions=[grant])
        )
        self.assertTrue(snapshot.eligible)
        self.assertEqual(snapshot.exception_ids, ["exc1"])

    def test_self_association_exception_is_invalid(self) -> None:
        from tests.helpers import make_profile
        from src.model import RuleException

        profile = make_profile(visas=())
        grant = RuleException(
            id="exc-self",
            rule_code="TERRITORY_VISA",
            official_id="OFF-A1",
            position_id="P1",
            reason="本协会自行批准",
            granted_by="甲协会主席",
            granted_by_association_id="ASSN-A",  # 与官员同协会
            granted_on=datetime(2026, 8, 1),
        )
        snapshot = rules.evaluate(
            context_for(profile, make_position(), exceptions=[grant])
        )
        self.assertFalse(snapshot.eligible)
        self.assertEqual(snapshot.exception_ids, [])

    def test_expired_exception_does_not_cover(self) -> None:
        from tests.helpers import make_profile
        from src.model import RuleException

        profile = make_profile(visas=())
        grant = RuleException(
            id="exc-expired",
            rule_code="TERRITORY_VISA",
            official_id="OFF-A1",
            position_id="P1",
            reason="过期特案",
            granted_by="委员会",
            granted_by_association_id="ASSN-B",
            granted_on=datetime(2026, 1, 1),
            expires_on=date(2026, 8, 31),
        )
        snapshot = rules.evaluate(
            context_for(profile, make_position(), exceptions=[grant],
                        as_of=datetime(2026, 9, 1))
        )
        self.assertFalse(snapshot.eligible)


if __name__ == "__main__":
    unittest.main()
