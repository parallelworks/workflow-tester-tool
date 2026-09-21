"""The results tree: records.jsonl per test, artifact directories per execution.

results/<platform>/<user>/<workflow_name>/<test>/
    records.jsonl                    one line per execution, append only
    <start time>_<run slug>/         run.log, launch.json, view.json, errors.txt
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

RECORDS_FILE = "records.jsonl"
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


def read_records(path: Path) -> List[dict]:
    """Records in file order. Malformed lines are ignored."""
    records: List[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (isinstance(record, dict) and record.get("schema") == SCHEMA
                and isinstance(record.get("test"), dict) and record["test"].get("id")
                and isinstance(record.get("outcome"), dict)):
            records.append(record)
    return records


def append_record(path: Path, record: dict) -> None:
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def scan(results_dir: Path) -> Dict[str, dict]:
    """test id -> {"dir", "records", "artifacts"} for every test directory that
    holds records or artifact directories."""
    found: Dict[str, dict] = {}
    if not results_dir.is_dir():
        return found
    for test_dir in _test_dirs(results_dir):
        test_id = "/".join(test_dir.relative_to(results_dir).parts)
        if not valid_id(test_id):
            continue
        records = read_records(test_dir / RECORDS_FILE)
        artifacts = sorted((p.name for p in test_dir.iterdir() if p.is_dir()), reverse=True)
        if records or artifacts:
            found[test_id] = {"dir": test_dir, "records": records, "artifacts": artifacts}
    return found


def _test_dirs(results_dir: Path):
    level = [results_dir]
    for _ in range(4):
        level = [child for parent in level if parent.is_dir()
                 for child in sorted(parent.iterdir()) if child.is_dir()]
    return level


def running_artifacts(test_dir: Path, records: List[dict], artifacts: List[str],
                      now: Optional[float] = None) -> List[str]:
    """Artifact directories that no record accounts for and whose run.log was
    written recently: executions still in progress."""
    now = time.time() if now is None else now
    known = set()
    for record in records:
        outcome = record.get("outcome") or {}
        if outcome.get("status") != "skip" and outcome.get("started_at"):
            known.add(artifact_dir_name(outcome["started_at"], outcome.get("run_slug")))
    running = []
    for name in artifacts:
        if name in known:
            continue
        try:
            mtime = (test_dir / name / "run.log").stat().st_mtime
        except OSError:
            continue
        if now - mtime <= RUNNING_MAX_AGE_S:
            running.append(name)
    return running


def state(test_id: str, records: List[dict], running: List[str]) -> dict:
    """Current state of one test from its records."""
    current = records[-1] if records else None
    status = (current or {}).get("outcome", {}).get("status") if current else None
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
