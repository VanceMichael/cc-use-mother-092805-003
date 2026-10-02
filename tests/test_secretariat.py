"""秘书处端到端流程测试。

覆盖：时点材料版本、邀请幂等、矛盾答复暂停、退出与赛程调整只释放未履行部分、
评价冻结资格依据、替补责任链、例外分权、申诉、持续待办、承办方最小视图、
审计可解释性。
"""

import unittest
from datetime import date, datetime

from src.model import (
    AppealStatus,
    CertificationLevel,
    InvitationStatus,
    PositionStatus,
    RelationKind,
    RelationRecord,
    ServiceWindow,
    TodoKind,
)
from src.secretariat import IneligibleError, SecretariatError
from tests.helpers import build_world, make_position, make_profile


class InvitationFlowTest(unittest.TestCase):
    def test_issue_and_confirm_freezes_basis_snapshot(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 9, 1))
        self.assertEqual(invitation.status, InvitationStatus.ISSUED)
        self.assertTrue(invitation.snapshot.eligible)
        self.assertEqual(invitation.snapshot.profile_version, 1)

        world.respond(invitation.id, True, at=datetime(2026, 9, 2), channel="邮件")
        self.assertEqual(invitation.status, InvitationStatus.CONFIRMED)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.HELD)

    def test_repeated_confirmation_returns_original_decision(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        responses_before = len(invitation.responses)
        decided_before = invitation.decided_at
        audit_before = len(world.audit_log)

        again = world.respond(invitation.id, True, at=datetime(2026, 9, 9), channel="电话")
        self.assertIs(again, invitation)
        self.assertEqual(len(invitation.responses), responses_before)
        self.assertEqual(invitation.decided_at, decided_before)
        self.assertEqual(len(world.audit_log), audit_before + 1)  # 仅留重放审计

    def test_contradictory_response_pauses_position_and_opens_todo(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        world.respond(invitation.id, False, at=datetime(2026, 9, 5))

        self.assertEqual(invitation.status, InvitationStatus.SUSPENDED)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.PAUSED)
        paused_todos = [t for t in world.open_todos() if t.kind == TodoKind.POSITION_PAUSED]
        self.assertEqual(len(paused_todos), 1)

        # 暂停期间继续答复不再改变状态
        world.respond(invitation.id, True, at=datetime(2026, 9, 6))
        self.assertEqual(invitation.status, InvitationStatus.SUSPENDED)

    def test_secretariat_resolves_pause_and_todo_closes(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        world.respond(invitation.id, False, at=datetime(2026, 9, 5))
        world.resolve_paused_position(
            "P1", uphold_accepted=True, by="秘书长", at=datetime(2026, 9, 7), note="以确认为准"
        )
        self.assertEqual(invitation.status, InvitationStatus.CONFIRMED)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.HELD)
        self.assertFalse(
            [t for t in world.open_todos() if t.kind == TodoKind.POSITION_PAUSED]
        )

    def test_decline_reopens_position_and_creates_replacement_todo(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, False, at=datetime(2026, 9, 3), note="档期冲突")
        self.assertEqual(invitation.status, InvitationStatus.DECLINED)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.OPEN)
        self.assertTrue(
            [t for t in world.open_todos() if t.kind == TodoKind.REPLACEMENT]
        )


class PointInTimeMaterialTest(unittest.TestCase):
    def test_invitation_uses_material_effective_at_that_time(self) -> None:
        world = build_world()
        # OFF-A1 第二版材料：证书在赛事日前到期，2026-11-20 才修订生效
        expiring = make_profile(cert_until=date(2026, 11, 9))
        world.submit_revision("ASSN-A", "OFF-A1", expiring, date(2026, 11, 1))
        self.assertEqual(
            world.associations["ASSN-A"].revision_effective_on("OFF-A1", date(2026, 10, 1)).version,
            1,
        )
        with self.assertRaises(IneligibleError):
            world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 11, 5))

        renewed = make_profile(cert_until=date(2028, 12, 31))
        world.submit_revision("ASSN-A", "OFF-A1", renewed, date(2026, 11, 20))
        invitation = world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 11, 21))
        self.assertEqual(invitation.snapshot.profile_version, 3)

    def test_association_cannot_revise_other_associations_officials(self) -> None:
        world = build_world()
        with self.assertRaisesRegex(SecretariatError, "本方人员"):
            world.submit_revision("ASSN-B", "OFF-A1", make_profile(), date(2026, 2, 1))


