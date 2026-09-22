"""The results tree: one directory per execution, holding its record and artifacts.

results/<platform>/<user>/<workflow_name>/<name>/
    <start time>_<run slug>/        record.json, run.log, launch.json, view.json, errors.txt
    <start time>_skip/              record.json, run.log (the test was skipped)
    <start time>_launch-failed/     record.json, run.log (the launch produced no run)

Every execution writes only its own new directory, so runners on different
machines can write to the same bucket without overwriting each other. A
directory without record.json is an execution still in progress.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

RECORD_FILE = "record.json"
SCHEMA = 1
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
ID_RE = re.compile(r"^%s(/%s){3}$" % (SEGMENT, SEGMENT))
HISTORY_LENGTH = 20
RUNNING_MAX_AGE_S = 2 * 3600


def artifact_dir_name(started_at: str, run_slug: Optional[str]) -> str:
    """2026-09-17T06:00:42Z + swift-falcon -> 2026-09-17T060042Z_swift-falcon."""
    return "%s_%s" % (started_at.replace(":", ""), run_slug or "launch-failed")


def valid_id(test_id: str) -> bool:
    return bool(ID_RE.match(test_id))


def id_parts(test_id: str) -> Dict[str, str]:
    platform, user, workflow_name, test = test_id.split("/")
    return {"platform": platform, "user": user, "workflow_name": workflow_name, "test": test}


def read_record(path: Path) -> Optional[dict]:
    """The record in a record.json, or None when missing or malformed."""
    try:
        record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if (isinstance(record, dict) and record.get("schema") == SCHEMA
            and isinstance(record.get("test"), dict) and record["test"].get("id")
            and isinstance(record.get("outcome"), dict)):
        return record
    return None


def write_record(directory: Path, record: dict) -> Path:
    """Write record.json into an execution directory, atomically."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".record-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
        fh.write("\n")
    target = directory / RECORD_FILE
    os.replace(tmp, str(target))
    return target


def execution_dirs(test_dir: Path) -> List[Path]:
    """Execution directories of a test, oldest first (names start with the time)."""
    if not test_dir.is_dir():
        return []
    return sorted((p for p in test_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.name)


def test_records(test_dir: Path) -> List[dict]:
    """Records of a test in execution order. Directories without a valid record
    are skipped."""
    records = []
    for directory in execution_dirs(test_dir):
        record = read_record(directory / RECORD_FILE)
        if record is not None:
            records.append(record)
    return records


def running_executions(test_dir: Path, now: Optional[float] = None) -> List[str]:
    """Execution directories without a record whose run.log changed recently."""
    now = time.time() if now is None else now
    running = []
    for directory in execution_dirs(test_dir):
        if (directory / RECORD_FILE).exists():
            continue
        try:
            mtime = (directory / "run.log").stat().st_mtime
        except OSError:
            continue
        if now - mtime <= RUNNING_MAX_AGE_S:
            running.append(directory.name)
    return running


def scan(results_dir: Path) -> Dict[str, dict]:
    """test id -> {"dir", "records", "artifacts", "running"} for every test
    directory that holds at least one execution directory."""
    found: Dict[str, dict] = {}
    if not results_dir.is_dir():
        return found
    for test_dir in _test_dirs(results_dir):
        test_id = "/".join(test_dir.relative_to(results_dir).parts)
        if not valid_id(test_id):
            continue
        dirs = execution_dirs(test_dir)
        if not dirs:
            continue
        found[test_id] = {
            "dir": test_dir,
            "records": test_records(test_dir),
            "artifacts": [d.name for d in reversed(dirs)],
            "running": running_executions(test_dir),
        }
    return found


def _test_dirs(results_dir: Path):
    level = [results_dir]
    for _ in range(4):
        level = [child for parent in level if parent.is_dir()
                 for child in sorted(parent.iterdir()) if child.is_dir() and not child.name.startswith(".")]
    return level


def state(test_id: str, records: List[dict], running: List[str]) -> dict:
    """Current state of one test from its records."""
    current = records[-1] if records else None
    status = current["outcome"].get("status") if current else None
    previous_status = None
    for record in reversed(records[:-1]):
        outcome = record.get("outcome") or {}
        if outcome.get("status") != "skip":
            previous_status = outcome.get("status")
            break
    change = None
    if status == "fail" and previous_status == "pass":
        change = "regression"
    elif status == "pass" and previous_status == "fail":
        change = "recovery"
    history = []
    for record in records[-HISTORY_LENGTH:]:
        outcome = record.get("outcome") or {}
        history.append({
            "status": outcome.get("status"),
            "failed_at": outcome.get("failed_at"),
            "error": outcome.get("error"),
            "started_at": outcome.get("started_at"),
            "duration_s": outcome.get("duration_s"),
            "run_slug": outcome.get("run_slug"),
            "suite_run": record.get("suite_run"),
            "commit": (record.get("workflow") or {}).get("commit"),
        })
    return {
        "id": test_id,
        "status": status,
        "running": bool(running),
        "running_artifacts": running,
        "current": current,
        "previous_status": previous_status,
        "change": change,
        "history": history,
        "record_count": len(records),
    }


def suite_runs(all_records: List[dict], limit: int = 20) -> List[dict]:
    """Per suite run: first start time and pass/fail/skip counts, newest first."""
    by_run: Dict[str, dict] = {}
    for record in all_records:
        name = record.get("suite_run") or "unknown"
        outcome = record.get("outcome") or {}
        entry = by_run.setdefault(name, {"suite_run": name, "started_at": None,
                                         "pass": 0, "fail": 0, "skip": 0, "tests": 0})
        started = outcome.get("started_at")
        if started and (entry["started_at"] is None or started < entry["started_at"]):
            entry["started_at"] = started
        entry["tests"] += 1
        status = outcome.get("status")
        if status in ("pass", "fail", "skip"):
            entry[status] += 1
    ordered = sorted(by_run.values(), key=lambda e: e["started_at"] or "", reverse=True)
    return ordered[:limit]
