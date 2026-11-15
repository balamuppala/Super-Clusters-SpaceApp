"""SQLite storage with physically separate tables per lane.

live_records / simulated_records  accepted observations, one table per origin lane
derived_records                   features computed from accepted records (tagged with origin lane)
quarantine                        suspicious records awaiting review (release / reject)
rejected                          unusable records, kept with raw payload for audit
ingest_runs, fetch_log            run audit trail and AOI-source refresh cache
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from fireverify.models import (
    BBox, DerivedFeature, IngestedRecord, Lane, RawRecord, RecordKind, stable_hash, utcnow,
)

ORIGIN_TABLES = {Lane.LIVE: "live_records", Lane.SIMULATED: "simulated_records"}

_RECORD_COLUMNS = """
    record_id TEXT PRIMARY KEY,
    raw_hash TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    parser TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    observed_at TEXT,
    target_id TEXT,
    data TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    flags TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS live_records ({_RECORD_COLUMNS});
CREATE TABLE IF NOT EXISTS simulated_records ({_RECORD_COLUMNS}, sim_truth TEXT);
CREATE INDEX IF NOT EXISTS ix_live_kind_time ON live_records(kind, observed_at);
CREATE INDEX IF NOT EXISTS ix_live_geo ON live_records(lat, lon);
CREATE INDEX IF NOT EXISTS ix_live_target ON live_records(target_id);
CREATE INDEX IF NOT EXISTS ix_sim_kind_time ON simulated_records(kind, observed_at);
CREATE INDEX IF NOT EXISTS ix_sim_geo ON simulated_records(lat, lon);
CREATE INDEX IF NOT EXISTS ix_sim_target ON simulated_records(target_id);

