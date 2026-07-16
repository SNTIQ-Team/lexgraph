# Lexgraph data rights matrix

Lexgraph separates rights in an individual legal text from rights in a source
database.  German statutes and official decisions are official works under
[§ 5 UrhG](https://www.gesetze-im-internet.de/urhg/__5.html); this does not by
itself remove a database producer's rights under
[§§ 87a–87b UrhG](https://www.gesetze-im-internet.de/urhg/__87b.html).

The Hugging Face dataset therefore uses `license: other`.  No blanket SNTIQ
licence is asserted over material supplied by public authorities or third
parties.

| Public file family | Source / regime | Reuse boundary |
| --- | --- | --- |
| `federal_catalog`, `federal_acts`, `federal_norms` | BMJ/BfJ GII; § 5 UrhG and GII's express reuse notice | `federal_catalog` is the official `gii-toc.xml` discovery metadata; the other files are official German legal text. Cite GII and do not imply endorsement. |
| `official_federal_state_observations`, `official_federal_state_transitions`, `official_federal_state_objects` | Complete official GII states captured by Lexgraph; § 5 UrhG; Lexgraph content addressing and own diff | Full statutory text remains an official work. Observation and transition metadata are Lexgraph-generated evidence records. `observed_at` proves a retrieval, not commencement; `effective_at` stays empty in the state-diff file. The state SHA identifies canonical uncompressed JSON. No Buzer data is used. |
| `official_transition_reviews` | Complete GII state pairs + integrity-checked final BGBl text + DIP commencement data | Lexgraph-generated acceptance records. A legal date is asserted only after the final BGBl amending command covers every changed norm and one exact DIP commencement clause resolves the amending article. Hashes and official links preserve provenance; Buzer is not evidence. |
| `verified_reconstructions` and its derived `federal_states/objects` extras | Complete official GII anchor + integrity-checked final BGBl commands + exact DIP commencement boundaries; Lexgraph reverse/forward replay | The statutory wording remains an official work, while the replay, review record, interval assertion and content addressing are Lexgraph-generated. A `derived_verified` body is complete and replay-verified but is **not** a source-supplied historical snapshot (`source_exact: false`). Derived objects are kept outside the official GII manifest and are published only with their official anchor, deterministic-gzip hash and explicit origin. |
| `retrospective_history`, `retrospective_legal_intervals`, `retrospective_amendment_events`, `retrospective_observations`, `retrospective_gaps` | GII complete states + DIP procedure/commencement metadata + integrity-checked final BGBl articles | Lexgraph-generated bitemporal evidence/index records. Legal-validity and Lexgraph-knowledge intervals are separate. Event-only rows do not claim a reconstructed consolidated body; unresolved article/sub-article dates remain null. The records are independently reproduced from official sources and contain no Buzer snapshot or synopsis. |
| `bundestag_procedures`, `patches` | DIP API and official Drucksachen | Reuse under the [DIP terms](https://dip.bundestag.de/documents/nutzungsbedingungen_dip.pdf); required attribution: **Deutscher Bundestag/Bundesrat – DIP**. Lexgraph-derived extraction, ranking and annotations are identified as transformations. |
| `verified_federal_events` | Adjacent official GII states, final BGBl/DIP reviews, and DIP draft text independently checked against current GII | Lexgraph-generated evidence records. An unreviewed `exact` row proves two retrieved official states, not an inferred effective date. A reviewed exact row names its final BGBl/DIP gate. `current_text_correspondence` proves only a sufficiently distinctive present-day match, sets `historical_attribution: false`, and asserts neither promulgation nor a patch-level effective date. Buzer is not an input to this public file. |
| `bgbl_events` and captured BGBl final-command evidence | recht.bund.de / BGBl | Official publication metadata and links; the promulgated text is an official work. Locally captured PDFs are integrity-checked and used to derive article-level review evidence; the HF review rows publish hashes, official links and matched state diffs rather than a private-source synopsis. |
| `bayern_acts`, `bayern_norms`, `bayern_recht_versions` | BAYERN.RECHT | Official legal text may be reused; BAYERN.RECHT reserves database rights in its [usage terms](https://www.gesetze-bayern.de/Content/Document/Nutzungshinweise), so Lexgraph exports only its curated corpus and provenance, not a mirror of the complete service. |
| `bayern_landtag_bills`, `gvbl_events` | Bavarian Landtag and official GVBl/BayMBl records | Curated official procedure/publication facts and origin links; source-page layout and editorial material are not mirrored. |
| `bayern_word_diffs` | Official BAYERN.RECHT states retrieved from current pages, daily snapshots and sparse Internet Archive captures; own conservative matching/diff | Statutory old/new text plus Lexgraph's derived transition record. Internet Archive is a retrieval channel, not a grant of rights; only official legal text and provenance are exported, and incomplete archive coverage is explicit. |
| `eu_instruments`, `eu_transpositions`, `eu_index` | EUR-Lex / CELLAR | EUR-Lex metadata is CC0; legal documents are reusable and editorial/consolidated material is CC BY 4.0 under the [EUR-Lex legal notice](https://eur-lex.europa.eu/content/legal-notice/legal-notice.html?locale=en). |
| `decisions` | Reviewed official decisions and RII | Decisions and official headnotes are § 5 works; RII also states that offered formats are freely reusable. Automated intake is disabled by default pending migration to the documented NeuRIS bulk API. |
| `neuris_archive`, `neuris_objects` | Official NeuRIS legislation changelog and advertised ZIP/XML/HTML artifacts (BMJ/DigitalService Testphase) | Captured legislation remains official material; Lexgraph's append-only event/capture ledger, hashes and gap labels are its own evidence metadata. The export preserves metadata-only, tombstone and failed-capture rows but ships bytes only for `capture_status: captured` objects whose size, SHA-256, media shape and official source URL verify. Temporary or failed partial downloads are never redistributed. NeuRIS ELI date components are retained as source identifiers and are not re-labelled as legal-effective dates. |
| `watched_procedures`, `procedure_watch_state`, `procedure_watch_history`, `amendment_fates`, `git` (`chronology` compatibility alias), `graph` | Derived from the listed official inputs; original Lexgraph analysis | Source facts retain their source regime. Lexgraph's original selection, annotations, forecasts and graph modelling use the repository's SNTIQ licensing set. Forecasts are not source facts. |

For commercial reuse of DIP-derived rows, the DIP terms additionally require
a linked notice that the source data is available free of charge at
[dip.bundestag.de](https://dip.bundestag.de). Lexgraph itself links every
procedure back to DIP and does not redistribute altered parliamentary PDFs.

## Quarantined sources

The following snapshot families may exist in a private research cache but are
excluded from public web data and Hugging Face exports by default:

- `buzer`, `buzer_synopse`: the retained research cache is excluded because
  Lexgraph does not redistribute or systematically extract Buzer's private
  version/synopsis database. Permission would matter only for reusing that
  database itself. It is not needed for independently producing equivalent or
  better facts and diffs from official sources with Lexgraph's own workflow.
  A curated per-act external link is not a republication: public act records
  may expose it as `cross_checks[]`, explicitly non-authoritative.
- `laender_bills`, `laender_monitor`: Parlamentsspiegel is used only as an
  internal discovery aid until facts are re-verified at the originating
  Landtag or republication permission is granted.

`LEXGRAPH_INCLUDE_QUARANTINED=1` is a private research switch.  The Hugging
Face exporter refuses to run when that switch influenced the web build.

## Software and original material

The repository's software and SNTIQ-authored annotations remain governed by
[`LICENSING.md`](../LICENSING.md).  Those licences do not relicense official
works, third-party databases, trademarks, logos or source-site editorial
content.
