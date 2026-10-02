"""区域体育合作秘书处的核心业务流程。

把 :mod:`src.model` 的数据与 :mod:`src.rules` 的核验串成完整流程：

- 协会维护材料（可修订、版本递增），但**不能独自批准本方人员的例外**；
- 承办方提交赛事、岗位、时段、等级与差旅名额；
- 秘书处按“当时有效”的材料版本发出邀请，并把资格依据冻结进快照；
- 同一邀请重复确认返回原决定；答复矛盾则暂停该岗位；
- 退出或赛程调整只释放尚未履行的安排，已完成评价保留原资格依据；
- 证书到期、签证期限、替补、申诉、岗位暂停全部形成持续待办；
- 审计只追加，所有决定、例外授权、换人责任链均可回溯解释。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import rules
from .model import (
    Appeal,
    AppealStatus,
    Association,
    AuditEntry,
    EligibilitySnapshot,
    Event,
    Invitation,
    InvitationStatus,
    Official,
    PerformanceEvaluation,
    Position,
    PositionStatus,
    RuleException,
    Todo,
    TodoKind,
    TodoStatus,
    Withdrawal,
    new_id,
    now,
)


class SecretariatError(ValueError):
    """业务规则拒绝本次操作。"""


class IneligibleError(SecretariatError):
    def __init__(self, failures: list[str]):
        super().__init__("候选人不满足岗位要求：" + "；".join(failures))
        self.failures = failures


# 审计动作名集中在此，便于测试与文档引用
ACT_ISSUE = "INVITATION_ISSUED"
ACT_REPEAT = "INVITATION_REPLAYED"
ACT_CONFIRM = "INVITATION_CONFIRMED"
ACT_DECLINE = "INVITATION_DECLINED"
ACT_CONTRADICTION = "RESPONSE_CONTRADICTION"
ACT_PAUSE_RESOLVE = "POSITION_PAUSE_RESOLVED"
ACT_FULFILL = "INVITATION_FULFILLED"
ACT_WITHDRAW = "INVITATION_WITHDRAWN"
ACT_SCHEDULE = "SCHEDULE_CHANGED"
ACT_REPLACE = "REPLACEMENT_ARRANGED"
ACT_EXCEPTION = "EXCEPTION_GRANTED"
ACT_REVISION = "PROFILE_REVISED"
ACT_APPEAL_FILED = "APPEAL_FILED"
ACT_APPEAL_RESOLVED = "APPEAL_RESOLVED"
ACT_TODO_OPENED = "TODO_OPENED"
ACT_TODO_CLOSED = "TODO_CLOSED"


class Secretariat:
    def __init__(
        self,
        rest_hours: int = rules.DEFAULT_REST_HOURS,
        cert_expiry_days: int = 30,
        visa_expiry_days: int = 30,
    ) -> None:
        self.rest_hours = rest_hours
        self.cert_expiry_days = cert_expiry_days
        self.visa_expiry_days = visa_expiry_days

        self.associations: dict[str, Association] = {}
        self.officials: dict[str, Official] = {}
        self.events: dict[str, Event] = {}
        self.invitations: dict[str, Invitation] = {}
        self.exceptions: dict[str, RuleException] = {}
        self.evaluations: dict[str, PerformanceEvaluation] = {}
        self.appeals: dict[str, Appeal] = {}
        self.todos: dict[str, Todo] = {}
        self.withdrawals: list[Withdrawal] = []
        self.audit_log: list[AuditEntry] = []
        self._seq = 0

    # ------------------------------------------------------------------
    # 注册与材料维护
    # ------------------------------------------------------------------

    def register_association(self, association: Association) -> Association:
        if association.id in self.associations:
            raise SecretariatError("协会已注册")
        self.associations[association.id] = association
        self._audit("secretariat", "ASSOCIATION_REGISTERED", "Association", association.id,
                    {"name": association.name, "territory": association.territory})
        return association

    def register_official(self, official: Official) -> Official:
        if official.association_id not in self.associations:
            raise SecretariatError("所属协会尚未注册")
        if official.id in self.officials:
            raise SecretariatError("技术官员已注册")
        self.officials[official.id] = official
        self._audit(official.association_id, "OFFICIAL_REGISTERED", "Official", official.id,
                    {"name": official.name})
        return official

    def submit_revision(
        self,
        association_id: str,
        official_id: str,
        profile,
        revised_on: date,
        note: str = "",
        revised_by: str = "",
    ):
        """协会提交本方材料修订。版本只能递增；协会不因此获得例外批准权。"""

        association = self._association(association_id)
        official = self._official(official_id)
        if official.association_id != association_id:
            raise SecretariatError("协会只能修订本方人员材料")
        current = association.latest_revision(official_id)
        if current is not None and revised_on < current.revised_on:
            raise SecretariatError("修订日期不得早于既有材料版本")
        revision = association.add_revision(
            official_id, profile, revised_on, note=note, revised_by=revised_by
        )
        self._audit(association_id, ACT_REVISION, "ProfileRevision",
                    f"{official_id}:v{revision.version}",
                    {"official_id": official_id, "version": revision.version,
                     "revised_on": str(revised_on), "note": note})
        # 材料更新后立即重扫续期待办，避免对已续期文件继续告警
        self.sweep_renewal_todos(revised_on)
        return revision

    # ------------------------------------------------------------------
    # 赛事与岗位
    # ------------------------------------------------------------------

    def register_event(self, event: Event) -> Event:
        if event.id in self.events:
            raise SecretariatError("赛事已登记")
        self.events[event.id] = event
        self._audit(event.host_association_id, "EVENT_REGISTERED", "Event", event.id,
                    {"name": event.name, "territory": event.territory,
                     "quotas": dict(event.travel_quotas)})
        return event

    def set_travel_quota(self, event_id: str, association_id: str, cap: int) -> None:
        event = self._event(event_id)
        if cap < 0:
            raise SecretariatError("名额不能为负")
        event.travel_quotas[association_id] = cap
        self._audit("secretariat", "TRAVEL_QUOTA_SET", "Event", event_id,
                    {"association_id": association_id, "cap": cap})

    def adjust_schedule(
        self, event_id: str, position_id: str, new_window, changed_on: date, note: str = ""
    ) -> list[str]:
        """赛程调整：更新时段并只释放尚未履行的安排。返回被释放的邀请ID。"""

        event = self._event(event_id)
        position = self._position(event, position_id)
        position.window = new_window
        position.schedule_version += 1
        released: list[str] = []
        for invitation in self._invitations_for_position(position_id):
            if invitation.status == InvitationStatus.FULFILLED:
                continue  # 已履行安排不因赛程调整而回滚
            if invitation.status in (
                InvitationStatus.CANCELLED,
                InvitationStatus.DECLINED,
                InvitationStatus.SUPERSEDED,
            ):
                continue
            self._release_invitation(
                invitation, "SCHEDULE_CHANGE",
                f"赛程调整至 {new_window.start}–{new_window.end}：{note}",
                changed_on,
            )
            released.append(invitation.id)
        has_fulfilled = any(
            i.status == InvitationStatus.FULFILLED
            for i in self._invitations_for_position(position_id)
        )
        if has_fulfilled:
            position.status = PositionStatus.FILLED
        elif position.status != PositionStatus.UNDER_APPEAL:
            position.status = PositionStatus.OPEN
        self._close_pause_todo_if_cleared(position_id)
        self._audit(event.host_association_id, ACT_SCHEDULE, "Position", position_id,
                    {"schedule_version": position.schedule_version,
                     "released": released, "note": note},
                    linked_ids=released)
        for invitation_id in released:
            self._open_todo(
                TodoKind.REPLACEMENT, "Position", position_id,
                f"岗位 {position_id} 赛程调整后需重新指派",
                f"被释放邀请 {invitation_id}，赛程版本 {position.schedule_version}",
                due_on=changed_on,
                dedupe_key=f"repl:{position_id}",
            )
        return released

    # ------------------------------------------------------------------
    # 例外授权（分权）
    # ------------------------------------------------------------------

    def grant_exception(
        self,
        official_id: str,
        position_id: str,
        rule_code: str,
        reason: str,
        granted_by: str,
        granted_by_association_id: str,
        granted_on: datetime | None = None,
        expires_on: date | None = None,
    ) -> RuleException:
        """授予资格例外。本协会不得批准本方人员的例外。"""

        official = self._official(official_id)
        if rule_code not in rules.ALL_RULES:
            raise SecretariatError(f"未知规则代码 {rule_code}")
        if granted_by_association_id == official.association_id:
            raise SecretariatError("协会不能独自批准本方人员的例外，须由秘书处或外部授权")
        if granted_by_association_id not in self.associations and granted_by_association_id != "secretariat":
            raise SecretariatError("授权方身份未登记")
        self._position_anywhere(position_id)  # 岗位必须存在
        grant = RuleException(
            id=new_id("exc"),
            rule_code=rule_code,
            official_id=official_id,
            position_id=position_id,
            reason=reason,
            granted_by=granted_by,
            granted_by_association_id=granted_by_association_id,
            granted_on=granted_on or now(),
            expires_on=expires_on,
        )
        self.exceptions[grant.id] = grant
        self._audit(granted_by_association_id, ACT_EXCEPTION, "RuleException", grant.id,
                    {"official_id": official_id, "position_id": position_id,
                     "rule_code": rule_code, "reason": reason,
                     "official_association_id": official.association_id,
                     "expires_on": str(expires_on) if expires_on else None})
        return grant

    # ------------------------------------------------------------------
    # 邀请、答复
    # ------------------------------------------------------------------

    def issue_invitation(
        self,
        official_id: str,
        position_id: str,
        at: datetime | None = None,
        replacement_for: str | None = None,
    ) -> Invitation:
        """依据当时有效材料作出邀请；不满足且无有效例外覆盖时拒绝发出。"""

        at = at or now()
        official = self._official(official_id)
        if not official.active:
            raise SecretariatError("技术官员处于非在役状态")
        event, position = self._event_of_position(position_id)
        if position.status not in (PositionStatus.OPEN,):
            raise SecretariatError(f"岗位当前状态 {position.status.value} 不能发出邀请")
        association = self._association(official.association_id)
        revision = association.revision_effective_on(official_id, at.date())
        if revision is None:
            raise SecretariatError("该时点没有有效的协会材料版本")

        snapshot = self._evaluate(official, association, event, position, at, revision.profile,
                                  revision.version, revision.revised_on)
        if not snapshot.eligible:
            self._audit("secretariat", "INVITATION_REJECTED_INELIGIBLE", "Position", position_id,
                        {"official_id": official_id, "failures": snapshot.failures})
            raise IneligibleError(snapshot.failures)

        if replacement_for is not None:
            predecessor = self.invitations.get(replacement_for)
            if predecessor is None or predecessor.position_id != position_id:
                raise SecretariatError("替补邀请必须对应同岗位的既有邀请")
        invitation = Invitation(
            id=new_id("inv"),
            official_id=official_id,
            position_id=position_id,
            event_id=event.id,
            issued_at=at,
            snapshot=snapshot,
            replacement_for=replacement_for,
        )
        self.invitations[invitation.id] = invitation
        position.status = PositionStatus.HELD
        if replacement_for is not None:
            predecessor.replaced_by_invitation_id = invitation.id
        self._audit("secretariat", ACT_ISSUE, "Invitation", invitation.id,
                    {"official_id": official_id, "position_id": position_id,
                     "profile_version": snapshot.profile_version,
                     "schedule_version": snapshot.schedule_version,
                     "exceptions": snapshot.exception_ids,
                     "replacement_for": replacement_for},
                    linked_ids=[replacement_for] if replacement_for else [])
        return invitation

    def respond(
        self,
        invitation_id: str,
        accepted: bool,
        at: datetime | None = None,
        channel: str = "",
        note: str = "",
    ) -> Invitation:
        """登记候选人答复。

        - 重复给出与原决定一致的答复：原样返回，不产生新决定；
        - 答复与原决定矛盾：邀请与岗位一并暂停，开立待办等秘书处处理。
        """

        at = at or now()
        invitation = self._invitation(invitation_id)
        response_note = note

        if invitation.status == InvitationStatus.SUSPENDED:
            self._audit(invitation.official_id, "RESPONSE_IGNORED_PAUSED",
                        "Invitation", invitation_id,
                        {"accepted": accepted, "note": response_note})
            return invitation

        if invitation.status in (InvitationStatus.CONFIRMED, InvitationStatus.DECLINED):
            original_accepted = invitation.status == InvitationStatus.CONFIRMED
            if accepted == original_accepted:
                self._audit(invitation.official_id, ACT_REPEAT, "Invitation", invitation_id,
                            {"accepted": accepted, "original": invitation.status.value})
                return invitation  # 返回原决定，不追加任何变更
            return self._suspend_for_contradiction(invitation, accepted, at, channel, response_note)

        response = self._append_response(invitation, accepted, at, channel, response_note)
        if response.accepted:
            invitation.status = InvitationStatus.CONFIRMED
            invitation.decided_at = at
            self._refresh_position_status(
                self._position(self._event(invitation.event_id), invitation.position_id)
            )
            self._resolve_todos(f"repl:{invitation.position_id}",
                                f"新邀请 {invitation.id} 已确认", at)
            self._audit(invitation.official_id, ACT_CONFIRM, "Invitation", invitation.id,
                        {"channel": channel})
        else:
            invitation.status = InvitationStatus.DECLINED
            invitation.decided_at = at
            position = self._position(self._event(invitation.event_id), invitation.position_id)
            position.status = PositionStatus.OPEN
            self._open_replacement_todo(position, invitation, at.date(), "候选人婉拒")
            self._audit(invitation.official_id, ACT_DECLINE, "Invitation", invitation_id,
                        {"channel": channel, "note": response_note})
        return invitation

    def resolve_paused_position(
        self, position_id: str, uphold_accepted: bool, by: str, at: datetime | None = None,
        note: str = "",
    ) -> Invitation:
        """秘书处处理答复矛盾后的暂停岗位：确定以接受还是婉拒为准。"""

        at = at or now()
        invitation = next(
            (i for i in self._invitations_for_position(position_id)
             if i.status == InvitationStatus.SUSPENDED),
            None,
        )
        if invitation is None:
            raise SecretariatError("该岗位没有暂停中的邀请")
        last = invitation.last_response()
        invitation.status = (
            InvitationStatus.CONFIRMED if uphold_accepted else InvitationStatus.DECLINED
        )
        invitation.decided_at = at
        position = self._position(self._event(invitation.event_id), position_id)
        if uphold_accepted:
            position.status = PositionStatus.HELD
            self._resolve_todos(f"repl:{position_id}", f"暂停解除，邀请 {invitation.id} 有效", at)
        else:
            position.status = PositionStatus.OPEN
            self._open_replacement_todo(position, invitation, at.date(), "矛盾答复后秘书处裁定婉拒")
        self._resolve_todos(f"pause:{position_id}", f"秘书处 {by} 裁定：{note}", at)
        self._audit(by, ACT_PAUSE_RESOLVE, "Position", position_id,
                    {"invitation_id": invitation.id, "uphold_accepted": uphold_accepted,
                     "note": note, "last_response_accepted": last.accepted if last else None},
                    linked_ids=[invitation.id])
        return invitation

    # ------------------------------------------------------------------
    # 履职、退出、换人
    # ------------------------------------------------------------------

    def fulfill(
        self, invitation_id: str, completed_on: date, rating: str, comment: str = "",
    ) -> PerformanceEvaluation:
        """登记履职完成并冻结评价。评价永久保留作出邀请时的资格依据。"""

        invitation = self._invitation(invitation_id)
        if invitation.status != InvitationStatus.CONFIRMED:
            raise SecretariatError("只有已确认的邀请可以登记履职")
        if invitation.snapshot is None:
            raise SecretariatError("邀请缺少资格依据快照")
        invitation.status = InvitationStatus.FULFILLED
        position = self._position(self._event(invitation.event_id), invitation.position_id)
        position.status = PositionStatus.FILLED
        evaluation = PerformanceEvaluation(
            id=new_id("eval"),
            invitation_id=invitation.id,
            official_id=invitation.official_id,
            position_id=invitation.position_id,
            completed_on=completed_on,
            rating=rating,
            comment=comment,
            basis_snapshot=invitation.snapshot,
        )
        self.evaluations[evaluation.id] = evaluation
        self._resolve_todos(f"repl:{invitation.position_id}",
                            f"岗位已由 {invitation.official_id} 履职完成", None)
        self._audit("secretariat", ACT_FULFILL, "PerformanceEvaluation", evaluation.id,
                    {"invitation_id": invitation.id, "official_id": invitation.official_id,
                     "profile_version": invitation.snapshot.profile_version,
                     "exceptions": invitation.snapshot.exception_ids},
                    linked_ids=[invitation.id])
        return evaluation

    def withdraw(
        self, invitation_id: str, requested_on: date, reason: str, kind: str = "WITHDRAWAL",
    ) -> Withdrawal:
        """退出或赛程调整。已履行的安排不释放、不回滚。"""

        invitation = self._invitation(invitation_id)
        if invitation.status == InvitationStatus.FULFILLED:
            raise SecretariatError("邀请已履职完成，不能退出；评价保留原资格依据")
        record = Withdrawal(
            id=new_id("wd"),
            official_id=invitation.official_id,
            position_id=invitation.position_id,
            invitation_id=invitation.id,
            requested_on=requested_on,
            reason=reason,
            kind=kind,
            fulfilled=False,
        )
        if invitation.status in (InvitationStatus.ISSUED, InvitationStatus.CONFIRMED,
                                 InvitationStatus.SUSPENDED):
            self._release_invitation(invitation, kind, reason, requested_on)
            record.released_invitation_ids.append(invitation.id)
            position = self._position(self._event(invitation.event_id), invitation.position_id)
            if position.status != PositionStatus.UNDER_APPEAL:
                position.status = PositionStatus.OPEN
            self._close_pause_todo_if_cleared(position.id)
            self._open_replacement_todo(position, invitation, requested_on, reason)
        self.withdrawals.append(record)
        self._audit(invitation.official_id, ACT_WITHDRAW, "Withdrawal", record.id,
                    {"invitation_id": invitation.id, "reason": reason, "kind": kind,
                     "released": record.released_invitation_ids},
                    linked_ids=record.released_invitation_ids)
        return record

    def arrange_replacement(
        self,
        released_invitation_id: str,
        new_official_id: str,
        at: datetime | None = None,
    ) -> Invitation:
        """为被释放/婉拒的邀请安排替补，责任链（旧邀请、评价、例外）全部保留。"""

        at = at or now()
        predecessor = self._invitation(released_invitation_id)
        invitation = self.issue_invitation(
            new_official_id, predecessor.position_id, at=at,
            replacement_for=predecessor.id,
        )
        self._audit("secretariat", ACT_REPLACE, "Invitation", invitation.id,
                    {"predecessor": predecessor.id,
                     "predecessor_status": predecessor.status.value,
                     "chain": self._replacement_chain_ids(predecessor.id)},
                    linked_ids=[predecessor.id])
        return invitation

    def _release_invitation(self, invitation: Invitation, reason_code: str,
                            reason: str, on: date) -> None:
        invitation.status = InvitationStatus.CANCELLED
        invitation.released_reason = f"{reason_code}: {reason}"

    # ------------------------------------------------------------------
    # 申诉
    # ------------------------------------------------------------------

    def file_appeal(
        self,
        official_id: str,
        position_id: str,
        raised_by_association_id: str,
        reason: str,
        filed_on: date,
        invitation_id: str | None = None,
    ) -> Appeal:
        position = self._position_anywhere(position_id)
        appeal = Appeal(
            id=new_id("apl"),
            official_id=official_id,
            position_id=position_id,
            invitation_id=invitation_id,
            raised_by_association_id=raised_by_association_id,
            reason=reason,
            filed_on=filed_on,
            position_status_on_filing=position.status.value,
        )
        self.appeals[appeal.id] = appeal
        if position.is_live():
            position.status = PositionStatus.UNDER_APPEAL
        self._open_todo(
            TodoKind.APPEAL, "Appeal", appeal.id,
            f"岗位 {position_id} 相关申诉待审", reason,
            due_on=filed_on + timedelta(days=14), dedupe_key=f"appeal:{appeal.id}",
        )
        self._audit(raised_by_association_id, ACT_APPEAL_FILED, "Appeal", appeal.id,
                    {"official_id": official_id, "position_id": position_id,
                     "reason": reason},
                    linked_ids=[invitation_id] if invitation_id else [])
        return appeal

    def resolve_appeal(
        self, appeal_id: str, upheld: bool, resolution: str, resolved_by: str,
        resolved_on: date,
    ) -> Appeal:
        appeal = self._appeal(appeal_id)
        if appeal.status not in (AppealStatus.FILED, AppealStatus.UNDER_REVIEW):
            raise SecretariatError("申诉已结案")
        appeal.status = AppealStatus.UPHELD if upheld else AppealStatus.REJECTED
        appeal.resolution = resolution
        appeal.resolved_by = resolved_by
        appeal.resolved_on = resolved_on

        event, position = self._event_of_position(appeal.position_id)
        if position.status == PositionStatus.UNDER_APPEAL:
            factual = self._factual_position_status(position)
            if upheld:
                prior = next(
                    (s for s in PositionStatus
                     if s.value == appeal.position_status_on_filing),
                    PositionStatus.OPEN,
                )
                # 受理前状态仍与现存邀请相容时才恢复，避免复活已释放的安排
                compatible = {
                    PositionStatus.FILLED: factual == PositionStatus.FILLED,
                    PositionStatus.HELD: factual == PositionStatus.HELD,
                    PositionStatus.PAUSED: factual == PositionStatus.PAUSED,
                    PositionStatus.OPEN: True,
                    PositionStatus.RELEASED: True,
                }.get(prior, False)
                position.status = prior if compatible else factual
            else:
                position.status = factual
        self._resolve_todos(f"appeal:{appeal.id}", resolution, None)
        self._audit(resolved_by, ACT_APPEAL_RESOLVED, "Appeal", appeal.id,
                    {"upheld": upheld, "resolution": resolution,
                     "position_status": position.status.value},
                    linked_ids=[appeal.invitation_id] if appeal.invitation_id else [])
        return appeal

    # ------------------------------------------------------------------
    # 持续待办
    # ------------------------------------------------------------------

    def sweep_renewal_todos(self, today: date) -> list[Todo]:
        """扫描全部官员最新材料，开立/解除证书与签证续期待办。"""

        opened: list[Todo] = []
        for official in self.officials.values():
            association = self.associations.get(official.association_id)
            if association is None:
                continue
            revision = association.latest_revision(official.id)
            if revision is None:
                continue
            profile = revision.profile
            live_keys: set[str] = set()
            for cert in profile.certifications:
                if cert.valid_until is None:
                    continue
                key = f"cert:{official.id}:{cert.discipline}:{cert.valid_until.isoformat()}"
                live_keys.add(key)
                if cert.valid_until <= today:
                    title, detail = (
                        f"{cert.discipline} 证书已到期",
                        f"等级 {cert.level.value} 已于 {cert.valid_until} 到期",
                    )
                elif cert.valid_until <= today + timedelta(days=self.cert_expiry_days):
                    title, detail = (
                        f"{cert.discipline} 证书即将到期",
                        f"等级 {cert.level.value} 将于 {cert.valid_until} 到期",
                    )
                else:
                    continue
                created = self._open_todo(
                    TodoKind.CERT_EXPIRY, "Official", official.id,
                    title, detail, due_on=cert.valid_until, dedupe_key=key,
                )
                if created:
                    opened.append(created)
            for visa in profile.visas:
                key = f"visa:{official.id}:{visa.territory}:{visa.valid_until.isoformat()}"
                live_keys.add(key)
                if visa.valid_until <= today:
                    title = f"{visa.territory} 签证已到期"
                elif visa.valid_until <= today + timedelta(days=self.visa_expiry_days):
                    title = f"{visa.territory} 签证即将到期"
                else:
                    continue
                created = self._open_todo(
                    TodoKind.VISA_EXPIRY, "Official", official.id,
                    title, f"签证有效期至 {visa.valid_until}",
                    due_on=visa.valid_until, dedupe_key=key,
                )
                if created:
                    opened.append(created)
            self._close_stale_renewals(official.id, live_keys, today)
        return opened

    def open_todos(self) -> list[Todo]:
        return [t for t in self.todos.values() if t.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS)]

    def _open_replacement_todo(self, position: Position, invitation: Invitation,
                               due_on: date, reason: str) -> Todo | None:
        return self._open_todo(
            TodoKind.REPLACEMENT, "Position", position.id,
            f"岗位 {position.id} 需要替补",
            f"原邀请 {invitation.id}（官员 {invitation.official_id}）已释放：{reason}",
            due_on=due_on, dedupe_key=f"repl:{position.id}",
        )

    def _open_todo(self, kind, ref_type: str, ref_id: str, title: str, detail: str,
                   due_on: date | None, dedupe_key: str) -> Todo | None:
        existing = next(
            (t for t in self.todos.values()
             if t.dedupe_key == dedupe_key and t.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS)),
            None,
        )
        if existing is not None:
            return None
        todo = Todo(
            id=new_id("todo"),
            kind=kind,
            ref_type=ref_type,
            ref_id=ref_id,
            title=title,
            detail=detail,
            due_on=due_on,
            created_at=now(),
            dedupe_key=dedupe_key,
        )
        todo.history.append((todo.created_at, TodoStatus.OPEN, "开立"))
        self.todos[todo.id] = todo
        self._audit("system", ACT_TODO_OPENED, "Todo", todo.id,
                    {"kind": kind.value, "title": title, "dedupe_key": dedupe_key})
        return todo

    def _resolve_todos(self, dedupe_key: str, resolution: str, at: datetime | None) -> None:
        for todo in self.todos.values():
            if todo.dedupe_key == dedupe_key and todo.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS):
                todo.record(TodoStatus.RESOLVED, resolution, at=at)
                self._audit("system", ACT_TODO_CLOSED, "Todo", todo.id,
                            {"resolution": resolution})

    def _close_pause_todo_if_cleared(self, position_id: str) -> None:
        """暂停中的邀请若已被释放/取消，岗位暂停待办不再有处理对象。"""

        still_paused = any(
            i.status == InvitationStatus.SUSPENDED
            for i in self._invitations_for_position(position_id)
        )
        if not still_paused:
            self._resolve_todos(f"pause:{position_id}", "暂停邀请已释放，岗位重开", None)

    def _close_stale_renewals(self, official_id: str, live_keys: set[str], today: date) -> None:
        """材料已续期（新版本中到期日变化）时，旧续期待办自动解除。"""

        for todo in self.todos.values():
            if todo.ref_id != official_id or todo.kind not in (
                TodoKind.CERT_EXPIRY, TodoKind.VISA_EXPIRY
            ):
                continue
            if todo.status not in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS):
                continue
            if todo.dedupe_key in live_keys:
                continue
            todo.record(TodoStatus.RESOLVED, f"材料已于 {today} 更新，证件续期", at=None)
            self._audit("system", ACT_TODO_CLOSED, "Todo", todo.id,
                        {"resolution": "材料已修订"})

    # ------------------------------------------------------------------
    # 视图与解释
    # ------------------------------------------------------------------

    def host_event_view(self, event_id: str) -> dict:
        """承办方视图：只包含履职所需信息，不含关系、其他赛事安排等完整材料。"""

        event = self._event(event_id)
        positions_view = []
        for position in event.positions.values():
            entry = {
                "position_id": position.id,
                "discipline": position.discipline,
                "role": position.role,
                "window": {"start": position.window.start.isoformat(),
                           "end": position.window.end.isoformat()},
                "status": position.status.value,
                "official": None,
            }
            invitation = next(
                (i for i in self._invitations_for_position(position.id)
                 if i.status in (InvitationStatus.CONFIRMED, InvitationStatus.FULFILLED)),
                None,
            )
            if invitation is not None:
                official = self._official(invitation.official_id)
                snapshot = invitation.snapshot
                entry["official"] = {
                    "official_id": official.id,
                    "name": official.name,
                    "contact": official.contact_for_events,
                    "serving_window": entry["window"],
                    "qualification_basis_version": snapshot.profile_version if snapshot else None,
                    # 仅给结论性履职信息，不给证书编号、关系清单等完整档案
                    "exceptions_used": snapshot.exception_ids if snapshot else [],
                }
            positions_view.append(entry)
        return {
            "event_id": event.id,
            "name": event.name,
            "territory": event.territory,
            "positions": positions_view,
        }

    def explain_assignment(self, invitation_id: str) -> dict:
        """解释一次指派为何成立、谁批准了例外、换人时保留了哪些责任记录。"""

        invitation = self._invitation(invitation_id)
        snapshot = invitation.snapshot
        exceptions = []
        if snapshot:
            for exc_id in snapshot.exception_ids:
                grant = self.exceptions.get(exc_id)
                if grant:
                    exceptions.append({
                        "exception_id": grant.id,
                        "rule_code": grant.rule_code,
                        "reason": grant.reason,
                        "granted_by": grant.granted_by,
                        "granted_by_association_id": grant.granted_by_association_id,
                        "granted_on": grant.granted_on.isoformat(),
                    })
        chain = self._replacement_chain_ids(invitation.id)
        evaluations = [
            {
                "evaluation_id": e.id,
                "official_id": e.official_id,
                "completed_on": str(e.completed_on),
                "rating": e.rating,
                "basis_profile_version": e.basis_snapshot.profile_version,
            }
            for e in self.evaluations.values()
            if e.position_id == invitation.position_id
        ]
        return {
            "invitation_id": invitation.id,
            "event_id": invitation.event_id,
            "position_id": invitation.position_id,
            "official_id": invitation.official_id,
            "status": invitation.status.value,
            "issued_at": invitation.issued_at.isoformat(),
            "qualification_basis": {
                "evaluated_at": snapshot.evaluated_at.isoformat() if snapshot else None,
                "profile_version": snapshot.profile_version if snapshot else None,
                "profile_revised_on": str(snapshot.profile_revised_on) if snapshot else None,
                "schedule_version": snapshot.schedule_version if snapshot else None,
                "rule_results": snapshot.rule_results if snapshot else {},
                "failures": snapshot.failures if snapshot else [],
            },
            "exceptions": exceptions,
            "responses": [
                {"at": r.responded_at.isoformat(), "accepted": r.accepted,
                 "channel": r.channel, "note": r.note}
                for r in invitation.responses
            ],
            "replacement_chain": chain,
            "released_reason": invitation.released_reason,
            "responsibility_records_kept": {
                "prior_invitation": invitation.replacement_for,
                "superseded_by": invitation.replaced_by_invitation_id,
                "evaluations_on_position": evaluations,
            },
        }

    def _replacement_chain_ids(self, invitation_id: str) -> list[str]:
        """沿替补关系回溯整条责任链（旧 → 新）。"""

        chain = [invitation_id]
        current = self.invitations.get(invitation_id)
        while current and current.replacement_for:
            chain.append(current.replacement_for)
            current = self.invitations.get(current.replacement_for)
        return list(reversed(chain))

    # ------------------------------------------------------------------
    # 核验装配
    # ------------------------------------------------------------------

    def _evaluate(self, official, association: Association, event: Event,
                  position: Position, at: datetime, profile, version: int,
                  revised_on: date) -> EligibilitySnapshot:
        assignments = [
            rules.AssignmentView(
                invitation_id=i.id,
                position_id=i.position_id,
                window=self._position_anywhere(i.position_id).window,
                status=i.status,
            )
            for i in self.invitations.values()
            if i.official_id == official.id
        ]
        quota_held = sum(
            1
            for i in self.invitations.values()
            if i.event_id == event.id
            and self.officials[i.official_id].association_id == official.association_id
            and i.status in (InvitationStatus.ISSUED, InvitationStatus.CONFIRMED,
                             InvitationStatus.SUSPENDED)
        )
        grants = [
            g for g in self.exceptions.values()
            if g.official_id == official.id and g.position_id == position.id
        ]
        ctx = rules.EvaluationContext(
            official_id=official.id,
            association_id=official.association_id,
            profile=profile,
            profile_version=version,
            profile_revised_on=revised_on,
            position=position,
            event=event,
            as_of=at,
            assignments=assignments,
            quota_held=quota_held,
            exceptions=grants,
            rest_hours=self.rest_hours,
        )
        return rules.evaluate(ctx)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _suspend_for_contradiction(self, invitation: Invitation, accepted: bool,
                                   at: datetime, channel: str, note: str) -> Invitation:
        self._append_response(invitation, accepted, at, channel, note)
        invitation.status = InvitationStatus.SUSPENDED
        position = self._position(self._event(invitation.event_id), invitation.position_id)
        position.status = PositionStatus.PAUSED
        self._open_todo(
            TodoKind.POSITION_PAUSED, "Position", position.id,
            f"岗位 {position.id} 因答复矛盾暂停",
            f"邀请 {invitation.id} 原决定与最新答复（accepted={accepted}）矛盾",
            due_on=at.date(), dedupe_key=f"pause:{position.id}",
        )
        self._audit(invitation.official_id, ACT_CONTRADICTION, "Invitation", invitation.id,
                    {"latest_accepted": accepted, "channel": channel},
                    linked_ids=[position.id])
        return invitation

    @staticmethod
    def _append_response(invitation: Invitation, accepted: bool, at: datetime,
                         channel: str, note: str):
        from .model import OfficialResponse

        response = OfficialResponse(
            responded_at=at, accepted=accepted, channel=channel, note=note
        )
        invitation.responses.append(response)
        return response

    def _invitations_for_position(self, position_id: str) -> list[Invitation]:
        return [i for i in self.invitations.values() if i.position_id == position_id]

    def _refresh_position_status(self, position: Position) -> None:
        """根据该岗位现存邀请重算状态（已履行优先，其次有效邀请）。"""

        position.status = self._factual_position_status(position)

    def _factual_position_status(self, position: Position) -> PositionStatus:
        """按现存邀请推断岗位事实状态，用于申诉结案等场景。"""

        invitations = self._invitations_for_position(position.id)
        if any(i.status == InvitationStatus.FULFILLED for i in invitations):
            return PositionStatus.FILLED
        if any(i.status == InvitationStatus.SUSPENDED for i in invitations):
            return PositionStatus.PAUSED
        if any(i.status in (InvitationStatus.CONFIRMED, InvitationStatus.ISSUED)
               for i in invitations):
            return PositionStatus.HELD
        return PositionStatus.OPEN

    def _audit(self, actor: str, action: str, target_type: str, target_id: str,
               detail: dict, linked_ids: list[str] | None = None) -> AuditEntry:
        self._seq += 1
        entry = AuditEntry(
            seq=self._seq, at=now(), actor=actor, action=action,
            target_type=target_type, target_id=target_id, detail=detail,
            linked_ids=linked_ids or [],
        )
        self.audit_log.append(entry)
        return entry

    def _association(self, association_id: str) -> Association:
        try:
            return self.associations[association_id]
        except KeyError:
            raise SecretariatError("协会未登记") from None

    def _official(self, official_id: str) -> Official:
        try:
            return self.officials[official_id]
        except KeyError:
            raise SecretariatError("技术官员未登记") from None

    def _event(self, event_id: str) -> Event:
        try:
            return self.events[event_id]
        except KeyError:
            raise SecretariatError("赛事未登记") from None

    def _position(self, event: Event, position_id: str) -> Position:
        try:
            return event.positions[position_id]
        except KeyError:
            raise SecretariatError("岗位不属于该赛事") from None

    def _event_of_position(self, position_id: str) -> tuple[Event, Position]:
        for event in self.events.values():
            if position_id in event.positions:
                return event, event.positions[position_id]
        raise SecretariatError("岗位未登记")

    def _position_anywhere(self, position_id: str) -> Position:
        return self._event_of_position(position_id)[1]

    def _invitation(self, invitation_id: str) -> Invitation:
        try:
            return self.invitations[invitation_id]
        except KeyError:
            raise SecretariatError("邀请不存在") from None

    def _appeal(self, appeal_id: str) -> Appeal:
        try:
            return self.appeals[appeal_id]
        except KeyError:
            raise SecretariatError("申诉不存在") from None
