"""Run PROBE tests and append one record per execution."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .definitions import Target, TestDef, load_tests
from .pw import Checkout, Endpoint, FetchError, Pw, PwError, one_line
from .results import RECORDS_FILE, SCHEMA, append_record, artifact_dir_name

TERMINAL = {"completed", "error", "canceled", "failed"}
LAUNCH_ATTEMPTS = 3
LAUNCH_BACKOFF_S = (10, 30)
MAX_POLL_ERRORS = 20
ENDPOINT_LIST_ATTEMPTS = 4
ENDPOINT_GONE_WAIT_S = 60
LEFTOVER_WAIT_S = 120
HTTP_ATTEMPTS = 3
CLEANUP_RANK = {"ok": 0, "kept": 0, "unknown": 1, "leftover": 2}

STOP = threading.Event()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def worst(a: str, b: str) -> str:
    return a if CLEANUP_RANK.get(a, 1) >= CLEANUP_RANK.get(b, 1) else b


@dataclass
class Options:
    tests_dir: Path
    results_dir: Path
    platform: Optional[str] = None
    user: Optional[str] = None
    suite_run: Optional[str] = None
    workers: int = 8
    poll_s: int = 15
    timeout_s: Optional[int] = None
    keep: bool = False
    dry_run: bool = False
    filters: List[str] = field(default_factory=list)
    ids: List[str] = field(default_factory=list)
    bucket: Optional[str] = None


class Console:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        with self.lock:
            self.stream.write("%s %s\n" % (utcnow().strftime("%H:%M:%S"), message))
            self.stream.flush()


class TestLog:
    """run.log writer; lines are buffered until the artifact directory exists."""

    def __init__(self):
        self._lines: List[str] = []
        self._fh = None
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = "%s %s" % (iso(utcnow()), message)
        with self._lock:
            if self._fh:
                self._fh.write(line + "\n")
                self._fh.flush()
            else:
                self._lines.append(line)

    def attach(self, path: Path) -> None:
        with self._lock:
            self._fh = open(path, "a", encoding="utf-8")
            for line in self._lines:
                self._fh.write(line + "\n")
            self._lines = []
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_status(url: str, token: Optional[str] = None, timeout: int = 20) -> int:
    """HTTP status of a GET on url without following redirects; 0 when no answer."""
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


class Suite:
    """State shared by the tests of one suite run."""

    def __init__(self, opts: Options, pw: Pw, console: Console):
        self.opts = opts
        self.pw = pw
        self.console = console
        self.suite_run = opts.suite_run or "probe-" + utcnow().strftime("%Y-%m-%dT%H:%MZ")
        self.pw_cli = pw.version()
        self._lock = threading.Lock()
        self._clusters: Optional[list] = None
        self._clusters_error: Optional[str] = None
        self._kube: Optional[list] = None
        self._kube_error: Optional[str] = None
        self._checkouts: Dict[Tuple[str, str], Checkout] = {}
        self._checkout_count = 0
        self._locks: Dict[Tuple, threading.Lock] = {}
        self.workdir = Path(tempfile.mkdtemp(prefix="probe-"))
        self.sync_failures = 0

    def close(self) -> None:
        shutil.rmtree(str(self.workdir), ignore_errors=True)

    def _cluster_list(self) -> Tuple[Optional[list], Optional[str]]:
        with self._lock:
            if self._clusters is None and self._clusters_error is None:
                try:
                    self._clusters = self.pw.clusters()
                except PwError as exc:
                    self._clusters_error = str(exc)
            return self._clusters, self._clusters_error

    def _kube_list(self) -> Tuple[Optional[list], Optional[str]]:
        with self._lock:
            if self._kube is None and self._kube_error is None:
                try:
                    self._kube = self.pw.kube_clusters()
                except PwError as exc:
                    self._kube_error = str(exc)
            return self._kube, self._kube_error

    def resource_state(self, target: Target) -> Tuple[str, str]:
        """(active | inactive | missing | unknown | none, detail)."""
        if not target.resource:
            return "none", "no resource in the inputs; nothing to check"
        if target.type == "kubernetes":
            clusters, error = self._kube_list()
            if clusters is None:
                return "unknown", error or "pw kube ls failed"
            if any(isinstance(c, dict) and c.get("name") == target.system for c in clusters):
                return "active", "listed by pw kube ls"
            return "missing", "kubernetes cluster %s is not listed by pw kube ls" % target.system
        clusters, error = self._cluster_list()
        if clusters is None:
            return "unknown", error or "pw cluster ls failed"
        namespace = target.namespace
        for cluster in clusters:
            if not isinstance(cluster, dict) or cluster.get("name") != target.system:
                continue
            owner = cluster.get("user") or cluster.get("namespace")
            if namespace is not None and owner not in (None, namespace):
                continue
            status = str(cluster.get("status") or "").lower()
            if status == "active":
                return "active", "status active"
            return "inactive", "resource %s is %s" % (target.resource, status or "in an unknown state")
        return "missing", "resource %s is not listed by pw cluster ls" % target.resource

    def workflow_file(self, test: TestDef) -> Tuple[Path, Optional[str]]:
        """Local path of the test's YAML at its ref, and the commit it came from.
        One shallow checkout per (repo, ref) is shared by the suite."""
        repo, ref, path = test.workflow["repo"], test.workflow["ref"], test.workflow["path"]
        key = (repo, ref)
        with self.lock(("checkout",) + key):
            with self._lock:
                checkout = self._checkouts.get(key)
            if checkout is None:
                with self._lock:
                    self._checkout_count += 1
                    dest = self.workdir / ("checkout-%d" % self._checkout_count)
                checkout = Checkout(repo, ref, dest)
                checkout.fetch()
                with self._lock:
                    self._checkouts[key] = checkout
            return checkout.file(path), checkout.commit

    def lock(self, key: Tuple) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def sync(self, test_dir: Path, artifact: Optional[Path], log) -> None:
        """Upload a test's new artifact directory and its records.jsonl to the
        bucket, the ground truth for results. The records file goes last so a
        record never points at artifacts the bucket does not hold yet."""
        if not self.opts.bucket:
            return
        base = self.opts.bucket.rstrip("/")
        prefix = "%s/%s" % (base, test_dir.relative_to(self.opts.results_dir).as_posix())
        uploads = []
        if artifact is not None and artifact.is_dir():
            uploads.append((str(artifact) + "/", "%s/%s/" % (prefix, artifact.name), True))
        uploads.append((str(test_dir / RECORDS_FILE), prefix + "/", False))
        for source, destination, recursive in uploads:
            r = self.pw.bucket_cp(source, destination, recursive)
            if r.rc != 0:
                with self._lock:
                    self.sync_failures += 1
                log("bucket sync failed: %s -> %s: %s" % (source, destination, r.one_line()))
                self.console("bucket sync failed for %s: %s" % (prefix, r.one_line(160)))
                return
        log("bucket synced: %s/" % prefix)


def format_errors(text: str) -> str:
    """Readable errors.txt from the JSON of pw workflows runs errors."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    lines = ["run %s: %s (%s)" % (data.get("slug"), data.get("status"), data.get("summary")), ""]
    for job in data.get("failedJobs") or []:
        for step in job.get("failedSteps") or []:
            lines.append("== %s > %s (%s)" % (job.get("name"), step.get("name"), step.get("status")))
            for annotation in step.get("annotations") or []:
                lines.append("   %s: %s" % (annotation.get("type"), annotation.get("message")))
            for entry in step.get("logTail") or []:
                lines.append("   | " + entry)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class TestRun:
    """One execution of one test."""

    def __init__(self, suite: Suite, test: TestDef):
        self.suite = suite
        self.test = test
        self.pw = suite.pw
        self.opts = suite.opts
        self.log = TestLog()
        self.started = utcnow()
        self.t0 = time.monotonic()
        self.target = test.target
        self.timeout_s = self.opts.timeout_s or test.timeout_s
        self.outcome = {
            "status": None, "failed_at": None, "error": None, "phase": None, "http": None,
            "cleanup": None, "run_slug": None, "endpoint": None,
            "started_at": iso(self.started), "ended_at": None, "duration_s": None,
        }
        self.commit: Optional[str] = None
        self.yaml_path: Optional[Path] = None
        self.test_dir = self.opts.results_dir / test.id
        self.art: Optional[Path] = None
        self.slug: Optional[str] = None
        self.view: dict = {}
        self.preexisting: List[str] = []
        self.poll_error = ""
        self.endpoint_list_failed = False

    def fail(self, where: str, error: str) -> None:
        self.outcome["status"] = "fail"
        if self.outcome["failed_at"] is None:
            self.outcome["failed_at"] = where
        self.outcome["error"] = one_line(error)
        self.log("FAIL at %s: %s" % (where, self.outcome["error"]))

    def execute(self) -> dict:
        test, log = self.test, self.log
        log("test %s" % test.id)
        log("definition %s" % test.path)
        log("workflow %s" % test.launch_target)
        log("target system=%s resource=%s type=%s node=%s" % (
            self.target.system, self.target.resource, self.target.type, self.target.node))
        try:
            state, detail = self.suite.resource_state(self.target)
            log("resource check: %s (%s)" % (state, detail))
            if state in ("inactive", "missing"):
                self.outcome["status"] = "skip"
                self.outcome["error"] = detail
                return self.finish()
            self.make_artifact_dir()
            try:
                yaml_path, self.commit = self.suite.workflow_file(test)
            except FetchError as exc:
                self.fail("launch", str(exc))
                self.rename_artifact_dir(None)
                return self.finish()
            log("workflow file %s (commit %s)" % (yaml_path, self.commit or "unknown"))
            self.yaml_path = yaml_path
            with self.suite.lock((self.target.resource, test.workflow_name)):
                if STOP.is_set():
                    self.fail("launch", "runner interrupted before launch")
                    self.rename_artifact_dir(None)
                    return self.finish()
                self.check_phase()
                self.snapshot_processes()
                if not self.launch():
                    return self.finish()
                status = self.poll()
                self.verdict(status)
                endpoints = self.find_endpoints()
                self.check_http(endpoints)
                self.set_ended()
            self.teardown(endpoints)
            return self.finish()
        except Exception as exc:  # a bug must still leave a record behind
            log("internal error: %r" % (exc,))
            self.fail("run" if self.slug else "launch", "internal error: %s" % exc)
            return self.finish()

    # -- artifacts ---------------------------------------------------------

    def make_artifact_dir(self) -> None:
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.art = self.test_dir / artifact_dir_name(self.outcome["started_at"], "pending")
        self.art.mkdir(exist_ok=True)
        self.log.attach(self.art / "run.log")

    def rename_artifact_dir(self, slug: Optional[str]) -> None:
        target = self.test_dir / artifact_dir_name(self.outcome["started_at"], slug)
        if self.art is None or target == self.art:
            return
        self.log.close()
        os.rename(str(self.art), str(target))
        self.art = target
        self.log.attach(target / "run.log")

    def save(self, name: str, text: str) -> None:
        if self.art is not None:
            (self.art / name).write_text(text, encoding="utf-8")

    # -- before launch -----------------------------------------------------

    def _login_node(self) -> bool:
        return bool(self.target.resource) and self.target.type == "cluster"

    def check_phase(self) -> None:
        markers = self.test.warm_marker
        if not markers or not self._login_node():
            return
        script = "; ".join('test -e "%s" && echo 1 || echo 0' % m for m in markers)
        r = self.pw.ssh(self.target.resource, script, timeout=120)
        flags = [w for w in r.out.split() if w in ("0", "1")]
        if r.rc != 0 or len(flags) != len(markers):
            self.log("warm marker check failed: %s" % (r.one_line() or "unexpected output"))
            return
        present = flags.count("1")
        self.outcome["phase"] = ("warm" if present == len(markers)
                                 else "cold" if present == 0 else "partial")
        self.log("phase %s (%d of %d markers present)" % (self.outcome["phase"], present, len(markers)))

    def snapshot_processes(self) -> None:
        if not self.test.leftover_patterns or not self._login_node():
            return
        r = self.pw.ssh(self.target.resource, "ps -u $USER -o pid=", timeout=120)
        if r.rc == 0:
            self.preexisting = [t for t in r.out.split() if t.isdigit()]
            self.log("%d processes present before launch" % len(self.preexisting))
        else:
            self.log("process snapshot failed: %s" % r.one_line())

    # -- launch and poll ---------------------------------------------------

    def launch(self) -> bool:
        name = "probe: %s" % self.test.id
        target = str(self.yaml_path)
        self.log("launch: pw workflows run --trust %s --name '%s' (%s)" % (target, name, self.test.launch_target))
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            try:
                run = self.pw.launch(target, self.test.inputs, name)
            except PwError as exc:
                self.log("launch attempt %d/%d failed: %s" % (attempt, LAUNCH_ATTEMPTS, exc))
                if not exc.transient or attempt == LAUNCH_ATTEMPTS or STOP.is_set():
                    self.fail("launch", str(exc))
                    self.rename_artifact_dir(None)
                    return False
                STOP.wait(LAUNCH_BACKOFF_S[min(attempt - 1, len(LAUNCH_BACKOFF_S) - 1)])
                continue
            self.slug = str(run["slug"])
            self.outcome["run_slug"] = self.slug
            self.rename_artifact_dir(self.slug)
            self.save("launch.json", json.dumps(run, indent=2))
            self.log("run %s launched (id %s)" % (self.slug, run.get("id")))
            return True
        return False

    def poll(self) -> str:
        deadline = self.t0 + self.timeout_s
        last = None
        errors = 0
        while True:
            if STOP.is_set():
                return "interrupted"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            if STOP.wait(max(1.0, min(float(self.opts.poll_s), remaining))):
                return "interrupted"
            try:
                self.view = self.pw.view(self.slug)
                errors = 0
            except PwError as exc:
                errors += 1
                self.poll_error = str(exc)
                self.log("status check failed (%d/%d): %s" % (errors, MAX_POLL_ERRORS, exc))
                if errors >= MAX_POLL_ERRORS:
                    return "poll_error"
                continue
            status = str(self.view.get("status") or "").lower()
            jobs = {k: v.get("status") for k, v in (self.view.get("executedJobs") or {}).items()
                    if isinstance(v, dict)}
            if (status, jobs) != last:
                self.log("run %s: %s %s" % (self.slug, status or "?", json.dumps(jobs, sort_keys=True)))
                last = (status, jobs)
            if status in TERMINAL:
                return status

    def verdict(self, status: str) -> None:
        if status == "completed":
            self.outcome["status"] = "pass"
            self.log("run %s completed" % self.slug)
        elif status == "timeout":
            r = self.pw.cancel(self.slug)
            self.log("timeout after %ds; cancel: rc=%d %s" % (self.timeout_s, r.rc, r.one_line(120)))
            self.fail("run", "timeout after %ds; run canceled" % self.timeout_s)
        elif status == "interrupted":
            r = self.pw.cancel(self.slug)
            self.log("interrupted; cancel: rc=%d %s" % (r.rc, r.one_line(120)))
            self.fail("run", "runner interrupted; run canceled")
        elif status == "poll_error":
            self.fail("run", "could not read the run status: %s" % self.poll_error)
        else:
            summary, text = self.pw.errors(self.slug)
            self.save("errors.txt", format_errors(text))
            self.fail("run", "run %s: %s" % (status, summary))
        if self.view:
            self.save("view.json", json.dumps(self.view, indent=2))

    def set_ended(self) -> None:
        ended = utcnow()
        self.outcome["ended_at"] = iso(ended)
        self.outcome["duration_s"] = max(0, int(round((ended - self.started).total_seconds())))

    # -- endpoints ---------------------------------------------------------

    def find_endpoints(self) -> List[Endpoint]:
        """Endpoints the run registered: every name ending in -<slug>. A run that
        completed has already seen its endpoint listed, so one listing is enough
        unless http_expect needs it."""
        suffix = "-%s" % self.slug
        expect_some = self.test.http_expect is not None and self.outcome["status"] == "pass"
        attempts = ENDPOINT_LIST_ATTEMPTS if expect_some else 1
        for attempt in range(1, attempts + 1):
            try:
                listed = self.pw.endpoints()
            except PwError as exc:
                self.log("endpoint listing failed: %s" % exc)
                self.endpoint_list_failed = True
                return []
            found = [e for e in listed if e.name.endswith(suffix)]
            if found or attempt == attempts:
                self.log("endpoints named *%s: %s" % (suffix, ", ".join(e.name for e in found) or "none"))
                return found
            STOP.wait(5)
        return []

    def check_http(self, endpoints: List[Endpoint]) -> None:
        if self.test.http_expect is None or self.outcome["status"] != "pass":
            return
        if not endpoints:
            self.fail("endpoint", "run completed but no endpoint named *-%s is listed" % self.slug)
            return
        url = endpoints[0].url
        if url.startswith("/"):
            url = "https://%s%s" % (self.test.platform, url)
        shown = url.split("?")[0]
        token = os.environ.get("PW_API_KEY") or None
        if not token:
            self.log("PW_API_KEY is not set; probing %s anonymously" % shown)
        code = 0
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            code = http_status(url, token)
            self.log("HTTP %s from %s (attempt %d)" % (code, shown, attempt))
            if code in self.test.http_expect or STOP.is_set():
                break
            STOP.wait(5)
        self.outcome["http"] = code or None
        if code not in self.test.http_expect:
            self.fail("http", "HTTP %s from %s, expected %s" % (
                code, shown, " or ".join(str(c) for c in self.test.http_expect)))

    def teardown(self, endpoints: List[Endpoint]) -> None:
        self.outcome["endpoint"] = ",".join(e.name for e in endpoints) or None
        if self.opts.keep:
            self.outcome["cleanup"] = "kept" if endpoints else "ok"
            self.log("cleanup %s (--keep)" % self.outcome["cleanup"])
            return
        cleanup = "unknown" if self.endpoint_list_failed else "ok"
        for endpoint in endpoints:
            r = self.pw.delete_endpoint(endpoint.name)
            self.log("endpoints delete %s: rc=%d %s" % (endpoint.name, r.rc, r.one_line(120)))
        if endpoints:
            deadline = time.monotonic() + ENDPOINT_GONE_WAIT_S
            while True:
                try:
                    remaining = [e.name for e in self.pw.endpoints() if e.name.endswith("-%s" % self.slug)]
                except PwError as exc:
                    self.log("endpoint listing failed: %s" % exc)
                    cleanup = worst(cleanup, "unknown")
                    break
                if not remaining:
                    self.log("endpoints gone")
                    break
                if time.monotonic() > deadline:
                    self.log("endpoints still listed after %ds: %s" % (ENDPOINT_GONE_WAIT_S, remaining))
                    cleanup = worst(cleanup, "leftover")
                    break
                STOP.wait(5)
        cleanup = worst(cleanup, self.check_leftovers())
        self.outcome["cleanup"] = cleanup
        self.log("cleanup %s" % cleanup)

    def check_leftovers(self) -> str:
        patterns = self.test.leftover_patterns
        if not patterns or not self._login_node():
            return "ok"
        skip = ",".join(self.preexisting)
        # Processes that predate the launch cannot be leftovers. The remote shell's
        # own command line contains these patterns, so each grep brackets its first
        # character and cannot match itself.
        ps = ("ps -u $USER -o pid=,args= | awk -v skip='%s' "
              "'BEGIN {split(skip, a, \",\"); for (i in a) s[a[i]]} !($1 in s) {$1=\"\"; sub(/^ +/, \"\"); print}'"
              % skip)
        checks = ["echo p%d=$(%s | grep -c -- '[%s]%s')" % (i, ps, p[0], p[1:].replace("'", "'\\''"))
                  for i, p in enumerate(patterns)]
        names = {"p%d" % i: "process:" + p for i, p in enumerate(patterns)}
        if self.target.node == "compute":
            checks.append("echo squeue=$( (command -v squeue >/dev/null && squeue -h -u $USER) 2>/dev/null | wc -l)")
            checks.append("echo qstat=$( (command -v qstat >/dev/null && qstat -u $USER) 2>/dev/null | grep -c '^[0-9]')")
            names.update({"squeue": "slurm job", "qstat": "pbs job"})
        command = "; ".join(checks)
        deadline = time.monotonic() + LEFTOVER_WAIT_S
        while True:
            r = self.pw.ssh(self.target.resource, command, timeout=120)
            if r.rc != 0:
                self.log("leftover check failed: %s" % r.one_line())
                return "unknown"
            counts = dict(re.findall(r"^(\S+)=(\d+)\s*$", r.out, re.M))
            if not counts:
                self.log("leftover check returned nothing usable: %s" % r.one_line())
                return "unknown"
            left = [names.get(k, k) for k, v in counts.items() if int(v) > 0]
            if not left:
                self.log("no leftovers")
                return "ok"
            if time.monotonic() > deadline or STOP.is_set():
                self.log("leftovers after %ds: %s" % (LEFTOVER_WAIT_S, ", ".join(left)))
                return "leftover"
            self.log("waiting for cleanup: %s" % ", ".join(left))
            STOP.wait(15)

    # -- record ------------------------------------------------------------

    def finish(self) -> dict:
        if self.outcome["ended_at"] is None:
            self.set_ended()
        record = {
            "schema": SCHEMA,
            "suite_run": self.suite.suite_run,
            "pw_cli": self.suite.pw_cli,
            "test": {"id": self.test.id, "workflow_name": self.test.workflow_name},
            "workflow": {"repo": self.test.workflow["repo"], "path": self.test.workflow["path"],
                         "ref": self.test.workflow["ref"], "commit": self.commit},
            "target": {"platform": self.test.platform, "user": self.test.user,
                       "system": self.target.system, "resource": self.target.resource,
                       "type": self.target.type, "node": self.target.node},
            "outcome": dict(self.outcome),
        }
        self.test_dir.mkdir(parents=True, exist_ok=True)
        append_record(self.test_dir / RECORDS_FILE, record)
        self.log("record: %s" % json.dumps(record["outcome"]))
        self.suite.sync(self.test_dir, self.art, self.log)
        self.log.close()
        return record


