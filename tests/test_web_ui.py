"""Milestone 7B tests: the workflow interface.

The UI is where this system's honesty reaches a screen. Every upstream
milestone works to keep a missing measurement missing; if the page renders
`null` as 0, or an absent colour as RED, all of that is undone at the last
step. So the display rules are pure functions in `display.js` and this file
EXECUTES them rather than string-matching the source.

Node is used where it is available (it is, on the CI runner); the behavioural
tests skip cleanly where it is not, and the structural guards always run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.route_surface import assert_route_surface_unchanged

WEB = Path("app/web")
DISPLAY = WEB / "display.js"
APP_JS = WEB / "app.js"
INDEX = WEB / "index.html"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not available")

client = TestClient(app)


def run_display(expression: str) -> str:
    """Evaluate one display expression against the real module."""
    script = (
        f"const D = require({str(DISPLAY.resolve())!r});"
        f"process.stdout.write(String({expression}));"
    )
    out = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


# --------------------------------------------------- it is actually served


def test_the_interface_is_served():
    page = client.get("/app/")
    assert page.status_code == 200
    assert "Product Intelligence" in page.text
    for asset in ("display.js", "app.js"):
        served = client.get(f"/app/{asset}")
        assert served.status_code == 200, asset
        assert served.content


def test_the_interface_adds_no_api_route():
    """A static mount, not a route: it cannot be mistaken for an endpoint."""
    assert_route_surface_unchanged(app)
    assert "/app" not in {
        route.path for route in app.routes if hasattr(route, "methods")
    }


def test_the_page_loads_the_display_rules_before_the_app():
    """`app.js` destructures `Display` at parse time, so the order matters."""
    html = INDEX.read_text(encoding="utf-8")
    assert html.index("display.js") < html.index("app.js")


# ------------------------------------- an absent measurement is never a zero


@needs_node
def test_an_absent_measurement_renders_as_absent_not_as_zero():
    for value in ("null", "undefined"):
        rendered = run_display(f"D.measurement({value}, ' / 100')")
        assert "not measured" in rendered
        assert ">0<" not in rendered
        assert "num" not in rendered, "it must not use the numeric style"


@needs_node
def test_an_observed_zero_renders_as_the_real_measurement_it_is():
    """The distinction the whole system exists to keep, at the last step."""
    rendered = run_display("D.measurement(0, ' / 100')")
    assert ">0<" in rendered
    assert "not measured" not in rendered
    absent = run_display("D.measurement(null, ' / 100')")
    assert rendered != absent


@needs_node
def test_a_real_score_renders_as_itself():
    assert ">72.38<" in run_display("D.measurement(72.38, ' / 100')")


# ---------------------------------------------- an absent colour is not RED


@needs_node
def test_an_absent_classification_is_never_rendered_as_red():
    for state in ("SCORED_UNCLASSIFIED", "INSUFFICIENT_EVIDENCE", "NOT_SCORED"):
        rendered = run_display(f"D.colourPill(null, {state!r})")
        assert "RED" not in rendered, state
        assert "NONE" in rendered
        assert "no colour" in rendered


@needs_node
def test_a_withheld_colour_says_why_it_was_withheld():
    below = run_display("D.colourPill(null, 'SCORED_UNCLASSIFIED')")
    assert "below the confidence floor" in below
    unscored = run_display("D.colourPill(null, 'INSUFFICIENT_EVIDENCE')")
    assert "no score exists" in unscored
    assert below != unscored, "the two absences are different and read differently"


@needs_node
@pytest.mark.parametrize("colour", ["RED", "YELLOW", "GREEN"])
def test_a_real_classification_renders_as_itself(colour):
    rendered = run_display(f"D.colourPill({colour!r}, 'CLASSIFIED')")
    assert colour in rendered
    assert "no colour" not in rendered


# ----------------------------------------------- a proxy stays a proxy


@needs_node
def test_the_purchase_proxy_keeps_its_label_wherever_it_is_shown():
    label = run_display("D.subScoreLabel('pos_purchase_proxy')")
    assert "PROXY" in label
    assert "never sales or revenue" in label


@needs_node
def test_attention_is_never_labelled_as_demand():
    label = run_display("D.subScoreLabel('pos_audience_attention')")
    assert "ATTENTION" in label
    assert "never buyer demand" in label


@needs_node
def test_every_dimension_states_whether_it_can_carry_a_score():
    contextual = run_display("D.dimensionRole({pos_eligible: false})")
    scored = run_display("D.dimensionRole({pos_eligible: true})")
    assert "never in a score" in contextual
    assert "feeds the score" in scored
    assert contextual != scored


@needs_node
def test_rendered_values_are_escaped():
    rendered = run_display("D.measurement('<script>x</script>')")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# ------------------------------------------------------ language boundaries


def test_the_interface_makes_no_forecast_or_revenue_claim():
    """Negation-aware: the page may NAME a claim in order to refuse it."""
    import re

    text = INDEX.read_text(encoding="utf-8") + APP_JS.read_text(encoding="utf-8")
    text += DISPLAY.read_text(encoding="utf-8")
    for claim in (
        "will sell", "guaranteed", "revenue", "profit", "units sold",
        "viral", "predicted", "forecast",
    ):
        for match in re.finditer(re.escape(claim), text, re.IGNORECASE):
            window = text[max(0, match.start() - 80) : match.start()]
            assert re.search(
                r"\b(never|not|no|nothing)\b", window, re.IGNORECASE
            ), f"unhedged claim {claim!r}: {text[match.start() - 80 : match.end() + 20]!r}"


def test_the_interface_states_that_the_policy_is_uncalibrated():
    html = INDEX.read_text(encoding="utf-8")
    assert "Uncalibrated V1 policy" in html
    assert "nothing on this page is a forecast" in html


def test_the_interface_shows_limitations_and_missing_reasons():
    source = APP_JS.read_text(encoding="utf-8")
    # Limitations are rendered, not hidden.
    assert "limitations" in source
    # A dimension's missing reason and a blocked sub-score's reason are shown.
    assert "missing_reason" in source
    assert "blocked_reason" in source
    assert "state_boundary" in source
    assert "excluded_dimensions" in source


def test_the_interface_says_an_unscoreable_candidate_is_unmeasured_not_rejected():
    source = APP_JS.read_text(encoding="utf-8")
    assert "unmeasured, not rejected" in source


def test_the_interface_never_coerces_a_missing_value_to_a_number():
    """`x || 0` on a score is exactly how a missing measurement becomes zero."""
    source = APP_JS.read_text(encoding="utf-8") + DISPLAY.read_text(encoding="utf-8")
    for coercion in ("|| 0", "|| '0'", "??0", "?? 0", "Number(s.opportunity_score)"):
        assert coercion not in source, coercion


def test_the_interface_calls_only_the_real_endpoints():
    """It can only display what the API actually returns."""
    source = APP_JS.read_text(encoding="utf-8")
    import re

    called = set(re.findall(r'api\("([^"]+)"', source))
    templated = set(re.findall(r'api\(`([^`]+)`', source))
    for path in called:
        assert path in {
            "/openapi.json",
            "/candidates/discover",
            "/research/preliminary",
            "/workflow/deep-research",
            "/product/specification",
            "/workflow/content-plan",
        }, path
    assert not templated or all("/workflow/" in p for p in templated)


def test_the_interface_is_a_single_page_with_no_build_step():
    """Function over polish: no framework, no bundler, no node_modules."""
    assert sorted(p.name for p in WEB.iterdir()) == [
        "app.js",
        "display.js",
        "index.html",
    ]
    html = INDEX.read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html, "no external assets"


# ===================================================================
# Milestone 7C — regressions for defects found by driving the real UI
# ===================================================================


@needs_node
def test_a_validation_error_is_readable_rather_than_raw_json():
    """DEFECT: FastAPI returns `detail` as a LIST for a 422, and the handler
    stringified it, putting a raw JSON blob in front of a person."""
    rendered = run_display(
        "D.formatApiError([{loc:['body','seed_keyword'],"
        "msg:'String should have at least 2 characters'}])"
    )
    assert "seed_keyword" in rendered
    assert "at least 2 characters" in rendered
    assert "{" not in rendered and "[" not in rendered


@needs_node
def test_every_error_shape_the_api_can_return_is_handled():
    """A plain string, the gate's {reason, message}, and a 422 list."""
    assert run_display("D.formatApiError('research run not found')") == (
        "research run not found"
    )
    assert "could not be scored" in run_display(
        "D.formatApiError({reason:'x', message:'candidate could not be scored'})"
    )
    assert run_display("D.formatApiError(null, 'Bad Request')") == "Bad Request"


