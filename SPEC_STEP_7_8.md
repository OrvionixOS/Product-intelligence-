# Milestone 6A-SPEC — Step 7 / Step 8 Normative Specification

**Status:** APPROVED — normative V1 Step 7 / Step 8 architecture, with review round 3 (ECS aggregation contract, deep-pass cache semantics) applied. Not yet implemented.
**Base:** `main` @ `1ad4c617fcac15908e87287f7be3f7088162bdc3`
**Scope:** specification only. No runtime behaviour changes, no scoring, no `/score`,
no new providers, no revival of legacy scoring constants.

## Approval record

- **Approved specification branch head:**
  `07f32ec01c90614630ce1b035e7fae0834dd6657`
  (branch `claude/milestone-6a-spec-step-7-8`, after review rounds 1 and 2).
  Independent review complete. This document became normative at that commit;
  the only change since is this status record, which alters no architectural
  decision.
- **Legacy scoring remains rejected and quarantined.** `app/services/scoring.py`
  keeps `SCORING_STATUS = "UNAPPROVED_EXPERIMENTAL"`, stays unreachable from any
  route, and is imported by no production module. Approving this specification
  approves none of its weights, thresholds or kill rules (§12). `POST /score`
  remains 410.
- **The six §13 policy decisions remain intentionally unresolved.** They are not
  oversights and must not be filled in by an implementer. Each must be decided
  by a person and shipped as a **separately named, versioned, explicitly
  uncalibrated assumption** before the implementation slice that depends on it:

  | Open decision | Blocks |
  |---|---|
  | U-1 per-dimension POS normalization/formulas | 6D |
  | U-2 weights and aggregation into the candidate POS scalar | 6E |
  | U-3 classification thresholds and the ECS floor | 6G |
  | U-4 per-capability sample floors | 6A-1 |
  | U-5 per-capability freshness windows | 6A-1 |
  | U-6 deep-pass cap values for `deep_pass_caps_v1` | 6B |
  | U-7 ECS component mapping values (`capability_health` ordinals, `provenance_directness` table) | 6A-1 |

  No implementation slice may proceed by inventing a value for the decision that
  blocks it.

- **Structural blockers, now cleared.** Two contracts were missing rather than
  merely unvalued, and an implementer would have had to invent them. Both are
  specified in this document and neither is an open policy question:

  | Blocker | Blocks | Status |
  |---|---|---|
  | ECS aggregation contract — normalization, enum and integer handling, aggregation rule, weights, blocked-vs-low, range, version, recomputation | 6A-1 | **Specified: §6.1 `ecs_v1`** |
  | Deep-pass cache-depth rule — a cached shallow result may never satisfy a deeper request | 6B | **Specified: §1.1** |

  6A-1 may not start until §6.1 is approved **and** U-4, U-5 and U-7 carry
  values. 6B may not start until §1.1 is approved **and** U-6 carries values.

Every decision below is stated as **Repository evidence → Decision → Consequence**.
Where a product-policy choice is being made, the alternative that was rejected is
named.

**Review round 1 settled six questions**, marked inline as "review decision":
Step 7 is a genuine deep-collection pass; problem/product fit is not a V1 POS
dimension and the architecture order 8 → 9 → 10 is preserved; price evidence is
contextual-only; the V1 POS surface is closed at three required dimensions;
there is no partial POS; and a candidate-level POS scalar is required. Six
policy items remain open (§13).

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

**APPROVED (review decision, was U-1).** Step 7 is a genuine second
deep-collection pass, not derivation-only. The alternative — deriving again over
step-3 evidence, which is what the repository does today — is rejected: the same
evidence cannot answer a question at two depths, and a "deep" stage that collects
nothing makes the step-3/step-7 distinction vacuous.

