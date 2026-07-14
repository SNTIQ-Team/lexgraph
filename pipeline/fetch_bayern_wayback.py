"""Conservative Wayback backfill for historical Bavarian norm diffs.

BAYERN.RECHT exposes a current consolidation plus an official version log, but
not historical consolidations.  Archived per-norm pages can recover some old
states.  The important word is *some*: sparse captures must never be stretched
over several legal events or turned into invented creation/deletion diffs.

This fetcher therefore works state-first rather than event-first:

* only ``source == "ffn"`` version rows describe target norms;
* all cached/available captures of a document are ordered by their declared
  ``Text gilt ab`` date (capture timestamp is the fallback), then collapsed
  into distinct textual states;
* one state transition is assigned to at most one compatible official event;
* an empty old/new side is allowed only for an explicit ``eingef.``/``aufgeh.``;
* ambiguous sparse intervals are counted as unresolved and omitted.

The canonical ``data/by_diffs.jsonl`` is never an implicit output.  Generate a
candidate explicitly, inspect it, then merge it separately:

    python3 pipeline/fetch_bayern_wayback.py \
        --acts PAG,BayVwVfG --offline \
        --output scratchpad/by-wayback-candidate.jsonl

Without ``--offline``, missing CDX/page cache entries are fetched politely.
Empty or failed CDX responses are deliberately not cached.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

import requests
from bs4 import BeautifulSoup

from common import latest_snapshot, read_jsonl

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "snapshots" / "wayback_by"
UA = "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)"

CDX_ACT = (
    "https://web.archive.org/cdx/search/cdx?url="
    "gesetze-bayern.de/Content/Document/{key}-&matchType=prefix"
    "&output=json&fl=original,timestamp&filter=statuscode:200"
)
PAGE = (
    "https://web.archive.org/web/{ts}id_/"
    "https://www.gesetze-bayern.de/Content/Document/{doc}"
)

MIN_EVENT = "2016-01-01"
DEFAULT_WORKERS = 3
NR = r"\d+[a-z]?"

OP_ADD = "add"
OP_MODIFY = "modify"
OP_REPLACE = "replace"
OP_REPEAL = "repeal"
OP_UNKNOWN = "unknown"


@dataclass(frozen=True, order=True)
class NormId:
    """A target norm with the label style used by the target act."""

    prefix: str  # "Art." or "§"
    nr: str

    @property
    def label(self) -> str:
        return f"{self.prefix} {self.nr}"


@dataclass(frozen=True)
class ParsedDescriptor:
    changes: tuple[tuple[NormId, str], ...]
    unspecified: bool

    def as_dict(self) -> dict[NormId, str]:
        return dict(self.changes)


@dataclass(frozen=True)
class ChangeEvent:
    jurabk: str
    date: str
    seq: int
    description: str
    changes: tuple[tuple[NormId, str], ...]
    unspecified: bool

    @property
    def event_id(self) -> str:
        raw = f"{self.jurabk}|{self.date}|{self.seq}|{self.description}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def operation_for(self, norm: NormId) -> str | None:
        return dict(self.changes).get(norm)


@dataclass(frozen=True)
class Capture:
    doc: str
    timestamp: str
    valid: str | None
    text: str


@dataclass(frozen=True)
class State:
    doc: str
    text: str
    valid: str | None
    first_capture: str
    last_capture: str


@dataclass(frozen=True)
class Transition:
    doc: str
    old: State
    new: State

    @property
    def transition_id(self) -> str:
        old_hash = hashlib.sha256(self.old.text.encode("utf-8")).hexdigest()
        new_hash = hashlib.sha256(self.new.text.encode("utf-8")).hexdigest()
        # The same textual A->B transition may legitimately recur years later.
        # Legal/capture anchors identify the epoch while still detecting a
        # duplicate assignment of the very same observed transition.
        raw = (f"{self.doc}|{old_hash}|{new_hash}|"
               f"{self.old.valid}|{self.old.last_capture}|"
               f"{self.new.valid}|{self.new.first_capture}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Candidate:
    transition_id: str
    row: dict


# ------------------------------------------------------------------ network

def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def http_get(url: str, delay: float, tries: int = 5) -> str | None:
    """Return a non-empty 200 body; never persist failure/empty responses."""
    for attempt in range(tries):
        time.sleep(delay if attempt == 0 else 45 * attempt)
        try:
            response = requests.get(
                url, headers={"User-Agent": UA}, timeout=90)
            if response.status_code == 200 and response.text.strip():
                return response.text
            if response.status_code == 404:
                return None
        except requests.RequestException:
            pass
    return None


def cdx_act(key: str, *, offline: bool) -> dict[str, list[str]]:
    """Return ``{document -> capture timestamps}`` for one act.

    A non-empty cache is reusable.  An empty/corrupt cache is only evidence of
    an earlier failed or empty query, so online runs retry it and never replace
    it with another empty result.
    """
    path = CACHE / "cdx-act" / f"{key}.json"
    cached = _read_json(path) if path.is_file() else None
    cached_index: dict[str, list[str]] = {}
    doc_pattern = re.compile(rf"{re.escape(key)}-({NR})")
    if isinstance(cached, dict):
        for doc, timestamps in cached.items():
            if (not doc_pattern.fullmatch(str(doc))
                    or not isinstance(timestamps, list)):
                continue
            valid_timestamps = {str(timestamp) for timestamp in timestamps
                                if re.fullmatch(r"\d{14}", str(timestamp))}
            if valid_timestamps:
                cached_index[str(doc)] = sorted(valid_timestamps)
    if offline:
        return cached_index

    body = http_get(CDX_ACT.format(key=key), delay=3.0)
    if body is None:
        return cached_index
    try:
        payload = json.loads(body)
        rows = payload[1:] if isinstance(payload, list) else []
    except (ValueError, IndexError):
        return cached_index

    out: dict[str, list[str]] = {}
    pattern = re.compile(
        rf"https?://[^/]*gesetze-bayern\.de/Content/Document/"
        rf"({re.escape(key)}-({NR}))$")
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        original, timestamp = str(row[0]), str(row[1])
        match = pattern.match(original)
        if match and re.fullmatch(r"\d{14}", timestamp):
            out.setdefault(match.group(1), []).append(timestamp)
    out = {doc: sorted(set(timestamps)) for doc, timestamps in out.items()}
    if out:  # Empty/transient CDX responses must not poison future runs.
        merged: dict[str, set[str]] = defaultdict(set)
        for index in (cached_index, out):
            for doc, timestamps in index.items():
                merged[doc].update(timestamps)
        complete = {doc: sorted(timestamps)
                    for doc, timestamps in merged.items()}
        _atomic_json(path, complete)
        return complete
    return cached_index


def cached_page_index(key: str) -> dict[str, list[str]]:
    """Discover capture timestamps from page caches, even without CDX cache."""
    out: dict[str, list[str]] = {}
    base = CACHE / "pages"
    if not base.is_dir():
        return out
    pattern = re.compile(
        rf"^({re.escape(key)}-({NR}))-(\d{{14}})\.json$")
    for path in base.glob(f"{key}-*.json"):
        match = pattern.match(path.name)
        if match:
            out.setdefault(match.group(1), []).append(match.group(3))
    return {doc: sorted(set(timestamps)) for doc, timestamps in out.items()}


def legacy_document_cdx_index(key: str) -> dict[str, list[str]]:
    """Read the predecessor crawler's ``cdx/<document>.json`` caches."""
    out: dict[str, list[str]] = {}
    base = CACHE / "cdx"
    if not base.is_dir():
        return out
    pattern = re.compile(rf"^({re.escape(key)}-({NR}))\.json$")
    for path in base.glob(f"{key}-*.json"):
        match = pattern.match(path.name)
        payload = _read_json(path)
        if not match or not isinstance(payload, list):
            continue
        timestamps = {str(timestamp) for timestamp in payload
                      if re.fullmatch(r"\d{14}", str(timestamp))}
        if timestamps:
            out[match.group(1)] = sorted(timestamps)
    return out


