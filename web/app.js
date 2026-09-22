// PROBE dashboard: reads api/state and renders the summary, matrix, table and details.

const url = (path) => new URL(path, document.baseURI).toString();

const STATUS = {
  pass: { label: "pass", icon: "✓" },
  fail: { label: "fail", icon: "✕" },
  skip: { label: "skip", icon: "–" },
  running: { label: "running", icon: "◌" },
  none: { label: "never run", icon: "·" },
};
const HISTORY_CELLS = 12;

const state = {
  data: null,
  filters: readFilters(),
  selected: null,
  timer: null,
  artifacts: [],
  records: null,
};

const el = {};
for (const id of [
  "top-meta", "run-all", "refresh", "theme", "notice", "f-platform", "f-user", "f-system", "f-workflow",
  "f-status", "f-search", "f-changes", "f-clear", "t-tests", "t-pass", "t-fail", "t-skip",
  "t-regressions", "t-last", "t-last-detail", "tile-fail", "tile-regressions", "matrix", "table-count",
  "tests-body", "suites-body", "drawer", "drawer-backdrop", "d-workflow", "d-title", "d-chips", "d-close",
  "d-admin", "d-rerun", "d-cancel", "d-delete", "d-admin-msg", "d-error", "d-facts", "d-history", "d-artifact", "d-file",
  "d-log", "d-definition-path", "d-definition", "d-record", "foot-version", "foot-results",
]) el[id] = document.getElementById(id);

// ---------------------------------------------------------------- utilities

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function fmtDuration(seconds) {
  if (seconds == null || isNaN(seconds)) return "–";
  const s = Math.round(Number(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function relTime(iso) {
  if (!iso) return "–";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return iso;
  const diff = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

function statusOf(test) {
  if (test.running) return "running";
  return test.status && STATUS[test.status] ? test.status : "none";
}

function chip(status, extraClass = "") {
  const s = STATUS[status] || STATUS.none;
  return `<span class="chip chip-${esc(status)} ${extraClass}"><span class="chip-icon" aria-hidden="true">${s.icon}</span>${esc(s.label)}</span>`;
}

function changeChip(change) {
  if (change === "regression") return `<span class="chip chip-regression"><span class="chip-icon" aria-hidden="true">▼</span>regression</span>`;
  if (change === "recovery") return `<span class="chip chip-recovery"><span class="chip-icon" aria-hidden="true">▲</span>recovery</span>`;
  return "";
}

function historyStrip(history) {
  const items = (history || []).slice(-HISTORY_CELLS);
  if (!items.length) return `<span class="muted">–</span>`;
  return `<span class="hist" aria-label="Last ${items.length} results">` + items.map((h) => {
    const s = STATUS[h.status] ? h.status : "none";
    const title = `${h.started_at || ""} ${s}${h.duration_s != null ? " " + fmtDuration(h.duration_s) : ""}${h.error ? " - " + h.error : ""}`;
    return `<i class="h h-${s}" title="${esc(title)}" aria-label="${esc(title)}">${STATUS[s].icon}</i>`;
  }).join("") + "</span>";
}

function uniq(values) {
  return [...new Set(values)];
}

function setOptions(select, values, current, labelOf = (v) => v) {
  const previous = current || select.value;
  select.innerHTML = `<option value="">All</option>` + values.map((v) =>
    `<option value="${esc(v)}">${esc(labelOf(v))}</option>`).join("");
  select.value = values.includes(previous) ? previous : "";
}

// One notice line. Definition errors come from the data and stay while they
// exist; action notices (runs started, deletions, failures) fade after a while
// and survive the periodic reload.
let noticeTimer = null;
function notice(message, kind = "info", source = "action") {
  clearTimeout(noticeTimer);
  if (!message) {
    if (source === "definitions" && el.notice.dataset.source !== "definitions") return;
    el.notice.hidden = true;
    el.notice.dataset.source = "";
    return;
  }
  el.notice.textContent = message;
  el.notice.dataset.kind = kind;
  el.notice.dataset.source = source;
  el.notice.hidden = false;
  if (source === "action") noticeTimer = setTimeout(() => { if (el.notice.dataset.source === "action") el.notice.hidden = true; }, 12000);
}

// ---------------------------------------------------------------- filters (kept in the URL hash)

function readFilters() {
  const params = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
  return {
    platform: params.get("platform") || "",
    user: params.get("user") || "",
    system: params.get("system") || "",
    workflow: params.get("workflow") || "",
    status: params.get("status") || "",
    search: params.get("q") || "",
    changes: params.get("changes") === "1",
    test: params.get("test") || "",
  };
}

function writeFilters() {
  const f = state.filters;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ platform: f.platform, user: f.user, system: f.system, workflow: f.workflow, status: f.status, q: f.search })) {
    if (value) params.set(key, value);
  }
  if (f.changes) params.set("changes", "1");
  if (state.selected) params.set("test", state.selected);
  const hash = params.toString();
  window.history.replaceState(null, "", hash ? "#" + hash : window.location.pathname + window.location.search);
}

function readFilterControls() {
  state.filters.platform = el["f-platform"].value;
  state.filters.user = el["f-user"].value;
  state.filters.system = el["f-system"].value;
  state.filters.workflow = el["f-workflow"].value;
  state.filters.status = el["f-status"].value;
  state.filters.search = el["f-search"].value.trim();
  state.filters.changes = el["f-changes"].checked;
}

function systemKey(test) {
  return test.system || "(no resource)";
}

function matches(test) {
  const f = state.filters;
  const status = statusOf(test);
  if (f.platform && test.platform !== f.platform) return false;
  if (f.user && test.user !== f.user) return false;
  if (f.system && systemKey(test) !== f.system) return false;
  if (f.workflow && test.workflow_name !== f.workflow) return false;
  if (f.status && status !== f.status) return false;
  if (f.changes && !test.change) return false;
  if (f.search) {
    const o = (test.current && test.current.outcome) || {};
    const w = (test.current && test.current.workflow) || {};
    const hay = [test.id, test.system, test.node, o.run_slug, o.error, o.endpoint, w.commit, w.ref]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(f.search.toLowerCase())) return false;
  }
  return true;
}

