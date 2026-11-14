'use strict';
// ---------- state ----------
let P, META, MON, map, aoiLayer, hotLayer, aoi = null; // aoi: {type:'box',s,w,n,e} | {type:'poly',ring:[[lat,lon],...]}
let SEL = null; // indices of points in current AOI (all dates), and in AOI+date range
const $ = id => document.getElementById(id);
const SERIES_LABEL = {HARMONIZED:'Harmonized MODIS-equivalent (MODIS obs. 2000-11→; mean of MODIS & k·VIIRS S-NPP from 2012-02)',
  MODIS:'MODIS Terra+Aqua observed', VIIRS:'VIIRS S-NPP 375 m observed', VIIRS_EQ:'VIIRS S-NPP scaled to MODIS-equivalent (k by calendar month)'};
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const Y0 = 2000, Y1 = 2026, NM = (Y1 - Y0 + 1) * 12;
const VIRIDIS = ['#440154','#482878','#3e4989','#31688e','#26828e','#1f9e89','#35b779','#6ece58','#b5de2b','#fde725'];
const DIVERG = ['#2166ac','#4393c3','#92c5de','#d1e5f0','#f7f7f7','#fee0b6','#fdb863','#e08214','#b35806'];
const hex = h => [1,3,5].map(i => parseInt(h.slice(i, i+2), 16));
function ramp(stops, t){ t = Math.max(0, Math.min(1, t)); const x = t*(stops.length-1), i = Math.min(stops.length-2, Math.floor(x)), f = x-i;
  const a = hex(stops[i]), b = hex(stops[i+1]); return `rgb(${a.map((v,j)=>Math.round(v+(b[j]-v)*f)).join(',')})`; }
const dayToISO = d => new Date(Date.UTC(2000,0,1) + d*864e5).toISOString().slice(0,10);
const isoToDay = s => Math.round((Date.parse(s+'T00:00:00Z') - Date.UTC(2000,0,1))/864e5);
const mi = (y,m) => (y-Y0)*12 + m; // m 0-based
const fmt = v => v == null || isNaN(v) ? '—' : (Math.abs(v) >= 100 || Number.isInteger(v) ? Math.round(v).toLocaleString('en') : v.toFixed(1));

// ---------- load ----------
Promise.all(['data/points.json','data/meta.json','data/monthly.json','data/france_metro.geojson'].map(u => fetch(u).then(r => r.json())))
.then(([p, m, mo, fr]) => {
  P = p; META = m; MON = mo; const n = P.lat.length;
  P.mon = new Int16Array(n); P.isV = new Uint8Array(n);
  for (let i = 0; i < n; i++) { const d = new Date(Date.UTC(2000,0,1) + P.day[i]*864e5); P.mon[i] = mi(d.getUTCFullYear(), d.getUTCMonth()); P.isV[i] = P.sat[i] === 2 ? 1 : 0; }
  // coverage per month index
  META.cov = {MODIS: new Float32Array(NM), VIIRS: new Float32Array(NM)};
  MON.forEach(r => { const k = mi(+r.month.slice(0,4), +r.month.slice(5,7)-1); META.cov.MODIS[k] = r.MODIS_cov; META.cov.VIIRS[k] = r.VIIRS_cov; });
  const ds = Object.entries(META.datasets).map(([k,v]) => `${k} ${v.dates[0]}→${v.dates[1]}`).join(' · ');
  $('subtitle').textContent = `Region: ${META.region}. Data: NASA FIRMS active-fire hotspots, vegetation fires only (type 0 / static-masked NRT), low-confidence removed. Coverage: MODIS ${META.coverage.MODIS.join('→')}, VIIRS S-NPP ${META.coverage.VIIRS.join('→')}. Pulled ${META.pulled_window}.`;
  initMap(fr); renderMethod(); run();
}).catch(e => { $('subtitle').textContent = 'Failed to load data: ' + e; });

