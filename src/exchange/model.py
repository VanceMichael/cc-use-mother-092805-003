"""人员交流与赛事指派系统的领域模型。

协会维护本方人员材料（身份、培训、证书、语言、签证、可服务项目、
与参赛队关系）；材料以只增版本保存，秘书处邀请时快照当时有效的依据。
本模块只定义值对象与时间计算，不产生任何事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# 同一技术官员两场执裁之间必须满足的最短休息时间（小时）。
REST_INTERVAL_HOURS = 11
# 证书/签证到期前多少天进入持续待办。
EXPIRY_HORIZON_DAYS = 30


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Certificate:
    cert_id: str
    discipline: str
    level: int  # 数值越大等级越高
    valid_from: date
    valid_to: date

    def covers(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "cert_id": self.cert_id,
            "discipline": self.discipline,
            "level": self.level,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Certificate":
        return cls(
            cert_id=raw["cert_id"],
            discipline=raw["discipline"],
            level=int(raw["level"]),
            valid_from=parse_date(raw["valid_from"]),
            valid_to=parse_date(raw["valid_to"]),
        )


@dataclass(frozen=True)
class Training:
    training_id: str
    title: str
    completed_on: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "training_id": self.training_id,
            "title": self.title,
            "completed_on": self.completed_on.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Training":
        return cls(raw["training_id"], raw["title"], parse_date(raw["completed_on"]))


@dataclass(frozen=True)
class Language:
    code: str  # 如 "zh"、"en"
    level: str  # 自评/认定等级，仅作记录

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "level": self.level}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Language":
        return cls(raw["code"], raw["level"])


@dataclass(frozen=True)
class Visa:
    country: str  # 签发/适用的国家或地区代码
    valid_to: date

    def to_dict(self) -> dict[str, Any]:
        return {"country": self.country, "valid_to": self.valid_to.isoformat()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Visa":
        return cls(raw["country"], parse_date(raw["valid_to"]))


@dataclass(frozen=True)
class Relation:
    """候选人与参赛队之间已申报的关系（如任职、亲属、同一协会注册）。"""

    team_id: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {"team_id": self.team_id, "kind": self.kind}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Relation":
        return cls(raw["team_id"], raw["kind"])


@dataclass(frozen=True)
class Material:
    """协会对本方人员某一版本的全部申报材料。"""

    revision: int
    home_region: str
    trainings: tuple[Training, ...]
    certificates: tuple[Certificate, ...]
    languages: tuple[Language, ...]
    visas: tuple[Visa, ...]
    disciplines: tuple[str, ...]  # 可服务项目
    relations: tuple[Relation, ...]

    def language_codes(self) -> frozenset[str]:
        return frozenset(lang.code for lang in self.languages)

    def certificate_for(self, discipline: str) -> Certificate | None:
        matching = [c for c in self.certificates if c.discipline == discipline]
        return max(matching, key=lambda c: c.level, default=None)

    def visa_for(self, country: str) -> Visa | None:
        return next((v for v in self.visas if v.country == country), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "home_region": self.home_region,
            "trainings": [t.to_dict() for t in self.trainings],
            "certificates": [c.to_dict() for c in self.certificates],
            "languages": [lang.to_dict() for lang in self.languages],
            "visas": [v.to_dict() for v in self.visas],
            "disciplines": list(self.disciplines),
            "relations": [r.to_dict() for r in self.relations],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Material":
        return cls(
            revision=int(raw["revision"]),
            home_region=raw["home_region"],
            trainings=tuple(Training.from_dict(x) for x in raw.get("trainings", [])),
            certificates=tuple(Certificate.from_dict(x) for x in raw.get("certificates", [])),
            languages=tuple(Language.from_dict(x) for x in raw.get("languages", [])),
            visas=tuple(Visa.from_dict(x) for x in raw.get("visas", [])),
            disciplines=tuple(raw.get("disciplines", [])),
            relations=tuple(Relation.from_dict(x) for x in raw.get("relations", [])),
        )


def material(
    revision: int,
    home_region: str,
    *,
    disciplines: list[str] | tuple[str, ...] = (),
    certificates: list[Certificate] | tuple[Certificate, ...] = (),
    languages: list[Language] | tuple[Language, ...] = (),
    visas: list[Visa] | tuple[Visa, ...] = (),
    trainings: list[Training] | tuple[Training, ...] = (),
    relations: list[Relation] | tuple[Relation, ...] = (),
) -> Material:
    """便捷构造函数。"""
    return Material(
        revision=revision,
        home_region=home_region,
        trainings=tuple(trainings),
        certificates=tuple(certificates),
        languages=tuple(languages),
        visas=tuple(visas),
        disciplines=tuple(disciplines),
        relations=tuple(relations),
    )


def sessions_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def cross_slot_gap_hours(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> float | None:
    """两场不同岗位场次之间的间隔小时数；时间重叠或首尾相接时返回 0。"""
    if sessions_overlap(start_a, end_a, start_b, end_b):
        return 0.0
    if end_a <= start_b:
        return (start_b - end_a).total_seconds() / 3600
    return (start_a - end_b).total_seconds() / 3600
