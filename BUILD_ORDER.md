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

### Milestone 5C — Production-ready content experiments (implemented)
- Pure derivation over Milestone 5B's result; no provider calls, network
  calls, persistence, endpoint, or LLM. Service-layer only, route count 15
- Consumes the 5B result and NOTHING else. 5B is the single verified gateway
  to 5A: it already checks 5A's candidate_id and research_run_id before
  reading an observation, so re-verifying 5A here would duplicate that check
  where the two could drift and disagree
- The supplied 5B result's own candidate_id and research_run_id are verified
  before anything is read; a mismatch is refused unread as
  PATTERNS_SCOPE_MISMATCH and leaks no count, value or lineage
- Versioned `content_experiments_v1`, `experiment_id_v1`,
  `experiment_ordering_v1`, `experiment_template_v1`
- Emits up to 30 experiments, each with a stable id, title, hypothesis,
  variable, baseline definition, observed evidence with full lineage,
  production instructions, what must be held constant, primary measurement,
  a success criterion expressed as a decision rule, evidence sufficiency and
  limitations
- Experiment ids derive from (scope, variable kind, variable value) only, so
  an experiment keeps its id when other experiments appear or disappear
- Experiments are ORDERED by a published rule — sufficiency, creator
  breadth, observation count, presence of a 5A comparison, then a canonical
  tie-break — and every ordering input is emitted so the order can be
  recomputed by hand. Ordering is not scoring
- Material distinctness is judged on the INTERVENTION, never on the observed
  videos. Co-extensive patterns stay distinct experiments: different token
  values, token versus bigram, and different tags/categories/duration bands
  are all independently manipulable. Suppression fires only when two
  candidates canonicalize to the same intervention; the cap bounds volume
  instead. The survivor STATES that canonical value — title, hypothesis,
  instructions and experiment id are all built from it — so the collapse
  emits one intervention and one id whichever spelling became the
  representative, while the raw spelling is kept on the evidence as lineage.
  Case folding applies to prose only: a duration band is an enum identifier
- The emitted success criterion is executable: `evaluate_success_criterion()`
  applies it, and a test holds the wording and the implementation together.
  Unknown publication outcomes are excluded from the count rather than
  counted as below, fewer than MIN_TEST_PUBLICATIONS known outcomes yields
  no decision rather than a stop, and the decision is a count over a set so
  it cannot depend on evaluation order
- Never manufactures experiments to reach the cap. GenerationState says
  whether evidence was exhausted or the cap bound the result, alongside
  observed / eligible / below-floor / suppressed / generated counts
- 5B's creator-concentration safeguards propagate: SINGLE_CREATOR and
  CREATOR_CONCENTRATED become NARROW_CREATOR_BASE, CREATOR_PARTIALLY_UNKNOWN
  and CREATOR_UNKNOWN become CREATOR_IDENTITY_INCOMPLETE, and conflicting or
  absent creator identity is never counted as attribution
- Co-occurrence is reported as observation only. No causal or predictive
  language is emitted, enforced by a negation-aware guard over every emitted
  string and the full enum surface
- Click-through, retention, watch time, conversions, sales, revenue,
  profitability and algorithmic preference are never named as a measurement,
  enforced by a dedicated guard. The only nominated outcome is the public
  creator-relative view outcome 5A already uses
- No numeric score of any kind, no RED/YELLOW/GREEN, no POS or ECS change;
  `value` is permanently `None` and the dimension is
  `preliminary_content_experiments`. Derived experiments stay INFERRED
- 44 mutations of the milestone's boundaries were applied; 43 were killed and
  one is a documented equivalent mutant (a deliberate redundancy over a 5B
  guarantee that is itself pinned by a test)

### Remaining Milestone 5 scope
- (none; publishing, scheduling and analytics are outside Milestone 5)

## Milestone 6 — Step 7 / Step 8 (specified in SPEC_STEP_7_8.md)

### Milestone 6A-1 — Evidence Confidence inputs and `ecs_v1` (implemented)
- `app/services/evidence_confidence.py` derives the seven ECS components of
  SPEC_STEP_7_8.md §6.1 from stored evidence, dimension summaries and the
  capability outcomes orchestration already records. Pure and deterministic:
  no provider, no network, no persistence, no LLM, no route
- Three named, versioned, **explicitly uncalibrated** V1 policy sets, adopted
  as policy rather than derived from data and never described as validated:
  `sample_floors_v1` (U-4), `freshness_windows_v1` (U-5) and
  `provenance_directness_v1` (U-7). Recalibration ships as `_v2` rather than
  editing in place, so an earlier result stays attributable to the policy that
  produced it. Every result carries all four version strings
- `ecs_v1` is the unweighted mean of the counted components × 100, rounded to
  2dp. There are no weights: no symbol implements one, and permuting which
  component holds which score cannot move the aggregate. Each result emits its
  component values, applicability, exclusion clause, units counted and units
  excluded, so the number is recomputable by hand from its own output
