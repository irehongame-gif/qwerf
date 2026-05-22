/* qwerf popup — talks to the local ymsync-server.
 *
 * Layout / behaviour:
 *   1. On open we read the active tab URL.
 *      - If it looks like a YouTube video, we render a YouTube context card
 *        with audio + per-resolution video buttons.
 *      - If it looks like a music.yandex.ru track page, we render the track
 *        as a single result row with a Download button.
 *   2. The search bar always works regardless of context. Search results
 *      stack below the context card.
 *   3. Every download (search hit, Yandex context, YouTube audio, YouTube
 *      video) goes through the listener's task system, polled at /tasks/{id}.
 */

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const SEARCH_DEBOUNCE_MS = 350;
const TASK_POLL_MS = 800;

// ---------------------------------------------------------------------------
// Element handles
// ---------------------------------------------------------------------------

const elements = {
  form: document.getElementById("search-form"),
  q: document.getElementById("q"),
  clear: document.getElementById("clear"),
  results: document.getElementById("results"),
  placeholder: document.getElementById("placeholder"),
  resultTemplate: document.getElementById("result-template"),
  ytTemplate: document.getElementById("yt-context-template"),
  contextLoadingTpl: document.getElementById("context-loading-template"),
  contextErrorTpl: document.getElementById("context-error-template"),
  contextSection: document.getElementById("context"),
  contextBody: document.getElementById("context-body"),
  contextLabel: document.getElementById("context-label"),
  footer: document.getElementById("footer"),
  footerText: document.getElementById("footer-text"),
  settingsToggle: document.getElementById("settings-toggle"),
  settings: document.getElementById("settings-panel"),
  serverUrl: document.getElementById("server-url"),
  settingsTest: document.getElementById("settings-test"),
  settingsSave: document.getElementById("settings-save"),
  settingsStatus: document.getElementById("settings-status"),
};

// Map of poll-key -> setTimeout handle. Keys are stable strings:
//   yandex:<trackId>            (search results & yandex context)
//   yt-audio:<url>              (YouTube audio)
//   yt-video:<url>#h=<height>   (YouTube video, matches server dedup_key)
const activePolls = new Map();
let lastSearchToken = 0;

// ---------------------------------------------------------------------------
// Settings
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
// Active-tab context detection
// ---------------------------------------------------------------------------

const YANDEX_HOSTS = new Set([
  "music.yandex.ru",
  "music.yandex.com",
  "music.yandex.by",
  "music.yandex.kz",
  "music.yandex.tj",
  "music.yandex.ua",
]);

function detectContext(rawUrl) {
  if (!rawUrl) return null;
  let u;
  try {
    u = new URL(rawUrl);
  } catch {
    return null;
  }

  // YouTube — watch / shorts / youtu.be
  const ytHost = u.hostname.replace(/^www\./, "").replace(/^m\./, "");
  if (ytHost === "youtube.com") {
    if (u.pathname === "/watch") {
      const v = u.searchParams.get("v");
      if (v) return { kind: "youtube", url: youtubeWatchUrl(v) };
    }
    if (u.pathname.startsWith("/shorts/")) {
      const v = u.pathname.split("/")[2];
      if (v) return { kind: "youtube", url: youtubeWatchUrl(v) };
    }
  }
  if (ytHost === "youtu.be") {
    const v = u.pathname.slice(1).split("/")[0];
    if (v) return { kind: "youtube", url: youtubeWatchUrl(v) };
  }

  // Yandex.Music track page
  if (YANDEX_HOSTS.has(u.hostname)) {
    const m = u.pathname.match(/(?:^|\/)(?:album\/(\d+)\/)?track\/(\d+)/);
    if (m) {
      // Re-canonicalise so the server / link always sees a stable form.
      const albumPart = m[1] ? `album/${m[1]}/` : "";
      return {
        kind: "yandex_track",
        url: `https://${u.hostname}/${albumPart}track/${m[2]}`,
        trackId: m[2],
      };
    }
  }

  return null;
}

function youtubeWatchUrl(videoId) {
  return `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`;
}

