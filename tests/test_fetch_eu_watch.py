from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_eu_watch import (  # noqa: E402
    active_eu_watches,
    apply_final_review_gate,
    fetch_council_development,
    merge_council_development,
    parse_council_register,
    parse_eurlex_procedure,
)


HTML = """
<div id="procedureHeading">
  <b>Procedure 2026/0186/NLE</b>
  <p>COM (2026) 345: Proposal for a Council Implementing Decision</p>
  <div><span class="procStatus procPending"></span><b>Ongoing</b></div>
</div>
<div id="NEG">
  <div id="2026-06-26_DIS" class="eventRow">
    <div class="eventTitle"><button data-target="#DETAIL"><div class="VMIMore">Discussions within the Council</div></button></div>
    <div class="eventCelex"></div><div class="eventDate"><span>26/06/2026</span></div>
  </div>
  <div id="DETAIL"><a>ST 11011 2026 INIT</a></div>
</div>
<div class="eventRow">
  <div class="eventTitle"><button><div class="VMIMore">Adoption by Commission</div></button></div>
  <div class="eventCelex">52026PC0345</div><div class="eventDate"><span>26/06/2026</span></div>
</div>
<div class="legal-context">Earlier act: 32022D0382</div>
"""


def test_parses_ongoing_status_and_official_event_chain() -> None:
    row = parse_eurlex_procedure(
        HTML, "eu-2026-0186-nle",
        {"id": "military", "procedure": "2026/0186/NLE",
         "celex_proposal": "52026PC0345", "official_url": "https://example"},
        "2026-07-15T02:00:00+00:00")
    assert row["status"] == "Ongoing"
    assert row["stage"] == "Discussions within the Council"
    council_event = next(event for event in row["events"]
                         if event["title"] == "Discussions within the Council")
    assert council_event["documents"] == ["ST 11011 2026 INIT"]
    assert row["proposal_celex"] == "52026PC0345"
    assert row["adopted_celexes"] == []
    assert row["terminal"] is False


def test_terminal_and_configured_archive_are_not_polled_again() -> None:
    watchlist = {"procedures": {
        "active": {"source": "EUR-Lex"},
        "terminal": {"source": "EUR-Lex"},
        "historical": {"source": "EUR-Lex", "monitor": False},
        "dip": {"source": "DIP"},
    }}
    state = {"procedures": {
        "active": {"active": True},
        "terminal": {"active": False, "terminal": True},
    }}

    watches, skipped = active_eu_watches(watchlist, state)

    assert [key for key, _ in watches] == ["active"]
    assert skipped == ["terminal", "historical"]


def test_only_final_act_event_can_nominate_adopted_celex() -> None:
    html = HTML + """
    <div class="eventRow">
      <div class="eventTitle"><button><div class="VMIMore">Adoption by Council</div></button></div>
      <div class="eventCelex">32026D1999</div>
      <div class="eventDate"><span>20/07/2026</span></div>
    </div>
    """
    row = parse_eurlex_procedure(html, "eu-x", {}, "2026-07-20T12:00:00Z")
    assert row["adopted_celexes"] == ["32026D1999"]
    # Parsing alone never claims publication; fetch_watch verifies the OJ page.
    assert row["terminal"] is False


def test_oj_publication_stays_active_until_matching_article_review() -> None:
    journal = [{"celex": "32026D1999", "citation": "OJ L, 2026/1999"}]
    published = apply_final_review_gate({"terminal": False}, {}, journal)
    assert published["publication_detected"] is True
    assert published["awaiting_final_review"] is True
    assert published["terminal"] is False

    wrong_celex = apply_final_review_gate({"terminal": False}, {
        "celex_proposal": "52026PC0345",
        "final_text_review": {
            "status": "passed", "article_2_compared": True,
            "reviewed_celexes": ["32026D1888"],
            "compared_to": "52026PC0345",
        }}, journal)
    assert wrong_celex["terminal"] is False

    reviewed = apply_final_review_gate({"terminal": False}, {
        "celex_proposal": "52026PC0345",
        "final_text_review": {
            "status": "passed", "article_2_compared": True,
            "reviewed_celexes": ["32026D1999"],
            "compared_to": "52026PC0345",
        }}, journal)
    assert reviewed["awaiting_final_review"] is False
    assert reviewed["terminal"] is True


COUNCIL_HTML = """
<html><head><title>Public register - Consilium</title></head><body>
<article>
  <h3>ST 11375 2026 INIT - NOTE 10/07/2026</h3>
  <p>Council Implementing Decision extending temporary protection, as
  introduced by Council Implementing Decision (EU) 2022/382, until 4 March
  2028 - Political agreement</p>
  <dl>
    <dt>Addressee:</dt><dd>Permanent Representatives Committee (Part 2)</dd>
    <dt>Date of meeting:</dt><dd>15/07/2026</dd>
  </dl>
  <p>The content of this document is not accessible.</p>
</article>
</body></html>
"""


def test_council_register_is_newer_evidence_but_not_enactment() -> None:
    config = {
        "council_register_document": "ST 11375/26",
        "council_register_url": "https://example.test/council-register",
    }
    development = parse_council_register(
        COUNCIL_HTML, config, "2026-07-15T04:00:00Z")
    assert development == {
        "source": "Council public register",
        "document": "ST 11375/26",
        "url": "https://example.test/council-register",
        "date": "2026-07-10",
        "title": ("Council Implementing Decision extending temporary "
                  "protection, as introduced by Council Implementing Decision "
                  "(EU) 2022/382, until 4 March 2028 - Political agreement"),
        "stage": "Political agreement",
        "document_type": "NOTE",
        "addressee": "Permanent Representatives Committee (Part 2)",
        "meeting_date": "2026-07-15",
        "content_accessible": False,
        "fetched_at": "2026-07-15T04:00:00Z",
        "retrieval_status": "fetched",
        "terminal": False,
    }
    row = {
        "status": "Ongoing", "stage": "Discussions within the Council",
        "date": "2026-06-26", "terminal": False,
        "events": [{"date": "2026-06-26", "title": "Discussions within the Council"}],
    }
    merged = merge_council_development(row, development)
    assert merged["status"] == "Ongoing"
    assert merged["stage"] == "Political agreement"
    assert merged["date"] == "2026-07-10"
    assert merged["terminal"] is False
    assert merged["events"][-1]["document"] == "ST 11375/26"


def test_council_register_browser_block_preserves_verified_seed() -> None:
    class BlockedResponse:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("403")

    class BlockedHttp:
        def get(self, _url: str, **_kwargs):
            return BlockedResponse()

    config = {
        "council_register_document": "ST 11375/26",
        "council_register_url": "https://example.test/register",
        "council_register_seed": {
            "date": "2026-07-10", "stage": "Political agreement",
            "meeting_date": "2026-07-15", "content_accessible": False,
        },
    }
    development = fetch_council_development(
        BlockedHttp(), config, "2026-07-15T04:00:00Z")
    assert development is not None
    assert development["retrieval_status"] == "fetch_unavailable"
    assert development["stage"] == "Political agreement"
    assert development["terminal"] is False
