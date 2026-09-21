"""Shared fixtures for the offline self-tests: a temporary workspace with a
tests tree, a results tree, and the mock pw and git on PATH."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MOCKBIN = HERE / "mockbin"

# Paths inside the fixture workflows repository; the mock pw matches them as
# substrings of the launched YAML path.
WEBSHELL = "workflows/webshell/yamls/general.yaml"
SUBMITTER = "workflows/script_submitter/v3.6/general.yaml"
JUPYTERLAB = "workflows/jupyterlab/yamls/general.yaml"
MLFLOW = "workflows/mlflow/yamls/k8s.yaml"

# file:// URL of the fixture repository of the running test case.
REPO_URL = "file:///fixture-not-created"


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=probe", "-c", "user.email=probe@example.com"] + list(args),
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_workflows_repo(root):
    """A small git repository shaped like parallelworks/workflows, on branch
    canary with an annotated tag v1.0.0. Returns (file URL, commit)."""
    src = root / "workflows-src"
    src.mkdir()
    for rel in (WEBSHELL, SUBMITTER, JUPYTERLAB, MLFLOW):
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("jobs: {}\n# %s\n" % rel)
    git("init", "-q", str(src))
    git("symbolic-ref", "HEAD", "refs/heads/canary", cwd=str(src))
    git("add", ".", cwd=str(src))
    git("commit", "-q", "-m", "fixture", cwd=str(src))
    git("tag", "-a", "v1.0.0", "-m", "release", cwd=str(src))
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=str(src))
    return "file://" + str(src), git("rev-parse", "HEAD", cwd=str(src))


def definition(workflow_name="webshell", path="workflows/webshell/yamls/general.yaml",
               resource="pw://alvaro/gcpsmall", scheduler=False, name=None, **extra):
    """A valid definition. Without `name`, write_test() fills it from the file name."""
    data = {
        "platform": "activate.parallel.works",
        "user": "alvaro",
        "workflow_name": workflow_name,
        "workflow": {"repo": REPO_URL, "path": path, "ref": "canary"},
        "timeout_s": 60,
        "inputs": {"cluster": {"resource": resource, "scheduler": scheduler}, "service": {}},
    }
    data.update(extra)
    if name is not None:
        data["name"] = name
    return data


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="probe-selftest-"))
        self.tests_dir = self.root / "tests"
        self.results_dir = self.root / "results"
        self.state_dir = self.root / "mock-state"
        self.tests_dir.mkdir()
        self.results_dir.mkdir()
        self.state_dir.mkdir()
        self.config_path = self.root / "mock-config.json"
        global REPO_URL
        REPO_URL, self.commit = make_workflows_repo(self.root)
        self._env = dict(os.environ)
        os.environ["PATH"] = "%s%s%s" % (MOCKBIN, os.pathsep, os.environ.get("PATH", ""))
        os.environ["MOCK_PW_CONFIG"] = str(self.config_path)
        os.environ["MOCK_PW_STATE"] = str(self.state_dir)
        os.environ.pop("PW_PLATFORM_HOST", None)
        os.environ.pop("PW_USER", None)
        os.environ.pop("PW_API_KEY", None)
        self.configure({"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}}})

    def configure(self, config):
        """Write the mock pw configuration. Endpoint-kind runs register an
        endpoint on completion unless the config says otherwise, so tests do not
        wait through the runner's endpoint-listing retries."""
        config = dict(config)
        config.setdefault("endpoint", {WEBSHELL: "webshell", JUPYTERLAB: "jupyterlab-host"})
        self.config_path.write_text(json.dumps(config))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.root, ignore_errors=True)

    def write_test(self, relative, data):
        path = self.tests_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, dict) and "name" not in data:
            data = dict(data, name=path.stem)
        path.write_text(json.dumps(data, indent=2))
        return path

    def records(self, test_id):
        from probe.results import test_records
        return test_records(self.results_dir / test_id)

    def calls(self):
        path = self.state_dir / "calls.log"
        return path.read_text().splitlines() if path.exists() else []
