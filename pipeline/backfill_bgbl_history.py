"""Build an official-only retrospective BGBl amendment inventory.

This pipeline discovers promulgated Bundestag procedures for electoral
periods 20 and 21, narrows them to the curated federal GII corpus using DIP's
``Rechtsmaterialien`` descriptors, and then verifies every association in the
final, integrity-checked Bundesgesetzblatt text from recht.bund.de.

The result is deliberately an *amendment candidate inventory*.  It records
which final BGBl article amends which curated act, its publication and
execution dates, and an effective date only where DIP's commencement clauses
resolve the complete amending article unambiguously.  It does not infer or
reconstruct a historical consolidated law text.

Official inputs:
    - Gesetze-im-Internet snapshot: curated act identifiers and canonical names
    - Deutscher Bundestag DIP API: final procedure, promulgation and dates
    - recht.bund.de: final BGBl HTML/PDF and advertised checksum

Output:
    data/snapshots/bgbl_history_backfill/<date>/documents.jsonl
    data/snapshots/bgbl_history_backfill/<date>/candidates.jsonl
    data/snapshots/bgbl_history_backfill/<date>/summary.json

The immutable PDF/text objects are shared with ``fetch_bgbl_documents.py``:
    data/bgbl_documents/objects/<sha256>.pdf
    data/bgbl_documents/texts/<sha256>.txt

Usage:
    python3 pipeline/backfill_bgbl_history.py
    python3 pipeline/backfill_bgbl_history.py --start 2023-01-01 --limit 20
    python3 pipeline/backfill_bgbl_history.py --inventory-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ``official_transition_review`` is a reusable build tool rather than a
# pipeline module.  Keep the script directly executable from the repository.
TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from common import ROOT, Http, latest_snapshot, read_jsonl, snapshot_dir
from fetch_bgbl_documents import (
    SourceIntegrityError,
    _atomic_jsonl,
    _validate_cached,
    canonical_urls,
    capture_document,
    load_cached_documents,
    reuse_document,
)
from fetch_dip import BASE as DIP_BASE
from fetch_dip import current_key
from patch_parser import _split_items, parse_command
SCHEMA_VERSION = 1
DEFAULT_START = "2023-01-01"
DEFAULT_WAHLPERIODEN = (20, 21)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAIN_AMENDMENT_RE = re.compile(
    r"\b(?:wird|werden)\b.{0,180}?"
    r"\b(?:geändert|gefasst|aufgehoben|gestrichen)\b",
    re.IGNORECASE,
)
ARTICLE_ID_PATTERN = r"\d+(?:[a-z]\d*)?"
RETRO_ARTICLE_ID_PATTERN = r"\d+(?:[ \t]*[a-z]\d*)?"
ARTICLE_MARKER_RE = re.compile(
    rf"\bArtikel\s+(?={ARTICLE_ID_PATTERN})", re.IGNORECASE)
ARTICLE_LIST_RE = re.compile(
    r"^\s*(?P<items>"
    rf"{ARTICLE_ID_PATTERN}(?:\s+bis\s+{ARTICLE_ID_PATTERN})?"
    r"(?:\s*(?:,|und|sowie)\s*"
    rf"{ARTICLE_ID_PATTERN}(?:\s+bis\s+{ARTICLE_ID_PATTERN})?)*"
    r")",
    re.IGNORECASE,
)
ARTICLE_UNIT_SCOPE_RE = re.compile(
    r"^(?:Nr\.|Nummer|Abs\.|Absatz|Buchst\.|Buchstabe|§)",
    re.IGNORECASE,
)
SHORTHAND_NARROWED_ARTICLE_RE = re.compile(
    rf"(?:,|und|sowie)\s*(?P<article>{ARTICLE_ID_PATTERN})\s+"
    r"(?=(?:Nr\.|Nummer|Abs\.|Absatz|Buchst\.|Buchstabe|§))",
    re.IGNORECASE,
)
RETRO_ARTICLE_RE = re.compile(
    rf"(?m)^[ \t]*Artikel[ \t]+"
    rf"(?P<number>{RETRO_ARTICLE_ID_PATTERN}|[IVXLCDM]+)[ \t]*$",
    re.IGNORECASE,
)
RETRO_ENTRY_RE = re.compile(
    r"(?im)^[ \t]*Inkrafttreten(?:\s+und\s+[^\n]+)?[ \t]*$")

# GII's long title is authoritative, but amendment articles normally use a
# legally defined short title.  These aliases are exact names found in final
# BGBl amendment headings/DIP's Rechtsmaterialien vocabulary.  They are not
# fuzzy search terms.
EXACT_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "asylvfg_1992": ("Asylgesetz",),
    "aufenthg_2004": ("Aufenthaltsgesetz",),
    "azrg": ("AZR-Gesetz", "Gesetz über das Ausländerzentralregister"),
    "baf_g": ("Bundesausbildungsförderungsgesetz",),
    "beeg": ("Bundeselterngeld- und Elternzeitgesetz",),
    "beg": ("Bundesentschädigungsgesetz",),
    "berathig": ("Beratungshilfegesetz",),
    "beschv_2013": ("Beschäftigungsverordnung",),
    "bkgg_1996": ("Bundeskindergeldgesetz",),
    "bvfg": ("Bundesvertriebenengesetz",),
    "entgfg": ("Entgeltfortzahlungsgesetz",),
    "freiz_gg_eu_2004": ("Freizügigkeitsgesetz/EU",),
    "gewschg": ("Gewaltschutzgesetz",),
    "gg": ("Grundgesetz",),
    "idnrg": ("Identifikationsnummerngesetz",),
    "intv": ("Integrationskursverordnung",),
    "milog": ("Mindestlohngesetz",),
    "ozg": ("Onlinezugangsgesetz",),
    "rbeg_2021": ("Regelbedarfsermittlungsgesetz",),
    "stag": ("Staatsangehörigkeitsgesetz",),
    "uhvorschg": ("Unterhaltsvorschussgesetz",),
}

SGB_NAMES: dict[int, tuple[str, str]] = {
    1: ("Erstes Buch Sozialgesetzbuch", "Sozialgesetzbuch I"),
    2: ("Zweites Buch Sozialgesetzbuch", "Sozialgesetzbuch II"),
    3: ("Drittes Buch Sozialgesetzbuch", "Sozialgesetzbuch III"),
    4: ("Viertes Buch Sozialgesetzbuch", "Sozialgesetzbuch IV"),
    5: ("Fünftes Buch Sozialgesetzbuch", "Sozialgesetzbuch V"),
    6: ("Sechstes Buch Sozialgesetzbuch", "Sozialgesetzbuch VI"),
    7: ("Siebtes Buch Sozialgesetzbuch", "Sozialgesetzbuch VII"),
    8: ("Achtes Buch Sozialgesetzbuch", "Sozialgesetzbuch VIII"),
    9: ("Neuntes Buch Sozialgesetzbuch", "Sozialgesetzbuch IX"),
    10: ("Zehntes Buch Sozialgesetzbuch", "Sozialgesetzbuch X"),
    11: ("Elftes Buch Sozialgesetzbuch", "Sozialgesetzbuch XI"),
    12: ("Zwölftes Buch Sozialgesetzbuch", "Sozialgesetzbuch XII"),
    14: ("Vierzehntes Buch Sozialgesetzbuch", "Sozialgesetzbuch XIV"),
}


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_legal_name(value: object) -> str:
    """Normalize typography only; never stem, truncate or fuzzy-match."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "")
    text = text.translate(str.maketrans({"–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", text).strip(" -").casefold()


def act_aliases(act: dict) -> tuple[str, ...]:
    """Return exact official names usable for descriptor/final-text joins."""
    raw = {_compact(act.get("long_title"))}
    raw.update(EXACT_NAME_ALIASES.get(str(act.get("slug") or ""), ()))
    match = re.fullmatch(r"sgb_(\d+)(?:_\d+)?", str(act.get("slug") or ""))
    if match:
        raw.update(SGB_NAMES.get(int(match.group(1)), ()))
    # One-character or abbreviation-only aliases are intentionally excluded:
    # an occurrence of "GG" or "BGB" is not proof of an amendment target.
    aliases = {normalize_legal_name(item) for item in raw
               if len(normalize_legal_name(item)) >= 8}
    return tuple(sorted(aliases, key=lambda item: (-len(item), item)))


def load_curated_acts() -> list[dict]:
    latest = latest_snapshot("gii")
    path = latest / "acts.jsonl" if latest else None
    if path is None or not path.is_file():
        raise FileNotFoundError("no GII acts snapshot")
    rows = []
    for act in read_jsonl(path):
        if not act.get("slug") or not act.get("jurabk") or not act.get(
                "long_title"):
            continue
        row = dict(act)
        row["aliases"] = list(act_aliases(row))
        rows.append(row)
    return sorted(rows, key=lambda row: str(row["slug"]))


def _valid_date(value: object) -> str | None:
    text = str(value or "")
    if not ISO_DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def fetch_promulgated_procedures(http: Http, key: str, start: str, end: str,
                                 wahlperioden: Iterable[int]) -> list[dict]:
    """Fetch final DIP legislation metadata, deduplicated by procedure ID."""
    if not (_valid_date(start) and _valid_date(end)) or start > end:
        raise ValueError(f"invalid retrospective range: {start}..{end}")
    found: dict[str, dict] = {}
    for wahlperiode in wahlperioden:
        cursor = None
        while True:
            params: dict[str, Any] = {
                "f.vorgangstyp": "Gesetzgebung",
                "f.wahlperiode": int(wahlperiode),
                "f.beratungsstand": "Verkündet",
                # DIP applies this to the range of associated documents.  The
                # exact publication range is enforced again below.
                "f.datum.start": start,
                "f.datum.end": end,
                "apikey": key,
            }
            if cursor:
                params["cursor"] = cursor
            response = http.get(f"{DIP_BASE}/vorgang", params=params,
                                timeout=60)
            if response.status_code == 401:
                key = current_key(http)
                params["apikey"] = key
                response = http.get(f"{DIP_BASE}/vorgang", params=params,
                                    timeout=60)
            response.raise_for_status()
            payload = response.json()
            documents = payload.get("documents") or []
            for procedure in documents:
                procedure_id = str(procedure.get("id") or "")
                if procedure_id.isdigit():
                    found[procedure_id] = procedure
            next_cursor = payload.get("cursor")
            if not documents or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    return sorted(found.values(), key=lambda row: str(row.get("id") or ""))


def _legal_material_names(procedure: dict) -> set[str]:
    return {
        normalize_legal_name(row.get("name"))
        for row in procedure.get("deskriptor") or []
        if str(row.get("typ") or "") == "Rechtsmaterialien" and row.get("name")
    }


def descriptor_matches(procedure: dict, acts: Iterable[dict]) -> list[dict]:
    """Select corpus acts by an exact DIP legal-material descriptor."""
    names = _legal_material_names(procedure)
    matched = []
    for act in acts:
        aliases = set(act.get("aliases") or act_aliases(act))
        exact = sorted(names.intersection(aliases), key=lambda item: (-len(item), item))
        if exact:
            matched.append({
                "slug": act["slug"],
                "jurabk": act["jurabk"],
                "long_title": _compact(act["long_title"]),
                "descriptor_aliases": exact,
                "aliases": list(aliases),
            })
    return sorted(matched, key=lambda row: str(row["slug"]))


def select_document_candidates(procedures: Iterable[dict], acts: Iterable[dict],
                               start: str, end: str) -> list[dict]:
    """Create unique BGBl I candidates, retaining every matching DIP row."""
    selected: dict[str, dict] = {}
    for procedure in procedures:
        matches = descriptor_matches(procedure, acts)
        if not matches:
            continue
        procedure_id = str(procedure.get("id") or "")
        for promulgation in procedure.get("verkuendung") or []:
            if str(promulgation.get("verkuendungsblatt_kuerzel") or "") != "BGBl I":
                continue
            publication = _valid_date(promulgation.get("verkuendungsdatum"))
            year = str(promulgation.get("jahrgang") or "")
            issue = str(promulgation.get("heftnummer") or "")
            if publication is None or not start <= publication <= end:
                continue
            try:
                urls = canonical_urls(year, issue)
            except ValueError:
                continue
            document_id = f"bgbl-1-{int(year):04d}-{int(issue)}"
            procedure_ref = {
                "procedure_id": procedure_id,
                "wahlperiode": procedure.get("wahlperiode"),
                "procedure_title": _compact(procedure.get("titel")),
                "procedure_status": procedure.get("beratungsstand"),
                "procedure_updated_at": procedure.get("aktualisiert"),
                "dip_entry_into_force": procedure.get("inkrafttreten") or [],
                "matched_acts": matches,
            }
            existing = selected.get(document_id)
            if existing is None:
                selected[document_id] = {
                    "document_id": document_id,
                    "year": str(int(year)),
                    "issue": str(int(issue)),
                    "title": _compact(procedure.get("titel")),
                    "eli": str(promulgation.get("pdf_url") or urls["eli"]),
                    "official_html_url": urls["html"],
                    "official_pdf_url": urls["pdf"],
                    "publication_date": publication,
                    "execution_date": _valid_date(
                        promulgation.get("ausfertigungsdatum")),
                    "procedure_id": procedure_id,
                    "procedure_status": procedure.get("beratungsstand"),
                    "procedure_updated_at": procedure.get("aktualisiert"),
                    "dip_entry_into_force": procedure.get("inkrafttreten") or [],
                    "referenced_corpus_acts": [],
                    "retrospective_procedures": [procedure_ref],
                }
                continue
            # Different final procedures must not silently claim the same
            # issue/date with contradictory official metadata.
            if existing["publication_date"] != publication or (
                    existing.get("execution_date") and
                    _valid_date(promulgation.get("ausfertigungsdatum")) and
                    existing["execution_date"] != _valid_date(
                        promulgation.get("ausfertigungsdatum"))):
                raise SourceIntegrityError(
                    f"conflicting DIP metadata for {document_id}")
            existing["retrospective_procedures"].append(procedure_ref)
    rows = list(selected.values())
    rows.sort(key=lambda row: (
        str(row["publication_date"]), int(row["year"]), int(row["issue"])),
        reverse=True)
    return rows


def load_backfill_documents() -> dict[str, dict]:
    """Load validated documents from both current and retrospective stores."""
    rows = load_cached_documents()
    latest = latest_snapshot("bgbl_history_backfill")
    path = latest / "documents.jsonl" if latest else None
    if path and path.is_file():
        for row in read_jsonl(path):
            if row.get("document_id") and _validate_cached(row):
                rows[str(row["document_id"])] = row
    return rows


def capture_candidates(http: Http, candidates: Iterable[dict], refresh: bool,
                       limit: int | None = None) -> tuple[list[dict], int, int]:
    """Capture or reuse candidate final documents; fail the batch on error."""
    cached = load_backfill_documents()
    rows = list(candidates)
    if limit is not None:
        rows = rows[:limit]
    captured = reused = 0
    documents = []
    for index, candidate in enumerate(rows, start=1):
        previous = None if refresh else cached.get(candidate["document_id"])
        if previous:
            row = reuse_document(previous, candidate)
            # ``reuse_document`` predates retrospective procedure evidence.
            row["retrospective_procedures"] = candidate[
                "retrospective_procedures"]
            reused += 1
            action = "cached"
        else:
            row = capture_document(http, candidate)
            captured += 1
            action = "captured"
        row = with_retrospective_sections(row)
        documents.append(row)
        print(f"[{index:03d}/{len(rows):03d}] {action:8} "
              f"BGBl I {row['year']} Nr. {row['issue']}")
    return documents, captured, reused


def parse_retrospective_article_sections(
        text: str) -> tuple[list[dict], dict | None]:
    """Split final BGBl text, including article IDs such as ``8z1``.

    The generic current-document parser historically accepted one optional
    suffix letter.  Amendment acts can continue past ``8z`` as ``8z1`` etc.;
    omitting those would lose real amendment dates and commands.
    """
    matches = list(RETRO_ARTICLE_RE.finditer(text))
    sections: list[dict] = []
    entry = None
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip()
        lines = block.splitlines()
        heading = next((_compact(line) for line in lines[1:] if line.strip()), None)
        number = re.sub(r"\s+", "", match.group("number"))
        section = {"article": number, "heading": heading, "text": block}
        sections.append(section)
        if entry is None and RETRO_ENTRY_RE.search(block):
            entry = dict(section)
            entry["effective_dates_inferred"] = False
    return sections, entry


def with_retrospective_sections(document: dict) -> dict:
    """Reparse the integrity-checked CAS text with retrospective IDs."""
    relative = str(document.get("text_object") or "")
    path = (ROOT / relative).resolve()
    text_root = (ROOT / "data" / "bgbl_documents" / "texts").resolve()
    try:
        path.relative_to(text_root)
    except ValueError as exc:
        raise SourceIntegrityError(
            f"retrospective text path escapes CAS: {relative}") from exc
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != document.get("text_sha256"):
        raise SourceIntegrityError(
            f"retrospective text hash mismatch: {relative}")
    sections, entry = parse_retrospective_article_sections(
        payload.decode("utf-8"))
    row = dict(document)
    row["articles"] = sections
    row["entry_into_force"] = entry
    row["retrospective_article_parser"] = 1
    return row


def legal_name_forms(alias: str) -> tuple[str, ...]:
    """Return exact, mechanically bounded German case forms of a law name.

    Final amendment prose inflects defined short titles: ``Bürgerliches
    Gesetzbuch`` becomes ``das Bürgerliche Gesetzbuch`` or ``des Bürgerlichen
    Gesetzbuchs``.  These transformations enumerate those forms; they do not
    stem words or accept edit-distance matches.
    """
    base = normalize_legal_name(alias)
    if not base:
        return ()
    forms = {base}
    words = base.split()

    # Productive genitive for Gesetz/Gesetzbuch compounds, including a title
    # head followed by a qualifier ("Grundgesetz für ...").
    for index, word in enumerate(words):
        suffix = None
        if word.endswith("gesetzbuch"):
            suffix = "s"
        elif word.endswith("gesetz"):
            suffix = "es"
        if suffix:
            changed = list(words)
            changed[index] = word + suffix
            forms.add(" ".join(changed))

    # Weak/nominative adjective forms used after articles.
    if words[0].endswith("es"):
        nominative = list(words)
        nominative[0] = words[0][:-1]  # bürgerliches -> bürgerliche
        forms.add(" ".join(nominative))

        genitive = list(words)
        genitive[0] = words[0][:-2] + "en"
        if len(genitive) > 1 and genitive[1] == "buch":
            # "des Fünften Buches Sozialgesetzbuch"
            genitive[1] = "buches"
        elif genitive[-1].endswith("gesetzbuch"):
            genitive[-1] += "s"
        elif genitive[-1].endswith("gesetz"):
            genitive[-1] += "es"
        forms.add(" ".join(genitive))
    return tuple(sorted(forms, key=lambda item: (-len(item), item)))


def _bounded_name_pattern(form: str) -> str:
    return r"(?<![\w/])" + re.escape(form) + r"(?![\w/])"


def _name_in_final_article(alias: str, section: dict) -> bool:
    """Require an exact target in the amendment heading/legal preamble.

    Replacement text often mentions other corpus acts.  Consequently a name
    appearing after the first operative amendment clause is not evidence that
    the surrounding BGBl article amends that act.
    """
    forms = legal_name_forms(alias)
    if not forms:
        return False
    heading = normalize_legal_name(section.get("heading"))
    complete = normalize_legal_name(section.get("text"))
    is_amending_section = bool(re.match(
        r"^(?:(?:weitere|sonstige)\s+)?(?:änderung|aufhebung|neufassung|"
        r"folgeänderungen)\b", heading))

    # Headings are accepted only when they explicitly describe an amendment,
    # repeal or recast.  This rejects nested replacement provisions such as a
    # standalone "Artikel 94" line inside a constitutional amendment.
    heading_prefix = r"^(?:weitere\s+)?(?:änderung|aufhebung|neufassung)\s+"
    for form in forms:
        target = _bounded_name_pattern(form)
        if re.search(heading_prefix + r"(?:des|der|von)\s+" + target +
                     r"(?:\s*[\d*]+)?$", heading):
            return True

    # The legally operative preamble ends at its first ``wird/werden ...
    # geändert/gefasst/...``.  Anything after that boundary is command or
    # replacement text and cannot establish the article's target.
    operation = MAIN_AMENDMENT_RE.search(complete)
    if is_amending_section and operation:
        preamble = complete[:operation.end()]
        if any(re.search(_bounded_name_pattern(form), preamble)
               for form in forms):
            return True

    # Collective Folgeänderungen articles have several preambles.  Outside the
    # first one, require the law name to be the grammatical subject and its own
    # amendment/repeal verb.  Merely appearing near "ersetzt" is insufficient.
    if not is_amending_section:
        return False
    for form in forms:
        target = _bounded_name_pattern(form)
        collective = (
            r"(?<!\w)(?:das|die|der)\s+" + target +
            r".{0,600}?\b(?:wird|werden)\b.{0,140}?"
            r"\b(?:geändert|aufgehoben|gestrichen)\b"
        )
        if re.search(collective, complete):
            return True
    return False


def exact_final_text_match(section: dict, matched_act: dict) -> str | None:
    """Return the longest exact act name confirmed by final BGBl text."""
    aliases = sorted(set(matched_act.get("aliases") or ()),
                     key=lambda item: (-len(item), item))
    for alias in aliases:
        if _name_in_final_article(alias, section):
            return alias
    return None


def _row_date(row: dict) -> str | None:
    return _valid_date(row.get("datum"))


def _expand_article_items(value: str) -> list[str]:
    """Expand a leading German article list, preserving written order."""
    parts = re.split(r"\s*(?:,|und|sowie)\s*", value.strip(),
                     flags=re.IGNORECASE)
    result: list[str] = []
    for part in parts:
        match = re.fullmatch(
            rf"(?P<start>{ARTICLE_ID_PATTERN})"
            rf"(?:\s+bis\s+(?P<end>{ARTICLE_ID_PATTERN}))?",
            part.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        start = re.sub(r"\s+", "", match.group("start")).casefold()
        end = re.sub(r"\s+", "", match.group("end") or "").casefold()
        if end and start.isdigit() and end.isdigit() and \
                int(start) <= int(end) <= int(start) + 100:
            result.extend(str(number) for number in range(
                int(start), int(end) + 1))
        else:
            result.append(start)
            if end and end != start:
                result.append(end)
    return result


def article_scope_kinds(article: str, explanation: str) -> set[str]:
    """Return ``whole``/``narrowed`` scopes for an article in one DIP clause.

    DIP commonly compresses references as ``Artikel 1, 2 und 3 Nr. 4``: the
    date covers all of Articles 1 and 2, but only Nummer 4 inside Article 3.
    Treating only the first number after ``Artikel`` would incorrectly assign
    the remainder/default date to Articles 2 or 3.
    """
    target = re.sub(r"\s+", "", str(article or "")).casefold()
    if not re.fullmatch(r"\d+(?:[a-z]\d*)?", target):
        return {"narrowed"}
    text = _compact(explanation)
    # DIP occasionally writes an article suffix with a space ("Artikel 13 b").
    # Collapse only a lowercase suffix in article-list punctuation contexts;
    # this cannot consume the capital N of the following word "Nr.".
    text = re.sub(
        r"(?<=\d)\s+([a-z]\d*)(?=\s*(?:,|und|sowie|bis|Nr\.|Nummer|"
        r"Abs\.|Absatz|Buchst\.|Buchstabe|§|$))",
        r"\1", text)
    markers = list(ARTICLE_MARKER_RE.finditer(text))
    scopes: set[str] = set()
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = text[marker.end():end]
        listed = ARTICLE_LIST_RE.match(segment)
        if listed is None:
            continue
        articles = _expand_article_items(listed.group("items"))
        tail = segment[listed.end():].lstrip(" ,;:")
        last_is_narrowed = bool(ARTICLE_UNIT_SCOPE_RE.match(tail))
        for position, current in enumerate(articles):
            if current == target:
                scopes.add("narrowed" if (
                    last_is_narrowed and position == len(articles) - 1
                ) else "whole")
        # After a narrowed item, DIP may continue ``..., 13a Nr. 2`` without
        # repeating the word Artikel.  Number-before-Nr is distinguishable
        # from a Nummer list, which is written Nr-before-number.
        for shorthand in SHORTHAND_NARROWED_ARTICLE_RE.finditer(tail):
            shorthand_article = re.sub(
                r"\s+", "", shorthand.group("article")).casefold()
            if shorthand_article == target:
                scopes.add("narrowed")
    return scopes


def resolve_article_effective_date(article: str,
                                   rows: Iterable[dict]) -> dict:
    """Resolve a complete amending article from official DIP clauses.

    A clause scoped to ``Nr.``, ``Buchst.`` or ``Absatz`` inside the article
    makes a single article-wide date impossible, unless the GII/DIP reference
    itself narrows to that exact scope.  Retrospective candidates currently
    reference complete final articles, so such cases fail closed.
    """
    valid = [dict(row) for row in rows if _row_date(row)]
    scoped: list[tuple[dict, set[str]]] = []
    for row in valid:
        explanation = _compact(row.get("erlaeuterung"))
        kinds = article_scope_kinds(article, explanation) if explanation else set()
        if kinds:
            scoped.append((row, kinds))
    if any("narrowed" in kinds for _row, kinds in scoped):
        return {
            "effective_at": None,
            "status": "unresolved_sub_article_scope",
            "evidence": [row for row, _kinds in scoped],
        }
    scoped_dates = {_row_date(row) for row, _kinds in scoped}
    if len(scoped_dates) == 1:
        return {
            "effective_at": next(iter(scoped_dates)),
            "status": "resolved_explicit_article_clause",
            "evidence": [row for row, _kinds in scoped],
        }
    if len(scoped_dates) > 1:
        return {
            "effective_at": None,
            "status": "unresolved_conflicting_article_clauses",
            "evidence": [row for row, _kinds in scoped],
        }

    defaults = [row for row in valid
                if not _compact(row.get("erlaeuterung"))]
    remainder = [row for row in valid if normalize_legal_name(
        row.get("erlaeuterung")) in {"im übrigen", "im uebrigen"}]
    fallback = defaults or remainder
    fallback_dates = {_row_date(row) for row in fallback}
    if len(fallback_dates) == 1:
        return {
            "effective_at": next(iter(fallback_dates)),
            "status": ("resolved_default_clause" if defaults else
                       "resolved_remainder_clause"),
            "evidence": fallback,
        }
    return {
        "effective_at": None,
        "status": ("unresolved_conflicting_default_clauses" if fallback else
                   "unresolved_no_article_wide_clause"),
        "evidence": fallback,
    }


def _candidate_id(act_slug: str, document_id: str, article: str) -> str:
    digest = hashlib.sha256(
        f"{act_slug}\0{document_id}\0{article}".encode("utf-8")
    ).hexdigest()[:20]
    return f"fed-bgbl-candidate:{digest}"


def _web_act_id(jurabk: str) -> str:
    """Mirror the stable public/CAS id derived from the official jurabk."""
    return "fed_" + re.sub(
        r"[^a-z0-9]+", "_", str(jurabk).lower()).strip("_")


def parse_amendment_commands(section: dict) -> tuple[list[dict], list[str]]:
    """Extract conservative norm addresses from one final BGBl article.

    This is discovery metadata, not a reconstructed state.  The first article
    marker and heading are removed so ``Artikel 4`` (the amendment article)
    cannot be confused with ``Artikel 4 GG`` (the amended norm).  Commands
    retain their raw official wording and parser result for later review.
    """
    text = str(section.get("text") or "")
    lines = text.splitlines()
    if lines and re.fullmatch(
            rf"\s*Artikel\s+(?:{ARTICLE_ID_PATTERN}|[IVXLCDM]+)\s*",
            lines[0], flags=re.IGNORECASE):
        lines = lines[1:]
    if lines and normalize_legal_name(lines[0]) == normalize_legal_name(
            section.get("heading")):
        lines = lines[1:]
    body = "\n".join(lines)
    parsed: list[dict] = []
    affected: set[str] = set()
    for item, raw in enumerate(_split_items(body), start=1):
        command = parse_command(raw)
        ref = command.get("ref") or {}
        if not ref.get("para"):
            article = re.search(
                r"\b(?:Artikel|Art\.)\s+(\d+\s*[a-z]?)\b", raw,
                flags=re.IGNORECASE)
            if article:
                ref["article"] = article.group(1).replace(" ", "")
        command["ref"] = ref
        command["item"] = item
        # Keep an operative container even when its leaf address is nested;
        # discard pure boilerplate with neither an operation nor an address.
        if command.get("operation") == "other" and not ref:
            continue
        parsed.append(command)
        if ref.get("para"):
            affected.add(f"§ {ref['para']}")
        if ref.get("article"):
            affected.add(f"Art. {ref['article']}")
    return parsed, sorted(affected, key=lambda value: (
        0 if value.startswith("§") else 1,
        re.sub(r"\D", "", value).zfill(8), value))


def build_inventory(documents: Iterable[dict]) -> list[dict]:
    """Verify descriptor discoveries against final BGBl article text."""
    inventory: dict[str, dict] = {}
    for document in documents:
        if not document.get("integrity_verified") or not re.fullmatch(
                r"[0-9a-f]{64}", str(document.get("sha256") or "")):
            continue
        sections = document.get("articles") or []
        for procedure in document.get("retrospective_procedures") or []:
            commencement = procedure.get("dip_entry_into_force") or []
            for matched_act in procedure.get("matched_acts") or []:
                for section in sections:
                    alias = exact_final_text_match(section, matched_act)
                    if alias is None:
                        continue
                    article = _compact(section.get("article"))
                    if not article:
                        continue
                    effective = resolve_article_effective_date(
                        article, commencement)
                    article_text = str(section.get("text") or "")
                    commands, affected_norms = parse_amendment_commands(
                        section)
                    candidate_id = _candidate_id(
                        str(matched_act["slug"]),
                        str(document["document_id"]), article)
                    row = {
                        "id": candidate_id,
                        "schema_version": SCHEMA_VERSION,
                        "candidate_only": True,
                        "historical_text_reconstructed": False,
                        "act_id": _web_act_id(str(matched_act["jurabk"])),
                        "slug": matched_act["slug"],
                        "jurabk": matched_act["jurabk"],
                        "long_title": matched_act["long_title"],
                        "document_id": document["document_id"],
                        "procedure_id": str(procedure.get("procedure_id") or ""),
                        "wahlperiode": procedure.get("wahlperiode"),
                        "procedure_title": procedure.get("procedure_title"),
                        "amending_article": article,
                        "article_heading": section.get("heading"),
                        "match_basis": "exact_name_in_final_bgbl_article",
                        "matched_legal_name": alias,
                        "descriptor_aliases": matched_act.get(
                            "descriptor_aliases") or [],
                        "execution_date": document.get("execution_date"),
                        "publication_date": document.get("publication_date"),
                        "effective_at": effective["effective_at"],
                        "effective_date_status": effective["status"],
                        "commencement_evidence": effective["evidence"],
                        "dip_entry_into_force": commencement,
                        "eli": document.get("eli"),
                        "official_html_url": document.get(
                            "official_html_url"),
                        "official_pdf_url": document.get("official_pdf_url"),
                        "pdf_sha256": document.get("sha256"),
                        "pdf_md5": document.get("md5"),
                        "advertised_md5": document.get("advertised_md5"),
                        "integrity_verified": True,
                        "pdf_object": document.get("pdf_object"),
                        "text_sha256": document.get("text_sha256"),
                        "text_object": document.get("text_object"),
                        "article_text_sha256": hashlib.sha256(
                            article_text.encode("utf-8")).hexdigest(),
                        "commands": commands,
                        "command_count": len(commands),
                        "affected_norms": affected_norms,
                        "source": "gii+dip+recht.bund.de",
                    }
                    previous = inventory.get(candidate_id)
                    if previous and any(previous.get(key) != row.get(key) for key in (
                            "procedure_id", "pdf_sha256", "effective_at")):
                        raise SourceIntegrityError(
                            f"conflicting evidence for {candidate_id}")
                    inventory[candidate_id] = row
    return sorted(inventory.values(), key=lambda row: (
        str(row.get("publication_date") or ""), str(row["document_id"]),
        str(row["amending_article"]), str(row["slug"])), reverse=True)


def build_summary(*, start: str, end: str, procedures: list[dict],
                  selected: list[dict], documents: list[dict],
                  inventory: list[dict], corpus_size: int,
                  captured: int, reused: int) -> dict:
    covered = {str(row["slug"]) for row in inventory}
    resolved = [row for row in inventory if row.get("effective_at")]
    unresolved: dict[str, int] = {}
    for row in inventory:
        if row.get("effective_at"):
            continue
        status = str(row.get("effective_date_status") or "unknown")
        unresolved[status] = unresolved.get(status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "range": {"start": start, "end": end},
        "sources": ["gesetze-im-internet.de", "search.dip.bundestag.de",
                    "recht.bund.de"],
        "corpus_acts": corpus_size,
        "dip_promulgated_procedures": len(procedures),
        "selected_bgbl_documents": len(selected),
        "verified_bgbl_documents": len(documents),
        "documents_captured": captured,
        "documents_reused": reused,
        "documents_captured_this_run": captured,
        "documents_reused_this_run": reused,
        "amendment_candidates": len(inventory),
        "acts_with_verified_candidates": len(covered),
        "candidates_with_effective_date": len(resolved),
        "candidates_without_article_wide_effective_date": (
            len(inventory) - len(resolved)),
        "unresolved_effective_date_statuses": dict(sorted(
            unresolved.items())),
        "historical_texts_reconstructed": 0,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--wahlperiode", type=int, action="append",
                        dest="wahlperioden")
    parser.add_argument("--limit", type=int,
                        help="capture only N newest selected documents")
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--inventory-only", action="store_true",
                        help="discover/count candidates without downloading PDFs")
    args = parser.parse_args()
    if not (_valid_date(args.start) and _valid_date(args.end)) or \
            args.start > args.end:
        parser.error("invalid --start/--end range")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.delay < 0.2:
        parser.error("--delay must be at least 0.2 seconds")

    acts = load_curated_acts()
    http = Http(delay=args.delay, retries=4)
    key = current_key(http)
    procedures = fetch_promulgated_procedures(
        http, key, args.start, args.end,
        args.wahlperioden or DEFAULT_WAHLPERIODEN)
    selected = select_document_candidates(
        procedures, acts, args.start, args.end)
    print(f"DIP: {len(procedures)} promulgated procedures; "
          f"{len(selected)} exact-descriptor BGBl I documents")
    if args.inventory_only:
        by_act = {row["slug"] for candidate in selected
                  for procedure in candidate["retrospective_procedures"]
                  for row in procedure["matched_acts"]}
        print(f"Descriptor discovery covers {len(by_act)}/{len(acts)} corpus acts")
        return 0

    documents, captured, reused = capture_candidates(
        http, selected, args.refresh, args.limit)
    inventory = build_inventory(documents)
    summary = build_summary(
        start=args.start, end=args.end, procedures=procedures,
        selected=selected[:args.limit] if args.limit else selected,
        documents=documents, inventory=inventory, corpus_size=len(acts),
        captured=captured, reused=reused)
    out = snapshot_dir("bgbl_history_backfill")
    _atomic_jsonl(out / "documents.jsonl", documents)
    _atomic_jsonl(out / "candidates.jsonl", inventory)
    _write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
