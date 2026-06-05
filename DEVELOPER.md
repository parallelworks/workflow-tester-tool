# Developer Guide

This document covers the internals of the Workflow Status Monitor: how to add tests, run the stack locally, and understand how the pieces fit together.

---

## Repository layout

```
workflow-tester-tool/
├── workflow/
│   └── workflow.yaml          # ACTIVATE workflow definition
├── web/
│   ├── index.html             # Public dashboard
│   ├── admin.html             # Admin dashboard (Rerun / Cancel)
│   └── assets/
│       ├── css/styles.css
│       └── js/
│           ├── app.js         # Dashboard logic (shared by both pages)
│           └── page-utils.js  # Theme toggle, relative-time formatter
├── serve.py                   # HTTP server for both dashboards
├── run_tests.py               # Test runner
├── .github/
│   └── workflows/
│       └── run-workflow.yml   # GitHub Actions dispatch workflow
└── <platform>/<user>/...      # Test definition files (JSON)
```

---

## Adding tests

### Directory hierarchy

Tests are plain JSON files in a five-level path:

```
<platform>/<user>/<kind>/<workflow>/<test-name>.json
```

| Level | Example | Notes |
|---|---|---|
| `platform` | `activate.parallel.works` | Must match the `--platform-host` value used by the `pw` CLI |
| `user` | `alvaro` | Authenticated user who owns the workflow on that platform |
| `kind` | `workflows` or `sessions` | Controls how the test is evaluated — see below |
| `workflow` | `marketplace.script_submitter.latest` | Workflow slug from `pw workflows ls` |
| `<test>.json` | `test1.json` | Any name; becomes the test's display name in the dashboard |

### Test kinds

| Kind | Pass criteria | Framework behaviour |
|---|---|---|
| `workflows` | Run reaches `completed` status | Polls until a terminal status appears (max 1 hour) |
| `sessions` | Session becomes healthy, then run is canceled | Polls run status until `running`, then polls sessions until `healthy`, then cancels (max 30 min) |

Use `sessions` for workflows that start an interactive session and never complete on their own (VS Code, JupyterLab, Desktop, etc.).

### Resource (cluster) gate

If a test's inputs contain a `resource` with a `pw://…` `uri`, the runner first
calls `pw cluster ls <uri> -o json` and reads the cluster's status:

- **on** (`active`/`on`/`running`/…) → launch the test as normal.
- **off** (`off`/`stopped`/`failed`/`provisioning`/…) → report `skipped`; the
  test is *not* launched, and it does **not** count as a failure.
- **indeterminate** (lookup failed, resource not listed, unrecognised status) →
  a warning is logged and the test is launched anyway, so a transient glitch
  never silently skips everything.

Tests whose resource has no `pw://` URI (e.g. a `managed-cluster` provisioned by
the workflow itself) are never gated. Skip decisions and the observed status are
written to each test's `run.log` under the `RESOURCE CHECK` section.

### Test file format

Each `.json` file is the raw input object passed verbatim to `pw workflows run -i`. No wrapper or metadata needed.

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
  "script": "echo hello"
}
```

> **Tip:** run a workflow once through the ACTIVATE UI, then export its inputs with `pw workflows runs view <slug> -o json` and copy the `inputs` field.

### Example layout

```
activate.parallel.works/
└── alvaro/
    ├── workflows/
    │   └── marketplace.script_submitter.latest/
    │       ├── test1.json
    │       └── test2.json
    └── sessions/
        └── marketplace.openvscode.latest/
            └── inputs.json
```

---

## Running locally

### Prerequisites

- Python 3.9+, no third-party packages
- [`pw` CLI](https://parallelworks.com/docs/cli/pw) authenticated (`pw auth`)

### Run the test suite

```bash
# All tests discovered in the current directory
python run_tests.py

# Filter by scope
python run_tests.py --platform activate.parallel.works
python run_tests.py --platform activate.parallel.works --user alvaro
python run_tests.py --platform activate.parallel.works --user alvaro --kind sessions
python run_tests.py --platform activate.parallel.works --user alvaro \
    --workflow marketplace.script_submitter.latest

# Single test file
python run_tests.py --test-file activate.parallel.works/alvaro/workflows/\
    marketplace.script_submitter.latest/test1.json

# Discover tests without running them
python run_tests.py --dry-run
```

| Flag | Default | Description |
|---|---|---|
| `--output-dir DIR` | `./output/` | Root for test output files |
| `--workers N` | `10` | Max parallel test runners |
| `--dry-run` | — | List discovered tests, do not execute |

### Serve the dashboards

```bash
# Public dashboard on port 8080
python serve.py

# Admin dashboard (enables Rerun / Cancel API)
python serve.py --admin

# Custom port and output directory
python serve.py --port 9000 --output-dir /path/to/output

