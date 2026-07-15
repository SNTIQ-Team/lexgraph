"""Fetch official EUR-Lex state for explicitly watched EU procedures.

This is intentionally narrow: the broad EU index covers instruments, while
``data/procedure_watchlist.json`` names the few pending procedures for which
Lexgraph promises frequent status checks.  A political agreement is not a
terminal state.  An Official Journal publication moves the record to final
review; polling becomes terminal only after a persisted comparison of the
final Article 2 against the tracked Commission proposal has passed.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from common import Http, snapshot_dir, write_jsonl

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "data" / "procedure_watchlist.json"
WATCH_STATE = ROOT / "data" / "procedure_watch_state.json"
CELEX_RE = re.compile(r"\b[35]\d{4}[A-Z]{1,3}\d{4,}\b")
ADOPTED_DECISION_RE = re.compile(r"^3\d{4}D\d{4,}$")
ADOPTION_EVENT_RE = re.compile(
    r"(?:adoption by (?:the )?council|act adopted|final act|"
    r"publication in (?:the )?official journal)", re.IGNORECASE)


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_eurlex_procedure(html: str, watch_key: str, config: dict,
                           fetched_at: str) -> dict:
    """Parse the stable server-rendered procedure heading and event rows."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("#procedureHeading")
    if heading is None:
        raise ValueError("EUR-Lex procedure heading missing")
    status_box = heading.select_one(".procStatus")
    status = _text(status_box.parent if status_box else None)
    if not status:
        raise ValueError("EUR-Lex procedure status missing")
    title_node = heading.find("p")
    title = _text(title_node) or str(config.get("procedure") or watch_key)

    events: list[dict] = []
    for row in soup.select("div.eventRow"):
        date_node = row.select_one(".eventDate span")
        event_date = _text(date_node)
        try:
            date_iso = datetime.strptime(event_date, "%d/%m/%Y").date().isoformat()
        except ValueError:
            date_iso = None
        event_title = _text(row.select_one(".eventTitle .VMIMore"))
        celexes = sorted(set(CELEX_RE.findall(_text(row.select_one(".eventCelex")))))
        details_id = None
        button = row.select_one(".eventTitle button[data-target]")
        if button:
            details_id = str(button.get("data-target") or "").lstrip("#")
        details = soup.find(id=details_id) if details_id else None
        documents = []
        if details:
            documents = sorted({_text(anchor) for anchor in details.select("a")
                                if _text(anchor)})
        events.append({
            "date": date_iso,
            "title": event_title,
            "celexes": celexes,
            "documents": documents,
        })
    events.sort(key=lambda event: (event.get("date") or "",
                                   event.get("title") or ""))
    # Do not scan the whole page for 3…D… identifiers: a procedure page may
    # cite earlier implementing decisions as legal context.  Only a final-act
    # event can nominate an adopted CELEX; the legal-content page is checked
    # for an OJ citation below before the watch is allowed to become terminal.
    adopted_celexes = sorted({
        celex
        for event in events
        if ADOPTION_EVENT_RE.search(str(event.get("title") or ""))
        for celex in event.get("celexes") or []
        if ADOPTED_DECISION_RE.match(celex)
    })
    latest = events[-1] if events else {}
    return {
        "id": watch_key,
        "watch_id": config.get("id") or watch_key,
        "source": "EUR-Lex",
        "jurisdiction": "EU",
        "procedure": config.get("procedure"),
        "proposal_celex": config.get("celex_proposal"),
        "title": title,
        "status": status,
        "stage": latest.get("title") or status,
        "date": latest.get("date"),
        "updated": None,
        "fetched_at": fetched_at,
        "events": events,
        "adopted_celexes": adopted_celexes,
        "official_journal": [],
        "terminal": False,
        "url": config.get("official_url"),
    }


def _official_journal_record(http: Http, celex: str) -> dict | None:
    url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
    response = http.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    notice = _text(soup.select_one("#PP1Contents"))
    match = re.search(r"\bOJ\s+L,\s*([^;]+?)(?:,\s*ELI:|\s+ELI:)", notice)
    if not match:
        return None
    eli = soup.select_one('#PP1Contents a[href*="/eli/"][href$="/oj"]')
    return {"celex": celex, "citation": f"OJ L, {match.group(1).strip()}",
            "eli": eli.get("href") if eli else None, "url": url}


