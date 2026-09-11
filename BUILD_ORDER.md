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

### Milestone 4D-0.1 — Deterministic provenance ordering (implemented)
- Fixes a pre-existing nondeterminism in 4A and 4B: `provenance.evidence_ids`
  and `provenance.listing_ids` were built in evidence arrival order, so an
  equivalent evidence set produced a different provenance record depending on
  arrival order. Measured on `89d28d59` before the fix: 100/100 shuffled
  permutations differed for both extractors; only `provenance` moved, features
  were byte-identical
- `evidence_ids` and `ListingView.evidence_ids` are sorted; `listing_ids` is
  ordered by `(str(value), type name)`, a total order across mixed types
- `sorted()`, never `set()`: an id may legitimately repeat when a record
  carries no payload hash and is therefore never collapsed
- Never `str()`: identifier values keep their original type, only the ordering
  is canonical. The type name breaks the tie `str()` alone leaves between
  distinct values that render identically, such as 1 and "1"
- The `views` list itself is deliberately left in arrival order, so feature
  computation is untouched and feature invariance holds by construction
- Only observable change: two existing provenance arrays are now sorted. Same
  members, same multiplicity, no field, type, endpoint or schema change

### Milestone 4D — Buyer reach (implemented)
- Deterministic derivation over evidence already collected by 3A/3B/3C — no
  new provider, no provider call, no endpoint, no persistence
- **Channel evidence, never buyer evidence.** Records WHERE a seller could
  show up and what is observably true about those places. It never estimates
  buyer counts, audience size, market size, conversion, or reachable
  population, and converts nothing: views are not buyers, subscribers are not
  reachable buyers, listings are not a buyer population, sellers are not
  market size, review proxies are not sales, CPC is not purchase intent
- Three channel classes: `MARKETPLACE_STOREFRONT` (distinct sellers),
  `CONTENT_PLATFORM_CREATOR` (distinct creators), `SEARCH_QUERY_SURFACE`
  (keywords with observed volume)
- Four claim layers kept apart, never collapsed: existence (OBSERVED),
  relevance (**permanently capped at INFERRED** — it rests on a Milestone 1
  query hypothesis), activity (OBSERVED or UNKNOWN), and commercial activity,
  which is **cited from 4A only** and never derived here. A purchase proxy is
  never promoted to a reach surface
- Capability-aware MISSING: capability-not-requested, provider-failed,
  unexpected-error and ran-but-empty stay four distinguishable facts. Missing
  evidence never becomes zero reach
- `reach_evidence_pattern_v1` describes evidence SHAPE only —
  NO_CHANNELS_OBSERVED / SINGLE_CHANNEL_CLASS / MULTI_CHANNEL_CLASS /
  CHANNELS_UNKNOWN. MULTI_CHANNEL_CLASS is never "better"
- `paid_auction_observed` means only that an advertiser auction was
  observably present. `null` is UNKNOWN, never "no auction"
- `observed_across_multiple_providers` is deliberately literal: different
  provider surfaces observe different things, they do not corroborate one
  proposition
- Geography is known only for search evidence and never inferred for
  marketplace or content records from absent data
- Identity-based counts, canonically ordered provenance (4D canonicalizes its
  own lineage; `collect_listing_views` keeps its arrival-order semantics
  unchanged), duplicate multiplicity preserved
- No score: `value` is permanently `None`, state is
  EVIDENCE_PRESENT_UNSCORED. No POS, ECS, or RED/YELLOW/GREEN
- Dimension name is `preliminary_buyer_reach`, never `buyer_reach`, so it
  cannot be wired into the legacy `ScoreDimensions.buyer_reach` weight

### Milestone 4E — Competition opportunity (complete)
- `app/services/competition_opportunity.py`, `competition_opportunity_v1`: a
  deterministic derivation over marketplace evidence 3A collected and 3C
  resolved. No provider, no provider call, no endpoint, no persistence
- Returned by `POST /research/preliminary` under `competition_opportunity`,
  as a fourth independent `DerivationOutcome` behind the same failure
  boundary as 4A/4B/4D
- Competition is modelled NON-LINEARLY. `competition_field_pattern_v1`
  reports the SHAPE of the field, and every pattern carries two opposed
  readings that are emitted together and never ranked. No pattern maps to a
  number and no ordering over the patterns exists; AST guards enforce both
- Observable evidence is kept apart from conclusions: `saturation`,
  `entry_difficulty`, `win_probability`, `differentiation_opportunity`,
  `competitor_strength`, `competitor_revenue` and `market_share_available`
  are permanent UNKNOWN markers
- Reuses `collect_listing_views`, so 4A, 4B and 4E can never disagree about
  what a listing is. An AST guard proves the module reads no price, review
  count or rating, so it cannot restate a 4A, 4B or 4D finding
