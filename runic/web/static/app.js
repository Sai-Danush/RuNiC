"use strict";

const $ = (id) => document.getElementById(id);
const selected = new Set();      // selected public playlist ids
let routeLoaded = false;
let eleChart = null;
let lastEntries = [];
let eleKms = [];                 // x-axis km labels, for the run marker
let runMarkerKm = null;          // current run position (km) drawn on the chart

// Chart.js plugin: a vertical line marking where on the route you currently are.
const runMarkerPlugin = {
  id: "runMarker",
  afterDatasetsDraw(chart) {
    if (runMarkerKm == null || !eleKms.length) return;
    // Nearest label index to the target km (x-axis is a category scale).
    let bi = 0, bd = Infinity;
    for (let i = 0; i < eleKms.length; i++) {
      const d = Math.abs(eleKms[i] - runMarkerKm);
      if (d < bd) { bd = d; bi = i; }
    }
    try {
      const x = chart.scales.x.getPixelForValue(bi);
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, top); ctx.lineTo(x, bottom);
      ctx.lineWidth = 2; ctx.strokeStyle = "#3aa0ff"; ctx.stroke();
      ctx.restore();
    } catch {}
  },
};

function showError(msg) {
  const e = $("error");
  e.textContent = msg;
  e.classList.remove("hidden");
  e.scrollIntoView({ behavior: "smooth", block: "center" });
}
function clearError() { $("error").classList.add("hidden"); }

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

// --- Auth --------------------------------------------------------------------

async function checkAuth() {
  try {
    const me = await api("/api/me");
    $("loginBtn").classList.add("hidden");
    $("userInfo").classList.remove("hidden");
    $("userInfo").textContent = `● ${me.display_name}`;
    $("logoutBtn").classList.remove("hidden");
    $("step-playlists").classList.remove("disabled");
    loadPlaylists();
  } catch {
    $("loginBtn").classList.remove("hidden");
    $("userInfo").classList.add("hidden");
    $("logoutBtn").classList.add("hidden");
  }
}

$("loginBtn").onclick = () => { location.href = "/login"; };
$("logoutBtn").onclick = async () => {
  await fetch("/logout", { method: "POST" });
  location.reload();
};

// --- Playlists ---------------------------------------------------------------

async function loadPlaylists() {
  const box = $("playlists");
  box.innerHTML = '<p class="muted">Loading playlists…</p>';
  try {
    const { playlists } = await api("/api/playlists");
    box.innerHTML = "";
    for (const p of playlists) {
      const el = document.createElement("div");
      el.className = "pl" + (p.public ? "" : " priv");
      const img = p.image
        ? `<img src="${p.image}" alt="" />`
        : `<div class="noimg"></div>`;
      const tag = p.public ? "" : `<span class="tag private">private</span>`;
      el.innerHTML = `${img}
        <div class="meta">
          <div class="pname">${escapeHtml(p.name)}${tag}</div>
          <div class="pinfo">${escapeHtml(p.owner || "")}</div>
        </div>`;
      if (p.public) {
        el.onclick = () => {
          if (selected.has(p.id)) { selected.delete(p.id); el.classList.remove("sel"); }
          else { selected.add(p.id); el.classList.add("sel"); }
          updatePlCount();
        };
      }
      box.appendChild(el);
    }
  } catch (e) {
    box.innerHTML = `<p class="muted">Could not load playlists: ${escapeHtml(e.message)}</p>`;
  }
}

function updatePlCount() {
  const n = selected.size;
  $("plCount").textContent = n ? `${n} selected` : "";
}

// Generic collapse/expand wiring (header click toggles body + chevron).
function wireCollapse(headerId, bodyId, chevronId) {
  $(headerId).onclick = () => {
    const collapsed = $(bodyId).classList.toggle("collapsed");
    $(chevronId).classList.toggle("up", !collapsed);
  };
}
wireCollapse("plHeader", "plBody", "plChevron");      // playlist picker (inner)
wireCollapse("resultsHeader", "resultsBody", "resultsChevron");
// Setup: same toggle, but drop the "tap to edit" hint once it's open again.
$("setupHeader").onclick = () => {
  const collapsed = $("setupBody").classList.toggle("collapsed");
  $("setupChevron").classList.toggle("up", !collapsed);
  if (!collapsed) $("setupSummary").textContent = "";
};

