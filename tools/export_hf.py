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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from common import latest_snapshot, read_jsonl  # noqa: E402


# Output name -> (snapshot family, source filename, description)
SNAPSHOT_FILES: dict[str, tuple[str, str, str]] = {
    "patches.jsonl": (
        "patches", "patches.jsonl",
        "legislative PatchInstructions with lifecycle and provenance"),
    "federal_acts.jsonl": (
        "gii", "acts.jsonl", "federal acts in the curated deep corpus"),
    "federal_norms.jsonl": (
        "gii", "norms.jsonl", "current federal sections and full text"),
    "bayern_acts.jsonl": (
        "bayern_recht", "acts.jsonl", "Bavarian acts in the deep corpus"),
    "bayern_norms.jsonl": (
        "bayern_recht", "norms.jsonl", "current Bavarian articles and full text"),
    "bayern_recht_versions.jsonl": (
        "bayern_recht", "versions.jsonl", "official Bavarian amendment metadata"),
    "buzer_versions.jsonl": (
        "buzer", "versions.jsonl", "federal amendment versions since 2006"),
    "buzer_synopse.jsonl": (
        "buzer_synopse", "synopse.jsonl", "retrieved old/new federal section text"),
    "eu_instruments.jsonl": (
        "eu_layer", "instruments.jsonl", "curated EU instruments with deep links"),
    "eu_transpositions.jsonl": (
        "eu_layer", "transpositions.jsonl", "German implementing measures"),
    "eu_index.jsonl": (
        "eu_index", "instruments.jsonl",
        "all in-force directives and basic regulations, metadata only"),
    "bayern_landtag_bills.jsonl": (
        "bay_landtag", "bills.jsonl", "Bavarian Landtag bills and lifecycle"),
    "laender_bills.jsonl": (
        "laender_bills", "bills.jsonl", "bills from all 16 Landtage"),
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
        "event-sourced legislative commit graph"),
    "graph.json": (
        ROOT / "web" / "data" / "graph.json",
        "QFS arena graph export"),
    "watched_procedures.json": (
        ROOT / "web" / "data" / "watched_procedures.json",
        "persistent DIP/EUR-Lex watch state with embedded change history"),
    "amendment_fates.json": (
        ROOT / "web" / "data" / "amendment_fates.json",
        "reviewed parliamentary document chains and current-law checks"),
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


CARD = """---
license: cc-by-nc-sa-4.0
language:
  - de
tags:
  - legal
  - germany
  - legislation
  - eu-law
  - court-decisions
  - event-sourced
pretty_name: Lexgraph — German and EU law as event-sourced data
configs:
{configs}
---

# Lexgraph dataset

The data plane of **[Lexgraph](https://github.com/SNTIQ-Team/lexgraph)** —
German legislation modelled as a temporal, multi-authority patch history
(Bund / Bayern / EU / 16 Länder). Built **{today}**.

Lexgraph is deliberately two-layered. The German migration, asylum, social-law
and related practice corpus has current full text and change history where the
official/retrieval sources support it. `eu_index.jsonl` provides breadth: all
in-force directives (including implementing/delegated directives) and basic
regulations exposed by CELLAR, as metadata only. It is not presented as deep
coverage of every EU instrument.

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
Markdown. Historical output is explicitly labelled partial whenever the
available source only supplies amendment excerpts rather than a lossless
consolidated snapshot; the dataset does not imply exact historical text where
the source archive cannot prove it.

`watched_procedures.json` preserves the current DIP/EUR-Lex observations and
their embedded status-change history. Terminal procedures remain archived but
leave the active polling set. `amendment_fates.json` separates reviewed roles
in a parliamentary document chain from the mechanical checks performed against
the current consolidated corpus.

Every source is documented in the repository's `docs/SOURCES.md`; reproduce
the current outputs with `refresh.sh` and `tools/export_hf.py`. Statutory texts
are official works under § 5 UrhG. Governed by the SNTIQ licensing set; public
dataset under CC BY-NC-SA 4.0.

Built by **[SNTIQ](https://sntiq.com/)**.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "hf_export"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

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

    ordered = (list(SNAPSHOT_FILES) + list(DERIVED_FILES)
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

    for name in ordered:
        print(f"  {name}: {counts[name]:,}")
    print(f"dataset card -> {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
