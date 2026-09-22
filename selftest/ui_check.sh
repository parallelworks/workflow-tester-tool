#!/usr/bin/env bash
# Starts a local admin dashboard on the mock pw CLI with a small results tree,
# then drives every control in headless Chrome (selftest/ui_check.js).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
WORK="$(mktemp -d)"
trap 'kill ${SERVER:-} 2>/dev/null || true; rm -rf "${WORK}"' EXIT
export PATH="${HERE}/mockbin:${PATH}"
export MOCK_PW_CONFIG="${WORK}/mock.json" MOCK_PW_STATE="${WORK}/state"
export PW_PLATFORM_HOST=activate.parallel.works PW_USER=alvaro
mkdir -p "${MOCK_PW_STATE}"
cat > "${MOCK_PW_CONFIG}" <<'JSON'
{"clusters": {"gcpsmall": {"user": "alvaro", "status": "active"}},
 "endpoint": {"webshell": "webshell", "jupyterlab": "jupyterlab-host"}}
JSON
# a fixture workflows repository the tests can be launched from
python3 - "${WORK}" <<'EOF'
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent) if "__file__" in dir() else ".")
EOF
cd "${REPO}"
python3 - "${WORK}" <<'EOF'
import json, sys
from pathlib import Path
sys.path.insert(0, "selftest")
from helpers import make_workflows_repo, definition, WEBSHELL, JUPYTERLAB, SUBMITTER
import helpers
work = Path(sys.argv[1])
url, commit = make_workflows_repo(work)
helpers.REPO_URL = url
tests = work / "tests"
for name, kw in [("webshell/gcpsmall-controller", {}), ("webshell/gcpsmall-compute", {"scheduler": True}),
                 ("jupyterlab/gcpsmall-controller", {"workflow_name": "jupyterlab", "path": JUPYTERLAB}),
                 ("script_submitter/gcpsmall-controller", {"workflow_name": "script_submitter", "path": SUBMITTER})]:
    path = tests / "activate.parallel.works" / "alvaro" / (name + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = definition(name=path.stem, **kw)
    data["workflow"]["repo"] = url
    path.write_text(json.dumps(data, indent=2))
EOF
echo "seeding results with one suite run (mock pw)"
python3 -m probe run --tests "${WORK}/tests" --results "${WORK}/results" --poll-interval 1 --suite-run seed >/dev/null
python3 -m probe serve --admin --results "${WORK}/results" --tests "${WORK}/tests" --port 8766 --host 127.0.0.1 --bucket pw://alvaro/gcpbucket/probe/results >"${WORK}/serve.log" 2>&1 &
SERVER=$!
sleep 1
node "${HERE}/ui_check.js" "http://127.0.0.1:8766/" "${MOCK_PW_STATE}/calls.log"