async function getActiveTabUrl() {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    return tabs && tabs[0] ? tabs[0].url || null : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Context rendering
// ---------------------------------------------------------------------------

async function renderContext() {
  const url = await getActiveTabUrl();
  const ctx = detectContext(url);
  if (!ctx) {
    elements.contextSection.classList.add("hidden");
    return;
  }

  elements.contextSection.classList.remove("hidden");
  elements.contextLabel.textContent =
    ctx.kind === "youtube" ? "From this YouTube page" : "From this Yandex.Music page";
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(elements.contextLoadingTpl.content.cloneNode(true));

  const server = await loadServerUrl();
  try {
    if (ctx.kind === "youtube") {
      await renderYoutubeContext(server, ctx.url);
    } else if (ctx.kind === "yandex_track") {
      await renderYandexContext(server, ctx);
    }
  } catch (err) {
    showContextError(err.message);
  }
}

function showContextError(message) {
  const node = elements.contextErrorTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".status").textContent = message;
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(node);
}

async function renderYandexContext(server, ctx) {
  const url = `${server}/yandex/track-info?url=${encodeURIComponent(ctx.url)}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(
      (await safeReadError(resp)) || `Lookup failed (HTTP ${resp.status})`
    );
  }
  const hit = await resp.json();

  // Render with the same row template the search results use, wrapped in a
  // <ul> for layout symmetry.
  const ul = document.createElement("ul");
  ul.appendChild(renderResultRow(hit));
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(ul);
}

async function renderYoutubeContext(server, url) {
  const resp = await fetch(`${server}/youtube/info?url=${encodeURIComponent(url)}`);
  if (!resp.ok) {
    throw new Error(
      (await safeReadError(resp)) || `yt-dlp lookup failed (HTTP ${resp.status})`
    );
  }
  const info = await resp.json();

  const node = elements.ytTemplate.content.firstElementChild.cloneNode(true);

  const thumb = node.querySelector(".yt-thumb");
  thumb.href = info.url;
  if (info.thumbnail_url) {
    thumb.querySelector("img").src = info.thumbnail_url;
  }

  const titleA = node.querySelector(".yt-title");
  titleA.href = info.url;
  titleA.textContent = info.title || "Untitled";
  titleA.title = info.title || "";

  node.querySelector(".yt-channel").textContent = info.channel || "Unknown channel";
  node.querySelector(".yt-duration").textContent = formatDuration(info.duration_s);
  if (!info.duration_s) {
    node.querySelector(".yt-duration").classList.add("hidden");
    node.querySelector(".yt-dot").classList.add("hidden");
  }

  // Quality dropdown: descending heights.
  const select = node.querySelector(".yt-quality");
  if (Array.isArray(info.available_heights) && info.available_heights.length) {
    for (const h of info.available_heights) {
      const opt = document.createElement("option");
      opt.value = String(h);
      opt.textContent = `${h}p`;
      select.appendChild(opt);
    }
    // Default to the highest available — that's what most people want.
    select.value = String(info.available_heights[0]);
  } else {
    const opt = document.createElement("option");
    opt.textContent = "—";
    select.appendChild(opt);
    select.disabled = true;
    node.querySelector(".yt-video").disabled = true;
  }

  // Audio button.
  const audioBtn = node.querySelector(".yt-audio");
  if (info.audio_already_exported) {
    setDlState(audioBtn, "skipped", "In library");
    audioBtn.disabled = true;
  } else {
    setDlState(audioBtn, "idle", "Audio");
    audioBtn.addEventListener("click", () => onYoutubeAudioClicked(info.url, audioBtn));
  }

  // Video button.
  const videoBtn = node.querySelector(".yt-video");
  if (!videoBtn.disabled) {
    setDlState(videoBtn, "idle", "Video");
    videoBtn.addEventListener("click", () =>
      onYoutubeVideoClicked(info.url, parseInt(select.value, 10), videoBtn)
    );
  }

  // If the user changes resolution mid-flight, drop any in-progress poll for
  // this URL+height so the button state can be reused for the new selection.
  select.addEventListener("change", () => {
    if (videoBtn.dataset.state !== "idle" && videoBtn.dataset.state !== "skipped") {
      // Clear poll for whichever height was last clicked; safest to clear all
      // yt-video polls for this URL.
      for (const key of [...activePolls.keys()]) {
        if (key.startsWith(`yt-video:${info.url}`)) cancelPoll(key);
      }
      setDlState(videoBtn, "idle", "Video");
      videoBtn.disabled = false;
    }
  });

  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(node);
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
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
  cancelPollsByPrefix("yandex:");  // search-result polls only; keep context.
  showFooter("Searching…");
  hidePlaceholder();
  renderEmptyResults();

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
// Result rendering
// ---------------------------------------------------------------------------

function renderEmptyResults() {
  elements.results.innerHTML = "";
  const ul = document.createElement("ul");
  elements.results.appendChild(ul);
  return ul;
}

function renderResults(hits) {
  const ul = renderEmptyResults();
  if (!hits.length) {
    showFooter("No results.");
    return;
  }
  hideFooter();
  for (const hit of hits) ul.appendChild(renderResultRow(hit));
}

function renderResultRow(hit) {
  const node = elements.resultTemplate.content.firstElementChild.cloneNode(true);
  node.dataset.trackId = hit.id;
  if (!hit.available) node.classList.add("unavailable");

  const cover = node.querySelector(".cover");
  const coverImg = cover.querySelector("img");
  cover.href = hit.yandex_url;
  cover.title = "Open in Yandex.Music";
  if (hit.cover_url) coverImg.src = hit.cover_url;

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
    dl.addEventListener("click", () => onYandexDownloadClicked(hit, dl));
  }
  return node;
}

function setDlState(btn, state, label) {
  btn.dataset.state = state;
  btn.querySelector(".dl-label").textContent = label;
  btn.disabled = state !== "idle" && state !== "error";
  btn.title = state === "idle" ? "" : label;
}

// ---------------------------------------------------------------------------
// Download orchestrators
// ---------------------------------------------------------------------------

async function onYandexDownloadClicked(hit, btn) {
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  const task = await postJson(server, "/download", { track_id: String(hit.id) }, btn);
  if (task) pollTask(server, task, btn, `yandex:${hit.id}`);
}

async function onYoutubeAudioClicked(url, btn) {
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  const task = await postJson(server, "/youtube/audio", { url }, btn);
  if (task) pollTask(server, task, btn, `yt-audio:${url}`);
}

async function onYoutubeVideoClicked(url, height, btn) {
  if (!Number.isFinite(height) || height <= 0) {
    setDlState(btn, "error", "Pick a quality");
    return;
  }
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  const task = await postJson(server, "/youtube/video", { url, height }, btn);
  if (task) pollTask(server, task, btn, `yt-video:${url}#h=${height}`);
}

