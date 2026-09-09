# Product Intelligence V1

Evidence-backed digital-product opportunity engine.

## Current state

Milestone 0 is implemented:

- Evidence truth model
- Opportunity dimensions
- Deterministic Opportunity Score
- Independent Evidence Confidence
- GREEN/YELLOW/RED decision rules
- Provider interfaces
- FastAPI skeleton
- Unit tests

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
`quota_budget_exhausted` — never silently dropped or guessed.

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

## Tests (fully mocked — no API credits spent)

```bash
pytest -q
```

All DataForSEO, Etsy, and YouTube calls in tests go through
`httpx.MockTransport` or fake providers; automated tests never touch the
network.

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