CREATE TABLE IF NOT EXISTS derived_records (
    derived_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    origin_lane TEXT NOT NULL CHECK (origin_lane IN ('live', 'simulated')),
    cluster_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    value TEXT NOT NULL,
    inputs TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_derived_cluster ON derived_records(origin_lane, cluster_id, feature, computed_at);

CREATE TABLE IF NOT EXISTS quarantine (
    qid TEXT PRIMARY KEY,
    raw_hash TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    origin_lane TEXT NOT NULL,
    source TEXT NOT NULL,
    parser TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT,
    reasons TEXT NOT NULL,
    details TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    sim_truth TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'released', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_quarantine_status ON quarantine(status, created_at);

CREATE TABLE IF NOT EXISTS rejected (
    rid TEXT PRIMARY KEY,
    raw_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    origin_lane TEXT NOT NULL,
    source TEXT NOT NULL,
    parser TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT,
    reasons TEXT NOT NULL,
    details TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    sim_truth TEXT,
    rejected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rejected_time ON rejected(rejected_at);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    params TEXT NOT NULL,
    stats TEXT,
    source_status TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id TEXT PRIMARY KEY,
    origin_lane TEXT NOT NULL CHECK (origin_lane IN ('live', 'simulated')),
    cluster_id TEXT NOT NULL,
    probability REAL NOT NULL,
    label TEXT NOT NULL,
    attribution TEXT,
    model_version TEXT NOT NULL,
    table_version TEXT NOT NULL,
    table_hash TEXT NOT NULL,
    verdict TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verdicts_cluster ON verdicts(origin_lane, cluster_id, computed_at);

CREATE TABLE IF NOT EXISTS calendar_coverage (
    lane TEXT NOT NULL CHECK (lane IN ('live', 'simulated')),
    family TEXT NOT NULL,
    day TEXT NOT NULL,
    area TEXT NOT NULL,
    west REAL NOT NULL,
    south REAL NOT NULL,
    east REAL NOT NULL,
    north REAL NOT NULL,
    source TEXT,
    status TEXT NOT NULL,
    run_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (lane, family, day, area)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    lane TEXT NOT NULL,
    source TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (lane, source, scope_key)
);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60)   # a long backfill may hold the write lock briefly
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; bulk backfills commit per row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- runs / cache

    def start_run(self, run_id: str, mode: str, params: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO ingest_runs(run_id, mode, started_at, params) VALUES (?,?,?,?)",
                              (run_id, mode, _ts(utcnow()), _dumps(params)))

    def finish_run(self, run_id: str, stats: dict[str, Any], source_status: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("UPDATE ingest_runs SET finished_at=?, stats=?, source_status=? WHERE run_id=?",
                              (_ts(utcnow()), _dumps(stats), _dumps(source_status), run_id))

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "params": json.loads(r["params"]), "stats": json.loads(r["stats"] or "{}"),
                 "source_status": json.loads(r["source_status"] or "{}")} for r in rows]

    def last_fetch(self, lane: Lane, source: str, scope_key: str) -> datetime | None:
        row = self.conn.execute("SELECT fetched_at FROM fetch_log WHERE lane=? AND source=? AND scope_key=?",
                                (lane.value, source, scope_key)).fetchone()
        return _parse_ts(row["fetched_at"]) if row else None

    def mark_fetched(self, lane: Lane, source: str, scope_key: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
                              (lane.value, source, scope_key, _ts(utcnow())))

    # ------------------------------------------------------- origin lanes

    def insert_record(self, rec: IngestedRecord, run_id: str) -> bool:
        """Insert into the record's origin lane. Returns False if already ingested."""
        raw = rec.raw
        table = ORIGIN_TABLES[raw.lane]
        cols = ["record_id", "raw_hash", "run_id", "kind", "source", "parser", "lat", "lon", "observed_at",
                "target_id", "data", "raw_payload", "flags", "fetched_at", "ingested_at"]
        vals = [rec.record_id, raw.raw_hash, run_id, raw.kind.value, raw.source, raw.parser, rec.latitude,
                rec.longitude, _ts(rec.observed_at), raw.target_id, rec.model.model_dump_json(),
                _dumps(raw.payload), _dumps(rec.flags), _ts(raw.fetched_at), _ts(utcnow())]
        if raw.lane is Lane.SIMULATED:
            cols.append("sim_truth")
            vals.append(_dumps(raw.sim_truth) if raw.sim_truth is not None else None)
        with self.conn:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return cur.rowcount == 1

    def query_records(self, lane: Lane, kinds: Iterable[RecordKind] | None = None, bbox: BBox | None = None,
                      start: datetime | None = None, end: datetime | None = None,
                      target_id: str | None = None, ids: Iterable[str] | None = None,
                      limit: int | None = None) -> list[dict[str, Any]]:
        table = ORIGIN_TABLES[lane]
        where, args = [], []
        if kinds:
            kinds = list(kinds)
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += [k.value for k in kinds]
        if bbox:
            where.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            args += [bbox.south, bbox.north, bbox.west, bbox.east]
        if start:
            where.append("observed_at >= ?")
            args.append(_ts(start))
        if end:
            where.append("observed_at <= ?")
            args.append(_ts(end))
        if target_id:
            where.append("target_id = ?")
            args.append(target_id)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            where.append(f"record_id IN ({','.join('?' * len(ids))})")
            args += ids
        sql = f"SELECT * FROM {table}" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY observed_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._record_row(r) for r in self.conn.execute(sql, args).fetchall()]

    @staticmethod
    def _record_row(r: sqlite3.Row) -> dict[str, Any]:
        out = dict(r)
        out["data"] = json.loads(out["data"])
        out["raw_payload"] = json.loads(out["raw_payload"])
        out["flags"] = json.loads(out["flags"])
        out["observed_at"] = _parse_ts(out["observed_at"])
        if out.get("sim_truth"):
            out["sim_truth"] = json.loads(out["sim_truth"])
        return out

    def hotspot_points(self, lane: Lane, bbox: BBox, start: datetime, end: datetime) -> list[tuple]:
        """Lean read of every accepted VIIRS/MODIS detection (NRT or archive) in an area and time range.

        Returns (lat, lon, observed_at, source, sensor, platform, frp_mw, confidence_class) tuples.
        """
        table = ORIGIN_TABLES[lane]
        rows = self.conn.execute(
            f"""SELECT lat, lon, observed_at, source, json_extract(data, '$.sensor'), json_extract(data, '$.platform'),
                       json_extract(data, '$.frp_mw'), json_extract(data, '$.confidence_class')
                FROM {table} WHERE kind IN ('hotspot', 'historical_hotspot')
                  AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND observed_at >= ? AND observed_at < ?""",
            (bbox.south, bbox.north, bbox.west, bbox.east, _ts(start), _ts(end))).fetchall()
        return [tuple(r) for r in rows]

    # ---------------------------------------------------- calendar coverage

    def mark_coverage(self, lane: Lane, family: str, bbox: BBox, days: Iterable[Any], status: str,
                      source: str | None, run_id: str | None) -> None:
        """Record that a sensor family's archive was (or could not be) fetched for these days and area."""
        now = _ts(utcnow())
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO calendar_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(lane.value, family, str(d), bbox.key(), bbox.west, bbox.south, bbox.east, bbox.north,
                  source, status, run_id, now) for d in days])

    def coverage_for(self, lane: Lane, bbox: BBox, start: Any, end: Any) -> dict[str, dict[str, str]]:
        """family -> day -> best status among fetched areas that contain ``bbox`` ('ok' beats anything else)."""
        eps = 1e-6
        rows = self.conn.execute(
            """SELECT family, day, status FROM calendar_coverage
               WHERE lane=? AND day BETWEEN ? AND ? AND west<=? AND south<=? AND east>=? AND north>=?""",
            (lane.value, str(start), str(end), bbox.west + eps, bbox.south + eps, bbox.east - eps,
             bbox.north - eps)).fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            fam = out.setdefault(r["family"], {})
            if fam.get(r["day"]) != "ok":
                fam[r["day"]] = r["status"]
        return out

    def calendar_areas(self, lane: Lane) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT area, west, south, east, north, MIN(day) first_day, MAX(day) last_day,
                      COUNT(DISTINCT day) n_days, MAX(updated_at) updated_at
               FROM calendar_coverage WHERE lane=? AND status='ok'
               GROUP BY area ORDER BY updated_at DESC""", (lane.value,)).fetchall()
        return [{"bbox": [r["west"], r["south"], r["east"], r["north"]], "first_day": r["first_day"],
                 "last_day": r["last_day"], "days": r["n_days"], "updated_at": r["updated_at"]} for r in rows]

    # ------------------------------------------------------------ derived

    def insert_derived(self, feat: DerivedFeature, run_id: str) -> bool:
        if feat.origin_lane not in ORIGIN_TABLES:
            raise ValueError("derived features must trace back to the live or simulated lane")
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO derived_records VALUES (?,?,?,?,?,?,?,?,?)",
                (feat.derived_id, run_id, feat.origin_lane.value, feat.cluster_id, feat.feature,
                 feat.algorithm_version, _dumps(feat.value), _dumps(feat.inputs), _ts(feat.computed_at)))
        return cur.rowcount == 1

    def latest_derived(self, lane: Lane, cluster_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM derived_records WHERE origin_lane=? AND cluster_id=? ORDER BY computed_at",
            (lane.value, cluster_id)).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:  # later rows overwrite earlier ones
            out[r["feature"]] = {"value": json.loads(r["value"]), "inputs": json.loads(r["inputs"]),
                                 "computed_at": r["computed_at"], "algorithm_version": r["algorithm_version"],
                                 "derived_id": r["derived_id"]}
        return out

    def list_clusters(self, lane: Lane, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT cluster_id, value, MAX(computed_at) AS computed_at FROM derived_records
               WHERE origin_lane=? AND feature='cluster_summary' GROUP BY cluster_id
               ORDER BY computed_at DESC LIMIT ?""", (lane.value, limit)).fetchall()
        return [{"cluster_id": r["cluster_id"], "computed_at": r["computed_at"], **json.loads(r["value"])}
                for r in rows]

    # --------------------------------------------------- quarantine / rejected

    def quarantine(self, raw: RawRecord, codes: list[str], details: list[str], run_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO quarantine(qid, raw_hash, run_id, origin_lane, source, parser, kind,
                   target_id, reasons, details, raw_payload, sim_truth, fetched_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("q_" + raw.raw_hash[:16], raw.raw_hash, run_id, raw.lane.value, raw.source, raw.parser,
                 raw.kind.value, raw.target_id, _dumps(codes), _dumps(details), _dumps(raw.payload),
                 _dumps(raw.sim_truth) if raw.sim_truth is not None else None, _ts(raw.fetched_at), _ts(utcnow())))
        return cur.rowcount == 1

    def reject(self, raw: RawRecord, codes: list[str], details: list[str], run_id: str) -> bool:
        rid = "r_" + stable_hash(raw.raw_hash, codes, raw.sim_truth, length=16)
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO rejected VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, raw.raw_hash, run_id, raw.lane.value, raw.source, raw.parser, raw.kind.value,
                 raw.target_id, _dumps(codes), _dumps(details), _dumps(raw.payload),
                 _dumps(raw.sim_truth) if raw.sim_truth is not None else None, _ts(utcnow())))
        return cur.rowcount == 1

    def reject_derivation(self, lane: Lane, cluster_id: str, error: str, run_id: str) -> None:
        rid = "r_" + stable_hash("derivation", cluster_id, error, run_id, length=16)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO rejected VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, rid, run_id, lane.value, "DERIVED", "derived", "derived_feature", cluster_id,
                 _dumps(["R_DERIVATION"]), _dumps([f"R_DERIVATION: {error}"]), _dumps({"cluster_id": cluster_id}),
                 None, _ts(utcnow())))

    def list_quarantine(self, status: str | None = "pending", lane: Lane | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if lane:
            where.append("origin_lane=?")
            args.append(lane.value)
        sql = "SELECT * FROM quarantine" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._audit_row(r) for r in self.conn.execute(sql, [*args, limit]).fetchall()]

    def pending_quarantine_for_target(self, lane: Lane, target_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM quarantine WHERE origin_lane=? AND target_id=? AND status='pending'",
            (lane.value, target_id)).fetchall()
        return [self._audit_row(r) for r in rows]

    def get_quarantine(self, qid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM quarantine WHERE qid=?", (qid,)).fetchone()
        return self._audit_row(row) if row else None

    def resolve_quarantine(self, qid: str, status: str, note: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE quarantine SET status=?, resolved_at=?, resolution_note=? WHERE qid=?",
                              (status, _ts(utcnow()), note, qid))

    def list_rejected(self, lane: Lane | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM rejected", []
        if lane:
            sql += " WHERE origin_lane=?"
            args.append(lane.value)
        sql += " ORDER BY rejected_at DESC LIMIT ?"
        return [self._audit_row(r) for r in self.conn.execute(sql, [*args, limit]).fetchall()]

    @staticmethod
    def _audit_row(r: sqlite3.Row) -> dict[str, Any]:
        out = dict(r)
        for key in ("reasons", "details", "raw_payload", "sim_truth"):
            if out.get(key):
                out[key] = json.loads(out[key])
        return out

    # ------------------------------------------------------------ verdicts

    def insert_verdict(self, v: dict[str, Any]) -> bool:
        """Insert a verdict; an identical verdict (same inputs/model/table) only refreshes its timestamp."""
        with self.conn:
            cur = self.conn.execute("UPDATE verdicts SET computed_at=? WHERE verdict_id=?",
                                    (v["computed_at"], v["verdict_id"]))
            if cur.rowcount:
                return False
            self.conn.execute(
                "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (v["verdict_id"], v["lane"], v["cluster_id"], v["probability"], v["label"], v["attribution"],
                 v["model_version"], v["table_version"], v["table_hash"], _dumps(v), v["computed_at"]))
        return True

    def latest_verdicts(self, lane: Lane, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT v.verdict FROM verdicts v
               JOIN (SELECT cluster_id, MAX(computed_at) m FROM verdicts WHERE origin_lane=? GROUP BY cluster_id) x
                 ON v.cluster_id = x.cluster_id AND v.computed_at = x.m
               WHERE v.origin_lane=? ORDER BY v.probability DESC LIMIT ?""",
            (lane.value, lane.value, limit)).fetchall()
        return [json.loads(r["verdict"]) for r in rows]

    def latest_verdict(self, lane: Lane, cluster_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT verdict FROM verdicts WHERE origin_lane=? AND cluster_id=? ORDER BY computed_at DESC LIMIT 1",
            (lane.value, cluster_id)).fetchone()
        return json.loads(row["verdict"]) if row else None

    # -------------------------------------------------------------- stats

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for lane, table in ORIGIN_TABLES.items():
            rows = self.conn.execute(f"SELECT kind, COUNT(*) n FROM {table} GROUP BY kind").fetchall()
            out[lane.value] = {r["kind"]: r["n"] for r in rows}
        rows = self.conn.execute("SELECT origin_lane, feature, COUNT(*) n FROM derived_records GROUP BY 1, 2").fetchall()
        out["derived"] = {}
        for r in rows:
            out["derived"].setdefault(r["origin_lane"], {})[r["feature"]] = r["n"]
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM quarantine GROUP BY status").fetchall()
        out["quarantined"] = {r["status"]: r["n"] for r in rows}
        out["rejected"] = self.conn.execute("SELECT COUNT(*) FROM rejected").fetchone()[0]
        rows = self.conn.execute("SELECT origin_lane, COUNT(DISTINCT cluster_id) n FROM verdicts GROUP BY 1").fetchall()
        out["verdicts"] = {r["origin_lane"]: r["n"] for r in rows}
        return out
