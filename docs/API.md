# Lexgraph API & Data Interface

Lexgraph models German legislation as **event-sourced git**: every legislative
change — federal (Bund), Bavarian (Bayern), EU, and the other Länder — is a
commit on a jurisdiction lane, HEAD is the consolidated text in force, pending
bills are open branches, and an EU directive transposed into German law is a
*merge*.

Lexgraph's "API" is four things:

- **A) A set of pre-built static JSON files** (`web/data/*.json`) consumed by
  the local web visualizer.
- **B) The pipeline** (`refresh.sh` + `pipeline/fetch_*.py`) that produces the
  snapshots and the QFS arena from which everything else is built.
- **C) A REST API** (`api/`, FastAPI) that serves those static JSON files as
  live HTTP endpoints — same shapes, with CORS — so a browser frontend can call
  it directly. Mirrors the sibling Amtsgraph project's `api/`.
- **D) CLI tools** (`tools/lex_log.py`, `tools/lex_blame.py`) that read the
  snapshot archive directly.

---

# A) Static data interface — `web/data/*.json`

All files are rebuilt by [`tools/build_web_data.py`](../tools/build_web_data.py)
(pure read → write, no network) from the newest snapshot of each source and
from the QFS arena. The web visualizer (`web/index.html`) polls `summary.json`
and lazy-loads the rest.

```bash
python3 tools/build_web_data.py
```

Serve the visualizer locally:

```bash
python3 -m http.server -d web 8777
# open http://localhost:8777/  — 4 views: Wiki & Realtime, Git log,
#                                Hierarchy, Graph; DE/EN toggle.
```

Files written:

| File | Contents |
|------|----------|
| `summary.json` | Dashboard stats + `built_at` (poll target). |
| `feed.json` | Merged realtime event stream, newest first (≤600). |
| `wiki.json` | Act index (federal + Bavaria). |
| `acts/<id>.json` | One full act (head, patches/bills, versions, norms). |
| `decisions.json` | Curated court decisions (Rechtsprechung), newest first. |
| `hierarchy.json` | Jurisdiction tree (EU / Bund / Bayern / Länder). |
| `graph.json` | The QFS arena export (nodes / edges / beliefs / ticks / worlds). |
| `git.json` | The commit-graph of lawmaking. |

JSON is minified (`separators=(",",":")`, `ensure_ascii=False`). All dates are
ISO `YYYY-MM-DD`; the frontend formats to `dd.mm.yyyy`.

---

## `summary.json`

```json
{
  "built_at": "2026-07-06T17:43:26+00:00",
  "acts_fed": 35,
  "acts_by": 10,
  "patches": { "proposed": 564, "adopted": 44, "rejected": 22, "published": 833, "not_merged": 4 },
  "vorgaenge": 358,
  "bay_bills": 123,
  "bay_verkuendet": 60,
  "eu_instruments": 47,
  "transpositions": 136,
  "feed_events": 600,
  "decisions": 3,
  "graph": { "nodes": 720, "edges": 1451, "beliefs": 2558, "ticks": 262, "worlds": 3 }
}
```

| Field | Meaning |
|-------|---------|
| `built_at` | Build timestamp (UTC, ISO). |
| `acts_fed` / `acts_by` | Federal / Bavarian acts in the index. |
| `patches` | Federal patch-command counts by status ladder. |
| `vorgaenge` | DIP legislative procedures. |
| `bay_bills` / `bay_verkuendet` | Bavarian Landtag bills / of those, promulgated. |
| `eu_instruments` / `transpositions` | EU instruments / DEU transposition mentions. |
| `feed_events` | Rows in `feed.json`. |
| `decisions` | Curated court decisions in `decisions.json`. |
| `graph` | Element counts of the arena export (`nodes`, `edges`, `beliefs`, `ticks`, `worlds`). |

---

## `feed.json`

Merged, deduplicated realtime stream across all sources, newest first, capped
at ~600. Array of:

