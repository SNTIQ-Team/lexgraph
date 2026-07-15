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
| `bundestag_procedures`, `patches` | DIP API and official Drucksachen | Reuse under the [DIP terms](https://dip.bundestag.de/documents/nutzungsbedingungen_dip.pdf); required attribution: **Deutscher Bundestag/Bundesrat – DIP**. Lexgraph-derived extraction, ranking and annotations are identified as transformations. |
| `bgbl_events` | recht.bund.de / BGBl | Official publication metadata and links; the promulgated text is an official work. |
| `bayern_acts`, `bayern_norms`, `bayern_recht_versions` | BAYERN.RECHT | Official legal text may be reused; BAYERN.RECHT reserves database rights in its [usage terms](https://www.gesetze-bayern.de/Content/Document/Nutzungshinweise), so Lexgraph exports only its curated corpus and provenance, not a mirror of the complete service. |
| `bayern_landtag_bills`, `gvbl_events` | Bavarian Landtag and official GVBl/BayMBl records | Curated official procedure/publication facts and origin links; source-page layout and editorial material are not mirrored. |
| `bayern_word_diffs` | Official BAYERN.RECHT states retrieved from current pages, daily snapshots and sparse Internet Archive captures; own conservative matching/diff | Statutory old/new text plus Lexgraph's derived transition record. Internet Archive is a retrieval channel, not a grant of rights; only official legal text and provenance are exported, and incomplete archive coverage is explicit. |
| `eu_instruments`, `eu_transpositions`, `eu_index` | EUR-Lex / CELLAR | EUR-Lex metadata is CC0; legal documents are reusable and editorial/consolidated material is CC BY 4.0 under the [EUR-Lex legal notice](https://eur-lex.europa.eu/content/legal-notice/legal-notice.html?locale=en). |
| `decisions` | Reviewed official decisions and RII | Decisions and official headnotes are § 5 works; RII also states that offered formats are freely reusable. Automated intake is disabled by default pending migration to the documented NeuRIS bulk API. |
| `watched_procedures`, `procedure_watch_state`, `procedure_watch_history`, `amendment_fates`, `chronology`, `graph` | Derived from the listed official inputs; original Lexgraph analysis | Source facts retain their source regime. Lexgraph's original selection, annotations, forecasts and graph modelling use the repository's SNTIQ licensing set. Forecasts are not source facts. |

For commercial reuse of DIP-derived rows, the DIP terms additionally require
a linked notice that the source data is available free of charge at
[dip.bundestag.de](https://dip.bundestag.de). Lexgraph itself links every
procedure back to DIP and does not redistribute altered parliamentary PDFs.

## Quarantined sources

The following snapshot families may exist in a private research cache but are
excluded from public web data and Hugging Face exports by default:

- `buzer`, `buzer_synopse`: written permission is required before systematic
  extraction or redistribution of the private version/synopsis database.
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
