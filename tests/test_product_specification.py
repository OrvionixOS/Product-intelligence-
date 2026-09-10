"""Milestone 4C tests: the product specification generator.

No network, no live API calls, no LLM. Everything is derived from evidence
built in-memory in exactly the shape Milestone 3A stores it.

Central invariants under test: the generator never fabricates demand,
purchases, revenue, conversion, or willingness to pay; it never presents an
asking price as a transaction price; it never upgrades a claim class; a
derived field is capped by the weakest source it rests on; an ideal format
outside V1 build capability is labelled as such and kept separate from the
buildable format; and contradictory signals are reported rather than
resolved silently.
"""

import random
import re
from dataclasses import asdict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import EvidencePurpose, ProductFormat, TruthClass
from app.domain.models import Candidate, EvidenceItem
from app.main import app
from app.services.price_evidence import extract_price_evidence
from app.services.product_specification import (
    FORMAT_ASSETS,
    JOB_TOKEN_STOPWORDS,
    EvidenceOwnershipError,
    ObservedText,
    unsupported_claim_in,
    FORMAT_SELECTION_VERSION,
    FORMAT_STRUCTURE,
    IDEAL_TO_BUILDABLE,
    JOB_TAXONOMY_VERSION,
    JOB_TO_IDEAL_FORMAT,
    JOB_TOKENS,
    PRODUCT_SPECIFICATION_VERSION,
    REWRITABLE_PROSE_FIELDS,
    V1_BUILDABLE_IDEAL_FORMATS,
    IdealFormat,
    JobToBeDone,
    ProseGenerationError,
    SpecClaimClass,
    SpecificationState,
    TemplateProseProvider,
    apply_prose,
    cap_claim_class,
    claim_from_truth,
    classify_job,
    collect_observed_text,
    generate_product_specification,
    observed_competitor_patterns,
    recommend_format,
    validate_prose,
)
from app.services.purchase_evidence import extract_purchase_evidence
from app.storage.memory import ResearchStore
from tests.test_orchestration import _transitive_app_imports
from tests.test_purchase_evidence import NOW, comparable, evidence_for_listing

SPEC_MODULE = "app.services.product_specification"
LEGACY_SCORING_MODULE = "app.services.scoring"


# ----------------------------------------------------------------- fixtures


def make_spec_candidate(
    title: str = "Freelance Quarterly Tax Estimator",
    problem: str = "Freelancers cannot work out how much tax to set aside",
    buyer_outcome: str = "Know the amount to set aside each quarter",
    proposed_format: ProductFormat = ProductFormat.PDF_GUIDE,
) -> Candidate:
    return Candidate(
        seed_keyword="freelance tax",
        title=title,
        problem=problem,
        target_buyer="UK freelancers filing self assessment",
        proposed_format=proposed_format,
        buyer_outcome=buyer_outcome,
        search_queries=["freelance tax calculator"],
        marketplace_queries=["freelance tax spreadsheet"],
        content_queries=["freelance tax uk"],
        generation_reason="test fixture",
    )


def keyword_evidence(
    candidate_id, keyword: str, truth_class: TruthClass = TruthClass.OBSERVED
) -> EvidenceItem:
    return EvidenceItem(
        candidate_id=candidate_id,
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=truth_class,
        provider="dataforseo",
        collection_method="official_api",
        raw_value=1200,
        raw_payload={"keyword": keyword},
    )


def video_evidence(
    candidate_id, title: str, tags: list[str] | None = None
) -> EvidenceItem:
    return EvidenceItem(
        candidate_id=candidate_id,
        signal_type="public_video_view_count",
        purpose=EvidencePurpose.AUDIENCE,
        truth_class=TruthClass.OBSERVED,
        provider="youtube",
        collection_method="official_api",
        raw_value=50000,
        raw_payload={"title": title, "tags": tags or []},
    )


def listing_evidence(candidate_id, listings) -> list[EvidenceItem]:
    return [
        item
        for listing in listings
        for item in evidence_for_listing(listing, candidate_id)
    ]


def build_spec(candidate=None, evidence=None, with_derivations: bool = True):
    candidate = candidate or make_spec_candidate()
    evidence = evidence if evidence is not None else []
    price = purchase = None
    if with_derivations and evidence:
        price = extract_price_evidence(candidate.id, evidence, now=NOW)
        purchase = extract_purchase_evidence(candidate.id, evidence, now=NOW)
    return generate_product_specification(
        candidate=candidate,
        evidence=evidence,
        price_evidence=price,
        purchase_evidence=purchase,
    )


def spec_text(specification) -> str:
    """Every string the specification would show a caller."""
    return str(asdict(specification))


# --------------------------------------------------------- claim classes


def test_estimated_never_becomes_observed():
    """The single most dangerous upgrade: ESTIMATED presented as OBSERVED."""
    assert claim_from_truth(TruthClass.ESTIMATED) == SpecClaimClass.INFERRED
    assert claim_from_truth(TruthClass.ESTIMATED) != SpecClaimClass.OBSERVED


def test_every_truth_class_maps_conservatively():
    expected = {
        TruthClass.OBSERVED: SpecClaimClass.OBSERVED,
        TruthClass.ESTIMATED: SpecClaimClass.INFERRED,
        TruthClass.INFERRED: SpecClaimClass.INFERRED,
        TruthClass.UNKNOWN: SpecClaimClass.UNKNOWN,
    }
    for truth in TruthClass:
        assert claim_from_truth(truth) == expected[truth], truth
    assert claim_from_truth(None) == SpecClaimClass.UNKNOWN


