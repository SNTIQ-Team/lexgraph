# Data sources (audit baseline 2026-07-06; official state archive 2026-07-15)

The 2026-07-06 baseline was probed live and adversarially re-tested. RII and
the CELLAR breadth index were added from their current fetcher contracts on
2026-07-14. The GII CAS, final-command capture and NeuRIS artifact retention
were verified on 2026-07-15. Baseline audit snippets:
[source-audit.json](source-audit.json).

## Verdict map

| Source | Status | Role | History | Cadence |
| --- | --- | --- | --- | --- |
| **DIP** (search.dip.bundestag.de/api/v1) | live | **primary — anticipation** (process graph) | Vorgänge back to WP8 (1976), incl. failed bills | intraday; delta sync via `f.aktualisiert.start` |
| **recht.bund.de** (BGBl) | live | **primary — genesis/promulgation and final command** | append-only archive since 2023-01-01 (2,760 issues), stable ELI; captured landing metadata, PDF hashes/text and article splits | several issues/week |
| **EUR-Lex / CELLAR** | live | **primary — EU layer** | consolidated versions are first-class dated works (CELEX sector 0) | working-daily |
| **Rechtsprechung im Internet (RII)** (BMJV/BfJ) | default-off / migration pending | **official — corpus-relevant federal decisions already retrieved** | seven rolling RSS feeds + official ZIP/XML records; retained snapshot is cumulative | no scheduled intake; moving to NeuRIS API |
| **OLDP** (openlegaldata.io) | live | research — bulk case graph (not in the current export) | 423,944 dated decisions, 18.6M case→§ citation edges | ~1 week ingest lag |
| **NeuRIS** (testphase.rechtsinformationen.bund.de) | live | **secondary today, primary-designate** | changelog metadata plus immediate content-addressed capture of advertised ZIP artifacts before temporary URLs disappear | continuous (Testphase) |
| **buzer.de** | private candidates + public deep links | discovery/manual QA and one-click cross-check only | private version/synopsis database since 2006 is not republished | no scheduled crawl |
| **GII** (gesetze-im-internet.de) | live | **primary — current HEAD and observed complete states** | source itself exposes current Fassung only; Lexgraph retains each complete retrieval in its own immutable SHA-256 state store | continuous consolidation, days–weeks lag; daily capture |
| **gesetze-bayern.de** (BAYERN.RECHT) | live | **primary — Bavaria HEAD + back-history** (upgraded 2026-07-06) | ffn register carries per-act Fortführungsnachweis amendment chains; `/Content/Zip/<key>` = structured XML (satz.nr, typed verweis incl. EU) | few working days after GVBl |
| **Internet Archive Wayback** (archived BAYERN.RECHT pages) | live/cache-first | secondary retrieval channel — sparse Bavarian old/new text | archived official per-norm pages from 2016+; only one-to-one state/event matches are exported | one-time backfill + cached reruns |
| **verkuendung-bayern.de** (GVBl/BayMBl) | live | **primary — Bavaria promulgation** | GVBl issues 1945+, permalinks `/gvbl/{Y}-{page}/` + sha256; BayMBl electronic = amtlich, GVBl electronic = nachrichtlich | RSS 50-item feeds, CSV Jahreslisten |
| **bayern.landtag.de** | live | **primary — Bavaria anticipation** (WP19 pipeline) | full Gesetzentwurf lifecycle incl. GVBl citation; ElanTextAblage PDFs; facet search + RSS (GESETZ/BESCHL) | RSS is the freshness channel (search sort lags) |
| **parlamentsspiegel.de** | quarantined / permission pending | internal Länder discovery only | origin-Landtag links; portal metadata is not republished | no scheduled broad crawl |
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
- **GII**: TOC `gii-toc.xml` (6,125 current entries → xml.zip each); per-file
  `builddate` + BJNE doknr counters = cheap diff detection; update feed is
  `aktuDienst-rss-feed.xml` (daily; announces **BGBl issues with ELI
  links**, not consolidation events). Latin-1 HTML. Public domain
  (§ 5 UrhG). After every successful complete corpus fetch,
  `tools/archive_gii_states.py` projects each act into canonical JSON, hashes
  the **uncompressed** bytes, stores a deterministic gzip CAS object and
  appends a retrieval observation. `observed_at` is never treated as an
  effective date.
- **recht.bund.de**: ELI scheme `/eli/bund/BGBl_1/{year}/{nr}`;
  append-only; officially sanctioned programmatic access.
  `fetch_bgbl_documents.py` captures the canonical `VO.html` landing page and
  `regelungstext.pdf`, checks the advertised MD5 before accepting it, records
  its own SHA-256, extracts text/articles and joins referenced corpus acts and
  DIP commencement rows. A GII state transition receives a legal effective
  date only when every changed norm passes this final-command gate.
- **Official retrospective inventory (2023+)**:
  `pipeline/backfill_bgbl_history.py` starts from promulgated DIP procedures,
  joins only exact `Rechtsmaterialien` names from the curated GII corpus, and
  then requires that the bounded exact act name occur in the target/preamble
  of a final, checksum-verified BGBl amendment article. Names found only in
  replacement text are rejected. DIP dates are assigned only when the whole
  article has one unambiguous commencement clause; sub-article splits remain
  unresolved. The event inventory is not treated as a historical consolidated
  text.
