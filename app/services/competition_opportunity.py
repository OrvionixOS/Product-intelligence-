"""Competition Opportunity (Milestone 4E).

A deterministic derivation over marketplace evidence 3A already collected and
3C already resolved. No provider, no provider call, no endpoint, no
persistence, and no score.

Observable competitive evidence is not a conclusion about saturation
--------------------------------------------------------------------

This module observes one thing: **how the supply side of a marketplace field
is structured** — how many listings compete, how they distribute across
sellers, and how concentrated that distribution is.

It never concludes anything about saturation, entry difficulty, win
probability, differentiation room, or available market share. Those require
facts nobody here can observe: how many buyers exist, what they would switch
for, how strong each competitor actually is, and what the operator of this
tool can build. `saturation`, `entry_difficulty`, `win_probability`,
`differentiation_opportunity`, `competitor_strength`, `competitor_revenue`
and `market_share_available` are therefore permanent UNKNOWN markers on every
result, so the refusal is structural rather than prose.

Competition is not monotonic, and this module refuses to pretend otherwise
----------------------------------------------------------------------------

"Less competition is better" and "more competition is better" are both
wrong, and each is wrong in a way that would quietly corrupt every downstream
decision:

    few listings        could mean an unserved need with room to enter,
                        or a need nobody found worth serving

    many listings       could mean demand real enough to sustain many
                        sellers, or a field where attention is already spent

    one dominant seller could mean an incumbent proved a need nobody has
                        contested, or an entrenched catalog no entrant
                        displaces

    many small sellers  could mean a low barrier and no lock-in, or a
                        commoditized field with nothing to differentiate on

So `competition_field_pattern_v1` classifies the SHAPE of the field, and
every pattern carries BOTH readings, permanently paired and never ranked
(see `PATTERN_READINGS`). There is no ordering over the patterns, no pattern
maps to a number, and a test walks this module's AST to prove no comparison,
sort, or ranking expression ever consumes one. A consumer that wants a
preference between two fields must supply the missing facts itself; this
module will not smuggle one in.

What this module deliberately does not compute
-----------------------------------------------

- **Review counts, ratings, purchase proxies** — Milestone 4A owns those, and
  a review count is a purchase proxy, never a sale.
- **Prices, price bands, price dispersion** — Milestone 4B owns those, and an
  asking price is never a transaction price.
- **Reachable channels and endpoints** — Milestone 4D owns those.

A test walks the AST and asserts this module never reads a price, a review
count, a rating, or any of the derived predicates built on them. If the code
cannot read them, it cannot restate another milestone's finding as a
competition finding however the prose later drifts.

It also computes nothing temporal. Listing age and established-listing counts
already exist in 4A, answering a purchase question; a second age statistic
answering a competition question is deferred rather than duplicated here.

Relationship to existing competition surfaces
----------------------------------------------

`marketplace_features.summarize_competition` (Milestone 3A) produces raw
per-snapshot features from the provider objects of a single research call. It
has no truth classes, no missing-versus-zero distinction, no provenance, and
no classification. This module is a different layer: a candidate-scoped
derivation over STORED evidence, with all of those. The 3C dimension
`preliminary_competition_field` remains what it was — an unscored count of
competing-listing evidence. Neither is changed by this milestone.

Missing evidence never becomes an empty field
----------------------------------------------

Four facts stay distinguishable: the marketplace capability was not
requested, the provider failed, the provider raised something unexpected, or
it ran and returned nothing for this candidate. Only the last is an observed
absence; the first three make the field UNKNOWN. A failed marketplace call
must never read as "no competitors".
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.marketplace_listing_view import (
    ListingView,
    collect_listing_views,
    weakest_truth_class,
)
from app.services.preliminary_dimensions import DimensionState

COMPETITION_OPPORTUNITY_VERSION = "competition_opportunity_v1"
COMPETITION_FEATURES_VERSION = "competition_field_features_v1"
COMPETITION_FIELD_PATTERN_VERSION = "competition_field_pattern_v1"

# Never "competition_opportunity": that is a ScoreDimensions field feeding the
# unapproved legacy weights in app/services/scoring.py. The prefix keeps this
# derivation structurally unable to reach them.
DIM_COMPETITION_OPPORTUNITY = "preliminary_competition_opportunity"

# The capability whose outcome decides whether an empty field is an observed
# absence or an unknown one.
CAPABILITY_MARKETPLACE = "marketplace"

# ---------------------------------------------------------------- thresholds
#
# Every constant below is an UNVALIDATED V1 ASSUMPTION chosen by inspection.
# None is calibrated against outcomes, because no outcome data exists in this
# repository. They decide which SHAPE a field is reported as; they never
# decide whether that shape is good, and no threshold is a quality bar.

# At or below this many competing listings the field is reported sparse. A
# handful of listings cannot support any statement about supply structure.
SPARSE_MAX_LISTINGS = 3

# Share of ATTRIBUTED listings held by the largest seller at which the field
# is reported concentrated.
DOMINANT_SELLER_LISTING_SHARE = 0.6

# A field is reported crowded only when it is large on BOTH axes: many
# listings and many distinct sellers. Either alone is a different shape.
CROWDED_MIN_LISTINGS = 12
CROWDED_MIN_SELLERS = 6

# Minimum share of listings that must carry a seller before ANY structural
# claim is made. Below it the field's structure is reported unknown.
#
# Without this floor, a field of 20 listings where the marketplace attributed
# only 2 — both to one seller — would read as CONCENTRATED_FIELD: a structural
# finding built on 10% of the evidence, with the other 90% silently treated as
# if it did not exist. Computing the share over ALL listings instead would
# commit the same error in the opposite direction, reporting an unattributed
# field as not concentrated. Missing attribution is missing, not evidence of
# either shape.
MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE = 0.5

LIMITATIONS = (
    "Competition Opportunity observes the STRUCTURE of marketplace supply. It "
    "is never a measure of saturation, entry difficulty, or opportunity.",
    "Every field pattern carries two opposed readings that the evidence cannot "
    "resolve. The patterns are unordered: none is better or worse than another.",
    "Marketplace search results are a provider-ordered sample, not the full "
    "market, so a sparse field is weak evidence of an unserved need.",
    "A listing is supply, never a sale. This module reads no review count, "
    "rating, or price, and restates no Milestone 4A, 4B, or 4D finding.",
    "Seller identity approximates a competitor. One operator running several "
    "storefronts counts as several sellers; this is an unvalidated assumption.",
    "Listings whose seller the marketplace did not report are excluded from "
    "concentration rather than merged into one fictional seller.",
    "Thresholds are unvalidated V1 assumptions, not calibrated against any "
    "outcome data.",
)


class CompetitionFieldPattern(str, Enum):
    """The SHAPE of the observable competitive field. Never its desirability.

    These are deliberately UNORDERED. CROWDED_FIELD is not worse than
    SPARSE_FIELD, and CONCENTRATED_FIELD is neither a warning nor an
    invitation. Each pattern's two opposed readings live in PATTERN_READINGS
    and are emitted together on every result.
    """

    # The marketplace capability did not produce a usable answer. Not an
    # absence of competitors — an absence of measurement.
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    # The capability ran and returned no relevant listing for this candidate.
    # A measured absence, which is still not a measure of opportunity.
    NO_LISTINGS_OBSERVED = "NO_LISTINGS_OBSERVED"
    # Few enough listings that supply structure cannot be characterized.
    SPARSE_FIELD = "SPARSE_FIELD"
    # Listings were observed, but too few of them carry a seller for supply
    # structure to be characterized. Distinct from an observed absence.
    FIELD_STRUCTURE_UNKNOWN = "FIELD_STRUCTURE_UNKNOWN"
    # One seller holds most of the attributed listings.
    CONCENTRATED_FIELD = "CONCENTRATED_FIELD"
    # Many listings spread across many sellers, none dominant.
    CROWDED_FIELD = "CROWDED_FIELD"
    # Several sellers, none dominant, without the scale of a crowded field.
    FRAGMENTED_FIELD = "FRAGMENTED_FIELD"


@dataclass(slots=True, frozen=True)
class CompetitionReadings:
    """The two opposed readings of one field pattern, permanently paired.

    `market_exists_reading` is the case the same evidence makes FOR the field
    being worth entering. `entry_difficulty_reading` is the case the SAME
    evidence makes against it. Neither is preferred, and neither may be
    emitted without the other: that pairing is how this module encodes the
    non-monotonicity of competition as structure instead of prose.
    """

    market_exists_reading: str
    entry_difficulty_reading: str
    unresolvable_because: str


# Every pattern appears here, with both readings populated. A test asserts
# the mapping is total and that no reading is empty, so a future pattern
# cannot be added without stating what it argues in both directions.
PATTERN_READINGS: dict[CompetitionFieldPattern, CompetitionReadings] = {
    CompetitionFieldPattern.UNKNOWN_FIELD: CompetitionReadings(
        market_exists_reading=(
            "Nothing was measured. The field may be busy or empty; this "
            "result argues neither."
        ),
        entry_difficulty_reading=(
            "Nothing was measured. Difficulty is unknown and must not be "
            "read as low."
        ),
        unresolvable_because=(
            "The marketplace capability did not produce a usable answer for "
            "this candidate."
        ),
    ),
    CompetitionFieldPattern.NO_LISTINGS_OBSERVED: CompetitionReadings(
        market_exists_reading=(
            "No competing listing was found, which is consistent with a need "
            "nobody currently serves."
        ),
        entry_difficulty_reading=(
            "No competing listing was found, which is equally consistent with "
            "a need nobody found worth serving. Absence of supply is not "
            "evidence of demand."
        ),
        unresolvable_because=(
            "Distinguishing an unserved need from an unviable one requires "
            "buyer demand evidence, which no marketplace listing provides."
        ),
    ),
    CompetitionFieldPattern.SPARSE_FIELD: CompetitionReadings(
        market_exists_reading=(
            "A small number of sellers have committed to this field, and the "
            "field is not visibly contested."
        ),
        entry_difficulty_reading=(
            "A small number of listings is also what a field looks like when "
            "sellers tried it and stopped. The sample is too small to "
            "characterize supply at all."
        ),
        unresolvable_because=(
            "Whether few listings mean room or rejection requires knowing why "
            "sellers are absent, which listings never record."
        ),
    ),
    CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN: CompetitionReadings(
        market_exists_reading=(
            "Listings exist, so the field is served by someone."
        ),
        entry_difficulty_reading=(
            "How supply is distributed is unknown, so the field may be one "
            "seller's catalog or many sellers' listings."
        ),
        unresolvable_because=(
            "The marketplace attributed too few listings to a seller for any "
            "concentration statistic to describe the field."
        ),
    ),
    CompetitionFieldPattern.CONCENTRATED_FIELD: CompetitionReadings(
        market_exists_reading=(
            "One seller has invested in a catalog here, which is consistent "
            "with a need worth serving that others have not contested."
        ),
        entry_difficulty_reading=(
            "One seller already occupies most of the observable field, which "
            "is equally consistent with an incumbent an entrant does not "
            "displace."
        ),
        unresolvable_because=(
            "Whether a dominant seller is unchallenged or unassailable "
            "requires competitor performance data, which is not public."
        ),
    ),
    CompetitionFieldPattern.CROWDED_FIELD: CompetitionReadings(
        market_exists_reading=(
            "Many sellers sustain many listings here, which is the strongest "
            "observable sign that the field supports supply at all."
        ),
        entry_difficulty_reading=(
            "Many sellers sustain many listings here, so buyer attention is "
            "already divided among them."
        ),
        unresolvable_because=(
            "Whether a crowded field validates demand or exhausts it requires "
            "the size of that demand, which supply never measures."
        ),
    ),
    CompetitionFieldPattern.FRAGMENTED_FIELD: CompetitionReadings(
        market_exists_reading=(
            "Several sellers hold small positions and none dominates, which "
            "is consistent with a field no incumbent has locked up."
        ),
        entry_difficulty_reading=(
            "Several sellers holding small positions is equally consistent "
            "with a commoditized field where nothing differentiates."
        ),
        unresolvable_because=(
            "Whether fragmentation means an opening or an absence of "
            "advantage requires product differentiation evidence, which "
            "listing counts never carry."
        ),
    ),
}


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order over identifiers of any type, without coercing them.

    Same rule Milestone 4D-0.1 established for 4A/4B provenance: the type name
    breaks the tie `str()` alone leaves between distinct values that render
    identically, such as 1 and "1".
    """
    return (str(value), type(value).__name__)