# Behind a reverse proxy at a fixed prefix
PW_BASE_PATH=/me/session/alvaro/test python serve.py
```

| Flag | Default | Description |
|---|---|---|
| `--host HOST` | `0.0.0.0` | Bind address |
| `--port PORT` | `8080` | Bind port |
| `--output-dir DIR` | `./output/` | Root directory of test outputs |
| `--prefix PREFIX` | `$PW_BASE_PATH` | URL prefix to strip (reverse proxy deployments) |
| `--admin` | off | Enable admin API and serve `admin.html` as the root page |

---

## Output structure

Each test run produces a directory that mirrors the test path under `output/`:

```
output/
└── activate.parallel.works/
    └── alvaro/
        ├── workflows/
        │   └── marketplace.script_submitter.latest/
        │       └── test1/
        │           ├── result.json    # compact summary (always written)
        │           ├── run.log        # timestamped log of every action
        │           ├── launch.json    # raw JSON from pw workflows run
        │           ├── view.json      # final JSON from pw workflows runs view
        │           └── errors.txt     # pw workflows runs errors (failures only)
        └── sessions/
            └── marketplace.openvscode.latest/
                └── inputs/
                    ├── result.json
                    ├── run.log
                    ├── launch.json
                    └── session.json   # healthy session snapshot
```

### `result.json` schema

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

---

## Architecture

### `run_tests.py`

- Discovers test JSON files with `discover_tests()` (glob over the five-level hierarchy)
- Runs up to `--workers` tests concurrently via `ThreadPoolExecutor`
- Resource gate (`_resource_gate()`) checks `pw cluster ls` before launch and reports `skipped` when the target resource is off
- Two dispatchers: `_run_workflow_test()` and `_run_session_test()`
- Shell helper `_cmd()` wraps every `pw` call with a timeout and ANSI stripping
- Stale per-run artifacts (`launch.json`/`view.json`/`errors.txt`/`session.json`) are cleared at the start of each test
- Active run slugs are tracked in `_active` so SIGTERM/SIGINT cancels them on the platform
- 409 Conflict on launch → exponential backoff retry (up to 5 attempts: 10 s, 20 s, 40 s, …)
- Exit code reflects failures only (`skipped` never fails the suite). The `workflow.yaml` `run_tests` step swallows the exit code so test failures don't tear down the dashboard sessions.

### `serve.py`

- `ThreadingHTTPServer` backed by `DashboardHandler` (extends `SimpleHTTPRequestHandler`)
- `GET /api/results` — scans `output/` for `result.json` files and returns aggregated JSON
- `GET /api/results/<test>/inputs` — returns the matching `<test>.json` inputs file
- In `--admin` mode:
  - Serves `admin.html` as the root page instead of `index.html`
  - `POST /api/run-test` — spawns `run_tests.py --test-file` in a background thread
  - `POST /api/cancel` — calls `pw workflows runs cancel <slug>` synchronously
  - `OPTIONS` — CORS preflight response
- URL prefix detection: strips `PW_BASE_PATH` / `--prefix` / `X-Forwarded-Prefix` header so the server works at any reverse-proxy path

### Web frontend (`web/`)

- `index.html` / `admin.html` — inject a `<base>` tag at load time so all relative fetch calls resolve correctly behind a proxy. `admin.html` also sets `window.IS_ADMIN = true`.
- `app.js` — shared module for both pages. Fetches `/api/results`, renders the table, handles filters and the detail panel. When `IS_ADMIN`, `registerAdminEvents()` wires the Rerun and Cancel buttons and shows feedback banners.
- `page-utils.js` — theme toggle (dark/light) persisted in `localStorage`, relative-time formatter.
- `styles.css` — design system with OKLCH-based dark and light themes; admin-specific classes (`.admin-banner`, `.admin-badge`, `.admin-actions`, `.ghost-btn.danger`).

### `workflow/workflow.yaml`

Six parallel jobs, all depending on `setup`:

| Job | Purpose |
|---|---|
| `setup` | Checkout repo, allocate two ports, restore outputs from bucket |
| `run_tests` | Run the full test suite, upload results to bucket |
| `serve` | Start `serve.py` (public, read-only) on `SESSION_PORT` |
| `serve_admin` | Start `serve.py --admin` on `ADMIN_PORT` |
| `create_session` | Register the public dashboard as an ACTIVATE session |
| `create_session_admin` | Register the admin dashboard as an ACTIVATE session |

`serve` and `serve_admin` run `sleep inf` so the sessions stay alive for the life of the workflow run. Both are cleaned up (process killed) when the workflow is canceled or the run ends.

---

## GitHub Actions CI

`.github/workflows/run-workflow.yml` is a `workflow_dispatch` action with these inputs:

| Input | Type | Notes |
|---|---|---|
| `platform` | choice | `activate.parallel.works` or `activate.hpc.mil` |
| `workflow_name` | string | Registered workflow slug (e.g. `workflowstatusmonitor`) |
| `resource` | string | Resource URI |
| `bucket` | string | Bucket URI |
| `bucket_path` | string | Optional; leave blank for the default |
| `run_tests` | boolean | |
| `serve_dashboard` | boolean | |

The action selects the correct repository secret based on the platform choice:

| Platform | Secret |
|---|---|
| `activate.parallel.works` | `ACTIVATE_PARALLEL_WORKS` |
| `activate.hpc.mil` | `ACTIVATE_HPC_MIL` |

Store these as **repository secrets** (not environment secrets) under `Settings → Secrets and variables → Actions`.

---

## Branching model

| Branch | Purpose |
|---|---|
| `main` | Stable; `workflow.yaml` checks out `main` |
| `dev` | Active development; PRs merge here first, then to `main` |
