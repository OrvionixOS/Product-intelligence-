/* Product Intelligence — minimum useful workflow interface.
 *
 * Three display rules carry this file, because the whole system exists to
 * keep them:
 *
 *   1. An absent measurement is never rendered as a number. No score, no
 *      confidence and no colour is shown as text saying so, never as 0 and
 *      never as RED.
 *   2. A purchase proxy is labelled a proxy everywhere it appears.
 *   3. Missing-data reasons and limitations are shown, not hidden behind a
 *      tooltip, because they are the reason a number can be trusted or not.
 */
const $ = (id) => document.getElementById(id);
const state = {
  runId: null, seed: null, candidates: [], workflow: {},
  selectedId: null, capabilities: [], stage: null, deep: null,
};

/* A new run invalidates everything the previous one produced. Leaving the old
 * scores on screen let a user act on a candidate from a different run: the
 * server correctly refused, but only after showing stale results as current. */
function resetRunState() {
  state.workflow = {};
  state.selectedId = null;
  state.capabilities = [];
  state.deep = null;
  for (const id of ["sec_deep", "sec_compare", "sec_detail"]) {
    const el = $(id);
    if (el) { el.hidden = true; el.innerHTML = ""; }
  }
}

const {
  esc, measurement, colourPill, subScoreLabel, dimensionRole,
  formatApiError, capabilityNote, comparisonRow, sortRows,
  stageIndex, STAGES, STAGE_LABELS,
} = Display;

/* Lightweight client-side persistence. Only two ids, only in this browser:
 * no account, no server session, no database. On reload the run is restored
 * if it still exists, and cleared if it does not -- a stale id must never be
 * presented as a live one. */
const STORE_KEY = "pi.workflow.v1";

const PROVIDER_NAMES = {
  p_search: ["dataforseo"],
  p_market: ["etsy"],
  p_content: ["youtube"],
};

function remember() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      runId: state.runId, candidateId: state.selectedId, seed: state.seed,
    }));
  } catch (e) { /* private mode, quota: the app works without it */ }
}

function recall() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); }
  catch (e) { return null; }
}

function forget() {
  try { localStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ }
}

