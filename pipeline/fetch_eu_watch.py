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
# EUR-Lex states the outcome in the procedure status itself, e.g.
# "Completed (Adopted act: 32026D1912 )".  On 2026/0186/NLE the final event
# row arrives with an empty title AND an empty CELEX cell, so the status line
# is the only place the adopted act is named — without it the site kept
# showing an adopted, published procedure as still pending.
STATUS_ADOPTED_RE = re.compile(
    r"adopted\s+act\s*:?\s*(3\d{4}D\d{4,})", re.IGNORECASE)
COUNCIL_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")

# CELLAR — the Publications Office's own RDF store, on a DIFFERENT host to
# eur-lex.europa.eu.  PITFALL (2026-08-29): every eur-lex.europa.eu HTML path
# now answers 302 -> /TodayOJ/index.html for this server, both with our UA and
# a browser UA, so the procedure page and the legal-content OJ notice are both
# unreachable.  CELLAR keeps working and is the same publisher, so the adopted
# act and its Official Journal publication are sourced from it instead of
# being lost to a redirect.
CELLAR_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
LANG_ENG_URI = "http://publications.europa.eu/resource/authority/language/ENG"
OJ_L_COLLECTION = ("http://publications.europa.eu/resource/authority/"
                   "document-collection/OJ-L")


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
    } | {
        # The status line is the page's own statement about this procedure,
        # not loose page text: only an explicit "Adopted act: <CELEX>" counts,
        # so legal-context citations elsewhere still cannot leak in.
        match.group(1)
        for match in STATUS_ADOPTED_RE.finditer(status)
        if ADOPTED_DECISION_RE.match(match.group(1))
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


def council_communication_evidence(config: dict, fetched_at: str) -> dict | None:
    """Return checked-in evidence of an official Council communication.

    Consilium press pages sit behind a browser check, so this record is a
    reviewer-verified seed rather than a live fetch.  A communicated political
    agreement documents an agreement in principle; it is never treated as
    formal Council adoption, publication or applicable law.
    """
    seed = config.get("council_communication_seed")
    if not isinstance(seed, dict):
        return None
    return {
        "source": "Council press release",
        "kind": seed.get("kind") or "press_release",
        "url": seed.get("url"),
        "date": seed.get("date"),
        "title": seed.get("title"),
        "stage": seed.get("stage") or "Political agreement",
        "body": seed.get("body"),
        "fetched_at": fetched_at,
        "retrieval_status": "verified_seed",
        "terminal": False,
    }


def merge_council_communication(row: dict,
                                communication: dict | None) -> dict:
    """Attach a verified Council communication and reselect the newest event.

    Once the communication is the latest dated official event, the displayed
    stage moves past the preparatory Coreper note to the communicated
    agreement in principle.  Formal adoption, legal-linguistic finalisation
    and Official Journal publication remain outstanding: ``terminal`` stays
    false and the final-review gate is untouched.
    """
    if not communication:
        return row
    row["council_communication"] = communication
    event = {
        "date": communication.get("date"),
        "title": communication.get("stage") or communication.get("title"),
        "headline": communication.get("title"),
        "source": communication.get("source"),
        "url": communication.get("url"),
        "document_type": str(communication.get("kind") or
                             "press_release").replace("_", " ").upper(),
        "retrieval_status": communication.get("retrieval_status"),
        "terminal": False,
        "celexes": [],
        "documents": [],
    }
    # A re-checked seed for the same communication replaces the older
    # representation instead of manufacturing a duplicate event.
    row["events"] = [
        old for old in row.get("events") or []
        if not (old.get("source") == communication.get("source") and
                old.get("url") == communication.get("url"))
    ] + [event]
    row["events"].sort(key=lambda item: (
        str(item.get("date") or ""), str(item.get("title") or "")))
    latest = row["events"][-1]
    row["stage"] = latest.get("title") or row.get("stage")
    row["date"] = latest.get("date") or row.get("date")
    # An agreement in principle is procedural evidence, never a final act.
    row["terminal"] = False
    return row


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


def cellar_adopted_act(http: Http, proposal_celex: str) -> dict | None:
    """The act that adopted ``proposal_celex``, with its OJ publication.

    An act declares its own origin in CELLAR with
    ``cdm:resource_legal_adopts_resource_legal``, so this is the publisher
    stating the link, not an inference of ours: 32026D1912 adopts 52026PC0345
    and carries official-journal-act_date_publication 2026-08-04 in OJ-L.

    Returns ``None`` when nothing has been adopted yet — which is the normal
    answer for a procedure still running, and must never be mistaken for a
    retrieval failure.
    """
    if not proposal_celex:
        return None
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?celex ?pub ?collection ?title WHERE {{
  ?proposal cdm:resource_legal_id_celex "{proposal_celex}"^^xsd:string .
  ?act cdm:resource_legal_adopts_resource_legal ?proposal ;
       cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?act cdm:official-journal-act_date_publication ?pub }}
  OPTIONAL {{ ?act cdm:official-journal-act_part_of_collection_document
                   ?collection }}
  OPTIONAL {{
    ?e cdm:expression_belongs_to_work ?act ;
       cdm:expression_uses_language <{LANG_ENG_URI}> ;
       cdm:expression_title ?title .
  }}
}} LIMIT 25"""
    response = http.get(CELLAR_SPARQL, params={
        "query": query, "format": "application/sparql-results+json"},
        timeout=90)
    response.raise_for_status()
    rows = [{key: value["value"] for key, value in binding.items()}
            for binding in response.json()["results"]["bindings"]]
    # One act, but several bindings (entry-into-force dates multiply rows):
    # collapse on CELEX and keep the first publication statement seen.
    best: dict[str, dict] = {}
    for row in rows:
        celex = str(row.get("celex") or "")
        if not ADOPTED_DECISION_RE.match(celex):
            continue
        entry = best.setdefault(celex, {"celex": celex})
        for key in ("pub", "collection", "title"):
            if row.get(key) and not entry.get(key):
                entry[key] = row[key]
    for celex in sorted(best):
        entry = best[celex]
        published = str(entry.get("pub") or "")
        # Only an OJ L publication counts as promulgation evidence here.
        if not published or not str(entry.get("collection") or "").endswith("OJ-L"):
            continue
        return {
            "celex": celex,
            "citation": f"OJ L, {published}",
            "published_at": published,
            "title": " ".join(str(entry.get("title") or "").split()),
            "eli": None,
            "url": ("https://eur-lex.europa.eu/legal-content/EN/TXT/"
                    f"?uri=CELEX:{celex}"),
            "source": "CELLAR (Publications Office SPARQL)",
            "retrieval": "cellar_sparql",
        }
    return None


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
    row = merge_council_communication(
        row, council_communication_evidence(config, fetched_at))
    journal = [record for celex in row["adopted_celexes"]
               if (record := _official_journal_record(http, celex))]
    # eur-lex.europa.eu HTML is unreachable from this host (302 -> TodayOJ),
    # so the notice lookup above yields nothing even for an act that IS
    # published. Ask CELLAR, the same publisher, on its own host.
    if not journal:
        journal = _cellar_journal(http, row, config)
    return apply_final_review_gate(row, config, journal)


def _cellar_journal(http: Http, row: dict, config: dict) -> list[dict]:
    """OJ evidence for the watched proposal, straight from CELLAR."""
    proposal = str(row.get("proposal_celex") or config.get("celex_proposal") or "")
    try:
        record = cellar_adopted_act(http, proposal)
    except Exception as exc:  # noqa: BLE001
        # Deliberately wide: this runs inside the retrieval error path, whose
        # entire job is to degrade without throwing.  A malformed CELLAR reply
        # must leave the watch on its last good observation, never escalate a
        # handled failure into an unhandled one.
        print(f"  [warn] CELLAR adopted-act lookup failed: "
              f"{type(exc).__name__}")
        return []
    if not record:
        return []
    celex = record["celex"]
    if celex not in row["adopted_celexes"]:
        row["adopted_celexes"] = sorted({*row["adopted_celexes"], celex})
    print(f"  [cellar] adopted act {celex} published {record['published_at']}")
    return [record]


def stale_fallback(watch_key: str, config: dict, previous: dict | None,
                   fetched_at: str, error: Exception,
                   http: Http | None = None) -> dict:
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
        "council_communication": previous.get("council_communication"),
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
    # The procedure page being unreachable says nothing about whether the act
    # was adopted. CELLAR is a separate host and still answers, so promulgation
    # evidence is recovered here rather than frozen at the last observation.
    if not row["official_journal"]:
        recovered = _cellar_journal(http, row, config) if http else []
        if recovered:
            row = apply_final_review_gate(row, config, recovered)
            row["retrieval_status"] = "stale_fallback_cellar_publication"
            row["source_stale"] = True
            row["retrieval_warning"] = (
                "EUR-Lex procedure page unreachable "
                f"({type(error).__name__}); Official Journal publication "
                "recovered from CELLAR.")

    seed = _council_seed(config, fetched_at, "verified_seed")
    previous_council = previous.get("council_development") or {}
    if seed and str(seed.get("date") or "") >= str(
            previous_council.get("date") or ""):
        row = merge_council_development(row, seed)
        # merge_council_development correctly refuses to infer terminality.
        row["source_stale"] = True
        row["retrieval_status"] = "stale_fallback"
    communication = council_communication_evidence(config, fetched_at)
    previous_communication = previous.get("council_communication") or {}
    if communication and str(communication.get("date") or "") >= str(
            previous_communication.get("date") or ""):
        row = merge_council_communication(row, communication)
        # merge_council_communication also refuses to infer terminality.
        row["source_stale"] = True
        row["retrieval_status"] = "stale_fallback"
    return row


def fetch_watch_resilient(http: Http, watch_key: str, config: dict,
                          fetched_at: str,
                          previous: dict | None) -> dict:
    try:
        row = fetch_watch(http, watch_key, config, fetched_at)
    except (requests.RequestException, ValueError, KeyError) as exc:
        return stale_fallback(watch_key, config, previous, fetched_at, exc,
                              http)
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
