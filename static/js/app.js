/* Wenet telemetry display.
 *
 * The whole page is built from what arrives. Nothing about which payloads fly,
 * which sensors they carry, or what those sensors are called is known in
 * advance -- a DS18X20 names itself from its ROM at runtime -- so the source
 * list, the field rows and the charts are all generated from the server's
 * discovery output rather than from a hardcoded layout.
 */

import { Chart, formatValue, formatTime } from "./chart.js";
import { TrackMap } from "./map.js";

const REFRESH_MS = 5000;      // chart refresh cadence while live
const STALL_FACTOR = 4;       // missed intervals before a source reads stalled
const STALL_FLOOR_S = 12;

const el = (id) => document.getElementById(id);

const state = {
  flightId: null,
  flights: [],
  sources: [],
  selected: new Set(),       // "source_key:field_name"
  colors: new Map(),         // source_key -> categorical slot (1-8)
  axis: "time",
  charts: new Map(),         // key -> {chart, wrap, meta}
  latest: null,
  events: null,
  dirty: false,
  live: false,
  // Grafana-style window. 'all' = the whole flight; 'relative' slides with the
  // clock while live; 'absolute' is what a drag-zoom produces and stays put.
  range: { mode: "all", seconds: 0, start: null, end: null },
  cursorX: null,
  domain: { min: null, max: null },
};

/* ------------------------------------------------------------------- api */

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

/* Colour follows the entity, never its rank: a source keeps its slot for the
   life of the page, so filtering or a new arrival never repaints the others. */
function colorFor(sourceKey) {
  if (!state.colors.has(sourceKey)) {
    state.colors.set(sourceKey, (state.colors.size % 8) + 1);
  }
  return `var(--series-${state.colors.get(sourceKey)})`;
}
function resolvedColor(sourceKey) {
  const slot = state.colors.get(sourceKey) || 1;
  return getComputedStyle(document.body).getPropertyValue(`--series-${slot}`).trim();
}

/* ---------------------------------------------------------------- sources */

function renderSources() {
  const host = el("sources");
  if (!state.sources.length) {
    host.innerHTML = `<p class="empty">Waiting for the first packet…<br>
      <span class="empty-sub">Sources and fields appear as they arrive — nothing is
      configured in advance.</span></p>`;
    el("source-count").textContent = "";
    return;
  }

  const byPayload = new Map();
  for (const src of state.sources) {
    if (!byPayload.has(src.payload_name)) byPayload.set(src.payload_name, []);
    byPayload.get(src.payload_name).push(src);
  }
  el("source-count").textContent =
    `${state.sources.length} source${state.sources.length === 1 ? "" : "s"}`;

  const live = new Map((state.latest?.sources || []).map((s) => [s.source_key, s]));
  host.innerHTML = "";

  for (const [payload, sources] of byPayload) {
    const group = document.createElement("div");
    group.className = "payload-group";
    group.innerHTML = `<div class="payload-name">${escapeHtml(payload)}</div>`;

    for (const src of sources) {
      const info = live.get(src.source_key);
      // A finished flight is not "stalled" -- it is over. Only a live flight
      // can have a source that has stopped reporting.
      const stalled = state.live && isStalled(info);
      const node = document.createElement("div");
      node.className = "source" + (stalled ? " stalled" : "");

      const rate = info ? `${info.rate_hz.toFixed(1)} Hz` : "—";
      const count = info ? info.packet_count.toLocaleString() : "0";

      node.innerHTML = `
        <div class="source-head">
          <span class="source-dot" style="background:${colorFor(src.source_key)}"></span>
          <span class="source-name" title="${escapeHtml(src.source_key)}">${escapeHtml(
            sourceLabel(src))}</span>
          <span class="source-meta">${
            stalled ? '<span class="source-flag">STALLED</span> ' : ""}${rate} · ${count}</span>
        </div>`;

      const fields = document.createElement("div");
      fields.className = "fields";
      for (const field of src.fields) {
        fields.appendChild(fieldRow(src, field, info));
      }
      node.appendChild(fields);
      group.appendChild(node);
    }
    host.appendChild(group);
  }
}

