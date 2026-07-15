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
| Official federal state archive | **172 observations / 63 states / 7 transitions** |
| Legally dated federal transitions | **1** final-text + commencement review |
| Official retrospective BGBl inventory (2023+) | **144 documents / 484 amendment articles / 399 resolved effective dates** |
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

Federal forward history is now retained as complete, content-addressed GII
states. A retrieval observation proves exactly what GII served on that day; it
does **not** by itself prove when the wording entered into force. Lexgraph's
own state diff becomes a legally dated event only after every changed norm is
matched to the integrity-checked final BGBl command and the exact DIP
commencement clause. The observation date, publication date and effective date
remain separate fields throughout Git, archive, Markdown, API and HF exports.

The retrospective layer independently walks promulgated DIP procedures and
the final BGBl archive from 2023 onward. It verifies 144 final PDFs by their
advertised MD5 plus Lexgraph SHA-256, links 484 amendment articles to 44 acts
in the deep corpus and resolves 399 article-wide commencement dates. The other
85 remain explicitly unresolved because DIP assigns different dates below the
article level or provides no article-wide clause. This is a bitemporal store:
`effective_from/effective_to` describe legal validity, while
`knowledge_from/knowledge_to` describe when Lexgraph asserted that result.
Publication, effect and observation dates are never collapsed into one field.
An amendment event is not presented as a reconstructed historical full text;
exact date checkout is enabled only for a verified complete GII state pair.

Third-party database rights are treated separately from copyright in the
individual legal texts. Buzer's private snapshots remain an internal candidate
and QA layer; public act pages may link to the corresponding Buzer history as a
non-authoritative cross-check, but do not copy its database. Lexgraph does not
need a Buzer licence for its own workflow: public federal history is rebuilt
independently from official GII/DIP/BGBl/NeuRIS evidence and Lexgraph's own
state capture, matching and diff engine. See
[`docs/RIGHTS.md`](docs/RIGHTS.md).

## What's here

- **`pipeline/`** — the fetchers (DIP, GII, BGBl, official RII and NeuRIS,
  BAYERN.RECHT, GVBl, Bay. Landtag, EU CELLAR breadth + curated layers,
  Bundesrat) and the PatchInstruction extractor. Every published source is
  documented in [`docs/SOURCES.md`](docs/SOURCES.md),
  [`docs/RIGHTS.md`](docs/RIGHTS.md) and
  [`docs/source-audit.json`](docs/source-audit.json).
- **`data/federal_states/`** — the cumulative official GII observation
  manifest and SHA-256 content-addressed full-state store. Rebuild it from
  complete dated GII snapshots with `tools/archive_gii_states.py`; final BGBl
  commands are captured independently by `pipeline/fetch_bgbl_documents.py`.
- **`pipeline/backfill_bgbl_history.py`** — reproducible 2023+ official
  retrospective inventory from GII identifiers, DIP procedure/dates and
  integrity-checked final BGBl articles. It writes event evidence, command
  addresses and unresolved-date gaps without inventing historical bodies.
- **`tools/build_qfs.py`** — fuses snapshots into a time-scrubbable QFS arena;
  `build_web_data.py` exports web JSON and the SQLite FTS5 full-text index;
  `export_hf.py` builds the versioned Hugging Face dataset; `lex_log.py` and
  `lex_blame.py` inspect one act's chronology and provenance.
- **`web/`** — a self-contained visualizer: Wiki & Realtime feed, **Laws as
  Git** (HEAD, commits, open/closed branches and evidence-bound merge links),
  competence-aware legal layers, a full-text Markdown
  reader/download (whole act or one §/Art.), and the force-directed arena.
  DE/EN/RU/UA. Current HEAD and a checkout backed by an exact archived GII
  observation are labelled exact; incomplete historical source chains remain
  visibly partial.
- **`docs/`** — [`VISION.md`](docs/VISION.md) (the data model & acceptance
  test), [`API.md`](docs/API.md) (the static-JSON + CLI interface),
  [`SOURCES.md`](docs/SOURCES.md), [`RIGHTS.md`](docs/RIGHTS.md).

## Run it

```bash
./refresh.sh                       # pull the live legislative state, rebuild
python3 -m http.server -d web 8777 # then open http://localhost:8777
uvicorn api.server:server --port 8010   # REST API over web/data (see docs/API.md)
curl 'localhost:8010/acts/fed_aufenthg_2004/markdown?norm=%C2%A7%2024'
curl 'localhost:8010/acts/fed_asylvfg_1992/markdown?at=2026-07-06&norm=%C2%A7%2029a'
curl 'localhost:8010/official-transition-reviews?act=fed_asylvfg_1992'
curl 'localhost:8010/acts/fed_asylblg/history'
curl 'localhost:8010/acts/fed_asylvfg_1992/markdown?at=2026-07-10'
curl -OJ 'localhost:8010/retrospective-history.sqlite'
python3 tools/lex_log.py AsylbLG   # chronology for one act
curl 'localhost:8010/federal-history?act=AsylbLG&tier=current_text_correspondence'
```

Data ships via the Hugging Face dataset; the repository carries the pipeline
that reproduces it. The portable official-history layer is split into
retrieval observations, own state transitions, full canonical state objects
and stricter BGBl/DIP transition reviews; see the exact file contracts in
[`docs/API.md`](docs/API.md). SNTIQ-authored software and annotations use the SNTIQ
licensing set in [`LICENSING.md`](LICENSING.md); official and third-party
source material retains the file-level regimes in [`docs/RIGHTS.md`](docs/RIGHTS.md).

Built by **[SNTIQ](https://sntiq.com/)**.
