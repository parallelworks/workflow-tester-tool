import { initThemeToggle, formatRelativeTime } from "./page-utils.js";

// Derive the API base URL from the <base> tag injected at page load.
// This makes all fetch() calls work whether the dashboard is served at / or
// behind a reverse proxy at an arbitrary path prefix like /me/session/foo/bar/.
function getApiBase() {
  const basePath = document.documentElement.dataset.basePath || "";
  if (basePath) return new URL(basePath, window.location.origin);
  const p = window.location.pathname;
  const dir = p.endsWith("/") ? p : p + "/";
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
    scheduleRefresh();
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
  const { total, passed, running, failed, pass_rate, last_run, status_counts, workflow_counts, kind_counts } = state.summary;

  if (el.totalTests)  el.totalTests.textContent  = total  ?? "--";
  if (el.passRate)    el.passRate.textContent     = total  ? `${pass_rate}%` : "--";
  if (el.lastRun) {
    el.lastRun.textContent = last_run ? formatRelativeTime(last_run) : "--";
    if (last_run) el.lastRun.title = new Date(last_run).toLocaleString();
  }

  if (el.failedCount) {
    el.failedCount.textContent = failed ?? "--";
  }
  if (el.failedCard) {
    el.failedCard.classList.toggle("alert", Number(failed) > 0);
  }

  // Show running count alongside the failed card label
  const failedLabel = el.failedCard?.querySelector("p");
  if (failedLabel) {
    failedLabel.textContent = Number(running) > 0 ? `Failed  (${running} running)` : "Failed";
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

  el.tableBody.innerHTML = rows.map(r => {
    const canCancel = window.IS_ADMIN && r.status === "running" && r.slug;
    const cancelBtn = canCancel
      ? `<button class="cancel-row-btn ghost-btn btn-sm danger"
             data-slug="${escapeHtml(r.slug)}"
             data-platform="${escapeHtml(r.platform)}"
             title="Cancel run ${escapeHtml(r.slug)}">✕ Cancel</button>`
      : "";
    return `
      <tr data-test-id="${escapeHtml(r.test)}" tabindex="0" role="button"
          aria-label="View test ${escapeHtml(r.test_name)}">
        <td><strong>${escapeHtml(r.test_name)}</strong></td>
        <td><span class="system-meta">${escapeHtml(r.workflow)}</span></td>
        <td>${kindChip(r.kind)}</td>
        <td class="status-cell">${statusBadge(r.status)}${cancelBtn}</td>
        <td>${formatDate(r.started_at)}</td>
        <td>${formatDuration(r.duration_s)}</td>
      </tr>
    `;
  }).join("");
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

  // Admin mode: show actions and update Cancel button state
  if (window.IS_ADMIN) {
    const adminActions = document.getElementById("admin-actions");
    if (adminActions) adminActions.removeAttribute("hidden");
    const cancelBtn = document.getElementById("cancel-btn");
    if (cancelBtn) {
      cancelBtn.disabled = !result.slug;
      cancelBtn.title = result.slug
        ? `Cancel PW run ${result.slug}`
        : "No run slug available to cancel";
    }
    const fb = document.getElementById("admin-feedback");
    if (fb) fb.setAttribute("hidden", "hidden");
  }

  el.overview.setAttribute("hidden", "hidden");
  el.detailPanel.removeAttribute("hidden");

  loadInputs(result.test);
}

function closeDetail() {
  state.activeTest = null;
  el.detailPanel.setAttribute("hidden", "hidden");
  el.overview.removeAttribute("hidden");
  if (window.IS_ADMIN) {
    const adminActions = document.getElementById("admin-actions");
    if (adminActions) adminActions.setAttribute("hidden", "hidden");
  }
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
    const cancelBtn = e.target.closest(".cancel-row-btn");
    if (cancelBtn) {
      e.stopPropagation();
      handleCancelRow(cancelBtn);
      return;
    }
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

// ── Admin actions (admin.html only) ──────────────────────────────────────────

async function handleCancelRow(btn) {
  const { slug, platform } = btn.dataset;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Canceling…";
  try {
    const resp = await fetch(new URL("api/cancel", API_BASE).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, platform }),
    });
    const data = await resp.json();
    if (resp.ok) {
      btn.textContent = "Canceled";
      setTimeout(() => loadData({ showLoading: false }), 2000);
    } else {
      btn.disabled = false;
      btn.textContent = origText;
      showAdminFeedback(`Cancel failed: ${escapeHtml(data.error || "Unknown error")}`, "error");
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = origText;
    showAdminFeedback(`Network error: ${escapeHtml(err.message)}`, "error");
  }
}

