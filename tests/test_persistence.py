"""服务中断后恢复的持久化测试。"""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.model import (
    InvitationStatus,
    PositionStatus,
    TodoKind,
    TodoStatus,
)
from src.persistence import load_state, save_state
from tests.helpers import build_world


class PersistenceTest(unittest.TestCase):
    def _build_mid_flow(self):
        world = build_world()
        invitation = world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 9, 1))
        world.respond(invitation.id, True, at=datetime(2026, 9, 2))
        # 制造证书临期与婉拒替补两类持续待办
        from tests.helpers import make_profile

        world.submit_revision(
            "ASSN-B", "OFF-B1",
            make_profile(cert_until=date(2026, 10, 20)),
            date(2026, 6, 1),
        )
        world.sweep_renewal_todos(date(2026, 10, 1))
        return world, invitation

    def test_state_round_trip_preserves_decisions_versions_and_audit(self) -> None:
        world, invitation = self._build_mid_flow()
        seq_before = world._seq
        audit_before = len(world.audit_log)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(world, path)
            restored = load_state(path)

        self.assertEqual(restored._seq, seq_before)
        self.assertEqual(len(restored.audit_log), audit_before)

        restored_invitation = restored.invitations[invitation.id]
        self.assertEqual(restored_invitation.status, InvitationStatus.CONFIRMED)
        self.assertTrue(restored_invitation.snapshot.rule_results["CERT_LEVEL_VALID"])
        self.assertEqual(restored_invitation.snapshot.profile_version, 1)

        revision = restored.associations["ASSN-B"].latest_revision("OFF-B1")
        self.assertEqual(revision.profile.certifications[0].valid_until, date(2026, 10, 20))

        todos = restored.open_todos()
        self.assertTrue(any(t.kind == TodoKind.CERT_EXPIRY for t in todos))
        renewal = next(t for t in todos if t.kind == TodoKind.CERT_EXPIRY)
        self.assertIs(renewal.history[0][1], TodoStatus.OPEN)

    def test_business_resumes_after_restoration_with_continuous_audit(self) -> None:
        world, invitation = self._build_mid_flow()
        seq_before = world._seq

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(world, path)
            restored = load_state(path)

        # 恢复后继续处理：矛盾答复暂停岗位，审计序号连续
        restored.respond(invitation.id, False, at=datetime(2026, 9, 3))
        self.assertEqual(
            restored.events["E1"].positions["P1"].status, PositionStatus.PAUSED
        )
        self.assertEqual(restored._seq, seq_before + 2)  # 待办开立 + 矛盾答复

        # 仍可解释恢复前的指派
        explanation = restored.explain_assignment(invitation.id)
        self.assertEqual(explanation["status"], InvitationStatus.SUSPENDED.value)
        self.assertEqual(
            explanation["qualification_basis"]["profile_version"], 1
        )

    def test_replacement_chain_survives_restart(self) -> None:
        world = build_world()
        first = world.issue_invitation("OFF-A1", "P1", at=datetime(2026, 9, 1))
        world.respond(first.id, True, at=datetime(2026, 9, 2))
        world.withdraw(first.id, date(2026, 10, 1), "伤病")
        second = world.arrange_replacement(first.id, "OFF-B1", at=datetime(2026, 10, 2))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(world, path)
            restored = load_state(path)

        chain = restored.explain_assignment(second.id)["replacement_chain"]
        self.assertEqual(chain, [first.id, second.id])
        self.assertEqual(
            restored.invitations[first.id].released_reason[:11], "WITHDRAWAL:"
        )


if __name__ == "__main__":
    unittest.main()