// ---------- AOI ----------
function inAoi(lat, lon){
  if (!aoi) return true;
  if (aoi.type === 'box') return lat >= aoi.s && lat <= aoi.n && lon >= aoi.w && lon <= aoi.e;
  const r = aoi.ring; let c = false; // ray casting
  for (let i = 0, j = r.length-1; i < r.length; j = i++) { const [yi,xi] = r[i], [yj,xj] = r[j];
    if ((yi > lat) !== (yj > lat) && lon < (xj-xi)*(lat-yi)/(yj-yi)+xi) c = !c; }
  return c;
}
function aoiText(){ if (!aoi) return 'All metropolitan France';
  if (aoi.type === 'box') return `Box S${aoi.s} W${aoi.w} N${aoi.n} E${aoi.e}`;
  return `Drawn polygon (${aoi.ring.length} vertices)`; }
function initMap(fr){
  map = L.map('map', {preferCanvas: true}).setView([46.6, 2.4], 5);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 18, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
  L.geoJSON(fr, {style: {color: '#333', weight: 1, fill: false}}).addTo(map);
  aoiLayer = new L.FeatureGroup().addTo(map); hotLayer = L.layerGroup().addTo(map);
  map.addControl(new L.Control.Draw({edit: {featureGroup: aoiLayer, edit: false, remove: false},
    draw: {polyline: false, circle: false, marker: false, circlemarker: false, rectangle: {shapeOptions: {color: '#d7191c'}}, polygon: {shapeOptions: {color: '#d7191c'}}}}));
  map.on(L.Draw.Event.CREATED, ev => { aoiLayer.clearLayers(); aoiLayer.addLayer(ev.layer);
    if (ev.layerType === 'rectangle') { const b = ev.layer.getBounds(); setBox(b.getSouth(), b.getWest(), b.getNorth(), b.getEast(), false); }
    else { aoi = {type: 'poly', ring: ev.layer.getLatLngs()[0].map(p => [p.lat, p.lng])}; ['s','w','n','e'].forEach(k => $(k).value = ''); }
    run(); });
  $('applyBox').onclick = () => { const v = ['s','w','n','e'].map(k => parseFloat($(k).value));
    if (v.some(isNaN) || v[0] >= v[2] || v[1] >= v[3]) { alert('Enter numeric S < N and W < E (decimal degrees).'); return; }
    setBox(...v, true); run(); };
  $('resetAoi').onclick = () => { aoi = null; aoiLayer.clearLayers(); ['s','w','n','e'].forEach(k => $(k).value = ''); map.setView([46.6, 2.4], 5); run(); };
  $('run').onclick = run; $('series').onchange = run; $('calMode').onchange = renderCalendar;
  $('expCsv').onclick = () => doExport('csv'); $('expGeo').onclick = () => doExport('geojson');
}
function setBox(s, w, n, e, draw){
  const r = x => Math.round(x*1e4)/1e4; aoi = {type: 'box', s: r(s), w: r(w), n: r(n), e: r(e)};
  $('s').value = aoi.s; $('w').value = aoi.w; $('n').value = aoi.n; $('e').value = aoi.e;
  if (draw) { aoiLayer.clearLayers(); const rc = L.rectangle([[s, w], [n, e]], {color: '#d7191c'}); aoiLayer.addLayer(rc); map.fitBounds(rc.getBounds()); }
}