```json
[
  {
    "time":  "2026-10-01",
    "juris": "DE",
    "source": "buzer",
    "kind":  "tritt in Kraft ⏳",
    "title": "Verordnung zur Gleichstellung von Prüfungszeugnissen …",
    "url":   "https://www.buzer.de/gesetz/7822/l.htm",
    "badge": null
  }
]
```

| Field | Meaning |
|-------|---------|
| `time` | Event date (`YYYY-MM-DD`). |
| `juris` | Jurisdiction: `DE`, `DE-BY`, `EU`, `DE-<Land>`, … |
| `source` | Originating source label (`BGBl`, `GVBl`, `OJ L`, `Landtag`, `Parlamentsspiegel`, `buzer`, a court short name like `SG München`, …). |
| `kind` | Event kind (`verkündet`, `veröffentlicht`, `gesetzentwurf`, `tritt in Kraft ⏳`, `Entscheidung`, …). |
| `title` | Truncated to 160 chars. |
| `url` | Source link, or `null`. |
| `badge` | Optional tag (`relevant`, gazette authenticity, …), or `null`. |

---

## `wiki.json`

The act index — the left-hand list in the *Wiki* view. Array of:

```json
[
  {
    "id": "fed_asylblg",
    "jurabk": "AsylbLG",
    "juris": "DE",
    "title": "Asylbewerberleistungsgesetz",
    "norms": 31,
    "build": "20260611",
    "last_change": "2026-06-12",
    "next_change": "2029-06-12",
    "pending": 10,
    "decisions": 3
  }
]
```

| Field | Meaning |
|-------|---------|
| `id` | Act id — `fed_<slug>` (federal) or `by_<slug>` (Bavaria). Filename of the detail file. |
| `jurabk` | Official abbreviation (`AsylbLG`, `AufnG`, …). |
| `juris` | `DE` or `DE-BY`. |
| `title` | Long title. |
| `norms` | Norm (§/Art.) count. |
| `build` | Corpus build date (`YYYYMMDD` federal, `YYYY-MM-DD` Bavaria). |
| `last_change` | Most recent past amendment date. |
| `next_change` | Nearest **future** amendment/version date, or `null`. |
| `pending` | Count of pending patches (`proposed` + `adopted`). |
| `decisions` | Count of curated court decisions touching this act (`0` if none). |

---

## `acts/<id>.json`

The full per-act record. **The shape differs between federal and Bavarian
acts** (they draw from different pipelines) — the common keys are `id`,
`jurabk`, `juris`, `title`, `build`, `norm_count`, `versions`, `temporal`,
`norms`. Both shapes gain an optional `decisions` key (see below) when a
curated court decision touches the act.

### Federal act (e.g. `acts/fed_asylblg.json`)

Keys: `id`, `jurabk`, `juris`, `title`, `stand`, `build`, `norm_count`,
`patches`, `upcoming`, `versions`, `temporal`, `norms`.

```json
{
  "id": "fed_asylblg",
  "jurabk": "AsylbLG",
  "juris": "DE",
  "title": "Asylbewerberleistungsgesetz",
  "stand": "Neugefasst durch Bek. v. …",
  "build": "20260611",
  "norm_count": 31,
  "patches": [
    {
      "status": "proposed",
      "op": "other",
      "para": null,
      "absatz": null,
      "proc": "Gesetz zur Entlastung der Sozialverwaltung",
      "doc": "bt-ds:109/26(B)",
      "stand": "Dem Bundestag zugeleitet - Noch nicht beraten",
      "valid_from": null,
      "old": null,
      "new": null
    }
  ],
  "upcoming": [ { "date": "…", "title": "…", "url": "…" } ],
  "versions": [
    {
      "date": "2026-06-12",
      "text": "§§ § 1 , § 1a , § 11 Artikel 4 GEAS-Anpassungsgesetz vom 23. April 2026 (BGBl. 2026 I Nr. 111)",
      "url": "https://www.buzer.de/gesetz/4846/v338943-2026-06-12.htm"
    }
  ],
  "temporal": {
    "last_change": "2026-06-12",
    "first_change": "2007-08-28",
    "change_count": 17,
    "next_change": "2029-06-12",
    "pending": 10
  },
  "norms": [
    { "enbez": "§ 1", "titel": "Leistungsberechtigte", "text": "(1) Leistungsberechtigt …", "glied": "" }
  ]
}
```

