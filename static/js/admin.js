/* Admin panel: rename, annotate, close, hide, merge and split flights.
 *
 * Authentication is the server's: logging in sets an HttpOnly cookie, and every
 * request below simply carries it. Nothing here is trusted -- a 401 from any
 * call sends the page back to the login form.
 */

const el = (id) => document.getElementById(id);

const state = { flights: [], showHidden: true };

try {
  const theme = localStorage.getItem("wenet.theme");
  if (theme) document.documentElement.setAttribute("data-theme", theme);
} catch { /* storage blocked: follow the system theme */ }

class AuthError extends Error {}

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) throw new AuthError("login required");
  if (!res.ok) {
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail ?? detail; } catch { /* plain text */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});
const patch = (path, body) => api(path, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Stored times are UTC ISO strings; show them as UTC, minute precision. */
function fmt(ts) {
  if (!ts) return "—";
  return ts.replace("T", " ").replace(/:\d\d(\.\d+)?Z$/, "Z");
}

function plural(n, one, many = `${one}s`) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function duration(f) {
  const end = Date.parse(f.ended_at || f.last_activity);
  const mins = Math.max(0, Math.round((end - Date.parse(f.started_at)) / 60000));
  return mins >= 90 ? `${(mins / 60).toFixed(1)} h` : `${mins} min`;
}

/* ------------------------------------------------------------- screens */

function show(screen) {
  for (const id of ["disabled", "login", "panel"]) el(id).hidden = id !== screen;
  el("logout").hidden = screen !== "panel";
  if (screen === "login") el("password").focus();
}

async function start() {
  const session = await api("/api/v1/admin/session");
  if (!session.enabled) return show("disabled");
  if (!session.authenticated) return show("login");
  show("panel");
  await refresh();
}

async function guarded(work) {
  el("panel-error").textContent = "";
  try {
    await work();
  } catch (err) {
    if (err instanceof AuthError) return show("login");
    el("panel-error").textContent = err.message;
  }
}

async function refresh() {
  await guarded(async () => {
    state.flights = await api("/api/v1/admin/flights");
    render();
  });
}

/* ---------------------------------------------------------------- list */

function label(f) {
  return `#${f.id} ${f.name}`;
}

/* Which way a merge should go: into the flight that knows its transmitter,
   so the result keeps the callsign and its name; otherwise into the older. */
function mergePair(a, b) {
  if (a.payload_callsign && !b.payload_callsign) return { target: a, from: b };
  if (b.payload_callsign && !a.payload_callsign) return { target: b, from: a };
  return a.id < b.id ? { target: a, from: b } : { target: b, from: a };
}

function render() {
  const byId = new Map(state.flights.map((f) => [f.id, f]));
  const shown = state.flights.filter((f) => state.showHidden || !f.hidden);
  const open = state.flights.filter((f) => !f.ended_at).length;
  const hidden = state.flights.filter((f) => f.hidden).length;
  el("summary").textContent =
    `${state.flights.length} flights · ${open} open · ${hidden} hidden`;

  el("flights").innerHTML = shown.map((f) => {
    const call = f.payload_callsign
      ? `<span class="badge badge-call">${escapeHtml(f.payload_callsign)}</span>`
      : `<span class="badge badge-untagged">untagged</span>`;
    const status = f.ended_at
      ? `<span class="badge">closed</span>`
      : `<span class="badge badge-open">open</span>`;
    const hiddenBadge = f.hidden ? `<span class="badge">hidden</span>` : "";

    const hints = f.overlaps.map((otherId) => {
      const other = byId.get(otherId);
      if (!other) return "";
      const { target, from } = mergePair(f, other);
      const verb = target.id === f.id ? `Absorb #${from.id}` : `Merge into #${target.id}`;
      return `<div class="hint">Overlaps ${escapeHtml(label(other))}
        ${other.payload_callsign ? `(${escapeHtml(other.payload_callsign)})` : "(untagged)"}
        — probably one flight split in two.
        <button class="btn" type="button" data-act="merge" data-target="${target.id}"
          data-from="${from.id}">${verb}</button></div>`;
    }).join("");

    const mergeOptions = state.flights
      .filter((o) => o.id !== f.id)
      .filter((o) => !o.payload_callsign || !f.payload_callsign
        || o.payload_callsign === f.payload_callsign)
      .map((o) => `<option value="${o.id}">${escapeHtml(label(o))}</option>`).join("");

    return `<article class="flight" data-id="${f.id}" data-hidden="${f.hidden ? 1 : 0}">
      <div class="flight-head">
        <span class="flight-id">#${f.id}</span>
        <input class="flight-name" type="text" value="${escapeHtml(f.name)}"
          aria-label="Name of flight ${f.id}" data-act="rename">
        <div class="flight-actions">
          <a class="btn" href="/?flight=${f.id}" target="_blank" rel="noopener">View</a>
          ${f.ended_at ? "" : `<button class="btn" type="button" data-act="close">Close</button>`}
          <button class="btn" type="button" data-act="hide">${f.hidden ? "Unhide" : "Hide"}</button>
        </div>
      </div>
      <div class="flight-meta">
        ${call} ${status} ${hiddenBadge}
        <span>${fmt(f.started_at)} → ${f.ended_at ? fmt(f.ended_at) : "now"} (${duration(f)})</span>
        <span>${plural(f.packet_count, "packet")}</span>
        <span>${plural(f.gps_count, "fix", "fixes")}</span>
        <span>${plural(f.source_count, "source")}</span>
      </div>
      ${hints}
      <details class="flight-more">
        <summary>Notes, merge, split</summary>
        <div class="more-grid">
          <fieldset>
            <legend>Notes</legend>
            <textarea data-field="notes" aria-label="Notes for flight ${f.id}">${
              escapeHtml(f.notes)}</textarea>
            <div class="row"><button class="btn" type="button" data-act="notes">Save notes</button></div>
          </fieldset>
          <fieldset>
            <legend>Merge this flight into another</legend>
            ${mergeOptions ? `<div class="row">
              <select data-field="merge-target" aria-label="Merge flight ${f.id} into">${mergeOptions}</select>
              <button class="btn btn-danger" type="button" data-act="merge-into">Merge</button>
            </div>` : `<p class="field-help">No compatible flight to merge with.</p>`}
            <p class="field-help">This flight's data moves into the chosen one and
              this flight is deleted. Different callsigns cannot be merged.</p>
          </fieldset>
          <fieldset>
            <legend>Split</legend>
            <div class="row">
              <input type="text" data-field="split-at" placeholder="2026-10-24T14:00:00Z"
                aria-label="Split flight ${f.id} at (UTC)">
              <button class="btn btn-danger" type="button" data-act="split">Split</button>
            </div>
            <p class="field-help">Everything at or after this UTC time becomes a new flight.</p>
          </fieldset>
        </div>
      </details>
    </article>`;
  }).join("") || `<p class="empty">No flights.</p>`;
}

/* ------------------------------------------------------------- actions */

async function onAction(event) {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const card = button.closest(".flight");
  const id = Number(card.dataset.id);
  const flight = state.flights.find((f) => f.id === id);
  const act = button.dataset.act;

  if (act === "merge" || act === "merge-into") {
    const target = act === "merge" ? Number(button.dataset.target)
      : Number(card.querySelector('[data-field="merge-target"]').value);
    const from = act === "merge" ? Number(button.dataset.from) : id;
    const t = state.flights.find((f) => f.id === target);
    const s = state.flights.find((f) => f.id === from);
    if (!confirm(`Merge ${label(s)} into ${label(t)}?\n\n` +
                 `#${from} will be deleted and its data moved into #${target}. ` +
                 `This cannot be undone except by splitting.`)) return;
    return run(button, () => post(`/api/v1/flights/${target}/merge?from=${from}`));
  }
  if (act === "close") {
    return run(button, () => post(`/api/v1/flights/${id}/close`));
  }
  if (act === "hide") {
    if (!flight.hidden && !flight.ended_at
        && !confirm(`${label(flight)} is open. Hiding it also closes it, so the ` +
                    `next packet from this transmitter starts a new, visible flight.`)) return;
    return run(button, () => patch(`/api/v1/flights/${id}`, { hidden: !flight.hidden }));
  }
  if (act === "notes") {
    const notes = card.querySelector('[data-field="notes"]').value;
    return run(button, () => patch(`/api/v1/flights/${id}`, { notes }));
  }
  if (act === "split") {
    const at = card.querySelector('[data-field="split-at"]').value.trim();
    if (!at) return;
    if (!confirm(`Split ${label(flight)} at ${at}?`)) return;
    return run(button, () => post(`/api/v1/flights/${id}/split?at=${encodeURIComponent(at)}`));
  }
}

/* A merge of a real flight takes seconds -- long enough that a button that
   merely greys out looks like nothing happened. */
async function run(button, work) {
  const text = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  await guarded(async () => {
    await work();
    state.flights = await api("/api/v1/admin/flights");
    render();
  });
  // Usually re-rendered away by now; restored for the failure case.
  button.disabled = false;
  button.textContent = text;
}

/* Renaming commits on Enter or on leaving the field, as on the display. */
async function onRename(event) {
  const input = event.target;
  if (input.dataset.act !== "rename") return;
  const id = Number(input.closest(".flight").dataset.id);
  const flight = state.flights.find((f) => f.id === id);
  const name = input.value.trim();
  if (!name || name === flight.name) { input.value = flight.name; return; }
  await guarded(async () => {
    const row = await patch(`/api/v1/flights/${id}`, { name });
    flight.name = row.name;
  });
}

el("flights").addEventListener("click", onAction);
el("flights").addEventListener("change", onRename);
el("flights").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.dataset.act === "rename") event.target.blur();
});

el("show-hidden").addEventListener("change", (event) => {
  state.showHidden = event.target.checked;
  render();
});

el("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("login-error").textContent = "";
  try {
    await api("/api/v1/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: el("password").value }),
    });
    el("password").value = "";
    await start();
  } catch (err) {
    el("login-error").textContent =
      err instanceof AuthError ? "Wrong password." : err.message;
  }
});

el("logout").addEventListener("click", async () => {
  await post("/api/v1/admin/logout").catch(() => {});
  show("login");
});

start().catch((err) => {
  show("disabled");
  el("disabled").querySelector("p").textContent = `Could not reach the server: ${err.message}`;
});
