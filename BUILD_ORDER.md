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

### Milestone 4C — Product generator (not started)
- Narrow product generator

### Milestone 4D — Buyer reach (not started)
- Buyer reach

## Milestone 5 — Faceless content intelligence
- YouTube creator baseline
- Robust outlier scoring
- Content pattern extraction
- 30 production-ready experiments
