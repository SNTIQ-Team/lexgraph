# Lexgraph API & Data Interface

Lexgraph models German and EU law as a temporal legal graph: consolidated text,
dated amendments, official legislative stages, court decisions and explicit
links between them. The optional `git.json` projection is one visualization of
that history; it is not the product name or a claim about legal hierarchy.

Lexgraph's "API" is four things:

- **A) A set of pre-built static JSON files** (`web/data/*.json`) consumed by
  the local web visualizer.
- **B) The pipeline** (`refresh.sh` + `pipeline/fetch_*.py`) that produces the
  snapshots and the QFS arena from which everything else is built.
- **C) A REST API** (`api/`, FastAPI) that serves those static JSON files as
  live HTTP endpoints — verbatim or in small filter/pagination envelopes, with
  CORS — so a browser frontend can call it directly. Mirrors the sibling
  Amtsgraph project's `api/`.
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
| `decisions.json` | Manual + official cumulative RII decisions, newest first. |
| `eu_index.json` | In-force EU directives + basic regulations, metadata only. |
| `search.sqlite` | Read-only FTS5 index over act metadata and complete current norm text. |
| `hierarchy.json` | Competence-aware legal layers (EU / Bund / Bayern / Länder). |
| `watched_procedures.json` | Persistent DIP/EUR-Lex watch state and change history. |
| `amendment_fates.json` | Reviewed document chains plus mechanical current-law checks. |
| `graph.json` | The QFS arena export (nodes / edges / beliefs / ticks / worlds). |
| `git.json` | The commit-graph of lawmaking. |

JSON is minified (`separators=(",",":")`, `ensure_ascii=False`). All dates are
ISO `YYYY-MM-DD`; the frontend formats to `dd.mm.yyyy`.

---

## `summary.json`

```json
{
  "built_at": "2026-07-14T22:33:57+00:00",
  "acts_fed": 51,
  "acts_by": 12,
  "patches": { "proposed": 607, "adopted": 9, "rejected": 23, "published": 841, "not_merged": 4 },
  "vorgaenge": 362,
  "watched_procedures": [
    { "id": "329468", "source": "DIP", "status": "Überwiesen", "active": true, "terminal": false, "…": "…" },
    { "id": "eu-2026-0186-nle", "source": "EUR-Lex", "status": "Ongoing", "active": true, "terminal": false, "…": "…" }
  ],
  "watched_active": 2,
  "watched_terminal": 1,
  "amendment_fates": 1,
  "amendment_fates_validated": 1,
  "bay_bills": 123,
  "bay_verkuendet": 60,
  "eu_instruments": 47,
  "eu_index_total": 7934,
  "transpositions": 136,
  "feed_events": 600,
  "decisions": 82,
  "search": { "acts": 63, "norms": 11852 },
  "graph": { "nodes": 745, "edges": 1471, "beliefs": 2588, "ticks": 264, "worlds": 3 }
}
```

| Field | Meaning |
|-------|---------|
| `built_at` | Build timestamp (UTC, ISO). |
| `acts_fed` / `acts_by` | Federal / Bavarian acts in the index. |
| `patches` | Federal patch-command counts by status ladder. |
| `vorgaenge` | DIP legislative procedures. |
| `watched_procedures` | Compact rows from the persistent DIP/EUR-Lex watch ledger. An active entry can still be a non-enacted proposal. |
| `watched_active` / `watched_terminal` | Active polling set / terminal records retained as an archive. |
| `amendment_fates` / `amendment_fates_validated` | Reviewed document-chain records / records whose declared current-law checks passed. |
| `bay_bills` / `bay_verkuendet` | Bavarian Landtag bills / of those, promulgated. |
| `eu_instruments` / `transpositions` | Curated EU instruments / DEU transposition mentions in the deep layer. |
| `eu_index_total` | Metadata rows in the EU breadth index; `0` until it has been fetched. |
| `feed_events` | Rows in `feed.json`. |
| `decisions` | Merged manual + official RII decisions in `decisions.json`. |
| `search` | Acts and current norms indexed in `search.sqlite`. |
| `graph` | Element counts of the arena export (`nodes`, `edges`, `beliefs`, `ticks`, `worlds`). |

