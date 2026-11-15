import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from fireverify.config import Settings
from fireverify.dashboard.server import DashboardApp, make_handler, _normalize_host


class DashboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        settings = Settings(enable_overpass=False, likelihoods_path=Path(cls.tmp.name) / "none.json")
        cls.app = DashboardApp(settings, Path(cls.tmp.name) / "dash.db")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        job = cls.post("/api/ingest", {"mode": "simulated", "runs": 1, "seed": 5, "days": 2})
        for _ in range(200):
            job = cls.get(f"/api/jobs/{job['job_id']}")
            if job["status"] != "running":
                break
            time.sleep(0.1)
        cls.job = job

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    @classmethod
    def get(cls, path):
        with urllib.request.urlopen(cls.base + path) as r:
            return json.loads(r.read())

    @classmethod
    def post(cls, path, body, headers=None):
        req = urllib.request.Request(cls.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers=headers if headers is not None else
                                     {"Content-Type": "application/json", "X-Requested-With": "fireverify"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_job_ran_and_events_are_scored(self):
        self.assertEqual(self.job["status"], "done", self.job["message"])
        events = self.get("/api/events?lane=simulated")
        self.assertEqual(len(events), 7)
        self.assertTrue(all(0 <= e["probability"] <= 1 and e["label"] for e in events))
        summary = self.get("/api/summary?lane=simulated")
        self.assertEqual(summary["events"], 7)
        self.assertGreater(summary["quarantine_pending"], 0)
        self.assertEqual(self.get("/api/summary?lane=live")["events"], 0)   # lanes stay separate

    def test_event_detail(self):
        cid = self.get("/api/events?lane=simulated")[0]["cluster_id"]
        d = self.get(f"/api/events/{cid}?lane=simulated")
        self.assertEqual(d["verdict"]["cluster_id"], cid)
        self.assertIn("cluster_summary", d["derived"])
        self.assertTrue(d["detections"])
        self.assertIsNotNone(d["sim_truth"])

    def test_quarantine_release_over_http(self):
        pending = self.get("/api/quarantine?lane=simulated")
        target = next(q for q in pending if "Q_LOW_CONF" in q["reasons"])
        out = self.post(f"/api/quarantine/{target['qid']}/release", {"note": "test"})
        self.assertTrue(out["outcome"].startswith("hs_"))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(f"/api/quarantine/{target['qid']}/release", {})
        self.assertEqual(ctx.exception.code, 409)   # already resolved

    def test_post_requires_same_origin_header(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/ingest", {"mode": "simulated"}, headers={"Content-Type": "application/json"})
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/ingest", {"mode": "simulated"}, headers={
                "Content-Type": "application/json", "X-Requested-With": "fireverify", "Origin": "http://evil.example"})
        self.assertEqual(ctx.exception.code, 403)

    def test_input_validation(self):
        for body in ({"mode": "live", "bbox": [0, 0, 1, 1]},          # no FIRMS key configured
                     {"mode": "simulated", "runs": 500},
                     {"mode": "nope"}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/api/ingest", body)
            self.assertEqual(ctx.exception.code, 400, body)

    def test_regions_and_active_areas(self):
        regions = self.get("/api/regions")
        self.assertEqual(len(regions), 20)
        for r in regions:
            w, s, e, n = r["bbox"]
            self.assertLessEqual((e - w) * (n - s), 100)       # every preset is a valid live area
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/active-areas")                       # needs a FIRMS key
        self.assertEqual(ctx.exception.code, 400)

    def test_live_area_limits(self):
        app = DashboardApp(Settings(firms_map_key="KEY", enable_overpass=False), Path(self.tmp.name) / "x.db")
        from fireverify.dashboard.server import ApiError
        with self.assertRaises(ApiError) as ctx:
            app.start_job({"mode": "live", "bbox": [-180, -85, 180, 85]})
        self.assertIn("too large", str(ctx.exception))
        with self.assertRaises(ApiError):
            app.start_job({"mode": "live", "bbox": [10, 5, 5, 10]})     # west > east

    def test_static_files_and_traversal(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"ftd.io", r.read())
        with urllib.request.urlopen(self.base + "/static/app.js") as r:
            self.assertIn("javascript", r.headers["Content-Type"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/static/../server.py")
        self.assertEqual(ctx.exception.code, 404)



class DashboardAuthAndHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        settings = Settings(enable_overpass=False, likelihoods_path=Path(cls.tmp.name) / "none.json")
        cls.app = DashboardApp(settings, Path(cls.tmp.name) / "auth.db")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app, admin_token="test-token"))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _post(self, headers):
        req = urllib.request.Request(
            self.base + "/api/ingest",
            data=json.dumps({"mode": "simulated", "runs": 1, "seed": 1, "days": 1}).encode(),
            method="POST",
            headers=headers,
        )
        return urllib.request.urlopen(req)

    def _assert_token_accepted(self, headers):
        """Auth (and origin) passed if the server starts a job (200) or is already busy (409)."""
        try:
            with self._post(headers) as r:
                self.assertEqual(r.status, 200)
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409, exc.read().decode())
            return None

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read()), {"ok": True})
        with urllib.request.urlopen(self.base + "/api/health") as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})

    def test_get_stays_public(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"ftd.io", r.read())
        with urllib.request.urlopen(self.base + "/api/summary?lane=simulated") as r:
            data = json.loads(r.read())
            self.assertEqual(data["lane"], "simulated")

    def test_post_without_token_is_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({"Content-Type": "application/json", "X-Requested-With": "fireverify"})
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("Bearer", ctx.exception.headers.get("WWW-Authenticate", ""))

    def test_post_with_bearer_token_is_accepted(self):
        data = self._assert_token_accepted({
            "Content-Type": "application/json",
            "X-Requested-With": "fireverify",
            "Authorization": "Bearer test-token",
        })
        if data is not None:
            self.assertIn("job_id", data)
            self.assertEqual(data["kind"], "ingest")

    def test_post_with_basic_auth_is_accepted(self):
        basic = base64.b64encode(b"admin:test-token").decode()
        self._assert_token_accepted({
            "Content-Type": "application/json",
            "X-Requested-With": "fireverify",
            "Authorization": f"Basic {basic}",
        })

    def test_post_with_wrong_token_is_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({
                "Content-Type": "application/json",
                "X-Requested-With": "fireverify",
                "Authorization": "Bearer nope",
            })
        self.assertEqual(ctx.exception.code, 401)

    def test_origin_allows_public_and_render_hosts(self):
        for host in ("nasa-harmonized-hotspots.nimblesoft.ch", "nasa-harmonized-hotspots.onrender.com"):
            self._assert_token_accepted({
                "Content-Type": "application/json",
                "X-Requested-With": "fireverify",
                "Authorization": "Bearer test-token",
                "Host": host,
                "Origin": f"https://{host}",
            })

    def test_normalize_host_strips_default_ports(self):
        self.assertEqual(_normalize_host("https://nasa-harmonized-hotspots.nimblesoft.ch"),
                         "nasa-harmonized-hotspots.nimblesoft.ch")
        self.assertEqual(_normalize_host("nasa-harmonized-hotspots.nimblesoft.ch:443"),
                         "nasa-harmonized-hotspots.nimblesoft.ch")
        self.assertEqual(_normalize_host("https://svc.onrender.com:443"), "svc.onrender.com")
        self.assertEqual(_normalize_host("127.0.0.1:8765"), "127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
