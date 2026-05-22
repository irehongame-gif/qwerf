/* qwerf popup — talks to the local ymsync-server. */

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const SEARCH_DEBOUNCE_MS = 350;
const TASK_POLL_MS = 800;

// ---------------------------------------------------------------------------
// Element handles
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

const elements = {
  topbar: document.querySelector(".topbar"),
  autoToggle: $("auto-toggle"),
  autoInput: $("auto-import"),
  settingsToggle: $("settings-toggle"),
  settings: $("settings-panel"),
  serverUrl: $("server-url"),
  settingsTest: $("settings-test"),
  settingsSave: $("settings-save"),
  settingsStatus: $("settings-status"),

  form: $("search-form"),
  q: $("q"),
  clear: $("clear"),

  scroll: $("scroll"),
  active: $("active"),
  activeList: $("active-list"),
  activeCancelAll: $("active-cancel-all"),
  context: $("context"),
  contextLabel: $("context-label"),
  contextAction: $("context-action"),
  contextBody: $("context-body"),
  results: $("results"),
  placeholder: $("placeholder"),

  footer: $("footer"),
  footerText: $("footer-text"),

  resultTpl: $("result-template"),
  ytTpl: $("yt-context-template"),
  collHeaderTpl: $("collection-header-template"),
  collRowTpl: $("collection-row-template"),
  activeRowTpl: $("active-row-template"),
  loadingTpl: $("context-loading-template"),
  errorTpl: $("context-error-template"),
};

/** Active polls. Keys are stable strings:
 *    yandex:<trackId>            (search row)
 *    yt-audio:<url>              (youtube audio)
 *    yt-video:<url>#h=<height>   (youtube video; matches server dedup_key)
 *    task:<task_id>              (active-jobs panel + cancellation)
 */