def fetch_capture(doc: str, timestamp: str, *, offline: bool) -> Capture | None:
    path = CACHE / "pages" / f"{doc}-{timestamp}.json"
    cached = _read_json(path) if path.is_file() else None
    if isinstance(cached, dict) and str(cached.get("text") or "").strip():
        return Capture(doc, timestamp, cached.get("valid"), cached["text"])
    if offline:
        return None

    html = http_get(PAGE.format(ts=timestamp, doc=doc), delay=1.0)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("div#content") or soup
    chrome = re.sub(r"\s+", " ", content.get_text(" "))
    match = re.search(
        r"(?:Text gilt ab|in Kraft ab):\s*(\d{2})\.(\d{2})\.(\d{4})",
        chrome)
    valid = (f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
             if match else None)
    parts = [
        re.sub(r"\s+", " ", node.get_text(" ")).strip()
        for node in content.select("div.absatz.paratext")
    ]
    text = "\n".join(part for part in parts if part)
    if not text:
        return None
    _atomic_json(path, {"valid": valid, "text": text})
    return Capture(doc, timestamp, valid, text)


# -------------------------------------------------------------- descriptors

PREFIX_RE = re.compile(r"(?<!\w)(?P<art>Art\s*[.,])|(?P<section>§{1,2})")
OP_RE = re.compile(
    r"(?P<replace>\bneu\s+gef(?:asst|aßt)?\.?)"
    r"|(?P<add>\beingef(?:ügt)?\.?)"
    r"|(?P<modify>\bgeänd(?:ert)?\.?)"
    r"|(?P<repeal>\baufgeh(?:oben)?\.?)",
    flags=re.IGNORECASE,
)
OP_NAMES = {
    "add": OP_ADD,
    "modify": OP_MODIFY,
    "replace": OP_REPLACE,
    "repeal": OP_REPEAL,
}


