# Product Intelligence V1

Evidence-backed digital-product opportunity engine.

## Current state

Milestones 0 through 4G and Milestone 5A are implemented. The foundation
below remains the Milestone 0 contract; later sections document the completed
research, derivation, product-specification, fit, and content-intelligence
slices:

- Evidence truth model
- Opportunity dimensions
- Provider interfaces
- FastAPI skeleton
- Unit tests
- A deterministic scoring *skeleton* — **not reachable, see below**

### Scoring is not implemented (removed from the API)

Milestone 0 produced the shape of a future Opportunity Score, Evidence
Confidence, and RED/YELLOW/GREEN classification. The shape is what Milestone
0 approved; **the numbers in it were never approved**. The POS dimension
weights, confidence weights, kill rules, and RED/YELLOW/GREEN thresholds in
`app/services/scoring.py` are placeholder v0.1 values that appear in no
approved repository specification and have never been validated against
evidence.

The `POST /score` endpoint that exposed them has therefore been
**removed**: it returns `410 Gone` for every request, is hidden from the
OpenAPI schema, and **there is no configuration that re-enables it**.
`app/api/routes.py` no longer imports the scoring module at all, so no
served route can reach it. Nothing in the application depended on it.

The module itself is retained rather than deleted, because the approved
final scoring engine may reuse or refactor that work. It survives as
importable library code with its Milestone 0 tests intact, and calling into
it raises a `DeprecationWarning`.

There is no Product Opportunity Score in this system today. ARCHITECTURE.md
places the real one at pipeline steps 8-9, after deep research — work that
has not been specified or built. When it is, it should be built fresh
against approved weights and thresholds, not by promoting these
placeholders. The Milestone 3C preliminary ranking is structurally separate
and cannot reach this module; tests enforce that statically and at runtime.

Milestone 1 (candidate discovery) is implemented:

- `POST /candidates/discover`: seed keyword in, ~20 structured candidates out
- Candidate generation behind a provider abstraction (`CandidateGenerationProvider`);
  a deterministic template provider ships as the V1 default
- Normalization, semantic dedupe, and supported-format validation
- Every candidate starts `UNRESEARCHED` — generation proposes, it never validates

Supported V1 formats: PDF guide, workbook, checklist, template pack,
spreadsheet/XLSX tool, CSV/data template, printable/reference bundle.
SaaS, apps, coaching, communities, courses, and video products are rejected.

Milestone 2 (search-demand evidence) is implemented:

- `POST /research/search-demand`: attach real search-demand evidence to
  candidates via a provider abstraction, with DataForSEO as the first adapter
  (`/v3/keywords_data/google_ads/search_volume/live`)
- Immutable evidence records (`purpose=SEARCH_DEMAND`, `truth_class=OBSERVED`)
  grouped under per-run evidence snapshots — later runs create new snapshots,
  never overwrite history
- Query deduplication across candidates, request batching, an in-process
  keyword cache, provider-call telemetry, cost fields, request caps, and
  graceful budget exhaustion (missing evidence is reported, never guessed)
- A deterministic candidate-level feature extractor
  (`search_demand_dimension_v1`, a documented log-scale V1 heuristic) — no
  Opportunity Score, no GREEN/YELLOW/RED in this milestone

Milestone 3A (Etsy marketplace evidence) is implemented:

- `POST /research/marketplace`: attach real Etsy marketplace evidence to
  candidates via a typed batch `MarketplaceProvider` abstraction, with the
  official Etsy Open API v3 as the first adapter (no scraping)
- Three evidence classifications per matched listing, each reflecting only
  what the field actually proves:
  - `purpose=PRICE`: the public asking price (`truth_class=OBSERVED` when
    returned)
  - `purpose=PURCHASE`: the listing's review count — explicitly a
    **purchase proxy**, never sales (`OBSERVED` when a real review lookup
    returned it, otherwise `UNKNOWN`)
  - `purpose=COMPETITION`: the listing's presence in the searched field
    (`OBSERVED`)
- Marketplace-query dedupe across candidates, listing dedupe by Etsy
  listing ID (a shared listing is one observation supporting many
  candidates), caching, query/result/review-lookup caps, provider
  telemetry, and graceful partial failure
- Deterministic candidate-level extractors (`marketplace_features_v1`):
  purchase-proxy summary, robust price quartiles (flagged
  `insufficient_evidence` below 5 paid comparables — no "optimal price"),
  and raw competition observables (no competition score, no
  "low competition = good") — no Opportunity Score changes in this slice
- Retrieve stored snapshots via
  `GET /research/marketplace/snapshots/{snapshot_id}`

### Etsy evidence limitations (OBSERVED vs. proxy)

`truth_class=OBSERVED` on marketplace evidence means the field was returned
by Etsy's official API — not that it is independently audited market truth.
The critical distinction:

- A public listing **price** is OBSERVED price evidence (an asking price,
  not a transaction price).
- A **review count** is OBSERVED marketplace data but only a **purchase
  proxy**: not every purchase produces a review, review timing lags
  purchases, and a review count is never units sold.
- **Exact competitor sales and revenue are never invented.** They are not
  public, so they remain `UNKNOWN` unless a seller later authorizes direct
  transactional access via OAuth. Review counts are never transformed into
  sales estimates.
- Etsy's listing search does not return per-listing ratings or review
  counts; review counts come from a separate capped lookup, and listings
  beyond the cap carry `UNKNOWN` review evidence (never zero). Ratings stay
  null until a real source returns them.

### Etsy credentials / app setup

1. Create an Etsy developer account and app at
   https://www.etsy.com/developers — the app's **keystring** is the API key.
2. New apps start with provisional (personal) access; **commercial use
   requires Etsy's app approval**. Public listing search needs only the
   keystring (sent as `x-api-key`); OAuth 2.0 is required solely for
   private seller data, which this milestone does not use.
3. Set `ETSY_API_KEY` in the environment (see `.env.example`). Credentials
   are never committed, logged, echoed in errors, or included in responses.

### Future controlled live Etsy smoke test (manual, uses real API quota)

```bash
export ETSY_API_KEY=...
export MARKETPLACE_MAX_PROVIDER_CALLS=2 MARKETPLACE_MAX_QUERIES=1 \
       MARKETPLACE_MAX_LISTINGS_PER_QUERY=3 MARKETPLACE_MAX_REVIEW_LOOKUPS=1
uvicorn app.main:app &
curl -s -X POST http://127.0.0.1:8000/candidates/discover \
  -H 'Content-Type: application/json' \
  -d '{"seed_keyword": "sourdough baking", "target_count": 2}' | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["research_run_id"])'
curl -X POST http://127.0.0.1:8000/research/marketplace \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<printed id>"}'
```

The caps above limit the smoke test to one listing search over at most three
listings plus one review lookup. Do not run it until the Etsy app has the
access level your usage requires.

Milestone 3B (YouTube public-content evidence) is implemented:

- `POST /research/public-content`: attach real public-content evidence to
  candidates via a typed batch `PublicContentProvider` abstraction, with the
  official YouTube Data API v3 as the first adapter
- What is collected per video (only when YouTube actually returns it):
  video ID, title, description, published date, channel ID/title, public
  view/like/comment counts, duration, tags, category, and — via a capped
  batched channel lookup — public subscriber/video/view counts per channel
- Three evidence classifications per matched video:
  - `purpose=AUDIENCE` / view count: `truth_class=OBSERVED` when returned,
    `UNKNOWN` when hidden — never zero-filled
  - `purpose=AUDIENCE` / engagement rate (likes÷views):
    `truth_class=INFERRED` — deterministically derived from OBSERVED
    fields, formula named in the evidence record
  - `purpose=CONTENT` / observation: the video's presence and metadata as
    content-pattern research input (`OBSERVED`)
- Deterministic extractors (`public_content_features_v1`): audience-interest
  features (video/channel counts, robust view/like/comment medians,
  recency, engagement) and `audience_interest_dimension_v1` (documented
  log-scale V1 heuristic mirroring the search-demand dimension; the
  10M-median-views saturation point is an explicit assumption)
- Content-outlier foundation (`content_outlier_v1`): creator-relative
  ratios — video views ÷ the median views of that channel's collected
  sample (≥3 videos required for a baseline). **No age term** — an old
  video and a new video with the same views get the same ratio. These are
  content-performance outlier observations, never "viral predictions"
- Query dedupe across candidates, video dedupe by ID (a shared video is one
  observation supporting many candidates), caching, caps, explicit **quota
  accounting** (YouTube's cost model: one search pass = 101 units, one
  channel batch = 1 unit; every response reports `quota_units_used`), and
  graceful partial failure
- Retrieve stored snapshots via
  `GET /research/public-content/snapshots/{snapshot_id}`

### YouTube evidence limitations (what is unavailable)

Private platform metrics are **not** public and are never estimated by
anything, LLMs included: watch time, retention, impressions, CTR,
subscriber conversion, traffic sources, sales, and revenue all remain
`UNKNOWN`. Hidden public metrics (disabled like counts, disabled comments,
hidden subscriber counts) stay null. Search results are a provider-ranked
sample, not the full content field. View counts measure content interest —
never buyers, purchases, or purchase intent.

### YouTube credentials / quota