const activePolls = new Map();
let lastSearchToken = 0;
let serverSettings = null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function loadServerUrl() {
  const stored = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  return (stored.serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function saveServerUrl(url) {
  await chrome.storage.sync.set({ serverUrl: url.replace(/\/+$/, "") });
}

async function getJson(server, path) {
  const r = await fetch(`${server}${path}`);
  if (!r.ok) throw new Error((await readErr(r)) || `HTTP ${r.status}`);
  return r.json();
}

async function postJson(server, path, body) {
  const r = await fetch(`${server}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error((await readErr(r)) || `HTTP ${r.status}`);
  return r.json();
}

async function putJson(server, path, body) {
  const r = await fetch(`${server}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error((await readErr(r)) || `HTTP ${r.status}`);
  return r.json();
}

async function readErr(resp) {
  try {
    const j = await resp.json();
    return j.detail || j.message;
  } catch { return null; }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
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

function formatBytes(n) {
  if (!n || n < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0; let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

// ---------------------------------------------------------------------------
// Settings panel + auto-import toggle
// ---------------------------------------------------------------------------

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
    const data = await getJson(url, "/health");
    setSettingsStatus(`OK — server v${data.version}`, "ok");
  } catch (err) {
    setSettingsStatus(`Failed: ${err.message}`, "err");
  }
});

function setSettingsStatus(text, level) {
  elements.settingsStatus.textContent = text;
  elements.settingsStatus.className = `status ${level || "muted"}`;
}

elements.autoInput.addEventListener("change", async () => {
  const want = elements.autoInput.checked;
  const server = await loadServerUrl();
  try {
    const updated = await putJson(server, "/settings", {
      auto_import_to_music: want,
    });
    serverSettings = updated;
    syncAutoToggleUI();
    showFooter(want
      ? "Auto-import enabled — downloads will land in Apple Music."
      : "Auto-import disabled — downloads stop after the Downloaded folder.");
  } catch (err) {
    elements.autoInput.checked = !want;  // revert
    setSettingsStatus(`Auto-import update failed: ${err.message}`, "err");
  }
});

function syncAutoToggleUI() {
  if (!serverSettings) {
    elements.autoToggle.classList.add("is-disabled");
    elements.autoInput.disabled = true;
    return;
  }
  elements.autoToggle.classList.remove("is-disabled");
  elements.autoInput.disabled = false;
  elements.autoInput.checked = !!serverSettings.auto_import_to_music;
  elements.autoToggle.classList.toggle("is-on", elements.autoInput.checked);
}

async function loadSettings(server) {
  try {
    serverSettings = await getJson(server, "/settings");
  } catch {
    serverSettings = null;
  }
  syncAutoToggleUI();
}

// ---------------------------------------------------------------------------
// Active-tab context detection
// ---------------------------------------------------------------------------

const YANDEX_HOSTS = new Set([
  "music.yandex.ru", "music.yandex.com", "music.yandex.by",
  "music.yandex.kz", "music.yandex.tj", "music.yandex.ua",
]);

function detectContext(rawUrl) {
  if (!rawUrl) return null;
  let u;
  try { u = new URL(rawUrl); } catch { return null; }

  const ytHost = u.hostname.replace(/^www\./, "").replace(/^m\./, "");

  // YouTube playlist (priority over single-watch when ?list= is present).
  if (ytHost === "youtube.com" || ytHost === "music.youtube.com") {
    const list = u.searchParams.get("list");
    if (list && !list.startsWith("RD")) {
      // RD* = auto-generated radio. We still treat real PL/UU/OL playlists
      // as playlists; radios fall through to the single-video card.
      return { kind: "youtube_playlist", url: rawUrl, listId: list };
    }
    if (u.pathname === "/playlist" && list) {
      return { kind: "youtube_playlist", url: rawUrl, listId: list };
    }
    if (u.pathname === "/watch") {
      const v = u.searchParams.get("v");
      if (v) return { kind: "youtube_watch", url: ytWatchUrl(v) };
    }
    if (u.pathname.startsWith("/shorts/")) {
      const v = u.pathname.split("/")[2];
      if (v) return { kind: "youtube_watch", url: ytWatchUrl(v) };
    }
  }
  if (ytHost === "youtu.be") {
    const v = u.pathname.slice(1).split("/")[0];
    if (v) return { kind: "youtube_watch", url: ytWatchUrl(v) };
  }

  if (YANDEX_HOSTS.has(u.hostname)) {
    // Track URL: /album/<id>/track/<id> or /track/<id>
    const trackMatch = u.pathname.match(/(?:^|\/)(?:album\/(\d+)\/)?track\/(\d+)/);
    if (trackMatch) {
      const albumPart = trackMatch[1] ? `album/${trackMatch[1]}/` : "";
      return {
        kind: "yandex_track",
        url: `https://${u.hostname}/${albumPart}track/${trackMatch[2]}`,
        trackId: trackMatch[2],
      };
    }
    // Album URL without /track/ suffix: /album/<id>
    const albumMatch = u.pathname.match(/^\/album\/(\d+)\/?$/);
    if (albumMatch) {
      return {
        kind: "yandex_album",
        url: `https://${u.hostname}/album/${albumMatch[1]}`,
        albumId: albumMatch[1],
      };
    }
  }

  return null;
}

function ytWatchUrl(videoId) {
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

async function renderContext(server) {
  const url = await getActiveTabUrl();
  const ctx = detectContext(url);
  if (!ctx) {
    elements.context.classList.add("hidden");
    return;
  }
  elements.context.classList.remove("hidden");
  elements.contextLabel.textContent = contextLabelFor(ctx.kind);
  elements.contextAction.classList.add("hidden");
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(elements.loadingTpl.content.cloneNode(true));

  try {
    if (ctx.kind === "yandex_track") {
      await renderYandexTrack(server, ctx);
    } else if (ctx.kind === "yandex_album") {
      await renderYandexAlbum(server, ctx);
    } else if (ctx.kind === "youtube_watch") {
      await renderYoutubeWatch(server, ctx.url);
    } else if (ctx.kind === "youtube_playlist") {
      await renderYoutubePlaylist(server, ctx.url);
    }
  } catch (err) {
    showContextError(err.message);
  }
}

function contextLabelFor(kind) {
  switch (kind) {
    case "yandex_track":     return "From this Yandex.Music page";
    case "yandex_album":     return "Album on this page";
    case "youtube_watch":    return "From this YouTube page";
    case "youtube_playlist": return "Playlist on this page";
    default: return "From this page";
  }
}

function showContextError(message) {
  const node = elements.errorTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".status").textContent = message;
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(node);
}

// --- Yandex single track --------------------------------------------------

async function renderYandexTrack(server, ctx) {
  const hit = await getJson(
    server, `/yandex/track-info?url=${encodeURIComponent(ctx.url)}`
  );
  const ul = document.createElement("ul");
  ul.appendChild(renderResultRow(hit));
  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(ul);
}

// --- Yandex album ---------------------------------------------------------

async function renderYandexAlbum(server, ctx) {
  const data = await getJson(
    server, `/yandex/album-info?url=${encodeURIComponent(ctx.url)}`
  );
  const album = data.album;
  const tracks = data.tracks || [];

  const header = elements.collHeaderTpl.content.firstElementChild.cloneNode(true);
  const cover = header.querySelector(".collection-cover");
  cover.href = album.yandex_url;
  if (album.cover_url) cover.querySelector("img").src = album.cover_url;

  const title = header.querySelector(".collection-title");
  title.textContent = album.title;
  title.href = album.yandex_url;

  const sub = header.querySelector(".collection-sub");
  const artists = (album.artists || []).join(", ") || "Unknown artist";
  const yr = album.year ? ` · ${album.year}` : "";
  sub.textContent = `${artists}${yr} · ${album.track_count} tracks`;

  const allBtn = header.querySelector(".collection-all");
  setDlState(allBtn, "idle", "Download all");
  allBtn.addEventListener("click", () =>
    onDownloadAllYandexAlbum(server, ctx, album, allBtn));

  const list = document.createElement("ul");
  list.className = "collection-list";
  tracks.forEach((hit, i) => {
    const row = renderCollectionRow(hit, i + 1, album.cover_url);
    list.appendChild(row);
  });

  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(header);
  elements.contextBody.appendChild(list);
}

function renderCollectionRow(hit, indexNum, fallbackCover) {
  const node = elements.collRowTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".row-num").textContent = String(indexNum);
  const cover = node.querySelector(".row-cover");
  cover.href = hit.yandex_url;
  cover.querySelector("img").src = hit.cover_url || fallbackCover || "";
  const meta = node.querySelector(".row-meta");
  meta.href = hit.yandex_url;
  meta.querySelector(".title").textContent = hit.title || "(untitled)";
  meta.querySelector(".artists").textContent =
    (hit.artists || []).join(", ") || "Unknown artist";
  const dl = node.querySelector(".row-dl");
  if (hit.already_exported) {
    setDlState(dl, "skipped", "In library");
    dl.disabled = true;
  } else if (!hit.available) {
    setDlState(dl, "error", "Unavailable");
    dl.disabled = true;
    node.classList.add("unavailable");
  } else {
    setDlState(dl, "idle", "Download");
    dl.addEventListener("click", () => onYandexDownloadClicked(hit, dl));
  }
  return node;
}

async function onDownloadAllYandexAlbum(server, ctx, album, btn) {
  setDlState(btn, "queued", "Queueing…");
  let resp;
  try {
    resp = await postJson(server, "/yandex/album", { url: ctx.url });
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 50)}`);
    return;
  }
  // Wire up: refresh active panel and start polling each task.
  await refreshActivePanel(server);
  setDlState(btn, "skipped", `${resp.tasks.length} queued`);
}

// --- YouTube single video -------------------------------------------------

async function renderYoutubeWatch(server, url) {
  const info = await getJson(
    server, `/youtube/info?url=${encodeURIComponent(url)}`
  );
  const node = elements.ytTpl.content.firstElementChild.cloneNode(true);

  const thumb = node.querySelector(".yt-thumb");
  thumb.href = info.url;
  if (info.thumbnail_url) thumb.querySelector("img").src = info.thumbnail_url;

  const titleA = node.querySelector(".yt-title");
  titleA.href = info.url;
  titleA.textContent = info.title || "Untitled";

  node.querySelector(".yt-channel").textContent = info.channel || "Unknown channel";
  node.querySelector(".yt-duration").textContent = formatDuration(info.duration_s);
  if (!info.duration_s) {
    node.querySelector(".yt-duration").classList.add("hidden");
    node.querySelector(".yt-dot").classList.add("hidden");
  }

  const select = node.querySelector(".yt-quality");
  if (Array.isArray(info.available_heights) && info.available_heights.length) {
    for (const h of info.available_heights) {
      const opt = document.createElement("option");
      opt.value = String(h);
      opt.textContent = `${h}p`;
      select.appendChild(opt);
    }
    select.value = String(info.available_heights[0]);
  } else {
    const opt = document.createElement("option");
    opt.textContent = "—";
    select.appendChild(opt);
    select.disabled = true;
    node.querySelector(".yt-video").disabled = true;
  }

  const audioBtn = node.querySelector(".yt-audio");
  if (info.audio_already_exported) {
    setDlState(audioBtn, "skipped", "In library");
    audioBtn.disabled = true;
  } else {
    setDlState(audioBtn, "idle", "Audio");
    audioBtn.addEventListener("click", () => onYoutubeAudioClicked(info.url, audioBtn));
  }

  const videoBtn = node.querySelector(".yt-video");
  if (!videoBtn.disabled) {
    setDlState(videoBtn, "idle", "Video");
    videoBtn.addEventListener("click", () =>
      onYoutubeVideoClicked(info.url, parseInt(select.value, 10), videoBtn));
  }
  select.addEventListener("change", () => {
    if (videoBtn.dataset.state !== "idle" && videoBtn.dataset.state !== "skipped") {
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

// --- YouTube playlist ----------------------------------------------------

async function renderYoutubePlaylist(server, url) {
  const info = await getJson(
    server, `/youtube/playlist-info?url=${encodeURIComponent(url)}&limit=200`
  );

  const header = elements.collHeaderTpl.content.firstElementChild.cloneNode(true);
  const cover = header.querySelector(".collection-cover");
  // Use first entry's thumbnail as the playlist cover (yt-dlp doesn't always
  // give a dedicated playlist thumbnail in flat extraction).
  const firstThumb = (info.entries || []).find((e) => e.thumbnail_url)?.thumbnail_url;
  cover.href = info.url;
  if (firstThumb) cover.querySelector("img").src = firstThumb;

  const title = header.querySelector(".collection-title");
  title.textContent = info.title || "Untitled playlist";
  title.href = info.url;

  const sub = header.querySelector(".collection-sub");
  sub.textContent = `${info.uploader || "—"} · ${info.entry_count} videos`;

  // Mode toggle (audio | video) + quality select for video mode.
  const modeBtn = header.querySelector(".collection-mode");
  const qSelect = header.querySelector(".collection-quality");
  modeBtn.classList.remove("hidden");
  qSelect.classList.remove("hidden");
  let mode = "audio";
  modeBtn.textContent = "Audio";
  qSelect.disabled = true;
  for (const h of (info.available_heights || [1080, 720, 480, 360, 240])) {
    const opt = document.createElement("option");
    opt.value = String(h); opt.textContent = `${h}p`;
    qSelect.appendChild(opt);
  }
  if (qSelect.options.length) qSelect.value = qSelect.options[0].value;
  modeBtn.addEventListener("click", () => {
    mode = mode === "audio" ? "video" : "audio";
    modeBtn.textContent = mode === "audio" ? "Audio" : "Video";
    qSelect.disabled = mode === "audio";
  });

  const allBtn = header.querySelector(".collection-all");
  setDlState(allBtn, "idle", "Download all");
  allBtn.addEventListener("click", async () => {
    const body = { url, mode };
    if (mode === "video") body.height = parseInt(qSelect.value, 10);
    setDlState(allBtn, "queued", "Queueing…");
    try {
      const resp = await postJson(server, "/youtube/playlist", body);
      await refreshActivePanel(server);
      setDlState(allBtn, "skipped", `${resp.tasks.length} queued`);
    } catch (err) {
      setDlState(allBtn, "error", `Error: ${err.message.slice(0, 50)}`);
    }
  });

  const list = document.createElement("ul");
  list.className = "collection-list";
  (info.entries || []).forEach((entry, i) => {
    list.appendChild(renderYtPlaylistRow(entry, i + 1, qSelect));
  });

  elements.contextBody.innerHTML = "";
  elements.contextBody.appendChild(header);
  elements.contextBody.appendChild(list);
}

function renderYtPlaylistRow(entry, indexNum, qSelect) {
  const node = elements.collRowTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".row-num").textContent = String(indexNum);
  const cover = node.querySelector(".row-cover");
  cover.href = entry.url;
  if (entry.thumbnail_url) cover.querySelector("img").src = entry.thumbnail_url;
  const meta = node.querySelector(".row-meta");
  meta.href = entry.url;
  meta.querySelector(".title").textContent = entry.title || "Untitled";
  meta.querySelector(".artists").textContent = entry.channel
    ? `${entry.channel}${entry.duration_s ? ` · ${formatDuration(entry.duration_s)}` : ""}`
    : (entry.duration_s ? formatDuration(entry.duration_s) : "");

  const dl = node.querySelector(".row-dl");
  if (entry.audio_already_exported) {
    setDlState(dl, "skipped", "In library");
    dl.disabled = true;
  } else {
    setDlState(dl, "idle", "Audio");
    dl.addEventListener("click", () => onYoutubeAudioClicked(entry.url, dl));
  }
  return node;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

let searchTimer = null;

elements.q.addEventListener("input", () => {
  elements.clear.classList.toggle("hidden", !elements.q.value);
  if (searchTimer) clearTimeout(searchTimer);
  const term = elements.q.value.trim();
  if (!term) { showPlaceholder(); return; }
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
  cancelPollsByPrefix("yandex:");
  showFooter("Searching…");
  hidePlaceholder();
  renderEmptyResults();

  const server = await loadServerUrl();
  let hits;
  try {
    hits = await getJson(server, `/search?q=${encodeURIComponent(term)}&limit=30`);
  } catch (err) {
    if (token !== lastSearchToken) return;
    showFooter(`Could not reach ${server} — ${err.message}`);
    showPlaceholderError(server, err);
    return;
  }
  if (token !== lastSearchToken) return;
  renderResults(hits);
}

function renderEmptyResults() {
  elements.results.innerHTML = "";
  const ul = document.createElement("ul");
  elements.results.appendChild(ul);
  return ul;
}

function renderResults(hits) {
  const ul = renderEmptyResults();
  if (!hits.length) { showFooter("No results."); return; }
  hideFooter();
  for (const hit of hits) ul.appendChild(renderResultRow(hit));
}

function renderResultRow(hit) {
  const node = elements.resultTpl.content.firstElementChild.cloneNode(true);
  node.dataset.trackId = hit.id;
  if (!hit.available) node.classList.add("unavailable");

  const cover = node.querySelector(".cover");
  cover.href = hit.yandex_url;
  cover.title = "Open in Yandex.Music";
  if (hit.cover_url) cover.querySelector("img").src = hit.cover_url;

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

// ---------------------------------------------------------------------------
// Download orchestrators
// ---------------------------------------------------------------------------

async function onYandexDownloadClicked(hit, btn) {
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  try {
    const task = await postJson(server, "/download", {
      track_id: String(hit.id),
    });
    pollTask(server, task.id, btn, `yandex:${hit.id}`);
    refreshActivePanel(server);
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 50)}`);
  }
}

