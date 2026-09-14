# Milestone 6A-SPEC — Step 7 / Step 8 Normative Specification

**Status:** PROPOSED. Not approved. Not implemented.
**Base:** `main` @ `1ad4c617fcac15908e87287f7be3f7088162bdc3`
**Scope:** specification only. No runtime behaviour changes, no scoring, no `/score`,
no new providers, no revival of legacy scoring constants.

Every decision below is stated as **Repository evidence → Decision → Consequence**.
Where a product-policy choice is being made, it is labelled POLICY and the
alternative that was rejected is named.

---

## 1. Step 7 purpose

**Repository evidence.** `ARCHITECTURE.md:15` contains the single line
"7. Deep research" and nothing else. `RQ1`–`RQ18` appear zero times in the
repository. No provider call occurs after selection: `app/api/routes.py` calls
exactly three service functions (`discover_candidates:489`,
`run_preliminary_research:1826`, `generate_product_specification:2372`).
`ARCHITECTURE.md:11` names step 3 "Batch **cheap** research", so the contrast
with step 7 is explicit in the only normative document that exists.
`research_orchestration.py:227-241` already defines `CapabilityCaps` with eight
per-capability limits "forwarded verbatim to that capability's runner".

**Decision.** Step 7 is defined as:

> **Deep Research is a second, deeper evidence pass over the ~5 selected
> candidates only, using the same three approved providers at raised caps,
> followed by deterministic derivation of the Step 7 dimensions from the
> enlarged evidence set.**

Two components, both required:

- **7a — Deep collection.** The same capabilities as step 3
  (search demand, marketplace, public content), re-run for selected candidates
  with explicitly raised `CapabilityCaps`. No new provider, no new external
  dependency. The cheap pass is cheap because it is capped across ~20
  candidates; depth is bought by spending the saved budget on 5.
- **7b — Deep derivation.** The Step 7 dimensions (§2), derived deterministically
  from the union of step-3 and step-7a evidence for that candidate and run.

**Consequence.** Step 7 is not a greenfield capability build. Six of its
dimensions already exist as implemented derivations (4A, 4B, 4D, 4E, 4F and the
3C search-demand bridge). What Step 7 adds is (i) evidence depth and (ii) a
uniformly scoped, persisted boundary object (§11). This is the single most
important finding carried from the 6A-0 audit into this specification.

**POLICY — rejected alternative.** Step 7 could have been defined as
derivation-only over step-3 evidence, which is what the repository does today.
Rejected: the same evidence cannot answer a question at two different depths,
and a "deep" stage that collects nothing makes the step-3/step-7 distinction
vacuous. If the reviewer prefers derivation-only, §14 U-1 records the
consequence.

---

## 2. Step 7 dimensions

Dimensions are named in a new `deep_` namespace. They are **not** derived from
`ScoreDimensions`; the audit established that no scoring dimension in the
repository is approved (`scoring.py:1`, `SCORING_STATUS = "UNAPPROVED_EXPERIMENTAL"`).
They are derived from what the three approved providers can observe.

Common rules for all six:

- **Scope requirement:** every dimension is computed for exactly one
  `(candidate_id, research_run_id)` pair, and verifies both before reading any
  evidence. Precedent: `content_patterns.py` `_in_scope`, and 5B's round-1
  BLOCKER, which was an unverified scope.
- **Provenance requirement:** every dimension emits contributing `evidence_ids`
  (sorted), `providers`, `platforms`, `source_truth_classes`, the observation
  identifiers it rests on, and its own formula version.
- **Derivation truth class:** a derived dimension is `INFERRED` even when every
  input is `OBSERVED`. Precedent: `preliminary_dimensions.py:23-26`.
- **Determinism:** pure function of the evidence set, not its arrival order.
  Precedent: 4D-0.1, 5B, 5C.

### D1 — `deep_search_demand`

| | |
|---|---|
| Business question | Are people searching for this, and how broadly? |
| Source evidence | `signal_type="search_volume"`, `purpose=SEARCH_DEMAND` |
| Provider/capability | DataForSEO / `search_demand` |
| Existing or new collection | **New (7a):** more keywords per candidate than the cheap pass |
| OBSERVED | A volume returned by the provider for a keyword |
| ESTIMATED | Provider-modelled volume, if the provider labels it so |
| INFERRED | Any aggregate this dimension computes |
| UNKNOWN | Keyword queried, provider returned no measurement |
| Derivation | Distribution over observed keyword volumes: median, count of keywords with a measurement, count queried. **No single 0–100 value** (see §5 R-3) |
| Missing behaviour | `MISSING` if capability not run/failed; `UNKNOWN` if run and no measurement. Never 0 |
| Conflict behaviour | Two OBSERVED volumes for one normalized keyword in one run → `CONFLICTING`, excluded from the median, reported separately |
| Required / optional | **Required** |
| May feed POS | **Yes** |
| May feed ECS | Coverage/freshness properties only, never the value |
| Contextual only | No |