**Caps are explicitly versioned.** The deep pass declares a named, versioned cap
set (`deep_pass_caps_v1`) supplying the eight `CapabilityCaps` fields. The
version is recorded on every `DeepResearchResult`, so any result can be
attributed to the collection budget that produced it, and a later budget change
cannot silently alter what an earlier result meant. The cap VALUES remain an
unresolved policy assumption (§13 U-6); the requirement that they be named and
versioned is settled here.

### 1.1 Deep-pass cache semantics (binding on 6B)

**Repository evidence.** Two of the three caches are keyed **without any result
limit**:

- `ResearchStore.cached_listings(provider, query)` — key `(provider, query)`
  (`app/storage/memory.py`), read at `marketplace.py:281`, while the effective
  limit `max_listings_per_query` is resolved separately at `marketplace.py:257`.
- `ResearchStore.cached_videos(provider, query)` — key `(provider, query)`, read
  at `public_content.py:293`, limit `max_videos_per_query` resolved at
  `public_content.py:264`.

A cheap pass capped at N results per query therefore stores N results under a key
that says nothing about N. A deep pass requesting M > N would hit that entry and
receive N results **while believing it had collected deeply**. Every Step 7
dimension built on it would then be shallow evidence wearing a deep label.

`cached_metrics(provider, keyword, location, language)` is **not affected**. Its
key is per keyword, and depth in search demand means *more keywords queried*
(`max_keywords`, `search_demand.py:193`), not more results per keyword. A cached
keyword metric is equally valid at any depth.

**Decision — limit-aware cache identity.** Chosen over bypass/refetch because it
is the least invasive deterministic design: it preserves the existing cache for
same-depth reuse, spends no provider budget re-fetching what is already deep
enough, and makes the depth question explicit rather than implicit.

> **A deep pass must never treat a cached result collected under a smaller
> effective limit as satisfying a larger deep-pass request.**

Normative rules for 6B:

1. **Every result-count-limited cache entry records the effective limit it was
   collected under.** This applies to the listing and video caches. The keyword
   cache is out of scope per the evidence above.
2. **Reuse requires `cached_effective_limit >= requested_effective_limit`.**
   Anything else is a **depth miss** and must be re-fetched. A depth miss is not
   an error; it is the normal cost of going deeper.
3. **A depth miss may never be silently downgraded** into "the cache had fewer
   results, so fewer exist." Fewer cached results is a property of the earlier
   budget, never an observation about the field.
4. **Re-fetching replaces the entry** with one recording the larger limit, so a
   later shallow request reuses it correctly and a later deeper one still misses.

**Interaction with `deep_pass_caps_v1`.** The requested effective limit comes
from the versioned cap set (§1). Both the requested limit and the cap-set version
are recorded with the reuse decision, so any reuse is auditable against the
budget that authorized it, and a cap change cannot retroactively legitimize
evidence collected under the old one.

**Interaction with provenance.** Every record states whether it came from a live
deep fetch or a reused cache entry, and the effective limit it was collected
under. A shallow-cached record may never claim deep provenance. Per §2 this is
metadata: it cannot change a truth class (precedent: 4D-0).

**Interaction with cache-hit telemetry.** `CapabilityOutcome.cached_query_count`
already exists. 6B must distinguish a **true hit** (entry present and deep
enough) from a **depth miss** (entry present but too shallow, so re-fetched), so
a deep pass that looks suspiciously cheap can be diagnosed rather than trusted.
Counting a depth miss as a hit would hide precisely the failure this rule exists
to prevent.

**Interaction with deduplication.** Step-3 and step-7 evidence overlap by
construction: the deep pass re-observes everything the cheap pass saw, plus more.
**6B introduces no new deduplication rule.** It relies on the existing
fingerprint mechanism — a re-observation of the same thing produces a
byte-identical payload, therefore an identical `raw_payload_hash`, therefore one
record, with the smallest evidence id retained (the rule 5B settled in its
round-2 review). Overlap is already a solved problem; a second, parallel dedup
rule would be a way for the two to disagree.

