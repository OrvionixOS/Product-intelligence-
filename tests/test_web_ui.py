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