Set `YOUTUBE_API_KEY` (a Google Cloud API key with the YouTube Data API v3
enabled; see `.env.example`). The default project quota is 10,000 units per
day; this service enforces a per-request quota budget
(`PUBLIC_CONTENT_MAX_QUOTA_UNITS`, default 1,000) and reports units used in
every response. Queries beyond the budget are reported as
`quota_budget_exhausted` — never silently dropped or guessed. Failed
requests are accounted too: quota the API is known to have charged is added
exactly, and when a failure makes exact consumption unknowable (e.g. a
transport error) the full operation cost is budgeted conservatively and the
response flags `quota_units_is_exact: false` — never zero merely because
the request failed.

### Future controlled live YouTube smoke test (manual, spends real quota)

```bash
export YOUTUBE_API_KEY=...
export PUBLIC_CONTENT_MAX_QUERIES=1 PUBLIC_CONTENT_MAX_VIDEOS_PER_QUERY=3 \
       PUBLIC_CONTENT_MAX_QUOTA_UNITS=102 PUBLIC_CONTENT_MAX_CHANNEL_LOOKUPS=1
uvicorn app.main:app &
curl -s -X POST http://127.0.0.1:8000/candidates/discover \
  -H 'Content-Type: application/json' \
  -d '{"seed_keyword": "sourdough baking", "target_count": 2}' | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["research_run_id"])'
curl -X POST http://127.0.0.1:8000/research/public-content \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<printed id>"}'
```

The caps above limit the smoke test to one search pass over at most three
videos plus one channel-stats batch — 102 quota units total.

Milestone 3C (research orchestration + preliminary ranking) is implemented:

- `POST /research/preliminary`: take the candidate set from Milestone 1,
  coordinate every evidence capability implemented through 3B, bridge the
  stored evidence into explicit preliminary dimensions, rank deterministically,
  and select the top five for the later deep-research stage
- Capabilities run in a fixed order and each one is independent: a provider
  that fails, is disabled, or has no credentials is reported as a capability
  outcome (`PROVIDER_FAILED` / `NOT_REQUESTED`) and the run continues on the
  remaining evidence. Each capability delegates to its existing runner, so its
  caps, cache, cost, and quota accounting are preserved unchanged
- Evidence bridge (`preliminary_dimensions_v1`): every preliminary dimension
  keeps its contributing evidence ids, providers, source references,
  truth basis, missing/UNKNOWN state, and named formula version

#### Preliminary dimensions

| Dimension | State | Formula |
| --- | --- | --- |
| `preliminary_search_demand` | scored | `search_demand_dimension_v1` |
| `preliminary_audience_interest` | scored | `audience_interest_dimension_v1` |
| `preliminary_price_evidence` | unscored observables | none approved |
| `preliminary_purchase_proxy` | unscored observables | none approved |
| `preliminary_competition_field` | unscored observables | none approved |

Only the two formulas already approved in this repository produce a 0-100
value. Marketplace evidence has no approved 0-100 formula, so it is bridged
as `EVIDENCE_PRESENT_UNSCORED` with its raw observable counts — no score is
invented. Dimension names are namespaced away from `ScoreDimensions` so
preliminary features can never be mistaken for, or silently wired into, the
final POS/ECS engine.

Four dimension states are distinguished and none of them is ever rewritten
as zero: `SCORED`, `EVIDENCE_PRESENT_UNSCORED`, `UNKNOWN` (evidence
collected, measurement unavailable), and `MISSING` (no evidence collected,
with a stated reason). Zero appears only where a formula explicitly defines
it as an observed value.

#### Preliminary ranking (`preliminary_rank_v1`)

Ranking is a **research-priority triage ordering**, not the Product
Opportunity Score, and makes no commercial claim about any candidate. It is
fully deterministic; an LLM never manufactures, adjusts, or assigns any
number in this path. Candidates are ordered by a fixed lexicographic
sequence of criteria — there is no weighted composite, because no dimension
weights are approved in the specification and 3C does not invent any:

1. `evidence_breadth` — count of dimensions backed by real evidence, desc
2. `search_demand` — `search_demand_dimension_v1` value, desc
3. `audience_interest` — `audience_interest_dimension_v1` value, desc
4. `price_comparables` — count of OBSERVED priced comparables, desc
5. `purchase_proxy_signals` — count of OBSERVED review-count proxies, desc
6. `title` — case-folded candidate title, asc (pure tie-break)
7. `candidate_id` — UUID string, asc (final deterministic tie-break)

A missing or UNKNOWN value sorts after every candidate that has a value on
that criterion, via an explicit has-value flag in the sort key — it is never
compared as zero, and an observed 0 outranks an absent measurement. The
first criterion that differs decides a pair, which is what makes every
ordering explainable: the response returns the deciding criterion and both
values for each adjacent pair.

> **This ordering is a V1 triage policy assumption, not an empirically
> validated opportunity-ranking formula.**
>
> Nothing about it has been tested against real outcomes. No evidence shows
> that a candidate ranked first is a better opportunity than one ranked
> fifth — only that it had broader existing evidence and, failing that,
> higher measured search demand. The criteria and their order encode one
> policy choice: spend expensive deep research where corroborating evidence
> already exists. Placing evidence breadth above search demand, and search
> demand above audience interest, is a judgment call, not a finding.
> Treating an observed zero as outranking an absent measurement is likewise
> a stated policy, not a validated rule.
>
> Rank position is a research-priority ordering only. It is not a score, not
> a verdict, not a prediction, and never a claim that a candidate is
> commercially validated. The ordering is expected to change once real
> outcome data exists; `preliminary_rank_v1` is versioned so that when it
> does, earlier results remain reproducible and attributable to this
> policy.

```bash
curl -X POST http://127.0.0.1:8000/research/preliminary \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id from /candidates/discover>"}'
```

Disable a capability by passing its provider as `null` (e.g.
`{"marketplace": null}`). `selection_size` defaults to 5.

#### What 3C deliberately does not do

No Product Opportunity Score, no Evidence Confidence Score, no
RED/YELLOW/GREEN classification, no kill rules, no POS weights, no ECS
thresholds, no ML, and no claim that any candidate is commercially
validated. Etsy review counts remain a purchase proxy and are never read as
sales; YouTube views and engagement remain audience interest and are never
read as purchase evidence; INFERRED values are never presented as OBSERVED.
A dimension value produced by a formula is itself a derivation, so its
`value_truth_class` is `INFERRED` even when every input was `OBSERVED`; the
inputs' own class is reported separately as `evidence_truth_basis`.

### Search-demand truth limitations

Search demand is **not** purchase evidence. Search volume measures search
interest — never sales, buyers, revenue, purchase intent, or validation.
`truth_class=OBSERVED` means the system observed the provider-supplied
measurement; the underlying number is a provider estimate, not exact market
truth. Fields the provider does not return are stored as null/UNKNOWN, never
invented. These limitations are recorded on every evidence item in
`known_limitations`.

### Provider replacement design

`SearchDemandProvider` (`app/providers/base.py`) is the batch research
contract. Adapters translate provider payloads into provider-agnostic
`KeywordDemandMetrics`; the domain and service layers never see
DataForSEO-specific response shapes. To add Google Ads API later, implement
the same interface and register it in `SEARCH_DEMAND_PROVIDERS`
(`app/api/routes.py`).

### Environment variables

Copy `.env.example` and fill in real values (never commit them):

- `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` — required for real search-demand research
- `DATAFORSEO_BASE_URL` — optional API base override
- `SEARCH_DEMAND_MAX_PROVIDER_CALLS` — cap on provider HTTP calls per request (default 5)
- `SEARCH_DEMAND_MAX_KEYWORDS` — cap on unique keywords per request (default 200)
- `ETSY_API_KEY` — required for real marketplace research
- `ETSY_BASE_URL` — optional API base override
- `MARKETPLACE_MAX_PROVIDER_CALLS` — cap on Etsy HTTP calls per request (default 10)
- `MARKETPLACE_MAX_QUERIES` — cap on unique marketplace queries per request (default 25)
- `MARKETPLACE_MAX_LISTINGS_PER_QUERY` — listings requested per query (default 25, max 100)
- `MARKETPLACE_MAX_REVIEW_LOOKUPS` — per-listing review-count lookups per request (default 20; 0 disables)
- `YOUTUBE_API_KEY` — required for real public-content research
- `YOUTUBE_BASE_URL` — optional API base override
- `PUBLIC_CONTENT_MAX_PROVIDER_CALLS` — cap on YouTube HTTP calls per request (default 20)
- `PUBLIC_CONTENT_MAX_QUERIES` — cap on unique content queries per request (default 25)
- `PUBLIC_CONTENT_MAX_VIDEOS_PER_QUERY` — videos requested per query (default 10, max 50)
- `PUBLIC_CONTENT_MAX_QUOTA_UNITS` — quota units spent per request (default 1000)
- `PUBLIC_CONTENT_MAX_CHANNEL_LOOKUPS` — batched channel-stats calls per request (default 2; 0 disables)

Milestone 4A (purchase evidence) is implemented:

- A deterministic Purchase Evidence extractor
  (`app/services/purchase_evidence.py`, `purchase_evidence_v1`) that derives
  purchase-proxy features from marketplace evidence **already collected** by
  3A and resolved by 3C. It makes no provider calls, spends no quota, and
  does not duplicate the 3A marketplace pipeline
- Derived for the selected (top-five) candidates and returned by
  `POST /research/preliminary` under `purchase_evidence`. No new endpoint
- Runs behind the 3C failure boundary as a `DerivationOutcome`: if the
  derivation fails, every other dimension and the ranking survive, the
  failure is explicit, and nothing is fabricated or zero-filled

#### What Purchase Evidence means

It answers one question: **what evidence exists that buyers actually spend
money on this type of product?**

It deliberately does not answer how many units a competitor sold, what a
competitor's revenue is, whether this product will sell, or the probability
of success. None of that is derivable from public marketplace data.

#### Direct evidence vs. public proxies

| Tier | Meaning | Status in 4A |
| --- | --- | --- |
| DIRECT / OBSERVED | Authorized transactional data for a seller: orders, receipts, transactions, refunds, revenue | **Not implemented.** Requires seller authorization this repository has no approved infrastructure for. The `PurchaseEvidenceSource.DIRECT_AUTHORIZED` interface is reserved only |
| PUBLIC PROXY | Review counts, review presence, listing longevity, paid comparables, distinct sellers, established products in the same problem/format area | What 4A actually uses |

#### Why review counts are proxies and never sales

A review count is one step removed from a purchase. Not every purchase
produces a review, review timing lags purchases, and the ratio between
reviews and purchases is unknown and is not estimated anywhere. These
conversions are forbidden and appear nowhere in the codebase:

```
review_count          ->  sales           FORBIDDEN
reviews               ->  revenue         FORBIDDEN
listing presence      ->  purchase count  FORBIDDEN
price x review_count  ->  revenue         FORBIDDEN
```

**Exact competitor sales and revenue are not public and remain UNKNOWN.**
Every Purchase Evidence result carries `exact_units_sold: UNKNOWN` and
`exact_revenue: UNKNOWN` as permanent, typed markers — they are TruthClass
fields and cannot hold a number.

#### Dimension states

The same four states 3C uses, and none is ever rewritten as zero:

- `MISSING` — no comparables were collected, or all were excluded as an
  unrelated (physical) format
- `UNKNOWN` — comparables were observed but no review count was measured for
  any of them. An absence of measurement, not an absence of purchases
- `EVIDENCE_PRESENT_UNSCORED` — real proxy features exist, **including the
  case where every observed review count is zero**, which is a measured
  absence and is distinct from UNKNOWN
- `SCORED` — reserved. 4A never produces it

#### No numeric score (deliberate)

**4A intentionally withholds a 0-100 value.** No purchase-evidence formula
is approved in this repository, and 3A/3C both declined to invent one for
marketplace evidence. Manufacturing a "sales score" from proxies would be a
fabrication, so the extractor returns the structured feature vector and
`EVIDENCE_PRESENT_UNSCORED` instead. `DimensionState.SCORED` stays available
for when a formula is specified and approved.

#### Market-validation pattern (`market_validation_pattern_v1`)

Describes the **shape** of proxy evidence, not its desirability. These are
deliberately not ranked — DISTRIBUTED is not "better" than CONCENTRATED:

| Pattern | Meaning |
| --- | --- |
| `NO_COMPARABLES` | Nothing observed |
| `UNKNOWN_PROXY` | Comparables exist, review counts unmeasured |
| `NO_PUBLIC_PROXY` | Review counts measured, all zero |
| `WEAK_PROXY` | Proxy evidence on very few listings or one seller |
| `CONCENTRATED` | Proxy volume dominated by a single seller |
| `MULTIPLE_SELLERS` | Several sellers carry proxy evidence, none dominant. Does **not** claim they are established — the classifier never checks listing age; `established_listing_count` reports that separately |
| `DISTRIBUTED` | Proxy evidence spans many distinct sellers |

`top_seller_proxy_share` is the share of **observed review counts** held by
the largest seller. It is **not market share, not revenue share, and not a
unit count**, and listings with no seller id are excluded from it rather
than merged into a fictional single seller.

#### Robust statistics

Marketplace review distributions are heavily skewed — one long-running
bestseller can hold more reviews than everything else combined. Arithmetic
means are avoided throughout. The extractor reports medians, quartiles, a
winsorized mean (capped at the 90th percentile), and robust counts, so one
extreme incumbent cannot dominate the dimension.

The winsorized mean is **withheld (null) below five observed review
counts**. With four values the 90th percentile sits beside the maximum, so
winsorizing would not deliver the robustness it implies and the number would
misrepresent itself as typical. Medians and quartiles, which are robust at
any sample size, are still reported.

#### V1 assumptions and thresholds

Every threshold below is an unvalidated **V1 assumption**, versioned by
`market_validation_pattern_v1`, appearing in no approved specification:

| Threshold | Value | Basis | Affects |
| --- | --- | --- | --- |
| `MIN_REVIEWS_FOR_PROXY` | 1 | **Definitional.** "At least one review exists" is the boundary between some evidence and none; any higher value would be pure judgment | pattern + counts |
| `MIN_SELLERS_FOR_MULTIPLE` | 2 | **Definitional.** "Multiple" cannot mean fewer than two | pattern |
| `ESTABLISHED_LISTING_MIN_AGE_DAYS` | 180 | **Unvalidated heuristic** | two reported counts only — not the pattern |
| `MIN_SELLERS_FOR_DISTRIBUTED` | 4 | **Unvalidated heuristic.** 3 or 5 would be equally defensible | pattern |
| `CONCENTRATION_DOMINANCE_THRESHOLD` | 0.6 | **Unvalidated heuristic.** No external benchmark is claimed | pattern |
| `WEAK_PROXY_MAX_LISTINGS` | 2 | **Unvalidated heuristic** | pattern |

Two of the six are definitional rather than arbitrary; the other four are
judgment calls that could reasonably be set differently. None is supported
by any approved specification, none has been validated against outcome data,
and no external benchmark is claimed for any of them. Changing any value is
a change to `market_validation_pattern_v1` and should be versioned as one.

#### Known limitations

- Purchase evidence describes a product **type** and a market, never a
  specific future product. It is not proof that a new product will sell and
  carries no probability of success.
- Marketplace search results are a provider-ordered sample, not the full
  market, so absence of comparables is weak evidence of absence.
- Physical listings are excluded from a digital product's comparables. A
  listing whose `is_digital` the marketplace did not report is kept and
  counted as unknown-relevance rather than silently dropped.
- Currencies are recorded for transparency only; review counts are
  currency-independent, so a mixed-currency market cannot alter any
  purchase-proxy statistic.
- Listing longevity depends on creation dates the marketplace may not
  return; listings without one are counted, never assigned an assumed age.

Milestone 4B (price evidence) is implemented:

- A deterministic Price Evidence extractor
  (`app/services/price_evidence.py`, `price_evidence_v1`) deriving
  per-currency asking-price bands from marketplace PRICE evidence **already
  collected** by 3A and resolved by 3C. No provider calls, no quota
- Derived for the selected candidates and returned by
  `POST /research/preliminary` under `price_evidence`. No new endpoint
- Runs as a second independent `DerivationOutcome` behind the same failure
  boundary as 4A: either derivation can fail without affecting the other,
  the dimensions, or the ranking

#### Asking price is not transaction price

Every figure is a public **asking price** — the number a seller displays.
Discounts, coupons, and sales are invisible in public listing data, so an
asking price is never a verified transaction price, never willingness to
pay, never revenue, and never an optimal or recommended price. **This
milestone recommends no price.** `transaction_prices`, `willingness_to_pay`,
and `recommended_price` are permanent `UNKNOWN` markers on every result.

#### Currencies are never combined

This repository has **no approved FX source**. Combining USD, EUR, and GBP
into one distribution would fabricate comparisons, so bands are computed
**per currency** and returned separately:

- every observed currency gets its own band, with its own count, quartiles,
  IQR, and dispersion;
- **no currency is discarded because another is more common** — 3A's
  dominant-currency collapse is deliberately not repeated here;
- a paid listing the marketplace priced without naming a currency belongs to
  no band and is reported as `paid_listings_without_currency`, never guessed
  into one;
- `cross_currency_comparison` is permanently `false`.

#### Free listings are evidence, not absence

A $0 listing is a free competitor and real OBSERVED evidence. It is counted
as `free_listing_count`, reported as a proportion of priced listings, and
**excluded from every paid-price statistic**. It is never missing data and
never a paid comparable. A listing with no OBSERVED price is `UNKNOWN` —
neither free nor zero.

#### Bundles are not unit-normalized

A $45 "50-template pack" and a $5 single printable are each one listing at
one asking price. Public listing data carries no trustworthy structured unit
quantity, so prices are **not** divided per item, template, or page. Both
remain separate observed prices and the limitation is reported rather than
hidden behind an invented divisor.

#### Statistics per band

Count, raw observed prices, min, P25, median, P75, P90, max, IQR,
coefficient of variation, a trimmed mean, plus established-listing and
purchase-proxy sub-population medians and seller counts.

