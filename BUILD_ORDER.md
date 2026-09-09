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

## Milestone 4 — Product + pricing
- Narrow product generator
- Comparable price bands
- Buyer reach

## Milestone 5 — Faceless content intelligence
- YouTube creator baseline
- Robust outlier scoring
- Content pattern extraction
- 30 production-ready experiments