// ---------------------------------------------------------------- rendering

function renderFilters(tests) {
  const f = state.filters;
  setOptions(el["f-platform"], uniq(tests.map((t) => t.platform)).sort(), f.platform);
  setOptions(el["f-user"], uniq(tests.map((t) => t.user)).sort(), f.user);
  setOptions(el["f-system"], uniq(tests.map(systemKey)).sort(), f.system);
  setOptions(el["f-workflow"], uniq(tests.map((t) => t.workflow_name)).sort(), f.workflow);
  setOptions(el["f-status"], ["pass", "fail", "skip", "running", "none"].filter((s) => tests.some((t) => statusOf(t) === s)), f.status,
    (s) => STATUS[s].label);
  el["f-search"].value = f.search;
  el["f-changes"].checked = f.changes;
}

function renderTiles(tests) {
  const count = (fn) => tests.filter(fn).length;
  el["t-tests"].textContent = tests.length;
  el["t-pass"].textContent = count((t) => statusOf(t) === "pass");
  const fails = count((t) => statusOf(t) === "fail");
  el["t-fail"].textContent = fails;
  el["tile-fail"].classList.toggle("is-bad", fails > 0);
  el["t-skip"].textContent = count((t) => statusOf(t) === "skip");
  const regressions = count((t) => t.change === "regression");
  el["t-regressions"].textContent = regressions;
  el["tile-regressions"].classList.toggle("is-bad", regressions > 0);
  let latest = null;
  for (const t of tests) {
    const o = t.current && t.current.outcome;
    if (o && o.started_at && (!latest || o.started_at > latest.started_at)) latest = { started_at: o.started_at, suite_run: t.current.suite_run };
  }
  el["t-last"].textContent = latest ? relTime(latest.started_at) : "–";
  el["t-last"].title = latest ? fmtDate(latest.started_at) : "";
  el["t-last-detail"].textContent = latest ? latest.suite_run || "" : "";
}

