const API = "";

let scenarios = [];
let currentRunId = null;
let currentRunData = null;

const el = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body}`);
  }
  return res.json();
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function statusOf(run) {
  return run.final_status || "unknown";
}

// ---------- scenario form ----------

async function loadScenarios() {
  scenarios = await api("/api/scenarios");
  const select = el("scenario-select");
  select.innerHTML = scenarios.map(s => `<option value="${s.id}">${s.label}</option>`).join("");
  select.addEventListener("change", updateScenarioDesc);
  updateScenarioDesc();
}

function updateScenarioDesc() {
  const id = el("scenario-select").value;
  const s = scenarios.find(s => s.id === id);
  el("scenario-desc").textContent = s ? s.description : "";
  el("custom-fields").classList.toggle("hidden", id !== "custom");
}

el("new-run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = el("run-btn");
  btn.disabled = true;
  btn.textContent = "Running...";
  try {
    const scenario = el("scenario-select").value;
    const body = { scenario };
    if (scenario === "custom") {
      body.user_amount = parseFloat(el("user-amount").value || "0");
      body.injected_note = el("injected-note").value || null;
      body.forge_attack = el("forge-attack").checked;
    }
    const { run_id } = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadRunList();
    await selectRun(run_id);
  } catch (err) {
    alert("Run failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run workflow";
  }
});

// ---------- run list ----------

async function loadRunList() {
  const runs = await api("/api/runs");
  const list = el("run-list");
  if (runs.length === 0) {
    list.innerHTML = `<p class="hint">No runs yet.</p>`;
    return;
  }
  list.innerHTML = runs.map(r => `
    <div class="run-item ${r.run_id === currentRunId ? "active" : ""}" data-run-id="${r.run_id}">
      <span class="run-item-scenario">${r.scenario}</span>
      <span class="run-item-id">${r.run_id.slice(0, 8)}</span>
    </div>
  `).join("");
  list.querySelectorAll(".run-item").forEach(node => {
    node.addEventListener("click", () => selectRun(node.dataset.runId));
  });
}

// ---------- run detail ----------

async function selectRun(runId) {
  currentRunId = runId;
  currentRunData = await api(`/api/runs/${runId}`);

  el("empty-state").classList.add("hidden");
  el("run-detail").classList.remove("hidden");
  el("replay-result").classList.add("hidden");
  el("causal-result").classList.add("hidden");

  document.querySelectorAll(".run-item").forEach(n => n.classList.toggle("active", n.dataset.runId === runId));

  el("run-scenario-label").textContent = currentRunData.scenario;
  el("run-id-label").textContent = runId;

  const finalEvent = currentRunData.events.find(e => e.event_type === "final_result");
  const status = finalEvent ? finalEvent.payload.status : "unknown";
  const statusBadge = el("status-badge");
  statusBadge.textContent = status;
  statusBadge.className = "badge " + (status === "executed" ? "ok" : "bad");

  const integrityBadge = el("integrity-badge");
  integrityBadge.textContent = currentRunData.integrity_ok ? "log integrity OK" : "log TAMPERED";
  integrityBadge.className = "badge " + (currentRunData.integrity_ok ? "ok" : "bad");

  renderTrace(currentRunData.events);
  renderContext(currentRunData.context_items);
}

function renderTrace(events) {
  el("trace-list").innerHTML = events.map((e, i) => `
    <div class="trace-event" data-idx="${i}">
      <div class="trace-event-head">
        <span class="agent-chip ${e.agent_id}">${e.agent_id}</span>
        <span class="event-type ${e.event_type}">${e.event_type}</span>
        <span class="hint">${fmtTime(e.ts)}</span>
      </div>
      <div class="trace-event-body"><pre>${escapeHtml(JSON.stringify(e.payload, null, 2))}</pre></div>
    </div>
  `).join("");
  document.querySelectorAll(".trace-event").forEach(node => {
    node.querySelector(".trace-event-head").addEventListener("click", () => {
      node.classList.toggle("expanded");
    });
  });
}

function renderContext(items) {
  el("context-list").innerHTML = items.map(item => `
    <div class="context-item" data-id="${item.id}">
      <div class="context-item-head">
        <label class="checkbox-row" style="margin:0;">
          <input type="checkbox" class="override-toggle" />
          <span class="context-item-id">${item.id}</span>
        </label>
        <span class="trust-badge ${item.trusted ? "trusted" : "untrusted"}">${item.trusted ? "trusted" : "untrusted"}</span>
        <span class="source-label">${item.source}</span>
      </div>
      <textarea class="override-text" disabled>${escapeHtml(String(item.content))}</textarea>
      <div class="relevance-note"></div>
    </div>
  `).join("");

  document.querySelectorAll(".context-item").forEach(node => {
    const toggle = node.querySelector(".override-toggle");
    const textarea = node.querySelector(".override-text");
    toggle.addEventListener("change", () => {
      textarea.disabled = !toggle.checked;
    });
  });
}

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function collectOverrides() {
  const overrides = {};
  document.querySelectorAll(".context-item").forEach(node => {
    const toggle = node.querySelector(".override-toggle");
    if (!toggle.checked) return;
    const id = node.dataset.id;
    const val = node.querySelector(".override-text").value.trim();
    overrides[id] = val.length > 0 ? val : null;
  });
  return overrides;
}

// ---------- replay ----------

el("replay-btn").addEventListener("click", async () => {
  if (!currentRunId) return;
  const overrides = collectOverrides();
  if (Object.keys(overrides).length === 0) {
    alert("Check at least one context item to override before replaying.");
    return;
  }
  const data = await api(`/api/runs/${currentRunId}/replay`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides }),
  });
  renderReplayResult(data);
});

function renderReplayResult(data) {
  const card = el("replay-result");
  card.classList.remove("hidden");
  card.innerHTML = `
    <h4>Replay result</h4>
    <div class="diff-row"><span class="diff-label">baseline</span><pre>${escapeHtml(JSON.stringify(data.baseline))}</pre></div>
    <div class="diff-row"><span class="diff-label">with override</span><pre>${escapeHtml(JSON.stringify(data.result))}</pre></div>
    <div class="verdict ${data.outcome_changed ? "changed" : "unchanged"}">
      ${data.outcome_changed ? "Outcome changed — this override altered the result." : "Outcome unchanged — this override did not affect the result."}
    </div>
  `;
}

// ---------- causal analysis ----------

el("causal-btn").addEventListener("click", async () => {
  if (!currentRunId) return;
  const btn = el("causal-btn");
  btn.disabled = true;
  btn.textContent = "Analyzing...";
  try {
    const data = await api(`/api/runs/${currentRunId}/causal`);
    renderCausalResult(data);
  } finally {
    btn.disabled = false;
    btn.textContent = "Auto-detect likely cause";
  }
});

function renderCausalResult(data) {
  const relevantIds = [];
  const redundantPairs = data.redundant_pairs || [];
  const redundantIds = new Set(redundantPairs.flatMap(p => p.pair));

  document.querySelectorAll(".context-item").forEach(node => {
    const id = node.dataset.id;
    const info = data.per_context[id];
    const note = node.querySelector(".relevance-note");
    node.classList.remove("relevant", "not-relevant", "redundant");
    if (!info) return;
    if (info.causally_relevant) {
      node.classList.add("relevant");
      note.className = "relevance-note relevant";
      note.textContent = `causally relevant — without this, outcome becomes ${JSON.stringify(info.without_this_context)}`;
      relevantIds.push(id);
    } else if (redundantIds.has(id)) {
      const pair = redundantPairs.find(p => p.pair.includes(id));
      const partner = pair.pair.find(x => x !== id);
      node.classList.add("redundant");
      note.className = "relevance-note redundant";
      note.textContent = `not relevant alone — but jointly sufficient together with ${partner} (redundant pair, found by pairwise intervention)`;
    } else {
      node.classList.add("not-relevant");
      note.className = "relevance-note not-relevant";
      note.textContent = "not relevant — removing it (alone or in a pair) does not change the outcome";
    }
  });

  const lines = [];
  relevantIds.forEach(id => {
    lines.push(`<div class="causal-line relevant"><span class="cid">${id}</span> is the likely root cause of this outcome.</div>`);
  });
  redundantPairs.forEach(p => {
    lines.push(`<div class="causal-line redundant"><span class="cid">${p.pair.join(" + ")}</span> are jointly sufficient — leave-one-out missed this individually, pairwise intervention caught it.</div>`);
  });
  if (lines.length === 0) {
    lines.push(`<div class="causal-line not-relevant">No single item, and no pair, changes the outcome when removed.</div>`);
  }

  const card = el("causal-result");
  card.classList.remove("hidden");
  card.innerHTML = `
    <h4>Causal analysis</h4>
    <div class="diff-row"><span class="diff-label">baseline</span><pre>${escapeHtml(JSON.stringify(data.baseline))}</pre></div>
    ${lines.join("")}
  `;
}

// ---------- boot ----------

(async function init() {
  await loadScenarios();
  await loadRunList();
})();
