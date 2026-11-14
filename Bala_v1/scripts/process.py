"""Build harmonized France (metropolitan) hotspot data for the web app.
Inputs: data/raw/country/*.csv, data/raw/api/*.csv, data/raw/boundary/ne_10m_admin_0_countries.geojson
Outputs: build/data/points.json, build/data/monthly.json, build/data/meta.json, data/processed/*.csv"""
import json, glob, os, pathlib, datetime as dt, numpy as np, pandas as pd, shapely
from shapely.geometry import shape, box
ROOT = pathlib.Path(__file__).resolve().parents[1]; RAW = ROOT/'data/raw'
OUT = ROOT/'build/data'; OUT.mkdir(parents=True, exist_ok=True); PROC = ROOT/'data/processed'; PROC.mkdir(exist_ok=True)
src = json.load(open(ROOT/'data/sources.json'))
pulled = {os.path.basename(e['file']): e['pulled_at'] for e in src['sources'] if 'file' in e}

# --- France metropolitan polygon (Natural Earth 10m, parts intersecting metro bbox, +0.01 deg coastal buffer)
gj = json.load(open(RAW/'boundary/ne_10m_admin_0_countries.geojson'))
fr = shape(next(f for f in gj['features'] if f['properties']['ADMIN'] == 'France')['geometry'])
METRO_BBOX = box(-5.5, 41.2, 9.8, 51.2)
metro = shapely.unary_union([p for p in fr.geoms if p.intersects(METRO_BBOX)]).buffer(0.01)
shapely.prepare(metro)
json.dump(shapely.geometry.mapping(metro.simplify(0.02)), open(OUT/'france_metro.geojson', 'w'))

def load(f, dataset):
    d = pd.read_csv(f, dtype={'acq_time': str})
    sensor = 'VIIRS' if 'bright_ti4' in d else 'MODIS'
    d = pd.DataFrame(dict(latitude=d.latitude, longitude=d.longitude, acq_date=d.acq_date, acq_time=d.acq_time.str.zfill(4),
        satellite=d.satellite.astype(str), sensor=sensor, confidence=d.confidence.astype(str), frp=d.frp, daynight=d.daynight,
        type=d['type'] if 'type' in d else np.nan, version=d.version.astype(str), dataset=dataset, raw_file=os.path.basename(f)))
    return d
frames = []
for f in sorted(glob.glob(str(RAW/'country/*.csv'))):
    s = os.path.basename(f).split('_')[0]
    frames.append(load(f, {'modis': 'MODIS_SP (FIRMS country archive)', 'viirs-snpp': 'VIIRS_SNPP_SP (FIRMS country archive)'}[s]))
for f in sorted(glob.glob(str(RAW/'api/*.csv'))):
    frames.append(load(f, os.path.basename(f).replace('_2026_bbox_FR.csv', '')))
a = pd.concat(frames, ignore_index=True)
log = {'raw_rows': int(len(a))}
a = a[a.acq_date >= '2000-11-01']
inside = shapely.contains_xy(metro, a.longitude.values, a.latitude.values)
log['dropped_outside_france_polygon'] = int((~inside).sum()); a = a[inside].copy()
a['satellite'] = a.satellite.replace({'N': 'Suomi NPP', 'T': 'Terra', 'A': 'Aqua'})

# --- static/industrial mask for NRT rows (no 'type' field): 0.01-deg cells (3x3 neighbourhood) with any SP type 2/3 detection
CELL = 0.01
def cells(df): return np.floor(df.latitude/CELL).astype(int), np.floor(df.longitude/CELL).astype(int)
st = a[a['type'].isin([2, 3])]; sy, sx = cells(st)
static = {(y+dy, x+dx) for y, x in zip(sy, sx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)}
nrt = a['type'].isna(); ny, nx = cells(a[nrt])
in_static = pd.Series(False, index=a.index); in_static[a.index[nrt]] = [(y, x) in static for y, x in zip(ny, nx)]
log['nrt_rows_dropped_static_mask'] = int(in_static.sum())
# --- vegetation-fire + confidence filter
is_veg = (a['type'] == 0) | (nrt & ~in_static)
conf_ok = np.where(a.sensor == 'MODIS', pd.to_numeric(a.confidence, errors='coerce') >= 30, a.confidence.isin(['n', 'h']))
log['dropped_non_vegetation_type'] = int((~is_veg & ~in_static).sum())
log['dropped_low_confidence'] = int((is_veg & ~conf_ok).sum())
a = a[is_veg & conf_ok].copy(); log['kept_rows'] = int(len(a))
log['kept_by_sensor'] = a.sensor.value_counts().to_dict()
a['month'] = a.acq_date.str[:7]

