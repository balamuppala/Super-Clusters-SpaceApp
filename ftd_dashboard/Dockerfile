FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY fireverify ./fireverify
COPY data/external ./data/external
COPY scripts/start.sh ./scripts/start.sh

RUN pip install --no-cache-dir . \
    && chmod +x /app/scripts/start.sh

# Optional: COPY _artifacts/fireverify-slim.db.gz /opt/fireverify-slim.db.gz
# Prefer FIREVERIFY_DB_URL (a GitHub release asset) so the 52 MB file is not in git.

ENV HOST=0.0.0.0
ENV FIREVERIFY_DB=/app/data/fireverify.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["/app/scripts/start.sh"]