def test_cap_claim_class_takes_the_weakest():
    assert (
        cap_claim_class(SpecClaimClass.OBSERVED, SpecClaimClass.ASSUMED)
        == SpecClaimClass.ASSUMED
    )
    assert (
        cap_claim_class(SpecClaimClass.INFERRED, SpecClaimClass.UNKNOWN)
        == SpecClaimClass.UNKNOWN
    )
    assert cap_claim_class(SpecClaimClass.OBSERVED) == SpecClaimClass.OBSERVED


def test_cap_claim_class_can_never_upgrade():
    """Adversarial: no combination of inputs produces a stronger class."""
    strength = ["OBSERVED", "INFERRED", "ASSUMED", "UNKNOWN"]
    for a in SpecClaimClass:
        for b in SpecClaimClass:
            capped = cap_claim_class(a, b)
            assert strength.index(capped.value) >= max(
                strength.index(a.value), strength.index(b.value)
            )


def test_no_inputs_caps_to_unknown():
    assert cap_claim_class() == SpecClaimClass.UNKNOWN
    assert cap_claim_class(None) == SpecClaimClass.UNKNOWN


# ------------------------------------------------------ text harvesting


def test_only_observed_evidence_contributes_text():
    """A record whose truth class is not OBSERVED contributes nothing."""
    candidate = make_spec_candidate()
    observed = collect_observed_text(
        [
            keyword_evidence(candidate.id, "tax calculator", TruthClass.OBSERVED),
            keyword_evidence(candidate.id, "invented keyword", TruthClass.UNKNOWN),
            keyword_evidence(candidate.id, "estimated keyword", TruthClass.ESTIMATED),
        ]
    )
    texts = [entry.text for entry in observed]
    assert texts == ["tax calculator"]


def test_harvested_text_keeps_its_evidence_id():
    candidate = make_spec_candidate()
    item = keyword_evidence(candidate.id, "budget planner")
    observed = collect_observed_text([item])
    assert observed[0].evidence_id == item.id
    assert observed[0].signal_type == "search_volume"


def test_listing_without_observed_price_still_yields_its_title():
    """Adversarial: the price record is UNKNOWN when no price was returned.

    The listing title is still an observation. Reading titles off the price
    record would silently discard every priceless competitor.
    """
    candidate = make_spec_candidate()
    evidence = listing_evidence(
        candidate.id, [comparable("L1", price=None, currency=None)]
    )
    patterns = observed_competitor_patterns(evidence)
    assert any("Listing L1" in (p.value or "") for p in patterns)


# ------------------------------------------------- job classification


def test_observed_text_yields_inferred_never_observed():
    """A job is derived from text; it is never an observation of the buyer."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "quarterly tax calculator")]
    job = classify_job(collect_observed_text(evidence), candidate)
    assert job.job == JobToBeDone.CALCULATE
    assert job.claim_class == SpecClaimClass.INFERRED
    assert job.claim_class != SpecClaimClass.OBSERVED
    assert job.evidence_ids == (evidence[0].id,)


def test_basis_separates_observed_matches_from_candidate_text():
    """The basis must not credit a Milestone 1 hypothesis as evidence."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "quarterly tax calculator")]
    job = classify_job(collect_observed_text(evidence), candidate)
    assert "observed evidence record(s) carrying" in job.basis
    assert "candidate text contributed a further" in job.basis


def test_candidate_text_alone_is_assumed_not_inferred():
    """Milestone 1 text is a hypothesis, so it cannot support INFERRED."""
    candidate = make_spec_candidate()
    job = classify_job([], candidate)
    assert job.job == JobToBeDone.CALCULATE
    assert job.claim_class == SpecClaimClass.ASSUMED
    assert job.evidence_ids == ()
    assert "not evidence" in job.basis


def test_no_matching_tokens_is_unknown():
    candidate = make_spec_candidate(
        title="Zzz", problem="Zzz zzz", buyer_outcome="Zzz zzz zzz"
    )
    job = classify_job([], candidate)
    assert job.job == JobToBeDone.UNKNOWN
    assert job.claim_class == SpecClaimClass.UNKNOWN
    assert job.matched_tokens == ()


def test_tied_signals_report_unknown_rather_than_guessing():
    """Conservative on conflict: a tie is reported, never broken arbitrarily."""
    candidate = make_spec_candidate(
        title="Calculate and organise", problem="calculate", buyer_outcome="organise"
    )
    job = classify_job([], candidate)
    assert job.job == JobToBeDone.UNKNOWN
    assert "did not separate" in job.basis
    assert len(job.scores) >= 2


def test_job_classification_is_deterministic():
    candidate = make_spec_candidate()
    evidence = [
        keyword_evidence(candidate.id, "tax calculator"),
        video_evidence(candidate.id, "How to calculate freelance tax"),
    ]
    observed = collect_observed_text(evidence)
    first = classify_job(observed, candidate)
    second = classify_job(observed, candidate)
    assert first == second


def test_job_classification_stamps_its_taxonomy_version():
    job = classify_job([], make_spec_candidate())
    assert job.taxonomy_version == JOB_TAXONOMY_VERSION


def test_job_tokens_never_overlap_between_jobs():
    """A shared token would score two jobs at once and manufacture ties."""
    owner: dict[str, str] = {}
    for job, tokens in JOB_TOKENS.items():
        for token in tokens:
            assert token not in owner, (token, owner.get(token), job.value)
            owner[token] = job.value


def test_every_job_has_an_ideal_format_and_every_ideal_is_buildable_somewhere():
    """Completeness: no job can fall through the format mapping."""
    for job in JobToBeDone:
        if job == JobToBeDone.UNKNOWN:
            continue
        assert job in JOB_TOKENS
        assert job in JOB_TO_IDEAL_FORMAT
    for ideal in IdealFormat:
        assert ideal in IDEAL_TO_BUILDABLE
        assert IDEAL_TO_BUILDABLE[ideal] in ProductFormat


