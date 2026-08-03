/* A small time/altitude series chart on canvas.
 *
 * Written by hand rather than pulled from a charting library because the server
 * has no outbound network access to vendor one, and a CDN reference that cannot
 * be tested is worse than 200 lines that can. The drawing rules follow the
 * house style: 2px lines, a 10% wash for the min/max band, hairline solid
 * gridlines one step off the surface, and axis text in text tokens rather than
 * the series colour.
 *
 * One series per chart. These are small multiples -- fields discovered at
 * runtime have unrelated units (hPa, °C, m/s²), and stacking them on a shared
 * axis would invent correlations that are not in the data.
 */

const PAD = { top: 8, right: 54, bottom: 18, left: 46 };

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

/** Ticks on clean 1/2/5 x 10^n boundaries. */
function niceTicks(lo, hi, target = 4) {
  if (!isFinite(lo) || !isFinite(hi)) return [];
  if (lo === hi) return [lo];
  const raw = (hi - lo) / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  // Thresholds at 1.5/3/7, not 2/5. The coarser mapping rounded a step of 2.2
  // all the way up to 5, which over a -44..14 range left exactly one tick on
  // the axis and no way to read the chart.
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(v);
  return out;
}

function formatValue(v, span) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (span != null && span > 0) {
    // Enough decimals to distinguish neighbouring ticks, and no more.
    const decimals = Math.max(0, Math.min(4, 1 - Math.floor(Math.log10(span))));
    return v.toFixed(decimals);
  }
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toPrecision(3);
}

function formatTime(epoch) {
  const d = new Date(epoch * 1000);
  return d.toISOString().slice(11, 19);
}

export class Chart {
  constructor(canvas, { onHover, onSelect } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.data = null;
    this.cursorX = null;      // in data units, shared across every chart
    this.onHover = onHover;
    this.onSelect = onSelect;
    this.drag = null;

    canvas.addEventListener("pointermove", (e) => this._pointer(e));
    canvas.addEventListener("pointerdown", (e) => this._down(e));
    canvas.addEventListener("pointerup", (e) => this._up(e));
    canvas.addEventListener("pointercancel", () => { this.drag = null; this.draw(); });
    canvas.addEventListener("pointerleave", () => {
      if (this.drag) return;    // a drag may legitimately leave the canvas
      if (this.onHover) this.onHover(null, this);
    });
    canvas.addEventListener("dblclick", () => {
      if (this.onSelect) this.onSelect(null, null, this);
    });
  }

  setData(series) {
    this.data = series;
    this.draw();
  }

  /* The x domain is imposed from outside so every chart in the stack shares
     it. Without that, two sources with different spans put the same instant at
     different screen positions and a "shared" cursor line is a lie. */
  setDomain(min, max) {
    this.domainMin = min;
    this.domainMax = max;
    this.draw();
  }

  _plotBox() {
    const { width, height } = this.canvas.getBoundingClientRect();
    return {
      x0: PAD.left, y0: PAD.top,
      x1: width - PAD.right, y1: height - PAD.bottom,
      width, height,
    };
  }

  /** Screen x -> data units, using the shared domain. */
  _valueAtPixel(clientX) {
    const box = this._plotBox();
    const rect = this.canvas.getBoundingClientRect();
    const t = (clientX - rect.left - box.x0) / Math.max(box.x1 - box.x0, 1);
    return this.xMin + Math.max(0, Math.min(1, t)) * (this.xMax - this.xMin);
  }

  _pointer(e) {
    if (this.xMin == null) return;
    const value = this._valueAtPixel(e.clientX);

    if (this.drag) {
      this.drag.current = value;
      this.draw();
      return;
    }
    if (this.onHover) {
      this.onHover({ x: value, clientX: e.clientX, clientY: e.clientY }, this);
    }
  }

  _down(e) {
    if (this.xMin == null || e.button !== 0) return;
    this.drag = { start: this._valueAtPixel(e.clientX), current: null };
    this.canvas.setPointerCapture(e.pointerId);
  }