async function api(path, body) {
  const res = await fetch(path, body === undefined
    ? {}
    : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const d = data && data.detail;
    const err = new Error(formatApiError(d, res.statusText));
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return data;
}

async function loadProviders() {
  const schema = await api("/openapi.json");
  // The registries are not exposed, so offer the names the API documents.
  // The adapters this build ships. The registry is server-side and not
  // exposed, so these are named here; a provider that cannot be constructed
  // (missing credentials) is not hidden from the list -- it reports itself
  // through the capability outcomes after a run, which is where the reason
  // actually is.
  const known = PROVIDER_NAMES;
  for (const [id, names] of Object.entries(known)) {
    for (const n of names) {
      const o = document.createElement("option"); o.value = n; o.textContent = n;
      $(id).appendChild(o);
    }
  }
  return schema;
}

function setStage(stage) {
  state.stage = stage;
  const bar = $("stagebar");
  if (!bar) return;
  const at = stageIndex(stage);
  bar.innerHTML = STAGES.map((s, i) => {
    const cls = i < at ? "done" : i === at ? "now" : "todo";
    return `<span class="stg ${cls}">${esc(STAGE_LABELS[s])}</span>`;
  }).join('<span class="stgsep">›</span>');
}

function busy(id, message) {
  const el = $(id);
  if (el) el.innerHTML = `<span class="spin"></span> ${esc(message)}`;
}

function fail(id, error) {
  const el = $(id);
  if (!el) return;
  const extra = error.status === 409
    ? ' <span class="mut">This is a closed gate, not a verdict about the candidate.</span>'
    : "";
  el.innerHTML = `<span class="err">${esc(error.message)}</span>${extra}`;
}

/* Capability outcomes are the single most common reason a whole dimension is
 * MISSING -- above all when a provider has no credentials -- and they are
 * invisible unless said out loud. */
function renderCapabilities(capabilities) {
  if (!capabilities || !capabilities.length) return "";
  return `<details ${capabilities.some((c) => c.status !== "COMPLETE") ? "open" : ""}>
    <summary>Evidence capabilities — why evidence is present or missing</summary>
    ${capabilities.map((c) => `<div class="note">
      <b>${esc(c.capability)}</b> · ${esc(c.status)}
      <div class="mut">${esc(capabilityNote(c))}</div>
    </div>`).join("")}</details>`;
}

$("go_discover").onclick = async () => {
  busy("msg_discover", "Generating candidates…");
  try {
    const out = await api("/candidates/discover", { seed_keyword: $("seed").value });
    // A new run invalidates the previous one entirely.
    resetRunState();
    state.runId = out.research_run_id || null;
    state.seed = $("seed").value;
    state.candidates = out.candidates || [];
    remember();
    setStage("DISCOVERED");
    $("msg_discover").innerHTML =
      `<span class="ok">${state.candidates.length} candidates generated.</span> ` +
      `<span class="mono mut">run ${esc(state.runId)}</span>`;
    $("go_prelim").disabled = !state.runId;
    renderCandidates();
  } catch (e) { fail("msg_discover", e); }
};

$("go_prelim").onclick = async () => {
  busy("msg_discover", "Running the cheap research pass…");
  try {
    const prelim = await api("/research/preliminary", {
      research_run_id: state.runId,
      search_demand_provider: $("p_search").value || null,
      marketplace: $("p_market").value || null,
      public_content_provider: $("p_content").value || null,
    });
    state.capabilities = prelim.capabilities || [];
    setStage("RESEARCHED");
    $("msg_discover").innerHTML =
      `<span class="ok">Preliminary pass complete.</span> ` +
      `<span class="mut">Select candidates for deep research below. ` +
      `Preliminary rank is triage only and never reaches the score.</span>` +
      renderCapabilities(state.capabilities);
    renderCandidates();
  } catch (e) { fail("msg_discover", e); }
};

function renderCandidates() {
  const sec = $("sec_candidates");
  if (!state.candidates.length) { sec.hidden = true; return; }
  sec.hidden = false;
  sec.innerHTML = `
    <h2>Step 6 — select candidates for deep research</h2>
    <div class="card">
      <div class="note">Selection is explicit. Preliminary triage never crosses into scoring.</div>
      <div class="scroll"><table>
        <tr><th></th><th>Candidate</th><th>Format</th><th>Buyer</th></tr>
        ${state.candidates.map((c, i) => `
          <tr>
            <td><input type="checkbox" class="pick" data-id="${esc(c.id)}" ${i < 5 ? "checked" : ""}
                 style="width:auto"></td>
            <td><b>${esc(c.title)}</b><div class="mut">${esc(c.problem)}</div></td>
            <td class="mut">${esc(c.proposed_format)}</td>
            <td class="mut">${esc(c.target_buyer)}</td>
          </tr>`).join("")}
      </table></div>
      <div class="row" style="margin-top:10px">
        <button id="go_deep">Run deep research + scoring</button>
        <span id="msg_deep" class="mut"></span>
      </div>
    </div>`;
  $("go_deep").onclick = runDeep;
}

async function runDeep() {
  const ids = [...document.querySelectorAll(".pick:checked")].map((e) => e.dataset.id);
  if (!ids.length) { $("msg_deep").innerHTML = `<span class="err">Select at least one.</span>`; return; }
  busy("msg_deep", "Steps 7–9: deep collection, dossier, confidence, score…");
  try {
    const out = await api("/workflow/deep-research", {
      research_run_id: state.runId,
      candidate_ids: ids,
      search_demand_provider: $("p_search").value || null,
      marketplace: $("p_market").value || null,
      public_content_provider: $("p_content").value || null,
    });
    state.workflow = {};
    for (const c of out.candidates) state.workflow[c.candidate_id] = c;
    state.deep = out;
    state.capabilities = out.capabilities || [];
    setStage("SCORED");
    remember();
    $("msg_deep").innerHTML = `<span class="ok">Done.</span>`;
    renderDeep(out);
    renderCompare();
  } catch (e) { fail("msg_deep", e); }
}

function renderDeep(out) {
  const sec = $("sec_deep");
  sec.hidden = false;
  const titleOf = (id) => (state.candidates.find((c) => c.id === id) || {}).title || id;
  sec.innerHTML = `
    <h2>Steps 8–9 — Opportunity Score and classification</h2>
    <div class="card">
      <div class="mut mono">deep pass ${esc(out.deep_pass_id)} ·
        cache hits ${out.true_cache_hits} · depth misses ${out.depth_misses}</div>
      <div class="grid" style="margin-top:10px">
        ${out.candidates.map((c) => {
          const s = c.scoring;
          return `
          <div class="card" style="margin:0">
            <b>${esc(titleOf(c.candidate_id))}</b>
            <div style="margin:8px 0 4px">${measurement(s.opportunity_score, " / 100")}</div>
            <div class="mut" style="font-size:12px">Opportunity Score</div>
            <div style="margin:8px 0 4px">${measurement(s.evidence_confidence, " / 100")}</div>
            <div class="mut" style="font-size:12px">Evidence Confidence</div>
            <div style="margin:10px 0">${colourPill(s.classification, s.scoring_state)}</div>
            <div class="note">${esc(s.state_boundary)}</div>
            ${c.unscoreable_reason
              ? `<div class="note err">${esc(c.unscoreable_reason.replace(/_/g, " "))}</div>` : ""}
            <button class="ghost" data-open="${esc(c.candidate_id)}"
              style="margin-top:8px">Open evidence</button>
          </div>`;
        }).join("")}
      </div>
      ${renderCapabilities(out.capabilities)}
      <details><summary>Component versions</summary>
        <div class="mono mut">${Object.entries(out.component_versions)
          .map(([k, v]) => `${esc(k)} = ${esc(v)}`).join("<br>")}</div></details>
    </div>`;
  sec.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => renderDetail(b.dataset.open)));
}