function showAdminFeedback(msg, variant = "info") {
  const detailOpen = el.detailPanel && !el.detailPanel.hasAttribute("hidden");
  const bannerId = detailOpen ? "admin-feedback" : "admin-global-feedback";
  const banner = document.getElementById(bannerId);
  if (!banner) return;
  banner.dataset.variant = variant;
  banner.textContent = msg;
  banner.removeAttribute("hidden");
  clearTimeout(banner._timer);
  banner._timer = setTimeout(() => banner.setAttribute("hidden", "hidden"), 6000);
}

async function handleRerunAll() {
  const btn = document.getElementById("rerun-all-btn");
  if (!btn) return;
  const origInner = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">▶</span><span>Launching…</span>';
  try {
    const resp = await fetch(new URL("api/run-all", API_BASE).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await resp.json();
    if (resp.ok) {
      showAdminFeedback(
        `All tests launched${data.platform ? ` for ${escapeHtml(data.platform)}` : ""}. Results will update as they complete.`,
        "success"
      );
    } else {
      showAdminFeedback(`Error: ${escapeHtml(data.error || "Unknown error")}`, "error");
    }
  } catch (err) {
    showAdminFeedback(`Network error: ${escapeHtml(err.message)}`, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = origInner;
  }
}

async function handleRerun() {
  if (!state.activeTest) return;
  const result = state.results.find(r => r.test === state.activeTest);
  if (!result) return;

  const btn = document.getElementById("rerun-btn");
  if (!btn) return;
  const origInner = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">↻</span><span>Launching…</span>';
  btn.classList.add("is-loading");

  try {
    const resp = await fetch(new URL("api/run-test", API_BASE).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ test: result.test }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showAdminFeedback("Test rerun launched. Results will update automatically.", "success");
    } else {
      showAdminFeedback(`Error: ${escapeHtml(data.error || "Unknown error")}`, "error");
    }
  } catch (err) {
    showAdminFeedback(`Network error: ${escapeHtml(err.message)}`, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = origInner;
    btn.classList.remove("is-loading");
  }
}

async function handleCancel() {
  if (!state.activeTest) return;
  const result = state.results.find(r => r.test === state.activeTest);
  if (!result?.slug) {
    showAdminFeedback("No run slug available to cancel.", "error");
    return;
  }

  const btn = document.getElementById("cancel-btn");
  if (!btn) return;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Canceling…";

  try {
    const resp = await fetch(new URL("api/cancel", API_BASE).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: result.slug, platform: result.platform }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showAdminFeedback(`Run ${escapeHtml(result.slug)} has been canceled.`, "success");
      setTimeout(() => loadData({ showLoading: false }), 3000);
    } else {
      showAdminFeedback(`Error: ${escapeHtml(data.error || "Cancel failed")}`, "error");
    }
  } catch (err) {
    showAdminFeedback(`Network error: ${escapeHtml(err.message)}`, "error");
  } finally {
    btn.disabled = !result.slug;
    btn.textContent = origText;
  }
}

function registerAdminEvents() {
  const rerunAllBtn = document.getElementById("rerun-all-btn");
  if (rerunAllBtn) rerunAllBtn.addEventListener("click", handleRerunAll);
  const rerunBtn = document.getElementById("rerun-btn");
  if (rerunBtn) rerunBtn.addEventListener("click", handleRerun);
  const cancelBtn = document.getElementById("cancel-btn");
  if (cancelBtn) cancelBtn.addEventListener("click", handleCancel);
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
// Poll every 10 s while any test is running; fall back to 3 min when idle.
let _refreshTimer = null;

function scheduleRefresh() {
  clearTimeout(_refreshTimer);
  const hasRunning = state.results.some(r => r.status === "running");
  _refreshTimer = setTimeout(
    () => loadData({ showLoading: false }),
    hasRunning ? 10_000 : 3 * 60 * 1000
  );
}

// ── Init ──────────────────────────────────────────────────────────────────────
initThemeToggle();
registerEvents();
if (window.IS_ADMIN) registerAdminEvents();
loadData();