# --- coverage (from dataset availability, NOT from detections) ---
TODAY = '2026-11-14'
COV = {'MODIS': ('2000-11-01', TODAY), 'VIIRS': ('2012-01-20', '2026-11-02')}
months = pd.period_range('2000-01', '2026-11', freq='M').astype(str)
def cov_frac(sensor, m):
    s, e = COV[sensor]; p = pd.Period(m, 'M'); ms, me = p.start_time.date(), p.end_time.date()
    s, e = max(ms, dt.date.fromisoformat(s)), min(me, dt.date.fromisoformat(e))
    return max(0, (e - s).days + 1) / ((me - ms).days + 1)
cnt = a.groupby(['sensor', 'month']).size().unstack(0).reindex(months).fillna(0)
mon = pd.DataFrame(index=months)
for s in ('MODIS', 'VIIRS'):
    mon[s+'_cov'] = [cov_frac(s, m) for m in months]
    mon[s] = np.where(mon[s+'_cov'] > 0, cnt[s], np.nan)

# --- harmonization fit: overlap of complete months for both sensors ---
ov = mon[(mon.MODIS_cov == 1) & (mon.VIIRS_cov == 1)].copy(); ov['cal'] = ov.index.str[5:7]; ov['year'] = ov.index.str[:4]
def r2(y, p): return float(1 - ((y-p)**2).sum() / ((y-y.mean())**2).sum())
def fit_global(d): k = d.MODIS.sum()/d.VIIRS.sum(); return lambda x: k * x.VIIRS
def fit_month(d):
    k = d.groupby('cal').MODIS.sum()/d.groupby('cal').VIIRS.sum(); return lambda x: x.cal.map(k) * x.VIIRS
def fit_loglog(d):
    b, c = np.polyfit(np.log1p(d.VIIRS), np.log1p(d.MODIS), 1); return lambda x: np.expm1(c + b*np.log1p(x.VIIRS))
models = {'A_global_ratio': fit_global, 'B_calendar_month_ratio': fit_month, 'C_loglog_ols': fit_loglog}
fits = {}
for name, fn in models.items():
    p = fn(ov)(ov)
    cv = pd.concat([fn(ov[ov.year != y])(ov[ov.year == y]) for y in ov.year.unique()]).reindex(ov.index)
    fits[name] = dict(r2_in_sample=round(r2(ov.MODIS, p), 3), r2_leave_one_year_out=round(r2(ov.MODIS, cv), 3),
                      rmse_loyo=round(float(np.sqrt(((ov.MODIS-cv)**2).mean())), 1),
                      r2_log_space_loyo=round(r2(np.log1p(ov.MODIS), np.log1p(cv.clip(lower=0))), 3))
k_glob = float(ov.MODIS.sum()/ov.VIIRS.sum())
k_cal = (ov.groupby('cal').MODIS.sum()/ov.groupby('cal').VIIRS.sum()).round(4)
b, c = np.polyfit(np.log1p(ov.VIIRS), np.log1p(ov.MODIS), 1)
pear = float(np.corrcoef(ov.MODIS, ov.VIIRS)[0, 1]); spear = float(ov.MODIS.rank().corr(ov.VIIRS.rank()))
chosen = 'B_calendar_month_ratio'
# MODIS-equivalent and harmonized series (counts per month; scaled for partial coverage NOT applied: gaps/partials flagged)
mon['cal'] = mon.index.str[5:7]
mon['VIIRS_modis_eq'] = mon.VIIRS * mon.cal.map(k_cal)
both = mon[['MODIS', 'VIIRS_modis_eq']]
mon['HARMONIZED'] = both.mean(axis=1, skipna=True)  # mean of available; NaN if neither
# months where coverage differs (e.g. Nov 2026: MODIS to 14th, VIIRS to 2nd) -> use MODIS only to avoid mixing unequal windows
diffcov = (mon.MODIS_cov != mon.VIIRS_cov) & mon.MODIS_cov.gt(0) & mon.VIIRS_cov.gt(0)
mon.loc[diffcov, 'HARMONIZED'] = mon.loc[diffcov, 'MODIS']
mon['HARM_cov'] = np.maximum(mon.MODIS_cov, mon.VIIRS_cov)
mon.round(2).to_csv(PROC/'monthly_france_metro.csv', index_label='month')
ov.to_csv(PROC/'overlap_fit_months.csv', index_label='month')

