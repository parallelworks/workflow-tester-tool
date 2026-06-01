import { initThemeToggle, formatRelativeTime } from "./page-utils.js";

// Derive the API base URL from the <base> tag injected at page load.
// This makes all fetch() calls work whether the dashboard is served at / or
// behind a reverse proxy at an arbitrary path prefix like /me/session/foo/bar/.
function getApiBase() {
  const basePath = document.documentElement.dataset.basePath || "";
  if (basePath) return new URL(basePath, window.location.origin);
  const p = window.location.pathname;
  const dir = p.endsWith("/") ? p : (p.slice(0, p.lastIndexOf("/") + 1) || "/");
  return new URL(dir, window.location.origin);
}

const API_BASE = getApiBase();
const RESULTS_URL = new URL("api/results", API_BASE).toString();

const state = {
  results: [],
  summary: {},
  loading: false,
  activeTest: null,
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
const el = {
  totalTests:         document.getElementById("total-tests"),
  passRate:           document.getElementById("pass-rate"),
  failedCount:        document.getElementById("failed-count"),
  failedCard:         document.getElementById("failed-card"),
  lastRun:            document.getElementById("last-run"),
  statusBreakdown:    document.getElementById("status-breakdown"),
  workflowBreakdown:  document.getElementById("workflow-breakdown"),
  kindBreakdown:      document.getElementById("kind-breakdown"),
  tableBody:          document.getElementById("tests-body"),
  tableCount:         document.getElementById("table-count"),
  searchInput:        document.getElementById("search-input"),
  statusFilter:       document.getElementById("status-filter"),
  workflowFilter:     document.getElementById("workflow-filter"),
  kindFilter:         document.getElementById("kind-filter"),
  refreshBtn:         document.getElementById("refresh-btn"),
  overview:           document.getElementById("overview-content"),
  detailPanel:        document.getElementById("test-detail"),
  detailBack:         document.getElementById("detail-back"),
  detailTitle:        document.getElementById("detail-title"),
  detailWorkflow:     document.getElementById("detail-workflow"),
  detailStatus:       document.getElementById("detail-status"),
  detailDuration:     document.getElementById("detail-duration"),
  detailKind:         document.getElementById("detail-kind"),
  detailPlatform:     document.getElementById("detail-platform"),
  detailSlug:         document.getElementById("detail-slug"),
  detailDate:         document.getElementById("detail-date"),
  detailErrorBanner:  document.getElementById("detail-error-banner"),
  detailInputs:       document.getElementById("detail-inputs"),
  detailInputsStatus: document.getElementById("detail-inputs-status"),
  footerGenerated:    document.getElementById("footer-generated"),
};

// ── Utilities ─────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return (str || "").replace(/[&<>"']/g, (ch) => {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return map[ch] || ch;
  });
}

function formatDate(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

function statusBadge(status) {
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}

function kindChip(kind) {
  return `<span class="kind-chip ${escapeHtml(kind)}">${escapeHtml(kind)}</span>`;
}

function debounce(fn, ms = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── JSON syntax highlighter ───────────────────────────────────────────────────
function highlightJson(obj) {
  const raw = JSON.stringify(obj, null, 2);
  return raw.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (match) => {
      let cls = "json-number";
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? "json-key" : "json-string";
      } else if (/true|false/.test(match)) {
        cls = "json-bool";
      } else if (/null/.test(match)) {
        cls = "json-null";
      }
      return `<span class="${cls}">${escapeHtml(match)}</span>`;
    }
  );
}

// ── Data loading ──────────────────────────────────────────────────────────────
async function loadData({ showLoading = true } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (showLoading) {
    el.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">Loading…</td></tr>`;
    el.tableCount.textContent = "Loading…";
  }
  try {
    const resp = await fetch(RESULTS_URL, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.results = data.results || [];
    state.summary = data.summary || {};
    updateSummary();
    populateFilters();
    renderTable();
    if (el.footerGenerated && data.generated_at) {
      el.footerGenerated.textContent = `Last fetched: ${formatDate(data.generated_at)}`;
    }
  } catch (err) {
    el.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">Error: ${escapeHtml(err.message)}</td></tr>`;
    el.tableCount.textContent = "Error";
  } finally {
    state.loading = false;
  }
}

async function loadInputs(testPath) {
  el.detailInputs.innerHTML = "<code>Loading…</code>";
  if (el.detailInputsStatus) el.detailInputsStatus.textContent = "";
  try {
    const encodedPath = testPath.split("/").map(encodeURIComponent).join("/");
    const url = new URL(`api/results/${encodedPath}/inputs`, API_BASE).toString();
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: "Not found" }));
      el.detailInputs.innerHTML = `<code class="placeholder">${escapeHtml(err.error || "Not found")}</code>`;
      return;
    }
    const data = await resp.json();
    el.detailInputs.innerHTML = `<code>${highlightJson(data.inputs)}</code>`;
  } catch (err) {
    el.detailInputs.innerHTML = `<code class="placeholder">${escapeHtml(err.message)}</code>`;
  }
}

// ── Summary & breakdowns ──────────────────────────────────────────────────────
function updateSummary() {
  const { total, passed, failed, pass_rate, last_run, status_counts, workflow_counts, kind_counts } = state.summary;

  if (el.totalTests)  el.totalTests.textContent  = total  ?? "--";
  if (el.passRate)    el.passRate.textContent     = total  ? `${pass_rate}%` : "--";
  if (el.failedCount) el.failedCount.textContent  = failed ?? "--";

  // Highlight failed card in danger when there are failures
  if (el.failedCard) {
    el.failedCard.classList.toggle("alert", Number(failed) > 0);
  }

  if (el.lastRun) {
    el.lastRun.textContent = last_run ? formatRelativeTime(last_run) : "--";
    if (last_run) el.lastRun.title = new Date(last_run).toLocaleString();
  }

  buildLegend(el.statusBreakdown, status_counts);
  buildLegend(el.workflowBreakdown, workflow_counts);
  buildLegend(el.kindBreakdown, kind_counts);
}