// ---------- aggregation ----------
let AGG; // {M:Float64Array, V:Float64Array, series:Float64Array}
function series(key, M, V){
  const out = new Float64Array(NM).fill(NaN), k = META.harmonization.k_calendar_month;
  for (let t = 0; t < NM; t++) {
    const cm = META.cov.MODIS[t], cv = META.cov.VIIRS[t], kk = k[String(t%12+1).padStart(2,'0')];
    const m = cm > 0 ? M[t] : NaN, v = cv > 0 ? V[t] : NaN, veq = cv > 0 ? kk*V[t] : NaN;
    if (key === 'MODIS') out[t] = m; else if (key === 'VIIRS') out[t] = v; else if (key === 'VIIRS_EQ') out[t] = veq;
    else { if (!isNaN(m) && !isNaN(veq)) out[t] = cm === cv ? (m+veq)/2 : m; else out[t] = isNaN(m) ? veq : m; }
  }
  return out;
}
const covOf = (key, t) => key === 'MODIS' ? META.cov.MODIS[t] : (key === 'HARMONIZED' ? Math.max(META.cov.MODIS[t], META.cov.VIIRS[t]) : META.cov.VIIRS[t]);
function run(){
  if (!P) return;
  const key = $('series').value, d0 = isoToDay($('d0').value), d1 = isoToDay($('d1').value);
  const M = new Float64Array(NM), V = new Float64Array(NM), sel = [], aoiIdx = [];
  const useM = key !== 'VIIRS' && key !== 'VIIRS_EQ', useV = key !== 'MODIS';
  for (let i = 0, n = P.lat.length; i < n; i++) {
    const la = P.lat[i]/1e4, lo = P.lon[i]/1e4;
    if (!inAoi(la, lo)) continue;
    aoiIdx.push(i);
    if (P.isV[i]) V[P.mon[i]]++; else M[P.mon[i]]++;
    if (P.day[i] >= d0 && P.day[i] <= d1 && (P.isV[i] ? useV : useM)) sel.push(i);
  }
  AGG = {M, V, key, s: series(key, M, V), d0, d1, aoiIdx};
  SEL = sel;
  const lab = `${aoiText()} · ${$('d0').value} → ${$('d1').value} · ${SERIES_LABEL[key]} · FIRMS pull ${META.pulled_window}`;
  $('searchLabel').textContent = 'Search: ' + lab;
  renderMap(key); renderCalendar(); renderCurrent();
}

// ---------- map ----------
function renderMap(key){
  hotLayer.clearLayers();
  const n = SEL.length, sensors = key === 'MODIS' ? 'MODIS Terra+Aqua' : key === 'VIIRS' || key === 'VIIRS_EQ' ? 'VIIRS S-NPP' : 'MODIS Terra+Aqua and VIIRS S-NPP';
  if (n <= 4000) {
    SEL.forEach(i => L.circleMarker([P.lat[i]/1e4, P.lon[i]/1e4], {radius: 3, weight: 0.5, color: '#000', fillColor: P.isV[i] ? '#e66101' : '#5e3c99', fillOpacity: 0.85})
      .bindTooltip(`${P.sat_codes[P.sat[i]]} ${dayToISO(P.day[i])} ${String(P.t[i]).padStart(4,'0')} UTC, FRP ${(P.frp[i]/10).toFixed(1)} MW, conf ${P.conf[i]}`).addTo(hotLayer));
    $('mapNote').innerHTML = `Layer: <b>${n.toLocaleString('en')}</b> individual detections (${sensors}; <span style="color:#5e3c99">● MODIS</span> <span style="color:#e66101">● VIIRS</span>), ${$('d0').value} → ${$('d1').value}, raw (not scaled) detections. Pull ${META.pulled_window}.`;
  } else {
    const G = 0.1, cells = new Map(); let mx = 0;
    SEL.forEach(i => { const k = Math.floor(P.lat[i]/1e4/G) + ',' + Math.floor(P.lon[i]/1e4/G); const c = (cells.get(k) || 0) + 1; cells.set(k, c); if (c > mx) mx = c; });
    cells.forEach((c, k) => { const [a, b] = k.split(',').map(Number);
      L.rectangle([[a*G, b*G], [(a+1)*G, (b+1)*G]], {weight: 0, fillColor: ramp(VIRIDIS, Math.log1p(c)/Math.log1p(mx)), fillOpacity: 0.8})
        .bindTooltip(`${c} detections in 0.1° cell`).addTo(hotLayer); });
    $('mapNote').innerHTML = `Layer: ${n.toLocaleString('en')} detections (${sensors}) aggregated on a 0.1° grid (viridis, log; max ${mx}/cell), ${$('d0').value} → ${$('d1').value}, raw detection counts. Narrow the AOI/date range below 4,000 detections to see points. Pull ${META.pulled_window}.`;
  }
}