@dataclass(slots=True, frozen=True)
class CompetitionFieldProvenance:
    """Lineage of every derived feature, preserved intact."""

    candidate_id: UUID
    evidence_ids: tuple[UUID, ...]
    listing_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    providers: tuple[str, ...]
    marketplaces: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    duplicate_evidence_suppressed: int


@dataclass(slots=True, frozen=True)
class CompetitionFieldFeatures:
    """Structural features of the observable competitive field.

    Every count is identity-based, so a re-delivered listing and a repeated
    research run cannot inflate any of them.
    """

    # Size of the field. Same population, by construction, as Milestone 4A's
    # relevant_comparable_count: both come from collect_listing_views filtered
    # by the shared is_format_relevant predicate. It is restated here so the
    # concentration features below are interpretable without joining to 4A,
    # NOT as an independent corroboration of that count. A regression test
    # pins the two together.
    competing_listing_count: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int

    # Seller attribution. Listings the marketplace did not attribute are
    # reported, never merged into one fictional seller and never dropped
    # silently from the field size.
    listings_with_seller_attribution: int
    listings_without_seller_attribution: int
    # Identical, by construction, to 4A's distinct_seller_count — same
    # listings, same predicate. Restated for the same reason as above and
    # pinned by the same regression test.
    seller_count_in_field: int
    # How much of the field the concentration statistics below actually
    # describe. None when there is no field to describe. A consumer must read
    # every share below as a statement about this share of the listings, not
    # about all of them.
    seller_attribution_share: float | None

    # Supply concentration. These are shares of LISTINGS — supply structure.
    # They are NOT 4A's top_seller_proxy_share, which is a share of observed
    # review volume, a different quantity over a different population.
    # None whenever no listing carries a seller, because a share with no
    # denominator is unknown, never zero.
    top_seller_listing_share: float | None
    max_listings_per_seller: int | None
    single_listing_seller_count: int | None
    sellers_covering_half_of_listings: int | None

    features_version: str = COMPETITION_FEATURES_VERSION


