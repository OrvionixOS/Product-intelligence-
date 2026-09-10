# Build Order

## Milestone 0 — Foundation
- Evidence schema
- Truth classes
- Deterministic scoring
- Provider contracts
- Tests

## Milestone 1 — Candidate discovery
- Seed keyword request
- Structured 20-candidate generation
- Semantic dedupe
- Supported-format filter

## Milestone 2 — Real evidence
- DataForSEO adapter
- Etsy official API adapter
- YouTube Data API adapter
- Provider call/cost telemetry
- Cache keys + freshness windows

## Milestone 3 — deliberately split into safer implementation slices
The original Milestone 3 scope is preserved in full; it is only decomposed.

### Milestone 3A — Etsy marketplace evidence (implemented)
- Typed batch MarketplaceProvider contract
- Etsy official API adapter (no scraping)
- Purchase-proxy, price, and competition evidence + deterministic extractors
- Query/result caps, caching, telemetry, partial-failure handling

### Milestone 3B — YouTube public-content evidence (implemented)
- Typed batch PublicContentProvider contract
- YouTube Data API v3 adapter (search + videos + channels; quota-accounted)
- Audience-interest evidence + deterministic extractors
- Creator-relative content-outlier foundation (content_outlier_v1, no age term)
- Query/quota/lookup caps, caching, telemetry, partial-failure handling

### Milestone 3C — Research orchestration (implemented)
- Preliminary research all candidates across every capability implemented
  through 3B, with honest per-capability partial failure
- Evidence → preliminary dimension bridge preserving provenance, truth
  class, missing/UNKNOWN state, and source references
- Deterministic preliminary ranking (`preliminary_rank_v1`, lexicographic,
  no invented weights) with explainable pairwise ordering
- Deterministic top-five deep-research selection with deterministic
  tie-breaking
- Explicitly NOT final POS/ECS: no weights, no thresholds, no
  RED/YELLOW/GREEN in this slice

## Milestone 4 — deliberately split into safer implementation slices

### Milestone 4A — Purchase evidence (implemented)
- Deterministic Purchase Evidence extractor over evidence already collected
  by 3A/3C — no new provider calls, no second marketplace research system
- Public purchase PROXIES only (review counts, review presence, listing
  longevity, paid comparables, seller breadth). Never sales, never revenue
- Direct/authorized transactional evidence interface reserved but NOT
  implemented: no seller OAuth, no connected-store transactions
- `market_validation_pattern_v1`: describes the SHAPE of proxy evidence
  (absent / unknown / weak / concentrated / multiple-sellers /
  distributed), explicitly not a ranking of desirability
- No 0-100 score: no purchase-evidence formula is approved, so the
  dimension returns EVIDENCE_PRESENT_UNSCORED rather than inventing one
- Inherits the 3C failure boundary as a derivation outcome

### Milestone 4B — Price evidence (implemented)
- Deterministic Price Evidence derivation over the marketplace PRICE
  evidence already collected by 3A/3C — no new provider calls
- Observed ASKING prices only. Never transaction prices, never willingness
  to pay, never revenue, and never a price recommendation
- **Per-currency price bands.** Currencies are never combined and none is
  discarded in favour of a dominant one; no approved FX source exists, so
  cross-currency comparison is explicitly not performed
- $0 listings are preserved as free competitors: counted separately,
  excluded from paid-price statistics, never treated as missing data and
  never as a paid comparable
- Prices are NOT normalized per item/template/page; a bundle and a single
  item remain separate observed listing prices
- No 0-100 score: no price-evidence formula is approved, so the dimension
  returns EVIDENCE_PRESENT_UNSCORED
- Shares one canonical listing view with 4A, so Purchase Evidence and Price
  Evidence cannot derive contradictory paid-comparable definitions
- Inherits the 4A DerivationOutcome and failure boundary

### Milestone 4C — Product generator (implemented)
- Deterministic product specification for ONE selected candidate, generated
  on demand via `POST /product/specification`. Never generated automatically
  for all five ranked candidates
- `job_to_be_done_v1`: an eight-job taxonomy (CALCULATE, DECIDE, PLAN,
  TRACK, LEARN, EXECUTE, ASSESS, ORGANISE) applied by deterministic keyword
  matching. An approved but UNVALIDATED V1 heuristic, never described as
  empirically proven. Signals that do not separate return UNKNOWN
- `format_selection_v1` returns TWO separate answers: an `ideal_format`
  that may lie outside V1 build capability (always ASSUMED, always labelled
  as outside capability, never implied to be market-validated), and a
  `buildable_v1_format` drawn only from Milestone 1's seven approved
  formats. Milestone 1's candidate validator is unchanged
- `SpecClaimClass` (OBSERVED / INFERRED / ASSUMED / UNKNOWN) is local to
  4C. The shared `TruthClass` is untouched; ESTIMATED maps to INFERRED and
  never to OBSERVED, and no class is ever upgraded
- Per-field provenance: every field carries its claim class, a written
  basis, contributing evidence ids, and signal types. A derived field is
  capped by the weakest source it rests on
- Price is a Milestone 4B citation only — observed ASKING prices, never a
  recommended, optimal, or transaction price and never willingness to pay
- No evidence produces a MISSING specification rather than an invented
  product; sparse evidence generates but reports `insufficient_evidence`;
  contradictory signals are preserved in `conflicts`
- `ProductSpecificationProvider` reserves a future prose-only LLM seam. The
  V1 default is a deterministic template and there is no live LLM
  dependency. A provider may reword narrative fields only: it cannot touch
  claim classes, evidence, formats, numbers, or decisions
- The forbidden-claim vocabulary gates the deterministic generator as well
  as any future provider, over Unicode-normalized text: candidate marketing
  copy is never restated as a product name or promise
- Observations are deduplicated and evidence reads are scoped to one
  research run, so a re-observed listing cannot double-weight a
  classification and a specification cites only its own run
- Produces no score of any kind, and no POS/ECS/RED-YELLOW-GREEN

### Milestone 4D-0 — Query provenance (implemented)
- Prerequisite for Buyer Reach, shipped as its own slice rather than hidden
  inside it
- Evidence records now retain the NORMALIZED query actually sent to the
  provider (`originating_queries`), so a later milestone can say "this
  observation came back from query X, generated for candidate Y" instead of
  the much weaker "something associated with candidate Y"
- Provider-request provenance, explicitly NOT the original raw candidate
  wording, and never a phrase a buyer typed
- Attribution is candidate-filtered: another candidate's query never lands on
  this candidate's evidence merely because both queries returned the same
  observation
- `originating_query_shared` marks query overlap between candidates in one
  run. Deliberately a boolean, not a count: a number would invite being read
  as popularity, demand, reach, or market strength
- Both fields live OUTSIDE `raw_payload` and are excluded from
  `raw_payload_hash`. Observation identity, candidate association, and
  retrieval provenance stay three separate concepts
- `None` means provenance UNKNOWN and is never backfilled; `()` means known
  to contain zero originating queries. SQL columns are nullable with no
  default so historical NULL keeps meaning UNKNOWN
- Provenance is metadata: it never changes a truth class and can never
  upgrade UNKNOWN or INFERRED to OBSERVED
- 4A, 4B and 4C outputs are unchanged

### Milestone 4D — Buyer reach (not started)
- Buyer reach, gated on 4D-0

## Milestone 5 — Faceless content intelligence
- YouTube creator baseline
- Robust outlier scoring
- Content pattern extraction
- 30 production-ready experiments
