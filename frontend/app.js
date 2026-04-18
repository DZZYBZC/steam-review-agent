// Steam Review Triage — frontend (replay only, task 4).
// Consumes the canonical SSE schema from /replays/{run_id}/stream.

const $ = (id) => document.getElementById(id);

// Minimal state reducer.
let state = emptyState();
let eventSource = null;
let eventQueue = [];        // events buffered, awaiting apply
let appliedEvents = [];     // events already applied, for undo
let streamEnded = false;    // server closed SSE
let autoPlay = false;       // auto-drain the queue when true
let autoPlayTimer = null;
let currentView = "demo";       // "demo" | "evals" | "notes"
let currentMode = "featured";   // "featured" | "live" (only meaningful in Demo view)
let selectedLiveCandidate = null; // candidate picked but not yet run (Try Live only)
let evalsLoaded = false;
let notesLoaded = false;
let memoryData = null;
// Tracks the most-recent-completed live demo run that hasn't been promoted yet.
// Populated on run_complete for live cases; cleared on successful promote or dismiss.
let pendingPromotion = null;  // { run_id, app_id, review_id, category, final_action, label }
const caseCache = new Map();  // run_id -> snapshot of {state, eventQueue, appliedEvents, streamEnded}
const liveRunByReview = new Map();  // review_id -> run_id (so live candidates resume cached runs on re-click)
const watchedCases = new Set();  // run_ids of cases fully played through to run_complete
let liveCandidates = [];        // cache of last /reviews/candidates response

function emptyState() {
  return {
    run_id: null,
    case: null,
    meta: null,
    iterations: new Map(),   // iteration_number -> { nodes: [], state: {} }
    retrievalSteps: [],      // [{stage, label, description, done}]
    gate: null,
    outcome: null,
  };
}

function resetMutableState() {
  // Clear everything except the selected-case identity fields.
  const { meta, case: c, run_id } = state;
  state = emptyState();
  state.meta = meta;
  state.case = c;
  state.run_id = run_id;
}

function currentIterEntry(iter) {
  if (!state.iterations.has(iter)) {
    state.iterations.set(iter, { iteration: iter, nodes: [], state: {} });
  }
  return state.iterations.get(iter);
}

// Maps each event type to the panel ids it affects, ordered top-to-bottom
// for scroll-into-view to pick the topmost.
const PANEL_ORDER = [
  "input-panel",
  "timeline-panel",
  "retrieval-panel",
  "why-panel",
  "draft-panel",
  "evidence-panel",
  "critique-panel",
  "gate-panel",
  "outcome-panel",
];

function panelsAffectedByEvent(evt) {
  const { type, payload } = evt;
  // Keep highlights narrow — only the primary panel(s) where the change is visibly meaningful.
  if (type === "node_start") return ["timeline-panel"];
  if (type === "state_update") {
    const node = payload?.node;
    if (node === "investigator") return ["evidence-panel"];            // evidence summary appears (from investigator)
    if (node === "responder")    return ["why-panel", "draft-panel", "evidence-panel"]; // proposed_action appears; draft + citations
    if (node === "critic")       return ["critique-panel", "why-panel"]; // critic verdict + audit trail grow
    return [];
  }
  if (type === "retrieval_step")   return ["retrieval-panel"];
  if (type === "human_gate_open")  return ["gate-panel"];
  if (type === "run_complete")     return ["outcome-panel"];
  if (type === "error")            return ["outcome-panel"];
  return [];
}