### D2 — `deep_purchase_proxy_evidence`

| | |
|---|---|
| Business question | Is there observable evidence that people have bought things like this? |
| Source evidence | `marketplace_review_count_purchase_proxy`, listing longevity, paid comparables, seller breadth |
| Provider/capability | Etsy / `marketplace` |
| Existing or new | **Existing derivation (4A) over new, deeper 7a evidence** |
| OBSERVED | Review counts, listing ages, seller identities as returned |
| INFERRED | The proxy pattern classification |
| UNKNOWN | `exact_units_sold`, `exact_revenue` — permanently UNKNOWN (`purchase_evidence.py:291-293`) |
| Derivation | `market_validation_pattern_v1` shape + raw counts. **Never sales, never revenue** |
| Missing behaviour | Four distinguishable states preserved (not requested / provider failed / error / ran-and-empty) |
| Conflict behaviour | Contradictory observations of one listing → excluded and reported |
| Required / optional | **Required** |
| May feed POS | **Yes, as a proxy** — and must be labelled a proxy wherever surfaced |
| May feed ECS | Sample adequacy only |
| Contextual only | No |

### D3 — `deep_price_evidence`

| | |
|---|---|
| Business question | What do sellers currently **ask** for comparable things? |
| Source evidence | `marketplace_listing_price` |
| Provider/capability | Etsy / `marketplace` |
| Existing or new | **Existing derivation (4B) over deeper 7a evidence** |
| OBSERVED | Asking prices, per currency |
| UNKNOWN | `transaction_prices`, `willingness_to_pay`, `recommended_price` — permanently UNKNOWN (`price_evidence.py:277-280`) |
| Derivation | Per-currency bands; currencies never combined; $0 listings counted separately as free competitors |
| Missing behaviour | `MISSING` vs `UNKNOWN` preserved; never 0 |
| Conflict behaviour | Per-currency bands cannot conflict across currencies by construction |
| Required / optional | **Optional** — a candidate with no priced comparables is still scoreable |
| May feed POS | **UNRESOLVED (§13 U-3)** — see the `price_strength` rejection in §12 |
| May feed ECS | Sample adequacy only |
| Contextual only | Currently yes, pending U-3 |

### D4 — `deep_competition_structure`

| | |
|---|---|
| Business question | What shape is the competitive field, and what does that shape not tell us? |
| Source evidence | `marketplace_competing_listing` |
| Provider/capability | Etsy / `marketplace` |
| Existing or new | **Existing derivation (4E) over deeper 7a evidence** |
| OBSERVED | Listing counts, distinct sellers |
| UNKNOWN | `saturation`, `entry_difficulty`, `win_probability`, `differentiation_opportunity`, `competitor_strength`, `competitor_revenue`, `market_share_available` — permanent UNKNOWN markers (BUILD_ORDER:215-219) |
| Derivation | `competition_field_pattern_v1`; **every pattern carries two opposed readings, emitted together and never ranked** |
| Missing behaviour | Below half the listings carrying a seller → `FIELD_STRUCTURE_UNKNOWN` |
| Conflict behaviour | Inherited from `collect_listing_views` |
| Required / optional | **Required** |
| May feed POS | **NO — structurally ineligible.** See §5 R-4 |
| May feed ECS | Coverage only |
| Contextual only | **Yes** |

### D5 — `deep_audience_attention`

