from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_gii import (  # noqa: E402
    CORPUS,
    PRACTICE_EXPANSION_2026_07,
    require_complete_corpus,
)


def test_practice_expansion_is_large_and_not_silently_shrunk() -> None:
    assert len(PRACTICE_EXPANSION_2026_07) == 74
    assert len(CORPUS) == 125
    assert len(CORPUS - PRACTICE_EXPANSION_2026_07) == 51


def test_practice_expansion_covers_priority_domains() -> None:
    required = {
        "migration": {
            "asylzbv_2026", "azrg-dv", "bmg", "pauswg", "deuf_v",
        },
        "social_and_housing": {
            "algiiv_2008", "sozhidav_2019", "wogv", "wofg", "pflegezg",
        },
        "family_and_work": {
            "famfg", "muschg_2018", "arbgg", "betrvg", "aentg_2009",
        },
        "courts_and_procedure": {
            "bverfgg", "gvg", "vwvg", "bdsg_2018", "fgo", "ao_1977",
        },
    }
    for slugs in required.values():
        assert slugs <= PRACTICE_EXPANSION_2026_07


def test_default_refresh_rejects_a_shrunken_official_catalog() -> None:
    links = {slug: f"https://example.test/{slug}.xml.zip"
             for slug in CORPUS - {"bverfgg"}}

    try:
        require_complete_corpus(CORPUS, links)
    except RuntimeError as exc:
        assert "bverfgg" in str(exc)
    else:
        raise AssertionError("a missing configured act must abort the refresh")