function buildLegend(container, counts = {}) {
  if (!container) return;
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) { container.innerHTML = "<li>No data</li>"; return; }
  container.innerHTML = entries
    .map(([label, value]) => `<li><span>${escapeHtml(label)}</span>${value}</li>`)
    .join("");
}

function populateFilters() {
  const statuses  = [...new Set(state.results.map(r => r.status))].sort();
  const workflows = [...new Set(state.results.map(r => r.workflow))].sort();

  const fill = (sel, values) => {
    const cur = sel.value;
    sel.innerHTML = `<option value="">All</option>` +
      values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
    if (values.includes(cur)) sel.value = cur;
  };

  fill(el.statusFilter, statuses);
  fill(el.workflowFilter, workflows);
}

// ── Table ─────────────────────────────────────────────────────────────────────
function filteredResults() {
  const q  = (el.searchInput.value || "").toLowerCase();
  const sf = el.statusFilter.value;
  const wf = el.workflowFilter.value;
  const kf = el.kindFilter.value;

  return state.results.filter(r => {
    if (q  && !r.test_name.toLowerCase().includes(q) && !r.workflow.toLowerCase().includes(q)) return false;
    if (sf && r.status   !== sf) return false;
    if (wf && r.workflow !== wf) return false;
    if (kf && r.kind     !== kf) return false;
    return true;
  });
}

function renderTable() {
  const rows = filteredResults();
  el.tableCount.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"}`;

  if (!rows.length) {
    el.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">No tests match the filters.</td></tr>`;
    return;
  }

  el.tableBody.innerHTML = rows.map(r => `
    <tr data-test-id="${escapeHtml(r.test)}" tabindex="0" role="button"
        aria-label="View test ${escapeHtml(r.test_name)}">
      <td><strong>${escapeHtml(r.test_name)}</strong></td>
      <td><span class="system-meta">${escapeHtml(r.workflow)}</span></td>
      <td>${kindChip(r.kind)}</td>
      <td>${statusBadge(r.status)}</td>
      <td>${formatDate(r.started_at)}</td>
      <td>${formatDuration(r.duration_s)}</td>
    </tr>
  `).join("");
}

// ── Detail panel ──────────────────────────────────────────────────────────────
function showDetail(testId) {
  const result = state.results.find(r => r.test === testId);
  if (!result) return;
  state.activeTest = testId;

  if (el.detailTitle)    el.detailTitle.textContent    = result.test_name;
  if (el.detailWorkflow) el.detailWorkflow.textContent = result.workflow;
  if (el.detailStatus) {
    el.detailStatus.className   = `badge ${result.status}`;
    el.detailStatus.textContent = result.status;
  }
  if (el.detailDuration) el.detailDuration.textContent = `Duration: ${formatDuration(result.duration_s)}`;
  if (el.detailKind)     el.detailKind.textContent     = result.kind;
  if (el.detailPlatform) el.detailPlatform.textContent = result.platform;
  if (el.detailSlug)     el.detailSlug.textContent     = result.slug || "—";
  if (el.detailDate)     el.detailDate.textContent     = formatDate(result.started_at);

  if (el.detailErrorBanner) {
    if (result.error) {
      el.detailErrorBanner.textContent = result.error;
      el.detailErrorBanner.removeAttribute("hidden");
    } else {
      el.detailErrorBanner.setAttribute("hidden", "hidden");
    }
  }

  el.overview.setAttribute("hidden", "hidden");
  el.detailPanel.removeAttribute("hidden");

  loadInputs(result.test);
}

function closeDetail() {
  state.activeTest = null;
  el.detailPanel.setAttribute("hidden", "hidden");
  el.overview.removeAttribute("hidden");
}

// ── Events ────────────────────────────────────────────────────────────────────
function registerEvents() {
  el.searchInput.addEventListener("input", debounce(renderTable, 150));
  el.statusFilter.addEventListener("change", renderTable);
  el.workflowFilter.addEventListener("change", renderTable);
  el.kindFilter.addEventListener("change", renderTable);

  el.refreshBtn.addEventListener("click", async () => {
    const orig = el.refreshBtn.innerHTML;
    el.refreshBtn.disabled = true;
    el.refreshBtn.innerHTML = '<span class="btn-icon">↻</span><span>Refreshing…</span>';
    el.refreshBtn.classList.add("is-loading");
    await loadData({ showLoading: false });
    el.refreshBtn.disabled = false;
    el.refreshBtn.innerHTML = orig;
    el.refreshBtn.classList.remove("is-loading");
  });

  el.tableBody.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-test-id]");
    if (row) showDetail(row.dataset.testId);
  });

  el.tableBody.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest("tr[data-test-id]");
    if (row) { e.preventDefault(); showDetail(row.dataset.testId); }
  });

  el.detailBack.addEventListener("click", closeDetail);

  // Close detail on Escape
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.activeTest) closeDetail();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
initThemeToggle();
registerEvents();
loadData();
// Auto-refresh every 3 minutes
setInterval(() => loadData({ showLoading: false }), 3 * 60 * 1000);