| | |
|---|---|
| Business question | Is there observable public attention around this topic, and how is it distributed? |
| Source evidence | `public_video_view_count`, `purpose=AUDIENCE` |
| Provider/capability | YouTube / `public_content` |
| Existing or new | **Existing derivation (4F) over deeper 7a evidence** |
| OBSERVED | View counts |
| UNKNOWN | `buyer_count`, `purchase_intent`, `candidate_audience_size`, `demand_durability`, `willingness_to_pay`, `watch_time`, `conversion_probability` — permanent UNKNOWN markers (BUILD_ORDER:246-248) |
| Derivation | Distribution and consistency, never a sum. `attention_pattern_v1`, scale-invariant |
| Missing behaviour | `ATTENTION_UNMEASURED` ≠ attention measured at zero. Five distinguishable facts |
| Conflict behaviour | Conflicting observations of one video excluded and reported |
| Required / optional | **Required** |
| May feed POS | **Yes — as attention, never as demand or buyers** |
| May feed ECS | Coverage/sample only |
| Contextual only | No |

### D6 — `deep_channel_reach`

| | |
|---|---|
| Business question | Where could a seller observably show up? |
| Source evidence | Marketplace sellers, content creators, search surfaces with observed volume |
| Provider/capability | All three |
| Existing or new | **Existing derivation (4D) over deeper 7a evidence** |
| OBSERVED | Channel existence, activity |
| INFERRED | Relevance — **permanently capped at INFERRED** (BUILD_ORDER:176-180), because it rests on a Milestone 1 query hypothesis |
| UNKNOWN | Buyer counts, audience size, market size, conversion, reachable population — never estimated |
| Derivation | `reach_evidence_pattern_v1` shape only; `MULTI_CHANNEL_CLASS` is never "better" |
| Missing behaviour | Capability-aware MISSING, four distinguishable reasons |
| Conflict behaviour | Identity-based counts; duplicate multiplicity preserved |
| Required / optional | **Required** |
| May feed POS | **NO — structurally ineligible.** See §5 R-4 |
| May feed ECS | Coverage only |
| Contextual only | **Yes** |

### Dimensions deliberately NOT specified

- **Problem/product fit.** Cannot be a Step 7 dimension. 4G consumes a 4C
  specification (`routes.py:2388`), and 4C is step 10. At step 7 the product does
  not exist. **Consequence:** the legacy `problem_product_fit` weight is
  unbuildable before step 10 (§13 U-2).
- **Price strength.** No implementation exists and 4B explicitly refuses a price
  recommendation or willingness-to-pay. Rejected in §12.
- **Buyer reach as a magnitude.** 4D refuses to estimate buyer counts. Only the
  channel-existence evidence of D6 exists.

---

## 3. Evidence eligibility matrix

| Component | Disposition |
|---|---|
| Raw search-demand evidence | **Step 7 required input** |
| Raw marketplace evidence | **Step 7 required input** |
| Raw public-content evidence | **Step 7 required input** |
| 4A purchase evidence | **Step 7 required input; POS eligible as a proxy** |
| 4B price evidence | **Step 7 optional input; POS eligibility UNRESOLVED (U-3)** |
| 4D buyer reach | **Step 7 required input; contextual only — POS ineligible** |
| 4E competition opportunity | **Step 7 required input; contextual only — POS ineligible** |
| 4F audience attention | **Step 7 required input; POS eligible as attention** |
| Preliminary search-demand 0–100 | **Preliminary-selection only. FORBIDDEN from Step 8** |
| Preliminary audience-interest 0–100 | **Preliminary-selection only. FORBIDDEN from Step 8** |
| Preliminary rank | **Preliminary-selection only. FORBIDDEN from Step 8 and from the Step 7 boundary object** |
| `CapabilityOutcome` | **Evidence Confidence only** |
| 4C product specification | **Post-score only (step 10)** |
| 4G product/job fit | **Post-score only (post-step-10)** |
| 5A robust outlier evidence | **Post-score only (step 12)** |
| 5B content patterns | **Post-score only (step 12)** |
| 5C content experiments | **Post-score only (step 13)** |

### The two 0–100 preliminary heuristics: **RECOMPUTED, and FORBIDDEN from Step 8**

**Repository evidence.** `search_demand_features.py:44-50` maps median volume by
`min(100, 100*log10(v+1)/6)`; `public_content_features.py:57-63` maps median
views by `min(100, 100*log10(v+1)/7)`. The saturation points (10⁶ searches,
10⁷ views) are documented as V1 assumptions; `public_content_features.py:44`
calls its own constant "documented V1 assumption". `preliminary_ranking.py:33-50`
states the whole ordering is "a policy choice, not an empirical finding".

**Decision.** They are **not promoted**. Step 8 recomputes demand and attention
from the deep evidence set under separately approved, separately versioned
formulas. The preliminary values remain in place, unchanged, for selection only.

