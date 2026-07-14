"""Conservative dated Markdown views of one built Lexgraph act.

The web data plane contains one exact, current consolidated snapshot plus a
best-effort amendment history.  This module deliberately does not pretend
that the latter is a complete temporal database:

* HEAD is exact because it is rendered directly from ``norms``;
* a historical body is changed only when a complete, non-empty ``new`` side
  matches the state being reversed;
* additions/repeals with an empty side are reported, never used to invent or
  remove a whole norm;
* the federal synopse collector historically capped each side at 1,200
  characters, so a side of exactly that size is treated as truncated;
* ``effective_date`` is the state transition date when present.  The version
  row's ``date`` is only the fallback (and, for Bavaria, often publication).

The result is useful as a Wayback/git-style reader while keeping its evidence
boundary visible in both response metadata and the Markdown itself.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


class ArchiveRequestError(ValueError):
    """A caller supplied a date or norm that cannot be resolved."""


class UnknownNormError(ArchiveRequestError):
    """The requested norm designator is missing or ambiguous."""


class InvalidArchiveDateError(ArchiveRequestError):
    """The requested archive date is invalid or lies beyond HEAD."""


@dataclass(frozen=True)
class _NormRef:
    index: int
    label: str
    kind: str | None
    number: str | None


_SPACE = re.compile(r"\s+")
_SECTION = re.compile(r"^\s*§+\s*(\d+[a-z]*)\b", re.IGNORECASE)
_ARTICLE = re.compile(r"^\s*art(?:ikel)?\.?\s*(\d+[a-z]*)\b", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"^\s*(\d+[a-z]*)\s*$", re.IGNORECASE)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _parse_date(value: Any) -> str | None:
    """Return YYYY-MM-DD for the date shapes present in the web export."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    elif re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw):
        raw = f"{raw[6:]}-{raw[3:5]}-{raw[:2]}"
    else:
        raw = raw[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def head_date_for(act: dict[str, Any], fallback: Any = None) -> str | None:
    """Date of the exact consolidated snapshot, never an amendment guess."""
    # summary.built_at is the data-plane snapshot boundary.  An act's ``build``
    # is a source/fetch marker and must not override that deployment-wide HEAD.
    if fallback is not None:
        raw = str(fallback).strip()
        if "T" in raw:
            try:
                moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if moment.tzinfo is not None:
                    # Legal dates in this corpus are German calendar dates;
                    # a late UTC build must not make HEAD appear one day old.
                    moment = moment.astimezone(ZoneInfo("Europe/Berlin"))
                return moment.date().isoformat()
            except ValueError:
                pass
    return _parse_date(fallback) or _parse_date(act.get("build"))


def _norm_key(value: Any) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if match := _SECTION.match(raw):
        return "section", match.group(1).lower()
    if match := _ARTICLE.match(raw):
        return "article", match.group(1).lower()
    if match := _BARE_NUMBER.match(raw):
        return None, match.group(1).lower()
    return None, None


def _norm_refs(norms: list[dict[str, Any]]) -> list[_NormRef]:
    refs = []
    for index, norm in enumerate(norms):
        label = str(norm.get("enbez") or "").strip()
        kind, number = _norm_key(label)
        refs.append(_NormRef(index, label, kind, number))
    return refs


def _predominant_norm_kind(act: dict[str, Any]) -> str | None:
    counts = Counter(ref.kind for ref in _norm_refs(
        list(act.get("norms") or [])) if ref.kind is not None)
    if not counts:
        return None
    ordered = counts.most_common()
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return None
    return ordered[0][0]


def _historical_designators(act: dict[str, Any]) -> list[str]:
    labels: dict[tuple[str | None, str | None], str] = {}
    default_kind = _predominant_norm_kind(act)
    if default_kind is None:
        if str(act.get("jurabk") or "").casefold() == "gg":
            default_kind = "article"
        elif act.get("juris") == "DE-BY":
            default_kind = "article"
        elif act.get("juris") == "DE":
            default_kind = "section"
    for version in act.get("versions") or []:
        for change in version.get("changes") or []:
            label = str(change.get("para") or "").strip()
            kind, number = _norm_key(label)
            if number is not None:
                if kind is None and default_kind == "article":
                    kind, label = "article", f"Art. {number}"
                elif kind is None and default_kind == "section":
                    kind, label = "section", f"§ {number}"
                labels.setdefault((kind, number), label)
    return list(labels.values())


def _resolve_norm(norms: list[dict[str, Any]], query: str,
                  historical: Iterable[str] = ()) -> _NormRef:
    raw = query.strip()
    if not raw:
        raise UnknownNormError("norm must not be empty")
    refs = _norm_refs(norms)

    # Preserve unusual identifiers (Anlage, Präambel, …) via exact label.
    exact = [ref for ref in refs if ref.label.casefold() == raw.casefold()]
    if len(exact) == 1:
        return exact[0]

    kind, number = _norm_key(raw)
    if number is None:
        raise UnknownNormError(f"unknown norm '{query}'")
    matches = [ref for ref in refs
               if ref.number == number and (kind is None or ref.kind == kind)]
    if len(matches) > 1:
        labels = ", ".join(ref.label for ref in matches)
        raise UnknownNormError(
            f"ambiguous norm '{query}'; use one of: {labels}")
    if matches:
        return matches[0]

    historical_matches = []
    for label in historical:
        old_kind, old_number = _norm_key(label)
        if old_number == number and (kind is None or old_kind == kind):
            historical_matches.append(_NormRef(-1, label, old_kind, old_number))
    unique = {(ref.kind, ref.number): ref for ref in historical_matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        labels = ", ".join(ref.label for ref in unique.values())
        raise UnknownNormError(
            f"ambiguous historical norm '{query}'; use one of: {labels}")
    raise UnknownNormError(f"unknown norm '{query}'")


def _same_norm(change_key: tuple[str | None, str | None],
               ref: _NormRef) -> bool:
    kind, number = change_key
    return number is not None and number == ref.number and (
        kind is None or ref.kind is None or kind == ref.kind)


def _body_equal(left: str, right: str) -> bool:
    return _SPACE.sub(" ", left).strip() == _SPACE.sub(" ", right).strip()


def _gap(reason: str, label: str, *, from_: str | None = None,
         to: str | None = None) -> dict[str, str | None]:
    return {"reason": reason, "label": label, "from": from_, "to": to}


def _dedupe_gaps(gaps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for gap in gaps:
        marker = json.dumps(gap, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            out.append(gap)
    return out


def _version_rows(act: dict[str, Any], head_date: str) -> list[dict[str, Any]]:
    rows = []
    for raw in act.get("versions") or []:
        event_date = _parse_date(raw.get("date"))
        if event_date is None or event_date > head_date:
            continue
        row = dict(raw)
        row["_event_date"] = event_date
        rows.append(row)
    return rows


def _change_rows(act: dict[str, Any], head_date: str) -> list[dict[str, Any]]:
    """Flatten and byte-deduplicate transition operations."""
    out = []
    seen: set[tuple[str, str, str, str]] = set()
    for version in _version_rows(act, head_date):
        event_date = version["_event_date"]
        for raw in version.get("changes") or []:
            effective = (_parse_date(raw.get("effective_date"))
                         or event_date)
            if effective > head_date:
                continue
            para = str(raw.get("para") or "").strip()
            old = str(raw.get("old") or "")
            new = str(raw.get("new") or "")
            marker = (effective, para.casefold(), old, new)
            if marker in seen:
                continue
            seen.add(marker)
            out.append({
                "effective_date": effective,
                "event_date": event_date,
                "para": para,
                "old": old,
                "new": new,
                "source": str(raw.get("source") or ""),
                "confidence": str(raw.get("confidence") or ""),
                "old_valid": _parse_date(raw.get("old_valid")),
                "new_valid": _parse_date(raw.get("new_valid")),
                "version_label": str(version.get("text") or "").strip(),
            })
    return out


def _global_gaps(act: dict[str, Any], head_date: str) -> list[dict[str, Any]]:
    versions = _version_rows(act, head_date)
    changes = _change_rows(act, head_date)
    gaps: list[dict[str, Any]] = []
    metadata_only = sum(not (row.get("changes") or []) for row in versions)
    empty_sides = sum(not change["old"] or not change["new"]
                      for change in changes)
    truncated = sum(
        act.get("juris") == "DE"
        and (len(change["old"]) == 1200 or len(change["new"]) == 1200)
        for change in changes)
    capped_versions = sum(len(row.get("changes") or []) >= 80
                          for row in versions)
    if metadata_only:
        gaps.append(_gap(
            "metadata_only_versions",
            f"{metadata_only} amendment entries have metadata but no old/new text"))
    if empty_sides:
        gaps.append(_gap(
            "empty_change_side",
            f"{empty_sides} changes have an empty side; this does not prove a whole-norm lifecycle"))
    if truncated:
        gaps.append(_gap(
            "truncated_synopse",
            f"{truncated} federal old/new sides hit the historic 1,200-character cap"))
    if capped_versions:
        gaps.append(_gap(
            "change_list_cap",
            f"{capped_versions} amendment entries hit the 80-change export cap"))
    return gaps


def build_archive_index(act: dict[str, Any], *, fallback_head: Any = None
                        ) -> dict[str, Any]:
    """Describe selectable dates and known coverage limits for one act."""
    head_date = head_date_for(act, fallback_head)
    if head_date is None:
        raise ArchiveRequestError("the consolidated snapshot has no build date")
    versions = _version_rows(act, head_date)
    changes = _change_rows(act, head_date)
    dates: dict[str, dict[str, Any]] = {}

    for row in versions:
        day = row["_event_date"]
        entry = dates.setdefault(day, {
            "date": day, "label": None, "has_changes": False,
            "exact": False, "partial": True,
        })
        entry["has_changes"] = entry["has_changes"] or bool(row.get("changes"))
        if not entry["label"] and row.get("text"):
            entry["label"] = str(row["text"])
    for change in changes:
        day = change["effective_date"]
        entry = dates.setdefault(day, {
            "date": day, "label": None, "has_changes": True,
            "exact": False, "partial": True,
        })
        entry["has_changes"] = True
        if not entry["label"]:
            label = change.get("version_label")
            entry["label"] = (f"Wirksam ab {day}: {label}" if label
                              else f"Wirksam ab {day}")

    dates[head_date] = {
        "date": head_date,
        "label": "HEAD · consolidated source snapshot",
        "has_changes": False,
        "exact": True,
        "partial": False,
    }
    gaps = _global_gaps(act, head_date)
    norms = [{
        "id": str(norm.get("enbez") or ""),
        "enbez": str(norm.get("enbez") or ""),
        "label": str(norm.get("enbez") or ""),
        "title": str(norm.get("titel") or ""),
    } for norm in act.get("norms") or [] if norm.get("enbez")]
    present = {_norm_key(norm["enbez"]) for norm in norms}
    for label in _historical_designators(act):
        kind, number = _norm_key(label)
        if any(number == old_number and (
                kind is None or old_kind is None or kind == old_kind)
               for old_kind, old_number in present):
            continue
        norms.append({"id": label, "enbez": label, "label": label,
                      "title": "historical designator"})
    return {
        "act_id": act.get("id"),
        "jurabk": act.get("jurabk"),
        "title": act.get("title"),
        "head_date": head_date,
        "entries": sorted(dates.values(), key=lambda item: item["date"]),
        "norms": norms,
        "gaps": gaps,
        "complete": not gaps and len(dates) == 1,
    }


def _reconstruct(act: dict[str, Any], target: str, head_date: str,
                 norm_ref: _NormRef | None
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    norms = copy.deepcopy(list(act.get("norms") or []))
    refs = _norm_refs(norms)
    all_changes = _change_rows(act, head_date)
    changes = [row for row in all_changes
               if row["effective_date"] > target]
    versions = _version_rows(act, head_date)
    gaps: list[dict[str, Any]] = [_gap(
        "reconstructed_not_source_snapshot",
        "Only HEAD is a complete consolidated source snapshot; every earlier date is a conservative reconstruction",
        from_=target, to=head_date)]

    # A metadata-only event after the requested date is an unknown transition.
    covered_events = {row["event_date"] for row in changes}
    for row in versions:
        day = row["_event_date"]
        if day > target and not row.get("changes") and day not in covered_events:
            gaps.append(_gap(
                "missing_old_new",
                f"No old/new text for the amendment dated {day}",
                from_=target, to=day))

    if versions:
        first = min(row["_event_date"] for row in versions)
        if target < first:
            gaps.append(_gap(
                "before_archive_start",
                f"Tracked amendment history starts on {first}",
                from_=target, to=first))
    elif target < head_date:
        gaps.append(_gap(
            "no_historical_transitions",
            "No old/new transitions are available before HEAD",
            from_=target, to=head_date))

    grouped: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = \
        defaultdict(list)
    for change in changes:
        key = _norm_key(change["para"])
        if norm_ref is not None and not _same_norm(key, norm_ref):
            continue
        grouped[(change["effective_date"], *key)].append(change)

    applied = 0

    # Bavarian Wayback rows preserve complete norm bodies together with the
    # dates for which those bodies were observed.  Prefer that dated source
    # state over reverse-patching from HEAD: a later metadata-only amendment
    # can make HEAD differ from the recorded ``new`` side even though the
    # archived old/new pair is still the best evidence for its own interval.
    # Daily snapshots likewise provide a complete ``new`` body from their
    # effective day.  Federal buzer excerpts intentionally do not enter this
    # path because they are often fragments or capped at 1,200 characters.
    anchor_candidates: dict[tuple[str | None, str | None],
                            list[tuple[str, str, str]]] = defaultdict(list)
    for change in all_changes:
        if change.get("source") not in {"wayback", "daily_snapshot"}:
            continue
        key = _norm_key(change["para"])
        if key[1] is None:
            continue
        effective = change["effective_date"]
        old_valid = change.get("old_valid")
        new_valid = change.get("new_valid") or effective
        if (change["old"] and old_valid and old_valid <= target < effective):
            anchor_candidates[key].append(
                (old_valid, change["old"], change["para"]))
        if change["new"] and new_valid <= target:
            anchor_candidates[key].append(
                (new_valid, change["new"], change["para"]))

    anchored: list[_NormRef] = []
    requested_refs = ([norm_ref] if norm_ref is not None
                      else refs + [_NormRef(-1, label, *_norm_key(label))
                                   for label in _historical_designators(act)])
    seen_requested: set[tuple[str | None, str | None]] = set()
    for requested in requested_refs:
        requested_key = (requested.kind, requested.number)
        if requested.number is None or requested_key in seen_requested:
            continue
        seen_requested.add(requested_key)
        candidates = []
        for key, rows in anchor_candidates.items():
            if _same_norm(key, requested):
                candidates.extend(rows)
        if not candidates:
            continue
        _, body, source_label = max(candidates, key=lambda row: row[0])
        current = [ref for ref in refs if _same_norm(requested_key, ref)]
        if len(current) == 1:
            norms[current[0].index]["text"] = body
            anchored.append(current[0])
        elif len(current) == 0:
            label = requested.label or source_label
            norms.append({"enbez": label, "titel": "historical designator",
                          "text": body, "glied": ""})
            added = _NormRef(len(norms) - 1, label,
                             requested.kind, requested.number)
            refs.append(added)
            anchored.append(added)
        applied += 1

    # Reverse newer transitions before older ones.
    for (day, kind, number), operations in sorted(
            grouped.items(), key=lambda item: item[0][0], reverse=True):
        if any(_same_norm((kind, number), ref) for ref in anchored):
            continue
        unique = {(op["old"], op["new"]): op for op in operations}
        if len(unique) != 1:
            gaps.append(_gap(
                "ambiguous_transition",
                f"Conflicting transitions for {operations[0]['para']} on {day}",
                from_=target, to=day))
            continue
        operation = next(iter(unique.values()))
        old, new = operation["old"], operation["new"]
        label = operation["para"] or number or "unknown norm"
        if not old or not new:
            gaps.append(_gap(
                "empty_change_side",
                f"{label} on {day} has an empty old/new side; it was not treated as a complete norm insertion/repeal",
                from_=target, to=day))
            continue
        if (act.get("juris") == "DE"
                and (len(old) == 1200 or len(new) == 1200)):
            gaps.append(_gap(
                "truncated_synopse",
                f"{label} on {day} reaches the federal 1,200-character capture cap",
                from_=target, to=day))
            continue

        candidates = [ref for ref in refs if ref.number == number and (
            kind is None or ref.kind is None or ref.kind == kind)]
        if len(candidates) != 1:
            gaps.append(_gap(
                "unresolved_norm",
                f"Could not resolve {label} uniquely in the current snapshot",
                from_=target, to=day))
            continue
        ref = candidates[0]
        current = str(norms[ref.index].get("text") or "")
        if not _body_equal(current, new):
            gaps.append(_gap(
                "state_mismatch",
                f"The current chain for {ref.label or label} does not match the recorded new side on {day}",
                from_=target, to=day))
            continue
        norms[ref.index]["text"] = old
        applied += 1

    # Old/new records version the body, not the separate current heading field.
    # Once a body is reversed, retaining a current heading is disclosed rather
    # than silently presented as an exact historical whole.
    if applied:
        gaps.append(_gap(
            "current_heading_metadata",
            "Norm headings are from HEAD; the historical old/new source versions body text only",
            from_=target, to=head_date))
    return norms, _dedupe_gaps(gaps), applied


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _markdown(act: dict[str, Any], norms: list[dict[str, Any]], *,
              target: str, head_date: str, norm_ref: _NormRef | None,
              exact: bool, gaps: list[dict[str, Any]]) -> str:
    status = "exact" if exact else "partial"
    scope = norm_ref.label if norm_ref is not None else "entire act"
    lines = [
        "---",
        f"act_id: {_yaml_string(act.get('id'))}",
        f"jurabk: {_yaml_string(act.get('jurabk'))}",
        f"jurisdiction: {_yaml_string(act.get('juris'))}",
        f"requested_at: {target}",
        f"resolved_at: {target}",
        f"head_date: {head_date}",
        f"archive_status: {status}",
        f"scope: {_yaml_string(scope)}",
        f"coverage_gaps: {len(gaps)}",
        "---",
        "",
    ]
    title = str(act.get("title") or act.get("jurabk") or act.get("id") or "Act")
    if norm_ref is None:
        lines.append(f"# {title}")
    else:
        lines.append(f"# {title} — {norm_ref.label}")
    lines.extend([
        "",
        f"> Archive status: **{status}** · requested/resolved {target} · HEAD {head_date}.",
    ])
    if gaps:
        lines.append(
            "> This is a conservative reconstruction. Unproven transitions are not guessed:")
        for gap in gaps:
            lines.append(f"> - {gap['label']}")
    else:
        lines.append("> Text is taken directly from, or losslessly identical to, the tracked state.")
    lines.append("")

    if norm_ref is None:
        selected = norms
    elif norm_ref.index >= 0:
        selected = [norms[norm_ref.index]]
    else:
        historical = [item for item in norms
                      if _same_norm(_norm_key(item.get("enbez")), norm_ref)]
        selected = historical[:1] or [{
            "enbez": norm_ref.label,
            "titel": "historical designator", "text": "",
        }]
    for norm in selected:
        label = str(norm.get("enbez") or "").strip()
        norm_title = str(norm.get("titel") or "").strip()
        body = str(norm.get("text") or "").strip()
        if not label and not norm_title and not body:
            continue
        heading = label or norm_title or "Untitled norm"
        if label and norm_title:
            heading = f"{label} — {norm_title}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(body or "_Kein Normtext im konsolidierten Quellsnapshot._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown_snapshot(act: dict[str, Any], *, requested_at: Any = None,
                             norm: str | None = None,
                             fallback_head: Any = None) -> dict[str, Any]:
    """Resolve an arbitrary date and render the full act or one norm."""
    head_date = head_date_for(act, fallback_head)
    if head_date is None:
        raise ArchiveRequestError("the consolidated snapshot has no build date")
    target = head_date if requested_at is None else _parse_date(requested_at)
    if target is None:
        raise InvalidArchiveDateError("at must be an ISO date (YYYY-MM-DD)")
    if target > head_date:
        raise InvalidArchiveDateError(
            f"requested date {target} is newer than HEAD {head_date}")

    current_norms = list(act.get("norms") or [])
    norm_ref = (_resolve_norm(current_norms, norm,
                              _historical_designators(act))
                if norm is not None else None)
    if target == head_date:
        norms = copy.deepcopy(current_norms)
        if norm_ref is not None and norm_ref.index < 0:
            gaps = [_gap(
                "norm_absent_at_head",
                f"{norm_ref.label} is a historical designator and is absent from HEAD",
                from_=head_date, to=head_date)]
            exact = False
        else:
            gaps = []
            exact = True
    else:
        norms, gaps, _ = _reconstruct(act, target, head_date, norm_ref)
        exact = not gaps
    markdown = _markdown(
        act, norms, target=target, head_date=head_date, norm_ref=norm_ref,
        exact=exact, gaps=gaps)
    return {
        "act_id": act.get("id"),
        "requested_at": target,
        "resolved_at": target,
        "head_date": head_date,
        "norm": norm_ref.label if norm_ref is not None else None,
        "exact": exact,
        "partial": not exact,
        "markdown": markdown,
        "gaps": gaps,
    }


def markdown_filename(result: dict[str, Any]) -> str:
    scope = result.get("norm") or "complete"
    raw = f"{result.get('act_id')}-{scope}-{result.get('resolved_at')}.md"
    safe = _SAFE_FILENAME.sub("-", raw).strip("-")
    safe = re.sub(r"-{2,}", "-", safe)
    return safe or "lexgraph-act.md"
