"""Milestone 4G tests: Problem/Product Fit Evidence.

No network, no live API calls, no new provider.

Four rules carry the milestone.

1. Structural fit is not demand. A product can suit a job perfectly and sell
   nothing. Eight permanent UNKNOWN markers say so, and no commercial-success
   vocabulary may appear in any emitted string.

2. Consuming Milestone 4C never launders generated text into evidence. 4G
   reads claim classes, scores and enums — never a generated string. A test
   plants a distinctive invented phrase in a specification and asserts it
   appears nowhere in the serialized assessment.

3. No claim is stronger than its weakest link. Every fit claim is capped
   through 4C's own `cap_claim_class` against the inputs it rests on, and the
   assessment itself is always INFERRED.

4. Magnitudes cannot make a product fit better. Search volume, views, review
   counts, listing counts and prices are unreadable from this module, proven
   by an AST guard.
"""

import ast
import random
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, ProductFormat, TruthClass
from app.domain.models import Candidate, EvidenceItem
from app.services.product_job_fit import (
    AMBIGUITY_MAX_LEAD,
    BUILDABLE_FORMAT_MODE,
    DIM_PRODUCT_JOB_FIT,
    FIT_PATTERN_VERSION,
    IDEAL_FORMAT_MODE,
    PATTERN_BOUNDARIES,
    PRODUCT_JOB_FIT_VERSION,
    UNDELIVERABLE_BY_ARTEFACT,
    FitPattern,
    InteractionMode,
    SubstitutionFidelity,
    extract_product_job_fit,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.price_evidence import extract_price_evidence
from app.services.product_specification import (
    IdealFormat,
    JobToBeDone,
    SpecClaimClass,
    SpecificationState,
    generate_product_specification,
)
from app.services.purchase_evidence import extract_purchase_evidence

from tests.route_surface import assert_openapi_surface_unchanged, assert_route_surface_unchanged

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MODULE_PATH = Path("app/services/product_job_fit.py")


# ----------------------------------------------------------------- fixtures


def make_candidate(
    title: str = "Printable weekly meal planner",
    problem: str = "Parents struggle to plan meals each week.",
    buyer_outcome: str = "A planned week of meals.",
    proposed_format: ProductFormat = ProductFormat.PRINTABLE_BUNDLE,
    candidate_id: UUID | None = None,
) -> Candidate:
    return Candidate(
        id=candidate_id or uuid4(),
        seed_keyword="meal planning",
        title=title,
        problem=problem,
        target_buyer="Busy parents",
        proposed_format=proposed_format,
        buyer_outcome=buyer_outcome,
        search_queries=["meal planner"],
        marketplace_queries=["meal planner printable"],
        content_queries=["how to meal plan"],
        generation_reason="seeded",
    )


def content_evidence(
    title: str,
    candidate_id: UUID,
    *,
    video_id: str = "V1",
    views: int | None = 1000,
) -> list[EvidenceItem]:
    """3B-shaped evidence whose TITLE carries the job tokens 4C reads."""
    payload: dict[str, Any] = {
        "video_id": video_id,
        "channel_id": "UC_A",
        "title": title,
        "published_at": (NOW - timedelta(days=30)).isoformat(),
        "view_count": views,
    }
    common = dict(
        candidate_id=candidate_id,
        provider="youtube",
        collection_method="official_api",
        platform="youtube",
        raw_payload=payload,
        raw_payload_hash=f"hash-{video_id}",
    )
    return [
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=TruthClass.OBSERVED if views is not None else TruthClass.UNKNOWN,
            raw_value=views,
            **common,
        ),
    ]


def listing_evidence(
    title: str,
    candidate_id: UUID,
    *,
    listing_id: str = "L1",
    seller_id: str = "shopA",
    price: float | None = 9.0,
) -> list[EvidenceItem]:
    payload: dict[str, Any] = {
        "listing_id": listing_id,
        "seller_id": seller_id,
        "title": title,
        "price": price,
        "currency": "USD",
        "is_digital": True,
        "review_count": 5,
        "created_at": (NOW - timedelta(days=400)).isoformat(),
    }
    common = dict(
        candidate_id=candidate_id,
        provider="etsy",
        collection_method="official_api",
        marketplace="etsy",
        raw_payload=payload,
        raw_payload_hash=f"hash-{listing_id}",
    )
    return [
        EvidenceItem(
            signal_type="marketplace_competing_listing",
            purpose=EvidencePurpose.COMPETITION,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_listing_price",
            purpose=EvidencePurpose.PRICE,
            truth_class=TruthClass.OBSERVED if price is not None else TruthClass.UNKNOWN,
            raw_value=price,
            **common,
        ),
    ]


def spec_for(candidate: Candidate, evidence: list[EvidenceItem]):
    """A real 4C specification, built the way the endpoint builds one.

    4A and 4B are re-derived from the same evidence rather than passed as
    None, because 4C flags a specification insufficient when no price
    reference is available — so a None-price fixture would make every
    specification insufficient and hide every pattern past the first.
    """
    return generate_product_specification(
        candidate=candidate,
        evidence=evidence,
        price_evidence=extract_price_evidence(candidate.id, evidence),
        purchase_evidence=extract_purchase_evidence(candidate.id, evidence),
        research_run_id=None,
    )


def fit_for(candidate: Candidate, evidence: list[EvidenceItem]):
    return extract_product_job_fit(
        candidate_id=candidate.id, specification=spec_for(candidate, evidence)
    )


