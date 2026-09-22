"""Test definitions: one self-contained JSON file per test. The test id comes
from the file's fields (platform, user, workflow_name, name), never from its path."""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_TIMEOUT_S = 1800
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KNOWN_KEYS = {
    "name", "platform", "user", "workflow_name", "workflow", "timeout_s", "inputs",
    "warm_marker", "leftover_patterns", "leftover_commands", "setup",
}
WORKFLOW_KEYS = {"repo", "path", "ref"}


class DefinitionError(ValueError):
    pass


@dataclass
class Target:
    system: Optional[str]
    resource: Optional[str]
    type: Optional[str]
    node: Optional[str]

    @property
    def namespace(self) -> Optional[str]:
        """User part of a pw://<user>/<name> resource, None for pw://<name>."""
        if not self.resource or not self.resource.startswith("pw://"):
            return None
        rest = self.resource[len("pw://"):].strip("/")
        return rest.split("/")[0] if "/" in rest else None


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def derive_target(inputs: dict, user: str) -> Target:
    """Where the test runs, from the resource in its inputs."""
    cluster = inputs.get("cluster") if isinstance(inputs.get("cluster"), dict) else {}
    k8s = inputs.get("k8s") if isinstance(inputs.get("k8s"), dict) else {}
    resource = inputs.get("resource")
    if resource is None:
        resource = cluster.get("resource")
    scheduler = cluster.get("scheduler", inputs.get("scheduler"))
    node = "compute" if _truthy(scheduler) else "controller"

    if isinstance(resource, dict):
        name = resource.get("name") or None
        uri = resource.get("uri") or None
        if resource.get("type") == "kubernetes":
            return Target(name, uri or ("pw://%s" % name if name else None), "kubernetes", None)
        if uri and not name:
            name = uri.rstrip("/").rsplit("/", 1)[-1]
        if name and not uri:
            uri = "pw://%s/%s" % (user, name)
        return Target(name, uri, "cluster" if (name or uri) else None, node if (name or uri) else None)

    if isinstance(resource, str) and resource.strip():
        value = resource.strip()
        if value.startswith("pw://"):
            name = value[len("pw://"):].strip("/").rsplit("/", 1)[-1]
            return Target(name, value, "cluster", node)
        return Target(value, "pw://%s/%s" % (user, value), "cluster", node)

    k8s_cluster = k8s.get("cluster")
    if isinstance(k8s_cluster, str) and k8s_cluster.strip():
        return Target(k8s_cluster.strip(), "pw://%s" % k8s_cluster.strip(), "kubernetes", None)

    return Target(None, None, None, None)


@dataclass
class TestDef:
    path: Path
    platform: str
    user: str
    workflow_name: str
    name: str
    workflow: dict
    timeout_s: int
    inputs: dict
    warm_marker: List[str]
    leftover_patterns: List[str]
    leftover_commands: dict
    setup: Optional[str]

    @property
    def id(self) -> str:
        return "%s/%s/%s/%s" % (self.platform, self.user, self.workflow_name, self.name)

    @property
    def recommended_path(self) -> str:
        """Where the file is expected to be, relative to the tests directory."""
        return self.id + ".json"

    @property
    def launch_target(self) -> str:
        return "%s/%s@%s" % (self.workflow["repo"].rstrip("/"), self.workflow["path"].strip("/"),
                             self.workflow["ref"])

    @property
    def target(self) -> Target:
        return derive_target(self.inputs, self.user)

    def matches(self, patterns: List[str]) -> bool:
        if not patterns:
            return True
        return any(fnmatch.fnmatchcase(self.id, p) or p in self.id for p in patterns)


