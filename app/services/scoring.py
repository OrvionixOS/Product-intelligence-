"""UNAPPROVED EXPERIMENTAL SCORING — DO NOT USE FOR PRODUCT RESULTS.

Milestone 0 built this module as a foundation exercise: a deterministic
shape for a future Product Opportunity Score and Evidence Confidence Score.
The *shape* is what Milestone 0 approved. The numbers in it are not.

Specifically unapproved, and therefore not usable as a product result:

- the POS dimension weights in `WEIGHTS`
- the confidence weights in `CONFIDENCE_WEIGHTS`
- the RED/YELLOW/GREEN thresholds in `classify`
- the kill rules in `apply_kill_rules`

None of these appear in an approved repository specification. They are
placeholder v0.1 values, never validated against evidence, and a
classification derived from them must never be presented to a user as a
verdict on a candidate. ARCHITECTURE.md places the real Opportunity Score,
Evidence Confidence, and RED/YELLOW/GREEN at pipeline steps 8-9, after deep
research — work that has not been specified or built.

This module is retained deliberately: the approved final scoring engine may
reuse or refactor this work, and the Milestone 0 tests keep documenting the
intended structure. It is unreachable from the API:

- `POST /score` has been removed. It returns 410 Gone for every request and
  there is no configuration that re-enables it;
- `app/api/routes.py` no longer imports this module at all, so no served
  route can reach it even by mistake;
- calling `score_opportunity` emits a DeprecationWarning;
- the Milestone 3C preliminary-ranking path must never import or call
  anything here, and `tests/test_orchestration.py` enforces every one of
  these guarantees statically and at runtime.

Import it directly only from tests, or from the future approved engine once
its weights and thresholds are specified. The final scoring engine is not
designed here, and these placeholders must not simply be promoted into it.
"""

import warnings
from collections.abc import Iterable

from app.domain.enums import Classification, TruthClass
from app.domain.models import EvidenceItem, ScoreDimensions, ScoreResult

# Machine-readable quarantine marker. Anything that finds this constant on a
# module is looking at unapproved scoring logic, not a product surface.
SCORING_STATUS = "UNAPPROVED_EXPERIMENTAL"

UNAPPROVED_SCORING_NOTICE = (
    "app.services.scoring contains unapproved placeholder POS weights, "
    "Evidence Confidence weights, kill rules, and RED/YELLOW/GREEN "
    "thresholds. Its output is not a valid product result and must not be "
    "presented as one."
)

WEIGHTS = {
    "purchase_evidence": 0.25,
    "search_demand": 0.15,
    "audience_interest": 0.10,
    "price_strength": 0.10,
    "competition_opportunity": 0.15,
    "buyer_reach": 0.15,
    "problem_product_fit": 0.10,
}

CONFIDENCE_WEIGHTS = {
    "source_quality": 0.25,
    "directness": 0.20,
    "independence": 0.15,
    "sample_adequacy": 0.10,
    "freshness": 0.10,
    "cross_source_agreement": 0.10,
    "geographic_relevance": 0.05,
    "marketplace_relevance": 0.05,
}


def weighted_opportunity_score(dimensions: ScoreDimensions) -> tuple[float, list[str]]:
    values = dimensions.model_dump()
    numerator = 0.0
    denominator = 0.0
    missing: list[str] = []

    for name, weight in WEIGHTS.items():
        value = values[name]
        if value is None:
            missing.append(name)
            continue
        numerator += weight * value
        denominator += weight

    if denominator == 0:
        return 0.0, missing
    return round(numerator / denominator, 2), missing


def evidence_confidence(items: Iterable[EvidenceItem]) -> float:
    items = [i for i in items if i.truth_class != TruthClass.UNKNOWN]
    if not items:
        return 0.0

    per_item_scores: list[float] = []
    for item in items:
        score = sum(
            getattr(item, key) * weight
            for key, weight in CONFIDENCE_WEIGHTS.items()
        )
        per_item_scores.append(score)

    # Mean avoids inflating confidence merely by adding more observations.
    # Sample adequacy and independence are represented inside each evidence item.
    return round(100 * sum(per_item_scores) / len(per_item_scores), 2)


def apply_kill_rules(dimensions: ScoreDimensions, evidence: list[EvidenceItem]) -> list[str]:
    kills: list[str] = []

    if dimensions.purchase_evidence is not None and dimensions.search_demand is not None:
        if dimensions.purchase_evidence == 0 and dimensions.search_demand < 15:
            kills.append("NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL")

    if dimensions.buyer_reach is not None and dimensions.buyer_reach < 20:
        kills.append("NO_IDENTIFIABLE_DISTRIBUTION_ROUTE")

    if dimensions.competition_opportunity is None:
        # Unknown differentiation blocks GREEN but is not a hard RED by itself.
        pass

    return kills


def classify(opportunity_score: float, confidence: float, kills: list[str]) -> Classification:
    if kills:
        return Classification.RED
    if opportunity_score < 50:
        return Classification.RED
    if opportunity_score >= 70 and confidence >= 70:
        return Classification.GREEN
    return Classification.YELLOW


def score_opportunity(
    dimensions: ScoreDimensions,
    evidence: list[EvidenceItem],
) -> ScoreResult:
    warnings.warn(UNAPPROVED_SCORING_NOTICE, DeprecationWarning, stacklevel=2)
    score, missing = weighted_opportunity_score(dimensions)
    confidence = evidence_confidence(evidence)
    kills = apply_kill_rules(dimensions, evidence)

    # Missing competition/differentiation prevents GREEN.
    classification = classify(score, confidence, kills)
    if dimensions.competition_opportunity is None and classification == Classification.GREEN:
        classification = Classification.YELLOW

    return ScoreResult(
        opportunity_score=score,
        evidence_confidence=confidence,
        classification=classification,
        dimensions=dimensions,
        kill_rules_triggered=kills,
        missing_dimensions=missing,
    )
