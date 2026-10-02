"""资格与冲突判定规则。

所有规则以纯函数形式给出，输入"邀请决定时点"的材料快照与候选人
当时的全部在途安排，输出失败条款列表。每条失败都带稳定代码，
秘书处批准的例外只能逐条覆盖这些代码，协会无权自行豁免。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .model import (
    REST_INTERVAL_HOURS,
    Material,
    cross_slot_gap_hours,
    sessions_overlap,
)

# 失败条款代码；例外按代码逐条豁免。
CERT_EXPIRED = "cert_expired"
CERT_EXPIRES_BEFORE_DUTY = "cert_expires_before_duty"
LEVEL_INSUFFICIENT = "level_insufficient"
DISCIPLINE_NOT_LISTED = "discipline_not_listed"
LANGUAGE_MISSING = "language_missing"
VISA_MISSING = "visa_missing"
VISA_EXPIRES_BEFORE_DUTY = "visa_expires_before_duty"
REGION_NOT_ALLOWED = "region_not_allowed"
TRAVEL_QUOTA_EXHAUSTED = "travel_quota_exhausted"
SLOT_OVERLAP = "slot_overlap"
REST_INTERVAL = "rest_interval"
TEAM_RELATION_CONFLICT = "team_relation_conflict"


@dataclass(frozen=True)
class Commitment:
    """候选人在另一岗位上的一场在途安排（待答复邀请也占用容量）。"""

    assignment_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PositionRequirement:
    assignment_id: str
    discipline: str
    required_level: int
    required_languages: frozenset[str]
    host_country: str
    allowed_regions: frozenset[str] | None  # None 表示不限地域
    participating_teams: frozenset[str]
    starts: tuple[datetime, ...]
    ends: tuple[datetime, ...]
    duty_days: tuple[date, ...]
    quota_remaining: int  # 该候选人所属地域在本赛事剩余的差旅名额

    @property
    def last_duty_day(self) -> date:
        return max(self.duty_days)


def evaluate(
    material: Material,
    requirement: PositionRequirement,
    commitments: list[Commitment],
    as_of: date,
) -> list[tuple[str, str]]:
    """返回失败条款 (代码, 说明)；空列表表示完全合格。"""
    failures: list[tuple[str, str]] = []

    if requirement.discipline not in material.disciplines:
        failures.append(
            (DISCIPLINE_NOT_LISTED, f"未申报可服务项目 {requirement.discipline}")
        )

    certificate = material.certificate_for(requirement.discipline)
    if certificate is None or not certificate.covers(as_of):
        failures.append(
            (CERT_EXPIRED, f"项目 {requirement.discipline} 证书在 {as_of} 已失效或缺失")
        )
    else:
        if certificate.level < requirement.required_level:
            failures.append(
                (
                    LEVEL_INSUFFICIENT,
                    f"证书等级 {certificate.level} 低于岗位要求 {requirement.required_level}",
                )
            )
        if not certificate.covers(requirement.last_duty_day):
            failures.append(
                (
                    CERT_EXPIRES_BEFORE_DUTY,
                    f"证书将于 {certificate.valid_to} 到期，未覆盖履责末日 {requirement.last_duty_day}",
                )
            )

    if not requirement.required_languages <= material.language_codes():
        missing = ", ".join(sorted(requirement.required_languages - material.language_codes()))
        failures.append((LANGUAGE_MISSING, f"缺少岗位要求语言：{missing}"))

    visa = material.visa_for(requirement.host_country)
    if visa is None:
        failures.append((VISA_MISSING, f"缺少 {requirement.host_country} 有效签证"))
    elif visa.valid_to < requirement.last_duty_day:
        failures.append(
            (
                VISA_EXPIRES_BEFORE_DUTY,
                f"签证 {visa.valid_to} 到期，未覆盖履责末日 {requirement.last_duty_day}",
            )
        )

    if requirement.allowed_regions is not None and material.home_region not in requirement.allowed_regions:
        failures.append(
            (
                REGION_NOT_ALLOWED,
                f"所属地域 {material.home_region} 不在岗位允许范围",
            )
        )

    if requirement.quota_remaining <= 0:
        failures.append(
            (TRAVEL_QUOTA_EXHAUSTED, f"地域 {material.home_region} 的差旅名额已用尽")
        )

    related = {r.team_id for r in material.relations} & requirement.participating_teams
    if related:
        failures.append(
            (
                TEAM_RELATION_CONFLICT,
                "候选人与参赛队存在已申报关系：" + ", ".join(sorted(related)),
            )
        )

    for start, end in zip(requirement.starts, requirement.ends):
        for other in commitments:
            if other.assignment_id == requirement.assignment_id:
                continue
            if sessions_overlap(start, end, other.start, other.end):
                failures.append(
                    (
                        SLOT_OVERLAP,
                        f"场次 {start:%Y-%m-%d %H:%M} 与岗位 {other.assignment_id} 时间重叠",
                    )
                )
                continue
            gap = cross_slot_gap_hours(start, end, other.start, other.end)
            if gap is not None and gap < REST_INTERVAL_HOURS:
                failures.append(
                    (
                        REST_INTERVAL,
                        f"与岗位 {other.assignment_id} 仅间隔 {gap:.1f} 小时，"
                        f"不足 {REST_INTERVAL_HOURS} 小时休息要求",
                    )
                )

    return failures


def failures_after_exception(
    failures: list[tuple[str, str]], waived_codes: frozenset[str]
) -> list[tuple[str, str]]:
    """扣除已批准例外覆盖的条款后，仍然存在的失败。"""
    return [(code, detail) for code, detail in failures if code not in waived_codes]
