#!/usr/bin/env bash
#
# Offline end-to-end test of run_tests.py, exercising it exactly the way the
# ACTIVATE platform's `run_tests` job does: from a checked-out copy of the repo
# inside a job directory (PW_PARENT_JOB_DIR), with the `pw` CLI on PATH.
#
# We substitute a mock `pw` CLI (mockbin/pw) so the run is deterministic and
# needs no platform credentials, and assert the resulting result.json statuses.
#
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${HARNESS_DIR}/.." && pwd)"
RUN_ROOT="${HOME}/pw/jobs/workflowstatusmonitor"
RUN_DIR="${RUN_ROOT}/00000"          # the fake platform run directory

PLATFORM="activate.parallel.works"
USER_NAME="alvaro"
CFG_DIR="$(mktemp -d)"
trap 'rm -rf "${CFG_DIR}"' EXIT

pass=0; fail=0

# ── helpers ───────────────────────────────────────────────────────────────────
status_of() {  # $1 = test path under output/  → prints the status field
  local f="${RUN_DIR}/output/${PLATFORM}/${USER_NAME}/$1/result.json"
  [ -f "$f" ] && python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['status'])" "$f" || echo "MISSING"
}

expect() {  # $1 = test path, $2 = expected status, $3 = human label
  local got; got="$(status_of "$1")"
  if [ "$got" = "$2" ]; then
    echo "    PASS  $3: $1 → $got"; pass=$((pass+1))
  else
    echo "    FAIL  $3: $1 → expected '$2', got '$got'"; fail=$((fail+1))
  fi
}

setup_run_dir() {
  # Simulate `parallelworks/checkout`: a clean copy of the repo into the job dir.
  rm -rf "${RUN_DIR}"
  mkdir -p "${RUN_DIR}"
  rsync -a --exclude 'output' --exclude 'test_harness' --exclude '.git' \
        "${REPO_DIR}/" "${RUN_DIR}/"
}

run_scenario() {  # $1 = scenario name, $2 = mock config file
  local name="$1" cfg="$2"
  echo ""
  echo "── Scenario: ${name} ────────────────────────────────────────────────"
  setup_run_dir

  export PATH="${HARNESS_DIR}/mockbin:${PATH}"
  export PW_PLATFORM_HOST="${PLATFORM}"
  export PW_USER="${USER_NAME}"
  export PW_PARENT_JOB_DIR="${RUN_DIR}"
  export MOCK_PW_CONFIG="${cfg}"
  export MOCK_PW_STATE="${RUN_DIR}/.mock_state"
  # Tiny poll intervals so the suite finishes in seconds, not minutes.
  export PW_TEST_POLL_INTERVAL=1 PW_TEST_SESSION_POLL_INTERVAL=1
  export PW_TEST_MAX_WAIT=30 PW_TEST_SESSION_MAX_WAIT=30

  ( cd "${RUN_DIR}" && python3 -u run_tests.py \
        --platform "${PW_PLATFORM_HOST}" \
        --user "${PW_USER}" \
        --output-dir "${RUN_DIR}/output" ) \
    || echo "  (run_tests.py exited non-zero — expected when a test fails)"
}

# ── Scenario 1: GPU server OFF → workflow tests skipped, session still runs ────
cat > "${CFG_DIR}/cfg_off.json" <<'JSON'
{
  "clusters": { "pw://alvaro/a30gpuserver": "off" },
  "launch":   { "marketplace.script_submitter.latest": "fail_400" }
}
JSON
run_scenario "a30gpuserver OFF (would-be launch failure is avoided)" "${CFG_DIR}/cfg_off.json"
expect "workflows/marketplace.script_submitter.latest/test1" skipped   "off→skipped"
expect "workflows/marketplace.script_submitter.latest/test2" skipped   "off→skipped"
expect "sessions/marketplace.openvscode.latest/inputs"       completed "no-uri→runs"

# ── Scenario 2: GPU server ON → workflow tests run to completion ───────────────
cat > "${CFG_DIR}/cfg_on.json" <<'JSON'
{
  "clusters": { "pw://alvaro/a30gpuserver": "active" }
}
JSON
run_scenario "a30gpuserver ON" "${CFG_DIR}/cfg_on.json"
expect "workflows/marketplace.script_submitter.latest/test1" completed "on→runs"
expect "workflows/marketplace.script_submitter.latest/test2" completed "on→runs"
expect "sessions/marketplace.openvscode.latest/inputs"       completed "session→healthy"

# ── Scenario 3: resource status indeterminate (not listed) → launch attempted ──
cat > "${CFG_DIR}/cfg_unknown.json" <<'JSON'
{
  "clusters": {},
  "launch":   { "marketplace.script_submitter.latest": "fail_400" }
}
JSON
run_scenario "a30gpuserver status indeterminate → not silently skipped" "${CFG_DIR}/cfg_unknown.json"
expect "workflows/marketplace.script_submitter.latest/test1" launch_failed "unknown→attempted"
expect "workflows/marketplace.script_submitter.latest/test2" launch_failed "unknown→attempted"

# ── summary ────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  Harness assertions:  PASS=${pass}  FAIL=${fail}"
echo "════════════════════════════════════════════════════════════════════════"
[ "${fail}" -eq 0 ]