class ExceptionGovernanceTest(unittest.TestCase):
    def test_self_association_cannot_grant_exception(self) -> None:
        world = build_world()
        with self.assertRaisesRegex(SecretariatError, "不能独自批准"):
            world.grant_exception(
                "OFF-A1", "P1", "TEAM_RELATION", "本协会特批",
                granted_by="甲协会", granted_by_association_id="ASSN-A",
            )

    def test_secretariat_exception_allows_otherwise_ineligible_candidate(self) -> None:
        world = build_world()
        conflicted = make_profile(
            relations=[RelationRecord(RelationKind.COACH, "TEAM-A")]
        )
        world.submit_revision("ASSN-A", "OFF-A2", conflicted, date(2026, 2, 1), note="补报关系")
        with self.assertRaises(IneligibleError):
            world.issue_invitation("OFF-A2", "P1")

        world.grant_exception(
            "OFF-A2", "P1", "TEAM_RELATION", "委员会审议认为不存在临场影响",
            granted_by="资格委员会", granted_by_association_id="secretariat",
            granted_on=datetime(2026, 9, 1),
        )
        invitation = world.issue_invitation("OFF-A2", "P1", at=datetime(2026, 9, 2))
        self.assertEqual(len(invitation.snapshot.exception_ids), 1)
        explanation = world.explain_assignment(invitation.id)
        self.assertEqual(explanation["exceptions"][0]["granted_by"], "资格委员会")


class WithdrawalAndScheduleTest(unittest.TestCase):
    def test_withdrawal_releases_only_unfulfilled_and_keeps_replacement_todo(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.withdraw(first.id, date(2026, 10, 1), "伤病")

        self.assertEqual(first.status, InvitationStatus.CANCELLED)
        self.assertTrue(first.released_reason)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.OPEN)
        self.assertTrue(
            [t for t in world.open_todos()
             if t.kind == TodoKind.REPLACEMENT and t.ref_id == "P1"]
        )

    def test_schedule_change_releases_unfulfilled_but_keeps_fulfilled(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.fulfill(first.id, date(2026, 11, 10), "A", comment="完成执裁")

        released = world.adjust_schedule(
            "E1", "P1",
            ServiceWindow(datetime(2026, 12, 5, 9), datetime(2026, 12, 5, 18)),
            date(2026, 11, 1), note="决赛改期",
        )
        self.assertEqual(released, [])
        self.assertEqual(first.status, InvitationStatus.FULFILLED)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.FILLED)

    def test_withdrawal_after_fulfillment_is_rejected(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.fulfill(first.id, date(2026, 11, 10), "A")
        with self.assertRaisesRegex(SecretariatError, "已履职完成"):
            world.withdraw(first.id, date(2026, 11, 11), "事后退出")

    def test_completed_evaluation_keeps_original_basis(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        evaluation = world.fulfill(first.id, date(2026, 11, 10), "A", comment="良好")

        # 赛后协会把材料改得面目全非：评价依据不变
        world.submit_revision(
            "ASSN-A", "OFF-A1",
            make_profile(level=CertificationLevel.L1, cert_until=date(2026, 1, 1)),
            date(2026, 12, 1), note="赛后材料变动",
        )
        self.assertTrue(evaluation.locked)
        self.assertEqual(evaluation.basis_snapshot.profile_version, 1)
        self.assertTrue(evaluation.basis_snapshot.rule_results["CERT_LEVEL_VALID"])


class ReplacementChainTest(unittest.TestCase):
    def test_replacement_preserves_responsibility_chain(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.withdraw(first.id, date(2026, 10, 1), "伤病")

        second = world.arrange_replacement(first.id, "OFF-B1", at=datetime(2026, 10, 2))
        world.respond(second.id, True, at=datetime(2026, 10, 3))
        world.fulfill(second.id, date(2026, 11, 10), "B")

        explanation = world.explain_assignment(second.id)
        self.assertEqual(explanation["replacement_chain"], [first.id, second.id])
        self.assertEqual(
            explanation["responsibility_records_kept"]["prior_invitation"], first.id
        )
        self.assertEqual(first.replaced_by_invitation_id, second.id)
        # 替补待办已随确认解除
        self.assertFalse(
            [t for t in world.open_todos() if t.kind == TodoKind.REPLACEMENT]
        )

    def test_two_level_replacement_chain_is_explainable(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1")
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.withdraw(first.id, date(2026, 10, 1), "伤病")

        second = world.arrange_replacement(first.id, "OFF-B1", at=datetime(2026, 10, 2))
        world.respond(second.id, True, at=datetime(2026, 10, 3))
        world.withdraw(second.id, date(2026, 10, 10), "航班取消")

        third = world.arrange_replacement(second.id, "OFF-B2", at=datetime(2026, 10, 11))
        chain = world.explain_assignment(third.id)["replacement_chain"]
        self.assertEqual(chain, [first.id, second.id, third.id])


class OverlapAtSystemLevelTest(unittest.TestCase):
    def test_double_assignment_across_events_is_prevented(self) -> None:
        from src.model import Event

        world = build_world()
        second_event = Event(
            id="E2", name="另一赛事", host_association_id="HOST",
            territory="SGP", participating_team_ids=set(),
        )
        world.register_event(second_event)
        second_event.positions["P2"] = make_position(
            "P2", event_id="E2",
            start=datetime(2026, 11, 10, 14), end=datetime(2026, 11, 10, 22),
        )
        first = world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 9, 1))
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        with self.assertRaises(IneligibleError) as caught:
            world.issue_invitation("OFF-A1", "P2", at=datetime(2026, 9, 3))
        self.assertTrue(
            any("POSITION_OVERLAP" in f for f in caught.exception.failures)
        )


class AppealTest(unittest.TestCase):
    def test_appeal_holds_position_and_resolution_restores_state(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))

        appeal = world.file_appeal(
            "OFF-A1", "P1", "ASSN-A", "对利益关系认定有异议",
            date(2026, 9, 10), invitation_id=invitation.id,
        )
        self.assertEqual(world.events["E1"].positions["P1"].status,
                         PositionStatus.UNDER_APPEAL)
        self.assertTrue([t for t in world.open_todos() if t.kind == TodoKind.APPEAL])

        world.resolve_appeal(
            appeal.id, upheld=True, resolution="重新核验后异议成立",
            resolved_by="申诉委员会", resolved_on=date(2026, 9, 20),
        )
        self.assertEqual(appeal.status, AppealStatus.UPHELD)
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.HELD)
        self.assertFalse([t for t in world.open_todos() if t.kind == TodoKind.APPEAL])

    def test_release_during_appeal_is_not_revived_when_appeal_upheld(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        world.file_appeal(
            "OFF-A1", "P1", "ASSN-A", "异议", date(2026, 9, 10),
            invitation_id=invitation.id,
        )
        # 申诉审理期间候选人退出：邀请被释放，但岗位仍挂在申诉中
        world.withdraw(invitation.id, date(2026, 9, 15), "伤病")
        self.assertEqual(invitation.status, InvitationStatus.CANCELLED)

        appeal = next(iter(world.appeals.values()))
        world.resolve_appeal(
            appeal.id, upheld=True, resolution="申诉成立但安排已释放",
            resolved_by="申诉委员会", resolved_on=date(2026, 9, 20),
        )
        # 不能复活已释放的邀请：岗位应开放等待替补，而不是回到 HELD
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.OPEN)
        self.assertTrue(
            [t for t in world.open_todos() if t.kind == TodoKind.REPLACEMENT]
        )


