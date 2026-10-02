"""人员交流与赛事指派系统的领域模型。

系统参与方：

- 协会维护本方技术官员的身份、培训、证书、语言与可服务项目（材料可修订，
  以版本方式保留全部历史，邀请只依据“当时有效”的版本）；
- 承办方提交赛事岗位、时段、等级要求与差旅名额；
- 秘书处依据时点有效材料发出邀请、核验资格、处理答复、替补与申诉；
- 例外不得由本协会单方批准，必须由秘书处按治理规则授权。

本文件只定义数据结构与少量不变量；业务规则在 :mod:`src.rules`，
跨实体流程在 :mod:`src.secretariat`，异构材料录入在 :mod:`src.intake`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    return datetime.now().astimezone()


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------


class CertificationLevel(str, Enum):
    """区域统一的执裁等级（各协会原始等级经 intake 归一化到此）。"""

    L1 = "L1"  # 入门：助理技术官员
    L2 = "L2"  # 国家级
    L3 = "L3"  # 洲际
    L4 = "L4"  # 国际级


class LanguageLevel(str, Enum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"


class RelationKind(str, Enum):
    """候选人与参赛队之间的关系类型，用于利益冲突核验。"""

    NATIONALITY = "NATIONALITY"        # 国籍/会籍所属协会
    COACH = "COACH"                    # 现任/近期教练
    FAMILY = "FAMILY"                  # 近亲属
    EMPLOYMENT = "EMPLOYMENT"          # 雇佣关系
    ATHLETE = "ATHLETE"                # 近期同为该队运动员
    OTHER = "OTHER"


class InvitationStatus(str, Enum):
    DRAFT = "DRAFT"                    # 秘书处已生成，尚未发出
    ISSUED = "ISSUED"                  # 已发出，等待候选人答复
    CONFIRMED = "CONFIRMED"            # 候选人确认接受（原决定可重放）
    DECLINED = "DECLINED"              # 候选人婉拒
    SUPERSEDED = "SUPERSEDED"          # 被后续安排替代（释放未履行部分）
    FULFILLED = "FULFILLED"            # 已履职完成
    SUSPENDED = "SUSPENDED"            # 答复矛盾，岗位被暂停
    CANCELLED = "CANCELLED"            # 退出/赛程调整释放（未履行部分）


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    HELD = "HELD"                      # 已有确认邀请
    PAUSED = "PAUSED"                  # 答复矛盾，暂停该岗位
    FILLED = "FILLED"                  # 赛事结束、履职完成
    RELEASED = "RELEASED"              # 退出/赛程调整释放
    UNDER_APPEAL = "UNDER_APPEAL"


class TodoKind(str, Enum):
    CERT_EXPIRY = "CERT_EXPIRY"                 # 证书即将/已经到期
    VISA_EXPIRY = "VISA_EXPIRY"                 # 签证期限不足
    REPLACEMENT = "REPLACEMENT"                 # 替补待办
    APPEAL = "APPEAL"                           # 申诉待办
    POSITION_PAUSED = "POSITION_PAUSED"         # 岗位暂停待秘书处处理
    DOCUMENT_RENEWAL = "DOCUMENT_RENEWAL"       # 其他材料续期


class TodoStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class AppealStatus(str, Enum):
    FILED = "FILED"
    UNDER_REVIEW = "UNDER_REVIEW"
    UPHELD = "UPHELD"           # 申诉成立
    REJECTED = "REJECTED"       # 申诉驳回
    WITHDRAWN = "WITHDRAWN"


# ---------------------------------------------------------------------------
# 协会与技术官员（协会侧材料，全部版本化）
# ---------------------------------------------------------------------------


@dataclass
class Certification:
    """证书/资质记录。等级以归一化后的 L1–L4 存储。"""

    discipline: str                       # 项目，如 "Swimming"
    level: CertificationLevel
    valid_from: date
    valid_until: date | None              # None 表示长期有效
    raw_level: str = ""                   # 协会原始等级表述，仅作溯源
    source: str = ""                      # 来源记录标识

    def is_valid_on(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_until is None or day <= self.valid_until


@dataclass
class LanguageAbility:
    language: str
    level: LanguageLevel
    valid_until: date | None = None       # 个别协会为语言证明设置有效期

    def is_valid_on(self, day: date) -> bool:
        return self.valid_until is None or day <= self.valid_until


@dataclass
class Visa:
    """签证/入境许可，决定官员可入境服务的国家或地区。"""

    territory: str
    valid_until: date
    entries: int | None = None            # None 表示多次入境

    def covers_on(self, day: date, needed_entries: int = 1) -> bool:
        if day > self.valid_until:
            return False
        if self.entries is not None and self.entries < needed_entries:
            return False
        return True


@dataclass
class TrainingRecord:
    discipline: str
    course: str
    completed_on: date


@dataclass
class RelationRecord:
    """候选人与某参赛队/协会的关系（利益冲突核验依据）。"""

    kind: RelationKind
    team_id: str
    detail: str = ""
    until: date | None = None             # None 表示持续；否则关系在该日后失效

    def active_on(self, day: date) -> bool:
        return self.until is None or day <= self.until


@dataclass
class OfficialProfile:
    """技术官员在某一版本时点的全部材料快照。"""

    certifications: list[Certification] = field(default_factory=list)
    languages: list[LanguageAbility] = field(default_factory=list)
    visas: list[Visa] = field(default_factory=list)
    training: list[TrainingRecord] = field(default_factory=list)
    disciplines: set[str] = field(default_factory=set)       # 可服务项目
    relations: list[RelationRecord] = field(default_factory=list)
    restricted_territories: set[str] = field(default_factory=set)  # 地域限制
    home_territory: str = ""

    def certification_on(self, discipline: str, day: date) -> Certification | None:
        candidates = [
            c for c in self.certifications
            if c.discipline == discipline and c.is_valid_on(day)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.level.value)


@dataclass
class ProfileRevision:
    """协会材料的一次修订。版本号只能递增，历史版本永不删除。"""

    version: int
    revised_on: date
    profile: OfficialProfile
    note: str = ""
    revised_by: str = ""                  # 协会侧操作人，仅作记录，不具例外批准权


@dataclass
class Association:
    id: str
    name: str
    territory: str
    officials: dict[str, list[ProfileRevision]] = field(default_factory=dict)

    def add_revision(
        self,
        official_id: str,
        profile: OfficialProfile,
        revised_on: date,
        note: str = "",
        revised_by: str = "",
    ) -> ProfileRevision:
        history = self.officials.setdefault(official_id, [])
        version = len(history) + 1
        revision = ProfileRevision(
            version=version,
            revised_on=revised_on,
            profile=profile,
            note=note,
            revised_by=revised_by,
        )
        history.append(revision)
        return revision

    def revision_effective_on(self, official_id: str, day: date) -> ProfileRevision | None:
        """返回该日期时点有效的最新材料版本（含当天修订）。"""

        history = self.officials.get(official_id, [])
        effective = [r for r in history if r.revised_on <= day]
        return effective[-1] if effective else None

    def latest_revision(self, official_id: str) -> ProfileRevision | None:
        history = self.officials.get(official_id, [])
        return history[-1] if history else None


@dataclass
class Official:
    id: str
    name: str
    association_id: str
    contact_for_events: str = ""          # 承办方履职所需联系方式
    active: bool = True


# ---------------------------------------------------------------------------
# 赛事与岗位（承办方侧）
# ---------------------------------------------------------------------------


@dataclass
class ServiceWindow:
    """岗位需要的服务时段（含报到与离场）。"""

    start: datetime
    end: datetime

    def overlaps(self, other: ServiceWindow) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Position:
    id: str
    event_id: str
    discipline: str
    role: str
    window: ServiceWindow
    required_level: CertificationLevel
    required_language: tuple[str, LanguageLevel] | None = None  # (语言, 最低等级)
    territory: str = ""                   # 赛事举办地
    travel_quota_id: str = ""             # 占用的差旅名额标识
    status: PositionStatus = PositionStatus.OPEN
    schedule_version: int = 1             # 赛程调整后递增
    note: str = ""

    def is_live(self) -> bool:
        """尚未履行、仍可能产生约束的岗位状态。"""

        return self.status in (
            PositionStatus.OPEN,
            PositionStatus.HELD,
            PositionStatus.PAUSED,
            PositionStatus.UNDER_APPEAL,
        )


@dataclass
class Event:
    id: str
    name: str
    host_association_id: str
    territory: str
    participating_team_ids: set[str] = field(default_factory=set)
    positions: dict[str, Position] = field(default_factory=dict)
    travel_quotas: dict[str, int] = field(default_factory=dict)  # 协会ID -> 名额上限

    def quota_remaining(self, association_id: str, held_count: int) -> int:
        cap = self.travel_quotas.get(association_id, 0)
        return max(0, cap - held_count)


# ---------------------------------------------------------------------------
# 邀请、答复、例外、评价
# ---------------------------------------------------------------------------


@dataclass
class RuleException:
    """对某条资格规则的例外授权。

    协会不能独自批准本方人员的例外：必须由秘书处角色授权，且与申请协会不同。
    """

    id: str
    rule_code: str
    official_id: str
    position_id: str
    reason: str
    granted_by: str                       # 秘书处授权人/委员会标识
    granted_by_association_id: str        # 必须不同于官员所属协会
    granted_on: datetime
    expires_on: date | None = None
    scope_note: str = ""

    def valid_for(self, association_id: str, day: date) -> bool:
        if self.granted_by_association_id == association_id:
            return False
        return self.expires_on is None or day <= self.expires_on


@dataclass
class EligibilitySnapshot:
    """邀请作出时的资格依据快照，供日后解释“为何成立”。"""

    evaluated_at: datetime
    profile_version: int
    profile_revised_on: date
    schedule_version: int
    rule_results: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    exception_ids: list[str] = field(default_factory=list)
    profile_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.failures


@dataclass
class OfficialResponse:
    responded_at: datetime
    accepted: bool
    channel: str = ""
    note: str = ""


@dataclass
class Invitation:
    id: str
    official_id: str
    position_id: str
    event_id: str
    issued_at: datetime
    status: InvitationStatus = InvitationStatus.ISSUED
    responses: list[OfficialResponse] = field(default_factory=list)
    decided_at: datetime | None = None
    snapshot: EligibilitySnapshot | None = None
    replaced_by_invitation_id: str | None = None
    replacement_for: str | None = None    # 本邀请是哪个邀请的替补
    released_reason: str = ""

    def last_response(self) -> OfficialResponse | None:
        return self.responses[-1] if self.responses else None


@dataclass
class PerformanceEvaluation:
    """履职完成后的评价。

    评价保留原资格依据（不可变快照）；之后材料变化不影响已完成评价的效力。
    """

    id: str
    invitation_id: str
    official_id: str
    position_id: str
    completed_on: date
    rating: str
    comment: str
    basis_snapshot: EligibilitySnapshot
    locked: bool = True


# ---------------------------------------------------------------------------
# 申诉、待办、审计
# ---------------------------------------------------------------------------


@dataclass
class Appeal:
    id: str
    official_id: str
    position_id: str
    invitation_id: str | None
    raised_by_association_id: str
    reason: str
    filed_on: date
    status: AppealStatus = AppealStatus.FILED
    resolution: str = ""
    resolved_by: str = ""
    resolved_on: date | None = None
    position_status_on_filing: str = ""   # 申诉受理时的岗位状态，用于结案后恢复


@dataclass
class Todo:
    """持续待办：证书到期、签证期限、替补、申诉等，服务中断后仍可恢复。"""

    id: str
    kind: TodoKind
    ref_type: str                         # 引用对象类型，如 "Official"
    ref_id: str
    title: str
    detail: str
    due_on: date | None
    created_at: datetime
    status: TodoStatus = TodoStatus.OPEN
    resolution: str = ""
    closed_at: datetime | None = None
    dedupe_key: str = ""                  # 同一待办不重复开立的唯一键
    history: list[tuple[datetime, TodoStatus, str]] = field(default_factory=list)

    def record(self, status: TodoStatus, note: str, at: datetime | None = None) -> None:
        self.status = status
        self.history.append((at or now(), status, note))
        if status in (TodoStatus.RESOLVED, TodoStatus.CLOSED):
            self.closed_at = at or now()
            self.resolution = note


@dataclass
class AuditEntry:
    """append-only 审计记录：每次指派为何成立、谁批准例外、更换时保留了什么。"""

    seq: int
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    detail: dict[str, Any]
    linked_ids: list[str] = field(default_factory=list)


@dataclass
class Withdrawal:
    """退出或赛程调整记录。只释放尚未履行的安排，已履行部分不回滚。"""

    id: str
    official_id: str
    position_id: str
    invitation_id: str
    requested_on: date
    reason: str
    kind: str                             # "WITHDRAWAL" | "SCHEDULE_CHANGE"
    fulfilled: bool                       # 提出时岗位是否已履行
    released_invitation_ids: list[str] = field(default_factory=list)
