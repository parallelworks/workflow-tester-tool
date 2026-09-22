"""Access to the pw CLI and to git for PROBE.

Every call has a timeout and returns plain data. A failure raises PwError with a
one-line message that fits a record's outcome.error.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TRANSIENT_MARKERS = (
    " 409", " 429", " 502", " 503", " 504", "timed out", "timeout",
    "connection reset", "connection refused", "temporarily unavailable",
    "unexpected eof", "no such host", "already in use",
)


class PwError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def one_line(text: str, limit: int = 300) -> str:
    line = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    return line[:limit]


def is_transient(text: str) -> bool:
    lowered = " " + text.lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


@dataclass
class Completed:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return (self.err or self.out).strip()

    def one_line(self, limit: int = 300) -> str:
        return one_line(self.text, limit)


def sh(args: List[str], timeout: int = 120, env: Optional[dict] = None) -> Completed:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return Completed(-1, "", "%s timed out after %ss" % (args[0], timeout))
    except FileNotFoundError:
        return Completed(-2, "", "%s not found on PATH" % args[0])
    return Completed(
        proc.returncode,
        ANSI_RE.sub("", proc.stdout or "").strip(),
        ANSI_RE.sub("", proc.stderr or "").strip(),
    )


def parse_json(text: str):
    """Parse the JSON document embedded in CLI output (notices may surround it)."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    end = max(text.rfind("}"), text.rfind("]"))
    if not starts or end < 0:
        raise ValueError("no JSON in output: %r" % text[:120])
    return json.loads(text[min(starts):end + 1])


@dataclass
class Endpoint:
    name: str
    status: str
    url: str


class Pw:
    """The pw CLI for one platform. `platform` None uses the CLI's current context."""

    def __init__(self, platform: Optional[str] = None, binary: str = "pw"):
        self.platform = platform
        self.base = [binary] + (["--platform-host", platform] if platform else [])

    def _run(self, *args: str, timeout: int = 120) -> Completed:
        return sh(self.base + list(args), timeout=timeout)

    def _json(self, *args: str, timeout: int = 120, empty=None):
        r = self._run(*args, timeout=timeout)
        label = "pw " + " ".join(args[:3])
        if r.rc != 0:
            raise PwError("%s failed: %s" % (label, clean_error(r.text)), transient=is_transient(r.text))
        try:
            return parse_json(r.out)
        except ValueError as exc:
            if empty is not None:
                return empty
            raise PwError("%s: %s" % (label, exc))

    def version(self) -> Optional[str]:
        r = self._run("--version=json", timeout=30)
        try:
            return parse_json(r.out).get("releaseVersion") or None
        except (ValueError, AttributeError):
            m = re.search(r"v\d+\.\d+\.\d+\S*", r.out + " " + r.err)
            return m.group(0) if m else None

    def context(self) -> Tuple[Optional[str], Optional[str]]:
        """(user, platform host) of the current CLI context."""
        r = self._run("context", "current", timeout=30)
        for line in (r.out + "\n" + r.err).splitlines():
            m = re.search(r"(?:user:)?([^@\s:]+)@([A-Za-z0-9.-]+)", line)
            if m:
                return m.group(1), m.group(2)
        return None, None

    def clusters(self) -> list:
        return self._json("cluster", "ls", "-o", "json", empty=[])

    def kube_clusters(self) -> list:
        return self._json("kube", "ls", "-o", "json", empty=[])

    def launch(self, target: str, inputs: dict, name: str) -> dict:
        """pw workflows run; returns the run object (slug, id, status, ...)."""
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="probe-inputs-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(inputs, fh)
            r = self._run("workflows", "run", "--trust", "-o", "json",
                          "--name", name, "-i", tmp, target, timeout=180)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if r.rc != 0:
            raise PwError(clean_error(r.text) or "pw workflows run failed", transient=is_transient(r.text))
        try:
            data = parse_json(r.out)
        except ValueError as exc:
            raise PwError("pw workflows run: %s" % exc)
        run = data.get("run", data) if isinstance(data, dict) else {}
        if not isinstance(run, dict) or not run.get("slug"):
            raise PwError("pw workflows run returned no run slug")
        return run

    def view(self, slug: str) -> dict:
        data = self._json("workflows", "runs", "view", "-o", "json", slug, timeout=90)
        return data if isinstance(data, dict) else {}

    def errors(self, slug: str) -> Tuple[str, str]:
        """(one-line summary, full JSON text). The command exits non-zero for a
        failed run, which is the normal case here, so the exit code is ignored."""
        r = self._run("workflows", "runs", "errors", "-o", "json", slug, timeout=90)
        try:
            data = parse_json(r.out)
        except ValueError:
            return one_line(r.text) or "run %s failed" % slug, r.text
        messages = [a.get("message", "") for a in (data.get("annotations") or [])
                    if isinstance(a, dict) and a.get("type") == "error" and a.get("message")]
        summary = messages[0] if messages else (data.get("summary") or "run %s failed" % slug)
        return one_line(summary), json.dumps(data, indent=2)

    def cancel(self, slug: str) -> Completed:
        return self._run("workflows", "runs", "cancel", slug, timeout=90)

    def endpoints(self) -> List[Endpoint]:
        r = self._run("endpoints", "list", timeout=90)
        if r.rc != 0:
            raise PwError("pw endpoints list failed: %s" % r.one_line(), transient=is_transient(r.text))
        found = []
        for line in r.out.splitlines():
            # one endpoint per line, tab-separated; other lines are notices
            if "\t" not in line:
                continue
            parts = line.split("\t")
            name = parts[0].strip()
            if not name or name.upper() == "NAME":
                continue
            found.append(Endpoint(name, parts[1].strip(), parts[2].strip() if len(parts) > 2 else ""))
        return found

    def delete_endpoint(self, name: str) -> Completed:
        return self._run("endpoints", "delete", name, timeout=90)

    def ssh(self, resource: str, command: str, timeout: int = 120) -> Completed:
        return self._run("ssh", resource, command, timeout=timeout)

    def bucket_cp(self, source: str, destination: str, recursive: bool = False) -> Completed:
        args = ["buckets", "cp"] + (["-r"] if recursive else []) + [source, destination]
        return self._run(*args, timeout=600)

    def bucket_rm(self, prefix: str) -> Completed:
        return self._run("buckets", "rm", "-r", "-f", prefix, timeout=600)


