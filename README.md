# ⟠ Lexgraph

**German legislation as event-sourced git.**

Lexgraph models the whole normative process — federal (Bund), Bavarian
(Bayern), EU, and the other 15 Länder — as a signed, temporal, multi-authority
patch history over normative state. Every legislative change is a *commit* on a
jurisdiction lane; the consolidated text in force is *HEAD*; a bill still in
committee is an *open branch*; and an EU directive transposed into German law is
a *merge*.

It answers, in real time and with provenance: **what law exists, where it comes
from, when it may pass, when it was last amended, and what changed.**

| | |
|---|---|
| WP21 legislative procedures (DIP) | **362** |
| Extracted PatchInstructions | **1,484** (607 proposed · 9 adopted · 841 published · 23 rejected · 4 not merged) |
| Federal acts (GII) + norms | **51 / 10,939** |
| Amendment versions (buzer, 2006+) | **1,674** |
| Bavarian acts (BAYERN.RECHT) + versions | **11 / 515** (since 1985) |
| Curated EU instruments + German transpositions | **47 / 136** |
| EU in-force metadata index | **7,934** directives and basic regulations |
| Court decisions | **82** (3 reviewed + 79 official federal RII) |
| Länder bills (all 16 Landtage) | **439** |
| QFS arena | 727 nodes · 2,574 beliefs · 3 jurisdiction worlds |

The EU breadth index covers every in-force directive (including delegated and
implementing directives) and basic regulation exposed by CELLAR, as metadata
only; texts and deep change history remain in the curated corpus. Court data
combines reviewed manual cases with a forward-cumulative import from the seven
official federal RII feeds. It is corpus-filtered and is not a catalogue of all
German courts; lower-court decisions remain curated manually.

For Bavaria, the 515 official version rows are amendment metadata, not 515
complete historical texts. Word-level old/new text is available only where
archived official pages yield an unambiguous state transition (sparse from
2016 onward), plus complete forward diffs between daily snapshots from July
2026. Missing historical diffs are left missing rather than reconstructed.

## What's here

- **`pipeline/`** — the fetchers (DIP, GII, BGBl, official RII, NeuRIS, buzer,
  BAYERN.RECHT, GVBl, Bay. Landtag, EU CELLAR breadth + curated layers,
  Parlamentsspiegel, Bundesrat) and the PatchInstruction extractor. Every
  source is live-verified; see
  [`docs/SOURCES.md`](docs/SOURCES.md) and [`docs/source-audit.json`](docs/source-audit.json).
- **`tools/build_qfs.py`** — fuses the snapshots into a QFS arena (the
  time-scrubbable graph), `build_web_data.py` exports the web JSON,
  `lex_log.py` / `lex_blame.py` are `git log` / `git blame` for a single act.
- **`web/`** — a self-contained visualizer: Wiki & Realtime feed, a `git log`
  of lawmaking, the jurisdiction hierarchy, and the force-directed arena. DE/EN.
- **`docs/`** — [`VISION.md`](docs/VISION.md) (the data model & acceptance
  test), [`API.md`](docs/API.md) (the static-JSON + CLI interface),
  [`SOURCES.md`](docs/SOURCES.md).

## Run it

```bash
./refresh.sh                       # pull the live legislative state, rebuild
python3 -m http.server -d web 8777 # then open http://localhost:8777
uvicorn api.server:server --port 8010   # REST API over web/data (see docs/API.md)
python3 tools/lex_log.py AsylbLG   # git log for one act (federal or Bavarian)
```

Data ships via the Hugging Face dataset; the repository carries the pipeline
that reproduces it. Governed by the SNTIQ licensing set — see
[`LICENSING.md`](LICENSING.md).

Built by **[SNTIQ](https://sntiq.com/)**.
