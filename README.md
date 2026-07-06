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
| WP21 legislative procedures (DIP) | **358** |
| Extracted PatchInstructions | **1,467** (833 enacted · 608 pending) |
| Amendment versions (buzer, 2006+) | **1,673** |
| Bavarian acts (BAYERN.RECHT) + versions | **10 / 503** (since 1985) |
| EU instruments + German transpositions | **47 / 136** |
| Länder bills (all 16 Landtage) | **439** |
| QFS arena | 720 nodes · 2,558 beliefs · 3 jurisdiction worlds |

## What's here

- **`pipeline/`** — the fetchers (DIP, GII, BGBl, NeuRIS, buzer, BAYERN.RECHT,
  GVBl, Bay. Landtag, EU CELLAR, Parlamentsspiegel, Bundesrat) and the
  PatchInstruction extractor. Every source is live-verified; see
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
python3 tools/lex_log.py AsylbLG   # git log for one act (federal or Bavarian)
```

Data ships via the Hugging Face dataset; the repository carries the pipeline
that reproduces it. Governed by the SNTIQ licensing set — see
[`LICENSING.md`](LICENSING.md).

Built by **[SNTIQ](https://sntiq.com/)**.
