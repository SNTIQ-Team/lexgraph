# Lexgraph — an event-sourced normative state engine

> Sibling of [Amtsgraph](https://github.com/SNTIQ-Team/amtsgraph).
> Amtsgraph answers **who** may decide. Lexgraph answers **what law
> applies, in what version, and why** — what the law *was*, *is*, and
> *may become* — with status, source tier and caveats on every answer.

## The formula

Git is the mental model, not the storage:

> **Official and non-official sources produce typed events. Events contain
> or reference patch instructions. Accepted patches produce norm versions.
> Norm versions materialize into a NormativeWorld for a date, jurisdiction
> and context. Proposed, rejected and stale patches remain queryable but
> do not alter the effective world.**

Or, in one line:

> **Law is not a text corpus. Law is a signed, temporal, multi-authority
> patch history over normative state.**

| Law | Git |
| --- | --- |
| Act (Gesetz) | repository |
| Norm (§ / Abs / Satz) | object (content-addressed text, stable norm_id) |
| Änderungsbefehl ("In § 10 Abs. 3 werden die Wörter … gestrichen") | **patch / commit body** |
| Fassung on a date | checkout |
| Bill (Entwurf) | branch |
| Bundesrat recommendation | review comment — **not** a merge |
| Final adopted text | **merge candidate** |
| Verkündung / official publication | **signed release** (merge into main) |
| Inkrafttreten | effective tag |
| Repeal | tombstone commit |
| Case law | annotated backlinks |

## Core objects

### PatchInstruction — the atom of German legislation

German amendment law is written as patch instructions, not full texts.
They are first-class objects, each with its own lifecycle:

```json
{
  "patch_id": "patch:de.bund.stag.p10.abs3.repeal.2025",
  "target": "norm:de.bund.stag.p10.abs3",
  "operation": "insert|replace|delete|repeal|renumber|split|merge",
  "old_text_constraint": "optional exact/normalized text",
  "new_text": "…",
  "status": "proposed|recommended|adopted|published|in_force|rejected|not_merged",
  "source_doc": "bt-ds:21/1373",
  "procedure": "bt-vorgang:…",
  "decided_at": null, "published_at": null, "valid_from": null
}
```

A recommendation that never reached the final adopted text is
`recommended, merged=false, validity_effect=none` — queryable history,
zero normative effect. (This is the 12-Monatsfrist episode, encoded.)

### LexEvent — the stream

An amendment is not one moment; it is a chain of typed events:

```json
{
  "event_id": "event:bgbl:2025-I-256:promulgation",
  "kind": "introduced|referred|recommended|adopted|approved|objected|
           mediated|signed|published|entered_into_force|repealed",
  "actor": "Bundestag|Bundesrat|Bundespräsident|BMJ|EU-Council|Court",
  "source_doc": "…", "time": "2025-10-30",
  "legal_effect": "none|procedural|creates_patch|adopts_patch|
                   publishes_law|activates_norm"
}
```

The engine is a reducer: `NormativeWorld(t) = reduce(LexEvents ≤ t)`.

### Norm identity — links, not hashes

`content_hash` identifies exact text. Semantic continuity across
renumbering/split/merge is NOT computable from text — it is asserted:

```json
{
  "norm_id": "stable internal id",
  "content_hash": "hash of normalized text",
  "identity_links": [
    { "type": "RENAMED_FROM|SPLIT_FROM|MERGED_FROM|CONTINUES",
      "target": "old_norm_id",
      "source": "amendment instruction / official rationale" }
  ]
}
```

## The status ladder (encoded invariants)

Teaching shorthand (README level):

```text
vorgeschlagen → empfohlen → beschlossen → ausgefertigt/verkündet → in Kraft
```

The schema models the real, **branching** state machine, keyed by
`procedure_type` (the German federal pipeline is one of several):

```text
drafted → introduced → referred → committee_recommended
        → [plenary_amended?] → adopted_by_bundestag
        → bundesrat_approved | bundesrat_no_objection
          | bundesrat_objected → mediation (Vermittlungsausschuss) → …
        → signed (Ausfertigung) → published (Verkündung) → in_force

EU:  commission_proposal → EP/Council procedure → adoption
     → OJ publication → entry_into_force → [application date]
```

- **Enumeration ≠ incorporation.** Drucksachen listed in a vote header are
  procedural pedigree, not adopted content.

- **Committee recommendation ≠ final law.** The dispositive text is the
  **final adopted text** — usually based on the leading committee's
  Beschlussempfehlung, but modified by successful plenary amendments,
  Bundesrat/Vermittlungsausschuss outcomes, or later procedural steps.

- **Final adopted text + BGBl publication = the authoritative release**
  (Art. 82 GG). Everything before is a prediction with a status.

## Dates are not one thing

| Field | Meaning |
| --- | --- |
| `document_date` | when the source document was created |
| `introduced_at` | bill entered procedure |
| `decided_at` | an organ voted/adopted |
| `signed_at` | Ausfertigung (Bundespräsident) |
| `published_at` | Verkündung (BGBl) |
| `valid_from` / `valid_until` | Inkrafttreten / Außerkrafttreten |
| `applicable_from/to` | facts covered — transitional law, Rückwirkung |
| `known_since` | when Lexgraph learned it |
| `fetched_at` | when the source was fetched |

`valid_from` and `applicable_from` must never be conflated (transitional
provisions, pending procedures, old fact patterns).

## Genesis — a typed multi-plane graph, never a chain

`§ 24 AufenthG ← 2022/382 ← 2001/55/EC` is README shorthand. The model
forbids linearity; the planes are distinct relations:

```text
from                   type             to                 (plane)
──────────────────────────────────────────────────────────────────────
§ 24 AufenthG          TRANSPOSITION_OF Directive 2001/55  (implementation)
Art. 5 Directive       AUTHORIZES       Decision 2022/382  (legal basis)
Decision 2022/382      ACTIVATES        Directive 2001/55  (trigger)
activation             MOTIVATED_BY     Russian invasion   (political cause)
Decision 2022/382      ADOPTED_BY       EU Council         (procedure)
source text            PUBLISHED_IN     EUR-Lex / OJ       (publication)
```

## Edge taxonomy

Normative: `AMENDS · REPEALS · REPLACES · REFERS_TO · IMPLEMENTS ·
DEROGATES_FROM · CONFLICTS_WITH · SUPERSEDES · VALID_IN · SUSPENDS ·
EXPIRES`
Genesis: `LEGAL_BASIS_OF · AUTHORIZES · ACTIVATES · TRANSPOSITION_OF ·
MOTIVATED_BY`
Procedure: `PROPOSES_PATCH · RECOMMENDS_PATCH · ADOPTS_PATCH ·
REJECTS_PATCH · NOT_MERGED · CREATED_BY · ADOPTED_BY · SIGNED_BY`
Publication: `PUBLISHED_IN · ENTERS_INTO_FORCE · HAS_SOURCE ·
HAS_OFFICIAL_VERSION · HAS_CONSOLIDATED_VERSION`
Application: `INTERPRETED_BY · APPLIED_BY · COMPETENCE_OF ·
PROCEDURE_UNDER · APPLIES_TO_FACT_PATTERN`

Every edge carries a **class**, so soft causal links can never masquerade
as legal grounds:

```text
hard_normative   AMENDS, REPEALS, IMPLEMENTS, AUTHORIZES, LEGAL_BASIS_OF, …
procedural       PROPOSES_PATCH, ADOPTS_PATCH, SIGNED_BY, …
publication      PUBLISHED_IN, ENTERS_INTO_FORCE, HAS_OFFICIAL_VERSION, …
soft_causal      MOTIVATED_BY, CREATED_BY (political context)
semantic         REFERS_TO, APPLIES_TO_FACT_PATTERN (thematic)
```

The load-bearing distinctions: `PROPOSES_PATCH ≠ ADOPTS_PATCH`,
`PUBLISHED_IN ≠ ENTERS_INTO_FORCE`, `IMPLEMENTS ≠ ACTIVATES`,
`LEGAL_BASIS_OF ≠ MOTIVATED_BY`. All edges additionally carry
Amtsgraph-style `delta` (directionality) and `trust` (source tier).

## Source tiers (root-of-truth discipline)

**The authoritative promulgation source is jurisdiction-specific** — there
is no single "is it law?" oracle:

| Layer | Authoritative publication |
| --- | --- |
| EU law | Official Journal / EUR-Lex (CELEX/ELI) |
| Bund | BGBl via recht.bund.de |
| Land | GVBl / the Land's Verkündungsplattform |
| Kommunalrecht | Amtsblatt / Satzungsbekanntmachung |
| Court law | official court databases / ECLI publication |

Procedure and text sources on top:

| Source | Tier |
| --- | --- |
| DIP (dip.bundestag.de) | authoritative **Bundestag** procedure |
| Bundesrat documents (Drucksachen, TO, Plenarprotokolle) | authoritative **Bundesrat** procedure — first-class, NOT derivable from DIP (Stellungnahmen, Einsprüche, Zustimmungen, Vermittlung) |
| gesetze-im-internet.de | official consolidated federal text |
| NeuRIS / rechtsinformationen.bund.de | official structured source (rising) |
| buzer.de | **non-authoritative convenience** — diff/history hints only |

Staleness is first-class: every source carries cadence + last_fetch;
a norm grounded in a stale feed carries that flag to the answer.

## Storage: QFS arena

Storage is the **QFS v4 arena format** (single 64-byte-aligned
content-addressed file; offsets as identities; renders natively in
qfs_visualizer with force layout, trust rings, δ-styled edges,
Contradictions mode and the 4D timeline). Writer/reader:
[pipeline/qfs.py](../pipeline/qfs.py) — byte-faithful to
`qfsParser.ts`, round-trip-tested against the reference demo.

Semantic mapping:

| Legal object | QFS object |
| --- | --- |
| Norm / act / institution / origin | QNode (label→Blob, trust tier) |
| Relation (edge taxonomy) | QEdge (reltype, δ, trust); hyperedges for multi-party events |
| Validity/applicability claim | QBelief — pTrue = claim holds; pFalse = rejected / not law / not applicable; pBoth = contested by authoritative sources or unresolved split; pNone = unknown / not yet grounded. Revisions = amendments, bornTick = event date. Epistemic truth ≠ legal validity — beliefs carry validity claims, never "truth" |
| Pipeline snapshot per tick | QState |
| Stage change (eingebracht→beschlossen→verkündet) | QTransition |
| Jurisdiction state per tick | QWorld — contradiction = **unresolved legal tension**: overdue duties, conflicting authorities, stale grounding, hierarchy conflict, disputed applicability, or a proposed patch circulating as if it were law |

The visualizer's timeline axis is built from State/World ticks and Belief
bornTicks — **scrubbing the timeline replays legislation in real time**;
belief-revision collapse shows current law, Contradictions mode shows
contested/diverging law.

## Internal schema (minimum honest set)

```text
source · source_snapshot · source_document · procedure · procedure_stage ·
actor · lex_event · patch_instruction · norm · norm_version · norm_text ·
norm_relation · applicability_rule · publication · deadline ·
interpretation · case_backlink · caveat
```

`source_document ≠ lex_event ≠ patch_instruction ≠ norm_version` — mixing
these makes the system confuse "a document contains a proposal" with "the
norm changed". That confusion is precisely the failure mode Lexgraph
exists to kill.

## MVP scope

**Substantive corpus** (practice-driven): AufenthG, AsylG, AsylbLG, StAG,
SGB I–XIV selection, Ukraine-VOs; EU layer: 2001/55/EC + 2022/382.
**Procedural core** (explicitly, or the engine knows *what is written*
but not *how it is contested*): VwGO (administrative courts), SGG (social
courts), SGB X (social administrative procedure), VwVfG / BayVwVfG
(general administrative procedure).
**Land layer**: Bavaria first (AGVwGO Art. 15 — Widerspruch largely
abolished; DVAsyl; ZustV-AuslR).

Features in build order:
1. QFS writer + norm ingestion at § granularity (current Fassung)
2. LexEvent stream from DIP (anticipation pipeline, realtime)
3. PatchInstruction extraction from Änderungsgesetze
4. `checkout / log / diff / blame` over the event stream
5. Genesis planes (EU layer)
6. Deadline objects with ⛔ ÜBERFÄLLIG detection
7. Case backlinks (openlegaldata) — later

## Acceptance test (the architecture's definition of done)

Claim: *"Untätigkeitsklage for citizenship was raised to 12 months."*

Correct machine answer:

```text
Claim: FALSE as geltendes Recht.
Found:     recommended patch (Bundesrat Stellungnahme, bt-ds 21/1373).
Not found: adopted patch in the final Bundestag decision;
           BGBl publication changing the rule;
           consolidated norm reflecting 12 months.
Classification: recommended-but-not-merged patch → no effect on
                NormativeWorld. § 75 VwGO threshold remains 3 months.
Caveat:    the proposal remains historically relevant legislative material.
```

If Lexgraph answers like this, the architecture is right.