function setCollapsed(bodyId, chevronId, collapsed) {
  $(bodyId).classList.toggle("collapsed", collapsed);
  $(chevronId).classList.toggle("up", !collapsed);
}

// --- GPX ---------------------------------------------------------------------

$("gpxFile").onchange = async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  $("fileName").textContent = file.name;
  clearError();
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await api("/api/gpx", { method: "POST", body: fd });
    routeLoaded = true;
    renderRoute(d);
  } catch (e) {
    showError("GPX error: " + e.message);
  }
};

// --- Map route picker (draw -> BRouter snap + elevation) ---------------------

let map = null;            // Leaflet map (lazy-initialised when map mode shown)
let markers = [];          // dropped waypoint markers
let guideLine = null;      // dashed polyline through the raw clicks
let snappedLine = null;    // solid line of the snapped route returned by BRouter
let waypoints = [];        // [[lat, lng], ...]

function initMap() {
  if (map) return;
  map = L.map("map");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  // Centre on the user if they allow it; otherwise a sensible default view.
  map.setView([51.5074, -0.1278], 13);
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (pos) => map.setView([pos.coords.latitude, pos.coords.longitude], 14),
      () => {}, { timeout: 5000 }
    );
  }
  map.on("click", (e) => addWaypoint(e.latlng.lat, e.latlng.lng));
}

function addWaypoint(lat, lng) {
  waypoints.push([lat, lng]);
  markers.push(L.marker([lat, lng]).addTo(map));
  // Drawing fresh waypoints invalidates any previously snapped route.
  if (snappedLine) { map.removeLayer(snappedLine); snappedLine = null; }
  if (guideLine) map.removeLayer(guideLine);
  guideLine = L.polyline(waypoints, { color: "#8b94a3", weight: 2, dashArray: "5,6" }).addTo(map);
  $("useRouteBtn").disabled = waypoints.length < 2;
  $("mapHint").textContent =
    waypoints.length < 2
      ? "Click the map to drop points along your route."
      : `${waypoints.length} points — click “Use this route” to snap & analyse.`;
}

function clearRoute() {
  waypoints = [];
  markers.forEach((m) => map && map.removeLayer(m));
  markers = [];
  if (guideLine) { map.removeLayer(guideLine); guideLine = null; }
  if (snappedLine) { map.removeLayer(snappedLine); snappedLine = null; }
  $("useRouteBtn").disabled = true;
  $("mapHint").textContent = "Click the map to drop points along your route.";
  $("routeStats").classList.add("hidden");
  $("chartWrap").classList.add("hidden");
  routeLoaded = false;
}

$("clearRouteBtn").onclick = clearRoute;

$("useRouteBtn").onclick = async () => {
  if (waypoints.length < 2) return;
  clearError();
  const btn = $("useRouteBtn");
  btn.disabled = true;
  $("mapHint").textContent = "Snapping to paths & fetching elevation…";
  try {
    const d = await api("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ waypoints, profile: "hiking-beta" }),
    });
    routeLoaded = true;
    renderRoute(d);
    // Replace the dashed guide with the snapped route line.
    if (guideLine) { map.removeLayer(guideLine); guideLine = null; }
    if (snappedLine) map.removeLayer(snappedLine);
    if (d.geometry && d.geometry.length) {
      snappedLine = L.polyline(d.geometry, { color: "#1db954", weight: 4 }).addTo(map);
      map.fitBounds(snappedLine.getBounds(), { padding: [30, 30] });
    }
    $("mapHint").textContent = `Route ready — ${d.distance_km} km. Add points to redraw.`;
  } catch (e) {
    showError("Route error: " + e.message);
    $("mapHint").textContent = "Click the map to drop points along your route.";
  } finally {
    btn.disabled = waypoints.length < 2;
  }
};

// Mode toggle: draw on map (default) vs upload GPX.
function setRouteMode(mode) {
  const onMap = mode === "map";
  $("modeMapBtn").classList.toggle("active", onMap);
  $("modeUploadBtn").classList.toggle("active", !onMap);
  $("routeMap").classList.toggle("hidden", !onMap);
  $("routeUpload").classList.toggle("hidden", onMap);
  if (onMap) {
    initMap();
    // Leaflet needs a sized, visible container to compute tile layout.
    setTimeout(() => map && map.invalidateSize(), 0);
  }
}
$("modeMapBtn").onclick = () => setRouteMode("map");
$("modeUploadBtn").onclick = () => setRouteMode("upload");
// Map is the default mode; init it once the page is ready.
setRouteMode("map");

