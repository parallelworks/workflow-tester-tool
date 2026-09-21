import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from probe import runner
from probe.results import artifact_dir_name
from probe.runner import Console, Options
from selftest import helpers
from selftest.helpers import SUBMITTER, WEBSHELL, ProbeCase, definition

CONTROLLER = "activate.parallel.works/alvaro/webshell/gcpsmall-controller"


class RunnerCase(ProbeCase):
    def run_suite(self, **overrides):
        opts = Options(tests_dir=self.tests_dir, results_dir=self.results_dir, poll_s=1, workers=4)
        for key, value in overrides.items():
            setattr(opts, key, value)
        self.console = io.StringIO()
        code = runner.run_suite(opts, Console(self.console))
        return code, self.console.getvalue()

    def artifact_dirs(self, test_id):
        test_dir = self.results_dir / test_id
        return sorted(p.name for p in test_dir.iterdir() if p.is_dir()) if test_dir.exists() else []


class RunnerTests(RunnerCase):
    def test_endpoint_pass_with_cleanup(self):
        self.write_test("webshell/gcpsmall-controller.json", definition(leftover_patterns=["ttyd"]))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "endpoint": {WEBSHELL: "webshell"}})
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        records = self.records(CONTROLLER)
        self.assertEqual(len(records), 1)
        record = records[0]
        outcome = record["outcome"]
        self.assertEqual(outcome["status"], "pass")
        self.assertIsNone(outcome["failed_at"])
        self.assertIsNone(outcome["error"])
        self.assertEqual(outcome["cleanup"], "ok")
        self.assertTrue(outcome["endpoint"].startswith("webshell-mock-"))
        self.assertTrue(outcome["run_slug"].startswith("mock-"))
        self.assertIsNone(outcome["http"])
        self.assertEqual(record["workflow"]["commit"], self.commit)
        self.assertEqual(record["workflow"]["repo"], helpers.REPO_URL)
        self.assertEqual(record["target"], {"platform": "activate.parallel.works", "user": "alvaro", "system": "gcpsmall",
                                            "resource": "pw://alvaro/gcpsmall", "type": "cluster", "node": "controller"})
        self.assertEqual(record["pw_cli"], "v7.99.0-mock")
        self.assertTrue(record["suite_run"].startswith("probe-"))
        self.assertEqual(set(outcome), {"status", "failed_at", "error", "phase", "http", "cleanup", "run_slug",
                                        "endpoint", "started_at", "ended_at", "duration_s"})
        # artifacts
        dirs = self.artifact_dirs(CONTROLLER)
        self.assertEqual(dirs, [artifact_dir_name(outcome["started_at"], outcome["run_slug"])])
        files = sorted(p.name for p in (self.results_dir / CONTROLLER / dirs[0]).iterdir())
        self.assertEqual(files, ["launch.json", "run.log", "view.json"])
        log = (self.results_dir / CONTROLLER / dirs[0] / "run.log").read_text()
        self.assertIn("launch: pw workflows run --trust ", log)
        self.assertIn(WEBSHELL, log)
        self.assertIn("workflow file ", log)
        self.assertIn("endpoints delete webshell-", log)
        self.assertIn("no leftovers", log)
        # the endpoint is gone and the launch carried --trust and the probe name
        launches = [c for c in self.calls() if " run --trust " in c]
        self.assertEqual(len(launches), 1)
        self.assertIn("--name probe: " + CONTROLLER, launches[0])
        self.assertIn("PASS", out)

    def test_batch_pass_has_null_endpoint_fields(self):
        self.write_test("script_submitter/gcpsmall-controller.json",
                        definition(kind="batch", workflow_name="script_submitter",
                                   path="workflows/script_submitter/v3.6/general.yaml"))
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        outcome = self.records("activate.parallel.works/alvaro/script_submitter/gcpsmall-controller")[0]["outcome"]
        self.assertEqual(outcome["status"], "pass")
        self.assertIsNone(outcome["endpoint"])
        self.assertIsNone(outcome["http"])
        self.assertEqual(outcome["cleanup"], "ok")
        self.assertFalse(any(c.startswith("endpoints delete") for c in self.calls()))

    def test_skip_when_resource_off_and_missing(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.write_test("webshell/gcpgpu-controller.json", definition(resource="pw://alvaro/gcpgpu"))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "off"}}})
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        off = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(off["status"], "skip")
        self.assertIn("is off", off["error"])
        self.assertIsNone(off["run_slug"])
        self.assertEqual(off["duration_s"], 0)
        self.assertEqual(self.artifact_dirs(CONTROLLER), [])
        missing = self.records("activate.parallel.works/alvaro/webshell/gcpgpu-controller")[0]["outcome"]
        self.assertEqual(missing["status"], "skip")
        self.assertIn("not listed", missing["error"])
        self.assertFalse(any(" run --trust " in c for c in self.calls()))

    def test_kubernetes_gate(self):
        data = definition(workflow_name="mlflow", path="workflows/mlflow/yamls/k8s.yaml")
        data["inputs"] = {"k8s": {"cluster": "k3sgpu", "namespace": "probe"}}
        self.write_test("mlflow/k3sgpu.json", data)
        self.configure({"kube": ["cloudinfra"]})
        code, _ = self.run_suite()
        outcome = self.records("activate.parallel.works/alvaro/mlflow/k3sgpu")[0]["outcome"]
        self.assertEqual(outcome["status"], "skip")
        self.assertIn("pw kube ls", outcome["error"])

    def test_run_error_records_failure_and_errors_txt(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "final": {WEBSHELL: "error"}, "errors": {WEBSHELL: "Endpoint webshell-x is unhealthy"}})
        code, out = self.run_suite()
        self.assertEqual(code, 1)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["failed_at"], "run")
        self.assertEqual(outcome["error"], "run error: Endpoint webshell-x is unhealthy")
        self.assertEqual(outcome["cleanup"], "ok")
        art = self.results_dir / CONTROLLER / self.artifact_dirs(CONTROLLER)[0]
        self.assertTrue((art / "errors.txt").exists())
        self.assertIn("Endpoint webshell-x is unhealthy", (art / "errors.txt").read_text())
        self.assertIn("FAIL", out)

    def test_launch_failure(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "launch": {WEBSHELL: "fail:Missing required fields: Cleanup Script Path"}})
        code, _ = self.run_suite()
        self.assertEqual(code, 1)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["failed_at"], "launch")
        self.assertEqual(outcome["error"], "Missing required fields: Cleanup Script Path")
        self.assertIsNone(outcome["run_slug"])
        self.assertEqual(self.artifact_dirs(CONTROLLER), [artifact_dir_name(outcome["started_at"], None)])
        self.assertEqual(len([c for c in self.calls() if " run --trust " in c]), 1)

    def test_transient_launch_failure_is_retried(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "launch": {WEBSHELL: "transient:1"}})
        with mock.patch.object(runner, "LAUNCH_BACKOFF_S", (0, 0)):
            code, _ = self.run_suite()
        self.assertEqual(code, 0)
        self.assertEqual(self.records(CONTROLLER)[0]["outcome"]["status"], "pass")
        self.assertEqual(len([c for c in self.calls() if " run --trust " in c]), 2)

    def test_timeout_cancels_run(self):
        data = definition()
        data["timeout_s"] = 3
        self.write_test("webshell/gcpsmall-controller.json", data)
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "final": {WEBSHELL: "hang"}})
        code, _ = self.run_suite()
        self.assertEqual(code, 1)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual((outcome["status"], outcome["failed_at"]), ("fail", "run"))
        self.assertIn("timeout after 3s; run canceled", outcome["error"])
        self.assertTrue(any(c.startswith("--platform-host activate.parallel.works workflows runs cancel") for c in self.calls()))

    def test_http_expect_pass_and_fail(self):
        self.write_test("webshell/gcpsmall-controller.json", definition(http_expect=[200, 302]))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "endpoint": {WEBSHELL: "webshell"}})
        with mock.patch.object(runner, "http_status", return_value=302):
            code, _ = self.run_suite()
        self.assertEqual(code, 0)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual((outcome["status"], outcome["http"]), ("pass", 302))
        with mock.patch.object(runner, "http_status", return_value=503), mock.patch.object(runner, "HTTP_ATTEMPTS", 1):
            code, _ = self.run_suite()
        self.assertEqual(code, 1)
        outcome = self.records(CONTROLLER)[1]["outcome"]
        self.assertEqual((outcome["status"], outcome["failed_at"], outcome["http"]), ("fail", "http", 503))
        self.assertIn("expected 200 or 302", outcome["error"])
        self.assertEqual(outcome["cleanup"], "ok")

    def test_http_expect_without_endpoint_fails_at_endpoint(self):
        self.write_test("webshell/gcpsmall-controller.json", definition(http_expect=200))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}}, "endpoint": {}})
        with mock.patch.object(runner, "ENDPOINT_LIST_ATTEMPTS", 1):
            code, _ = self.run_suite()
        self.assertEqual(code, 1)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual((outcome["status"], outcome["failed_at"]), ("fail", "endpoint"))

    def test_keep_leaves_endpoint(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "endpoint": {WEBSHELL: "webshell"}})
        code, _ = self.run_suite(keep=True)
        self.assertEqual(code, 0)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(outcome["cleanup"], "kept")
        self.assertFalse(any(c.startswith("endpoints delete") for c in self.calls()))

    def test_leftovers_and_phase(self):
        self.write_test("webshell/gcpsmall-controller.json",
                        definition(leftover_patterns=["ttyd"], warm_marker=["${HOME}/a", "${HOME}/b"]))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "endpoint": {WEBSHELL: "webshell"},
                        "ssh": {"test -e": "1\n0", "grep -c": "p0=2"}})
        with mock.patch.object(runner, "LEFTOVER_WAIT_S", 0):
            code, _ = self.run_suite()
        self.assertEqual(code, 0)
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(outcome["status"], "pass")
        self.assertEqual(outcome["phase"], "partial")
        self.assertEqual(outcome["cleanup"], "leftover")

    def test_ssh_failure_gives_unknown_cleanup_and_null_phase(self):
        self.write_test("webshell/gcpsmall-controller.json", definition(leftover_patterns=["ttyd"], warm_marker="x"))
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
                        "endpoint": {WEBSHELL: "webshell"}, "ssh_fail": True})
        code, _ = self.run_suite()
        outcome = self.records(CONTROLLER)[0]["outcome"]
        self.assertEqual(outcome["status"], "pass")
        self.assertIsNone(outcome["phase"])
        self.assertEqual(outcome["cleanup"], "unknown")

    def test_platform_and_user_selection(self):
        self.write_test("mine.json", definition())
        other = definition()
        other["user"] = "someone"
        self.write_test("other.json", other)
        code, out = self.run_suite()
        self.assertEqual(code, 0)
        self.assertIn("1 selected of 2 defined (1 for other platforms or users", out)
        self.assertFalse((self.results_dir / "activate.parallel.works/someone").exists())
        # environment variables override the pw context
        os.environ["PW_PLATFORM_HOST"] = "https://activate.parallel.works"
        os.environ["PW_USER"] = "someone"
        code, out = self.run_suite()
        self.assertIn("1 selected of 2 defined", out)
        self.assertTrue((self.results_dir / "activate.parallel.works/someone").exists())

    def test_filters_ids_and_dry_run(self):
        self.write_test("a.json", definition())
        self.write_test("b.json", definition(workflow_name="jupyterlab", path="workflows/jupyterlab/yamls/general.yaml"))
        code, out = self.run_suite(dry_run=True, filters=["jupyterlab"])
        self.assertEqual(code, 0)
        self.assertIn("1 selected of 2 defined (0 for other platforms or users, 1 filtered out", out)
        self.assertFalse(any(" run --trust " in c for c in self.calls()))
        code, out = self.run_suite(ids=["activate.parallel.works/alvaro/webshell/a"])
        self.assertEqual(code, 0)
        self.assertTrue((self.results_dir / "activate.parallel.works/alvaro/webshell/a").exists())
        self.assertFalse((self.results_dir / "activate.parallel.works/alvaro/jupyterlab/b").exists())

    def test_definition_errors_give_exit_2_but_valid_tests_run(self):
        self.write_test("ok.json", definition())
        (self.tests_dir / "bad.json").write_text("{")
        code, out = self.run_suite()
        self.assertEqual(code, 2)
        self.assertIn("definition error: bad.json", out)
        self.assertEqual(self.records("activate.parallel.works/alvaro/webshell/ok")[0]["outcome"]["status"], "pass")

    def test_records_append_and_history(self):
        self.write_test("webshell/gcpsmall-controller.json", definition())
        self.run_suite(suite_run="first")
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}}, "final": {WEBSHELL: "error"}})
        self.run_suite(suite_run="second")
        records = self.records(CONTROLLER)
        self.assertEqual([r["suite_run"] for r in records], ["first", "second"])
        self.assertEqual([r["outcome"]["status"] for r in records], ["pass", "fail"])
        self.assertEqual(len(self.artifact_dirs(CONTROLLER)), 2)

    def test_same_workflow_same_resource_runs_serially(self):
        self.write_test("a.json", definition())
        self.write_test("b.json", definition(scheduler=True))
        self.write_test("c.json", definition(workflow_name="jupyterlab", path="workflows/jupyterlab/yamls/general.yaml"))
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        starts = [line for line in out.splitlines() if " start  " in line]
        self.assertEqual(len(starts), 3)
        # the two webshell tests never overlap: the second launch happens after the first record
        lines = out.splitlines()
        first_pass = next(i for i, l in enumerate(lines) if "PASS" in l and "/webshell/" in l)
        second_launch_calls = [c for c in self.calls() if " run --trust " in c and "webshell" in c]
        self.assertEqual(len(second_launch_calls), 2)
        self.assertGreater(len(lines), first_pass)

    def test_unreachable_repo_fails_at_launch(self):
        data = definition()
        data["workflow"]["repo"] = "file://" + str(self.root / "missing-repo")
        self.write_test("t.json", data)
        code, _ = self.run_suite()
        self.assertEqual(code, 1)
        record = self.records("activate.parallel.works/alvaro/webshell/t")[0]
        self.assertEqual((record["outcome"]["status"], record["outcome"]["failed_at"]), ("fail", "launch"))
        self.assertIn("cannot fetch", record["outcome"]["error"])
        self.assertIsNone(record["workflow"]["commit"])
        self.assertFalse(any(" run --trust " in c for c in self.calls()))

    def test_bad_ref_and_missing_path_fail_at_launch(self):
        data = definition()
        data["workflow"]["ref"] = "doesnotexist"
        self.write_test("ref.json", data)
        data = definition(path="workflows/webshell/yamls/nope.yaml")
        self.write_test("path.json", data)
        code, _ = self.run_suite()
        self.assertEqual(code, 1)
        ref = self.records("activate.parallel.works/alvaro/webshell/ref")[0]["outcome"]
        self.assertEqual(ref["failed_at"], "launch")
        self.assertIn("cannot fetch", ref["error"])
        path = self.records("activate.parallel.works/alvaro/webshell/path")[0]["outcome"]
        self.assertEqual(path["failed_at"], "launch")
        self.assertIn("no workflows/webshell/yamls/nope.yaml", path["error"])

    def test_commit_and_tag_refs(self):
        data = definition()
        data["workflow"]["ref"] = self.commit
        self.write_test("commit.json", data)
        data = definition()
        data["workflow"]["ref"] = "v1.0.0"
        self.write_test("tag.json", data)
        code, _ = self.run_suite()
        self.assertEqual(code, 0)
        self.assertEqual(self.records("activate.parallel.works/alvaro/webshell/commit")[0]["workflow"]["commit"], self.commit)
        self.assertEqual(self.records("activate.parallel.works/alvaro/webshell/tag")[0]["workflow"]["commit"], self.commit)

    def test_one_checkout_per_repo_and_ref(self):
        self.write_test("a.json", definition())
        self.write_test("b.json", definition(workflow_name="jupyterlab", path="workflows/jupyterlab/yamls/general.yaml"))
        code, _ = self.run_suite()
        self.assertEqual(code, 0)
        launches = [c for c in self.calls() if " run --trust " in c]
        self.assertEqual(len(launches), 2)
        dirs = {c.split(" -i ")[1].split(" ")[1].split("/workflows/")[0] for c in launches}
        self.assertEqual(len(dirs), 1, launches)


class FormatTests(unittest.TestCase):
    def test_format_errors(self):
        text = json.dumps({"slug": "s", "status": "error", "summary": "1 job(s) failed",
                           "failedJobs": [{"name": "j", "failedSteps": [{"name": "st", "status": "error",
                                                                          "logTail": ["line"], "annotations": [{"type": "error", "message": "boom"}]}]}]})
        rendered = runner.format_errors(text)
        self.assertIn("== j > st (error)", rendered)
        self.assertIn("error: boom", rendered)
        self.assertIn("| line", rendered)
        self.assertEqual(runner.format_errors("not json"), "not json")


if __name__ == "__main__":
    unittest.main()