---

## `eu_index.json`

Breadth metadata for all in-force directives (`DIR`, `DIR_DEL`, `DIR_IMPL`)
and basic regulations (`REG`) returned by CELLAR. Delegated and implementing
regulations are deliberately excluded. This file contains no legal text or
diffs; each `celex` is the stable key for linking to EUR-Lex.

```json
{
  "built_at": "2026-07-14",
  "total": 7934,
  "instruments": [
    { "celex": "32026R1547", "type": "REG", "date": "2026-07-01",
      "title": "Verordnung (EU) 2026/1547 …" }
  ]
}
```

Titles are German where available, with English fallback. The current count is
about 7,934 and will move as CELLAR's in-force status changes.

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
| `decisions` | Count of merged manual/RII decisions touching this act (`0` if none). |

---

## `acts/<id>.json`

The full per-act record. **The shape differs between federal and Bavarian
acts** (they draw from different pipelines) — the common keys are `id`,
`jurabk`, `juris`, `title`, `build`, `norm_count`, `versions`, `temporal`,
`norms`. Both shapes gain an optional `decisions` key (see below) when a court
decision from the merged manual/RII layer touches the act.

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
  synopsis `url`). Bavarian rows may carry `changes[]` with exact
  `{para, old, new}` text. Historical `changes` are sparse, conservative
  Wayback matches from 2016+; daily snapshot diffs are complete only from July
  2026 onward. Absence of `changes` does not mean the amendment changed no text.
- `norms[]` — `{enbez, titel, text, glied}`.

### `decisions[]` (both shapes, optional)

Court decisions touching this act, embedded from `decisions.json` as a
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

Only the effects whose `act_id` equals this act are embedded. The complete
exported row lives in `decisions.json` / `GET /decisions/{id}`; reviewed manual
rows can include multilingual summaries, relations, and anonymized text, while
automatic RII rows are metadata-only.

---

## `decisions.json`

Court decisions (Rechtsprechung) affecting acts in the corpus, exported as a
**plain array** sorted by date, newest first. The build merges reviewed manual
rows from `data/decisions.json` with the latest forward-cumulative official RII
snapshot. Manual rows win duplicate ids or court/date/docket cases because they
carry richer reviewed summaries and relations.

RII intake covers the rolling feeds of BVerfG, BGH, BVerwG, BFH, BAG, BSG, and
BPatG, filtered by official `<norm>` metadata to acts in the GII corpus. It
accumulates from the first successful snapshot forward; it is neither a full
historical archive nor all German courts. Lower-court and EU cases remain
manual. Automated RII rows normally have a German summary and no embedded full
text; manual rows may add multilingual summaries, relations, quotes, and text.
Their official norm links use the neutral effect kind `cited`: `<norm>`
proves that the decision cites a provision, but does not by itself prove that
the court interpreted or applied it.
Array of:

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
| `summary` | Language-keyed summaries; manual rows may have `{de, en, ru, ua}`, RII rows normally only `de`. |
| `outcome` | Outcome in a few words. |
| `effects[]` | How the decision links to norms: `act_id` (corpus act id, when the act is in the index), `eu_celex` (for EU instruments), `jurabk`, `paras`, `kind` (`cited` / `disapplied` / `incompatible` / `referred` / `interpreted` / `applied`), `note`. Automated RII rows use `cited`; stronger kinds are reviewed manual assertions. |
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

The competence-aware legal structure (no graph geometry) used by the
*Hierarchy* view. Its display order is navigation, **not** a claim that every
entry is subordinate to the preceding section.

```json
{
  "meta": { "schema_version": 2, "model": "competence-aware", "not_a_total_order": true },
  "eu": {
    "instruments": [ { "celex": "32001L0055", "kind": "directive", "title": "…", "in_force": true, "geas": true, "deu_mnes": 3 } ],
    "primary": { "indexed": false, "references": [ { "celex": "12016M/TXT", "kind": "treaty", "title": "…", "in_corpus": false } ] },
    "secondary": { "directives": [ … ], "regulations": [ … ], "other": [] },
    "pipeline": { "Ongoing": [ { "id": "eu-2026-0186-nle", "source": "EUR-Lex", "stage": "…", "terminal": false } ] }
  },
  "bund":   { "acts": [ … ], "layers": { "constitution": [ … ], "statutes": [ … ], "ordinances": [ … ] }, "pipeline": { "<beratungsstand>": [ … ] } },
  "bayern": { "acts": [ … ], "layers": { "constitution": [ … ], "statutes": [ … ], "ordinances": [ … ] }, "pipeline": { "<status>": [ … ] } },
  "laender": { "DE-BB": [ { "title": "…", "date": "…", "url": "…" } ], "DE-BE": [ … ] }
}
```