Statistics that need a sample are **withheld rather than faked**: P90 below
10 paid comparables, the trimmed mean below 5, sub-population medians when
no listing qualifies, and the coefficient of variation unless mathematically
valid (at least two prices). Bands below 5 paid comparables are flagged
`insufficient_evidence`. **Raw observed prices are preserved alongside every
derived statistic**, so robust statistics never destroy the source
observations.

#### One shared definition of a paid comparable

4A and 4B reconstruct listings through one shared helper
(`app/services/marketplace_listing_view.py`), so they cannot silently
disagree. The canonical definition: **a paid comparable is a listing whose
price was OBSERVED and is greater than zero.**

This fixed a real pre-existing inconsistency — 3A excluded $0 from paid
comparables while 4A counted it as paid. 4A's `paid_comparable_count` now
excludes $0 listings, which is a correctness fix aligning it with 3A and
with the rule that a free listing is never a paid comparable.

#### V1 assumptions

4B reuses existing repository constants wherever one already covers the
question (`MIN_PRICE_COMPARABLES` from 3A;
`MIN_SAMPLE_FOR_WINSORIZED_MEAN`, `ESTABLISHED_LISTING_MIN_AGE_DAYS`, and
`MIN_REVIEWS_FOR_PROXY` from 4A). It introduces exactly **one** new sample
minimum, `MIN_SAMPLE_FOR_P90 = 10`, plus a 10% trim proportion — both
unvalidated V1 assumptions appearing in no approved specification.

#### Known limitations

- An observed band describes the **sampled** listings, not the whole market:
  marketplace search returns a provider-ordered sample.
- Asking prices cannot reveal discounting behaviour or actual paid amounts.
- Bundle size is unknown, so a band can mix incomparable units.
- Sub-population medians (established, purchase-proxy) inherit 4A's
  unvalidated age and review thresholds.

Milestone 4C (product generator) is implemented:

- A deterministic product-specification generator
  (`app/services/product_specification.py`, `product_specification_v1`)
  turning evidence **already collected** by 3A/3C into a build specification
  for ONE selected candidate. No provider calls, no LLM, no quota, no score
- Exposed on demand at `POST /product/specification`. It is **not** run
  automatically for the five ranked candidates: a caller selects a candidate
  and asks for a specification

#### Claim classes are local, conservative, and never upgraded

4C answers a different question from the evidence pipeline, so it uses its
own `SpecClaimClass` — `OBSERVED | INFERRED | ASSUMED | UNKNOWN`. The shared
`TruthClass` is untouched. The mapping is deliberately lossy in the safe
direction: **ESTIMATED becomes INFERRED, never OBSERVED.**

Every field carries its claim class, a written basis, the evidence ids
behind it, and the signal types involved. **A derived field is capped by the
weakest source it rests on** (`cap_claim_class`), so no derivation can
increase evidentiary certainty. A job classified from a Milestone 1
hypothesis is ASSUMED; a job derived from observed provider text is
INFERRED — never OBSERVED, because the buyer's actual job was not observed.

#### Two format answers, never conflated

Milestone 1 accepts seven product formats and explicitly rejects apps,
SaaS, courses, coaching, communities, and subscriptions. That validator is
**not** relaxed or reversed by 4C. Instead `format_selection_v1` returns two
separate answers:

- `ideal_format` — the best conceptual fit for the buyer's job, which may be
  a CALCULATOR, QUIZ_ASSESSMENT, MINI_COURSE, or MICRO_SAAS. It is
  **always ASSUMED**, flagged `outside_v1_build_capability` when this system
  cannot build it, and never implied to be market-validated;
- `buildable_v1_format` — the nearest of Milestone 1's seven approved
  formats, which is what V1 can actually produce.

Format is chosen on the job, not by copying incumbents. What the market
actually ships is reported as `observed_dominant_format` for contrast, and a
divergence is recorded as a conflict rather than resolved silently.

#### The job taxonomy is an approved but unvalidated assumption

`job_to_be_done_v1` classifies into CALCULATE, DECIDE, PLAN, TRACK, LEARN,
EXECUTE, ASSESS, and ORGANISE by deterministic keyword matching over
OBSERVED provider text. It is a documented V1 heuristic, **not an
empirically proven model of buyer behaviour**, and no token is shared
between two jobs. When signals do not separate the leading jobs, the result
is UNKNOWN and the tie is reported — the conservative answer, not a guess.

Matching is literal and word-boundary anchored: there is **no stemming or
lemmatization**, so an inflected form such as `calculators` does not match
the token `calculator`, and a genuine signal can be missed. The error runs
in the safe direction — a missed token lowers a job's score and pushes the
result toward UNKNOWN or toward a weaker claim class, never toward a
stronger claim than the evidence supports. Adding stemming would change
which candidates classify at all, so it is deferred to a calibrated
revision of the taxonomy rather than patched in.

#### Missing evidence stays missing

No evidence produces a `MISSING` specification with a `missing_reason`, not
an invented product. Sparse evidence still generates a specification but
flags `insufficient_evidence` and leaves unsupported fields ASSUMED or
UNKNOWN. Contradictory signals are preserved in `conflicts` with the
deterministic rule that resolved them.

#### The claim filter applies to the generator too

Milestone 1 text is a generation hypothesis and may carry marketing
language; Milestone 1's validator rejects unsupported *formats*, not
unsupported *claims*. So the forbidden-claim vocabulary gates the
deterministic generator as well as any future provider: a candidate titled
"Proven Best-Selling …" or promising "guaranteed revenue" yields **no**
product name or core promise, with the basis naming the pattern that was
matched. Matching is done on an NFKC-normalized, invisible-character-stripped,
homoglyph-folded copy of the text, so a look-alike character cannot smuggle a
claim past the filter.

#### The guardrails are V1 assumptions, not validated models

The protections described above are deliberate, documented guesses. The
stopword list, the forbidden-claim vocabulary, the Unicode normalization
and homoglyph-folding table, and the token and format mappings they support
are **unvalidated V1 assumptions**, arrived at by inspection and by
adversarial probing of this system — not by measuring buyers, and not from
any empirically validated model of market or natural language.

Concretely: the stopword list is not a linguistic corpus, the forbidden
vocabulary is not an exhaustive enumeration of every way a text can assert
an unsupported market claim, and the homoglyph table covers the confusable
characters that were tested rather than all of Unicode. They are a floor,
not a proof. Each is versioned so a later calibration is traceable, and
none should be cited as evidence that the system cannot state an
unsupported claim — only that the known ways of doing so are blocked.

#### Evidence reads are scoped to one research run

The store is append-only across runs, so a candidate researched twice
accumulates one set of records per run. `evidence_for_candidate` therefore
takes an optional `research_run_id`, and the endpoint always passes it:
an unscoped read would mix provenance and count a listing observed in two
runs twice, which can change the job classification. A record carrying no
run id is returned by every scoped read — it cannot belong to a different
run, and dropping it would silently discard stored evidence. In inline mode
the run stamp is taken from the evidence itself, never from the request.

#### What 4C never claims

It fabricates no customer problem, demand, purchase, sale, revenue, market
share, conversion rate, or willingness to pay. Price appears only as a
**citation of Milestone 4B**: observed ASKING prices, never a recommended,
optimal, or transaction price. Competitor product *contents* are not
collected, so differentiation ideas are explicitly design hypotheses rather
than observed gaps. Nothing here predicts that a product will sell.

#### The LLM seam is prose-only

`ProductSpecificationProvider` reserves a place for a future language model,
and the V1 default (`TemplateProseProvider`) is deterministic with **no live
LLM dependency**. Every decision is made before a provider is consulted. A
provider may reword `product_name` and `core_promise` and nothing else: it
cannot select or change a claim class, create or alter evidence, produce any
number, change the format decision, or convert an assumption into a finding.
Proposed prose is validated against forbidden market claims and rejected if
it smuggles one in.

Milestone 4D-0 (query provenance) is implemented:

- Evidence records retain the query that produced them
  (`app/services/query_provenance.py`, `query_provenance_v1`), closing a gap
  that would otherwise have forced Milestone 4D to guess why an observation
  was attached to a candidate

#### Three concepts, kept separate

| Concept | Where it lives |
| --- | --- |
| What was observed | `raw_payload` / `raw_payload_hash` |
| Which candidate it belongs to | `candidate_id`, `research_run_id` |
| **How it was found** | `originating_queries` |

`originating_queries` and `originating_query_shared` live **outside**
`raw_payload` and are excluded from `raw_payload_hash`. That is not a
stylistic choice: the hash is the identity of the observation, and every
downstream deduplicator keys on it. One listing reached by two different
queries is still one listing and must still hash identically — embedding
provenance in the payload would split that identity and inflate every
derived count.

#### Provider-request provenance, not the buyer's words

The stored string is the NORMALIZED query actually sent to the provider —
lowercased, whitespace-collapsed. It is not the original raw candidate
wording, and it is **not** a phrase any buyer typed. Nothing reconstructs a
query from candidate text.

#### Candidate-filtered attribution

One query is often generated for several candidates, and one observation is
often returned by several queries. Only queries generated for *this*
candidate appear on *this* candidate's evidence.
`originating_query_shared` reports whether any of them was also generated
for another candidate in the same run, so a shared query can never read as
evidence that it was uniquely generated for this candidate. It is a boolean
rather than a count on purpose: a number would invite being read as
popularity, demand, reach, or market strength, none of which query overlap
measures.