# ---------------------------------------------------- format selection


def test_ideal_format_outside_v1_is_labelled_and_kept_separate():
    """The core of decision A: two answers, never conflated."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")]
    job = classify_job(collect_observed_text(evidence), candidate)
    rec = recommend_format(job, candidate, evidence)

    assert rec.ideal_format == IdealFormat.CALCULATOR
    assert rec.outside_v1_build_capability is True
    assert rec.buildable_v1_format == ProductFormat.SPREADSHEET_TOOL
    assert "OUTSIDE current V1 build capability" in rec.rationale
    assert rec.buildable_v1_format.value in {f.value for f in ProductFormat}


def test_ideal_format_is_always_assumed_even_with_observed_evidence():
    """An ideal format is a design judgment. Evidence never promotes it."""
    candidate = make_spec_candidate()
    evidence = [
        keyword_evidence(candidate.id, "tax calculator"),
        video_evidence(candidate.id, "calculate your tax", ["calculator"]),
    ]
    job = classify_job(collect_observed_text(evidence), candidate)
    rec = recommend_format(job, candidate, evidence)
    assert job.claim_class == SpecClaimClass.INFERRED
    assert rec.ideal_claim_class == SpecClaimClass.ASSUMED


def test_buildable_claim_class_is_capped_by_the_job():
    """A format derived from an ASSUMED job cannot be INFERRED."""
    candidate = make_spec_candidate()
    job = classify_job([], candidate)  # candidate text only -> ASSUMED
    rec = recommend_format(job, candidate, [])
    assert job.claim_class == SpecClaimClass.ASSUMED
    assert rec.buildable_claim_class == SpecClaimClass.ASSUMED


def test_unknown_job_recommends_no_ideal_format():
    candidate = make_spec_candidate(
        title="Zzz", problem="Zzz zzz", buyer_outcome="Zzz zzz zzz"
    )
    job = classify_job([], candidate)
    rec = recommend_format(job, candidate, [])
    assert rec.ideal_format is None
    assert rec.ideal_claim_class == SpecClaimClass.UNKNOWN
    # The Milestone 1 proposal is carried forward, clearly as a hypothesis.
    assert rec.buildable_v1_format == candidate.proposed_format
    assert rec.buildable_claim_class == SpecClaimClass.ASSUMED


def test_buildable_format_is_always_one_of_the_seven_approved_formats():
    """Adversarial: sweep every job and assert no unbuildable leak."""
    candidate = make_spec_candidate()
    for job_enum, ideal in JOB_TO_IDEAL_FORMAT.items():
        buildable = IDEAL_TO_BUILDABLE[ideal]
        assert isinstance(buildable, ProductFormat)
        if ideal in V1_BUILDABLE_IDEAL_FORMATS:
            assert ideal.value == buildable.value
    assert len(list(ProductFormat)) == 7
    assert candidate.proposed_format in ProductFormat


def test_observed_dominant_format_is_reported_not_copied():
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")] + listing_evidence(
        candidate.id, [comparable("L1"), comparable("L2")]
    )
    job = classify_job(collect_observed_text(evidence), candidate)
    rec = recommend_format(job, candidate, evidence)
    assert rec.observed_dominant_format == "download"
    # The market ships downloads; the job still drives the choice.
    assert rec.driven_by_job == JobToBeDone.CALCULATE
    assert rec.selection_version == FORMAT_SELECTION_VERSION


def test_dominant_format_counts_each_listing_once():
    """Adversarial: 3A emits three records per listing; count listings."""
    candidate = make_spec_candidate()
    evidence = listing_evidence(candidate.id, [comparable("L1"), comparable("L2")])
    from app.services.product_specification import observed_dominant_format

    dominant, count = observed_dominant_format(evidence)
    assert (dominant, count) == ("download", 2)


# ------------------------------------------------------ the specification


def test_no_evidence_is_missing_and_fabricates_nothing():
    """No evidence must never become an invented product."""
    spec = build_spec(evidence=[])
    assert spec.state == SpecificationState.MISSING
    assert spec.missing_reason == "no_evidence_collected_for_candidate"
    assert spec.insufficient_evidence is True
    assert spec.format_recommendation is None
    assert spec.structure == ()
    assert spec.required_assets == ()
    assert spec.supporting_evidence_ids == ()
    for field_name in ("product_name", "target_buyer", "buyer_job", "core_promise"):
        assert getattr(spec, field_name).value is None
        assert getattr(spec, field_name).claim_class == SpecClaimClass.UNKNOWN


def test_sparse_evidence_still_generates_but_flags_itself():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert spec.state == SpecificationState.GENERATED
    assert spec.insufficient_evidence is True
    assert spec.price_evidence_reference.available is False
    assert any("asking-price band" in u for u in spec.unknowns)


def test_full_evidence_generates_a_complete_specification():
    candidate = make_spec_candidate()
    evidence = (
        [keyword_evidence(candidate.id, "quarterly tax calculator")]
        + listing_evidence(
            candidate.id,
            [comparable(f"L{i}", price=10.0 + i, seller_id=f"shop-{i}") for i in range(6)],
        )
        + [video_evidence(candidate.id, "Calculate freelance tax", ["tax", "calculator"])]
    )
    spec = build_spec(candidate, evidence)
    assert spec.state == SpecificationState.GENERATED
    assert spec.product_name.value
    assert spec.structure
    assert spec.required_assets
    assert spec.price_evidence_reference.available is True
    assert spec.version == PRODUCT_SPECIFICATION_VERSION


def test_specification_is_deterministic():
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")] + listing_evidence(
        candidate.id, [comparable("L1"), comparable("L2")]
    )
    assert build_spec(candidate, evidence) == build_spec(candidate, evidence)


def test_target_buyer_is_assumed_because_no_buyer_research_exists():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert spec.target_buyer.value == candidate.target_buyer
    assert spec.target_buyer.claim_class == SpecClaimClass.ASSUMED
    assert "no qualitative" in spec.target_buyer.basis


def test_structure_and_assets_are_assumed_templates():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert all(m.claim_class == SpecClaimClass.ASSUMED for m in spec.structure)
    assert any("unvalidated V1 templates" in a for a in spec.assumptions)


def test_every_format_has_structure_and_assets():
    for product_format in ProductFormat:
        assert FORMAT_STRUCTURE.get(product_format), product_format
        assert FORMAT_ASSETS.get(product_format), product_format


# --------------------------------------------------- provenance & evidence


def test_supporting_evidence_ids_are_real_stored_ids():
    """Adversarial: no invented or renumbered evidence identifiers."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")] + listing_evidence(
        candidate.id, [comparable("L1")]
    )
    real_ids = {item.id for item in evidence}
    spec = build_spec(candidate, evidence)
    assert spec.supporting_evidence_ids
    assert set(spec.supporting_evidence_ids) <= real_ids
    for pattern in spec.observed_competitor_patterns:
        assert set(pattern.evidence_ids) <= real_ids
    assert set(spec.buyer_job.evidence_ids) <= real_ids