**Consequence.** Selection and scoring may disagree about a candidate, and that
is correct: they answer different questions ("where do we spend research budget?"
vs. "how attractive is this opportunity?"). Promoting the heuristics would have
imported two arbitrary saturation constants into the final engine unexamined,
which is precisely the failure mode `scoring.py:1-37` was written to prevent.

---

## 4. Step 8 input contract

**Repository evidence.** Every derivation since 4A returns
`EVIDENCE_PRESENT_UNSCORED` with `value=None`. `DimensionState`
(`preliminary_dimensions.py:112-122`) already distinguishes SCORED /
EVIDENCE_PRESENT_UNSCORED / UNKNOWN / MISSING.

**Decision.** Step 8 consumes **evidence and explicit states, never precomputed
composite values.** Its input is one `DeepResearchResult` (§11) per
`(candidate_id, research_run_id)`, containing for each Step 7 dimension:

```
dimension_name        : str            # deep_* namespace
state                 : DimensionState # SCORED | EVIDENCE_PRESENT_UNSCORED | UNKNOWN | MISSING
missing_reason        : str | None     # required when state is MISSING
observed_features     : dict           # raw observables, not scores
truth_basis           : TruthClass     # weakest contributing class
evidence_ids          : tuple[UUID]    # sorted, canonical
formula_version       : str
conflict_count        : int
sample_size           : int | None     # None means unknown, never 0
```

Step 8 must reject any input where `candidate_id` or `research_run_id` does not
match its own scope, unread — the pattern 5B enforces today.

**Consequence.** Step 8 cannot be handed a number it did not derive. Every value
it produces is traceable to observables and a named formula version.

---

## 5. Opportunity Score contract

**Repository evidence.** No approved weights exist. 4E emits opposed readings
that are "never ranked" and has "no ordering over the patterns", enforced by AST
guards. 4D caps relevance at INFERRED permanently. No outcome data exists
anywhere in the repository against which any weight could be calibrated.

**Decision.** The POS contract specifies **structure now, numbers later.**

| POS dimension | Meaning | Source | Formula | Normalization | Range | Missing | UNKNOWN allowed | Zero legitimate? | Direction | Version |
|---|---|---|---|---|---|---|---|---|---|---|
| `pos_search_demand` | Observed search interest | D1 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | dimension UNKNOWN | Yes | **Yes** — an observed zero-volume keyword is a real measurement | higher = more | `pos_search_demand_v1` (unassigned) |
| `pos_purchase_proxy` | Observable evidence that comparable things sell | D2 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | dimension UNKNOWN | Yes | **Yes** — zero observed review proxies is a real observation | higher = more | unassigned |
| `pos_audience_attention` | Observed public attention | D5 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | dimension UNKNOWN | Yes | **Yes** — an observed zero-view video is a real measurement | higher = more | unassigned |
| `pos_price_opportunity` | — | D3 | **UNRESOLVED (U-3)** | — | — | — | — | — | — | — |
| Competition | — | D4 | **INELIGIBLE (R-4)** | — | — | — | — | — | — | — |
| Channel reach | — | D6 | **INELIGIBLE (R-4)** | — | — | — | — | — | — | — |
| Problem/product fit | — | 4G | **INELIGIBLE at step 8 (U-2)** | — | — | — | — | — | — | — |

**No formula is specified in this milestone.** Inventing one to fill the table
is explicitly forbidden by this milestone's own brief and by `scoring.py:35-37`.

Five rules govern any future formula:

- **R-1 — Named and versioned.** Every dimension formula carries its own version
  string, independently approved.
- **R-2 — No composite without approved weights.** Until weights are approved
  against evidence, Step 8 emits per-dimension sub-scores and **no single POS
  scalar**. A candidate-level POS is itself gated on U-4.
- **R-3 — Distribution over point estimates.** Where a distribution is available
  (D1, D5), the formula reads the distribution, not a bare median. 4F exists
  because a total or median is the statistic one outlier corrupts.
- **R-4 — Structural ineligibility is permanent.** D4 and D6 may never contribute
  a POS magnitude. D4 deliberately has no ordering over its patterns; D6's
  relevance is permanently INFERRED. Giving either a number would require
  inventing the ordering both milestones deliberately refused. They are
  **contextual**: reportable alongside a score, never inside it.
- **R-5 — Proxies stay labelled.** `pos_purchase_proxy` is a proxy and must be
  surfaced as one wherever it appears.

