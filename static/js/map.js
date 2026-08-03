/* A minimal slippy map on canvas: OSM tiles, pan, zoom, and the flight track.
 *
 * Same reasoning as chart.js -- no network to vendor Leaflet, and an untested
 * CDN reference is worse than code that can be verified. The tile scheme is
 * standard Web Mercator z/x/y, so swapping in a real map library later is a
 * contained change.
 *
 * If tiles fail to load -- an offline chase vehicle, a blocked tile server --
 * the track still draws over a plain grid. Losing the basemap must not lose the
 * flight path.
 */

const TILE = 256;
const MAX_ZOOM = 18;
const MIN_ZOOM = 2;

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

/* Web Mercator, in "world pixels" at the current zoom. */
function lonToX(lon, scale) { return (lon + 180) / 360 * scale; }
function latToY(lat, scale) {
  const s = Math.sin(lat * Math.PI / 180);
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale;
}
function xToLon(x, scale) { return x / scale * 360 - 180; }
function yToLat(y, scale) {
  const n = Math.PI - 2 * Math.PI * y / scale;
  return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}

/* The sequential blue ramp from the palette: one hue, light to dark. */
const RAMP = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"];

function rampColor(t) {
  const clamped = Math.max(0, Math.min(1, t));
  const pos = clamped * (RAMP.length - 1);
  const i = Math.min(RAMP.length - 2, Math.floor(pos));
  const f = pos - i;
  const a = parseInt(RAMP[i].slice(1), 16);
  const b = parseInt(RAMP[i + 1].slice(1), 16);
  const mix = (shift) => {
    const av = (a >> shift) & 255, bv = (b >> shift) & 255;
    return Math.round(av + (bv - av) * f);
  };
  return `rgb(${mix(16)},${mix(8)},${mix(0)})`;
}