- Structural claims require at least half the listings to carry a seller;
  below that the field is FIELD_STRUCTURE_UNKNOWN rather than a shape
  inferred from a minority of the evidence
- No score: `value` is permanently `None`, state is
  EVIDENCE_PRESENT_UNSCORED. No POS, ECS, or RED/YELLOW/GREEN
- Dimension name is `preliminary_competition_opportunity`, never
  `competition_opportunity`, so it cannot be wired into the legacy
  `ScoreDimensions.competition_opportunity` weight

### Milestone 4F — Audience attention (complete)
- `app/services/audience_attention.py`, `audience_attention_v1`: a
  deterministic derivation over public-content evidence 3B collected and 3C
  resolved. No provider, no provider call, no endpoint, no persistence
- Returned by `POST /research/preliminary` under `audience_attention`, as a
  fifth independent `DerivationOutcome` behind the same failure boundary
- Reports the DISTRIBUTION and CONSISTENCY of observed attention, never a
  sum. No total-views feature exists, because a total is the statistic one
  viral video corrupts. `attention_pattern_v1` reports the shape, and every
  pattern states explicitly what it does NOT establish
- The largest contributor is isolated rather than averaged away:
  `top_video_attention_share`, `median_views_excluding_top_video`,
  `videos_covering_half_of_attention`, `top_channel_attention_share`. A field
  whose attention is one video reports SINGLE_VIDEO_ATTENTION however large
  that video is, and every shape classification is scale-invariant
- Attention is not demand: `buyer_count`, `purchase_intent`,
  `candidate_audience_size`, `demand_durability`, `willingness_to_pay`,
  `watch_time` and `conversion_probability` are permanent UNKNOWN markers
- Subscriber counts are not read at all. An AST guard proves the module
  cannot reach `channel_subscriber_count`, so channel context can never
  become a candidate's audience size
- Five facts stay distinguishable: capability not requested, provider
  failed, unexpected error, ran-and-returned-nothing, and videos observed
  whose view counts were not. The last is ATTENTION_UNMEASURED and is never
  the same as attention measured at zero
- No score: `value` is permanently `None`, state is
  EVIDENCE_PRESENT_UNSCORED. No POS, ECS, or RED/YELLOW/GREEN
- Dimension name is `preliminary_audience_attention` — neither the legacy
  `ScoreDimensions.audience_interest` nor 3C's scored
  `preliminary_audience_interest`, which this milestone leaves untouched

### Milestone 4G — Problem/product fit evidence (complete)
- `app/services/product_job_fit.py`, `product_job_fit_v1`: a deterministic
  assessment of whether a 4C specification is STRUCTURALLY appropriate for
  the job its own evidence supports. No provider, no network, no persistence
- Returned by `POST /product/specification` under `product_job_fit`, behind
  its own boundary so an assessment bug cannot break an endpoint that already
  produced a valid specification
- The first milestone permitted to consume 4C, because its purpose is to
  evaluate the relationship between the proposed product and the
  evidence-backed job. It reads only 4C's STRUCTURED result — claim classes,
  job scores, format enums, conflict topics, which fields are UNKNOWN — and
  never a generated string
- Assesses the SUPPORT CHAIN rather than re-checking the format against the
  job, which 4C chose FROM the job and would therefore answer YES by
  construction. `fit_assessment_pattern_v1` reports the FIRST break in that
  chain; the complete set of findings is emitted separately as observations
- Structural fit is derived from `interaction_mode_v1`, an explicit taxonomy
  of how each format is used, so substitution fidelity follows from a stated
  property rather than being asserted pair by pair. A spreadsheet preserves a
  calculator's repeated computation; a PDF cannot provide a community's
  ongoing interaction
- Never claims sales, conversion, product-market fit, willingness to pay,
  market size, revenue, usefulness, or proven demand: eight permanent UNKNOWN
  markers, and no emitted string may carry commercial-success vocabulary
- Reads no magnitude — search volume, views, review counts, listing counts
  and prices cannot make a product structurally more suitable for a job
- No score: `value` is permanently `None`, state is
  EVIDENCE_PRESENT_UNSCORED. No POS, ECS, or RED/YELLOW/GREEN
- Dimension name is `preliminary_product_job_fit`, never `product_market_fit`
  and never the legacy `ScoreDimensions.problem_product_fit`

## Milestone 5 — Faceless content intelligence

### Milestone 5A — Robust creator-relative outlier evidence (implemented)
- Pure derivation over already-collected Milestone 3B public-content
  evidence; no provider calls, network calls, persistence, or new endpoint