// ---------- calendar ----------
function medians(s, key){ // per calendar month, over months with full coverage
  const med = []; for (let m = 0; m < 12; m++) { const v = []; for (let y = Y0; y <= Y1; y++) { const t = mi(y,m); if (covOf(key,t) === 1 && !isNaN(s[t])) v.push(s[t]); }
    v.sort((a,b) => a-b); med.push(v.length ? (v.length % 2 ? v[(v.length-1)/2] : (v[v.length/2-1]+v[v.length/2])/2) : NaN); }
  return med;
}
function renderCalendar(){
  if (!AGG) return;
  const key = AGG.key, s = AGG.s, mode = $('calMode').value, med = medians(s, key);
  const cw = 46, ch = 17, left = 40, top = 20, W = left + 12*cw + 10, H = top + (Y1-Y0+1)*ch + 6;
  let mx = 0; s.forEach(v => { if (!isNaN(v) && v > mx) mx = v; });
  let amx = 0; for (let t = 0; t < NM; t++) if (!isNaN(s[t]) && !isNaN(med[t%12])) amx = Math.max(amx, Math.abs(s[t]-med[t%12]));
  let svg = `<svg width="${W}" height="${H}" role="img" aria-label="Month by year calendar"><defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="#e4e4e4"/><line x1="0" y1="0" x2="0" y2="5" stroke="#aaa" stroke-width="1.5"/></pattern></defs>`;
  MONTHS.forEach((m, i) => svg += `<text x="${left+i*cw+cw/2}" y="${top-6}" text-anchor="middle">${m}</text>`);
  const ym0 = new Date(Date.UTC(2000,0,1)+AGG.d0*864e5), ym1 = new Date(Date.UTC(2000,0,1)+AGG.d1*864e5);
  const t0 = mi(ym0.getUTCFullYear(), ym0.getUTCMonth()), t1 = mi(ym1.getUTCFullYear(), ym1.getUTCMonth());
  for (let y = Y0; y <= Y1; y++) {
    svg += `<text x="${left-4}" y="${top+(y-Y0)*ch+ch-5}" text-anchor="end">${y}</text>`;
    for (let m = 0; m < 12; m++) {
      const t = mi(y,m), v = s[t], cov = covOf(key,t), x = left+m*cw, yy = top+(y-Y0)*ch;
      let fill, tip;
      if (isNaN(v) || cov === 0) { fill = 'url(#hatch)'; tip = `${MONTHS[m]} ${y}: no data from ${key === 'MODIS' ? 'MODIS' : key === 'HARMONIZED' ? 'MODIS or VIIRS' : 'VIIRS S-NPP'} (gap)`; }
      else if (mode === 'count') { fill = v === 0 ? '#f2f2f2' : ramp(VIRIDIS, Math.log1p(v)/Math.log1p(mx)); }
      else { const a = v - med[m]; fill = isNaN(a) ? '#ccc' : ramp(DIVERG, 0.5 + 0.5*Math.sign(a)*Math.sqrt(Math.abs(a)/(amx||1))); }
      if (!tip) tip = `${MONTHS[m]} ${y}: ${fmt(v)} (${SERIES_LABEL[key]})<br>MODIS obs ${fmt(AGG.M[t])}, VIIRS obs ${META.cov.VIIRS[t] > 0 ? fmt(AGG.V[t]) : 'no data'}<br>Median ${MONTHS[m]}: ${fmt(med[m])}, anomaly ${fmt(v-med[m])}${cov < 1 ? `<br>Partial month: ${Math.round(cov*100)}% of days covered` : ''}`;
      const dim = t < t0 || t > t1 ? ' opacity="0.25"' : '';
      svg += `<rect x="${x}" y="${yy}" width="${cw-2}" height="${ch-2}" fill="${fill}"${dim} data-tip="${tip.replace(/"/g,'&quot;')}"/>`;
      if (cov > 0 && cov < 1) svg += `<circle cx="${x+cw-6}" cy="${yy+4}" r="2" fill="#d7191c" pointer-events="none"/>`;
    }
  }
  svg += '</svg>';
  $('cal').innerHTML = svg;
  $('calLabel').textContent = `${mode === 'count' ? 'Monthly hotspot count' : 'Anomaly (count − median of same calendar month, full-coverage years)'} · ${SERIES_LABEL[key]} · ${aoiText()} · data ${key === 'MODIS' ? META.coverage.MODIS.join('→') : key === 'HARMONIZED' ? META.coverage.MODIS[0]+'→'+META.coverage.MODIS[1] : META.coverage.VIIRS.join('→')} · selected range ${$('d0').value} → ${$('d1').value} · pull ${META.pulled_window}`;
  const sw = (stops, n) => Array.from({length: n}, (_, i) => `<span style="display:inline-block;width:18px;height:12px;background:${ramp(stops, i/(n-1))}"></span>`).join('');
  $('calLegend').innerHTML = mode === 'count' ? `0 ${sw(VIRIDIS, 10)} ${fmt(mx)} (log)` : `−${fmt(amx)} ${sw(DIVERG, 9)} +${fmt(amx)}`;
  $('cal').querySelectorAll('rect[data-tip]').forEach(r => { r.onmousemove = e => { const tp = $('tip'); tp.innerHTML = r.dataset.tip; tp.style.display = 'block'; tp.style.left = (e.clientX+12)+'px'; tp.style.top = (e.clientY+12)+'px'; }; r.onmouseleave = () => $('tip').style.display = 'none'; });
}