#### Unknown is not empty

`None` means provenance is UNKNOWN — evidence recorded before this
milestone, or by a path that does not capture it. `()` means provenance is
known and contains zero originating queries. These are different facts. The
SQL columns are nullable **with no default** precisely so a historical NULL
keeps meaning UNKNOWN; a `not null default '[]'` would silently reinterpret
history as "known to have zero queries". Nothing is backfilled.

#### Metadata, never support

Knowing how a record was found says nothing about how well it is evidenced.
Provenance never changes a truth class and can never upgrade UNKNOWN or
INFERRED to OBSERVED. A listing with no observed price still carries an
UNKNOWN price record, provenance attached.

#### Additive API change

The three snapshot endpoints (`GET /research/search-demand/snapshots/{id}`,
`GET /research/marketplace/snapshots/{id}`,
`GET /research/public-content/snapshots/{id}`) now expose two additional
optional fields on each element of `evidence[]`: `originating_queries` and
`originating_query_shared`. Both may be `null`. No existing field is
removed, renamed, or retyped, and 4A/4B/4C outputs are unchanged.

#### Provenance ordering is canonical

Milestone 4D-0.1 fixed a pre-existing nondeterminism: `extract_purchase_evidence`
and `extract_price_evidence` built `provenance.evidence_ids` and
`provenance.listing_ids` in evidence **arrival** order, so an equivalent
evidence set produced a different provenance record depending on the order
records happened to arrive. Measured on `89d28d59` before the fix: 100/100
shuffled permutations differed for both extractors. Only `provenance` moved —
`features` were byte-identical — but a specification that cannot be reproduced
byte-for-byte from the same evidence is not reproducible.

The invariant now holds:

- `provenance.evidence_ids` is sorted, and so is `ListingView.evidence_ids`;
- `provenance.listing_ids` is ordered by `(str(value), type name)`;
- every other provenance field already passed through `sorted()`.

Two rules make the fix safe. **`sorted()`, never `set()`** — an evidence id may
legitimately repeat, because a record carrying no payload hash is deliberately
never collapsed, and deduplicating to achieve ordering would silently change
deduplication semantics and contradict `duplicate_evidence_suppressed`.
**Order by `(str, type name)`, never `str()`** — identifier values keep whatever
type the provider payload carried; only the ordering is canonical, so a
mixed-type payload reaching the extractors through inline evidence orders
deterministically instead of raising `TypeError`. The type name breaks the one
tie `str()` alone leaves: two distinct values that render identically, such as
`1` and `"1"`, would otherwise fall back to arrival order under Python's stable
sort and stay nondeterministic.

The `views` list returned by `collect_listing_views` is deliberately **not**
sorted. It feeds feature computation, so leaving it in arrival order keeps
feature invariance true by construction rather than by test. Any future consumer
that exposes lineage canonicalizes its own provenance tuples.

Milestone 4D (buyer reach) is implemented:

- A deterministic Buyer Reach derivation (`app/services/buyer_reach.py`,
  `buyer_reach_v1`) over evidence **already collected** by 3A/3B/3C. No
  provider call, no quota, no new endpoint, no persistence
- Returned by `POST /research/preliminary` under `buyer_reach`, as a third
  independent `DerivationOutcome` behind the same failure boundary as 4A/4B

#### Channel evidence is not buyer evidence

This is the rule the milestone is built around. Relevant videos with real
engagement demonstrate an observable content audience *around a problem*; they
never establish that a viewer will buy. Etsy listings demonstrate marketplace
supply and public commercial proxies; they never establish buyer reach. A
keyword with an ad auction shows advertisers bid there; it says nothing about
purchase intent.

Buyer Reach therefore records **channels**, never buyers, and converts nothing:
views are not buyers, subscribers are not reachable buyers, listings are not a
buyer population, sellers are not market size, review proxies are not sales,
CPC is not purchase intent, and multi-channel presence is not a better
opportunity. `buyer_count`, `audience_size`, `market_size`,
`conversion_probability`, `addressability` and `guaranteed_distribution` are
permanent `UNKNOWN` markers on every result.

#### Four claim layers, never collapsed

| Layer | Question | Best attainable |
| --- | --- | --- |
| existence | did a provider return a concrete surface? | OBSERVED |
| relevance | is the surface linked to *this* candidate? | **INFERRED, capped** |
| activity | is the surface live or recent? | OBSERVED or UNKNOWN |
| commercial | does anyone transact there? | **cited from 4A only** |

Relevance is permanently capped at INFERRED. Milestone 4D-0 made its basis
auditable — every record carries the normalized provider query that returned it
— but a query is a Milestone 1 hypothesis, so relevance is a derivation, never
an observation. Commercial activity is not computed here at all: promoting a
purchase proxy to a channel would let a proxy read as a reach surface.

#### Missing evidence never becomes zero reach

Four facts stay distinguishable: the capability was not requested, the provider
failed, the provider raised something unexpected, or it ran and returned nothing
for this candidate. Only the last is an observed absence; the first three make
the channel picture UNKNOWN. Buyer Reach receives the capability outcomes
precisely so a failed marketplace call cannot read as "no marketplace channel".

#### What the pattern does and does not say

`reach_evidence_pattern_v1` reports NO_CHANNELS_OBSERVED, SINGLE_CHANNEL_CLASS,
MULTI_CHANNEL_CLASS, or CHANNELS_UNKNOWN. It describes the **shape** of the
evidence and nothing else — MULTI_CHANNEL_CLASS is not better, larger, or more
reachable than SINGLE_CHANNEL_CLASS.

`observed_across_multiple_providers` is deliberately literal rather than
"corroborated": different provider surfaces observe different things, they do
not verify one proposition.

#### The paid-auction signal

`paid_auction_observed` uses CPC and top-of-page bid data the search provider
already returns, with no additional call. It means **only** that an advertiser
auction was observably present. It is never purchase intent, conversion
likelihood, market attractiveness, or marketplace competition. `null` means
UNKNOWN — no keyword carried auction data — never "no auction".

#### Geography

Known only for search evidence. Marketplace and public-content records carry no
per-record geography, and it is never inferred from absent data.

#### Naming that cannot be conflated

4D reports `sellers_with_relevant_listing_count`: sellers with any
candidate-relevant listing. Milestone 4A's `distinct_seller_count` counts a
**different population** — sellers carrying review-proxy evidence. The names
differ so the two can never be silently conflated, and a regression test
demonstrates the difference.

#### Additive API change

`POST /research/preliminary` now returns one additional field, `buyer_reach`,
alongside the existing `purchase_evidence` and `price_evidence` arrays. No
existing field is removed, renamed, or retyped, no endpoint is added or
changed, and there is no schema migration.

`buyer_reach` is a **required** field on `PreliminaryResearchResponse`, because
the derivation always reports an outcome — including `NOT_REQUESTED` and
`DERIVATION_ERROR`, which are facts rather than absences and must not be
silently omitted. A client that tolerates additive response fields is therefore
unaffected. A schema-strict consumer — one validating the response against a
pinned schema, or asserting a byte-for-byte response body — will see one new
array and may need updating.

#### Known limitations

- **Top-level `missing_reason` is coarser than the channel level.** Two
  distinct facts — a channel-relevant field being absent from a record that
  *was* returned (`field_absent`), and a capability that ran and returned
  nothing for this candidate (`ran_empty`) — currently surface the same
  document-level `missing_reason`. They stay distinguishable per channel, so
  neither collapses into an observed zero and neither fabricates zero reach:
  one listing carrying no `seller_id` remains one listing whose storefront is
  unknown, never "zero sellers". This is a **granularity** limitation of the
  top-level summary field, not a correctness failure, and it is recorded here
  rather than fixed so that changing the field's values stays a deliberate,
  separately reviewed API change.
- **`endpoint_sample` is illustrative, not exhaustive.** Each channel's
  `endpoint_sample` is capped at 10 entries and ordered by identifier, never by
  size, activity, or any notion of quality. It exists to make a channel
  inspectable, and a caller must never read it as the full set of endpoints or
  infer anything from its length. `distinct_endpoint_count` remains the
  complete derived count for the evidence set and is unaffected by the cap.
- Relevance is permanently capped at INFERRED, so unrelated high-volume
  content linked only by a shared query is still not fully detectable.
  `channels_found_only_via_shared_queries` reports that dilution rather than
  correcting for it.
- Treating a distinct `seller_id` or `channel_id` as one distinct reachable
  endpoint is an unvalidated V1 assumption. One operator running several
  storefronts counts as several endpoints.

Milestone 4E (competition opportunity) is implemented:

- A deterministic Competition Opportunity derivation
  (`app/services/competition_opportunity.py`, `competition_opportunity_v1`)
  over marketplace evidence **already collected** by 3A and resolved by 3C.
  No provider call, no quota, no new endpoint, no persistence
- Returned by `POST /research/preliminary` under `competition_opportunity`,
  as a fourth independent `DerivationOutcome` behind the same failure
  boundary as 4A/4B/4D

#### Competition is not monotonic, and the code refuses to pretend otherwise

