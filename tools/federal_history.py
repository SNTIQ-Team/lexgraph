"""Build public, evidence-bound federal history without republishing Buzer.

Buzer remains a private discovery/cross-check source.  Public history is
derived only from official inputs:

* adjacent GII snapshots yield exact *observed state pairs*; and
* a DIP PatchInstruction becomes a non-historical
  ``current_text_correspondence`` only when sufficiently distinctive proposed
  wording appears exactly once in the current official GII norm.

An observed state pair proves what GII served at two retrieval dates.  It does
not, by itself, prove the legal effective date, so ``effective_at`` remains
``null`` unless an official source states one explicitly.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:  # build_web_data adds pipeline/ to sys.path; tests import as a package
    from common import read_jsonl
except ModuleNotFoundError:  # pragma: no cover - depends on entrypoint
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
    from common import read_jsonl

SCHEMA_VERSION = 1
PUBLIC_VERIFICATION = frozenset({
    "exact", "current_text_correspondence", "metadata_only",
})
PRIVATE_VERIFICATION = "candidate_private"
MIN_PATCH_MATCH_CHARS = 48


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\xa0", " ").translate(str.maketrans({
        "„": '"', "“": '"', "”": '"', "’": "'", "–": "-", "—": "-",
    }))
    return re.sub(r"\s+", " ", value).strip()


def _match_text(value: Any) -> str:
    return _text(value).casefold()


def _sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _section_label(patch: dict) -> str | None:
    para = (patch.get("ref") or {}).get("para")
    return f"§ {para}" if para else None


def _dip_url(procedure: str | None) -> str | None:
    match = re.search(r"(\d+)$", str(procedure or ""))
    return f"https://dip.bundestag.de/vorgang/{match.group(1)}" \
        if match else None


def validate_public_event(event: dict) -> None:
    """Fail closed when a private candidate leaks into a public build."""
    tier = event.get("verification")
    if tier not in PUBLIC_VERIFICATION:
        raise ValueError(f"non-public federal-history tier: {tier!r}")
    evidence = event.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("public federal-history event has no evidence")
    official_hosts = {
        "GII": {"www.gesetze-im-internet.de"},
        "DIP": {"dip.bundestag.de", "search.dip.bundestag.de"},
        "BGBl": {"www.recht.bund.de", "recht.bund.de"},
    }
    for row in evidence:
        if not isinstance(row, dict) or row.get("source") not in official_hosts:
            raise ValueError(
                "federal-history evidence must be official GII/DIP/BGBl")
        url = str(row.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in official_hosts[
                str(row["source"])]:
            raise ValueError("federal-history evidence URL is not an official host")
    if tier == "exact":
        changes = event.get("changes") or []
        if not changes:
            raise ValueError("exact state pairs require changes")
        complete_pair = event.get("complete_parsed_state_pair") is True
        if complete_pair:
            for key in ("old_state_sha256", "new_state_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(event.get(key) or "")):
                    raise ValueError("complete state pair requires state hashes")
        for change in changes:
            old, new = change.get("old"), change.get("new")
            old_present = change.get("old_present") is True
            new_present = change.get("new_present") is True
            if (not isinstance(old, str) or not isinstance(new, str)
                    or old == new or (not old_present and not new_present)):
                raise ValueError("exact state pairs require distinct text states")
            if change.get("old_sha256") != _sha256(old) or \
                    change.get("new_sha256") != _sha256(new):
                raise ValueError("exact state-pair hash mismatch")
            if not complete_pair and (not old_present or not new_present):
                raise ValueError("exact state pairs require both captured norms")


def official_state_transition_events(
        transitions: Iterable[dict]) -> list[dict]:
    """Project complete, content-addressed GII state pairs to public events.

    Unlike the older adjacent-snapshot helper, a complete state object proves
    norm additions and removals as well as replacements.  It still proves only
    what the official source served on two retrieval days.  Publication and
    legal-effect dates remain empty until a final BGBl command is separately
    joined and verified.
    """
    events = []
    for transition in transitions:
        act = str(transition.get("jurabk") or transition.get("act") or "")
        observed_at = str(transition.get("observed_at") or "")
        previous_at = str(transition.get("previous_observed_at") or "")
        old_digest = str(transition.get("previous_state_sha256")
                         or transition.get("old_state_sha256") or "")
        new_digest = str(transition.get("state_sha256")
                         or transition.get("new_state_sha256") or "")
        changes = list(transition.get("changes") or [])
        if not (act and re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_at)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", previous_at)
                and changes):
            continue
        digest = _sha256(json.dumps(
            [act, previous_at, observed_at, old_digest, new_digest],
            ensure_ascii=False, separators=(",", ":")))[:16]
        slug = str(transition.get("slug") or "")
        source_url = str(transition.get("source_url") or (
            f"https://www.gesetze-im-internet.de/{slug}/" if slug else
            "https://www.gesetze-im-internet.de/"))
        reviewed = bool(transition.get("effective_at")
                        and transition.get("published_at")
                        and transition.get("legal_effect_verified"))
        event = {
            "id": f"fed-history:{digest}",
            "act": act,
            "title": ("Final BGBl command matched to complete GII state pair"
                      if reviewed else
                      "Complete official GII state change observed"),
            "procedure": None,
            "published_at": transition.get("published_at") if reviewed else None,
            "effective_at": transition.get("effective_at") if reviewed else None,
            "previous_observed_at": previous_at,
            "observed_at": observed_at,
            "date_basis": transition.get("date_basis") if reviewed else
            "retrieval_observation_not_effective_date",
            "verification": "exact",
            "verification_scope": (
                "final_bgbl_command_and_complete_parsed_official_state_pair"
                if reviewed else "complete_parsed_official_state_pair"),
            "complete_parsed_state_pair": True,
            "legal_effect_attribution": reviewed,
            "legal_effect_verified": reviewed,
            "old_builddate": transition.get("old_builddate"),
            "new_builddate": transition.get("new_builddate")
            or transition.get("builddate"),
            "old_state_sha256": old_digest,
            "new_state_sha256": new_digest,
            "changes": changes,
            "evidence": (list(transition.get("review_evidence") or [])
                         if reviewed else [
                {"source": "GII", "url": source_url,
                 "snapshot": previous_at,
                 "builddate": transition.get("old_builddate"),
                 "state_sha256": old_digest},
                {"source": "GII", "url": source_url,
                 "snapshot": observed_at,
                 "builddate": transition.get("new_builddate")
                 or transition.get("builddate"),
                 "state_sha256": new_digest},
            ]),
            "derivation": {
                "tool": "lexgraph-official-states",
                "schema_version": SCHEMA_VERSION,
                "algorithm": (
                    "gii-state-pair+bgbl-final-command+dip-commencement"
                    if reviewed else
                    "content-addressed-complete-state-diff"),
            },
        }
        if reviewed:
            event.update({
                "review_id": transition.get("review_id"),
                "legal_verification": transition.get("legal_verification"),
                "amending_articles": transition.get("amending_articles"),
                "procedure": (f"dip-vorgang:{transition['procedure_id']}"
                              if transition.get("procedure_id") else None),
                "bgbl": transition.get("bgbl"),
            })
        validate_public_event(event)
        events.append(event)
    return sorted(events, key=lambda event: (
        event["observed_at"], event["id"]), reverse=True)


def current_text_correspondence_events(
        patches: Iterable[dict], norms: Iterable[dict],
        observed_at: str) -> list[dict]:
    """Find distinctive DIP-draft wording in the current official GII text.

    A procedure merely reaching ``Verkündet`` is not enough: committee or
    plenary amendments may have changed its initial draft.  We publish only
    individual commands whose sufficiently distinctive new wording can still
    be found in the target norm.  This proves a current-text correspondence,
    not by itself that this particular document introduced the wording or that
    a bill-wide commencement date applies to this command.  It is therefore
    deliberately not called an exact historical state pair.
    """
    current: dict[tuple[str, str], dict] = {}
    for norm in norms:
        label = str(norm.get("enbez") or "").strip()
        if label:
            current[(str(norm.get("jurabk") or ""), label)] = norm

    grouped: dict[tuple[str, str, str], dict] = {}
    for patch in patches:
        if patch.get("status") != "published":
            continue
        label = _section_label(patch)
        new = _text(patch.get("new_text"))
        norm = current.get((str(patch.get("target_act") or ""), label or ""))
        matched_new = _match_text(new)
        matched_old = _match_text(patch.get("old_text_constraint"))
        current_text = _match_text(norm.get("text")) if norm else ""
        # Short references and single-word substitutions collide constantly
        # across a long norm.  If an explicit old constraint is still present,
        # the match is ambiguous as well (it can be an untouched occurrence).
        # Both cases fail closed instead of manufacturing a historical event.
        if not norm or len(matched_new) < MIN_PATCH_MATCH_CHARS or \
                current_text.count(matched_new) != 1 or \
                (matched_old and matched_old in current_text):
            continue
        procedure = str(patch.get("procedure") or "")
        act = str(patch.get("target_act") or "")
        status_at = str(patch.get("published_at") or "")
        url = _dip_url(procedure)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", status_at) or not url:
            continue
        key = (procedure, act, status_at)
        row = grouped.setdefault(key, {
            "id": "",
            "act": act,
            "title": str(patch.get("procedure_title") or ""),
            "procedure": procedure,
            "published_at": None,
            "procedure_status_at": status_at,
            "effective_at": None,
            "draft_bill_declared_effective_at": patch.get("valid_from"),
            "observed_at": observed_at,
            "date_basis": "procedure_status_date",
            "verification": "current_text_correspondence",
            "verification_scope": "current_text_correspondence_only",
            "historical_attribution": False,
            "changes": [],
            "evidence": [],
            "derivation": {
                "tool": "lexgraph-federal-history",
                "schema_version": SCHEMA_VERSION,
                "algorithm": "dip-draft-current-gii-correspondence",
            },
        })
        row["changes"].append({
            "norm": label,
            "operation": patch.get("operation"),
            "new": new,
            "new_sha256": _sha256(new),
            "current_norm_sha256": _sha256(norm.get("text")),
            "source_patch_id": patch.get("patch_id"),
            "verification": "current_official_text_contains_new_wording",
        })
        if url and not row["evidence"]:
            row["evidence"].append({
                "source": "DIP", "url": url,
                "document": patch.get("source_doc"),
            })
            row["evidence"].append({
                "source": "GII",
                "url": ("https://www.gesetze-im-internet.de/"
                        f"{norm.get('slug')}/"),
                "snapshot": observed_at,
            })

    events = []
    for (procedure, act, status_at), row in grouped.items():
        row["changes"].sort(key=lambda c: str(c.get("norm") or ""))
        digest = _sha256(json.dumps(
            [procedure, act, status_at,
             [change["source_patch_id"] for change in row["changes"]]],
            ensure_ascii=False, separators=(",", ":")))[:16]
        row["id"] = f"fed-history:{digest}"
        validate_public_event(row)
        events.append(row)
    return sorted(events, key=lambda e: (
        str(e.get("procedure_status_at") or ""), e["id"]),
        reverse=True)


def exact_gii_state_events(snapshot_dirs: Iterable[Path]) -> list[dict]:
    """Diff adjacent official GII retrieval states for shared corpus acts."""
    dirs = sorted(path for path in snapshot_dirs
                  if path.is_dir()
                  and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
                  and (path / "acts.jsonl").is_file()
                  and (path / "norms.jsonl").is_file())
    events: list[dict] = []
    for old_dir, new_dir in zip(dirs, dirs[1:]):
        old = {(str(row.get("jurabk") or ""),
                str(row.get("enbez") or "")): row
               for row in read_jsonl(old_dir / "norms.jsonl")}
        new = {(str(row.get("jurabk") or ""),
                str(row.get("enbez") or "")): row
               for row in read_jsonl(new_dir / "norms.jsonl")}
        old_acts = {str(row.get("jurabk") or ""): row
                    for row in read_jsonl(old_dir / "acts.jsonl")}
        new_acts = {str(row.get("jurabk") or ""): row
                    for row in read_jsonl(new_dir / "acts.jsonl")}
        old_counts: dict[str, int] = {}
        new_counts: dict[str, int] = {}
        for act, _label in old:
            old_counts[act] = old_counts.get(act, 0) + 1
        for act, _label in new:
            new_counts[act] = new_counts.get(act, 0) + 1
        shared_acts = {
            act for act in old_acts.keys() & new_acts.keys()
            if old_counts.get(act) == old_acts[act].get("norm_count")
            and new_counts.get(act) == new_acts[act].get("norm_count")
            and old_acts[act].get("builddate") != new_acts[act].get("builddate")
        }
        by_act: dict[str, list[dict]] = {}
        # Additions/removals need explicit completeness semantics.  The strict
        # tier therefore compares only norms captured on both sides.
        for key in sorted(set(old) & set(new)):
            act, label = key
            if act not in shared_acts:
                continue
            old_text = str(old.get(key, {}).get("text") or "")
            new_text = str(new.get(key, {}).get("text") or "")
            if old_text == new_text:
                continue
            by_act.setdefault(act, []).append({
                "norm": label or None,
                "old": old_text,
                "new": new_text,
                "old_present": True,
                "new_present": True,
                "old_sha256": _sha256(old_text),
                "new_sha256": _sha256(new_text),
                "old_doknr": old[key].get("doknr"),
                "new_doknr": new[key].get("doknr"),
            })
        for act, changes in by_act.items():
            old_act, new_act = old_acts[act], new_acts[act]
            digest = _sha256(json.dumps(
                [old_dir.name, new_dir.name, act,
                 [(c["norm"], c["old_sha256"], c["new_sha256"])
                  for c in changes]],
                ensure_ascii=False, separators=(",", ":")))[:16]
            event = {
                "id": f"fed-history:{digest}",
                "act": act,
                "title": "Official GII state change observed",
                "procedure": None,
                "published_at": None,
                "effective_at": None,
                "observed_at": new_dir.name,
                "date_basis": "retrieval_observation_not_effective_date",
                "verification": "exact",
                "old_builddate": old_act.get("builddate"),
                "new_builddate": new_act.get("builddate"),
                "changes": changes,
                "evidence": [
                    {"source": "GII",
                     "url": ("https://www.gesetze-im-internet.de/"
                             f"{old_act.get('slug')}/"),
                     "snapshot": old_dir.name,
                     "builddate": old_act.get("builddate")},
                    {"source": "GII",
                     "url": ("https://www.gesetze-im-internet.de/"
                             f"{new_act.get('slug')}/"),
                     "snapshot": new_dir.name,
                     "builddate": new_act.get("builddate")},
                ],
                "derivation": {
                    "tool": "lexgraph-federal-history",
                    "schema_version": SCHEMA_VERSION,
                    "algorithm": "official-state-diff",
                },
            }
            validate_public_event(event)
            events.append(event)
    return sorted(events, key=lambda e: (e["observed_at"], e["id"]),
                  reverse=True)


def build_public_federal_history(patches: Iterable[dict],
                                 norms: Iterable[dict],
                                 snapshot_dirs: Iterable[Path],
                                 observed_at: str,
                                 exact_events: Iterable[dict] | None = None
                                 ) -> dict:
    events = (list(exact_events) if exact_events is not None
              else exact_gii_state_events(snapshot_dirs))
    events.extend(current_text_correspondence_events(
        patches, norms, observed_at))
    for event in events:
        validate_public_event(event)
    events.sort(key=lambda e: str(e.get("effective_at") or
                                  e.get("procedure_status_at") or
                                  e.get("published_at") or
                                  e.get("observed_at") or ""), reverse=True)
    tiers: dict[str, int] = {}
    for event in events:
        tier = str(event["verification"])
        tiers[tier] = tiers.get(tier, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "built_at": observed_at,
        "source_policy": {
            "official_only": True,
            "buzer_role": "private_candidate_and_cross_check",
            "effective_dates_inferred": False,
        },
        "tiers": tiers,
        "total": len(events),
        "events": events,
    }
