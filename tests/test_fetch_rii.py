from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_rii import (  # noqa: E402
    CorpusAct,
    FeedItem,
    alias_pattern,
    effects_for_norm,
    norm_segments,
    parse_decision,
    parse_xml,
    section_matches,
)


def act(slug: str, jurabk: str, *aliases: str) -> CorpusAct:
    return CorpusAct(
        slug=slug,
        jurabk=jurabk,
        act_id=f"fed_{slug}",
        patterns=tuple(alias_pattern(alias) for alias in aliases),
    )


ACTS = [
    act("sgb_2", "SGB 2", "SGB 2", "SGB II"),
    act("sgg", "SGG", "SGG"),
    act("zpo", "ZPO", "ZPO"),
]


class NormAttributionTests(unittest.TestCase):
    def test_following_section_shorthand_keeps_the_base_norm(self) -> None:
        text = "§§ 330ff, 331 ff., 332 f., 10f ZPO"
        self.assertEqual(
            section_matches(text),
            [(0, ["330", "331", "332", "10f"])],
        )

    def test_does_not_inherit_sections_from_an_untracked_act(self) -> None:
        text = ("§§ 63ff BVerfGG, § 32 Abs 1 BVerfGG, "
                "§ 63 BVerfGG, SGB 2")
        effects = effects_for_norm(text, ACTS)
        self.assertEqual(
            [(effect["jurabk"], effect["paras"]) for effect in effects],
            [("SGB 2", [])],
        )

    def test_keeps_numeric_section_enumeration_together(self) -> None:
        text = "§§ 3, 4 SGB II, § 170 SGG"
        self.assertEqual(
            norm_segments(text, ACTS),
            ["§§ 3, 4 SGB II", "§ 170 SGG"],
        )
        effects = effects_for_norm(text, ACTS)
        self.assertEqual(
            [(effect["jurabk"], effect["paras"]) for effect in effects],
            [("SGB 2", ["3", "4"]), ("SGG", ["170"])],
        )

    def test_assigns_only_the_segment_for_the_tracked_act(self) -> None:
        text = ("§ 72 Abs 5 ArbGG, § 74 Abs 2 ArbGG, "
                "§ 551 Abs 3 ZPO")
        effects = effects_for_norm(text, ACTS)
        self.assertEqual(effects[0]["jurabk"], "ZPO")
        self.assertEqual(effects[0]["paras"], ["551"])


class XmlTests(unittest.TestCase):
    def test_rejects_internal_entities(self) -> None:
        raw = (b'<!DOCTYPE dokument [<!ENTITY injected "bad">]>'
               b'<dokument><doknr>&injected;</doknr></dokument>')
        with self.assertRaisesRegex(ValueError, "entity declarations"):
            parse_xml(raw, label="test")

    def test_decision_row_matches_web_contract(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dokument SYSTEM "https://example.invalid/rii-dok.dtd">
<dokument>
  <doknr>TEST123</doknr><gertyp>BSG</gertyp>
  <entsch-datum>20260714</entsch-datum><aktenzeichen>B 1 X 1/26 R</aktenzeichen>
  <doktyp>Urteil</doktyp><norm>§ 3 SGB 2</norm>
  <titelzeile><p>Leitsatz zum Testfall</p></titelzeile>
  <leitsatz><p>Amtlicher Leitsatz</p></leitsatz>
</dokument>""".encode("utf-8")
        item = FeedItem(
            court_key="bsg",
            guid="jb-TEST123",
            link="https://www.rechtsprechung-im-internet.de/test",
            title="",
            description="",
        )
        row = parse_decision(xml, item, ACTS)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], "rii-test123")
        self.assertEqual(row["date"], "2026-07-14")
        self.assertEqual(row["summary"], {"de": "Amtlicher Leitsatz"})
        self.assertEqual(row["effects"][0]["act_id"], "fed_sgb_2")
        self.assertEqual(row["effects"][0]["kind"], "cited")
        self.assertIsNone(row["text"])


if __name__ == "__main__":
    unittest.main()