**Consequence.** 6B cannot accidentally reuse shallow cached evidence as deep
evidence, and the one place where that could happen silently — a cache hit — now
produces a visible depth miss and a re-fetch instead.

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
| May feed POS | **NO — contextual only in V1 (review decision, was U-3).** Observed asking prices, without transaction prices or willingness-to-pay, cannot establish opportunity strength: a high asking price may signal margin or merely ambition, and a low one may signal commodity pressure or efficient production. The evidence is real and is retained in the dossier; the interpretation is not available |
| May feed ECS | Sample adequacy only |
| Contextual only | **Yes** |

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
| May feed POS | **NO — POS-ineligible in V1 under the current derivation.** See §5 R-4 |
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
| May feed POS | **NO — POS-ineligible in V1 under the current derivation.** See §5 R-4 |
| May feed ECS | Coverage only |
| Contextual only | **Yes** |

### Dimensions deliberately NOT specified

- **Problem/product fit.** Cannot be a Step 7 dimension, and is **not a V1 POS
  dimension** (review decision, was U-2). 4G consumes a 4C specification
  (`routes.py:2388`), and 4C is step 10; at step 7 the product does not exist.
  **The canonical architecture order is preserved: Step 8 scoring → Step 9
  classification → Step 10 product generation.** Scoring does not move after
  product generation. 4C and 4G remain post-score and must not influence V1 POS
  or V1 ECS by any path. **Consequence:** the legacy `problem_product_fit`
  weight is rejected for V1 (§12), not deferred.
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
| 4B price evidence | **Step 7 optional input; contextual only — POS ineligible in V1** |
| 4D buyer reach | **Step 7 required input; contextual only — POS ineligible in V1 (R-4)** |
| 4E competition opportunity | **Step 7 required input; contextual only — POS ineligible in V1 (R-4)** |
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

**Decision.** The POS contract specifies **structure now, numbers later**, over a
**closed V1 dimension surface of exactly three dimensions.**

### V1 POS input surface (closed)

| POS dimension | Meaning | Source | Formula | Normalization | Range | Missing | UNKNOWN allowed | Zero legitimate? | Direction | Version |
|---|---|---|---|---|---|---|---|---|---|---|
| `pos_search_demand` | Observed search interest | D1 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | see §8 — blocks candidate POS | Yes, at dimension level | **Yes** — an observed zero-volume keyword is a real measurement | higher = more | `pos_search_demand_v1` (unassigned) |
| `pos_purchase_proxy` | Observable evidence that comparable things sell | D2 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | see §8 — blocks candidate POS | Yes, at dimension level | **Yes** — zero observed review proxies is a real observation | higher = more | unassigned |
| `pos_audience_attention` | Observed public attention | D5 | **UNRESOLVED** | **UNRESOLVED** | 0–100 | see §8 — blocks candidate POS | Yes, at dimension level | **Yes** — an observed zero-view video is a real measurement | higher = more | unassigned |

**All three are REQUIRED.** The V1 surface is closed: no other dimension
contributes a POS magnitude.

### Retained as contextual dossier evidence, not score magnitudes

| Evidence | Source | Why contextual in V1 |
|---|---|---|
| Competition structure | D4 | The V1 derivation emits opposed readings with no ordering over its patterns |
| Channel reach | D6 | The V1 derivation's relevance is capped at INFERRED and it refuses magnitude |
| Price evidence | D3 | Asking prices without transaction prices or willingness-to-pay cannot establish opportunity strength (review decision, was U-3) |
| Problem/product fit | 4G | Post-score by architecture; step 10 output cannot feed step 8 (review decision, was U-2) |

These are carried in the Step 7 dossier, surfaced beside a score, and never
inside one.

**No formula is specified in this milestone.** Inventing one to fill the table
is explicitly forbidden by this milestone's own brief and by `scoring.py:35-37`.

Five rules govern any future formula:

- **R-1 — Named and versioned.** Every dimension formula carries its own version
  string, independently approved.