// ---------- current month vs median ----------
function renderCurrent(){
  const key = AGG.key, today = META.today, cy = +today.slice(0,4), cm = +today.slice(5,7)-1;
  const endM = META.coverage.MODIS[1], endV = META.coverage.VIIRS[1];
  // window = days 1..D of the current month, D = last covered day for the series' data
  const lastDay = key === 'MODIS' || key === 'HARMONIZED' ? endM : endV; const D = lastDay.slice(0,7) === today.slice(0,7) ? +lastDay.slice(8,10) : 31;
  const k = META.harmonization.k_calendar_month[String(cm+1).padStart(2,'0')];
  const byYear = {};
  for (const i of AGG.aoiIdx) { const d = new Date(Date.UTC(2000,0,1)+P.day[i]*864e5); if (d.getUTCMonth() !== cm || d.getUTCDate() > D) continue;
    const y = d.getUTCFullYear(); (byYear[y] = byYear[y] || {M: 0, V: 0})[P.isV[i] ? 'V' : 'M']++; }
  const vals = [];
  for (let y = Y0; y <= cy; y++) {
    const t = mi(y, cm), hasM = META.cov.MODIS[t] === 1 || (y === cy && META.cov.MODIS[t] > 0), hasV = META.cov.VIIRS[t] === 1 || (y === cy && META.cov.VIIRS[t] > 0 && key !== 'HARMONIZED');
    const b = byYear[y] || {M: 0, V: 0}; let v = NaN;
    if (key === 'MODIS' && hasM) v = b.M; else if (key === 'VIIRS' && hasV) v = b.V; else if (key === 'VIIRS_EQ' && hasV) v = k*b.V;
    else if (key === 'HARMONIZED') v = hasM && hasV ? (b.M + k*b.V)/2 : hasM ? b.M : hasV ? k*b.V : NaN;
    if (!isNaN(v)) vals.push([y, v]);
  }
  const cur = vals.find(r => r[0] === cy), hist = vals.filter(r => r[0] < cy).map(r => r[1]).sort((a,b) => a-b);
  const med = hist.length ? (hist.length % 2 ? hist[(hist.length-1)/2] : (hist[hist.length/2-1]+hist[hist.length/2])/2) : NaN;
  const q = p => hist[Math.min(hist.length-1, Math.floor(p*(hist.length-1)))];
  const an = cur ? cur[1] - med : NaN, rank = cur ? hist.filter(v => v > cur[1]).length + 1 : NaN;
  $('current').innerHTML = cur ? `<p class="label">${MONTHS[cm]} ${cy}, days 1–${D} (data to ${lastDay}) · ${SERIES_LABEL[key]} · ${aoiText()} · pull ${META.pulled_window}</p>
    <p><span class="big">${fmt(cur[1])}</span> hotspots vs median <b>${fmt(med)}</b> for ${MONTHS[cm]} 1–${D} in ${vals[0][0]}–${cy-1} (n=${hist.length} years; IQR ${fmt(q(.25))}–${fmt(q(.75))})</p>
    <p>Anomaly: <b class="${an >= 0 ? 'pos' : 'neg'}">${an >= 0 ? '+' : ''}${fmt(an)}</b>${med > 0 ? ` (${an >= 0 ? '+' : ''}${Math.round(an/med*100)}%)` : ''} · rank ${rank} of ${hist.length+1}${key === 'HARMONIZED' ? '<br><small>Current month uses MODIS only (VIIRS S-NPP NRT ends ' + endV + '); history uses MODIS & k·VIIRS mean.</small>' : ''}</p>`
    : `<p>No data for the current month in this series.</p>`;
  // bar chart
  const svg = $('curChart'), W = 460, H = 170, l = 34, b = 22, mx = Math.max(1, ...vals.map(r => r[1]));
  const bw = (W-l-6)/vals.length; let s = `<text x="${l}" y="11" font-size="11">${MONTHS[cm]} 1–${D} hotspots per year (${key === 'HARMONIZED' ? 'harmonized' : key}); red line = median of prior years</text>`;
  vals.forEach(([y, v], i) => { const h = (H-b-18)*v/mx, x = l+i*bw;
    s += `<rect x="${x}" y="${H-b-h}" width="${bw-1}" height="${h}" fill="${y === cy ? '#e66101' : '#3e4989'}"><title>${y}: ${fmt(v)}</title></rect>`;
    if ((y % 5 === 0 && cy - y > 2) || y === cy) s += `<text x="${x+bw/2}" y="${H-b+12}" font-size="9" text-anchor="middle">${y}</text>`; });
  if (!isNaN(med)) { const yy = H-b-(H-b-18)*med/mx; s += `<line x1="${l}" x2="${W-4}" y1="${yy}" y2="${yy}" stroke="#d7191c" stroke-dasharray="4 2"/>`; }
  s += `<text x="${l-4}" y="${H-b}" font-size="9" text-anchor="end">0</text><text x="${l-4}" y="24" font-size="9" text-anchor="end">${fmt(mx)}</text>`;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('aria-label', `Bar chart of ${MONTHS[cm]} hotspot counts by year`); svg.innerHTML = s;
}