def repo_url(repo: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*://", repo) or repo.startswith("git@"):
        return repo
    return "https://" + repo


class FetchError(Exception):
    pass


class Checkout:
    """A workflow repository at one ref, fetched shallowly into `dest`.

    Tests run from a local copy of their YAML rather than from
    <repo>/<path>@<ref>: the platform gives every inline run of a repository
    the same run slug, so concurrent tests of one repository would collide.
    A local file gets a slug of its own. The YAML's own checkout and
    subworkflow references are unaffected, so the run behaves the same.
    """

    def __init__(self, repo: str, ref: str, dest: Path):
        self.repo = repo
        self.ref = ref
        self.dest = dest
        self.commit: Optional[str] = None
        self._env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")

    def _git(self, *args: str, timeout: int = 120) -> Completed:
        return sh(["git", "-C", str(self.dest)] + list(args), timeout=timeout, env=self._env)

    def fetch(self) -> str:
        """Fetch ref (branch, tag or commit) with depth 1 and return its commit."""
        self.dest.mkdir(parents=True, exist_ok=True)
        r = sh(["git", "init", "-q", str(self.dest)], timeout=30, env=self._env)
        if r.rc != 0:
            raise FetchError("git init failed: %s" % r.one_line())
        self._git("remote", "add", "origin", repo_url(self.repo))
        r = self._git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", self.ref, timeout=300)
        if r.rc != 0:
            r = self._git("fetch", "-q", "--depth", "1", "origin", self.ref, timeout=300)
        if r.rc != 0:
            raise FetchError("cannot fetch %s@%s: %s" % (self.repo, self.ref, clean_error(r.text)))
        self._git("sparse-checkout", "init", "--no-cone")
        r = self._git("checkout", "-q", "--detach", "FETCH_HEAD")
        if r.rc != 0:
            raise FetchError("cannot check out %s@%s: %s" % (self.repo, self.ref, clean_error(r.text)))
        self.commit = self._git("rev-parse", "HEAD").out.strip() or None
        return self.commit

    def directory(self, path: str) -> Path:
        """Absolute path of directory `path` in the checkout, materialising it."""
        target = self.dest / path.strip("/")
        self._git("sparse-checkout", "add", path.strip("/"))
        if not target.is_dir():
            self._git("checkout", "-q", "FETCH_HEAD", "--", path.strip("/"))
        if not target.is_dir():
            raise FetchError("no directory %s in %s@%s" % (path, self.repo, self.ref))
        return target.resolve()

    def file(self, path: str) -> Path:
        """Absolute path of `path` in the checkout, materialising it if needed."""
        target = self.dest / path
        if not target.is_file():
            self._git("sparse-checkout", "add", path)
        if not target.is_file():
            self._git("checkout", "-q", "FETCH_HEAD", "--", path)
        if not target.is_file():
            raise FetchError("no %s in %s@%s" % (path, self.repo, self.ref))
        return target.resolve()


CLI_PREFIX_RE = re.compile(r"\d{4}-\d\d-\d\dT\S+ \[(?:ERROR|FATAL|WARN|WARNING|INFO)\] ")


def clean_error(text: str) -> str:
    """One line without the CLI's timestamp and level prefixes."""
    return one_line(CLI_PREFIX_RE.sub("", text))