async function onYoutubeAudioClicked(url, btn) {
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  try {
    const task = await postJson(server, "/youtube/audio", { url });
    pollTask(server, task.id, btn, `yt-audio:${url}`);
    refreshActivePanel(server);
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 50)}`);
  }
}

async function onYoutubeVideoClicked(url, height, btn) {
  if (!Number.isFinite(height) || height <= 0) {
    setDlState(btn, "error", "Pick a quality"); return;
  }
  setDlState(btn, "pending", "Queued…");
  const server = await loadServerUrl();
  try {
    const task = await postJson(server, "/youtube/video", { url, height });
    pollTask(server, task.id, btn, `yt-video:${url}#h=${height}`);
    refreshActivePanel(server);
  } catch (err) {
    setDlState(btn, "error", `Error: ${err.message.slice(0, 50)}`);
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

const STAGE_LABELS = {
  pending: "Queued…",
  queued: "Queued…",
  downloading: "Downloading",
  converting: "Converting…",
  exporting: "Exporting…",
  done: "In library",
  skipped: "In library",
  error: "Error",
  cancelled: "Cancelled",
};

function setDlState(btn, state, label) {
  btn.dataset.state = state;
  const lbl = btn.querySelector(".dl-label");
  if (lbl) lbl.textContent = label;
  else btn.textContent = label;
  btn.disabled = state !== "idle" && state !== "error";
  btn.title = state === "idle" ? "" : label;
}

function pollTask(server, taskId, btn, key) {
  cancelPoll(key);
  const tick = async () => {
    try {
      const t = await getJson(server, `/tasks/${taskId}`);
      if (t.stage === "error") {
        if (btn) setDlState(btn, "error", `Error: ${(t.error || "").slice(0, 60)}`);
        cancelPoll(key);
        refreshActivePanel(server);
        return;
      }
      if (t.stage === "cancelled") {
        if (btn) setDlState(btn, "cancelled", "Cancelled");
        cancelPoll(key);
        refreshActivePanel(server);
        return;
      }
      if (t.stage === "done" || t.stage === "skipped") {
        if (btn) setDlState(btn, t.stage, STAGE_LABELS[t.stage]);
        cancelPoll(key);
        refreshActivePanel(server);
        return;
      }
      if (btn) {
        const base = STAGE_LABELS[t.stage] || t.stage;
        const lbl = (t.stage === "downloading" && t.progress_pct != null)
          ? `${base} ${t.progress_pct}%`
          : `${base}…`;
        setDlState(btn, t.stage, lbl);
      }
    } catch (err) {
      if (btn) setDlState(btn, "error", `Error: ${err.message.slice(0, 40)}`);
      cancelPoll(key);
      return;
    }
    activePolls.set(key, setTimeout(tick, TASK_POLL_MS));
  };
  activePolls.set(key, setTimeout(tick, 0));
}

function cancelPoll(key) {
  const handle = activePolls.get(key);
  if (handle) { clearTimeout(handle); activePolls.delete(key); }
}

function cancelPollsByPrefix(prefix) {
  for (const key of [...activePolls.keys()]) {
    if (key.startsWith(prefix)) cancelPoll(key);
  }
}

// ---------------------------------------------------------------------------
// Active jobs panel (carries over between popup sessions)
// ---------------------------------------------------------------------------

async function refreshActivePanel(server) {
  let tasks = [];
  try {
    tasks = await getJson(server, "/tasks?active=true&limit=50");
  } catch {
    elements.active.classList.add("hidden");
    return;
  }
  if (!tasks.length) {
    elements.active.classList.add("hidden");
    elements.activeList.innerHTML = "";
    return;
  }
  elements.active.classList.remove("hidden");
  // Re-render: keep it simple, full refresh.
  elements.activeList.innerHTML = "";
  for (const t of tasks) {
    elements.activeList.appendChild(renderActiveRow(server, t));
    // Only attach polling for tasks that can still change state. Errored
    // rows are terminal; polling them would just thrash refreshActivePanel
    // in an infinite loop (each tick would see 'error' and re-fetch the
    // panel, which would re-attach the poll, which would tick again…).
    if (t.stage !== "error") {
      pollTask(server, t.id, null, `task:${t.id}`);
    }
  }
}

function renderActiveRow(server, task) {
  const node = elements.activeRowTpl.content.firstElementChild.cloneNode(true);
  node.dataset.taskId = task.id;

  const kindEl = node.querySelector(".active-kind");
  if (task.kind === "yandex_track") {
    kindEl.classList.add("k-yandex"); kindEl.textContent = "Y";
  } else {
    kindEl.classList.add("k-yt");
    kindEl.textContent = task.kind === "youtube_video" ? "V" : "A";
  }

  node.querySelector(".active-title").textContent =
    task.title || task.dedup_key || "(unnamed)";

  const subBits = [];
  if (task.stage === "error" && task.error) {
    // Errors lead the sub-line so the user sees what went wrong without
    // having to dig.
    subBits.push(task.error.replace(/\s+/g, " ").trim().slice(0, 100));
  } else {
    if (task.artists && task.artists.length) subBits.push(task.artists.join(", "));
    if (task.height) subBits.push(`${task.height}p`);
    if (task.group_label) subBits.push(`from “${task.group_label}”`);
    if (task.total_bytes) subBits.push(formatBytes(task.total_bytes));
  }
  node.querySelector(".active-sub").textContent = subBits.join(" · ");

  const stage = node.querySelector(".active-stage");
  const base = STAGE_LABELS[task.stage] || task.stage;
  stage.textContent = task.stage === "downloading" && task.progress_pct != null
    ? `${task.progress_pct}%`
    : base;
  stage.className = `active-stage s-${task.stage}`;

  const cancel = node.querySelector(".active-cancel");
  if (task.stage === "error") {
    // Cancelling a finished-with-error task is a no-op server-side. The
    // 'X' button just dismisses the row from the popup; clicking the
    // original Audio/Video button retries (server expires errored tasks
    // from the dedup map immediately).
    cancel.title = "Dismiss";
    cancel.addEventListener("click", () => onDismissTaskClicked(node));
  } else if (!task.cancellable) {
    cancel.classList.add("hidden");
  } else {
    cancel.title = "Cancel";
    cancel.addEventListener("click", () =>
      onCancelTaskClicked(server, task.id, node));
  }

  return node;
}

function onDismissTaskClicked(rowNode) {
  rowNode.remove();
  if (!elements.activeList.children.length) {
    elements.active.classList.add("hidden");
  }
}

async function onCancelTaskClicked(server, taskId, rowNode) {
  try {
    await postJson(server, `/tasks/${taskId}/cancel`, {});
  } catch {
    /* ignore — refresh below will show whatever the server thinks */
  }
  cancelPoll(`task:${taskId}`);
  refreshActivePanel(server);
}

elements.activeCancelAll.addEventListener("click", async () => {
  const server = await loadServerUrl();
  // No bulk endpoint without a group, so we cancel each visible row.
  const ids = [...elements.activeList.querySelectorAll(".active-row")]
    .map((r) => r.dataset.taskId).filter(Boolean);
  await Promise.all(
    ids.map((id) => postJson(server, `/tasks/${id}/cancel`, {}).catch(() => null))
  );
  refreshActivePanel(server);
});

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

function hideFooter() { elements.footer.classList.add("hidden"); }

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

(async function init() {
  elements.serverUrl.value = await loadServerUrl();

  const server = await loadServerUrl();
  let listenerOk = true;
  try {
    await getJson(server, "/health");
  } catch {
    listenerOk = false;
    showFooter(`Listener offline at ${server}`);
  }

  if (listenerOk) {
    await loadSettings(server);
    await refreshActivePanel(server);
    renderContext(server).catch((err) => console.warn("context render failed", err));
  } else {
    syncAutoToggleUI();
  }
})();
