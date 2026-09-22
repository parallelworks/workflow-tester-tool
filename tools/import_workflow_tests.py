#!/usr/bin/env python3
"""Turn the end-to-end tests recorded in the parallelworks/workflows repository into
PROBE test definitions.

    python3 tools/import_workflow_tests.py /path/to/workflows --variant general \
        --platform activate.parallel.works --user alvaro --out tests

Every workflows/<name>/tests/<variant>/<test>.json becomes
<out>/<platform>/<user>/<name>/<test>.json: the file's inputs are kept, its `_test`
object maps to timeout_s, warm_marker, leftover_patterns, leftover_commands and setup,
and the workflow is workflows/<name>/yamls/<variant>.yaml at --ref. Existing files are
overwritten. Tests with `_test` keys PROBE does not know are reported and skipped.
"""
import argparse
import json
import sys
from pathlib import Path

KNOWN = {"timeout_s", "warm_marker", "leftover_patterns", "leftover_commands", "setup"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflows_repo", help="local checkout of parallelworks/workflows")
    ap.add_argument("--variant", default="general", help="tests/<variant> directory and yamls/<variant>.yaml")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--out", default="tests", help="PROBE tests directory")
    ap.add_argument("--repo", default="github.com/parallelworks/workflows", help="workflow.repo to record")
    ap.add_argument("--ref", default="canary", help="workflow.ref to record")
    args = ap.parse_args()

    root = Path(args.workflows_repo) / "workflows"
    written, skipped = [], []
    for source in sorted(root.glob("*/tests/%s/*.json" % args.variant)):
        name = source.parts[-4]
        data = json.loads(source.read_text())
        meta = data.pop("_test", {}) or {}
        unknown = sorted(set(meta) - KNOWN)
        if unknown:
            skipped.append("%s: unsupported _test keys %s" % (source, ", ".join(unknown)))
            continue
        definition = {
            "name": source.stem,
            "platform": args.platform,
            "user": args.user,
            "workflow_name": name,
            "workflow": {"repo": args.repo, "path": "workflows/%s/yamls/%s.yaml" % (name, args.variant),
                         "ref": args.ref},
            "timeout_s": int(meta.get("timeout_s", 1800)),
        }
        for key in ("warm_marker", "leftover_patterns", "leftover_commands", "setup"):
            if meta.get(key):
                definition[key] = meta[key]
        definition["inputs"] = data
        target = Path(args.out) / args.platform / args.user / name / (source.stem + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(definition, indent=2) + "\n")
        written.append(str(target))
    for line in written:
        print("wrote", line)
    for line in skipped:
        print("skipped", line, file=sys.stderr)
    print("%d written, %d skipped" % (len(written), len(skipped)))
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