---

## 6. Evidence Confidence contract

**Repository evidence.** `EvidenceItem` (`models.py:70-77`) carries eight
confidence fields with defaults of 0.5 or 1.0. Exactly **two** are ever assigned
in production: `directness=0.4` (`marketplace.py:215`) and `freshness=1.0`
(`search_demand.py:160`). The other six are never set by any builder. The only
tests exercising ECS (`tests/test_scoring.py`) hand-supply all eight in their
fixtures, which is why the gap was invisible. Separately,
`cross_source_agreement` is **incoherent as a per-item field**: agreement is a
property of a set of observations, not of one record.

**Decision: option B — replace the model.** The eight-field per-item rating
contract is deprecated. Evidence Confidence is recomputed from **facts the
system already observes**, never from ratings.

| ECS input | Observable source | Calculation | Range | Missing behaviour | Default |
|---|---|---|---|---|---|
| `dimension_coverage` | Step 7 dimension states | required dimensions in a scoreable state ÷ required dimensions | 0–1 | cannot be missing — always computable | **none** |
| `sample_adequacy` | `sample_size` per dimension | observed sample vs. that dimension's declared minimum | 0–1 | dimension contributes `UNKNOWN`, excluded from numerator **and** denominator | **none** |
| `provenance_directness` | `signal_type`, `purpose`, `collection_method` | declared per (signal, purpose) pair in an approved table; `official_api` ≠ `cache` ≠ unknown | 0–1 | record excluded, counted as uncovered | **none** |
| `corroboration_breadth` | distinct `provider` / `platform` per dimension | count of independent surfaces contributing | integer | zero surfaces = dimension MISSING | **none** |
| `freshness` | `retrieved_at` / `collected_at` vs. run time | position within a declared per-capability freshness window | 0–1 | timestamp absent → `UNKNOWN`, excluded | **none** |
| `capability_health` | `CapabilityOutcome.status`, `provider_errors`, `failure_reason` | clean / partial / failed, already captured verbatim | enum | cannot be missing — the outcome always exists | **none** |
| `conflict_rate` | `CONFLICTING` field states and conflict counts | conflicting observations ÷ total observations per dimension | 0–1 | no observations → dimension MISSING | **none** |

**Hard rules.**

1. **No defaults are permitted anywhere in Evidence Confidence.** An unavailable
   input is `UNKNOWN` and is excluded from both numerator and denominator; it
   never takes a value. Precedent: 5C's `evaluate_success_criterion`, where an
   UNKNOWN publication outcome is excluded rather than counted as below, and
   fewer than the floor yields no decision rather than a stop.
2. **ECS measures the evidence, not the opportunity.** No ECS input may read the
   *magnitude* of any observation.
3. **Low coverage lowers ECS; it never lowers POS.** See §7.
4. The eight `EvidenceItem` confidence fields are **deprecated**. They remain on
   the model (removing them is a schema change out of scope here), are never
   read by Step 8, and must not be populated to "fix" them — populating
   `source_quality` would require inventing a per-provider constant, which is the
   same defect in a different place.

**Consequence.** ECS becomes computable from data the system already has, with
no invented constants. It also becomes honest: a candidate researched through a
failed capability gets low confidence because the capability failed, not because
a default said 0.5.

---

## 7. POS / ECS separation rules

**Decision.** A formal, testable boundary:

> **POS reads the VALUES of observations. ECS reads the PROPERTIES of the
> evidence record set — its count, coverage, source, recency, agreement and
> capability health. No single fact may be read by both.**

Consequences, stated as rules:

- **S-1.** A *low observed value* is a POS input. A *missing observation* is an
  ECS input, and makes the corresponding POS dimension `UNKNOWN`.
- **S-2.** Worked example, as posed in the brief: low search volume that was
  actually measured **reduces `pos_search_demand`** and does not affect ECS.
  Search volume that was never measured **leaves `pos_search_demand` UNKNOWN**
  and **reduces `dimension_coverage`** in ECS. Absence is never scored as zero
  demand.
- **S-3.** Sample size may influence ECS only. It must not scale a POS
  sub-score, because that would double-count thin evidence — once as a lower
  score and once as lower confidence.
- **S-4.** Conflict is an ECS input. Conflicting observations are excluded from
  POS derivation and counted in `conflict_rate`.
- **S-5.** Freshness may influence ECS only. Stale evidence does not make an
  opportunity less attractive; it makes the conclusion less trustworthy.