def _canonical_norm(prefix: str, nr: str,
                    known: Sequence[NormId]) -> NormId:
    matches = [norm for norm in known if norm.nr == nr]
    return matches[0] if len(matches) == 1 else NormId(prefix, nr)


def _expand_range(prefix: str, start: str, end: str,
                  known: Sequence[NormId]) -> list[NormId]:
    same_prefix = [norm for norm in known if norm.prefix == prefix]
    starts = [i for i, norm in enumerate(same_prefix) if norm.nr == start]
    ends = [i for i, norm in enumerate(same_prefix) if norm.nr == end]
    if starts and ends and starts[0] <= ends[0]:
        return same_prefix[starts[0]:ends[0] + 1]

    if start.isdigit() and end.isdigit():
        a, b = int(start), int(end)
        if a <= b and b - a <= 500:
            return [_canonical_norm(prefix, str(n), known)
                    for n in range(a, b + 1)]

    left = re.fullmatch(r"(\d+)([a-z])", start)
    right = re.fullmatch(r"(\d+)([a-z])", end)
    if left and right and left.group(1) == right.group(1):
        a, b = ord(left.group(2)), ord(right.group(2))
        if a <= b:
            return [
                _canonical_norm(prefix, f"{left.group(1)}{chr(letter)}", known)
                for letter in range(a, b + 1)
            ]
    return [_canonical_norm(prefix, start, known),
            _canonical_norm(prefix, end, known)]


def _parse_number_sequence(text: str, prefix: str,
                           known: Sequence[NormId]) -> list[NormId]:
    """Parse one ``9, 52 und 121`` / ``78a bis 78l`` sequence.

    The parser is anchored at the start and stops at ``Abs./Satz/Nr.`` so
    paragraph/subparagraph numbers cannot leak into the norm list.
    """
    match = re.match(rf"^[\s,;.:]*({NR})", text)
    if not match:
        return []
    first = match.group(1)
    pos = match.end()
    range_match = re.match(rf"\s*(?:[-–]|bis)\s*({NR})", text[pos:])
    if range_match:
        end = range_match.group(1)
        norms = _expand_range(prefix, first, end, known)
        pos += range_match.end()
    else:
        norms = [_canonical_norm(prefix, first, known)]

    while True:
        item = re.match(rf"\s*(?:,|und)\s*({NR})(?=[\s,.]|$)", text[pos:])
        if not item:
            break
        norms.append(_canonical_norm(prefix, item.group(1), known))
        pos += item.end()
    return norms