// ---------- export ----------
function rowsForExport(){
  return SEL.map(i => ({latitude: P.lat[i]/1e4, longitude: P.lon[i]/1e4, acq_date: dayToISO(P.day[i]), acq_time_utc: String(P.t[i]).padStart(4,'0'),
    satellite: P.sat_codes[P.sat[i]], instrument: P.isV[i] ? 'VIIRS' : 'MODIS', confidence: P.conf[i], frp_mw: P.frp[i]/10, daynight: P.dn[i], firms_dataset: P.dataset_codes[P.ds[i]]}));
}
async function doExport(fmtType){
  const rows = rowsForExport(), key = AGG.key;
  const tag = (aoi ? (aoi.type === 'box' ? `box_${aoi.s}_${aoi.w}_${aoi.n}_${aoi.e}` : 'polygon') : 'france_metro');
  const stamp = new Date().toLocaleString('sv-SE', {timeZone: 'Europe/Paris'}).replace(/[-: ]/g, '') + 'Paris';
  const fname = `hotspots_${tag}_${$('d0').value}_${$('d1').value}_${key}_${stamp}.${fmtType === 'csv' ? 'csv' : 'geojson'}`;
  const prov = {region: aoiText() + ' (within metropolitan France)', date_range: [$('d0').value, $('d1').value], series: SERIES_LABEL[key],
    note: 'Raw FIRMS detections after vegetation/confidence filtering (not scaled). Source: NASA FIRMS. Pull ' + META.pulled_window, exported_at: new Date().toLocaleString('sv-SE', {timeZone: 'Europe/Paris'}) + ' Europe/Paris'};
  let content;
  if (fmtType === 'csv') { const cols = Object.keys(rows[0] || {latitude: 0, longitude: 0, acq_date: 0, acq_time_utc: 0, satellite: 0, instrument: 0, confidence: 0, frp_mw: 0, daynight: 0, firms_dataset: 0});
    content = `# ${JSON.stringify(prov)}\n` + cols.join(',') + '\n' + rows.map(r => cols.map(c => r[c]).join(',')).join('\n') + '\n'; }
  else content = JSON.stringify({type: 'FeatureCollection', metadata: prov, features: rows.map(r => ({type: 'Feature', geometry: {type: 'Point', coordinates: [r.longitude, r.latitude]}, properties: r}))});
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([content], {type: 'text/plain'})); a.download = fname; a.click();
  try { const r = await fetch('api/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: fname, content})});
    const j = await r.json(); $('expMsg').textContent = `Saved ${rows.length.toLocaleString('en')} hotspots → ${j.saved}`; }
  catch (e) { $('expMsg').textContent = `Downloaded ${rows.length} hotspots (server save unavailable)`; }
}