- `eu.instruments[]` is the schema-v1-compatible flat list. `eu.primary`
  contains official external references because primary EU law is not part of
  the deep corpus; `eu.secondary` groups the indexed list by legal form.
- `eu.pipeline` contains explicitly watched pending EUR-Lex procedures. It is
  separate from instruments in force and preserves the official stage without
  inferring adoption.
- `bund` / `bayern` retain their flat `acts` arrays and additionally partition
  every act exactly once into `constitution`, `statutes`, or `ordinances`.
  Their `pipeline` remains bucketed by parliamentary status.
- `laender`: bills/activity per Land, keyed by `DE-<code>`.

---

## `watched_procedures.json`

Persistent presentation of the explicit watchlist. `procedures[]` merges the
latest official DIP or EUR-Lex observation with reviewed search aliases,
scope, relevant norms and `history[]`. `active` controls frequent polling;
terminal records remain visible as an immutable archive. An EU political
agreement is not terminal. OJ publication becomes `pending_final_review`;
polling stops only after a persisted review has compared the final Article 2
with the tracked Commission proposal and records a pass for that adopted CELEX.
If an official snapshot temporarily omits a watched id, the row becomes
`source_missing` and retains separate `last_observed_*` fields; stale status is
never presented as a fresh observation. History events carry stable `event_id`
values so a retry after a process crash cannot duplicate a transition.
The terminal review is persisted in the watch configuration as
`final_text_review: {status:"passed", article_2_compared:true,
reviewed_celexes:["…"], compared_to:"<proposal CELEX>"}`; all four conditions
must match the published act before the fetcher emits `terminal:true`.

```json
{
  "schema_version": 1,
  "checked_at": "2026-07-15T08:17:00+00:00",
  "active_count": 2,
  "terminal_count": 1,
  "archived_count": 0,
  "procedures": [
    { "id": "eu-2026-0186-nle", "source": "EUR-Lex",
      "status": "Ongoing", "stage": "Discussions within the Council",
      "active": true, "terminal": false, "history": [ … ] }
  ]
}
```

## `amendment_fates.json`

Reviewed document chains for proposals whose fate cannot be inferred from a
single Drucksache. `document_chain[]` states each document's procedural role;
`validation.checks[]` reports the separately declared checks performed against
the current consolidated corpus. The data never labels a committee
recommendation itself as the final law.

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
must not kill the arena rebuild); the arena and web-data builds are fatal, and
fetchers refuse to overwrite a good same-day snapshot with empty output.

```bash
./refresh.sh
```

The 21 steps are:

| # | Step | Fetcher |
|---|------|---------|
| 1 | DIP legislative pipeline (Bund, intraday) | `fetch_dip.py` |
| 2 | Explicit pending EUR-Lex procedure watches | `fetch_eu_watch.py` |
| 3 | Persistent watch state + change-only history (only if both official refreshes succeeded) | `tools/update_procedure_watch.py` |
| 4 | BGBl promulgation events | `fetch_bgbl_events.py` |
| 5 | GII corpus HEAD | `fetch_gii.py` |
| 6 | Federal case law from seven official RII feeds | `fetch_rii.py` |
| 7 | NeuRIS changelog (append-only) | `fetch_neuris_changelog.py` |
| 8 | buzer back-history (max once/day; skipped if today's snapshot exists) | `fetch_buzer.py` |
| 9 | PatchInstruction extraction (writes the DIP text cache) | `extract_patches.py` |
| 10 | Bundesrat texts (cache-first, 30 s crawl-delay) | `fetch_br_texts.py` |
| 11 | PatchInstruction **re-extraction** (only if new BR texts arrived) | `extract_patches.py` |
| 12 | BAYERN.RECHT corpus HEAD + BayRS chains | `fetch_bayern_recht.py` |
| 13 | GVBl/BayMBl promulgation events (RSS) | `fetch_gvbl_events.py` |
| 14 | Bayerischer Landtag pipeline | `fetch_bay_landtag.py` |
| 15 | Curated EU layer (CELLAR + DEU transpositions + OJ-L) | `fetch_eu_layer.py` |
| 16 | EU breadth index (all directives + basic regulations) | `fetch_eu_index.py` |
| 17 | Länder monitor (Parlamentsspiegel, Asyl/Sozial) | `fetch_parlamentsspiegel.py` |
| 18 | Länder-Gesetzentwürfe (all 16 Landtage) | `fetch_laender_bills.py` |
| 19 | Build the QFS arena | `tools/build_qfs.py` |
| 20 | Export web data | `tools/build_web_data.py` |
| 21 | LLM digest (skips without `OPENROUTER_API_KEY`) | `tools/build_digest.py` |

Snapshots land in `data/snapshots/<source>/<date>/*.jsonl`; each build reads the
newest snapshot per source. **Each fetcher documents its own source, cadence,
and quirks in its module docstring** — read the top of any
`pipeline/fetch_*.py` for the authoritative behavior of that step.

Step 19 also deploys the arena to a local `qfs_visualizer` checkout if present.

---

# C) REST API — `api/` (FastAPI)

A small, read-only HTTP wrapper around the section-A data plane. It does **not**
recompute anything: `tools/build_web_data.py` is the build step, `web/data/*.json`
*are* the data, and each endpoint just projects one of those files. Some return
the file verbatim; filtered endpoints add the small envelopes documented below.
This mirrors the sibling
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
frontend can call the API. Archive/download metadata (`Content-Disposition`
and the `X-Lexgraph-*` headers documented below) is exposed to browser
JavaScript as well. The data plane is loaded lazily and cached; there is no
database.

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
| `GET /acts/{id}/archive` | one act's `norms` + `versions` | selectable HEAD/event/effective dates and honest coverage gaps |
| `GET /acts/{id}/markdown?at=&norm=&download=` | one act's `norms` + `versions` | raw Markdown for the whole act or one norm; omit `at` for HEAD |
| `GET /decisions?q=&act=` | `decisions.json` | court decisions, newest first; `limit` 1–200 (default 50) |
| `GET /decisions/{id}` | one `decisions.json` row | full exported row; **404** if unknown |
| `GET /git?lane=&limit=` | `git.json` | optional `lane` 0–3; `limit` 1–1000 |
| `GET /graph` | `graph.json` | the QFS arena export |
| `GET /hierarchy` | `hierarchy.json` | competence-aware legal layers |
| `GET /eu-index?q=&kind=&limit=&offset=` | `eu_index.json` | filter and paginate the EU breadth index; **404** until built |
| `GET /procedures/watched` | `watched_procedures.json` | active and archived DIP/EUR-Lex watches with change history |
| `GET /amendment-fates?procedure_id=&validation_id=` | `amendment_fates.json` | reviewed document chains and current-law validation checks |
| `GET /search?q=&limit=&norm_limit=&procedure_limit=` | `search.sqlite` + `wiki.json` + `hierarchy.json` | ranked search over acts, current norms and official DIP/EUR-Lex procedures |
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

## `GET /eu-index?q=&kind=&limit=&offset=`

Filters the metadata-only EU breadth index. `q` is an optional
case-insensitive substring search over `celex` and `title`; `kind` is `DIR` or
`REG`. `DIR` includes `DIR`, `DIR_DEL`, and `DIR_IMPL`, while `REG` contains
basic regulations only. `limit` ∈ [1, 500] (default 100), and `offset` is a
non-negative row offset (default 0).

```bash
curl 'http://127.0.0.1:8010/eu-index?q=internationalen%20Schutz&kind=DIR&limit=20&offset=0'
```

```json
{
  "built_at": "2026-07-14",
  "total": 7934,
  "matched": 3,
  "offset": 0,
  "limit": 20,
  "instruments": [
    { "celex": "32024L1346", "type": "DIR", "date": "2024-05-14",
      "title": "Richtlinie (EU) 2024/1346 …" }
  ]
}
```

`total` is the unfiltered file total; `matched` is the count after `q` and
`kind`, before pagination. Returns **404** until `eu_index.json` has been built.

## `GET /procedures/watched` and `GET /amendment-fates`

`/procedures/watched` returns `watched_procedures.json` unchanged. This is the
stable endpoint for a persistent “tracked projects” panel; clients do not have
to discover watched rows by searching the full hierarchy.

EU rows can additionally contain `council_development`: a separately sourced
Council public-register record (`document`, `url`, document and meeting dates,
addressee, access status). Its date/stage can be newer than EUR-Lex while the
top-level `status` remains `Ongoing`. A register title saying `Political
agreement` is explicitly non-terminal; only the final-act/OJ review gate can
end polling.

`/amendment-fates` returns all validation records. `procedure_id` filters by
official DIP id and `validation_id` by the Lexgraph record id; filters can be
combined. Each record keeps reviewed document roles separate from mechanical
checks of the current consolidated law.

```bash
curl 'http://127.0.0.1:8010/procedures/watched'
curl 'http://127.0.0.1:8010/amendment-fates?procedure_id=322125'
```

## Dated act archive and Markdown

`GET /acts/{id}/archive` lists the deployment's exact consolidated `HEAD`,
known amendment event dates, distinct `effective_date` values, current and
historical norm designators, and corpus-level gaps. Publication/event date and
effective date are intentionally both retained: a Bavarian act can be
promulgated on one day and enter into force on another.

```bash
curl 'http://127.0.0.1:8010/acts/fed_aufenthg_2004/archive'
```

```json
{
  "act_id": "fed_aufenthg_2004",
  "head_date": "2026-07-15",
  "entries": [
    {"date":"2024-03-01","label":"…","has_changes":true,
     "exact":false,"partial":true},
    {"date":"2026-07-15","label":"HEAD · consolidated source snapshot",
     "has_changes":false,"exact":true,"partial":false}
  ],
  "norms": [{"enbez":"§ 24","title":"Aufenthaltsgewährung zum vorübergehenden Schutz"}],
  "gaps": [{"reason":"metadata_only_versions","label":"…"}],
  "complete": false
}
```

`GET /acts/{id}/markdown` returns raw `text/markdown`, not a JSON wrapper.
Both filters are optional:

- omit `at` for the exact current HEAD; `at=YYYY-MM-DD` requests the state on
  an earlier date;
- omit `norm` for the **entire act**; use `norm=§24`, `norm=24`, or
  `norm=Art.59` for one norm;
- `download=true` adds an attachment filename so browsers save a `.md` file.

```bash
# Complete current act as Markdown
curl 'http://127.0.0.1:8010/acts/fed_aufenthg_2004/markdown'

# One norm at a date (URL-encode § in production clients)
curl --get 'http://127.0.0.1:8010/acts/fed_aufenthg_2004/markdown' \
  --data-urlencode 'at=2024-03-02' --data-urlencode 'norm=§ 24'

# Download the complete historical act
curl -OJ 'http://127.0.0.1:8010/acts/fed_aufenthg_2004/markdown?at=2024-03-02&download=true'
```

The response exposes:

```text
Content-Type: text/markdown; charset=utf-8
X-Lexgraph-Requested-Date: 2024-03-02
X-Lexgraph-Resolved-Date: 2024-03-02
X-Lexgraph-Head-Date: 2026-07-15
X-Lexgraph-Exact: false
X-Lexgraph-Archive-Status: partial
X-Lexgraph-Missing-Transitions: 3
X-Lexgraph-Archive-Gaps: [{"reason":"…","label":"…"}]
Content-Disposition: attachment; filename="…md"   # download=true only
```

The truth boundary is strict: only HEAD is a complete consolidated source
snapshot. Historical output uses dated complete norm states from verified
Bavarian Wayback/daily snapshots where available, then conservatively reverses
only reconcilable recorded `old/new` bodies; it is labelled `partial`.
Metadata-only changes, the historic
1,200-character federal capture cap, empty change sides, ambiguous ordering,
and state mismatches are reported and never guessed. In particular, an empty
side can be a paragraph-level edit; the API never treats it as proof that a
whole §/Art. was created or repealed. Norm headings in reconstructed output
remain HEAD metadata and are disclosed as such.

## `GET /search?q=&limit=&norm_limit=&procedure_limit=`

Ranked SQLite FTS5 search across act id/abbreviation/title and every current
norm's §/Art. identifier, heading, and complete text. Matching is Unicode- and
case-insensitive; accents and German umlauts are folded consistently. A
versioned, data-driven synonym file (`data/search_synonyms.json`) supplies
multilingual/domain aliases, so e.g. `Ukraine`, `ukrainisch`, `Украина`, and
`Україна` find the same corpus area, including temporary-protection norms.
Explicit target priorities in that file put the controlling and benefit norms
(for example AufenthG § 24, its § 81 residence-title fiction, SGB II § 74 and
SGB XII § 146) before incidental text mentions. These are transparent
retrieval hints, not an embedding or an assertion that two legal terms are
equivalent. An explicit `§ 24` or `Art. 24` is constrained to the norm
identifier; a plain numeric word remains an ordinary full-text token.

The same query also searches current official Bundestag/DIP procedure
metadata: full title, descriptors, policy areas, initiator and DIP abstract.
Rows configured in `data/procedure_watchlist.json` additionally carry explicit
search aliases and watch metadata. This exposes the current official stage; it
does not present a bill as enacted law or infer that its text is in force.

`limit` caps act results, `norm_limit` caps norm results, and
`procedure_limit` caps DIP procedure results (defaults 25, 50 and 20).
For compatibility, `total` and `matches` retain the original act-only contract
and every `matches` row keeps the `/acts` index shape. `act_matches` adds
ranking metadata; `norm_matches` contains
`{act_id,jurabk,juris,act_title,enbez,norm_title,snippet,score,matched_fields,
source,url}`. Snippets are plain text, `source` is `gii` or `bayern_recht`, and
`url` is the API-relative act detail path. `procedure_matches` contains the
official DIP or EUR-Lex id, title, stage, dates, source-specific identifiers,
topics, initiators, descriptors, abstract/scope, source link and optional watch
metadata. `result_total` is
`act_total + norm_total + procedure_total` before the three result limits.

```bash
curl 'http://127.0.0.1:8010/search?q=Ukraine&norm_limit=10'
```

```json
{
  "query": "Ukraine",
  "total": 2,
  "matches": [
    {"id":"fed_ukraineaufenthfgv","jurabk":"UkraineAufenthFGV", …}
  ],
  "result_total": 25,
  "act_total": 2,
  "norm_total": 22,
  "procedure_total": 1,
  "act_matches": [
    {"id":"fed_ukraineaufenthfgv","score":156,
     "source":"gii","url":"/acts/fed_ukraineaufenthfgv", …}
  ],
  "norm_matches": [
    {"act_id":"fed_aufenthg_2004","jurabk":"AufenthG 2004",
     "enbez":"§ 24","norm_title":"Aufenthaltsgewährung zum vorübergehenden Schutz",
     "snippet":"Einem Ausländer kann zum vorübergehenden Schutz …",
     "source":"gii","url":"/acts/fed_aufenthg_2004", …}
  ],
  "procedure_matches": [
    {"id":"329468","gesta":"G013","status":"Überwiesen",
     "watched":true,"source":"DIP",
     "url":"https://dip.bundestag.de/vorgang/_/329468", …}
  ]
}
```

An older data deployment without `search.sqlite` degrades to the former act
title/abbreviation substring search rather than returning an error.

## `GET /decisions?q=&act=` and `GET /decisions/{id}`

Court decisions from `decisions.json` (full rows — see section A), newest
first. `q` matches case-insensitively in `az`, `court`, `court_short`,
`title`, all `summary` languages, and `effects[].jurabk`; `act` filters by
`effects[].act_id` (an act index id); `limit` ∈ [1, 200], default 50.
`/decisions/{id}` returns one full exported row and **404** for an unknown id.

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
`web/data/digest.json`, written by `tools/build_digest.py` (refresh step 21)
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