function fieldRow(src, field, info) {
  const key = `${src.source_key}:${field.name}`;
  const row = document.createElement("label");
  const isText = field.kind !== "numeric";
  row.className = "field" + (isText ? " text-field" : "");

  const raw = info?.data?.[field.name];
  const shown = raw == null ? "—"
    : (typeof raw === "number" ? formatValue(raw) : String(raw));

  row.innerHTML = `
    ${isText ? "" : `<input type="checkbox" ${state.selected.has(key) ? "checked" : ""}>`}
    <span class="field-name" title="${escapeHtml(field.description || field.name)}">${
      escapeHtml(field.label || field.name)}</span>
    <span class="field-value">${escapeHtml(shown)}</span>
    <span class="field-unit">${escapeHtml(field.unit || "")}</span>`;

  if (!isText) {
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) state.selected.add(key); else state.selected.delete(key);
      syncCharts();
      persistSelection();
    });
  }
  return row;
}

/* The payload group header already carries the payload name, so the row shows
   the task. But a task name is whatever followed the last underscore, which for
   `ADC_Neck_1` is "1" -- meaningless on its own, so fall back to the full key. */
function sourceLabel(src) {
  const task = (src.task_name || "").trim();
  if (!task || /^\d+$/.test(task)) return src.source_key;
  return task;
}

/* Server time, as best this browser can tell.

   Staleness has to be measured on one clock. `time_epoch` is the payload's own
   RTC, which is real time only if a PCF8523 was found at boot -- otherwise it
   restarts at 2021-01-01 on every power cycle, and judging age against it pins
   a healthy source to STALLED forever while its values keep updating. So the
   comparison uses the server's reception clock, corrected for this machine's
   offset from it. */
function serverNow() {
  return Date.now() / 1000 - (state.clockOffset || 0);
}

function isStalled(info) {
  if (!info) return true;
  const heard = info.heard_epoch != null ? info.heard_epoch : info.time_epoch;
  const expected = info.rate_hz > 0 ? STALL_FACTOR / info.rate_hz : STALL_FLOOR_S;
  const age = serverNow() - heard;
  return age > Math.max(expected, STALL_FLOOR_S);
}

function renderReporters(reporters) {
  const host = el("reporters");
  if (!reporters.length) { host.innerHTML = `<p class="empty">No receptions yet.</p>`; return; }
  const top = Math.max(...reporters.map((r) => r.packets));
  host.innerHTML = reporters.map((r) => {
    // The split is in the tooltip because a station can be carrying the flight
    // on GPS alone, and a single merged number hides that.
    const parts = [];
    if (r.sensor != null) parts.push(`${r.sensor.toLocaleString()} sensor`);
    if (r.gps != null) parts.push(`${r.gps.toLocaleString()} GPS`);
    const title = parts.length ? `${escapeHtml(r.reporter_id)}: ${parts.join(", ")}` : "";
    return `
    <div class="reporter" title="${title}">
      <span class="reporter-name">${escapeHtml(r.reporter_id)}</span>
      <span class="reporter-bar"><span class="reporter-fill"
        style="width:${(r.packets / top * 100).toFixed(1)}%"></span></span>
      <span class="reporter-count">${r.packets.toLocaleString()}</span>
    </div>`;
  }).join("");
}

/* ----------------------------------------------------------------- charts */

function syncCharts() {
  const host = el("charts");
  const wanted = [...state.selected];

  for (const [key, entry] of state.charts) {
    if (!wanted.includes(key)) { entry.wrap.remove(); state.charts.delete(key); }
  }
  if (!wanted.length) {
    host.innerHTML = `<p class="empty">Select fields on the left to plot them.</p>`;
    return;
  }
  host.querySelector(".empty")?.remove();

  for (const key of wanted) {
    if (state.charts.has(key)) continue;
    const [sourceKey, name] = splitKey(key);

    const wrap = document.createElement("div");
    wrap.className = "chart";
    wrap.innerHTML = `
      <div class="chart-head">
        <span class="chart-key" style="background:${colorFor(sourceKey)}"></span>
        <span class="chart-title"></span>
        <span class="chart-sub"></span>
        <span class="chart-last"></span>
      </div>
      <canvas></canvas>`;
    host.appendChild(wrap);

    const chart = new Chart(wrap.querySelector("canvas"), {
      onHover: handleHover,
      onSelect: handleSelect,
    });
    state.charts.set(key, { chart, wrap, sourceKey, name });
  }
  loadSeries();
}