**Consequence.** The system can say "this looks strong, and we are not confident"
and "this looks weak, and we are confident" — which is the distinction
`ARCHITECTURE.md` already requires with "Evidence Confidence is separate from
Opportunity Score."

---

## 8. Missing-data semantics

**Repository evidence.** `DimensionState` already separates MISSING from
UNKNOWN. 4D preserves four capability-missing reasons; 4F preserves five facts
including `ATTENTION_UNMEASURED`; 5B separates absence from conflict.

**Decision.** Nine input conditions, each with a defined consequence. None
collapses to zero.

| Condition | Dimension state | POS effect | ECS effect | Classification effect |
|---|---|---|---|---|
| Capability not requested | `MISSING` (`capability_not_requested`) | dimension excluded | coverage ↓ | may block |
| Provider failure | `MISSING` (`capability_provider_failed`) | dimension excluded | coverage ↓, `capability_health` ↓ | may block |
| Unexpected error | `MISSING` (`derivation_error`) | dimension excluded | coverage ↓, health ↓ | may block |
| Ran and returned nothing | `UNKNOWN` | dimension `UNKNOWN` | coverage ↓ | may block |
| **Zero observed results** | `EVIDENCE_PRESENT_UNSCORED` / scoreable | **contributes a real low value** | no effect | does not block |
| Conflicting evidence | conflicting observations excluded | excluded from derivation | `conflict_rate` ↑ | does not block by itself |
| Insufficient sample | `UNKNOWN` for that dimension | dimension `UNKNOWN` | `sample_adequacy` ↓ | may block |
| Stale evidence | scoreable, flagged | unaffected | `freshness` ↓ | may block via ECS floor |
| Derived dimension unavailable | `MISSING` | excluded | coverage ↓ | may block |

**The zero rule, stated once:** an observed zero is a measurement and enters POS.
An absent measurement is not a zero and never enters POS. This is the single rule
every milestone from 3C onward has enforced, and Step 8 inherits it unchanged.

**Partial scoring.** POS may be computed over the subset of required dimensions
that are scoreable, **provided** the result records which dimensions were
excluded and why. POS must remain unavailable when **no** required dimension is
scoreable. The minimum number of required dimensions for a partial POS is
**UNRESOLVED (U-5)**.

---

## 9. Classification mechanics

**Repository evidence.** `scoring.py:135-144` classifies on `<50` RED,
`>=70 and >=70` GREEN, else YELLOW. These thresholds "appear in no approved
repository specification" (`scoring.py:30-32`).

**Decision.** Mechanics are specified; **no numeric threshold is chosen.**

- **C-1.** Classification depends on **POS and ECS jointly**, never POS alone.
  A high score on thin evidence must not present as GREEN.
- **C-2.** A minimum Evidence Confidence is required to classify at all. Below
  it, the candidate is `SCORED_UNCLASSIFIED` — a score exists, a colour does
  not. The threshold value is **UNRESOLVED (U-6)**.
- **C-3.** Incomplete required dimensions block GREEN. Whether they block
  YELLOW is **UNRESOLVED (U-6)**.
- **C-4.** Kill rules are **disqualifiers, not score adjustments.** A kill rule
  fires on an evidence condition, overrides the score entirely, and names
  itself in the result. No kill rule may fire on a *missing* dimension — only
  on an observed condition. The legacy
  `NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL` rule violates this: it fires when
  `purchase_evidence == 0 and search_demand < 15`, which cannot distinguish an
  observed zero from a defaulted one. It is rejected (§12).
- **C-5.** `UNCLASSIFIED` is a permanent, first-class outcome, not an error.
- **C-6.** Every classification records the POS version, the ECS version, the
  threshold set version, and the evidence ids behind it.

---

## 10. Persistence and state requirements

**Repository evidence.** `schema.sql:168-180` defines `score_versions` with
`opportunity_score numeric NOT NULL`, `evidence_confidence numeric NOT NULL`,
and `classification text NOT NULL check (classification in ('RED','YELLOW','GREEN'))`.
This cannot represent any state the system actually produces:
every derivation since 4A returns `EVIDENCE_PRESENT_UNSCORED`.

**Decision.** The future schema contract requires an explicit scoring state and
nullable results:

```
scoring_state    : NOT_SCORED | INSUFFICIENT_EVIDENCE | SCORED_UNCLASSIFIED | CLASSIFIED
opportunity_score      : nullable   -- NULL unless state is SCORED_UNCLASSIFIED or CLASSIFIED
evidence_confidence    : nullable   -- NULL unless computed
classification         : nullable, RED|YELLOW|GREEN only when state is CLASSIFIED
excluded_dimensions    : jsonb      -- name + missing_reason per excluded dimension
kill_rules_triggered   : jsonb
pos_version, ecs_version, threshold_set_version : text
```

State meanings:

- `NOT_SCORED` — Step 8 has not run for this candidate/run.
- `INSUFFICIENT_EVIDENCE` — Step 8 ran; too few required dimensions were
  scoreable. No score, no colour.
- `SCORED_UNCLASSIFIED` — POS computed; ECS below the classification floor, or
  required dimensions incomplete. Score exists, colour does not.
- `CLASSIFIED` — POS, ECS and a colour all exist.

**Consequence.** "Missing values remain missing" becomes representable in
storage for the first time. **No migration in this milestone.**

---

## 11. Step 7 boundary object

**Repository evidence.** `PreliminaryResearchResult` has 5 required and 9
optional fields; `profiles` covers **all** candidates while the five derivation
dicts cover **selected only**; it holds no raw evidence; it has **no consumer**
in `app/`; routes serialize it and discard it. The durable state is
`ResearchStore.evidence_for_candidate(candidate_id, research_run_id)`, which
`/product/specification` already uses to rebuild 4A and 4B from scratch
(`routes.py:2368-2369`).

**Decision.** Create a **new `DeepResearchResult`**. Do not refactor
`PreliminaryResearchResult`.

Rationale: the preliminary object legitimately serves a different job —
all-candidate triage telemetry — and forcing one object to carry both scopes is
what makes it unusable as a scoring input today. Two objects with uniform scope
each are clearer than one object with mixed scope.

```
DeepResearchResult:
    candidate_id        : UUID        # exactly one candidate
    research_run_id     : UUID        # required, not optional
    deep_pass_id        : UUID        # identifies the 7a collection
    dimensions          : dict[str, DeepDimension]   # the §4 contract, D1..D6
    capability_outcomes : tuple[CapabilityOutcome]   # ECS input
    evidence_ids        : tuple[UUID] # sorted union of all contributing records
    providers, platforms: tuple[str]
    component_versions  : dict[str, str]
    state               : DeepResearchState
```

Required invariants:

- **Uniform scope.** One candidate, one run. Never a mixed-scope collection.
- **Verified scope.** Both ids checked before any evidence is read.
- **Persisted.** Unlike the preliminary result, it must survive the request.
- **Deterministic.** Same evidence set → identical object, arrival order
  irrelevant, lineage included.
- **`preliminary_rank` is excluded.** Rank position must not appear on this
  object in any form. It is selection-only, and carrying it forward would let
  triage policy leak into scoring.

**Consequence.** Step 8's input is one uniformly scoped object per candidate.
`ResearchStore` remains the evidence system of record; `DeepResearchResult` is
the derived, versioned view of it that scoring consumes.

---

## 12. Legacy scoring artifacts — rejected or retained

| Artifact | Disposition |
|---|---|
| `WEIGHTS` (7 POS weights) | **REJECTED.** Never promoted. Four of the seven dimensions are structurally ineligible or unbuildable |
| `CONFIDENCE_WEIGHTS` (8 ECS weights) | **REJECTED.** Replaced by §6 |
| 8 `EvidenceItem` confidence fields | **DEPRECATED.** Retained on the model, never read by Step 8, never populated |
| `classify` thresholds (50 / 70 / 70) | **REJECTED.** Mechanics respecified in §9; numbers unresolved |
| `apply_kill_rules` — `NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL` | **REJECTED.** Cannot distinguish observed zero from defaulted zero (violates C-4) |
| `apply_kill_rules` — `NO_IDENTIFIABLE_DISTRIBUTION_ROUTE` | **REJECTED.** Reads `buyer_reach < 20`; no such magnitude exists or may exist (R-4) |
| `ScoreDimensions` (7 fields) | **REJECTED as the scoring contract.** Replaced by §4 |
| `ScoreDimensions.price_strength` | **REJECTED.** No implementation; 4B refuses price recommendation and WTP |
| `ScoreDimensions.problem_product_fit` | **REJECTED at step 8.** Unbuildable before step 10 (U-2) |
| `score_opportunity`, `weighted_opportunity_score`, `evidence_confidence` | **RETAINED as quarantined library code**, unreachable, per `scoring.py:20-24` |
| `SCORING_STATUS` quarantine marker | **RETAINED.** Working as intended |
| `score_versions` table | **RETAINED, contract superseded** by §10. No migration here |
| `POST /score` → 410 | **RETAINED unchanged** |

