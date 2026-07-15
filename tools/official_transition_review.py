"""Verify observed GII diffs against final BGBl commands and DIP dates.

This is the acceptance gate between an official retrieval observation and a
legal change event.  A state pair alone has no effective date.  A review is
published only when every changed norm is found in a final, integrity-checked
BGBl article and DIP supplies one unambiguous commencement date for the exact
amending article/sub-article reference.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 1
_ARTICLE_REF = re.compile(
    r"^(?P<article>\d+[a-z]?)"
    r"(?:\s+(?:Abs\.|Absatz)\s*(?P<absatz>\d+[a-z]?))?",
    re.IGNORECASE,
)
_ARTICLE_SCOPE = re.compile(
    r"Artikel\s+(?P<start>\d+[a-z]?)"
    r"(?:\s+bis\s+(?P<end>\d+[a-z]?))?"
    r"(?P<tail>.*?)(?=(?:\s+(?:sowie|und)\s+Artikel\s+\d+)|$)",
    re.IGNORECASE,
)
_ABS_SCOPE = re.compile(
    r"(?:Abs\.|Absatz)\s*(?P<values>\d+[a-z]?"
    r"(?:\s*(?:,|und|bis)\s*\d+[a-z]?)+|\d+[a-z]?)",
    re.IGNORECASE,
)
_NARROWING_SCOPE = re.compile(
    r"\b(?:Abs\.|Absatz|Nr\.|Nummer|Buchst\.|Buchstabe)\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u00ad", "")
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    value = value.translate(str.maketrans({
        "„": '"', "“": '"', "”": '"', "’": "'", "–": "-", "—": "-",
    }))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _expand_numbers(value: str) -> set[str]:
    pieces = re.split(r"\s*(,|und|bis)\s*", value)
    out: list[str] = []
    for index in range(0, len(pieces), 2):
        current = pieces[index].casefold()
        if index >= 2 and pieces[index - 1].casefold() == "bis" and \
                out[-1].isdigit() and current.isdigit():
            start, end = int(out[-1]), int(current)
            if start < end <= start + 100:
                out.extend(str(number) for number in range(start + 1, end + 1))
                continue
        out.append(current)
    return set(out)


def article_in_scope(reference: str, explanation: str) -> bool:
    """Whether a DIP exception clause includes one GII amending reference."""
    ref = _ARTICLE_REF.match(str(reference or "").strip())
    if ref is None:
        return False
    article = ref.group("article").casefold()
    absatz = (ref.group("absatz") or "").casefold()
    for match in _ARTICLE_SCOPE.finditer(str(explanation or "")):
        start = match.group("start").casefold()
        end = (match.group("end") or start).casefold()
        if article.isdigit() and start.isdigit() and end.isdigit():
            included = int(start) <= int(article) <= int(end)
        else:
            included = article == start == end
        if not included:
            continue
        abs_scope = _ABS_SCOPE.search(match.group("tail") or "")
        if not abs_scope:
            return True
        if not absatz:
            # The exception narrows only part of this article, while the GII
            # citation does not identify that part.  Treat it as ambiguous.
            return True
        return absatz in _expand_numbers(abs_scope.group("values"))
    return False


def article_scope_is_ambiguous(reference: str, explanation: str) -> bool:
    """Whether an exception narrows an article beyond the GII citation.

    GII's ``stand`` often says only ``Art. 1`` while DIP assigns different
    dates to individual Nummern/Buchstaben inside that article.  One matching
    exception date is still not enough: without the sub-reference Lexgraph
    cannot know whether the observed norm change belongs to that exception.
    """
    ref = _ARTICLE_REF.match(str(reference or "").strip())
    if ref is None or ref.group("absatz"):
        return False
    article = ref.group("article").casefold()
    for match in _ARTICLE_SCOPE.finditer(str(explanation or "")):
        start = match.group("start").casefold()
        end = (match.group("end") or start).casefold()
        if article.isdigit() and start.isdigit() and end.isdigit():
            included = int(start) <= int(article) <= int(end)
        else:
            included = article == start == end
        if included and _NARROWING_SCOPE.search(match.group("tail") or ""):
            return True
    return False


def effective_date_for(reference: str, rows: Iterable[dict]) -> str | None:
    """Resolve a specific amending reference without spreading bill dates."""
    rows = [row for row in rows if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", str(row.get("datum") or ""))]
    exceptions = [row for row in rows if str(
        row.get("erlaeuterung") or "").strip()]
    matching_rows = [row for row in exceptions if article_in_scope(
        reference, str(row.get("erlaeuterung") or ""))]
    if any(article_scope_is_ambiguous(
            reference, str(row.get("erlaeuterung") or ""))
           for row in matching_rows):
        return None
    matches = [row["datum"] for row in matching_rows]
    if matches:
        unique = set(matches)
        return next(iter(unique)) if len(unique) == 1 else None
    defaults = {str(row["datum"]) for row in rows
                if not str(row.get("erlaeuterung") or "").strip()}
    return next(iter(defaults)) if len(defaults) == 1 else None


def _inserted_phrases(old: str, new: str) -> list[str]:
    before, after = _text(old).split(), _text(new).split()
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    phrases = []
    for op, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if op in {"insert", "replace"} and j2 > j1:
            phrase = " ".join(after[j1:j2]).strip()
            if len(phrase) >= 32:
                phrases.append(phrase)
    return sorted(set(phrases), key=len, reverse=True)


def command_matches(change: dict, article_text: str) -> bool:
    """Mechanical final-command check for one complete-norm state diff."""
    haystack = _text(article_text)
    label = _text(change.get("para"))
    if not label or label not in haystack:
        return False
    operation = change.get("operation")
    if operation == "delete":
        return any(word in haystack for word in (
            "gestrichen", "aufgehoben", "weggefallen"))
    phrases = _inserted_phrases(
        str(change.get("old") or ""), str(change.get("new") or ""))
    if not phrases:
        return False
    return any(phrase in haystack for phrase in phrases)


def _documents_by_act(documents: Iterable[dict]) -> dict[str, list[tuple[dict, dict]]]:
    by_act: dict[str, list[tuple[dict, dict]]] = {}
    for document in documents:
        pdf = urlparse(str(document.get("official_pdf_url") or ""))
        if not document.get("integrity_verified") or not re.fullmatch(
                r"[0-9a-f]{64}", str(document.get("sha256") or "")) or \
                not re.fullmatch(r"\d+", str(
                    document.get("procedure_id") or "")) or \
                pdf.scheme != "https" or pdf.hostname != "www.recht.bund.de":
            continue
        for reference in document.get("referenced_corpus_acts") or []:
            jurabk = str(reference.get("jurabk") or "")
            if jurabk:
                by_act.setdefault(jurabk, []).append((document, reference))
    return by_act


def review_transitions(transitions: Iterable[dict],
                       documents: Iterable[dict]) -> list[dict]:
    """Return only transitions passing state, final-text, and date gates."""
    by_act = _documents_by_act(documents)
    reviews = []
    for transition in transitions:
        changes = list(transition.get("changes") or [])
        if not changes or not transition.get("full_state_pair"):
            continue
        candidates = []
        for document, reference in by_act.get(
                str(transition.get("jurabk") or ""), []):
            top_article = str(reference.get("article") or "").split()[0]
            section = next((row for row in document.get("articles") or []
                            if str(row.get("article") or "").casefold()
                            == top_article.casefold()), None)
            effective = effective_date_for(
                str(reference.get("article") or ""),
                document.get("dip_entry_into_force") or [])
            if section is None or effective is None:
                continue
            candidates.append({
                "document": document,
                "reference": reference,
                "section": section,
                "effective_at": effective,
            })

        matched = []
        for change in changes:
            hits = [candidate for candidate in candidates if command_matches(
                change, str(candidate["section"].get("text") or ""))]
            if len(hits) != 1:
                matched = []
                break
            matched.append((change, hits[0]))
        if len(matched) != len(changes):
            continue
        effective_dates = {row["effective_at"] for _change, row in matched}
        documents_used = {row["document"]["document_id"]: row["document"]
                          for _change, row in matched}
        if len(effective_dates) != 1 or len(documents_used) != 1:
            continue
        document = next(iter(documents_used.values()))
        effective_at = next(iter(effective_dates))
        publication = str(document.get("publication_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication):
            continue
        articles = sorted({str(row["reference"].get("article") or "")
                           for _change, row in matched})
        review_id = hashlib.sha256(json.dumps([
            transition.get("act_id"), transition.get("previous_state_sha256"),
            transition.get("state_sha256"), document["document_id"], articles,
            effective_at,
        ], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]
        reviews.append({
            "id": f"fed-review:{review_id}",
            "schema_version": SCHEMA_VERSION,
            "act_id": transition["act_id"],
            "act": transition["jurabk"],
            "jurabk": transition["jurabk"],
            "published_at": publication,
            "effective_at": effective_at,
            "retroactive": effective_at < publication,
            "observed_at": transition["observed_at"],
            "previous_observed_at": transition["previous_observed_at"],
            "date_basis": "official_bgbl_command_and_commencement_clause",
            "verification": "official_final_text_and_complete_state_pair",
            "state_sha256": transition["state_sha256"],
            "previous_state_sha256": transition["previous_state_sha256"],
            "old_builddate": transition.get("old_builddate"),
            "new_builddate": transition.get("new_builddate"),
            "changes": changes,
            "amending_articles": articles,
            "procedure_id": document.get("procedure_id"),
            "bgbl": {
                "document_id": document["document_id"],
                "year": document["year"],
                "issue": document["issue"],
                "eli": document.get("eli"),
                "pdf_url": document.get("official_pdf_url"),
                "pdf_sha256": document["sha256"],
                "text_sha256": document.get("text_sha256"),
                "integrity_verified": True,
            },
            "evidence": [
                {"source": "GII", "url": transition.get("source_url"),
                 "snapshot": transition["previous_observed_at"],
                 "state_sha256": transition["previous_state_sha256"]},
                {"source": "BGBl", "url": document.get("official_pdf_url"),
                 "document": document["document_id"],
                 "sha256": document["sha256"]},
                {"source": "DIP",
                 "url": ("https://dip.bundestag.de/vorgang/"
                         f"{document.get('procedure_id')}"),
                 "procedure": document.get("procedure_id")},
                {"source": "GII", "url": transition.get("source_url"),
                 "snapshot": transition["observed_at"],
                 "state_sha256": transition["state_sha256"]},
            ],
            "derivation": {
                "tool": "lexgraph-official-transition-review",
                "algorithm": "gii-state-pair+bgbl-final-command+dip-commencement",
                "effective_dates_inferred": False,
            },
        })
    return sorted(reviews, key=lambda row: (
        row["effective_at"], row["id"]), reverse=True)