/* ------------------------------------------------------------ time range */

function rangeParams() {
  const { mode, seconds, start, end } = state.range;
  if (mode === "absolute" && start != null && end != null) {
    return { start: new Date(start * 1000).toISOString(),
             end: new Date(end * 1000).toISOString() };
  }
  if (mode === "relative" && seconds > 0) {
    // Anchor to the newest data, not to wall-clock: replaying a past flight
    // would otherwise ask for a window that ended hours ago and show nothing.
    const anchor = newestSampleEpoch();
    if (anchor != null) {
      return { start: new Date((anchor - seconds) * 1000).toISOString(),
               end: new Date(anchor * 1000).toISOString() };
    }
  }
  return {};
}

function newestSampleEpoch() {
  const times = (state.latest?.sources || []).map((s) => s.time_epoch).filter(Boolean);
  if (state.latest?.gps?.time_epoch) times.push(state.latest.gps.time_epoch);
  return times.length ? Math.max(...times) : null;
}

function setRange(next, { silent = false } = {}) {
  state.range = next;
  syncRangeToUrl();
  if (!silent) {
    el("range-select").value =
      next.mode === "all" ? "all"
      : next.mode === "relative" ? String(next.seconds)
      : "custom";
    el("range-select").querySelector('option[value="custom"]').hidden =
      next.mode !== "absolute";
  }
  renderRangeReadout();
  loadSeries();
  refreshTrack();
}

/* Keep the window in the address bar so a zoomed view can be pasted to someone
   else -- "look at the burst" is the normal thing to want to share. */
function syncRangeToUrl() {
  const url = new URL(location.href);
  url.searchParams.delete("range");
  url.searchParams.delete("from");
  url.searchParams.delete("to");

  if (state.range.mode === "relative") {
    url.searchParams.set("range", String(state.range.seconds));
  } else if (state.range.mode === "absolute") {
    url.searchParams.set("from", new Date(state.range.start * 1000).toISOString());
    url.searchParams.set("to", new Date(state.range.end * 1000).toISOString());
  }
  history.replaceState(null, "", url);
}

function rangeFromUrl() {
  const p = new URLSearchParams(location.search);
  const from = p.get("from"), to = p.get("to");
  if (from && to) {
    const start = Date.parse(from) / 1000, end = Date.parse(to) / 1000;
    if (isFinite(start) && isFinite(end) && end > start) {
      return { mode: "absolute", seconds: 0, start, end };
    }
  }
  const seconds = Number(p.get("range"));
  if (seconds > 0) return { mode: "relative", seconds, start: null, end: null };
  return null;
}

function renderRangeReadout() {
  const node = el("range-readout");
  const { mode } = state.range;
  if (mode === "all") { node.textContent = ""; return; }
  const p = rangeParams();
  if (!p.start) { node.textContent = ""; return; }
  const a = new Date(p.start), b = new Date(p.end);
  const span = (b - a) / 1000;
  node.textContent = `${a.toISOString().slice(11, 19)} – ${b.toISOString().slice(11, 19)} · ${
    span >= 3600 ? (span / 3600).toFixed(1) + " h" : Math.round(span / 60) + " min"}`;
}

/* Drag on any chart zooms every chart, because they share one x domain. */
function handleSelect(from, to) {
  if (from == null || to == null) {           // double-click resets
    setRange({ mode: "all", seconds: 0, start: null, end: null });
    return;
  }
  if (state.axis === "altitude") {
    // The x axis is metres here, so a drag is not a time window. Zoom the view
    // rather than pretending to filter by time.
    state.domain = { min: from, max: to };
    applyDomain();
    return;
  }
  setRange({ mode: "absolute", seconds: 0, start: from, end: to });
}