def install_signal_handlers(console: Console) -> None:
    def handler(signum, _frame):
        if STOP.is_set():
            os._exit(130)
        console("signal %d received: canceling active runs and stopping" % signum)
        STOP.set()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, handler)
    except ValueError:  # not in the main thread
        pass


def execute_one(suite: Suite, test: TestDef, console: Console) -> Optional[dict]:
    if STOP.is_set():
        console("not started %s (runner interrupted)" % test.id)
        return None
    console("start  %s  ->  %s" % (test.id, test.target.resource or "no resource"))
    record = TestRun(suite, test).execute()
    outcome = record["outcome"]
    console("%-6s %s  %ss  %s%s" % (
        outcome["status"].upper(), test.id, outcome["duration_s"], outcome["run_slug"] or "-",
        ("  " + outcome["error"]) if outcome["error"] else ""))
    return record


def clean_host(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.split("/")[0].strip() or None


def run_suite(opts: Options, console: Optional[Console] = None) -> int:
    """Exit code: 0 all pass or skip, 1 any fail, 2 definition or setup errors."""
    console = console or Console()
    STOP.clear()
    tests, errors = load_tests(opts.tests_dir)
    for error in errors:
        console("definition error: %s" % error)

    platform = clean_host(opts.platform or os.environ.get("PW_PLATFORM_HOST"))
    user = (opts.user or os.environ.get("PW_USER") or "").strip() or None
    if not platform or not user:
        context_user, context_host = Pw().context()
        platform = platform or clean_host(context_host)
        user = user or context_user
    if not platform or not user:
        console("cannot determine the platform and user: pass --platform and --user, "
                "set PW_PLATFORM_HOST and PW_USER, or run pw auth")
        return 2

    mine = [t for t in tests if t.platform == platform and t.user == user]
    selected = mine
    if opts.ids:
        wanted = set(opts.ids)
        selected = [t for t in selected if t.id in wanted]
    if opts.filters:
        selected = [t for t in selected if t.matches(opts.filters)]

    pw = Pw(platform)
    suite = Suite(opts, pw, console)
    console("PROBE %s  suite %s  platform %s  user %s  pw %s" % (
        __version__, suite.suite_run, platform, user, suite.pw_cli or "unknown"))
    console("tests: %d selected of %d defined (%d for other platforms or users, %d filtered out, %d invalid)" % (
        len(selected), len(tests), len(tests) - len(mine), len(mine) - len(selected), len(errors)))
    if opts.dry_run or not selected:
        suite.close()
    if opts.dry_run:
        for test in selected:
            console("  %s  ->  %s  on %s (%s)" % (test.id, test.launch_target,
                                                  test.target.resource or "no resource",
                                                  test.target.node or test.target.type or "-"))
        return 2 if errors else 0
    if not selected:
        console("nothing to run")
        return 2 if errors else 0

    console("results: %s%s" % (opts.results_dir, ("  bucket: " + opts.bucket) if opts.bucket else "  (no bucket: results stay local)"))
    install_signal_handlers(console)
    records: List[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
            futures = [pool.submit(execute_one, suite, test, console) for test in selected]
            for future in futures:
                record = future.result()
                if record:
                    records.append(record)
    finally:
        suite.close()

    counts = {"pass": 0, "fail": 0, "skip": 0}
    for record in records:
        counts[record["outcome"]["status"]] = counts.get(record["outcome"]["status"], 0) + 1
    console("summary: %d pass, %d fail, %d skip of %d selected (suite %s)" % (
        counts["pass"], counts["fail"], counts["skip"], len(selected), suite.suite_run))
    for record in records:
        outcome = record["outcome"]
        if outcome["status"] == "fail":
            console("  FAIL %s  at %s: %s" % (record["test"]["id"], outcome["failed_at"], outcome["error"]))
    if suite.sync_failures:
        console("%d bucket sync failure(s): the bucket is missing results of this run" % suite.sync_failures)
    if errors or suite.sync_failures:
        return 2
    return 1 if counts["fail"] else 0
