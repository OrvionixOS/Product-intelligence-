from uuid import uuid4

from app.domain.enums import Classification, EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem, ScoreDimensions
from app.services.scoring import evidence_confidence, score_opportunity, weighted_opportunity_score


def observed_evidence(**overrides):
    payload = dict(
        opportunity_id=uuid4(),
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=TruthClass.OBSERVED,
        provider="test",
        collection_method="official_api",
        source_quality=0.95,
        directness=0.9,
        independence=0.9,
        sample_adequacy=0.8,
        freshness=1.0,
        cross_source_agreement=0.8,
        geographic_relevance=1.0,
        marketplace_relevance=0.9,
    )
    payload.update(overrides)
    return EvidenceItem(**payload)


def test_missing_dimensions_are_redistributed_not_zeroed():
    dims = ScoreDimensions(purchase_evidence=80, search_demand=80)
    score, missing = weighted_opportunity_score(dims)
    assert score == 80
    assert "audience_interest" in missing


def test_confidence_does_not_inflate_with_duplicate_count():
    one = observed_evidence()
    c1 = evidence_confidence([one])
    c2 = evidence_confidence([one, one, one, one])
    assert c1 == c2


def test_high_score_low_confidence_is_yellow():
    dims = ScoreDimensions(
        purchase_evidence=90,
        search_demand=90,
        audience_interest=90,
        price_strength=80,
        competition_opportunity=80,
        buyer_reach=90,
        problem_product_fit=90,
    )
    weak = observed_evidence(
        source_quality=0.3,
        directness=0.3,
        independence=0.3,
        sample_adequacy=0.3,
        freshness=0.3,
        cross_source_agreement=0.3,
        geographic_relevance=0.3,
        marketplace_relevance=0.3,
    )
    result = score_opportunity(dims, [weak])
    assert result.classification == Classification.YELLOW


def test_no_demand_kill_rule_is_red():
    dims = ScoreDimensions(
        purchase_evidence=0,
        search_demand=10,
        audience_interest=100,
        price_strength=80,
        competition_opportunity=70,
        buyer_reach=90,
        problem_product_fit=90,
    )
    result = score_opportunity(dims, [observed_evidence()])
    assert result.classification == Classification.RED
    assert "NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL" in result.kill_rules_triggered