async function refreshTrack() {
  if (state.flightId == null) return;
  const params = new URLSearchParams({ max_points: "4000" });
  // A time window on the map is meaningless when the charts are plotting
  // against altitude -- the window is a chart concept there.
  if (state.axis === "time") {
    const { start, end } = rangeParams();
    if (start) { params.set("start", start); params.set("end", end); }
  }
  try {
    const track = await api(`/api/v1/flights/${state.flightId}/track?${params}`);
    map.setTrack(track);
    updateRampLegend(track);
    el("map-empty").hidden = track.n > 0;
  } catch (err) {
    console.error("track failed", err);
  }
}

/* Every chart shares one x domain, so a cursor at value X sits at the same
   screen position in all of them. The line is drawn at the exact value rather
   than snapped to any one series' nearest sample -- otherwise sources at
   different rates would each pull the line somewhere slightly different and the
   marker would visibly stagger down the stack. */
function applyDomain() {
  let min = state.domain.min, max = state.domain.max;

  if (min == null || max == null) {
    min = Infinity; max = -Infinity;
    for (const { chart } of state.charts.values()) {
      const x = chart.data?.x;
      if (!x || !x.length) continue;
      min = Math.min(min, x[0]);
      max = Math.max(max, x[x.length - 1]);
    }
    if (!isFinite(min)) { min = null; max = null; }
  }
  for (const { chart } of state.charts.values()) chart.setDomain(min, max);
}

function setCursor(value, clientX, clientY) {
  state.cursorX = value;
  for (const { chart } of state.charts.values()) chart.setCursor(value);
  renderFieldTiles();
  if (value == null) { el("tooltip").hidden = true; return; }
  if (clientX != null) positionTooltip(clientX, clientY);
}

function handleHover(hover) {
  if (!hover) { setCursor(null); return; }
  setCursor(hover.x, hover.clientX, hover.clientY);
}

function positionTooltip(clientX, clientY) {
  const tip = el("tooltip");
  const rows = [];
  for (const [key, entry] of state.charts) {
    const v = entry.chart.valueAtCursor();
    if (!v) continue;
    const [, name] = splitKey(key);
    rows.push(`<div><span class="tooltip-dim">${escapeHtml(name)}</span> <b>${
      formatValue(v.avg)}</b> ${escapeHtml(entry.unit || "")}</div>`);
  }
  const heading = state.axis === "altitude"
    ? `${Math.round(state.cursorX).toLocaleString()} m`
    : formatTime(state.cursorX);

  tip.innerHTML = `<div><b>${heading}</b></div>${rows.join("")}`;
  tip.hidden = false;

  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let left = clientX + pad;
  if (left + rect.width > window.innerWidth) left = clientX - rect.width - pad;
  let top = clientY + pad;
  if (top + rect.height > window.innerHeight) top = clientY - rect.height - pad;
  tip.style.left = `${Math.max(4, left)}px`;
  tip.style.top = `${Math.max(4, top)}px`;
}

/* ------------------------------------------------------------ field cards */

/* A checked field gets a card beside the altitude and ascent-rate tiles. With
   a cursor on the charts the cards read at the cursor instead of at "now", so
   the whole header describes the same instant as the marker line. */
function renderFieldTiles() {
  const host = el("field-tiles");
  const cards = [];

  for (const [key, entry] of state.charts) {
    const [sourceKey, name] = splitKey(key);
    const atCursor = state.cursorX != null ? entry.chart.valueAtCursor() : null;
    const live = (state.latest?.sources || []).find((s) => s.source_key === sourceKey);

    // The card must agree with the chart under it. Reading "latest" while a
    // window is open put the flight's final value beside a chart ending on the
    // window's -- the same quantity showing two numbers on one screen.
    const inView = entry.chart.data?.avg?.length
      ? entry.chart.data.avg[entry.chart.data.avg.length - 1]
      : null;
    const liveValue = typeof live?.data?.[name] === "number" ? live.data[name] : null;
    const windowed = state.range.mode !== "all";

    let value;
    if (atCursor) value = atCursor.avg;                 // what the marker points at
    else if (windowed) value = inView;                  // end of the window in view
    else value = liveValue != null ? liveValue : inView; // freshest, unzoomed

    cards.push(`
      <div class="tile${atCursor ? " at-cursor" : ""}">
        <div class="tile-label">
          <span class="tile-key" style="background:${colorFor(sourceKey)}"></span>
          ${escapeHtml(entry.label || name)}
        </div>
        <div class="tile-value">${value == null ? "—" : escapeHtml(formatValue(value))}</div>
        <div class="tile-unit">
          ${escapeHtml(entry.unit || "")}
          <span class="tile-source" title="${escapeHtml(sourceKey)}">${
            escapeHtml(sourceKey)}</span>
        </div>
      </div>`);
  }
  host.innerHTML = cards.join("");
}