"Less competition is better" and "more competition is better" are both wrong,
and each is wrong in a way that would quietly corrupt every downstream
decision:

| Observation | One reading | The opposite reading |
| --- | --- | --- |
| few listings | an unserved need with room to enter | a need nobody found worth serving |
| many listings | demand real enough to sustain many sellers | a field where attention is already spent |
| one dominant seller | an incumbent proved a need nobody contested | an entrenched catalog no entrant displaces |
| many small sellers | a low barrier and no lock-in | a commoditized field with nothing to differentiate on |

So `competition_field_pattern_v1` reports the **shape** of the field, and
every pattern carries **both** readings, permanently paired and never ranked.
The refusal is structural rather than prose: no pattern maps to a number, no
ordering over the patterns exists, and AST guards assert that no comparison,
sort, or ranking expression in the module ever consumes one. A consumer that
wants a preference between two fields must supply the missing facts itself.

#### Observable evidence is not a conclusion

Saturation, entry difficulty, win probability, differentiation room,
competitor strength, competitor revenue and available market share all
require facts nobody here can observe: how many buyers exist, what they would
switch for, how strong each competitor actually is, and what the operator can
build. All seven are permanent `UNKNOWN` markers on every result.

#### Seven field shapes, none of them ranked

`UNKNOWN_FIELD`, `NO_LISTINGS_OBSERVED`, `SPARSE_FIELD`,
`FIELD_STRUCTURE_UNKNOWN`, `CONCENTRATED_FIELD`, `CROWDED_FIELD`,
`FRAGMENTED_FIELD`. The classifier's rule order is an explainability device,
not an order over the patterns it returns.

#### Structure is never inferred from a minority of the field

A structural claim requires at least `MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE`
(0.5) of the listings to carry a seller. Found by adversarially probing this
milestone's own implementation: a field of 20 listings where the marketplace
attributed only 2 — both to one seller — classified as `CONCENTRATED_FIELD`,
a structural finding built on 10% of the evidence with the other 90% silently
treated as if it did not exist. Computing the share over *all* listings
instead would commit the same error in the opposite direction, reporting an
unattributed field as not concentrated. Missing attribution is missing, so
below the floor the field is `FIELD_STRUCTURE_UNKNOWN` and
`seller_attribution_share` reports how much of the field the concentration
statistics actually describe.

Listings the marketplace did not attribute are never merged into one
fictional seller, and `top_seller_listing_share` is `null` — unknown, never
zero — when no listing carries one.

#### What 4E deliberately does not compute

Review counts, ratings and purchase proxies belong to 4A; prices and price
bands to 4B; reachable channels and endpoints to 4D. An AST guard asserts the
module never reads a price, review count or rating, so a competition finding
can never be another milestone's finding wearing a new name.

`competing_listing_count` and `seller_count_in_field` are, by construction,
the **same numbers** as 4A's `relevant_comparable_count` and
`distinct_seller_count` — both come from the shared `collect_listing_views`
and the same `is_format_relevant` predicate. They are restated so the
concentration features are interpretable without joining to 4A, **not** as
independent corroboration, and a regression test pins them together so the
two milestones can never disagree about what the marketplace showed.

`top_seller_listing_share` is a share of **listings** — supply structure. It
is not 4A's `top_seller_proxy_share`, which is a share of observed review
volume over a different population; a regression test builds a field where
one seller holds most listings while another holds most review volume, and
asserts the two statistics diverge.

Nothing temporal is computed. Listing age and established-listing counts
already exist in 4A answering a purchase question; a second age statistic
answering a competition question is deferred rather than duplicated.

Search-demand `competition` / `competition_index` is deliberately excluded
from V1: it measures **advertiser** bidding, not marketplace supply, and 4D
already reports auction presence as `paid_auction_observed`.

#### V1 assumptions

Every threshold is an unvalidated assumption chosen by inspection, calibrated
against no outcome data, and decides only which shape is reported — never
whether that shape is good: `SPARSE_MAX_LISTINGS` (3),
`DOMINANT_SELLER_LISTING_SHARE` (0.6), `CROWDED_MIN_LISTINGS` (12),
`CROWDED_MIN_SELLERS` (6), `MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE` (0.5).
Treating a distinct `seller_id` as one competitor is likewise an unvalidated
assumption: one operator running several storefronts counts as several
sellers.

#### Additive API change

`POST /research/preliminary` returns one additional field,
`competition_opportunity`. No existing field is removed, renamed, or retyped,
no endpoint is added or changed, and there is no schema migration. As with
`buyer_reach`, the field is **required** because the derivation always
reports an outcome — `NOT_REQUESTED` and `DERIVATION_ERROR` included — so
additive-tolerant clients are unaffected while schema-strict consumers will
see one new array.

#### Known limitations

- Marketplace search results are a provider-ordered sample, not the full
  market, so a sparse field is weak evidence of an unserved need and a
  crowded one is weak evidence of the field's true size.
- The patterns describe supply only. Nothing here observes demand, so no
  pattern can be read as a supply-versus-demand balance.
- `FIELD_STRUCTURE_UNKNOWN` reports that attribution was too thin to
  characterize the field. It is not a statement that the field is
  unstructured.
- Competitor performance is not public, so a dominant seller's strength is
  unknown and `CONCENTRATED_FIELD` says only that one seller holds most of
  the observable listings.

Milestone 4F (audience attention) is implemented:

- A deterministic Audience Attention derivation
  (`app/services/audience_attention.py`, `audience_attention_v1`) over
  public-content evidence **already collected** by 3B and resolved by 3C. No
  provider call, no quota, no new endpoint, no persistence
- Returned by `POST /research/preliminary` under `audience_attention`, as a
  fifth independent `DerivationOutcome` behind the same failure boundary

#### Attention is not interest, and interest is not demand

| Observation | What it is | What it is not |
| --- | --- | --- |
| a view | one playback event | a person, a buyer, or a purchase intent |
| a like | an interaction with a video | willingness to pay |
| a subscriber | context about a **channel** | the size of a candidate's audience |
| a viral video | one video that was watched | proof demand exists, or will persist |

`buyer_count`, `purchase_intent`, `candidate_audience_size`,
`demand_durability`, `willingness_to_pay`, `watch_time` and
`conversion_probability` are permanent `UNKNOWN` markers on every result.

#### Subscriber counts are not read at all

`VideoObservation` carries `channel_subscriber_count`, `channel_view_count`
and `channel_video_count`. This module reads none of them, and an AST guard
proves it. A subscriber count describes a creator's whole audience across
every topic they cover; treating it as a candidate's audience is the easiest
way to turn channel context into a fabricated market size, and the safest
guarantee is code that cannot reach the number.

#### Distribution and consistency, never a sum

Summing views is how one viral video makes a whole opportunity look
universally popular, so **no total-views feature exists**. What is reported
instead isolates the largest contributor rather than averaging it away:

| Feature | Question |
| --- | --- |
| `top_video_attention_share` | how much of the attention is one video's |
| `median_views_excluding_top_video` | what the field looks like without it |
| `videos_covering_half_of_attention` | how few videos account for half |
| `top_channel_attention_share` | the same question at creator level |
| `videos_within_band_of_median` | how many videos are typical rather than freak |

A field of one video at 50,000,000 views alongside 29 videos at 5 views
reports `SINGLE_VIDEO_ATTENTION`, a median of 5, and a
`median_views_excluding_top_video` of 5 — however large the outlier. Every
shape classification is **scale-invariant**: multiplying every view count
changes the medians and nothing else, because concentration is a ratio.

**Consistency is measured relative to this field's median.** A field whose
videos are uniformly ignored is perfectly consistent at a trivial level, so
`proportion_within_band_of_median` must always be read next to `median_views`
and never as a level of interest. Both are emitted together, alongside the
pattern naming the outlier, and a limitation string says so explicitly.

#### Eight shapes, none of them a level of demand

`UNKNOWN_ATTENTION`, `NO_CONTENT_OBSERVED`, `ATTENTION_UNMEASURED`,
`NO_OBSERVED_ATTENTION`, `SPARSE_SAMPLE`, `SINGLE_VIDEO_ATTENTION`,
`CONCENTRATED_ATTENTION`, `DISTRIBUTED_ATTENTION`. Each carries an
`AttentionClaim` with both what it observes and what it **does not
establish**, emitted together so a consumer cannot receive a pattern without
its boundary. The broadest pattern, `DISTRIBUTED_ATTENTION`, explicitly
disclaims that demand exists, that it would persist, or that any viewer would
buy anything.

#### Five facts about missing data, all distinguishable

Capability not requested, provider failed, unexpected provider error, ran and
returned nothing, and — kept separate from all four — videos observed whose
view counts were not. That last is `ATTENTION_UNMEASURED`, and it is never
the same as `NO_OBSERVED_ATTENTION`, which is attention measured at zero. A
video with no observed view count is not a video with zero views, and a share
of no observed attention is `null` rather than zero.

The evidence **record**, not the payload snapshot, is the authority: a view
count is read only from a record whose `truth_class` is OBSERVED, so a
payload claiming 999,999 views against an UNKNOWN record is unmeasured. The
reducer reads exactly three payload keys — `video_id`, `channel_id`,
`published_at` — and a test pins that set.

#### What 4F deliberately does not compute