@dataclass(slots=True, frozen=True)
class CompetitionOpportunityResult:
    """Competition Opportunity for one candidate: shape, readings, lineage."""

    candidate_id: UUID
    state: DimensionState
    # Permanently None in 4E. No competition scoring formula is approved, and
    # a non-monotonic quantity has no defensible single number.
    value: float | None
    pattern: CompetitionFieldPattern
    readings: CompetitionReadings
    features: CompetitionFieldFeatures | None
    provenance: CompetitionFieldProvenance

    # Derived numbers are INFERRED even when every input was OBSERVED.
    value_truth_class: TruthClass | None
    features_truth_class: TruthClass | None
    # The most conservative class among contributing evidence, reported
    # separately so a derivation can never look better than its sources.
    evidence_truth_basis: TruthClass | None

    # Permanent UNKNOWN markers. Each names a conclusion that observable
    # supply structure cannot support, so no consumer can read one out of
    # this result by accident.
    saturation: TruthClass = TruthClass.UNKNOWN
    entry_difficulty: TruthClass = TruthClass.UNKNOWN
    win_probability: TruthClass = TruthClass.UNKNOWN
    differentiation_opportunity: TruthClass = TruthClass.UNKNOWN
    competitor_strength: TruthClass = TruthClass.UNKNOWN
    competitor_revenue: TruthClass = TruthClass.UNKNOWN
    market_share_available: TruthClass = TruthClass.UNKNOWN

    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_COMPETITION_OPPORTUNITY
    version: str = COMPETITION_OPPORTUNITY_VERSION
    pattern_version: str = COMPETITION_FIELD_PATTERN_VERSION