def test_competitor_patterns_are_observed_but_contents_stay_unknown():
    candidate = make_spec_candidate()
    evidence = listing_evidence(candidate.id, [comparable("L1"), comparable("L2")])
    spec = build_spec(candidate, evidence)
    patterns = spec.observed_competitor_patterns
    assert patterns
    assert all(p.claim_class == SpecClaimClass.OBSERVED for p in patterns)
    assert any("contents" in u and "UNKNOWN" in u for u in spec.unknowns)


def test_differentiation_is_always_a_hypothesis():
    """Competitor contents are not collected, so no gap can be observed."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")] + listing_evidence(
        candidate.id, [comparable("L1")]
    )
    spec = build_spec(candidate, evidence)
    assert spec.differentiation_opportunities
    for idea in spec.differentiation_opportunities:
        assert idea.claim_class in (SpecClaimClass.ASSUMED, SpecClaimClass.UNKNOWN)
        assert idea.claim_class != SpecClaimClass.OBSERVED


def test_evidence_for_another_candidate_is_refused():
    """Adversarial: foreign evidence would attach real ids to false claims."""
    candidate = make_spec_candidate()
    other = make_spec_candidate()
    with pytest.raises(EvidenceOwnershipError):
        generate_product_specification(
            candidate=candidate,
            evidence=[keyword_evidence(other.id, "tax calculator")],
        )


def test_evidence_without_a_candidate_id_is_accepted():
    """Not every stored record carries an owner; absence is not a mismatch."""
    candidate = make_spec_candidate()
    orphan = EvidenceItem(
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=TruthClass.OBSERVED,
        provider="dataforseo",
        collection_method="official_api",
        raw_payload={"keyword": "tax calculator"},
    )
    spec = generate_product_specification(candidate=candidate, evidence=[orphan])
    assert spec.state == SpecificationState.GENERATED


def test_endpoint_refuses_inline_evidence_for_another_candidate():
    client = TestClient(app)
    candidate = make_spec_candidate()
    other = make_spec_candidate()
    response = client.post(
        "/product/specification",
        json={
            "candidate": candidate.model_dump(mode="json"),
            "evidence": [
                keyword_evidence(other.id, "tax calculator").model_dump(mode="json")
            ],
        },
    )
    assert response.status_code == 422
    assert "another candidate" in response.json()["detail"]


def test_blank_prose_is_rejected():
    """An empty rewrite would erase a field rather than improve it."""
    with pytest.raises(ProseGenerationError):
        validate_prose("   ")
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    with pytest.raises(ProseGenerationError):
        apply_prose(spec, {"product_name": ""})


def test_every_field_carries_a_basis():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    fields = [
        spec.product_name,
        spec.target_buyer,
        spec.buyer_job,
        spec.core_promise,
        *spec.differentiation_opportunities,
        *spec.observed_competitor_patterns,
    ]
    for spec_field in fields:
        assert spec_field.basis.strip(), spec_field


# ------------------------------------------------------------ price truth


def test_price_reference_is_asking_price_and_never_a_recommendation():
    candidate = make_spec_candidate()
    evidence = listing_evidence(
        candidate.id,
        [comparable(f"L{i}", price=10.0 + i, seller_id=f"shop-{i}") for i in range(5)],
    )
    spec = build_spec(candidate, evidence)
    reference = spec.price_evidence_reference
    assert reference.available is True
    assert "ASKING" in reference.note
    assert "not a recommended price" in reference.note
    assert "willingness-to-pay" in reference.note
    assert not hasattr(reference, "recommended_price")
    assert not hasattr(reference, "optimal_price")


def test_price_currencies_are_never_combined_in_the_citation():
    candidate = make_spec_candidate()
    evidence = listing_evidence(
        candidate.id,
        [
            comparable("U1", price=10.0, currency="USD", seller_id="s1"),
            comparable("U2", price=12.0, currency="USD", seller_id="s2"),
            comparable("E1", price=90.0, currency="EUR", seller_id="s3"),
        ],
    )
    spec = build_spec(candidate, evidence)
    currencies = [band[0] for band in spec.price_evidence_reference.observed_asking_bands]
    assert sorted(currencies) == ["EUR", "USD"]
    assert len(set(currencies)) == len(currencies)


def test_missing_price_evidence_is_reported_not_zeroed():
    spec = build_spec(evidence=[])
    assert spec.price_evidence_reference.available is False
    assert spec.price_evidence_reference.free_listing_count is None


def test_purchase_pattern_is_cited_as_structure_not_desirability():
    candidate = make_spec_candidate()
    evidence = listing_evidence(
        candidate.id,
        [comparable(f"L{i}", seller_id=f"shop-{i}") for i in range(5)],
    )
    spec = build_spec(candidate, evidence)
    cited = [u for u in spec.unknowns if "market-structure pattern" in u]
    assert cited
    assert "units sold and revenue remain UNKNOWN" in cited[0]
    assert "not a prediction that this product will sell" in cited[0]


# ------------------------------------------------------------- conflicts


def test_conflicting_format_signals_are_preserved_not_hidden():
    """Milestone 1 proposed a guide; the job points elsewhere. Both survive."""
    candidate = make_spec_candidate(proposed_format=ProductFormat.PDF_GUIDE)
    evidence = [keyword_evidence(candidate.id, "tax calculator")]
    spec = build_spec(candidate, evidence)
    topics = {c.topic for c in spec.conflicts}
    assert "format_vs_candidate_hypothesis" in topics
    conflict = next(c for c in spec.conflicts if c.topic == "format_vs_candidate_hypothesis")
    assert "PDF_GUIDE" in conflict.description
    assert conflict.resolution


def test_market_divergence_is_recorded_as_a_conflict():
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "tax calculator")] + listing_evidence(
        candidate.id, [comparable("L1")]
    )
    spec = build_spec(candidate, evidence)
    assert any(c.topic == "format_vs_observed_market" for c in spec.conflicts)


def test_inconclusive_job_signals_are_reported_as_a_conflict():
    candidate = make_spec_candidate(
        title="Calculate and organise", problem="calculate", buyer_outcome="organise"
    )
    # A keyword carrying no job token leaves the tie intact.
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "freelance uk")])
    assert spec.job_classification.job == JobToBeDone.UNKNOWN
    assert any(c.topic == "job_signals_inconclusive" for c in spec.conflicts)


# ------------------------------------------------- forbidden market claims


# Affirmative market assertions. Disclaimers that NEGATE these words are
# required elsewhere, so the patterns match only the asserting form.
FORBIDDEN_CLAIM_PATTERNS = (
    r"\b(is|are|was|were) proven\b",
    r"\bproven to\b",
    r"\bguaranteed\b",
    r"\bmarket share\b",
    r"\bcommercially validated\b",
    r"\bconversion rate\b",
    r"\bexpected revenue\b",
    r"\bprofit\b",
    r"\bunits sold (?!and revenue remain)",
    r"\bwillingness to pay of\b",
    r"\bbest[- ]sell(er|ing)\b",
    r"\bhigh demand\b",
    r"\bdemand is (strong|high|proven)\b",
)


@pytest.mark.parametrize("pattern", FORBIDDEN_CLAIM_PATTERNS)
def test_specification_never_asserts_a_market_finding(pattern):
    candidate = make_spec_candidate()
    evidence = (
        [keyword_evidence(candidate.id, "quarterly tax calculator")]
        + listing_evidence(
            candidate.id,
            [comparable(f"L{i}", price=10.0 + i, seller_id=f"shop-{i}") for i in range(6)],
        )
        + [video_evidence(candidate.id, "Calculate freelance tax", ["calculator"])]
    )
    rendered = spec_text(build_spec(candidate, evidence))
    assert not re.search(pattern, rendered, re.IGNORECASE), pattern


@pytest.mark.parametrize("phrase", ["will sell", "proven", "revenue", "units sold"])
def test_loaded_words_appear_only_inside_a_disclaimer(phrase):
    """Adversarial: every occurrence must be a denial, never an assertion."""
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "quarterly tax calculator")] + (
        listing_evidence(
            candidate.id,
            [comparable(f"L{i}", price=10.0 + i, seller_id=f"shop-{i}") for i in range(5)],
        )
    )
    rendered = spec_text(build_spec(candidate, evidence))
    negations = ("not", "no ", "never", "unknown", "cannot", "is not")
    for match in re.finditer(re.escape(phrase), rendered, re.IGNORECASE):
        window = rendered[max(0, match.start() - 90) : match.end() + 90].lower()
        assert any(word in window for word in negations), window


def test_core_promise_is_an_intention_never_a_guarantee():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert spec.core_promise.claim_class == SpecClaimClass.ASSUMED
    assert "not a guarantee" in spec.core_promise.value


def test_specification_carries_no_score_field():
    """4C produces no number of any kind: no POS, no ECS, no rating."""
    spec = build_spec(make_spec_candidate(), [])
    rendered = spec_text(spec)
    for banned in (
        r"opportunity_score",
        r"\bECS\b",
        r"\bPOS\b",
        r"\bRED\b",
        r"\bYELLOW\b",
        r"\bGREEN\b",
    ):
        assert not re.search(banned, rendered), banned
    assert not any(name.endswith("_score") for name in asdict(spec))


# ------------------------------------------------------- prose boundary


def test_default_provider_leaves_the_specification_untouched():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    proposed = TemplateProseProvider().propose_prose(spec)
    assert proposed == {}
    assert apply_prose(spec, proposed) == spec


def test_prose_may_only_touch_narrative_fields():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    with pytest.raises(ProseGenerationError):
        apply_prose(spec, {"target_buyer": "Everyone"})
    with pytest.raises(ProseGenerationError):
        apply_prose(spec, {"format_recommendation": "anything"})
    assert set(REWRITABLE_PROSE_FIELDS) == {"product_name", "core_promise"}


@pytest.mark.parametrize(
    "text",
    [
        "A proven bestseller for freelancers",
        "This will sell to every freelancer",
        "Guaranteed to increase revenue",
        "Buyers show a willingness to pay of $40",
        "Captures 20% market share",
        "Delivers a 30% conversion for sellers",
        "Validated by the market",
    ],
)
def test_prose_asserting_a_market_finding_is_rejected(text):
    with pytest.raises(ProseGenerationError):
        validate_prose(text)


def test_accepted_prose_keeps_its_claim_class_and_provenance():
    """Rewording a sentence cannot change how well evidence supports it."""
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    rewritten = apply_prose(spec, {"product_name": "The Quarterly Set-Aside Sheet"})
    assert rewritten.product_name.value == "The Quarterly Set-Aside Sheet"
    assert rewritten.product_name.claim_class == spec.product_name.claim_class
    assert rewritten.product_name.basis == spec.product_name.basis
    assert rewritten.product_name.evidence_ids == spec.product_name.evidence_ids


def test_prose_cannot_change_any_decision():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    rewritten = apply_prose(spec, {"product_name": "A Tidy Little Sheet"})
    assert rewritten.format_recommendation == spec.format_recommendation
    assert rewritten.job_classification == spec.job_classification
    assert rewritten.structure == spec.structure
    assert rewritten.supporting_evidence_ids == spec.supporting_evidence_ids
    assert rewritten.price_evidence_reference == spec.price_evidence_reference
    assert rewritten.conflicts == spec.conflicts


def test_provider_base_class_generates_nothing_by_default():
    from app.services.product_specification import ProductSpecificationProvider

    with pytest.raises(NotImplementedError):
        ProductSpecificationProvider().propose_prose(build_spec(evidence=[]))


# ------------------------------------------------------------------- API


@pytest.fixture
def client():
    return TestClient(app)


def test_endpoint_generates_for_an_inline_candidate(client):
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "quarterly tax calculator")]
    response = client.post(
        "/product/specification",
        json={
            "candidate": candidate.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        },
    )
    assert response.status_code == 200
    spec = response.json()["specification"]
    assert spec["state"] == "GENERATED"
    assert spec["job_classification"]["job"] == "CALCULATE"


def test_endpoint_returns_both_format_answers_separately(client):
    candidate = make_spec_candidate()
    evidence = [keyword_evidence(candidate.id, "quarterly tax calculator")]
    response = client.post(
        "/product/specification",
        json={
            "candidate": candidate.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        },
    )
    rec = response.json()["specification"]["format_recommendation"]
    assert rec["ideal_format"] == "CALCULATOR"
    assert rec["outside_v1_build_capability"] is True
    assert rec["buildable_v1_format"] == "SPREADSHEET_TOOL"
    assert rec["ideal_claim_class"] == "ASSUMED"


def test_endpoint_reads_stored_evidence_for_a_registered_run(client):
    from app.api.routes import get_research_store

    store: ResearchStore = get_research_store()
    candidate = make_spec_candidate()
    run_id = uuid4()
    store.register_run(run_id, [candidate])
    for item in listing_evidence(candidate.id, [comparable("L1"), comparable("L2")]):
        store.add_evidence(item)

    response = client.post(
        "/product/specification",
        json={"research_run_id": str(run_id), "candidate_id": str(candidate.id)},
    )
    assert response.status_code == 200
    spec = response.json()["specification"]
    assert spec["state"] == "GENERATED"
    assert spec["research_run_id"] == str(run_id)
    assert spec["observed_competitor_patterns"]


def test_endpoint_rejects_an_unknown_run(client):
    response = client.post(
        "/product/specification",
        json={"research_run_id": str(uuid4()), "candidate_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_endpoint_rejects_a_candidate_outside_the_run(client):
    from app.api.routes import get_research_store

    store: ResearchStore = get_research_store()
    run_id = uuid4()
    store.register_run(run_id, [make_spec_candidate()])
    response = client.post(
        "/product/specification",
        json={"research_run_id": str(run_id), "candidate_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert "is not part of research run" in response.json()["detail"]


def test_endpoint_requires_a_candidate_selection(client):
    response = client.post("/product/specification", json={})
    assert response.status_code == 422


def test_preliminary_response_never_auto_generates_specifications():
    """Decision C: on demand only, never for all five candidates."""
    from app.api.routes import PreliminaryResearchResponse

    assert "specification" not in PreliminaryResearchResponse.model_fields
    assert "product_specification" not in PreliminaryResearchResponse.model_fields


# --------------------------------------------------------- scope guards


def test_4c_never_imports_legacy_scoring():
    reachable = _transitive_app_imports(SPEC_MODULE)
    assert LEGACY_SCORING_MODULE not in reachable


def test_4c_does_not_modify_milestone_1_validation():
    """Decision A: the Milestone 1 validator is not relaxed or reversed."""
    from app.services.candidate_validation import _UNSUPPORTED_TERMS

    for term in ("saas", "app", "course", "coaching", "community", "subscription"):
        assert term in _UNSUPPORTED_TERMS
    reachable = _transitive_app_imports(SPEC_MODULE)
    assert "app.services.candidate_validation" not in reachable


def test_4c_introduces_no_pos_ecs_or_traffic_light_vocabulary():
    import pathlib

    source = pathlib.Path("app/services/product_specification.py").read_text()
    for banned in (
        r"product_opportunity_score",
        r"evidence_confidence_score",
        r"\bRED\b",
        r"\bYELLOW\b",
        r"\bGREEN\b",
        r"\bPOS\b",
        r"\bECS\b",
    ):
        assert not re.search(banned, source), banned


def test_spec_claim_class_is_local_and_truth_class_is_untouched():
    """Decision B: the shared truth model is not modified."""
    assert [t.value for t in TruthClass] == [
        "OBSERVED",
        "ESTIMATED",
        "INFERRED",
        "UNKNOWN",
    ]
    assert [c.value for c in SpecClaimClass] == [
        "OBSERVED",
        "INFERRED",
        "ASSUMED",
        "UNKNOWN",
    ]
    assert SpecClaimClass is not TruthClass


# ==========================================================================
# Adversarial review regressions (review of 8705da1)
#
# Each test below reproduces a defect the review found by probing, not by
# reading. They are grouped so a future change that reintroduces one fails
# with the reason attached.
# ==========================================================================


def test_evidence_for_another_job_never_raises_this_job_class():
    """A job with zero observed matches rests on the hypothesis alone.

    Regression: `observed_support` was a single global flag, so evidence that
    matched ANY job promoted the winning job to INFERRED and cited that
    unrelated record as its provenance.
    """
    candidate = make_spec_candidate(
        title="Freelance tax calculator estimator",
        problem="Freelancers cannot calculate the cost or estimate the formula",
        buyer_outcome="Calculate the percentage to set aside",
    )
    # One observed record, matching LEARN ("guide") and nothing in CALCULATE.
    evidence = [keyword_evidence(candidate.id, "freelance tax guide")]
    job = classify_job(collect_observed_text(evidence), candidate)

    assert job.job == JobToBeDone.CALCULATE
    assert job.claim_class == SpecClaimClass.ASSUMED
    assert job.claim_class != SpecClaimClass.INFERRED
    assert job.evidence_ids == ()
    assert "no observed evidence text matched this job" in job.basis


def test_the_class_downgrade_propagates_to_dependent_fields():
    candidate = make_spec_candidate(
        title="Freelance tax calculator estimator",
        problem="Freelancers cannot calculate the cost or estimate the formula",
        buyer_outcome="Calculate the percentage to set aside",
    )
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "freelance tax guide")])
    assert spec.buyer_job.claim_class == SpecClaimClass.ASSUMED
    assert spec.buyer_job.evidence_ids == ()
    # A format derived from an ASSUMED job cannot be INFERRED.
    assert spec.format_recommendation.buildable_claim_class == SpecClaimClass.ASSUMED


def test_cited_evidence_actually_matched_the_winning_job():
    """Provenance must support the claim it is attached to."""
    candidate = make_spec_candidate()
    matching = keyword_evidence(candidate.id, "quarterly tax calculator")
    unrelated = keyword_evidence(candidate.id, "freelance tax guide")
    job = classify_job(collect_observed_text([matching, unrelated]), candidate)
    assert job.job == JobToBeDone.CALCULATE
    assert job.claim_class == SpecClaimClass.INFERRED
    assert job.evidence_ids == (matching.id,)
    assert unrelated.id not in job.evidence_ids


def test_no_single_word_job_token_is_a_stopword():
    """Regression: "or", "which" and "best" classified on grammar alone.

    Multi-word tokens are exempt: "how much" and "should i" carry intent
    that neither of their words carries alone. A ONE-word token has nothing
    else to lean on, so it must not be a stopword.
    """
    for job, tokens in JOB_TOKENS.items():
        for token in tokens:
            if " " in token:
                continue
            assert token not in JOB_TOKEN_STOPWORDS, (job.value, token)


def test_an_ordinary_title_is_not_classified_by_a_conjunction():
    """Regression: 'meal chart for mums or dads' was classified DECIDE."""
    candidate = make_spec_candidate(title="Zzz", problem="Zzz", buyer_outcome="Zzz")
    job = classify_job(
        [ObservedText("Printable meal chart for mums or dads", uuid4(), "x")], candidate
    )
    assert job.job == JobToBeDone.UNKNOWN
    assert job.scores == ()


def test_a_superlative_does_not_outrank_the_meaningful_token():
    """Regression: 'best' + 'which' beat 'planner' in a planning title."""
    candidate = make_spec_candidate(title="Zzz", problem="Zzz", buyer_outcome="Zzz")
    job = classify_job(
        [ObservedText("Wedding planner: which colours, best day", uuid4(), "x")],
        candidate,
    )
    assert job.job == JobToBeDone.PLAN


@pytest.mark.parametrize(
    "title",
    [
        "Proven Best-Selling Freelance Tax Calculator",
        "The Guaranteed Budget Planner",
        "Revenue Booster Spreadsheet",
    ],
)
def test_a_market_claim_in_the_candidate_title_is_not_restated_as_a_name(title):
    """Regression: candidate text bypassed the forbidden-claim filter.

    The filter guarded a future LLM's prose but not the deterministic
    generator, so Milestone 1 marketing copy became the product name.
    """
    candidate = make_spec_candidate(title=title)
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert spec.product_name.value is None
    assert spec.product_name.claim_class == SpecClaimClass.UNKNOWN
    assert "unsupported market claim" in spec.product_name.basis


def test_a_market_claim_in_the_buyer_outcome_is_not_restated_as_a_promise():
    candidate = make_spec_candidate(
        buyer_outcome="Earn guaranteed revenue of $5,000 per month, validated by the market"
    )
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    assert spec.core_promise.value is None
    assert spec.core_promise.claim_class == SpecClaimClass.UNKNOWN
    assert "unsupported market claim" in spec.core_promise.basis


def test_candidate_claim_gate_uses_the_same_vocabulary_as_the_prose_gate():
    """One rule, applied to the generator and to any future provider."""
    for phrase in ("proven", "guaranteed", "revenue", "best-selling", "will sell"):
        assert unsupported_claim_in(f"A {phrase} product") is not None
    assert unsupported_claim_in("A quarterly set-aside sheet") is None


@pytest.mark.parametrize(
    "text",
    [
        "A provеn bestseller",          # Cyrillic homoglyph
        "A pro​ven winner",            # zero-width space
        "Boosts ｒｅｖｅｎｕｅ",  # fullwidth
        "This will sell out",          # non-breaking space
        "A pro­ven method",            # soft hyphen
    ],
)
def test_unicode_tricks_cannot_smuggle_a_market_claim_past_the_filter(text):
    """Regression: every one of these was accepted before normalization."""
    with pytest.raises(ProseGenerationError):
        validate_prose(text)


def test_clean_prose_survives_normalization():
    validate_prose("A tidy quarterly set-aside sheet")


@pytest.mark.parametrize(
    "proposed",
    [
        {"product_name": 42},
        {"product_name": {"nested": "value"}},
        {"product_name": ["a", "b"]},
        {"product_name": None},
    ],
)
def test_malformed_provider_values_raise_a_controlled_error(proposed):
    """Regression: these raised AttributeError, not ProseGenerationError."""
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    with pytest.raises(ProseGenerationError):
        apply_prose(spec, proposed)


def test_provider_output_that_is_not_a_mapping_is_rejected():
    candidate = make_spec_candidate()
    spec = build_spec(candidate, [keyword_evidence(candidate.id, "tax calculator")])
    with pytest.raises(ProseGenerationError):
        apply_prose(spec, [("product_name", "x")])


def test_re_observing_one_listing_does_not_double_weight_it():
    """Regression: a duplicate of ONE listing flipped TRACK to UNKNOWN.

    The store is append-only across runs, so the same listing can be stored
    more than once. It is one listing, and must count once.
    """
    candidate = make_spec_candidate(title="Zzz", problem="Zzz", buyer_outcome="Zzz")

    def titled(listing_id: str, title: str):
        evidence = listing_evidence(candidate.id, [comparable(listing_id)])
        for item in evidence:
            object.__setattr__(item, "raw_payload", {**item.raw_payload, "title": title})
        return evidence

    calc, track = titled("A", "tax calculator"), titled("B", "habit tracker")
    balanced = classify_job(collect_observed_text(calc + track), candidate)
    duplicated = classify_job(
        collect_observed_text(calc + titled("A", "tax calculator") + track), candidate
    )
    assert balanced.job == JobToBeDone.TRACK
    assert duplicated.job == balanced.job
    assert duplicated.scores == balanced.scores


def test_specification_is_invariant_under_evidence_order():
    """Equivalent evidence must yield an identical document, not a shuffled one."""
    candidate = make_spec_candidate()
    evidence = (
        [keyword_evidence(candidate.id, "quarterly tax calculator")]
        + listing_evidence(
            candidate.id,
            [comparable(f"L{i}", price=10.0 + i, seller_id=f"s{i}") for i in range(4)],
        )
        + [video_evidence(candidate.id, "Calculate freelance tax", ["calculator"])]
    )
    base = build_spec(candidate, list(evidence))
    rng = random.Random(20260910)
    for _ in range(50):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert build_spec(candidate, shuffled) == base


def test_stored_evidence_reads_are_scoped_to_one_research_run():
    """Regression: the accessor merged every run a candidate appeared in."""
    store = ResearchStore()
    candidate = make_spec_candidate()
    run_a, run_b = uuid4(), uuid4()
    for run_id in (run_a, run_b):
        for item in listing_evidence(candidate.id, [comparable("L1")]):
            store.add_evidence(item.model_copy(update={"research_run_id": run_id}))

    unscoped = store.evidence_for_candidate(candidate.id)
    scoped = store.evidence_for_candidate(candidate.id, run_a)
    assert len(unscoped) == 6
    assert len(scoped) == 3
    assert {item.research_run_id for item in scoped} == {run_a}


def test_unstamped_evidence_is_never_dropped_by_a_scoped_read():
    """A record with no run id cannot belong to a different run."""
    store = ResearchStore()
    candidate = make_spec_candidate()
    for item in listing_evidence(candidate.id, [comparable("L1")]):
        store.add_evidence(item)
    assert len(store.evidence_for_candidate(candidate.id, uuid4())) == 3


def test_inline_mode_does_not_trust_a_caller_supplied_run_id():
    """Regression: a caller could stamp any run id onto inline evidence."""
    client = TestClient(app)
    candidate = make_spec_candidate()
    evidence = keyword_evidence(candidate.id, "quarterly tax calculator")
    response = client.post(
        "/product/specification",
        json={
            "candidate": candidate.model_dump(mode="json"),
            "research_run_id": str(uuid4()),  # unrelated to the evidence
            "evidence": [evidence.model_dump(mode="json")],
        },
    )
    assert response.status_code == 200
    # The evidence carries no run, so the specification claims none.
    assert response.json()["specification"]["research_run_id"] is None


def test_inline_mode_takes_the_run_stamp_from_the_evidence():
    client = TestClient(app)
    candidate = make_spec_candidate()
    run_id = uuid4()
    evidence = keyword_evidence(candidate.id, "quarterly tax calculator").model_copy(
        update={"research_run_id": run_id}
    )
    response = client.post(
        "/product/specification",
        json={
            "candidate": candidate.model_dump(mode="json"),
            "evidence": [evidence.model_dump(mode="json")],
        },
    )
    assert response.json()["specification"]["research_run_id"] == str(run_id)
