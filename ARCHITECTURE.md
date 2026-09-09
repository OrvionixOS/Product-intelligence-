# V1 Architecture

## Core principle

The system stores evidence first and generates prose second.

Pipeline:

1. Seed niche
2. Generate ~20 candidate products
3. Batch cheap research
4. Normalize evidence
5. Preliminary score
6. Select top ~5
7. Deep research
8. Deterministic Opportunity Score + Evidence Confidence
9. RED/YELLOW/GREEN
10. Product generation
11. Pricing + buyer-channel analysis
12. Faceless content intelligence
13. 30 content experiments

## V1 providers

- Search demand: DataForSEO first, Google Ads later
- Marketplace: Etsy official API
- Public content: YouTube Data API

TikTok competitor intelligence is intentionally excluded from V1 unless a commercially licensed provider is added later.

## Non-negotiables

- Numeric scores are deterministic.
- LLMs never invent missing evidence.
- Missing values remain missing.
- Every evidence item has a truth class: OBSERVED, ESTIMATED, INFERRED, UNKNOWN.
- Evidence Confidence is separate from Opportunity Score.
- Old evidence snapshots are immutable.
- Every scoring result records its algorithm version.