async function loadSeries() {
  if (!state.charts.size || state.flightId == null) return;
  const fields = [...state.charts.keys()];
  const params = new URLSearchParams({ axis: state.axis, max_points: "900" });
  for (const f of fields) params.append("field", f);
  const { start, end } = rangeParams();
  if (start) { params.set("start", start); params.set("end", end); }

  let series;
  try {
    series = await api(`/api/v1/flights/${state.flightId}/series?${params}`);
  } catch (err) {
    console.error("series failed", err);
    return;
  }

  const bySpec = new Map(series.map((s) => [`${s.source}:${s.name}`, s]));
  for (const [key, entry] of state.charts) {
    const s = bySpec.get(key);
    entry.unit = s?.unit || "";
    const title = entry.wrap.querySelector(".chart-title");
    const sub = entry.wrap.querySelector(".chart-sub");
    const last = entry.wrap.querySelector(".chart-last");

    entry.label = s ? (s.label || s.name) : entry.name;
    title.textContent = entry.label;
    sub.textContent = `${entry.sourceKey}${s?.unit ? " · " + s.unit : ""}`;

    if (!s || !s.x.length) {
      entry.chart.setData({
        x: [], min: [], max: [], avg: [], color: resolvedColor(entry.sourceKey),
        axis: state.axis,
        emptyText: state.axis === "altitude" ? "No altitude resolved yet" : "No data",
      });
      last.textContent = "";
      continue;
    }
    entry.chart.setData({
      x: s.x, min: s.min, max: s.max, avg: s.avg,
      downsampled: s.downsampled,
      color: resolvedColor(entry.sourceKey),
      axis: state.axis,
    });
    last.textContent = `${formatValue(s.avg[s.avg.length - 1])} ${s.unit || ""}`.trim();
  }

  // Domain last: it needs every series loaded before it can span them all.
  if (state.axis === "time" || state.domain.min == null) {
    state.domain = { min: null, max: null };
  }
  applyDomain();
  renderFieldTiles();
}

/* ------------------------------------------------------------------- data */

async function loadFlights() {
  state.flights = await api("/api/v1/flights");
  const select = el("flight-select");
  select.innerHTML = state.flights.map((f) =>
    `<option value="${f.id}">${escapeHtml(f.name)} — ${f.packet_count.toLocaleString()} pkt${
      f.ended_at ? "" : " · open"}</option>`).join("")
    || `<option>No flights yet</option>`;

  if (state.flightId == null && state.flights.length) {
    // ?flight=N makes a past flight linkable -- the launch team sharing "look at
    // this bit" is the normal case after a flight, not an edge case.
    const asked = Number(new URLSearchParams(location.search).get("flight"));
    const found = state.flights.find((f) => f.id === asked);
    state.flightId = found ? found.id : state.flights[0].id;
  }
  if (state.flightId != null) select.value = String(state.flightId);

  const flight = state.flights.find((f) => f.id === state.flightId);
  state.live = !!flight && !flight.ended_at;
}

async function loadFlightData() {
  if (state.flightId == null) return;
  const [sources, latest, reporters] = await Promise.all([
    api(`/api/v1/flights/${state.flightId}/sources`),
    api(`/api/v1/flights/${state.flightId}/latest`),
    api(`/api/v1/flights/${state.flightId}/reporters`),
  ]);

  state.sources = sources;
  state.latest = latest;
  state.reporters = reporters;
  if (latest?.server_now) state.clockOffset = Date.now() / 1000 - latest.server_now;
  // Assign colour slots in a stable order so they never shuffle.
  for (const src of sources) colorFor(src.source_key);

  // Selection first: renderSources draws the checkboxes from it, and restoring
  // afterwards left the list claiming nothing was selected while the charts
  // below it plotted the defaults.
  restoreSelection();

  renderSources();
  renderReporters(reporters);
  renderTiles();
  map.endLabel = state.live ? "now" : "end";
  await refreshTrack();
  renderRangeReadout();

  renderFlightName();
  renderExportMenu();
  syncCharts();
}