Keyword search volume, CPC and auction data belong to search demand; channels
as reachable endpoints to 4D; listings, sellers, prices and review proxies to
4A, 4B and 4E. An AST guard asserts the module reads none of their fields.
`channels_with_observed_attention` requires an OBSERVED view count, so it is
deliberately **not** 4D's channel population, which counts creators with a
relevant video whether or not any metric was returned; a regression test
builds a field where the two numbers differ.

#### Relationship to the existing scored audience dimension

3B's `audience_interest_dimension_v1` produces a 0-100 value from median
views, which 3C exposes as the SCORED dimension
`preliminary_audience_interest` and the preliminary ranking consumes. **4F
neither changes nor replaces it**, and a regression test asserts it still
scores. 4F is a different layer with no value at all, named
`preliminary_audience_attention` so the two can never be confused. The
milestone is deliberately unscored: a single number derived from median views
is monotone in views, which is precisely the reading 4F exists to avoid.

#### V1 assumptions

Every threshold is an unvalidated assumption chosen by inspection, calibrated
against no outcome data, and decides only which shape is reported:
`SPARSE_SAMPLE_MAX_VIDEOS` (3), `SINGLE_VIDEO_DOMINANCE_SHARE` (0.6),
`CONCENTRATION_SHARE` (0.4), `DISTRIBUTED_MIN_CHANNELS` (3), and the typical
band of 0.5x-2.0x the median. The band is multiplicative because view
distributions are heavy-tailed, so a symmetric absolute band would classify
almost everything as atypical.

#### Additive API change

`POST /research/preliminary` returns one additional field,
`audience_attention`. No existing field is removed, renamed, or retyped, no
endpoint is added or changed, and there is no schema migration. As with
`buyer_reach` and `competition_opportunity`, the field is **required**
because the derivation always reports an outcome.

#### Known limitations

- Public content is a provider-ordered sample, so absence of attention
  measures the sample rather than the world.
- Attention statistics describe the videos that were returned. They describe
  no population of buyers, and no pattern is a level of demand.
- The sample is whatever the content queries returned. Relevance rests on
  those Milestone 1 hypotheses exactly as it does for 4D.
- Temporal spread (`publish_span_days`, `distinct_publish_months`) records
  when content appeared. It is never evidence that attention persisted.

Milestone 4G (problem/product fit evidence) is implemented:

- A deterministic Problem/Product Fit assessment
  (`app/services/product_job_fit.py`, `product_job_fit_v1`) over a Milestone
  4C specification and the structured evidence behind it. No provider call,
  no quota, no new endpoint, no persistence
- Returned by `POST /product/specification` under `product_job_fit`, behind
  its own failure boundary: a bug in the assessment degrades the assessment
  alone and never breaks an endpoint that already produced a specification

#### Four things kept strictly apart

| | |
| --- | --- |
| observed market evidence | what a provider actually returned |
| inferred problem/job | Milestone 4C's `job_to_be_done_v1` derivation |
| generated specification | Milestone 4C's template-generated document |
| fit assessment | this milestone |

4G is the **first milestone permitted to consume 4C**, because its whole
purpose is to evaluate the relationship between the proposed product and the
evidence-backed job. That permission comes with the obligation that makes it
safe: consuming generated text must never launder it into evidence. This
module reads only 4C's structured result — claim classes, job scores, format
enums, conflict topics, which fields are UNKNOWN — and never a generated
string. A test plants a distinctive invented phrase in a specification, in
both the field-known and field-UNKNOWN paths, and asserts it appears nowhere
in the serialized assessment.

Every claim is capped through **4C's own `cap_claim_class`** against the
inputs it rests on, so a fit claim is never stronger than its weakest link,
and the assessment itself is always INFERRED even when every contributing
record was OBSERVED.

#### What is assessable, and what would be a tautology

Milestone 4C chooses a format **from** the job via `JOB_TO_IDEAL_FORMAT`, so
asking "does the format match the job?" is answered YES by construction and
measures nothing. 4G assesses the **support chain** that choice rests on:

1. is the job backed by observed provider text, or only by candidate text?
2. was the classification close enough that the format choice was arbitrary?
3. is the ideal format buildable — and if not, does the V1 substitute
   preserve what the job actually needs?
4. is the specification complete enough for any of this to mean anything?

`fit_assessment_pattern_v1` reports the **first break** in that chain. The
complete set of findings is emitted separately as `observations`, each with
its own claim class and its own `does_not_establish` line, so summarizing
loses nothing.

#### Structural fit is derived, not asserted

`interaction_mode_v1` states how each format is used — `STATIC_REFERENCE`,
`FILL_IN`, `SEQUENCED_EXECUTION`, `ARTEFACT_PRODUCTION`,
`REPEATED_COMPUTATION`, `TIME_SEQUENCED_DELIVERY`, `ONGOING_SERVICE`,
`ONGOING_INTERACTION` — and substitution fidelity follows from comparing the
two modes rather than being asserted pair by pair:

| Substitution | Fidelity | Why |
| --- | --- | --- |
| `PDF_GUIDE` → `PDF_GUIDE` | `DIRECT` | no substitution happened |
| `CALCULATOR` → `SPREADSHEET_TOOL` | `MODE_PRESERVED` | both perform repeated computation |
| `TEMPLATE_PACK` → `PDF_GUIDE` | `MODE_CHANGED` | producing an artefact becomes reading one |
| `COMMUNITY` → `PDF_GUIDE` | `MODE_UNMET` | ongoing interaction is not something a document provides |
| `MINI_COURSE` → `WORKBOOK` | `MODE_UNMET` | a schedule is not a set of pages |

The three modes no V1-buildable artefact can provide —
`TIME_SEQUENCED_DELIVERY`, `ONGOING_SERVICE`, `ONGOING_INTERACTION` — are
listed explicitly, and a test asserts no buildable format claims one.

#### Weak or ambiguous job classification is exposed, not inherited

4C classifies a job on a lead of one token over the runner-up. A win by
exactly that margin is a real classification that was one token from going
the other way, so 4G reports `job_is_ambiguous` with the margin and names
every competing job within it — and because the format follows the job, that
is also a statement that the recommended format would have differed. A
specification whose job came only from the candidate's own wording reports
`JOB_ASSUMED_NOT_OBSERVED`: the fit assessed is fit to a hypothesis.

#### What 4G never claims

`sales_probability`, `conversion_probability`, `product_market_fit`,
`willingness_to_pay`, `market_size`, `expected_revenue`,
`usefulness_to_buyer` and `buyer_demand_proven` are permanent `UNKNOWN`
markers. A test scans every string the API returns, across every pattern, for
commercial-success vocabulary. Even the strongest pattern,
`ALIGNED_WITH_OBSERVED_JOB`, states in its own boundary that it is not
product-market fit, not demand, and not evidence the product would sell — a
product can suit a job perfectly and sell nothing.

Search volume, views, review counts, listing counts and price magnitudes are
**not read at all**, proven by an AST guard. They can make a market look
bigger; they can never make a product structurally more suitable for a job.

#### V1 assumptions

`AMBIGUITY_MAX_LEAD` (1) and the `interaction_mode_v1` taxonomy itself are
unvalidated assumptions chosen by inspection, calibrated against no outcome
data. They describe structure; they never rank product quality. Milestone
4C's own unvalidated job taxonomy and format-selection heuristic are
inherited wholesale, so 4G is at most as good as they are.

#### Additive API change

`POST /product/specification` returns one additional field,
`product_job_fit`. No existing field is removed, renamed, or retyped, no
endpoint is added or changed, and there is no schema migration.

#### Known limitations

- The job being fitted is 4C's INFERRED derivation, not an observed fact
  about buyers. Fit to an inferred job is inferred fit.
- The specification being assessed is generated. Consuming it never makes its
  text evidence.
- A recommendation that diverges from the observed market is reported as a
  fact, and is neither a defect nor an advantage.
- Structural suitability says nothing about execution quality: a well-fitted
  format can still be built badly.

Milestone 5A (robust creator-relative outlier evidence) is implemented:

- `app/services/faceless_content_intelligence.py` is a pure derivation over
  already-collected Milestone 3B public-content evidence. It makes no
  provider or network calls, writes no persistence records, and adds no
  endpoint. It is deliberately separate from the existing
  `content_outlier_v1` foundation, which is unchanged.
- The derivation is scoped to one candidate and one research run. It accepts
  only `purpose=AUDIENCE` / `signal_type=public_video_view_count` evidence,
  and the evidence record is authoritative: a payload value cannot upgrade
  an UNKNOWN record.
- Only `TruthClass.OBSERVED` view counts contribute to creator baselines or
  scores. Unknown view counts remain UNKNOWN; observed zero remains a real
  zero and is never converted to missing or adjusted with an epsilon.
- A creator baseline requires at least three unique observed-view videos and
  is their median view count. The separately versioned
  `robust_content_outlier_v1` relative score is exactly
  `log2(video_views / creator_median_views)` and is calculated only when both
  values are strictly positive.
- A zero baseline, missing video/channel identity, insufficient creator
  sample, conflicting observations, and unavailable/invalid view counts have
  explicit non-scoreable states. Missing channel ids never form a synthetic
  creator.
