# Lexgraph API & Data Interface

Lexgraph models German and EU law as a temporal legal graph: consolidated text,
dated amendments, official legislative stages, court decisions and explicit
links between them. `git.json` is the stable **Laws as Git** projection: a
navigation model for HEAD, commits, open/closed branches and evidence-bound
merges.
The metaphor never overrides the legal status stated by the official source.

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
| `gii_catalog.json` | Complete official GII TOC, metadata only; deep fields exist only for curated acts. |
| `official_federal_states.json` | Cumulative public GII observation manifest: canonical state hashes, retrieval provenance and counts. |
| `federal_states/manifest.json` + `federal_states/objects/sha256/…` | The same manifest beside its verified deterministic-gzip full-state CAS. |
| `official_transition_reviews.json` | Federal state changes whose legal date passed final BGBl command + DIP commencement review. |
| `search.sqlite` | Read-only FTS5 index over act metadata and complete current norm text. |
| `hierarchy.json` | Competence-aware legal layers (EU / Bund / Bayern / Länder). |
| `watched_procedures.json` | Persistent DIP/EUR-Lex watch state and change history. |
| `amendment_fates.json` | Reviewed document chains plus mechanical current-law checks. |
| `graph.json` | The QFS arena export (nodes / edges / beliefs / ticks / worlds). |
| `git.json` | Laws-as-Git event log: HEAD context, commits, open/closed branches and evidence-bound merges. |

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
  "official_federal_observations": 172,
  "official_federal_states": 63,
  "official_federal_transitions": 7,
  "official_federal_legal_reviews": 1,
  "bay_bills": 123,
  "bay_verkuendet": 60,
  "eu_instruments": 47,
  "eu_index_total": 7934,
  "gii_catalog_total": 6125,
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
| `official_federal_observations` | Complete GII act retrievals retained in the cumulative state manifest. |
| `official_federal_states` | Unique canonical full-act objects in the federal SHA-256 CAS. |
| `official_federal_transitions` | Normative changes found by Lexgraph between adjacent complete states; their retrieval dates are not legal dates. |
| `official_federal_legal_reviews` | State transitions that additionally passed the final BGBl command and exact DIP commencement gate. |
| `bay_bills` / `bay_verkuendet` | Bavarian Landtag bills / of those, promulgated. |
| `eu_instruments` / `transpositions` | Curated EU instruments / DEU transposition mentions in the deep layer. |
| `eu_index_total` | Metadata rows in the EU breadth index; `0` until it has been fetched. |
| `gii_catalog_total` | Official federal acts discoverable through the metadata-only GII catalogue; `0` for older snapshots. |
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

## `gii_catalog.json`

Metadata-only discovery layer for every item in the official
`gesetze-im-internet.de/gii-toc.xml`. It does **not** download or imply local
full text outside the curated corpus.

```json
{
  "schema_version": 1,
  "built_at": "2026-07-15",
  "total": 6125,
  "acts": [
    {"id":"gii:bgb","abbrev":"bgb","title":"Bürgerliches Gesetzbuch",
     "url":"https://www.gesetze-im-internet.de/bgb/",
     "act_id":"fed_bgb","jurabk":"BGB"},
    {"id":"gii:mietbg","abbrev":"mietbg","title":"Mietbeihilfengesetz",
     "url":"https://www.gesetze-im-internet.de/mietbg/"}
  ]
}
```

`id` and `abbrev` are derived from GII's stable path token. `abbrev` is not a
guessed printed abbreviation. Only a curated row has `act_id` (the local
`/acts/{id}` key) and the XML-derived official `jurabk`. All other rows link
straight to the official GII page.

---

## `feed.json`

Merged, deduplicated realtime stream across all sources, newest first, capped
at ~600. Array of:

```json
[
  {
    "time":  "2026-07-13",
    "juris": "DE",
    "source": "BGBl",
    "kind":  "verkündet",
    "title": "BGBl. 2026 I Nr. 200",
    "url":   "https://www.recht.bund.de/eli/bund/BGBl_1/2026/200",
    "badge": null
  }
]
```