function renderRoute(d) {
  $("routeStats").classList.remove("hidden");
  $("routeStats").innerHTML = `
    <div class="stat"><b>${d.distance_km}</b><span>km</span></div>
    <div class="stat"><b>+${d.ascent_m}</b><span>m ascent</span></div>
    <div class="stat"><b>−${d.descent_m}</b><span>m descent</span></div>
    <div class="stat"><b>${d.n_segments}</b><span>segments</span></div>`;
  $("chartWrap").classList.remove("hidden");

  const labels = d.elevation.map((p) => p.km);
  const data = d.elevation.map((p) => p.ele);
  eleKms = labels;
  if (eleChart) eleChart.destroy();
  eleChart = new Chart($("eleChart"), {
    type: "line",
    plugins: [runMarkerPlugin],
    data: {
      labels,
      datasets: [{
        data, fill: true, borderColor: "#1db954",
        backgroundColor: "rgba(29,185,84,0.15)", borderWidth: 2,
        pointRadius: 0, tension: 0.3,
      }],
    },
    options: {
      plugins: { legend: { display: false },
        tooltip: { callbacks: { title: (i) => `${i[0].label} km`,
          label: (i) => `${i.raw} m` } } },
      scales: {
        x: { title: { display: true, text: "distance (km)", color: "#8b94a3" },
          ticks: { color: "#8b94a3", maxTicksLimit: 10 }, grid: { color: "#2a313c" } },
        y: { title: { display: true, text: "elevation (m)", color: "#8b94a3" },
          ticks: { color: "#8b94a3" }, grid: { color: "#2a313c" } },
      },
    },
  });
}

// --- PBs ---------------------------------------------------------------------

const DISTANCES = [
  { token: "5k", label: "5K" },
  { token: "10k", label: "10K" },
  { token: "half", label: "Half" },
  { token: "full", label: "Full" },
  { token: "custom", label: "Custom" },
];

function addPbRow(dist = "5k", h = 0, m = 25, s = 0) {
  const row = document.createElement("div");
  row.className = "pb";

  const pills = DISTANCES.map((d) =>
    `<button type="button" class="pill${d.token === dist ? " active" : ""}" data-d="${d.token}">${d.label}</button>`
  ).join("");

  const showH = dist === "full" || h > 0;
  row.innerHTML = `
    <div class="dist-pills">${pills}</div>
    <input type="number" class="custom-km hidden" placeholder="km" step="0.1" min="0.1" />
    <div class="time-sel">
      <div class="t-unit${showH ? "" : " dim"}"><input class="t-h" type="number" min="0" max="9" value="${h}"><label>hr</label></div>
      <span class="colon">:</span>
      <div class="t-unit"><input class="t-m" type="number" min="0" max="59" value="${String(m).padStart(2,"0")}"><label>min</label></div>
      <span class="colon">:</span>
      <div class="t-unit"><input class="t-s" type="number" min="0" max="59" value="${String(s).padStart(2,"0")}"><label>sec</label></div>
    </div>
    <button type="button" class="del" title="remove">×</button>`;

  const pillBtns = row.querySelectorAll(".pill");
  const customKm = row.querySelector(".custom-km");
  const hInput = row.querySelector(".t-h");
  const hUnit = hInput.parentElement;
  // Hours field is "dim" only when it's empty/zero AND not a marathon.
  const syncHour = () => {
    const isFull = row.querySelector(".pill.active")?.dataset.d === "full";
    hUnit.classList.toggle("dim", !isFull && !(+hInput.value > 0));
  };
  hInput.oninput = syncHour;
  pillBtns.forEach((b) => {
    b.onclick = () => {
      pillBtns.forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      customKm.classList.toggle("hidden", b.dataset.d !== "custom");
      syncHour();
    };
  });
  syncHour();
  // Pad seconds/minutes on blur for a tidy look.
  row.querySelectorAll(".t-m, .t-s").forEach((i) => {
    i.onblur = () => { i.value = String(Math.min(59, Math.max(0, +i.value || 0))).padStart(2, "0"); };
  });
  row.querySelector(".del").onclick = () => {
    if ($("pbList").children.length > 1) { row.remove(); updateDelVisibility(); }
  };
  $("pbList").appendChild(row);
  updateDelVisibility();
}
function updateDelVisibility() {
  const rows = $("pbList").children;
  for (const r of rows) r.querySelector(".del").style.visibility =
    rows.length > 1 ? "visible" : "hidden";
}
$("addPbBtn").onclick = () => addPbRow("10k", 0, 50, 0);
addPbRow("5k", 0, 25, 0);