function matrixChip(test, cell) {
  const labels = cell.map((x) => x.node || x.test);
  const useName = new Set(labels).size !== labels.length;
  const label = useName || !test.node ? test.test : test.node;
  const s = statusOf(test);
  const flag = test.change === "regression" ? `<span class="chip-flag" title="regression" aria-label="regression">▼</span>` : "";
  return `<button type="button" class="chip chip-${s} chip-btn" data-id="${esc(test.id)}" title="${esc(test.id)}: ${esc(STATUS[s].label)}"><span class="chip-icon" aria-hidden="true">${STATUS[s].icon}</span>${esc(label)}${flag}</button>`;
}

function renderMatrix(tests) {
  if (!tests.length) { el.matrix.innerHTML = `<p class="empty">No tests match the filters.</p>`; return; }
  const systems = uniq(tests.map(systemKey)).sort();
  const workflows = uniq(tests.map((t) => t.workflow_name)).sort();
  let html = `<table class="matrix"><thead><tr><th scope="col">Workflow</th>` +
    systems.map((s) => `<th scope="col">${esc(s)}</th>`).join("") + `</tr></thead><tbody>`;
  for (const w of workflows) {
    html += `<tr><th scope="row">${esc(w)}</th>`;
    for (const s of systems) {
      const cell = tests.filter((t) => t.workflow_name === w && systemKey(t) === s);
      html += `<td>${cell.map((t) => matrixChip(t, cell)).join("")}</td>`;
    }
    html += `</tr>`;
  }
  el.matrix.innerHTML = html + `</tbody></table>`;
}

function renderTable(tests) {
  el["table-count"].textContent = `${tests.length} test${tests.length === 1 ? "" : "s"}`;
  if (!tests.length) { el["tests-body"].innerHTML = `<tr><td colspan="8" class="empty">No tests match the filters.</td></tr>`; return; }
  const order = { fail: 0, running: 1, pass: 2, skip: 3, none: 4 };
  const sorted = [...tests].sort((a, b) => (order[statusOf(a)] - order[statusOf(b)]) || a.id.localeCompare(b.id));
  el["tests-body"].innerHTML = sorted.map((t) => {
    const s = statusOf(t);
    const o = (t.current && t.current.outcome) || {};
    const commit = t.current && t.current.workflow && t.current.workflow.commit;
    return `<tr data-id="${esc(t.id)}" tabindex="0" aria-label="Open ${esc(t.id)}">
      <td class="status-cell">${chip(s)}${changeChip(t.change)}</td>
      <td><div><strong>${esc(t.workflow_name)}</strong> <span class="muted">/ ${esc(t.test)}</span></div><div class="muted small">${esc(t.platform)} · ${esc(t.user)}</div></td>
      <td>${esc(t.system || "–")}</td>
      <td>${esc(t.node || (t.type === "kubernetes" ? "kubernetes" : "–"))}</td>
      <td>${historyStrip(t.history)}</td>
      <td class="num">${fmtDuration(o.duration_s)}</td>
      <td title="${esc(o.started_at || "")}">${o.started_at ? relTime(o.started_at) : "–"}</td>
      <td class="mono">${commit ? esc(String(commit).slice(0, 10)) : "–"}</td>
    </tr>`;
  }).join("");
}

function renderSuites(runs) {
  if (!runs.length) { el["suites-body"].innerHTML = `<tr><td colspan="6" class="empty">No records yet.</td></tr>`; return; }
  el["suites-body"].innerHTML = runs.map((r) => `<tr>
    <td class="mono">${esc(r.suite_run)}</td><td title="${esc(r.started_at || "")}">${fmtDate(r.started_at)}</td>
    <td class="num">${r.tests}</td><td class="num">${r.pass}</td><td class="num${r.fail ? " is-bad" : ""}">${r.fail}</td><td class="num">${r.skip}</td></tr>`).join("");
}