| Field | Meaning |
|-------|---------|
| `time` | Event date (`YYYY-MM-DD`). |
| `juris` | Jurisdiction: `DE`, `DE-BY`, `EU`, `DE-<Land>`, … |
| `source` | Published source label (`BGBl`, `GVBl`, `OJ L`, `Landtag`, a court short name like `SG München`, …). Permission-gated research sources are excluded from public builds. |
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
  "upcoming": [],
  "versions": [],
  "temporal": {
    "last_change": null,
    "first_change": null,
    "change_count": 0,
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
scope, relevant norms, `history[]` and deterministic `analysis`. `active`
controls frequent polling;
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

Every active DIP watch additionally fetches its complete official
`vorgangsposition` chain. Configured content assertions are rerun against DIP's
official Drucksache plaintext on every refresh; only compact match evidence is
persisted. This lets the Ukraine cutoff check distinguish the shorthand DIP
abstract about entry from the operative 21/3539 rules for the first § 24
permit or corresponding Fiktionsbescheinigung. Official Bundestag evidence
pages such as a committee hearing are revalidated in the same step.

`analysis` has a stable schema and no LLM or network dependency during build:

```json
{
  "as_of": "2026-07-15T08:17:00+00:00",
  "method": "deterministic_official_evidence",
  "forecast": {
    "outcome": "progress_toward_committee_recommendation_likely",
    "likelihood": {"band": "moderate", "minimum": null, "maximum": null},
    "confidence": "medium_low",
    "not_a_fact": true
  },
  "facts": [], "inferences": [], "factors": [],
  "next_milestone": {}, "checks": [], "chronology": [], "warnings": []
}
```

Facts, deterministic inferences and forecasts remain separate. Checks use
`passed`, `failed`, `pending` or `not_applicable` for source availability,
document roles, procedural transitions, final text and current law. A
committee recommendation is never treated as law; Council preparation or a
political agreement is never treated as adoption. Likelihood is deliberately
qualitative because the rules are not statistically calibrated.

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

## `git.json` — Laws as Git

The stable Laws-as-Git event log, laned by jurisdiction and newest first.
`commit`, `open`, `closed` and `merge` are deliberate navigation concepts. Consumers
must still use `refs`, status and source evidence to determine legal effect;
an open branch is not geltendes Recht and a merge is not blanket supervision.

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
| `total` | Event count. |
| `commits[].hash` | Stable 8-hex CRC32 commit/event identifier. |
| `commits[].date` | Event date. |
| `commits[].lane` | **Integer** index into `lanes` (`0`=EU, `1`=Bund, `2`=Bayern, `3`=Länder). |
| `commits[].type` | Git-state enum: `commit` = documented promulgated event, `open` = pending proposal, `closed` = rejected/not-merged branch, `merge` = a legally specific EU implementation/applicability link. |
| `commits[].actor` | `Bundestag`, `Landtag Bayern`, `EU`, `Landtag <Land>`. |
| `commits[].msg` | Title (≤120 chars). |
| `commits[].acts` | Affected acts (≤6). |
| `commits[].paras` | Affected §§ (≤8). |
| `commits[].refs` | Status / stand tags. |
| `commits[].merge_ref` | For `type=merge`: the related CELEX id (e.g. `32023L2225`), else `null`; this does not imply blanket EU supervision. |
| `commits[].doc` | Source document (`bt-ds:…`, Drs.-Nr., …). |
| `commits[].targets_verified` | For DIP-derived rows, `false`: displayed acts/paragraphs came from draft patch instructions and are not claimed as final-text attribution. |

Per-lane extras: EU commits add `celex`; Bavarian add `url` and `gvbl`;
Länder add `url`.

---

# B) Pipeline — how the data is produced

[`refresh.sh`](../refresh.sh) is the cron entrypoint. It pulls the live
legislative state across Bund / Bayern / EU, rebuilds the QFS arena,
then exports the web JSON. **Fetch steps degrade gracefully** (a flaky source
must not kill the arena rebuild); the arena and web-data builds are fatal, and
fetchers refuse to overwrite a good same-day snapshot with empty output.

```bash
./refresh.sh
```

The 23 steps are:

| # | Step | Fetcher |
|---|------|---------|
| 1 | DIP pipeline plus watched official position/document checks (Bund, intraday) | `fetch_dip.py` |
| 2 | Explicit pending EUR-Lex procedure watches | `fetch_eu_watch.py` |
| 3 | Persistent watch state + change-only history (fresh observations or an explicitly marked persisted fallback) | `tools/update_procedure_watch.py` |
| 4 | BGBl promulgation events | `fetch_bgbl_events.py` |
| 5 | GII corpus HEAD | `fetch_gii.py` |
| 6 | Verify and append every complete GII act state to the cumulative canonical SHA-256 store | `tools/archive_gii_states.py` |
| 7 | Capture final BGBl landing/PDF documents, verify integrity, split articles and join DIP commencement rows | `fetch_bgbl_documents.py` |
| 8 | Federal case law from seven official RII feeds (**default-off** pending NeuRIS migration; `LEXGRAPH_ENABLE_RII=1`) | `fetch_rii.py` |
| 9 | NeuRIS changelog plus immediate content-addressed capture of selected expiring ZIP artifacts | `fetch_neuris_changelog.py` |
| 10 | Private version-history QA (**default-off**, explicit opt-in `LEXGRAPH_ENABLE_BUZER=1`; not used by public builds) | `fetch_buzer.py` |
| 11 | PatchInstruction extraction (writes the DIP text cache) | `extract_patches.py` |
| 12 | Bundesrat texts (cache-first, 30 s crawl-delay) | `fetch_br_texts.py` |
| 13 | PatchInstruction **re-extraction** (only if new BR texts arrived) | `extract_patches.py` |
| 14 | BAYERN.RECHT corpus HEAD + BayRS chains | `fetch_bayern_recht.py` |
| 15 | GVBl/BayMBl promulgation events (RSS) | `fetch_gvbl_events.py` |
| 16 | Bayerischer Landtag pipeline | `fetch_bay_landtag.py` |
| 17 | Curated EU layer (CELLAR + DEU transpositions + OJ-L) | `fetch_eu_layer.py` |
| 18 | EU breadth index (all directives + basic regulations) | `fetch_eu_index.py` |
| 19 | Länder discovery monitor (**default-off**, permission gate) | `fetch_parlamentsspiegel.py` |
| 20 | Broad Länder discovery (**default-off**, separate bulk permission gate) | `fetch_laender_bills.py` |
| 21 | Build the QFS arena | `tools/build_qfs.py` |
| 22 | Verify official state/review inputs and export web data | `tools/build_web_data.py` |
| 23 | LLM digest (skips without `OPENROUTER_API_KEY`) | `tools/build_digest.py` |

Snapshots land in `data/snapshots/<source>/<date>/*.jsonl`; each build reads the
newest snapshot per source. **Each fetcher documents its own source, cadence,
and quirks in its module docstring** — read the top of any
`pipeline/fetch_*.py` for the authoritative behavior of that step.
Permission-gated snapshots are retained only as private research artifacts;
the public web/HF builders exclude them unless an explicit private build mode
is selected, and the HF exporter refuses that mode.

Step 21 also deploys the arena to a local `qfs_visualizer` checkout if present.

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
| `GET /data-policy` | `data_policy.json` | public-build mode and excluded source families |
| `GET /federal-history?act=&tier=&limit=&offset=` | `verified_federal_events.json` | official-only state pairs and current-text-verified DIP patches |
| `GET /official-states?act=&limit=&offset=` | `official_federal_states.json` | exact GII retrieval observations and immutable state hashes |
| `GET /official-transition-reviews?act=&procedure_id=&limit=&offset=` | `official_transition_reviews.json` | legal dates accepted by the final BGBl + DIP + complete-state gate |
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
| `GET /search?q=&limit=&norm_limit=&procedure_limit=&catalog_limit=` | deep index + procedures + `gii_catalog.json` | ranked deep results followed by complete official federal-law discovery |
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
{ "dataset": "Lexgraph", "version": "1.2",
  "built_at": "2026-07-06T17:43:26+00:00",
  "source": "https://github.com/SNTIQ-Team/lexgraph" }
```

## `GET /feed?limit=`

Newest-first slice of `feed.json`. `limit` ∈ [1, 600], default 100.

```bash
curl 'http://127.0.0.1:8010/feed?limit=2'
```

```json
{ "total": 600, "limit": 2, "events": [ { "time": "2026-07-13", "juris": "DE",
  "source": "BGBl", "kind": "verkündet", "title": "BGBl. 2026 I Nr. 200", "url": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/200",
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
  "official_states": [
    {"observed_at":"2026-07-15","state_sha256":"…",
     "date_basis":"retrieval_observation_not_effective_date",
     "verification":"exact","source_url":"https://www.gesetze-im-internet.de/…/"}
  ],
  "temporal": { … }, "norms": [ … ] }
```

## `GET /git?lane=&limit=`

Stable Laws-as-Git endpoint backed by `git.json`. Optional `lane` filters by the integer lane index
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

## `GET /federal-history?act=&tier=&limit=&offset=`

Returns the public federal-history ledger without reading the quarantined
Buzer cache. `act` is an exact case-insensitive JurAbk filter; `tier` is
`exact`, `current_text_correspondence`, or `metadata_only`. `exact` means
Lexgraph captured two different complete official GII states and hashed both
sides. Its `observed_at` is a retrieval date, **not** a silently inferred legal
effective date.

```bash
curl 'http://127.0.0.1:8010/federal-history?act=AsylbLG&tier=current_text_correspondence'
```

Each event carries official `evidence[]`, derivation metadata and a
verification tier. An exact event has non-null `published_at` / `effective_at`
only when its complete state pair also passed
`official_final_text_and_complete_state_pair`: every changed norm matched one
integrity-checked final BGBl command and DIP supplied an unambiguous
commencement date for that exact amending article. The event keeps
`observed_at` separately, because consolidation may be observed later.

A `current_text_correspondence` row proves only a sufficiently distinctive
correspondence with the current official norm; it leaves `effective_at` and
`published_at` empty. A procedure-state date is exposed as
`procedure_status_at`; a bill-level commencement date may be retained as
`draft_bill_declared_effective_at`, but neither is asserted as a promulgation
or individual patch effective date. The
corresponding act detail embeds its rows in
`verified_history[]`; `cross_checks[]` may additionally contain a secondary,
non-authoritative Buzer deep link.

### Official federal state store

`official_federal_states.json` and `federal_states/manifest.json` describe the
same cumulative store. State identity is the SHA-256 of canonical,
**uncompressed** UTF-8 JSON; the referenced object is a deterministic gzip at
`federal_states/objects/sha256/{first-two}/{digest}.json.gz`. Every object is a
complete act projection (`id`, `jurabk`, title/stand/build and all `norms`).
Every `observations[]` row records GII `source_url`, `builddate`, `observed_at`,
`state_sha256`, `date_basis` and verification. Repeated observations of an
unchanged state remain in the manifest while the CAS stores its bytes once.

`official_transition_reviews.json` is deliberately separate. Its `reviews[]`
rows join a complete old/new state pair to a final BGBl PDF hash, amending
article, DIP procedure and exact commencement clause. It contains no private
Buzer snapshot or synopsis. The Hugging Face export makes the same boundary
portable as four JSONL configurations:

- `official_federal_state_observations.jsonl` — retrieval facts;
- `official_federal_state_transitions.jsonl` — Lexgraph's own state diffs,
  `effective_at:null` unless separately reviewed;
- `official_federal_state_objects.jsonl` — full verified states and their
  observation provenance;
- `official_transition_reviews.jsonl` — the stricter legal-event gate.

The same evidence boundary is queryable without downloading the top-level
files:

```bash
# Exact retrieval observations for one act (id or exact JurAbk)
curl --get 'http://127.0.0.1:8010/official-states' \
  --data-urlencode 'act=AsylVfG 1992'

# Only transitions whose legal date passed the final-source gate
curl 'http://127.0.0.1:8010/official-transition-reviews?act=fed_asylvfg_1992'
```

`/official-states` returns metadata and SHA-256 pointers; retrieve the verified
full state or one norm through `/acts/{id}/markdown?at=...`. It never substitutes
the nearest observation. `/official-transition-reviews` may legitimately be
small: a row exists only when every strict acceptance check passes.

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

If EUR-Lex transiently serves a placeholder or an unparsable page, the fetcher
reuses only the last persisted official observation, marks it
`source_stale:true` / `retrieval_status:"stale_fallback"`, and lowers analysis
confidence. It does not fabricate a status transition and does not abort the
DIP/build/publish cycle. A first observation with no persisted fallback still
fails closed.

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
exact archived official GII observation dates, known amendment event dates,
distinct `effective_date` values, current and historical norm designators, and
corpus-level gaps. Observation, publication/event and effective dates are
intentionally all retained: GII may expose a consolidated change days after it
legally entered into force, and a Bavarian act can be promulgated on one day
and enter into force on another.

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
    {"date":"2026-07-13","observed_at":"2026-07-13",
     "date_basis":"retrieval_observation_not_effective_date",
     "state_digest":"…","source_url":"https://www.gesetze-im-internet.de/…/",
     "exact":true,"partial":false},
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
  an earlier date. If that date is an official GII observation, its verified
  content-addressed full state is rendered directly;
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
X-Lexgraph-Date-Basis: retrieval_observation_not_effective_date
X-Lexgraph-State-SHA256: 012345…cdef
X-Lexgraph-Source-URL: https://www.gesetze-im-internet.de/…/
X-Lexgraph-Legal-Effect-Verified: true  # only for a reviewed legal transition
X-Lexgraph-Published-Date: 2026-07-09
X-Lexgraph-Effective-Date: 2026-07-10
X-Lexgraph-Review-ID: fed-review:…
X-Lexgraph-Procedure-ID: 327966
Content-Disposition: attachment; filename="…md"   # download=true only
```

The truth boundary is strict: HEAD and a date backed by a verified complete GII
CAS object are exact source snapshots. An exact observation says what GII
served on that day; the header and Markdown front matter explicitly state that
the date is a retrieval observation, not commencement. Other historical output
uses dated complete norm states from verified Bavarian Wayback/daily snapshots
where available, then conservatively reverses only reconcilable recorded
`old/new` bodies; it is labelled `partial`.
Metadata-only changes, the historic
1,200-character federal capture cap, empty change sides, ambiguous ordering,
and state mismatches are reported and never guessed. In particular, an empty
side can be a paragraph-level edit; the API never treats it as proof that a
whole §/Art. was created or repealed. Norm headings in reconstructed output
remain HEAD metadata and are disclosed as such.

## `GET /search?q=&limit=&norm_limit=&procedure_limit=&catalog_limit=`

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

`limit` caps act results, `norm_limit` caps norm results,
`procedure_limit` caps DIP/EUR-Lex procedure results, and `catalog_limit` caps
metadata-only GII discovery results (defaults 25, 50, 20 and 25).
For compatibility, `total` and `matches` retain the original act-only contract
and every `matches` row keeps the `/acts` index shape. `act_matches` adds
ranking metadata; `norm_matches` contains
`{act_id,jurabk,juris,act_title,enbez,norm_title,snippet,score,matched_fields,
source,url}`. Snippets are plain text, `source` is `gii` or `bayern_recht`, and
`url` is the API-relative act detail path. `procedure_matches` contains the
official DIP or EUR-Lex id, title, stage, dates, source-specific identifiers,
topics, initiators, descriptors, abstract/scope, source link and optional watch
metadata. `catalog_matches` is ranked deterministically after the deep corpus:
exact/prefix abbreviation matches precede title phrase and token-prefix
matches. A catalogue row without `act_id` has not been downloaded or indexed
locally; use its official `url`. Rows already present in the curated deep
corpus are excluded from this last section, including deep hits beyond the
current page limit. `catalog_total` is the unpaginated remaining match count.
`result_total` is
`act_total + norm_total + procedure_total + catalog_total` before limits.

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
  ],
  "catalog_total": 2,
  "catalog_matches": [
    {"id":"gii:sozsichabkg_ukr","abbrev":"sozsichabkg_ukr",
     "title":"Gesetz zu dem Abkommen … Deutschland und der Ukraine über Soziale Sicherheit",
     "url":"https://www.gesetze-im-internet.de/sozsichabkg_ukr/",
     "score":760,"matched_fields":["title"],"source":"gii_catalog"}
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

## `tools/lex_log.py` — chronology for one act

```bash
python3 tools/lex_log.py AsylbLG          # federal corpus
python3 tools/lex_log.py AufnG            # Bavarian corpus
python3 tools/lex_log.py "SGB 2" --all    # full back-history
```

Prints, in one view:

- **Current snapshot** — GII / BAYERN.RECHT build date, norm count (and BayRS number for
  Bavaria).
- **Pipeline** — pending Bundestag patches / Landtag bills, on the status
  ladder, explicitly marked **NOT geltendes Recht**.
- **Promulgated, enters force soon** — official pipeline data when available.
- **Back-history** — official Bavarian ffn *Fortführungsnachweis* + XML
  `aenderungsverlauf`; private quarantined research caches may add federal
  hints only after explicit source permission/risk review. Last 10 by default;
  `--all` for the full list.

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

- **`blame <ACT> <REF>`** — merges the official Bavarian BayRS ffn + XML tier
  (`amtlich`) with extracted Bundestag patch commands (`pipeline`). A private,
  permission-gated research cache can additionally supply non-authoritative
  federal affected-§ hints; it is not part of the public dataset. Pending patch
  commands are flagged **NOT geltendes Recht**. `REF` accepts `3a`,
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
