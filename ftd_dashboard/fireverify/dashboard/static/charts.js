/* Small SVG chart kit for the dashboard: no dependencies, colors come from CSS tokens. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function luminance(color) {
    const probe = document.createElement("span");
    probe.style.color = color;
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
    probe.remove();
    const lin = rgb.map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  }
  const labelInk = (fill) => (luminance(fill) > 0.4 ? "#0b0b0b" : "#ffffff");

  // ---------------------------------------------------------------- tooltip
  const tip = {
    el: null,
    show(evt, html) {
      this.el = this.el || document.getElementById("tooltip");
      this.el.innerHTML = html;
      this.el.hidden = false;
      const pad = 14, r = this.el.getBoundingClientRect();
      let x = evt.clientX + pad, y = evt.clientY + pad;
      if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
      if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
      this.el.style.left = `${x}px`;
      this.el.style.top = `${y}px`;
    },
    hide() { if (this.el) this.el.hidden = true; },
  };

  function bindTips(root) {
    root.querySelectorAll("[data-tip]").forEach((node) => {
      node.addEventListener("mousemove", (e) => tip.show(e, node.getAttribute("data-tip")));
      node.addEventListener("mouseleave", () => tip.hide());
      node.addEventListener("focus", (e) => {
        const r = node.getBoundingClientRect();
        tip.show({ clientX: r.right, clientY: r.top }, node.getAttribute("data-tip"));
      });
      node.addEventListener("blur", () => tip.hide());
    });
  }

  // Horizontal bar: square at the baseline, 4px rounded data end.
  function hbarPath(x0, x1, y, h, r = 4) {
    const w = Math.abs(x1 - x0);
    r = Math.min(r, w, h / 2);
    if (w < 0.5) return "";
    if (x1 >= x0) {
      return `M${x0},${y}H${x1 - r}Q${x1},${y} ${x1},${y + r}V${y + h - r}Q${x1},${y + h} ${x1 - r},${y + h}H${x0}Z`;
    }
    return `M${x0},${y}H${x1 + r}Q${x1},${y} ${x1},${y + r}V${y + h - r}Q${x1},${y + h} ${x1 + r},${y + h}H${x0}Z`;
  }

  const fmtLR = (lr) => (lr >= 1 ? `×${lr >= 10 ? lr.toFixed(0) : lr.toFixed(1)}` : `×${lr >= 0.1 ? lr.toFixed(2) : lr.toFixed(3)}`);
  const svgOpen = (w, h, label) => `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${esc(label)}">`;

  // ----------------------------------------------------------- segmented bar
  function segmentedBar(el, parts, opts = {}) {
    const total = parts.reduce((s, p) => s + p.value, 0);
    const w = Math.max(el.clientWidth, 200), h = 28, gap = 2;
    if (!total) { el.innerHTML = `<div class="empty">No scored events yet.</div>`; return; }
    let x = 0, html = svgOpen(w, h, opts.label || "distribution");
    const visible = parts.filter((p) => p.value > 0);
    const usable = w - gap * (visible.length - 1);
    visible.forEach((p, i) => {
      const pw = (p.value / total) * usable;
      const fill = cssVar(p.color);
      const share = Math.round((p.value / total) * 100);
      const tipHtml = `<b>${esc(p.label)}</b><br>${p.value} event${p.value === 1 ? "" : "s"} · ${share}%`;
      html += `<g tabindex="0" data-tip="${esc(tipHtml)}" aria-label="${esc(p.label)}: ${p.value} (${share}%)">`;
      html += `<rect x="${x}" y="0" width="${pw}" height="${h}" rx="4" fill="${fill}"></rect>`;
      const text = `${share}%`;
      if (pw > text.length * 7 + 16) {
        html += `<text x="${x + 8}" y="${h / 2 + 4}" style="fill:${labelInk(fill)};font-size:12px;font-weight:600">${text}</text>`;
      }
      html += `</g>`;
      x += pw + (i < visible.length - 1 ? gap : 0);
    });
    html += `</svg><div class="seg-legend">${parts.map((p) => `<span><i class="dot" style="--c:var(${p.color})"></i>${esc(p.label)}</span>`).join("")}</div>`;
    el.innerHTML = html;
    bindTips(el);
  }

  // ------------------------------------------- evidence waterfall (log-odds)
  function evidenceChart(el, verdict) {
    const post = verdict.posterior;
    const w = Math.max(el.clientWidth, 320);
    const labelW = 128, valueW = 58, rowH = 26, barH = 12;
    const rows = [];
    const priorLO = Math.log(post.prior / (1 - post.prior));
    rows.push({ kind: "prior", label: "Prior", sub: post.prior_basis, v: priorLO,
      text: `P ${post.prior.toFixed(2)}`, tip: `<b>Prior</b> from land cover: ${esc(post.prior_basis)}<br>P(fire) = ${post.prior.toFixed(2)} · log-odds ${priorLO.toFixed(2)}` });
    post.contributions.forEach((c) => rows.push({ kind: "term", label: c.group.replace("_", " "), sub: c.state.replaceAll("_", " "), v: c.log_lr,
      text: fmtLR(c.lr), tip: `<b>${esc(c.group)}: ${esc(c.state)}</b><br>${esc(c.detail)}<br><span class="t-muted">P(state|fire) ${c.p_given_fire.toFixed(3)} · P(state|not) ${c.p_given_not_fire.toFixed(3)}<br>likelihood ratio ${fmtLR(c.lr)} · log ${c.log_lr >= 0 ? "+" : ""}${c.log_lr.toFixed(2)}</span>` }));
    Object.entries(post.missing).forEach(([g, why]) => rows.push({ kind: "missing", label: g.replace("_", " "), sub: "missing", v: 0, text: "×1",
      tip: `<b>${esc(g)}</b> not used (likelihood ratio 1)<br>${esc(why)}` }));
    rows.push({ kind: "posterior", label: "Posterior", sub: "log-odds", v: post.log_odds, text: `P ${verdict.probability.toFixed(3)}`,
      tip: `<b>Posterior</b><br>log-odds ${post.log_odds.toFixed(2)} → P(active vegetation fire) = ${verdict.probability.toFixed(4)}` });

    const maxAbs = Math.max(3, ...rows.map((r) => Math.abs(r.v)));
    const plotW = w - labelW - valueW;
    const cx = labelW + plotW / 2;
    const sx = (v) => cx + (v / maxAbs) * (plotW / 2 - 4);
    const h = rows.length * rowH + 30;
    let html = svgOpen(w, h, "Evidence contributions in log-odds");
    // recessive guides
    [-maxAbs / 2, maxAbs / 2].forEach((g) => { html += `<line class="grid" x1="${sx(g)}" x2="${sx(g)}" y1="0" y2="${rows.length * rowH}"></line>`; });
    rows.forEach((r, i) => {
      const y = i * rowH;
      if (r.kind === "posterior") html += `<line class="axis" x1="0" x2="${w}" y1="${y + 1}" y2="${y + 1}"></line>`;
      const color = r.v >= 0 ? "var(--v-confirmed)" : "var(--v-false)";
      const by = y + (rowH - barH) / 2;
      html += `<g class="row" tabindex="0" data-tip="${esc(r.tip)}">`;
      html += `<rect class="hover-wash" x="0" y="${y}" width="${w}" height="${rowH}" fill="transparent" rx="4"></rect>`;
      html += `<text class="label" x="4" y="${y + rowH / 2 + 4}" style="${r.kind === "missing" ? "fill:var(--muted)" : ""};${r.kind === "posterior" ? "font-weight:600;fill:var(--ink)" : ""}">${esc(r.label)}<tspan class="muted-text" dx="6" style="font-size:11px">${esc(r.sub)}</tspan></text>`;
      if (r.kind !== "missing") {
        const path = hbarPath(cx, sx(r.v), by, barH);
        html += `<path d="${path}" fill="${color}" ${r.kind === "prior" ? 'fill-opacity="0.55"' : ""}></path>`;
      } else {
        html += `<line x1="${cx - 10}" x2="${cx + 10}" y1="${y + rowH / 2}" y2="${y + rowH / 2}" stroke="var(--axis)" stroke-width="2" stroke-linecap="round"></line>`;
      }
      html += `<text class="value" text-anchor="end" x="${w - 2}" y="${y + rowH / 2 + 4}" style="${r.kind === "missing" ? "fill:var(--muted)" : ""};${r.kind === "posterior" ? "font-weight:600" : ""}">${esc(r.text)}</text>`;
      html += `</g>`;
    });
    html += `<line class="axis" x1="${cx}" x2="${cx}" y1="0" y2="${rows.length * rowH}"></line>`;
    const ay = rows.length * rowH + 18;
    html += `<text class="muted-text" x="${cx - 8}" y="${ay}" text-anchor="end">◀ toward false alarm</text>`;
    html += `<text class="muted-text" x="${cx + 8}" y="${ay}">toward fire ▶</text>`;
    html += `</svg>`;
    el.innerHTML = html;
    bindTips(el);
  }

  // ------------------------------------------- before/after dumbbell rows
  function beforeAfter(el, rows) {
    rows = rows.filter((r) => r.pre != null || r.post != null);
    if (!rows.length) { el.innerHTML = `<div class="empty" style="padding:8px">No before/after imagery values yet.</div>`; return; }
    const w = Math.max(el.clientWidth, 300), labelW = 64, valueW = 128, rowH = 30;
    const h = rows.length * rowH + 4;
    let html = `<div class="chart-legend"><span><svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="var(--surface)" stroke="var(--ink-2)" stroke-width="2"/></svg>before fire</span><span><svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--ink)"/></svg>after fire</span></div>`;
    html += svgOpen(w, h, "Before and after values");
    rows.forEach((r, i) => {
      const y = i * rowH + rowH / 2;
      const [d0, d1] = r.domain;
      const sx = (v) => labelW + ((Math.min(d1, Math.max(d0, v)) - d0) / (d1 - d0)) * (w - labelW - valueW - 12) + 6;
      const delta = r.pre != null && r.post != null ? r.post - r.pre : null;
      const tipHtml = `<b>${esc(r.label)}</b><br>before ${r.pre == null ? "–" : r.fmt(r.pre)} · after ${r.post == null ? "not yet imaged" : r.fmt(r.post)}${delta == null ? "" : `<br>change ${delta >= 0 ? "+" : ""}${r.fmt(delta)}`}<br><span class="t-muted">${esc(r.note || "")}</span>`;
      html += `<g class="row" tabindex="0" data-tip="${esc(tipHtml)}">`;
      html += `<rect class="hover-wash" x="0" y="${y - rowH / 2}" width="${w}" height="${rowH}" fill="transparent" rx="4"></rect>`;
      html += `<text class="label" x="4" y="${y + 4}">${esc(r.label)}</text>`;
      html += `<line class="grid" x1="${sx(d0)}" x2="${sx(d1)}" y1="${y}" y2="${y}"></line>`;
      if (r.pre != null && r.post != null) html += `<line x1="${sx(r.pre)}" x2="${sx(r.post)}" y1="${y}" y2="${y}" stroke="var(--muted)" stroke-width="2" stroke-linecap="round"></line>`;
      if (r.pre != null) html += `<circle cx="${sx(r.pre)}" cy="${y}" r="5" fill="var(--surface)" stroke="var(--ink-2)" stroke-width="2"></circle>`;
      if (r.post != null) html += `<circle cx="${sx(r.post)}" cy="${y}" r="6" fill="var(--ink)" stroke="var(--surface)" stroke-width="2"></circle>`;
      const txt = r.post == null ? `${r.fmt(r.pre)} → pending` : delta == null ? r.fmt(r.post) : `${r.fmt(r.pre)} → ${r.fmt(r.post)}`;
      html += `<text class="value" text-anchor="end" x="${w - 2}" y="${y + 4}">${esc(txt)}</text>`;
      html += `</g>`;
    });
    html += `</svg>`;
    el.innerHTML = html;
    bindTips(el);
  }

  // ---------------------------------------------------- detection timeline
  function timeline(el, detections) {
    if (!detections.length) { el.innerHTML = ""; return; }
    const w = Math.max(el.clientWidth, 300), labelW = 78, rowH = 24, top = 4, axisH = 22;
    const platforms = [...new Set(detections.map((d) => d.platform))].sort();
    const times = detections.map((d) => Date.parse(d.observed_at));
    let t0 = Math.min(...times), t1 = Math.max(...times);
    const pad = Math.max((t1 - t0) * 0.06, 45 * 60 * 1000);
    t0 -= pad; t1 += pad;
    const plotW = w - labelW - 12;
    const sx = (t) => labelW + ((t - t0) / (t1 - t0)) * plotW;
    const maxFrp = Math.max(1, ...detections.map((d) => d.frp_mw || 0));
    const h = top + platforms.length * rowH + axisH;
    let html = `<div class="chart-legend"><span><svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--ink-2)"/></svg>day pass</span><span><svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="var(--surface)" stroke="var(--ink-2)" stroke-width="2"/></svg>night pass</span><span class="t-muted">dot size = fire radiative power</span></div>`;
    html += svgOpen(w, h, "Detections over time by satellite");
    platforms.forEach((p, i) => {
      const y = top + i * rowH + rowH / 2;
      html += `<line class="grid" x1="${labelW}" x2="${w}" y1="${y}" y2="${y}"></line>`;
      html += `<text class="label" x="4" y="${y + 4}">${esc(p)}</text>`;
    });
    const fmt = (t) => new Date(t).toISOString().slice(5, 16).replace("T", " ");
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const t = t0 + ((t1 - t0) * i) / ticks;
      const x = sx(t);
      html += `<text class="muted-text" x="${x}" y="${h - 6}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${fmt(t)}</text>`;
    }
    html += `<line class="axis" x1="${labelW}" x2="${w}" y1="${h - axisH}" y2="${h - axisH}"></line>`;
    const sorted = [...detections].sort((a, b) => (b.frp_mw || 0) - (a.frp_mw || 0));
    sorted.forEach((d) => {
      const y = top + platforms.indexOf(d.platform) * rowH + rowH / 2;
      const r = 3.5 + 5 * Math.sqrt((d.frp_mw || 0) / maxFrp);
      const night = d.daynight === "N";
      const tipHtml = `<b>${esc(d.platform)}</b> ${esc(d.sensor)} · ${night ? "night" : "day"}<br>${esc(d.observed_at.slice(0, 16).replace("T", " "))} UTC<br>FRP ${d.frp_mw == null ? "–" : d.frp_mw.toFixed(1) + " MW"} · ${esc(d.confidence)} confidence<br><span class="t-muted">${d.lat.toFixed(4)}, ${d.lon.toFixed(4)}</span>`;
      html += `<circle tabindex="0" data-tip="${esc(tipHtml)}" cx="${sx(Date.parse(d.observed_at))}" cy="${y}" r="${r}" ${night ? `fill="var(--surface)" stroke="var(--ink-2)" stroke-width="2"` : `fill="var(--ink-2)" fill-opacity="0.85" stroke="var(--surface)" stroke-width="2"`}></circle>`;
    });
    html += `</svg>`;
    el.innerHTML = html;
    bindTips(el);
  }

  // ------------------------------------------------- single-series bar chart
  function hbars(el, rows, color, title) {
    if (!rows.length) { el.innerHTML = `<div class="empty" style="padding:12px">None</div>`; return; }
    const w = Math.max(el.clientWidth, 260), labelW = 112, valueW = 40, rowH = 24, barH = 12;
    const max = Math.max(...rows.map((r) => r.value));
    const sx = (v) => labelW + (v / max) * (w - labelW - valueW);
    let html = svgOpen(w, rows.length * rowH, title);
    rows.forEach((r, i) => {
      const y = i * rowH;
      const tipHtml = `<b>${esc(r.label)}</b><br>${esc(r.description || "")}<br>${r.value} record${r.value === 1 ? "" : "s"}`;
      html += `<g class="row" tabindex="0" data-tip="${esc(tipHtml)}">`;
      html += `<rect class="hover-wash" x="0" y="${y}" width="${w}" height="${rowH}" fill="transparent" rx="4"></rect>`;
      html += `<text class="label mono" x="4" y="${y + rowH / 2 + 4}" style="font-size:11px">${esc(r.label)}</text>`;
      html += `<path d="${hbarPath(labelW, sx(r.value), y + (rowH - barH) / 2, barH)}" fill="${color}"></path>`;
      html += `<text class="value" x="${sx(r.value) + 6}" y="${y + rowH / 2 + 4}">${r.value}</text>`;
      html += `</g>`;
    });
    html += `</svg>`;
    el.innerHTML = html;
    bindTips(el);
  }

  window.Charts = { segmentedBar, evidenceChart, beforeAfter, timeline, hbars, tip, esc, cssVar };
})();