# ------------------------------------------------------------------ internals


def _seller_listing_counts(listings: list[ListingView]) -> dict[str, int]:
    """Listings per attributed seller. Unattributed listings are excluded."""
    counts: dict[str, int] = {}
    for listing in listings:
        if listing.seller_id is not None:
            counts[listing.seller_id] = counts.get(listing.seller_id, 0) + 1
    return counts


def _sellers_covering_half(counts: dict[str, int]) -> int:
    """Fewest sellers accounting for at least half the attributed listings.

    Deterministic by construction rather than by tie-break: the answer is a
    function of the MULTISET of per-seller counts alone, since sellers are
    taken largest-first and only the running sum decides when to stop. Two
    sellers holding the same number of listings are interchangeable here, so
    no permutation of them can change the result.

    The identifier tie-break is nevertheless applied, so the iteration order
    is total should this ever return the sellers themselves rather than a
    count. A test pins the multiset property directly, because a test that
    only shuffled the input would pass even if the tie-break were removed.
    """
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], _id_sort_key(kv[0])))
    running = 0
    covered = 0
    for _seller, count in ordered:
        running += count
        covered += 1
        if running * 2 >= total:
            break
    return covered


def _build_features(listings: list[ListingView]) -> CompetitionFieldFeatures:
    excluded_physical = sum(1 for listing in listings if not listing.is_format_relevant)
    relevant = [listing for listing in listings if listing.is_format_relevant]
    unknown_format = sum(1 for listing in relevant if listing.is_digital is None)

    counts = _seller_listing_counts(relevant)
    attributed = sum(counts.values())
    attribution_share = (
        round(attributed / len(relevant), 4) if relevant else None
    )

    top_share: float | None = None
    max_per_seller: int | None = None
    single_listing_sellers: int | None = None
    half_cover: int | None = None
    if counts and attributed > 0:
        max_per_seller = max(counts.values())
        top_share = round(max_per_seller / attributed, 4)
        single_listing_sellers = sum(1 for count in counts.values() if count == 1)
        half_cover = _sellers_covering_half(counts)

    return CompetitionFieldFeatures(
        competing_listing_count=len(relevant),
        excluded_physical_listing_count=excluded_physical,
        unknown_format_listing_count=unknown_format,
        listings_with_seller_attribution=attributed,
        listings_without_seller_attribution=len(relevant) - attributed,
        seller_count_in_field=len(counts),
        seller_attribution_share=attribution_share,
        top_seller_listing_share=top_share,
        max_listings_per_seller=max_per_seller,
        single_listing_seller_count=single_listing_sellers,
        sellers_covering_half_of_listings=half_cover,
    )