def top(series, n=3):
    s = series.dropna().sort_values(ascending=False).head(n); return [[i, round(float(v), 1)] for i, v in s.items()]
yr = mon.copy(); yr['y'] = yr.index.str[:4]
yrs = yr.groupby('y')[['MODIS', 'VIIRS', 'HARMONIZED']].sum(min_count=1)
tops = {k: dict(months=top(mon[k]), years=top(yrs[k])) for k in ('MODIS', 'VIIRS', 'HARMONIZED')}
yrs.round(1).to_csv(PROC/'yearly_france_metro.csv', index_label='year')

# --- client point table (columnar) ---
EPOCH = dt.date(2000, 1, 1)
a = a.sort_values(['acq_date', 'acq_time'])
day = (pd.to_datetime(a.acq_date) - pd.Timestamp(EPOCH)).dt.days
sat_codes = ['Terra', 'Aqua', 'Suomi NPP']; ds_codes = sorted(a.dataset.unique())
pts = dict(epoch=str(EPOCH), sat_codes=sat_codes, dataset_codes=ds_codes,
    lat=(a.latitude*1e4).round().astype(int).tolist(), lon=(a.longitude*1e4).round().astype(int).tolist(),
    day=day.tolist(), t=a.acq_time.astype(int).tolist(), sat=a.satellite.map({s: i for i, s in enumerate(sat_codes)}).astype(int).tolist(),
    conf=a.confidence.tolist(), frp=(a.frp*10).round().astype(int).tolist(), dn=a.daynight.tolist(),
    ds=a.dataset.map({s: i for i, s in enumerate(ds_codes)}).astype(int).tolist())
json.dump(pts, open(OUT/'points.json', 'w'), separators=(',', ':'))
ds_pull = {}
for e in src['sources']:
    if 'file' not in e or 'boundary' in e['file']: continue
    key = e['dataset_id'] if 'country' not in e['file'] else ('MODIS_SP (FIRMS country archive)' if 'modis' in e['file'] else 'VIIRS_SNPP_SP (FIRMS country archive)')
    ds_pull.setdefault(key, []).append(e['pulled_at'])
dsinfo = {k: dict(dates=[str(a[a.dataset == k].acq_date.min()), str(a[a.dataset == k].acq_date.max())], rows=int((a.dataset == k).sum()),
                  pulled_at=max(v)) for k, v in ds_pull.items()}
meta = dict(region='Metropolitan France (incl. Corsica); Natural Earth 10m polygon + 0.01° buffer', built_at=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
    pulled_window='2026-11-14 12:02–12:05 Europe/Paris', coverage=COV, today=TODAY, datasets=dsinfo, filters=log,
    harmonization=dict(chosen=chosen, overlap=[ov.index.min(), ov.index.max()], n_months=int(len(ov)), k_global=round(k_glob, 4),
        k_calendar_month=k_cal.to_dict(), loglog=dict(slope=round(float(b), 4), intercept=round(float(c), 4)),
        pearson_r=round(pear, 3), spearman_r=round(spear, 3), fits=fits), tops=tops)
json.dump(meta, open(OUT/'meta.json', 'w'), indent=1)
mj = mon.reset_index().rename(columns={'index': 'month'}).replace({np.nan: None})
json.dump(mj[['month', 'MODIS', 'VIIRS', 'VIIRS_modis_eq', 'HARMONIZED', 'MODIS_cov', 'VIIRS_cov', 'HARM_cov']].to_dict('records'), open(OUT/'monthly.json', 'w'))
print(json.dumps(meta, indent=1, default=str))
print('points bytes', os.path.getsize(OUT/'points.json'))
