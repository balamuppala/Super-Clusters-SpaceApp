/* ftd.io dashboard app (vanilla JS + Leaflet). */
(function () {
  "use strict";
  const { esc, cssVar } = Charts;

  const LABELS = [
    { key: "confirmed_fire", label: "Confirmed fire", color: "--v-confirmed" },
    { key: "likely_fire", label: "Likely fire", color: "--v-likely" },
    { key: "uncertain", label: "Uncertain", color: "--v-uncertain" },
    { key: "likely_false_alarm", label: "Likely false alarm", color: "--v-likely-false" },
    { key: "false_alarm", label: "False alarm", color: "--v-false" },
  ];
  const LABEL = Object.fromEntries(LABELS.map((l) => [l.key, l]));
  const CAUSES = {
    gas_flare: "Gas flare", volcanic_activity: "Volcanic activity", industrial_heat_source: "Industrial heat source",
    persistent_heat_source: "Persistent heat source", stationary_heat_source: "Stationary heat source",
    weak_single_detection: "Weak single detection", single_detection: "Single detection",
    non_vegetated_surface: "Non-vegetated surface", no_vegetation_change: "No vegetation change",
    no_backscatter_change: "No radar change", urban_hot_surface: "Urban hot surface",
    water_surface_glint: "Water glint", bare_hot_surface: "Hot bare ground", unexplained_non_fire: "Unexplained",
  };
  const REASONS = {
    R_PARSE: "Payload could not be parsed", R_COORD: "Coordinates out of range", R_AOI: "Outside the area of interest",
    R_FUTURE: "Timestamp far in the future", R_PHYSICS: "Physically impossible value", R_DUPLICATE: "Duplicate record",
    R_NO_DATA: "No usable samples", R_GEOMETRY: "Malformed geometry", R_DERIVATION: "Derived feature failed",
    R_MANUAL: "Rejected by a reviewer", Q_LOW_CONF: "Low provider confidence", Q_CLOCK_SKEW: "Timestamp slightly in the future",
    Q_OUT_OF_WINDOW: "Older than the requested window", Q_DAYNIGHT: "Day/night flag contradicts sun position",
    Q_SAA: "South Atlantic Anomaly artefact", Q_MISSING_FRP: "Fire radiative power missing",
    Q_CLOUDY: "Too cloudy for vegetation index", Q_LOW_SAMPLES: "Too few valid radar samples",
  };

  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const state = {
    lane: store.get("fv.lane", "simulated"), summary: null, events: [], selected: null, detail: null,
    tab: "events", filter: "all", search: "", sort: { key: "last_seen", dir: -1 },
    basemap: "map", job: null, runMode: "simulated", view: store.get("fv.view", "verify"),
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // ------------------------------------------------------------------ api
  const ADMIN_STORE = "fv.adminToken";
  function adminToken() { try { return sessionStorage.getItem(ADMIN_STORE) || ""; } catch { return ""; } }
  function setAdminToken(value) {
    try { if (value) sessionStorage.setItem(ADMIN_STORE, value); else sessionStorage.removeItem(ADMIN_STORE); } catch { /* private mode */ }
  }
  function promptAdminToken(message) {
    const entered = window.prompt(message || "Enter the ingestion password to run ingest, backfill, or quarantine actions.");
    if (entered && entered.trim()) setAdminToken(entered.trim());
    return adminToken();
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (init.method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-Requested-With"] = "fireverify";
      const tok = adminToken();
      if (tok) init.headers["Authorization"] = "Bearer " + tok;
      init.body = JSON.stringify(opts.body || {});
    }
    const res = await fetch(path, init);
    const data = await res.json().catch(() => ({}));
    if (res.status === 401 && init.method !== "GET" && !opts._retried) {
      setAdminToken("");
      if (promptAdminToken("Password missing or incorrect. Enter the ingestion password.")) {
        return api(path, Object.assign({}, opts, { _retried: true }));
      }
    }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const laneQ = () => `lane=${state.lane}`;

  function toast(msg, ms = 4500) {
    const el = $("#toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { el.hidden = true; }, ms);
  }

  // ---------------------------------------------------------- formatting
  const pct = (p) => (p >= 0.9995 ? ">99.9%" : p < 0.0005 ? "<0.1%" : p >= 0.995 || p < 0.01 ? `${(p * 100).toFixed(1)}%` : `${Math.round(p * 100)}%`);
  const when = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "–");
  const num = (v, d = 1) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
  const chip = (label) => { const l = LABEL[label] || { label, color: "--muted" }; return `<span class="chip"><i class="dot" style="--c:var(${l.color})"></i>${esc(l.label)}</span>`; };
  const cause = (c) => (c ? esc(CAUSES[c] || c.replaceAll("_", " ")) : "");
  const codes = (list) => (list || []).map((c) => `<span class="code" title="${esc(REASONS[c] || c)}">${esc(c)}</span>`).join("");

  // ----------------------------------------------------------------- map
  let map = null, eventLayer = null, focusLayer = null, baseLayer = null;
  const markers = new Map();

  function isDark() {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function initMap() {
    if (typeof L === "undefined") {
      $("#map").innerHTML = `<div class="empty">Map library could not load (offline?). The table below still works.</div>`;
      return;
    }
    map = L.map("map", { zoomControl: true, worldCopyJump: true, preferCanvas: false }).setView([20, 0], 2);
    eventLayer = L.layerGroup().addTo(map);
    focusLayer = L.layerGroup().addTo(map);
    setBasemap(state.basemap);
  }

  function setBasemap(kind) {
    if (!map) return;
    state.basemap = kind;
    if (baseLayer) map.removeLayer(baseLayer);
    const esri = (path) => `https://server.arcgisonline.com/ArcGIS/rest/services/${path}/MapServer/tile/{z}/{y}/{x}`;
    if (kind === "satellite") {
      baseLayer = L.layerGroup([
        L.tileLayer(esri("World_Imagery"), { maxZoom: 19, maxNativeZoom: 17, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }),
        L.tileLayer(esri("Reference/World_Boundaries_and_Places"), { maxZoom: 19, maxNativeZoom: 13, opacity: 0.8 }),
      ]);
    } else {
      // Keyless Esri canvas basemaps, light or dark to match the theme.
      const tone = isDark() ? "Dark" : "Light";
      baseLayer = L.layerGroup([
        L.tileLayer(esri(`Canvas/World_${tone}_Gray_Base`), { maxZoom: 19, maxNativeZoom: 13, attribution: "Basemap © Esri, HERE, Garmin, © OpenStreetMap contributors" }),
        L.tileLayer(esri(`Canvas/World_${tone}_Gray_Reference`), { maxZoom: 19, maxNativeZoom: 13 }),
      ]);
    }
    baseLayer.addTo(map);
    baseLayer.eachLayer((l) => l.bringToBack());
    $$("[data-basemap]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.basemap === kind)));
  }

  function markerStyle(ev, selected) {
    return {
      radius: Math.min(16, 5 + Math.sqrt(ev.n_detections) * 1.1) + (selected ? 3 : 0),
      fillColor: cssVar(LABEL[ev.label]?.color || "--muted"),
      color: selected ? cssVar("--ink") : cssVar("--surface"),
      weight: selected ? 3 : 2, fillOpacity: 0.95, opacity: 1,
    };
  }

  function renderMarkers(fit) {
    if (!map) return;
    eventLayer.clearLayers();
    markers.clear();
    const shown = filteredEvents();
    // false alarms underneath, fires on top
    const order = ["false_alarm", "likely_false_alarm", "uncertain", "likely_fire", "confirmed_fire"];
    [...shown].sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label)).forEach((ev) => {
      const m = L.circleMarker([ev.lat, ev.lon], markerStyle(ev, ev.cluster_id === state.selected));
      m.bindTooltip(`<b>${esc(LABEL[ev.label]?.label || ev.label)}</b> · P ${pct(ev.probability)}<br>${ev.attribution ? cause(ev.attribution) + "<br>" : ""}${ev.n_detections} detections · ${esc(ev.sensors.join("+"))}`, { direction: "top", offset: [0, -6] });
      m.on("click", () => select(ev.cluster_id, map.getZoom() < 8));
      m.addTo(eventLayer);
      markers.set(ev.cluster_id, m);
    });
    $("#map-empty").hidden = state.events.length > 0;
    if (fit && shown.length) {
      const b = L.latLngBounds(shown.map((e) => [e.lat, e.lon]));
      map.fitBounds(b.pad(0.2), { maxZoom: 10 });
    }
    renderLegend();
  }

  function renderLegend() {
    const counts = state.summary?.labels || {};
    $("#map-legend").innerHTML = LABELS.map((l) => `<div class="row"><i class="dot" style="--c:var(${l.color})"></i>${l.label}<span class="muted" style="margin-left:auto;padding-left:10px">${counts[l.key] || 0}</span></div>`).join("")
      + `<div class="row muted" style="margin-top:2px">size = number of detections</div>`;
  }

  function drawFocus(detail) {
    if (!map) return;
    focusLayer.clearLayers();
    if (!detail) return;
    const s = detail.derived.cluster_summary;
    L.circle([s.centroid_lat, s.centroid_lon], { radius: s.radius_km * 1000, color: cssVar("--ink-2"), weight: 1, fill: false, interactive: false }).addTo(focusLayer);
    detail.detections.forEach((d) => {
      L.circleMarker([d.lat, d.lon], { radius: 3, color: cssVar("--surface"), weight: 1, fillColor: cssVar("--ink"), fillOpacity: 0.9, interactive: false }).addTo(focusLayer);
    });
  }

  // ---------------------------------------------------------- summary/KPIs
  function renderSummary() {
    const s = state.summary;
    if (!s) return;
    const l = s.labels || {};
    const fire = (l.confirmed_fire || 0) + (l.likely_fire || 0);
    const falseA = (l.false_alarm || 0) + (l.likely_false_alarm || 0);
    $("#kpi-events").textContent = num(s.events, 0);
    $("#kpi-events-foot").textContent = s.last_run ? `last run ${when(s.last_run.started_at)} UTC` : "no runs yet";
    $("#kpi-fire").textContent = num(fire, 0);
    $("#kpi-fire-foot").textContent = `${l.confirmed_fire || 0} confirmed`;
    $("#kpi-uncertain").textContent = num(l.uncertain || 0, 0);
    $("#kpi-false").textContent = num(falseA, 0);
    const top = Object.entries(s.causes || {}).sort((a, b) => b[1] - a[1])[0];
    $("#kpi-false-foot").textContent = top ? `top cause: ${(CAUSES[top[0]] || top[0]).toLowerCase()}` : "";
    $("#kpi-quarantine").textContent = num(s.quarantine_pending, 0);
    $("#kpi-rejected").textContent = `${num(s.rejected, 0)} rejected`;
    $("#count-events").textContent = s.events || "";
    $("#count-quarantine").textContent = s.quarantine_pending || "";
    $("#count-rejected").textContent = s.rejected || "";
    $("#table-version").textContent = `Likelihoods ${s.likelihood_table.version}`;
    Charts.segmentedBar($("#distribution"), LABELS.map((x) => ({ ...x, value: l[x.key] || 0 })), { label: "Verdict distribution" });
  }

  // --------------------------------------------------------------- detail
  function renderDetail() {
    const el = $("#detail");
    const d = state.detail;
    if (!d) {
      el.innerHTML = `<div class="detail-empty"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="10" r="3"/><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/></svg><p>Select an event on the map or in the table to see why it was scored the way it was.</p></div>`;
      return;
    }
    const v = d.verdict, s = d.derived.cluster_summary || {}, veg = d.derived.vegetation_loss || {},
      sar = d.derived.sar_backscatter_change || {}, tf = d.derived.temporal_frequency || {},
      ctx = d.derived.context_proximity || {}, xs = d.derived.cross_sensor_corroboration || {};
    const HEAT = ["gas_flare", "volcano", "industrial", "power_plant", "landfill"];
    const nearest = Object.entries(ctx.nearest || {}).filter(([k]) => HEAT.includes(k)).sort((a, b) => a[1].distance_km - b[1].distance_km)[0];
    el.innerHTML = `
      <div class="detail-head">
        <div>
          <div class="event-id">${esc(v.cluster_id)}</div>
          <div class="hero">${pct(v.probability)}<small>P(active vegetation fire)</small></div>
        </div>
        ${chip(v.label)}
      </div>
      ${v.attribution ? `<div class="cause">Probable cause: <b>${cause(v.attribution)}</b></div>` : ""}
      <div class="prob-track" aria-hidden="true"><div class="prob-marker" style="left:${(v.probability * 100).toFixed(2)}%"></div></div>
      <div class="prob-scale"><span>false alarm</span><span>uncertain</span><span>fire</span></div>
      ${d.sim_truth ? `<div class="truth">Simulation ground truth: <b>${esc(d.sim_truth.replaceAll("_", " "))}</b></div>` : ""}

      <div class="section"><h3>Why this score</h3><div id="ev-chart"></div></div>
      <div class="section"><h3>Vegetation &amp; radar change</h3><div id="ba-chart"></div></div>
      <div class="section"><h3>Detections (${s.n_detections}) over time</h3><div id="tl-chart"></div></div>
      <div class="section"><h3>Facts</h3>
        <dl class="facts">
          <div><dt>Overpasses</dt><dd>${s.n_overpasses} · ${esc((s.sensors || []).join(" + "))}</dd></div>
          <div><dt>Max / total FRP</dt><dd>${num(s.max_frp_mw)} / ${num(s.total_frp_mw, 0)} MW</dd></div>
          <div><dt>First seen (UTC)</dt><dd>${when(s.first_seen)}</dd></div>
          <div><dt>Last seen (UTC)</dt><dd>${when(s.last_seen)}</dd></div>
          <div><dt>Location</dt><dd>${num(s.centroid_lat, 4)}, ${num(s.centroid_lon, 4)}</dd></div>
          <div><dt>Extent</dt><dd>${num(Math.max(0, s.radius_km - 0.5), 2)} km</dd></div>
          <div><dt>Fire history</dt><dd>${esc(tf.recurrence_class || "–")} · ${tf.distinct_days ?? 0} days / ${tf.distinct_years ?? 0} yrs</dd></div>
          <div><dt>Nearest known source</dt><dd>${nearest ? `${esc(nearest[0].replace("_", " "))} ${num(nearest[1].distance_km, 2)} km` : "none mapped"}</dd></div>
          <div><dt>Land cover</dt><dd>${esc((ctx.land_cover || []).join(", ") || "unknown")}</dd></div>
          <div><dt>Sentinel-3 granules</dt><dd>${xs.s3_slstr_frp_granules ?? 0}</dd></div>
        </dl>
        ${Object.keys(v.posterior.missing).length ? `<ul class="missing-list">${Object.entries(v.posterior.missing).map(([g, why]) => `<li><b>${esc(g)}</b> not used: ${esc(why)}</li>`).join("")}</ul>` : ""}
      </div>`;
    drawDetailCharts();
  }

  function drawDetailCharts() {
    const d = state.detail;
    if (!d || !$("#ev-chart")) return;
    const veg = d.derived.vegetation_loss || {}, sar = d.derived.sar_backscatter_change || {};
    Charts.evidenceChart($("#ev-chart"), d.verdict);
    const optical = d.enrichment.find((e) => e.kind === "optical_change")?.data || {};
    const sarRec = d.enrichment.find((e) => e.kind === "sar_change")?.data || {};
    const f2 = (x) => (x >= 0 ? "" : "−") + Math.abs(x).toFixed(2);
    const fdb = (x) => `${(x >= 0 ? "" : "−") + Math.abs(x).toFixed(1)} dB`;
    Charts.beforeAfter($("#ba-chart"), [
      { label: "NDVI", pre: optical.ndvi_pre, post: optical.ndvi_post, domain: [-0.2, 1], fmt: f2, note: veg.status === "complete" ? `burn severity: ${veg.burn_severity}` : `status: ${veg.status}` },
      { label: "NBR", pre: optical.nbr_pre, post: optical.nbr_post, domain: [-0.6, 1], fmt: f2, note: veg.dnbr != null ? `dNBR ${veg.dnbr.toFixed(3)}` : "" },
      { label: "Radar VH", pre: sarRec.vh_pre_db, post: sarRec.vh_post_db, domain: [-26, -4], fmt: fdb, note: sar.status === "complete" ? `dVH ${sar.dvh_db.toFixed(2)} dB` : `status: ${sar.status}` },
    ]);
    Charts.timeline($("#tl-chart"), d.detections);
  }

  async function select(cid, pan = true) {
    state.selected = cid;
    markers.forEach((m, id) => {
      const ev = state.events.find((e) => e.cluster_id === id);
      if (ev) m.setStyle(markerStyle(ev, id === cid));
      if (id === cid) m.bringToFront();
    });
    $$("#tab-panel tr[data-cid]").forEach((tr) => tr.classList.toggle("selected", tr.dataset.cid === cid));
    try {
      state.detail = await api(`/api/events/${cid}?${laneQ()}`);
    } catch (e) {
      toast(e.message);
      return;
    }
    if (state.selected !== cid) return;
    renderDetail();
    drawFocus(state.detail);
    if (map && pan) {
      const s = state.detail.derived.cluster_summary;
      map.setView([s.centroid_lat, s.centroid_lon], Math.max(map.getZoom(), 11));
    }
    if (window.innerWidth < 1180) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------------ tabs
  function filteredEvents() {
    const q = state.search.trim().toLowerCase();
    return state.events.filter((e) => (state.filter === "all" || e.label === state.filter)
      && (!q || e.cluster_id.includes(q) || (e.attribution || "").includes(q) || e.label.includes(q)));
  }

  async function renderTab() {
    $$(".tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    const panel = $("#tab-panel");
    try {
      if (state.tab === "events") renderEventsTab(panel);
      else if (state.tab === "quarantine") renderQuarantineTab(panel, await api(`/api/quarantine?${laneQ()}`));
      else if (state.tab === "rejected") renderRejectedTab(panel, await api(`/api/rejected?${laneQ()}`));
      else if (state.tab === "runs") renderRunsTab(panel, await api(`/api/runs`));
      else if (state.tab === "sources") renderSourcesTab(panel, await api(`/api/sources`));
    } catch (e) {
      panel.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  function renderEventsTab(panel) {
    const counts = state.summary?.labels || {};
    const rows = filteredEvents();
    const { key, dir } = state.sort;
    rows.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null) return 1;
      if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
    const th = (k, label, cls = "") => `<th data-sort="${k}" class="${cls}" aria-sort="${state.sort.key === k ? (dir > 0 ? "ascending" : "descending") : "none"}">${label}</th>`;
    panel.innerHTML = `
      <div class="toolbar">
        <button class="filter-chip" data-filter="all" aria-pressed="${state.filter === "all"}">All <span class="muted">${state.events.length}</span></button>
        ${LABELS.map((l) => `<button class="filter-chip" data-filter="${l.key}" aria-pressed="${state.filter === l.key}"><i class="dot" style="--c:var(${l.color})"></i>${l.label} <span class="muted">${counts[l.key] || 0}</span></button>`).join("")}
        <input class="search" type="search" placeholder="Search id, cause, label" value="${esc(state.search)}" aria-label="Search events">
      </div>
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr>${th("label", "Verdict")}${th("probability", "P(fire)", "num")}${th("attribution", "Probable cause")}${th("n_detections", "Detections", "num")}${th("max_frp_mw", "Max FRP (MW)", "num")}<th>Sensors</th>${th("evidence_observed", "Evidence", "num")}${th("last_seen", "Last seen (UTC)")}<th>Location</th></tr></thead>
        <tbody>${rows.map((e) => `<tr class="clickable ${e.cluster_id === state.selected ? "selected" : ""}" data-cid="${e.cluster_id}" tabindex="0">
          <td>${chip(e.label)}</td><td class="num">${pct(e.probability)}</td><td>${cause(e.attribution)}</td>
          <td class="num">${e.n_detections}</td><td class="num">${num(e.max_frp_mw)}</td><td>${esc(e.sensors.join(" + "))}</td>
          <td class="num">${e.evidence_observed}/6</td><td>${when(e.last_seen)}</td><td class="mono">${e.lat.toFixed(3)}, ${e.lon.toFixed(3)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">${state.events.length ? "No events match this filter." : "No events yet. Use Run ingestion to fetch or simulate data."}</div>`}`;
    panel.querySelector(".search").addEventListener("input", (e) => {
      state.search = e.target.value;
      clearTimeout(renderEventsTab.t);
      renderEventsTab.t = setTimeout(() => { renderEventsTab(panel); renderMarkers(false); const s = panel.querySelector(".search"); s.focus(); s.setSelectionRange(s.value.length, s.value.length); }, 200);
    });
  }

  function reasonRows(obj) {
    return Object.entries(obj || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: k, value: v, description: REASONS[k] || "" }));
  }

  function renderQuarantineTab(panel, rows) {
    panel.innerHTML = `
      <div class="charts-row">
        <div><h3><svg class="status-icon warn" viewBox="0 0 24 24" aria-hidden="true" style="vertical-align:-2px;margin-right:4px"><path d="M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5"/></svg>Pending by reason</h3><div id="q-chart"></div></div>
        <div class="muted" style="align-self:center">Quarantined records are plausible but suspect. <b>Release</b> re-checks the hard rules and moves the record into the ${esc(state.lane)} lane; <b>Reject</b> sends it to the audit table. Released detections join an event on the next run.</div>
      </div>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Reasons</th><th>Source</th><th>Kind</th><th>Detail</th><th>Quarantined (UTC)</th><th></th></tr></thead><tbody>
        ${rows.map((r) => `<tr data-qid="${r.qid}"><td>${codes(r.reasons)}</td><td>${esc(r.source)}</td><td>${esc(r.kind.replaceAll("_", " "))}</td>
          <td class="detail-text">${esc((r.details || []).join("; "))}</td><td>${when(r.created_at)}</td>
          <td><div class="row-actions"><button class="btn btn-sm" data-act="release">Release</button><button class="btn btn-sm" data-act="reject">Reject</button></div></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Quarantine is empty.</div>`}`;
    Charts.hbars($("#q-chart"), reasonRows(state.summary?.quarantine_reasons), "var(--st-warning)", "Pending quarantine by reason");
  }

  function renderRejectedTab(panel, rows) {
    panel.innerHTML = `
      <div class="charts-row">
        <div><h3><svg class="status-icon crit" viewBox="0 0 24 24" aria-hidden="true" style="vertical-align:-2px;margin-right:4px"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/></svg>Rejected by reason</h3><div id="r-chart"></div></div>
        <div class="muted" style="align-self:center">Rejected records are impossible or unusable. They are kept with their raw payload for audit and never used for scoring.</div>
      </div>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Reasons</th><th>Source</th><th>Kind</th><th>Detail</th><th>Rejected (UTC)</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td>${codes(r.reasons)}</td><td>${esc(r.source)}</td><td>${esc(r.kind.replaceAll("_", " "))}</td><td class="detail-text">${esc((r.details || []).join("; "))}</td><td>${when(r.rejected_at)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Nothing rejected.</div>`}`;
    Charts.hbars($("#r-chart"), reasonRows(state.summary?.rejected_reasons), "var(--st-critical)", "Rejected by reason");
  }

  function renderRunsTab(panel, runs) {
    panel.innerHTML = runs.length ? `<div class="table-wrap"><table><thead><tr><th>Run</th><th>Mode</th><th>Started (UTC)</th><th class="num">Accepted</th><th class="num">Quarantined</th><th class="num">Rejected</th><th class="num">Events enriched</th><th>Source problems</th></tr></thead><tbody>
      ${runs.map((r) => {
        const st = r.stats || {};
        const bad = Object.entries(r.source_status || {}).filter(([, s]) => s.status === "error" || s.status === "unavailable");
        return `<tr><td class="mono">${esc(r.run_id)}</td><td>${esc(r.mode)}</td><td>${when(r.started_at)}</td><td class="num">${st.accepted ?? "–"}</td><td class="num">${st.quarantined ?? "–"}</td><td class="num">${st.rejected ?? "–"}</td><td class="num">${st.clusters_enriched ?? "–"}</td>
          <td class="detail-text">${bad.length ? bad.map(([n, s]) => `<span class="code" title="${esc(s.detail)}">${esc(n)}: ${esc(s.status)}</span>`).join("") : '<span class="status-ok">✓ all ok</span>'}</td></tr>`;
      }).join("")}</tbody></table></div>` : `<div class="empty">No runs yet.</div>`;
  }

  function renderSourcesTab(panel, sources) {
    const s = state.summary;
    const last = s?.last_run?.source_status || {};
    panel.innerHTML = `
      <p class="muted" style="margin-top:0">Credentials are read from <span class="mono">.env</span>. Likelihood table: <b>${esc(s?.likelihood_table.version)}</b> (${esc(s?.likelihood_table.meta?.source || "")}).</p>
      <div class="table-wrap"><table><thead><tr><th>Source</th><th>Configured</th><th>Last ${esc(state.lane)} run</th><th>Detail</th></tr></thead><tbody>
      ${Object.entries(sources).map(([name, info]) => {
        const lr = last[name];
        return `<tr><td class="mono">${esc(name)}</td>
          <td>${info.available ? '<span class="status-ok">✓ ready</span>' : '<span class="status-bad">✕ ' + esc(info.detail) + "</span>"}</td>
          <td>${lr ? `${esc(lr.status)} · ${lr.fetched} fetched` : "–"}</td>
          <td class="detail-text">${lr && lr.status !== "ok" ? esc(lr.detail) : ""}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  // ------------------------------------------------------------ run dialog
  const MAX_LIVE_AREA = 100;
  let areaLayer = null, regionsLoaded = false, activeAreas = [];
  const fmtLat = (v) => `${Math.abs(v).toFixed(1)}°${v >= 0 ? "N" : "S"}`;
  const fmtLon = (v) => `${Math.abs(v).toFixed(1)}°${v >= 0 ? "E" : "W"}`;

  function viewBbox() {
    if (!map) return null;
    const b = map.getBounds();
    return [Math.max(-180, b.getWest()), Math.max(-90, b.getSouth()), Math.min(180, b.getEast()), Math.min(90, b.getNorth())].map((x) => +x.toFixed(2));
  }

  function setAreaInputs(bbox) {
    const f = $("#run-form");
    ["w", "s", "e", "n"].forEach((k, i) => { f[k].value = bbox ? bbox[i] : ""; });
    checkArea();
  }

  function readArea() {
    const f = $("#run-form");
    const v = ["w", "s", "e", "n"].map((k) => parseFloat(f[k].value));
    return v.every(Number.isFinite) ? v : null;
  }

  // Returns the bbox if it is valid for a live run, else null; updates the status line and Start button.
  function checkArea() {
    const el = $("#area-check"), start = $("#run-submit");
    if (state.runMode !== "live") { start.disabled = false; return null; }
    const ok = (msg) => { el.className = "area-check"; el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--st-good)"><path d="m5 12 5 5 9-10"/></svg>${msg}`; };
    const bad = (msg) => { el.className = "area-check bad"; el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--st-warning)"><path d="M12 3 2 21h20L12 3zm0 6v6m0 2.5v.5"/></svg>${msg}`; };
    let result = null;
    const b = readArea();
    if (!state.summary?.credentials.firms) bad("FIRMS_MAP_KEY is not set in .env, so live runs are unavailable.");
    else if (!b) bad("Enter all four coordinates, or pick an area above.");
    else {
      const [w, s, e, n] = b;
      const area = (e - w) * (n - s);
      if (w < -180 || e > 180 || s < -90 || n > 90) bad("Coordinates are out of range (longitude ±180, latitude ±90).");
      else if (w >= e || s >= n) bad("West must be less than East, and South less than North.");
      else if (area > MAX_LIVE_AREA) bad(`This area is ${Math.round(area).toLocaleString()} deg². Live runs are limited to ${MAX_LIVE_AREA} deg² (about 10° × 10°): pick an active area, a region, or draw a smaller box.`);
      else {
        const kmW = (e - w) * 111.32 * Math.cos(((s + n) / 2) * Math.PI / 180), kmH = (n - s) * 110.57;
        ok(`${area < 1 ? area.toFixed(2) : area.toFixed(1)} deg² (≈ ${Math.round(kmW).toLocaleString()} × ${Math.round(kmH).toLocaleString()} km) around ${fmtLat((s + n) / 2)}, ${fmtLon((w + e) / 2)}`);
        result = b;
      }
    }
    start.disabled = !result;
    return result;
  }

  async function loadAreaOptions() {
    if (!regionsLoaded) {
      try {
        const regions = await api("/api/regions");
        $("#area-regions").innerHTML = regions.map((r, i) => `<option value="region:${i}" data-bbox="${r.bbox.join(",")}">${esc(r.name)}</option>`).join("");
        regionsLoaded = true;
      } catch { /* presets are optional */ }
    }
    const group = $("#area-active");
    if (!state.summary?.credentials.firms) { group.innerHTML = `<option disabled>Needs FIRMS_MAP_KEY</option>`; return; }
    if (activeAreas.length) return;
    try {
      activeAreas = await api("/api/active-areas");
      group.innerHTML = activeAreas.length
        ? activeAreas.map((a, i) => `<option value="active:${i}" data-bbox="${a.bbox.join(",")}">${fmtLat(a.center[0])} ${fmtLon(a.center[1])} · ${a.detections.toLocaleString()} detections</option>`).join("")
        : `<option disabled>No active fires found</option>`;
    } catch (e) {
      group.innerHTML = `<option disabled>Could not load: ${esc(e.message)}</option>`;
    }
  }

  function onAreaSource() {
    const sel = $("#area-source");
    const v = sel.value;
    if (v === "view") setAreaInputs(viewBbox());
    else if (v === "draw") startDraw();
    else if (v === "custom") checkArea();
    else setAreaInputs(sel.selectedOptions[0].dataset.bbox.split(",").map(Number));
  }

  function setRunMode(mode) {
    state.runMode = mode;
    $$("#run-mode [data-mode]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
    $$("[data-mode-panel]").forEach((p) => { p.hidden = p.dataset.modePanel !== mode; });
    if (mode === "live") {
      loadAreaOptions();
      if ($("#area-source").value === "view" || !readArea()) { $("#area-source").value = "view"; setAreaInputs(viewBbox()); }
    }
    checkArea();
  }

  function openRun(mode) {
    $("#run-error").hidden = true;
    setRunMode(typeof mode === "string" ? mode : state.lane);
    const field = $("#admin-token");
    if (field && adminToken()) field.value = adminToken();
    $("#run-dialog").showModal();
    (state.runMode === "live" ? $("#area-source") : $("#run-form").runs).focus();
  }

  function drawAreaBox(bbox) {
    if (!map) return;
    if (areaLayer) map.removeLayer(areaLayer);
    if (!bbox) { areaLayer = null; return; }
    const [w, s, e, n] = bbox;
    areaLayer = L.rectangle([[s, w], [n, e]], { color: cssVar("--ink"), weight: 2, fill: true, fillOpacity: 0.06, interactive: false }).addTo(map);
  }

  function startDraw() {
    $("#run-dialog").close();
    if (!map) return;
    const card = $(".map-card"), banner = $("#draw-banner");
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("drawing");
    banner.hidden = false;
    map.dragging.disable();
    map.boxZoom.disable();
    let start = null, rect = null;
    const down = (e) => {
      e.originalEvent.preventDefault();   // no text selection while dragging
      start = e.latlng;
      rect = L.rectangle([start, start], { color: cssVar("--ink"), weight: 2, fillOpacity: 0.08, interactive: false }).addTo(map);
      rect.bindTooltip("", { permanent: true, direction: "center", className: "draw-size" }).openTooltip();
    };
    const move = (e) => {
      if (!start || !rect) return;
      const b = L.latLngBounds(start, e.latlng);
      rect.setBounds(b);
      const area = (b.getEast() - b.getWest()) * (b.getNorth() - b.getSouth());
      const tooBig = area > MAX_LIVE_AREA;
      rect.setStyle({ color: tooBig ? cssVar("--st-critical") : cssVar("--ink") });
      rect.setTooltipContent(`${area < 10 ? area.toFixed(1) : Math.round(area).toLocaleString()} deg²${tooBig ? " · too large (max 100)" : ""}`);
      rect.openTooltip(b.getCenter());
    };
    const up = (e) => { if (start) finish(L.latLngBounds(start, e.latlng)); };
    const key = (e) => { if (e.key === "Escape") finish(null); };
    const cancel = () => finish(null);
    map.on("mousedown", down); map.on("mousemove", move); map.on("mouseup", up);
    document.addEventListener("keydown", key);
    $("#draw-cancel").addEventListener("click", cancel, { once: true });

    function finish(bounds) {
      map.off("mousedown", down); map.off("mousemove", move); map.off("mouseup", up);
      document.removeEventListener("keydown", key);
      $("#draw-cancel").removeEventListener("click", cancel);
      if (rect) map.removeLayer(rect);
      map.dragging.enable();
      map.boxZoom.enable();
      card.classList.remove("drawing");
      banner.hidden = true;
      const sel = $("#area-source");
      if (bounds && bounds.getNorth() - bounds.getSouth() > 0.01 && bounds.getEast() - bounds.getWest() > 0.01) {
        const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].map((x) => +x.toFixed(2));
        sel.value = "custom";
        setAreaInputs(bbox);
        drawAreaBox(bbox);
      } else if (sel.value === "draw") {
        sel.value = "view";
        setAreaInputs(viewBbox());
      }
      openRun("live");
    }
  }

  async function submitRun(e) {
    e.preventDefault();
    const f = $("#run-form");
    const typed = ($("#admin-token") && $("#admin-token").value.trim()) || "";
    if (typed) setAdminToken(typed);
    let body;
    if (state.runMode === "live") {
      const bbox = checkArea();
      if (!bbox) return;
      body = { mode: "live", bbox, days: +f.live_days.value };
    } else {
      body = { mode: "simulated", runs: +f.runs.value, days: +f.sim_days.value, seed: +f.seed.value };
    }
    try {
      const job = await api("/api/ingest", { method: "POST", body });
      $("#run-dialog").close();
      if (body.mode === "live" && map) {
        drawAreaBox(body.bbox);
        const [w, s, ee, n] = body.bbox;
        map.fitBounds([[s, w], [n, ee]], { padding: [20, 20] });
      }
      trackJob(job);
    } catch (err) {
      $("#run-error").textContent = err.message;
      $("#run-error").hidden = false;
    }
  }

  function trackJob(job) {
    state.job = job;
    const pill = $("#job-status");
    $("#run-btn").disabled = job.status === "running";
    if ($("#cal-fetch")) $("#cal-fetch").disabled = job.status === "running";
    if (job.status === "running") {
      pill.hidden = false;
      const prog = job.progress != null ? ` · ${Math.round(job.progress * 100)}%` : "";
      pill.innerHTML = `<span class="spinner"></span>${esc(job.kind === "calendar" ? "Calendar: " : "")}${esc(job.message)}${prog}`;
      window.FV.calendar?.onJobProgress(job);
      setTimeout(async () => {
        try { trackJob(await api(`/api/jobs/${job.job_id}`)); } catch (err) { pill.textContent = err.message; }
      }, 1500);
      return;
    }
    pill.hidden = true;
    if (job.kind === "calendar") { window.FV.calendar?.onJobDone(job); return; }
    if (job.status === "done") {
      const r = job.result || {};
      const errs = Object.keys(r.source_errors || {});
      toast(`Run finished: ${r.totals?.accepted ?? 0} accepted, ${r.totals?.quarantined ?? 0} quarantined, ${r.totals?.rejected ?? 0} rejected · ${r.events_scored} events scored${errs.length ? ` · source errors: ${errs.join(", ")}` : ""}`, 8000);
      const fit = job.params.mode !== "live";   // live: keep the map on the area that was just run
      if (state.lane !== job.params.mode) setLane(job.params.mode, fit);
      else loadAll(fit);
    } else {
      toast(`Run failed: ${job.message}`, 10000);
    }
  }

  // --------------------------------------------------------------- loading
  let loadSeq = 0;
  async function loadAll(fit) {
    const seq = ++loadSeq;   // a slower, older response must never overwrite a newer one (e.g. after a lane switch)
    let summary, events;
    try {
      [summary, events] = await Promise.all([api(`/api/summary?${laneQ()}`), api(`/api/events?${laneQ()}`)]);
    } catch (e) {
      if (seq === loadSeq) toast(`Could not load data: ${e.message}`);
      return;
    }
    if (seq !== loadSeq) return;
    state.summary = summary;
    state.events = events;
    renderSummary();
    renderMarkers(fit);
    if (state.selected && !state.events.some((e) => e.cluster_id === state.selected)) {
      state.selected = null; state.detail = null; renderDetail(); drawFocus(null);
    }
    renderTab();
  }

  function setView(view) {
    state.view = view;
    store.set("fv.view", view);
    $$(".viewbar [data-view]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
    $("#view-verify").hidden = view !== "verify";
    $("#view-calendar").hidden = view !== "calendar";
    if (view === "verify" && map) setTimeout(() => map.invalidateSize(), 0);
    if (view === "calendar") window.FV.calendar?.show();
  }

  function setLane(lane, fit = true) {
    state.lane = lane;
    store.set("fv.lane", lane);
    state.selected = null; state.detail = null; state.filter = "all";
    $$(".topbar [data-lane]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.lane === lane)));
    renderDetail();
    drawFocus(null);
    loadAll(fit);
    window.FV.calendar?.onLane(lane);
  }

  function applyTheme(theme) {
    if (theme) document.documentElement.dataset.theme = theme;
    setBasemap(state.basemap);
    renderSummary();
    renderMarkers(false);
    drawFocus(state.detail);
    drawDetailCharts();
    if (state.tab === "quarantine" || state.tab === "rejected") renderTab();
    window.FV.calendar?.onTheme();
  }

  // ---------------------------------------------------------------- events
  function wire() {
    $$(".topbar [data-lane]").forEach((b) => b.addEventListener("click", () => setLane(b.dataset.lane)));
    $$(".viewbar [data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
    $$("[data-basemap]").forEach((b) => b.addEventListener("click", () => setBasemap(b.dataset.basemap)));
    $$(".tabs [data-tab]").forEach((b) => b.addEventListener("click", () => { state.tab = b.dataset.tab; renderTab(); }));
    $("#run-btn").addEventListener("click", openRun);
    document.addEventListener("click", (e) => { if (e.target.closest("[data-open-run]")) openRun(); });
    $$("#run-mode [data-mode]").forEach((b) => b.addEventListener("click", () => setRunMode(b.dataset.mode)));
    $("#run-form").addEventListener("submit", submitRun);
    $("#run-cancel").addEventListener("click", () => $("#run-dialog").close());
    $("#area-source").addEventListener("change", onAreaSource);
    ["w", "s", "e", "n"].forEach((k) => $("#run-form")[k].addEventListener("input", () => { $("#area-source").value = "custom"; checkArea(); }));
    $("#theme-toggle").addEventListener("click", () => {
      const next = isDark() ? "light" : "dark";
      store.set("fv.theme", next);
      applyTheme(next);
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme());

    const panel = $("#tab-panel");
    panel.addEventListener("click", async (e) => {
      const f = e.target.closest("[data-filter]");
      if (f) { state.filter = f.dataset.filter; renderTab(); renderMarkers(true); return; }
      const s = e.target.closest("th[data-sort]");
      if (s) {
        const k = s.dataset.sort;
        state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "label" || k === "attribution" ? 1 : -1) };
        renderTab();
        return;
      }
      const act = e.target.closest("[data-act]");
      if (act) {
        const qid = act.closest("tr").dataset.qid;
        const action = act.dataset.act;
        if (action === "reject" && !confirm("Reject this record? It moves to the audit table and is never used for scoring.")) return;
        act.disabled = true;
        try {
          const r = await api(`/api/quarantine/${qid}/${action}`, { method: "POST", body: { note: `${action}d from dashboard` } });
          toast(action === "release" ? (r.outcome === "rejected" ? "Release failed a hard rule; record rejected." : "Released into the lane.") : "Rejected.");
          loadAll(false);
        } catch (err) { toast(err.message); act.disabled = false; }
        return;
      }
      const row = e.target.closest("tr[data-cid]");
      if (row) select(row.dataset.cid);
    });
    panel.addEventListener("keydown", (e) => {
      const row = e.target.closest("tr[data-cid]");
      if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(row.dataset.cid); }
    });

    let rt;
    new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { renderSummary(); drawDetailCharts(); }, 120); }).observe($("main"));
  }

  // Shared with calendar.js
  window.FV = Object.assign(window.FV || {}, {
    api, toast, esc, cssVar, isDark, fmtLat, fmtLon, trackJob,
    lane: () => state.lane, summary: () => state.summary, view: () => state.view,
  });

  // ------------------------------------------------------------------ boot
  async function boot() {
    const theme = store.get("fv.theme", null);
    if (theme) document.documentElement.dataset.theme = theme;
    $$(".topbar [data-lane]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.lane === state.lane)));
    initMap();
    wire();
    await loadAll(true);
    if (state.view === "calendar") setView("calendar");
    try {
      const job = await api("/api/jobs/latest");
      if (job && job.status === "running") trackJob(job);
    } catch { /* no jobs */ }
  }
  boot();
})();