- **EUR-Lex**: use the **CELLAR machine channel only** — the website (and
  even its robots.txt) sits behind a WAF JS-challenge. Formex XML carries
  `ARTICLE/PARAG IDENTIFIER`s; consolidated versions per date
  (`02016R0399-20240710` style). Reuse per Decision 2011/833/EU. The breadth
  index queries in-force `DIR`, `DIR_DEL`, `DIR_IMPL`, and basic `REG` works;
  it deliberately excludes `REG_DEL`/`REG_IMPL`, stores title/date/type/CELEX
  metadata only, and links out to EUR-Lex for text. A CELEX-shape gate removes
  sector X/Y addenda and `...R(…)` corrigenda that CELLAR occasionally labels
  as `REG`; parenthesized base identifiers for older EEC/Euratom acts remain.
- **RII**: the seven feeds are BVerfG, BGH, BVerwG, BFH, BAG, BSG, and BPatG
  (`bsjrs-{court}.xml`); each GUID resolves to an official ZIP/XML record.
  RSS is a rolling current window, not a historical archive, so each successful
  run merges matches into the previous snapshot by decision id. The fetch is
  all-or-safe: every selected feed and document must validate before atomic
  replacement, and ZIPs are cached for seven days. Only records whose official
  `<norm>` metadata names an act in the current GII corpus are retained. Those
  links are exported neutrally as `cited`; the metadata alone does not support
  a stronger claim that the court interpreted or applied the norm. This is not
  all German case law; lower-court, Land-court, and EU decisions remain in the
  reviewed manual layer. RII permits reuse of the offered decision formats,
  but its `robots.txt`/`tdm-reservation` signals conflict with automated ZIP
  polling. New intake is therefore default-off while it is migrated to the
  documented NeuRIS changelog/bulk API; the existing official decisions may
  remain published with provenance.
- **NeuRIS**: open API, no key; article-level eIds; temporal query params
  (`temporalCoverageFrom/To`); changelog endpoint returned 2,448 changes
  for an arbitrary window. Changelog artifact URLs are locators, not a durable
  archive: advertised ZIPs may disappear. The fetcher therefore downloads and
  hashes each selected artifact during the changelog pass and retains the
  original URL plus capture status. Testphase — treat as rising primary, keep
  GII as HEAD source until coverage is proven.
- **buzer.de**: permissive robots and the absence of a scraping clause are not
  a reuse licence. Although individual statutory passages are official works,
  the private consolidation, version segmentation and synopsis alignment may
  be protected as a database under §§ 87a ff. UrhG. Both fetchers are
  default-off and existing snapshots are quarantined. Public web/HF builds do
  not load them. A small curated mapping supplies only direct per-act history
  links. Lexgraph does not request permission to reproduce Buzer's database;
  it independently recreates the useful workflow from official sources.
  Published history rows come from the official-only verification pipeline in
  `tools/federal_history.py`.
- **OLDP**: keyless REST + bulk dumps; laws under ODbL, citation graph
  CC-BY; alive again in 2026 after a dump hiatus.
- **gesetze-bayern.de**: robots fully permissive; doc keys ≠ official
  abbreviations (AufnG → `BayAsylAufnG`) — resolve via the `ffn` register
  (1 page, all 872 acts + amendment chains). XML only inside
  `/Content/Zip/<key>`; no sitemap; `/Search` needs session+token — avoid.
- **Wayback / Bavarian text history**: archived pages are ordered by their
  declared `Text gilt ab` date (capture time is only a fallback). A text-state
  transition is attached to at most one compatible official `ffn` event;
  ambiguous or single-state intervals are omitted, and empty sides require an
  explicit introduction or repeal. This is deliberately sparse evidence, not
  a reconstructed full consolidation history. Daily local snapshots provide
  complete forward diffs from July 2026.
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
Forward observations   complete daily GII states in Lexgraph's SHA-256 CAS
                       + captured NeuRIS artifacts + gesetze-bayern builddate
Verified federal rows  own diffs between adjacent complete GII states;
                       a legal effective date only after final BGBl command
                       + exact DIP commencement review
Back history           captured NeuRIS artifacts where still available
                       + BundesGit checkpoints (2013, 2022)
                       + BGBl ELI archive (2023+)
                       + Bavaria ffn Fortführungsnachweis chains
                       + sparse archived BAYERN.RECHT state transitions (2016+)
Genesis / publication  recht.bund.de ELI (Bund), GVBl/BayMBl RSS (Bayern),
                       OJ-L RSS + CELLAR (EU)
Genesis EU→DE          CELLAR MNE (measure_national_implementing) +
                       "Umsetzung der RL"-titles in DIP
Anticipation           DIP Vorgänge (intraday, Bund) + Bundesrat RSS/PDFs
                       + bayern.landtag WP19 lifecycle (Land)
                       + origin-Landtag feeds/APIs as they are verified
Discovery only         Parlamentsspiegel (private cache; no public mirror)
Federal decisions      seven official RII feeds, filtered to the GII corpus;
                       rolling-window intake accumulated forward per refresh
Other decisions        reviewed manual cases (including lower and EU courts)
Case graph research    OLDP citation graph (not in the current export)
```
