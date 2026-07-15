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

import requests
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
COUNCIL_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


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


def _iso_eu_date(value: str | None) -> str | None:
    match = COUNCIL_DATE_RE.search(str(value or ""))
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _council_document_pattern(document: str) -> str:
    match = re.fullmatch(r"\s*([A-Z]+)\s+(\d+)\s*/\s*(\d{2,4})\s*",
                         document, re.IGNORECASE)
    if not match:
        return re.escape(document)
    prefix, number, year = match.groups()
    year_long = f"20{year}" if len(year) == 2 else year
    return (rf"{re.escape(prefix)}\s+{re.escape(number)}\s*"
            rf"(?:/\s*{re.escape(year)}|{re.escape(year_long)})")


def parse_council_register(html: str, config: dict,
                           fetched_at: str) -> dict:
    """Parse one Council public-register result into source evidence.

    The register deliberately exposes metadata even when the document body is
    not public.  Its title may describe a *political agreement*; this parser
    records that wording but never treats it as adoption or enactment.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_title = _text(soup.title)
    if "browser check" in page_title.casefold():
        raise ValueError("Council register browser check returned")
    text = _text(soup)
    document = str(config.get("council_register_document") or "").strip()
    if not document:
        raise ValueError("council_register_document missing")
    document_pattern = _council_document_pattern(document)
    if not re.search(document_pattern, text, re.IGNORECASE):
        raise ValueError(f"Council register document not found: {document}")

    title_match = re.search(
        r"(Council Implementing Decision.{1,700}?Political agreement)",
        text, re.IGNORECASE)
    title = " ".join(title_match.group(1).split()) if title_match else None
    date_match = COUNCIL_DATE_RE.search(text)
    addressee_match = re.search(
        r"Addressee\s*:?\s*(.+?)(?=Date of meeting|The content|$)",
        text, re.IGNORECASE)
    meeting_match = re.search(
        r"Date of meeting\s*:?\s*(\d{1,2}[./]\d{1,2}[./]\d{4})",
        text, re.IGNORECASE)
    type_match = re.search(
        rf"{document_pattern}\s*(?:INIT\s*)?-\s*([A-Z][A-Z \"'-]+?)\s+"
        r"(\d{1,2}[./]\d{1,2}[./]\d{4})", text, re.IGNORECASE)
    inaccessible = bool(re.search(
        r"content of this document is not accessible", text,
        re.IGNORECASE))
    title_folded = str(title or "").casefold()
    stage = (
        "Preparation for a political agreement"
        if "preparation for a political agreement" in title_folded else
        "Political agreement"
        if title_folded.endswith("political agreement") else title)
    return {
        "source": "Council public register",
        "document": document,
        "url": config.get("council_register_url"),
        "date": _iso_eu_date(type_match.group(2) if type_match else
                             date_match.group(0) if date_match else None),
        "title": title,
        "stage": stage,
        "document_type": (" ".join(type_match.group(1).split()).upper()
                          if type_match else None),
        "addressee": (" ".join(addressee_match.group(1).split())
                      if addressee_match else None),
        "meeting_date": _iso_eu_date(
            meeting_match.group(1) if meeting_match else None),
        "content_accessible": not inaccessible,
        "fetched_at": fetched_at,
        "retrieval_status": "fetched",
        "terminal": False,
    }


def _council_seed(config: dict, fetched_at: str,
                  retrieval_status: str = "configured") -> dict | None:
    seed = config.get("council_register_seed")
    if not isinstance(seed, dict):
        return None
    return {
        "source": "Council public register",
        "document": config.get("council_register_document"),
        "url": config.get("council_register_url"),
        "date": seed.get("date"),
        "title": seed.get("title"),
        "stage": seed.get("stage") or "Political agreement",
        "document_type": seed.get("document_type"),
        "addressee": seed.get("addressee"),
        "meeting_date": seed.get("meeting_date"),
        "content_accessible": seed.get("content_accessible"),
        "fetched_at": fetched_at,
        "retrieval_status": retrieval_status,
        "terminal": False,
    }


def fetch_council_development(http: Http, config: dict,
                              fetched_at: str) -> dict | None:
    """Fetch optional Council evidence, preserving verified seed metadata.

    Consilium may serve a browser-check page to non-browser clients.  A
    checked-in seed prevents that availability problem from erasing an
    already verified register record; ``retrieval_status`` stays explicit.
    """
    url = str(config.get("council_register_url") or "")
    document = str(config.get("council_register_document") or "")
    if not url or not document:
        return None
    try:
        # Consilium sometimes leaves non-browser clients waiting for its
        # browser-check response.  This is supplementary metadata with a
        # checked-in, explicitly labelled seed, so do not let three long
        # transport retries hold the twice-daily production refresh.
        response = http.get(url, timeout=12, retries=1)
        response.raise_for_status()
        parsed = parse_council_register(response.text, config, fetched_at)
        seed = _council_seed(config, fetched_at)
        if seed:
            for key, value in seed.items():
                if parsed.get(key) is None:
                    parsed[key] = value
        parsed["retrieval_status"] = "fetched"
        return parsed
    except (requests.RequestException, ValueError):
        return _council_seed(config, fetched_at, "fetch_unavailable")


def merge_council_development(row: dict,
                              development: dict | None) -> dict:
    """Attach Council evidence and select the newest official event."""
    if not development:
        return row
    row["council_development"] = development
    event = {
        "date": development.get("date"),
        "title": development.get("stage") or development.get("title"),
        "source": development.get("source"),
        "document": development.get("document"),
        "url": development.get("url"),
        "document_type": development.get("document_type"),
        "addressee": development.get("addressee"),
        "meeting_date": development.get("meeting_date"),
        "content_accessible": development.get("content_accessible"),
        "retrieval_status": development.get("retrieval_status"),
        "terminal": False,
        "celexes": [],
        "documents": [development.get("document")]
                     if development.get("document") else [],
    }
    # A checked, corrected seed for the same Council document replaces the
    # older representation instead of manufacturing a duplicate event.
    row["events"] = [
        old for old in row.get("events") or []
        if not (development.get("document") and
                old.get("document") == development.get("document") and
                old.get("source") == development.get("source"))
    ] + [event]
    row["events"].sort(key=lambda item: (
        str(item.get("date") or ""), str(item.get("title") or "")))
    latest = row["events"][-1]
    row["stage"] = latest.get("title") or row.get("stage")
    row["date"] = latest.get("date") or row.get("date")
    # A political agreement is procedural evidence, never a final act.
    row["terminal"] = False
    return row


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
    row = merge_council_development(
        row, fetch_council_development(http, config, fetched_at))
    journal = [record for celex in row["adopted_celexes"]
               if (record := _official_journal_record(http, celex))]
    return apply_final_review_gate(row, config, journal)


def stale_fallback(watch_key: str, config: dict, previous: dict | None,
                   fetched_at: str, error: Exception) -> dict:
    """Preserve the last official observation after a transient fetch failure.

    The fallback is intentionally *not* a new official observation.  Status,
    stage and evidence are copied unchanged, while explicit stale metadata lets
    the exporter/UI lower confidence without inventing a transition.
    """
    if not previous:
        raise error
    row = {
        "id": watch_key,
        "procedure": previous.get("procedure") or config.get("procedure"),
        "proposal_celex": previous.get("proposal_celex") or
                          config.get("celex_proposal"),
        "title": previous.get("title") or config.get("procedure") or watch_key,
        "status": previous.get("status") or "?",
        "stage": previous.get("stage") or previous.get("status") or "?",
        "date": previous.get("date"),
        "updated": previous.get("updated"),
        "url": previous.get("url") or config.get("official_url"),
        "events": previous.get("events") or [],
        "council_development": previous.get("council_development"),
        "adopted_celexes": previous.get("adopted_celexes") or [],
        "official_journal": previous.get("official_journal") or [],
        "publication_detected": bool(previous.get("publication_detected")),
        "awaiting_final_review": bool(previous.get("awaiting_final_review")),
        "final_text_review": previous.get("final_text_review"),
        "terminal": bool(previous.get("terminal")),
        "fetched_at": fetched_at,
        "retrieval_status": "stale_fallback",
        "source_stale": True,
        "retrieval_warning": (
            "EUR-Lex refresh failed; reusing the last persisted official "
            f"observation ({type(error).__name__})."),
    }
    seed = _council_seed(config, fetched_at, "verified_seed")
    previous_council = previous.get("council_development") or {}
    if seed and str(seed.get("date") or "") >= str(
            previous_council.get("date") or ""):
        row = merge_council_development(row, seed)
        # merge_council_development correctly refuses to infer terminality.
        row["source_stale"] = True
        row["retrieval_status"] = "stale_fallback"
    return row


def fetch_watch_resilient(http: Http, watch_key: str, config: dict,
                          fetched_at: str,
                          previous: dict | None) -> dict:
    try:
        row = fetch_watch(http, watch_key, config, fetched_at)
    except (requests.RequestException, ValueError, KeyError) as exc:
        return stale_fallback(watch_key, config, previous, fetched_at, exc)
    row["retrieval_status"] = "fresh"
    row["source_stale"] = False
    row["retrieval_warning"] = None
    return row


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
    state_rows = (state or {}).get("procedures") or {}
    rows = [fetch_watch_resilient(
        http, str(key), config, fetched_at, state_rows.get(str(key)))
        for key, config in watches]
    out = snapshot_dir("eu_watch")
    write_jsonl(out / "procedures.jsonl", rows)
    for row in rows:
        print(f"  {row['procedure']}: {row['status']} / {row['stage']}"
              f" — terminal={row['terminal']}"
              f" stale={bool(row.get('source_stale'))}")
    print(f"eu-watch: {len(rows)} polled / {len(skipped)} archived -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
