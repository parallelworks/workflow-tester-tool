#!/usr/bin/env python3
"""
PW Workflow Testing Framework

Test files live under:
  <platform>/<user>/workflows/<workflow>/<test-name>.json   → runs to completion
  <platform>/<user>/sessions/<workflow>/<test-name>.json    → starts a session; cancels when healthy

Each JSON file contains the workflow inputs exactly as the pw CLI expects them.
Outputs are written to a mirrored tree under --output-dir (default: ./output/).

Usage:
  python run_tests.py                                              # run all tests
  python run_tests.py --platform activate.parallel.works          # filter by platform
  python run_tests.py --platform <p> --user <u>                   # filter by user
  python run_tests.py --platform <p> --user <u> --kind sessions   # only session tests
  python run_tests.py --platform <p> --user <u> --workflow <w>    # filter by workflow
  python run_tests.py --test-file <path>                          # single test file
"""

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TESTS_DIR = Path(__file__).parent


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to `default`."""
    try:
        val = int(os.environ.get(name, ""))
        return val if val > 0 else default
    except ValueError:
        return default


# Workflow tests: poll until the run reaches a terminal status.
# Timing is env-overridable so test harnesses can run without real waits.
POLL_INTERVAL = _env_int("PW_TEST_POLL_INTERVAL", 5)
MAX_WAIT = _env_int("PW_TEST_MAX_WAIT", 3600)
MAX_POLL_ERRORS = _env_int("PW_TEST_MAX_POLL_ERRORS", 10)

# Session tests: poll until a healthy session appears, then cancel
SESSION_POLL_INTERVAL = _env_int("PW_TEST_SESSION_POLL_INTERVAL", 10)
SESSION_MAX_WAIT = _env_int("PW_TEST_SESSION_MAX_WAIT", 1800)  # 30-min ceiling (cluster boot is slow)
SESSION_HEALTHY_STATUS = "running"

# Launch retry on 409 Conflict (platform serialises same-workflow runs)
LAUNCH_RETRIES = 5
LAUNCH_BACKOFF = 10           # seconds; doubles on each retry

TERMINAL = {"completed", "error", "canceled", "failed"}
KINDS = ("workflows", "sessions")

# ── Resource (cluster) status gate ────────────────────────────────────────────
# Before launching a test we check that its target compute resource is on, using
# `pw cluster ls <uri>`. Tests whose resource is off are reported as "skipped"
# rather than "failed" so an idle GPU server doesn't pollute the dashboard.
#
# `pw cluster ls --status` documents the vocabulary as active / off / failed
# (with `on` an alias for `active`). We normalise the per-cluster status string
# against these sets; anything we don't recognise is treated as indeterminate
# and the test is launched anyway (with a warning) so a glitch never silently
# skips everything.
CLUSTER_STATUS_FIELDS = ("status", "provisionStatus", "state")
CLUSTER_ON_STATES = {"active", "on", "running", "ready", "provisioned", "up", "healthy"}
CLUSTER_OFF_STATES = {
    "off", "stopped", "inactive", "disabled", "deleted", "deleting",
    "failed", "error", "terminated", "stopping", "down",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJ]")

# ── Active run tracking (for signal-handler cancellation) ─────────────────────

_active: dict[str, "TestCase"] = {}
_active_lock = threading.Lock()


def _track(slug: str, test: "TestCase") -> None:
    with _active_lock:
        _active[slug] = test


def _untrack(slug: str) -> None:
    with _active_lock:
        _active.pop(slug, None)


def _cancel_all_active() -> None:
    with _active_lock:
        items = list(_active.items())
    if not items:
        return
    print(f"\nCanceling {len(items)} active platform run(s)...", flush=True)
    for slug, test in items:
        rc, _, err = _cmd(
            ["pw", "--platform-host", test.platform,
             "workflows", "runs", "cancel", slug],
            timeout=30,
        )
        print(f"  {slug}: {'ok' if rc == 0 else err[:100]}", flush=True)


def _setup_signal_handlers() -> None:
    def _handler(_sig, _frame):
        _cancel_all_active()
        os._exit(1)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hms() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    platform: str
    user: str
    kind: str        # "workflows" | "sessions"
    workflow: str
    test_name: str
    inputs_file: Path

    @property
    def id(self) -> str:
        return f"{self.platform}/{self.user}/{self.kind}/{self.workflow}/{self.test_name}"

    def output_dir(self, base: Path) -> Path:
        return base / self.platform / self.user / self.kind / self.workflow / self.test_name


@dataclass
class TestResult:
    test: TestCase
    slug: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
    duration: float = 0.0
    output_dir: Optional[Path] = None


# ── Logger ────────────────────────────────────────────────────────────────────

class RunLogger:
    def __init__(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = log_path.open("w", encoding="utf-8")

    def log(self, msg: str) -> None:
        self._fh.write(f"[{hms()}] {msg}\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        self._fh.write(f"\n{'─' * 60}\n{title}\n{'─' * 60}\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ── Shell helper ──────────────────────────────────────────────────────────────

def _cmd(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """
    Run a subprocess. Returns (returncode, stdout, stderr) with ANSI stripped.
    Never raises — all errors become rc=-1 with the reason in stderr.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (
            proc.returncode,
            strip_ansi(proc.stdout.strip()),
            strip_ansi(proc.stderr.strip()),
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"Executable not found: {args[0]}"
    except Exception as exc:
        return -1, "", f"Unexpected subprocess error: {exc}"


def _rel(path: Path) -> str:
    """Path relative to TESTS_DIR for display, or the absolute path if outside it."""
    try:
        return f"{path.relative_to(TESTS_DIR)}"
    except ValueError:
        return str(path)


# ── Resource (cluster) status gate ────────────────────────────────────────────

def _resource_uri(test: "TestCase") -> Optional[str]:
    """Return the test's target compute-resource URI (pw://…), or None."""
    try:
        inputs = json.loads(test.inputs_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    resource = inputs.get("resource") if isinstance(inputs, dict) else None
    if isinstance(resource, dict):
        uri = resource.get("uri")
        if isinstance(uri, str) and uri.startswith("pw://"):
            return uri
    return None


def _find_cluster_record(out: str, uri: str) -> Optional[dict]:
    """Locate the cluster record matching `uri` in `pw cluster ls -o json` output.

    `pw cluster ls` records carry `name`/`user` rather than a `uri` field, so we
    match on the name (last URI segment) and, when present, the namespace/user.
    """
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None  # CLI prints a non-JSON "No clusters found" notice
    records = [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]

    name = uri.rstrip("/").rsplit("/", 1)[-1]
    namespace = uri.split("/")[2] if uri.count("/") >= 3 else None
    for rec in records:
        if rec.get("uri") == uri:
            return rec
        if rec.get("name") == name and (namespace is None
                                        or rec.get("user") in (None, namespace)
                                        or rec.get("namespace") in (None, namespace)):
            return rec
    # A single record returned for a specific-URI query is unambiguous.
    return records[0] if len(records) == 1 else None


def _cluster_status_label(rec: dict) -> str:
    """Human-readable status for a cluster record (first non-empty known field)."""
    if rec.get("currentlyProvisioning") is True:
        return "provisioning"
    for field in CLUSTER_STATUS_FIELDS:
        val = rec.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
        if isinstance(val, dict):  # e.g. {"state": "off"}
            for nested in ("status", "state", "phase"):
                nv = val.get(nested)
                if isinstance(nv, str) and nv.strip():
                    return nv.strip().lower()
    return ""


def _resource_gate(test: "TestCase", logger: "RunLogger") -> Optional[str]:
    """
    Check that the test's target resource is on before launching.

    Returns a skip-reason string when the resource is confirmed *not* on, or
    None to proceed. When the status cannot be determined we log a warning and
    return None (proceed) so transient lookup failures never silently skip tests.
    """
    uri = _resource_uri(test)
    if not uri:
        logger.log("No pw:// compute resource in inputs — resource gate not applicable")
        return None

    logger.log(f"Checking target resource {uri} is on (pw cluster ls)...")
    rc, out, err = _cmd([
        "pw", "--platform-host", test.platform,
        "cluster", "ls", uri, "-o", "json",
    ], timeout=30)

    if rc != 0:
        logger.log(f"  WARNING: cluster lookup failed (rc={rc}): {(err or out)[:200]}")
        logger.log("  → status indeterminate; launching anyway")
        return None

    rec = _find_cluster_record(out, uri)
    if rec is None:
        logger.log(f"  WARNING: resource {uri} not found in 'pw cluster ls' output")
        logger.log("  → status indeterminate; launching anyway")
        return None

    status = _cluster_status_label(rec)
    if status in CLUSTER_ON_STATES:
        logger.log(f"  resource is ON (status={status}) — proceeding")
        return None
    if status in CLUSTER_OFF_STATES or status == "provisioning":
        logger.log(f"  resource is NOT on (status={status}) — SKIPPING test")
        return f"Resource {uri} is not on (status={status})"

    logger.log(f"  WARNING: unrecognised resource status '{status or '∅'}' — launching anyway")
    return None


# ── Test discovery ────────────────────────────────────────────────────────────

def discover_tests(
    platform: Optional[str] = None,
    user: Optional[str] = None,
    kind: Optional[str] = None,
    workflow: Optional[str] = None,
    test_file: Optional[str] = None,
) -> list[TestCase]:
    """Return test cases matching the given filters."""
    if test_file:
        p = Path(test_file)
        if not p.is_absolute():
            p = TESTS_DIR / p
        p = p.resolve()

        if not p.exists():
            raise ValueError(f"Test file not found: {p}")
        if p.suffix != ".json":
            raise ValueError(f"Test file must be a .json file: {p}")

        try:
            rel = p.relative_to(TESTS_DIR)
            parts = rel.parts
        except ValueError:
            parts = p.parts[-5:]

        if len(parts) != 5:
            raise ValueError(
                f"Expected <platform>/<user>/workflows|sessions/<workflow>/<test>.json, got: {p}"
            )
        if parts[2] not in KINDS:
            raise ValueError(
                f"Directory level 3 must be 'workflows' or 'sessions', got '{parts[2]}'"
            )
        return [TestCase(
            platform=parts[0],
            user=parts[1],
            kind=parts[2],
            workflow=parts[3],
            test_name=p.stem,
            inputs_file=p,
        )]

    p_glob = platform or "*"
    u_glob = user or "*"
    k_glob = kind or "*"
    w_glob = workflow or "*"

    tests: list[TestCase] = []
    for f in sorted(TESTS_DIR.glob(f"{p_glob}/{u_glob}/{k_glob}/{w_glob}/*.json")):
        parts = f.relative_to(TESTS_DIR).parts
        if len(parts) != 5:
            continue
        k = parts[2]
        if k not in KINDS:
            continue
        tests.append(TestCase(
            platform=parts[0],
            user=parts[1],
            kind=k,
            workflow=parts[3],
            test_name=f.stem,
            inputs_file=f,
        ))
    return tests


# ── Shared helpers ────────────────────────────────────────────────────────────

def _save(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_result(result: TestResult, started_at: str) -> None:
    if result.output_dir is None:
        return
    _save(result.output_dir / "result.json", json.dumps({
        "test": result.test.id,
        "kind": result.test.kind,
        "status": result.status,
        "slug": result.slug,
        "duration_s": round(result.duration, 2),
        "started_at": started_at,
        "error": result.error,
    }, indent=2))


def _validate_inputs(test: TestCase) -> Optional[str]:
    """Validate inputs file. Returns error message if invalid, None if OK."""
    try:
        raw = test.inputs_file.read_text(encoding="utf-8")
        json.loads(raw)
        return None
    except FileNotFoundError:
        return f"Inputs file not found: {test.inputs_file}"
    except json.JSONDecodeError as exc:
        return f"Inputs file is not valid JSON: {exc}"


def _launch(
    test: TestCase,
    result: TestResult,
    logger: RunLogger,
    out_dir: Path,
    start: float,
    started_at: str,
) -> Optional[str]:
    """
    Launch the workflow run with retry on 409.
    Returns the run slug on success, or sets result.status/error and returns None on failure.
    """
    launch_cmd = [
        "pw",
        "--platform-host", test.platform,
        "workflows", "run",
        "-i", str(test.inputs_file),
        "--name", f"pw-test: {test.id}",
        "-o", "json",
        test.workflow,
    ]
    logger.log(f"Command: {' '.join(launch_cmd)}")

    for attempt in range(1, LAUNCH_RETRIES + 1):
        if attempt > 1:
            backoff = LAUNCH_BACKOFF * (2 ** (attempt - 2))
            logger.log(f"Attempt {attempt}/{LAUNCH_RETRIES}: waiting {backoff}s after 409 Conflict...")
            time.sleep(backoff)
        else:
            logger.log(f"Attempt {attempt}/{LAUNCH_RETRIES}...")

        rc, out, err = _cmd(launch_cmd, timeout=60)
        logger.log(f"  exit={rc}")
        if err:
            logger.log(f"  stderr: {err[:400]}")

        if rc == 0:
            _save(out_dir / "launch.json", out)
            try:
                slug = json.loads(out)["run"]["slug"]
                logger.log(f"  Run launched: slug={slug}")
                _track(slug, test)
                return slug
            except (json.JSONDecodeError, KeyError) as exc:
                result.status = "launch_failed"
                result.error = f"Could not parse run slug ({exc})"
                result.duration = time.monotonic() - start
                logger.log(f"ERROR: {result.error} — response: {out[:200]}")
                _write_result(result, started_at)
                return None

        if "409" in err and attempt < LAUNCH_RETRIES:
            logger.log("  409 Conflict — will retry")
            continue

        result.status = "launch_failed"
        result.error = err or out
        result.duration = time.monotonic() - start
        logger.log(f"ERROR: Launch failed after {attempt} attempt(s)")
        _write_result(result, started_at)
        return None

    result.status = "launch_failed"
    result.error = f"Exhausted {LAUNCH_RETRIES} launch retries (repeated 409 Conflict)"
    result.duration = time.monotonic() - start
    logger.log(f"ERROR: {result.error}")
    _write_result(result, started_at)
    return None


def _cancel(test: TestCase, slug: str, logger: RunLogger) -> None:
    """Cancel a running workflow run. Logs result but never raises."""
    logger.log(f"Canceling run {slug}...")
    rc, out, err = _cmd([
        "pw", "--platform-host", test.platform,
        "workflows", "runs", "cancel", slug,
    ], timeout=30)
    logger.log(f"  cancel exit={rc}: {(out or err)[:200]}")


# ── Workflow test (runs to completion) ────────────────────────────────────────

def _run_workflow_test(
    test: TestCase,
    result: TestResult,
    out_dir: Path,
    logger: RunLogger,
    start: float,
    started_at: str,
) -> TestResult:

    logger.section("LAUNCH")
    slug = _launch(test, result, logger, out_dir, start, started_at)
    if slug is None:
        return result

    result.slug = slug
    result.status = "running"
    _write_result(result, started_at)

    logger.section("POLLING")
    poll_cmd = [
        "pw", "--platform-host", test.platform,
        "workflows", "runs", "view", "-o", "json", slug,
    ]

    consecutive_errors = 0
    elapsed = 0
    last_view_json = ""

    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        rc, out, err = _cmd(poll_cmd, timeout=30)
        if rc != 0:
            consecutive_errors += 1
            logger.log(f"Poll error #{consecutive_errors}/{MAX_POLL_ERRORS} at {elapsed}s: {err or out}")
            if consecutive_errors >= MAX_POLL_ERRORS:
                result.status = "poll_error"
                result.error = f"{MAX_POLL_ERRORS} consecutive poll failures. Last: {err or out}"
                result.duration = time.monotonic() - start
                if last_view_json:
                    _save(out_dir / "view.json", last_view_json)
                logger.log("ERROR: Aborting after too many poll errors")
                _write_result(result, started_at)
                return result
            continue

        consecutive_errors = 0
        last_view_json = out

        try:
            status = json.loads(out).get("status", "").lower()
        except json.JSONDecodeError as exc:
            logger.log(f"Poll at {elapsed}s: invalid JSON ({exc})")
            continue

        logger.log(f"Poll at {elapsed}s: status={status}")

        if status not in TERMINAL:
            continue

        logger.section("RESULT")
        _save(out_dir / "view.json", out)
        result.status = status
        result.duration = time.monotonic() - start

        if status == "completed":
            logger.log(f"Run completed in {result.duration:.1f}s")
        else:
            rc2, err_out, _ = _cmd([
                "pw", "--platform-host", test.platform,
                "workflows", "runs", "errors", slug,
            ], timeout=30)
            logger.log(f"Run {status} in {result.duration:.1f}s — error details (rc={rc2}): {err_out[:400]}")
            if err_out:
                _save(out_dir / "errors.txt", err_out)
            result.error = err_out[:500] if err_out else f"Run ended with status: {status}"

        _write_result(result, started_at)
        return result

    logger.section("TIMEOUT")
    result.status = "timeout"
    result.error = f"Exceeded {MAX_WAIT}s timeout (run: {slug})"
    result.duration = time.monotonic() - start
    if last_view_json:
        _save(out_dir / "view.json", last_view_json)
    logger.log(result.error)
    _write_result(result, started_at)
    return result


# ── Session test (runs indefinitely; pass when session is healthy) ─────────────

def _run_session_test(
    test: TestCase,
    result: TestResult,
    out_dir: Path,
    logger: RunLogger,
    start: float,
    started_at: str,
) -> TestResult:

    logger.section("LAUNCH")
    slug = _launch(test, result, logger, out_dir, start, started_at)
    if slug is None:
        return result

    result.slug = slug
    result.status = "running"
    _write_result(result, started_at)

    # ── Poll until run is running ─────────────────────────────────────────────
    logger.section("WAITING FOR RUN TO START")
    run_poll_cmd = [
        "pw", "--platform-host", test.platform,
        "workflows", "runs", "view", "-o", "json", slug,
    ]

    elapsed = 0
    while elapsed < SESSION_MAX_WAIT:
        time.sleep(SESSION_POLL_INTERVAL)
        elapsed += SESSION_POLL_INTERVAL

        rc, out, err = _cmd(run_poll_cmd, timeout=30)
        if rc != 0:
            logger.log(f"Run view error at {elapsed}s: {err or out}")
            continue

        try:
            run_status = json.loads(out).get("status", "").lower()
        except json.JSONDecodeError:
            continue

        logger.log(f"Run status at {elapsed}s: {run_status}")

        if run_status in TERMINAL:
            # Run ended before a session was created
            result.status = run_status
            result.duration = time.monotonic() - start
            result.error = f"Run ended with status '{run_status}' before a healthy session appeared"
            _save(out_dir / "view.json", out)
            _, err_out, _ = _cmd([
                "pw", "--platform-host", test.platform,
                "workflows", "runs", "errors", slug,
            ], timeout=30)
            logger.log(f"Run ended early: {result.error}")
            if err_out:
                _save(out_dir / "errors.txt", err_out)
                result.error += f" — {err_out[:300]}"
            _write_result(result, started_at)
            return result

        if run_status == "running":
            logger.log("Run is running — switching to session health polling")
            break
    else:
        result.status = "timeout"
        result.error = f"Run did not reach 'running' state within {SESSION_MAX_WAIT}s"
        result.duration = time.monotonic() - start
        logger.log(f"ERROR: {result.error}")
        _cancel(test, slug, logger)
        _write_result(result, started_at)
        return result

    # ── Poll until session is healthy ─────────────────────────────────────────
    logger.section("WAITING FOR HEALTHY SESSION")
    session_cmd = [
        "pw", "--platform-host", test.platform,
        "sessions", "ls", "-o", "json",
    ]

    while elapsed < SESSION_MAX_WAIT:
        time.sleep(SESSION_POLL_INTERVAL)
        elapsed += SESSION_POLL_INTERVAL

        rc, out, err = _cmd(session_cmd, timeout=30)
        if rc != 0:
            logger.log(f"Sessions list error at {elapsed}s: {err or out}")
            continue

        try:
            sessions = json.loads(out)
        except json.JSONDecodeError:
            # CLI prints an INFO message when there are no sessions
            logger.log(f"Sessions list at {elapsed}s: no sessions yet (non-JSON response)")
            continue

        if not isinstance(sessions, list):
            logger.log(f"Sessions list at {elapsed}s: unexpected response type")
            continue

        # Find the session linked to our run
        matched = [
            s for s in sessions
            if isinstance(s.get("workflowRun"), dict)
            and s["workflowRun"].get("slug") == slug
        ]

        if not matched:
            logger.log(f"Sessions list at {elapsed}s: no session linked to {slug} yet")
            continue

        session = matched[0]
        sess_status = session.get("status", "").lower()
        healthy = session.get("healthy")  # None means platform doesn't expose this field
        logger.log(
            f"Session at {elapsed}s: name={session.get('name')} "
            f"status={sess_status} healthy={healthy}"
        )

        # Cancel as soon as status is running and healthy is not explicitly False.
        # healthy=None means the platform doesn't expose the field — trust status alone.
        if sess_status == SESSION_HEALTHY_STATUS and healthy is not False:
            # ── Success ───────────────────────────────────────────────────────
            logger.section("SESSION HEALTHY — CANCELING RUN")
            _save(out_dir / "session.json", json.dumps(session, indent=2))
            _cancel(test, slug, logger)

            result.status = "completed"
            result.duration = time.monotonic() - start
            logger.log(f"Session test passed in {result.duration:.1f}s")
            _write_result(result, started_at)
            return result

    # ── Timeout ───────────────────────────────────────────────────────────────
    logger.section("TIMEOUT")
    result.status = "timeout"
    result.error = f"Session for run '{slug}' did not become healthy within {SESSION_MAX_WAIT}s"
    result.duration = time.monotonic() - start
    logger.log(result.error)
    _cancel(test, slug, logger)
    _write_result(result, started_at)
    return result


# ── Top-level dispatcher ──────────────────────────────────────────────────────

# Stale per-run artifacts removed before each run so a skipped/failed test does
# not display leftover files restored from a previous (passing) run's bucket.
_STALE_ARTIFACTS = ("launch.json", "view.json", "errors.txt", "session.json")


def run_test(test: TestCase, output_base: Path) -> TestResult:
    out_dir = test.output_dir(output_base)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in _STALE_ARTIFACTS:
        (out_dir / name).unlink(missing_ok=True)

    result = TestResult(test=test, output_dir=out_dir)
    logger = RunLogger(out_dir / "run.log")
    start = time.monotonic()
    started_at = utc_now()

    try:
        logger.section("TEST START")
        logger.log(f"Test:        {test.id}")
        logger.log(f"Kind:        {test.kind}")
        logger.log(f"Platform:    {test.platform}")
        logger.log(f"Workflow:    {test.workflow}")
        logger.log(f"Inputs:      {test.inputs_file}")
        logger.log(f"Output dir:  {out_dir}")
        logger.log(f"Started at:  {started_at}")

        err = _validate_inputs(test)
        if err:
            result.status = "launch_failed"
            result.error = err
            result.duration = time.monotonic() - start
            logger.log(f"ERROR: {err}")
            _write_result(result, started_at)
            return result

        logger.section("RESOURCE CHECK")
        skip_reason = _resource_gate(test, logger)
        if skip_reason:
            result.status = "skipped"
            result.error = skip_reason
            result.duration = time.monotonic() - start
            _write_result(result, started_at)
            return result

        if test.kind == "sessions":
            return _run_session_test(test, result, out_dir, logger, start, started_at)
        else:
            return _run_workflow_test(test, result, out_dir, logger, start, started_at)

    except Exception as exc:
        result.status = "internal_error"
        result.error = f"Unhandled exception: {exc}"
        result.duration = time.monotonic() - start
        logger.log(f"FATAL: {result.error}")
        _write_result(result, started_at)
        return result
    finally:
        if result.slug:
            _untrack(result.slug)
        logger.close()


# ── Reporting ─────────────────────────────────────────────────────────────────

def _icon(status: str) -> str:
    if status == "completed":
        return "✓"
    if status == "skipped":
        return "⊘"
    return "✗"


def print_summary(results: list[TestResult], output_base: Path) -> bool:
    passed  = [r for r in results if r.status == "completed"]
    skipped = [r for r in results if r.status == "skipped"]
    failed  = [r for r in results if r.status not in ("completed", "skipped")]

    COL = 62
    print()
    print("=" * 96)
    print("  WORKFLOW TEST RESULTS")
    print("=" * 96)

    for r in sorted(results, key=lambda r: r.test.id):
        icon = _icon(r.status)
        label = r.test.id
        if len(label) > COL:
            label = "…" + label[-(COL - 1):]
        slug_str = r.slug or "—"
        dur_str = f"{r.duration:.1f}s"
        kind_tag = f"[{r.test.kind[:4]}]"
        print(f"  {icon}  {kind_tag}  {label:<{COL}}  {r.status:<16}  {dur_str:>8}  {slug_str}")
        if r.error:
            print(f"         ! {r.error.splitlines()[0][:105]}")
        if r.output_dir:
            print(f"         → {_rel(r.output_dir)}/")

    print("-" * 96)
    print(
        f"  Total: {len(results)}  |  Passed: {len(passed)}  |  "
        f"Failed: {len(failed)}  |  Skipped: {len(skipped)}"
    )
    print(f"  Output: {_rel(output_base)}/")
    print("=" * 96)
    # Skipped tests (resource off) are not failures — the suite passes if nothing failed.
    return not failed


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _setup_signal_handlers()
    ap = argparse.ArgumentParser(
        description="PW Workflow Testing Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python run_tests.py
              python run_tests.py --platform activate.parallel.works
              python run_tests.py --platform activate.parallel.works --user alvaro
              python run_tests.py --platform activate.parallel.works --user alvaro --kind workflows
              python run_tests.py --platform activate.parallel.works --user alvaro --kind sessions
              python run_tests.py --test-file activate.parallel.works/alvaro/workflows/\\
                  marketplace.script_submitter.latest/test1.json
        """),
    )
    ap.add_argument("--platform", help="Filter by platform host")
    ap.add_argument("--user", help="Filter by user")
    ap.add_argument("--kind", choices=list(KINDS),
                    help="Filter by test kind: workflows or sessions")
    ap.add_argument("--workflow", help="Filter by workflow name")
    ap.add_argument("--test-file", metavar="PATH", help="Run a single test JSON file")
    ap.add_argument("--output-dir", metavar="DIR", default=str(TESTS_DIR / "output"),
                    help="Root directory for test output (default: ./output/)")
    ap.add_argument("--workers", type=int, default=10,
                    help="Max parallel workers (default: 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Discover and list tests without executing them")
    args = ap.parse_args()

    output_base = Path(args.output_dir).resolve()

    try:
        tests = discover_tests(
            platform=args.platform,
            user=args.user,
            kind=args.kind,
            workflow=args.workflow,
            test_file=args.test_file,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not tests:
        print("No tests found matching the given filters.")
        sys.exit(0)

    print(f"Discovered {len(tests)} test(s):")
    for t in tests:
        print(f"  [{t.kind[:4]}]  {t.id}")

    if args.dry_run:
        sys.exit(0)

    print(f"\nRunning {len(tests)} test(s) in parallel (workers={args.workers})...")
    print(f"Output: {output_base}/\n")

    results: list[TestResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_test, t, output_base): t for t in tests}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            slug_info = f"  [{r.slug}]" if r.slug else ""
            kind_tag = f"[{r.test.kind[:4]}]"
            print(
                f"  {_icon(r.status)} {kind_tag} {r.test.id}"
                f"  →  {r.status.upper()}  ({r.duration:.1f}s){slug_info}"
            )

    success = print_summary(results, output_base)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
