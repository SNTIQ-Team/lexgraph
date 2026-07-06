# Data sources (live-verified 2026-07-06, extended same day: Bavaria/EU/Länder/Bundesrat)

Every endpoint below was probed live by one agent and adversarially
re-tested by a second. Full audit with sample snippets:
[source-audit.json](source-audit.json).

## Verdict map

| Source | Status | Role | History | Cadence |
| --- | --- | --- | --- | --- |
| **DIP** (search.dip.bundestag.de/api/v1) | live | **primary — anticipation** (process graph) | Vorgänge back to WP8 (1976), incl. failed bills | intraday; delta sync via `f.aktualisiert.start` |
| **recht.bund.de** (BGBl) | live | **primary — genesis/promulgation** | append-only archive since 2023-01-01 (2,760 issues), stable ELI | several issues/week |
| **EUR-Lex / CELLAR** | live | **primary — EU layer** | consolidated versions are first-class dated works (CELEX sector 0) | working-daily |
| **OLDP** (openlegaldata.io) | live | **primary — case graph** | 423,944 dated decisions, 18.6M case→§ citation edges | ~1 week ingest lag |
| **NeuRIS** (testphase.rechtsinformationen.bund.de) | live | **secondary today, primary-designate** | LegalDocML with native temporal ELIs (`{pointInTime}/{version}`), changelog endpoint, forward-accruing | continuous (Testphase) |
| **buzer.de** | live | **secondary — back-history** | per-§ version chains since **2006**, word-level diffs, amendment→BGBl-article provenance | daily ("tagaktuell") |
| **GII** (gesetze-im-internet.de) | live | seed_only — current HEAD | none (current Fassung only) | continuous consolidation, days–weeks lag |
| **gesetze-bayern.de** (BAYERN.RECHT) | live | **primary — Bavaria HEAD + back-history** (upgraded 2026-07-06) | ffn register carries per-act Fortführungsnachweis amendment chains; `/Content/Zip/<key>` = structured XML (satz.nr, typed verweis incl. EU) | few working days after GVBl |
| **verkuendung-bayern.de** (GVBl/BayMBl) | live | **primary — Bavaria promulgation** | GVBl issues 1945+, permalinks `/gvbl/{Y}-{page}/` + sha256; BayMBl electronic = amtlich, GVBl electronic = nachrichtlich | RSS 50-item feeds, CSV Jahreslisten |
| **bayern.landtag.de** | live | **primary — Bavaria anticipation** (WP19 pipeline) | full Gesetzentwurf lifecycle incl. GVBl citation; ElanTextAblage PDFs; facet search + RSS (GESETZ/BESCHL) | RSS is the freshness channel (search sort lags) |
| **parlamentsspiegel.de** | live | secondary — all-16-Länder discovery | Vorgänge back to ~1970, daily currency, origin-server PDF links | HTML-only (no API/RSS); JSESSIONID + GET permalinks |
| **bundesrat.de** | live | **primary — BR texts + events** (closes DIP cover-letter gap) | predictable Drucksachen PDF URLs incl. `(B)` Beschluss variants; text-based PDFs | 6 RSS feeds; robots Crawl-delay **30s** |
| **data.europarl.europa.eu** (EP API v2) | live | secondary — EU pipeline detail | JSON-LD procedures/adopted texts with ELI + FRBR; OEIL reference joins (`2022/0066(COD)` → `2022-0066`) | no auth, CC BY 4.0; unfiltered lists time out |
| **BundesGit** (github.com/bundestag/gesetze) | **stale** | seed_only — two checkpoints | 2013 + 2022 snapshots + one dense window (Aug 2012–Jan 2013) | dead since 2022-03 |

## Operational keys & gotchas (verified)

- **DIP**: mandatory `apikey` query param; the officially published public
  key (from `/api/v1/openapi.yaml`) currently is
  `R2BZaee.DjdCyihKZMf8AOjtScubP2EVydegzjmBIQ`. Cursor pagination
  (100/page). `beratungsstand` (e.g. `Verkündet`), `verkuendung[]` with
  BGBl citation + recht.bund.de ELI pdf_url, `inkrafttreten[]` per
  Artikel. Rotating key → re-read openapi.yaml on 401.