@needs_node
def test_a_capability_that_could_not_be_constructed_explains_itself():
    """DEFECT: capability outcomes were never rendered, so with no credentials
    every dimension read MISSING with nothing on screen saying why."""
    note = run_display(
        "D.capabilityNote({status:'PROVIDER_FAILED',"
        "failure_reason:'MissingCredentialsError: ETSY_API_KEY is not set'})"
    )
    assert "ETSY_API_KEY" in note
    assert "MISSING" in note
    assert "nothing was assumed" in note

    not_requested = run_display("D.capabilityNote({status:'NOT_REQUESTED'})")
    assert "no provider was selected" in not_requested
    assert "rather than zero" in not_requested


@needs_node
def test_a_comparison_row_preserves_null_rather_than_substituting_a_number():
    row = run_display(
        "JSON.stringify(D.comparisonRow({candidate_id:'c1',"
        "scoring:{scoring_state:'INSUFFICIENT_EVIDENCE',opportunity_score:null,"
        "evidence_confidence:71.2,classification:null,"
        "sub_scores:[{name:'pos_search_demand',value:74.4},"
        "{name:'pos_purchase_proxy',value:null}],"
        "excluded_dimensions:[{dimension:'deep_audience_attention'}]},"
        "context:[]}, 'A candidate'))"
    )
    import json as _json

    parsed = _json.loads(row)
    assert parsed["opportunity_score"] is None
    assert parsed["pos_purchase_proxy"] is None
    assert parsed["pos_audience_attention"] is None, "absent sub-score stays absent"
    assert parsed["pos_search_demand"] == 74.4
    assert parsed["evidence_confidence"] == 71.2
    assert parsed["excluded"] == ["deep_audience_attention"]


