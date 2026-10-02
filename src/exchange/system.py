"""秘书处人员交流与赛事指派系统。

角色
----
- ``secretariat``              秘书处：注册、邀请、例外批准、暂停裁决、录入执裁评价
- ``assoc:<协会ID>``           协会：注册本方人员、修订材料、提交例外申请、退出
- ``person:<人员ID>``          技术官员本人：答复邀请、退出、申诉
- 赛事承办方即主办协会自身，使用 ``assoc:<主办协会ID>`` 身份提交岗位

状态由只增事件流导出，中断后从事件存储重放即可恢复；每份邀请保存
"发出时有效材料"的快照，执裁完成时评价沿用该快照，事后可逐条解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .errors import AuthorizationError, ConflictStateError, DomainError, IneligibleError
from .model import EXPIRY_HORIZON_DAYS, Material, parse_dt
from .policy import (
    Commitment,
    PositionRequirement,
    evaluate,
    failures_after_exception,
)
from .store import Event, EventStore

SEC = "secretariat"


def assoc_actor(assoc_id: str) -> str:
    return f"assoc:{assoc_id}"


def person_actor(person_id: str) -> str:
    return f"person:{person_id}"


# ---------------------------------------------------------------- 内部状态


@dataclass
class _Slot:
    slot_id: str
    start: datetime
    end: datetime


@dataclass
class _Position:
    position_id: str
    event_id: str
    discipline: str
    required_level: int
    required_languages: frozenset[str]
    host_country: str
    allowed_regions: frozenset[str] | None
    participating_teams: frozenset[str]
    quotas: dict[str, int]
    slots: list[_Slot]
    status: str = "active"  # active / paused / withdrawn
    invitation_ids: list[str] = field(default_factory=list)
    retained_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Invitation:
    invitation_id: str
    position_id: str
    person_id: str
    status: str  # proposed / pending / accepted / declined / contradicted / released
    issued_at: datetime | None = None
    basis: dict[str, Any] | None = None
    first_response: dict[str, Any] | None = None
    conflicting_response: str | None = None
    approved_codes: frozenset[str] = frozenset()
    approvals: list[dict[str, Any]] = field(default_factory=list)
    released_slots: list[str] = field(default_factory=list)
    covering_slots: list[str] | None = None  # None 表示覆盖岗位全部场次；替补只承接剩余场次
    release_reason: str | None = None
    released_at: datetime | None = None
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)
    replaces: str | None = None


@dataclass
class _Person:
    person_id: str
    assoc_id: str
    display: str
    revisions: dict[int, Material] = field(default_factory=dict)
    current_revision: int = 0


@dataclass
class _Request:
    request_id: str
    invitation_id: str
    codes: frozenset[str]
    reason: str
    requested_by: str
    at: datetime
    status: str = "requested"  # requested / approved / rejected
    approver: str | None = None
    decided_at: datetime | None = None


@dataclass
class _Appeal:
    appeal_id: str
    subject_ref: str
    person_id: str
    reason: str
    at: datetime
    status: str = "open"  # open / ruled
    ruling: str | None = None
    ruled_at: datetime | None = None
    ruled_by: str | None = None


# ---------------------------------------------------------------- 决定返回值


@dataclass(frozen=True)
class Decision:
    """对邀请的答复决定；重复确认时原样返回。"""

    invitation_id: str
    position_id: str
    person_id: str
    outcome: str  # accepted / declined
    decided_by: str
    decided_at: datetime
    idempotent: bool = False


# ---------------------------------------------------------------- 系统


class AssignmentSystem:
    def __init__(self, store: EventStore | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store or EventStore()
        self.clock = clock or datetime.now
        self.associations: dict[str, dict[str, str]] = {}
        self.persons: dict[str, _Person] = {}
        self.events_meta: dict[str, dict[str, str]] = {}
        self.positions: dict[str, _Position] = {}
        self.invitations: dict[str, _Invitation] = {}
        self.requests: dict[str, _Request] = {}
        self.appeals: dict[str, _Appeal] = {}
        self.store.replay(self._apply)

    # ============================================================ 事件投影

    def _append(self, type_: str, actor: str, payload: dict[str, Any]) -> Event:
        event = self.store.append(type_, actor, payload, self.clock())
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        p = event.payload
        kind = event.type
        if kind == "AssociationRegistered":
            self.associations[p["assoc_id"]] = {"name": p["name"]}
        elif kind == "PersonRegistered":
            self.persons[p["person_id"]] = _Person(p["person_id"], p["assoc_id"], p["display"])
        elif kind == "MaterialRevised":
            person = self.persons[p["person_id"]]
            material = Material.from_dict(p["material"])
            if material.revision != person.current_revision + 1:
                raise ConflictStateError("材料版本只能递增")
            person.revisions[material.revision] = material
            person.current_revision = material.revision
        elif kind == "EventRegistered":
            self.events_meta[p["event_id"]] = {
                "host_assoc": p["host_assoc"],
                "host_country": p["country"],
                "title": p["title"],
            }
        elif kind == "PositionSubmitted":
            self.positions[p["position_id"]] = _Position(
                position_id=p["position_id"],
                event_id=p["event_id"],
                discipline=p["discipline"],
                required_level=int(p["required_level"]),
                required_languages=frozenset(p["required_languages"]),
                host_country=p["host_country"],
                allowed_regions=frozenset(p["allowed_regions"]) if p["allowed_regions"] is not None else None,
                participating_teams=frozenset(p["participating_teams"]),
                quotas=dict(p["quotas"]),
                slots=[_Slot(s["slot_id"], parse_dt(s["start"]), parse_dt(s["end"])) for s in p["slots"]],
            )
        elif kind == "PositionAdjusted":
            position = self.positions[p["position_id"]]
            position.slots = [_Slot(s["slot_id"], parse_dt(s["start"]), parse_dt(s["end"])) for s in p["slots"]]
            if p.get("participating_teams") is not None:
                position.participating_teams = frozenset(p["participating_teams"])
        elif kind == "PositionStatusChanged":
            self.positions[p["position_id"]].status = p["status"]
        elif kind == "InvitationProposed":
            inv = _Invitation(p["invitation_id"], p["position_id"], p["person_id"], "proposed")
            if p.get("covering_slots") is not None:
                inv.covering_slots = list(p["covering_slots"])
            self.invitations[p["invitation_id"]] = inv
            self.positions[p["position_id"]].invitation_ids.append(p["invitation_id"])
        elif kind == "InvitationIssued":
            inv = self.invitations[p["invitation_id"]]
            inv.status = "pending"
            inv.issued_at = event.at
            inv.basis = p["basis"]
            if p.get("replaces"):
                inv.replaces = p["replaces"]
            if p.get("covering_slots") is not None:
                inv.covering_slots = list(p["covering_slots"])
        elif kind == "ExceptionRequested":
            self.requests[p["request_id"]] = _Request(
                p["request_id"], p["invitation_id"], frozenset(p["codes"]), p["reason"],
                event.actor, event.at,
            )
        elif kind == "ExceptionDecided":
            request = self.requests[p["request_id"]]
            request.status = p["decision"]
            request.approver = event.actor
            request.decided_at = event.at
            if p["decision"] == "approved":
                inv = self.invitations[request.invitation_id]
                inv.approved_codes = inv.approved_codes | request.codes
                inv.approvals.append(
                    {
                        "request_id": request.request_id,
                        "codes": sorted(request.codes),
                        "approver": event.actor,
                        "approved_at": event.at.isoformat(),
                        "reason": request.reason,
                    }
                )
        elif kind == "InvitationResponded":
            inv = self.invitations[p["invitation_id"]]
            inv.status = p["status"]
            inv.first_response = {
                "response": p["response"],
                "by": event.actor,
                "at": event.at.isoformat(),
            }
        elif kind == "ContradictionRecorded":
            inv = self.invitations[p["invitation_id"]]
            inv.status = "contradicted"
            inv.conflicting_response = p["conflicting_response"]
        elif kind == "InvitationReleased":
            inv = self.invitations[p["invitation_id"]]
            inv.status = "released"
            inv.released_slots = sorted(set(inv.released_slots) | set(p["slot_ids"]))
            inv.release_reason = p["reason"]
            inv.released_at = event.at
        elif kind == "SlotMarked":
            inv = self.invitations[p["invitation_id"]]
            inv.completed[p["slot_id"]] = {
                "at": event.at.isoformat(),
                "evaluation": p["evaluation"],
                "basis": p["basis"],
            }
        elif kind == "ContradictionResolved":
            inv = self.invitations[p["invitation_id"]]
            inv.status = p["status"]
            if p["status"] == "released":
                inv.released_slots = sorted(set(inv.released_slots) | set(p["slot_ids"]))
                inv.release_reason = "contradiction_resolved_latest"
                inv.released_at = event.at
            elif p["status"] == "accepted":
                # 秘书处采纳"改口接受"：恢复安排，原释放仅留存于事件历史
                inv.released_slots = []
                inv.release_reason = None
                inv.released_at = None
        elif kind == "AppealFiled":
            self.appeals[p["appeal_id"]] = _Appeal(
                p["appeal_id"], p["subject_ref"], p["person_id"], p["reason"], event.at
            )
        elif kind == "AppealRuled":
            appeal = self.appeals[p["appeal_id"]]
            appeal.status = "ruled"
            appeal.ruling = p["ruling"]
            appeal.ruled_at = event.at
            appeal.ruled_by = event.actor
        else:
            raise DomainError(f"未知事件类型：{kind}")

    # ============================================================ 授权工具

    @staticmethod
    def _require(actor: str, expected: str) -> None:
        if actor != expected:
            raise AuthorizationError(f"仅 {expected} 可执行该动作，当前为 {actor}")

    def _require_person_party(self, actor: str, person_id: str) -> None:
        person = self.persons[person_id]
        if actor not in (person_actor(person_id), assoc_actor(person.assoc_id)):
            raise AuthorizationError("仅本人或所属协会可代为操作")

    def _material(self, person_id: str) -> Material:
        person = self.persons[person_id]
        if person.current_revision == 0:
            raise ConflictStateError(f"{person_id} 尚未提交任何材料版本")
        return person.revisions[person.current_revision]

    # ============================================================ 协会与人员

    def register_association(self, actor: str, assoc_id: str, name: str) -> Event:
        self._require(actor, SEC)
        if assoc_id in self.associations:
            raise ConflictStateError("协会已注册")
        return self._append("AssociationRegistered", actor, {"assoc_id": assoc_id, "name": name})

    def register_event(
        self, actor: str, event_id: str, host_assoc: str, country: str, title: str
    ) -> Event:
        self._require(actor, SEC)
        if host_assoc not in self.associations:
            raise DomainError("主办协会尚未注册")
        if event_id in self.events_meta:
            raise ConflictStateError("赛事已注册")
        return self._append(
            "EventRegistered",
            actor,
            {"event_id": event_id, "host_assoc": host_assoc, "country": country, "title": title},
        )

    def register_person(
        self, actor: str, person_id: str, assoc_id: str, display: str, first_material: Material
    ) -> Event:
        self._require(actor, assoc_actor(assoc_id))
        if assoc_id not in self.associations:
            raise DomainError("协会尚未注册")
        if person_id in self.persons:
            raise ConflictStateError("人员已注册")
        if first_material.revision != 1:
            raise DomainError("首版材料必须为 revision=1")
        self._append("PersonRegistered", actor, {
            "person_id": person_id, "assoc_id": assoc_id, "display": display,
        })
        return self._append(
            "MaterialRevised", actor, {"person_id": person_id, "material": first_material.to_dict()}
        )

    def revise_material(self, actor: str, person_id: str, material: Material) -> Event:
        """协会可随时修订本方材料；版本必须严格递增，旧版本永久保留。"""
        person = self.persons.get(person_id)
        if person is None:
            raise DomainError("人员未注册")
        self._require(actor, assoc_actor(person.assoc_id))
        if material.revision != person.current_revision + 1:
            raise ConflictStateError(
                f"材料版本必须严格递增：下一个应为 {person.current_revision + 1}，收到 {material.revision}"
            )
        return self._append(
            "MaterialRevised", actor, {"person_id": person_id, "material": material.to_dict()}
        )

    # ============================================================ 岗位

    def submit_position(
        self,
        actor: str,
        position_id: str,
        event_id: str,
        discipline: str,
        required_level: int,
        required_languages: set[str] | frozenset[str],
        slots: list[tuple[str, datetime, datetime]],
        quotas: dict[str, int],
        participating_teams: set[str] | frozenset[str],
        allowed_regions: set[str] | frozenset[str] | None = None,
    ) -> Event:
        """承办方（主办协会）提交岗位、时段、等级要求与分地域差旅名额。"""
        meta = self.events_meta.get(event_id)
        if meta is None:
            raise DomainError("赛事未注册")
        self._require(actor, assoc_actor(meta["host_assoc"]))
        if position_id in self.positions:
            raise ConflictStateError("岗位已存在")
        if not slots:
            raise DomainError("岗位至少包含一个时段")
        normalized = [
            {"slot_id": sid, "start": start.isoformat(), "end": end.isoformat()}
            for sid, start, end in slots
        ]
        return self._append(
            "PositionSubmitted",
            actor,
            {
                "position_id": position_id,
                "event_id": event_id,
                "discipline": discipline,
                "required_level": required_level,
                "required_languages": sorted(required_languages),
                "host_country": meta["host_country"],
                "allowed_regions": sorted(allowed_regions) if allowed_regions is not None else None,
                "participating_teams": sorted(participating_teams),
                "quotas": dict(quotas),
                "slots": normalized,
            },
        )

    def adjust_schedule(
        self,
        actor: str,
        position_id: str,
        slots: list[tuple[str, datetime, datetime]],
        participating_teams: set[str] | frozenset[str] | None = None,
    ) -> list[Event]:
        """赛程调整：只释放尚未履行的场次，已完成的执裁记录随岗位保留。"""
        position = self._require_position(actor, position_id)
        if position.status != "active":
            raise ConflictStateError("岗位当前不接受赛程调整")
        events: list[Event] = []
        for invitation_id in list(position.invitation_ids):
            inv = self.invitations[invitation_id]
            if inv.status in ("pending", "accepted"):
                unfulfilled = [
                    s.slot_id for s in self._covered_slots(position, inv)
                    if s.slot_id not in inv.completed
                ]
                if unfulfilled:
                    events.append(
                        self._append(
                            "InvitationReleased",
                            SEC,
                            {
                                "invitation_id": invitation_id,
                                "slot_ids": unfulfilled,
                                "reason": "schedule_adjusted",
                            },
                        )
                    )
        normalized = [
            {"slot_id": sid, "start": start.isoformat(), "end": end.isoformat()}
            for sid, start, end in slots
        ]
        events.append(
            self._append(
                "PositionAdjusted",
                actor,
                {
                    "position_id": position_id,
                    "slots": normalized,
                    "participating_teams": sorted(participating_teams)
                    if participating_teams is not None
                    else None,
                },
            )
        )
        return events

    def withdraw_position(self, actor: str, position_id: str) -> list[Event]:
        position = self._require_position(actor, position_id)
        if position.status == "withdrawn":
            raise ConflictStateError("岗位已撤销")
        events: list[Event] = []
        for invitation_id in list(position.invitation_ids):
            inv = self.invitations[invitation_id]
            if inv.status in ("pending", "accepted"):
                unfulfilled = [
                    s.slot_id for s in self._covered_slots(position, inv)
                    if s.slot_id not in inv.completed
                ]
                if unfulfilled:
                    events.append(
                        self._append(
                            "InvitationReleased",
                            SEC,
                            {"invitation_id": invitation_id, "slot_ids": unfulfilled,
                             "reason": "position_withdrawn"},
                        )
                    )
        events.append(
            self._append("PositionStatusChanged", actor,
                         {"position_id": position_id, "status": "withdrawn"})
        )
        return events

    def _require_position(self, actor: str, position_id: str) -> _Position:
        position = self.positions.get(position_id)
        if position is None:
            raise DomainError("岗位不存在")
        meta = self.events_meta[position.event_id]
        self._require(actor, assoc_actor(meta["host_assoc"]))
        return position

    # ============================================================ 资格评估

    def _requirement(
        self,
        position: _Position,
        material: Material,
        exclude_invitation: str | None,
        slots: list[_Slot],
    ) -> PositionRequirement:
        consumed = self._consumed_quotas(position, exclude_invitation=exclude_invitation)
        region = material.home_region
        remaining = position.quotas.get(region, 0) - consumed.get(region, 0)
        duty_days = tuple(sorted({s.start.date() for s in slots} | {s.end.date() for s in slots}))
        return PositionRequirement(
            assignment_id=exclude_invitation or f"prospect:{position.position_id}",
            discipline=position.discipline,
            required_level=position.required_level,
            required_languages=position.required_languages,
            host_country=position.host_country,
            allowed_regions=position.allowed_regions,
            participating_teams=position.participating_teams,
            starts=tuple(s.start for s in slots),
            ends=tuple(s.end for s in slots),
            duty_days=duty_days,
            quota_remaining=remaining,
        )

    def _consumed_quotas(self, position: _Position, exclude_invitation: str | None = None) -> dict[str, int]:
        consumed: dict[str, int] = {}
        for invitation_id in position.invitation_ids:
            if invitation_id == exclude_invitation:
                continue
            inv = self.invitations[invitation_id]
            if inv.status in ("pending", "accepted", "contradicted"):
                region = self._material(inv.person_id).home_region
                consumed[region] = consumed.get(region, 0) + 1
        return consumed

    def _covered_slots(self, position: _Position, inv: _Invitation) -> list[_Slot]:
        """邀请实际承接的未释放场次（替补只承接剩余场次）。"""
        if inv.covering_slots is None:
            ids = {s.slot_id for s in position.slots}
        else:
            ids = set(inv.covering_slots)
        ids -= set(inv.released_slots)
        return [s for s in position.slots if s.slot_id in ids]

    def _commitments(self, person_id: str, exclude_invitation: str) -> list[Commitment]:
        """该人员在其他岗位上的在途场次（含待答复邀请，避免双重指派）。"""
        result: list[Commitment] = []
        for inv in self.invitations.values():
            if inv.person_id != person_id or inv.invitation_id == exclude_invitation:
                continue
            if inv.status not in ("pending", "accepted"):
                continue
            position = self.positions[inv.position_id]
            for slot in self._covered_slots(position, inv):
                result.append(Commitment(inv.invitation_id, slot.start, slot.end))
        return result

    def _evaluate(
        self,
        position_id: str,
        person_id: str,
        invitation_id: str,
        covering_slots: list[str] | None = None,
    ) -> tuple[Material, list[tuple[str, str]]]:
        position = self.positions[position_id]
        material = self._material(person_id)
        if covering_slots is None:
            slots = list(position.slots)
        else:
            wanted = set(covering_slots)
            slots = [s for s in position.slots if s.slot_id in wanted]
        requirement = self._requirement(position, material, invitation_id, slots)
        commitments = self._commitments(person_id, invitation_id)
        failures = evaluate(material, requirement, commitments, self.clock().date())
        return material, failures

    # ============================================================ 邀请与例外

    def propose(
        self,
        actor: str,
        position_id: str,
        person_id: str,
        covering_slots: list[str] | None = None,
    ) -> str:
        """秘书处依据当时有效的资料提出邀请候选；不合格时返回失败条款供例外申请。

        covering_slots 用于替补邀请：只评估并承接指定的剩余场次。
        """
        self._require(actor, SEC)
        position = self.positions.get(position_id)
        if position is None or person_id not in self.persons:
            raise DomainError("岗位或人员不存在")
        if position.status != "active":
            raise ConflictStateError("岗位未处于可邀请状态")
        if covering_slots is not None and not set(covering_slots) <= {s.slot_id for s in position.slots}:
            raise DomainError("覆盖场次超出岗位范围")
        for invitation_id in position.invitation_ids:
            inv = self.invitations[invitation_id]
            if inv.person_id == person_id and inv.status in ("proposed", "pending", "accepted"):
                return invitation_id  # 幂等：已有在途提案
        invitation_id = f"inv-{position_id}-{person_id}-{len(position.invitation_ids) + 1}"
        self._append(
            "InvitationProposed",
            actor,
            {"invitation_id": invitation_id, "position_id": position_id, "person_id": person_id,
             "covering_slots": covering_slots},
        )
        return invitation_id

    def open_failures(self, invitation_id: str) -> list[tuple[str, str]]:
        """提案当前未被已批准例外覆盖的失败条款（随材料修订动态变化）。"""
        inv = self._get_invitation(invitation_id)
        _, failures = self._evaluate(inv.position_id, inv.person_id, invitation_id)
        return failures_after_exception(failures, inv.approved_codes)

    def request_exception(
        self, actor: str, invitation_id: str, request_id: str, codes: set[str] | frozenset[str], reason: str
    ) -> Event:
        """协会或本人可申请例外，但批准权在秘书处。"""
        inv = self._get_invitation(invitation_id)
        self._require_person_party(actor, inv.person_id)
        if inv.status != "proposed":
            raise ConflictStateError("例外只能在邀请发出之前申请；发出后发现的问题走复核与替补流程")
        open_codes = {code for code, _ in self.open_failures(invitation_id)}
        codes = frozenset(codes)
        if not codes or not codes <= open_codes:
            raise DomainError(f"例外条款必须针对当前未满足项：{sorted(open_codes)}")
        if request_id in self.requests:
            raise ConflictStateError("例外申请编号已存在")
        return self._append(
            "ExceptionRequested",
            actor,
            {"request_id": request_id, "invitation_id": invitation_id,
             "codes": sorted(codes), "reason": reason},
        )

    def decide_exception(self, actor: str, request_id: str, approve: bool) -> Event:
        """只有秘书处可以批准例外；协会无法独自批准本方人员的例外。"""
        self._require(actor, SEC)
        request = self.requests.get(request_id)
        if request is None or request.status != "requested":
            raise ConflictStateError("例外申请不存在或已裁决")
        return self._append(
            "ExceptionDecided",
            actor,
            {"request_id": request_id, "decision": "approved" if approve else "rejected"},
        )

    def issue(self, actor: str, invitation_id: str) -> Event:
        """正式发出邀请：按当前材料重新评估，失败项须全部有已批准例外覆盖。"""
        return self._issue(actor, invitation_id, replaces=None, covering_slots=None)

    def _issue(
        self,
        actor: str,
        invitation_id: str,
        replaces: str | None,
        covering_slots: list[str] | None,
    ) -> Event:
        self._require(actor, SEC)
        inv = self._get_invitation(invitation_id)
        if inv.status != "proposed":
            raise ConflictStateError("仅提案状态可发出邀请")
        position = self.positions[inv.position_id]
        if position.status != "active":
            raise ConflictStateError("岗位未处于可邀请状态")
        material, failures = self._evaluate(
            inv.position_id, inv.person_id, invitation_id, covering_slots=covering_slots
        )
        remaining = failures_after_exception(failures, inv.approved_codes)
        if remaining:
            raise IneligibleError(remaining)
        certificate = material.certificate_for(position.discipline)
        visa = material.visa_for(position.host_country)
        if covering_slots is None:
            covering_slots = [s.slot_id for s in position.slots]
        basis = {
            "evaluated_at": self.clock().date().isoformat(),
            "material_revision": material.revision,
            "discipline": position.discipline,
            "required_level": position.required_level,
            "certificate": certificate.to_dict() if certificate else None,
            "visa_valid_to": visa.valid_to.isoformat() if visa else None,
            "languages": sorted(material.language_codes()),
            "required_languages": sorted(position.required_languages),
            "home_region": material.home_region,
            "covering_slots": list(covering_slots),
            "failures_checked": [{"code": c, "detail": d} for c, d in failures],
            "waived_codes": sorted(inv.approved_codes),
            "approvals": list(inv.approvals),
        }
        return self._append(
            "InvitationIssued",
            actor,
            {
                "invitation_id": invitation_id,
                "basis": basis,
                "replaces": replaces,
                "covering_slots": covering_slots,
            },
        )

    def propose_substitute(
        self, actor: str, position_id: str, person_id: str, released_invitation_id: str
    ) -> str:
        """秘书处为已释放安排提名替补，覆盖范围自动限定为剩余场次。"""
        self._require(actor, SEC)
        prior = self._get_invitation(released_invitation_id)
        if prior.position_id != position_id:
            raise DomainError("替补邀请必须与原安排属于同一岗位")
        if prior.status != "released":
            raise ConflictStateError("只能为已释放的安排提名替补")
        position = self.positions[position_id]
        released_ids = set(prior.released_slots)
        remaining = [
            s.slot_id for s in position.slots
            if s.slot_id in released_ids and s.slot_id not in prior.completed
        ]
        if not remaining:
            remaining = [s.slot_id for s in position.slots if s.slot_id not in prior.completed]
        if not remaining:
            raise ConflictStateError("前任安排已全部履行，无需替补")
        return self.propose(actor, position_id, person_id, covering_slots=remaining)

    def issue_substitute(self, actor: str, invitation_id: str, released_invitation_id: str) -> Event:
        """替补邀请发出：只承接前任尚未履行的剩余场次；已完成评价与依据随岗位保留。"""
        self._require(actor, SEC)
        prior = self._get_invitation(released_invitation_id)
        if prior.status != "released":
            raise ConflictStateError("只能对已释放的安排安排替补")
        position = self.positions[prior.position_id]
        released_ids = set(prior.released_slots)
        remaining = [
            s.slot_id for s in position.slots
            if s.slot_id in released_ids and s.slot_id not in prior.completed
        ]
        if not remaining:
            # 释放后时段已被赛程调整替换：承接当前岗位中无人完成的场次
            remaining = [s.slot_id for s in position.slots if s.slot_id not in prior.completed]
        if not remaining:
            raise ConflictStateError("前任安排已全部履行，无需替补")
        inv = self._get_invitation(invitation_id)
        if inv.covering_slots is None or set(inv.covering_slots) != set(remaining):
            raise ConflictStateError("替补邀请的覆盖场次与前任剩余场次不一致，请先 propose_substitute")
        return self._issue(actor, invitation_id, released_invitation_id, remaining)

    def respond(self, actor: str, invitation_id: str, response: str) -> Decision:
        """答复邀请。

        - 首次接受/拒绝：记录终局决定；
        - 重复同样的答复：原样返回原决定，不产生新事件；
        - 答复内容与首次矛盾：暂停该岗位，等待秘书处裁决。
        """
        if response not in ("accepted", "declined"):
            raise DomainError("答复只能是 accepted 或 declined")
        inv = self._get_invitation(invitation_id)
        self._require_person_party(actor, inv.person_id)
        if inv.status == "pending":
            event = self._append(
                "InvitationResponded",
                actor,
                {"invitation_id": invitation_id, "response": response,
                 "status": "accepted" if response == "accepted" else "released"},
            )
            decided_at = event.at
            if response == "declined":
                position = self.positions[inv.position_id]
                self._append(
                    "InvitationReleased",
                    actor,
                    {"invitation_id": invitation_id,
                     "slot_ids": [s.slot_id for s in self._covered_slots(position, inv)],
                     "reason": "declined"},
                )
            return Decision(invitation_id, inv.position_id, inv.person_id, response, actor, decided_at)

        if inv.first_response is not None and inv.first_response["response"] == response:
            first = inv.first_response
            return Decision(
                invitation_id, inv.position_id, inv.person_id, response,
                first["by"], parse_dt(first["at"]), idempotent=True,
            )

        if inv.first_response is None:
            # 邀请在等待答复期间已因退出/赛程调整/撤岗被释放，不存在"原决定"可言
            raise ConflictStateError("邀请已被释放，无需再答复；可由秘书处重新提名")

        if inv.status in ("accepted", "released"):
            # 与首次答复矛盾 → 暂停岗位
            self._append(
                "ContradictionRecorded",
                actor,
                {"invitation_id": invitation_id, "position_id": inv.position_id,
                 "first_response": inv.first_response["response"],
                 "conflicting_response": response},
            )
            self._append(
                "PositionStatusChanged",
                SEC,
                {"position_id": inv.position_id, "status": "paused"},
            )
            raise ConflictStateError("答复内容矛盾，该岗位已暂停")
        if inv.status == "contradicted":
            raise ConflictStateError("岗位已因矛盾答复暂停，等待秘书处裁决")
        raise ConflictStateError(f"邀请当前状态 {inv.status} 不接受答复")

    def resolve_contradiction(
        self, actor: str, invitation_id: str, uphold: str
    ) -> list[Event]:
        """秘书处裁决矛盾答复：uphold='first' 维持原决定；'latest' 采纳后答复并释放。"""
        self._require(actor, SEC)
        if uphold not in ("first", "latest"):
            raise DomainError("uphold 只能是 first 或 latest")
        inv = self._get_invitation(invitation_id)
        if inv.status != "contradicted":
            raise ConflictStateError("该邀请不存在待裁决的矛盾答复")
        position = self.positions[inv.position_id]
        if uphold == "latest" and inv.conflicting_response == "accepted":
            occupied = [
                other_id for other_id in position.invitation_ids
                if other_id != invitation_id
                and self.invitations[other_id].status in ("pending", "accepted")
            ]
            if occupied:
                raise ConflictStateError("岗位上已有替补在途，须先释放替补才能恢复原安排")
        events: list[Event] = []
        if uphold == "first":
            status = "accepted" if inv.first_response["response"] == "accepted" else "released"
            events.append(self._append(
                "ContradictionResolved", actor,
                {"invitation_id": invitation_id, "status": status, "slot_ids": []},
            ))
        else:
            # 采纳后答复：改口拒绝则仅释放未履行场次；改口接受则恢复安排
            if inv.conflicting_response == "declined":
                unfulfilled = [
                    s.slot_id for s in position.slots if s.slot_id not in inv.completed
                ]
                events.append(self._append(
                    "ContradictionResolved", actor,
                    {"invitation_id": invitation_id, "status": "released",
                     "slot_ids": unfulfilled},
                ))
            else:
                events.append(self._append(
                    "ContradictionResolved", actor,
                    {"invitation_id": invitation_id, "status": "accepted", "slot_ids": []},
                ))
        events.append(self._append(
            "PositionStatusChanged", SEC,
            {"position_id": inv.position_id, "status": "active"},
        ))
        return events

    def withdraw(self, actor: str, invitation_id: str) -> Event:
        """退出：已完成场次的执裁与依据保留，仅释放尚未履行的安排。"""
        inv = self._get_invitation(invitation_id)
        self._require_person_party(actor, inv.person_id)
        if inv.status not in ("pending", "accepted"):
            raise ConflictStateError("仅在途邀请可以退出")
        position = self.positions[inv.position_id]
        unfulfilled = [
            s.slot_id for s in self._covered_slots(position, inv)
            if s.slot_id not in inv.completed
        ]
        if not unfulfilled:
            raise ConflictStateError("承接场次已全部履行完毕，无需退出")
        return self._append(
            "InvitationReleased",
            actor,
            {"invitation_id": invitation_id, "slot_ids": unfulfilled, "reason": "withdrawn"},
        )

    def mark_duty_completed(
        self, actor: str, invitation_id: str, slot_id: str, evaluation: dict[str, Any]
    ) -> Event:
        """秘书处录入已完成执裁的评价；资格依据沿用邀请发出时的快照。"""
        self._require(actor, SEC)
        inv = self._get_invitation(invitation_id)
        if inv.status != "accepted":
            raise ConflictStateError("只有已接受的邀请可录入执裁评价")
        position = self.positions[inv.position_id]
        if not any(s.slot_id == slot_id for s in self._covered_slots(position, inv)):
            raise DomainError("该时段不在本邀请承接范围内")
        if slot_id in inv.completed:
            raise ConflictStateError("该时段已录入评价")
        if slot_id in inv.released_slots:
            raise ConflictStateError("该场次已被释放，不能补录")
        event = self._append(
            "SlotMarked",
            actor,
            {
                "invitation_id": invitation_id,
                "slot_id": slot_id,
                "evaluation": evaluation,
                "basis": inv.basis,  # 保留原资格依据，材料事后修订不影响已完成执裁
            },
        )
        self._retain_record(position, inv, slot_id, evaluation)
        return event

    def _retain_record(
        self, position: _Position, inv: _Invitation, slot_id: str, evaluation: dict[str, Any]
    ) -> None:
        slot = next(s for s in position.slots if s.slot_id == slot_id)
        position.retained_records.append(
            {
                "slot_id": slot_id,
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "person_id": inv.person_id,
                "invitation_id": inv.invitation_id,
                "evaluation": evaluation,
                "basis_revision": inv.basis["material_revision"],
                "waived_codes": list(inv.basis["waived_codes"]),
                "approvals": list(inv.basis["approvals"]),
            }
        )

    # ============================================================ 申诉

    def file_appeal(
        self, actor: str, appeal_id: str, subject_ref: str, person_id: str, reason: str
    ) -> Event:
        self._require_person_party(actor, person_id)
        if appeal_id in self.appeals:
            raise ConflictStateError("申诉编号已存在")
        return self._append(
            "AppealFiled",
            actor,
            {"appeal_id": appeal_id, "subject_ref": subject_ref,
             "person_id": person_id, "reason": reason},
        )

    def rule_appeal(self, actor: str, appeal_id: str, ruling: str) -> Event:
        self._require(actor, SEC)
        appeal = self.appeals.get(appeal_id)
        if appeal is None or appeal.status != "open":
            raise ConflictStateError("申诉不存在或已裁决")
        return self._append(
            "AppealRuled", actor, {"appeal_id": appeal_id, "ruling": ruling}
        )

    # ============================================================ 持续待办

    def todos(self, today: date | None = None) -> list[dict[str, Any]]:
        """从当前状态投影持续待办：证书到期、签证期限、替补缺口、申诉、材料失效复核。"""
        today = today or self.clock().date()
        horizon = today.fromordinal(today.toordinal() + EXPIRY_HORIZON_DAYS)
        result: list[dict[str, Any]] = []

        for person_id, person in sorted(self.persons.items()):
            material = person.revisions.get(person.current_revision)
            if material is None:
                continue
            for cert in sorted(material.certificates, key=lambda c: c.valid_to):
                if today <= cert.valid_to <= horizon:
                    result.append({
                        "type": "certificate_expiring",
                        "person_id": person_id,
                        "cert_id": cert.cert_id,
                        "expires_on": cert.valid_to.isoformat(),
                    })
                elif cert.valid_to < today:
                    result.append({
                        "type": "certificate_expired",
                        "person_id": person_id,
                        "cert_id": cert.cert_id,
                        "expires_on": cert.valid_to.isoformat(),
                    })
            for visa in sorted(material.visas, key=lambda v: v.valid_to):
                if today <= visa.valid_to <= horizon:
                    result.append({
                        "type": "visa_expiring",
                        "person_id": person_id,
                        "country": visa.country,
                        "expires_on": visa.valid_to.isoformat(),
                    })
                elif visa.valid_to < today:
                    result.append({
                        "type": "visa_expired",
                        "person_id": person_id,
                        "country": visa.country,
                        "expires_on": visa.valid_to.isoformat(),
                    })

        for position in sorted(self.positions.values(), key=lambda p: p.position_id):
            if position.status == "withdrawn":
                continue
            active_inv = next(
                (self.invitations[i] for i in position.invitation_ids
                 if self.invitations[i].status in ("pending", "accepted")),
                None,
            )
            if active_inv is not None:
                unfulfilled = [
                    s.slot_id for s in self._covered_slots(position, active_inv)
                    if s.slot_id not in active_inv.completed
                ]
            else:
                done = {
                    slot_id
                    for i in position.invitation_ids
                    for slot_id in self.invitations[i].completed
                }
                unfulfilled = [s.slot_id for s in position.slots if s.slot_id not in done]
            if position.status != "paused" and active_inv is None and unfulfilled:
                result.append({
                    "type": "replacement_needed",
                    "position_id": position.position_id,
                    "event_id": position.event_id,
                    "open_slots": unfulfilled,
                })
            if position.status == "paused":
                result.append({
                    "type": "position_paused",
                    "position_id": position.position_id,
                })

        for inv in sorted(self.invitations.values(), key=lambda i: i.invitation_id):
            if inv.status in ("pending", "accepted") and inv.basis is not None:
                material = self._material(inv.person_id)
                _, failures = self._evaluate(inv.position_id, inv.person_id, inv.invitation_id)
                remaining = failures_after_exception(failures, inv.approved_codes)
                if remaining:
                    result.append({
                        "type": "assignment_recheck",
                        "invitation_id": inv.invitation_id,
                        "position_id": inv.position_id,
                        "person_id": inv.person_id,
                        "failures": [{"code": c, "detail": d} for c, d in remaining],
                        "material_revision_now": material.revision,
                        "basis_revision": inv.basis["material_revision"],
                    })

        for appeal in sorted(self.appeals.values(), key=lambda a: a.appeal_id):
            if appeal.status == "open":
                result.append({
                    "type": "appeal_open",
                    "appeal_id": appeal.appeal_id,
                    "subject_ref": appeal.subject_ref,
                })
        return result

    # ============================================================ 视图与解释

    def organizer_view(self, actor: str, position_id: str) -> dict[str, Any]:
        """赛事承办方视图：只返回履职必需信息，隐藏关系明细、签证、培训与其他候选人。"""
        position = self.positions.get(position_id)
        if position is None:
            raise DomainError("岗位不存在")
        host = self.events_meta[position.event_id]["host_assoc"]
        self._require(actor, assoc_actor(host))
        officials = []
        for invitation_id in position.invitation_ids:
            inv = self.invitations[invitation_id]
            if inv.status not in ("accepted",) or inv.basis is None:
                continue
            person = self.persons[inv.person_id]
            officials.append({
                "person_code": person.display,
                "discipline": position.discipline,
                "cert_level": inv.basis["certificate"]["level"] if inv.basis["certificate"] else None,
                "cert_valid_to": inv.basis["certificate"]["valid_to"] if inv.basis["certificate"] else None,
                "duty_languages": sorted(position.required_languages),
                "home_region": inv.basis["home_region"],
                "slots": [
                    {"slot_id": s.slot_id, "start": s.start.isoformat(), "end": s.end.isoformat()}
                    for s in self._covered_slots(position, inv)
                ],
                "completed_slots": sorted(inv.completed),
                "has_secretariat_exception": bool(inv.basis["waived_codes"]),
            })
        return {
            "position_id": position_id,
            "status": position.status,
            "slots": [
                {"slot_id": s.slot_id, "start": s.start.isoformat(), "end": s.end.isoformat()}
                for s in position.slots
            ],
            "officials": officials,
            # 换人时前任已完成的场次及评价随岗位提供，使承办方掌握完整履职记录；
            # 仍不包含关系明细、签证、培训等与履职安排无关的信息
            "completed_by_previous_officials": [
                {
                    "slot_id": r["slot_id"],
                    "person_code": self.persons[r["person_id"]].display,
                    "evaluation": r["evaluation"],
                }
                for r in position.retained_records
                if not any(r["slot_id"] in o["completed_slots"] for o in officials)
            ],
        }

    def explain(self, invitation_id: str) -> dict[str, Any]:
        """完整解释一次指派为何成立、例外由谁批准、换人时保留了哪些责任记录。"""
        inv = self._get_invitation(invitation_id)
        position = self.positions[inv.position_id]
        person = self.persons[inv.person_id]
        return {
            "invitation_id": invitation_id,
            "position_id": inv.position_id,
            "event_id": position.event_id,
            "person_id": inv.person_id,
            "assoc_id": person.assoc_id,
            "status": inv.status,
            "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
            "qualification_basis": inv.basis,
            "all_checks_passed_at_issue": not (inv.basis or {}).get("failures_checked"),
            "exception_requests": [
                {
                    "request_id": rid,
                    "codes": sorted(self.requests[rid].codes),
                    "requested_by": self.requests[rid].requested_by,
                    "status": self.requests[rid].status,
                    "approver": self.requests[rid].approver,
                    "decided_at": self.requests[rid].decided_at.isoformat()
                    if self.requests[rid].decided_at is not None
                    else None,
                }
                for rid in sorted(self.requests)
                if self.requests[rid].invitation_id == invitation_id
            ],
            "first_response": inv.first_response,
            "release": None
            if inv.released_at is None
            else {"reason": inv.release_reason, "at": inv.released_at.isoformat(),
                  "released_slots": inv.released_slots},
            "completed_duties": [
                {"slot_id": slot_id, **record}
                for slot_id, record in sorted(inv.completed.items())
            ],
            "replaces": inv.replaces,
            "retained_responsibility_records": [
                record for record in position.retained_records
                if (inv.replaces and record["invitation_id"] == inv.replaces)
                or record["invitation_id"] == invitation_id
            ],
        }

    def _get_invitation(self, invitation_id: str) -> _Invitation:
        inv = self.invitations.get(invitation_id)
        if inv is None:
            raise DomainError("邀请不存在")
        return inv