class PausedPositionCleanupTest(unittest.TestCase):
    def test_withdraw_of_suspended_invitation_closes_pause_todo(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        world.respond(invitation.id, False, at=datetime(2026, 9, 5))
        self.assertTrue(
            [t for t in world.open_todos() if t.kind == TodoKind.POSITION_PAUSED]
        )
        world.withdraw(invitation.id, date(2026, 9, 8), "候选人明确退出")
        self.assertFalse(
            [t for t in world.open_todos() if t.kind == TodoKind.POSITION_PAUSED]
        )
        self.assertEqual(world.events["E1"].positions["P1"].status, PositionStatus.OPEN)
        self.assertTrue(
            [t for t in world.open_todos() if t.kind == TodoKind.REPLACEMENT]
        )


class RenewalTodoTest(unittest.TestCase):
    def test_cert_and_visa_expiry_open_todos_and_renewal_closes_them(self) -> None:
        world = build_world(cert_expiry_days=30)
        world.submit_revision(
            "ASSN-A", "OFF-A1",
            make_profile(
                cert_until=date(2026, 10, 20),
                visas=(("SGP", date(2026, 10, 25)),),
            ),
            date(2026, 6, 1),
        )
        opened = world.sweep_renewal_todos(date(2026, 10, 1))
        kinds = {todo.kind for todo in opened}
        self.assertEqual(kinds, {TodoKind.CERT_EXPIRY, TodoKind.VISA_EXPIRY})

        # 重复扫描不重复开立
        world.sweep_renewal_todos(date(2026, 10, 2))
        cert_todos = [t for t in world.todos.values() if t.kind == TodoKind.CERT_EXPIRY]
        self.assertEqual(len(cert_todos), 1)

        # 协会续期后旧待办自动解除
        world.submit_revision(
            "ASSN-A", "OFF-A1",
            make_profile(cert_until=date(2028, 1, 1), visas=(("SGP", date(2028, 1, 1)),)),
            date(2026, 10, 5), note="完成续期",
        )
        self.assertFalse(
            [t for t in world.open_todos()
             if t.ref_id == "OFF-A1" and t.kind in (TodoKind.CERT_EXPIRY, TodoKind.VISA_EXPIRY)]
        )


class HostViewTest(unittest.TestCase):
    def test_host_sees_only_duty_necessary_information(self) -> None:
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1")
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        view = world.host_event_view("E1")
        position_view = next(p for p in view["positions"] if p["position_id"] == "P1")
        official_view = position_view["official"]
        self.assertEqual(official_view["name"], "甲一")
        self.assertEqual(official_view["contact"], "甲一履职联系")
        self.assertEqual(set(official_view), {
            "official_id", "name", "contact", "serving_window",
            "qualification_basis_version", "exceptions_used",
        })
        # 不暴露关系清单、签证明细、其他岗位安排
        self.assertNotIn("relations", official_view)
        self.assertNotIn("visas", official_view)