def _require_str(data: dict, key: str, path_like: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError("'%s' must be a non-empty string" % key)
    value = value.strip()
    if path_like and not SAFE_NAME_RE.match(value):
        raise DefinitionError("'%s' may only contain letters, digits, '.', '_' and '-': %r" % (key, value))
    return value


def load_definition(path: Path) -> TestDef:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DefinitionError("not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise DefinitionError("the file must contain a JSON object")

    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise DefinitionError("unknown key(s): %s" % ", ".join(unknown))

    name = _require_str(data, "name", path_like=True)
    platform = _require_str(data, "platform", path_like=True)
    user = _require_str(data, "user", path_like=True)
    workflow_name = _require_str(data, "workflow_name", path_like=True)

    workflow = data.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != WORKFLOW_KEYS:
        raise DefinitionError("'workflow' must be an object with exactly the keys repo, path, ref")
    for key in ("repo", "path", "ref"):
        _require_str(workflow, key)
    if "@" in workflow["repo"] and not workflow["repo"].startswith("git@"):
        raise DefinitionError("'workflow.repo' must not carry an @ref; put the ref in 'workflow.ref'")

    timeout_s = data.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise DefinitionError("'timeout_s' must be a positive integer number of seconds")

    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        raise DefinitionError("'inputs' must be an object (the pw workflows run -i payload)")

    warm_marker = data.get("warm_marker", [])
    if isinstance(warm_marker, str):
        warm_marker = [warm_marker]
    if not isinstance(warm_marker, list) or not all(isinstance(m, str) and m.strip() for m in warm_marker):
        raise DefinitionError("'warm_marker' must be a path or a list of paths")

    leftover_patterns = data.get("leftover_patterns", [])
    if not isinstance(leftover_patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in leftover_patterns):
        raise DefinitionError("'leftover_patterns' must be a list of process patterns")

    leftover_commands = data.get("leftover_commands", {})
    if not isinstance(leftover_commands, dict) or not all(
            SAFE_NAME_RE.match(k) and isinstance(v, str) and v.strip() for k, v in leftover_commands.items()):
        raise DefinitionError("'leftover_commands' must map names to shell snippets that print a count")

    setup = data.get("setup")
    if setup is not None and (not isinstance(setup, str) or not setup.strip()):
        raise DefinitionError("'setup' must be a shell snippet")

    return TestDef(
        path=path, platform=platform, user=user, workflow_name=workflow_name, name=name,
        workflow={k: workflow[k].strip() for k in WORKFLOW_KEYS}, timeout_s=timeout_s,
        inputs=inputs, warm_marker=[m.strip() for m in warm_marker],
        leftover_patterns=[p.strip() for p in leftover_patterns],
        leftover_commands={k: v.strip() for k, v in leftover_commands.items()},
        setup=setup.strip() if setup else None,
    )


def load_tests(root: Path) -> Tuple[List[TestDef], List[str]]:
    """Every *.json under root. Returns (valid tests, error lines). Files that
    share a test id are reported and dropped, as are files that fail validation."""
    tests: List[TestDef] = []
    errors: List[str] = []
    if not root.is_dir():
        return tests, ["tests directory not found: %s" % root]
    for path in sorted(p for p in root.rglob("*.json") if p.is_file()):
        try:
            tests.append(load_definition(path))
        except DefinitionError as exc:
            errors.append("%s: %s" % (_rel(path, root), exc))
    by_id = {}
    for test in tests:
        by_id.setdefault(test.id, []).append(test)
    duplicates = {tid: group for tid, group in by_id.items() if len(group) > 1}
    for tid, group in sorted(duplicates.items()):
        errors.append("duplicate test id %s: %s" % (tid, ", ".join(_rel(t.path, root) for t in group)))
    tests = [t for t in tests if t.id not in duplicates]
    return tests, errors


def location_notes(tests: List[TestDef], root: Path) -> List[str]:
    """One line per test whose file is not at the recommended location
    <platform>/<user>/<workflow_name>/<name>.json. Informational only."""
    notes = []
    for test in tests:
        actual = _rel(test.path, root).replace("\\", "/")
        if actual != test.recommended_path:
            notes.append("%s defines %s; the recommended location is %s" % (actual, test.id, test.recommended_path))
    return notes


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