def _classify(features: CompetitionFieldFeatures) -> CompetitionFieldPattern:
    """competition_field_pattern_v1. Describes field shape, not desirability.

    Order matters: each rule is reached only when the ones above it did not
    apply, which is what keeps the classification explainable. Nothing here
    ranks the outcomes, and the order of the rules is not an order over the
    patterns they return.
    """
    if features.competing_listing_count == 0:
        return CompetitionFieldPattern.NO_LISTINGS_OBSERVED
    if features.competing_listing_count <= SPARSE_MAX_LISTINGS:
        # Too few listings to characterize supply structure at all, whatever
        # the seller attribution happens to be.
        return CompetitionFieldPattern.SPARSE_FIELD
    if (
        features.seller_attribution_share is None
        or features.seller_attribution_share < MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE
    ):
        # Listings exist; too few say who owns them. Reporting a shape here
        # would describe a minority of the field as if it were the field.
        return CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN
    if (
        features.top_seller_listing_share is not None
        and features.top_seller_listing_share >= DOMINANT_SELLER_LISTING_SHARE
    ):
        return CompetitionFieldPattern.CONCENTRATED_FIELD
    if (
        features.competing_listing_count >= CROWDED_MIN_LISTINGS
        and features.seller_count_in_field >= CROWDED_MIN_SELLERS
    ):
        return CompetitionFieldPattern.CROWDED_FIELD
    return CompetitionFieldPattern.FRAGMENTED_FIELD