---

## 13. Unresolved product decisions

| # | Decision required | Why it cannot be settled from the repository |
|---|---|---|
| **U-1** | Does Step 7 collect new evidence (7a), or derive only? | §1 proposes collection; the repository currently does derivation-only. This is a cost/depth policy call, not a technical one |
| **U-2** | Does POS include problem/product fit — and if so, does Step 8 move after step 10? | 4G needs a 4C spec. Either POS drops the dimension, or scoring happens after product generation, reordering the architecture |
| **U-3** | Is price evidence POS-eligible at all? | Asking prices without transaction data may indicate opportunity or merely supply. 4B refuses to interpret them |
| **U-4** | Does Step 8 emit a single POS scalar, or only per-dimension sub-scores? | No approved weights and no outcome data. R-2 defaults to sub-scores only |
| **U-5** | Minimum required dimensions for a partial POS | A policy floor; no evidence justifies a specific count |
| **U-6** | Numeric thresholds: ECS floor to classify, and GREEN/YELLOW cutoffs | No outcome data exists. Choosing numbers now repeats the v0.1 mistake |
| **U-7** | Per-capability freshness windows and per-dimension minimum samples | Needed by §6; each is a policy constant requiring justification |
| **U-8** | Do the deep-pass caps differ per capability, and by how much? | A budget decision |

**None of these is a technical blocker. All are product-policy choices that must
be made by a person and recorded, not inferred by an implementer.**

---

## 14. The 4A naming collision

**Repository evidence.** `DIM_PURCHASE_EVIDENCE = "purchase_evidence"`
(`purchase_evidence.py:93`) is name-identical to `ScoreDimensions.purchase_evidence`
and to the `WEIGHTS` key (verified programmatically). 4D–4G and 5B–5C all adopted
a `preliminary_` prefix expressly to prevent this; 4A and 4B predate the
convention. 4B's `"price_evidence"` avoids collision only because the legacy
field is named `price_strength`.

**Decision.** Three-part, with **no runtime change in this milestone**:

1. **4A keeps its current dimension name.** Renaming it would change a shipped
   API response for no functional gain and is out of scope here.
2. **Legacy score naming is discarded wholesale.** `ScoreDimensions` is rejected
   as the scoring contract (§12), so the collision target ceases to exist.
3. **Step 8 dimensions use the reserved `pos_` prefix and Step 7 the `deep_`
   prefix.** Neither namespace can collide with an evidence dimension name.

**Consequence.** Ambiguity is removed by making the legacy name unreachable
rather than by renaming a shipped field. A future cleanup milestone may align
4A/4B with the `preliminary_` convention; it is not required for Step 8 safety.

---

## 15. Implementation sequence after approval

Nothing below is started until this specification is approved.

| Order | Milestone | Depends on | Rationale |
|---|---|---|---|
| 1 | **6A-1 Evidence Confidence inputs** | U-7 | Independent of every POS decision. The §6 inputs are computable from data that already exists; this is the one blocker that is purely mechanical |
| 2 | **6B Deep collection (7a)** | U-1, U-8 | Raise caps for selected candidates using existing providers and the existing `CapabilityCaps` seam |
| 3 | **6C `DeepResearchResult` boundary object** | 6B | Uniform scope, persisted, deterministic, rank excluded |
| 4 | **6D Per-dimension POS formulas** | U-2, U-3, U-4, U-5 | One dimension per slice, each separately approved and versioned |
| 5 | **6E Scoring state persistence** | §10 | Schema migration for the four scoring states |
| 6 | **6F Classification** | U-6 | Last, because it needs both POS and ECS to exist |

**Steps 1–3 are safe to build before any threshold decision is made.** Steps 4–6
are blocked on the unresolved product decisions in §13.

---

## Non-goals honoured

No weights implemented. Legacy scoring not activated. No thresholds chosen.
`/score` untouched (still 410). No RED/YELLOW/GREEN produced. 4C, 4G and 5A–5C
unaltered. 4H not revived. Step 11 not specified. No LLM scoring. No new
external providers.