- **R-2 — A candidate-level POS scalar is required; its weights are not yet
  chosen.** V1 architecture requires **one deterministic candidate-level POS
  scalar, plus the three retained per-dimension sub-scores**, because Step 9
  consumes a candidate-level opportunity judgment and cannot classify a vector.
  The scalar's existence is settled here. Its **aggregation rule and weights
  remain unresolved** (§13 U-2) and must be a named, versioned policy assumption
  until outcome data supports calibration. Sub-scores are always retained
  alongside the scalar so the scalar can be recomputed and audited by hand.
- **R-3 — Distribution over point estimates.** Where a distribution is available
  (D1, D5), the formula reads the distribution, not a bare median. 4F exists
  because a total or median is the statistic one outlier corrupts.
- **R-4 — V1 ineligibility is a property of the current derivations, not a
  permanent ban.** The current V1 D4/D6 derivations are POS-ineligible. Making
  either scoreable in a future scoring version requires a separately approved
  observable magnitude, formula, truth contract, and tests. Existing
  qualitative/state derivations may never be numerically ordered retroactively:
  a future scoreable competition or reach signal must be a NEW, separately
  versioned derivation, not a number attached to the existing patterns.
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

**Two senses of "required" are in play and must not be blurred.** *Required Step 7
dimensions* are the five the dossier must carry (D1, D2, D4, D5, D6; D3 is
optional). *Required V1 POS dimensions* are the three that feed the score
(`pos_search_demand`, `pos_purchase_proxy`, `pos_audience_attention`, §5).
Evidence Confidence measures the former: coverage over the three POS dimensions
alone would be constant, since §8 forbids a candidate POS unless all three are
already scoreable.

Each row states the metric's **applicability contract** — when it is expected at
all — and what happens when it is expected but unavailable. Read together with
the three cases in the Hard rules below: only case (a), *not applicable by
contract*, leaves the denominator.

| ECS input | Observable source | Calculation | Range | Applicable when (case (a) exclusion) | Expected but unavailable (case (b)) | Default |
|---|---|---|---|---|---|---|
| `dimension_coverage` | Step 7 dimension states | required **Step 7** dimensions in a scoreable state ÷ required Step 7 dimensions (D1, D2, D4, D5, D6 — D3 is optional and excluded from both sides) | 0–1 | Always applicable; never excluded | Cannot occur — the dimension states always exist | **none** |
| `sample_adequacy` | `sample_size` per dimension | observed sample vs. that dimension's declared minimum (§13 U-4) | 0–1 | Applicable to every dimension that reached a scoreable state | **Stays in the denominator and scores as absent**, lowering ECS. A scoreable dimension that cannot report its sample size is less trustworthy, not exempt from the question | **none** |
| `provenance_directness` | `signal_type`, `purpose`, `collection_method` | declared per (signal, purpose) pair in an approved table; `official_api` ≠ `cache` ≠ unknown | 0–1 | Applicable to every contributing record | **Stays in the denominator and scores as absent.** An unrecognised or absent `collection_method` is the weakest provenance, never a waiver | **none** |
| `corroboration_breadth` | distinct `provider` / `platform` per dimension | count of independent surfaces contributing | integer | Applicable to every dimension a capability was asked to serve | Zero surfaces makes the dimension MISSING, which lowers `dimension_coverage`. It never removes the metric | **none** |
| `freshness` | `retrieved_at` / `collected_at` vs. run time | position within a declared per-capability freshness window (§13 U-5) | 0–1 | Applicable to every contributing record | **Stays in the denominator and scores as absent.** A record with no timestamp cannot be shown to be current, so it is treated as un-evidenced recency, not as exempt | **none** |
| `capability_health` | `CapabilityOutcome.status`, `provider_errors`, `failure_reason` | clean / partial / failed, already captured verbatim | enum | Always applicable; never excluded | Cannot occur — a `CapabilityOutcome` always exists for every attempted capability. **If one is absent, ECS is BLOCKED**, because the collection history is unverifiable | **none** |
| `conflict_rate` | `CONFLICTING` field states and conflict counts | conflicting observations ÷ total observations per dimension | 0–1 | Applicable to dimensions with at least one observation | Zero observations makes the dimension MISSING, lowering `dimension_coverage`. The metric is not removed | **none** |