async function postJson(server, path, body, btn) {
  try {
    const resp = await fetch(`${server}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const detail = await safeReadError(resp);
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 60)}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

const STAGE_LABELS = {
  pending: "Queued…",
  downloading: "Downloading",
  converting: "Converting…",
  done: "In library",
  skipped: "In library",
  error: "Error",
};

function pollTask(server, task, btn, key) {
  cancelPoll(key);

  const tick = async () => {
    try {
      const r = await fetch(`${server}/tasks/${task.id}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const t = await r.json();
      if (t.stage === "error") {
        setDlState(btn, "error", `Error: ${(t.error || "").slice(0, 60)}`);
        cancelPoll(key);
        return;
      }
      if (t.stage === "done" || t.stage === "skipped") {
        setDlState(btn, t.stage, STAGE_LABELS[t.stage]);
        cancelPoll(key);
        return;
      }
      const base = STAGE_LABELS[t.stage] || t.stage;
      const label =
        t.stage === "downloading" && t.progress_pct != null
          ? `${base} ${t.progress_pct}%`
          : `${base}…`;
      setDlState(btn, t.stage, label);
    } catch (err) {
      setDlState(btn, "error", `Error: ${err.message.slice(0, 40)}`);
      cancelPoll(key);
      return;
    }
    activePolls.set(key, setTimeout(tick, TASK_POLL_MS));
  };
  activePolls.set(key, setTimeout(tick, 0));
}

function cancelPoll(key) {
  const handle = activePolls.get(key);
  if (handle) {
    clearTimeout(handle);
    activePolls.delete(key);
  }
}

function cancelPollsByPrefix(prefix) {
  for (const key of [...activePolls.keys()]) {
    if (key.startsWith(prefix)) cancelPoll(key);
  }
}

// ---------------------------------------------------------------------------
// Placeholder / footer
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
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

(async function init() {
  await refreshSettingsField();

  // Quick health probe so we can give immediate feedback if the server is down.
  const server = await loadServerUrl();
  let listenerOk = true;
  try {
    const r = await fetch(`${server}/health`, { method: "GET" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch {
    listenerOk = false;
    showFooter(`Listener offline at ${server}`);
  }

  if (listenerOk) {
    // Render the page-context card *after* health passes — there's no point
    // hitting /youtube/info or /yandex/track-info if the server is down.
    renderContext().catch((err) => {
      console.warn("context render failed", err);
    });
  }
})();
