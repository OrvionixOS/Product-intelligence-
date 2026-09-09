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

- `POST /research/marketplace`: attach observable Etsy marketplace evidence to
  candidates via a typed batch `MarketplaceProvider` abstraction, with the
  official Etsy Open API v3 as the first adapter (no scraping)
- Field-level immutable evidence: listing price → `PRICE`, review count →
  `PURCHASE` (as a purchase **proxy**), listing presence/seller → `COMPETITION`,
  all `truth_class=OBSERVED`
- Query dedupe, result dedupe by listing id, caching, request/result/review
  caps, partial-failure handling, and telemetry, mirroring Milestone 2
- Deterministic summaries (`marketplace_features_v1`): purchase proxy, robust
  price quartiles, and observable competition features — no Opportunity Score,
  no "optimal price", no "low competition = good" judgment
- `GET /research/snapshots/{id}`: generalized snapshot retrieval for all
  research types

### Marketplace evidence limitations (OBSERVED vs proxy)

`OBSERVED` means the field was returned directly by Etsy's official API — a
listing price is an observed *asking* price, not a transaction record. A
review count is observed marketplace data but only a **purchase proxy**: it
proves some reviewed purchases occurred, never how many sales, units, or how
much revenue. Exact competitor sales and revenue are UNKNOWN and are never
inferred or invented — reviews are never converted into sales figures. Fields
Etsy does not return (ratings and review counts are absent from listing
search and require capped per-listing review lookups) stay null/UNKNOWN.

### Etsy credentials / app setup

1. Register an app at https://www.etsy.com/developers/register to get a
   keystring. New apps start with provisional "personal" access; Etsy's
   commercial-approval process is required for production/commercial use.
2. Set `ETSY_API_KEY` (see `.env.example`). The adapter uses only public v3
   application endpoints (`listings/active` keyword search and per-listing
   reviews) authenticated with the `x-api-key` header — no OAuth user grant
   and no scraping.

### Controlled live Etsy smoke test (manual, rate-limited)

```bash
export ETSY_API_KEY=...
export MARKETPLACE_MAX_PROVIDER_CALLS=3 MARKETPLACE_MAX_QUERIES=1 \
       MARKETPLACE_MAX_RESULTS_PER_QUERY=5 MARKETPLACE_MAX_REVIEW_LOOKUPS=2
uvicorn app.main:app &
# discover candidates, note research_run_id, then:
curl -X POST http://127.0.0.1:8000/research/marketplace \
  -H 'Content-Type: application/json' \
  -d '{"research_run_id": "<id>"}'
```

The caps limit the smoke test to one search plus two review lookups.

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

- `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` — required for real research
- `DATAFORSEO_BASE_URL` — optional API base override
- `SEARCH_DEMAND_MAX_PROVIDER_CALLS` — cap on provider HTTP calls per request (default 5)
- `SEARCH_DEMAND_MAX_KEYWORDS` — cap on unique keywords per request (default 200)

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

## Tests (fully mocked — no API credits spent)

```bash
pytest -q
```

All DataForSEO calls in tests go through `httpx.MockTransport` or fake
providers; automated tests never touch the network.

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