**The only legitimate case (a) exclusions** are metrics that cannot apply to an
evidence class at all — for example `marketplace_relevance` for a search-demand
record, where no marketplace exists to be relevant to. Applicability is declared
per (metric, evidence class) in an approved table, never decided per record at
runtime, so "not applicable" cannot become a way to make an inconvenient metric
disappear.

**Hard rules.**

1. **No defaults are permitted anywhere in Evidence Confidence.** No ECS input
   may ever take an invented constant — not `0.5`, not `1.0`, not a
   per-provider assumption. Precedent: 5C's `evaluate_success_criterion`, where
   an UNKNOWN outcome is excluded rather than counted as below.

   **Missingness resolves into exactly three cases, and only the first is
   excluded from the denominator:**

   | Case | Meaning | Treatment |
   |---|---|---|
   | **(a) Not applicable by contract** | The metric does not apply to this evidence class at all — e.g. `marketplace_relevance` for a search-demand record, where no marketplace exists to be relevant to | **Excluded from the denominator.** Its absence is not a deficiency |
   | **(b) Expected but unavailable** | The metric applies and should have had a value, but does not — missing `retrieved_at`, absent sample size, unreported capability health | **Stays in the denominator and scores as absent**, lowering coverage/confidence. Where the metric's own contract declares it mandatory, it **blocks ECS entirely** rather than lowering it |
   | **(c) Opportunity observation UNKNOWN** | The underlying measurement of the opportunity is unknown — no search volume returned, view count unavailable | **Lowers ECS coverage; never converted into a POS value of any kind**, negative or otherwise. Per §8 it makes the POS dimension UNKNOWN, which under §8 blocks the candidate POS |

   **Anti-inflation rule.** Case (b) must never be silently reclassified as case
   (a). Missing freshness, provenance, sample information or capability health
   are case (b): they remain in the denominator, so ECS **falls**. It must be
   impossible for ECS to rise merely because a metric vanished. Every ECS result
   therefore reports its denominator composition — which metrics were counted,
   which were excluded as not-applicable, and under which contract clause — so
   that an inflated score is visible rather than inferred.
2. **ECS measures the evidence, not the opportunity.** No ECS input may read the
   *magnitude* of any observation.
3. **Low coverage lowers ECS; it never lowers POS.** See §7.
4. The eight `EvidenceItem` confidence fields are **deprecated**. They remain on
   the model (removing them is a schema change out of scope here), are never
   read by Step 8, and must not be populated to "fix" them — populating
   `source_quality` would require inventing a per-provider constant, which is the
   same defect in a different place.

### 6.1 ECS aggregation contract (`ecs_v1`)

**Repository evidence.** §6 defines seven heterogeneous inputs — a ratio, two
per-record averages, an integer count, a decay, an enum and a rate — but no rule
turning them into one number. Without this, 6A-1 would have to invent its own
mappings and weights, which is the defect this whole specification exists to
prevent.

**Decision.** `ecs_v1` is fully specified below. **6A-1 implements this contract
exactly and invents nothing.**

**Step 1 — Normalize every input to a component score in [0, 1], higher = more
confidence.** Direction is normalized here, so no component is a "badness" value
at aggregation time.