def _refs_from_fragment(fragment: str, inherited_prefix: str | None,
                        known: Sequence[NormId]) -> tuple[list[NormId], str | None]:
    """Extract explicit prefix groups and an optional prefixless continuation."""
    matches = list(PREFIX_RE.finditer(fragment))
    refs: list[NormId] = []
    last_prefix = inherited_prefix

    if not matches:
        if inherited_prefix:
            refs.extend(_parse_number_sequence(fragment, inherited_prefix, known))
        return refs, last_prefix

    # ``..., 115, 116 aufgeh., Art. 120 ...`` may start with a continuation.
    if inherited_prefix:
        refs.extend(_parse_number_sequence(
            fragment[:matches[0].start()], inherited_prefix, known))

    for index, match in enumerate(matches):
        prefix = "Art." if match.group("art") else "§"
        end = matches[index + 1].start() if index + 1 < len(matches) \
            else len(fragment)
        refs.extend(_parse_number_sequence(fragment[match.end():end],
                                           prefix, known))
        last_prefix = prefix
    return refs, last_prefix


def parse_descriptor(description: str,
                     known: Sequence[NormId] = ()) -> ParsedDescriptor:
    """Parse an FFN target descriptor into norm-specific operations."""
    operations: dict[NormId, str] = {}
    op_matches = list(OP_RE.finditer(description))
    last_end = 0
    last_prefix: str | None = None

    for match in op_matches:
        refs, last_prefix = _refs_from_fragment(
            description[last_end:match.start()], last_prefix, known)
        operation = OP_NAMES[match.lastgroup or ""]
        for norm in refs:
            operations[norm] = operation
        last_end = match.end()

    # A terse FFN entry such as ``Art. 7`` is still a named target, but its
    # operation is unknown.  When any verb exists, unqualified trailing text is
    # not interpreted as another target.
    if not op_matches:
        refs, _ = _refs_from_fragment(description, None, known)
        for norm in refs:
            operations[norm] = OP_UNKNOWN

    # Only the FFN's explicit "mehrfach geänd." marker is a wildcard.  Blank,
    # table-of-contents and structural descriptions are not evidence that an
    # arbitrary norm changed.
    unspecified = "mehrfach" in description.casefold()
    ordered = tuple(sorted(operations.items(), key=lambda item:
                           (item[0].prefix, _natural_nr(item[0].nr))))
    return ParsedDescriptor(ordered, unspecified)


def _natural_nr(nr: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d+)([a-z]?)", nr)
    return (int(match.group(1)), match.group(2)) if match else (10**9, nr)


# --------------------------------------------------------------- state model

def _capture_date(timestamp: str) -> str:
    return f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"


def _state_date(state: State, *, first: bool) -> str:
    capture = state.first_capture if first else state.last_capture
    return state.valid or _capture_date(capture)


def build_states(captures: Iterable[Capture]) -> list[State]:
    """Order captures by legal-validity date and collapse adjacent duplicates."""
    usable = [capture for capture in captures if capture.text.strip()]
    usable.sort(key=lambda capture: (
        capture.valid or _capture_date(capture.timestamp), capture.timestamp))
    states: list[State] = []
    for capture in usable:
        if states and states[-1].text == capture.text:
            previous = states[-1]
            states[-1] = replace(
                previous,
                first_capture=min(previous.first_capture, capture.timestamp),
                last_capture=max(previous.last_capture, capture.timestamp),
                valid=previous.valid or capture.valid,
            )
            continue
        states.append(State(
            doc=capture.doc,
            text=capture.text,
            valid=capture.valid,
            first_capture=capture.timestamp,
            last_capture=capture.timestamp,
        ))
    return states


def build_transitions(states: Sequence[State]) -> list[Transition]:
    return [Transition(states[index - 1].doc,
                       states[index - 1], states[index])
            for index in range(1, len(states))]