@needs_node
def test_an_unmeasured_candidate_sorts_last_in_both_directions():
    """An absent score is not a low one, so it never sorts as though it were."""
    rows = (
        "[{title:'none',opportunity_score:null},"
        "{title:'low',opportunity_score:10},"
        "{title:'high',opportunity_score:90}]"
    )
    desc = run_display(
        f"D.sortRows({rows},'opportunity_score',true).map(r=>r.title).join(',')"
    )
    asc = run_display(
        f"D.sortRows({rows},'opportunity_score',false).map(r=>r.title).join(',')"
    )
    assert desc == "high,low,none"
    assert asc == "low,high,none"


def test_sorting_is_over_an_approved_field_and_adds_no_composite():
    """Negation-aware: the module may NAME a composite in order to refuse it.

    A bare substring scan fires on the comment that says "and no
    recommendation", which is the opposite of the thing being guarded against.
    """
    import re

    source = DISPLAY.read_text(encoding="utf-8")
    for invented in ("weight", "rank", "recommend", "composite"):
        for match in re.finditer(invented, source, re.IGNORECASE):
            window = source[max(0, match.start() - 60) : match.start()]
            assert re.search(
                r"\b(no|not|never|nor)\b", window, re.IGNORECASE
            ), f"{invented}: {source[match.start() - 60 : match.end() + 30]!r}"
    # And the sort really is a comparison of one field, with no arithmetic.
    sort_body = source.split("function sortRows(")[1].split("\n  }")[0]
    for arithmetic in ("*", "+", "/"):
        assert arithmetic not in sort_body, arithmetic


