# ⟠ Lexgraph

**Lexgraph — a temporal graph of German and EU law.**

Lexgraph models legal texts, amendments, legislative procedures and case links
across the federal (Bund), Bavarian (Bayern) and EU levels as a sourced,
temporal graph. It distinguishes current consolidated text, past changes,
pending bills and legally specific EU implementation/applicability links.

It answers, in real time and with provenance: **what law exists, where it comes
from, when it may pass, when it was last amended, and what changed.**

| | |
|---|---|
| WP21 legislative procedures (DIP) | **362** |
| Extracted PatchInstructions | **1,484** (607 proposed · 9 adopted · 841 published · 23 rejected · 4 not merged) |
| Federal acts (GII) + norms | **51 / 10,939** |
| Bavarian acts (BAYERN.RECHT) + versions | **12 / 531** (since 1985) |
| Curated EU instruments + German transpositions | **47 / 136** |
| EU in-force metadata index | **7,934** directives and basic regulations |
| Court decisions | **82** (3 reviewed + 79 official federal RII) |
| QFS arena | 745 nodes · 2,588 beliefs · 3 jurisdiction worlds |

The EU breadth index covers every in-force directive (including delegated and
implementing directives) and basic regulation exposed by CELLAR, as metadata
only; texts and deep change history remain in the curated corpus. Court data
combines reviewed manual cases with a forward-cumulative import from the seven
official federal RII feeds. It is corpus-filtered and is not a catalogue of all
German courts; lower-court decisions remain curated manually.

Explicitly watched procedures are checked twice daily against DIP, EUR-Lex
and Council evidence. Each API row carries a deterministic evidence analysis:
verified facts, document-role and final-text checks, official chronology,
next milestone, uncertainty factors and a qualitative forecast. Predictions
are never promoted to facts; recommendations and political preparations are
kept distinct from adoption and promulgation.

For Bavaria, the 531 official version rows are amendment metadata, not 531
complete historical texts. Word-level old/new text is available only where
archived official pages yield an unambiguous state transition (sparse from
2016 onward), plus complete forward diffs between daily snapshots from July
2026. Missing historical diffs are left missing rather than reconstructed.

Third-party database rights are treated separately from copyright in the
individual legal texts. Buzer and broad Parlamentsspiegel snapshots are kept
out of the public API/Hugging Face export unless written reuse permission is
obtained; see [`docs/RIGHTS.md`](docs/RIGHTS.md). Federal back-history is being
rebuilt from official BGBl/NeuRIS data and Lexgraph's own daily snapshots.

## What's here

- **`pipeline/`** — the fetchers (DIP, GII, BGBl, official RII and NeuRIS,
  BAYERN.RECHT, GVBl, Bay. Landtag, EU CELLAR breadth + curated layers,
  Bundesrat) and the PatchInstruction extractor. Every published source is
  documented in [`docs/SOURCES.md`](docs/SOURCES.md),
  [`docs/RIGHTS.md`](docs/RIGHTS.md) and
  [`docs/source-audit.json`](docs/source-audit.json).
- **`tools/build_qfs.py`** — fuses snapshots into a time-scrubbable QFS arena;
  `build_web_data.py` exports web JSON and the SQLite FTS5 full-text index;
  `export_hf.py` builds the versioned Hugging Face dataset; `lex_log.py` and
  `lex_blame.py` inspect one act's chronology and provenance.
- **`web/`** — a self-contained visualizer: Wiki & Realtime feed, a dated
  legislative chronology, competence-aware legal layers, a full-text Markdown
  reader/download (whole act or one §/Art.), and the force-directed arena.
  DE/EN/RU/UA. Only the current consolidated snapshot is labelled exact;
  incomplete historical source chains remain visibly partial.
- **`docs/`** — [`VISION.md`](docs/VISION.md) (the data model & acceptance
  test), [`API.md`](docs/API.md) (the static-JSON + CLI interface),
  [`SOURCES.md`](docs/SOURCES.md), [`RIGHTS.md`](docs/RIGHTS.md).

## Run it

```bash
./refresh.sh                       # pull the live legislative state, rebuild
python3 -m http.server -d web 8777 # then open http://localhost:8777
uvicorn api.server:server --port 8010   # REST API over web/data (see docs/API.md)
curl 'localhost:8010/acts/fed_aufenthg_2004/markdown?norm=%C2%A7%2024'
python3 tools/lex_log.py AsylbLG   # chronology for one act
```

Data ships via the Hugging Face dataset; the repository carries the pipeline
that reproduces it. SNTIQ-authored software and annotations use the SNTIQ
licensing set in [`LICENSING.md`](LICENSING.md); official and third-party
source material retains the file-level regimes in [`docs/RIGHTS.md`](docs/RIGHTS.md).

Built by **[SNTIQ](https://sntiq.com/)**.
