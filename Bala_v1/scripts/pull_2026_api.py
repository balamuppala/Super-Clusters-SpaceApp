"""Pull 2026 FIRMS hotspots via the area API (5-day chunks) for the metropolitan-France bbox.
Key read from ../.env (FIRMS_MAP_KEY); never printed or written to disk."""
import os, time, datetime as dt, io, requests, pandas as pd, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
key = [l.split('=',1)[1].strip().strip('"\'') for l in open(ROOT/'.env') if l.startswith('FIRMS_MAP_KEY')][0]
BBOX = "-5.5,41.2,9.8,51.2"
JOBS = [("MODIS_SP","2026-01-01","2026-08-31"),("MODIS_NRT","2026-09-01","2026-11-14"),
        ("VIIRS_SNPP_SP","2026-01-01","2026-07-31"),("VIIRS_SNPP_NRT","2026-08-01","2026-11-14")]
out = ROOT/'data/raw/api'; out.mkdir(parents=True, exist_ok=True)
for ds, a, b in JOBS:
    d = dt.date.fromisoformat(a); end = dt.date.fromisoformat(b); frames=[]
    while d <= end:
        n = min(5, (end-d).days+1)
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{ds}/{BBOX}/{n}/{d}"
        for attempt in range(4):
            r = requests.get(url, timeout=120)
            if r.ok and r.text.startswith('latitude'): break
            time.sleep(5*(attempt+1))
        else:
            raise SystemExit(f"FAILED {ds} {d}: HTTP {r.status_code} {r.text[:100].replace(key,'<KEY>')}")
        df = pd.read_csv(io.StringIO(r.text)); frames.append(df)
        d += dt.timedelta(days=n); time.sleep(0.3)
    df = pd.concat(frames, ignore_index=True)
    df.to_csv(out/f"{ds}_2026_bbox_FR.csv", index=False)
    print(ds, len(df), df.acq_date.min() if len(df) else None, df.acq_date.max() if len(df) else None)