@needs_node
def test_the_stage_indicator_covers_the_whole_pipeline_in_order():
    stages = run_display("D.STAGES.join(',')")
    assert stages == "DISCOVERED,RESEARCHED,SCORED,PRODUCT,CONTENT"
    assert run_display("String(D.stageIndex('SCORED'))") == "2"
    assert run_display("String(D.stageIndex('nonsense'))") == "-1"


def test_a_new_run_clears_every_trace_of_the_previous_one():
    """DEFECT: starting a new discovery left the previous run's scores,
    evidence and action buttons on screen and clickable. Acting on one sent
    the NEW run id with an OLD candidate id; the server refused, but only
    after stale results had been shown as current."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "function resetRunState()" in source
    # Every section the previous run populated is cleared.
    for section in ("sec_deep", "sec_compare", "sec_detail"):
        assert section in source.split("function resetRunState()")[1][:400], section
    # And it runs on a new discovery, before the new run is adopted.
    discover = source.split('$("go_discover").onclick')[1]
    assert discover.index("resetRunState()") < discover.index("state.runId = out.research_run_id")


def test_the_selected_candidate_is_tracked_so_state_cannot_drift():
    source = APP_JS.read_text(encoding="utf-8")
    assert "state.selectedId = candidateId" in source
    # renderDetail refuses a candidate the current run does not know.
    assert "if (!c) return;" in source


def test_run_state_is_persisted_and_stale_state_is_cleared():
    source = APP_JS.read_text(encoding="utf-8")
    assert "localStorage" in source
    assert "pi.workflow.v1" in source
    # A saved run that no longer exists on the server is forgotten, not shown.
    assert "function forget()" in source
    assert "forget();" in source
    assert "no longer on the server" in source
    # No account, no server session, no database was introduced for this.
    for forbidden in ("cookie", "sessionStorage", "/login", "auth"):
        assert forbidden not in source.lower(), forbidden


def test_persistence_failure_never_breaks_the_page():
    """Private mode and quota limits make localStorage throw."""
    source = APP_JS.read_text(encoding="utf-8")
    remember = source.split("function remember()")[1].split("function recall()")[0]
    assert "try {" in remember and "catch" in remember
    recall = source.split("function recall()")[1].split("function forget()")[0]
    assert "try {" in recall and "catch" in recall


def test_the_page_requests_no_favicon():
    """DEFECT: every page load logged a 404 for /favicon.ico."""
    html = INDEX.read_text(encoding="utf-8")
    assert 'rel="icon" href="data:,"' in html


def test_loading_and_error_states_exist_for_every_long_action():
    source = APP_JS.read_text(encoding="utf-8")
    assert "function busy(" in source and "function fail(" in source
    # Anchored on each handler's own opening, not on the first mention of its
    # name: slicing from the name catches a declaration elsewhere in the file.
    handlers = {
        "discover": '$("go_discover").onclick = async () => {',
        "preliminary": '$("go_prelim").onclick = async () => {',
        "deep research": "async function runDeep() {",
        "product": "async function runSpec(",
        "content": "async function runContent(",
    }
    for name, anchor in handlers.items():
        assert source.count(anchor) == 1, name
        body = source.split(anchor)[1][:1600]
        assert "busy(" in body, f"{name} shows no progress"
        assert "fail(" in body, f"{name} does not handle failure"
    # A closed gate is explained as a gate, not as a verdict.
    assert "closed gate, not a verdict" in source


def test_capability_outcomes_are_rendered_wherever_they_are_returned():
    source = APP_JS.read_text(encoding="utf-8")
    assert "function renderCapabilities(" in source
    # Both the cheap pass and the deep pass surface them.
    assert source.count("renderCapabilities(") >= 3


def test_the_comparison_view_shows_every_required_column():
    source = APP_JS.read_text(encoding="utf-8")
    compare = source.split("function renderCompare()")[1]
    for column in (
        "opportunity_score", "evidence_confidence", "classification",
        "pos_search_demand", "pos_purchase_proxy", "pos_audience_attention",
        "excluded", "price_context", "channel_context",
    ):
        assert column in compare, column
    # Nulls are rendered as absences, and the proxy keeps its label.
    assert "not measured" in compare
    assert "proxy" in compare and "attention" in compare
