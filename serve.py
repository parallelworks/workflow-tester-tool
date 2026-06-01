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

Usage:
  python serve.py [--host HOST] [--port PORT] [--output-dir DIR] [--prefix PREFIX]
"""

import argparse
import json
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

TESTS_DIR = Path(__file__).parent

# API path segment (used for pattern-based routing)
_API_RESULTS = "/api/results"
_API_RESULTS_SLASH = "/api/results/"


class DashboardHandler(SimpleHTTPRequestHandler):
    web_dir: Path = TESTS_DIR / "web"
    output_dir: Path = TESTS_DIR / "output"
    url_prefix: str = ""   # optional prefix, e.g. "/me/session/alvaro/test"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.web_dir), **kwargs)

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        raw = parsed.path          # path as received (may include proxy prefix)
        clean = self._strip(raw)   # path with prefix removed

        # ── API: GET …/api/results ──────────────────────────────────────────
        # Use endswith so the route fires whether or not the proxy stripped
        # the prefix (raw may be /prefix/api/results or just /api/results).
        if clean == _API_RESULTS or raw.endswith(_API_RESULTS):
            return self._handle_results()

        # ── API: GET …/api/results/<test_path>/inputs ───────────────────────
        # Use the path that contains /api/results/ (either clean or raw).
        for candidate in (clean, raw):
            if _API_RESULTS_SLASH in candidate and candidate.endswith("/inputs"):
                idx = candidate.index(_API_RESULTS_SLASH)
                test_path = unquote(
                    candidate[idx + len(_API_RESULTS_SLASH):-len("/inputs")]
                )
                return self._handle_inputs(test_path)

        # ── Static files ─────────────────────────────────────────────────────
        # Rewrite self.path to the prefix-stripped path so SimpleHTTPRequestHandler
        # looks in the right place inside web/.
        qs = f"?{parsed.query}" if parsed.query else ""
        self.path = clean + qs
        return super().do_GET()

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

        total  = len(results)
        passed = sum(1 for r in results if r["status"] == "completed")
        failed = total - passed

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
                "failed":          failed,
                "pass_rate":       round(passed / total * 100, 1) if total else 0,
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
""",
    )
    ap.add_argument("--host",       default="0.0.0.0")
    ap.add_argument("--port",       type=int, default=8080)
    ap.add_argument("--output-dir", default=str(TESTS_DIR / "output"))
    ap.add_argument(
        "--prefix",
        default=os.environ.get("PW_BASE_PATH", ""),
        help="URL prefix to strip (e.g. /me/session/alvaro/test). "
             "Also read from PW_BASE_PATH env var.",
    )
    args = ap.parse_args()

    DashboardHandler.output_dir = Path(args.output_dir).resolve()
    DashboardHandler.url_prefix = args.prefix.rstrip("/")

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    prefix_note = f"  Prefix:           {args.prefix}" if args.prefix else ""
    print(f"Workflow Test Dashboard: http://localhost:{args.port}/")
    print(f"  Output directory: {DashboardHandler.output_dir}")
    if prefix_note:
        print(prefix_note)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