function dimRow(d) {
  const feats = Object.entries(d.observed_features || {});
  return `<tr>
    <td><b>${esc(d.dimension_name)}</b>
      <div class="mut" style="font-size:12px">
        ${esc(dimensionRole(d))}${d.required ? " · required" : " · optional"}</div></td>
    <td>${esc(d.state)}${d.missing_reason
      ? `<div class="mut">${esc(d.missing_reason)}</div>` : ""}</td>
    <td>${d.sample_size === null ? `<span class="mut">not reported</span>` : esc(d.sample_size)}</td>
    <td>${esc(d.conflict_count)}</td>
    <td class="mono">${feats.length
      ? feats.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")
      : `<span class="mut">none</span>`}</td></tr>`;
}

function renderDetail(candidateId) {
  const c = state.workflow[candidateId];
  if (!c) return;
  state.selectedId = candidateId;
  remember();
  const s = c.scoring;
  const sec = $("sec_detail");
  sec.hidden = false;
  sec.innerHTML = `
    <h2>Evidence — ${esc((state.candidates.find((x) => x.id === candidateId) || {}).title || candidateId)}</h2>
    <div class="card">
      <h3 style="font-size:14px;margin:0 0 6px">Score inputs</h3>
      <div class="scroll"><table>
        <tr><th>Sub-score</th><th>State</th><th>Value</th><th>Observed inputs</th></tr>
        ${s.sub_scores.map((x) => `<tr>
          <td><b>${esc(x.name)}</b>${subScoreLabel(x.name)
            ? `<div class="mut" style="font-size:12px">${esc(subScoreLabel(x.name))}</div>` : ""}</td>
          <td>${esc(x.state)}${x.blocked_reason
            ? `<div class="mut">${esc(x.blocked_reason)}</div>` : ""}</td>
          <td>${x.value === null ? `<span class="mut">blocked</span>` : esc(x.value)}</td>
          <td class="mono">${(x.inputs || []).map((i) =>
            `${esc(i.statistic)}: ${esc(i.observed)} → ${esc(Number(i.normalized).toFixed(2))}`).join("<br>")
            || `<span class="mut">none</span>`}</td></tr>`).join("")}
      </table></div>
      ${s.excluded_dimensions.length ? `<div class="note">Excluded: ${s.excluded_dimensions
        .map((e) => `${esc(e.dimension)} (${esc(e.reason)})`).join(", ")}</div>` : ""}

      <h3 style="font-size:14px;margin:18px 0 6px">Step 7 dimensions</h3>
      <div class="scroll"><table>
        <tr><th>Dimension</th><th>State</th><th>Sample</th><th>Conflicts</th><th>Observed</th></tr>
        ${c.dimensions.map(dimRow).join("")}
      </table></div>

      <h3 style="font-size:14px;margin:18px 0 6px">Context — beside the score, never inside it</h3>
      ${c.context.map((x) => `<div class="note"><b>${esc(x.dimension_name)}</b> · ${esc(x.state)}
        <div class="mono">${Object.entries(x.observed_features || {})
          .map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>") || "none"}</div></div>`).join("")}

      <details open><summary>Limitations</summary>
        <ul class="mut">${(s.limitations || []).concat(c.limitations || [])
          .map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>

      <div class="row" style="margin-top:12px">
        <button id="go_spec" ${c.may_generate_product ? "" : "disabled"}>Generate product (step 10)</button>
        <button id="go_content" class="ghost" ${c.may_generate_product ? "" : "disabled"}>
          Content experiments (steps 12–13)</button>
        ${c.may_generate_product ? "" :
          `<span class="mut">Product generation needs a score. This candidate is unmeasured, not rejected.</span>`}
      </div>
      <div id="msg_post" class="mut" style="margin-top:8px"></div>
      <div id="out_post"></div>
    </div>`;
  $("go_spec").onclick = () => runSpec(candidateId);
  $("go_content").onclick = () => runContent(candidateId);
  sec.scrollIntoView({ behavior: "smooth" });
}

async function runSpec(candidateId) {
  busy("msg_post", "Generating specification…");
  try {
    const out = await api("/product/specification",
      { research_run_id: state.runId, candidate_id: candidateId });
    const spec = out.specification, fit = out.product_job_fit;
    setStage("PRODUCT");
    $("msg_post").innerHTML = `<span class="ok">Specification generated.</span>`;
    $("out_post").innerHTML = `
      <div class="card">
        <b>${esc(spec.title ? spec.title.value : "Specification")}</b>
        <div class="mut">state ${esc(spec.state)}</div>
        <div class="note">${esc(out.scoring_boundary)}</div>
        <details open><summary>Product/job fit (step 10, after generation)</summary>
          <div>pattern <b>${esc(fit.pattern)}</b></div>
          <div class="note">${esc(fit.pattern_boundary)}</div></details>
      </div>`;
  } catch (e) { fail("msg_post", e); }
}

async function runContent(candidateId) {
  busy("msg_post", "Deriving content experiments…");
  try {
    const out = await api("/workflow/content-plan",
      { research_run_id: state.runId, candidate_id: candidateId });
    setStage("CONTENT");
    $("msg_post").innerHTML = `<span class="ok">${out.experiments.length} experiments.</span>`;
    $("out_post").innerHTML = `
      <div class="card">
        <div class="mut">outliers ${esc(out.outlier_state)} · patterns ${esc(out.patterns_state)}
          · experiments ${esc(out.experiments_state)}</div>
        ${out.experiments.map((e) => `
          <div class="note"><b>${esc(e.title)}</b>
            <div>${esc(e.hypothesis)}</div>
            <div class="mono mut">variable ${esc(e.variable_family)} / ${esc(e.variable_kind)}
              = ${esc(e.variable_value)}</div>
            <div class="mut">measure: ${esc(e.primary_measurement)}</div>
            <div class="mut">success: ${esc(e.success_criterion)}</div></div>`).join("")}
        <details open><summary>Limitations</summary>
          <ul class="mut">${(out.limitations || []).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
        </details>
      </div>`;
  } catch (e) { fail("msg_post", e); }
}

/* ---------------------------------------------- candidate comparison ---- */

let compareSort = { field: "opportunity_score", desc: true };

function renderCompare() {
  const sec = $("sec_compare");
  const entries = Object.values(state.workflow);
  if (!entries.length) { sec.hidden = true; return; }
  sec.hidden = false;
  const titleOf = (id) => (state.candidates.find((c) => c.id === id) || {}).title || id;
  const rows = sortRows(
    entries.map((c) => comparisonRow(c, titleOf(c.candidate_id))),
    compareSort.field,
    compareSort.desc
  );
  const cell = (v) => (v === null || v === undefined
    ? `<span class="mut">not measured</span>` : esc(v));
  const sortable = (field, label) =>
    `<th><button class="sortbtn" data-sort="${field}">${esc(label)}${
      compareSort.field === field ? (compareSort.desc ? " ↓" : " ↑") : ""}</button></th>`;
  sec.innerHTML = `
    <h2>Compare candidates in this run</h2>
    <div class="card">
      <div class="note">Sorting is over one already-approved field. There is no combined
        ranking here and no recommendation: a candidate with no measurement sorts last in
        both directions, because an absent score is not a low one.</div>
      <div class="scroll"><table>
        <tr><th>Candidate</th><th>State</th>
          ${sortable("opportunity_score", "POS")}
          ${sortable("evidence_confidence", "ECS")}
          <th>Colour</th>
          ${sortable("pos_search_demand", "Search")}
          ${sortable("pos_purchase_proxy", "Purchase proxy")}
          ${sortable("pos_audience_attention", "Attention")}
          <th>Missing</th><th>Price context</th><th>Channel context</th><th></th></tr>
        ${rows.map((r) => `<tr>
          <td><b>${esc(r.title)}</b></td>
          <td>${esc(r.scoring_state)}</td>
          <td>${cell(r.opportunity_score)}</td>
          <td>${cell(r.evidence_confidence)}</td>
          <td>${colourPill(r.classification, r.scoring_state)}</td>
          <td>${cell(r.pos_search_demand)}</td>
          <td>${cell(r.pos_purchase_proxy)}<div class="mut" style="font-size:11px">proxy</div></td>
          <td>${cell(r.pos_audience_attention)}<div class="mut" style="font-size:11px">attention</div></td>
          <td class="mut">${r.excluded.length ? esc(r.excluded.join(", ")) : "none"}</td>
          <td class="mut">${r.price_context ? esc(r.price_context.state) : "—"}</td>
          <td class="mut">${r.channel_context ? esc(r.channel_context.state) : "—"}</td>
          <td><button class="ghost" data-open="${esc(r.candidate_id)}">Open</button></td>
        </tr>`).join("")}
      </table></div>
    </div>`;
  sec.querySelectorAll("[data-sort]").forEach((b) => (b.onclick = () => {
    const f = b.dataset.sort;
    compareSort = { field: f, desc: compareSort.field === f ? !compareSort.desc : true };
    renderCompare();
  }));
  sec.querySelectorAll("[data-open]").forEach((b) =>
    (b.onclick = () => renderDetail(b.dataset.open)));
}

/* ------------------------------------------------------------ restore ---- */

async function restore() {
  const saved = recall();
  if (!saved || !saved.runId) return;
  try {
    // Ask the server whether this run still exists, using a candidate-state
    // probe: a stale id must never be presented as a live one.
    const probe = await fetch(
      `/workflow/runs/${saved.runId}/candidates/${saved.candidateId || saved.runId}`
    );
    if (!probe.ok) throw new Error("gone");
    $("seed").value = saved.seed || $("seed").value;
    $("restored").innerHTML =
      `<span class="mut">Restored run <span class="mono">${esc(saved.runId)}</span>. ` +
      `Candidate evidence is not cached in this browser, so re-run deep research to see scores again.</span>`;
    state.runId = saved.runId;
    state.seed = saved.seed || null;
    setStage("DISCOVERED");
  } catch (e) {
    // The run is gone (the store is in-memory and the server restarted).
    forget();
    $("restored").innerHTML =
      `<span class="mut">A previously saved run is no longer on the server, so it was cleared.</span>`;
  }
}

loadProviders().catch(() => {});
restore();
