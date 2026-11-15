/* ftd.io fire calendar: harmonized MODIS + VIIRS daily record, trends and anomalies. */
(function () {
  "use strict";
  const FV = window.FV;
  const { api, toast, esc, cssVar, fmtLat, fmtLon } = FV;
  const tip = Charts.tip;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const MAX_AREA = 25;          // deg², server limit for a calendar backfill
  const DAY_MS = 86400000;
  const METRICS = {
    estimate: { label: "Harmonized fire cells", ramp: "o", unit: "cells" },
    modis_cells: { label: "MODIS fire cells", ramp: "b", unit: "cells" },
    viirs_cells: { label: "VIIRS fire cells", ramp: "a", unit: "cells" },
    frp_mw: { label: "Peak overpass FRP", ramp: "o", unit: "MW" },
  };
  const ANOM = {
    spike: { title: "Activity spike", icon: "M3 17l5-5 4 4 9-9" },
    off_season: { title: "Off-season fires", icon: "M12 3v4m0 10v4M3 12h4m10 0h4" },
    quiet: { title: "Quiet spell", icon: "M4 14h16M4 10h16" },
    high_intensity: { title: "High-intensity fires", icon: "M12 2c1 3.5-1.5 5-1.5 7.5 0 1.4 1 2.5 2.3 2.5 1.6 0 2.4-1.4 2.2-3.2C17.6 10.6 19 13 19 15.5 19 19.1 15.9 22 12 22s-7-2.9-7-6.5C5 10 10.5 7.5 12 2z" },
    sensor_gap: { title: "Sensor data gap", icon: "M4 12h5m6 0h5M9 8v8m6-8v8" },
    low_agreement: { title: "Sensors disagree", icon: "M6 6l12 12M18 6 6 18" },
  };

  const cal = {
    data: null, metric: "estimate", selected: null, loaded: false, seq: 0,
    regions: [], active: null, areas: [], dayMap: null, dayLayer: null, anomSel: null,
  };

  // --------------------------------------------------------------- dates
  const iso = (t) => new Date(t).toISOString().slice(0, 10);
  const parse = (d) => Date.parse(`${d}T00:00:00Z`);
  const yesterday = () => iso(Date.now() - DAY_MS);
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const nice = (d) => { const t = new Date(parse(d)); return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]} ${t.getUTCFullYear()}`; };
  const short = (d) => { const t = new Date(parse(d)); return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`; };
  const span = (a, b) => (a === b ? nice(a) : `${short(a)} – ${nice(b)}`);
  const n0 = (v) => (v == null ? "–" : Math.round(v).toLocaleString());
  const n1 = (v) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }));

  // -------------------------------------------------------------- inputs
  const form = () => ({ w: $("[name=cw]"), s: $("[name=cs]"), e: $("[name=ce]"), n: $("[name=cn]") });
  function readBbox() {
    const f = form();
    const v = [f.w, f.s, f.e, f.n].map((i) => parseFloat(i.value));
    return v.every(Number.isFinite) ? v : null;
  }
  function setBbox(b) {
    const f = form();
    [f.w, f.s, f.e, f.n].forEach((i, k) => { i.value = b ? +(+b[k]).toFixed(2) : ""; });
  }
  const MODIS_START = "2000-11-01", VIIRS_START = "2012-01-20";
  const sampled = () => $("#cal-period").value === "sampled";
  const longterm = () => ["since2000", "sampled"].includes($("#cal-period").value);
  // Mirrors calendar.backfill.sampled_years: every 5th year since 2001 + last full year; VIIRS for the last two.
  function sampledYears() {
    const last = new Date().getUTCFullYear() - 1, ys = new Set([last]);
    for (let y = 2001; y <= last; y += 5) ys.add(y);
    const list = [...ys].sort((a, b) => a - b);
    return list.map((y, i) => ({ year: y, viirs: i >= list.length - 2 }));
  }
  // Daily view range (at most 24 months). "Since 2000" shows the last 24 months daily plus the long-term panel.
  function readRange() {
    const p = longterm() ? "730" : $("#cal-period").value;
    if (p !== "custom") {
      const end = yesterday();
      return [iso(parse(end) - (+p - 1) * DAY_MS), end];
    }
    return [$("#cal-start").value, $("#cal-end").value];
  }
  // Range a fetch asks for: the daily range, or everything since the MODIS archive began.
  function fetchRange() { return longterm() ? [MODIS_START, yesterday()] : readRange(); }
  function syncRangeInputs() {
    const custom = $("#cal-period").value === "custom";
    if (!custom) { const [s, e] = readRange(); $("#cal-start").value = s; $("#cal-end").value = e; }
    $("#cal-start").disabled = $("#cal-end").disabled = !custom;
  }

  // Validates area + period; returns {bbox, start, end} or null and writes the status line.
  function check() {
    const el = $("#cal-status");
    const bbox = readBbox(), [start, end] = readRange();
    const bad = (msg) => { el.className = "area-check cal-status bad"; el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--st-warning)"><path d="M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5"/></svg>${msg}`; $("#cal-fetch").disabled = true; return null; };
    if (!bbox) return bad("Pick an area or enter all four coordinates.");
    const [w, s, e, n] = bbox;
    if (w < -180 || e > 180 || s < -90 || n > 90) return bad("Coordinates are out of range (longitude ±180, latitude ±90).");
    if (w >= e || s >= n) return bad("West must be less than East, and South less than North.");
    const area = (e - w) * (n - s);
    if (area > MAX_AREA + 1e-9) return bad(`This area is ${area.toFixed(1)} deg². Calendars are limited to ${MAX_AREA} deg² (5° × 5°): a year of detections in a bigger box is too much to store and harmonize.`);
    if (!start || !end || start > end) return bad("Choose a start date before the end date.");
    const days = (parse(end) - parse(start)) / DAY_MS + 1;
    if (days > 731) return bad("At most 24 months at a time.");
    if (end > iso(Date.now())) return bad("The end date can't be in the future.");
    const live = FV.lane() === "live";
    if (live && !FV.summary()?.credentials.firms) return bad("FIRMS_MAP_KEY is not set in .env, so the live archive can't be fetched. Switch to Simulated to try the calendar.");
    const [fs, fe] = fetchRange();
    const fdays = (parse(fe) - parse(fs)) / DAY_MS + 1;
    const vdays = Math.max(0, (parse(fe) - Math.max(parse(fs), parse(VIIRS_START))) / DAY_MS + 1);
    const sy = sampledYears();
    const chunks = sampled() ? sy.reduce((a, y) => a + 75 * (y.viirs ? 2 : 1), 0) : Math.ceil((fdays + 2) / 5) + Math.ceil((vdays + 2) / 5);
    // FIRMS allows ~5,000 transactions per 10 min and a 5-day request costs ~10, so long fetches are paced.
    const paced = Math.ceil((chunks * 10) / 4250) * 10 * 60;
    const secs = live ? Math.max(5, Math.round(chunks * 0.8), chunks > 400 ? paced : 0) : Math.max(3, Math.round(fdays / 30));
    const dur = secs < 90 ? `${secs} s` : secs < 5400 ? `${Math.round(secs / 60)} min` : `${(secs / 3600).toFixed(1)} h`;
    el.className = "area-check cal-status";
    el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--st-good)"><path d="m5 12 5 5 9-10"/></svg>`
      + `${area.toFixed(area < 1 ? 2 : 1)} deg² around ${fmtLat((s + n) / 2)}, ${fmtLon((w + e) / 2)} · ${sampled() ? `years ${sy.map((y) => y.year).join(", ")} (VIIRS for ${sy.filter((y) => y.viirs).map((y) => y.year).join(" and ")}; daily view: last 24 months)` : longterm() ? `${short(fs)} – ${nice(fe)} (daily view: last 24 months)` : `${days} days`}. `
      + (live ? `Fetching asks NASA FIRMS for ${chunks.toLocaleString()} five-day chunks (MODIS + VIIRS, about ${dur}${chunks > 400 ? ", paced to your key's quota" : ""}); chunks already stored are skipped.`
        : `Fetching generates a simulated fire regime with planted anomalies (about ${dur}).`);
    $("#cal-fetch").disabled = !!FV.job?.running;
    return { bbox, start, end };
  }

  // --------------------------------------------------------- area options
  async function loadAreaOptions() {
    const lane = FV.lane();
    try {
      cal.areas = await api(`/api/calendar/areas?lane=${lane}`);
    } catch { cal.areas = []; }
    $("#cal-area-fetched").innerHTML = cal.areas.length
      ? cal.areas.map((a, i) => `<option value="fetched:${i}" data-bbox="${a.bbox.join(",")}">${fmtLat((a.bbox[1] + a.bbox[3]) / 2)} ${fmtLon((a.bbox[0] + a.bbox[2]) / 2)} · ${short(a.first_day)} – ${nice(a.last_day)}</option>`).join("")
      : `<option disabled>None yet</option>`;
    if (!cal.regions.length) {
      try {
        // Regions are 4°×4° live presets; 16 deg² also fits the calendar limit.
        cal.regions = await api("/api/regions");
        $("#cal-area-regions").innerHTML = cal.regions.map((r, i) => `<option value="region:${i}" data-bbox="${r.bbox.join(",")}">${esc(r.name)}</option>`).join("");
      } catch { /* optional */ }
    }
    const group = $("#cal-area-active");
    if (!FV.summary()?.credentials.firms) group.innerHTML = `<option disabled>Needs FIRMS_MAP_KEY</option>`;
    else if (!cal.active) {
      try {
        cal.active = await api("/api/active-areas");
        group.innerHTML = cal.active.length
          ? cal.active.map((a, i) => `<option value="active:${i}" data-bbox="${a.bbox.join(",")}">${fmtLat(a.center[0])} ${fmtLon(a.center[1])} · ${a.detections.toLocaleString()} detections today</option>`).join("")
          : `<option disabled>No active fires found</option>`;
      } catch (e) { group.innerHTML = `<option disabled>Could not load: ${esc(e.message)}</option>`; }
    }
  }

  function pickDefaultArea() {
    const sel = $("#cal-area");
    const saved = (() => { try { return JSON.parse(localStorage.getItem(`fv.cal.${FV.lane()}`) || "null"); } catch { return null; } })();
    if (saved?.bbox) {
      setBbox(saved.bbox);
      const match = [...sel.options].find((o) => o.dataset.bbox === saved.bbox.join(","));
      sel.value = match ? match.value : "custom";
      if (saved.period) $("#cal-period").value = saved.period;
      if (saved.period === "custom" && saved.start) { $("#cal-start").value = saved.start; $("#cal-end").value = saved.end; }
      return;
    }
    const first = sel.querySelector('#cal-area-fetched option[data-bbox]') || sel.querySelector('#cal-area-regions option[data-bbox]');
    if (first) { sel.value = first.value; setBbox(first.dataset.bbox.split(",").map(Number)); }
  }

  function remember() {
    const [start, end] = readRange();
    try { localStorage.setItem(`fv.cal.${FV.lane()}`, JSON.stringify({ bbox: readBbox(), period: $("#cal-period").value, start, end })); } catch { /* private mode */ }
  }

  // ----------------------------------------------------------------- load
  async function load() {
    syncRangeInputs();
    const q = check();
    if (!q) { cal.data = null; renderAll(); return; }
    remember();
    const seq = ++cal.seq;
    $("#cal-grid").innerHTML = `<div class="cal-empty"><span class="spinner"></span><p>Harmonizing MODIS and VIIRS…</p></div>`;
    try {
      const data = await api(`/api/calendar?lane=${FV.lane()}&bbox=${q.bbox.join(",")}&start=${q.start}&end=${q.end}&low=${$("#cal-low").checked ? 1 : 0}`);
      if (seq !== cal.seq) return;
      cal.data = data;
      if (cal.selected && !data.days.some((d) => d.day === cal.selected)) cal.selected = null;
      renderAll();
      loadLongterm(q.bbox, seq).then(() => loadNational(q.bbox, seq));
    } catch (e) {
      if (seq !== cal.seq) return;
      cal.data = null;
      renderAll();
      toast(`Could not load the calendar: ${e.message}`);
    }
  }

  async function fetchData() {
    const q = check();
    if (!q) return;
    try {
      const [start, end] = fetchRange();
      const job = await api("/api/calendar/backfill", { method: "POST", body: { mode: FV.lane(), bbox: q.bbox, start, end, sample_years: sampled() } });
      $("#cal-fetch").disabled = true;
      FV.trackJob(job);
    } catch (e) { toast(e.message, 8000); }
  }

  // ------------------------------------------------------------ rendering
  function hasAnyData(d) { return d && d.days.some((x) => x.has_data); }

  function renderAll() {
    renderKpis();
    renderGrid();
    renderSeries();
    renderDay();
    renderTrends();
    renderAnoms();
    renderActions();
  }

  function renderKpis() {
    const d = cal.data, ok = hasAnyData(d);
    const set = (id, v, foot) => { $(`#${id}`).textContent = v; $(`#${id}-foot`).textContent = foot || ""; };
    if (!ok) { ["ck-cells", "ck-days", "ck-ratio", "ck-agree", "ck-trend", "ck-anoms"].forEach((id) => set(id, "–", "")); return; }
    const t = d.analysis.totals, c = d.calibration, tr = d.analysis.trend, an = d.analysis.anomalies;
    set("ck-cells", n0(t.harmonized_cells), `MODIS ${n0(t.modis_cells)} · VIIRS ${n0(t.viirs_cells)} cells`);
    set("ck-days", n0(t.active_days), `of ${t.days_with_data} days with data${t.peak_day ? ` · peak ${short(t.peak_day)}` : ""}`);
    set("ck-ratio", c.viirs_cells_per_modis_cell ? `${c.viirs_cells_per_modis_cell.toFixed(1)}×` : "–",
      c.viirs_pixels_per_cell ? `${c.viirs_pixels_per_cell.toFixed(1)} VIIRS pixels per 1-km cell` : "needs days with both sensors");
    set("ck-agree", c.modis_confirmed_by_viirs != null ? `${Math.round(c.modis_confirmed_by_viirs * 100)}%` : "–",
      c.viirs_confirmed_by_modis != null ? `MODIS sees ${Math.round(c.viirs_confirmed_by_modis * 100)}% of VIIRS cells` : "");
    if (tr.status === "ok") {
      const rate = tr.pct_per_year != null ? `${tr.pct_per_year > 0 ? "+" : ""}${tr.pct_per_year}%/yr` : `${tr.pct_per_month > 0 ? "+" : ""}${tr.pct_per_month}%/mo`;
      const arrow = tr.direction === "increasing" ? "↑ " : tr.direction === "decreasing" ? "↓ " : "";
      set("ck-trend", tr.direction.startsWith("no") ? "Flat" : `${arrow}${rate}`, tr.direction.startsWith("no") ? `no significant trend (p ${pv(tr.p_value)})` : `${tr.direction}, p ${pv(tr.p_value)}`);
    } else set("ck-trend", "–", tr.detail || "");
    const high = an.filter((a) => a.severity === "high").length;
    set("ck-anoms", n0(an.length), an.length ? `${high} high severity` : "none found");
  }
  const f1 = (v) => (v == null ? "–" : (+v).toFixed(1));
  const pv = (p) => (p < 0.001 ? "< 0.001" : `= ${p.toFixed(3)}`);

  // ------------------------------------------------------- calendar grid
  function metricValue(day, metric) { return day.has_data ? day[metric] : null; }

  function bins(values) {
    const nz = values.filter((v) => v != null && v > 0);
    if (!nz.length) return [];
    const max = Math.max(...nz), min = Math.max(Math.min(...nz), 0.5);
    if (max <= min * 1.01) return [max];
    const edges = [];
    for (let k = 1; k <= 7; k++) edges.push(Math.exp(Math.log(min) + (k / 7) * (Math.log(max) - Math.log(min))));
    return edges;   // value v goes to the first edge >= v
  }
  function step(v, edges) {
    if (v == null) return null;
    if (v <= 0 || !edges.length) return 0;
    const i = edges.findIndex((e) => v <= e * 1.0000001);
    return (i < 0 ? edges.length - 1 : i) + 1;
  }

  function renderLegend(edges) {
    const m = METRICS[cal.metric];
    const fmt = (v) => (v >= 100 ? n0(v) : v >= 10 ? Math.round(v) : v.toFixed(v < 2 ? 1 : 0));
    let html = `<span>${esc(m.label)} per day:</span><span class="ramp"><i style="background:var(--cal-zero)"></i>`;
    for (let k = 1; k <= 7; k++) html += `<i style="background:var(--${m.ramp}${k})" title="${k <= edges.length ? "≤ " + fmt(edges[k - 1]) : ""}"></i>`;
    html += `</span><span class="muted">0 → ${edges.length ? fmt(edges[edges.length - 1]) : 0} ${m.unit} (log scale)</span>`;
    html += `<span><i class="sw" style="background:repeating-linear-gradient(135deg, var(--surface-2) 0 3px, var(--grid) 3px 4px)"></i>no data fetched</span>`;
    html += `<span><i class="sw" style="box-shadow: inset 0 0 0 2px var(--ink)"></i>possible anomaly</span>`;
    html += `<span><i class="sw" style="background:repeating-linear-gradient(45deg, var(--${m.ramp}4) 0 3px, var(--surface) 3px 5px)"></i>estimated from one sensor</span>`;
    $("#cal-legend").innerHTML = html;
  }

  function anomalyDays() {
    const map = new Map();
    (cal.data?.analysis.anomalies || []).forEach((a, i) => {
      for (let t = parse(a.start); t <= parse(a.end); t += DAY_MS) {
        const k = iso(t);
        if (!map.has(k)) map.set(k, []);
        map.get(k).push({ ...a, n: i + 1 });
      }
    });
    return map;
  }

  function renderGrid() {
    const d = cal.data, grid = $("#cal-grid");
    $("#cal-range").textContent = d ? `· ${span(d.start, d.end)}` : "";
    if (!d) { grid.innerHTML = ""; $("#cal-legend").innerHTML = ""; return; }
    if (!hasAnyData(d)) {
      const live = FV.lane() === "live";
      grid.innerHTML = `<div class="cal-empty"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--axis)" stroke-width="1.5" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4m8-4v4"/></svg>
        <p><b>No MODIS or VIIRS data stored for this area and period yet.</b></p>
        <p class="muted">${live ? "Fetch pulls the NASA FIRMS archive (science-quality where available, near real-time for recent weeks) and harmonizes both sensors on a 1-km daily grid." : "Fetch generates a simulated year of MODIS and VIIRS detections with a fire season, a trend and planted anomalies, so you can check what the analysis finds."}</p>
        <button class="btn btn-primary" type="button" data-cal-fetch>Fetch MODIS + VIIRS</button></div>`;
      $("#cal-legend").innerHTML = "";
      return;
    }
    const metric = cal.metric, ramp = METRICS[metric].ramp;
    const edges = bins(d.days.map((x) => metricValue(x, metric)));
    renderLegend(edges);
    const anoms = anomalyDays();
    const byMonth = new Map();
    d.days.forEach((x) => { const k = x.day.slice(0, 7); if (!byMonth.has(k)) byMonth.set(k, []); byMonth.get(k).push(x); });
    let html = "";
    for (const [ym, days] of byMonth) {
      const [y, m] = ym.split("-").map(Number);
      const first = new Date(Date.UTC(y, m - 1, 1));
      const lead = (first.getUTCDay() + 6) % 7;   // Monday-first
      const total = days.reduce((s, x) => s + (metricValue(x, metric) || 0), 0);
      const byDay = new Map(days.map((x) => [x.day, x]));
      html += `<div class="cal-month"><h4>${MONTHS[m - 1]} ${y}<span>${metric === "frp_mw" ? "" : n0(total)}</span></h4><div class="cal-days">`;
      html += ["M", "T", "W", "T", "F", "S", "S"].map((w) => `<div class="cal-wd" aria-hidden="true">${w}</div>`).join("");
      for (let i = 0; i < lead; i++) html += `<div class="cal-d blank"></div>`;
      const dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
      for (let dd = 1; dd <= dim; dd++) {
        const key = `${ym}-${String(dd).padStart(2, "0")}`;
        const x = byDay.get(key);
        if (!x) { html += `<div class="cal-d blank"></div>`; continue; }
        const v = metricValue(x, metric), st = step(v, edges);
        const cls = ["cal-d"];
        if (v == null) cls.push("nodata");
        if (anoms.has(key)) cls.push("anom");
        if (x.estimated && v != null && metric === "estimate") cls.push("est");
        if (cal.selected === key) cls.push("sel");
        const fill = st ? `style="--f:var(--${ramp}${st})"` : "";
        const label = `${nice(key)}: ${v == null ? "no data" : `${n1(v)} ${METRICS[metric].unit}`}${anoms.has(key) ? ", possible anomaly" : ""}`;
        html += `<button type="button" class="${cls.join(" ")}" ${fill} data-day="${key}" aria-label="${esc(label)}"></button>`;
      }
      html += `</div></div>`;
    }
    grid.innerHTML = html;
  }

  function dayTip(x) {
    const anoms = anomalyDays().get(x.day) || [];
    if (!x.has_data) return `<b>${nice(x.day)}</b><br><span class="t-muted">No data fetched for this day</span>`;
    const agree = x.modis_cells ? `${Math.round((x.modis_confirmed / x.modis_cells) * 100)}% of MODIS cells confirmed by VIIRS` : "";
    return `<b>${nice(x.day)}</b><br>${n1(x.estimate)} harmonized fire cells${x.estimated ? `<br><span class="t-muted">${esc(x.estimated)}</span>` : ""}`
      + `<br>MODIS ${x.modis_cells} cells (${x.modis_pixels} px) · VIIRS ${x.viirs_cells} cells (${x.viirs_pixels} px)`
      + (agree ? `<br><span class="t-muted">${agree}</span>` : "")
      + `<br>Peak overpass FRP ${n0(x.frp_mw)} MW`
      + anoms.map((a) => `<br><b>${a.n}. ${esc(ANOM[a.type]?.title || a.type)}</b>`).join("");
  }

  // ------------------------------------------------------- series chart
  function rolling(values, k = 3) {
    return values.map((_, i) => {
      const win = values.slice(Math.max(0, i - k), i + k + 1).filter((v) => v != null);
      return win.length >= 4 ? win.reduce((a, b) => a + b, 0) / win.length : null;
    });
  }
  function niceMax(v) {
    if (v <= 0) return 4;
    const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }

  function renderSeries() {
    const el = $("#cal-series"), d = cal.data;
    if (!hasAnyData(d)) { el.innerHTML = `<div class="empty">No data yet.</div>`; return; }
    const days = d.days, n = days.length;
    const w = Math.max(el.clientWidth, 320), h = 270, m = { l: 44, r: 10, t: 26, b: 26 };
    const pw = w - m.l - m.r, ph = h - m.t - m.b;
    const est = days.map((x) => (x.has_data ? x.estimate : null));
    const roll = { h: rolling(est), m: rolling(days.map((x) => (x.has_data ? x.modis_cells : null))), v: rolling(days.map((x) => (x.has_data ? x.viirs_cells : null))) };
    const ymax = niceMax(Math.max(1, ...est.filter((v) => v != null)));
    const x = (i) => m.l + ((i + 0.5) / n) * pw;
    const y = (v) => m.t + ph - (Math.min(v, ymax) / ymax) * ph;
    const bw = pw / n;
    let html = `<div class="chart-legend">
      <span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--o2)"/></svg>harmonized, daily</span>
      <span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--c-harm)" stroke-width="2"/></svg>harmonized</span>
      <span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--c-modis)" stroke-width="2"/></svg>MODIS</span>
      <span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--c-viirs)" stroke-width="2"/></svg>VIIRS</span>
      ${d.analysis.trend.weekly_line ? `<span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="4 3"/></svg>trend (Sen)</span>` : ""}
      <span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--ink)" fill-opacity=".1"/></svg>anomaly</span></div>`;
    html += `<div class="series-wrap"><svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Daily harmonized fire cells with MODIS and VIIRS 7-day averages">`;
    html += `<defs><pattern id="gap-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="transparent"/><line x1="0" y1="0" x2="0" y2="6" stroke="var(--ink)" stroke-opacity=".18" stroke-width="2"/></pattern></defs>`;
    // grid + y ticks
    for (let k = 0; k <= 4; k++) {
      const v = (ymax * k) / 4, yy = y(v);
      html += `<line class="${k ? "grid" : "axis"}" x1="${m.l}" x2="${w - m.r}" y1="${yy}" y2="${yy}"></line>`;
      html += `<text class="muted-text" x="${m.l - 6}" y="${yy + 4}" text-anchor="end">${n0(v)}</text>`;
    }
    // anomaly bands
    d.analysis.anomalies.forEach((a, i) => {
      const i0 = days.findIndex((dd) => dd.day === a.start), i1 = days.findIndex((dd) => dd.day === a.end);
      if (i0 < 0) return;
      const x0 = m.l + (i0 / n) * pw, x1 = m.l + (((i1 < 0 ? n - 1 : i1) + 1) / n) * pw;
      html += `<rect x="${x0}" y="${m.t}" width="${Math.max(2, x1 - x0)}" height="${ph}" fill="${a.type === "sensor_gap" ? "url(#gap-hatch)" : "var(--ink)"}" ${a.type === "sensor_gap" ? "" : 'fill-opacity=".07"'}></rect>`;
      html += `<g><circle cx="${(x0 + x1) / 2}" cy="${m.t - 12}" r="8" fill="var(--ink)"></circle><text x="${(x0 + x1) / 2}" y="${m.t - 8}" text-anchor="middle" style="fill:var(--surface);font-size:10px;font-weight:700">${i + 1}</text></g>`;
    });
    // daily bars
    days.forEach((dd, i) => {
      if (!dd.has_data || !dd.estimate) return;
      const yy = y(dd.estimate);
      html += `<rect x="${x(i) - bw / 2 + (bw > 3 ? 0.5 : 0)}" y="${yy}" width="${Math.max(0.8, bw - (bw > 3 ? 1 : 0))}" height="${m.t + ph - yy}" fill="var(--o2)"></rect>`;
    });
    // trend line (weekly Sen line -> per day)
    const tl = d.analysis.trend.weekly_line;
    if (tl) {
      const first = days.findIndex((dd) => dd.estimate != null);
      let last = n - 1; while (last > 0 && days[last].estimate == null) last--;
      html += `<line x1="${x(first)}" x2="${x(last)}" y1="${y(Math.max(0, tl[0] / 7))}" y2="${y(Math.max(0, tl[1] / 7))}" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"></line>`;
    }
    // 7-day lines
    const path = (vals) => { let p = "", pen = false; vals.forEach((v, i) => { if (v == null) { pen = false; return; } p += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true; }); return p; };
    html += `<path d="${path(roll.m)}" fill="none" stroke="var(--c-modis)" stroke-width="2" stroke-linejoin="round"></path>`;
    html += `<path d="${path(roll.v)}" fill="none" stroke="var(--c-viirs)" stroke-width="2" stroke-linejoin="round"></path>`;
    html += `<path d="${path(roll.h)}" fill="none" stroke="var(--c-harm)" stroke-width="2.5" stroke-linejoin="round"></path>`;
    // month ticks
    let lastM = "";
    const every = n > 400 ? 2 : 1;
    let mcount = 0;
    days.forEach((dd, i) => {
      const ym = dd.day.slice(0, 7);
      if (ym !== lastM && dd.day.endsWith("-01")) {
        if (mcount++ % every === 0) html += `<text class="muted-text" x="${x(i)}" y="${h - 8}" text-anchor="middle">${MONTHS[+ym.slice(5) - 1]}${ym.endsWith("-01") ? " " + ym.slice(2, 4) : ""}</text>`;
        html += `<line class="grid" x1="${x(i)}" x2="${x(i)}" y1="${m.t + ph}" y2="${m.t + ph + 4}"></line>`;
      }
      lastM = ym;
    });
    // selected day marker + crosshair
    const si = days.findIndex((dd) => dd.day === cal.selected);
    if (si >= 0) html += `<line x1="${x(si)}" x2="${x(si)}" y1="${m.t}" y2="${m.t + ph}" stroke="var(--focus)" stroke-width="2"></line>`;
    html += `<line class="xhair" id="cal-xhair" x1="0" x2="0" y1="${m.t}" y2="${m.t + ph}" visibility="hidden"></line>`;
    html += `<rect id="cal-hit" x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent" style="cursor:pointer"></rect>`;
    html += `</svg></div>`;
    el.innerHTML = html;
    const hit = $("#cal-hit", el), xh = $("#cal-xhair", el), svg = $("svg", el);
    const idx = (evt) => {
      const r = svg.getBoundingClientRect();
      const px = ((evt.clientX - r.left) / r.width) * w;
      return Math.max(0, Math.min(n - 1, Math.floor(((px - m.l) / pw) * n)));
    };
    hit.addEventListener("mousemove", (evt) => {
      const i = idx(evt);
      xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
      const dd = days[i];
      tip.show(evt, dayTip(dd) + (roll.h[i] != null ? `<br><span class="t-muted">7-day avg: harmonized ${n1(roll.h[i])} · MODIS ${n1(roll.m[i])} · VIIRS ${n1(roll.v[i])}</span>` : ""));
    });
    hit.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); tip.hide(); });
    hit.addEventListener("click", (evt) => selectDay(days[idx(evt)].day));
  }

  // ------------------------------------------------------------ day panel
  async function selectDay(day) {
    cal.selected = day;
    $$("#cal-grid .cal-d.sel").forEach((b) => b.classList.remove("sel"));
    const btn = $(`#cal-grid .cal-d[data-day="${day}"]`);
    if (btn) btn.classList.add("sel");
    renderSeries();
    renderDay();
  }

  function renderDay() {
    const body = $("#cal-day-body"), mapEl = $("#cal-day-map");
    const d = cal.data;
    const x = d && cal.selected ? d.days.find((dd) => dd.day === cal.selected) : null;
    if (!x) {
      mapEl.hidden = true;
      body.innerHTML = `<div class="detail-empty" style="min-height:300px"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4m8-4v4"/></svg><p>Click a day in the calendar or the chart to see which sensor saw which fires.</p></div>`;
      return;
    }
    const anoms = anomalyDays().get(x.day) || [];
    const both = x.modis_confirmed, mOnly = x.modis_cells - x.modis_confirmed, vOnly = x.viirs_cells - x.viirs_confirmed;
    body.innerHTML = `<div class="event-id">${esc(x.day)} · local solar day</div>
      <div class="hero">${x.has_data ? n1(x.estimate) : "–"}<small>harmonized 1-km fire cells${x.estimated ? ` · ${esc(x.estimated)}` : ""}</small></div>
      ${x.has_data ? `<div id="cal-day-bar" style="margin-top:12px"></div>` : `<p class="muted">No data fetched for this day.</p>`}
      <dl class="facts" style="margin-top:14px">
        <div><dt>MODIS (Terra + Aqua)</dt><dd>${x.modis_cells} cells · ${x.modis_pixels} px</dd></div>
        <div><dt>VIIRS (NOAA-20 / S-NPP)</dt><dd>${x.viirs_cells} cells · ${x.viirs_pixels} px</dd></div>
        <div><dt>Peak overpass FRP</dt><dd>${n0(x.frp_mw)} MW</dd></div>
        <div><dt>Data</dt><dd>MODIS ${esc(x.modis || "–")} · VIIRS ${esc(x.viirs || "–")}</dd></div>
      </dl>
      ${anoms.map((a) => `<div class="truth" style="margin-top:10px"><b>${a.n}. ${esc(ANOM[a.type]?.title || a.type)}</b> · ${esc(a.summary)}</div>`).join("")}
      <div class="muted" id="cal-day-passes" style="margin-top:10px"></div>`;
    if (x.has_data) {
      Charts.segmentedBar($("#cal-day-bar"), [
        { label: "Seen by both", value: both, color: "--c-harm" },
        { label: "MODIS only", value: mOnly, color: "--c-modis" },
        { label: "VIIRS only", value: vOnly, color: "--c-viirs" },
      ], { label: "Fire cells by sensor" });
    }
    loadDayCells(x);
  }

  async function loadDayCells(x) {
    const mapEl = $("#cal-day-map");
    if (!x.has_data || !(x.modis_pixels + x.viirs_pixels) || typeof L === "undefined") { mapEl.hidden = true; return; }
    const day = x.day, b = cal.data.bbox;
    let res;
    try {
      res = await api(`/api/calendar/day?lane=${FV.lane()}&bbox=${b.join(",")}&date=${day}&low=${$("#cal-low").checked ? 1 : 0}`);
    } catch (e) { $("#cal-day-passes").textContent = `Could not load cells: ${e.message}`; return; }
    if (cal.selected !== day) return;
    mapEl.hidden = false;
    if (!cal.dayMap) {
      cal.dayMap = L.map(mapEl, { zoomControl: true, attributionControl: true });
      cal.dayLayer = L.layerGroup().addTo(cal.dayMap);
      setDayBasemap();
    }
    cal.dayMap.invalidateSize();
    cal.dayLayer.clearLayers();
    const color = { both: cssVar("--c-harm"), modis: cssVar("--c-modis"), viirs: cssVar("--c-viirs") };
    const label = { both: "both sensors", modis: "MODIS only", viirs: "VIIRS only" };
    res.cells.forEach((c) => {
      L.circleMarker([c.lat, c.lon], { radius: Math.min(9, 3 + Math.sqrt(c.frp_mw) / 3), color: cssVar("--surface"), weight: 1, fillColor: color[c.seen_by], fillOpacity: 0.9 })
        .bindTooltip(`<b>${label[c.seen_by]}</b><br>MODIS ${c.modis_pixels} px · VIIRS ${c.viirs_pixels} px<br>FRP ${n1(c.frp_mw)} MW<br><span style="color:var(--muted)">${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}</span>`)
        .addTo(cal.dayLayer);
    });
    L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: cssVar("--ink-2"), weight: 1, fill: false, dashArray: "4 4", interactive: false }).addTo(cal.dayLayer);
    const pts = res.cells.map((c) => [c.lat, c.lon]);
    if (pts.length) cal.dayMap.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 10 });
    else cal.dayMap.fitBounds([[b[1], b[0]], [b[3], b[2]]]);
    const rel = (t) => { const dd = Math.round((parse(t.slice(0, 10)) - parse(day)) / DAY_MS); return dd ? ` (${dd > 0 ? "+" : ""}${dd} d)` : ""; };
    const passes = res.overpasses.map((p) => `${esc(p.platform)} ${p.time.slice(11, 16)} UTC${rel(p.time)}: ${p.pixels} px`);
    $("#cal-day-passes").innerHTML = (passes.length ? `Overpasses: ${passes.join(" · ")}<br>` : "")
      + `Map: one dot per 1-km cell, colored as in the bar above; dot size = fire radiative power.`;
  }

  function setDayBasemap() {
    if (!cal.dayMap) return;
    if (cal.dayBase) cal.dayMap.removeLayer(cal.dayBase);
    const tone = FV.isDark() ? "Dark" : "Light";
    const esri = (path) => `https://server.arcgisonline.com/ArcGIS/rest/services/${path}/MapServer/tile/{z}/{y}/{x}`;
    cal.dayBase = L.tileLayer(esri(`Canvas/World_${tone}_Gray_Base`), { maxZoom: 16, maxNativeZoom: 13, attribution: "© Esri" }).addTo(cal.dayMap);
    cal.dayBase.bringToBack();
  }

  // ---------------------------------------------------------------- trends
  function renderTrends() {
    const el = $("#cal-trends"), d = cal.data;
    $("#cal-trend-method").textContent = "";
    if (!hasAnyData(d)) { el.innerHTML = `<div class="empty">No data yet.</div>`; return; }
    const a = d.analysis, t = a.trend, c = d.calibration;
    let html = `<div class="trend-list">`;
    if (t.status === "ok") {
      $("#cal-trend-method").textContent = t.method;
      const up = t.direction === "increasing", down = t.direction === "decreasing";
      const icon = up ? "M4 17 10 11l4 4 6-7m0 0h-5m5 0v5" : down ? "M4 7l6 6 4-4 6 7m0 0h-5m5 0v-5" : "M4 12h16";
      const rate = t.pct_per_year != null ? `${t.pct_per_year > 0 ? "+" : ""}${t.pct_per_year}% per year` : `${t.pct_per_month > 0 ? "+" : ""}${t.pct_per_month}% per month`;
      html += `<div><div class="trend-head"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icon}"/></svg>${up ? "Fire activity is rising" : down ? "Fire activity is falling" : "No significant trend"}</div>
        <p>${up || down ? `${rate} (Sen's slope ${t.slope > 0 ? "+" : ""}${t.slope} ${esc(t.slope_unit)}), ` : ""}p ${pv(t.p_value)}${t.tau != null ? `, Kendall's τ = ${t.tau}` : ""}.</p>
        ${t.caveat ? `<div class="caveat" style="margin-top:8px">${esc(t.caveat)}</div>` : ""}</div>`;
    } else {
      html += `<div><div class="trend-head">Not enough data for a trend</div><p>${esc(t.detail || "")}</p></div>`;
    }
    if (a.recent) {
      const r = a.recent;
      html += `<div><h3>Last ${r.window_days} days</h3><p>${n0(r.last)} harmonized cells, ${r.ratio == null ? "no activity in" : `<b>${r.ratio}×</b>`} the previous ${r.window_days} days (${n0(r.previous)})${r.ratio_last_year != null ? `, and <b>${r.ratio_last_year}×</b> the same period last year (${n0(r.last_year)})` : ""}.</p></div>`;
    }
    a.seasons.forEach((s) => {
      html += `<div><h3>Fire season ${s.from.slice(0, 4) === s.to.slice(0, 4) ? s.from.slice(0, 4) : `${s.from.slice(0, 4)}–${s.to.slice(2, 4)}`}</h3>
        <p>Runs from <b>${short(s.start)}</b> to <b>${short(s.end)}</b> (10–90% of the year's fire cells); half of the activity is done by ${short(s.middle)}. The busiest week centres on <b>${nice(s.peak_week_center)}</b> with ${n0(s.peak_week_cells)} cells.</p></div>`;
    });
    if (!a.seasons.length) html += `<div><h3>Fire season</h3><p class="muted">Needs about a year of data to place the season.</p></div>`;
    if (c.days_used) {
      html += `<div><h3>MODIS ↔ VIIRS harmonization</h3>
        <p>On ${c.days_used} days with both sensors, VIIRS reported <b>${f1(c.viirs_cells_per_modis_cell)}×</b> as many 1-km fire cells as MODIS. Each VIIRS cell holds ${f1(c.viirs_pixels_per_cell)} of its 375 m pixels on average. ${c.modis_confirmed_by_viirs != null ? `<b>${Math.round(c.modis_confirmed_by_viirs * 100)}%</b> of MODIS cells had a VIIRS detection within one cell; MODIS saw ${Math.round((c.viirs_confirmed_by_modis || 0) * 100)}% of VIIRS cells (VIIRS catches smaller fires).` : ""} The harmonized count is MODIS cells plus the VIIRS cells MODIS missed, about ${f1(c.harmonized_per_modis_cell)}× the MODIS count here.</p></div>`;
    }
    html += `</div>`;
    el.innerHTML = html;
  }

  // ------------------------------------------------------------ anomalies
  function sevBadge(s) {
    const color = s === "high" ? "var(--st-critical)" : "var(--st-warning)";
    return `<span class="sev"><svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:${color}"><path d="M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5"/></svg>${s === "high" ? "High" : "Medium"}</span>`;
  }

  function renderAnoms() {
    const el = $("#cal-anoms"), d = cal.data;
    $("#cal-anom-note").textContent = "";
    if (!hasAnyData(d)) { el.innerHTML = `<div class="empty">No data yet.</div>`; return; }
    const list = d.analysis.anomalies;
    let html = "";
    if (d.injected && d.injected.length) {
      const hits = d.injected.filter((i) => i.detected).length;
      const names = { spike: "outbreak spike", modis_gap: "MODIS outage", off_season: "off-season burst", quiet: "quiet spell" };
      html += `<div class="sim-check"><b>Simulation check:</b> ${hits} of ${d.injected.length} planted anomalies detected.<ul>${d.injected.map((i) => `<li>${esc(names[i.type] || i.type)}, ${span(i.start, i.end)}: ${i.detected ? `found (as ${esc(ANOM[i.detected_as]?.title.toLowerCase() || i.detected_as)})` : "<b>missed</b>"}</li>`).join("")}</ul></div>`;
    }
    if (!list.length) { el.innerHTML = html + `<div class="empty">No anomalies: daily activity stayed within its usual range and both sensors agreed.</div>`; return; }
    $("#cal-anom-note").textContent = "robust z-scores against the surrounding 6 weeks";
    html += `<div class="anom-list">${list.map((a, i) => `<button type="button" class="anom${cal.anomSel === i ? " sel" : ""}" data-anom="${i}">
        <div class="anom-top"><span class="anom-num">${i + 1}</span>${esc(ANOM[a.type]?.title || a.type)}${a.sensor ? ` (${esc(a.sensor)})` : ""} ${sevBadge(a.severity)}<span class="when">${span(a.start, a.end)}</span></div>
        <p>${esc(a.summary)}</p></button>`).join("")}</div>`;
    el.innerHTML = html;
  }

  // ------------------------------------------------------------- actions
  const PRIORITY = {
    urgent: { label: "Urgent", color: "var(--st-critical)", icon: "M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5" },
    high: { label: "High", color: "var(--st-serious)", icon: "M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5" },
    medium: { label: "Medium", color: "var(--st-warning)", icon: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 5v5m0 3v.5" },
    low: { label: "Low", color: "var(--st-good)", icon: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm-4 9 3 3 5-6" },
  };

  function renderActions() {
    const el = $("#cal-actions"), d = cal.data;
    const act = d?.analysis?.actions;
    if (!hasAnyData(d) || !act) { el.innerHTML = `<div class="empty">No data yet.</div>`; return; }
    el.innerHTML = actionsHtml(act, "No findings that call for action in this period.");
  }

  function actionsHtml(act, emptyText) {
    if (!act.groups.length) return `<div class="empty">${esc(emptyText)}</div>`;
    return act.groups.map((g) => {
      const p = PRIORITY[g.priority] || PRIORITY.low;
      const finding = esc(g.finding).replace(/#(\d+)/g, '<a href="#" class="anom-ref" data-anom-ref="$1">#$1</a>');
      return `<div class="action-group">
        <div class="action-head"><span class="sev"><svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:${p.color}"><path d="${p.icon}"/></svg>${p.label}</span><b>${finding}</b></div>
        <p class="action-why">${esc(g.why)}</p>
        <div class="table-wrap"><table class="action-table"><thead><tr><th>Option</th><th>Lead</th><th>When</th></tr></thead><tbody>
        ${g.actions.map((a) => `<tr><td><b>${esc(a.title)}</b><div class="detail-text">${esc(a.detail)}</div></td><td>${esc(a.lead)}</td><td class="nowrap">${esc(a.timeframe)}</td></tr>`).join("")}
        </tbody></table></div></div>`;
    }).join("") + `<p class="muted action-disclaimer">${esc(act.disclaimer)}</p>`;
  }

  // ------------------------------------------------------------ long-term
  async function loadLongterm(bbox, seq) {
    const card = $("#cal-lt");
    let lt;
    try {
      lt = await api(`/api/calendar/longterm?lane=${FV.lane()}&bbox=${bbox.join(",")}&low=${$("#cal-low").checked ? 1 : 0}`);
    } catch (e) { if (longterm()) { card.hidden = false; $("#lt-body").innerHTML = `<div class="empty">Could not load the long-term record: ${esc(e.message)}</div>`; } return; }
    if (seq !== cal.seq) return;
    cal.lt = lt;
    const years = lt.status === "ok" ? lt.years.filter((y) => y.full).length : 0;
    card.hidden = !(longterm() || years >= 3);
    if (!card.hidden) renderLongterm();
  }

  function renderLongterm() {
    const lt = cal.lt, body = $("#lt-body");
    if (!lt || lt.status !== "ok" || lt.years.filter((y) => y.full).length < 3) {
      $("#lt-range").textContent = "";
      body.innerHTML = `<div class="cal-empty"><p><b>Not enough years stored for this area yet.</b></p><p class="muted">Choose “Since 2000, every 5th year (fast)” and Fetch for a quick long-term picture, or “every year” for the complete record from November 2000 (paced to your FIRMS quota, which takes hours for a big area).</p></div>`;
      return;
    }
    const fy = lt.full_years || [];
    $("#lt-range").textContent = fy.length ? `· ${fy[0]}–${fy[fy.length - 1]}${lt.sampled ? ` · ${fy.length} sampled years` : ""}` : "";
    let html = `<div class="lt-grid"><div>${ltHeatmap(lt)}</div><div>${ltFacts(lt)}</div></div>`;
    html += `<h3 style="margin-top:18px">Fire cells per year</h3><div id="lt-annual"></div>`;
    html += `<h3 style="margin-top:18px">Long-term government action options</h3><div id="lt-actions">${lt.actions ? actionsHtml(lt.actions, "No long-term findings that call for action.") : ""}</div>`;
    body.innerHTML = html;
    ltAnnual($("#lt-annual"), lt);
  }

  function ltHeatmap(lt) {
    const vals = lt.months.map((m) => (m.days_with_data ? m.harmonized : null));
    const edges = bins(vals);
    const byKey = new Map(lt.months.map((m) => [`${m.year}-${m.month}`, m]));
    const years = [...new Set(lt.years.map((y) => y.year))];
    let html = `<div class="lt-heat" role="img" aria-label="Monthly harmonized fire cells by year"><div></div>${MONTHS.map((m) => `<div class="lt-mh">${m[0]}</div>`).join("")}`;
    years.forEach((y) => {
      html += `<div class="lt-yl">${y}</div>`;
      for (let m = 1; m <= 12; m++) {
        const r = byKey.get(`${y}-${m}`);
        if (!r || !r.days_with_data) { html += `<div class="lt-c none"></div>`; continue; }
        const st = step(r.harmonized, edges);
        html += `<div class="lt-c${r.viirs_available ? "" : " modis-only"}" ${st ? `style="--f:var(--o${st})"` : ""} data-lt="${y}-${m}"></div>`;
      }
    });
    html += `</div><div class="cal-legend" style="margin-top:8px"><span class="ramp"><i style="background:var(--cal-zero)"></i>${[1, 2, 3, 4, 5, 6, 7].map((k) => `<i style="background:var(--o${k})"></i>`).join("")}</span><span class="muted">harmonized cells per month (log scale)</span><span><i class="sw lt-modis-sw"></i>MODIS only (before VIIRS), scaled</span></div>`;
    return html;
  }

  function ltTrendText(t, label) {
    if (!t) return `<p class="muted">${label}: not enough full years.</p>`;
    const dir = t.direction === "increasing" ? "rising" : t.direction === "decreasing" ? "falling" : "no significant trend";
    return `<p><b>${label}:</b> ${dir}${t.direction.startsWith("no") ? "" : ` ${t.pct_per_year > 0 ? "+" : ""}${t.pct_per_year}% per year`} (${t.first_year}–${t.last_year}, p ${pv(t.p_value)}, Sen's slope ${t.slope_per_year > 0 ? "+" : ""}${n0(t.slope_per_year)} cells/yr).</p>`;
  }

  function ltFacts(lt) {
    let html = `<div class="trend-list">`;
    const t = lt.trend_modis;
    const up = t?.direction === "increasing", down = t?.direction === "decreasing";
    const icon = up ? "M4 17 10 11l4 4 6-7m0 0h-5m5 0v5" : down ? "M4 7l6 6 4-4 6 7m0 0h-5m5 0v-5" : "M4 12h16";
    html += `<div><div class="trend-head"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icon}"/></svg>${up ? "Long-term rise in fire activity" : down ? "Long-term decline in fire activity" : "No significant long-term trend"}</div>`;
    html += ltTrendText(t, "MODIS (consistent sensor since 2000)");
    html += ltTrendText(lt.trend_harmonized, "Harmonized MODIS + VIIRS");
    if (lt.seasonal_kendall_modis) {
      const sk = lt.seasonal_kendall_modis;
      html += `<p><b>Month-by-month (seasonal Kendall):</b> ${sk.direction === "increasing" ? "rising" : sk.direction === "decreasing" ? "falling" : "no significant change"}, p ${pv(sk.p_value)}.</p>`;
    }
    if (lt.sampled) html += `<p class="muted">Based on ${lt.full_years.length} sampled years (${lt.full_years.join(", ")}), not every year; Sen's slope uses the real gaps between them.</p>`;
    html += `<div class="caveat" style="margin-top:6px">The headline trend uses MODIS alone so the 2012 arrival of VIIRS can't masquerade as a trend. MODIS (Terra/Aqua) is near end of life; recent years may carry slightly more uncertainty.</div></div>`;
    const sh = lt.season_shift || {};
    const fmtShift = (x, word) => (x ? `${word} ${x.days_per_decade > 0 ? "+" : ""}${x.days_per_decade} days/decade${x.significant ? "" : " (not significant)"}` : null);
    const parts = [fmtShift(sh.start, "start"), fmtShift(sh.end, "end"), fmtShift(sh.length, "length")].filter(Boolean);
    if (parts.length) html += `<div><h3>Season timing</h3><p>${parts.join(" · ")}. Negative start = the season begins earlier.</p></div>`;
    if (lt.top_years.length) html += `<div><h3>Biggest years</h3><p>${lt.top_years.slice(0, 5).map((y) => `<b>${y.year}</b> (${n0(y.harmonized)})`).join(", ")}.${lt.unusual_years.length ? ` Unusual: ${lt.unusual_years.map((u) => `${u.year} ${u.kind === "extreme" ? "↑" : "↓"} ${u.ratio_to_median}× typical`).join(", ")}.` : ""}</p></div>`;
    if (lt.record_intensity) { const r = lt.record_intensity; html += `<div><h3>Record intensity</h3><p><b>${r.year}</b>: peak overpass FRP ${n0(r.peak_frp_mw)} MW, ${r.ratio}× the next highest year (${r.next_year}).</p></div>`; }
    if (lt.decades.length) html += `<div><h3>By decade (MODIS cells per year)</h3><p>${lt.decades.map((d) => `${d.decade}: <b>${n0(d.mean_modis_cells)}</b>`).join(" · ")}</p></div>`;
    return html + `</div>`;
  }

  function ltAnnual(el, lt) {
    const ys = lt.years;
    const w = Math.max(el.clientWidth, 320), h = 220, m = { l: 52, r: 10, t: 12, b: 24 };
    const pw = w - m.l - m.r, ph = h - m.t - m.b;
    const y0 = ys[0].year, n = ys[ys.length - 1].year - y0 + 1;   // x by calendar year, so sampled gaps stay visible
    const ymax = niceMax(Math.max(1, ...ys.map((y) => Math.max(y.harmonized, y.modis))));
    const x = (i) => m.l + ((ys[i].year - y0 + 0.5) / n) * pw, y = (v) => m.t + ph - (Math.min(v, ymax) / ymax) * ph, bw = Math.min(pw / n, 40);
    let html = `<div class="chart-legend"><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--c-harm)"/></svg>harmonized (full year)</span><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--o2)"/></svg>partial year</span><span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--c-modis)" stroke-width="2"/><circle cx="8" cy="6" r="3.5" fill="var(--c-modis)"/></svg>MODIS only</span>${lt.trend_modis ? `<span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="4 3"/></svg>MODIS trend (Sen)</span>` : ""}</div>`;
    html += `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Annual fire cells">`;
    for (let k = 0; k <= 4; k++) { const v = (ymax * k) / 4; html += `<line class="${k ? "grid" : "axis"}" x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}"></line><text class="muted-text" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${n0(v)}</text>`; }
    let lastLabel = -1e9;
    ys.forEach((r, i) => {
      const tipHtml = `<b>${r.year}</b>${r.full ? "" : " (partial year)"}<br>${n0(r.harmonized)} harmonized cells<br>MODIS ${n0(r.modis)} · VIIRS ${r.viirs_days ? n0(r.viirs) : "not yet in orbit"}<br>${r.active_days} active fire days · peak overpass FRP ${n0(r.peak_frp_mw)} MW`;
      html += `<rect x="${x(i) - bw / 2 + 1}" y="${y(r.harmonized)}" width="${Math.max(1, bw - 2)}" height="${m.t + ph - y(r.harmonized)}" rx="2" fill="var(${r.full ? "--c-harm" : "--o2"})" tabindex="0" data-tip="${esc(tipHtml)}"></rect>`;
      if (i === 0 || x(i) - lastLabel >= 34) {   // skip labels that would collide
        html += `<text class="muted-text" x="${x(i)}" y="${h - 6}" text-anchor="middle">${r.year}</text>`;
        lastLabel = x(i);
      }
    });
    // MODIS: a dot per full year; lines only join consecutive years, so sampled gaps aren't drawn as data.
    let path = "", prev = null;
    ys.forEach((r, i) => {
      if (r.modis_days < 300) { prev = null; return; }
      path += `${prev !== null && r.year - prev === 1 ? "L" : "M"}${x(i).toFixed(1)},${y(r.modis).toFixed(1)}`;
      prev = r.year;
    });
    html += `<path d="${path}" fill="none" stroke="var(--c-modis)" stroke-width="2"></path>`;
    ys.forEach((r, i) => { if (r.modis_days >= 300) html += `<circle cx="${x(i)}" cy="${y(r.modis)}" r="4" fill="var(--c-modis)" stroke="var(--surface)" stroke-width="2"></circle>`; });
    const t = lt.trend_modis;
    if (t) {
      const i0 = ys.findIndex((r) => r.year === t.first_year), i1 = ys.findIndex((r) => r.year === t.last_year);
      html += `<line x1="${x(i0)}" x2="${x(i1)}" y1="${y(Math.max(0, t.line[0]))}" y2="${y(Math.max(0, t.line[1]))}" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"></line>`;
    }
    html += `</svg>`;
    el.innerHTML = html;
    el.querySelectorAll("[data-tip]").forEach((node) => {
      node.addEventListener("mousemove", (e) => tip.show(e, node.getAttribute("data-tip")));
      node.addEventListener("mouseleave", () => tip.hide());
    });
  }

  // ------------------------------------------------------- national context
  async function loadNational(bbox, seq, name) {
    const card = $("#cal-nat");
    if (!cal.natList) {
      try { cal.natList = await api("/api/country/list"); } catch { cal.natList = { available: false, countries: [] }; }
      $("#nat-country").innerHTML = `<option value="">Guess from area</option>` + cal.natList.countries.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
    }
    if (!cal.natList.available) { card.hidden = true; return; }
    const pick = name ?? $("#nat-country").value;
    let p;
    try {
      p = await api(`/api/country/profile?lane=${FV.lane()}&bbox=${bbox.join(",")}${pick ? `&name=${encodeURIComponent(pick)}` : ""}`);
    } catch (e) { card.hidden = false; $("#nat-body").innerHTML = `<div class="empty">Could not load national context: ${esc(e.message)}</div>`; return; }
    if (seq !== cal.seq) return;
    cal.nat = p;
    card.hidden = false;
    renderNational();
  }

  function renderNational() {
    const p = cal.nat, body = $("#nat-body");
    if (!p || p.status !== "ok") {
      $("#nat-sub").textContent = "";
      body.innerHTML = `<div class="empty">No country matched this area's centre. Pick a country above to see its national record.</div>`;
      return;
    }
    $("#nat-sub").textContent = `· ${p.country}${p.guess === p.country && !$("#nat-country").value ? " (matched from the area)" : ""}`;
    const years = p.annual_modis.map((r) => r.year);
    let html = `<p class="muted" style="margin:0 0 12px">NASA FIRMS country archive: MODIS detections for ${years.length} supplied years (${years[0]}–${years[years.length - 1]}), VIIRS for recent years. Detections per year for the whole country; missing years are missing, not zero.</p>`;
    html += `<div class="lt-grid"><div><h3>National detections per year</h3><div id="nat-annual"></div></div><div>${natFacts(p)}</div></div>`;
    html += `<h3 style="margin-top:18px">National fire calendar${p.area_vs_national ? " vs this area" : ""}</h3><div id="nat-season"></div>`;
    if (p.anomalies.length || p.crosschecks_2025.length) html += natAnoms(p);
    html += `<details class="nat-caveats"><summary>Data caveats</summary><ul>${p.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>`;
    html += `<h3 style="margin-top:18px">National government action options</h3><div>${actionsHtml(p.actions, "No national findings that call for action.")}</div>`;
    body.innerHTML = html;
    natAnnual($("#nat-annual"), p);
    natSeason($("#nat-season"), p);
  }

  function natFacts(p) {
    const t = p.trend, rb = p.robustness, c = p.comparison;
    let html = `<div class="trend-list">`;
    if (t) {
      const sig = t.direction !== "no significant trend";
      const head = rb.robust ? (t.slope_per_year > 0 ? "Robust national rise" : "Robust national decline") : sig ? "Trend not robust" : "No significant national trend";
      const icon = rb.robust && t.slope_per_year > 0 ? "M4 17 10 11l4 4 6-7m0 0h-5m5 0v5" : rb.robust ? "M4 7l6 6 4-4 6 7m0 0h-5m5 0v-5" : "M4 12h16";
      html += `<div><div class="trend-head"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icon}"/></svg>${head}</div>
        <p>Mann-Kendall p ${pv(t.p_value)}, Sen's slope ${t.slope_per_year > 0 ? "+" : ""}${n0(t.slope_per_year)} detections/yr (${t.pct_per_year > 0 ? "+" : ""}${t.pct_per_year}%/yr).</p></div>`;
      const row = (label, x, pct) => `<tr><td>${label}</td><td class="num">${x ? `${x.pct_per_year > 0 ? "+" : ""}${x.pct_per_year}%/yr` : "–"}</td><td class="num">${pct == null ? "–" : `${pct > 0 ? "+" : ""}${pct}%`}</td></tr>`;
      html += `<div><h3>Does the direction hold?</h3><div class="table-wrap"><table><thead><tr><th>Subset</th><th class="num">Sen slope</th><th class="num">Recent vs early</th></tr></thead><tbody>
        ${row("All detections", t, c?.pct)}${row("High confidence", rb.high_confidence, c?.high_confidence_pct)}${row("Aqua only", rb.aqua, c?.aqua_pct)}${row("Terra only", rb.terra, c?.terra_pct)}</tbody></table></div>
        <p class="muted">${rb.agree ? "All subsets point the same way." : "<b>Subsets disagree</b>, so don't build policy on a direction."}${c ? ` Early years ${esc(c.early_years.replaceAll(",", ", "))} vs recent ${esc(c.recent_years.replaceAll(",", ", "))}.` : ""}</p></div>`;
    }
    if (p.seasonal_kendall) {
      const sk = p.seasonal_kendall;
      html += `<div><h3>Month by month</h3><p>${sk.direction === "no significant trend" ? "No significant change" : sk.direction === "increasing" ? "Rising" : "Falling"} (seasonal Kendall, p ${pv(sk.p_value)}).</p></div>`;
    }
    if (p.pattern) {
      const pt = p.pattern;
      html += `<div><h3>Season shape</h3><p>Peak <b>${esc(pt.peak_month)}</b>; top three months ${esc(pt.top3)} hold ${Math.round(pt.top3_share * 100)}% of detections. The peak falls in the same month in ${Math.round(pt.peak_agreement * 100)}% of years${pt.peak_stable ? "" : `; with high-confidence detections only, the peak moves to ${esc(pt.high_confidence_peak)}`}.</p></div>`;
    }
    if (p.area_vs_national) {
      const a = p.area_vs_national;
      html += `<div><h3>This area vs the country</h3><p>Area peak <b>${a.area_peak}</b>, national peak <b>${a.national_peak}</b>${a.peak_shift_months ? ` (${Math.abs(a.peak_shift_months)} month${Math.abs(a.peak_shift_months) > 1 ? "s" : ""} ${a.peak_shift_months > 0 ? "later" : "earlier"})` : ""}; the monthly patterns overlap ${Math.round(a.overlap * 100)}%.</p></div>`;
    }
    return html + `</div>`;
  }

  function natAnnual(el, p) {
    const rows = p.annual_modis, vi = p.annual_viirs || {};
    const all = [...rows.map((r) => r.year), ...Object.values(vi).flat().map((r) => r.year)];
    const y0 = Math.min(...all), y1 = Math.max(...all), n = y1 - y0 + 1;
    const w = Math.max(el.clientWidth, 300), h = 200, m = { l: 56, r: 10, t: 10, b: 24 };
    const pw = w - m.l - m.r, ph = h - m.t - m.b;
    const ymax = niceMax(Math.max(1, ...rows.map((r) => r.retained), ...Object.values(vi).flat().map((r) => r.retained)));
    const x = (yr) => m.l + ((yr - y0 + 0.5) / n) * pw, y = (v) => m.t + ph - (Math.min(v, ymax) / ymax) * ph, bw = Math.min(pw / n - 2, 26);
    const plat = { "viirs-jpss1": ["NOAA-20", "--c-viirs"], "viirs-snpp": ["S-NPP", "--a7"] };
    let html = `<div class="chart-legend"><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--c-modis)"/></svg>MODIS</span>${Object.keys(vi).map((k) => `<span><svg width="12" height="12"><circle cx="6" cy="6" r="4.5" fill="var(${(plat[k] || [k, "--ink-2"])[1]})"/></svg>VIIRS ${esc((plat[k] || [k])[0])}</span>`).join("")}${p.trend ? `<span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="4 3"/></svg>MODIS trend</span>` : ""}</div>`;
    html += `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="National detections per year">`;
    for (let k = 0; k <= 4; k++) { const v = (ymax * k) / 4; html += `<line class="${k ? "grid" : "axis"}" x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}"></line><text class="muted-text" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v >= 10000 ? (v % 1000 ? (v / 1000).toFixed(1) : v / 1000) + "k" : n0(v)}</text>`; }
    let last = -1e9;
    rows.forEach((r) => {
      const tipHtml = `<b>${r.year}</b> MODIS<br>${n0(r.retained)} detections · ${n0(r.high_confidence)} high confidence<br>${n0(r.night)} at night`;
      html += `<rect x="${x(r.year) - bw / 2}" y="${y(r.retained)}" width="${bw}" height="${m.t + ph - y(r.retained)}" rx="2" fill="var(--c-modis)" tabindex="0" data-tip="${esc(tipHtml)}"></rect>`;
    });
    Object.entries(vi).forEach(([k, list]) => list.forEach((r) => {
      html += `<circle cx="${x(r.year)}" cy="${y(r.retained)}" r="4.5" fill="var(${(plat[k] || [k, "--ink-2"])[1]})" stroke="var(--surface)" stroke-width="1.5" tabindex="0" data-tip="${esc(`<b>${r.year}</b> VIIRS ${(plat[k] || [k])[0]}<br>${n0(r.retained)} detections`)}"></circle>`;
    }));
    if (p.trend) html += `<line x1="${x(p.trend.years[0])}" x2="${x(p.trend.years[p.trend.years.length - 1])}" y1="${y(Math.max(0, p.trend.line[0]))}" y2="${y(Math.max(0, p.trend.line[1]))}" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"></line>`;
    [...new Set(all)].sort((a, b) => a - b).forEach((yr) => { if (x(yr) - last >= 34) { html += `<text class="muted-text" x="${x(yr)}" y="${h - 6}" text-anchor="middle">${yr}</text>`; last = x(yr); } });
    el.innerHTML = html + `</svg>`;
    el.querySelectorAll("[data-tip]").forEach((node) => { node.addEventListener("mousemove", (e) => tip.show(e, node.getAttribute("data-tip"))); node.addEventListener("mouseleave", () => tip.hide()); });
  }

  function natSeason(el, p) {
    const s = p.season;
    if (!s.length) { el.innerHTML = `<div class="empty">No seasonal calendar for this country.</div>`; return; }
    const area = p.area_vs_national?.area_share;
    const w = Math.max(el.clientWidth, 300), h = 190, m = { l: 40, r: 10, t: 10, b: 24 };
    const pw = w - m.l - m.r, ph = h - m.t - m.b;
    const ymax = Math.min(1, Math.ceil(Math.max(...s.map((r) => r.share), ...(area || [0])) * 10 + 0.5) / 10);
    const x = (i) => m.l + ((i + 0.5) / 12) * pw, y = (v) => m.t + ph - (v / ymax) * ph, bw = pw / 12 - 6;
    const band = { high: "--o5", moderate: "--o3", low: "--o1" };
    let html = `<div class="chart-legend"><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--o5)"/></svg>high</span><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--o3)"/></svg>moderate</span><span><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--o1)" stroke="var(--axis)"/></svg>low (national share of the year's detections)</span>${area ? `<span><svg width="16" height="12"><line x1="0" x2="16" y1="6" y2="6" stroke="var(--ink)" stroke-width="2"/><circle cx="8" cy="6" r="3" fill="var(--ink)"/></svg>this area</span>` : ""}</div>`;
    html += `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="National monthly share of detections">`;
    for (let k = 0; k <= 4; k++) { const v = (ymax * k) / 4; html += `<line class="${k ? "grid" : "axis"}" x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}"></line><text class="muted-text" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>`; }
    s.forEach((r, i) => {
      const tipHtml = `<b>${MONTHS[r.month - 1]}</b> · ${esc(r.band)} (seasonal index ${r.seasonal_index})<br>${Math.round(r.share * 100)}% of a year's national detections<br>typical ${n0(r.median)} detections (p10 ${n0(r.p10)} – p90 ${n0(r.p90)})${area ? `<br>this area: ${Math.round(area[i] * 100)}%` : ""}`;
      html += `<rect x="${x(i) - bw / 2}" y="${y(r.share)}" width="${bw}" height="${m.t + ph - y(r.share)}" rx="2" fill="var(${band[r.band] || "--o2"})" tabindex="0" data-tip="${esc(tipHtml)}"></rect>`;
      html += `<text class="muted-text" x="${x(i)}" y="${h - 6}" text-anchor="middle">${MONTHS[r.month - 1]}</text>`;
    });
    if (area) {
      html += `<path d="${area.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(Math.min(v, ymax)).toFixed(1)}`).join("")}" fill="none" stroke="var(--ink)" stroke-width="2"></path>`;
      area.forEach((v, i) => { html += `<circle cx="${x(i)}" cy="${y(Math.min(v, ymax))}" r="3" fill="var(--ink)"></circle>`; });
    }
    el.innerHTML = html + `</svg>`;
    el.querySelectorAll("[data-tip]").forEach((node) => { node.addEventListener("mousemove", (e) => tip.show(e, node.getAttribute("data-tip"))); node.addEventListener("mouseleave", () => tip.hide()); });
  }

  function natAnoms(p) {
    let html = `<h3 style="margin-top:18px">Exceptional months in the national record</h3>`;
    if (p.anomalies.length) {
      html += `<div class="table-wrap"><table><thead><tr><th>Month</th><th class="num">Detections</th><th class="num">Usual (median)</th><th class="num">Ratio</th><th class="num">High-conf. ratio</th><th>Note</th></tr></thead><tbody>${p.anomalies.map((a) => `<tr><td>${a.month_name} ${a.year}</td><td class="num">${n0(a.detections)}</td><td class="num">${n0(a.historical_median)}</td><td class="num"><b>${a.ratio}×</b></td><td class="num">${a.high_confidence_ratio}×</td><td class="detail-text">${a.drift_review ? "MODIS orbital-drift review needed" : `vs ${a.prior_years} earlier years`}</td></tr>`).join("")}</tbody></table></div>`;
    }
    if (p.crosschecks_2025.length) {
      html += `<p class="muted">2025 check against each sensor's own history: ${p.crosschecks_2025.map((c) => `${c.month_name} ${esc(c.sensor)} ${c.ratio}× (${c.prior_years} earlier yrs)`).join(" · ")}.</p>`;
    }
    return html;
  }

  // ---------------------------------------------------------------- events
  function wire() {
    $("#cal-area").addEventListener("change", () => {
      const o = $("#cal-area").selectedOptions[0];
      if (o?.dataset.bbox) setBbox(o.dataset.bbox.split(",").map(Number));
      load();
    });
    Object.values(form()).forEach((i) => i.addEventListener("change", () => { $("#cal-area").value = "custom"; load(); }));
    Object.values(form()).forEach((i) => i.addEventListener("input", () => { $("#cal-area").value = "custom"; check(); }));
    $("#cal-period").addEventListener("change", load);
    $("#cal-start").addEventListener("change", load);
    $("#cal-end").addEventListener("change", load);
    $("#cal-low").addEventListener("change", load);
    $("#cal-fetch").addEventListener("click", fetchData);
    $$("#cal-metric [data-metric]").forEach((b) => b.addEventListener("click", () => {
      cal.metric = b.dataset.metric;
      $$("#cal-metric [data-metric]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      renderGrid();
    }));
    const grid = $("#cal-grid");
    grid.addEventListener("click", (e) => {
      if (e.target.closest("[data-cal-fetch]")) { fetchData(); return; }
      const b = e.target.closest(".cal-d[data-day]");
      if (b && !b.classList.contains("nodata")) selectDay(b.dataset.day);
    });
    grid.addEventListener("mousemove", (e) => {
      const b = e.target.closest(".cal-d[data-day]");
      if (!b || !cal.data) { tip.hide(); return; }
      const x = cal.data.days.find((dd) => dd.day === b.dataset.day);
      if (x) tip.show(e, dayTip(x));
    });
    grid.addEventListener("mouseleave", () => tip.hide());
    $("#cal-anoms").addEventListener("click", (e) => {
      const a = e.target.closest("[data-anom]");
      if (!a) return;
      cal.anomSel = +a.dataset.anom;
      const an = cal.data.analysis.anomalies[cal.anomSel];
      $$("#cal-anoms .anom").forEach((x) => x.classList.toggle("sel", x === a));
      selectDay(an.peak_day || an.start);
      $(`#cal-grid .cal-d[data-day="${an.peak_day || an.start}"]`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    $("#lt-body").addEventListener("mousemove", (e) => {
      const c = e.target.closest("[data-lt]");
      if (!c || !cal.lt) { if (!e.target.closest("[data-tip]")) tip.hide(); return; }
      const [y, mo] = c.dataset.lt.split("-").map(Number);
      const r = cal.lt.months.find((m) => m.year === y && m.month === mo);
      if (r) tip.show(e, `<b>${MONTHS[mo - 1]} ${y}</b><br>${n0(r.harmonized)} harmonized cells<br>MODIS ${n0(r.modis)} · VIIRS ${r.viirs_available ? n0(r.viirs) : "n/a"}<br><span class="t-muted">${r.days_with_data} days with data${r.viirs_available ? "" : " · MODIS only, scaled"}</span>`);
    });
    $("#lt-body").addEventListener("mouseleave", () => tip.hide());
    $("#nat-country").addEventListener("change", () => {
      const b = cal.data?.bbox || readBbox();
      if (b) loadNational(b, cal.seq);
    });
    $("#cal-actions").addEventListener("click", (e) => {
      const r = e.target.closest("[data-anom-ref]");
      if (!r) return;
      e.preventDefault();
      const card = $(`#cal-anoms [data-anom="${+r.dataset.anomRef - 1}"]`);
      if (card) { card.click(); card.scrollIntoView({ behavior: "smooth", block: "center" }); }
    });
    let rt;
    new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { if (!$("#view-calendar").hidden) { renderSeries(); if (!$("#cal-lt").hidden && cal.lt) renderLongterm(); if (!$("#cal-nat").hidden && cal.nat) renderNational(); } }, 120); }).observe($("#view-calendar"));
  }

  // ------------------------------------------------------------ public API
  FV.calendar = {
    async show() {
      if (!cal.loaded) {
        cal.loaded = true;
        await loadAreaOptions();
        pickDefaultArea();
      }
      load();
    },
    onLane() {
      if ($("#view-calendar").hidden) { cal.loaded = false; return; }
      cal.loaded = false; cal.selected = null; cal.data = null;
      this.show();
    },
    onTheme() {
      if (cal.dayMap) { setDayBasemap(); if (cal.selected) renderDay(); }
    },
    onJobProgress(job) {
      FV.job = { running: true };
      if (job.kind !== "calendar" || $("#view-calendar").hidden) return;
      const el = $("#cal-status");
      el.className = "area-check cal-status";
      el.innerHTML = `<span class="spinner"></span><span>${esc(job.message)}</span><span class="progress"><i style="width:${Math.round((job.progress || 0) * 100)}%"></i></span>`;
    },
    async onJobDone(job) {
      FV.job = { running: false };
      if (job.status === "done") {
        const r = job.result || {}, t = r.totals || {};
        const errs = Object.keys(r.source_errors || {});
        toast(`Calendar data ready: ${n0(t.accepted)} new detections from ${r.chunks} chunks${r.chunks_skipped ? ` (${r.chunks_skipped} already stored)` : ""}${errs.length ? ` · errors from ${errs.join(", ")}` : ""}`, 8000);
      } else {
        toast(`Calendar fetch failed: ${job.message}`, 10000);
      }
      if (job.params.mode !== FV.lane()) return;
      await loadAreaOptions();
      const sel = $("#cal-area"), key = job.params.bbox.join(",");
      const match = [...sel.options].find((o) => o.dataset.bbox === key);
      if (match) sel.value = match.value;
      if (!$("#view-calendar").hidden) load();
    },
  };

  wire();
})();
