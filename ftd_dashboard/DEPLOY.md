# Render free deployment notes

Prepared 15 November 2026 (Europe/Paris). Nothing in this folder has been pushed, deployed, or signed up for.

## What changed in this copy (vs the Mac working tree)

- Dashboard binds on `0.0.0.0` and `$PORT` when `PORT` is set (Render does this). `--host` / `--port` still work. Binding a public interface without `FTD_ADMIN_TOKEN` is refused.
- All POST routes require `FTD_ADMIN_TOKEN` when it is set: `Authorization: Bearer <token>`, HTTP Basic (password = token), or `X-Admin-Token`. GET stays public, including `/` and `/api/*`.
- Same-origin check is unchanged in spirit (Origin host must match the `Host` header). Hostnames are compared case-insensitively with default ports 80/443 stripped, so `https://nasa-harmonized-hotspots.nimblesoft.ch` and `https://<service>.onrender.com` both work when the visitor is on that host.
- The ingest dialog has a password field. The frontend stores the token in `sessionStorage` and prompts if a POST returns 401 (covers ingest, calendar backfill, and quarantine release/reject).
- `GET /health` and `GET /api/health` return `{"ok": true}` for Render health checks.
- `Dockerfile`, `render.yaml`, and `scripts/start.sh` added.

## Database plan

Source on the Mac: `data/fireverify.db` was 389–402 MB on disk (including WAL). A compact copy was made with `VACUUM INTO` **without modifying the Mac database**.

| Copy | Uncompressed | gzip -9 |
| --- | ---: | ---: |
| Full slim (live + simulated kept) | 402,509,824 bytes (383.86 MiB) | 54,428,278 bytes (51.91 MiB) |
| Optional: drop `simulated_records` | 316,366,848 bytes (301.71 MiB) | 41,614,920 bytes (39.69 MiB) |

**Keep `simulated_records`.** The UI defaults to the Simulated lane, calendar backfill can generate a simulated regime, and the existing 73,175 simulated rows are the demo data for that tab. Dropping the table would empty Simulated until someone with the password re-runs ingest. The 80 MiB saving is not worth breaking the default demo.

Tables kept: live_records (264,133), simulated_records (73,175), calendar_coverage (13,937), fetch_log, ingest_runs, derived_records, quarantine, rejected, verdicts.

The gzipped slim file is on the box at `_artifacts/fireverify-slim.db.gz` (do **not** commit it as a necessity; it is a local artifact). The Mac also has informational copies in `/tmp` and a transfer folder on the Desktop that should be deleted after this work.

### How Render gets the database

GitHub rejects files over 100 MB, so the uncompressed 384 MiB DB cannot go in the repo. The gzipped 52 MiB file *could* fit, but a binary that large is a poor git citizen.

Recommended:

1. After Praveen pushes the code (on the user's go), create a GitHub **release** (or use a private Drive/S3 URL) and attach `fireverify-slim.db.gz`. Release assets may be up to 2 GB.
2. Set Render env `FIREVERIFY_DB_URL` to the asset's direct download URL (a private repo needs a tokenized URL or a public release).
3. `scripts/start.sh` downloads and gunzips the file to `$FIREVERIFY_DB` (`/app/data/fireverify.db`) when that path is missing.

Alternatively, bake the `.gz` into the Docker image at build time (`COPY` or `curl` during `docker build`) so every cold start does not re-download 52 MiB. The start script already unpacks `/opt/fireverify-slim.db.gz` if present.

`data/external/burning_activity_calendar` (29 MB, largest file 14 MB) can be committed; the image copies it. Country profiles need it.

### Ephemeral disk (free tier)

Render free has **no persistent disk**. Ingestion, backfill, quarantine decisions, and even GET `/api/summary` / `/api/events` (they call `_ensure_verdicts`, which **writes** missing verdicts) only last until the instance restarts, is redeployed, or spins down after ~15 minutes idle. The next boot unpacks the same baked/downloaded DB again.

### Memory

The free plan is 512 MB RAM / 0.1 CPU. SQLite does not load the whole 384 MiB file into RAM, but a heavy calendar query plus Python could get tight. If the service is OOM-killed, options are dropping simulated data, pre-aggregating further, or a paid instance.

## Do not commit

- `.env`, `keys.txt`, `.claude/`, `ser.bundle`, `burning_activity_calendar_handoff.zip`
- `data/fireverify.db` and WAL/SHM files
- `_artifacts/` (local slim DB gzip)
- Real values of `FTD_ADMIN_TOKEN`, `FIRMS_MAP_KEY`, `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET`, and any tokenized `FIREVERIFY_DB_URL`

Note: the **local Mac repo already tracks `keys.txt`**. Remove it from the tree (and prefer rewriting history, or at least rotate the keys) before the first push to a public or team remote.

## Env vars (values only in Render secrets)

| Variable | Role |
| --- | --- |
| `FTD_ADMIN_TOKEN` | Required. Password for all POST routes. |
| `FIRMS_MAP_KEY` | NASA FIRMS (live ingest, archive backfill, active-areas). |
| `CDSE_CLIENT_ID` / `CDSE_CLIENT_SECRET` | Copernicus Sentinel enrichment. |
| `FIREVERIFY_DB_URL` | URL of the slim DB or `.db.gz`. |
| `FIREVERIFY_DB` | Path, default `/app/data/fireverify.db`. |
| `FIREVERIFY_LIKELIHOODS` | Optional likelihood table path. |
| `FIREVERIFY_CONTEXT_PATHS` | Optional extra GeoJSON layers. |
| `FIREVERIFY_OVERPASS` | `1` or `0`. |
| `FIREVERIFY_COUNTRY_ARCHIVE` | Optional; default `data/external/burning_activity_calendar`. |
| `HOST` | `0.0.0.0`. |
| `PORT` | Set by Render. |

## Start command

Docker: image `CMD` is `/app/scripts/start.sh`.

Native Render Python: build `pip install .`, start `./scripts/start.sh`.

Health path: `/health`.

## Caveat

`GET /api/active-areas` stays public (per the GET-is-public rule) but it calls FIRMS with the map key. A scraper could burn the daily quota. Consider rate-limiting later if that happens.