- The anti-inflation rule is the load-bearing one. Evidence that was expected
  but is unavailable scores 0.0 and **stays in the denominator**; it never
  leaves the mean. A missing timestamp, a missing sample size, an unknown
  collection method, a degraded capability and a conflict all lower ECS, and
  no combination of missing evidence can raise it
- For a REQUIRED dimension, absence is missing expected evidence, never an
  inapplicable question: it contributes 0.0 to dimension coverage, sample
  adequacy, corroboration breadth and conflict rate alike. Case-(a) exclusion
  survives only where the metric cannot apply to a dimension that IS present —
  a dimension carrying no observations has nothing to agree or disagree about,
  and one that never reached a scoreable state is outside sample adequacy by
  §6. Omitting a required dimension is therefore never cheaper than reporting
  it badly, proved over 300 randomised dimension sets and pinned by the
  regression it came from
- Sample counts are per capability, not one scalar. A dimension spanning
  several capabilities carries counts in incompatible units — D6's keyword,
  listing and video counts measure three different things against three
  different floors — so `sample_sizes_by_capability` names each one and each is
  measured against its own floor. The scalar shorthand exists only where a
  dimension draws on exactly one capability and refuses D6. A capability that
  reported no count contributes 0.0 to its dimension's adequacy rather than
  leaving the average, so silence is never free. Counts are canonically
  ordered, and a count from a capability the dimension does not draw on, a
  duplicate, or a negative is refused rather than absorbed
- BLOCKED is not low confidence. A blocked result is `None` with a reason,
  never `0.0`, and is tested as distinct from a genuine floor-value 0.0
- `dimension_coverage` and `capability_health` are never excludable. A
  capability nobody requested is excluded from the health average (a non-attempt
  is not a failure), but if that empties the component it still counts, at 0.0:
  no attempted collection is no evidence of health
- Freshness reads `retrieved_at` only. `collected_at` is when this system
  stored the record, so using it as a fallback would let storage time
  masquerade as evidence recency; a record without `retrieved_at` scores 0.0
- The eight legacy per-item confidence floats on `EvidenceItem`
  (`freshness` defaulting to 1.0, `directness` to 0.5, …) are never read. A
  test pins both halves: that the defaults still exist, and that no attribute
  access reaches them
- ECS measures the evidence, never the opportunity. No value-bearing field is
  read: naming a signal type to route a record is not reading its magnitude,
  and the guard distinguishes the two
- Scope is verified before any record is read. Evidence belonging to another
  candidate or another research run yields SCOPE_MISMATCH rather than being
  silently mixed in
- Reachability sweeps cover `EcsState`, `Applicability`, `CapabilityHealth`
  and the component set. Two `DirectnessClass` values
  (DETERMINISTIC_DERIVATION, INDIRECT_PROVIDER_MEDIATED) are approved policy
  with no current production producer; a test records them as
  declared-but-unproduced rather than letting the table look fully exercised
- `VALIDATED_CACHE` was reachable from search demand alone when 6A-1 shipped;
  marketplace and public content passed the provider's own collection method
  through even on cache reuse, so their cache hits scored as direct API
  observations — an OVER-credit. **Closed by 6B**, which makes all three state
  reuse; the 6A-1 test that pinned the gap now pins its closure
- Boundary guards scan executed symbols via the AST, not raw file text. A text
  scan reads this module's own refusals ("no POS, no weights, no
  RED/YELLOW/GREEN") as violations, and `PROVENANCE_DIRECTNESS` contains the
  substring "proven"
- No POS, no `/score` activation, no deep collection, no new endpoint, no
  schema change. Legacy `app/services/scoring.py` remains quarantined
- 35 mutations of the milestone's boundaries were applied; all 35 were killed

### Milestone 6B — Deep collection, Step 7a (implemented)
- `app/services/deep_collection.py` re-runs the same three approved
  capabilities over the selected candidates at raised caps, through the
  `CapabilityCaps` seam that already existed. No new provider, no new external
  dependency, no new endpoint, no derivation — 7b belongs to 6C
- `deep_pass_caps_v1` (U-6) supplies all eight cap fields as a NAMED,
  VERSIONED, **explicitly uncalibrated** V1 collection budget, recorded on
  every result so evidence stays attributable to the budget that authorized
  it. A `None` anywhere would silently fall back to a cheap-pass default and
  make the pass deep in name only, so a test requires all eight
- One cap reads like a reduction and is not. `max_listings_per_query` (50 vs
  25) and `max_videos_per_query` (25 vs 10) are PER QUERY and compare
  directly. Every other cap is a whole-pass budget, and the passes cover
  different candidate counts: `max_keywords` at 100 is a smaller pass total
  than the cheap default of 200 and twice the depth per candidate — 100/5 = 20
  against 200/20 = 10. That is the §1 trade, and comparing pass totals
  directly reads a deepening as a cut
