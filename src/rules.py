"""时点资格核验引擎。

秘书处发出邀请前，依据“当时有效”的协会材料版本与赛程版本逐条核验：

- ``DISCIPLINE_SERVICEABLE`` 可服务项目
- ``CERT_LEVEL_VALID``      证书等级与有效期
- ``LANGUAGE_REQUIREMENT``  岗位语言要求
- ``REST_INTERVAL``         连续执裁的休息间隔
- ``TERRITORY_VISA``        地域限制与签证期限
- ``POSITION_OVERLAP``      岗位时段重叠（防止同时被两个项目指派）
- ``TEAM_RELATION``         候选人与参赛队的利益关系
- ``TRAVEL_QUOTA``          承办方差旅名额

每条规则给出通过/失败及可读原因。失败规则可被**有效例外**覆盖；例外的
有效性（授权方非本协会、未过期）同样在本模块判定，秘书处不能在核验之后
偷偷放宽。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .model import (
    CertificationLevel,
    EligibilitySnapshot,
    InvitationStatus,
    LanguageLevel,
    OfficialProfile,
    Position,
    Event,
    RuleException,
    ServiceWindow,
)


RULE_DISCIPLINE = "DISCIPLINE_SERVICEABLE"
RULE_CERT = "CERT_LEVEL_VALID"
RULE_LANGUAGE = "LANGUAGE_REQUIREMENT"
RULE_REST = "REST_INTERVAL"
RULE_TERRITORY = "TERRITORY_VISA"
RULE_OVERLAP = "POSITION_OVERLAP"
RULE_CONFLICT = "TEAM_RELATION"
RULE_QUOTA = "TRAVEL_QUOTA"

ALL_RULES = (
    RULE_DISCIPLINE,
    RULE_CERT,
    RULE_LANGUAGE,
    RULE_REST,
    RULE_TERRITORY,
    RULE_OVERLAP,
    RULE_CONFLICT,
    RULE_QUOTA,
)

# 占用岗位、会造成时段冲突的邀请状态
OCCUPYING_STATUSES = frozenset(
    {
        InvitationStatus.ISSUED,
        InvitationStatus.CONFIRMED,
        InvitationStatus.SUSPENDED,
    }
)

# 产生休息间隔约束的状态（含已履行：刚执裁完仍需恢复）
REST_RELEVANT_STATUSES = frozenset(
    {
        InvitationStatus.ISSUED,
        InvitationStatus.CONFIRMED,
        InvitationStatus.SUSPENDED,
        InvitationStatus.FULFILLED,
    }
)

DEFAULT_REST_HOURS = 24


@dataclass
class AssignmentView:
    """某位官员的另一条安排，核验时只需窗口与状态。"""

    invitation_id: str
    position_id: str
    window: ServiceWindow
    status: InvitationStatus


@dataclass
class EvaluationContext:
    official_id: str
    association_id: str
    profile: OfficialProfile
    profile_version: int
    profile_revised_on: date
    position: Position
    event: Event
    as_of: datetime
    assignments: list[AssignmentView] = field(default_factory=list)
    quota_held: int = 0
    exceptions: list[RuleException] = field(default_factory=list)
    rest_hours: int = DEFAULT_REST_HOURS


@dataclass
class RuleResult:
    code: str
    passed: bool
    reason: str
    covered_by_exception: str | None = None


_LEVEL_ORDER = {level: index for index, level in enumerate(CertificationLevel)}
_LANG_ORDER = {level: index for index, level in enumerate(LanguageLevel)}


def _level_at_least(actual: CertificationLevel, required: CertificationLevel) -> bool:
    return _LEVEL_ORDER[actual] >= _LEVEL_ORDER[required]


def evaluate(ctx: EvaluationContext) -> EligibilitySnapshot:
    """按当时有效资料逐条核验，返回可长期保存的资格依据快照。"""

    results: dict[str, RuleResult] = {}
    profile = ctx.profile
    position = ctx.position
    day = ctx.as_of.date()

    # 1. 可服务项目
    if position.discipline in profile.disciplines:
        results[RULE_DISCIPLINE] = RuleResult(
            RULE_DISCIPLINE, True, f"材料列出可服务项目 {position.discipline}"
        )
    else:
        results[RULE_DISCIPLINE] = RuleResult(
            RULE_DISCIPLINE, False, f"材料未列出可服务项目 {position.discipline}"
        )

    # 2. 证书等级与有效期（以岗位开始日的证书状态为准）
    service_day = position.window.start.date()
    cert = profile.certification_on(position.discipline, service_day)
    if cert is None:
        results[RULE_CERT] = RuleResult(
            RULE_CERT, False, f"{service_day} 当日无 {position.discipline} 的有效证书"
        )
    elif not _level_at_least(cert.level, position.required_level):
        results[RULE_CERT] = RuleResult(
            RULE_CERT,
            False,
            f"证书等级 {cert.level.value} 低于岗位要求 {position.required_level.value}",
        )
    else:
        results[RULE_CERT] = RuleResult(
            RULE_CERT,
            True,
            f"证书等级 {cert.level.value} 满足 {position.required_level.value}（有效至 "
            f"{cert.valid_until or '长期'}）",
        )

    # 3. 语言
    required_language = position.required_language
    if required_language is None:
        results[RULE_LANGUAGE] = RuleResult(RULE_LANGUAGE, True, "岗位无语言要求")
    else:
        wanted_lang, wanted_level = required_language
        match = next(
            (
                ability
                for ability in profile.languages
                if ability.language == wanted_lang
                and ability.is_valid_on(day)
                and _LANG_ORDER[ability.level] >= _LANG_ORDER[wanted_level]
            ),
            None,
        )
        results[RULE_LANGUAGE] = (
            RuleResult(
                RULE_LANGUAGE,
                True,
                f"{wanted_lang} {match.level.value} 满足 {wanted_level.value}",
            )
            if match
            else RuleResult(
                RULE_LANGUAGE,
                False,
                f"{wanted_lang} 能力低于 {wanted_level.value} 或证明已失效",
            )
        )

    # 4. 岗位时段重叠（防止同时被两个项目指派）
    overlap = next(
        (
            a
            for a in ctx.assignments
            if a.position_id != position.id
            and a.status in OCCUPYING_STATUSES
            and a.window.overlaps(position.window)
        ),
        None,
    )
    results[RULE_OVERLAP] = (
        RuleResult(
            RULE_OVERLAP,
            False,
            f"与岗位 {overlap.position_id}（邀请 {overlap.invitation_id}）时段重叠",
        )
        if overlap
        else RuleResult(RULE_OVERLAP, True, "与既有岗位时段不重叠")
    )

    # 5. 休息间隔
    rest = timedelta(hours=ctx.rest_hours)
    rest_violation = _find_rest_violation(ctx, rest)
    results[RULE_REST] = (
        RuleResult(
            RULE_REST,
            False,
            f"与岗位 {rest_violation.position_id} 间隔不足 {ctx.rest_hours} 小时",
        )
        if rest_violation
        else RuleResult(RULE_REST, True, f"相邻岗位间隔不少于 {ctx.rest_hours} 小时")
    )

    # 6. 地域限制与签证
    territory_reason = _territory_failure(ctx)
    results[RULE_TERRITORY] = (
        RuleResult(RULE_TERRITORY, False, territory_reason)
        if territory_reason
        else RuleResult(RULE_TERRITORY, True, f"可在 {position.territory} 入境服务")
    )

    # 7. 与参赛队的利益关系
    conflict = next(
        (
            relation
            for relation in profile.relations
            if relation.active_on(service_day)
            and relation.team_id in ctx.event.participating_team_ids
        ),
        None,
    )
    results[RULE_CONFLICT] = (
        RuleResult(
            RULE_CONFLICT,
            False,
            f"与参赛队 {conflict.team_id} 存在 {conflict.kind.value} 关系",
        )
        if conflict
        else RuleResult(RULE_CONFLICT, True, "未发现与参赛队的未决利益关系")
    )

    # 8. 差旅名额
    cap = ctx.event.travel_quotas.get(ctx.association_id, 0)
    if ctx.quota_held < cap:
        results[RULE_QUOTA] = RuleResult(
            RULE_QUOTA,
            True,
            f"差旅名额剩余 {cap - ctx.quota_held}（上限 {cap}）",
        )
    else:
        results[RULE_QUOTA] = RuleResult(
            RULE_QUOTA, False, f"协会差旅名额已用尽（已占用 {ctx.quota_held}/{cap}）"
        )

    # 有效例外只能覆盖其指定规则；自我批准的例外视为无效
    failures: list[str] = []
    used_exceptions: list[str] = []
    for code in ALL_RULES:
        result = results[code]
        if result.passed:
            continue
        grant = _covering_exception(ctx, code)
        if grant is not None:
            result.covered_by_exception = grant.id
            used_exceptions.append(grant.id)
        else:
            failures.append(code)

    return EligibilitySnapshot(
        evaluated_at=ctx.as_of,
        profile_version=ctx.profile_version,
        profile_revised_on=ctx.profile_revised_on,
        schedule_version=ctx.position.schedule_version,
        rule_results={code: results[code].passed for code in ALL_RULES},
        failures=[_format_failure(results[code]) for code in failures],
        exception_ids=used_exceptions,
        profile_summary=_summarize(profile),
    )


def _find_rest_violation(
    ctx: EvaluationContext, rest: timedelta
) -> AssignmentView | None:
    for other in ctx.assignments:
        if other.position_id == ctx.position.id:
            continue
        if other.status not in REST_RELEVANT_STATUSES:
            continue
        window = ctx.position.window
        # 先服务 other，再来本岗位：other.end + rest <= window.start
        if other.window.end <= window.start and other.window.end + rest > window.start:
            return other
        # 先服务本岗位，再去 other：window.end + rest <= other.start
        if window.end <= other.window.start and window.end + rest > other.window.start:
            return other
    return None


def _territory_failure(ctx: EvaluationContext) -> str:
    profile = ctx.profile
    territory = ctx.position.territory
    if territory in profile.restricted_territories:
        return f"受地域限制，不得在 {territory} 服务"
    if profile.home_territory == territory:
        return ""  # 回本地区服务无需签证
    entry_day = ctx.position.window.start.date()
    visa = next((v for v in profile.visas if v.territory == territory), None)
    if visa is None:
        return f"无 {territory} 的有效签证"
    if not visa.covers_on(entry_day):
        return f"{territory} 签证在 {entry_day} 已失效或入境次数不足"
    return ""


def _covering_exception(
    ctx: EvaluationContext, code: str
) -> RuleException | None:
    for grant in ctx.exceptions:
        if grant.rule_code != code:
            continue
        if grant.official_id != ctx.official_id or grant.position_id != ctx.position.id:
            continue
        if not grant.valid_for(ctx.association_id, ctx.as_of.date()):
            continue
        return grant
    return None


def _format_failure(result: RuleResult) -> str:
    return f"{result.code}: {result.reason}"


def _summarize(profile: OfficialProfile) -> dict:
    return {
        "disciplines": sorted(profile.disciplines),
        "certifications": [
            {
                "discipline": c.discipline,
                "level": c.level.value,
                "valid_until": str(c.valid_until) if c.valid_until else None,
            }
            for c in profile.certifications
        ],
        "languages": [
            {"language": a.language, "level": a.level.value} for a in profile.languages
        ],
        "visas": [
            {"territory": v.territory, "valid_until": str(v.valid_until)}
            for v in profile.visas
        ],
    }
