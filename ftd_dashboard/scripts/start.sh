#!/bin/sh
# Start the dashboard for Render (or any host that sets $PORT).
# Downloads the SQLite database from FIREVERIFY_DB_URL when the file is missing.
# Writes during this process lifetime are kept on the container disk and are
# lost on every restart or free-tier spin-down.
set -eu

DB="${FIREVERIFY_DB:-/app/data/fireverify.db}"
mkdir -p "$(dirname "$DB")"

unpack_or_move() {
  src="$1"
  dest="$2"
  case "$src" in
    *.gz) gzip -dc "$src" > "$dest" ;;
    *) cp "$src" "$dest" ;;
  esac
}

if [ ! -s "$DB" ]; then
  if [ -n "${FIREVERIFY_DB_URL:-}" ]; then
    echo "Downloading database from FIREVERIFY_DB_URL..."
    tmp="${DB}.download"
    curl -fL --retry 3 -o "$tmp" "$FIREVERIFY_DB_URL"
    case "$FIREVERIFY_DB_URL" in
      *.gz) gzip -dc "$tmp" > "$DB" && rm -f "$tmp" ;;
      *) mv "$tmp" "$DB" ;;
    esac
    echo "Database ready at $DB ($(wc -c < "$DB") bytes)"
  elif [ -f /opt/fireverify-slim.db.gz ]; then
    echo "Unpacking baked database..."
    gzip -dc /opt/fireverify-slim.db.gz > "$DB"
  else
    echo "WARNING: $DB not found and FIREVERIFY_DB_URL is unset; starting with an empty database" >&2
  fi
fi

if [ -z "${FTD_ADMIN_TOKEN:-}" ]; then
  echo "FTD_ADMIN_TOKEN is required in production (protects POST /api/ingest, backfill, quarantine)" >&2
  exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
exec ftd dashboard --host "$HOST" --port "$PORT"
