"""Milestone 6B — Step 7a: deep collection.

SPEC_STEP_7_8.md §1 defines Step 7 as a second, deeper evidence pass over the
selected candidates only, using the same three approved providers at raised
caps. This module is the 7a half: collection. Derivation of the Step 7
dimensions (7b) belongs to 6C and is not done here.

Three things carry this slice.

**No new provider, no new external dependency.** The deep pass runs exactly the
capability runners the cheap pass runs, through the `CapabilityCaps` seam that
already existed. Depth is bought by spending the saved budget on ~5 candidates
instead of ~20, not by reaching somewhere new.

**Caps are named and versioned.** `deep_pass_caps_v1` supplies all eight
`CapabilityCaps` fields and its version is recorded on the result, so any
evidence can be attributed to the collection budget that produced it and a
later budget change cannot silently alter what an earlier result meant.

**A cached shallow result may never satisfy a deeper request.** §1.1 is
enforced in the storage layer (limit-aware cache identity) and surfaced here as
a per-capability depth-miss count, so a deep pass that looks suspiciously cheap
can be diagnosed rather than trusted.

No POS, no Evidence Confidence computation, no scoring, no classification, no
route. This module collects; nothing here reads the value of an observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.models import Candidate
from app.providers.base import (
    MarketplaceProvider,
    ProviderError,
    PublicContentProvider,
    SearchDemandProvider,
)
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_ORDER,
    CAPABILITY_SEARCH_DEMAND,
    MISSING_CAPABILITY_NOT_REQUESTED,
    STATUS_NOT_REQUESTED,
    STATUS_PROVIDER_FAILED,
    STATUS_UNEXPECTED_PROVIDER_ERROR,
    CapabilityCaps,
    CapabilityOutcome,
    run_marketplace_capability,
    run_public_content_capability,
    run_search_demand_capability,
    safe_exception_message,
)
from app.storage.memory import ResearchStore

DEEP_COLLECTION_VERSION = "deep_collection_v1"

# ------------------------------------------------------------ U-6 cap values
#
# `deep_pass_caps_v1`. A NAMED, VERSIONED, EXPLICITLY UNCALIBRATED V1 policy
# assumption: a collection-budget decision taken by inspection, not derived
# from outcome data, and not to be described as empirically validated. A later
# recalibration ships as `_v2` rather than editing these in place, so evidence
# already collected stays attributable to the budget that authorized it.
DEEP_PASS_CAPS_VERSION = "deep_pass_caps_v1"

DEEP_PASS_CAPS = CapabilityCaps(
    max_provider_calls=50,
    max_queries=40,
    max_keywords=100,
    max_listings_per_query=50,
    max_review_lookups=40,
    max_videos_per_query=25,
    max_quota_units=3000,
    max_channel_lookups=5,
)

# Reading these against the cheap-pass defaults needs one distinction, because
# one cap looks like a REDUCTION and is not.
#
# `max_listings_per_query` and `max_videos_per_query` are PER QUERY, so they
# compare directly: 50 against 25, and 25 against 10. Both strictly deeper, and
# both are what §1.1's cache-depth rule governs.
#
# Every other cap is a budget for the whole pass, and the two passes cover
# different numbers of candidates: the cheap pass spreads its budget over ~20
# discovered candidates, the deep pass concentrates it on the ~5 selected. So
# `max_keywords` at 100 is a smaller pass-level number than the cheap default
# of 200 and nonetheless twice the depth per candidate -- 100/5 = 20 keywords
# each, against 200/20 = 10. That is the trade §1 describes: "the cheap pass is
# cheap because it is capped across ~20 candidates; depth is bought by spending
# the saved budget on 5." Comparing the two pass totals directly reads a
# deepening as a reduction.

LIMITATIONS: tuple[str, ...] = (
    "The deep-pass caps are an unvalidated V1 collection budget chosen by "
    "inspection. They are not empirically validated, and a larger budget is "
    "not evidence that a larger budget is warranted.",
    "Depth is a property of the search, never of the field. Collecting more "
    "listings or videos says nothing about how many exist; a capped or "
    "exhausted result is never an observation that there are no more.",
    "This module collects. It derives no dimension, computes no score, and "
    "reads the value of no observation.",
)


@dataclass(slots=True, frozen=True)
class DeepCollectionResult:
    """What one deep pass achieved, reported honestly.

    Carries the cap-set version so the evidence it produced can always be
    attributed to the budget that authorized it.
    """

    research_run_id: UUID
    candidate_ids: tuple[UUID, ...]
    capabilities: tuple[CapabilityOutcome, ...]
    snapshot_ids: tuple[UUID, ...] = ()
    version: str = DEEP_COLLECTION_VERSION
    caps_version: str = DEEP_PASS_CAPS_VERSION
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    @property
    def true_cache_hits(self) -> int:
        """Entries reused because they were already deep enough."""
        return sum(o.cached_query_count for o in self.capabilities)

    @property
    def depth_misses(self) -> int:
        """Entries present but too shallow, so re-fetched.

        A deep pass with many true hits and no depth misses over a store built
        by a shallower pass is the signature §1.1 exists to make visible.
        """
        return sum(o.depth_miss_query_count for o in self.capabilities)


def _not_requested(capability: str) -> CapabilityOutcome:
    return CapabilityOutcome(
        capability=capability,
        status=STATUS_NOT_REQUESTED,
        failure_reason=MISSING_CAPABILITY_NOT_REQUESTED,
    )


async def run_deep_collection(
    candidates: list[Candidate],
    store: ResearchStore,
    research_run_id: UUID,
    search_demand_provider: SearchDemandProvider | None = None,
    marketplace_provider: MarketplaceProvider | None = None,
    public_content_provider: PublicContentProvider | None = None,
    location: str = "US",
    language: str = "en",
    caps: CapabilityCaps | None = None,
    unavailable_capabilities: dict[str, str] | None = None,
) -> DeepCollectionResult:
    """Re-run the three approved capabilities at deep-pass caps.

    `research_run_id` is required, not defaulted: a deep pass extends an
    existing run's evidence and must not invent a scope of its own.

    A provider passed as None is not requested. A provider that fails degrades
    that capability only — the same boundary the cheap pass uses, for the same
    reason: one defective provider must not destroy a pass that others have
    already contributed evidence to.
    """
    budget = caps if caps is not None else DEEP_PASS_CAPS
    unavailable = unavailable_capabilities or {}
    outcomes: list[CapabilityOutcome] = []
    snapshot_ids: list[UUID] = []

    for capability in CAPABILITY_ORDER:
        if capability == CAPABILITY_SEARCH_DEMAND:
            provider = search_demand_provider
        elif capability == CAPABILITY_MARKETPLACE:
            provider = marketplace_provider
        else:
            provider = public_content_provider

        if capability in unavailable:
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_PROVIDER_FAILED,
                    failure_reason=unavailable[capability],
                )
            )
            continue

        if provider is None:
            outcomes.append(_not_requested(capability))
            continue

        try:
            if capability == CAPABILITY_SEARCH_DEMAND:
                outcome, _, snapshot_id = await run_search_demand_capability(
                    candidates, provider, store, research_run_id, location, language, budget
                )
            elif capability == CAPABILITY_MARKETPLACE:
                outcome, _, snapshot_id = await run_marketplace_capability(
                    candidates, provider, store, research_run_id, budget
                )
            else:
                outcome, _, snapshot_id = await run_public_content_capability(
                    candidates, provider, store, research_run_id, budget
                )
        except ProviderError as exc:
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_PROVIDER_FAILED,
                    provider=getattr(provider, "name", None),
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - deliberate capability boundary
            # Mirrors the cheap pass's boundary exactly. Outside the provider
            # contract: an adapter bug, a library error, anything unforeseen.
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_UNEXPECTED_PROVIDER_ERROR,
                    provider=getattr(provider, "name", None),
                    failure_reason=safe_exception_message(exc),
                )
            )
            continue

        outcomes.append(outcome)
        if snapshot_id is not None:
            snapshot_ids.append(snapshot_id)

    return DeepCollectionResult(
        research_run_id=research_run_id,
        candidate_ids=tuple(candidate.id for candidate in candidates),
        capabilities=tuple(outcomes),
        snapshot_ids=tuple(snapshot_ids),
    )