def _build_provenance(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    listings: list[ListingView],
    evidence_ids: tuple[UUID, ...],
    suppressed: int,
) -> CompetitionFieldProvenance:
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    return CompetitionFieldProvenance(
        candidate_id=candidate_id,
        # sorted(), never set(): an id may legitimately repeat, and dropping
        # the repeat would contradict duplicate_evidence_suppressed.
        evidence_ids=tuple(sorted(evidence_ids)),
        listing_ids=tuple(
            sorted((listing.listing_id for listing in listings), key=_id_sort_key)
        ),
        seller_ids=tuple(
            sorted(
                {
                    listing.seller_id
                    for listing in listings
                    if listing.seller_id is not None
                },
                key=_id_sort_key,
            )
        ),
        providers=tuple(sorted({item.provider for item in contributing})),
        marketplaces=tuple(
            sorted({item.marketplace for item in contributing if item.marketplace})
        ),
        source_truth_classes=tuple(
            sorted({item.truth_class.value for item in contributing})
        ),
        duplicate_evidence_suppressed=suppressed,
    )


# ----------------------------------------------------------------- extraction


def extract_competition_opportunity(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    missing_reasons: dict[str, str] | None = None,
) -> CompetitionOpportunityResult:
    """Derive Competition Opportunity for one candidate from stored evidence.

    Deterministic and pure: identical evidence yields identical output,
    whatever order it arrives in. No provider calls, no LLM, no fabricated
    values, and no conversion of supply into demand, sales, or opportunity.

    `missing_reasons` maps a capability name to why it produced nothing. It is
    what keeps a failed marketplace call from reading as an empty field: with
    a reason present and no listing observed, the field is UNKNOWN, not empty.
    """
    reasons = missing_reasons or {}
    listings, suppressed, evidence_ids = collect_listing_views(evidence)
    provenance = _build_provenance(
        candidate_id, evidence, listings, evidence_ids, suppressed
    )
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    basis = weakest_truth_class(contributing)

    capability_reason = reasons.get(CAPABILITY_MARKETPLACE)

    if not listings:
        if capability_reason is not None:
            # The capability never delivered an answer. Zero competitors was
            # not observed; nothing was.
            return CompetitionOpportunityResult(
                candidate_id=candidate_id,
                state=DimensionState.MISSING,
                value=None,
                pattern=CompetitionFieldPattern.UNKNOWN_FIELD,
                readings=PATTERN_READINGS[CompetitionFieldPattern.UNKNOWN_FIELD],
                features=None,
                provenance=provenance,
                value_truth_class=None,
                features_truth_class=None,
                evidence_truth_basis=basis,
                missing_reason=capability_reason,
            )
        return CompetitionOpportunityResult(
            candidate_id=candidate_id,
            state=DimensionState.UNKNOWN,
            value=None,
            pattern=CompetitionFieldPattern.NO_LISTINGS_OBSERVED,
            readings=PATTERN_READINGS[CompetitionFieldPattern.NO_LISTINGS_OBSERVED],
            features=None,
            provenance=provenance,
            value_truth_class=None,
            features_truth_class=None,
            evidence_truth_basis=basis,
            missing_reason="no_marketplace_listings_returned_for_candidate",
        )

    features = _build_features(listings)
    pattern = _classify(features)

    if features.competing_listing_count == 0:
        # Listings were observed, and every one was excluded as an unrelated
        # (physical) format. The competitive field for a digital product was
        # therefore never measured — which is missing, not empty.
        return CompetitionOpportunityResult(
            candidate_id=candidate_id,
            state=DimensionState.MISSING,
            value=None,
            pattern=CompetitionFieldPattern.NO_LISTINGS_OBSERVED,
            readings=PATTERN_READINGS[CompetitionFieldPattern.NO_LISTINGS_OBSERVED],
            features=features,
            provenance=provenance,
            value_truth_class=None,
            features_truth_class=TruthClass.INFERRED,
            evidence_truth_basis=basis,
            missing_reason="all_listings_excluded_as_unrelated_format",
        )

    return CompetitionOpportunityResult(
        candidate_id=candidate_id,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        value=None,
        pattern=pattern,
        readings=PATTERN_READINGS[pattern],
        features=features,
        provenance=provenance,
        value_truth_class=None,
        # Derived from OBSERVED inputs, but itself a derivation.
        features_truth_class=TruthClass.INFERRED,
        evidence_truth_basis=basis,
    )