def _transition_window(transition: Transition) -> tuple[str, str] | None:
    lower = _state_date(transition.old, first=False)
    upper = _state_date(transition.new, first=True)
    # Capture time is a fallback only for a state whose declared validity date
    # is absent (_state_date handles that).  Equal/contradictory legal dates are
    # not repaired with weaker archive timing: their event interval is unknown.
    return (lower, upper) if lower < upper else None


def _event_affects(event: ChangeEvent, norm: NormId,
                   allowed_ops: set[str]) -> bool:
    operation = event.operation_for(norm)
    return ((operation in allowed_ops) if operation is not None
            else event.unspecified)


def _events_in_window(events: Sequence[ChangeEvent], norm: NormId,
                      lower: str | None, upper: str,
                      allowed_ops: set[str]) -> list[ChangeEvent]:
    return [
        event for event in events
        if (lower is None or lower < event.date) and event.date <= upper
        and _event_affects(event, norm, allowed_ops)
    ]


def _candidate_row(norm: NormId, event: ChangeEvent, operation: str,
                   old: State | None, new: State | None,
                   transition_id: str, confidence: str) -> Candidate:
    return Candidate(transition_id, {
        "jurabk": event.jurabk,
        "date": event.date,
        "para": norm.label,
        "old": old.text if old else "",
        "new": new.text if new else "",
        "source": "wayback",
        "event_source": "ffn",
        "event_id": event.event_id,
        "event_seq": event.seq,
        "event_description": event.description,
        "operation": operation,
        "confidence": confidence,
        "effective_date": new.valid if new else None,
        "old_valid": old.valid if old else None,
        "new_valid": new.valid if new else None,
        "old_capture": old.last_capture if old else None,
        "new_capture": new.first_capture if new else None,
        "transition_id": transition_id,
    })


def assign_document_transitions(
    norm: NormId,
    states: Sequence[State],
    events: Sequence[ChangeEvent],
) -> tuple[list[Candidate], Counter]:
    """Conservatively assign this document's states to FFN events."""
    candidates: list[Candidate] = []
    stats: Counter = Counter()
    if not states:
        stats["no_state"] += 1
        return candidates, stats
    if len(states) == 1:
        stats["single_state"] += 1

    for transition in build_transitions(states):
        stats["transitions"] += 1
        window = _transition_window(transition)
        if window is None:
            stats["invalid_window"] += 1
            continue
        compatible = _events_in_window(
            events, norm, window[0], window[1],
            {OP_ADD, OP_MODIFY, OP_REPLACE, OP_REPEAL, OP_UNKNOWN})
        if len(compatible) != 1:
            stats["ambiguous" if compatible else "unmatched"] += 1
            continue
        event = compatible[0]
        operation = event.operation_for(norm) or OP_MODIFY
        if operation not in {OP_MODIFY, OP_REPLACE, OP_UNKNOWN}:
            stats["operation_mismatch"] += 1
            continue
        confidence = ("named" if event.operation_for(norm)
                      else "unique-unspecified")
        candidates.append(_candidate_row(
            norm, event, operation, transition.old, transition.new,
            transition.transition_id, confidence))
        stats["assigned"] += 1

    if states:
        first = states[0]
        first_date = _state_date(first, first=True)
        compatible = _events_in_window(
            events, norm, None, first_date,
            {OP_ADD, OP_MODIFY, OP_REPLACE, OP_REPEAL, OP_UNKNOWN})
        if len(compatible) == 1 and compatible[0].operation_for(norm) == OP_ADD:
            event = compatible[0]
            transition_id = hashlib.sha256(
                (f"{first.doc}|ADD|{event.event_id}|{first.valid}|"
                 f"{first.first_capture}|"
                 f"{hashlib.sha256(first.text.encode()).hexdigest()}")
                .encode()).hexdigest()[:24]
            candidates.append(_candidate_row(
                norm, event, OP_ADD, None, first, transition_id,
                "explicit-add"))
            stats["additions"] += 1
        elif any(event.operation_for(norm) == OP_ADD for event in compatible):
            stats["ambiguous_addition"] += 1

        repeal_events = [event for event in events
                         if event.operation_for(norm) == OP_REPEAL]
        for event in repeal_events:
            prior = [state for state in states
                     if _state_date(state, first=False) <= event.date]
            if not prior:
                stats["unmatched_repeal"] += 1
                continue
            old = prior[-1]
            lower = _state_date(old, first=False)
            compatible = _events_in_window(
                events, norm, lower, event.date,
                {OP_ADD, OP_MODIFY, OP_REPLACE, OP_REPEAL, OP_UNKNOWN})
            if len(compatible) != 1 or compatible[0] != event:
                stats["ambiguous_repeal"] += 1
                continue
            transition_id = hashlib.sha256(
                (f"{old.doc}|{hashlib.sha256(old.text.encode()).hexdigest()}|"
                 f"DELETE|{event.event_id}|{old.valid}|{old.last_capture}")
                .encode()).hexdigest()[:24]
            candidates.append(_candidate_row(
                norm, event, OP_REPEAL, old, None, transition_id,
                "explicit-repeal"))
            stats["repeals"] += 1

    return candidates, stats


