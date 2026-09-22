# PROBE

PROBE runs compatibility tests of [Parallel Works ACTIVATE](https://parallelworks.com)
workflows on the systems connected to a platform and shows the results in a dashboard.
A test launches one workflow from a git repository with fixed inputs on one system.
PROBE waits for the run to end, deletes the endpoints the run registered, and appends a
record. Runs are repeated over time, so a workflow that stops working on a system shows
up as a regression.

## The pieces

```
 test definitions                 runner                          results bucket
 tests/<platform>/<user>/         python3 -m probe run            <bucket>/<path>/<test id>/
   <workflow_name>/<name>.json ─▶  launches pw workflows run  ─▶    <start>_<run slug>/record.json
 (this repo or any git repo)       one directory per execution         + run.log, launch.json, view.json
                                        ▲                                   │
              workflow/run-tests.yaml ──┘                                   ▼
              (admin dashboard, GitHub "Run PROBE tests",          dashboard  python3 -m probe serve
               or pw workflows run by hand)                        workflow/workflow.yaml, two pw endpoints,
                                                                   Refresh pulls the bucket
```

| Piece | What it is |
|---|---|
| Test definitions | One JSON file per test. Recommended location `<platform>/<user>/<workflow_name>/<name>.json`, in this repository's `tests/` or in any git repository you point the workflows at. |
| Results bucket | The ground truth. `<bucket>/<path>/<platform>/<user>/<workflow_name>/<name>/` holds one directory per execution with its `record.json` and artifacts. Every runner uploads an execution's directory as soon as the test finishes; nothing is ever overwritten. |
| Runner | `python3 -m probe run`: checks the target is on, launches the workflow, waits for the verdict, cleans up, appends the record and uploads it. |
| Dashboard | `python3 -m probe serve`: the compatibility matrix, per-test history and logs. `Refresh` pulls the latest results from the bucket. The admin dashboard can also start tests and cancel runs. |
| `workflow/workflow.yaml` | Platform workflow that starts the two dashboards as `pw` endpoints `probe-<run slug>` (read-only) and `probe-admin-<run slug>`. Like the session workflows, the run completes once both endpoints answer and the dashboards keep serving until their endpoints are deleted. Runs no tests. |
| `workflow/run-tests.yaml` | Platform workflow that runs all tests, or the listed test files, once and exits. Ends in error when a test fails. |
| `.github/workflows/dashboard.yml` | GitHub action, manual: deploys `workflow.yaml` on the platform. |
| `.github/workflows/run-tests.yml` | GitHub action, manual or nightly (06:00 UTC): deploys `run-tests.yaml`, waits, and fails when a test fails. |

The code is the `probe/` package (Python 3.8+, standard library only) and `web/`.

## Start the dashboard

Run `workflow/workflow.yaml` on the platform: from the GitHub action **Start PROBE
dashboard**, or by hand with `pw workflows run --trust -i inputs.json
/abs/path/workflow/workflow.yaml`.

| Input | Meaning |
|---|---|
| Resource | Where the dashboards run and where tests started from the admin dashboard are launched: the user workspace or a cluster login node with the `pw` CLI, `python3` and `git`. |
| Test definitions | Repository, branch and directory of the test files. |
| Results | Bucket and path of the results. Restored when the run starts; `Refresh` pulls them again. |
| PROBE code | Repository and branch of this code. |

The run completes as soon as both endpoints answer; `pw endpoints list` shows their
URLs. The dashboards keep running on the resource until you delete them:
`pw endpoints delete probe-<run slug>` and `pw endpoints delete probe-admin-<run slug>`.

## Run tests

Any of these runs tests. All of them write to the bucket, and the dashboard shows the
new results after `Refresh`.

- **Admin dashboard**: `Run all`, or open a test and press `Rerun test`. Results show up
  without a refresh, since this runner writes the dashboard's own copy too.
- **GitHub action "Run PROBE tests"**: dispatch it with `all` or a list of test files,
  or let the nightly schedule run everything. It waits for the platform run and turns
  red when a test fails. For the schedule, set the repository variables
  `PROBE_PLATFORM`, `PROBE_RESOURCE`, `PROBE_BUCKET` and `PROBE_BUCKET_PATH`
  (defaults: `activate.parallel.works`, `pw://alvaro/gcpsmall`, `pw://alvaro/gcpbucket`,
  `probe/results`). The platform API keys are the repository secrets
  `ACTIVATE_PARALLEL_WORKS` and `ACTIVATE_HPC_MIL`.
- **By hand on the platform**: `pw workflows run --trust -i inputs.json
  /abs/path/workflow/run-tests.yaml`, with `selection` set to `all` or to test files
  relative to the tests directory.
- **Command line** on a machine with the `pw` CLI authenticated (the context selects
  platform and user):

```bash
python3 -m probe list --tests tests                                 # validate the definitions
python3 -m probe run --tests tests --results results --bucket pw://alvaro/gcpbucket/probe/results --all
python3 -m probe run --tests tests --results results --bucket pw://alvaro/gcpbucket/probe/results \
    --test activate.parallel.works/alvaro/webshell/gcpsmall-controller.json
python3 -m probe run --tests tests --results results --dry-run     # list what would run
python3 -m probe run --tests tests --results results --keep        # leave endpoints running
python3 -m probe serve --results results --tests tests --port 8080 [--admin] [--bucket URI]
```

`--filter` (id substring or glob) and `--id` (exact id) are the other selectors. Without
`--bucket` the results stay in the local results directory. Exit codes: 0 every test
passed or was skipped, 1 a test failed, 2 an invalid definition, a bad selection or a
failed bucket transfer. `PW_PLATFORM_HOST` and `PW_USER` override the `pw` context; the
platform sets them inside a workflow run. Interrupting the runner cancels its platform
runs and records them as failed. Only tests whose `platform` and `user` match the runner's
own are executed.

## Test definitions

One self-contained JSON file per test. The test id is
`<platform>/<user>/<workflow_name>/<name>`, built from the file's fields alone; the
file's location does not matter, but `<platform>/<user>/<workflow_name>/<name>.json` is
the recommended one and `python3 -m probe list` notes files found elsewhere. Two files
with the same id are an error, as is any unknown key.

```json
{
  "name": "gcpsmall-controller",
  "platform": "activate.parallel.works",
  "user": "alvaro",
  "workflow_name": "webshell",
  "workflow": {
    "repo": "github.com/parallelworks/workflows",
    "path": "workflows/webshell/yamls/general.yaml",
    "ref": "canary"
  },
  "timeout_s": 1200,
  "warm_marker": "${HOME}/pw/software/noVNC-1.3.0/ttyd.x86_64",
  "leftover_patterns": ["ttyd", "pw endpoints run"],
  "inputs": {
    "cluster": { "resource": "pw://alvaro/gcpsmall", "scheduler": false },
    "service": {}
  }
}
```

| Field | Meaning |
|---|---|
| `name` | Test name, unique within its platform, user and workflow. |
| `platform`, `user` | Platform host and user that run the test. |
| `workflow_name` | Groups the tests of one workflow, for example its directory name. |
| `workflow.repo`, `workflow.path` | Repository (host and path, or a full git URL) and path of the workflow YAML. |
| `workflow.ref` | Branch, tag or commit. A branch follows development (canary test); a tag or commit pins a release. |
| `timeout_s` | Seconds to wait for the verdict, default 1800. On timeout the run is canceled and the test fails. |
| `inputs` | Passed verbatim to `pw workflows run -i`. |
| `http_expect` | Optional. HTTP status code, or list of codes, the run's endpoint must answer with before it is deleted; a run that registered no endpoint then fails. Off by default: the workflow already checks its endpoint before completing. |
| `warm_marker` | Optional. Path, or list of paths, on the target system. All present before launch: phase `warm`; none: `cold`; some: `partial`. |
| `leftover_patterns` | Optional. Process command-line patterns that must be gone from the target system after cleanup; compute tests also require an empty scheduler queue. Processes that existed before the launch are ignored. |

Every test is judged the same way: it passes when its run completes. A workflow fails
its run when its service is not healthy and completes once it is. After the verdict
PROBE deletes every endpoint named `*-<run slug>`; a workflow that registers none has
nothing to delete. Before launching, PROBE checks the target with `pw cluster ls` (or
`pw kube ls`); a resource that is off or not listed skips the test. The target comes
from `inputs.resource` or `inputs.cluster.resource` as a name, a `pw://user/name` URI or
a resource object; `scheduler: true` marks a compute-node test.

## Results

```
<bucket>/<path>/<platform>/<user>/<workflow_name>/<name>/
|-- 2026-09-21T150902Z_swift-falcon/      one directory per execution: <start time>_<run slug>
|   |-- record.json                       the record (below)
|   |-- run.log                           what PROBE did and saw, with timestamps
|   |-- launch.json                       the run object returned by pw workflows run
|   |-- view.json                         the last pw workflows runs view of the run
|   `-- errors.txt                        pw workflows runs errors, failed runs only
|-- 2026-09-21T150902Z_skip/              a skipped execution: record.json and run.log
`-- 2026-09-21T150902Z_launch-failed/     a launch that produced no run
```

The current state of a test is the record of its newest execution directory; the
history is all of them. Every execution writes only its own directory and uploads it
when it finishes, so runners started from the dashboard, from GitHub or by hand can
share one bucket without overwriting each other. A directory without `record.json` is
an execution still in progress. Readers ignore malformed records. To reclaim space,
delete the large files of old executions but keep `record.json`, or the history loses
that execution.

```json
{
  "schema": 1,
  "suite_run": "probe-2026-09-21T15:09Z",
  "pw_cli": "v7.99.0",
  "test": {"id": "activate.parallel.works/alvaro/webshell/gcpsmall-compute", "workflow_name": "webshell"},
  "workflow": {"repo": "github.com/parallelworks/workflows", "path": "workflows/webshell/yamls/general.yaml",
               "ref": "canary", "commit": "031eb00c31c1af10de5868c78865e3000004e583"},
  "target": {"platform": "activate.parallel.works", "user": "alvaro", "system": "gcpsmall",
             "resource": "pw://alvaro/gcpsmall", "type": "cluster", "node": "compute"},
  "outcome": {"status": "pass", "failed_at": null, "error": null, "phase": "warm", "http": null,
              "cleanup": "ok", "run_slug": "swift-falcon", "endpoint": "webshell-swift-falcon",
              "started_at": "2026-09-21T15:09:02Z", "ended_at": "2026-09-21T15:13:09Z", "duration_s": 247}
}
```

| Field | Values |
|---|---|
| `suite_run` | Label of the run of the suite this execution belonged to. |
| `workflow.commit` | Commit `ref` resolved to at launch. |
| `target.type`, `target.node` | `cluster` or `kubernetes`; `controller` or `compute` (null on Kubernetes). |
| `outcome.status` | `pass`, `fail`, `skip`. |
| `outcome.failed_at` | `launch`, `run`, `endpoint`, `http`, or null. |
| `outcome.error` | One line; the skip reason for skipped tests. Details are in the artifact directory. |
| `outcome.phase` | `cold`, `warm`, `partial`, or null without `warm_marker`. |
| `outcome.http` | Status code seen by `http_expect`, else null. |
| `outcome.cleanup` | `ok`, `leftover`, `unknown` (the check could not run), `kept` (`--keep`). Independent of `status`. |
| `outcome.run_slug` | Platform handle: `pw workflows runs view <slug>`. |
| `outcome.endpoint` | Endpoint names deleted after the verdict, comma separated; null when the run registered none. |
| `outcome.duration_s` | Test start to verdict, excluding cleanup. |

Every key is always present; not applicable or unknown is null. A regression is a
`pass` followed by a `fail` for the same test id, ignoring skips in between.

## Dashboard

Summary tiles (tests, passing, failing, skipped, regressions, last suite run), the
compatibility matrix of workflows by system, the table of tests with their recent
history, and the suite runs. `Refresh` pulls the results from the bucket. Filters and
the selected test are kept in the URL, so a view can be shared. A test opens into its
history, the `run.log` and other artifacts of every execution, its definition and its
current record. The pages use system fonts and load nothing from the internet.

## Development

[DEVELOPER.md](DEVELOPER.md) covers the code layout, the offline self-tests
(`python3 -m unittest discover -s selftest -t .`) and the design decisions.