function collectPbs() {
  const pbs = [];
  for (const row of $("pbList").children) {
    const active = row.querySelector(".pill.active");
    if (!active) continue;
    let dist = active.dataset.d;
    if (dist === "custom") {
      const km = parseFloat(row.querySelector(".custom-km").value);
      if (!km || km <= 0) continue;
      dist = `${km}k`;
    }
    const h = +row.querySelector(".t-h").value || 0;
    const m = +row.querySelector(".t-m").value || 0;
    const s = +row.querySelector(".t-s").value || 0;
    if (h + m + s === 0) continue;
    const time = h > 0
      ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
      : `${m}:${String(s).padStart(2,"0")}`;
    pbs.push(`${dist}=${time}`);
  }
  return pbs;
}

// --- Generate ----------------------------------------------------------------

$("generateBtn").onclick = async () => {
  clearError();
  if (selected.size === 0) return showError("Select at least one public playlist (step 1).");
  if (!routeLoaded) return showError("Upload a GPX route (step 2).");
  const pbs = collectPbs();
  const cadence = $("cadence").value.trim();
  if (pbs.length === 0 && !cadence) return showError("Add a personal best or cadence (step 3).");

  $("generateBtn").disabled = true;
  $("genStatus").textContent = "Fetching songs + matching to terrain… (this can take ~10–20s)";
  try {
    const payload = {
      playlist_ids: [...selected],
      pbs,
      cadence: cadence || null,
    };
    const res = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("genStatus").textContent = "";
    renderResults(res);
  } catch (e) {
    showError("Generation failed: " + e.message);
    $("genStatus").textContent = "";
  } finally {
    $("generateBtn").disabled = false;
  }
};

