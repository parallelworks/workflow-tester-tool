# PROBE

PROBE runs compatibility tests of [Parallel Works ACTIVATE](https://parallelworks.com)
workflows on the systems connected to a platform and shows the results in a dashboard.
A test launches one workflow from a git repository with fixed inputs on one system.
PROBE waits for the run to end, deletes the endpoints the run registered, and appends a
record.

| | |
|---|---|
| Test definitions | One JSON file per test under `tests/` in this repository, or under any directory of any git repository given to the workflow. Recommended location: `<platform>/<user>/<workflow_name>/<name>.json` |
| Results | `<bucket>/<path>/<platform>/<user>/<workflow_name>/<test>/`: `records.jsonl` plus one directory of artifacts per execution. The bucket is the ground truth; each test's results are uploaded as soon as it finishes |
| Running tests | The admin dashboard (`Run all`, `Rerun test`) or `python3 -m probe run` |
| Code | `probe/` runner and dashboard server (Python 3.8+, standard library only), `web/` dashboard, `workflow/workflow.yaml` deployment |

## Deploy

Run `workflow/workflow.yaml` on the platform, either registered as a workflow or with
`pw workflows run --trust -i inputs.json /abs/path/workflow/workflow.yaml`.

| Input | Meaning |
|---|---|
| Resource | Where PROBE runs: the user workspace or a cluster login node with the `pw` CLI, `python3` and `git`. Tests are launched from here. |
| Test definitions | Repository, branch and directory of the test files. Default: this repository, `main`, `tests`. |
| Results | Bucket and path of the results. Required. Restored when the run starts, updated after every test. |
| PROBE code | Repository and branch of the runner and dashboard code. |

The run registers two endpoints and lives until it is canceled:

- `probe-<run slug>`: read-only dashboard, safe to share.
- `probe-admin-<run slug>`: same view plus `Run all`, `Rerun test` and `Cancel run`.

Nothing runs at deployment. Only tests whose `platform` and `user` match the run's own
can be executed from it.

## Run tests

From the admin dashboard: `Run all` starts every test of this platform and user;
opening a test and pressing `Rerun test` starts that one. A test shows as running until
its record is written; the results reach the bucket right after. A request that overlaps
a run still in progress is refused.

From a shell with the `pw` CLI authenticated (the context selects platform and user):

```bash
python3 -m probe list --tests tests                                     # validate the definitions
python3 -m probe run --tests tests --results results --bucket pw://alvaro/gcpbucket/probe/results
python3 -m probe run --tests tests --results results --filter webshell  # id substring or glob
python3 -m probe run --tests tests --results results --id activate.parallel.works/alvaro/webshell/gcpsmall-controller
python3 -m probe run --tests tests --results results --dry-run          # list what would run
python3 -m probe run --tests tests --results results --keep             # leave endpoints running
python3 -m probe serve --results results --tests tests --port 8080 [--admin] [--bucket URI]
```

Without `--bucket` the results stay in the local results directory. Exit codes: 0 every
test passed or was skipped, 1 a test failed, 2 an invalid definition or a failed bucket
upload. `PW_PLATFORM_HOST` and `PW_USER` override the `pw` context; the platform sets
them inside a workflow run. Interrupting the runner cancels its platform runs and
records them as failed.

## Test definitions

One self-contained JSON file per test. The test id is
`<platform>/<user>/<workflow_name>/<name>`, built from the file's fields alone; where
the file sits does not matter. The recommended location is
`<platform>/<user>/<workflow_name>/<name>.json`, and `python3 -m probe list` notes files
found elsewhere. Two files with the same id are an error, as is any unknown key.

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
nothing to delete. Before launching, PROBE checks the target with
`pw cluster ls` (or `pw kube ls`); a resource that is off or not listed skips the test.
The target comes from `inputs.resource` or `inputs.cluster.resource` as a name, a
`pw://user/name` URI or a resource object; `scheduler: true` marks a compute-node test.

Tests can live in a separate repository, for example a deployment's own git server.
Point the workflow's `Test definitions` inputs at it; the layout is the same.

## Results

```
<bucket>/<path>/<platform>/<user>/<workflow_name>/<test>/
|-- records.jsonl                         one line per execution, append only
|-- 2026-09-21T150902Z_swift-falcon/      <start time>_<run slug>
|   |-- run.log                           what PROBE did and saw, with timestamps
|   |-- launch.json                       the run object returned by pw workflows run
|   |-- view.json                         the last pw workflows runs view of the run
|   `-- errors.txt                        pw workflows runs errors, failed runs only
`-- 2026-09-21T150902Z_launch-failed/     a launch that never produced a run
```

The current state of a test is the last line of its `records.jsonl`; the history is
every line. Skipped tests append a line but no artifact directory. Nothing is ever
overwritten and readers ignore malformed lines. One deployment per bucket path: two
runners writing the same `records.jsonl` would overwrite each other.

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
history, and the suite runs. Filters and the selected test are kept in the URL, so a
view can be shared. A test opens into its history, the `run.log` and other artifacts of
every execution, its definition and its current record. The pages use system fonts and
load nothing from the internet.

## Development

[DEVELOPER.md](DEVELOPER.md) covers the code layout, the offline self-tests
(`python3 -m unittest discover -s selftest -t .`) and the design decisions.