function renderTiles() {
  const l = state.latest;
  const gps = l?.gps;
  el("kpi-alt").textContent = gps?.altitude != null
    ? Math.round(gps.altitude).toLocaleString() : "—";
  el("kpi-ascent").textContent = gps?.ascent_rate != null
    ? gps.ascent_rate.toFixed(1) : "—";

  // Say so when the altitude on screen is the last known one rather than a
  // current reading. Presenting a stale figure as live is worse than a gap.
  const status = l?.gps_status;
  const stale = !!status && status.position_is_stale;
  const unit = el("kpi-alt").parentElement.querySelector(".tile-unit");
  unit.textContent = stale
    ? `m · no fix${status.num_sv != null ? ` (${status.num_sv} sv)` : ""}`
    : "m";
  unit.classList.toggle("stale", stale);
  el("kpi-peak").textContent = l?.peak_altitude != null
    ? Math.round(l.peak_altitude).toLocaleString() : "—";
  const packets = (l?.sources || []).reduce((a, s) => a + s.packet_count, 0);
  el("kpi-packets").textContent = packets.toLocaleString();
}

function updateRampLegend(track) {
  const legend = el("ramp-legend");
  const alts = (track.alt || []).filter((a) => a != null);
  if (alts.length < 2) { legend.hidden = true; return; }
  legend.hidden = false;
  el("ramp-lo").textContent = `${Math.round(Math.min(...alts)).toLocaleString()} m`;
  el("ramp-hi").textContent = `${Math.round(Math.max(...alts)).toLocaleString()} m`;
}

/* -------------------------------------------------------------------- sse */

function connectEvents() {
  state.events?.close();
  state.events = null;
  if (state.flightId == null) return;

  // A closed flight receives nothing further, so opening a live stream for it
  // would hold a connection open forever to deliver nothing.
  if (!state.live) {
    setStatus("replay", "Replay");
    return;
  }

  const source = new EventSource(`/events?flight=${state.flightId}`);
  state.events = source;

  source.onopen = () => setStatus(state.live ? "live" : "replay",
                                 state.live ? "Live" : "Replay");
  source.onerror = () => setStatus("stalled", "Reconnecting");
  source.onmessage = (e) => {
    let event;
    try { event = JSON.parse(e.data); } catch { return; }
    applyUpdate(event);
  };
}

/* A live event refreshes the numbers immediately; the charts redraw on a timer.
   Re-querying every series on every packet would be pointless work at 2 Hz. */
function applyUpdate(event) {
  setStatus("live", "Live");

  if (event.new_sources?.length || event.new_fields?.length) {
    loadFlightData();   // discovery changed the shape of the page
    return;
  }

  for (const [sourceKey, packet] of Object.entries(event.sources || {})) {
    const info = (state.latest?.sources || []).find((s) => s.source_key === sourceKey);
    if (info) {
      info.data = packet.data;
      info.count = packet.count;
      info.time = packet.time;
      info.time_epoch = Date.parse(packet.time) / 1000;
      // The event arriving *is* the reception, so this is the freshest possible
      // answer on the clock staleness is measured against.
      info.heard_epoch = serverNow();
      info.packet_count += 1;
    }
  }
  if (event.gps?.length && state.latest) {
    // Packets without a fix now arrive with null coordinates rather than being
    // dropped. Taking the last point blindly would blank the tiles every time
    // the receiver missed a lock, so position comes from the last point that
    // actually had one -- while the status below still reflects the newest.
    const positioned = event.gps.filter((p) => p.lat != null);
    const fix = positioned[positioned.length - 1];
    if (fix) {
      state.latest.gps = {
        ...(state.latest.gps || {}),
        altitude: fix.alt, ascent_rate: fix.ascent_rate,
        latitude: fix.lat, longitude: fix.lon,
      };
      if (fix.alt != null) {
        state.latest.peak_altitude = Math.max(state.latest.peak_altitude ?? -Infinity, fix.alt);
      }
    }
    const newest = event.gps[event.gps.length - 1];
    state.latest.gps_status = {
      ...(state.latest.gps_status || {}),
      num_sv: newest.num_sv,
      has_position: newest.lat != null,
      position_is_stale: newest.lat == null && !!state.latest.gps,
    };
    // Don't extend the track past a window the user pinned by dragging: new
    // fixes fall outside it by definition.
    if (map.track && state.range.mode !== "absolute") {
      for (const p of event.gps) {
        if (p.lat == null) continue;
        map.track.t.push(Date.parse(p.t) / 1000);
        map.track.lat.push(p.lat); map.track.lon.push(p.lon); map.track.alt.push(p.alt);
      }
      map.setTrack(map.track);
      el("map-empty").hidden = true;
    }
  }

  renderSources();
  renderTiles();
  state.dirty = true;
}