- Publication time and channel aggregate metrics are not read. The result
  carries no candidate-level numeric value and establishes no demand, buyer,
  conversion, market-size, sales, revenue, or commercial conclusion.
- `tests/test_faceless_content_intelligence.py` covers formula correctness,
  scope isolation, UNKNOWN-versus-zero semantics, zero baselines, identity
  boundaries, duplicate suppression, canonical provenance, shuffle
  invariance, forbidden fields, and the no-network contract.

### Known technical debt

- **Unbounded in-memory research store and its indexes.** `ResearchStore` is
  a process-wide, append-only, in-memory store with no eviction, so snapshots
  and evidence accumulate for the life of the process. Milestone 3C adds three
  snapshots per preliminary run rather than one, which reaches the limit
  sooner. Milestone 4C adds a second unbounded structure alongside the
  existing snapshot index: `_evidence_by_candidate`, which grows with every
  stored record and is never pruned. Both the store and **both** of its
  indexes are in-memory only — nothing survives a process restart, and
  reading a candidate's history across runs relies entirely on that
  process-local index. Deferred deliberately: the store and its indexes must
  be replaced or redesigned by the persistence milestone that implements
  `schema.sql`, not patched in the in-memory seam.
- **`_id_sort_key` is duplicated across three modules.** The deterministic
  identifier-ordering helper — `(str(value), type(value).__name__)`, the key
  that gives mixed-type identifiers a total order without coercing either
  value — now exists independently in `app/services/purchase_evidence.py`
  (4A), `app/services/price_evidence.py` (4B), and
  `app/services/buyer_reach.py` (4D). This is **maintainability debt only**:
  all three compute the same key today — the bodies are character-identical
  and only the docstrings differ — and every affected derivation is
  deterministic, verified by each milestone's shuffle-invariance tests. The
  risk is drift — a future change to one copy that does not reach the others
  would make two milestones order identifiers differently from a third.
  Deferred deliberately: consolidating it touches three shipped derivations at
  once and belongs in a change reviewed for that, not in a documentation pass.
- **`_id_sort_key` now exists in a fifth module.** Milestone 4F adds its own
  copy in `app/services/audience_attention.py`. The entries above apply
  unchanged; consolidating now touches five shipped derivations.
- **Two evidence reducers with the same shape.** `collect_listing_views`
  (marketplace) and `collect_video_views` (public content) implement the same
  pattern — dedupe by entity id, gate values on the evidence record's truth
  class, canonicalize `evidence_ids`, count suppressed duplicates — against
  different signal types. 4F deliberately kept its reducer local rather than
  generalising the marketplace one, because generalising would have touched
  4A, 4B and 4E in a milestone that adds a derivation. If a sixth consumer
  appears, extract the shared shape then rather than growing a third copy.
- **The scored 3C audience dimension and the unscored 4F derivation
  coexist.** `preliminary_audience_interest` remains SCORED via
  `audience_interest_dimension_v1`, a value monotone in median views that the
  preliminary ranking consumes, while `preliminary_audience_attention`
  deliberately carries no value. Both are correct for what they are, but a
  consumer reading only the scored dimension gets exactly the
  more-views-is-better reading 4F exists to avoid. Reconciling them means
  changing ranking behaviour, which is out of scope for a derivation
  milestone and belongs in the scoring milestone.
- **4G is reachable only through `POST /product/specification`.** Unlike
  4A/4B/4D/4E/4F, which run as derivations inside `POST /research/preliminary`,
  the fit assessment needs a 4C specification and 4C is not part of the
  preliminary orchestration. Wiring it there would mean generating a
  specification per selected candidate inside the research run, which is new
  behaviour rather than a derivation over stored evidence. Deferred
  deliberately; revisit if a consumer needs fit for every ranked candidate.
- **4G inherits two unvalidated 4C taxonomies wholesale.** `job_to_be_done_v1`
  and `format_selection_v1` decide the job and the format that 4G then
  assesses, so a systematic error in either propagates into every fit result.
  4G reports the weakness (ambiguity margins, ASSUMED classifications) but
  cannot correct it. Any future calibration of the job taxonomy must re-run
  4G's reachability tests, because a change in claim-class behaviour is what
  made an earlier version of the classifier dead code.
- **`_id_sort_key` now exists in a fourth module.** Milestone 4E adds its own
  copy in `app/services/competition_opportunity.py`, for the same reason and
  with the same body as the 4A/4B/4D copies. The entry above applies
  unchanged; consolidating it now touches four shipped derivations.
- **Identifier types are canonical in the services and `str` at the API
  boundary.** Milestone 4D-0.1 established that identifier values keep
  whatever type the provider payload carried and only their ORDER is
  canonical, and 4E's service layer honours that — a mixed `int`/`str`
  seller id orders deterministically rather than raising. The response
  models, however, declare `listing_ids: list[str]` and `seller_ids:
  list[str]`, so a non-string identifier reaching the API layer would fail
  response validation. This is pre-existing and repository-wide: 4A and 4B
  declare their identifier lists the same way. It is unreachable through
  `POST /research/preliminary`, where identifiers come from
  `MarketplaceListing.seller_id` typed `str | None`. Deferred deliberately:
  widening the declared types is one change across three shipped response
  schemas, not a 4E-local fix.
- **Structural features restate two 4A counts.** `competing_listing_count`
  and `seller_count_in_field` are the same numbers as 4A's
  `relevant_comparable_count` and `distinct_seller_count`, by construction
  rather than by coincidence. A regression test pins them equal, so the
  duplication is a maintained invariant rather than a drift risk, but a
  future consumer joining both derivations will see each count twice.
- **Top-level `missing_reason` granularity, again.** As with 4D, 4E's
  document-level `missing_reason` does not distinguish a capability that ran
  and returned nothing from one whose listings were all excluded as an
  unrelated format — those carry distinct reasons, but a field absent from an
  individual payload (a listing with no `seller_id`) is visible only in
  `seller_attribution_share`, not in `missing_reason`. A granularity
  limitation, not a correctness failure: no absence becomes a zero.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

## Discover candidates

```bash
curl -X POST http://127.0.0.1:8000/candidates/discover \
  -H 'Content-Type: application/json' \
  -d '{"seed_keyword": "sourdough baking", "target_count": 20}'
```

The response includes a `research_run_id` you can feed to search-demand
research.

## Research search demand

```bash
curl -X POST http://127.0.0.1:8000/research/search-demand \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id from /candidates/discover>", "location": "US", "language": "en"}'
```

Alternatively pass `"candidates": [...]` (the objects returned by discovery)
instead of `research_run_id`. Retrieve a stored snapshot with its evidence via
`GET /research/search-demand/snapshots/{snapshot_id}`.

## Research marketplace evidence (Etsy)

```bash
curl -X POST http://127.0.0.1:8000/research/marketplace \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id from /candidates/discover>"}'
```

The response contains snapshot metadata (with provider call telemetry),
per-candidate purchase-proxy/price/competition summaries, evidence
references, provider errors, and explicit missing/unknown evidence. Retrieve
a stored snapshot via `GET /research/marketplace/snapshots/{snapshot_id}`.

## Research public content (YouTube)

```bash
curl -X POST http://127.0.0.1:8000/research/public-content \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id from /candidates/discover>"}'
```

The response contains snapshot metadata, quota telemetry
(`quota_units_used`), per-candidate audience-interest summaries with
content-outlier observations, evidence references, provider errors, and
explicit missing/unknown records. Retrieve a stored snapshot via
`GET /research/public-content/snapshots/{snapshot_id}`.

## Generate a product specification (Milestone 4C)

```bash
curl -X POST http://127.0.0.1:8000/product/specification \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id from /candidates/discover>", "candidate_id": "<one selected candidate>"}'
```

Derived from evidence already stored for that candidate: no provider calls,
no LLM, and no score. The response separates `ideal_format` from
`buildable_v1_format`, carries per-field claim classes with their basis and
evidence ids, and reports `conflicts`, `assumptions`, and `unknowns`
explicitly. Pass `"candidate": {...}` (with optional inline `"evidence"`)
instead of the run/candidate pair to specify a candidate directly.

## Tests (fully mocked — no API credits spent)

```bash
pytest -q
```

All DataForSEO, Etsy, and YouTube calls in tests go through
`httpx.MockTransport` or fake providers; automated tests never touch the
network. Milestone 3C adds no live calls and no Etsy or YouTube smoke
tests — its orchestration tests drive in-memory fake providers only.

## One real DataForSEO smoke test (manual, costs real credits)

```bash
export DATAFORSEO_LOGIN=... DATAFORSEO_PASSWORD=...
export SEARCH_DEMAND_MAX_PROVIDER_CALLS=1 SEARCH_DEMAND_MAX_KEYWORDS=3
uvicorn app.main:app &
curl -s -X POST http://127.0.0.1:8000/candidates/discover \
  -H 'Content-Type: application/json' \
  -d '{"seed_keyword": "sourdough baking", "target_count": 2}' | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["research_run_id"])'
curl -X POST http://127.0.0.1:8000/research/search-demand \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<printed id>", "location": "US", "language": "en"}'
```

The caps above limit the smoke test to a single provider call over at most
three keywords.

Run tests:

```bash
pytest -q
```
