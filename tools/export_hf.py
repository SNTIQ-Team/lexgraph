#!/usr/bin/env python3
"""Build the public Lexgraph Hugging Face dataset from the latest snapshots.

The exporter deliberately copies immutable source snapshots instead of the
API's compact presentation JSON wherever possible.  It also exports the two
derived graph views and a merged court-decision table so a dataset consumer
gets the same case set as the public API.

    python3 tools/export_hf.py [--out hf_export]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

from common import latest_snapshot, read_jsonl  # noqa: E402
from official_states import (  # noqa: E402
    DATE_BASIS as OFFICIAL_OBSERVATION_DATE_BASIS,
    load_manifest as load_official_state_manifest,
    load_state_verified as load_official_state,
    transitions as build_official_state_transitions,
)


# Output name -> (snapshot family, source filename, description)
SNAPSHOT_FILES: dict[str, tuple[str, str, str]] = {
    "patches.jsonl": (
        "patches", "patches.jsonl",
        "legislative PatchInstructions with lifecycle and provenance"),
    "federal_acts.jsonl": (
        "gii", "acts.jsonl", "federal acts in the curated deep corpus"),
    "federal_catalog.jsonl": (
        "gii", "catalog.jsonl",
        "complete official GII table of contents, metadata only"),
    "federal_norms.jsonl": (
        "gii", "norms.jsonl", "current federal sections and full text"),
    "bayern_acts.jsonl": (
        "bayern_recht", "acts.jsonl", "Bavarian acts in the deep corpus"),
    "bayern_norms.jsonl": (
        "bayern_recht", "norms.jsonl", "current Bavarian articles and full text"),
    "bayern_recht_versions.jsonl": (
        "bayern_recht", "versions.jsonl", "official Bavarian amendment metadata"),
    "eu_instruments.jsonl": (
        "eu_layer", "instruments.jsonl", "curated EU instruments with deep links"),
    "eu_transpositions.jsonl": (
        "eu_layer", "transpositions.jsonl", "German implementing measures"),
    "eu_index.jsonl": (
        "eu_index", "instruments.jsonl",
        "all in-force directives and basic regulations, metadata only"),
    "bayern_landtag_bills.jsonl": (
        "bay_landtag", "bills.jsonl", "Bavarian Landtag bills and lifecycle"),
    "bundestag_procedures.jsonl": (
        "dip", "vorgaenge.jsonl",
        "official DIP legislative procedures and current stages"),
    "bgbl_events.jsonl": (
        "bgbl_events", "events.jsonl", "federal promulgation events"),
    "gvbl_events.jsonl": (
        "gvbl_events", "events.jsonl", "Bavarian promulgation events"),
}

DERIVED_FILES: dict[str, tuple[Path, str]] = {
    "bayern_word_diffs.jsonl": (
        ROOT / "data" / "by_diffs.jsonl",
        "verified archived-state and forward daily Bavarian text changes"),
    "git.json": (
        ROOT / "web" / "data" / "git.json",
        "Laws-as-Git event log with commits, open/closed branches and evidence-bound merges"),
    "chronology.json": (
        ROOT / "web" / "data" / "git.json",
        "compatibility alias of git.json for existing dataset consumers"),
    "graph.json": (
        ROOT / "web" / "data" / "graph.json",
        "QFS arena graph export"),
    "watched_procedures.json": (
        ROOT / "web" / "data" / "watched_procedures.json",
        "persistent DIP/EUR-Lex watch state with evidence checks, chronology and qualitative forecasts"),
    "amendment_fates.json": (
        ROOT / "web" / "data" / "amendment_fates.json",
        "reviewed parliamentary document chains and current-law checks"),
    "verified_federal_events.json": (
        ROOT / "web" / "data" / "verified_federal_events.json",
        "official GII state pairs plus explicitly non-historical DIP/current-text correspondences"),
    "data_policy.json": (
        ROOT / "web" / "data" / "data_policy.json",
        "machine-readable exclusions for third-party database rights"),
}

# Raw lifecycle files do not exist before the first watch update.  Export them
# when present, while the required watched_procedures.json above always carries
# the complete API-facing state and embedded history.
OPTIONAL_DERIVED_FILES: dict[str, tuple[Path, str]] = {
    "procedure_watch_state.json": (
        ROOT / "data" / "procedure_watch_state.json",
        "raw persistent state used to stop terminal watch polling"),
    "procedure_watch_history.jsonl": (
        ROOT / "data" / "procedure_watch_history.jsonl",
        "append-only official procedure status-change ledger"),
}

OFFICIAL_STATE_STORE = ROOT / "web" / "data" / "federal_states"
OFFICIAL_REVIEWS = ROOT / "web" / "data" / \
    "official_transition_reviews.json"

OFFICIAL_HISTORY_FILES: dict[str, str] = {
    "official_federal_state_observations.jsonl": (
        "complete official GII retrieval observations; observed_at is not "
        "an effective date"),
    "official_federal_state_transitions.jsonl": (
        "own diffs between adjacent complete GII states, with no inferred "
        "legal date"),
    "official_federal_state_objects.jsonl": (
        "full content-addressed federal act states, including every captured "
        "norm and observation provenance"),
    "official_transition_reviews.jsonl": (
        "state transitions accepted as legal events only after final BGBl "
        "command and commencement review"),
}


def require_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"missing or empty {label}: {path}")
    return path


def copy_file(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


def line_count(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def case_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("court_short") or "").casefold(),
        re.sub(r"\s+", "", str(row.get("az") or "")).casefold(),
        str(row.get("date") or ""),
    )


def merged_decisions() -> list[dict]:
    """Mirror the API merge: reviewed manual records win duplicates."""
    manual_file = ROOT / "data" / "decisions.json"
    manual = json.loads(require_file(
        manual_file, "reviewed decisions").read_text(encoding="utf-8")
    ).get("decisions") or []

    rii_dir = latest_snapshot("rii")
    automated = list(read_jsonl(require_file(
        rii_dir / "decisions.jsonl" if rii_dir else Path(""),
        "official RII decisions")))

    rows = list(manual)
    ids = {str(row["id"]) for row in rows if row.get("id")}
    cases = {case_key(row) for row in rows}
    for row in automated:
        if (row.get("id") and str(row["id"]) in ids) or case_key(row) in cases:
            continue
        rows.append(row)
        if row.get("id"):
            ids.add(str(row["id"]))
        cases.add(case_key(row))
    return sorted(rows, key=lambda row: row.get("date") or "", reverse=True)


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(path)
    return count


def _read_official_reviews(path: Path = OFFICIAL_REVIEWS) -> list[dict]:
    """Read the accepted web-data reviews, never a private candidate cache."""
    payload = json.loads(require_file(
        path, "official transition reviews").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("official transition reviews must be an object")
    policy = payload.get("source_policy") or {}
    if not policy.get("official_only") or policy.get(
            "includes_quarantined_sources") or policy.get(
            "effective_dates_inferred") is not False:
        raise RuntimeError(
            "official transition reviews have a non-public source policy")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or payload.get("total") != len(reviews):
        raise RuntimeError("official transition review count mismatch")
    return reviews


def _official_history_rows(
        store: Path = OFFICIAL_STATE_STORE,
        reviews_path: Path = OFFICIAL_REVIEWS) -> dict[str, Iterable[dict]]:
    """Verify and materialize the published GII state store for HF.

    The web generation is the consistency boundary: the exporter reads its
    manifest and CAS, verifies every canonical object, and serializes portable
    JSONL rows instead of leaking local cache paths or gzip implementation
    details into the dataset.
    """
    store = Path(store)
    manifest = load_official_state_manifest(store)
    observations = list(manifest.get("observations") or [])
    objects = manifest.get("objects") or {}
    if not observations or not objects:
        raise RuntimeError("published official federal state store is empty")

    by_digest: dict[str, list[dict]] = {}
    for observation in observations:
        if observation.get("date_basis") != \
                OFFICIAL_OBSERVATION_DATE_BASIS:
            raise RuntimeError("official observation has an invalid date basis")
        by_digest.setdefault(observation["state_sha256"], []).append(
            observation)

    def state_rows() -> Iterable[dict]:
        # Stream full states one at a time.  This keeps HF export memory flat
        # even as the cumulative CAS grows on the small production host.
        for digest in sorted(objects):
            state = load_official_state(store, digest)
            state_observations = sorted(
                by_digest.get(digest, []),
                key=lambda row: (row["observed_at"], row["act_id"]),
            )
            if not state_observations:
                raise RuntimeError(
                    f"official state {digest} has no retrieval observation")
            yield {
                "state_sha256": digest,
                "state_identity": "sha256-canonical-uncompressed-json",
                **state,
                "observations": state_observations,
                "provenance": {
                    "source": "GII",
                    "source_urls": sorted({
                        row["source_url"] for row in state_observations
                    }),
                    "date_basis": OFFICIAL_OBSERVATION_DATE_BASIS,
                    "verification": "exact_complete_state",
                    "effective_date_asserted": False,
                },
            }

    transition_rows = build_official_state_transitions(
        manifest, store)
    for row in transition_rows:
        row["evidence"] = [
            {
                "source": "GII",
                "url": row["source_url"],
                "observed_at": row["previous_observed_at"],
                "state_sha256": row["previous_state_sha256"],
            },
            {
                "source": "GII",
                "url": row["source_url"],
                "observed_at": row["observed_at"],
                "state_sha256": row["state_sha256"],
            },
        ]
        row["provenance"] = {
            "source": "GII",
            "algorithm": "lexgraph-complete-state-diff",
            "date_basis": OFFICIAL_OBSERVATION_DATE_BASIS,
            "effective_date_asserted": False,
            "official_only": True,
        }

    reviews = _read_official_reviews(Path(reviews_path))
    transitions_by_pair = {
        (row["previous_state_sha256"], row["state_sha256"]): row
        for row in transition_rows
    }
    official_hosts = {
        "GII": "www.gesetze-im-internet.de",
        "BGBl": "www.recht.bund.de",
        "DIP": "dip.bundestag.de",
    }
    for review in reviews:
        pair = (review.get("previous_state_sha256"),
                review.get("state_sha256"))
        transition = transitions_by_pair.get(pair)
        if transition is None:
            raise RuntimeError(
                f"official review {review.get('id')} has no state transition")
        if review.get("date_basis") != \
                "official_bgbl_command_and_commencement_clause" or \
                review.get("verification") != \
                "official_final_text_and_complete_state_pair":
            raise RuntimeError(
                f"official review {review.get('id')} has a weak date claim")
        if any(review.get(field) != transition.get(field) for field in (
                "act_id", "jurabk", "observed_at",
                "previous_observed_at")) or not review.get("changes"):
            raise RuntimeError(
                f"official review {review.get('id')} mismatches its state pair")
        bgbl = review.get("bgbl") or {}
        if bgbl.get("integrity_verified") is not True or not re.fullmatch(
                r"[0-9a-f]{64}", str(bgbl.get("pdf_sha256") or "")):
            raise RuntimeError(
                f"official review {review.get('id')} lacks BGBl integrity")
        try:
            published = date.fromisoformat(str(review.get("published_at")))
            effective = date.fromisoformat(str(review.get("effective_at")))
        except ValueError as exc:
            raise RuntimeError(
                f"official review {review.get('id')} has an invalid date") \
                from exc
        if published > effective:
            raise RuntimeError(
                f"official review {review.get('id')} has an effective date "
                "before publication")
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RuntimeError(
                f"official review {review.get('id')} lacks evidence")
        for item in evidence:
            source = item.get("source") if isinstance(item, dict) else None
            parsed = urlparse(str(item.get("url") or "")) \
                if isinstance(item, dict) else None
            if source not in official_hosts or parsed.scheme != "https" or \
                    parsed.hostname != official_hosts[source]:
                raise RuntimeError(
                    f"official review {review.get('id')} has private evidence")

    return {
        "official_federal_state_observations.jsonl": observations,
        "official_federal_state_transitions.jsonl": transition_rows,
        "official_federal_state_objects.jsonl": state_rows(),
        "official_transition_reviews.jsonl": reviews,
    }


CARD = """---
license: other
language:
  - de
