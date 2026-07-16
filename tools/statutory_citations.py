"""Deterministic current-text statutory citation/backlink index.

The input is the already parsed official GII/BAYERN.RECHT act detail corpus.
Only explicit ``§``/``§§`` and ``Art.``/``Artikel`` designators are extracted.
An unqualified designator is a same-act reference.  A cross-act edge is made
only when the immediately attached act name/abbreviation has an exact entry in
the alias registry.  Unknown or ambiguous aliases and missing target norms are
retained as unresolved evidence; this module never guesses with fuzzy text.

The result describes the current consolidated state observed at build time.
It is a citation graph, not a claim about legal effect or interpretation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_ALIAS_WINDOW = 180
MAX_EXCERPT = 360
MAX_RANGE_EXPANSION = 200

DESIGNATOR_RE = re.compile(
    r"^(?P<marker>§|Art(?:ikel)?\.?)\s*(?P<number>\d+[a-zA-Z]*)$",
    re.IGNORECASE,
)
CITATION_START_RE = re.compile(
    r"(?<![\w§])(?P<marker>§§|§|Art(?:ikel)?\.?)\s*(?=\d)",
    re.IGNORECASE,
)
BASE_RE = re.compile(r"\d+[a-zA-Z]*")
PINPOINT_LABEL_RE = re.compile(
    r"\s+(?:Abs(?:atz|ätze)?\.?|Satz|Sätze|Nr\.?|Nummer(?:n)?|"
    r"Buchst(?:abe)?\.?|Halbsatz|Alternative)\s*",
    re.IGNORECASE,
)
PINPOINT_VALUE_RE = re.compile(
    r"(?:\d+[a-zA-Z]*|[a-zA-Z])"
    r"(?:\s*(?:,|bis|und|oder)\s*(?:\d+[a-zA-Z]*|[a-zA-Z]))*",
    re.IGNORECASE,
)
NORM_SEPARATOR_RE = re.compile(
    r"\s*(?P<separator>,|bis(?:\s+einschließlich)?|und|oder)\s*",
    re.IGNORECASE,
)
ALIAS_PREFIX_RE = re.compile(
    r"^(?:(?:des|der|dem|den|die|das|nach|gemäß|in|im|von|vom|"
    r"dieses|dieser|diesem|dieser|nach\s+Maßgabe|im\s+Sinn(?:e)?)\s+|"
    r"[\s,()\-–—])*$",
    re.IGNORECASE,
)
UNKNOWN_ALIAS_PREFIX_RE = re.compile(
    r"^(?:(?:des|der|dem|den|die|das|nach|gemäß|in|im|von|vom|"
    r"nach\s+Maßgabe|im\s+Sinn(?:e)?)\s+|[\s,()\-–—])*",
    re.IGNORECASE,
)
UNKNOWN_LONG_ALIAS_RE = re.compile(
    r"(?P<alias>(?:[A-ZÄÖÜ][\wÄÖÜäöüß/.-]*\s+){0,7}"
    r"(?:[A-ZÄÖÜ][\wÄÖÜäöüß/.-]*?"
    r"(?:gesetz(?:es)?|gesetzbuch(?:es)?|verordnung(?:en)?|ordnung(?:en)?)|"
    r"Gesetz(?:es)?|Gesetzbuch(?:es)?|Verordnung(?:en)?|Ordnung(?:en)?))"
)
UNKNOWN_SHORT_ALIAS_RE = re.compile(
    r"(?P<alias>(?=[A-Za-zÄÖÜäöüß0-9/.-]{2,40}(?:\s|$))"
    r"(?:[A-ZÄÖÜ]{2,12}|"
    r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9/.-]*[A-ZÄÖÜ]"
    r"[A-Za-zÄÖÜäöüß0-9/.-]*)(?:\s+[IVXLCDM]+)?)"
)


# This is a closed alias registry, not a fuzzy title table.  Targets are the
# exact JurAbk values used by the current corpus.  Entries remain useful when a
# cited act is outside the curated corpus: the mention is then explicitly
# exported as unresolved instead of silently becoming a same-act link.
EXPLICIT_ALIAS_TARGETS: dict[str, str] = {
    "AufenthG": "AufenthG 2004",
    "Aufenthaltsgesetz": "AufenthG 2004",
    "Aufenthaltsgesetzes": "AufenthG 2004",
    "AsylG": "AsylVfG 1992",
    "Asylgesetz": "AsylVfG 1992",
    "Asylgesetzes": "AsylVfG 1992",
    "BeschV": "BeschV 2013",
    "Beschäftigungsverordnung": "BeschV 2013",
    "FreizügG/EU": "FreizügG/EU 2004",
    "Freizügigkeitsgesetz/EU": "FreizügG/EU 2004",
    "Staatsangehörigkeitsgesetz": "StAG",
    "Staatsangehörigkeitsgesetzes": "StAG",
    "Asylbewerberleistungsgesetz": "AsylbLG",
    "Asylbewerberleistungsgesetzes": "AsylbLG",
    "Grundgesetz": "GG",
    "Grundgesetzes": "GG",
    "Bürgerliches Gesetzbuch": "BGB",
    "Bürgerlichen Gesetzbuchs": "BGB",
    "Bayerisches Verwaltungsverfahrensgesetz": "BayVwVfG",
    "Bayerischen Verwaltungsverfahrensgesetzes": "BayVwVfG",
    "Bayerisches Polizeiaufgabengesetz": "PAG",
    "Bayerischen Polizeiaufgabengesetzes": "PAG",
    "Bayerisches Integrationsgesetz": "BayIntG",
    "Bayerischen Integrationsgesetzes": "BayIntG",
    "Bayerisches Erziehungs- und Unterrichtsgesetz": "BayEUG",
    "Bayerischen Erziehungs- und Unterrichtsgesetzes": "BayEUG",
    "Gemeindeordnung": "GO",
    "Gemeindeordnung für den Freistaat Bayern": "GO",
    "GO": "GO",
    "Landkreisordnung": "LKrO",
    "LKrO": "LKrO",
    "Bezirksordnung": "BezO",
    "BezO": "BezO",
    "Finanzausgleichsgesetz": "FAG",
    "Finanzausgleichsgesetzes": "FAG",
    "FAG": "FAG",
}

_SGB_ALIASES = (
    ("I", "Erstes", "Ersten", "SGB 1"),
    ("II", "Zweites", "Zweiten", "SGB 2"),
    ("III", "Drittes", "Dritten", "SGB 3"),
    ("IV", "Viertes", "Vierten", "SGB 4"),
    ("V", "Fünftes", "Fünften", "SGB 5"),
    ("VI", "Sechstes", "Sechsten", "SGB 6"),
    ("VII", "Siebtes", "Siebten", "SGB 7"),
    ("VIII", "Achtes", "Achten", "SGB 8"),
    ("IX", "Neuntes", "Neunten", "SGB 9 2018"),
    ("X", "Zehntes", "Zehnten", "SGB 10"),
    ("XI", "Elftes", "Elften", "SGB 11"),
    ("XII", "Zwölftes", "Zwölften", "SGB 12"),
    ("XIII", "Dreizehntes", "Dreizehnten", "SGB 13"),
    ("XIV", "Vierzehntes", "Vierzehnten", "SGB 14"),
)
for _roman, _nominative, _genitive, _target in _SGB_ALIASES:
    EXPLICIT_ALIAS_TARGETS.update({
        f"SGB {_roman}": _target,
        f"{_nominative} Buch Sozialgesetzbuch": _target,
        f"{_genitive} Buches Sozialgesetzbuch": _target,
        f"Sozialgesetzbuch {_nominative} Buch": _target,
    })


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _key(value: object) -> str:
    return _clean(value).casefold()


def canonical_norm(value: object) -> str | None:
    """Canonicalize punctuation/spacing, without approximate matching."""
    cleaned = _clean(value)
    match = DESIGNATOR_RE.fullmatch(cleaned)
    if not match:
        return None
    marker = "§" if match.group("marker").startswith("§") else "Art."
    return f"{marker} {match.group('number').casefold()}"


def citation_filter_key(value: object) -> str:
    """Exact API/SQLite key with typography-only norm normalization."""
    cleaned = _clean(value).casefold()
    cleaned = re.sub(r"^(?:artikel|art)\.?\s*", "art. ", cleaned)
    cleaned = re.sub(r"^§\s*", "§ ", cleaned)
    return cleaned


@dataclass(frozen=True)
class TargetSpec:
    start: str
    end: str | None = None
    pinpoint: str | None = None


@dataclass(frozen=True)
class CitationGroup:
    start: int
    end: int
    marker: str
    raw: str
    specs: tuple[TargetSpec, ...]


@dataclass(frozen=True)
class AliasMatch:
    text: str
    target_jurabks: tuple[str, ...]
    end: int
    known: bool


def _parse_pinpoints(text: str, position: int) -> tuple[int, str | None]:
    start = position
    while True:
        label = PINPOINT_LABEL_RE.match(text, position)
        if not label:
            break
        value = PINPOINT_VALUE_RE.match(text, label.end())
        if not value:
            break
        position = value.end()
    if position == start:
        return position, None
    return position, _clean(text[start:position])


def parse_citation_groups(text: str) -> list[CitationGroup]:
    """Parse explicit norm groups; standalone Absatz/Satz is out of scope."""
    groups: list[CitationGroup] = []
    search_from = 0
    while True:
        match = CITATION_START_RE.search(text, search_from)
        if not match:
            break
        marker = "§" if match.group("marker").startswith("§") else "Art."
        first = BASE_RE.match(text, match.end())
        if not first:
            search_from = match.end()
            continue
        first_norm = f"{marker} {first.group().casefold()}"
        position, pinpoint_tail = _parse_pinpoints(text, first.end())
        if pinpoint_tail:
            specs = [TargetSpec(
                first_norm,
                pinpoint=f"{first_norm} {pinpoint_tail}",
            )]
        else:
            specs = [TargetSpec(first_norm)]
            while True:
                separator = NORM_SEPARATOR_RE.match(text, position)
                if not separator:
                    break
                following_at = separator.end()
                repeated_marker = CITATION_START_RE.match(text, following_at)
                if repeated_marker:
                    repeated_kind = "§" if repeated_marker.group(
                        "marker").startswith("§") else "Art."
                    if repeated_kind != marker:
                        break
                    following_at = repeated_marker.end()
                following = BASE_RE.match(text, following_at)
                if not following:
                    break
                following_norm = f"{marker} {following.group().casefold()}"
                word = separator.group("separator").casefold()
                if word.startswith("bis"):
                    previous = specs[-1]
                    if previous.end is not None:
                        break
                    specs[-1] = TargetSpec(previous.start, following_norm)
                else:
                    specs.append(TargetSpec(following_norm))
                position = following.end()
        groups.append(CitationGroup(
            start=match.start(), end=position, marker=marker,
            raw=_clean(text[match.start():position]), specs=tuple(specs),
        ))
        # When a repeated marker was consumed (``§ 1 und § 2 SGB II``), do
        # not emit a duplicate group for its second head.  Otherwise the next
        # search still finds an independent later citation and its own alias.
        search_from = max(position, match.end())
    return groups


def _alias_registry(
        acts: list[dict],
        explicit_aliases: Mapping[str, str] | None = None,
        ) -> tuple[dict[str, set[str]], dict[str, set[str]], list[tuple[str, re.Pattern]]]:
    targets: dict[str, set[str]] = {}
    forms: dict[str, set[str]] = {}

    def add(alias: object, target: object) -> None:
        alias_text, target_text = _clean(alias), _clean(target)
        if not alias_text or not target_text:
            return
        alias_key = _key(alias_text)
        targets.setdefault(alias_key, set()).add(target_text)
        forms.setdefault(alias_key, set()).add(alias_text)

    for act in acts:
        jurabk = act["jurabk"]
        add(jurabk, jurabk)
        add(act.get("title"), jurabk)
    for alias, target in EXPLICIT_ALIAS_TARGETS.items():
        add(alias, target)
    for alias, target in (explicit_aliases or {}).items():
        add(alias, target)

    patterns: list[tuple[str, re.Pattern]] = []
    for alias_key, aliases in forms.items():
        for alias in aliases:
            pattern = re.compile(
                rf"(?<![\w/]){re.escape(alias)}(?![\w/])",
                re.IGNORECASE,
            )
            patterns.append((alias_key, pattern))
    patterns.sort(key=lambda item: len(item[1].pattern), reverse=True)
    return targets, forms, patterns


def _alias_segment(text: str, group: CitationGroup) -> tuple[str, int]:
    end = min(len(text), group.end + MAX_ALIAS_WINDOW)
    next_marker = CITATION_START_RE.search(text, group.end)
    if next_marker:
        end = min(end, next_marker.start())
    for separator in (";", ".", ":", "\n"):
        found = text.find(separator, group.end, end)
        if found >= 0:
            end = min(end, found)
    return text[group.end:end], group.end


def _match_alias(text: str, group: CitationGroup,
                 targets: dict[str, set[str]],
                 patterns: list[tuple[str, re.Pattern]]) -> AliasMatch | None:
    segment, absolute_start = _alias_segment(text, group)
    candidates: list[tuple[int, int, str, re.Match]] = []
    for alias_key, pattern in patterns:
        found = pattern.search(segment)
        if not found:
            continue
        prefix = segment[:found.start()]
        parenthesized = (found.start() > 0 and segment[found.start() - 1] == "("
                         and found.end() < len(segment)
                         and segment[found.end()] == ")")
        if parenthesized or ALIAS_PREFIX_RE.fullmatch(prefix):
            candidates.append((found.start(), -len(found.group()),
                               alias_key, found))
    if candidates:
        _, _, alias_key, found = min(candidates)
        return AliasMatch(
            text=_clean(found.group()),
            target_jurabks=tuple(sorted(targets[alias_key], key=str.casefold)),
            end=absolute_start + found.end(),
            known=True,
        )

    # A leading law-shaped token is evidence of a cross-act citation even when
    # it is not in the closed alias table.  Export it unresolved; do not fall
    # back to a plausible-looking same-act norm.
    prefix = UNKNOWN_ALIAS_PREFIX_RE.match(segment)
    position = prefix.end() if prefix else 0
    remainder = segment[position:]
    if re.match(r"(?:dies(?:es|er|em)|vorliegend(?:es|er|em))\s+"
                r"(?:Gesetz(?:es)?|Verordnung)", remainder, re.IGNORECASE):
        return None
    unknown = UNKNOWN_LONG_ALIAS_RE.match(remainder) \
        or UNKNOWN_SHORT_ALIAS_RE.match(remainder)
    if not unknown:
        return None
    alias = _clean(unknown.group("alias"))
    if alias.casefold() in {"gesetz", "gesetzes", "verordnung"}:
        return None
    return AliasMatch(
        text=alias,
        target_jurabks=(alias,),
        end=absolute_start + position + unknown.end(),
        known=False,
    )


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 100)
    right = min(len(text), max(end, start + 1) + 180)
    value = _clean(text[left:right])
    if len(value) > MAX_EXCERPT:
        value = value[:MAX_EXCERPT - 1].rstrip() + "…"
    if left:
        value = "…" + value
    if right < len(text) and not value.endswith("…"):
        value += "…"
    return value


def _citation_id(key: tuple) -> str:
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return "cite:" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _act_inventory(acts: Iterable[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    inventory: list[dict] = []
    by_jurabk: dict[str, list[dict]] = {}
    for source in acts:
        act_id = _clean(source.get("id"))
        jurabk = _clean(source.get("jurabk"))
        if not act_id or not jurabk:
            continue
        norm_order: list[str] = []
        norm_set: set[str] = set()
        norms: list[dict] = []
        for raw_norm in source.get("norms") or []:
            source_designator = _clean(raw_norm.get("enbez"))
            source_norm = canonical_norm(source_designator) or source_designator
            if not source_norm:
                continue
            text = str(raw_norm.get("text") or "")
            norms.append({"norm": source_norm, "text": text})
            target_designator = canonical_norm(source_designator)
            if target_designator and target_designator not in norm_set:
                norm_set.add(target_designator)
                norm_order.append(target_designator)
        act = {
            "id": act_id,
            "jurabk": jurabk,
            "juris": _clean(source.get("juris")),
            "title": _clean(source.get("title")),
            "norms": norms,
            "norm_order": norm_order,
            "norm_set": norm_set,
            "norm_position": {norm: index
                              for index, norm in enumerate(norm_order)},
            "norm_markers": {norm.split(" ", 1)[0] for norm in norm_order},
        }
        inventory.append(act)
        by_jurabk.setdefault(_key(jurabk), []).append(act)
    inventory.sort(key=lambda act: act["id"])
    return inventory, by_jurabk


def _range_targets(spec: TargetSpec, target_act: dict
                   ) -> tuple[list[tuple[str, str | None]], str | None]:
    if spec.end is None:
        if spec.start not in target_act["norm_set"]:
            return [], "target_norm_not_in_current_corpus"
        return [(spec.start, spec.pinpoint)], None
    positions = target_act["norm_position"]
    if spec.start not in positions or spec.end not in positions:
        return [], "range_endpoint_not_in_current_corpus"
    first, last = positions[spec.start], positions[spec.end]
    if first > last:
        return [], "range_order_invalid"
    selected = target_act["norm_order"][first:last + 1]
    if len(selected) > MAX_RANGE_EXPANSION:
        return [], "range_too_broad"
    return [(norm, None) for norm in selected], None


def _unresolved_designator(spec: TargetSpec) -> str:
    if spec.end:
        return f"{spec.start} bis {spec.end}"
    return spec.pinpoint or spec.start


def build_citation_index(acts: Iterable[dict], *, built_at: str,
                         explicit_aliases: Mapping[str, str] | None = None,
                         source_snapshots: Mapping[str, str] | None = None,
                         ) -> dict:
    """Build a stable outbound/inbound index from current official text."""
    inventory, by_jurabk = _act_inventory(acts)
    alias_targets, _, alias_patterns = _alias_registry(
        inventory, explicit_aliases)
    deduped: dict[tuple, dict] = {}
    source_norms_scanned = 0

    for source_act in inventory:
        for source_norm_row in source_act["norms"]:
            source_norms_scanned += 1
            body = source_norm_row["text"]
            for group in parse_citation_groups(body):
                alias = _match_alias(
                    body, group, alias_targets, alias_patterns)
                alias_end = alias.end if alias else group.end
                citation_text = _clean(body[group.start:alias_end])
                excerpt = _excerpt(body, group.start, alias_end)

                if alias is None:
                    target_candidates = [source_act]
                    target_jurabk = source_act["jurabk"]
                    alias_reason = None if group.marker in source_act[
                        "norm_markers"] else "unqualified_foreign_marker"
                elif len(alias.target_jurabks) != 1:
                    target_candidates = []
                    target_jurabk = " | ".join(alias.target_jurabks)
                    alias_reason = "ambiguous_explicit_alias"
                else:
                    target_jurabk = alias.target_jurabks[0]
                    target_candidates = by_jurabk.get(_key(target_jurabk), [])
                    if len(target_candidates) > 1:
                        alias_reason = "ambiguous_target_act"
                        target_candidates = []
                    elif not target_candidates:
                        alias_reason = "target_act_not_in_current_corpus"
                    else:
                        alias_reason = None

                resolved_target = target_candidates[0] \
                    if len(target_candidates) == 1 else None
                kind = "self" if (resolved_target is not None
                                  and resolved_target["id"] == source_act["id"]
                                  ) else "cross_act"
                if alias is None:
                    kind = "self"

                for spec in group.specs:
                    targets: list[tuple[str, str | None]] = []
                    reason = alias_reason
                    if resolved_target is not None:
                        targets, norm_reason = _range_targets(
                            spec, resolved_target)
                        reason = reason or norm_reason
                    if not targets:
                        targets = [(_unresolved_designator(spec),
                                    spec.pinpoint)]

                    for target_norm, target_pinpoint in targets:
                        status = "resolved" if reason is None else "unresolved"
                        target_act_id = resolved_target["id"] \
                            if resolved_target is not None else None
                        target_act_jurabk = resolved_target["jurabk"] \
                            if resolved_target is not None else target_jurabk
                        key = (
                            source_act["id"], source_norm_row["norm"],
                            target_act_id, target_act_jurabk, target_norm,
                            target_pinpoint, kind, status, reason,
                        )
                        existing = deduped.get(key)
                        if existing:
                            existing["occurrence_count"] += 1
                            if alias and alias.text not in existing[
                                    "matched_aliases"]:
                                existing["matched_aliases"].append(alias.text)
                                existing["matched_aliases"].sort(
                                    key=str.casefold)
                            continue
                        deduped[key] = {
                            "id": _citation_id(key),
                            "status": status,
                            "unresolved_reason": reason,
                            "kind": kind,
                            "source_act": source_act["id"],
                            "source_jurabk": source_act["jurabk"],
                            "source_norm": source_norm_row["norm"],
                            "source_excerpt": excerpt,
                            "source_snapshot": (source_snapshots or {}).get(
                                source_act["juris"]),
                            "date_basis": (
                                "current_consolidated_snapshot_observation_"
                                "not_legal_effect"),
                            "citation_text": citation_text,
                            "matched_aliases": [alias.text] if alias else [],
                            "target_act": target_act_id,
                            "target_jurabk": target_act_jurabk,
                            "target_norm": target_norm,
                            "target_pinpoint": target_pinpoint,
                            "occurrence_count": 1,
                            "machine_extracted": True,
                            "current_state_only": True,
                            "legal_interpretation": "not_asserted",
                        }

    rows = sorted(deduped.values(), key=lambda row: (
        row["source_act"], row["source_norm"], row["target_jurabk"] or "",
        row["target_norm"] or "", row["target_pinpoint"] or "", row["id"],
    ))
    counts = {
        "total": len(rows),
        "resolved": sum(row["status"] == "resolved" for row in rows),
        "unresolved": sum(row["status"] == "unresolved" for row in rows),
        "self": sum(row["kind"] == "self" for row in rows),
        "cross_act": sum(row["kind"] == "cross_act" for row in rows),
        "acts_scanned": len(inventory),
        "source_norms_scanned": source_norms_scanned,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at,
        "machine_extracted": True,
        "current_state_only": True,
        "legal_interpretation": "not_asserted",
        "source_policy": {
            "official_current_text_only": True,
            "inputs": ["GII", "BAYERN.RECHT"],
            "cross_act_resolution": "exact_explicit_alias_only",
            "fuzzy_matching": False,
            "range_expansion": "existing_target_norm_order_only",
            "standalone_absatz_or_satz_references": "not_extracted",
            "unresolved_mentions_retained": True,
        },
        "source_snapshots": dict(sorted((source_snapshots or {}).items())),
        "counts": counts,
        "citations": rows,
    }


def citation_manifest(index: Mapping[str, object]) -> dict:
    """Return the small JSON envelope; citation rows live only in SQLite."""
    manifest = {
        key: value for key, value in index.items() if key != "citations"
    }
    counts = manifest.get("counts") or {}
    manifest["storage"] = {
        "format": "sqlite3",
        "file": "citations.sqlite",
        "table": "citation",
        "rows": int(counts.get("total") or 0),
        "ordering": "ordinal",
        "read_only": True,
    }
    return manifest


def write_citation_database(output: Path, index: Mapping[str, object]) -> None:
    """Write a deterministic, atomically replaced exact citation index."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.unlink(missing_ok=True)
    rows = index.get("citations") or []
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            PRAGMA application_id = 0x4c584354;
            PRAGMA user_version = 1;

            CREATE TABLE citation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE citation (
                ordinal INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL
                    CHECK (status IN ('resolved', 'unresolved')),
                unresolved_reason TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('self', 'cross_act')),
                source_act TEXT NOT NULL,
                source_act_key TEXT NOT NULL,
                source_jurabk TEXT NOT NULL,
                source_jurabk_key TEXT NOT NULL,
                source_norm TEXT NOT NULL,
                source_norm_key TEXT NOT NULL,
                source_excerpt TEXT NOT NULL,
                source_snapshot TEXT,
                date_basis TEXT NOT NULL,
                citation_text TEXT NOT NULL,
                matched_aliases_json TEXT NOT NULL,
                target_act TEXT,
                target_act_key TEXT NOT NULL,
                target_jurabk TEXT NOT NULL,
                target_jurabk_key TEXT NOT NULL,
                target_norm TEXT NOT NULL,
                target_norm_key TEXT NOT NULL,
                target_pinpoint TEXT,
                target_pinpoint_key TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                machine_extracted INTEGER NOT NULL
                    CHECK (machine_extracted IN (0, 1)),
                current_state_only INTEGER NOT NULL
                    CHECK (current_state_only IN (0, 1)),
                legal_interpretation TEXT NOT NULL
            );

            CREATE INDEX citation_source_act
                ON citation(source_act_key, source_norm_key, kind, ordinal);
            CREATE INDEX citation_source_jurabk
                ON citation(source_jurabk_key, source_norm_key, kind, ordinal);
            CREATE INDEX citation_target_act
                ON citation(target_act_key, target_norm_key, kind, ordinal);
            CREATE INDEX citation_target_jurabk
                ON citation(target_jurabk_key, target_norm_key, kind, ordinal);
            CREATE INDEX citation_target_pinpoint
                ON citation(target_pinpoint_key, kind, ordinal);
            CREATE INDEX citation_status
                ON citation(status, unresolved_reason, ordinal);
        """)
        metadata = {
            "schema_version": str(index.get("schema_version") or ""),
            "built_at": str(index.get("built_at") or ""),
            "counts": json.dumps(
                index.get("counts") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
            "source_policy": json.dumps(
                index.get("source_policy") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
            "source_snapshots": json.dumps(
                index.get("source_snapshots") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
        }
        conn.executemany(
            "INSERT INTO citation_meta(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        insert = """INSERT INTO citation(
            ordinal, id, status, unresolved_reason, kind,
            source_act, source_act_key, source_jurabk, source_jurabk_key,
            source_norm, source_norm_key, source_excerpt, source_snapshot,
            date_basis, citation_text, matched_aliases_json,
            target_act, target_act_key, target_jurabk, target_jurabk_key,
            target_norm, target_norm_key, target_pinpoint,
            target_pinpoint_key, occurrence_count, machine_extracted,
            current_state_only, legal_interpretation
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        def values():
            """Yield SQLite rows without duplicating the citation graph.

            A broad full-text corpus can contain tens of thousands of citation
            dictionaries.  Building a second equally large tuple list here
            needlessly raises the peak RSS of the refresh/watch workers.
            ``sqlite3.executemany`` consumes this iterator incrementally.
            """
            for ordinal, row in enumerate(rows):
                yield (
                ordinal, row["id"], row["status"],
                row.get("unresolved_reason"), row["kind"],
                row["source_act"], _key(row["source_act"]),
                row["source_jurabk"], _key(row["source_jurabk"]),
                row["source_norm"], citation_filter_key(row["source_norm"]),
                row["source_excerpt"], row.get("source_snapshot"),
                row["date_basis"], row["citation_text"],
                json.dumps(row.get("matched_aliases") or [],
                           ensure_ascii=False, separators=(",", ":")),
                row.get("target_act"), _key(row.get("target_act")),
                row["target_jurabk"], _key(row["target_jurabk"]),
                row["target_norm"], citation_filter_key(row["target_norm"]),
                row.get("target_pinpoint"),
                citation_filter_key(row.get("target_pinpoint")),
                int(row["occurrence_count"]),
                int(bool(row["machine_extracted"])),
                int(bool(row["current_state_only"])),
                row["legal_interpretation"],
                )
        conn.executemany(insert, values())
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"citation sqlite quick_check failed: {check}")
    except Exception:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    os.replace(tmp, output)


__all__ = [
    "EXPLICIT_ALIAS_TARGETS",
    "build_citation_index",
    "canonical_norm",
    "citation_filter_key",
    "citation_manifest",
    "parse_citation_groups",
    "write_citation_database",
]