  _up(e) {
    if (!this.drag) return;
    const { start } = this.drag;
    const end = this._valueAtPixel(e.clientX);
    this.drag = null;
    if (this.canvas.hasPointerCapture?.(e.pointerId)) {
      this.canvas.releasePointerCapture(e.pointerId);
    }

    const span = Math.abs(end - start);
    const full = Math.abs(this.xMax - this.xMin);
    // A click is not a zoom. Anything under ~1% of the width is a stray drag.
    if (span > full * 0.01 && this.onSelect) {
      this.onSelect(Math.min(start, end), Math.max(start, end), this);
    } else {
      this.draw();
    }
  }

  /** Put the shared cursor at an exact x value, whether or not this chart has
      a sample there. The line is the reference; the dot is this series' answer. */
  setCursor(value) {
    this.cursorX = value;
    this.draw();
  }

  /** Index of the sample nearest an x value, or null if this chart has none. */
  nearestIndex(value) {
    const x = this.data?.x;
    if (!x || !x.length || value == null) return null;
    let lo = 0, hi = x.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (x[mid] < value) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(x[lo - 1] - value) < Math.abs(x[lo] - value)) lo -= 1;
    return lo;
  }

  draw() {
    const ctx = this.ctx;
    const box = this._plotBox();
    const dpr = window.devicePixelRatio || 1;

    if (this.canvas.width !== Math.round(box.width * dpr) ||
        this.canvas.height !== Math.round(box.height * dpr)) {
      this.canvas.width = Math.round(box.width * dpr);
      this.canvas.height = Math.round(box.height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, box.width, box.height);

    const d = this.data;
    if (!d || !d.x.length) {
      // Keep the domain so a cursor line still crosses an empty panel: a source
      // that stopped reporting is information, and a gap in the stack shows it.
      this.xMin = this.domainMin;
      this.xMax = this.domainMax;
      ctx.fillStyle = cssVar("--text-muted");
      ctx.font = "11px " + cssVar("--font");
      ctx.textAlign = "center";
      ctx.fillText(d && d.emptyText ? d.emptyText : "No data", box.width / 2, box.height / 2);
      if (this.xMin != null && this.cursorX != null) {
        const px = box.x0 + ((this.cursorX - this.xMin) / (this.xMax - this.xMin))
                 * (box.x1 - box.x0);
        if (px >= box.x0 && px <= box.x1) {
          ctx.strokeStyle = cssVar("--axis");
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(Math.round(px) + 0.5, box.y0);
          ctx.lineTo(Math.round(px) + 0.5, box.y1);
          ctx.stroke();
        }
      }
      return;
    }

    const grid = cssVar("--grid");
    const axis = cssVar("--axis");
    const muted = cssVar("--text-muted");
    const surface = cssVar("--surface-1");
    const color = d.color;

    // The imposed domain wins, so every chart in the stack maps the same value
    // to the same screen position.
    this.xMin = this.domainMin != null ? this.domainMin : d.x[0];
    this.xMax = this.domainMax != null ? this.domainMax : d.x[d.x.length - 1];
    if (this.xMax === this.xMin) this.xMax = this.xMin + 1;

    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < d.x.length; i++) {
      if (d.min[i] < lo) lo = d.min[i];
      if (d.max[i] > hi) hi = d.max[i];
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const headroom = (hi - lo) * 0.08;
    lo -= headroom; hi += headroom;

    const sx = (v) => box.x0 + ((v - this.xMin) / (this.xMax - this.xMin)) * (box.x1 - box.x0);
    const sy = (v) => box.y1 - ((v - lo) / (hi - lo)) * (box.y1 - box.y0);

    // --- grid: hairline, solid, one step off the surface -------------------
    const ticks = niceTicks(lo, hi, 3);
    const tickSpan = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : (hi - lo);
    ctx.lineWidth = 1;
    ctx.strokeStyle = grid;
    ctx.font = "10px " + cssVar("--font");
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(sy(t)) + 0.5;
      if (y < box.y0 || y > box.y1) continue;
      ctx.beginPath();
      ctx.moveTo(box.x0, y);
      ctx.lineTo(box.x1, y);
      ctx.stroke();
      ctx.fillText(formatValue(t, tickSpan), box.x0 - 6, y);
    }

    ctx.strokeStyle = axis;
    ctx.beginPath();
    ctx.moveTo(box.x0 + 0.5, box.y0);
    ctx.lineTo(box.x0 + 0.5, box.y1 + 0.5);
    ctx.lineTo(box.x1, box.y1 + 0.5);
    ctx.stroke();

    // x labels: ends only, so they cannot collide
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    const fmtX = d.axis === "altitude"
      ? (v) => Math.round(v).toLocaleString() + " m"
      : formatTime;
    ctx.fillText(fmtX(this.xMin), box.x0, box.y1 + 4);
    ctx.textAlign = "right";
    ctx.fillText(fmtX(this.xMax), box.x1, box.y1 + 4);

    // --- min/max band: a 10% wash, never a saturated block -----------------
    if (d.downsampled) {
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.10;
      ctx.beginPath();
      ctx.moveTo(sx(d.x[0]), sy(d.max[0]));
      for (let i = 1; i < d.x.length; i++) ctx.lineTo(sx(d.x[i]), sy(d.max[i]));
      for (let i = d.x.length - 1; i >= 0; i--) ctx.lineTo(sx(d.x[i]), sy(d.min[i]));
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // --- the series: 2px, round join and cap -------------------------------
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < d.x.length; i++) {
      const px = sx(d.x[i]), py = sy(d.avg[i]);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // End marker with a 2px surface ring, plus the one direct label we allow.
    const lastX = sx(d.x[d.x.length - 1]);
    const lastY = sy(d.avg[d.avg.length - 1]);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = surface;
    ctx.stroke();

    // --- drag-to-zoom selection --------------------------------------------
    if (this.drag && this.drag.current != null) {
      const a = sx(Math.min(this.drag.start, this.drag.current));
      const b = sx(Math.max(this.drag.start, this.drag.current));
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.12;
      ctx.fillRect(a, box.y0, b - a, box.y1 - box.y0);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(a) + 0.5, box.y0); ctx.lineTo(Math.round(a) + 0.5, box.y1);
      ctx.moveTo(Math.round(b) + 0.5, box.y0); ctx.lineTo(Math.round(b) + 0.5, box.y1);
      ctx.stroke();
    }

    // --- shared cursor -----------------------------------------------------
    // Drawn at the exact x value, not snapped to this series' nearest sample,
    // so the line is continuous down the stack even where sources disagree on
    // rate or have gaps. The dot is what *this* series reads there.
    if (this.cursorX != null) {
      const hx = sx(this.cursorX);
      if (hx >= box.x0 && hx <= box.x1) {
        ctx.strokeStyle = axis;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(hx) + 0.5, box.y0);
        ctx.lineTo(Math.round(hx) + 0.5, box.y1);
        ctx.stroke();

        const idx = this.nearestIndex(this.cursorX);
        if (idx != null) {
          ctx.beginPath();
          ctx.arc(sx(d.x[idx]), sy(d.avg[idx]), 4, 0, Math.PI * 2);
          ctx.fillStyle = color;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = surface;
          ctx.stroke();
        }
      }
    }
  }

  valueAt(index) {
    if (!this.data || index == null || index >= this.data.x.length) return null;
    return {
      x: this.data.x[index],
      avg: this.data.avg[index],
      min: this.data.min[index],
      max: this.data.max[index],
    };
  }

  /** What this series reads at a shared cursor position. */
  valueAtCursor() {
    return this.valueAt(this.nearestIndex(this.cursorX));
  }
}

export { formatValue, formatTime };