function highlightChanges(evt, { scroll = true } = {}) {
  document.querySelectorAll(".panel.just-changed").forEach((p) => p.classList.remove("just-changed"));
  if (!evt) return;
  const ids = panelsAffectedByEvent(evt);
  const panels = ids.map((id) => $(id)).filter(Boolean);
  panels.forEach((p) => p.classList.add("just-changed"));
  if (scroll && panels.length > 0) {
    const topmost = PANEL_ORDER
      .map((id) => panels.find((p) => p.id === id))
      .find(Boolean);
    // `block: "nearest"` only scrolls if the panel isn't already in view —
    // avoids aggressive jump-to-panel on every streamed event. Previous value
    // `"start"` forced the canvas past the Input panel on the first event of
    // every new case, which looked like "new case inherits prior scroll."
    if (topmost) topmost.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function clearHighlights() {
  document.querySelectorAll(".panel.just-changed").forEach((p) => p.classList.remove("just-changed"));
}

function applyEvent(evt) {
  const { type, iteration, payload } = evt;
  if (type === "node_start") {
    const e = currentIterEntry(iteration);
    // Any prior active node in this iteration is finished once the next starts.
    // Handles nodes like coordinator that never emit a state_update.
    for (const prev of e.nodes) {
      if (prev.status === "active") prev.status = "done";
    }
    if (!e.nodes.find((n) => n.node === payload.node)) {
      e.nodes.push({ node: payload.node, status: "active" });
    }
  } else if (type === "state_update") {
    const e = currentIterEntry(iteration);
    const n = e.nodes.find((n) => n.node === payload.node);
    if (n) n.status = "done";
    Object.assign(e.state, payload.updates);
    // Mirror review_tone (set by investigator) into meta so the Input panel's tone chip updates live.
    if (payload.updates?.review_tone && state.meta) {
      state.meta.review_tone = payload.updates.review_tone;
    }
  } else if (type === "retrieval_step") {
    // Accumulate; mark prior step done when a new one arrives.
    if (state.retrievalSteps.length > 0) {
      state.retrievalSteps[state.retrievalSteps.length - 1].done = true;
    }
    state.retrievalSteps.push({ ...payload, done: false });
  } else if (type === "human_gate_open") {
    state.gate = payload;
    // All retrieval steps complete by the time gate opens.
    for (const s of state.retrievalSteps) s.done = true;
    // Live runs: pause autoplay at the gate so human can interact with the buttons.
    if (autoPlay && state.case?.live === true) stopAutoPlay();
  } else if (type === "run_complete") {
    state.outcome = payload;
    for (const s of state.retrievalSteps) s.done = true;
    // Track completed live runs as promotion candidates.
    if (state.case?.live === true && state.run_id) {
      pendingPromotion = {
        run_id: state.run_id,
        app_id: state.meta?.app_id,
        review_id: state.meta?.review_id,
        category: state.meta?.category,
        final_action: payload.final_action,
        label: state.case?.label || "Live run",
      };
    }
  } else if (type === "error") {
    state.outcome = { ...payload, stop_reason: "error" };
  }
  render();
}

// ============== Rendering ==============

function render() {
  // A "case" is active once the user has selected anything (live candidate
  // staged OR featured replay running OR live run in progress). Using
  // `state.case` lets the staged live-candidate preview render before the
  // user clicks ▶ Run (when state.run_id is still null).
  const hasCase = !!state.case;
  $("welcome").hidden = hasCase;
  $("run-panels").hidden = !hasCase;
  $("stepper").hidden = !hasCase;
  $("talk-track").hidden = !hasCase;
  if (!hasCase) return;
  renderInput();
  renderTimeline();
  renderRetrieval();
  renderWhy();
  renderDraft();
  renderEvidence();
  renderCritique();
  renderGate();
  renderOutcome();
}

function renderRetrieval() {
  const list = $("retrieval-steps");
  const status = $("retrieval-status");
  if (state.retrievalSteps.length === 0) {
    list.innerHTML = "<li>—</li>";
    list.classList.add("empty");
    status.textContent = "";
    return;
  }
  list.classList.remove("empty");
  list.innerHTML = "";
  for (const s of state.retrievalSteps) {
    const li = document.createElement("li");
    if (s.done) li.classList.add("done");
    const hasDetails = s.details && Object.keys(s.details).length > 0;
    li.innerHTML = `
      <button class="stage-toggle" aria-expanded="false"${hasDetails ? "" : " disabled"}>
        <span class="stage-arrow">${hasDetails ? "▸" : " "}</span>
        <span class="stage-label">${escapeHtml(s.label)}</span>
        <span class="stage-desc">${escapeHtml(s.description || "")}</span>
      </button>
      <div class="stage-body" hidden></div>
    `;
    if (hasDetails) {
      const btn = li.querySelector(".stage-toggle");
      const body = li.querySelector(".stage-body");
      btn.addEventListener("click", () => {
        const open = btn.getAttribute("aria-expanded") === "true";
        btn.setAttribute("aria-expanded", String(!open));
        btn.querySelector(".stage-arrow").textContent = open ? "▸" : "▾";
        body.hidden = open;
        if (!open && !body.dataset.filled) {
          body.innerHTML = renderStageDetails(s.details, state.meta?.app_id);
          body.dataset.filled = "1";
        }
      });
    }
    list.appendChild(li);
  }
  const doneCount = state.retrievalSteps.filter((s) => s.done).length;
  status.textContent = `${doneCount}/${state.retrievalSteps.length} stages`;
}

function renderStageDetails(d, app_id) {
  const parts = [];
  if (d.inputs && d.inputs.length) {
    parts.push(`<div class="stage-section"><div class="stage-section-label">Inputs</div><ul>${d.inputs.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>`);
  }
  if (d.operations && d.operations.length) {
    parts.push(`<div class="stage-section"><div class="stage-section-label">Operations</div><ul>${d.operations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>`);
  }
  if (d.output) {
    parts.push(`<div class="stage-section"><div class="stage-section-label">Output</div><div>${escapeHtml(d.output)}</div></div>`);
  }
  if (typeof d.evidence_confidence === "number") {
    parts.push(`<div class="stage-section"><div class="stage-section-label">Confidence</div><div>${d.evidence_confidence}</div></div>`);
  }
  if (d.retrieval_decision) {
    parts.push(`<div class="stage-section"><div class="stage-section-label">Retrieval decision</div><div>${escapeHtml(d.retrieval_decision)}</div></div>`);
  }
  if (d.cited_chunk_ids && d.cited_chunk_ids.length) {
    const chips = d.cited_chunk_ids.map((id) => `<code class="chunk-chip">${escapeHtml(id)}</code>`).join(" ");
    parts.push(`<div class="stage-section"><div class="stage-section-label">Cited chunk ids</div><div class="chunk-chips">${chips}</div><div class="stage-hint">Click any chunk id in the Cited evidence panel below to see its text.</div></div>`);
  }
  if (d.note) {
    parts.push(`<div class="stage-note">${escapeHtml(d.note)}</div>`);
  }
  return parts.join("");
}

function renderInput() {
  const meta = state.meta;
  const tone = $("input-tone");
  if (!meta) {
    $("input-meta").textContent = "";
    tone.hidden = true;
    $("input-text").textContent = "Select a case from the sidebar.";
    $("input-text").classList.add("empty");
    return;
  }
  const gameLabel = meta.game_name || meta.app_id;
  $("input-meta").textContent = `${gameLabel} · ${meta.category}`;
  if (meta.review_tone) {
    tone.hidden = false;
    tone.className = "tone-chip tone-" + meta.review_tone.toLowerCase().replace(/[^a-z]/g, "_");
    tone.textContent = "tone: " + meta.review_tone;
  } else {
    tone.hidden = true;
  }
  $("input-text").textContent = meta.review_text || "(no text)";
  $("input-text").classList.remove("empty");
}

const NODES_ORDER = ["coordinator", "investigator", "responder", "critic"];

function renderTimeline() {
  const container = $("timeline");
  if (state.iterations.size === 0) {
    container.textContent = "—";
    container.classList.add("empty");
    $("timeline-status").textContent = "";
    return;
  }
  container.classList.remove("empty");
  container.innerHTML = "";
  const iters = [...state.iterations.values()].sort((a, b) => a.iteration - b.iteration);
  for (const it of iters) {
    const group = document.createElement("div");
    group.className = "iter-group";
    const label = document.createElement("span");
    label.className = "iter-label";
    label.textContent = `iter ${it.iteration}`;
    group.appendChild(label);
    for (const n of it.nodes) {
      const dot = document.createElement("span");
      const approved = it.state.approved;
      const isCritic = n.node === "critic";
      let cls = "node";
      if (n.status === "done") {
        if (isCritic && approved === false) cls += " rejected";
        else cls += " done";
      } else cls += " active";
      dot.className = cls;
      dot.textContent = `● ${n.node}`;
      group.appendChild(dot);
    }
    container.appendChild(group);
  }
  const gate = state.gate ? " → human gate" : "";
  const done = state.outcome ? " → complete" : "";
  $("timeline-status").textContent = `${state.iterations.size} iter${state.iterations.size > 1 ? "s" : ""}${gate}${done}`;
}

function finalIter() {
  if (state.iterations.size === 0) return null;
  const iters = [...state.iterations.values()].sort((a, b) => a.iteration - b.iteration);
  return iters[iters.length - 1];
}

function renderWhy() {
  const it = finalIter();
  const grid = $("why");
  if (!it || !it.state.proposed_action) {
    grid.innerHTML = "—";
    grid.classList.add("empty");
    return;
  }
  grid.classList.remove("empty");
  const action = it.state.proposed_action || "";
  const critStatus = it.state.approved === undefined
    ? "pending"
    : it.state.approved ? `approved (iter ${it.iteration})` : `rejected (iter ${it.iteration}, ${it.state.reason_type || "?"})`;
  const ev = [...state.iterations.values()].map((i) => i.state.evidence_package).find(Boolean) || {};
  const sources = it.state.source_ids_cited || [];
  const retrievalDecision = ev.retrieval_decision || "—";
  grid.innerHTML = `
    <dt>Proposed action</dt>
    <dd><span class="action-chip ${action}">${action.toUpperCase()}</span></dd>
    <dt>Critic status</dt>
    <dd>${critStatus}</dd>
    <dt>Evidence sources cited</dt>
    <dd>${sources.length}</dd>
    <dt>Retrieval decision</dt>
    <dd>${retrievalDecision}</dd>
    <dt>Iterations</dt>
    <dd>${state.iterations.size}</dd>
  `;
}

function renderDraft() {
  const it = finalIter();
  const body = $("draft-text");
  const iterMeta = $("draft-iter");
  if (!it || !it.state.drafted_response) {
    body.textContent = "—";
    body.classList.add("empty");
    iterMeta.textContent = "";
    return;
  }
  body.classList.remove("empty");
  body.textContent = it.state.drafted_response;
  iterMeta.textContent = `final (iter ${it.iteration})`;
}

function renderCritique() {
  const it = finalIter();
  const body = $("critique-text");
  const status = $("critique-status");
  const trail = $("critique-trail");
  if (!it || !it.state.critique) {
    body.textContent = "—";
    body.classList.add("empty");
    status.textContent = "";
    trail.hidden = true;
    return;
  }
  body.classList.remove("empty");
  body.textContent = it.state.critique;
  const approved = it.state.approved;
  const rt = it.state.reason_type || "";
  status.textContent = approved
    ? `approved (iter ${it.iteration})`
    : `rejected (iter ${it.iteration}${rt ? `, ${rt}` : ""})`;

  // Audit trail across iterations (only meaningful when there's more than one).
  if (state.iterations.size > 1) {
    trail.hidden = false;
    const body2 = $("critique-trail-body");
    body2.innerHTML = "";
    const iters = [...state.iterations.values()].sort((a, b) => a.iteration - b.iteration);
    for (const i of iters) {
      const div = document.createElement("div");
      const ap = i.state.approved;
      div.className = "trail-iter " + (ap ? "approved" : "rejected");
      const reasonType = i.state.reason_type || "";
      const act = i.state.proposed_action || "";
      div.innerHTML = `
        <div class="trail-head">iter ${i.iteration} — action=${escapeHtml(act)} — ${ap ? "approved" : "rejected" + (reasonType ? ` (${reasonType})` : "")}</div>
        <div class="trail-critique">${escapeHtml(i.state.critique || "")}</div>
      `;
      body2.appendChild(div);
    }
  } else {
    trail.hidden = true;
  }
}

const chunkTextCache = new Map();  // chunk_id -> {text, section_title, parent_context, patch_date}

async function fetchChunk(app_id, chunk_id) {
  const key = `${app_id}::${chunk_id}`;
  if (chunkTextCache.has(key)) return chunkTextCache.get(key);
  const res = await fetch(`/chunks/${app_id}/${chunk_id}`);
  if (!res.ok) {
    const err = { error: `fetch failed (${res.status})` };
    chunkTextCache.set(key, err);
    return err;
  }
  const data = await res.json();
  chunkTextCache.set(key, data);
  return data;
}

function renderEvidence() {
  const it = finalIter();
  const sources = (it && it.state.source_ids_cited) || [];
  const list = $("evidence-list");
  const ev = [...state.iterations.values()].map((i) => i.state.evidence_package).find(Boolean) || {};

  // Evidence summary is the investigator's output — show it as soon as it arrives,
  // regardless of whether the responder has cited anything yet.
  if (ev.summary) {
    $("evidence-summary-wrap").hidden = false;
    $("evidence-summary").textContent = ev.summary;
  } else {
    $("evidence-summary-wrap").hidden = true;
  }

  list.innerHTML = "";
  if (sources.length === 0) {
    list.innerHTML = "<li>—</li>";
    list.classList.add("empty");
    $("evidence-count").textContent = "0 sources";
    return;
  }
  list.classList.remove("empty");
  const app_id = state.meta?.app_id;
  for (const id of sources) {
    const li = document.createElement("li");
    li.className = "evidence-item";
    li.innerHTML = `
      <button class="chunk-toggle" aria-expanded="false">▸ ${escapeHtml(id)}</button>
      <div class="chunk-body" hidden><em>Loading…</em></div>
    `;
    const btn = li.querySelector(".chunk-toggle");
    const body = li.querySelector(".chunk-body");
    btn.addEventListener("click", async () => {
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", String(!open));
      btn.textContent = `${open ? "▸" : "▾"} ${id}`;
      body.hidden = open;
      if (open) return;
      if (!body.dataset.loaded) {
        const chunk = await fetchChunk(app_id, id);
        body.innerHTML = renderChunkBody(chunk);
        body.dataset.loaded = "1";
      }
    });
    list.appendChild(li);
  }
  $("evidence-count").textContent = `${sources.length} source${sources.length > 1 ? "s" : ""}`;
}

function renderChunkBody(chunk) {
  if (chunk.error) return `<div class="chunk-error">${escapeHtml(chunk.error)}</div>`;
  const meta = [];
  if (chunk.section_title) meta.push(`section: ${escapeHtml(chunk.section_title)}`);
  if (chunk.patch_date) meta.push(`date: ${escapeHtml(chunk.patch_date)}`);
  if (chunk.parent_id) meta.push(`parent: ${escapeHtml(chunk.parent_id)}`);
  const metaLine = meta.length ? `<div class="chunk-meta">${meta.join(" · ")}</div>` : "";
  const parentLine = chunk.parent_context
    ? `<details class="chunk-parent"><summary>+ parent context (${chunk.parent_context.length} chars)</summary><pre>${escapeHtml(chunk.parent_context)}</pre></details>`
    : "";
  return `
    ${metaLine}
    <pre class="chunk-text">${escapeHtml(chunk.text || "(empty)")}</pre>
    ${parentLine}
  `;
}

function renderGate() {
  const panel = $("gate-panel");
  const body = $("gate-body");
  const status = $("gate-status");
  if (!state.gate) {
    panel.classList.remove("active");
    body.classList.remove("open");
    body.textContent = "Not reached yet.";
    status.textContent = "";
    return;
  }
  panel.classList.add("active");
  body.classList.add("open");
  const g = state.gate;
  const freezeLine = g.action_freeze_applied
    ? `<div class="gate-fact">action-freeze: <strong>yes</strong> — frozen_action=${escapeHtml(g.frozen_action)}</div>`
    : "";

  const isLive = state.case?.live === true;
  const sent = state.humanDecisionSent;

  if (!isLive) {
    status.textContent = "open (replay)";
    body.innerHTML = `
      <div class="gate-fact">current_action: ${escapeHtml(g.current_action)}</div>
      <div class="gate-fact">iterations: ${g.iteration_count}</div>
      ${freezeLine}
    `;
    return;
  }

  if (sent) {
    status.textContent = `submitted: ${sent.decision}`;
    const overrideLine = sent.override
      ? `<div class="gate-fact">action override: <strong>${escapeHtml(sent.override)}</strong></div>`
      : "";
    const feedbackLine = sent.feedback
      ? `<div class="gate-fact">feedback: ${escapeHtml(sent.feedback)}</div>`
      : "";
    body.innerHTML = `
      <div class="gate-fact">current_action: ${escapeHtml(g.current_action)}</div>
      <div class="gate-fact">iterations: ${g.iteration_count}</div>
      ${freezeLine}
      <div class="gate-sent">Decision sent — ${escapeHtml(sent.decision)} ${overrideLine ? "(with override)" : ""}</div>
      ${overrideLine}
      ${feedbackLine}
    `;
    return;
  }

  status.textContent = "awaiting human decision";
  const actionOptions = ["no_action", "monitor", "investigate", "escalate"]
    .map((a) => `<option value="${a}"${a === g.current_action ? " disabled" : ""}>${a}</option>`)
    .join("");
  body.innerHTML = `
    <div class="gate-fact">current_action: <span class="action-chip ${escapeHtml(g.current_action)}">${escapeHtml(g.current_action.toUpperCase())}</span></div>
    <div class="gate-fact">iterations: ${g.iteration_count}</div>
    ${freezeLine}

    <div class="gate-options">

      <section class="gate-option-section">
        <h4 class="gate-option-title">Approve</h4>
        <p class="gate-option-hint">Ship the draft as-is with the responder's action (<code>${escapeHtml(g.current_action)}</code>).</p>
        <button class="gate-btn gate-approve-btn" data-act="approve">Approve</button>
      </section>

      <section class="gate-option-section">
        <h4 class="gate-option-title">Approve with different action</h4>
        <p class="gate-option-hint">Ship the draft text as-is, but swap the action label. Use this when the wording is fine but you disagree with the rubric rung.</p>
        <div class="gate-sub-inline">
          <select class="gate-override-select">
            <option value="">-- pick an override --</option>
            ${actionOptions}
          </select>
          <button class="gate-btn gate-approve-btn" data-act="approve-override">Submit override</button>
        </div>
      </section>

      <section class="gate-option-section">
        <h4 class="gate-option-title">Reject with feedback</h4>
        <p class="gate-option-hint">Send back for revision. Responder will re-draft using your feedback as the revision reason.</p>
        <textarea class="gate-reject-feedback" rows="3" placeholder="Why is this wrong? e.g. concrete hard-blocker + persistence → should be escalate"></textarea>
        <button class="gate-btn gate-reject-btn" data-act="reject">Submit rejection</button>
      </section>

    </div>
  `;

  body.querySelector("[data-act='approve']").addEventListener("click", () => submitHumanDecision("approved"));
  body.querySelector("[data-act='approve-override']").addEventListener("click", () => {
    const sel = body.querySelector(".gate-override-select");
    const override = sel.value;
    if (!override) { alert("Pick an action to override with."); return; }
    submitHumanDecision("approved", "", override);
  });
  body.querySelector("[data-act='reject']").addEventListener("click", () => {
    const fb = body.querySelector(".gate-reject-feedback").value.trim();
    if (!fb) { alert("Feedback required for rejection."); return; }
    submitHumanDecision("rejected", fb);
  });
}

async function submitHumanDecision(decision, feedback = "", override = "") {
  if (!state.run_id) return;
  const payload = {
    human_decision: decision,
    human_feedback: feedback,
    human_action_override: override,
  };
  let ok = false;
  try {
    const res = await fetch(`/runs/${state.run_id}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    ok = res.status === 202;
    if (!ok) {
      const err = await res.text();
      alert(`Resume failed (${res.status}): ${err}`);
      return;
    }
  } catch (err) {
    alert("Resume request failed — see console");
    console.error(err);
    return;
  }
  state.humanDecisionSent = { decision, feedback, override };
  render();
  // Open a NEW stream for post-gate events. Do NOT reset the event queue /
  // applied log — we want to continue appending to the existing timeline.
  closeStream();
  openStream(`/runs/${state.run_id}/stream?from=resume`, { reset: false });
}

function renderOutcome() {
  const badge = $("outcome-badge");
  const summary = $("outcome-summary");
  if (!state.outcome) {
    badge.className = "outcome-badge pending";
    badge.textContent = "IN PROGRESS";
    summary.textContent = "";
    return;
  }
  const o = state.outcome;
  let label = "COMPLETE";
  let cls = "approved";
  if (o.stop_reason === "error" || o.stop_reason === "llm_error" || o.stop_reason === "parse_error") {
    label = "ERROR"; cls = "error";
  } else if (o.stop_reason === "max_iterations_reached") {
    // Graph terminated without reaching the gate — critic kept rejecting across
    // the AGENT_MAX_ITERATIONS=3 budget. No consensus between responder and critic.
    label = "MAX ITERATIONS — no gate reached"; cls = "error";
  } else if (o.human_decision === "rejected" || o.stop_reason === "human_rejected") {
    label = "REJECTED — re-enters revision"; cls = "rejected";
  } else if (o.stop_reason === "human_approved") {
    if (state.gate?.action_freeze_applied) { label = "APPROVED (action-frozen)"; cls = "frozen"; }
    else if (o.human_action_override) { label = "APPROVED (overridden)"; cls = "overridden"; }
    else if ((o.iteration_count || 0) > 1) { label = "APPROVED (revised)"; cls = "revised"; }
    else { label = "APPROVED"; cls = "approved"; }
  } else if (o.stop_reason === "no_response_needed") {
    label = "NO_RESPONSE"; cls = "approved";
  }
  badge.className = "outcome-badge " + cls;
  badge.textContent = label;

  const parts = [
    `final_action=${o.final_action}`,
    `iterations=${o.iteration_count}`,
  ];
  if (o.total_tokens) parts.push(`${o.total_tokens.toLocaleString()} tokens`);
  if (o.is_demo_override) parts.push("demo override");
  summary.innerHTML = parts.join(" · ");

  if (o.token_usage_by_node && Object.keys(o.token_usage_by_node).length > 0) {
    const grid = renderTokenBreakdown(o.token_usage_by_node);
    summary.appendChild(grid);
  }

  if (o.human_feedback) {
    const fb = document.createElement("div");
    fb.className = "human-feedback";
    fb.innerHTML = `<strong>Human feedback:</strong> ${escapeHtml(o.human_feedback)}`;
    summary.appendChild(fb);
  }
}

function renderTokenBreakdown(byNode) {
  const totals = Object.entries(byNode).map(([node, counts]) => ({
    node,
    input: Number(counts?.input || 0),
    output: Number(counts?.output || 0),
    total: Number(counts?.input || 0) + Number(counts?.output || 0),
  }));
  const grandTotal = totals.reduce((s, r) => s + r.total, 0) || 1;
  totals.sort((a, b) => b.total - a.total);

  const wrap = document.createElement("details");
  wrap.className = "token-breakdown";
  wrap.innerHTML = `<summary>Token usage by node</summary>`;
  const grid = document.createElement("div");
  grid.className = "token-grid";
  for (const r of totals) {
    const pct = (r.total / grandTotal) * 100;
    const row = document.createElement("div");
    row.className = "token-row";
    row.innerHTML = `
      <div class="token-node">${escapeHtml(r.node)}</div>
      <div class="token-bar"><div class="token-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
      <div class="token-count">${r.total.toLocaleString()}<span class="token-split"> (${r.input.toLocaleString()} in / ${r.output.toLocaleString()} out)</span></div>
    `;
    grid.appendChild(row);
  }
  wrap.appendChild(grid);
  return wrap;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ============== Sidebar + selection ==============

async function loadCaseList() {
  const res = await fetch("/replays");
  const data = await res.json();
  const list = $("case-list");
  list.innerHTML = "";
  if (!data.cases || data.cases.length === 0) {
    list.innerHTML = `<li class="empty">No featured cases. ${data.note || ""}</li>`;
    return;
  }
  for (const c of data.cases) {
    const li = document.createElement("li");
    li.dataset.runId = c.run_id;
    li.innerHTML = `
      <div class="label">${c.label}</div>
      <div class="story">${c.story}</div>
      <span class="chip">${c.meta.category}</span>
    `;
    li.addEventListener("click", () => selectCase(c.run_id));
    list.appendChild(li);
  }
  applyWatchedMarks();
}

function applyWatchedMarks() {
  // Reapply ✓ to any list item whose run_id has been watched this session
  // (checks both data-run-id for featured and data-review-id for live).
  for (const li of document.querySelectorAll(".case-list li")) {
    const runId = li.dataset.runId;
    if (runId && watchedCases.has(runId)) {
      li.classList.add("watched");
      continue;
    }
    const reviewId = li.dataset.reviewId;
    if (reviewId) {
      const mappedRun = liveRunByReview.get(reviewId);
      if (mappedRun && watchedCases.has(mappedRun)) li.classList.add("watched");
    }
  }
}

async function loadLiveCandidates() {
  const res = await fetch("/reviews/candidates?limit=24");
  const data = await res.json();
  const recommended = data.recommended || [];
  liveCandidates = [...recommended, ...(data.candidates || [])];
  const list = $("case-list");
  list.innerHTML = "";
  if (liveCandidates.length === 0) {
    list.innerHTML = `<li class="empty">${data.note || "No reviews available."}</li>`;
    return;
  }

  if (recommended.length > 0) {
    const header = document.createElement("li");
    header.className = "sidebar-subheader";
    header.textContent = "Recommended for Demo";
    list.appendChild(header);
    for (const c of recommended) {
      list.appendChild(renderLiveCandidateLi(c, true));
    }
    const sep = document.createElement("li");
    sep.className = "sidebar-subheader";
    sep.textContent = "Anything Else";
    list.appendChild(sep);
  }

  for (const c of (data.candidates || [])) {
    list.appendChild(renderLiveCandidateLi(c, false));
  }
  applyWatchedMarks();
}

function renderLiveCandidateLi(c, isRecommended) {
  const li = document.createElement("li");
  li.dataset.reviewId = c.review_id;
  li.dataset.appId = c.app_id;
  if (isRecommended) li.classList.add("recommended");
  const hours = c.playtime_hours ? ` · ${Math.round(c.playtime_hours)}h played` : "";
  const thumb = c.voted_up ? "👍" : "👎";
  const game = c.game_name || c.app_id;
  if (isRecommended) {
    li.innerHTML = `
      <div class="label">${escapeHtml(c.label)}</div>
      <div class="story">${escapeHtml(c.story)}</div>
      <span class="chip">${escapeHtml(game)} · ${escapeHtml(c.category)}</span>
    `;
  } else {
    li.innerHTML = `
      <div class="label">${thumb} ${escapeHtml(c.category)}${hours}</div>
      <div class="story">${escapeHtml(c.preview)}</div>
      <span class="chip">${escapeHtml(game)}</span>
    `;
  }
  li.addEventListener("click", () => selectLiveCandidate(c));
  return li;
}

// Try Live: clicking a sidebar candidate now just SELECTS it (shows preview +
// talk track). The user must then click the ▶ Run button in the topbar to
// actually POST /runs and start streaming.
function selectLiveCandidate(candidate) {
  stopAutoPlay();
  closeStream();
  clearHighlights();
  snapshotCurrentCase();
  for (const li of document.querySelectorAll(".case-list li")) {
    li.classList.toggle("selected", li.dataset.reviewId === candidate.review_id);
  }

  // If we've already run this review, restore the cached run instead of staging
  // a fresh one (mirrors featured-case behavior — clicking again resumes).
  const cachedRunId = liveRunByReview.get(candidate.review_id);
  if (cachedRunId && caseCache.has(cachedRunId)) {
    selectedLiveCandidate = null;
    if (restoreCase(cachedRunId)) { scrollCanvasToTop(); return; }
  }

  // Stage the candidate: show Input + talk-track, no events yet. Run button enables.
  selectedLiveCandidate = candidate;
  state = emptyState();
  eventQueue = [];
  appliedEvents = [];
  streamEnded = false;
  state.run_id = null;  // not yet minted
  state.meta = {
    app_id: candidate.app_id,
    game_name: candidate.game_name,
    review_id: candidate.review_id,
    category: candidate.category,
    review_text: candidate.review_text,
    review_tone: "",
  };
  const isRecommended = candidate.recommended === true;
  state.case = {
    label: isRecommended
      ? `Live · ${candidate.label}`
      : `Live · ${candidate.game_name || candidate.app_id} / ${candidate.category}`,
    story: isRecommended
      ? candidate.story
      : "Real agent run against Anthropic API. Click ▶ Run above to start.",
    start_panel: "timeline",
    talk_track: isRecommended ? candidate.talk_track : (
      "This is a staged live candidate. Click ▶ Run in the topbar to actually " +
      "execute the agent graph against the Anthropic API. Events will stream as " +
      "each node finishes (~30–60s wall clock for a full run)."
    ),
    live: true,
  };
  renderTalkTrack(state.case);
  render();
  scrollCanvasToTop();
  // Stepper visible but Run enabled; other buttons disabled until run starts.
  $("stepper").hidden = false;
  updateStepperUI();
}

async function startLiveRun(candidateOverride) {
  const candidate = candidateOverride || selectedLiveCandidate;
  if (!candidate) return;

  // POST /runs to register run_id + thread_id
  let runMeta;
  try {
    const res = await fetch("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ app_id: candidate.app_id, review_id: candidate.review_id }),
    });
    if (!res.ok) throw new Error(`POST /runs failed: ${res.status}`);
    runMeta = await res.json();
  } catch (err) {
    console.error(err);
    alert("Failed to start live run — see console");
    return;
  }

  // selectLiveCandidate already set state.case (with live:true) and state.meta.
  // We just need to mint the run_id, clear any stale event buffers, and open
  // the stream. DO NOT reset `state = emptyState()` here — that would wipe
  // state.case and strip the `live: true` flag, breaking the gate's interactive
  // approve/reject rendering downstream.
  eventQueue = [];
  appliedEvents = [];
  streamEnded = false;
  state.run_id = runMeta.run_id;
  // Update the case story from "click Run to start" to "executing" (keep live:true).
  state.case = {
    ...(state.case || { live: true }),
    story: candidate.recommended ? candidate.story : "Real agent run against Anthropic API. Events buffer as each node finishes; step through when you're ready.",
  };
  renderTalkTrack(state.case);
  selectedLiveCandidate = null;  // consumed
  render();
  openStream(`/runs/${runMeta.run_id}/stream`);
  liveRunByReview.set(candidate.review_id, runMeta.run_id);
  // Events buffer into the queue; user clicks Next / Play / Skip to step through.
  // Live streaming is too fast to follow by eye — letting the user control the pace
  // with the same stepper as replay gives a consistent experience.
}

function snapshotCurrentCase() {
  if (!state.run_id) return;
  caseCache.set(state.run_id, {
    state: structuredClone(state),
    eventQueue: structuredClone(eventQueue),
    appliedEvents: structuredClone(appliedEvents),
    streamEnded,
  });
}

function restoreCase(run_id) {
  const snap = caseCache.get(run_id);
  if (!snap) return false;
  state = structuredClone(snap.state);
  eventQueue = structuredClone(snap.eventQueue);
  appliedEvents = structuredClone(snap.appliedEvents);
  streamEnded = snap.streamEnded;
  renderTalkTrack(state.case);
  render();
  updateStepperUI();
  return true;
}

async function selectCase(run_id) {
  stopAutoPlay();
  closeStream();
  clearHighlights();
  snapshotCurrentCase();
  for (const li of document.querySelectorAll(".case-list li")) {
    li.classList.toggle("selected", li.dataset.runId === run_id);
  }
  if (restoreCase(run_id)) { scrollCanvasToTop(); return; }
  // First visit — fetch metadata and open stream fresh.
  state = emptyState();
  eventQueue = [];
  appliedEvents = [];
  streamEnded = false;
  const res = await fetch(`/replays/${run_id}`);
  const case_ = await res.json();
  state.case = case_;
  state.meta = case_.meta;
  state.run_id = run_id;
  renderTalkTrack(case_);
  render();
  scrollCanvasToTop();
  openStream(`/replays/${run_id}/stream?speed=instant`);
}

function scrollCanvasToTop() {
  // Reset canvas scroll position. Called on case / view switches so a new
  // case doesn't inherit the prior case's scroll offset.
  const canvas = document.querySelector(".canvas");
  if (!canvas) return;
  // Instant scroll now — no animation to fight with.
  canvas.scrollTop = 0;
  // Also schedule another reset for the next animation frame, in case a
  // synchronous DOM update re-triggered a layout that clamped or moved scroll.
  requestAnimationFrame(() => { canvas.scrollTop = 0; });
  // And once more after DOM settles, to catch any streaming event that might
  // force a scrollIntoView in its first frame.
  setTimeout(() => { canvas.scrollTop = 0; }, 50);
}

function goHome() {
  stopAutoPlay();
  closeStream();
  clearHighlights();
  snapshotCurrentCase();
  for (const li of document.querySelectorAll(".case-list li")) li.classList.remove("selected");
  state = emptyState();
  eventQueue = [];
  appliedEvents = [];
  streamEnded = false;
  render();
  scrollCanvasToTop();
}

function renderTalkTrack(case_) {
  const el = $("talk-track");
  el.hidden = false;
  el.dataset.open = "false";
  el.querySelector(".content").textContent = case_.talk_track;
  const btn = el.querySelector(".toggle");
  btn.textContent = `Interviewer notes — ${case_.label} ▸`;
  btn.onclick = () => {
    const open = el.dataset.open === "true";
    el.dataset.open = open ? "false" : "true";
    btn.setAttribute("aria-expanded", String(!open));
    btn.textContent = `Interviewer notes — ${case_.label} ${open ? "▸" : "▾"}`;
  };
}

function openStream(url, { reset = true } = {}) {
  // Fetch at instant speed (replay) / real-time (live) — all events arrive
  // into the queue; the stepper controls when they get applied to state.
  // When `reset` is false, the existing queue + applied events are preserved
  // (used for post-gate resume so the pre-gate history stays intact).
  if (reset) {
    eventQueue = [];
    appliedEvents = [];
  }
  streamEnded = false;
  autoPlay = false;
  $("stepper").hidden = false;
  updateStepperUI();
  eventSource = new EventSource(url);
  eventSource.onmessage = (e) => {
    try {
      eventQueue.push(JSON.parse(e.data));
      updateStepperUI();
      if (autoPlay) drainOne();
    } catch (err) { console.error("bad event", err, e.data); }
  };
  eventSource.onerror = () => {
    streamEnded = true;
    closeStream();
    updateStepperUI();
  };
}

function closeStream() {
  if (eventSource) { eventSource.close(); eventSource = null; }
}

function drainOne() {
  if (eventQueue.length === 0) return false;
  const evt = eventQueue.shift();
  applyEvent(evt);
  appliedEvents.push(evt);
  highlightChanges(evt);
  updateStepperUI();
  return true;
}

function restartCase() {
  if (!state.run_id) return;
  stopAutoPlay();
  // Merge applied + queued back into queue in original arrival order.
  const allEvents = [...appliedEvents, ...eventQueue];
  eventQueue = allEvents;
  appliedEvents = [];
  resetMutableState();
  render();
  clearHighlights();
  updateStepperUI();
}

function undoOne() {
  if (appliedEvents.length === 0) return false;
  const last = appliedEvents.pop();
  eventQueue.unshift(last);
  // Rebuild state from scratch by replaying remaining applied events.
  resetMutableState();
  const events = appliedEvents;
  appliedEvents = [];
  for (const e of events) {
    applyEvent(e);
    appliedEvents.push(e);
  }
  if (appliedEvents.length === 0) render();   // handle undo-to-empty
  // Highlight the NEW most-recent event (what's now at the tip).
  const tip = appliedEvents[appliedEvents.length - 1];
  highlightChanges(tip || null);
  updateStepperUI();
  return true;
}

function drainAll() {
  if (eventQueue.length === 0) return;
  stopAutoPlay();
  while (eventQueue.length > 0) {
    const evt = eventQueue.shift();
    applyEvent(evt);
    appliedEvents.push(evt);
  }
  // Highlight only the final event's panels so the view lands on the outcome.
  const tip = appliedEvents[appliedEvents.length - 1];
  highlightChanges(tip || null);
  updateStepperUI();
}

function startAutoPlay() {
  if (autoPlay) return;
  autoPlay = true;
  $("step-play").textContent = "Pause";
  // Live runs auto-play quickly (events arrive spaced out by LLM latency anyway);
  // replay cases pace at 1500ms for readable demo pacing.
  const perEventDelay = state.case?.live === true ? 100 : 1500;
  const tick = () => {
    if (!autoPlay) return;
    const more = drainOne();
    if (more) autoPlayTimer = setTimeout(tick, perEventDelay);
    else if (!streamEnded) autoPlayTimer = setTimeout(tick, 200);
    else {
      autoPlay = false;
      $("step-play").textContent = "Play remaining";
      updateStepperUI();
    }
  };
  tick();
}

function stopAutoPlay() {
  autoPlay = false;
  if (autoPlayTimer) { clearTimeout(autoPlayTimer); autoPlayTimer = null; }
  $("step-play").textContent = "Play remaining";
}

function updateStepperUI() {
  const stepRun = $("step-run");
  const stepPrev = $("step-prev");
  const stepNext = $("step-next");
  const stepPlay = $("step-play");
  const stepEnd = $("step-end");
  const stepRestart = $("step-restart");
  const status = $("step-status");
  const pending = eventQueue.length;
  const applied = appliedEvents.length;
  const total = pending + applied;
  const hasOutcome = !!state.outcome;

  // Always-reset visibility so switching between cases re-shows Play.
  stepPrev.hidden = false;
  stepPlay.hidden = false;
  stepEnd.hidden = false;

  // Run button: visible + enabled only when a live candidate has been selected
  // but the stream hasn't been opened yet. Hidden for replay (featured) cases
  // and for live runs that are already in progress / complete.
  const isLivePending = !!selectedLiveCandidate && !state.run_id;
  stepRun.hidden = !isLivePending;
  stepRun.disabled = !isLivePending;

  stepPrev.disabled = applied === 0;
  stepNext.disabled = pending === 0;
  stepPlay.disabled = pending === 0;
  stepEnd.disabled = pending === 0;
  stepRestart.disabled = applied === 0;

  const counter = total > 0 ? `Step ${applied} / ${total}` : "";

  if (hasOutcome && pending === 0) {
    status.textContent = `${counter} · run complete`.replace(/^ · /, "");
    stepNext.textContent = "Done";
    if (state.run_id && !watchedCases.has(state.run_id)) {
      watchedCases.add(state.run_id);
      markCaseAsWatched(state.run_id);
    }
  } else if (pending > 0) {
    const nextType = eventQueue[0].type.replace(/_/g, " ");
    const nextNode = eventQueue[0].payload?.node || eventQueue[0].payload?.label || "";
    stepNext.textContent = `Next ▶ ${nextType}${nextNode ? ` (${nextNode})` : ""}`;
    const receiving = streamEnded ? "" : " · receiving…";
    status.textContent = `${counter}${receiving}`;
  } else {
    stepNext.textContent = "Next step ▶";
    status.textContent = streamEnded ? "waiting" : "receiving…";
  }
}

function markCaseAsWatched(run_id) {
  // Featured cases carry data-run-id; live candidates carry data-review-id.
  // Look up both so the ✓ renders consistently across modes.
  const byRun = document.querySelector(`.case-list li[data-run-id="${run_id}"]`);
  if (byRun) byRun.classList.add("watched");
  const reviewId = state.meta?.review_id;
  if (reviewId) {
    const byReview = document.querySelector(`.case-list li[data-review-id="${reviewId}"]`);
    if (byReview) byReview.classList.add("watched");
  }
}

function resetState() {
  state = emptyState();
  eventQueue = [];
  appliedEvents = [];
  streamEnded = false;
  selectedLiveCandidate = null;
  stopAutoPlay();
  clearHighlights();
  $("talk-track").hidden = true;
  $("stepper").hidden = true;
  render();
}

// ============== Init ==============

$("reset-btn").addEventListener("click", () => {
  closeStream();
  caseCache.clear();
  watchedCases.clear();
  resetState();
  for (const li of document.querySelectorAll(".case-list li")) {
    li.classList.remove("selected");
    li.classList.remove("watched");
  }
});
$("home-btn").addEventListener("click", goHome);

for (const btn of document.querySelectorAll(".mode-btn")) {
  btn.addEventListener("click", () => {
    const mode = btn.dataset.mode;
    if (btn.disabled || mode === currentMode) return;
    setMode(mode);
  });
}

function setMode(mode) {
  currentMode = mode;
  for (const b of document.querySelectorAll(".mode-btn")) {
    b.classList.toggle("active", b.dataset.mode === mode);
  }
  $("sidebar-heading").textContent = mode === "featured" ? "Featured Cases" : "Unprocessed Reviews";
  goHome();
  if (mode === "featured") loadCaseList();
  else loadLiveCandidates();
}

for (const btn of document.querySelectorAll(".view-tab")) {
  btn.addEventListener("click", () => {
    const view = btn.dataset.view;
    if (view === currentView) return;
    setView(view);
  });
}

function setView(view) {
  currentView = view;
  for (const b of document.querySelectorAll(".view-tab")) {
    b.classList.toggle("active", b.dataset.view === view);
  }

  // Demo-view chrome only shown in Demo
  const isDemo = view === "demo";
  $("sidebar").hidden = !isDemo;
  document.querySelector(".layout").classList.toggle("no-sidebar", !isDemo);
  document.querySelector(".mode-toggle").hidden = !isDemo;
  $("home-btn").hidden = !isDemo;
  $("reset-btn").hidden = !isDemo;

  // Canvas regions — "case active" = state.case exists (covers staged live + replay + in-progress live)
  const caseActive = !!state.case;
  $("welcome").hidden = !isDemo || caseActive;
  $("run-panels").hidden = !isDemo || !caseActive;
  $("evals-view").hidden = view !== "evals";
  $("notes-view").hidden = view !== "notes";
  $("stepper").hidden = !isDemo || !caseActive;
  $("talk-track").hidden = !isDemo || !caseActive;

  if (view === "evals" && !evalsLoaded) loadEvals();
  if (view === "notes") {
    // Always re-fetch on Memory enter so any promotion from a previous live
    // run is reflected immediately without a page refresh.
    loadMemory();
    renderPromoteBanner();
  }
  scrollCanvasToTop();
}

async function loadMemory() {
  try {
    const res = await fetch("/memory/summary");
    if (!res.ok) {
      $("notes-loading").textContent = `Failed to load: ${res.status}`;
      return;
    }
    memoryData = await res.json();
    renderMemory(memoryData);
    $("notes-loading").hidden = true;
    $("notes-body").hidden = false;
    notesLoaded = true;
  } catch (err) {
    console.error(err);
    $("notes-loading").textContent = "Failed to load memory — see console.";
  }
}

async function renderPromoteBanner() {
  const banner = $("promote-banner");
  if (!pendingPromotion) {
    banner.hidden = true;
    return;
  }
  const p = pendingPromotion;

  // Fetch full content so user can review what would be promoted.
  let detail;
  try {
    const res = await fetch(`/memory/pending/${p.run_id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    detail = await res.json();
  } catch (err) {
    console.error(err);
    banner.hidden = false;
    banner.innerHTML = `<div class="promote-body promote-error">Could not fetch pending promotion — see console.</div>`;
    return;
  }

  // Track per-item handling. Any items already promoted (is_demo=0) or
  // already marked handled by this session are hidden from the prompt.
  if (!p.handled) p.handled = { audit: false, noteIds: new Set() };
  const auditStillPending = !p.handled.audit && detail.audit?.is_demo === 1;
  const pendingNotes = (detail.notes || []).filter(
    (n) => n.is_demo === 1 && !p.handled.noteIds.has(n.id)
  );

  if (!auditStillPending && pendingNotes.length === 0) {
    // All items have been handled (promoted or skipped). Clear and hide.
    pendingPromotion = null;
    banner.hidden = true;
    return;
  }

  banner.hidden = false;

  const auditCard = auditStillPending ? renderAuditReviewCard(detail.audit) : "";
  const noteCards = pendingNotes.map((n) => renderNoteReviewCard(n)).join("");

  banner.innerHTML = `
    <div class="promote-header">
      <strong>Live run finished</strong> on review ${escapeHtml(p.review_id)}
      (${escapeHtml(p.category || "?")}, final action <code>${escapeHtml(p.final_action || "?")}</code>).
      Review each item and choose which to keep.
    </div>
    <div class="promote-items">
      ${auditCard}
      ${noteCards}
    </div>
  `;

  // Wire up per-item buttons
  const auditNode = banner.querySelector("[data-promote-audit]");
  if (auditNode) {
    auditNode.querySelector(".promote-yes").addEventListener("click", () => promoteItem({audit: true}));
    auditNode.querySelector(".promote-skip").addEventListener("click", () => skipItem({audit: true}));
  }
  for (const card of banner.querySelectorAll("[data-promote-note]")) {
    const noteId = parseInt(card.dataset.promoteNote, 10);
    card.querySelector(".promote-yes").addEventListener("click", () => promoteItem({noteId}));
    card.querySelector(".promote-skip").addEventListener("click", () => skipItem({noteId}));
  }
}

function renderAuditReviewCard(audit) {
  const fields = [
    { label: "Review", value: audit.review_text },
    { label: "Evidence summary (investigator)", value: audit.evidence_summary },
    { label: "Drafted response (responder)", value: audit.drafted_response },
    { label: "Critique (critic)", value: audit.critique },
  ];
  const fieldsHtml = fields
    .filter((f) => f.value)
    .map((f) => `<div class="promote-item-field"><strong>${escapeHtml(f.label)}:</strong><pre>${escapeHtml(f.value)}</pre></div>`)
    .join("");
  return `
    <div class="promote-item" data-promote-audit>
      <div class="promote-item-head">
        <span class="promote-item-type">Audit entry → responder's few-shot pool</span>
        <span class="promote-item-meta">${escapeHtml(audit.game_name)} · ${escapeHtml(audit.category)} · action <code>${escapeHtml(audit.proposed_action || "?")}</code> · iter ${audit.iteration_count} · stop_reason <code>${escapeHtml(audit.stop_reason || "?")}</code></span>
      </div>
      <div class="promote-item-body">${fieldsHtml}</div>
      <div class="promote-item-actions">
        <button class="promote-btn promote-yes">Promote</button>
        <button class="promote-btn promote-skip">Skip</button>
      </div>
    </div>
  `;
}

function renderNoteReviewCard(note) {
  return `
    <div class="promote-item" data-promote-note="${note.id}">
      <div class="promote-item-head">
        <span class="promote-item-type">Cluster note (${escapeHtml(note.note_type)}) → investigator's context</span>
        <span class="promote-item-meta">${escapeHtml(note.game_name)} · ${escapeHtml(note.category)} · by ${escapeHtml(note.created_by)}</span>
      </div>
      <div class="promote-item-body">
        <div class="promote-item-field"><pre>${escapeHtml(note.note_text || "")}</pre></div>
      </div>
      <div class="promote-item-actions">
        <button class="promote-btn promote-yes">Promote</button>
        <button class="promote-btn promote-skip">Skip</button>
      </div>
    </div>
  `;
}

async function promoteItem({ audit = false, noteId = null }) {
  if (!pendingPromotion) return;
  const payload = {
    run_id: pendingPromotion.run_id,
    promote_audit: audit,
    promote_note_ids: noteId == null ? [] : [noteId],
  };
  try {
    const res = await fetch("/memory/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.text();
      alert(`Promote failed (${res.status}): ${err}`);
      return;
    }
  } catch (err) {
    alert("Promote request failed — see console");
    console.error(err);
    return;
  }
  if (audit) pendingPromotion.handled.audit = true;
  if (noteId != null) pendingPromotion.handled.noteIds.add(noteId);
  // Refresh Memory lists (promoted item now appears in the main list) and re-render banner.
  await loadMemory();
  await renderPromoteBanner();
}

function skipItem({ audit = false, noteId = null }) {
  if (!pendingPromotion) return;
  if (audit) pendingPromotion.handled.audit = true;
  if (noteId != null) pendingPromotion.handled.noteIds.add(noteId);
  renderPromoteBanner();
}

function renderMemory(data) {
  const t = data.totals;
  const cn = t.cluster_notes;
  const al = t.audit_log;
  $("notes-totals").innerHTML = `
    <div class="metric-card">
      <div class="metric-label">cluster notes — visible</div>
      <div class="metric-value">${cn.promoted_visible}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">cluster notes — raw pool</div>
      <div class="metric-value">${cn.all.toLocaleString()}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">audit log few-shot pool — visible</div>
      <div class="metric-value">${al.promoted_visible}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">audit log — total</div>
      <div class="metric-value">${al.all.toLocaleString()}</div>
    </div>
  `;

  // ---- Cluster notes filters + list ----
  const noteCats = [...new Set(data.notes.map((n) => n.category))].sort();
  const noteTypes = [...new Set(data.notes.map((n) => n.note_type))].sort();
  const noteGames = [...new Set(data.notes.map((n) => n.game_name))].sort();

  const catSel = $("notes-filter-category");
  catSel.innerHTML = `<option value="">all (${data.notes.length})</option>` +
    noteCats.map((c) => {
      const n = data.notes.filter((x) => x.category === c).length;
      return `<option value="${escapeHtml(c)}">${escapeHtml(c)} (${n})</option>`;
    }).join("");
  const typeSel = $("notes-filter-type");
  typeSel.innerHTML = `<option value="">all</option>` +
    noteTypes.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
  const gameSel = $("notes-filter-game");
  gameSel.innerHTML = `<option value="">all</option>` +
    noteGames.map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`).join("");
  const notesSortSel = $("notes-sort");
  for (const sel of [catSel, typeSel, gameSel, notesSortSel]) {
    sel.onchange = renderNotesList;
  }

  // ---- Audit log filters + list ----
  const auditCats = [...new Set(data.audits.map((a) => a.category || ""))].filter(Boolean).sort();
  const auditActions = [...new Set(data.audits.map((a) => a.proposed_action || ""))].filter(Boolean).sort();
  const auditGames = [...new Set(data.audits.map((a) => a.game_name))].sort();

  const aCatSel = $("audits-filter-category");
  aCatSel.innerHTML = `<option value="">all (${data.audits.length})</option>` +
    auditCats.map((c) => {
      const n = data.audits.filter((x) => x.category === c).length;
      return `<option value="${escapeHtml(c)}">${escapeHtml(c)} (${n})</option>`;
    }).join("");
  const aActSel = $("audits-filter-action");
  aActSel.innerHTML = `<option value="">all</option>` +
    auditActions.map((a) => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join("");
  const aGameSel = $("audits-filter-game");
  aGameSel.innerHTML = `<option value="">all</option>` +
    auditGames.map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`).join("");
  const auditsSortSel = $("audits-sort");
  for (const sel of [aCatSel, aActSel, aGameSel, auditsSortSel]) {
    sel.onchange = renderAuditsList;
  }

  renderNotesList();
  renderAuditsList();
}

function sortNotes(arr, sort) {
  const cmp = (a, b) => (a == null ? "" : String(a)).localeCompare(b == null ? "" : String(b));
  switch (sort) {
    case "promoted_desc": arr.sort((a, b) => cmp(b.promoted_at, a.promoted_at)); break;
    case "promoted_asc":  arr.sort((a, b) => cmp(a.promoted_at, b.promoted_at)); break;
    case "created_desc":  arr.sort((a, b) => cmp(b.created_at, a.created_at)); break;
    case "created_asc":   arr.sort((a, b) => cmp(a.created_at, b.created_at)); break;
    case "category":      arr.sort((a, b) => cmp(a.category, b.category)); break;
  }
}

function sortAudits(arr, sort) {
  const cmp = (a, b) => (a == null ? "" : String(a)).localeCompare(b == null ? "" : String(b));
  switch (sort) {
    case "promoted_desc": arr.sort((a, b) => cmp(b.promoted_at, a.promoted_at)); break;
    case "promoted_asc":  arr.sort((a, b) => cmp(a.promoted_at, b.promoted_at)); break;
    case "created_desc":  arr.sort((a, b) => cmp(b.created_at, a.created_at)); break;
    case "created_asc":   arr.sort((a, b) => cmp(a.created_at, b.created_at)); break;
    case "category":      arr.sort((a, b) => cmp(a.category, b.category)); break;
    case "iterations":    arr.sort((a, b) => (b.iteration_count || 0) - (a.iteration_count || 0)); break;
  }
}

function renderNotesList() {
  if (!memoryData) return;
  const cat = $("notes-filter-category").value;
  const type = $("notes-filter-type").value;
  const game = $("notes-filter-game").value;
  const sort = $("notes-sort")?.value || "promoted_desc";
  const filtered = memoryData.notes.filter((n) =>
    (!cat || n.category === cat) &&
    (!type || n.note_type === type) &&
    (!game || n.game_name === game)
  );
  sortNotes(filtered, sort);
  $("notes-list-meta").textContent = `${filtered.length} notes shown of ${memoryData.notes.length}`;
  const list = $("notes-list");
  list.innerHTML = filtered.map((n) => {
    const noteText = n.note_text || "";
    const collapsed = noteText.length > 240 ? noteText.slice(0, 237) + "…" : noteText;
    return `
      <li class="note-card">
        <div class="note-head">
          <span class="note-type chip-${escapeHtml(n.note_type)}">${escapeHtml(n.note_type)}</span>
          <span class="note-cat">${escapeHtml(n.game_name)} · ${escapeHtml(n.category)}</span>
          <span class="note-review">review ${escapeHtml(n.source_review_id || "?")}</span>
        </div>
        <div class="note-body">${escapeHtml(collapsed)}</div>
        ${noteText.length > 240 ? `<details class="note-full"><summary>Full note</summary><p>${escapeHtml(noteText)}</p></details>` : ""}
        <div class="note-foot">promoted ${escapeHtml(n.promoted_at || "?")} by <code>${escapeHtml(n.promoted_by || "?")}</code></div>
      </li>
    `;
  }).join("");
}

function renderAuditsList() {
  if (!memoryData) return;
  const cat = $("audits-filter-category").value;
  const action = $("audits-filter-action").value;
  const game = $("audits-filter-game").value;
  const sort = $("audits-sort")?.value || "promoted_desc";
  const filtered = memoryData.audits.filter((a) =>
    (!cat || a.category === cat) &&
    (!action || a.proposed_action === action) &&
    (!game || a.game_name === game)
  );
  sortAudits(filtered, sort);
  $("audits-list-meta").textContent = `${filtered.length} shown of ${memoryData.audits.length}`;
  const list = $("audits-list");
  list.innerHTML = filtered.map((a) => `
    <li class="audit-card">
      <div class="audit-head">
        <span class="action-chip ${escapeHtml(a.proposed_action || "")}">${escapeHtml((a.proposed_action || "").toUpperCase())}</span>
        <span class="audit-cat">${escapeHtml(a.game_name)} · ${escapeHtml(a.category || "")}</span>
        <span class="audit-meta">iter=${a.iteration_count} · run ${escapeHtml((a.run_id || "").slice(0, 8))}</span>
      </div>
      <div class="audit-review"><strong>Review:</strong> ${escapeHtml(a.review_preview || "")}</div>
      <div class="audit-draft"><strong>Draft:</strong> ${escapeHtml(a.drafted_preview || "")}</div>
      <div class="note-foot">promoted by <code>${escapeHtml(a.promoted_by || "?")}</code></div>
    </li>
  `).join("");
}

// ---- Evals tab ----

async function loadEvals() {
  try {
    const res = await fetch("/evals/latest");
    if (!res.ok) {
      $("evals-loading").textContent = `Failed to load: ${res.status}`;
      return;
    }
    const data = await res.json();
    renderEvals(data);
    $("evals-loading").hidden = true;
    $("evals-body").hidden = false;
    evalsLoaded = true;
  } catch (err) {
    console.error(err);
    $("evals-loading").textContent = "Failed to load evals — see console.";
  }
}

function fmtPct(v) {
  if (v == null) return "—";
  return (v * 100).toFixed(1) + "%";
}
function fmtNum(v) {
  if (v == null) return "—";
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(3);
}
function fmtDelta(d, asPct = false) {
  if (d == null || d === 0) return "";
  const sign = d > 0 ? "+" : "";
  const val = asPct ? (d * 100).toFixed(1) + "pp" : d.toFixed(3);
  const cls = d > 0 ? "delta-up" : "delta-down";
  return `<span class="delta ${cls}">${sign}${val}</span>`;
}

function renderEvals(data) {
  // Snapshot meta — run-flow breakdown in the header right corner
  const action = data.action || {};
  const meta = data.meta || {};
  const freezeN = action.n_excluded_action_freeze || 0;
  const noRespN = action.n_excluded_no_response || 0;
  const errN = action.n_excluded_infra_error || 0;
  const evalN = action.n_evaluable || 0;
  const totalN = meta.n_records || (evalN + freezeN + noRespN + errN);
  $("evals-meta").textContent = `${totalN} golden cases → ${evalN} action-eligible · ${noRespN} no-response · ${freezeN} freeze · ${errN} error`;

  // Headline cards — README-table metrics in the display order:
  // Action Correctness · NDCG@7 · Concept Recall · Concept Precision · Gating Accuracy.
  const h = data.headline || {};
  const cards = [
    { label: "Action Correctness", value: fmtPct(h.action_correctness), sub: `macro F1 ${fmtNum(h.action_macro_f1)} · n=${h.action_n_evaluable}` },
    { label: "NDCG@7",             value: fmtNum(h.ndcg_at_7),          sub: "ranking quality — top-K=7 (reranker pool)" },
    { label: "Concept Recall",     value: fmtPct(h.concept_recall),     sub: "of gold concepts, fraction retrieved" },
    { label: "Concept Precision",  value: fmtPct(h.concept_precision),  sub: "of chunks investigator kept, fraction useful" },
    { label: "Gating Accuracy",    value: fmtPct(h.gating_accuracy),    sub: `false-skip ${fmtPct(h.gating_false_skip_rate)} · false-retrieve ${fmtPct(h.gating_false_retrieve_rate)}` },
  ];
  $("evals-headline").innerHTML = cards.map((c) => `
    <div class="metric-card">
      <div class="metric-label">${escapeHtml(c.label)}</div>
      <div class="metric-value">${c.value}</div>
      <div class="metric-sub">${c.sub}</div>
    </div>
  `).join("");

  // Critic health — uses README vocabulary: "First-pass Rate" is non-freeze
  // iter-0 approval (matches README value 84.8%), not iter0_approval over all
  // action-eligible cases (which conflates freeze as "not approved").
  const ch = data.critic_health || {};
  $("evals-critic-meta").textContent = `mean iters → approval: ${fmtNum(ch.mean_iters_to_approval)}`;
  const reasons = ch.rejections_by_reason_type || {};
  const reasonTotal = Object.values(reasons).reduce((a, b) => a + (b || 0), 0);
  $("evals-critic").innerHTML = `
    <div class="metric-card">
      <div class="metric-label">First-pass Rate</div>
      <div class="metric-value">${fmtPct(ch.first_pass_rate_non_freeze)}</div>
      <div class="metric-sub">${ch.first_pass_n_approved}/${ch.first_pass_n_non_freeze} non-freeze · ${ch.first_pass_n_freeze_excluded} freeze excluded from denominator</div>
    </div>
    <div class="metric-card wide">
      <div class="metric-label">rejections by reason_type</div>
      <div class="reason-bars">
        ${Object.entries(reasons).map(([rt, n]) => {
          const pct = reasonTotal > 0 ? (n / reasonTotal) * 100 : 0;
          return `
            <div class="reason-row">
              <div class="reason-label">${escapeHtml(rt || "(none)")}</div>
              <div class="reason-bar"><div class="reason-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
              <div class="reason-count">${n}</div>
            </div>
          `;
        }).join("")}
      </div>
      <div class="metric-sub">total rejections: ${reasonTotal}</div>
    </div>
  `;

  // Judge — each category has its own meaningful metric pattern
  const j = data.judge || {};
  const judgeCards = [];

  // Ordered by industry importance (most universal RAG/agent metric → most niche):
  // Faithfulness → Context Sufficiency → Action Severity → Low-confidence Citations → Revision Improvement

  // 1. Faithfulness — THE hallucination-prevention metric. #1 in every RAG eval framework
  //    (Ragas, TruLens, LangSmith, DeepEval). Does the draft only claim what the retrieved
  //    context actually supports?
  const dg = j.draft_grounding || {};
  const dgTot = (dg.n_judged || 0);
  judgeCards.push({
    label: "Faithfulness",
    value: dgTot > 0 ? `${dg.n_supports || 0}/${dgTot} supports` : "—",
    sub: `${dg.n_partially_supports || 0} partial · ${dg.n_does_not_support || 0} unsupported`,
  });

  // 2. Context Sufficiency — paired with Faithfulness to diagnose "retrieval vs generation"
  //    issue. Universal in RAG; sometimes called Context Relevance / Context Quality.
  const ps = j.pool_sufficiency || {};
  const psTot = (ps.n_judged || 0);
  judgeCards.push({
    label: "Context Sufficiency",
    value: psTot > 0 ? `${ps.n_supports || 0}/${psTot} supports` : "—",
    sub: `${ps.n_partially_supports || 0} partial · ${ps.n_does_not_support || 0} unsupported`,
  });

  // 3. Action Severity Errors — task-specific output correctness (this system's equivalent
  //    of Answer Correctness).
  const a = j.action || {};
  const aTot = (a.n_flagged || 0);
  judgeCards.push({
    label: "Action Severity Errors",
    value: aTot > 0 ? String(aTot) : "0",
    sub: `over ${a.n_over_escalation || 0} · missed ${a.n_missed_escalation || 0} · drift ${a.n_category_drift || 0} · tolerable ${a.n_tolerable_disagreement || 0}`,
  });

  // 4. Low-confidence Citations — calibration/honesty. Emerging standard; matters for
  //    cited-evidence systems but less universal than the above.
  const lcc = j.low_conf_with_cite || {};
  const lccTot = (lcc.n_flagged || 0);
  judgeCards.push({
    label: "Low-confidence Citations",
    value: lccTot > 0 ? `${lcc.n_misleading_fix_claim}/${lccTot} misleading` : "—",
    sub: `${lcc.n_honest_hedge || 0} honest hedge · ${lcc.n_unclear || 0} unclear`,
  });

  // 5. Revision Improvement — diagnostic for agentic systems with critic loops. Niche;
  //    only relevant to iterative architectures.
  const p = j.pairwise || {};
  // Strip `n_deterministic` from the denominator — those are runs with no revision
  // (iter 0 == final iter), auto-labeled neutral.
  const pDet = p.n_deterministic || 0;
  const pLLM = (p.n_judged || 0) - pDet;
  const improved = p.n_revision_improved || 0;
  const regressed = p.n_revision_regressed || 0;
  judgeCards.push({
    label: "Revision Improvement",
    value: pLLM > 0 ? `${improved}/${pLLM} improved` : "no revisions ran",
    sub: `${regressed} regressed · ${pDet} runs had no revision`,
  });


  $("evals-judge").innerHTML = judgeCards.map((c) => `
    <div class="metric-card">
      <div class="metric-label">${escapeHtml(c.label)}</div>
      <div class="metric-value">${escapeHtml(c.value)}</div>
      <div class="metric-sub">${escapeHtml(c.sub)}</div>
    </div>
  `).join("");

  // Action freeze — two cards:
  // (1) how often freeze fires
  // (2) responder-vs-critic correctness under freeze — empirical justification for the design
  const freezeTotal = action.n_excluded_action_freeze || 0;
  const freezeCorrect = action.freeze_n_correct || 0;
  const freezeRate = action.freeze_correct_rate;
  const nEligible = evalN + freezeTotal;
  const criticCorrect = freezeTotal - freezeCorrect;
  const criticRate = freezeTotal > 0 ? criticCorrect / freezeTotal : 0;

  $("evals-freeze").innerHTML = `
    <div class="metric-card">
      <div class="metric-label">Freeze Fire Count</div>
      <div class="metric-value">${freezeTotal}</div>
      <div class="metric-sub">of ${nEligible} action-eligible cases (${fmtPct(freezeTotal / (nEligible || 1))})</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Responder Right When Frozen</div>
      <div class="metric-value">${freezeCorrect}/${freezeTotal} (${fmtPct(freezeRate)})</div>
      <div class="metric-sub">Critic preferred would have been correct: ${criticCorrect}/${freezeTotal} (${fmtPct(criticRate)})</div>
    </div>
  `;
}

$("step-run").addEventListener("click", () => startLiveRun());
$("step-prev").addEventListener("click", () => {
  stopAutoPlay();
  undoOne();
});
$("step-next").addEventListener("click", () => {
  stopAutoPlay();
  drainOne();
});
$("step-play").addEventListener("click", () => {
  if (autoPlay) stopAutoPlay();
  else startAutoPlay();
});
$("step-end").addEventListener("click", drainAll);
$("step-restart").addEventListener("click", restartCase);

loadCaseList();