export class TrackMap {
  constructor(canvas, { tileUrl = "https://tile.openstreetmap.org/{z}/{x}/{y}.png" } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.tileUrl = tileUrl;
    this.tiles = new Map();
    this.failed = new Set();
    this.track = null;
    this.zoom = 9;
    this.center = { lat: 32.9, lon: -97.2 };
    this.userMoved = false;

    this._bindPointer();
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoomBy(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });

    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  _bindPointer() {
    let dragging = false, lastX = 0, lastY = 0;
    this.canvas.addEventListener("pointerdown", (e) => {
      dragging = true; lastX = e.clientX; lastY = e.clientY;
      this.canvas.classList.add("dragging");
      this.canvas.setPointerCapture(e.pointerId);
    });
    this.canvas.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const scale = TILE * Math.pow(2, this.zoom);
      const cx = lonToX(this.center.lon, scale) - (e.clientX - lastX);
      const cy = latToY(this.center.lat, scale) - (e.clientY - lastY);
      this.center = { lon: xToLon(cx, scale), lat: yToLat(cy, scale) };
      lastX = e.clientX; lastY = e.clientY;
      this.userMoved = true;
      this.draw();
    });
    const stop = (e) => {
      dragging = false;
      this.canvas.classList.remove("dragging");
      if (e.pointerId != null && this.canvas.hasPointerCapture?.(e.pointerId)) {
        this.canvas.releasePointerCapture(e.pointerId);
      }
    };
    this.canvas.addEventListener("pointerup", stop);
    this.canvas.addEventListener("pointercancel", stop);
  }

  zoomBy(delta) {
    this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.zoom + delta));
    this.userMoved = true;
    this.draw();
  }

  setTrack(track) {
    this.track = track;
    if (!this.userMoved) this.fit();
    else this.draw();
  }

  /** Frame the whole track. */
  fit() {
    const t = this.track;
    if (!t || !t.lat.length) { this.draw(); return; }

    let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    for (let i = 0; i < t.lat.length; i++) {
      minLat = Math.min(minLat, t.lat[i]); maxLat = Math.max(maxLat, t.lat[i]);
      minLon = Math.min(minLon, t.lon[i]); maxLon = Math.max(maxLon, t.lon[i]);
    }
    this.center = { lat: (minLat + maxLat) / 2, lon: (minLon + maxLon) / 2 };

    const { width, height } = this.canvas.getBoundingClientRect();
    let zoom = MAX_ZOOM;
    for (; zoom > MIN_ZOOM; zoom--) {
      const scale = TILE * Math.pow(2, zoom);
      const w = Math.abs(lonToX(maxLon, scale) - lonToX(minLon, scale));
      const h = Math.abs(latToY(minLat, scale) - latToY(maxLat, scale));
      if (w < width * 0.82 && h < height * 0.82) break;
    }
    this.zoom = zoom;
    this.userMoved = false;
    this.draw();
  }

  _isDark() {
    const surface = cssVar("--surface-1");
    const hex = surface.startsWith("#") ? surface.slice(1) : "";
    if (hex.length !== 6) return false;
    const n = parseInt(hex, 16);
    // Rec. 601 luma; the surface is the thing the map has to sit against.
    return (((n >> 16) & 255) * 0.299 + ((n >> 8) & 255) * 0.587 + (n & 255) * 0.114) < 128;
  }

  _tile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (this.tiles.has(key)) return this.tiles.get(key);
    if (this.failed.has(key)) return null;

    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => this.draw();
    img.onerror = () => { this.failed.add(key); this.tiles.delete(key); };
    img.src = this.tileUrl.replace("{z}", z).replace("{x}", x).replace("{y}", y);
    this.tiles.set(key, img);
    return img;
  }

  draw() {
    const ctx = this.ctx;
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = rect.width, height = rect.height;
    if (!width || !height) return;

    if (this.canvas.width !== Math.round(width * dpr)) {
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--plane");
    ctx.fillRect(0, 0, width, height);

    const z = Math.round(this.zoom);
    const scale = TILE * Math.pow(2, z);
    const cx = lonToX(this.center.lon, scale);
    const cy = latToY(this.center.lat, scale);
    const originX = cx - width / 2;
    const originY = cy - height / 2;

    // --- basemap -----------------------------------------------------------
    const n = Math.pow(2, z);
    const x0 = Math.floor(originX / TILE), x1 = Math.floor((originX + width) / TILE);
    const y0 = Math.floor(originY / TILE), y1 = Math.floor((originY + height) / TILE);
    let drewAny = false;
    for (let tx = x0; tx <= x1; tx++) {
      for (let ty = y0; ty <= y1; ty++) {
        if (ty < 0 || ty >= n) continue;
        const wrapped = ((tx % n) + n) % n;
        const img = this._tile(z, wrapped, ty);
        if (img && img.complete && img.naturalWidth) {
          ctx.drawImage(img, tx * TILE - originX, ty * TILE - originY, TILE, TILE);
          drewAny = true;
        }
      }
    }
    // The tile server only serves a light basemap. Inverting the lightness
    // while restoring hue keeps roads and water recognisable on a dark page,
    // instead of a glaring white slab in the middle of the layout.
    if (drewAny && this._isDark()) {
      ctx.save();
      ctx.globalCompositeOperation = "difference";
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      ctx.globalCompositeOperation = "hue";
      ctx.fillStyle = cssVar("--plane");
      ctx.globalAlpha = 0.35;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    }
    if (!drewAny) this._drawGrid(ctx, width, height, originX, originY);

    // --- the track ---------------------------------------------------------
    const t = this.track;
    if (!t || !t.lat.length) return;

    let aMin = Infinity, aMax = -Infinity;
    for (const a of t.alt) {
      if (a == null) continue;
      aMin = Math.min(aMin, a); aMax = Math.max(aMax, a);
    }
    const span = aMax - aMin || 1;

    const px = (i) => lonToX(t.lon[i], scale) - originX;
    const py = (i) => latToY(t.lat[i], scale) - originY;

    // Segment-by-segment so colour can carry altitude (sequential, one hue).
    ctx.lineWidth = 2.5;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    for (let i = 1; i < t.lat.length; i++) {
      const a = t.alt[i] != null ? t.alt[i] : aMin;
      ctx.strokeStyle = rampColor((a - aMin) / span);
      ctx.beginPath();
      ctx.moveTo(px(i - 1), py(i - 1));
      ctx.lineTo(px(i), py(i));
      ctx.stroke();
    }

    this._marker(ctx, px(0), py(0), cssVar("--text-secondary"), "launch");
    const last = t.lat.length - 1;
    this._marker(ctx, px(last), py(last), cssVar("--series-2"), this.endLabel || "now");
  }

  _drawGrid(ctx, width, height, originX, originY) {
    ctx.strokeStyle = cssVar("--grid");
    ctx.lineWidth = 1;
    for (let gx = -originX % TILE; gx < width; gx += TILE) {
      ctx.beginPath(); ctx.moveTo(Math.round(gx) + 0.5, 0);
      ctx.lineTo(Math.round(gx) + 0.5, height); ctx.stroke();
    }
    for (let gy = -originY % TILE; gy < height; gy += TILE) {
      ctx.beginPath(); ctx.moveTo(0, Math.round(gy) + 0.5);
      ctx.lineTo(width, Math.round(gy) + 0.5); ctx.stroke();
    }
    ctx.fillStyle = cssVar("--text-muted");
    ctx.font = "10px " + cssVar("--font");
    ctx.textAlign = "center";
    ctx.fillText("basemap unavailable — track shown", width / 2, 14);
  }

  /* Markers carry a 2px surface ring so they stay legible over the track. */
  _marker(ctx, x, y, color, label) {
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = cssVar("--surface-1");
    ctx.stroke();

    ctx.font = "10px " + cssVar("--font");
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.lineWidth = 3;
    ctx.strokeStyle = cssVar("--surface-1");
    ctx.strokeText(label, x, y - 8);
    ctx.fillStyle = cssVar("--text-secondary");
    ctx.fillText(label, x, y - 8);
  }
}

export { rampColor };
