#!/usr/bin/env python3
"""
Dashboard server for workflow-tester-tool.

Reads test results from the output/ directory and serves a web dashboard.

The server works behind a reverse proxy at any URL prefix (e.g.
https://activate.parallel.works/me/session/alvaro/test/) without
configuration: it detects the prefix automatically from the X-Forwarded-Prefix
header or strips it via pattern-based API routing.

Explicit prefix override (optional):
  --prefix /me/session/alvaro/test   (CLI)
  PW_BASE_PATH=/me/session/alvaro/test python serve.py  (env var)

Admin mode:
  python serve.py --admin
  Enables POST /api/run-test, POST /api/run-all, and POST /api/cancel.
  Serves admin.html as root.

Usage:
  python serve.py [--host HOST] [--port PORT] [--output-dir DIR] [--prefix PREFIX] [--admin]
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

TESTS_DIR = Path(__file__).parent

# API path segments
_API_RESULTS = "/api/results"
_API_RESULTS_SLASH = "/api/results/"
_API_RUN_TEST = "/api/run-test"
_API_RUN_ALL  = "/api/run-all"
_API_CANCEL   = "/api/cancel"


class DashboardHandler(SimpleHTTPRequestHandler):
    web_dir: Path = TESTS_DIR / "web"
    output_dir: Path = TESTS_DIR / "output"
    url_prefix: str = ""
    admin_mode: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.web_dir), **kwargs)

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        raw = parsed.path
        clean = self._strip(raw)

        # ── API: GET …/api/results ──────────────────────────────────────────
        if clean == _API_RESULTS or raw.endswith(_API_RESULTS):
            return self._handle_results()

        # ── API: GET …/api/results/<test_path>/inputs ───────────────────────
        for candidate in (clean, raw):
            if _API_RESULTS_SLASH in candidate and candidate.endswith("/inputs"):
                idx = candidate.index(_API_RESULTS_SLASH)
                test_path = unquote(
                    candidate[idx + len(_API_RESULTS_SLASH):-len("/inputs")]
                )
                return self._handle_inputs(test_path)

        # ── Static files ─────────────────────────────────────────────────────
        qs = f"?{parsed.query}" if parsed.query else ""
        if self.__class__.admin_mode and clean in ("/", "", "/index.html"):
            self.path = "/admin.html" + qs
        else:
            self.path = clean + qs
        return super().do_GET()

    def do_POST(self):
        if not self.__class__.admin_mode:
            self._json({"error": "Admin mode not enabled"}, status=403)
            return

        parsed = urlparse(self.path)
        raw = parsed.path
        clean = self._strip(raw)

        if clean == _API_RUN_TEST or raw.endswith(_API_RUN_TEST):
            return self._handle_run_test()
        elif clean == _API_RUN_ALL or raw.endswith(_API_RUN_ALL):
            return self._handle_run_all()
        elif clean == _API_CANCEL or raw.endswith(_API_CANCEL):
            return self._handle_cancel()
        else:
            self._json({"error": "Not found"}, status=404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Prefix detection ──────────────────────────────────────────────────────

    def _strip(self, path: str) -> str:
        """Return path with the configured (or auto-detected) prefix removed."""
        prefix = (
            self.url_prefix
            or self.headers.get("X-Forwarded-Prefix", "").strip()
        ).rstrip("/")

        if not prefix:
            return path
        if path == prefix or path == prefix + "/":
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    # ── No-cache for JS / CSS so updates are always picked up ─────────────────

    def end_headers(self):
        p = getattr(self, "path", "") or ""
        if p.split("?")[0].endswith((".js", ".css")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ── Body reader ───────────────────────────────────────────────────────────

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_results(self):
        results = []
        if self.output_dir.exists():
            for result_file in sorted(self.output_dir.rglob("result.json")):
                try:
                    data = json.loads(result_file.read_text(encoding="utf-8"))
                    test_id = data.get("test", "")
                    parts = test_id.split("/")
                    results.append({
                        "test":       test_id,
                        "platform":   parts[0] if len(parts) > 0 else "",
                        "user":       parts[1] if len(parts) > 1 else "",
                        "kind":       parts[2] if len(parts) > 2 else "",
                        "workflow":   parts[3] if len(parts) > 3 else "",
                        "test_name":  parts[4] if len(parts) > 4 else "",
                        "status":     data.get("status", "unknown"),
                        "slug":       data.get("slug"),
                        "duration_s": data.get("duration_s", 0),
                        "started_at": data.get("started_at"),
                        "error":      data.get("error"),
                    })
                except Exception:
                    pass

        results.sort(key=lambda r: r.get("started_at") or "", reverse=True)

        total   = len(results)
        passed  = sum(1 for r in results if r["status"] == "completed")
        running = sum(1 for r in results if r["status"] == "running")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        # Skipped tests (resource off) are neither passes nor failures.
        failed  = total - passed - running - skipped
        # Pass rate is measured over tests that actually ran (exclude skipped/running).
        rated   = passed + failed

        status_counts:   dict = {}
        workflow_counts: dict = {}
        kind_counts:     dict = {}
        for r in results:
            status_counts[r["status"]]     = status_counts.get(r["status"], 0)     + 1
            workflow_counts[r["workflow"]] = workflow_counts.get(r["workflow"], 0) + 1
            kind_counts[r["kind"]]         = kind_counts.get(r["kind"], 0)         + 1

        self._json({
            "results": results,
            "summary": {
                "total":           total,
                "passed":          passed,
                "running":         running,
                "skipped":         skipped,
                "failed":          failed,
                "pass_rate":       round(passed / rated * 100, 1) if rated else 0,
                "last_run":        results[0]["started_at"] if results else None,
                "status_counts":   status_counts,
                "workflow_counts": workflow_counts,
                "kind_counts":     kind_counts,
            },
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def _handle_inputs(self, test_path: str):
        inputs_file = TESTS_DIR / (test_path + ".json")
        if not inputs_file.exists():
            self._json({"error": f"Inputs file not found: {test_path}"}, status=404)
            return
        try:
            data = json.loads(inputs_file.read_text(encoding="utf-8"))
            self._json({"inputs": data, "test_path": test_path})
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _handle_run_test(self):
        try:
            data = self._read_json_body()
            test = (data.get("test") or "").strip()
            if not test:
                self._json({"error": "Missing 'test' field"}, status=400)
                return

            test_file = TESTS_DIR / (test + ".json")
            if not test_file.exists():
                self._json({"error": f"Test file not found: {test}.json"}, status=404)
                return

            def _run():
                subprocess.run(
                    [sys.executable, "-u", str(TESTS_DIR / "run_tests.py"),
                     "--test-file", str(test_file)],
                    cwd=str(TESTS_DIR),
                )

            threading.Thread(target=_run, daemon=True).start()
            self._json({"status": "launched", "test": test})
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _handle_run_all(self):
        try:
            cmd = [sys.executable, "-u", str(TESTS_DIR / "run_tests.py")]
            platform = os.environ.get("PW_PLATFORM_HOST", "").strip()
            user = os.environ.get("PW_USER", "").strip()
            if platform:
                cmd += ["--platform", platform]
            if user:
                cmd += ["--user", user]

            threading.Thread(
                target=lambda: subprocess.run(cmd, cwd=str(TESTS_DIR)),
                daemon=True,
            ).start()
            self._json({"status": "launched", "platform": platform, "user": user})
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _handle_cancel(self):
        try:
            data = self._read_json_body()
            slug = (data.get("slug") or "").strip()
            platform = (data.get("platform") or "").strip()
            if not slug:
                self._json({"error": "Missing 'slug' field"}, status=400)
                return
            if not platform:
                self._json({"error": "Missing 'platform' field"}, status=400)
                return

            proc = subprocess.run(
                ["pw", "--platform-host", platform,
                 "workflows", "runs", "cancel", slug],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                self._json({"status": "canceled", "slug": slug})
            else:
                err = proc.stderr.strip() or proc.stdout.strip()
                self._json({"error": err or "Cancel failed"}, status=500)
        except subprocess.TimeoutExpired:
            self._json({"error": "Cancel command timed out"}, status=504)
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


# ── Server (dual-stack IPv6) ───────────────────────────────────────────────────

class DualStackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that binds IPv6 dual-stack when given an IPv6 host.

    The ACTIVATE proxy may resolve the session hostname to an IPv6 address
    first. A plain IPv4 ('0.0.0.0') bind then refuses that connection and the
    proxy returns {"error":true,"message":"Proxy Error"}. Binding to '::' with
    IPV6_V6ONLY disabled accepts both IPv4 and IPv6 clients on dual-stack hosts.
    """

    def __init__(self, server_address, *args, **kwargs):
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, *args, **kwargs)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                # No dual-stack support — IPv6-only is still better than failing.
                pass
        super().server_bind()