`patches[]` — an extracted Bundestag patch command:

| Field | Meaning |
|-------|---------|
| `status` | `proposed` / `adopted` / `published` / `rejected` / `not_merged`. |
| `op` | Operation (`replace`, `insert`, `repeal`, `other`, …). |
| `para` / `absatz` | Target §/Absatz, or `null`. |
| `proc` | Procedure (bill) title. |
| `doc` | Source document id (`bt-ds:…`). |
| `stand` | `beratungsstand` (parliamentary status text). |
| `valid_from` | In-force date, or `null`. |
| `old` / `new` | Old-text constraint / new text (≤400 chars), or `null`. |

### Bavarian act (e.g. `acts/by_aufng.json`)

Keys: `id`, `jurabk`, `juris`, `title`, `bayrs`, `build`, `norm_count`,
`permalink`, `bills`, `gvbl_events`, `versions`, `temporal`, `norms`.

```json
{
  "id": "by_aufng",
  "jurabk": "AufnG",
  "juris": "DE-BY",
  "title": "Gesetz über die Aufnahme der Leistungsberechtigten nach dem AsylbLG",
  "bayrs": "26-5-1-I",
  "build": "2026-06-11",
  "norm_count": 8,
  "permalink": "https://www.gesetze-bayern.de/Content/Document/BayAufnG",
  "bills": [
    { "status": "abgelehnt", "drs": "19/3866", "title": "Gesetzentwurf … Bayerisches Asylnotstandsgesetz", "gvbl": null }
  ],
  "gvbl_events": [
    { "date": "…", "gazette": "GVBl", "title": "…", "url": "…" }
  ],
  "versions": [
    { "date": "2022-12-09", "text": "Art. 5 geänd. (§ 1 G v. 09.12.2022, S. 676)" }
  ],
  "temporal": { "last_change": "…", "first_change": "…", "change_count": 3, "next_change": null, "pending": 0 },
  "norms": [ { "enbez": "Art. 1", "titel": "…", "text": "…", "glied": "" } ]
}
```

- `bayrs` — BayRS Gliederungsnummer (Bavarian classification key).
- `bills[]` — Landtag bills touching this act: `{status, drs, title, gvbl}`.
- `gvbl_events[]` — GVBl/BayMBl promulgations joined via the BayRS number:
  `{date, gazette, title, url}`.
- `versions[]` — amendment history (`{date, text}`; federal rows also carry a
  synopsis `url`).
- `norms[]` — `{enbez, titel, text, glied}`.

### `decisions[]` (both shapes, optional)

