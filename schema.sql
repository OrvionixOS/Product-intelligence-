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

create table if not exists evidence_items (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  signal_type text not null,
  purpose text not null,
  truth_class text not null check (truth_class in ('OBSERVED','ESTIMATED','INFERRED','UNKNOWN')),
  provider text not null,
  collection_method text not null,
  source_reference text,
  source_url text,
  collected_at timestamptz not null default now(),
  geography text,
  language text,
  platform text,
  marketplace text,
  raw_value jsonb,
  unit text,
  normalized_value numeric(6,2) check (normalized_value between 0 and 100),
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
create index if not exists idx_evidence_provider_collected on evidence_items(provider, collected_at desc);
create index if not exists idx_scores_opportunity_calculated on score_versions(opportunity_id, calculated_at desc);
