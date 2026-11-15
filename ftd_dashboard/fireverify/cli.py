"""Command-line interface for the ingestion engine.

    python -m fireverify sources
    python -m fireverify ingest --mode simulated
    python -m fireverify ingest --mode live --bbox -123,37,-119,40 --days 1
    python -m fireverify clusters --mode simulated
    python -m fireverify evidence <cluster_id> --mode simulated
    python -m fireverify quarantine list | release <qid> | reject <qid>
    python -m fireverify rejected
    python -m fireverify import-archive fire_archive_SV-C2_12345.csv --source VIIRS_SNPP_SP
    python -m fireverify stats

    python -m fireverify verify --mode simulated
    python -m fireverify explain <cluster_id> --mode simulated
    python -m fireverify verdicts --mode simulated
    python -m fireverify simulate-batch --runs 40
    python -m fireverify evaluate --mode simulated
    python -m fireverify calibrate --mode simulated --holdout 0.3
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

from fireverify.config import Settings
from fireverify.ingestion.engine import IngestionEngine, RunReport
from fireverify.ingestion.simulated import SCENARIOS, batch_aoi
from fireverify.ingestion.store import Store
from fireverify.models import BBox, Lane, Mode
from fireverify.verification import LikelihoodTable, Verdict, Verifier
from fireverify.verification.calibration import evaluate, fit, split

DEFAULT_SIM_BBOX = "-121.0,38.0,-119.0,40.0"


def _print_report(report: RunReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return
    print(f"\n{report.run_id}  mode={report.mode}  aoi={report.aoi}")
    print(f"window {report.start:%Y-%m-%d %H:%M} -> {report.end:%Y-%m-%d %H:%M} UTC\n")
    header = f"{'source':<26}{'status':<12}{'fetched':>8}{'accepted':>9}{'dup/old':>8}{'quarant':>8}{'reject':>7}  reasons"
    print(header)
    print("-" * len(header))
    for name, st in report.sources.items():
        reasons = ", ".join(f"{k}x{v}" for k, v in sorted(st.reasons.items()))
        print(f"{name:<26}{st.status:<12}{st.fetched:>8}{st.accepted:>9}{st.already_ingested:>8}"
              f"{st.quarantined:>8}{st.rejected:>7}  {reasons}")
        if st.status in ("unavailable", "error") and st.detail:
            print(f"{'':<26}  -> {st.detail[:110]}")
    t = report.totals()
    print("-" * len(header))
    print(f"{'TOTAL':<38}{t['fetched']:>8}{t['accepted']:>9}{t['already_ingested']:>8}{t['quarantined']:>8}{t['rejected']:>7}")
    print(f"\nclusters found: {report.clusters_found}   enriched: {len(report.clusters_enriched)}"
          f"   derived features written: {report.derived_written}")
    for cid in report.clusters_enriched:
        print(f"  {cid}")
    for err in report.derivation_errors:
        print(f"  derivation error: {err}")


def _print_verdicts(verdicts: list[Verdict]) -> None:
    from collections import Counter
    for v in sorted(verdicts, key=lambda v: -v.probability):
        print(f"{v.cluster_id}  P={v.probability:7.4f}  {v.label:<19} {v.attribution or '':<24} "
              f"evidence {len(v.posterior.contributions)}/6")
    print("\n" + "  ".join(f"{k}={n}" for k, n in Counter(v.label for v in verdicts).most_common()))


def _print_explanation(v: Verdict) -> None:
    post = v.posterior
    print(f"\n{v.cluster_id}   P(active vegetation fire) = {v.probability:.4f}   ->  {v.label.upper()}")
    if v.attribution:
        print(f"probable cause: {v.attribution}")
    print(f"table {v.table_version} ({v.table_hash})\n")
    print(f"{'term':<14}{'state':<22}{'P(s|fire)':>10}{'P(s|not)':>10}{'LR':>9}{'log-LR':>8}   detail")
    print("-" * 110)
    print(f"{'prior':<14}{post.prior_basis:<22}{'':>10}{'':>10}{'':>9}{post.prior_log_odds:>+8.2f}   "
          f"P(fire) = {post.prior:.2f} from land cover {v.evidence.land_cover or 'unknown'}")
    for c in post.contributions:
        print(f"{c.group:<14}{c.state:<22}{c.p_given_fire:>10.3f}{c.p_given_not_fire:>10.3f}"
              f"{c.lr:>9.3f}{c.log_lr:>+8.2f}   {c.detail[:60]}")
    for group, why in post.missing.items():
        print(f"{group:<14}{'(missing)':<22}{'':>10}{'':>10}{'1':>9}{0:>+8.2f}   {why[:60]}")
    print("-" * 110)
    print(f"{'posterior':<14}{'':<22}{'':>10}{'':>10}{'':>9}{post.log_odds:>+8.2f}   P = {v.probability:.4f}")


def _print_eval(name: str, m: dict) -> None:
    if not m.get("n"):
        print(f"{name}: no labelled examples")
        return
    print(f"\n{name}  (n={m['n']}, table {m['table_version']})")
    print(f"  accuracy {m['accuracy']}  precision {m['precision']}  recall {m['recall']}  f1 {m['f1']}"
          f"  brier {m['brier']}  log-loss {m['log_loss']}")
    print(f"  confusion {m['confusion']}")
    print(f"  {'truth':<30}{'n':>4}{'mean P':>9}   decisions")
    for truth, row in m["by_truth"].items():
        print(f"  {truth:<30}{row['n']:>4}{row['mean_p']:>9.3f}   {row['labels']}")


def _read_labels(path: Path | None) -> dict[str, bool] | None:
    if path is None:
        return None
    with path.open(newline="") as fh:
        return {r["cluster_id"].strip(): r["is_fire"].strip().lower() in {"1", "true", "yes", "fire"}
                for r in csv.DictReader(fh)}


def _calendar(args: argparse.Namespace, engine: IngestionEngine, store: Store) -> int:
    from datetime import date, timedelta

    from fireverify.calendar.backfill import MAX_CALENDAR_AREA_DEG2, run_backfill
    from fireverify.dashboard.server import DashboardApp

    bbox = BBox.parse(args.bbox)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=364)
    if args.ccmd == "backfill":
        if bbox.area_deg2 > MAX_CALENDAR_AREA_DEG2:
            print(f"area {bbox.area_deg2:.1f} deg² is larger than {MAX_CALENDAR_AREA_DEG2:.0f} deg²", file=sys.stderr)
            return 2
        out = run_backfill(engine, args.mode, bbox, start, end, seed=args.seed, refresh=args.refresh,
                           progress=lambda msg, frac: print(f"{frac:5.0%}  {msg}" if frac >= 0 else f"       {msg}", flush=True))
        print(f"{out['run_id']}: {out['chunks']} chunks fetched, {out['chunks_skipped']} up to date; {out['totals']}")
        for name, st in out["sources"].items():
            if st["status"] == "error":
                print(f"  {name}: {st['detail'][:160]}")
        if out["injected"]:
            print(f"  injected anomalies: {out['injected']}")
        return 0
    app = DashboardApp(engine.settings, store.path)
    q = {"bbox": [args.bbox], "start": [start.isoformat()], "end": [end.isoformat()], "low": ["1" if args.low else "0"]}
    res = app.calendar(Mode(args.mode).lane, q)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    a, cal, tot = res["analysis"], res["calibration"], res["analysis"]["totals"]
    print(f"{res['start']} -> {res['end']}  {tot['days_with_data']}/{tot['days']} days with data, "
          f"{tot['days_complete']} with both sensors")
    print(f"harmonized fire cells {tot['harmonized_cells']:,}  (MODIS {tot['modis_cells']:,} cells / "
          f"{tot['modis_pixels']:,} px, VIIRS {tot['viirs_cells']:,} cells / {tot['viirs_pixels']:,} px)")
    print(f"cross-sensor: {cal['viirs_cells_per_modis_cell']} VIIRS cells per MODIS cell; "
          f"{cal['modis_confirmed_by_viirs']} of MODIS cells confirmed by VIIRS")
    t = a["trend"]
    if t["status"] == "ok":
        rate = f"{t['pct_per_year']:+}%/year" if "pct_per_year" in t else f"{t['pct_per_month']:+}%/month"
        print(f"trend: {t['direction']} ({rate}, p={t['p_value']}) - {t['method']}")
    for sn in a["seasons"]:
        print(f"fire season {sn['from']}..{sn['to']}: {sn['start']} -> {sn['end']}, peak week around "
              f"{sn['peak_week_center']}")
    print(f"\n{len(a['anomalies'])} anomalies")
    for an in a["anomalies"]:
        print(f"  [{an['severity']:<6}] {an['type']:<14} {an['summary']}")
    groups = a.get("actions", {}).get("groups", [])
    if groups:
        print(f"\ngovernment action options ({len(groups)} findings)")
        for g in groups:
            print(f"  [{g['priority']:<6}] {g['finding']}")
            for act in g["actions"]:
                print(f"           - {act['title']} ({act['lead']}; {act['timeframe']})")
    if res["injected"]:
        hits = sum(i["detected"] for i in res["injected"])
        print(f"\nsimulation: {hits}/{len(res['injected'])} injected anomalies detected")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fireverify", description="ftd.io - satellite fire hotspot verification (MODIS, VIIRS, Sentinel-1/2/3)")
    parser.add_argument("--db", type=Path, help="SQLite database path (default: $FIREVERIFY_DB or data/fireverify.db)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sources", help="show which live sources are configured")

    p = sub.add_parser("ingest", help="run one ingestion cycle")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--bbox", help="west,south,east,north (default for simulated: %s)" % DEFAULT_SIM_BBOX)
    p.add_argument("--days", type=float, default=2.0, help="look-back window in days (default 2)")
    p.add_argument("--seed", type=int, default=42, help="simulation seed")
    p.add_argument("--scenarios", help=f"comma-separated subset of: {','.join(SCENARIOS)}")
    p.add_argument("--no-enrich", action="store_true", help="skip per-cluster Sentinel/historical enrichment")
    p.add_argument("--force-enrich", action="store_true", help="re-enrich every cluster, not only ones with new detections")
    p.add_argument("--verify", action="store_true", help="score clusters with the naive Bayes verifier afterwards")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("simulate-batch", help="many simulated runs over separate AOIs (for calibration/evaluation)")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--days", type=float, default=2.0)
    p.add_argument("--verify", action="store_true")

    lik_help = "likelihood table JSON (default: $FIREVERIFY_LIKELIHOODS if present, else expert defaults)"
    p = sub.add_parser("verify", help="score clusters: P(active vegetation fire) via naive Bayes")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--cluster", help="score a single cluster")
    p.add_argument("--likelihoods", type=Path, help=lik_help)

    p = sub.add_parser("explain", help="show the per-evidence breakdown of one cluster's score")
    p.add_argument("cluster_id")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--likelihoods", type=Path, help=lik_help)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verdicts", help="latest verdict per cluster")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("evaluate", help="score labelled clusters (simulation truth or --labels CSV)")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--labels", type=Path, help="CSV with cluster_id,is_fire (needed for live mode)")
    p.add_argument("--likelihoods", type=Path, help=lik_help)

    p = sub.add_parser("calibrate", help="fit likelihood tables from labelled clusters")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--labels", type=Path, help="CSV with cluster_id,is_fire (needed for live mode)")
    p.add_argument("--holdout", type=float, default=0.3, help="fraction held out for evaluation (0 = none)")
    p.add_argument("--strength", type=float, default=10.0, help="pseudo-counts given to the current table")
    p.add_argument("--likelihoods", type=Path, help="table to start from (" + lik_help + ")")
    p.add_argument("--out", type=Path, help="where to save (default: $FIREVERIFY_LIKELIHOODS)")

    p = sub.add_parser("clusters", help="list clustered fire events with derived features")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("evidence", help="print the evidence bundle for one cluster (JSON)")
    p.add_argument("cluster_id")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")

    p = sub.add_parser("quarantine", help="review quarantined records")
    qsub = p.add_subparsers(dest="qcmd", required=True)
    q = qsub.add_parser("list")
    q.add_argument("--status", default="pending", choices=["pending", "released", "rejected", "all"])
    q.add_argument("--limit", type=int, default=50)
    q = qsub.add_parser("show")
    q.add_argument("qid")
    q = qsub.add_parser("release")
    q.add_argument("qid")
    q.add_argument("--note")
    q = qsub.add_parser("reject")
    q.add_argument("qid")
    q.add_argument("--note")

    p = sub.add_parser("rejected", help="list rejected records")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("import-archive", help="import a FIRMS archive CSV as historical detections")
    p.add_argument("path", type=Path)
    p.add_argument("--source", required=True, help="e.g. VIIRS_SNPP_SP, VIIRS_NOAA20_SP, MODIS_SP")

    p = sub.add_parser("calendar", help="harmonized MODIS + VIIRS fire calendar: backfill, trends, anomalies")
    csub = p.add_subparsers(dest="ccmd", required=True)
    for name, text in (("backfill", "fetch the MODIS + VIIRS archive for an area and date range"),
                       ("show", "harmonize, then print the trend, fire season and anomalies")):
        c = csub.add_parser(name, help=text)
        c.add_argument("--mode", choices=[m.value for m in Mode], default="simulated")
        c.add_argument("--bbox", required=True, help="west,south,east,north (<= 25 deg² for backfill)")
        c.add_argument("--start", help="YYYY-MM-DD (default: 365 days before --end)")
        c.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
        if name == "backfill":
            c.add_argument("--seed", type=int, default=1, help="simulated regime seed")
            c.add_argument("--refresh", action="store_true", help="re-fetch chunks fetched before")
        else:
            c.add_argument("--low", action="store_true", help="include low-confidence pixels")
            c.add_argument("--json", action="store_true")

    p = sub.add_parser("dashboard", help="open the interactive web dashboard")
    # Render and similar hosts set PORT; when it is set, bind on all interfaces.
    default_port = int(os.environ.get("PORT", "8765"))
    default_host = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    p.add_argument("--host", default=default_host,
                   help="bind address (default: 127.0.0.1, or 0.0.0.0 when $PORT is set)")
    p.add_argument("--port", type=int, default=default_port,
                   help="port (default: $PORT or 8765)")

    sub.add_parser("stats", help="record counts per lane")
    p = sub.add_parser("runs", help="recent ingestion runs")
    p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env()
    store = Store(args.db or settings.db_path)
    engine = IngestionEngine(settings, store)
    try:
        return _dispatch(args, engine, store, settings)
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, engine: IngestionEngine, store: Store, settings: Settings) -> int:
    def verifier() -> Verifier:
        path = getattr(args, "likelihoods", None) or settings.likelihoods_path
        if getattr(args, "likelihoods", None) and not path.is_file():
            raise SystemExit(f"likelihood table {path} not found")
        return Verifier(store, LikelihoodTable.load_or_default(path))

    if args.cmd == "sources":
        for name, (ok, why) in engine.source_availability().items():
            print(f"{'OK ' if ok else '-- '} {name:<26} {why}")
        print("\nSimulated mode needs no credentials.")
        return 0

    if args.cmd == "ingest":
        mode = Mode(args.mode)
        bbox_text = args.bbox or (DEFAULT_SIM_BBOX if mode is Mode.SIMULATED else None)
        if not bbox_text:
            print("--bbox is required in live mode", file=sys.stderr)
            return 2
        scenarios = [s.strip() for s in args.scenarios.split(",")] if args.scenarios else None
        report = engine.run(mode, BBox.parse(bbox_text), days=args.days, seed=args.seed, scenarios=scenarios,
                            enrich=not args.no_enrich, force_enrich=args.force_enrich)
        _print_report(report, args.json)
        if args.verify:
            print()
            _print_verdicts(verifier().verify_all(mode.lane))
        return 0

    if args.cmd == "simulate-batch":
        for i in range(args.runs):
            seed = args.seed_start + i
            aoi = batch_aoi(seed)
            r = engine.run(Mode.SIMULATED, aoi, days=args.days, seed=seed)
            t = r.totals()
            print(f"seed {seed:<4} aoi {aoi.key():<38} accepted={t['accepted']:<4} quarantined={t['quarantined']:<3}"
                  f" rejected={t['rejected']:<3} clusters={len(r.clusters_enriched)}")
        if args.verify:
            print()
            _print_verdicts(verifier().verify_all(Lane.SIMULATED))
        return 0

    if args.cmd == "verify":
        lane = Mode(args.mode).lane
        v = verifier()
        if args.cluster:
            verdict = v.verify_cluster(lane, args.cluster)
            if verdict is None:
                print(f"no derived features for {args.cluster}", file=sys.stderr)
                return 1
            _print_explanation(verdict)
        else:
            verdicts = v.verify_all(lane)
            if not verdicts:
                print("no clusters to verify - run `ingest` first")
            else:
                _print_verdicts(verdicts)
        return 0

    if args.cmd == "explain":
        verdict = verifier().verify_cluster(Mode(args.mode).lane, args.cluster_id)
        if verdict is None:
            print(f"no derived features for {args.cluster_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(verdict.as_dict(), indent=2, default=str))
        else:
            _print_explanation(verdict)
        return 0

    if args.cmd == "verdicts":
        rows = store.latest_verdicts(Mode(args.mode).lane, args.limit)
        if not rows:
            print("no verdicts yet - run `verify` first")
        for r in rows:
            print(f"{r['cluster_id']}  P={r['probability']:7.4f}  {r['label']:<19} {r['attribution'] or '':<24} "
                  f"{r['table_version']}  {r['computed_at'][:19]}")
        return 0

    if args.cmd == "evaluate":
        lane = Mode(args.mode).lane
        v = verifier()
        examples = v.examples(lane, _read_labels(args.labels))
        if lane is Lane.LIVE and args.labels is None:
            print("live mode needs --labels (cluster_id,is_fire)", file=sys.stderr)
            return 2
        _print_eval("evaluation", evaluate(examples, v.table))
        if lane is Lane.SIMULATED:
            print("\nnote: simulated truth measures agreement with the simulator, not real-world accuracy")
        return 0

    if args.cmd == "calibrate":
        lane = Mode(args.mode).lane
        if lane is Lane.LIVE and args.labels is None:
            print("live mode needs --labels (cluster_id,is_fire)", file=sys.stderr)
            return 2
        v = verifier()
        examples = v.examples(lane, _read_labels(args.labels))
        train, test = split(examples, args.holdout) if args.holdout > 0 else (examples, [])
        if not train:
            print("no labelled training examples", file=sys.stderr)
            return 1
        source = f"{lane.value} lane" + (f" labels {args.labels}" if args.labels else " simulation truth")
        fitted = fit(train, v.table, strength=args.strength, source=source)
        print(f"fitted on {len(train)} clusters ({sum(e.is_fire for e in train)} fire), holding out {len(test)}")
        if test:
            _print_eval("held-out, current table", evaluate(test, v.table))
            _print_eval("held-out, fitted table", evaluate(test, fitted))
        out = args.out or settings.likelihoods_path
        fitted.save(out)
        print(f"\nsaved {fitted.version} -> {out}")
        if lane is Lane.SIMULATED:
            print("warning: fitted on simulated data - these likelihoods describe the simulator, not the real world")
        return 0

    if args.cmd == "clusters":
        rows = store.list_clusters(Mode(args.mode).lane, args.limit)
        if not rows:
            print("no clusters yet - run `ingest` first")
        for r in rows:
            print(f"{r['cluster_id']}  n={r['n_detections']:<3} overpasses={r['n_overpasses']:<2} "
                  f"sensors={'+'.join(r['sensors']):<12} maxFRP={r['max_frp_mw']}  "
                  f"({r['centroid_lat']:.4f}, {r['centroid_lon']:.4f})  {r['first_seen'][:16]}")
        return 0

    if args.cmd == "evidence":
        bundle = engine.evidence(Mode(args.mode).lane, args.cluster_id)
        if bundle is None:
            print(f"no derived features for {args.cluster_id}", file=sys.stderr)
            return 1
        print(json.dumps(bundle, indent=2, default=str))
        return 0

    if args.cmd == "quarantine":
        if args.qcmd == "list":
            rows = store.list_quarantine(None if args.status == "all" else args.status, limit=args.limit)
            for r in rows:
                print(f"{r['qid']}  {r['status']:<8} {r['origin_lane']:<9} {r['source']:<20} "
                      f"{','.join(r['reasons']):<28} {r['details'][0][:70] if r['details'] else ''}")
            if not rows:
                print("quarantine is empty")
        elif args.qcmd == "show":
            row = store.get_quarantine(args.qid)
            print(json.dumps(row, indent=2, default=str) if row else f"no entry {args.qid}")
        elif args.qcmd == "release":
            outcome = engine.release_quarantined(args.qid, args.note)
            print(f"{args.qid} -> {outcome}")
        elif args.qcmd == "reject":
            engine.reject_quarantined(args.qid, args.note)
            print(f"{args.qid} -> rejected")
        return 0

    if args.cmd == "rejected":
        for r in store.list_rejected(limit=args.limit):
            print(f"{r['rid']}  {r['origin_lane']:<9} {r['source']:<20} {','.join(r['reasons']):<22} "
                  f"{r['details'][0][:80] if r['details'] else ''}")
        return 0

    if args.cmd == "import-archive":
        _print_report(engine.import_archive(args.path, args.source), as_json=False)
        return 0

    if args.cmd == "calendar":
        return _calendar(args, engine, store)

    if args.cmd == "dashboard":
        from fireverify.dashboard.server import serve
        serve(settings, store.path, args.host, args.port)
        return 0

    if args.cmd == "stats":
        print(json.dumps(store.counts(), indent=2))
        return 0

    if args.cmd == "runs":
        for r in store.list_runs(args.limit):
            st = r["stats"]
            print(f"{r['run_id']}  {r['mode']:<14} accepted={st.get('accepted', 0):<5} "
                  f"quarantined={st.get('quarantined', 0):<4} rejected={st.get('rejected', 0):<4} "
                  f"clusters={st.get('clusters_enriched', '-')}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