def unique_candidates(candidates: Sequence[Candidate]) -> tuple[list[dict], int]:
    """Drop every key/transition collision instead of choosing silently."""
    by_key: dict[tuple[str, str, str], list[Candidate]] = defaultdict(list)
    by_transition: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        row = candidate.row
        by_key[(row["jurabk"], row["date"], row["para"])].append(candidate)
        by_transition[candidate.transition_id].append(candidate)

    bad = {
        id(candidate)
        for group in list(by_key.values()) + list(by_transition.values())
        if len(group) > 1
        for candidate in group
    }
    rows = [candidate.row for candidate in candidates if id(candidate) not in bad]
    rows.sort(key=lambda row: (row["date"], row["jurabk"],
                               _natural_nr(row["para"].split()[-1])))
    return rows, len(bad)


# -------------------------------------------------------------- orchestration

def _current_norms(snapshot: Path) -> dict[str, list[NormId]]:
    out: dict[str, list[NormId]] = defaultdict(list)
    pattern = re.compile(rf"^(Art\.|§)\s*({NR})$")
    for row in read_jsonl(snapshot / "norms.jsonl"):
        match = pattern.match(row.get("enbez") or "")
        if match:
            norm = NormId(match.group(1), match.group(2))
            if norm not in out[row["jurabk"]]:
                out[row["jurabk"]].append(norm)
    return out


def _load_events(snapshot: Path, known: dict[str, list[NormId]]) \
        -> dict[str, list[ChangeEvent]]:
    out: dict[str, list[ChangeEvent]] = defaultdict(list)
    for row in read_jsonl(snapshot / "versions.jsonl"):
        if row.get("source") != "ffn":
            continue
        date = row.get("date") or ""
        if date < MIN_EVENT:
            continue
        jurabk = row["jurabk"]
        description = row.get("description") or ""
        parsed = parse_descriptor(description, known.get(jurabk, ()))
        if not parsed.changes and not parsed.unspecified:
            continue
        out[jurabk].append(ChangeEvent(
            jurabk=jurabk,
            date=date,
            seq=int(row.get("seq") or 0),
            description=description,
            changes=parsed.changes,
            unspecified=parsed.unspecified,
        ))
    for events in out.values():
        events.sort(key=lambda event: (event.date, event.seq,
                                       event.description))
    return out


def _merge_capture_indexes(*indexes: dict[str, list[str]]) \
        -> dict[str, list[str]]:
    merged: dict[str, set[str]] = defaultdict(set)
    for index in indexes:
        for doc, timestamps in index.items():
            merged[doc].update(timestamps)
    return {doc: sorted(timestamps) for doc, timestamps in merged.items()}


