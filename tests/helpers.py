"""测试共用的领域对象构造辅助。"""

from __future__ import annotations

from datetime import date, datetime

from src.model import (
    Association,
    Certification,
    CertificationLevel,
    Event,
    LanguageAbility,
    LanguageLevel,
    Official,
    OfficialProfile,
    Position,
    ServiceWindow,
    Visa,
)
from src.secretariat import Secretariat


def make_profile(
    *,
    disciplines=("Swimming",),
    level: CertificationLevel = CertificationLevel.L3,
    cert_until: date | None = date(2027, 12, 31),
    home_territory: str = "MAC",
    visas=(("SGP", date(2027, 12, 31)),),
    languages=(("en", LanguageLevel.C1),),
    restricted=(),
    relations=(),
) -> OfficialProfile:
    profile = OfficialProfile(
        home_territory=home_territory,
        disciplines=set(disciplines),
        restricted_territories=set(restricted),
        languages=[LanguageAbility(code, lvl) for code, lvl in languages],
        visas=[Visa(territory, until) for territory, until in visas],
        relations=list(relations),
    )
    for discipline in disciplines:
        profile.certifications.append(
            Certification(
                discipline=discipline,
                level=level,
                valid_from=date(2025, 1, 1),
                valid_until=cert_until,
            )
        )
    return profile


def make_position(
    position_id: str = "P1",
    *,
    event_id: str = "E1",
    discipline: str = "Swimming",
    start: datetime = datetime(2026, 11, 10, 9),
    end: datetime = datetime(2026, 11, 10, 18),
    required_level: CertificationLevel = CertificationLevel.L3,
    required_language=("en", LanguageLevel.B2),
    territory: str = "SGP",
) -> Position:
    return Position(
        id=position_id,
        event_id=event_id,
        discipline=discipline,
        role="裁判",
        window=ServiceWindow(start, end),
        required_level=required_level,
        required_language=required_language,
        territory=territory,
    )


def build_world(
    *,
    rest_hours: int = 24,
    cert_expiry_days: int = 30,
    visa_expiry_days: int = 30,
    teams=("TEAM-A", "TEAM-B"),
    quota_a: int = 2,
    quota_b: int = 2,
) -> Secretariat:
    """建立含 A/B 两个协会、各两名官员和一个赛事的标准测试世界。"""

    secretariat = Secretariat(
        rest_hours=rest_hours,
        cert_expiry_days=cert_expiry_days,
        visa_expiry_days=visa_expiry_days,
    )
    secretariat.register_association(Association(id="ASSN-A", name="甲协会", territory="MAC"))
    secretariat.register_association(Association(id="ASSN-B", name="乙协会", territory="JPN"))
    secretariat.register_official(
        Official(id="OFF-A1", name="甲一", association_id="ASSN-A", contact_for_events="甲一履职联系")
    )
    secretariat.register_official(Official(id="OFF-A2", name="甲二", association_id="ASSN-A"))
    secretariat.register_official(Official(id="OFF-B1", name="乙一", association_id="ASSN-B"))
    secretariat.register_official(Official(id="OFF-B2", name="乙二", association_id="ASSN-B"))
    for official_id in ("OFF-A1", "OFF-A2", "OFF-B1", "OFF-B2"):
        association_id = "ASSN-A" if official_id.startswith("OFF-A") else "ASSN-B"
        secretariat.submit_revision(
            association_id, official_id, make_profile(), date(2026, 1, 1)
        )

    event = Event(
        id="E1",
        name="测试锦标赛",
        host_association_id="HOST",
        territory="SGP",
        participating_team_ids=set(teams),
    )
    secretariat.register_event(event)
    secretariat.set_travel_quota("E1", "ASSN-A", quota_a)
    secretariat.set_travel_quota("E1", "ASSN-B", quota_b)
    event.positions["P1"] = make_position("P1")
    return secretariat