| Input | Type | Component score | Notes |
|---|---|---|---|
| `dimension_coverage` | ratio | identity | Already [0, 1] (§6). Never excludable |
| `sample_adequacy` | ratio | mean over required Step 7 dimensions of `min(1, observed_sample ÷ declared_floor)` | Floors are U-4. A scoreable dimension that cannot report its sample scores **0.0** and stays in the mean |
| `provenance_directness` | ratio | mean over contributing records of the value declared for its `(signal_type, purpose, collection_method)` triple | Table is U-7. An unrecognised or absent `collection_method` scores **0.0** — weakest provenance, never a waiver |
| `corroboration_breadth` | **integer** | `min(1, observed_distinct_surfaces ÷ expected_surfaces_for_that_dimension)`, then mean over required dimensions | **No free parameter.** `expected_surfaces` is fixed by the dimension's own contract in §2 — D1, D2, D3, D4 and D5 are each served by one capability, D6 by all three. The denominator is read from the specification, not chosen |
| `freshness` | decay | mean over contributing records of position in the declared window: `1.0` at collection, linear to `0.0` at the window edge, `0.0` beyond | Windows are U-5. A record with no timestamp scores **0.0** and stays in the mean |
| `capability_health` | **enum** | declared ordinal: clean → `1.0`, partial → `0.5`, failed → `0.0` | Ordinals are U-7. Never excludable |
| `conflict_rate` | rate | `1 − conflict_rate` | Direction inverted here so aggregation never mixes senses |

**Step 2 — Aggregate as an unweighted arithmetic mean of the seven components,
expressed 0–100 and rounded to two decimal places.**

> `evidence_confidence = round(100 × mean(applicable components), 2)`

**There are NO weights in `ecs_v1`.** No evidence in the repository supports
treating any confidence component as more important than another, and inventing
a weighting would repeat exactly the `CONFIDENCE_WEIGHTS` mistake this
specification rejects (§12). The unweighted mean is the simplest transparent
policy, is **explicitly uncalibrated**, and is versioned so a future weighted
model becomes `ecs_v2` rather than a silent change of meaning.

**Step 3 — Applicability, and why exclusion cannot inflate.** The top-level mean
is taken over all seven components. Case (a) *not-applicable-by-contract*
exclusions operate **inside** a component's own per-record average, never by
removing the component from the top-level mean. A component leaves the top-level
mean only when every record for it is case-(a) excluded, in which case the
component is `NOT_APPLICABLE` and **must be reported as such**.
`dimension_coverage` and `capability_health` can never be excluded by any path.

Missing-but-expected values (case (b)) score `0.0` and remain in the mean. It is
therefore impossible for ECS to rise because evidence went missing.

**Step 4 — Blocked is not low.**

| | `evidence_confidence` | Meaning |
|---|---|---|
| **ECS computed** | 0.00–100.00 | A measurement. A low value is a real finding about weak evidence |
| **`ECS_BLOCKED`** | **NULL** | ECS could not be computed at all. Trigger: a `CapabilityOutcome` is absent for an attempted capability, so the collection history is unverifiable (§6) |

A blocked ECS is **NULL**, never `0.0`. It may not be rendered, stored, compared
or classified as a zero. Under §9 C-2 a blocked ECS cannot satisfy any
classification floor, so the candidate is `SCORED_UNCLASSIFIED` or
`INSUFFICIENT_EVIDENCE` — never classified by default.

**Step 5 — Deterministic recomputation.** Every ECS result emits: each of the
seven component scores, each component's applicability state, the denominator
composition (which components were counted, which were `NOT_APPLICABLE` and
under which contract clause), `ecs_v1`, and the component-mapping table version.
ECS is a pure function of the evidence set — same set, same number, whatever
order the records arrived in. A reader must be able to recompute the aggregate
by hand from the emitted components alone.

**Consequence.** 6A-1 has no latitude: every mapping, the aggregation rule, the
range, the rounding and the blocked/low distinction are fixed here. What remains
open is three tables of policy VALUES (U-4, U-5, U-7), each of which must ship
named, versioned and labelled uncalibrated.

---

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

**No partial scoring (review decision, was U-5).** All three required V1 POS
dimensions must be scoreable for a candidate-level POS to exist. If any required
dimension is `MISSING`, or `UNKNOWN` beyond what its own derivation rules permit,
then for that candidate and run:

- `scoring_state` = `INSUFFICIENT_EVIDENCE`
- candidate-level `opportunity_score` = **NULL**
- `classification` = **NULL**

The per-dimension sub-scores that WERE computable are still retained and
reported, together with `excluded_dimensions` naming each unavailable dimension
and its `missing_reason`. A candidate POS is never computed over a varying
subset of dimensions.

**Why.** A composite over a varying subset is not comparable between candidates:
two candidates with the same printed score could rest on different dimensions,
and the number would silently mean something different for each. The legacy
`weighted_opportunity_score` did exactly this — it renormalized over whichever
weights happened to be present (`scoring.py:88-104`) — which is how a candidate
missing most of its evidence could score 80.

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
  not. The threshold value is **UNRESOLVED (U-3)**.
- **C-3.** Required-dimension completeness is **already settled upstream and is
  not a classification question.** With no partial POS (§8), an incomplete
  required dimension yields `INSUFFICIENT_EVIDENCE`, a NULL candidate POS and a
  NULL classification before classification is ever reached. Classification
  therefore only ever sees candidates whose three required dimensions were all
  scoreable, and there is no separate question of incompleteness blocking GREEN
  versus YELLOW. What remains unresolved is the threshold values alone (U-3).
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
- `INSUFFICIENT_EVIDENCE` — Step 8 ran; **at least one** of the three required
  POS dimensions was not scoreable. No candidate score, no colour. Computable
  sub-scores and `excluded_dimensions` are still retained (§8), and **Evidence
  Confidence is still computed and stored** where its own inputs allow: knowing
  how good the evidence was is most useful precisely when it was not good
  enough to score.
- `SCORED_UNCLASSIFIED` — all three required dimensions were scoreable and a
  candidate POS exists, but ECS is below the classification floor. Score exists,
  colour does not.
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
| `WEIGHTS` (7 POS weights) | **REJECTED.** Never promoted. Four of the seven dimensions are POS-ineligible in V1 or unbuildable before step 10 |
| `CONFIDENCE_WEIGHTS` (8 ECS weights) | **REJECTED.** Replaced by §6 |
| 8 `EvidenceItem` confidence fields | **DEPRECATED.** Retained on the model, never read by Step 8, never populated |
| `classify` thresholds (50 / 70 / 70) | **REJECTED.** Mechanics respecified in §9; numbers unresolved |
| `apply_kill_rules` — `NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL` | **REJECTED.** Cannot distinguish observed zero from defaulted zero (violates C-4) |
| `apply_kill_rules` — `NO_IDENTIFIABLE_DISTRIBUTION_ROUTE` | **REJECTED for V1.** Reads `buyer_reach < 20`; no approved V1 buyer-reach magnitude exists for it to read, so the rule is invalid for V1. A future version would need a separately approved magnitude (R-4) |
| `ScoreDimensions` (7 fields) | **REJECTED as the scoring contract.** Replaced by §4 |
| `ScoreDimensions.price_strength` | **REJECTED.** No implementation; 4B refuses price recommendation and WTP |
| `ScoreDimensions.problem_product_fit` | **REJECTED for V1.** Unbuildable before step 10; the architecture order 8 → 9 → 10 is preserved rather than reordered |
| `score_opportunity`, `weighted_opportunity_score`, `evidence_confidence` | **RETAINED as quarantined library code**, unreachable, per `scoring.py:20-24` |
| `SCORING_STATUS` quarantine marker | **RETAINED.** Working as intended |
| `score_versions` table | **RETAINED, contract superseded** by §10. No migration here |
| `POST /score` → 410 | **RETAINED unchanged** |

---

## 13. Unresolved product decisions