def _select_acts(acts: dict[str, str],
                 requested: set[str] | None) -> dict[str, str]:
    if requested is None:
        return acts
    if not requested:
        raise ValueError("--acts must contain at least one jurabk or key")
    known_tokens = set(acts) | set(acts.values())
    unknown = sorted(requested - known_tokens)
    if unknown:
        raise ValueError(f"unknown --acts value(s): {', '.join(unknown)}")
    return {jurabk: key for jurabk, key in acts.items()
            if jurabk in requested or key in requested}


def _norm_for_doc(doc: str, key: str, known: Sequence[NormId],
                  events: Sequence[ChangeEvent]) -> NormId | None:
    match = re.fullmatch(rf"{re.escape(key)}-({NR})", doc)
    if not match:
        return None
    nr = match.group(1)
    current = [norm for norm in known if norm.nr == nr]
    if len(current) == 1:
        return current[0]
    historical = {
        norm for event in events for norm, _ in event.changes if norm.nr == nr
    }
    if len(historical) == 1:
        return next(iter(historical))
    prefix = Counter(norm.prefix for norm in known).most_common(1)
    return NormId(prefix[0][0] if prefix else "Art.", nr)


def generate_candidates(*, acts_filter: set[str] | None, offline: bool,
                        workers: int) -> tuple[list[dict], Counter]:
    snapshot = latest_snapshot("bayern_recht")
    if not snapshot:
        raise RuntimeError("run fetch_bayern_recht.py first")

    acts = {row["jurabk"]: row["key"]
            for row in read_jsonl(snapshot / "acts.jsonl")}
    acts = _select_acts(acts, acts_filter)
    known = _current_norms(snapshot)
    events_by_act = _load_events(snapshot, known)

    all_candidates: list[Candidate] = []
    totals: Counter = Counter()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for jurabk, key in sorted(acts.items()):
            events = events_by_act.get(jurabk, [])
            if not events:
                continue
            index = _merge_capture_indexes(
                cdx_act(key, offline=offline),
                legacy_document_cdx_index(key),
                cached_page_index(key),
            )

            # Named historical norms may be absent from the current corpus/CDX
            # cache.  Include their canonical document key so explicit repeal
            # or a later online CDX refresh can still resolve them.
            for event in events:
                for norm, _ in event.changes:
                    index.setdefault(f"{key}-{norm.nr}", [])

            print(f"== {jurabk} ({key}): {len(events)} FFN events, "
                  f"{len(index)} documents", file=sys.stderr)
            for doc, timestamps in sorted(index.items()):
                norm = _norm_for_doc(doc, key, known.get(jurabk, ()), events)
                if norm is None:
                    continue
                jobs = [(doc, timestamp) for timestamp in timestamps]
                captures = [capture for capture in pool.map(
                    lambda job: fetch_capture(*job, offline=offline), jobs)
                    if capture is not None]
                states = build_states(captures)
                totals["documents"] += 1
                totals["captures"] += len(captures)
                totals["states"] += len(states)
                candidates, stats = assign_document_transitions(
                    norm, states, events)
                all_candidates.extend(candidates)
                totals.update(stats)
    finally:
        pool.shutdown()

    rows, collisions = unique_candidates(all_candidates)
    totals["collisions_dropped"] += collisions
    totals["rows"] = len(rows)
    return rows, totals


def write_output(path: str, rows: Sequence[dict]) -> None:
    if path == "-":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a conservative Wayback diff candidate (never the "
                    "canonical ledger implicitly).")
    parser.add_argument("--acts", help="comma-separated jurabk/key filter")
    parser.add_argument(
        "--output", required=True,
        help="candidate JSONL path, or '-' for stdout; overwritten atomically")
    parser.add_argument(
        "--offline", action="store_true",
        help="use only merged CDX/page caches; make no network requests")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        choices=range(1, 9), metavar="1..8")
    args = parser.parse_args()
    acts_filter = ({item.strip() for item in args.acts.split(",") if item.strip()}
                   if args.acts is not None else None)
    try:
        rows, stats = generate_candidates(
            acts_filter=acts_filter, offline=args.offline,
            workers=args.workers)
        write_output(args.output, rows)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
    print(f"candidate: {len(rows)} rows -> {args.output} ({summary})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
