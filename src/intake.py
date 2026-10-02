"""异构协会材料的归一化录入。

各协会提交的记录格式不同，例如等级可能写 ``"洲际级"``、``"Level-3"``、``3``、
``"L3"``；有效期可能是 ISO 日期、``"2026年12月31日"``、``"长期"``；
语言可能写 ``"英语 C1"``、``{"lang": "en", "level": "advanced"}``；
利益关系可能是自由文本或代码。本模块把它们统一转换为 :mod:`src.model`
中的标准结构，无法识别的内容不静默丢弃，而是收集为录入问题返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .model import (
    Certification,
    CertificationLevel,
    LanguageAbility,
    LanguageLevel,
    OfficialProfile,
    RelationKind,
    RelationRecord,
    Visa,
)


# ---------------------------------------------------------------------------
# 等级归一化
# ---------------------------------------------------------------------------

LEVEL_ALIASES: dict[str, CertificationLevel] = {}
for _alias in ("l1", "level1", "level-1", "1", "一级", "入门", "入门级", "助理"):
    LEVEL_ALIASES[_alias] = CertificationLevel.L1
for _alias in ("l2", "level2", "level-2", "2", "二级", "国家级", "国家组"):
    LEVEL_ALIASES[_alias] = CertificationLevel.L2
for _alias in ("l3", "level3", "level-3", "3", "三级", "洲际级", "洲际", "亚洲级", "亚组"):
    LEVEL_ALIASES[_alias] = CertificationLevel.L3
for _alias in ("l4", "level4", "level-4", "4", "四级", "国际级", "国际"):
    LEVEL_ALIASES[_alias] = CertificationLevel.L4


LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en", "eng": "en", "english": "en", "英语": "en", "英文": "en",
    "zh": "zh", "zho": "zh", "chinese": "zh", "汉语": "zh", "中文": "zh",
    "ja": "ja", "japanese": "ja", "日语": "ja",
    "ko": "ko", "korean": "ko", "韩语": "ko",
    "ar": "ar", "arabic": "ar", "阿拉伯语": "ar",
}

LANGUAGE_LEVEL_ALIASES: dict[str, LanguageLevel] = {}
for _lvl, _names in (
    (LanguageLevel.A1, ("a1", "1", "入门", "beginner")),
    (LanguageLevel.A2, ("a2", "2", "基础", "elementary")),
    (LanguageLevel.B1, ("b1", "3", "中级", "intermediate")),
    (LanguageLevel.B2, ("b2", "4", "中高级", "upper-intermediate", "advanced_low")),
    (LanguageLevel.C1, ("c1", "5", "高级", "advanced", "流利")),
    (LanguageLevel.C2, ("c2", "6", "精通", "mastery", "native", "母语")),
):
    for _name in _names:
        LANGUAGE_LEVEL_ALIASES[_name] = _lvl


RELATION_ALIASES: dict[str, RelationKind] = {}
for _kind, _names in (
    (RelationKind.NATIONALITY, ("nationality", "nation", "国籍", "会籍", "所属协会")),
    (RelationKind.COACH, ("coach", "coaching", "教练", "执教")),
    (RelationKind.FAMILY, ("family", "relative", "亲属", "家属", "家庭")),
    (RelationKind.EMPLOYMENT, ("employment", "employer", "employee", "雇佣", "任职")),
    (RelationKind.ATHLETE, ("athlete", "player", "teammate", "运动员", "队友")),
    (RelationKind.OTHER, ("other", "其他", "其它")),
):
    for _name in _names:
        RELATION_ALIASES[_name] = _kind


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


def normalize_level(raw: Any) -> CertificationLevel:
    if isinstance(raw, CertificationLevel):
        return raw
    key = str(raw).strip().lower().replace(" ", "").replace("_", "-")
    key = key.replace("level-", "l").replace("level", "l") if key.startswith(("level", "level-")) else key
    if key in LEVEL_ALIASES:
        return LEVEL_ALIASES[key]
    # 直接匹配“L3”等
    if key.startswith("l") and key[1:] in LEVEL_ALIASES:
        return LEVEL_ALIASES[key[1:]]
    raise ValueError(f"无法识别的证书等级: {raw!r}")


def parse_date(raw: Any) -> date | None:
    """解析多种日期写法；``None``、空串、“长期/永久”返回 None（长期有效）。"""

    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw).strip()
    if not text or text in {"长期", "长期有效", "永久", "永久有效", "open", "none", "null"}:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace("/", "-").replace(".", "-").strip("-")
    parts = text.split("-")
    if len(parts) == 3:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    return date.fromisoformat(text)


def parse_date_required(raw: Any, field_name: str) -> date:
    value = parse_date(raw)
    if value is None:
        raise ValueError(f"{field_name}不能为长期有效")
    return value


def normalize_language(raw: Any) -> LanguageAbility:
    """支持 ``"英语 C1"``、``{"lang": "en", "level": "C1"}``、``("en","c1")``。"""

    valid_until: date | None = None
    if isinstance(raw, LanguageAbility):
        return raw
    if isinstance(raw, str):
        bits = raw.replace("：", ":").replace(",", " ").split()
        if len(bits) == 1 and ":" in bits[0]:
            lang_part, level_part = bits[0].split(":", 1)
        elif len(bits) >= 2:
            lang_part, level_part = bits[0], bits[1]
        else:
            raise ValueError(f"无法识别的语言记录: {raw!r}")
    elif isinstance(raw, dict):
        lang_part = raw.get("lang") or raw.get("language") or ""
        level_part = raw.get("level") or raw.get("cefr") or ""
        valid_until = parse_date(raw.get("valid_until"))
    elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
        lang_part, level_part = raw[0], raw[1]
    else:
        raise ValueError(f"无法识别的语言记录: {raw!r}")

    lang_key = str(lang_part).strip().lower()
    language = LANGUAGE_ALIASES.get(lang_key, str(lang_part).strip().lower())
    level_key = str(level_part).strip().lower().replace(" ", "")
    if level_key not in LANGUAGE_LEVEL_ALIASES:
        raise ValueError(f"无法识别的语言等级: {level_part!r}")
    return LanguageAbility(
        language=language,
        level=LANGUAGE_LEVEL_ALIASES[level_key],
        valid_until=valid_until,
    )


def normalize_relation(raw: Any) -> RelationRecord:
    """支持代码、自由文本关键词与 dict 结构。"""

    if isinstance(raw, RelationRecord):
        return raw
    if isinstance(raw, dict):
        kind_raw = raw.get("kind") or raw.get("type") or "other"
        team_id = str(raw.get("team_id") or raw.get("team") or "").strip()
        if not team_id:
            raise ValueError("关系记录缺少 team_id")
        kind = RELATION_ALIASES.get(str(kind_raw).strip().lower())
        if kind is None:
            kind = _infer_relation_kind(str(kind_raw))
        return RelationRecord(
            kind=kind,
            team_id=team_id,
            detail=str(raw.get("detail") or raw.get("note") or ""),
            until=parse_date(raw.get("until")),
        )
    raise ValueError(f"无法识别的关系记录: {raw!r}")


def _infer_relation_kind(text: str) -> RelationKind:
    lowered = text.strip().lower()
    for alias, kind in RELATION_ALIASES.items():
        if alias in lowered:
            return kind
    return RelationKind.OTHER


# ---------------------------------------------------------------------------
# 整份材料录入
# ---------------------------------------------------------------------------


@dataclass
class IntakeReport:
    profile: OfficialProfile = field(default_factory=OfficialProfile)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _pick(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return None


def ingest_profile(record: dict[str, Any]) -> IntakeReport:
    """把一份异构协会材料转换为标准材料档案。

    识别失败的条目记录到 ``issues``，不阻断其余条目的归一化。
    """

    report = IntakeReport()
    profile = report.profile

    profile.home_territory = str(_pick(record, "home_territory", "territory", "home") or "")
    restricted = _pick(record, "restricted_territories", "restrictions", "banned_territories") or []
    profile.restricted_territories = {str(t).strip() for t in restricted if str(t).strip()}

    disciplines = _pick(record, "disciplines", "services", "can_serve", "events") or []
    profile.disciplines = {str(d).strip() for d in disciplines if str(d).strip()}

    for raw in _pick(record, "certifications", "certs", "certificates") or []:
        try:
            if not isinstance(raw, dict):
                raise ValueError("证书记录必须是对象")
            discipline = str(_pick(raw, "discipline", "sport", "event") or "").strip()
            if not discipline:
                raise ValueError("证书缺少项目")
            valid_from = parse_date_required(_pick(raw, "valid_from", "issued", "from"), "证书生效日")
            cert = Certification(
                discipline=discipline,
                level=normalize_level(_pick(raw, "level", "grade", "rank")),
                valid_from=valid_from,
                valid_until=parse_date(_pick(raw, "valid_until", "expires", "expiry", "until")),
                raw_level=str(_pick(raw, "level", "grade", "rank") or ""),
                source=str(_pick(raw, "source", "id", "ref") or ""),
            )
            profile.certifications.append(cert)
            profile.disciplines.add(discipline)
        except ValueError as exc:
            report.issues.append(f"证书条目被拒绝: {exc}")

    for raw in _pick(record, "languages", "lang") or []:
        try:
            profile.languages.append(normalize_language(raw))
        except ValueError as exc:
            report.issues.append(f"语言条目被拒绝: {exc}")

    for raw in _pick(record, "visas", "visa") or []:
        try:
            if not isinstance(raw, dict):
                raise ValueError("签证记录必须是对象")
            territory = str(_pick(raw, "territory", "country", "region") or "").strip()
            if not territory:
                raise ValueError("签证缺少地区")
            entries = _pick(raw, "entries", "entry_count")
            profile.visas.append(
                Visa(
                    territory=territory,
                    valid_until=parse_date_required(
                        _pick(raw, "valid_until", "expires", "expiry"), "签证有效期"
                    ),
                    entries=None if entries in (None, "", "M", "multi") else int(entries),
                )
            )
        except ValueError as exc:
            report.issues.append(f"签证条目被拒绝: {exc}")

    for raw in _pick(record, "relations", "conflicts", "affiliations") or []:
        try:
            profile.relations.append(normalize_relation(raw))
        except ValueError as exc:
            report.issues.append(f"关系条目被拒绝: {exc}")

    return report