Curated court decisions touching this act, embedded from `decisions.json` as a
**minimal projection**, newest first; the key is **omitted** when there are
none (the `wiki.json` row's `decisions` count is `0` then).

```json
"decisions": [
  {
    "id": "eugh-2026-c-621-24",
    "court_short": "EuGH",
    "level": "EuGH",
    "az": "C-621/24",
    "date": "2026-06-04",
    "kind": "Urteil",
    "title": "Leistungseinschränkung nach § 1a AsylbLG …",
    "effects": [
      { "act_id": "fed_asylblg", "jurabk": "AsylbLG", "paras": ["1a"],
        "kind": "incompatible", "note": "…" }
    ]
  }
]
```

Only the effects whose `act_id` equals this act are embedded. The full record
(multilingual summaries, related decisions, anonymized full text) lives in
`decisions.json` / `GET /decisions/{id}`.

---

## `decisions.json`

Curated court decisions (Rechtsprechung) affecting acts in the corpus —
maintained by hand in `data/decisions.json`, exported as a **plain array**
sorted by date, newest first. Personal data is anonymized per German
court-publication practice. Array of:

```json
[
  {
    "id": "eugh-2026-c-621-24",
    "court": "Gerichtshof der Europäischen Union",
    "court_short": "EuGH",
    "level": "EuGH",
    "az": "C-621/24",
    "date": "2026-06-04",
    "kind": "Urteil",
    "proc": "Vorabentscheidungsverfahren (Art. 267 AEUV)",
    "juris": "EU",
    "title": "Leistungseinschränkung nach § 1a AsylbLG …",
    "summary": { "de": "…", "en": "…", "ru": "…", "ua": "…" },
    "outcome": "unvereinbar",
    "effects": [
      { "act_id": "fed_asylblg", "jurabk": "AsylbLG", "paras": ["1a"],
        "kind": "incompatible", "note": "…" }
    ],
    "related": [
      { "rel": "answers", "ref": "bsg-2024-b-8-ay-6-23-r", "label": "…" }
    ],
    "quote": "…",
    "url": "https://…",
    "source": "EuGH",
    "text": "…"
  }
]
```

| Field | Meaning |
|-------|---------|
| `id` | Decision id (slug) — the `GET /decisions/{id}` key. |
| `court` / `court_short` | Full / short court name. |
| `level` | Court level: `EuGH`, `BVerfG`, `BSG`, `LSG`, `SG`, … |
| `az` | Aktenzeichen (case number). |
| `date` | Decision date. |
| `kind` | `Urteil` / `Beschluss` / `Vorlagebeschluss`. |
| `proc` | Procedure type. |
| `juris` | `EU`, `DE`, `DE-BY`. |
| `title` | German headline. |
| `summary` | One summary per UI language: `{de, en, ru, ua}`. |
| `outcome` | Outcome in a few words. |
| `effects[]` | What the decision does to which norms: `act_id` (corpus act id, when the act is in the index), `eu_celex` (for EU instruments), `jurabk`, `paras`, `kind` (`disapplied` / `incompatible` / `referred` / `interpreted` / `applied`), `note`. |
| `related[]` | Links between decisions: `rel` (`follows` / `answers` / `cites`), `ref` (decision id), `label`. |
| `quote` | Key quote from the decision, or `null`. |
| `url` | Source link, or `null`. |
| `source` | Source label (court / database). |
| `text` | Anonymized full text, or `null`. |

Each decision also appears in `feed.json` (`kind: "Entscheidung"`, `source` =
`court_short`), as a per-act embedding in `acts/<id>.json`, and as a
`decisions` count on the `wiki.json` rows.

---

## `hierarchy.json`

The jurisdiction tree (no graph geometry) — the *Hierarchy* view.

```json
{
  "eu":     { "instruments": [ { "celex": "32001L0055", "kind": "directive", "title": "…", "in_force": true, "geas": true, "deu_mnes": 3 } ] },
  "bund":   { "acts": [ /* wiki rows where juris=DE */ ], "pipeline": { "<beratungsstand>": [ { "title": "…", "date": "…" } ] } },
  "bayern": { "acts": [ /* wiki rows where juris=DE-BY */ ], "pipeline": { "<status>": [ { "title": "…", "drs": "…", "date": "…" } ] } },
  "laender": { "DE-BB": [ { "title": "…", "date": "…", "url": "…" } ], "DE-BE": [ … ] }
}
```

- `eu.instruments[]`: `celex`, `kind`, `title`, `in_force`, `geas` (in the GEAS
  core), `deu_mnes` (number of DEU transposition mentions).
- `bund` / `bayern`: `acts` (the matching `wiki.json` rows) plus a `pipeline`
  bucketed by parliamentary status.
- `laender`: bills/activity per Land, keyed by `DE-<code>`.

---

## `graph.json`

The QFS arena export for the *Graph* view — the belief/contradiction arena of
the legislative process.

```json
{
  "nodes":  [ { "label": "WIRD_GESETZ", "trust": 3, "kind": "hub", "born": 23826 } ],
  "edges":  [ { "s": 38, "t": 39, "r": 10, "d": 1.0 } ],
  "beliefs":[ { "n": 38, "b": 24318, "pT": 0.15, "pF": 0.05, "pN": 0.8 } ],
  "ticks":  [ 23826, 23886, 23903 ],
  "tick_labels": { "23826": "1985-07", "23886": "1990-07" },
  "worlds": [ { "id": 1, "stability": 0.405, "contradiction": 0.028 } ]
}
```

| Field | Meaning |
|-------|---------|
| `nodes[]` | `label` (≤80 chars), `trust`, `kind` (`hub`, `fed-act`, `by-act`, `by-bill`, `eu`, `land`, `initiator`, `topic`, `vorgang`), `born` (first tick the node is believed in). |
| `edges[]` | `s`/`t` = source/target **node index** (into `nodes`), `r` = relation type (int), `d` = delta (directionality, rounded). |
| `beliefs[]` | `n` = node index, `b` = born tick, `pT`/`pF`/`pN` = P(true)/P(false)/P(none). |
| `ticks[]` | Sorted list of tick ordinals present. |
| `tick_labels` | `{tick → "YYYY-MM"}`. |
| `worlds[]` | Arena worlds: `id`, `stability`, `contradiction` level. |

---

## `git.json`

The normative history rendered as a **git commit graph** — one commit per
legislative change, laned by jurisdiction, newest first.

```json
{
  "lanes": ["EU", "Bund", "Bayern", "Länder"],
  "total": 744,
  "commits": [
    {
      "hash": "631ee95e",
      "date": "2026-07-01",
      "lane": 1,
      "type": "open",
      "actor": "Bundestag",
      "msg": "Gesetz zur Reform der Notfallversorgung",
      "acts": ["SGB 5", "SGB 11"],
      "paras": ["15", "27", "30", "60", "73", "75", "76", "87"],
      "refs": ["proposed", "Dem Bundestag zugeleitet - Noch nicht beraten"],
      "merge_ref": null,
      "doc": "bt-ds:…"
    }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `lanes` | The four jurisdiction lanes, in index order. |
| `total` | Commit count. |
| `commits[].hash` | 8-hex CRC32 of the commit key. |
| `commits[].date` | Commit date. |
| `commits[].lane` | **Integer** index into `lanes` (`0`=EU, `1`=Bund, `2`=Bayern, `3`=Länder). |
| `commits[].type` | `commit` (enacted) / `open` (pending branch) / `merge` (an EU directive merged into German law). |
| `commits[].actor` | `Bundestag`, `Landtag Bayern`, `EU`, `Landtag <Land>`. |
| `commits[].msg` | Title (≤120 chars). |
| `commits[].acts` | Affected acts (≤6). |
| `commits[].paras` | Affected §§ (≤8). |
| `commits[].refs` | Status / stand tags. |
| `commits[].merge_ref` | For `type=merge`: the CELEX-style id of the merged directive/regulation (e.g. `32023L2225`), else `null`. |
| `commits[].doc` | Source document (`bt-ds:…`, Drs.-Nr., …). |

Per-lane extras: EU commits add `celex`; Bavarian add `url` and `gvbl`;
Länder add `url`.

---

# B) Pipeline — how the data is produced

[`refresh.sh`](../refresh.sh) is the cron entrypoint. It pulls the live
legislative state across Bund / Bayern / EU / Länder, rebuilds the QFS arena,
then exports the web JSON. **Fetch steps degrade gracefully** (a flaky source
must not kill the arena rebuild); only the final build is fatal, and fetchers
refuse to overwrite a good same-day snapshot with empty output.

```bash
./refresh.sh
```

The 16 steps (note: the script's early step labels read `n/14`, the later ones
`n/16` — a cosmetic inconsistency; there are 16 steps):

| # | Step | Fetcher |
|---|------|---------|
| 1 | DIP legislative pipeline (Bund, intraday) | `fetch_dip.py` |
| 2 | BGBl promulgation events | `fetch_bgbl_events.py` |
| 3 | GII corpus HEAD | `fetch_gii.py` |
| 4 | NeuRIS changelog (append-only) | `fetch_neuris_changelog.py` |
| 5 | buzer back-history (max once/day; skipped if today's snapshot exists) | `fetch_buzer.py` |
| 6 | PatchInstruction extraction (writes the DIP text cache) | `extract_patches.py` |
| 7 | Bundesrat texts (cache-first, 30 s crawl-delay) | `fetch_br_texts.py` |
| 8 | PatchInstruction **re-extraction** (only if new BR texts arrived) | `extract_patches.py` |
| 9 | BAYERN.RECHT corpus HEAD + BayRS chains | `fetch_bayern_recht.py` |
| 10 | GVBl/BayMBl promulgation events (RSS) | `fetch_gvbl_events.py` |
| 11 | Bayerischer Landtag pipeline | `fetch_bay_landtag.py` |
| 12 | EU layer (CELLAR + DEU transpositions + OJ-L) | `fetch_eu_layer.py` |
| 13 | Länder monitor (Parlamentsspiegel, Asyl/Sozial) | `fetch_parlamentsspiegel.py` |
| 14 | Länder-Gesetzentwürfe (all 16 Landtage) | `fetch_laender_bills.py` |
| 15 | Build the QFS arena | `tools/build_qfs.py` |
| 16 | Export web data | `tools/build_web_data.py` |

Snapshots land in `data/snapshots/<source>/<date>/*.jsonl`; each build reads the
newest snapshot per source. **Each fetcher documents its own source, cadence,
and quirks in its module docstring** — read the top of any
`pipeline/fetch_*.py` for the authoritative behavior of that step.

Step 15 also deploys the arena to a local `qfs_visualizer` checkout if present.

---

# C) REST API — `api/` (FastAPI)

A small, read-only HTTP wrapper around the section-A data plane. It does **not**
recompute anything: `tools/build_web_data.py` is the build step, `web/data/*.json`
*are* the data, and each endpoint just projects one of those files. **Response
shapes equal the JSON files' shapes** documented in section A, so the frontend
and these docs share one contract. This mirrors the sibling
[Amtsgraph](https://github.com/SNTIQ-Team/amtsgraph) project's `api/`
(`api/main.py` = the app, `api/server.py` = the ASGI composition root).

## Run

```bash
pip install fastapi uvicorn            # or: pip install -r requirements.txt
python3 tools/build_web_data.py        # ensure web/data/*.json exists
uvicorn api.server:server --host 127.0.0.1 --port 8010 --workers 1
# → http://127.0.0.1:8010  (interactive docs at /docs)
```

The data directory defaults to `<repo>/web/data`; override it with the
`LEXGRAPH_DATA` environment variable (e.g. a deployment path). The dataset is
static per deploy, so responses are cached in-process and sent with
`Cache-Control: public, max-age=3600`. **CORS is open (`*`)** so any browser
frontend can call the API. The data plane is loaded lazily and cached; there is
no database.

## Endpoint summary

| Method & path | Serves | Notes |
|---|---|---|
| `GET /` | service index | operator, dataset, endpoint list; browsers (`Accept: text/html`) get an HTML landing page |
| `GET /health` | liveness | `{status, built_at, data_dir}`; 503 if data missing |
| `GET /version` | build id | `dataset`, `version`, `built_at` |
| `GET /stats` | `summary.json` | the dashboard counts, verbatim |
| `GET /feed?limit=` | `feed.json` | newest first; `limit` 1–600 (default 100) |
| `GET /acts` | `wiki.json` | the act index |
| `GET /acts/{id}` | `acts/<id>.json` | full act; **404** if unknown |
| `GET /decisions?q=&act=` | `decisions.json` | court decisions, newest first; `limit` 1–200 (default 50) |
| `GET /decisions/{id}` | one `decisions.json` row | full decision; **404** if unknown |
| `GET /git?lane=&limit=` | `git.json` | optional `lane` 0–3; `limit` 1–1000 |
| `GET /graph` | `graph.json` | the QFS arena export |
| `GET /hierarchy` | `hierarchy.json` | the jurisdiction tree |
| `GET /search?q=` | `wiki.json` rows | substring match on `jurabk`/`title` |
| `GET /digest` | `digest.json` | **experimental, LLM-generated** activity digest; **404** if none generated |

`/stats`, `/acts`, `/acts/{id}`, `/decisions/{id}`, `/graph`, `/hierarchy`
return the underlying JSON **unchanged** (see section A for every field). The
endpoints below add a thin envelope.

## `GET /health`

```json
{ "status": "ok", "built_at": "2026-07-06T17:43:26+00:00",
  "data_dir": "…/web/data" }
```

Returns **503** if `summary.json` is missing (run `tools/build_web_data.py`).

## `GET /version`

```json
{ "dataset": "Lexgraph", "version": "1.0",
  "built_at": "2026-07-06T17:43:26+00:00",
  "source": "https://github.com/SNTIQ-Team/lexgraph" }
```

## `GET /feed?limit=`

Newest-first slice of `feed.json`. `limit` ∈ [1, 600], default 100.

```bash
curl 'http://127.0.0.1:8010/feed?limit=2'
```

```json
{ "total": 600, "limit": 2, "events": [ { "time": "2026-10-01", "juris": "DE",
  "source": "buzer", "kind": "tritt in Kraft ⏳", "title": "…", "url": "…",
  "badge": null } ] }
```

## `GET /acts` and `GET /acts/{id}`

`/acts` is `wiki.json` verbatim (the act index). `/acts/{id}` returns the full
per-act record `acts/<id>.json` (federal or Bavarian shape — see section A) and
**404** for an unknown id.

```bash
curl http://127.0.0.1:8010/acts/fed_asylblg
```

```json
{ "id": "fed_asylblg", "jurabk": "AsylbLG", "juris": "DE",
  "title": "Asylbewerberleistungsgesetz", "stand": "…", "build": "20260611",
  "norm_count": 31, "patches": [ … ], "upcoming": [], "versions": [ … ],
  "temporal": { … }, "norms": [ … ] }
```

## `GET /git?lane=&limit=`

The commit-graph `git.json`. Optional `lane` filters by the integer lane index
(`0`=EU, `1`=Bund, `2`=Bayern, `3`=Länder); `limit` ∈ [1, 1000], default 100.

```bash
curl 'http://127.0.0.1:8010/git?lane=0&limit=3'
```

```json
{ "lanes": ["EU","Bund","Bayern","Länder"], "lane": 0, "total": 47,
  "commits": [ { "hash": "db392498", "date": "2026-01-01", "lane": 0,
  "type": "commit", "actor": "EU", "msg": "Verordnung (EU) 2025/2649 …",
  "acts": [], "paras": [], "refs": [ … ], "merge_ref": null, "doc": "…",
  "celex": "…" } ] }
```

## `GET /search?q=`

Case-insensitive substring match on `jurabk` and `title` across `wiki.json`;
returns act-index rows (same shape as `/acts`). `limit` ∈ [1, 200], default 25.

```bash
curl 'http://127.0.0.1:8010/search?q=Asyl'
```

```json
{ "query": "Asyl", "total": 4, "matches": [
  { "id": "fed_asylblg", "jurabk": "AsylbLG", "title": "Asylbewerberleistungsgesetz", … },
  { "id": "fed_asylvfg_1992", "jurabk": "AsylVfG 1992", "title": "Asylgesetz", … } ] }
```

## `GET /decisions?q=&act=` and `GET /decisions/{id}`

Court decisions from `decisions.json` (full rows — see section A), newest
first. `q` matches case-insensitively in `az`, `court`, `court_short`,
`title`, all `summary` languages, and `effects[].jurabk`; `act` filters by
`effects[].act_id` (an act index id); `limit` ∈ [1, 200], default 50.
`/decisions/{id}` returns one full decision and **404** for an unknown id.

```bash
curl 'http://127.0.0.1:8010/decisions?act=fed_asylblg&limit=1'
```

```json
{ "query": null, "act": "fed_asylblg", "total": 3, "decisions": [
  { "id": "sg-muenchen-2026-s-42-ay-55-26-er", "court_short": "SG München",
    "level": "SG", "az": "S 42 AY 55/26 ER", "date": "2026-07-06",
    "kind": "Beschluss", "title": "Leistungskürzung nach § 1a AsylbLG …",
    "summary": { "de": "…", "en": "…", "ru": "…", "ua": "…" },
    "effects": [ … ], … } ] }
```

## `GET /digest`

**Experimental.** A short multilingual digest of legislative activity —
`web/data/digest.json`, written by `tools/build_digest.py` (refresh step 17)
when `OPENROUTER_API_KEY` is set. The facts are computed deterministically
from the section-A data; the phrasing is **LLM-generated** (the winning
model id is in `model`). Informational only, not legal advice.

```json
{ "generated_at": "2026-07-13T04:00:00+00:00",
  "model": "deepseek/deepseek-chat-v3-0324:free", "llm": true,
  "periods": {
    "year":     { "de": "…", "en": "…", "ru": "…", "ua": "…" },
    "month":    { "de": "…", "en": "…", "ru": "…", "ua": "…" },
    "upcoming": { "de": "…", "en": "…", "ru": "…", "ua": "…" } } }
```

Returns **404** (`{"detail": "no digest available"}`) when no digest has
been generated yet; the file is read fresh per request (never cached in
process), so a refresh replaces it without a restart.

## Dependencies

`fastapi` + `uvicorn` (see [`requirements.txt`](../requirements.txt)); the app
itself uses only the standard library beyond those. If FastAPI is not installed,
the section-A static files remain fully usable without the server.

---

# D) CLI tools

Read-only over `data/snapshots` — **no network, no snapshot writes**. Acts
resolve case-insensitively by `jurabk` (federal GII corpus first, then the
Bavarian corpus), so the same tools work for federal and Bavarian acts.

## `tools/lex_log.py` — `git log` for one act

```bash
python3 tools/lex_log.py AsylbLG          # federal corpus
python3 tools/lex_log.py AufnG            # Bavarian corpus
python3 tools/lex_log.py "SGB 2" --all    # full back-history
```

Prints, in one view:

- **HEAD** — GII / BAYERN.RECHT build date, norm count (and BayRS number for
  Bavaria).
- **Pipeline** — pending Bundestag patches / Landtag bills, on the status
  ladder, explicitly marked **NOT geltendes Recht**.
- **Promulgated, enters force soon** — buzer `/v.htm` upcoming.
- **Back-history** — amendment history (buzer 2006+ for federal;
  ffn *Fortführungsnachweis* + XML `aenderungsverlauf` for Bavaria). Last 10 by
  default; `--all` for the full list.

A recommended-but-unmerged patch always appears under its real status and never
as geltendes Recht — the VISION acceptance discipline.

## `tools/lex_blame.py` — `git blame` / `git checkout` over the snapshots

```bash
# every amendment/patch touching one §/Art., newest first, across all tiers
python3 tools/lex_blame.py blame AsylbLG 3a
python3 tools/lex_blame.py blame AufnG 4

# which consolidated version was in force at a date
python3 tools/lex_blame.py checkout AsylbLG --at 2020-06-01
```

- **`blame <ACT> <REF>`** — merges three tiers: `buzer` (federal back-history,
  affected-§ list parsed from the synopsis title, non-authoritative);
  `amtlich` (Bavarian BayRS ffn + XML); `pipeline` (extracted Bundestag patch
  commands, pending ones flagged NOT geltendes Recht). `REF` accepts `3a`,
  `§ 3a`, `Art. 4` alike. Version rows that name no §/Art. are kept as
  `? unspezif.` rather than hidden — they *may* touch the ref, and silence is
  not proof.
- **`checkout <ACT> --at YYYY-MM-DD`** — the latest consolidated version dated
  `≤ AT`, how many amendments came after, and the synopsis of the *next*
  change (that diff is exactly what changed next).

---

## The QFS arena & licensing

The arena lives at `data/lexgraph_de_wp21.qfs` (binary; produced by
`tools/build_qfs.py`, consumed by `build_web_data.py` and by the standalone
`qfs_visualizer`). `graph.json` above is its JSON projection.

Licensing is governed by the repository's own terms.