// ---------- method ----------
function renderMethod(){
  const h = META.harmonization, f = h.fits, F = META.filters;
  const rows = Object.entries(f).map(([k, v]) => `<tr><th style="text-align:left">${k}${k === h.chosen ? ' ✔' : ''}</th><td>${v.r2_in_sample}</td><td>${v.r2_leave_one_year_out}</td><td>${v.rmse_loyo}</td><td>${v.r2_log_space_loyo}</td></tr>`).join('');
  const kk = MONTHS.map((_, i) => `<td>${h.k_calendar_month[String(i+1).padStart(2,'0')].toFixed(3)}</td>`).join('');
  const ds = Object.entries(META.datasets).map(([k, v]) => `<tr><th style="text-align:left">${k}</th><td>${v.dates[0]} → ${v.dates[1]}</td><td>${v.rows.toLocaleString('en')}</td><td>${v.pulled_at.replace('T',' ').slice(0,16)}</td></tr>`).join('');
  $('method').innerHTML = `<p><b>Filtering.</b> FIRMS type = 0 (presumed vegetation fire) kept; types 2 (static land source) and 3 (offshore) dropped. NRT feeds carry no type field, so NRT detections within ~3 km (3×3 cells of 0.01°) of any historical type-2/3 detection are dropped. Low confidence removed (MODIS &lt; 30; VIIRS 'l'). Points clipped to the metropolitan France polygon (Natural Earth 10 m, 0.01° buffer). Rows: raw ${F.raw_rows.toLocaleString('en')} → kept ${F.kept_rows.toLocaleString('en')} (outside polygon ${F.dropped_outside_france_polygon}, non-vegetation ${F.dropped_non_vegetation_type}, NRT static mask ${F.nrt_rows_dropped_static_mask}, low confidence ${F.dropped_low_confidence}).</p>
  <p><b>Harmonization.</b> Overlap ${h.overlap[0]} → ${h.overlap[1]} (${h.n_months} complete months, both sensors). MODIS vs VIIRS S-NPP monthly counts: Pearson r = ${h.pearson_r}, Spearman ρ = ${h.spearman_r}. Chosen model: MODIS-equivalent = k<sub>m</sub> × VIIRS count with one ratio-of-sums factor per calendar month (scale-free, so it applies to any AOI). Harmonized series = MODIS observed up to 2012-01; from 2012-02 the mean of MODIS observed and k<sub>m</sub>·VIIRS (MODIS only where VIIRS coverage of the month differs). No gap filling or interpolation.</p>
  <table class="m"><tr><th>model</th><th>R² in-sample</th><th>R² leave-one-year-out</th><th>RMSE LOYO</th><th>R² log-space LOYO</th></tr>${rows}</table>
  <table class="m" style="margin-top:6px"><tr><th>k<sub>m</sub></th>${MONTHS.map(m => `<th>${m}</th>`).join('')}</tr><tr><th>MODIS/VIIRS</th>${kk}</tr></table>
  <p><b>Datasets used</b> (pull times Europe/Paris):</p><table class="m"><tr><th>dataset</th><th>detections kept (dates)</th><th>kept rows</th><th>pulled</th></tr>${ds}</table>
  <p class="note">Full write-up: <a href="METHOD.md">METHOD.md</a>. Data: NASA FIRMS (MODIS C6.1, VIIRS S-NPP 375 m), open data per NASA Earthdata policy.</p>`;
}