function setStatus(stateName, text) {
  const node = el("link-status");
  node.dataset.state = stateName;
  node.querySelector(".status-text").textContent = text;
}

/* ----------------------------------------------------------------- export */

function renderExportMenu() {
  if (state.flightId == null) return;
  const base = `/api/v1/flights/${state.flightId}`;
  el("dl-csv").href = `${base}/export.csv`;
  el("dl-jsonl").href = `${base}/export.jsonl`;
  el("dl-gps").href = `${base}/gps.csv`;
  el("dl-cbor").href = `${base}/export.cbor`;

  el("dl-sources").innerHTML = state.sources.map((s) =>
    `<a class="menu-item" href="${base}/export.csv?source=${
      encodeURIComponent(s.source_key)}">${escapeHtml(s.source_key)}</a>`).join("")
    || `<div class="menu-item" style="opacity:.6">No sources yet</div>`;
}

function toggleExportMenu(open) {
  const menu = el("export-menu");
  const next = open ?? menu.hidden;
  menu.hidden = !next;
  el("export-toggle").setAttribute("aria-expanded", String(next));
}

/* Renaming happens in place on the title -- a flight gets a real name ("RAB-9")
   once, right after it lands, and a dialog for that is more ceremony than the
   job deserves. */
function wireRename() {
  const title = el("flight-name");

  const commit = async () => {
    title.contentEditable = "false";
    const name = title.textContent.trim();
    const flight = state.flights.find((f) => f.id === state.flightId);
    if (!name || !flight || name === flight.name) { renderFlightName(); return; }
    try {
      await api(`/api/v1/flights/${state.flightId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await loadFlights();
      renderFlightName();
    } catch (err) {
      console.error("rename failed", err);
      renderFlightName();
    }
  };

  title.addEventListener("click", () => {
    if (state.flightId == null) return;
    title.contentEditable = "true";
    title.focus();
    getSelection().selectAllChildren(title);
  });
  title.addEventListener("blur", commit);
  title.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); title.blur(); }
    if (e.key === "Escape") { renderFlightName(); title.blur(); }
  });
}

function renderFlightName() {
  const flight = state.flights.find((f) => f.id === state.flightId);
  el("flight-name").textContent = flight ? flight.name : "Wenet Telemetry";
}

/* ------------------------------------------------------------------ misc */

function splitKey(key) {
  const i = key.lastIndexOf(":");
  return [key.slice(0, i), key.slice(i + 1)];
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function persistSelection() {
  try {
    localStorage.setItem(`wenet.fields.${state.flightId}`, JSON.stringify([...state.selected]));
  } catch { /* private browsing */ }
}

function restoreSelection() {
  if (state.selected.size) return;
  try {
    const saved = JSON.parse(localStorage.getItem(`wenet.fields.${state.flightId}`) || "[]");
    for (const key of saved) state.selected.add(key);
  } catch { /* ignore */ }

  if (!state.selected.size) {
    // A useful default beats an empty page: the first numeric field of each
    // source, capped so a busy payload does not open thirty charts.
    for (const src of state.sources.slice(0, 3)) {
      const field = src.fields.find((f) => f.kind === "numeric");
      if (field) state.selected.add(`${src.source_key}:${field.name}`);
    }
  }
}

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  el("theme-icon").textContent = theme === "dark" ? "●" : theme === "light" ? "○" : "◐";
  // Canvas colours are read from CSS variables, so both surfaces must redraw.
  map.draw();
  for (const { chart } of state.charts.values()) chart.draw();
}

/* ------------------------------------------------------------------- boot */

const map = new TrackMap(el("map"));

el("map-fit").addEventListener("click", () => map.fit());
el("map-in").addEventListener("click", () => map.zoomBy(1));
el("map-out").addEventListener("click", () => map.zoomBy(-1));

for (const btn of document.querySelectorAll(".seg")) {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.axis = btn.dataset.axis;
    // Metres and seconds are not the same domain; carrying one over to the
    // other would zoom to a nonsense window.
    state.domain = { min: null, max: null };
    setCursor(null);
    loadSeries();
    refreshTrack();
  });
}

el("range-select").addEventListener("change", (e) => {
  const value = e.target.value;
  if (value === "all") {
    state.domain = { min: null, max: null };
    setRange({ mode: "all", seconds: 0, start: null, end: null }, { silent: true });
  } else if (value !== "custom") {
    state.domain = { min: null, max: null };
    setRange({ mode: "relative", seconds: Number(value), start: null, end: null },
             { silent: true });
  }
});

el("range-reset").addEventListener("click", () => {
  state.domain = { min: null, max: null };
  setCursor(null);
  setRange({ mode: "all", seconds: 0, start: null, end: null });
});

el("flight-select").addEventListener("change", (e) => {
  state.flightId = Number(e.target.value);
  state.selected.clear();
  for (const [, entry] of state.charts) entry.wrap.remove();
  state.charts.clear();
  map.userMoved = false;
  boot();
});

el("export-toggle").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleExportMenu();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".menu")) toggleExportMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") toggleExportMenu(false);
});
wireRename();

el("theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : current === "light" ? null : "dark";
  if (next) localStorage.setItem("wenet.theme", next);
  else localStorage.removeItem("wenet.theme");
  applyTheme(next);
});

window.addEventListener("resize", () => {
  for (const { chart } of state.charts.values()) chart.draw();
});

/* Charts and the reporter tally both refresh here. The tally was previously
   drawn only by loadFlightData(), which runs at boot and then only when a new
   source or field appears -- so on a steady flight the bars held their
   page-load values indefinitely. One GROUP BY every 5 s while live. */
setInterval(async () => {
  if (!state.live || !state.dirty) return;
  state.dirty = false;
  loadSeries();
  try {
    state.reporters = await api(`/api/v1/flights/${state.flightId}/reporters`);
    renderReporters(state.reporters);
  } catch {
    // The link indicator already reports trouble; don't double-report it.
  }
}, REFRESH_MS);

// Sources can go quiet without any event arriving, so re-evaluate stalled state
// on a timer rather than only when data shows up. Pointless once a flight has
// closed -- nothing about it can change again.
setInterval(() => { if (state.live) renderSources(); }, 3000);

async function boot() {
  try {
    await loadFlights();
    if (state.flightId == null) {
      setStatus("idle", "No flights");
      return;
    }
    await loadFlightData();
    connectEvents();
    setStatus(state.live ? "live" : "replay", state.live ? "Live" : "Replay");
  } catch (err) {
    console.error(err);
    setStatus("stalled", "Server unreachable");
  }
}

/* URL overrides, so a view can be linked rather than described: ?flight=12
   picks the flight, ?axis=altitude opens the sounding view, ?theme=light pins a
   chase-vehicle tablet to the sun-readable mode. */
const params = new URLSearchParams(location.search);

if (params.get("axis") === "altitude") {
  state.axis = "altitude";
  document.querySelectorAll(".seg").forEach((b) =>
    b.classList.toggle("active", b.dataset.axis === "altitude"));
}

const urlRange = rangeFromUrl();
if (urlRange) {
  state.range = urlRange;
  const select = el("range-select");
  select.value = urlRange.mode === "relative" ? String(urlRange.seconds) : "custom";
  select.querySelector('option[value="custom"]').hidden = urlRange.mode !== "absolute";
}

const urlTheme = params.get("theme");
applyTheme(urlTheme === "light" || urlTheme === "dark"
  ? urlTheme
  : localStorage.getItem("wenet.theme"));

boot();