- Separate `faceless_content_intelligence_v1` and
  `robust_content_outlier_v1` versions; the existing `content_outlier_v1`
  ratio is unchanged
- Candidate and research-run scoped evidence reads, with only
  `purpose=AUDIENCE` / `signal_type=public_video_view_count` records in scope
- Only `TruthClass.OBSERVED` view counts contribute. UNKNOWN counts remain
  UNKNOWN and are never zero-filled or read from a payload snapshot
- Creator baseline requires at least three unique observed-view videos and is
  the median observed view count for that creator
- Relative score is exactly
  `log2(video_views / creator_median_views)` and is calculated only when both
  values are strictly greater than zero
- A zero creator baseline is reported as a structured non-scoreable state;
  no epsilon, offset, or fabricated value is used
- Missing video/channel identity, conflicting observations, insufficient
  samples, observed zero values, and unavailable view counts remain distinct
  non-scoreable states; missing channel ids never form a synthetic creator
- Publication age and subscriber/channel aggregate metrics are not read;
  no demand, buyer, conversion, market-size, sales, revenue, or candidate
  score is produced
- Canonical evidence lineage and shuffled-input determinism are tested

### Milestone 5B — Content pattern extraction (implemented)
- Pure derivation over already-collected Milestone 3B public-content
  evidence; no provider calls, network calls, persistence, endpoint, or LLM
- Service-layer only, matching 5A's boundary; route count unchanged at 15
- Scoped to `public_content_observation` / `purpose=CONTENT` records, and
  only where the RECORD is `TruthClass.OBSERVED`. A payload value behind a
  non-OBSERVED record is never read. 5A keeps `purpose=AUDIENCE` view
  magnitudes, so the two milestones read disjoint signals
- Candidate and research-run scoping identical to 5A: an omitted run id
  admits only explicitly runless inline evidence
- Versioned `content_patterns_v1`, `content_normalization_v1`,
  `duration_band_v1`, `outlier_cooccurrence_v1`
- Emits recurring title tokens, adjacent title bigrams, tags, categories and
  duration bands, each with counts, distinct-creator counts, concentration
  state, and evidence/video lineage
- PREVALENCE and OUTLIER CO-OCCURRENCE are separate structures with separate
  denominators and are never combined into a strength or quality measure
- Creator concentration is always reported beside the raw count, so one
  prolific creator cannot make a pattern read as field-wide support.
  Co-occurrence additionally requires three independent creators before any
  comparison is reported
- `top_channel_share` is measured against EVERY occurrence of the pattern,
  never the attributed subset, so unknown or conflicting creator identity can
  only lower it. Absent and conflicting channel ids both count against a
  dominance claim and are reported as separate counts; below the threshold
  with identity incomplete the state is CREATOR_PARTIALLY_UNKNOWN, since
  SINGLE_CREATOR and MULTI_CREATOR would claim a spread that is not known
- Missing title/tags/category/duration stays UNAVAILABLE for that field,
  shrinks that field's denominator, and is never counted as absence or zero
- Deterministic NFKC + casefold normalization preserving Unicode letters,
  numbers and combining marks, plus fixed duration bands; no stemming, no
  semantic clustering, no LLM. Observed content is never deleted: accented
  Latin, digits and non-Latin scripts all survive
- Two OBSERVED records that disagree about the same video make that field
  CONFLICTING — reported and excluded, never resolved by arrival order, so
  the result is a function of the record SET rather than its sequence
- Duplicate suppression retains the smallest evidence id per fingerprint, so
  lineage is order-independent too; a record with no payload hash is never
  collapsed. Derived features stay INFERRED on every path even though every
  source record is OBSERVED
- Milestone 5A evidence is consumed only after its candidate_id and
  research_run_id are verified against this derivation's scope; a mismatch
  is reported as OUTLIER_SCOPE_MISMATCH and no observation is read
- No numeric score of any kind, no 0-100 value, no RED/YELLOW/GREEN, no POS
  or ECS change; `value` is permanently `None` and the dimension name is
  `preliminary_content_patterns`
- Co-occurrence is reported as observation only. No causal or predictive
  language is emitted, enforced by a negation-aware guard over every emitted
  string and the full enum surface
- Channel aggregates (`channel_subscriber_count`, `channel_view_count`,
  `channel_video_count`) and engagement magnitudes are unreadable, enforced
  by an AST guard that also pins the exact payload keys read
- 30 mutations of the milestone's guards were applied and all 30 were killed,
  including reverting the concentration denominator, folding CONFLICTING
  channel identity into ordinary absence, declaring the dimension SCORED,
  reverting duplicate retention to first-wins, and promoting derived features
  to OBSERVED

### Remaining Milestone 5 scope
- 30 production-ready experiments
