import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from probe import results
from probe.server import Config, build_state, conflicts, make_server, runner_command
from selftest.helpers import ProbeCase, definition

TEST_ID = "activate.parallel.works/alvaro/webshell/gcpsmall-controller"


def record(status, started, slug="mock-abc", suite="probe-1", failed_at=None, error=None):
    return {
        "schema": 1, "suite_run": suite, "pw_cli": "v7.99.0",
        "test": {"id": TEST_ID, "workflow_name": "webshell"},
        "workflow": {"repo": "github.com/parallelworks/workflows", "path": "workflows/webshell/yamls/general.yaml",
                     "ref": "canary", "commit": "0" * 40},
        "target": {"platform": "activate.parallel.works", "user": "alvaro", "system": "gcpsmall",
                   "resource": "pw://alvaro/gcpsmall", "type": "cluster", "node": "controller"},
        "outcome": {"status": status, "failed_at": failed_at, "error": error, "phase": None,
                    "cleanup": "ok", "run_slug": slug if status != "skip" else None, "endpoint": None,
                    "started_at": started, "ended_at": started, "duration_s": 10},
    }


class ResultsTests(ProbeCase):
    def write_records(self, records, test_id=TEST_ID):
        """One execution directory per record; a string writes a malformed record.json."""
        test_dir = self.results_dir / test_id
        test_dir.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(records):
            if isinstance(r, dict):
                o = r["outcome"]
                directory = test_dir / results.artifact_dir_name(o["started_at"], o.get("run_slug") or ("skip" if o.get("status") == "skip" else None))
                results.write_record(directory, r)
            else:
                directory = test_dir / ("2026-09-%02dT000000Z_broken" % (i + 1))
                directory.mkdir()
                (directory / results.RECORD_FILE).write_text(r)
        return test_dir

    def test_malformed_records_ignored(self):
        self.write_records([record("pass", "2026-09-18T06:00:00Z"), "{broken", "", json.dumps({"schema": 2})])
        recs = results.test_records(self.results_dir / TEST_ID)
        self.assertEqual(len(recs), 1)

    def test_regression_skips_over_skips(self):
        self.write_records([
            record("pass", "2026-09-18T06:00:00Z", "a", "s1"),
            record("skip", "2026-09-19T06:00:00Z", None, "s2"),
            record("fail", "2026-09-20T06:00:00Z", "c", "s3", "run", "boom"),
        ])
        scanned = results.scan(self.results_dir)
        state = results.state(TEST_ID, scanned[TEST_ID]["records"], [])
        self.assertEqual(state["status"], "fail")
        self.assertEqual(state["previous_status"], "pass")
        self.assertEqual(state["change"], "regression")
        self.assertEqual(len(state["history"]), 3)
        runs = results.suite_runs(scanned[TEST_ID]["records"])
        self.assertEqual([r["suite_run"] for r in runs], ["s3", "s2", "s1"])
        self.assertEqual(runs[0]["fail"], 1)

    def test_recovery(self):
        self.write_records([record("fail", "2026-09-18T06:00:00Z", "a", "s1", "run", "x"),
                            record("pass", "2026-09-19T06:00:00Z", "b", "s2")])
        state = results.state(TEST_ID, results.scan(self.results_dir)[TEST_ID]["records"], [])
        self.assertEqual(state["change"], "recovery")

    def test_running_detection(self):
        test_dir = self.write_records([record("pass", "2026-09-18T06:00:00Z", "a")])
        (test_dir / "2026-09-18T060000Z_a" / "run.log").write_text("done")
        fresh = test_dir / "2026-09-19T060000Z_mock-new"
        fresh.mkdir()
        (fresh / "run.log").write_text("running")
        scanned = results.scan(self.results_dir)[TEST_ID]
        self.assertEqual(scanned["running"], ["2026-09-19T060000Z_mock-new"])
        self.assertEqual(scanned["artifacts"], ["2026-09-19T060000Z_mock-new", "2026-09-18T060000Z_a"])
        self.assertEqual(len(scanned["records"]), 1)
        self.assertEqual(results.running_executions(test_dir, now=1e12), [])