def spec_with_job(
    job: JobToBeDone,
    # INFERRED is 4C's CEILING for a job classification: reading a job out of
    # provider text is a derivation, never an observation. A fixture that
    # forced OBSERVED would test a specification 4C cannot produce.
    claim_class: SpecClaimClass = SpecClaimClass.INFERRED,
    *,
    ideal: IdealFormat | None = None,
    buildable: ProductFormat | None = None,
    scores: tuple[tuple[str, int], ...] | None = None,
    evidence_ids: tuple[UUID, ...] = (UUID(int=1),),
    candidate_id: UUID | None = None,
    insufficient: bool = False,
):
    """A real 4C specification with its job/format substituted.

    Built by generating a genuine specification and replacing only the
    structured fields under test, so every other field stays exactly as 4C
    produces it rather than being invented by the test.
    """
    candidate = make_candidate(candidate_id=candidate_id)
    evidence = content_evidence("how to plan meals", candidate.id)
    for index in range(3):
        evidence.extend(
            listing_evidence(
                "printable meal planner",
                candidate.id,
                listing_id=f"B{index}",
                seller_id=f"base{index}",
            )
        )
    base = spec_for(candidate, evidence)
    classification = replace(
        base.job_classification,
        job=job,
        claim_class=claim_class,
        evidence_ids=evidence_ids,
        scores=scores if scores is not None else base.job_classification.scores,
    )
    recommendation = base.format_recommendation
    if recommendation is not None and (ideal is not None or buildable is not None):
        recommendation = replace(
            recommendation,
            ideal_format=ideal if ideal is not None else recommendation.ideal_format,
            buildable_v1_format=(
                buildable
                if buildable is not None
                else recommendation.buildable_v1_format
            ),
            outside_v1_build_capability=(
                ideal not in {
                    IdealFormat.PDF_GUIDE, IdealFormat.WORKBOOK,
                    IdealFormat.CHECKLIST, IdealFormat.TEMPLATE_PACK,
                    IdealFormat.SPREADSHEET_TOOL, IdealFormat.DATA_TEMPLATE,
                    IdealFormat.PRINTABLE_BUNDLE,
                }
                if ideal is not None
                else recommendation.outside_v1_build_capability
            ),
        )
    return replace(
        base,
        job_classification=classification,
        format_recommendation=recommendation,
        insufficient_evidence=insufficient,
    )


def run(specification, candidate_id: UUID | None = None):
    return extract_product_job_fit(
        candidate_id=candidate_id or uuid4(), specification=specification
    )


# ------------------------------------------------ the taxonomy is complete


def test_every_ideal_format_has_an_interaction_mode():
    for fmt in IdealFormat:
        assert fmt in IDEAL_FORMAT_MODE, fmt


def test_every_buildable_format_has_an_interaction_mode():
    for fmt in ProductFormat:
        assert fmt in BUILDABLE_FORMAT_MODE, fmt


def test_the_two_mode_tables_agree_where_they_name_the_same_format():
    """A format cannot be used one way as an ideal and another as a build."""
    for ideal, mode in IDEAL_FORMAT_MODE.items():
        matching = [f for f in ProductFormat if f.value == ideal.value]
        for buildable in matching:
            assert BUILDABLE_FORMAT_MODE[buildable] is mode, ideal


def test_every_pattern_states_what_it_does_not_establish():
    for pattern in FitPattern:
        assert pattern in PATTERN_BOUNDARIES, pattern
        assert PATTERN_BOUNDARIES[pattern].strip()


def test_no_buildable_format_provides_an_ongoing_mode():
    """The undeliverable set must actually be undeliverable in V1."""
    for mode in UNDELIVERABLE_BY_ARTEFACT:
        assert mode not in set(BUILDABLE_FORMAT_MODE.values()), mode


# -------------------------------------------------- the required scenarios


