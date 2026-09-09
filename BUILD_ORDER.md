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

## Milestone 2 — Search-demand evidence (done)
- DataForSEO adapter
- Provider call/cost telemetry
- Cache keys + freshness windows
- (Etsy and YouTube adapters moved to Milestones 3A/3B)

## Milestone 3 — split into implementation slices
The originally broad "research orchestration" milestone is decomposed into
smaller slices. The intended capabilities are unchanged; only the delivery
order is split.

### Milestone 3A — Etsy marketplace evidence (done)
- Typed batch MarketplaceProvider contract
- Etsy official API adapter (no scraping)
- Purchase-proxy / price / competition evidence + deterministic summaries

### Milestone 3B — YouTube public-content evidence
- YouTube Data API adapter
- Audience/content evidence + deterministic summaries

### Milestone 3C — Research orchestration
- Preliminary research all candidates (all signal types in one run)
- Evidence normalization across signal types
- Evidence -> dimension bridge
- Preliminary ranking
- Top-five deep-research selection

## Milestone 4 — Product + pricing
- Narrow product generator
- Comparable price bands
- Buyer reach

## Milestone 5 — Faceless content intelligence
- YouTube creator baseline
- Robust outlier scoring
- Content pattern extraction
- 30 production-ready experiments
