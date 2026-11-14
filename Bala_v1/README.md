# AnuSpace: France burning calendar (NASA Space Apps 2026, MODIS + VIIRS hot spots)

## Rebuild
```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install shapely pandas requests playwright
scripts/fetch_country_csv.sh                 # FIRMS yearly France CSVs (no key needed)
echo FIRMS_MAP_KEY=... > .env                # your free FIRMS key (never commit)
.venv/bin/python scripts/pull_2026_api.py    # 2026 SP+NRT via area API
# Natural Earth boundary: data/raw/boundary/ne_10m_admin_0_countries.geojson (URL in data/sources.json)
.venv/bin/python scripts/process.py          # -> build/data/*.json, data/processed/*.csv
.venv/bin/python scripts/serve.py 8792       # http://127.0.0.1:8792/  (POST /api/export saves into exports/)
.venv/bin/python scripts/screenshot.py http://127.0.0.1:8792/
```
Note: `data/sources.json` is written by an inline script in the first pull. Regenerate it when you re-pull.
See METHOD.md for the method. Data: NASA FIRMS (open data). Basemap © OpenStreetMap contributors. Leaflet and Leaflet.draw are vendored in build/vendor.
