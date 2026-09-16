"""The application's route surface, pinned once.

Eight milestones each asserted "my slice added no endpoint" by hard-coding a
route count, which meant a deliberate surface change had to be edited in eight
places and a reviewer had to trust that all eight agreed. The surface is named
here instead, as the set of paths rather than a count: a test that names the
paths says what changed when it fails, and a count only says that something
did.

Adding a route is a deliberate act. It belongs in this list, in the milestone
that adds it, with the reason.
"""

EXPECTED_ROUTE_PATHS: frozenset[str] = frozenset(
    {
        # Infrastructure and docs.
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        # Steps 1-3: discovery and the cheap research pass.
        "/candidates/discover",
        "/research/search-demand",
        "/research/search-demand/snapshots/{snapshot_id}",
        "/research/marketplace",
        "/research/marketplace/snapshots/{snapshot_id}",
        "/research/public-content",
        "/research/public-content/snapshots/{snapshot_id}",
        "/research/preliminary",
        # Steps 7-9, added by Milestone 7A: deep research, scoring,
        # classification, and the post-score content chain.
        "/workflow/deep-research",
        "/workflow/runs/{research_run_id}/candidates/{candidate_id}",
        "/workflow/content-plan",
        # Step 10: product generation, gated on a score by 7A.
        "/product/specification",
        # Quarantined since the legacy scoring was rejected. Always 410.
        "/score",
    }
)


def assert_route_surface_unchanged(app) -> None:
    """The live surface is exactly the pinned one.

    Named-set comparison rather than a count, so an unintended addition or
    removal reports which path it was.
    """
    live = {route.path for route in app.routes if hasattr(route, "methods")}
    assert live == EXPECTED_ROUTE_PATHS, {
        "unexpected": sorted(live - EXPECTED_ROUTE_PATHS),
        "missing": sorted(EXPECTED_ROUTE_PATHS - live),
    }


# The subset that appears in the OpenAPI schema: the docs and schema endpoints
# are served by FastAPI itself, and `/score` is `include_in_schema=False`
# because it exists only to answer 410.
NON_SCHEMA_PATHS: frozenset[str] = frozenset(
    {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc", "/score"}
)

EXPECTED_OPENAPI_PATHS: frozenset[str] = EXPECTED_ROUTE_PATHS - NON_SCHEMA_PATHS


def assert_openapi_surface_unchanged(schema: dict) -> None:
    live = set(schema["paths"])
    assert live == EXPECTED_OPENAPI_PATHS, {
        "unexpected": sorted(live - EXPECTED_OPENAPI_PATHS),
        "missing": sorted(EXPECTED_OPENAPI_PATHS - live),
    }
