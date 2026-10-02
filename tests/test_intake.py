"""异构协会材料归一化测试。"""

import unittest
from datetime import date

from src.intake import ingest_profile, normalize_level, normalize_language, parse_date
from src.model import CertificationLevel, LanguageLevel, RelationKind


class LevelNormalizationTest(unittest.TestCase):
    def test_chinese_and_alphanumeric_levels(self) -> None:
        self.assertIs(normalize_level("洲际级"), CertificationLevel.L3)
        self.assertIs(normalize_level("Level-4"), CertificationLevel.L4)
        self.assertIs(normalize_level(3), CertificationLevel.L3)
        self.assertIs(normalize_level("L2"), CertificationLevel.L2)
        self.assertIs(normalize_level("国际级"), CertificationLevel.L4)
        self.assertIs(normalize_level("国家级"), CertificationLevel.L2)

    def test_unknown_level_is_collected_as_issue_not_dropped(self) -> None:
        report = ingest_profile({
            "disciplines": ["Diving"],
            "certs": [{"sport": "Diving", "grade": "银河级", "issued": "2026-01-01"}],
        })
        self.assertFalse(report.ok)
        self.assertTrue(any("证书" in issue for issue in report.issues))
        self.assertEqual(report.profile.disciplines, {"Diving"})

    def test_bad_record_does_not_block_others(self) -> None:
        report = ingest_profile({
            "certs": [
                {"discipline": "Swimming", "level": "L3", "valid_from": "2026年1月1日",
                 "valid_until": "2027年12月31日"},
                "裸字符串",
                {"discipline": "Diving", "level": "bad", "valid_from": "2026-01-01"},
            ],
            "languages": ["英语 C1", "bad-language", {"lang": "ja", "level": "b1"}],
        })
        levels = {(c.discipline, c.level) for c in report.profile.certifications}
        self.assertIn(("Swimming", CertificationLevel.L3), levels)
        self.assertEqual(len([c for c in report.profile.certifications if c.discipline == "Swimming"]), 1)
        langs = {a.language for a in report.profile.languages}
        self.assertEqual(langs, {"en", "ja"})
        self.assertGreaterEqual(len(report.issues), 3)


class DateParsingTest(unittest.TestCase):
    def test_chinese_iso_slash_and_perpetual(self) -> None:
        self.assertEqual(parse_date("2026年10月1日"), date(2026, 10, 1))
        self.assertEqual(parse_date("2026/10/02"), date(2026, 10, 2))
        self.assertEqual(parse_date("2026.10.02"), date(2026, 10, 2))
        self.assertIsNone(parse_date("长期有效"))
        self.assertIsNone(parse_date("永久"))
        self.assertIsNone(parse_date(None))

    def test_required_date_rejects_perpetual(self) -> None:
        from src.intake import parse_date_required

        with self.assertRaisesRegex(ValueError, "生效日"):
            parse_date_required("长期", "证书生效日")


class LanguageNormalizationTest(unittest.TestCase):
    def test_string_dict_and_tuple_formats(self) -> None:
        first = normalize_language("英语 B2")
        second = normalize_language({"lang": "en", "level": "advanced"})
        third = normalize_language(("zh", "c2"))
        for ability in (first, second):
            self.assertEqual(ability.language, "en")
        self.assertIs(second.level, LanguageLevel.C1)
        self.assertIs(first.level, LanguageLevel.B2)
        self.assertIs(third.level, LanguageLevel.C2)
        self.assertEqual(third.language, "zh")

    def test_expiring_language_proof(self) -> None:
        ability = normalize_language({"lang": "en", "level": "C1", "valid_until": "2026年9月1日"})
        self.assertEqual(ability.valid_until, date(2026, 9, 1))
        self.assertFalse(ability.is_valid_on(date(2026, 9, 2)))
        self.assertTrue(ability.is_valid_on(date(2026, 9, 1)))


class RelationNormalizationTest(unittest.TestCase):
    def test_dict_with_chinese_kind_and_free_text_inference(self) -> None:
        from src.intake import normalize_relation

        first = normalize_relation({"kind": "教练", "team_id": "TEAM-A", "until": "2025-01-01"})
        self.assertIs(first.kind, RelationKind.COACH)
        self.assertEqual(first.team_id, "TEAM-A")
        self.assertEqual(first.until, date(2025, 1, 1))
        self.assertTrue(first.active_on(date(2025, 1, 1)))
        self.assertFalse(first.active_on(date(2025, 1, 2)))

    def test_missing_team_is_issue(self) -> None:
        from src.intake import normalize_relation

        with self.assertRaises(ValueError):
            normalize_relation({"kind": "亲属"})


if __name__ == "__main__":
    unittest.main()
