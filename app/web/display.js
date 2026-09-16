/* Pure display rules, kept separate from the DOM so they can be executed by a
 * test rather than string-matched.
 *
 * These three functions are where the system's honesty reaches a screen. Every
 * upstream milestone works to keep a missing measurement missing; if this file
 * renders `null` as 0, or an absent colour as RED, all of that is undone at the
 * last step. So they live here, take plain values, and return plain strings.
 */
(function (root) {
  "use strict";

  const esc = (s) =>
    String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* An absent measurement says so. It is never 0, never blank, never a dash
   * that could read as a value. */
  function measurement(value, suffix) {
    if (value === null || value === undefined) {
      return '<span class="absent">not measured</span>';
    }
    return (
      '<span class="num">' + esc(value) + "</span>" +
      '<span class="mut">' + esc(suffix || "") + "</span>"
    );
  }

  /* An absent classification is NOT RED. It is the absence of a colour, and
   * the reason it is absent is shown beside it. */
  function colourPill(classification, scoringState) {
    if (classification) {
      return '<span class="pill ' + esc(classification) + '">' + esc(classification) + "</span>";
    }
    const why =
      scoringState === "SCORED_UNCLASSIFIED"
        ? "no colour — evidence below the confidence floor"
        : "no colour — no score exists";
    return '<span class="pill NONE">NONE</span> <span class="mut">' + esc(why) + "</span>";
  }

  /* A sub-score keeps its label wherever it is shown. */
  function subScoreLabel(name) {
    if (name === "pos_purchase_proxy") return "PURCHASE PROXY — never sales or revenue";
    if (name === "pos_audience_attention") return "ATTENTION — never buyer demand";
    if (name === "pos_search_demand") return "observed search interest";
    return "";
  }

  /* Whether a dimension may carry a score magnitude, stated on every row. */
  function dimensionRole(dimension) {
    return dimension.pos_eligible
      ? "feeds the score"
      : "contextual only — never in a score";
  }

  /* A FastAPI error `detail` arrives in three shapes: a plain string, the
   * {reason, message} object the workflow gates raise, or the LIST of
   * per-field objects a 422 validation failure produces. Stringifying the
   * list puts raw JSON in front of a person, so each shape is handled. */
  function formatApiError(detail, fallback) {
    if (detail === null || detail === undefined || detail === "") {
      return fallback || "Request failed.";
    }
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const where = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
          return where ? where + ": " + (d.msg || "invalid") : d.msg || "invalid";
        })
        .join("; ");
    }
    if (detail.message) return detail.message;
    return fallback || "Request failed.";
  }

  /* The pipeline a candidate moves through, named once. */
  const STAGES = ["DISCOVERED", "RESEARCHED", "SCORED", "PRODUCT", "CONTENT"];

  const STAGE_LABELS = {
    DISCOVERED: "Candidates",
    RESEARCHED: "Cheap research",
    SCORED: "Deep research + score",
    PRODUCT: "Product",
    CONTENT: "Content",
  };

  function stageIndex(stage) {
    const i = STAGES.indexOf(stage);
    return i < 0 ? -1 : i;
  }

  /* Why a capability produced what it produced. A capability that could not
   * be constructed is the single most common reason a whole dimension is
   * MISSING, and it is invisible unless it is said out loud. */
  function capabilityNote(outcome) {
    const status = outcome.status;
    if (status === "NOT_REQUESTED") {
      return "not requested — no provider was selected, so its dimensions are MISSING rather than zero";
    }
    if (status === "PROVIDER_FAILED") {
      return (
        "provider unavailable — " +
        (outcome.failure_reason || "it could not be reached") +
        ". Its dimensions stay MISSING; nothing was assumed in their place"
      );
    }
    if (status === "UNEXPECTED_PROVIDER_ERROR") {
      return "the provider failed in a way outside its contract; its evidence is MISSING";
    }
    if (status === "PARTIAL") return "ran, and some queries did not return";
    if (status === "COMPLETE") return "ran and returned";
    return status;
  }

  /* One comparison row. Nulls are preserved as nulls: the caller decides how
   * to render an absence, and this never substitutes a number for one. */
  function comparisonRow(candidate, title) {
    const s = candidate.scoring;
    const byName = {};
    for (const sub of s.sub_scores || []) byName[sub.name] = sub.value;
    const context = {};
    for (const c of candidate.context || []) context[c.dimension_name] = c;
    return {
      candidate_id: candidate.candidate_id,
      title: title || candidate.candidate_id,
      scoring_state: s.scoring_state,
      opportunity_score: s.opportunity_score,
      evidence_confidence: s.evidence_confidence,
      classification: s.classification,
      pos_search_demand: byName.pos_search_demand === undefined ? null : byName.pos_search_demand,
      pos_purchase_proxy: byName.pos_purchase_proxy === undefined ? null : byName.pos_purchase_proxy,
      pos_audience_attention:
        byName.pos_audience_attention === undefined ? null : byName.pos_audience_attention,
      excluded: (s.excluded_dimensions || []).map((e) => e.dimension),
      price_context: context.deep_price_evidence || null,
      channel_context: context.deep_channel_reach || null,
    };
  }

  /* Sorting is over an already-approved field only. There is no new composite
   * and no recommendation: a null sorts last in both directions because an
   * absent measurement is not a low one. */
  function sortRows(rows, field, descending) {
    const copy = rows.slice();
    copy.sort((a, b) => {
      const x = a[field];
      const y = b[field];
      const xMissing = x === null || x === undefined;
      const yMissing = y === null || y === undefined;
      if (xMissing && yMissing) return String(a.title).localeCompare(String(b.title));
      if (xMissing) return 1;
      if (yMissing) return -1;
      if (x === y) return String(a.title).localeCompare(String(b.title));
      return descending ? (y > x ? 1 : -1) : x > y ? 1 : -1;
    });
    return copy;
  }

  const api = {
    esc,
    measurement,
    colourPill,
    subScoreLabel,
    dimensionRole,
    formatApiError,
    capabilityNote,
    comparisonRow,
    sortRows,
    stageIndex,
    STAGES,
    STAGE_LABELS,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Display = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