def apply_final_review_gate(row: dict, config: dict,
                            journal: list[dict]) -> dict:
    """Require a persisted final-text comparison before stopping polling.

    OJ publication proves that a final act exists, but does not prove that its
    operative Article 2 still matches the proposal Lexgraph described.  The
    reviewed CELEX/status lives in the versioned watch configuration.  Until a
    reviewer records a passed Article-2 comparison for one of the published
    CELEX identifiers, the procedure stays active as ``pending_final_review``.
    """
    review = config.get("final_text_review")
    review = review if isinstance(review, dict) else {}
    reviewed_celexes = review.get("reviewed_celexes") or []
    if isinstance(reviewed_celexes, str):
        reviewed_celexes = [reviewed_celexes]
    reviewed = {str(value) for value in reviewed_celexes}
    published = {str(record.get("celex")) for record in journal
                 if record.get("celex")}
    expected_proposal = str(config.get("celex_proposal") or "")
    passed = (
        str(review.get("status") or "").casefold() == "passed"
        and review.get("article_2_compared") is True
        and bool(expected_proposal)
        and str(review.get("compared_to") or "") == expected_proposal
        and bool(reviewed & published)
    )
    row["official_journal"] = journal
    row["publication_detected"] = bool(journal)
    row["final_text_review"] = review or None
    row["awaiting_final_review"] = bool(journal) and not passed
    row["terminal"] = bool(journal) and passed
    if row["awaiting_final_review"]:
        row["tracking_hint"] = "pending_final_review"
    return row


def fetch_watch(http: Http, watch_key: str, config: dict,
                fetched_at: str) -> dict:
    url = str(config.get("official_url") or "")
    if not url:
        raise ValueError(f"{watch_key}: official_url missing")
    response = http.get(url, timeout=45)
    response.raise_for_status()
    row = parse_eurlex_procedure(response.text, watch_key, config, fetched_at)
    journal = [record for celex in row["adopted_celexes"]
               if (record := _official_journal_record(http, celex))]
    return apply_final_review_gate(row, config, journal)


def active_eu_watches(watchlist: dict, state: dict | None) -> tuple[
        list[tuple[str, dict]], list[str]]:
    """Return only EU watches that still require an official-source poll.

    ``procedure_watch_state.json`` is the durable lifecycle boundary.  Once
    the state updater has made a procedure terminal/archive-only, the final
    observation remains in that file and its history, but this narrow fetcher
    no longer contacts EUR-Lex for it.  ``monitor: false`` also suppresses the
    very first poll for a deliberately historical validation record.
    """
    state_rows = (state or {}).get("procedures") or {}
    active: list[tuple[str, dict]] = []
    skipped: list[str] = []
    for raw_key, config in (watchlist.get("procedures") or {}).items():
        key = str(raw_key)
        if str(config.get("source") or "DIP").casefold() != "eur-lex":
            continue
        previous = state_rows.get(key)
        configured = bool(config.get("monitor", True))
        still_active = previous is None or bool(previous.get("active", True))
        if configured and still_active:
            active.append((key, config))
        else:
            skipped.append(key)
    return active, skipped


def main() -> int:
    payload = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    state = json.loads(WATCH_STATE.read_text(encoding="utf-8")) \
        if WATCH_STATE.is_file() else None
    watches, skipped = active_eu_watches(payload, state)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    http = Http(delay=0.6)
    rows = [fetch_watch(http, str(key), config, fetched_at)
            for key, config in watches]
    out = snapshot_dir("eu_watch")
    write_jsonl(out / "procedures.jsonl", rows)
    for row in rows:
        print(f"  {row['procedure']}: {row['status']} / {row['stage']}"
              f" — terminal={row['terminal']}")
    print(f"eu-watch: {len(rows)} polled / {len(skipped)} archived -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