function renderMeta(data) {
  const running = data.tests.filter((t) => t.running).length;
  el["top-meta"].textContent = `Updated ${fmtDate(data.generated_at)}${running ? ` · ${running} running` : ""}${data.admin ? " · admin" : ""}`;
  el["foot-version"].textContent = `PROBE ${data.version}`;
  el["foot-results"].textContent = data.bucket ? `Results bucket: ${data.bucket}` : (data.results_dir ? `Results: ${data.results_dir} (no bucket)` : "");
  el["run-all"].hidden = !data.admin;
  if (data.definition_errors && data.definition_errors.length) {
    notice(`${data.definition_errors.length} invalid test definition(s): ${data.definition_errors.join("; ")}`, "warn", "definitions");
  } else {
    notice("", "info", "definitions");
  }
}

function renderAll() {
  const data = state.data;
  if (!data) return;
  renderFilters(data.tests);
  const tests = data.tests.filter(matches);
  renderTiles(tests);
  renderMatrix(tests);
  renderTable(tests);
  renderSuites(data.suite_runs || []);
  renderMeta(data);
  if (state.selected) {
    const t = data.tests.find((x) => x.id === state.selected);
    if (t) fillDrawerHeader(t);
  }
}

// ---------------------------------------------------------------- data

async function load() {
  try {
    const response = await fetch(url("api/state"), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    document.body.classList.remove("is-stale");
    renderAll();
  } catch (error) {
    document.body.classList.add("is-stale");
    notice(`Could not load results: ${error.message}. Showing the last data received.`, "error");
    if (!state.data) el["tests-body"].innerHTML = `<tr><td colspan="8" class="empty">No data.</td></tr>`;
  } finally {
    schedule();
  }
}

function schedule() {
  clearTimeout(state.timer);
  const running = state.data && state.data.tests.some((t) => t.running);
  state.timer = setTimeout(load, running ? 10000 : 30000);
}

// ---------------------------------------------------------------- drawer

function fact(label, value, mono = false) {
  if (value == null || value === "") return "";
  return `<div><dt>${esc(label)}</dt><dd class="${mono ? "mono" : ""}">${esc(value)}</dd></div>`;
}

function fillDrawerHeader(test) {
  const s = statusOf(test);
  const rec = test.current || {};
  const o = rec.outcome || {};
  const w = rec.workflow || {};
  el["d-workflow"].textContent = `${test.platform} · ${test.user} · ${test.workflow_name}`;
  el["d-title"].textContent = test.test;
  el["d-chips"].innerHTML = chip(s) + changeChip(test.change) +
    (o.cleanup ? `<span class="chip chip-plain${o.cleanup === "leftover" ? " chip-warn" : ""}">cleanup ${esc(o.cleanup)}</span>` : "");
  const failedAt = o.failed_at ? ` (at ${o.failed_at})` : "";
  el["d-error"].hidden = !o.error;
  el["d-error"].textContent = o.error ? `${s === "skip" ? "Skipped" : "Error"}${failedAt}: ${o.error}` : "";
  el["d-facts"].innerHTML =
    fact("System", test.system) + fact("Node", test.node) + fact("Type", test.type) + fact("Resource", test.resource, true) +
    fact("Workflow", test.launch_target || (w.repo ? `${w.repo}/${w.path}@${w.ref}` : null), true) +
    fact("Commit", w.commit, true) + fact("Run", o.run_slug, true) + fact("Endpoint", o.endpoint, true) +
    fact("Phase", o.phase) + fact("Started", o.started_at ? fmtDate(o.started_at) : null) +
    fact("Duration", o.duration_s != null ? fmtDuration(o.duration_s) : null) + fact("Suite run", rec.suite_run, true) +
    fact("pw CLI", rec.pw_cli) + fact("Records", test.record_count) +
    (test.defined ? fact("Definition", test.definition_path, true) : `<div><dt>Definition</dt><dd class="is-bad">not in the tests directory</dd></div>`);
  const admin = state.data && state.data.admin;
  el["d-admin"].hidden = !admin;
  if (admin) {
    const runningSlug = (test.running_artifacts || []).map((n) => n.split("_").slice(1).join("_")).find((slug) => slug && slug !== "pending" && slug !== "launch-failed");
    el["d-cancel"].disabled = !runningSlug;
    el["d-cancel"].dataset.slug = runningSlug || "";
    el["d-cancel"].title = runningSlug ? `Cancel run ${runningSlug}` : "No run in progress";
    el["d-rerun"].disabled = !test.defined || test.running;
    // results of a test that no longer has a definition can be removed
    el["d-delete"].hidden = test.defined || test.running || !test.record_count;
    el["d-delete"].dataset.count = test.record_count || 0;
  }
  el["d-record"].textContent = test.current ? JSON.stringify(test.current, null, 2) : "No record yet.";
  renderHistory(state.records || (test.current ? null : []) || null, test);
}

function renderHistory(records, test) {
  const rows = records ? records.map((r) => ({
    started_at: r.outcome.started_at, status: r.outcome.status, failed_at: r.outcome.failed_at,
    duration_s: r.outcome.duration_s, run_slug: r.outcome.run_slug, commit: r.workflow && r.workflow.commit,
    suite_run: r.suite_run, error: r.outcome.error,
  })) : (test.history || []);
  if (!rows.length) { el["d-history"].innerHTML = `<tr><td colspan="8" class="empty">No executions yet.</td></tr>`; return; }
  el["d-history"].innerHTML = [...rows].reverse().map((h) => `<tr>
    <td title="${esc(h.started_at || "")}">${fmtDate(h.started_at)}</td><td>${chip(h.status || "none")}</td>
    <td>${esc(h.failed_at || "–")}</td><td class="num">${fmtDuration(h.duration_s)}</td>
    <td class="mono">${esc(h.run_slug || "–")}</td><td class="mono">${h.commit ? esc(String(h.commit).slice(0, 10)) : "–"}</td>
    <td class="mono small">${esc(h.suite_run || "")}</td><td class="small">${esc(h.error || "")}</td></tr>`).join("");
}

async function openDrawer(id) {
  const test = state.data && state.data.tests.find((t) => t.id === id);
  if (!test) return;
  state.selected = id;
  state.records = null;
  state.artifacts = [];
  writeFilters();
  selectTab("history");
  fillDrawerHeader(test);
  el.drawer.hidden = false;
  el["drawer-backdrop"].hidden = false;
  document.body.classList.add("drawer-open");
  el["d-close"].focus();
  el["d-log"].textContent = "Loading";
  el["d-definition"].textContent = "Loading";
  el["d-definition-path"].textContent = "";
  el["d-artifact"].innerHTML = "";
  el["d-file"].innerHTML = "";
  const encoded = id.split("/").map(encodeURIComponent).join("/");
  fetch(url(`api/tests/${encoded}/records`), { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((data) => {
    if (data && state.selected === id) { state.records = data.records; renderHistory(state.records, test); }
  }).catch(() => {});
  fetch(url(`api/tests/${encoded}/artifacts`), { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((data) => {
    if (!data || state.selected !== id) return;
    state.artifacts = data.artifacts || [];
    if (!state.artifacts.length) { el["d-log"].textContent = "No artifacts. Skipped tests and tests that never ran have none."; return; }
    el["d-artifact"].innerHTML = state.artifacts.map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
    fillFileSelect();
    loadArtifactFile();
  }).catch(() => { el["d-log"].textContent = "Could not list artifacts."; });
  fetch(url(`api/tests/${encoded}/definition`), { cache: "no-store" }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (state.selected !== id) return;
    if (!r.ok) { el["d-definition"].textContent = data.error || "No definition."; return; }
    el["d-definition-path"].textContent = data.path || "";
    el["d-definition"].textContent = JSON.stringify(data.definition, null, 2);
  }).catch(() => { el["d-definition"].textContent = "Could not load the definition."; });
}

function fillFileSelect() {
  const artifact = state.artifacts.find((a) => a.name === el["d-artifact"].value);
  const files = artifact ? artifact.files : [];
  const preferred = ["run.log", "errors.txt", "launch.json", "view.json"];
  const rank = (name) => { const i = preferred.indexOf(name); return i < 0 ? preferred.length : i; };
  const ordered = [...files].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  el["d-file"].innerHTML = ordered.map((f) => `<option value="${esc(f.name)}">${esc(f.name)} (${f.size} B)</option>`).join("");
}

async function loadArtifactFile() {
  const artifact = el["d-artifact"].value;
  const file = el["d-file"].value;
  if (!artifact || !file || !state.selected) { el["d-log"].textContent = "No files."; return; }
  const encoded = state.selected.split("/").map(encodeURIComponent).join("/");
  el["d-log"].textContent = "Loading";
  try {
    const response = await fetch(url(`api/tests/${encoded}/artifacts/${encodeURIComponent(artifact)}/${encodeURIComponent(file)}`), { cache: "no-store" });
    const text = await response.text();
    el["d-log"].textContent = response.ok ? (text || "(empty)") : `Could not load ${file}: HTTP ${response.status}`;
    if (response.headers.get("X-Truncated") === "true") el["d-log"].textContent = "[truncated to the last 4 MB]\n" + el["d-log"].textContent;
  } catch (error) {
    el["d-log"].textContent = `Could not load ${file}: ${error.message}`;
  }
}

function closeDrawer() {
  state.selected = null;
  writeFilters();
  el.drawer.hidden = true;
  el["drawer-backdrop"].hidden = true;
  document.body.classList.remove("drawer-open");
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
}

// ---------------------------------------------------------------- admin actions

async function post(path, body) {
  const response = await fetch(url(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

// Embedded in the platform's session view, window.confirm is blocked, so a
// destructive button asks for a second click within a few seconds instead.
function armed(button, label) {
  if (button.dataset.armed === "1") {
    clearTimeout(button._disarm);
    button.dataset.armed = "";
    button.textContent = button.dataset.label;
    return true;
  }
  button.dataset.label = button.dataset.label || button.textContent;
  button.dataset.armed = "1";
  button.textContent = label;
  button._disarm = setTimeout(() => {
    button.dataset.armed = "";
    button.textContent = button.dataset.label;
  }, 6000);
  return false;
}

function adminMessage(text, bad = false) {
  el["d-admin-msg"].textContent = text;
  el["d-admin-msg"].classList.toggle("is-bad", bad);
}

async function rerunSelected() {
  if (!state.selected) return;
  el["d-rerun"].disabled = true;
  adminMessage("Starting");
  try {
    await post("api/run", { ids: [state.selected] });
    adminMessage("Rerun started. The status updates when the runner writes its record.");
    setTimeout(load, 3000);
  } catch (error) {
    adminMessage(`Rerun failed: ${error.message}`, true);
    el["d-rerun"].disabled = false;
  }
}

async function cancelSelected() {
  const slug = el["d-cancel"].dataset.slug;
  const test = state.data.tests.find((t) => t.id === state.selected);
  if (!slug || !test) return;
  if (!armed(el["d-cancel"], `Click again to cancel ${slug}`)) return;
  el["d-cancel"].disabled = true;
  adminMessage("Canceling");
  try {
    await post("api/cancel", { slug, platform: test.platform });
    adminMessage(`Run ${slug} canceled. The runner records the result shortly.`);
    setTimeout(load, 5000);
  } catch (error) {
    adminMessage(`Cancel failed: ${error.message}`, true);
    el["d-cancel"].disabled = false;
  }
}

async function deleteSelected() {
  const test = state.data && state.data.tests.find((t) => t.id === state.selected);
  if (!test || test.defined) return;
  const count = el["d-delete"].dataset.count || test.record_count;
  if (!armed(el["d-delete"], `Click again to delete ${count} execution(s)`)) return;
  el["d-delete"].disabled = true;
  adminMessage("Deleting");
  try {
    const data = await post("api/delete", { id: test.id });
    notice(`Results of ${test.id} deleted (${data.executions} execution(s)).`, "info");
    closeDrawer();
    await load();
  } catch (error) {
    adminMessage(`Delete failed: ${error.message}`, true);
  } finally {
    el["d-delete"].disabled = false;
  }
}

async function runAll() {
  if (!armed(el["run-all"], "Click again to run every test")) return;
  el["run-all"].disabled = true;
  try {
    await post("api/run", { all: true });
    notice("Suite run started. Results appear as each test finishes.", "info");
    setTimeout(load, 3000);
  } catch (error) {
    notice(`Could not start the suite: ${error.message}`, "error");
  } finally {
    el["run-all"].disabled = false;
    el["run-all"].textContent = el["run-all"].dataset.label || "Run all";
  }
}

// ---------------------------------------------------------------- theme

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
  try { if (theme) window.localStorage.setItem("probe-theme", theme); else window.localStorage.removeItem("probe-theme"); } catch (e) { /* storage unavailable */ }
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme ||
    (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(current === "dark" ? "light" : "dark");
}

// ---------------------------------------------------------------- events

function onFilterChange() {
  readFilterControls();
  writeFilters();
  renderAll();
}

for (const id of ["f-platform", "f-user", "f-system", "f-workflow", "f-status", "f-changes"]) {
  el[id].addEventListener("change", onFilterChange);
}
let searchTimer = null;
el["f-search"].addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(onFilterChange, 150); });
el["f-clear"].addEventListener("click", () => {
  state.filters = { platform: "", user: "", system: "", workflow: "", status: "", search: "", changes: false, test: "" };
  writeFilters();
  renderAll();
});
el.refresh.addEventListener("click", async () => {
  el.refresh.disabled = true;
  if (state.data && state.data.bucket) {
    el.refresh.textContent = "Refreshing";
    try {
      await post("api/refresh", {});
      notice("");
    } catch (error) {
      notice(`Could not refresh from the bucket: ${error.message}`, "error");
    }
    el.refresh.textContent = "Refresh";
  }
  await load();
  el.refresh.disabled = false;
});
el.theme.addEventListener("click", toggleTheme);
el["run-all"].addEventListener("click", runAll);

document.addEventListener("click", (event) => {
  const chipButton = event.target.closest(".chip-btn[data-id]");
  if (chipButton) { openDrawer(chipButton.dataset.id); return; }
  const row = event.target.closest("tr[data-id]");
  if (row) openDrawer(row.dataset.id);
});
el["tests-body"].addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest("tr[data-id]");
  if (row) { event.preventDefault(); openDrawer(row.dataset.id); }
});
el["d-close"].addEventListener("click", closeDrawer);
el["drawer-backdrop"].addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && state.selected) closeDrawer(); });
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));
el["d-artifact"].addEventListener("change", () => { fillFileSelect(); loadArtifactFile(); });
el["d-file"].addEventListener("change", loadArtifactFile);
el["d-rerun"].addEventListener("click", rerunSelected);
el["d-cancel"].addEventListener("click", cancelSelected);
el["d-delete"].addEventListener("click", deleteSelected);
window.addEventListener("hashchange", () => { state.filters = readFilters(); renderAll(); });

load().then(() => { if (state.filters.test) openDrawer(state.filters.test); });
