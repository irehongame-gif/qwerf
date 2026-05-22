/* qwerf popup — talks to the local ymsync-server. */

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const SEARCH_DEBOUNCE_MS = 350;
const TASK_POLL_MS = 800;

const elements = {
  form: document.getElementById("search-form"),
  q: document.getElementById("q"),
  clear: document.getElementById("clear"),
  results: document.getElementById("results"),
  placeholder: document.getElementById("placeholder"),
  template: document.getElementById("result-template"),
  footer: document.getElementById("footer"),
  footerText: document.getElementById("footer-text"),
  settingsToggle: document.getElementById("settings-toggle"),
  settings: document.getElementById("settings-panel"),
  serverUrl: document.getElementById("server-url"),
  settingsTest: document.getElementById("settings-test"),
  settingsSave: document.getElementById("settings-save"),
  settingsStatus: document.getElementById("settings-status"),
};

/** App state. Map of trackId -> active poll handle so we can cancel on rerender. */
const activePolls = new Map();
let lastSearchToken = 0;

// ---------------------------------------------------------------------------
// Settings (server URL)
// ---------------------------------------------------------------------------

async function loadServerUrl() {
  const stored = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  return (stored.serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function saveServerUrl(url) {
  await chrome.storage.sync.set({ serverUrl: url.replace(/\/+$/, "") });
}

async function refreshSettingsField() {
  elements.serverUrl.value = await loadServerUrl();
}

elements.settingsToggle.addEventListener("click", () => {
  elements.settings.classList.toggle("hidden");
  if (!elements.settings.classList.contains("hidden")) {
    elements.serverUrl.focus();
  }
});

elements.settingsSave.addEventListener("click", async () => {
  const url = (elements.serverUrl.value || "").trim() || DEFAULT_SERVER;
  await saveServerUrl(url);
  setSettingsStatus(`Saved ${url}`, "ok");
});

elements.settingsTest.addEventListener("click", async () => {
  const url = (elements.serverUrl.value || "").trim() || DEFAULT_SERVER;
  setSettingsStatus("Connecting…", "muted");
  try {
    const r = await fetch(`${url}/health`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    setSettingsStatus(`OK — server v${data.version}`, "ok");
  } catch (err) {
    setSettingsStatus(`Failed: ${err.message}`, "err");
  }
});

function setSettingsStatus(text, level) {
  elements.settingsStatus.textContent = text;
  elements.settingsStatus.className = `status ${level || "muted"}`;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

let searchTimer = null;

elements.q.addEventListener("input", () => {
  elements.clear.classList.toggle("hidden", !elements.q.value);
  if (searchTimer) clearTimeout(searchTimer);
  const term = elements.q.value.trim();
  if (!term) {
    showPlaceholder();
    return;
  }
  searchTimer = setTimeout(() => runSearch(term), SEARCH_DEBOUNCE_MS);
});

elements.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (searchTimer) clearTimeout(searchTimer);
  const term = elements.q.value.trim();
  if (term) runSearch(term);
});

elements.clear.addEventListener("click", () => {
  elements.q.value = "";
  elements.clear.classList.add("hidden");
  showPlaceholder();
  elements.q.focus();
});

async function runSearch(term) {
  const token = ++lastSearchToken;
  cancelAllPolls();
  showFooter("Searching…");
  hidePlaceholder();
  renderEmptyList();

  const server = await loadServerUrl();
  let hits;
  try {
    const url = `${server}/search?q=${encodeURIComponent(term)}&limit=30`;
    const resp = await fetch(url);
    if (!resp.ok) {
      const detail = await safeReadError(resp);
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    hits = await resp.json();
  } catch (err) {
    if (token !== lastSearchToken) return;
    showFooter(`Could not reach ${server} — ${err.message}`);
    showPlaceholderError(server, err);
    return;
  }
  if (token !== lastSearchToken) return;
  renderResults(hits);
}

async function safeReadError(resp) {
  try {
    const j = await resp.json();
    return j.detail || j.message;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderEmptyList() {
  // Replace results body with an empty <ul>; placeholder is hidden separately.
  elements.results.innerHTML = "";
  const ul = document.createElement("ul");
  elements.results.appendChild(ul);
  return ul;
}

function renderResults(hits) {
  const ul = renderEmptyList();
  if (!hits.length) {
    showFooter("No results.");
    return;
  }
  hideFooter();

  for (const hit of hits) {
    ul.appendChild(renderRow(hit));
  }
}

function renderRow(hit) {
  const node = elements.template.content.firstElementChild.cloneNode(true);
  node.dataset.trackId = hit.id;
  if (!hit.available) node.classList.add("unavailable");

  const cover = node.querySelector(".cover");
  const coverImg = cover.querySelector("img");
  cover.href = hit.yandex_url;
  cover.title = "Open in Yandex.Music";
  if (hit.cover_url) {
    coverImg.src = hit.cover_url;
  }

  const meta = node.querySelector(".meta");
  meta.href = hit.yandex_url;
  meta.title = "Open in Yandex.Music";
  meta.querySelector(".title").textContent = hit.title || "(untitled)";
  meta.querySelector(".artists").textContent =
    (hit.artists || []).join(", ") || "Unknown artist";

  const dl = node.querySelector(".dl");
  if (hit.already_exported) {
    setDlState(dl, "skipped", "In library");
    dl.disabled = true;
  } else if (!hit.available) {
    setDlState(dl, "error", "Unavailable");
    dl.disabled = true;
  } else {
    setDlState(dl, "idle", "Download");
    dl.addEventListener("click", () => onDownloadClicked(hit, dl));
  }
  return node;
}

function setDlState(btn, state, label) {
  btn.dataset.state = state;
  btn.querySelector(".dl-label").textContent = label;
  if (state === "idle") {
    btn.disabled = false;
    btn.title = "";
  } else {
    btn.disabled = state !== "error";
    btn.title = label;
  }
}

// ---------------------------------------------------------------------------
// Download / polling
// ---------------------------------------------------------------------------

async function onDownloadClicked(hit, btn) {
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  let task;
  try {
    const resp = await fetch(`${server}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: String(hit.id) }),
    });
    if (!resp.ok) {
      const detail = await safeReadError(resp);
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    task = await resp.json();
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 40)}`);
    return;
  }
  pollTask(server, task, btn, hit);
}

const STAGE_LABELS = {
  pending: "Queued…",
  downloading: "Downloading…",
  converting: "Converting…",
  done: "In library",
  skipped: "In library",
  error: "Error",
};

function pollTask(server, task, btn, hit) {
  cancelPoll(hit.id);

  const tick = async () => {
    try {
      const r = await fetch(`${server}/tasks/${task.id}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const t = await r.json();
      const label = STAGE_LABELS[t.stage] || t.stage;
      if (t.stage === "error") {
        setDlState(btn, "error", `Error: ${(t.error || "").slice(0, 60)}`);
        cancelPoll(hit.id);
        return;
      }
      if (t.stage === "done" || t.stage === "skipped") {
        setDlState(btn, t.stage, label);
        cancelPoll(hit.id);
        return;
      }
      setDlState(btn, t.stage, label);
    } catch (err) {
      setDlState(btn, "error", `Error: ${err.message.slice(0, 40)}`);
      cancelPoll(hit.id);
      return;
    }
    activePolls.set(hit.id, setTimeout(tick, TASK_POLL_MS));
  };
  activePolls.set(hit.id, setTimeout(tick, 0));
}

function cancelPoll(trackId) {
  const handle = activePolls.get(trackId);
  if (handle) {
    clearTimeout(handle);
    activePolls.delete(trackId);
  }
}

function cancelAllPolls() {
  for (const handle of activePolls.values()) clearTimeout(handle);
  activePolls.clear();
}

// ---------------------------------------------------------------------------
// Placeholder / footer helpers
// ---------------------------------------------------------------------------

function showPlaceholder() {
  elements.results.innerHTML = "";
  elements.placeholder.classList.remove("hidden");
  elements.results.appendChild(elements.placeholder);
  hideFooter();
}

function hidePlaceholder() {
  elements.placeholder.classList.add("hidden");
}

function showPlaceholderError(server, err) {
  elements.results.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "placeholder";
  wrap.innerHTML = `
    <div class="hero">
      <p class="hero-title">Couldn't reach the listener</p>
      <p class="hero-sub">
        Tried <code>${escapeHtml(server)}</code>.<br/>
        ${escapeHtml(err.message || "")}<br/><br/>
        Start it with <code>ymsync-server</code> on your Mac, then try again.
      </p>
    </div>`;
  elements.results.appendChild(wrap);
}

function showFooter(text) {
  elements.footerText.textContent = text;
  elements.footer.classList.remove("hidden");
}

function hideFooter() {
  elements.footer.classList.add("hidden");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

(async function init() {
  await refreshSettingsField();
  // Quick health probe so we can give immediate feedback if the server is down.
  const server = await loadServerUrl();
  try {
    const r = await fetch(`${server}/health`, { method: "GET" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (err) {
    showFooter(`Listener offline at ${server}`);
  }
})();