function fmt(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

let lastResults = null;
let debugMode = false;

function renderResults(res) {
  if (res) { lastResults = res; lastEntries = res.entries; }
  const data = lastResults;
  if (!data) return;
  const s = data.summary;
  $("results").classList.remove("hidden");

  if (res) {
    // Fresh generation: tuck setup away, open the results box.
    setCollapsed("setupBody", "setupChevron", true);
    setCollapsed("resultsBody", "resultsChevron", false);
    $("setupSummary").textContent = "tap to edit";
  }

  // User-facing summary: just the essentials. Tech detail behind debug.
  let summary = `<b>${s.track_count}</b> tracks · playlist <b>${fmt(s.playlist_s)}</b> vs predicted run <b>${fmt(s.predicted_run_s)}</b>`;
  if (debugMode) {
    summary += ` · cadence <b>${Math.round(s.cadence_spm)}</b> SPM · ${s.candidate_count} candidates`
      + (s.skipped_count ? ` (${s.skipped_count} not in ReccoBeats)` : "");
  }
  $("resultSummary").innerHTML = summary;

  const dbgHead = debugMode ? "<th>BPM</th><th>Energy</th><th>Why</th>" : "";
  const rows = data.entries.map((e) => {
    const dbg = debugMode
      ? `<td>${e.bpm}</td><td>${e.energy}</td><td class="muted">${escapeHtml(e.reason)}</td>`
      : "";
    return `
    <tr>
      <td>${e.order}</td>
      <td>${fmt(e.start_s)}–${fmt(e.end_s)}</td>
      <td><span class="terr ${e.terrain}">${e.terrain.replace("_", " ")}</span></td>
      <td><a href="${e.url}" target="_blank">${escapeHtml(e.title)}</a><br>
          <span class="muted">${escapeHtml(e.artist)}</span></td>
      ${dbg}
    </tr>`;
  }).join("");
  $("resultTable").innerHTML = `<table>
    <thead><tr><th>#</th><th>Time</th><th>Terrain</th><th>Song</th>${dbgHead}</tr></thead>
    <tbody>${rows}</tbody></table>`;
  if (res) $("results").scrollIntoView({ behavior: "smooth" });
}

$("debugToggle").onchange = (e) => { debugMode = e.target.checked; renderResults(); };

$("copyBtn").onclick = async () => {
  const links = lastEntries.map((e) => e.url).join("\n");
  await navigator.clipboard.writeText(links);
  $("copyMsg").textContent = `Copied ${lastEntries.length} links — paste into a Spotify playlist.`;
  setTimeout(() => { $("copyMsg").textContent = ""; }, 4000);
};

function csvCell(s) {
  s = (s == null ? "" : String(s));
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

$("csvBtn").onclick = () => {
  if (!lastEntries.length) return;
  const header = ["Title", "Artist", "Spotify URL"];
  const rows = lastEntries.map((e) => [e.title, e.artist, e.url].map(csvCell).join(","));
  const csv = [header.join(","), ...rows].join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "runic-playlist.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  $("copyMsg").textContent = `Downloaded ${lastEntries.length} tracks — import at tunemymusic.com.`;
  setTimeout(() => { $("copyMsg").textContent = ""; }, 5000);
};

$("ytBtn").onclick = async () => {
  if (!lastEntries.length) return;
  const btn = $("ytBtn");
  btn.disabled = true;
  $("copyMsg").textContent = "Matching tracks on YouTube Music…";
  try {
    const stamp = new Date().toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
    const res = await api("/api/ytmusic/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: `Runic run — ${stamp}`,
        tracks: lastEntries.map((e) => ({
          title: e.title, artist: e.artist, duration_s: e.duration_s,
        })),
      }),
    });
    let msg = `Created YT Music playlist (${res.matched}/${res.total} matched). `;
    $("copyMsg").innerHTML =
      `${escapeHtml(msg)}<a href="${res.url}" target="_blank">Open ▸</a>`;
    if (res.unmatched && res.unmatched.length) {
      console.log("Unmatched on YT Music:", res.unmatched);
    }
  } catch (e) {
    $("copyMsg").textContent = "YT Music: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- Run player (Spotify Web Playback SDK) -----------------------------------

let sdkPlayer = null;
let sdkDeviceId = null;
let deviceReadyResolve;
const deviceReadyP = new Promise((r) => { deviceReadyResolve = r; });

// SDK calls this once spotify-player.js has loaded. Guard the race where the
// SDK finishes before this script runs (window.Spotify already present).
let sdkLoadedResolve;
const sdkLoadedP = new Promise((r) => { sdkLoadedResolve = r; });
window.onSpotifyWebPlaybackSDKReady = () => sdkLoadedResolve();
if (window.Spotify) sdkLoadedResolve();

// Live playback clock (extrapolated between state-change events).
let curPos = 0, curDur = 0, curPaused = true, lastTick = 0, curTrackId = null;

function setPlayerStatus(msg) {
  $("playerStatus").classList.remove("hidden");
  $("playerStatus").innerHTML = msg;
}

async function fetchToken() {
  const r = await fetch("/api/token");
  if (!r.ok) throw new Error("Not signed in to Spotify.");
  return (await r.json()).access_token;
}

async function initPlayer() {
  if (sdkPlayer) return sdkPlayer;
  await sdkLoadedP;
  await fetchToken();                       // surface auth errors early
  sdkPlayer = new Spotify.Player({
    name: "Runic",
    getOAuthToken: (cb) => { fetchToken().then(cb).catch(() => {}); },
    volume: 0.6,
  });
  sdkPlayer.addListener("ready", ({ device_id }) => {
    sdkDeviceId = device_id; deviceReadyResolve(device_id);
  });
  sdkPlayer.addListener("not_ready", () => { sdkDeviceId = null; });
  sdkPlayer.addListener("account_error", () =>
    setPlayerStatus("Spotify <b>Premium</b> is required to play here. Use “Copy all track links” instead."));
  sdkPlayer.addListener("authentication_error", ({ message }) =>
    setPlayerStatus("Spotify auth error — try logging out and back in. (" + escapeHtml(message) + ")"));
  sdkPlayer.addListener("initialization_error", ({ message }) =>
    setPlayerStatus("Player could not initialise: " + escapeHtml(message)));
  sdkPlayer.addListener("player_state_changed", onPlayerState);
  const ok = await sdkPlayer.connect();
  if (!ok) throw new Error("Spotify player failed to connect.");
  return sdkPlayer;
}

async function startRun() {
  if (!lastEntries.length) return;
  $("player").classList.remove("hidden");
  $("player").scrollIntoView({ behavior: "smooth" });
  setPlayerStatus("Connecting to Spotify…");
  try {
    await initPlayer();
    // Unlock audio (required by some browsers' autoplay policy; no-op elsewhere).
    if (sdkPlayer.activateElement) { try { await sdkPlayer.activateElement(); } catch {} }
    setPlayerStatus("Registering this browser as a Spotify device…");
    const deviceId = await deviceReadyP;
    setPlayerStatus("Starting playback…");
    const uris = lastEntries.map((e) => `spotify:track:${e.spotify_id}`);
    const res = await api("/api/playback/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: deviceId, uris }),
    });
    if (!res.ok) {
      setPlayerStatus(res.status === 403
        ? "Spotify refused playback (403) — this usually means the account isn’t Premium."
        : "Spotify refused playback (HTTP " + res.status + ").");
      return;
    }
    $("playerStatus").classList.add("hidden");
    $("playerBody").classList.remove("hidden");
  } catch (e) {
    setPlayerStatus("Could not start the player: " + escapeHtml(e.message));
  }
}

