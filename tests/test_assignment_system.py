"""人员交流与赛事指派系统的规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.exchange.errors import (
    AuthorizationError,
    ConflictStateError,
    DomainError,
    IneligibleError,
)
from src.exchange.model import (
    Certificate,
    Language,
    Relation,
    Visa,
    material,
)
from src.exchange.policy import (
    CERT_EXPIRED,
    DISCIPLINE_NOT_LISTED,
    LANGUAGE_MISSING,
    LEVEL_INSUFFICIENT,
    REGION_NOT_ALLOWED,
    REST_INTERVAL,
    SLOT_OVERLAP,
    TEAM_RELATION_CONFLICT,
    TRAVEL_QUOTA_EXHAUSTED,
    VISA_MISSING,
)
from src.exchange.store import EventStore
from src.exchange.system import AssignmentSystem, assoc_actor, person_actor

SEC = "secretariat"
HKG = assoc_actor("A-HKG")
JPN = assoc_actor("A-JPN")
KOR = assoc_actor("A-KOR")


class Clock:
    """固定可拨的时钟，便于验证"决定时点有效资料"。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


def good_material(revision: int = 1, region: str = "EAST", **overrides) -> object:
    params = dict(
        disciplines=["fb"],
        certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2027, 12, 31))],
        languages=[Language("zh", "B2"), Language("en", "C1")],
        visas=[Visa("HK", date(2027, 12, 31))],
    )
    params.update(overrides)
    return material(revision, region, **params)


class AssignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(datetime(2026, 10, 2, 12, 0))
        self.sys = AssignmentSystem(clock=self.clock)
        self.sys.register_association(SEC, "A-HKG", "香港协会")
        self.sys.register_association(SEC, "A-JPN", "日本协会")
        self.sys.register_association(SEC, "A-KOR", "韩国协会")
        self.sys.register_event(SEC, "EV1", "A-HKG", "HK", "亚洲交流赛")
        self.slots = [
            ("S1", datetime(2026, 11, 1, 9, 0), datetime(2026, 11, 1, 12, 0)),
            ("S2", datetime(2026, 11, 2, 9, 0), datetime(2026, 11, 2, 12, 0)),
        ]
        self.sys.submit_position(
            HKG, "POS1", "EV1", "fb", required_level=3,
            required_languages={"zh"}, slots=self.slots,
            quotas={"EAST": 2, "SE": 1}, participating_teams={"T-A", "T-B"},
        )

    def register(self, person_id: str = "P1", assoc=JPN, mat=None) -> None:
        self.sys.register_person(
            assoc, person_id, "A-JPN" if assoc == JPN else "A-KOR",
            f"官方-{person_id}", mat or good_material(),
        )

    def propose_issue_accept(self, person_id: str = "P1", position_id: str = "POS1") -> str:
        inv = self.sys.propose(SEC, position_id, person_id)
        self.sys.issue(SEC, inv)
        self.sys.respond(person_actor(person_id), inv, "accepted")
        return inv

    # ---------------------------------------------------------- 材料与授权

    def test_material_must_be_revised_strictly_incrementally(self) -> None:
        self.register()
        with self.assertRaisesRegex(ConflictStateError, "严格递增"):
            self.sys.revise_material(JPN, "P1", good_material(1))
        self.sys.revise_material(JPN, "P1", good_material(2))

    def test_association_can_only_revise_own_people(self) -> None:
        self.register()
        with self.assertRaises(AuthorizationError):
            self.sys.revise_material(KOR, "P1", good_material(2))

    def test_only_host_association_may_submit_positions(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.sys.submit_position(
                JPN, "POS-X", "EV1", "fb", 3, {"zh"}, self.slots,
                quotas={"EAST": 1}, participating_teams={"T-A"},
            )

    def test_association_cannot_register_or_self_approve_exceptions(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.sys.register_person(SEC, "P9", "A-JPN", "x", good_material())
        self.register()
        # 低等级证书 → 需要等级例外
        self.sys.revise_material(
            JPN, "P1",
            good_material(2, certificates=[Certificate("C1", "fb", 2, date(2025, 1, 1), date(2027, 12, 31))]),
        )
        inv = self.sys.propose(SEC, "POS1", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(LEVEL_INSUFFICIENT, codes)
        self.sys.request_exception(JPN, inv, "R1", {LEVEL_INSUFFICIENT}, "区域决赛经验丰富")
        # 协会无权裁决例外，更不能批准本方人员
        with self.assertRaises(AuthorizationError):
            self.sys.decide_exception(JPN, "R1", True)
        with self.assertRaises(AuthorizationError):
            self.sys.decide_exception(person_actor("P1"), "R1", True)
        # 秘书处批准后方可发出
        self.sys.decide_exception(SEC, "R1", True)
        self.sys.issue(SEC, inv)

    def test_rejected_exception_keeps_invitation_blocked(self) -> None:
        self.register()
        self.sys.revise_material(
            JPN, "P1",
            good_material(2, certificates=[Certificate("C1", "fb", 2, date(2025, 1, 1), date(2027, 12, 31))]),
        )
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.request_exception(JPN, inv, "R1", {LEVEL_INSUFFICIENT}, "理由")
        self.sys.decide_exception(SEC, "R1", False)
        with self.assertRaises(IneligibleError):
            self.sys.issue(SEC, inv)

    # ---------------------------------------------------------- 资格四类检查

    def test_certificate_must_be_valid_at_decision_time(self) -> None:
        self.register(mat=good_material(
            certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2026, 9, 30))]))
        inv = self.sys.propose(SEC, "POS1", "P1")
        with self.assertRaises(IneligibleError) as ctx:
            self.sys.issue(SEC, inv)
        self.assertIn(CERT_EXPIRED, [c for c, _ in ctx.exception.failures])

    def test_certificate_expiring_mid_event_blocks_issue(self) -> None:
        # 决定时有效，但 11/2 履责末日已过期
        self.register(mat=good_material(
            certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2026, 11, 1))]))
        inv = self.sys.propose(SEC, "POS1", "P1")
        with self.assertRaises(IneligibleError):
            self.sys.issue(SEC, inv)

    def test_level_language_visa_discipline_region_quota_checks(self) -> None:
        self.register(mat=material(
            1, "WEST",
            disciplines=["fb", "vb"],
            certificates=[Certificate("C1", "fb", 1, date(2025, 1, 1), date(2027, 12, 31))],
            languages=[Language("en", "C1")],
        ))
        inv = self.sys.propose(SEC, "POS1", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(LANGUAGE_MISSING, codes)
        self.assertIn(VISA_MISSING, codes)
        self.assertIn(TRAVEL_QUOTA_EXHAUSTED, codes)  # WEST 无名额
        # 等级不匹配（岗位要 3 级）
        self.assertIn(LEVEL_INSUFFICIENT, codes)

        # 未申报可服务项目时单独触发
        self.sys.revise_material(
            JPN, "P1",
            material(2, "WEST", disciplines=["vb"],
                     certificates=[Certificate("C1", "vb", 1, date(2025, 1, 1), date(2027, 12, 31))],
                     languages=[Language("en", "C1")]),
        )
        self.assertIn(DISCIPLINE_NOT_LISTED, {c for c, _ in self.sys.open_failures(inv)})

    def test_allowed_regions_filter(self) -> None:
        self.sys.submit_position(
            HKG, "POS-SEA", "EV1", "fb", 3, {"zh"}, self.slots,
            quotas={"EAST": 1}, participating_teams={"T-A"},
            allowed_regions={"SE"},
        )
        self.register()
        inv = self.sys.propose(SEC, "POS-SEA", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(REGION_NOT_ALLOWED, codes)

    def test_team_relation_conflict_detected(self) -> None:
        self.register(mat=good_material(relations=[Relation("T-A", "同一注册单位")]))
        inv = self.sys.propose(SEC, "POS1", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(TEAM_RELATION_CONFLICT, codes)

    def test_double_assignment_overlap_detected_even_when_pending(self) -> None:
        self.register()
        self.register("P2", assoc=KOR)
        # P1 先接受同周末另一赛事岗位
        self.sys.submit_position(
            HKG, "POS2", "EV1", "fb", 3, {"zh"},
            [("X1", datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 11, 0))],
            quotas={"EAST": 2}, participating_teams={"T-C"},
        )
        other = self.sys.propose(SEC, "POS2", "P1")
        self.sys.issue(SEC, other)
        self.sys.respond(person_actor("P1"), other, "accepted")
        inv = self.sys.propose(SEC, "POS1", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(SLOT_OVERLAP, codes)

    def test_rest_interval_enforced_between_back_to_back_slots(self) -> None:
        self.register()
        # 前一场 11/1 12:00 结束，新场 11/1 18:00 开始，仅 6 小时间隔
        self.sys.submit_position(
            HKG, "POS2", "EV1", "fb", 3, {"zh"},
            [("X1", datetime(2026, 11, 1, 6, 0), datetime(2026, 11, 1, 12, 0))],
            quotas={"EAST": 2}, participating_teams={"T-C"},
        )
        other = self.sys.propose(SEC, "POS2", "P1")
        self.sys.issue(SEC, other)
        self.sys.respond(person_actor("P1"), other, "accepted")
        self.sys.submit_position(
            HKG, "POS3", "EV1", "fb", 3, {"zh"},
            [("Y1", datetime(2026, 11, 1, 18, 0), datetime(2026, 11, 1, 21, 0))],
            quotas={"EAST": 2}, participating_teams={"T-D"},
        )
        inv = self.sys.propose(SEC, "POS3", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.assertIn(REST_INTERVAL, codes)

    def test_recheck_after_material_revision_releases_no_events(self) -> None:
        # 修订材料不会改写历史邀请；待办里会出现复核项
        self.register()
        inv = self.propose_issue_accept()
        self.sys.revise_material(
            JPN, "P1",
            good_material(2, languages=[Language("fr", "C1")]),  # 丢掉中文
        )
        todos = {t["type"] for t in self.sys.todos()}
        self.assertIn("assignment_recheck", todos)

    def test_issue_uses_material_valid_at_issue_time_not_proposal_time(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")  # 提案时合格
        self.sys.revise_material(
            JPN, "P1",
            good_material(2, certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2026, 9, 1))]),
        )
        # 发出时按当时有效资料重新评估 → 证书已失效，阻止发出
        with self.assertRaises(IneligibleError) as ctx:
            self.sys.issue(SEC, inv)
        self.assertIn(CERT_EXPIRED, [c for c, _ in ctx.exception.failures])

    def test_respond_after_unanswered_release_is_blocked(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.issue(SEC, inv)
        # 赛程调整释放了尚未答复的安排
        self.sys.adjust_schedule(HKG, "POS1", [
            ("S1", datetime(2026, 11, 5, 9, 0), datetime(2026, 11, 5, 12, 0)),
            ("S2", datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 6, 12, 0)),
        ])
        with self.assertRaisesRegex(ConflictStateError, "已被释放"):
            self.sys.respond(person_actor("P1"), inv, "accepted")

    def test_propose_is_idempotent_while_invitation_in_flight(self) -> None:
        self.register()
        first = self.sys.propose(SEC, "POS1", "P1")
        second = self.sys.propose(SEC, "POS1", "P1")
        self.assertEqual(first, second)

    def test_organizer_view_shows_previous_official_completion_after_substitution(self) -> None:
        self.register()
        inv = self.propose_issue_accept()
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})
        self.sys.withdraw(person_actor("P1"), inv)
        self.register("P2", assoc=KOR)
        sub = self.sys.propose_substitute(SEC, "POS1", "P2", inv)
        self.sys.issue_substitute(SEC, sub, inv)
        self.sys.respond(person_actor("P2"), sub, "accepted")
        self.clock.set(datetime(2026, 11, 2, 13, 0))
        self.sys.mark_duty_completed(SEC, sub, "S2", {"grade": "B"})
        view = self.sys.organizer_view(HKG, "POS1")
        current = view["officials"][0]
        self.assertEqual(current["completed_slots"], ["S2"])
        self.assertEqual(current["slots"][0]["slot_id"], "S2")  # 替补只见自己承接的 S2
        previous = view["completed_by_previous_officials"]
        self.assertEqual(previous, [{"slot_id": "S1", "person_code": "官方-P1",
                                     "evaluation": {"grade": "A"}}])

    # ---------------------------------------------------------- 答复与暂停

    def test_duplicate_confirmation_returns_original_decision(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.issue(SEC, inv)
        first = self.sys.respond(person_actor("P1"), inv, "accepted")
        second = self.sys.respond(person_actor("P1"), inv, "accepted")
        self.assertTrue(second.idempotent)
        self.assertEqual(second.decided_at, first.decided_at)
        self.assertEqual(second.decided_by, first.decided_by)

    def test_contradictory_responses_pause_position(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.issue(SEC, inv)
        self.sys.respond(person_actor("P1"), inv, "accepted")
        with self.assertRaisesRegex(ConflictStateError, "矛盾"):
            self.sys.respond(person_actor("P1"), inv, "declined")
        self.assertEqual(self.sys.positions["POS1"].status, "paused")
        # 暂停期间不能发新邀请
        self.register("P2", assoc=KOR)
        with self.assertRaises(ConflictStateError):
            self.sys.propose(SEC, "POS1", "P2")
        # 秘书处维持原决定 → 岗位恢复
        self.sys.resolve_contradiction(SEC, inv, uphold="first")
        self.assertEqual(self.sys.positions["POS1"].status, "active")
        self.assertEqual(self.sys.invitations[inv].status, "accepted")

    def test_contradiction_resolved_latest_releases_unfulfilled_only(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.issue(SEC, inv)
        self.sys.respond(person_actor("P1"), inv, "accepted")
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})
        # 先接受后改口拒绝
        with self.assertRaises(ConflictStateError):
            self.sys.respond(person_actor("P1"), inv, "declined")
        self.sys.resolve_contradiction(SEC, inv, uphold="latest")
        state = self.sys.invitations[inv]
        self.assertEqual(state.status, "released")
        self.assertEqual(state.released_slots, ["S2"])  # 已完成的 S1 不释放
        self.assertIn("S1", state.completed)

    # ---------------------------------------------------------- 退出与赛程

    def test_withdrawal_releases_only_unfulfilled_and_keeps_evaluation(self) -> None:
        self.register()
        inv = self.propose_issue_accept()
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A", "notes": "准时"})
        self.sys.withdraw(person_actor("P1"), inv)
        state = self.sys.invitations[inv]
        self.assertEqual(state.status, "released")
        self.assertEqual(state.released_slots, ["S2"])
        # 已完成评价保留发出邀请时的资格依据（修订号 1）
        self.assertEqual(state.completed["S1"]["basis"]["material_revision"], 1)
        self.assertEqual(state.completed["S1"]["evaluation"]["grade"], "A")

    def test_completed_evaluation_keeps_original_basis_after_revision(self) -> None:
        self.register()
        inv = self.propose_issue_accept()
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "B"})
        # 赛后协会把证书降级修订：已完成执裁仍以修订 1 的依据解释
        self.sys.revise_material(
            JPN, "P1",
            good_material(2, certificates=[Certificate("C1", "fb", 1, date(2025, 1, 1), date(2027, 12, 31))]),
        )
        explanation = self.sys.explain(inv)
        done = explanation["completed_duties"][0]
        self.assertEqual(done["basis"]["material_revision"], 1)
        self.assertEqual(done["basis"]["certificate"]["level"], 3)

    def test_schedule_adjustment_releases_only_unfulfilled(self) -> None:
        self.register()
        inv = self.propose_issue_accept()
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})
        # S2 改期到 11/3
        self.sys.adjust_schedule(
            HKG, "POS1",
            [("S1", datetime(2026, 11, 1, 9, 0), datetime(2026, 11, 1, 12, 0)),
             ("S2", datetime(2026, 11, 3, 9, 0), datetime(2026, 11, 3, 12, 0))],
        )
        state = self.sys.invitations[inv]
        self.assertEqual(state.status, "released")
        self.assertEqual(state.released_slots, ["S2"])
        self.assertIn("S1", state.completed)

    # ---------------------------------------------------------- 替补与责任保留

    def test_substitute_covers_only_remaining_slots_and_retains_records(self) -> None:
        self.register()
        inv = self.propose_issue_accept()
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})
        self.sys.withdraw(person_actor("P1"), inv)

        self.register("P2", assoc=KOR)
        sub = self.sys.propose_substitute(SEC, "POS1", "P2", inv)
        self.sys.issue_substitute(SEC, sub, inv)
        self.sys.respond(person_actor("P2"), sub, "accepted")
        # 替补只承接 S2
        self.assertEqual(self.sys.invitations[sub].covering_slots, ["S2"])
        self.clock.set(datetime(2026, 11, 2, 13, 0))
        self.sys.mark_duty_completed(SEC, sub, "S2", {"grade": "B"})
        explanation = self.sys.explain(sub)
        self.assertEqual(explanation["replaces"], inv)
        retained_ids = {r["invitation_id"] for r in explanation["retained_responsibility_records"]}
        self.assertIn(inv, retained_ids)  # 前任 S1 的责任记录随岗位保留
        prior_record = next(r for r in explanation["retained_responsibility_records"] if r["slot_id"] == "S1")
        self.assertEqual(prior_record["evaluation"], {"grade": "A"})

    def test_contradiction_upholding_latest_acceptance_restores_assignment(self) -> None:
        self.register()
        inv = self.sys.propose(SEC, "POS1", "P1")
        self.sys.issue(SEC, inv)
        # 本人先拒绝、协会随后代为确认接受 → 矛盾暂停
        self.sys.respond(person_actor("P1"), inv, "declined")
        with self.assertRaises(ConflictStateError):
            self.sys.respond(JPN, inv, "accepted")
        self.sys.resolve_contradiction(SEC, inv, uphold="latest")
        state = self.sys.invitations[inv]
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.released_slots, [])
        # 恢复后可正常录入执裁
        self.clock.set(datetime(2026, 11, 1, 13, 0))
        self.sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})

    def test_visa_expired_todo(self) -> None:
        self.register(mat=good_material(visas=[Visa("HK", date(2026, 9, 1))]))
        self.assertIn("visa_expired", {t["type"] for t in self.sys.todos()})

    # ---------------------------------------------------------- 持续待办

    def test_todos_cover_expiring_cert_visa_replacement_and_appeal(self) -> None:
        from src.exchange.policy import (
            CERT_EXPIRES_BEFORE_DUTY,
            VISA_EXPIRES_BEFORE_DUTY,
        )
        self.register(mat=good_material(
            certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2026, 10, 20))],
            visas=[Visa("HK", date(2026, 10, 25))],
        ))
        # 证书/签证在履责期前到期：秘书处按例外批准后发出，随后本人拒绝 → 替补待办
        inv = self.sys.propose(SEC, "POS1", "P1")
        codes = {c for c, _ in self.sys.open_failures(inv)}
        self.sys.request_exception(
            JPN, inv, "R1",
            codes & {CERT_EXPIRES_BEFORE_DUTY, VISA_EXPIRES_BEFORE_DUTY},
            "证件续办中，赛前补齐",
        )
        self.sys.decide_exception(SEC, "R1", True)
        self.sys.issue(SEC, inv)
        self.sys.respond(person_actor("P1"), inv, "declined")
        # 申诉
        self.sys.file_appeal(person_actor("P1"), "AP1", inv, "P1", "拒绝原因有异议")
        todos = self.sys.todos()
        types = {t["type"] for t in todos}
        self.assertIn("certificate_expiring", types)
        self.assertIn("visa_expiring", types)
        self.assertIn("replacement_needed", types)
        self.assertIn("appeal_open", types)
        # 秘书处裁决后申诉待办消失
        self.sys.rule_appeal(SEC, "AP1", "维持原决定，建议补充材料后重报")
        self.assertNotIn("appeal_open", {t["type"] for t in self.sys.todos()})

    def test_expired_certificate_appears_as_expired_todo(self) -> None:
        self.register(mat=good_material(
            certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2026, 9, 1))]))
        self.assertIn("certificate_expired", {t["type"] for t in self.sys.todos()})

    # ---------------------------------------------------------- 最小披露

    def test_organizer_view_contains_only_duty_information(self) -> None:
        self.register(mat=good_material(relations=[Relation("T-X", "亲属")]))
        # 秘书处就语言以外项目批例外使邀请可发（这里直接构造无关系材料另注册一人）
        self.register("P2", assoc=KOR)
        inv = self.propose_issue_accept("P2")
        view = self.sys.organizer_view(HKG, "POS1")
        official = view["officials"][0]
        allowed = {"person_code", "discipline", "cert_level", "cert_valid_to",
                   "duty_languages", "home_region", "slots", "completed_slots",
                   "has_secretariat_exception"}
        self.assertEqual(set(official), allowed)
        self.assertNotIn("relations", str(view))
        self.assertNotIn("T-X", str(view))

    def test_other_association_cannot_read_organizer_view(self) -> None:
        self.register()
        self.propose_issue_accept()
        with self.assertRaises(AuthorizationError):
            self.sys.organizer_view(JPN, "POS1")

    # ---------------------------------------------------------- 持久化与解释

    def test_state_recovers_after_interruption_and_decisions_are_explainable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            sys = AssignmentSystem(store=store, clock=self.clock)
            sys.register_association(SEC, "A-HKG", "香港协会")
            sys.register_association(SEC, "A-JPN", "日本协会")
            sys.register_event(SEC, "EV1", "A-HKG", "HK", "亚洲交流赛")
            sys.register_person(JPN, "P1", "A-JPN", "官方-P1", good_material(
                certificates=[Certificate("C1", "fb", 2, date(2025, 1, 1), date(2027, 12, 31))]))
            sys.submit_position(
                HKG, "POS1", "EV1", "fb", 3, {"zh"}, self.slots,
                quotas={"EAST": 2}, participating_teams={"T-A"},
            )
            inv = sys.propose(SEC, "POS1", "P1")
            sys.request_exception(JPN, inv, "R1", {LEVEL_INSUFFICIENT}, "经验替代")
            sys.decide_exception(SEC, "R1", True)
            sys.issue(SEC, inv)
            sys.respond(person_actor("P1"), inv, "accepted")
            self.clock.set(datetime(2026, 11, 1, 13, 0))
            sys.mark_duty_completed(SEC, inv, "S1", {"grade": "A"})
            sys.withdraw(person_actor("P1"), inv)

            # 服务中断：用同一事件流重建系统
            recovered = AssignmentSystem(EventStore(path), clock=self.clock)
            state = recovered.invitations[inv]
            self.assertEqual(state.status, "released")
            self.assertEqual(state.released_slots, ["S2"])
            explanation = recovered.explain(inv)
            self.assertEqual(explanation["qualification_basis"]["material_revision"], 1)
            self.assertEqual(explanation["qualification_basis"]["waived_codes"], [LEVEL_INSUFFICIENT])
            approval = explanation["exception_requests"][0]
            self.assertEqual(approval["approver"], SEC)  # 谁批准了例外
            self.assertEqual(approval["requested_by"], JPN)
            self.assertEqual(explanation["release"]["reason"], "withdrawn")
            self.assertEqual(explanation["completed_duties"][0]["evaluation"], {"grade": "A"})
            self.assertIn("replacement_needed", {t["type"] for t in recovered.todos()})

    def test_corrupted_event_sequence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text('{"seq": 3, "type": "X", "at": "2026-01-01T00:00:00", "actor": "x", "payload": {}}\n',
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "序号不连续"):
                AssignmentSystem(EventStore(path))


if __name__ == "__main__":
    unittest.main()