- **Limit-aware cache identity (§1.1).** The listing and video caches record
  the effective limit each entry was collected under, and reuse requires
  `cached_effective_limit >= requested`. A shallower entry is a DEPTH_MISS and
  is re-fetched. The stored limit is what was REQUESTED, never how many came
  back: a query returning three results under a limit of fifty was collected
  deeply, and recording three would turn a budget fact into a claim about the
  field. A depth miss yields no items at all, so the shallow set cannot leak
  into a deep pass as "the cache had fewer, so fewer exist". Re-fetching
  replaces the entry, so a later shallow request reuses it and a later deeper
  one still misses. The keyword cache is untouched: depth in search demand
  means more keywords queried, not more results per keyword
- **A depth miss is not a cache hit.** They are indistinguishable from
  outside — an entry was present — so `depth_miss_query_count` is reported
  separately from `cached_query_count` on every capability outcome and through
  the API, and a deep pass that looks suspiciously cheap can be diagnosed
  rather than trusted
- **Cache reuse is stated in provenance.** All three capabilities now record
  the shared `COLLECTION_METHOD_CACHE` constant on reused evidence instead of
  the provider's own method, which fixes the over-credit 6A-1 recorded. A
  listing or video reached by at least one live fetch IS a live observation,
  whatever else also returned it; only one seen solely through reused entries
  is cache-sourced. The constants live in `app/domain/enums.py`, the domain
  that owns `EvidenceItem.collection_method`, so providers depend on the
  domain and Evidence Confidence keeps its provider-free boundary
- No new deduplication rule (§1.1). Cheap and deep passes overlap by
  construction, and a re-observation produces a byte-identical payload and
  hash, which the existing 5B fingerprint mechanism already collapses
- Evidence stays immutable and run-scoped: the deep pass extends the run it is
  given rather than inventing one, and `research_run_id` is required rather
  than defaulted. A provider failure degrades only its own capability
- 23 mutations of the milestone's boundaries were applied; all 23 were killed.
  One survivor was found and fixed first: nothing asserted that a LIVE
  public-content fetch keeps the provider's own method, so marking every video
  record cache-sourced passed

### Milestone 6C — Deep research boundary object, Step 7b (implemented)
- `app/services/deep_research.py` derives the six Step 7 dimensions from the
  **union** of cheap-pass and deep-pass evidence for one `(candidate, run)`
  and emits SPEC_STEP_7_8.md §11's `DeepResearchResult`. `D2`–`D6` reuse the
  existing 4A/4B/4E/4F/4D derivations unchanged; only `D1` is new, because §2
  requires a distribution over observed keyword volumes and forbids the single
  0–100 composite the 3C bridge produces
- **Uniform, verified scope.** One candidate, one run, both checked before any
  evidence is read. A mixed-scope collection is refused unread rather than
  filtered — filtering silently answers a question nobody asked
- **§4's input contract per dimension**: raw observables, `DimensionState`,
  `missing_reason`, truth basis, sorted evidence ids, formula version,
  conflict count and sample size. Step 8 cannot be handed a number this module
  invented, because it computes none
- **Required and POS-eligible are different properties, and the sets
  overlap.** Competition structure and channel reach are REQUIRED — the
  dossier is incomplete without them and ECS coverage counts them — and both
  are contextual-only, POS-ineligible in V1 because their derivations refuse
  magnitude. Price evidence is optional AND contextual. Every dimension states
  all three flags, and a test asserts the POS-eligible and contextual sets
  partition the surface exactly
- **Conflict is disagreement, not repetition.** Two OBSERVED records of one
  entity (keyword, listing, video) reporting different values conflict: they
  are excluded from the derivation and counted. Identical repeat observations
  are duplicates, which the 5B fingerprint rule already collapses, and an
  UNKNOWN record never conflicts with an OBSERVED one — absence of a
  measurement is not a competing measurement
- MISSING, UNKNOWN and observed-zero stay three different things. A sample
  size of None means the dimension could not report one; it is never 0, and an
  observed zero volume is a real measurement that enters
- **`preliminary_rank` appears nowhere**, in any form. Rank is selection-only
  and carrying it forward would let triage policy leak into scoring, enforced
  by a field guard and an AST guard over the module's symbols
- Evidence Confidence (6A-1) is wired in over the dossier's own dimensions and
  capability outcomes, including per-capability sample counts, so richer
  evidence raises confidence and conflicts lower it
- `DeepResearchState` is COMPLETE / PARTIAL / SCOPE_MISMATCH. PARTIAL is not a
  low score: the dossier is still emitted in full, retaining what was
  computable and naming what was not
- The canonical `DIMENSION_ORDER` is load-bearing: the internal mapping is
  deliberately written in a different order, so the emitted order cannot
  quietly come from how the literal happened to be typed
- 24 mutations of the milestone's boundaries were applied; 23 were killed and
  one is a documented equivalent mutant — a redundant re-sort of dimension
  evidence ids, kept as defence in depth because every current derivation
  already sorts its own provenance