tags:
  - legal
  - germany
  - legislation
  - eu-law
  - court-decisions
  - temporal-data
pretty_name: Lexgraph — Laws as Git for German and EU law
configs:
{configs}
---

# Lexgraph dataset

The data plane of **[Lexgraph](https://github.com/SNTIQ-Team/lexgraph)** —
German legislation modelled as **Laws as Git**: a temporal, multi-authority
event log with HEAD, commits, open/closed branches and evidence-bound merges
(Bund / Bayern / EU; Länder records only after verification at the originating
Landtag). Built **{today}**.

Git is a navigation metaphor, not a substitute for legal status. Every row's
official source and status controls whether it is current law, a pending branch
or a documented implementation/applicability link.

Lexgraph is deliberately two-layered. The German migration, asylum, social-law
and related practice corpus has current full text and change history where the
official/retrieval sources support it. `federal_catalog.jsonl` provides every
act listed in the official GII table of contents as discovery metadata only;
`eu_index.jsonl` provides EU breadth: all
in-force directives (including implementing/delegated directives) and basic
regulations exposed by CELLAR, as metadata only. It is not presented as deep
coverage of every EU instrument.

The independent federal archive is split by claim strength. An
`official_federal_state_observations.jsonl` row says only what complete GII
state Lexgraph retrieved on `observed_at`; that date is **not** silently
treated as commencement. `official_federal_state_transitions.jsonl` contains
Lexgraph's own old/new diff between two such states and likewise leaves
`effective_at` empty. A row enters `official_transition_reviews.jsonl` only
after the complete state pair is matched to the final, integrity-checked BGBl
amending command and the exact DIP commencement clause. Full portable states,
including every captured norm, are in
`official_federal_state_objects.jsonl` and are identified by the SHA-256 of
canonical uncompressed JSON.

Buzer is not an input to these four files and no private Buzer snapshot or
synopsis is shipped. It may be used outside the export as a non-authoritative
human QA/deep-link cross-check; all published state bytes, diffs and legal-date
evidence above are reproduced independently from official sources.

| File | Rows | Content |
|---|---:|---|
{rows_table}

`decisions.jsonl` combines reviewed cases with a forward-cumulative,
corpus-filtered import from the seven official federal
Rechtsprechung-im-Internet feeds. Automated norm links are citations, not
claims that the court interpreted every cited provision. The table is not a
catalogue of all German case law.

For Bavaria, official version rows are amendment metadata. Word-level history
is exported only when archived official pages yield an unambiguous state
transition, plus complete forward diffs between daily snapshots. Missing
history is kept missing instead of reconstructed. In
`bayern_word_diffs.jsonl`, `date` is the promulgation/event date while
`effective_date` is the date on which the consolidated wording changes; data
consumers doing time travel must prefer `effective_date`.

The public API can serialize the current complete act or one provision as
Markdown. It can also check out an exact archived GII observation when the
requested date exists in the state store. Historical output remains explicitly
labelled partial whenever the available source only supplies amendment
excerpts rather than a lossless consolidated snapshot; the dataset does not
imply exact historical text where the source archive cannot prove it.

`watched_procedures.json` preserves the current DIP/EUR-Lex observations,
official document chronology and embedded status-change history. Its
`analysis` object keeps verified facts, deterministic inferences and a
qualitative forecast separate; likelihood is not presented as a statistically
calibrated probability. Terminal procedures remain archived but leave the
active polling set. `amendment_fates.json` separates reviewed roles in a
parliamentary document chain from the mechanical checks performed against the
current consolidated corpus.

DIP-derived rows use the attribution **Deutscher Bundestag/Bundesrat – DIP**.
Lexgraph extraction, ranking, annotations and forecasts are transformations,
not source statements. DIP makes its source data available free of charge at
[dip.bundestag.de](https://dip.bundestag.de).

Every source and file-level rights regime is documented in the repository's
`docs/SOURCES.md` and `docs/RIGHTS.md`; reproduce
the current outputs with `refresh.sh` and `tools/export_hf.py`. Statutory texts
are official works under § 5 UrhG. The dataset is intentionally marked
`license: other`: SNTIQ's licence covers its original annotations and software,
not third-party or official source material. See `RIGHTS.md` before reuse.

Built by **[SNTIQ](https://sntiq.com/)**.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "hf_export"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    policy_path = require_file(
        ROOT / "web" / "data" / "data_policy.json", "public data policy")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("includes_quarantined_sources") or not policy.get(
            "public_build"):
        raise RuntimeError(
            "refusing HF export: rebuild web data without quarantined sources")

    # Avoid stale files surviving when the exported schema changes.
    for path in out.iterdir():
        if path.is_file():
            path.unlink()

    descriptions: dict[str, str] = {}
    counts: dict[str, int] = {}
    for name, (source, filename, description) in SNAPSHOT_FILES.items():
        snap = latest_snapshot(source)
        src = require_file(
            snap / filename if snap else Path(""), f"{source}/{filename}")
        dst = out / name
        copy_file(src, dst)
        counts[name] = line_count(dst)
        descriptions[name] = description

    for name, (src, description) in DERIVED_FILES.items():
        dst = out / name
        copy_file(require_file(src, name), dst)
        counts[name] = line_count(dst) if dst.suffix == ".jsonl" else 1
        descriptions[name] = description

    official_rows = _official_history_rows()
    for name, description in OFFICIAL_HISTORY_FILES.items():
        counts[name] = write_jsonl(out / name, official_rows[name])
        descriptions[name] = description

    optional_exported: list[str] = []
    for name, (src, description) in OPTIONAL_DERIVED_FILES.items():
        if not src.is_file() or src.stat().st_size == 0:
            continue
        dst = out / name
        copy_file(src, dst)
        counts[name] = line_count(dst) if dst.suffix == ".jsonl" else 1
        descriptions[name] = description
        optional_exported.append(name)

    counts["decisions.jsonl"] = write_jsonl(
        out / "decisions.jsonl", merged_decisions())
    descriptions["decisions.jsonl"] = (
        "reviewed and official federal decisions affecting the deep corpus")

    ordered = (list(SNAPSHOT_FILES) + list(OFFICIAL_HISTORY_FILES)
               + list(DERIVED_FILES)
               + optional_exported + ["decisions.jsonl"])
    rows_table = "\n".join(
        f"| `{name}` | {counts[name]:,} | {descriptions[name]} |"
        for name in ordered)
    configs = "\n".join(
        f"  - config_name: {Path(name).stem}\n    data_files: {name}"
        for name in ordered if name.endswith(".jsonl"))
    (out / "README.md").write_text(CARD.format(
        today=date.today().isoformat(), configs=configs,
        rows_table=rows_table), encoding="utf-8")
    copy_file(require_file(ROOT / "docs" / "RIGHTS.md", "rights matrix"),
              out / "RIGHTS.md")

    for name in ordered:
        print(f"  {name}: {counts[name]:,}")
    print(f"dataset card -> {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