| # | Decision required | Why it cannot be settled from the repository |
|---|---|---|
| **U-1** | Exact per-dimension POS normalization and formulas for `pos_search_demand`, `pos_purchase_proxy`, `pos_audience_attention` | No outcome data exists against which any normalization could be calibrated. Each must be a named, versioned policy assumption until it can be |
| **U-2** | Weights and aggregation rule combining the three sub-scores into the candidate POS scalar | R-2 settles that a scalar is required; no evidence supports any particular weighting. Versioned policy assumption until calibrated |
| **U-3** | Classification thresholds and the ECS floor required to classify | No outcome data. Choosing numbers now repeats the v0.1 mistake (`scoring.py:30-32`) |
| **U-4** | Per-capability sample floors (the minimums `sample_adequacy` measures against) | Each is a policy constant requiring justification |
| **U-5** | Per-capability freshness windows | Same |
| **U-6** | Deep-pass cap values for `deep_pass_caps_v1` | A collection-budget decision. The requirement that caps be named and versioned is settled (§1), and the cache-depth rule that makes them meaningful is settled (§1.1) |
| **U-7** | ECS component mapping values: the `capability_health` ordinals (clean/partial/failed) and the `provenance_directness` table over `(signal_type, purpose, collection_method)` | The `ecs_v1` aggregation contract is settled (§6.1); these two tables are the policy VALUES it reads. No evidence fixes them, so each is a named, versioned, uncalibrated assumption |

**None of these is a technical blocker. All are product-policy choices that must
be made by a person and recorded, not inferred by an implementer. Every one of
them must ship as a NAMED, VERSIONED policy assumption, explicitly labelled
uncalibrated, unless and until outcome evidence supports calibration.**

Resolved by this review and no longer open: Step 7 is a genuine deep-collection
pass (§1); problem/product fit is not a V1 POS dimension and the architecture
order 8 → 9 → 10 stands (§2); price evidence is contextual-only (§2 D3); the V1
POS surface is closed at three required dimensions (§5); there is no partial
POS (§8); a candidate-level POS scalar is required (§5 R-2).

Resolved by review round 3: the ECS aggregation contract, including the decision
that **`ecs_v1` carries no weights** (§6.1); and the deep-pass cache-depth rule
(§1.1). Round 3 introduced exactly one new open decision, U-7, which is the table
of values the now-fixed ECS contract reads.

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
| 1 | **6A-1 Evidence Confidence inputs and `ecs_v1`** | §6.1, U-4, U-5, U-7 | Independent of every POS decision. The §6 inputs are computable from data that already exists, and §6.1 fixes every mapping and the aggregation rule, so the slice implements a contract rather than designing one |
| 2 | **6B Deep collection (7a)** | §1.1, U-6 | Approved in §1. Raise caps for selected candidates using the existing providers and the existing `CapabilityCaps` seam, under a versioned `deep_pass_caps_v1`. §1.1 binds it: limit-aware cache identity, so a shallow cached result can never satisfy a deeper request |
| 3 | **6C `DeepResearchResult` boundary object** | 6B | Uniform scope, persisted, deterministic, preliminary rank excluded |
| 4 | **6D Per-dimension POS sub-scores** | U-1 | Exactly three dimensions, one slice each, every formula separately approved and versioned. No aggregation yet |
| 5 | **6E Candidate POS scalar** | U-2, 6D | Aggregation of the three sub-scores. Required by R-2; blocked only on the weighting policy |
| 6 | **6F Scoring state persistence** | §10 | Schema migration for the four scoring states, including NULL score/classification |
| 7 | **6G Classification** | U-3, 6E, 6F | Last: it needs POS, ECS and the state model to exist |

**Steps 1–3 are safe to build before any formula, weight or threshold decision is
made** — they depend only on policy constants (sample floors, freshness windows,
cap values), each of which ships as a named versioned assumption. Steps 4–7 are
blocked on the unresolved decisions in §13.

---

## Non-goals honoured

No weights implemented. Legacy scoring not activated. No thresholds chosen.
`/score` untouched (still 410). No RED/YELLOW/GREEN produced. 4C, 4G and 5A–5C
unaltered. 4H not revived. Step 11 not specified. No LLM scoring. No new
external providers.
