# Product Intelligence V1

Evidence-backed digital-product opportunity engine.

## Current state

Milestone 0 is implemented:

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

### Known technical debt

- **Unbounded in-memory research store.** `ResearchStore` is a process-wide,
  append-only, in-memory store with no eviction, so snapshots and evidence
  accumulate for the life of the process. Milestone 3C adds three snapshots
  per preliminary run rather than one, which reaches the limit sooner.
  Deferred deliberately: it is resolved by the persistence milestone that
  implements `schema.sql`, not by patching the in-memory seam.

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