def test_strong_alignment_when_the_job_is_observed_and_the_format_is_direct():
    specification = spec_with_job(
        JobToBeDone.LEARN,
        SpecClaimClass.INFERRED,
        ideal=IdealFormat.PDF_GUIDE,
        buildable=ProductFormat.PDF_GUIDE,
        scores=(("LEARN", 5), ("PLAN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.ALIGNED_WITH_OBSERVED_JOB
    assert result.state is DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert result.features.substitution_fidelity is SubstitutionFidelity.DIRECT
    assert result.features.job_supported_by_observed_evidence is True
    # Even the strongest pattern disclaims commercial success.
    assert "NOT product-market fit" in result.pattern_boundary
    assert "sell" in result.pattern_boundary


def test_obvious_mismatch_an_ongoing_need_answered_by_a_static_artefact():
    """The user's 'ongoing recurring need' case, structurally detected."""
    specification = spec_with_job(
        JobToBeDone.LEARN,
        SpecClaimClass.INFERRED,
        ideal=IdealFormat.COMMUNITY,
        buildable=ProductFormat.PDF_GUIDE,
        scores=(("LEARN", 5), ("PLAN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.STRUCTURAL_NEED_UNMET
    assert result.features.substitution_fidelity is SubstitutionFidelity.MODE_UNMET
    assert result.features.ideal_interaction_mode == "ONGOING_INTERACTION"
    assert result.features.buildable_interaction_mode == "STATIC_REFERENCE"
    topics = {o.topic for o in result.observations}
    assert "structural_substitution" in topics


def test_repeated_calculation_survives_substitution_into_a_spreadsheet():
    """The user's 'repeated task/calculation' case: mode is preserved."""
    specification = spec_with_job(
        JobToBeDone.CALCULATE,
        SpecClaimClass.INFERRED,
        ideal=IdealFormat.CALCULATOR,
        buildable=ProductFormat.SPREADSHEET_TOOL,
        scores=(("CALCULATE", 4), ("LEARN", 0)),
    )
    result = run(specification)
    assert result.features.substitution_fidelity is SubstitutionFidelity.MODE_PRESERVED
    assert result.features.ideal_interaction_mode == "REPEATED_COMPUTATION"
    assert result.features.buildable_interaction_mode == "REPEATED_COMPUTATION"
    assert result.pattern is FitPattern.ALIGNED_WITH_OBSERVED_JOB


def test_time_sequenced_delivery_collapsed_into_a_workbook_is_unmet():
    """A five-day challenge is a schedule; a workbook is not."""
    specification = spec_with_job(
        JobToBeDone.EXECUTE,
        SpecClaimClass.INFERRED,
        ideal=IdealFormat.FIVE_DAY_CHALLENGE,
        buildable=ProductFormat.WORKBOOK,
        scores=(("EXECUTE", 4), ("LEARN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.STRUCTURAL_NEED_UNMET
    assert result.features.ideal_interaction_mode == "TIME_SEQUENCED_DELIVERY"


def test_mode_change_is_reported_without_being_called_worse():
    specification = spec_with_job(
        JobToBeDone.PLAN,
        SpecClaimClass.INFERRED,
        ideal=IdealFormat.TEMPLATE_PACK,
        buildable=ProductFormat.PDF_GUIDE,
        scores=(("PLAN", 4), ("LEARN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.SUBSTITUTED_WITH_MODE_CHANGE
    assert result.features.substitution_fidelity is SubstitutionFidelity.MODE_CHANGED
    observation = next(
        o for o in result.observations if o.topic == "structural_substitution"
    )
    assert "worse" in observation.does_not_establish


def test_ambiguous_job_is_surfaced_not_hidden():
    """4C classifies on a lead of one; 4G reports that the choice was close."""
    specification = spec_with_job(
        JobToBeDone.PLAN,
        SpecClaimClass.INFERRED,
        scores=(("PLAN", 3), ("TRACK", 3 - AMBIGUITY_MAX_LEAD), ("LEARN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.JOB_AMBIGUOUS
    assert result.features.job_is_ambiguous is True
    assert result.features.job_score_lead == AMBIGUITY_MAX_LEAD
    assert "TRACK" in result.features.competing_jobs


def test_a_clear_win_is_not_reported_as_ambiguous():
    specification = spec_with_job(
        JobToBeDone.PLAN,
        SpecClaimClass.INFERRED,
        scores=(("PLAN", 9), ("TRACK", 1)),
    )
    result = run(specification)
    assert result.features.job_is_ambiguous is False
    assert result.features.competing_jobs == ()
    assert result.pattern is not FitPattern.JOB_AMBIGUOUS


def test_multiple_plausible_formats_are_all_named():
    """Three jobs within the margin means three possible formats."""
    specification = spec_with_job(
        JobToBeDone.PLAN,
        SpecClaimClass.INFERRED,
        scores=(("PLAN", 4), ("TRACK", 4), ("ORGANISE", 3), ("LEARN", 0)),
    )
    result = run(specification)
    assert result.pattern is FitPattern.JOB_AMBIGUOUS
    assert set(result.features.competing_jobs) == {"TRACK", "ORGANISE"}


def test_unknown_job_makes_fit_unassessable_not_unfit():
    specification = spec_with_job(
        JobToBeDone.UNKNOWN, SpecClaimClass.UNKNOWN, evidence_ids=()
    )
    result = run(specification)
    assert result.pattern is FitPattern.JOB_UNKNOWN
    assert result.state is DimensionState.UNKNOWN
    assert result.missing_reason == "job_not_classified_from_evidence"
    assert result.features.job is None
    # Absent analysis, never a finding of misfit.
    assert "never a finding that no job exists" in result.pattern_boundary


def test_insufficient_evidence_is_unassessable_not_unfit():
    specification = spec_with_job(
        JobToBeDone.LEARN, SpecClaimClass.INFERRED, insufficient=True
    )
    result = run(specification)
    assert result.pattern is FitPattern.SPECIFICATION_INSUFFICIENT
    assert result.state is DimensionState.UNKNOWN
    assert result.missing_reason == "specification_evidence_insufficient"
    assert "not a finding of misfit" in result.pattern_boundary


def test_a_specification_unsupported_by_evidence_reports_the_gap():
    """The job classified from the candidate's own words, nothing observed.

    This is the 'generated specification unsupported by underlying evidence'
    case: 4C will still produce a document, and 4G must say the fit assessed
    is fit to a hypothesis.
    """
    candidate = make_candidate(title="Weekly meal planner checklist for parents")
    # Enough evidence that 4C does not flag insufficiency, but none of it
    # mentions anything the job classifier recognises: the job can only come
    # from the candidate's own title.
    evidence: list[EvidenceItem] = []
    for index in range(4):
        evidence.extend(
            content_evidence(
                "unrelated topic entirely", candidate.id, video_id=f"V{index}"
            )
        )
    for index in range(4):
        evidence.extend(
            listing_evidence(
                "unrelated topic entirely",
                candidate.id,
                listing_id=f"L{index}",
                seller_id=f"shop{index}",
            )
        )
    specification = spec_for(candidate, evidence)
    assert specification.insufficient_evidence is False
    result = extract_product_job_fit(
        candidate_id=candidate.id, specification=specification
    )
    assert result.features.job_supported_by_observed_evidence is False
    assert result.pattern in (
        FitPattern.JOB_ASSUMED_NOT_OBSERVED,
        FitPattern.JOB_UNKNOWN,
    )
    if result.pattern is FitPattern.JOB_ASSUMED_NOT_OBSERVED:
        assert "candidate's own wording" in result.pattern_boundary


def test_missing_specification_is_unassessable():
    candidate_id = uuid4()
    result = extract_product_job_fit(candidate_id=candidate_id, specification=None)
    assert result.pattern is FitPattern.NO_SPECIFICATION
    assert result.state is DimensionState.MISSING
    assert result.missing_reason == "no_product_specification_available"
    assert result.features is None
    assert result.fit_claim_class is SpecClaimClass.UNKNOWN
    assert "not evidence that a product would or would not suit" in (
        result.pattern_boundary
    )


def test_a_specification_in_the_missing_state_is_unassessable():
    specification = replace(
        spec_with_job(JobToBeDone.LEARN),
        state=SpecificationState.MISSING,
        missing_reason="no_candidate_evidence",
    )
    result = run(specification)
    assert result.pattern is FitPattern.NO_SPECIFICATION
    assert result.missing_reason == "no_candidate_evidence"


def test_contradictory_evidence_is_surfaced_as_an_observation():
    """4C records conflicts; 4G must expose them rather than average them."""
    candidate = make_candidate(
        title="Meal planning guide", proposed_format=ProductFormat.PDF_GUIDE
    )
    evidence = content_evidence("how to plan meals step by step", candidate.id)
    evidence.extend(
        listing_evidence("meal planning spreadsheet tracker", candidate.id)
    )
    specification = spec_for(candidate, evidence)
    result = extract_product_job_fit(
        candidate_id=candidate.id, specification=specification
    )
    assert result.features.conflict_count == len(specification.conflicts)
    if specification.conflicts:
        topics = {o.topic for o in result.observations}
        assert "evidence_conflicts" in topics


def test_unknown_specification_fields_are_reported_by_name_only():
    candidate = make_candidate(
        title="Guaranteed best-selling proven meal planner",
        buyer_outcome="guaranteed revenue of $5,000 per month",
    )
    evidence = content_evidence("how to plan meals", candidate.id)
    result = fit_for(candidate, evidence)
    # 4C's forbidden-claim filter rejects that text, leaving fields UNKNOWN.
    assert result.features.unknown_specification_fields
    assert result.features.known_specification_field_count < 4
    observation = next(
        o for o in result.observations if o.topic == "specification_completeness"
    )
    # Only field NAMES appear, never the rejected prose.
    assert "guaranteed" not in observation.finding.lower()
    assert "proven" not in observation.finding.lower()


# ------------------------------------- generated prose never becomes evidence


def test_generated_prose_appears_nowhere_in_the_assessment():
    """The integrity guarantee, tested behaviourally rather than by reading.

    A distinctive invented phrase is planted in every generated field the
    candidate can reach, and the entire serialized 4G result is searched for
    it — in both the field-known and the field-UNKNOWN case, because those
    take different code paths and only the second reaches the
    unknown-field reporting.
    """
    from app.api.routes import ProductJobFitOut

    marker = "zyxwvutsrq"
    cases = [
        # Fields KNOWN: the marker reaches product_name and core_promise.
        make_candidate(
            title=f"{marker} meal planner",
            buyer_outcome=f"a {marker} week of meals",
            problem=f"parents cannot {marker} their meals",
        ),
        # Fields UNKNOWN: 4C's forbidden-claim filter rejects the text, so
        # the fields are reported as unknown BY NAME and the rejected prose
        # must not travel with the name.
        make_candidate(
            title=f"guaranteed proven {marker} planner",
            buyer_outcome=f"guaranteed revenue from {marker}",
        ),
    ]
    saw_known = saw_unknown = False
    for candidate in cases:
        evidence = content_evidence("how to plan meals", candidate.id)
        for index in range(3):
            evidence.extend(
                listing_evidence(
                    f"{marker} planner",
                    candidate.id,
                    listing_id=f"L{index}",
                    seller_id=f"shop{index}",
                )
            )
        specification = spec_for(candidate, evidence)
        result = extract_product_job_fit(
            candidate_id=candidate.id, specification=specification
        )
        if result.features is not None and result.features.unknown_specification_fields:
            saw_unknown = True
        if marker in (specification.product_name.value or ""):
            saw_known = True
        serialized = ProductJobFitOut.from_result(result).model_dump_json()
        assert marker not in serialized, candidate.title

    # Both paths were genuinely exercised, so neither can silently stop
    # being covered.
    assert saw_known, "no case produced a marker-bearing generated field"
    assert saw_unknown, "no case produced an UNKNOWN specification field"


def test_unknown_fields_are_reported_by_name_only_never_by_value():
    """`unknown_specification_fields` may contain field NAMES and nothing else."""
    from app.services.product_job_fit import _COMPLETENESS_FIELDS

    candidate = make_candidate(
        title="guaranteed proven best-selling planner",
        buyer_outcome="guaranteed revenue of $5,000 per month",
    )
    evidence = content_evidence("how to plan meals", candidate.id)
    for index in range(3):
        evidence.extend(
            listing_evidence(
                "meal planner",
                candidate.id,
                listing_id=f"L{index}",
                seller_id=f"shop{index}",
            )
        )
    result = fit_for(candidate, evidence)
    assert result.features.unknown_specification_fields
    for name in result.features.unknown_specification_fields:
        assert name in _COMPLETENESS_FIELDS, name


def test_value_is_none_on_every_code_path():
    """No path may emit a number, including the unassessable ones."""
    specifications = [
        None,
        replace(
            spec_with_job(JobToBeDone.LEARN),
            state=SpecificationState.MISSING,
            missing_reason="none",
        ),
        spec_with_job(JobToBeDone.LEARN, insufficient=True),
        spec_with_job(JobToBeDone.UNKNOWN, SpecClaimClass.UNKNOWN, evidence_ids=()),
        spec_with_job(JobToBeDone.LEARN, SpecClaimClass.ASSUMED),
        spec_with_job(
            JobToBeDone.LEARN,
            ideal=IdealFormat.COMMUNITY,
            buildable=ProductFormat.PDF_GUIDE,
        ),
        spec_with_job(
            JobToBeDone.LEARN,
            ideal=IdealFormat.PDF_GUIDE,
            buildable=ProductFormat.PDF_GUIDE,
            scores=(("LEARN", 5), ("PLAN", 0)),
        ),
    ]
    for specification in specifications:
        result = run(specification)
        assert result.value is None, result.pattern


def test_the_module_reads_no_generated_specification_value():
    """AST guard: the fields whose `.value` is generated prose are unread."""
    source = MODULE_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "value":
            # `.value` is legitimate on enums; it must never be reached
            # through a generated SpecField.
            inner = node.value
            if isinstance(inner, ast.Attribute):
                assert inner.attr not in {
                    "product_name",
                    "core_promise",
                    "target_buyer",
                    "buyer_job",
                    "rationale",
                    "basis",
                }, ast.dump(node)


def test_the_assessment_is_always_inferred_even_from_observed_evidence():
    """Every contributing record is OBSERVED; the assessment still is not.

    4C caps the job at INFERRED and 4G caps itself at INFERRED again, so no
    chain of observations can produce an observed fit claim.
    """
    candidate = make_candidate()
    evidence = content_evidence("how to plan meals", candidate.id)
    for index in range(3):
        evidence.extend(
            listing_evidence(
                "printable meal planner",
                candidate.id,
                listing_id=f"L{index}",
                seller_id=f"shop{index}",
            )
        )
    assert all(item.truth_class is TruthClass.OBSERVED for item in evidence)
    result = fit_for(candidate, evidence)
    assert result.features.job_claim_class is SpecClaimClass.INFERRED
    assert result.assessment_truth_class is TruthClass.INFERRED
    assert result.fit_claim_class is not SpecClaimClass.OBSERVED


@pytest.mark.parametrize(
    "job_class",
    # OBSERVED is deliberately absent: Milestone 4C caps a job classification
    # at INFERRED, so a specification carrying an OBSERVED job cannot exist.
    [SpecClaimClass.INFERRED, SpecClaimClass.ASSUMED, SpecClaimClass.UNKNOWN],
)
def test_fit_claim_is_never_stronger_than_its_weakest_input(job_class):
    from app.services.product_specification import _CLAIM_STRENGTH

    specification = spec_with_job(JobToBeDone.LEARN, job_class)
    result = run(specification)
    inputs = [specification.job_classification.claim_class]
    if specification.format_recommendation is not None:
        inputs.append(specification.format_recommendation.ideal_claim_class)
        inputs.append(specification.format_recommendation.buildable_claim_class)
    weakest = max(_CLAIM_STRENGTH[c] for c in inputs)
    assert _CLAIM_STRENGTH[result.fit_claim_class] >= weakest


# ------------------------------------------- no commercial-success claims


@pytest.mark.parametrize(
    "marker",
    [
        "sales_probability",
        "conversion_probability",
        "product_market_fit",
        "willingness_to_pay",
        "market_size",
        "expected_revenue",
        "usefulness_to_buyer",
        "buyer_demand_proven",
    ],
)
def test_commercial_markers_are_permanently_unknown(marker):
    for specification in [
        None,
        spec_with_job(JobToBeDone.LEARN, SpecClaimClass.INFERRED),
        spec_with_job(JobToBeDone.UNKNOWN, SpecClaimClass.UNKNOWN, evidence_ids=()),
        spec_with_job(JobToBeDone.LEARN, insufficient=True),
    ]:
        result = run(specification)
        assert getattr(result, marker) is TruthClass.UNKNOWN


def test_no_emitted_string_makes_a_commercial_claim():
    """Every string the API returns is scanned for success vocabulary."""
    from app.api.routes import ProductJobFitOut

    banned = (
        r"\bwill sell\b",
        r"\bwill succeed\b",
        r"\bproven demand\b",
        r"\bguaranteed\b",
        r"\bbest[- ]selling\b",
        r"\bhigh[- ]demand\b",
        r"\bprofitable\b",
        r"\brevenue potential\b",
        r"\bmarket validated\b",
    )
    for specification in [
        None,
        spec_with_job(
            JobToBeDone.LEARN, SpecClaimClass.INFERRED,
            ideal=IdealFormat.PDF_GUIDE, buildable=ProductFormat.PDF_GUIDE,
            scores=(("LEARN", 9), ("PLAN", 0)),
        ),
        spec_with_job(
            JobToBeDone.LEARN, SpecClaimClass.INFERRED,
            ideal=IdealFormat.COMMUNITY, buildable=ProductFormat.PDF_GUIDE,
        ),
        spec_with_job(JobToBeDone.UNKNOWN, SpecClaimClass.UNKNOWN, evidence_ids=()),
    ]:
        serialized = ProductJobFitOut.from_result(run(specification)).model_dump_json()
        for pattern in banned:
            assert not re.search(pattern, serialized, re.IGNORECASE), (
                pattern,
                specification is None,
            )


def test_value_is_permanently_none_and_cannot_reach_legacy_scoring():
    result = run(spec_with_job(JobToBeDone.LEARN, SpecClaimClass.INFERRED))
    assert result.value is None
    assert result.dimension_name == DIM_PRODUCT_JOB_FIT
    assert result.dimension_name == "preliminary_product_job_fit"
    # Neither the legacy ScoreDimensions field nor a market-fit claim.
    assert result.dimension_name != "problem_product_fit"
    assert "market" not in result.dimension_name


def test_no_pattern_or_fidelity_maps_to_a_number():
    """AST guard: no dict literal turns a categorical into a score."""
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            keyed_on_categorical = (
                isinstance(key, ast.Attribute)
                and isinstance(key.value, ast.Name)
                and key.value.id
                in {"FitPattern", "SubstitutionFidelity", "InteractionMode"}
            )
            if keyed_on_categorical:
                assert not (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, (int, float))
                ), ast.dump(node)


def test_versions_are_reported():
    result = run(spec_with_job(JobToBeDone.LEARN))
    assert result.version == PRODUCT_JOB_FIT_VERSION
    assert result.pattern_version == FIT_PATTERN_VERSION
    assert result.interaction_mode_version == "interaction_mode_v1"


# --------------------------------------------------------- no magnitudes


def test_module_reads_no_magnitude_field():
    """Magnitudes cannot make a product structurally more suitable."""
    forbidden = {
        "search_volume",
        "cpc",
        "competition_index",
        "view_count",
        "views",
        "like_count",
        "review_count",
        "rating",
        "price",
        "median_views",
        "relevant_listing_count",
        "distinct_seller_count",
        "top_video_attention_share",
    }
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    read = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            read.add(node.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
    assert not (read & forbidden), sorted(read & forbidden)


def test_module_imports_only_4c_and_shared_primitives():
    tree = ast.parse(MODULE_PATH.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    for banned in (
        "scoring",
        "purchase_evidence",
        "price_evidence",
        "buyer_reach",
        "competition_opportunity",
        "audience_attention",
        "search_demand",
        "marketplace",
        "public_content",
    ):
        assert not any(banned in module for module in modules), banned
    # 4C is the one derivation 4G is permitted to consume.
    assert any("product_specification" in module for module in modules)


def test_no_scoring_vocabulary_is_introduced():
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    code = ast.unparse(tree)
    for token in ("RED", "YELLOW", "GREEN"):
        assert not re.search(rf"\b{token}\b", code), token
    for token in ("score_opportunity", "ScoreDimensions", "Classification"):
        assert token not in code, token


# ----------------------------------------------------------------- determinism


def _identity(result) -> tuple:
    features = result.features
    return (
        result.state,
        result.pattern,
        result.pattern_boundary,
        result.missing_reason,
        result.fit_claim_class,
        tuple((o.topic, o.finding, o.claim_class) for o in result.observations),
        None
        if features is None
        else (
            features.job,
            features.job_claim_class,
            features.job_supported_by_observed_evidence,
            features.job_citation_count,
            features.job_matched_token_count,
            features.job_score_lead,
            features.job_is_ambiguous,
            features.competing_jobs,
            features.ideal_format,
            features.buildable_format,
            features.ideal_interaction_mode,
            features.buildable_interaction_mode,
            features.substitution_fidelity,
            features.unknown_specification_fields,
            features.known_specification_field_count,
            features.conflict_count,
            features.conflict_topics,
            features.diverges_from_observed_market,
        ),
    )


def test_output_is_identical_under_1000_evidence_shuffles():
    """Shuffling the evidence 4C saw must not move the fit assessment."""
    candidate = make_candidate()
    evidence: list[EvidenceItem] = []
    for index in range(6):
        evidence.extend(
            content_evidence(
                "how to plan meals and track a weekly schedule",
                candidate.id,
                video_id=f"V{index}",
            )
        )
    for index in range(4):
        evidence.extend(
            listing_evidence(
                "printable meal planner template",
                candidate.id,
                listing_id=f"L{index}",
                seller_id=f"shop{index}",
            )
        )
    baseline = _identity(fit_for(candidate, evidence))
    rng = random.Random(20260911)
    mismatches = 0
    for _ in range(1000):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        if _identity(fit_for(candidate, shuffled)) != baseline:
            mismatches += 1
    assert mismatches == 0


def test_reversed_and_repeated_evidence_are_identical():
    candidate = make_candidate()
    evidence: list[EvidenceItem] = []
    for index in range(5):
        evidence.extend(
            content_evidence("how to plan meals", candidate.id, video_id=f"V{index}")
        )
    baseline = _identity(fit_for(candidate, evidence))
    assert _identity(fit_for(candidate, list(reversed(evidence)))) == baseline
    for _ in range(5):
        assert _identity(fit_for(candidate, evidence)) == baseline


def test_competing_jobs_and_conflict_topics_are_canonically_sorted():
    specification = spec_with_job(
        JobToBeDone.PLAN,
        SpecClaimClass.INFERRED,
        scores=(("PLAN", 4), ("TRACK", 4), ("ORGANISE", 4), ("ASSESS", 4)),
    )
    result = run(specification)
    assert list(result.features.competing_jobs) == sorted(
        result.features.competing_jobs
    )
    assert list(result.features.conflict_topics) == sorted(
        result.features.conflict_topics
    )
    assert list(result.features.unknown_specification_fields) == sorted(
        result.features.unknown_specification_fields
    )


def test_score_order_does_not_change_the_margin():
    """4C's score tuple order must not decide ambiguity."""
    scores = (("PLAN", 4), ("TRACK", 3), ("LEARN", 1))
    baseline = run(spec_with_job(JobToBeDone.PLAN, scores=scores))
    rng = random.Random(5)
    for _ in range(50):
        shuffled = list(scores)
        rng.shuffle(shuffled)
        result = run(spec_with_job(JobToBeDone.PLAN, scores=tuple(shuffled)))
        assert result.features.job_score_lead == baseline.features.job_score_lead
        assert result.features.competing_jobs == baseline.features.competing_jobs


# ---------------------------------------------------------------------- API


def test_the_specification_endpoint_returns_a_fit_assessment():
    from fastapi.testclient import TestClient

    from app.main import app


    candidate = make_candidate()
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "evidence": [
            item.model_dump(mode="json")
            for item in content_evidence("how to plan meals", candidate.id)
        ],
    }
    response = TestClient(app).post("/product/specification", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "product_job_fit" in body
    fit = body["product_job_fit"]
    assert fit["value"] is None
    assert fit["dimension_name"] == "preliminary_product_job_fit"
    assert fit["assessment_truth_class"] == "INFERRED"
    assert fit["product_market_fit"] == "UNKNOWN"
    assert fit["pattern_boundary"].strip()


def test_route_count_unchanged_and_score_still_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert_route_surface_unchanged(app)
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_specification_response_is_additive():
    from app.main import app

    schema = app.openapi()
    response = schema["components"]["schemas"]["ProductSpecificationResponse"]
    assert "specification" in response["properties"]
    assert "product_job_fit" in response["properties"]
    assert_openapi_surface_unchanged(schema)


def test_every_pattern_is_actually_reachable():
    """Regression for the defect this milestone's own testing found.

    An earlier version tested the job classification for SpecClaimClass
    OBSERVED, which Milestone 4C never assigns — it caps a job at INFERRED by
    design. That made `job_supported_by_observed_evidence` permanently False,
    so JOB_ASSUMED_NOT_OBSERVED fired for every specification and every
    pattern below it in the chain was dead code that no test could reach.

    Enumerating reachability is the guard: a classifier whose later branches
    cannot fire is not a conservative classifier, it is a broken one.
    """
    reached = set()

    reached.add(run(None).pattern)
    reached.add(
        run(
            replace(
                spec_with_job(JobToBeDone.LEARN),
                state=SpecificationState.MISSING,
                missing_reason="none",
            )
        ).pattern
    )
    reached.add(run(spec_with_job(JobToBeDone.LEARN, insufficient=True)).pattern)
    reached.add(
        run(
            spec_with_job(
                JobToBeDone.UNKNOWN, SpecClaimClass.UNKNOWN, evidence_ids=()
            )
        ).pattern
    )
    reached.add(
        run(spec_with_job(JobToBeDone.LEARN, SpecClaimClass.ASSUMED)).pattern
    )
    reached.add(
        run(
            spec_with_job(
                JobToBeDone.PLAN, scores=(("PLAN", 3), ("TRACK", 2), ("LEARN", 0))
            )
        ).pattern
    )
    reached.add(
        run(
            spec_with_job(
                JobToBeDone.LEARN,
                ideal=IdealFormat.COMMUNITY,
                buildable=ProductFormat.PDF_GUIDE,
                scores=(("LEARN", 5), ("PLAN", 0)),
            )
        ).pattern
    )
    reached.add(
        run(
            spec_with_job(
                JobToBeDone.PLAN,
                ideal=IdealFormat.TEMPLATE_PACK,
                buildable=ProductFormat.PDF_GUIDE,
                scores=(("PLAN", 5), ("LEARN", 0)),
            )
        ).pattern
    )
    reached.add(
        run(
            spec_with_job(
                JobToBeDone.LEARN,
                ideal=IdealFormat.PDF_GUIDE,
                buildable=ProductFormat.PDF_GUIDE,
                scores=(("LEARN", 5), ("PLAN", 0)),
            )
        ).pattern
    )

    # ASSESSMENT_UNAVAILABLE is produced only by the endpoint's boundary, so
    # it is excluded here and covered by its own test.
    expected = set(FitPattern) - {FitPattern.ASSESSMENT_UNAVAILABLE}
    assert reached == expected, sorted(p.value for p in expected - reached)


def test_every_substitution_fidelity_is_reachable():
    fidelities = set()
    fidelities.add(run(None).features is None and SubstitutionFidelity.UNKNOWN)
    cases = [
        (IdealFormat.PDF_GUIDE, ProductFormat.PDF_GUIDE),
        (IdealFormat.CALCULATOR, ProductFormat.SPREADSHEET_TOOL),
        (IdealFormat.TEMPLATE_PACK, ProductFormat.PDF_GUIDE),
        (IdealFormat.COMMUNITY, ProductFormat.PDF_GUIDE),
    ]
    seen = set()
    for ideal, buildable in cases:
        result = run(
            spec_with_job(JobToBeDone.LEARN, ideal=ideal, buildable=buildable)
        )
        seen.add(result.features.substitution_fidelity)
    assert seen == {
        SubstitutionFidelity.DIRECT,
        SubstitutionFidelity.MODE_PRESERVED,
        SubstitutionFidelity.MODE_CHANGED,
        SubstitutionFidelity.MODE_UNMET,
    }


def test_assessment_boundary_degrades_only_the_assessment(monkeypatch):
    """A bug in 4G must never break an endpoint that produced a valid spec."""
    from fastapi.testclient import TestClient

    import app.api.routes as routes
    from app.main import app

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("fit assessment bug")

    monkeypatch.setattr(routes, "extract_product_job_fit", boom)

    candidate = make_candidate()
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "evidence": [
            item.model_dump(mode="json")
            for item in content_evidence("how to plan meals", candidate.id)
        ],
    }
    response = TestClient(app).post("/product/specification", json=payload)
    # The specification still comes back.
    assert response.status_code == 200
    body = response.json()
    assert body["specification"]["product_name"] is not None
    fit = body["product_job_fit"]
    assert fit["pattern"] == "ASSESSMENT_UNAVAILABLE"
    assert fit["features"] is None
    assert "RuntimeError" in fit["missing_reason"]
    assert "error_id=" in fit["missing_reason"]


def test_an_unknown_field_carrying_a_value_still_leaks_nothing():
    """Defensive: the guarantee must not depend on a 4C invariant.

    Milestone 4C never emits an UNKNOWN SpecField that still carries a value,
    so a leak through the unknown-field path is unreachable today. That makes
    the safety an accident of 4C's behaviour rather than a property of this
    module, and a mutation proved a naive leak survives every other test.
    This hand-builds the case 4C does not produce and asserts the name-only
    guarantee holds anyway.
    """
    from app.api.routes import ProductJobFitOut
    from app.services.product_specification import SpecField

    marker = "qponmlkjih"
    specification = replace(
        spec_with_job(JobToBeDone.LEARN),
        product_name=SpecField(
            value=f"{marker} planner",
            claim_class=SpecClaimClass.UNKNOWN,
            basis=f"rejected: {marker}",
        ),
    )
    assert specification.product_name.value is not None
    assert specification.product_name.claim_class is SpecClaimClass.UNKNOWN

    result = run(specification)
    assert "product_name" in result.features.unknown_specification_fields
    serialized = ProductJobFitOut.from_result(result).model_dump_json()
    assert marker not in serialized


def test_4c_never_emits_an_unknown_field_that_carries_a_value():
    """Pins the 4C invariant the test above deliberately does not rely on."""
    candidate = make_candidate(
        title="guaranteed proven best-selling planner",
        buyer_outcome="guaranteed revenue of $5,000 per month",
    )
    evidence = content_evidence("how to plan meals", candidate.id)
    specification = spec_for(candidate, evidence)
    for name in ("product_name", "target_buyer", "buyer_job", "core_promise"):
        field = getattr(specification, name)
        if field.claim_class is SpecClaimClass.UNKNOWN:
            assert field.value is None, name


def test_the_stated_fit_dimensions_map_to_the_modes_they_claim():
    """Each structural pairing this milestone exists to detect, pinned.

    These are the compatibilities the milestone was specified around. Pinning
    them here means a future edit to the mode tables cannot quietly change
    what "repeated calculation needs a tool" or "ongoing need is unmet by a
    document" means.
    """
    assert IDEAL_FORMAT_MODE[IdealFormat.CALCULATOR] is (
        InteractionMode.REPEATED_COMPUTATION
    )
    assert BUILDABLE_FORMAT_MODE[ProductFormat.SPREADSHEET_TOOL] is (
        InteractionMode.REPEATED_COMPUTATION
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.TEMPLATE_PACK] is (
        InteractionMode.ARTEFACT_PRODUCTION
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.CHECKLIST] is (
        InteractionMode.SEQUENCED_EXECUTION
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.PDF_GUIDE] is (
        InteractionMode.STATIC_REFERENCE
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.MINI_COURSE] is (
        InteractionMode.TIME_SEQUENCED_DELIVERY
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.MICRO_SAAS] is (
        InteractionMode.ONGOING_SERVICE
    )
    assert IDEAL_FORMAT_MODE[IdealFormat.COMMUNITY] is (
        InteractionMode.ONGOING_INTERACTION
    )
    # The last three are the ones no V1 artefact can provide.
    for mode in (
        InteractionMode.TIME_SEQUENCED_DELIVERY,
        InteractionMode.ONGOING_SERVICE,
        InteractionMode.ONGOING_INTERACTION,
    ):
        assert mode in UNDELIVERABLE_BY_ARTEFACT