def _default_bind_host() -> str:
    """Prefer dual-stack IPv6 ('::'); fall back to IPv4 ('0.0.0.0') with no IPv6.

    Mirrors the fix in librechat-singularity-manager: the platform proxy may
    resolve the hostname to IPv6 first, so an IPv4-only bind causes "Proxy Error".
    """
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.close()
        return "::"
    except OSError:
        return "0.0.0.0"


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Workflow Test Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Prefix auto-detection (pick one, or omit for localhost):
  python serve.py --prefix /me/session/alvaro/test
  PW_BASE_PATH=/me/session/alvaro/test python serve.py
The server also honours the X-Forwarded-Prefix header set by some proxies.

Admin mode:
  python serve.py --admin
  Enables POST /api/run-test, POST /api/run-all, and POST /api/cancel.
  Serves admin.html as root.
""",
    )
    ap.add_argument("--host",       default=None,
                    help="Bind address (default: '::' dual-stack IPv6, "
                         "or '0.0.0.0' when the host has no IPv6)")
    ap.add_argument("--port",       type=int, default=8080)
    ap.add_argument("--output-dir", default=str(TESTS_DIR / "output"))
    ap.add_argument(
        "--prefix",
        default=os.environ.get("PW_BASE_PATH", ""),
        help="URL prefix to strip (e.g. /me/session/alvaro/test). "
             "Also read from PW_BASE_PATH env var.",
    )
    ap.add_argument(
        "--admin",
        action="store_true",
        help="Enable admin API (run-test, cancel) and serve admin.html as root.",
    )
    args = ap.parse_args()

    DashboardHandler.output_dir = Path(args.output_dir).resolve()
    DashboardHandler.url_prefix = args.prefix.rstrip("/")
    DashboardHandler.admin_mode = args.admin

    bind_host = args.host or _default_bind_host()
    server = DualStackHTTPServer((bind_host, args.port), DashboardHandler)
    mode = "ADMIN" if args.admin else "public (read-only)"
    print(f"Workflow Test Dashboard: http://localhost:{args.port}/")
    print(f"  Output directory: {DashboardHandler.output_dir}")
    print(f"  Mode:             {mode}")
    print(f"  Bind host:        {bind_host}")
    if args.prefix:
        print(f"  Prefix:           {args.prefix}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