- **GII**: TOC `gii-toc.xml` (6,124 laws → xml.zip each); per-file
  `builddate` + BJNE doknr counters = cheap diff detection; update feed is
  `aktuDienst-rss-feed.xml` (daily; announces **BGBl issues with ELI
  links**, not consolidation events). Latin-1 HTML. Public domain
  (§ 5 UrhG).
- **recht.bund.de**: ELI scheme `/eli/bund/BGBl_1/{year}/{nr}`;
  append-only; officially sanctioned programmatic access.
- **EUR-Lex**: use the **CELLAR machine channel only** — the website (and
  even its robots.txt) sits behind a WAF JS-challenge. Formex XML carries
  `ARTICLE/PARAG IDENTIFIER`s; consolidated versions per date
  (`02016R0399-20240710` style). Reuse per Decision 2011/833/EU.
- **NeuRIS**: open API, no key; article-level eIds; temporal query params
  (`temporalCoverageFrom/To`); changelog endpoint returned 2,448 changes
  for an arbitrary window. Testphase — treat as rising primary, keep GII
  as HEAD source until coverage is proven.
- **buzer.de**: no ToS prohibiting scraping (Impressum + usage notes read
  in full); robots.txt permits `/gesetz/` + version pages (only `/s2.htm`
  search is disallowed — never fetched). Polite crawl, self-identifying,
  used as **non-authoritative** back-history hints tiered below official
  sources. The per-§ old/new **text** we extract from synopse pages is
  itself statutory text — **amtliches Werk, § 5 UrhG, no copyright**, also
  free at gesetze-im-internet.de / BGBl; buzer is credited as the
  retrieval channel only. We do **not** reproduce buzer's synopse
  arrangement or diff markup — diffs are recomputed locally.
- **OLDP**: keyless REST + bulk dumps; laws under ODbL, citation graph
  CC-BY; alive again in 2026 after a dump hiatus.
- **gesetze-bayern.de**: robots fully permissive; doc keys ≠ official
  abbreviations (AufnG → `BayAsylAufnG`) — resolve via the `ffn` register
  (1 page, all 872 acts + amendment chains). XML only inside
  `/Content/Zip/<key>`; no sitemap; `/Search` needs session+token — avoid.
- **verkuendung-bayern.de**: no ELI; issue PDFs
  `/files/gvbl/{Y}/{NN}/gvbl-{Y}-{NN}.pdf` (NN zero-padded, BayMBl Nr
  unpadded); CSV export needs `export-as=csv` in POST body — check
  Content-Type, silent HTML fallback otherwise.
- **bayern.landtag.de**: robots bans `/service/suche`, `/*?eID=*`,
  webangebot2-Vorgangsmappe; RSS `titel` param mandatory (any value);
  ElanTextAblage first bucket dir is `0000000001`, not `0000000000`.
- **bundesrat.de**: GET only (HEAD → 303 WAF); require
  `Content-Type: application/pdf` (same URL sans `?__blob` = HTML detail
  page); Crawl-delay 30s is binding — batch harvests take hours.
- **EU/EP**: EUR-Lex HTML = WAF 202 challenge, use CELLAR SPARQL + RSS
  (`display-feed.rss?rssId=222` OJ-L acts, `rssId=162` all legislation);
  MNE property is `cdm:measure_national_implementing_*` — the
  `implements` property returns empty silently. EP API: always filter.

## Layered strategy

```text
HEAD (current law)     GII (federal) + gesetze-bayern XML (Land) + CELLAR (EU)
Forward history        daily GII snapshots (builddate diff) + NeuRIS ELIs
                       + gesetze-bayern builddate
Back history           buzer per-§ chains (2006+) + BundesGit checkpoints
                       (2013, 2022) + BGBl ELI archive (2023+)
                       + Bavaria ffn Fortführungsnachweis chains
Genesis / publication  recht.bund.de ELI (Bund), GVBl/BayMBl RSS (Bayern),
                       OJ-L RSS + CELLAR (EU)
Genesis EU→DE          CELLAR MNE (measure_national_implementing) +
                       "Umsetzung der RL"-titles in DIP
Anticipation           DIP Vorgänge (intraday, Bund) + Bundesrat RSS/PDFs
                       + bayern.landtag WP19 lifecycle (Land)
                       + Parlamentsspiegel (16 Länder monitoring)
Case backlinks         OLDP citation graph
```