class ServerTests(ProbeCase):
    def setUp(self):
        super().setUp()
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.write_test("webshell/gcpsmall-compute.json", definition(scheduler=True))
        test_dir = self.results_dir / TEST_ID
        results.write_record(test_dir / "2026-09-18T060000Z_a", record("pass", "2026-09-18T06:00:00Z", "a", "s1"))
        art = test_dir / "2026-09-19T060000Z_b"
        results.write_record(art, record("fail", "2026-09-19T06:00:00Z", "b", "s2", "run", "boom"))
        (art / "run.log").write_text("hello log\n")
        (art / "errors.txt").write_text("boom\n")
        self.secret = self.root / "secret.txt"
        self.secret.write_text("secret")
        self.cfg = Config(self.results_dir, self.tests_dir, admin=False)
        self.server = make_server(self.cfg, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, prefix=""):
        request = urllib.request.Request("http://127.0.0.1:%d%s%s" % (self.port, prefix, path))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode(), dict(exc.headers)

    def post(self, path, body):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode())

    def test_state(self):
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["admin"])
        self.assertIsNone(data["bucket"])
        ids = [t["id"] for t in data["tests"]]
        self.assertEqual(ids, ["activate.parallel.works/alvaro/webshell/gcpsmall-compute", TEST_ID])
        controller = data["tests"][1]
        self.assertEqual(controller["status"], "fail")
        self.assertEqual(controller["change"], "regression")
        self.assertTrue(controller["defined"])
        self.assertEqual(controller["system"], "gcpsmall")
        compute = data["tests"][0]
        self.assertIsNone(compute["status"])
        self.assertEqual(compute["node"], "compute")
        self.assertEqual(compute["launch_target"].split("@")[1], "canary")
        self.assertEqual([r["suite_run"] for r in data["suite_runs"]], ["s2", "s1"])
        self.assertEqual(data["definition_errors"], [])

    def test_state_direct(self):
        data = build_state(self.cfg)
        self.assertEqual(len(data["tests"]), 2)

    def test_records_definition_artifacts(self):
        status, body, _ = self.get("/api/tests/%s/records" % TEST_ID)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["records"]), 2)
        status, body, _ = self.get("/api/tests/%s/definition" % TEST_ID)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["definition"]["workflow_name"], "webshell")
        status, body, _ = self.get("/api/tests/%s/artifacts" % TEST_ID)
        listing = json.loads(body)["artifacts"]
        self.assertEqual(listing[0]["name"], "2026-09-19T060000Z_b")
        self.assertEqual([f["name"] for f in listing[0]["files"]], ["errors.txt", "record.json", "run.log"])
        status, body, headers = self.get("/api/tests/%s/artifacts/2026-09-19T060000Z_b/run.log" % TEST_ID)
        self.assertEqual((status, body), (200, "hello log\n"))
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))

    def test_path_safety(self):
        for path in ["/api/tests/%s/artifacts/../../../../secret.txt" % TEST_ID,
                     "/api/tests/%s/artifacts/2026-09-19T060000Z_b/..%%2F..%%2Fsecret.txt" % TEST_ID,
                     "/api/tests/a/b/c/../../../secret.txt/records",
                     "/api/tests/%s/artifacts/2026-09-19T060000Z_b/missing.txt" % TEST_ID]:
            status, body, _ = self.get(path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn("secret", body)
        status, body, _ = self.get("/../secret.txt")
        self.assertNotIn("secret", body)

    def test_static_and_prefix(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>PROBE</title>", body)
        status, body, _ = self.get("/app.js")
        self.assertEqual(status, 200)
        # a path-based endpoint puts everything under a prefix
        self.cfg.prefix = "/me/session/alvaro/probe-x"
        status, body, _ = self.get("/me/session/alvaro/probe-x/")
        self.assertIn("<title>PROBE</title>", body)
        status, body, _ = self.get("/me/session/alvaro/probe-x/styles.css")
        self.assertEqual(status, 200)
        status, body, _ = self.get("/me/session/alvaro/probe-x/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.cfg.prefix = ""

    def test_runner_command_and_conflicts(self):
        cfg = Config(self.results_dir, self.tests_dir, bucket="pw://alvaro/gcpbucket/probe/results/")
        command = runner_command(cfg, [TEST_ID])
        self.assertEqual(command[1:4], ["-m", "probe", "run"])
        self.assertIn("--bucket", command)
        self.assertEqual(command[command.index("--bucket") + 1], "pw://alvaro/gcpbucket/probe/results")
        self.assertEqual(command[-2:], ["--id", TEST_ID])
        self.assertNotIn("--bucket", runner_command(Config(self.results_dir, self.tests_dir), []))
        self.assertFalse(conflicts([], []))
        self.assertFalse(conflicts([{"a"}], ["b"]))
        self.assertTrue(conflicts([{"a"}], ["a", "b"]))
        self.assertTrue(conflicts([{"a"}], []))
        self.assertTrue(conflicts([None], ["b"]))

    def test_refresh_pulls_from_the_bucket_even_when_read_only(self):
        status, data = self.post("/api/refresh", {})
        self.assertEqual(status, 502)
        self.assertIn("no results bucket", data["message"])
        self.cfg.bucket = "pw://alvaro/gcpbucket/probe/results"
        try:
            # nothing in the bucket yet: the local copy is kept
            status, data = self.post("/api/refresh", {})
            self.assertEqual(status, 200)
            self.assertIn("no results yet", data["message"])
            self.assertTrue((self.results_dir / TEST_ID).exists())
            # the bucket holds one other test: after a refresh the local copy is exactly the bucket
            other = "activate.parallel.works/alvaro/webshell/gcpsmall-compute"
            bucket_dir = self.state_dir / "bucket" / "alvaro/gcpbucket/probe/results" / other / "2026-09-20T060000Z_z"
            results.write_record(bucket_dir, dict(record("pass", "2026-09-20T06:00:00Z", "z"), test={"id": other, "workflow_name": "webshell"}))
            status, data = self.post("/api/refresh", {})
            self.assertEqual(status, 200)
            self.assertIn("replaced", data["message"])
            self.assertTrue((self.results_dir / other / "2026-09-20T060000Z_z" / "record.json").exists())
            self.assertFalse((self.results_dir / TEST_ID).exists(), "results deleted from the bucket disappear")
            # while a run started here is in progress, the download is merged instead
            class Alive:
                def poll(self):
                    return None
            (self.results_dir / TEST_ID / "2026-09-21T060000Z_pending").mkdir(parents=True)
            self.cfg.active_runs.append((Alive(), {TEST_ID}))
            status, data = self.post("/api/refresh", {})
            self.assertEqual(status, 200)
            self.assertIn("merged", data["message"])
            self.assertTrue((self.results_dir / TEST_ID / "2026-09-21T060000Z_pending").exists())
            self.assertTrue((self.results_dir / other).exists())
            self.cfg.active_runs = []
        finally:
            self.cfg.bucket = None

    def test_admin_actions_refused_when_read_only(self):
        status, data = self.post("/api/run", {"ids": [TEST_ID]})
        self.assertEqual(status, 403)
        status, data = self.post("/api/cancel", {"slug": "x"})
        self.assertEqual(status, 403)

    def test_admin_validation(self):
        self.cfg.admin = True
        try:
            status, data = self.post("/api/run", {"ids": ["../etc"]})
            self.assertEqual(status, 400)
            status, data = self.post("/api/run", {})
            self.assertEqual(status, 400)
            status, data = self.post("/api/cancel", {"slug": "bad slug"})
            self.assertEqual(status, 400)
            status, data = self.post("/api/cancel", {"slug": "nope", "platform": "activate.parallel.works"})
            self.assertEqual(status, 502)
        finally:
            self.cfg.admin = False


if __name__ == "__main__":
    unittest.main()
