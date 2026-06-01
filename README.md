# PW Workflow Testing Framework

> A CLI and web dashboard for automated testing of Parallel Works ACTIVATE workflows and sessions.

A tool for running automated tests against [Parallel Works ACTIVATE](https://parallelworks.com) workflows using the [`pw` CLI](https://parallelworks.com/docs/cli/pw/workflows). Tests are defined as plain JSON files containing workflow inputs. The framework runs them in parallel, polls for results, and produces a structured output tree with logs and artifacts for every test. A web dashboard lets you browse results and inspect inputs at a glance.

## Quick start

```bash
# Run all tests
python run_tests.py

# Open the dashboard
python serve.py          # → http://localhost:8080
```

## Directory structure

Tests live under a five-level hierarchy:

```
<platform>/<user>/workflows/<workflow>/<test-name>.json
<platform>/<user>/sessions/<workflow>/<test-name>.json
```

| Level | Example | Description |
|---|---|---|
| `platform` | `activate.parallel.works` | Platform host (matches `--platform-host` pw flag) |
| `user` | `alvaro` | Authenticated user who owns the workflow |
| `kind` | `workflows` or `sessions` | Test kind — see below |
| `workflow` | `marketplace.script_submitter.latest` | Workflow name as it appears in `pw workflows ls` |
| `<test>.json` | `test1.json` | Workflow inputs passed verbatim to `pw workflows run -i` |

### Test kinds

| Kind | Success criteria | What happens |
|---|---|---|
| `workflows` | Run reaches `completed` status | Polls until terminal status |
| `sessions` | Session becomes healthy, then run is canceled | Polls until `status=running` + `healthy=true`, then cancels |

Use `sessions` for workflows that start an interactive session and run indefinitely (e.g. VS Code, Desktop, JupyterLab). The run is automatically canceled once the session is confirmed healthy.

### Example layout

```
activate.parallel.works/
└── alvaro/
    ├── workflows/
    │   └── marketplace.script_submitter.latest/
    │       ├── test1.json          # script with sleep
    │       └── test2.json          # fast echo-only script
    └── sessions/
        └── marketplace.openvscode.latest/
            └── inputs.json         # starts a VS Code session
```

## Test file format

Each `.json` file is the raw input object for the workflow, identical to what you would pass with `pw workflows run -i`. No special wrapper or metadata fields are needed.

```json
{
  "resource": {
    "$type": "computeResource",
    "id": "68af39533a57203649e66d6d",
    "ip": "gpu.parallel.works",
    "name": "a30gpuserver",
    "provider": "existing",
    "type": "existing",
    "uri": "pw://alvaro/a30gpuserver",
    "user": "alvaro"
  },
  "script": "echo \"$(date) Running on ${HOSTNAME}\"\ndate"
}
```

## Running tests

```bash
python run_tests.py [options]
```

### Filter by scope

```bash
# All tests for a platform
python run_tests.py --platform activate.parallel.works

# All tests for a user
python run_tests.py --platform activate.parallel.works --user alvaro

# Only session tests
python run_tests.py --platform activate.parallel.works --user alvaro --kind sessions

# Only workflow tests
python run_tests.py --platform activate.parallel.works --user alvaro --kind workflows

# All tests for a specific workflow
python run_tests.py --platform activate.parallel.works --user alvaro \
    --workflow marketplace.script_submitter.latest

# A single test file
python run_tests.py --test-file activate.parallel.works/alvaro/workflows/\
    marketplace.script_submitter.latest/test1.json
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--output-dir DIR` | `./output/` | Root for test output files |
| `--workers N` | `10` | Max parallel test runners |
| `--dry-run` | — | Discover and list tests without running them |

## Web dashboard

`serve.py` starts an HTTP server that reads the `output/` directory and renders a live dashboard.

```bash
python serve.py                        # http://localhost:8080
python serve.py --port 9000            # custom port
python serve.py --output-dir /path/to/output
```

### Dashboard features

- **Summary cards** — total tests, pass rate, failed count, time of last run
- **Breakdowns** — by status, by workflow, by kind (workflows vs sessions)
- **Filterable table** — search by test name or workflow; filter by status, workflow, kind
- **Detail view** — click any row to see the `inputs.json` with syntax highlighting
- **Dark / light theme** — toggled and persisted per browser
- **Auto-refresh** — data reloads every 3 minutes

### Serving behind a reverse proxy

The dashboard works at any URL prefix (e.g. `https://activate.parallel.works/me/session/alvaro/test/`) with no configuration required. The server auto-detects the prefix from incoming request paths and the frontend derives its API base URL from `window.location` at runtime.

If the proxy forwards the full path to the container (no prefix stripping), you can also set the prefix explicitly:

```bash
# CLI flag
python serve.py --prefix /me/session/alvaro/test

# Environment variable
PW_BASE_PATH=/me/session/alvaro/test python serve.py
```

The server also honours the `X-Forwarded-Prefix` header when set by the proxy.

### Dashboard flags

| Flag | Default | Description |
|---|---|---|
| `--host HOST` | `0.0.0.0` | Bind address |
| `--port PORT` | `8080` | Bind port |
| `--output-dir DIR` | `./output/` | Root directory of test outputs to display |
| `--prefix PREFIX` | `$PW_BASE_PATH` | URL prefix to strip (for reverse proxy deployments) |

## Output structure

For every test run, a directory is created that mirrors the test path:

```
output/
└── activate.parallel.works/
    └── alvaro/
        ├── workflows/
        │   └── marketplace.script_submitter.latest/
        │       └── test1/
        │           ├── run.log        # timestamped log of every action
        │           ├── launch.json    # raw JSON from pw workflows run
        │           ├── view.json      # final JSON from pw workflows runs view
        │           ├── errors.txt     # pw workflows runs errors output (failures only)
        │           └── result.json    # compact summary
        └── sessions/
            └── marketplace.openvscode.latest/
                └── inputs/
                    ├── run.log
                    ├── launch.json
                    ├── session.json   # healthy session snapshot
                    └── result.json
```

### `result.json`

```json
{
  "test": "activate.parallel.works/alvaro/workflows/marketplace.script_submitter.latest/test1",
  "kind": "workflows",
  "status": "completed",
  "slug": "mp-script-submitter-00017",
  "duration_s": 28.3,
  "started_at": "2026-05-29T15:34:58Z",
  "error": null
}
```

## Terminal statuses

| Status | Meaning |
|---|---|
| `completed` | **Pass** — run finished successfully (workflows) or session became healthy (sessions) |
| `error` | Run ended in error state |
| `canceled` | Run was canceled (not by the framework) |
| `launch_failed` | `pw workflows run` returned non-zero (includes invalid JSON inputs) |
| `poll_error` | 10+ consecutive failures polling run status |
| `timeout` | Exceeded per-test timeout (1 hour for workflows, 30 min for sessions) |
| `internal_error` | Unhandled exception in the test runner |

## Error handling

- **409 Conflict** — the platform serialises simultaneous runs of the same workflow. The launcher retries up to 5 times with exponential backoff (10 s, 20 s, 40 s, …).
- **ANSI codes** — all `pw` CLI color codes are stripped from captured output before writing to logs or `result.json`.
- **Subprocess timeouts** — every `pw` call has a hard timeout; a hung command becomes `rc=-1` without stalling the test.
- **Invalid inputs** — the inputs file is parsed as JSON before launch; a syntax error is reported as `launch_failed` immediately without hitting the API.

## Prerequisites

- [`pw` CLI](https://parallelworks.com/docs/cli/pw) installed and authenticated (`pw auth`)
- Python 3.9+, no third-party dependencies

## Last test run

<!-- updated by running: python run_tests.py -->

```
Discovered 3 test(s):
  [sess]  activate.parallel.works/alvaro/sessions/marketplace.openvscode.latest/inputs
  [work]  activate.parallel.works/alvaro/workflows/marketplace.script_submitter.latest/test1
  [work]  activate.parallel.works/alvaro/workflows/marketplace.script_submitter.latest/test2

Running 3 test(s) in parallel (workers=10)...

  ✓ [work] …/marketplace.script_submitter.latest/test2  →  COMPLETED  (17.4s)  [mp-script-submitter-00018]
  ✓ [work] …/marketplace.script_submitter.latest/test1  →  COMPLETED  (28.3s)  [mp-script-submitter-00017]
  ✓ [sess] …/marketplace.openvscode.latest/inputs       →  COMPLETED  (106.6s) [mp-openvscode-00061]

================================================================================================
  WORKFLOW TEST RESULTS
================================================================================================
  ✓  [sess]  …/sessions/marketplace.openvscode.latest/inputs        completed    106.6s  mp-openvscode-00061
  ✓  [work]  …/workflows/marketplace.script_submitter.latest/test1  completed     28.3s  mp-script-submitter-00017
  ✓  [work]  …/workflows/marketplace.script_submitter.latest/test2  completed     17.4s  mp-script-submitter-00018
------------------------------------------------------------------------------------------------
  Total: 3  |  Passed: 3  |  Failed: 0
================================================================================================
```