function entryFor(trackId) {
  return lastEntries.find((e) => e.spotify_id === trackId);
}

function onPlayerState(state) {
  if (!state) return;
  const cur = state.track_window.current_track;
  curPos = state.position; curDur = state.duration;
  curPaused = state.paused; lastTick = Date.now(); curTrackId = cur.id;
  $("playPauseBtn").textContent = state.paused ? "▶" : "⏸";
  updateNowPlaying(cur);
  renderUpNext(cur.id);
}

function updateNowPlaying(track) {
  const e = entryFor(track.id);
  $("npTitle").textContent = track.name;
  $("npArtist").textContent = (track.artists || []).map((a) => a.name).join(", ");
  const img = track.album && track.album.images && track.album.images[0];
  const art = $("npArt");
  if (img) {
    art.style.backgroundImage = `url(${img.url})`;
    art.style.backgroundSize = "cover";
    art.style.backgroundPosition = "center";
  }
  const t = e ? e.terrain : "flat";
  const badge = $("npTerrain");
  badge.className = "terr " + t;
  badge.textContent = t.replace("_", " ");
  $("npIndex").textContent = e ? `track ${e.order} of ${lastEntries.length}` : "";
}

function renderUpNext(currentId) {
  const idx = lastEntries.findIndex((e) => e.spotify_id === currentId);
  const upcoming = idx >= 0 ? lastEntries.slice(idx + 1, idx + 5) : [];
  const rows = upcoming.map((e) => `
    <div class="un-row">
      <span class="terr ${e.terrain}">${e.terrain.replace("_", " ")}</span>
      <div style="min-width:0">
        <div class="un-title">${escapeHtml(e.title)}</div>
        <div class="un-artist">${escapeHtml(e.artist)}</div>
      </div>
    </div>`).join("");
  $("upNext").innerHTML = `<div class="un-head">Up next</div>`
    + (rows || `<div class="un-empty">Last track — enjoy the finish.</div>`);
}

// Tick the progress bar + elevation marker between state events.
setInterval(() => {
  if (!sdkPlayer || curDur <= 0 || $("playerBody").classList.contains("hidden")) return;
  let pos = curPos + (curPaused ? 0 : Date.now() - lastTick);
  pos = Math.max(0, Math.min(pos, curDur));
  $("npElapsed").textContent = fmt(pos / 1000);
  $("npDur").textContent = fmt(curDur / 1000);
  $("npBar").style.width = (100 * pos / curDur) + "%";

  // Map current position to a route distance for the chart marker.
  const e = lastResults && entryForCurrent();
  if (e && lastResults.summary.predicted_run_s > 0 && eleKms.length) {
    const elapsedTotal = e.start_s + pos / 1000;
    const frac = Math.min(1, elapsedTotal / lastResults.summary.predicted_run_s);
    runMarkerKm = frac * eleKms[eleKms.length - 1];
    if (eleChart) eleChart.update("none");
  }
}, 500);

function entryForCurrent() {
  return curTrackId ? entryFor(curTrackId) : null;
}

$("startRunBtn").onclick = startRun;
$("playPauseBtn").onclick = () => sdkPlayer && sdkPlayer.togglePlay();
$("nextBtn").onclick = () => sdkPlayer && sdkPlayer.nextTrack();
$("prevBtn").onclick = () => sdkPlayer && sdkPlayer.previousTrack();

// --- Init --------------------------------------------------------------------

const params = new URLSearchParams(location.search);
if (params.get("error")) {
  showError("Spotify auth error: " + params.get("error"));
  history.replaceState({}, "", "/");
}
checkAuth();
