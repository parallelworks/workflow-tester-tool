"""Dashboard server: the static page in web/ plus a JSON API over the results tree.

The server runs behind the platform's endpoint proxy at "/" (subdomain endpoint)
or under a prefix such as /me/session/<user>/<name>/ (path-based endpoint). The
prefix comes from --prefix, PW_ENDPOINT_PATH or the X-Forwarded-Prefix header;
API routes are recognised by their /api/ segment wherever the prefix puts them.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import unquote, urlparse

from . import __version__
from .definitions import load_tests
from .pw import Pw
from .results import RECORDS_FILE, id_parts, running_artifacts, scan, state, suite_runs, valid_id

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
TEXT_TYPES = {".log": "text/plain", ".txt": "text/plain", ".json": "application/json"}


class Config:
    def __init__(self, results_dir: Path, tests_dir: Path, web_dir: Path = WEB_DIR,
                 prefix: str = "", admin: bool = False, bucket: Optional[str] = None):
        self.results_dir = results_dir.resolve()
        self.tests_dir = tests_dir.resolve()
        self.web_dir = web_dir.resolve()
        self.prefix = ("/" + prefix.strip("/")) if prefix and prefix.strip("/") else ""
        self.admin = admin
        self.bucket = bucket.rstrip("/") if bucket else None
        # runners started from the admin dashboard: (process, ids or None for all)
        self.active_runs: list = []
        self.lock = threading.Lock()


def runner_command(cfg: Config, ids: List[str]) -> List[str]:
    command = [sys.executable, "-m", "probe", "run",
               "--tests", str(cfg.tests_dir), "--results", str(cfg.results_dir)]
    if cfg.bucket:
        command += ["--bucket", cfg.bucket]
    for test_id in ids:
        command += ["--id", test_id]
    return command


def conflicts(active: List[Optional[Set[str]]], ids: List[str]) -> bool:
    """True when a requested run overlaps a run still in progress. None means
    every test of the platform and user."""
    requested = set(ids) if ids else None
    for running in active:
        if running is None or requested is None or (running & requested):
            return True
    return False


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_state(cfg: Config) -> dict:
    scanned = scan(cfg.results_dir)
    tests, errors = load_tests(cfg.tests_dir)
    defined = {t.id: t for t in tests}
    all_records: List[dict] = []
    items = []
    for test_id in sorted(set(scanned) | set(defined)):
        entry = scanned.get(test_id)
        records = entry["records"] if entry else []
        all_records.extend(records)
        running = running_artifacts(entry["dir"], records, entry["artifacts"]) if entry else []
        current_state = state(test_id, records, running)
        definition = defined.get(test_id)
        current = current_state["current"]
        if current:
            target = dict(current.get("target") or {})
            kind = (current.get("test") or {}).get("kind")
        else:
            target, kind = {}, None
        if definition is not None:
            kind = definition.kind
            if not current:
                t = definition.target
                target = {"system": t.system, "resource": t.resource, "type": t.type, "node": t.node}
        item = dict(id_parts(test_id))
        item.update({
            "kind": kind,
            "system": target.get("system"),
            "node": target.get("node"),
            "type": target.get("type"),
            "resource": target.get("resource"),
            "defined": definition is not None,
            "definition_path": _relative(definition.path, cfg.tests_dir) if definition else None,
            "launch_target": definition.launch_target if definition else None,
        })
        item.update(current_state)
        items.append(item)
    return {
        "generated_at": now_iso(),
        "version": __version__,
        "admin": cfg.admin,
        "results_dir": str(cfg.results_dir),
        "bucket": cfg.bucket,
        "tests": items,
        "suite_runs": suite_runs(all_records),
        "definition_errors": errors,
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _under(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child.resolve()), str(parent)]) == str(parent)
    except ValueError:
        return False


class Handler(SimpleHTTPRequestHandler):
    cfg: Config = None  # set by serve()
    server_version = "probe/" + __version__

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.cfg.web_dir), **kwargs)

    # -- routing -----------------------------------------------------------

    def _prefix(self) -> str:
        prefix = self.cfg.prefix or self.headers.get("X-Forwarded-Prefix", "").strip()
        return prefix.rstrip("/")

    def _strip_prefix(self, path: str) -> str:
        prefix = self._prefix()
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):]
        return path or "/"

    @staticmethod
    def _api_route(path: str) -> Optional[str]:
        marker = "/api/"
        index = path.find(marker)
        return path[index + len(marker) - 1:] if index >= 0 else None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        route = self._api_route(path)
        if route is not None:
            return self._get_api(route)
        local = self._strip_prefix(path)
        if local in ("", "/"):
            local = "/index.html"
        self.path = local + ("?" + parsed.query if parsed.query else "")
        return super().do_GET()

    def do_POST(self):
        route = self._api_route(unquote(urlparse(self.path).path))
        if not self.cfg.admin:
            return self._json({"error": "this dashboard is read-only"}, 403)
        if route == "/run":
            return self._post_run()
        if route == "/cancel":
            return self._post_cancel()
        return self._json({"error": "not found"}, 404)

    def end_headers(self):
        path = (getattr(self, "path", "") or "").split("?")[0]
        if path.endswith((".js", ".css", ".html")) or "/api/" in path:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    # -- GET handlers ------------------------------------------------------

    def _get_api(self, route: str):
        if route == "/state":
            return self._json(build_state(self.cfg))
        if route == "/health":
            return self._json({"ok": True, "version": __version__, "time": now_iso()})
        m = re.match(r"^/tests/((?:[^/]+/){3}[^/]+)/(records|definition|artifacts)(?:/(.*))?$", route)
        if not m:
            return self._json({"error": "not found"}, 404)
        test_id, what, rest = m.group(1), m.group(2), m.group(3)
        if not valid_id(test_id):
            return self._json({"error": "invalid test id"}, 400)
        test_dir = self.cfg.results_dir / test_id
        if what == "records":
            from .results import read_records
            return self._json({"id": test_id, "records": read_records(test_dir / RECORDS_FILE)})
        if what == "definition":
            tests, _ = load_tests(self.cfg.tests_dir)
            for test in tests:
                if test.id == test_id:
                    try:
                        data = json.loads(test.path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        return self._json({"error": str(exc)}, 500)
                    return self._json({"id": test_id, "path": _relative(test.path, self.cfg.tests_dir),
                                       "definition": data})
            return self._json({"error": "no definition for %s" % test_id}, 404)
        if not rest:
            return self._json({"id": test_id, "artifacts": self._list_artifacts(test_dir)})
        return self._send_artifact(test_dir, rest)

    def _list_artifacts(self, test_dir: Path) -> list:
        listing = []
        if not test_dir.is_dir():
            return listing
        for entry in sorted((p for p in test_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
            files = []
            for item in sorted(entry.iterdir()):
                if item.is_file() and NAME_RE.match(item.name):
                    files.append({"name": item.name, "size": item.stat().st_size})
            listing.append({"name": entry.name, "files": files})
        return listing

    def _send_artifact(self, test_dir: Path, rest: str):
        parts = rest.split("/")
        if len(parts) != 2 or not all(NAME_RE.match(p) for p in parts):
            return self._json({"error": "not found"}, 404)
        path = test_dir / parts[0] / parts[1]
        if not _under(path, self.cfg.results_dir) or not path.is_file():
            return self._json({"error": "not found"}, 404)
        data = path.read_bytes()
        truncated = len(data) > MAX_ARTIFACT_BYTES
        if truncated:
            data = data[-MAX_ARTIFACT_BYTES:]
        content_type = TEXT_TYPES.get(path.suffix, "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Truncated", "true" if truncated else "false")
        self.end_headers()
        self.wfile.write(data)

    # -- POST handlers (admin) ---------------------------------------------

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _post_run(self):
        body = self._body()
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) and valid_id(i) for i in ids):
            return self._json({"error": "'ids' must be a list of test ids"}, 400)
        if not ids and not body.get("all"):
            return self._json({"error": "pass 'ids' or 'all': true"}, 400)
        with self.cfg.lock:
            self.cfg.active_runs = [(p, s) for p, s in self.cfg.active_runs if p.poll() is None]
            if conflicts([s for _, s in self.cfg.active_runs], ids):
                return self._json({"error": "a run of these tests is already in progress"}, 409)
            try:
                process = subprocess.Popen(runner_command(self.cfg, ids), cwd=str(self.cfg.web_dir.parent),
                                           stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                return self._json({"error": "could not start the runner: %s" % exc}, 500)
            self.cfg.active_runs.append((process, set(ids) if ids else None))
        return self._json({"started": True, "pid": process.pid, "ids": ids, "all": not ids})

    def _post_cancel(self):
        body = self._body()
        slug = str(body.get("slug") or "").strip()
        platform = str(body.get("platform") or "").strip() or None
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", slug):
            return self._json({"error": "'slug' is required"}, 400)
        if platform and not re.match(r"^[A-Za-z0-9.-]+$", platform):
            return self._json({"error": "invalid platform"}, 400)
        result = Pw(platform).cancel(slug)
        if result.rc != 0:
            return self._json({"error": result.one_line() or "cancel failed"}, 502)
        return self._json({"canceled": slug})

    def _json(self, data, status: int = 200):
        body = json.dumps(data, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DualStackServer(ThreadingHTTPServer):
    """Binds '::' with IPV6_V6ONLY off so the platform proxy reaches the server
    over IPv4 or IPv6; a plain IPv4 bind makes the proxy answer 'Proxy Error'
    when it resolves the session host to an IPv6 address first."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                pass
        super().server_bind()


def default_host() -> str:
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return "::"
    except OSError:
        return "0.0.0.0"


def make_server(cfg: Config, host: Optional[str], port: int) -> DualStackServer:
    handler = type("ConfiguredHandler", (Handler,), {"cfg": cfg})
    return DualStackServer((host or default_host(), port), handler)


def serve(cfg: Config, host: Optional[str], port: int) -> None:
    server = make_server(cfg, host, port)
    print("PROBE dashboard %s on %s port %d (%s)" % (
        __version__, server.server_address[0], server.server_address[1],
        "admin" if cfg.admin else "read-only"), flush=True)
    print("  results %s\n  bucket  %s\n  tests   %s\n  prefix  %s" % (
        cfg.results_dir, cfg.bucket or "none", cfg.tests_dir, cfg.prefix or "/"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
