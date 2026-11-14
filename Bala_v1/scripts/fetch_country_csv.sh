#!/usr/bin/env bash
# Download FIRMS yearly per-country CSVs (France) for MODIS and VIIRS S-NPP into data/raw/country/
set -euo pipefail; cd "$(dirname "$0")/../data/raw" && mkdir -p country && cd country
for s in modis viirs-snpp; do for y in $(seq 2000 2026); do f=${s}_${y}_France.csv
  c=$(curl -s -o "$f" -w '%{http_code}' -m 120 "https://firms.modaps.eosdis.nasa.gov/data/country/$s/$y/$f")
  [ "$c" = 200 ] || { rm -f "$f"; echo "not available: $s $y ($c)"; }; done; done
