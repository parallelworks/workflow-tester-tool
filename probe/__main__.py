"""PROBE command line: python3 -m probe {run,serve,list} ..."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .definitions import load_tests, location_notes
from .runner import Options, clean_host, run_suite
from .server import WEB_DIR, Config, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe",
        description="PROBE runs workflow compatibility tests on ACTIVATE and serves their results.")
    parser.add_argument("--version", action="version", version="probe " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    run = sub.add_parser("run", help="run the tests defined for the current platform and user")
    run.add_argument("--tests", default="tests", metavar="DIR", help="test definitions (default: tests)")
    run.add_argument("--results", default="results", metavar="DIR", help="results tree (default: results)")
    run.add_argument("--platform", help="platform host (default: PW_PLATFORM_HOST or the pw context)")
    run.add_argument("--user", help="platform user (default: PW_USER or the pw context)")
    run.add_argument("--suite-run", metavar="LABEL", help="suite run label (default: probe-<UTC time>)")
    run.add_argument("--workers", type=int, default=8, help="tests run in parallel (default: 8)")
    run.add_argument("--poll-interval", type=int, default=15, metavar="SECONDS",
                     help="seconds between run status checks (default: 15)")
    run.add_argument("--timeout", type=int, metavar="SECONDS", help="override every test's timeout_s")
    run.add_argument("--keep", action="store_true", help="leave endpoints and services running")
    run.add_argument("--dry-run", action="store_true", help="list what would run and exit")
    run.add_argument("--filter", action="append", default=[], metavar="PATTERN",
                     help="only test ids matching this glob or substring (repeatable)")
    run.add_argument("--id", action="append", default=[], metavar="TEST_ID",
                     help="only this exact test id (repeatable)")
    run.add_argument("--test", action="append", default=[], metavar="FILE",
                     help="only this test definition file, relative to --tests (repeatable)")
    run.add_argument("--all", action="store_true",
                     help="every test of this platform and user; the default when no test is selected")
    run.add_argument("--bucket", metavar="URI", default=os.environ.get("PROBE_RESULTS_BUCKET") or None,
                     help="bucket path (pw://<user>/<bucket>/<path>) that receives each test's "
                          "records and artifacts as soon as it finishes (default: PROBE_RESULTS_BUCKET)")

    srv = sub.add_parser("serve", help="serve the dashboard")
    srv.add_argument("--results", default="results", metavar="DIR")
    srv.add_argument("--tests", default="tests", metavar="DIR")
    srv.add_argument("--host", default=None, help="bind address (default: '::' dual-stack, or 0.0.0.0)")
    srv.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080))
    srv.add_argument("--prefix", default=os.environ.get("PW_ENDPOINT_PATH") or os.environ.get("PW_BASE_PATH") or "",
                     help="URL prefix the dashboard is served under (default: PW_ENDPOINT_PATH)")
    srv.add_argument("--admin", action="store_true", help="enable the run and cancel actions")
    srv.add_argument("--bucket", metavar="URI", default=os.environ.get("PROBE_RESULTS_BUCKET") or None,
                     help="bucket path the runs started from the dashboard sync their results to")
    srv.add_argument("--web", default=str(WEB_DIR), metavar="DIR", help=argparse.SUPPRESS)

    lst = sub.add_parser("list", help="list the test definitions and report invalid ones")
    lst.add_argument("--tests", default="tests", metavar="DIR")
    lst.add_argument("--platform", help="only tests for this platform host")
    lst.add_argument("--user", help="only tests for this user")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        opts = Options(
            tests_dir=Path(args.tests), results_dir=Path(args.results).resolve(),
            platform=args.platform, user=args.user, suite_run=args.suite_run,
            workers=args.workers, poll_s=max(1, args.poll_interval), timeout_s=args.timeout,
            keep=args.keep, dry_run=args.dry_run, filters=args.filter, ids=args.id,
            test_files=args.test, run_all=args.all, bucket=args.bucket,
        )
        return run_suite(opts)
    if args.command == "serve":
        cfg = Config(results_dir=Path(args.results), tests_dir=Path(args.tests),
                     web_dir=Path(args.web), prefix=args.prefix, admin=args.admin, bucket=args.bucket)
        serve(cfg, args.host, args.port)
        return 0
    if args.command == "list":
        tests, errors = load_tests(Path(args.tests))
        platform = clean_host(args.platform)
        for test in tests:
            if platform and test.platform != platform:
                continue
            if args.user and test.user != args.user:
                continue
            target = test.target
            print("%s\n    %s  timeout=%ss  resource=%s  node=%s" % (
                test.id, test.launch_target, test.timeout_s,
                target.resource or "-", target.node or target.type or "-"))
        for note in location_notes(tests, Path(args.tests)):
            print("note: %s" % note, file=sys.stderr)
        for error in errors:
            print("error: %s" % error, file=sys.stderr)
        return 2 if errors else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
