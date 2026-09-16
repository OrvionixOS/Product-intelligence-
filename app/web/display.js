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

  const api = { esc, measurement, colourPill, subScoreLabel, dimensionRole };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Display = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
