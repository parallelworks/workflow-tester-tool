const THEME_STORAGE_KEY = "wf-test-theme";

const safeGetStoredTheme = () => {
  try { return window.localStorage.getItem(THEME_STORAGE_KEY); }
  catch { return null; }
};

const safeSetStoredTheme = (value) => {
  try { window.localStorage.setItem(THEME_STORAGE_KEY, value); }
  catch { /* ignore */ }
};

const resolveDefaultTheme = () =>
  document.documentElement.dataset.theme || "dark";

const updateThemeToggleIndicator = (theme) => {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  const label = toggle.querySelector(".theme-label");
  const icon = toggle.querySelector(".theme-icon");
  if (label) label.textContent = theme === "dark" ? "Dark" : "Light";
  if (icon) icon.textContent = theme === "dark" ? "🌙" : "☀️";
  toggle.setAttribute("data-theme", theme);
};

const applyTheme = (theme, { persist = true } = {}) => {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  if (document.body) document.body.dataset.theme = normalized;
  if (persist) safeSetStoredTheme(normalized);
  updateThemeToggleIndicator(normalized);
  return normalized;
};

export const initThemeToggle = () => {
  const initial = safeGetStoredTheme() || resolveDefaultTheme();
  applyTheme(initial, { persist: false });
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
    });
  }
};

export const formatRelativeTime = (timestamp) => {
  if (!timestamp) return "Unknown";
  const now = Date.now();
  const then = new Date(timestamp).getTime();
  if (isNaN(then)) return "Unknown";
  const diffSeconds = Math.floor((now - then) / 1000);
  if (diffSeconds < 60) return "Just now";
  if (diffSeconds < 120) return "1 minute ago";
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} minutes ago`;
  if (diffSeconds < 7200) return "1 hour ago";
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} hours ago`;
  return `${Math.floor(diffSeconds / 86400)} days ago`;
};
