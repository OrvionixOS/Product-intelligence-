create extension if not exists pgcrypto;

create table if not exists research_runs (
  id uuid primary key default gen_random_uuid(),
  seed_keyword text not null,
  geography text not null default 'US',
  language text not null default 'en',
  status text not null default 'PENDING',
  created_at timestamptz not null default now()
);

create table if not exists opportunities (
  id uuid primary key default gen_random_uuid(),
  research_run_id uuid not null references research_runs(id) on delete cascade,
  title text not null,
  problem_statement text,
  target_buyer text,
  product_format text,
  created_at timestamptz not null default now()
);

create table if not exists candidates (
  id uuid primary key default gen_random_uuid(),
  research_run_id uuid references research_runs(id) on delete cascade,
  seed_keyword text not null,
  title text not null,
  problem text not null,
  target_buyer text not null,
  proposed_format text not null check (proposed_format in (
    'PDF_GUIDE','WORKBOOK','CHECKLIST','TEMPLATE_PACK',
    'SPREADSHEET_TOOL','DATA_TEMPLATE','PRINTABLE_BUNDLE'
  )),
  buyer_outcome text not null,
  search_queries jsonb not null default '[]'::jsonb,
  marketplace_queries jsonb not null default '[]'::jsonb,
  content_queries jsonb not null default '[]'::jsonb,
  generation_reason text not null,
  status text not null default 'UNRESEARCHED' check (status in (
    'UNRESEARCHED','RESEARCHING','RESEARCHED','REJECTED'
  )),
  created_at timestamptz not null default now()
);

create table if not exists evidence_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  research_run_id uuid not null references research_runs(id) on delete cascade,
  provider text not null,
  started_at timestamptz not null,
  completed_at timestamptz,
  geography text not null,
  language text not null,
  status text not null check (status in ('PENDING','COMPLETE','PARTIAL','FAILED')),
  provider_call_count integer not null default 0,
  provider_cost numeric(12,6),
  provider_cost_is_estimate boolean,
  normalization_version text not null
);

create table if not exists evidence_items (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid references opportunities(id) on delete cascade,
  candidate_id uuid references candidates(id) on delete cascade,
  research_run_id uuid references research_runs(id) on delete cascade,
  snapshot_id uuid references evidence_snapshots(snapshot_id) on delete cascade,
  signal_type text not null,
  purpose text not null,
  truth_class text not null check (truth_class in ('OBSERVED','ESTIMATED','INFERRED','UNKNOWN')),
  provider text not null,
  collection_method text not null,
  source_reference text,
  source_url text,
  collected_at timestamptz not null default now(),
  retrieved_at timestamptz,
  geography text,
  language text,
  platform text,
  marketplace text,
  raw_value jsonb,
  unit text,
  normalized_value numeric(6,2) check (normalized_value between 0 and 100),
  sample_size integer,
  directness numeric(5,4) not null default 0.5,
  source_quality numeric(5,4) not null default 0.5,
  sample_adequacy numeric(5,4) not null default 0.5,
  freshness numeric(5,4) not null default 1.0,
  independence numeric(5,4) not null default 0.5,
  cross_source_agreement numeric(5,4) not null default 0.5,
  geographic_relevance numeric(5,4) not null default 1.0,
  marketplace_relevance numeric(5,4) not null default 1.0,
  known_limitations jsonb not null default '[]'::jsonb,
  provider_version text,
  normalization_version text not null default 'v0.1',
  raw_payload_hash text,
  created_at timestamptz not null default now()
);

-- Canonical marketplace listing observations (Milestone 3A). One row per
-- (provider, listing, retrieval): the deduped observation that per-candidate
-- evidence_items reference via raw_payload_hash. Review counts are purchase
-- PROXIES; exact sales/revenue are never stored because they are never known.
create table if not exists marketplace_listing_observations (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  listing_id text not null,
  title text,
  url text,
  price numeric(12,2),
  currency text,
  seller_id text,
  rating numeric(3,2),
  review_count integer,
  listing_created_at timestamptz,
  state text,
  taxonomy text,
  listing_type text,
  is_digital boolean,
  retrieved_at timestamptz not null,
  raw_payload_hash text not null,
  created_at timestamptz not null default now()
);

-- Canonical public-content video observations (Milestone 3B). One row per
-- (provider, video, retrieval): the deduped observation that per-candidate
-- evidence_items reference via raw_payload_hash. Public counts are audience
-- interest only; private metrics (watch time, retention, impressions, CTR,
-- subscriber conversion, sales, revenue) are never stored because they are
-- never known. Hidden statistics stay null — never zero.
create table if not exists public_content_video_observations (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  video_id text not null,
  title text,
  description text,
  published_at timestamptz,
  channel_id text,
  channel_title text,
  view_count bigint,
  like_count bigint,
  comment_count bigint,
  duration_seconds integer,
  tags jsonb not null default '[]'::jsonb,
  category text,
  channel_subscriber_count bigint,
  channel_video_count bigint,
  channel_view_count bigint,
  channel_stats_retrieved_at timestamptz,
  url text,
  retrieved_at timestamptz not null,
  raw_payload_hash text not null,
  created_at timestamptz not null default now()
);

create table if not exists score_versions (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  scoring_version text not null,
  confidence_version text not null,
  feature_vector jsonb not null,
  opportunity_score numeric(6,2) not null,
  evidence_confidence numeric(6,2) not null,
  classification text not null check (classification in ('RED','YELLOW','GREEN')),
  missing_dimensions jsonb not null default '[]'::jsonb,
  kill_rules_triggered jsonb not null default '[]'::jsonb,
  calculated_at timestamptz not null default now()
);

create index if not exists idx_opportunities_research_run on opportunities(research_run_id);
create index if not exists idx_candidates_seed_keyword on candidates(seed_keyword);
create index if not exists idx_candidates_status on candidates(status);
create index if not exists idx_evidence_opportunity on evidence_items(opportunity_id);
create index if not exists idx_evidence_candidate on evidence_items(candidate_id);
create index if not exists idx_evidence_snapshot on evidence_items(snapshot_id);
create index if not exists idx_snapshots_research_run on evidence_snapshots(research_run_id);
create index if not exists idx_evidence_provider_collected on evidence_items(provider, collected_at desc);
create index if not exists idx_scores_opportunity_calculated on score_versions(opportunity_id, calculated_at desc);
create index if not exists idx_marketplace_obs_provider_listing on marketplace_listing_observations(provider, listing_id, retrieved_at desc);
create index if not exists idx_content_obs_provider_video on public_content_video_observations(provider, video_id, retrieved_at desc);
create index if not exists idx_content_obs_channel on public_content_video_observations(channel_id, published_at desc);
