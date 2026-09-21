# PROBE

PROBE runs compatibility tests for [Parallel Works ACTIVATE](https://parallelworks.com)
workflows across the systems connected to a platform and shows the results in a
dashboard. Each test launches one workflow from a git repository with fixed inputs on
one system, waits for the verdict, cleans up, and appends a record. Runs repeat on a
schedule, so a workflow that stops working on a system shows up as a regression.

PROBE is Milestone 0002 of the FY27 HPCMP prototype extension.

## How it works

```
tests/<platform>/<user>/<workflow_name>/<test>.json      one file per test
   |
   |  python3 -m probe run          launches with the pw CLI, one record per execution
   v
results/<platform>/<user>/<workflow_name>/<test>/
   records.jsonl                                       append only
   <start time>_<run slug>/run.log, launch.json, view.json, errors.txt
   |
   |  python3 -m probe serve        dashboard: matrix of workflows by system, history, logs
   v
pw endpoint  probe-<run slug>
```

The runner and the dashboard are one Python package (`probe/`, standard library only,
Python 3.8 or newer). The test definitions can live in this repository or in any other
git repository, for example a deployment's own GitLab.

## Launching PROBE on the platform

Register `workflow/workflow.yaml` as a workflow (or run it directly with
`pw workflows run /abs/path/workflow/workflow.yaml -i inputs.json`) and fill in the form:

| Input | Meaning |
|---|---|
| Resource | Where PROBE runs: the user workspace or the login node of a connected cluster. It needs the `pw` CLI (installed with the agent), `python3` and `git`. |
| Test definitions | Repository, branch and directory holding the test files. Default: this repository, `main`, `tests`. |
| Test filter | Optional. Only tests whose id contains this text or matches this glob. |
| Results | Bucket and path where results are kept between runs. Optional; without a bucket the results live only as long as the run. |
| Schedule | Repeat every N hours. 0 runs the suite once. |
| Run tests | No serves the dashboard of stored results without running anything. |
| Serve dashboard | Registers the endpoint `probe-<run slug>`. |
| Serve admin dashboard | Also registers `probe-admin-<run slug>`, where tests can be rerun and runs canceled. |
| PROBE code | Repository and branch the runner and dashboard are fetched from. |

The run stays alive for as long as the dashboard should be reachable. Cancel the run
to take the dashboard down. Only the tests whose `platform` and `user` match the
run's platform and user are executed.

The GitHub Actions workflow `.github/workflows/run-workflow.yml` launches a
registered PROBE workflow from CI with the same inputs.

## Test definitions

A test is one self-contained JSON file. Files can be anywhere under the tests
directory; the recommended layout is `<platform>/<user>/<workflow_name>/<test>.json`.
The test id is `<platform>/<user>/<workflow_name>/<file name without .json>`, built
from the file's fields and name, not from its location. Two files with the same id are
an error, as is any unknown key.

```json
{
  "platform": "activate.parallel.works",
  "user": "alvaro",
  "workflow_name": "webshell",
  "workflow": {
    "repo": "github.com/parallelworks/workflows",
    "path": "workflows/webshell/yamls/general.yaml",
    "ref": "canary"
  },
  "kind": "endpoint",
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
| `platform`, `user` | Platform host and user that run the test. |
| `workflow_name` | Groups tests of the same workflow, for example the workflow's directory name. |
| `workflow.repo`, `workflow.path` | Repository (host and path, or a full git URL) and path of the workflow YAML. |
| `workflow.ref` | Branch, tag or commit. A branch follows development (canary test); a tag or commit pins a release. |
| `kind` | `batch`: pass when the run completes. `endpoint`: pass when the run completes; the `pw` endpoints named `*-<run slug>` are deleted afterwards. |
| `timeout_s` | Seconds to wait for the verdict. Default 1800. On timeout the run is canceled and the test fails. |
| `inputs` | Passed verbatim to `pw workflows run -i`. |
| `http_expect` | Optional, endpoint tests. HTTP status code, or list of codes, the endpoint URL must answer with before it is deleted. Off by default: the workflow already checks its endpoint before completing. |
| `warm_marker` | Optional. Path, or list of paths, on the target system. All present before launch: phase `warm`; none: `cold`; some: `partial`. |
| `leftover_patterns` | Optional. Process command-line patterns that must be gone from the target system after cleanup; on compute tests the scheduler queue must be empty too. Processes that existed before the launch are ignored. |

The verdict is the run status. A workflow is responsible for failing its run when its
service is not healthy and for completing once it is; PROBE does not judge the service
itself unless `http_expect` asks for it.

Before launching, PROBE checks the target with `pw cluster ls` (or `pw kube ls` for
Kubernetes). A resource that is off or not listed skips the test instead of failing it.

The target system is read from `inputs.resource` or `inputs.cluster.resource`: a name,
a `pw://user/name` URI or a resource object. `scheduler: true` marks the test as a
compute-node test.

## Results

```
results/<platform>/<user>/<workflow_name>/<test>/
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
overwritten, and readers ignore malformed lines. With a results bucket configured, the
workflow restores the tree before a suite and syncs it back afterwards.

One record:

```json
{
  "schema": 1,
  "suite_run": "probe-2026-09-21T15:09Z",
  "pw_cli": "v7.99.0",
  "test": {"id": "activate.parallel.works/alvaro/webshell/gcpsmall-compute", "workflow_name": "webshell", "kind": "endpoint"},
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
| `suite_run` | Label of the suite run this execution belonged to. |
| `workflow.commit` | Commit `ref` resolved to at launch. |
| `target.type`, `target.node` | `cluster` or `kubernetes`; `controller` or `compute` (null on Kubernetes). |
| `outcome.status` | `pass`, `fail`, `skip`. |
| `outcome.failed_at` | `launch`, `run`, `endpoint`, `http`, or null. |
| `outcome.error` | One line. The skip reason for skipped tests. Details are in the artifact directory. |
| `outcome.phase` | `cold`, `warm`, `partial`, or null when the test has no `warm_marker`. |
| `outcome.http` | Status code seen by `http_expect`, else null. |
| `outcome.cleanup` | `ok`, `leftover`, `unknown` (the check could not run), `kept` (`--keep`). Independent of `status`. |
| `outcome.run_slug` | Platform handle: `pw workflows runs view <slug>`. |
| `outcome.endpoint` | Endpoint names deleted after the verdict, comma separated; null for batch tests. |
| `outcome.duration_s` | Test start to verdict, excluding cleanup. |

Every key is always present; not applicable or unknown is null. A regression is a
`pass` followed by a `fail` for the same test id (skips in between are ignored).

## Dashboard

The dashboard shows a summary row (tests, passing, failing, skipped, regressions, last
suite run), the compatibility matrix of workflows by system, the table of tests with
their recent history, and the suite runs. Filters and the selected test are kept in the
page URL so a view can be shared. Clicking a test opens its history, the `run.log` and
other artifacts of every execution, its definition and its full current record. The
admin dashboard adds rerun and cancel actions. Pages use system fonts and load nothing
from the internet.

## Running locally

```bash
pw auth                                   # once; the pw context selects platform and user
python3 -m probe list --tests tests       # show the definitions and report invalid ones
python3 -m probe run --tests tests --results results --dry-run
python3 -m probe run --tests tests --results results
python3 -m probe run --tests tests --results results --filter webshell --keep
python3 -m probe serve --results results --tests tests --port 8080 [--admin]
```

`run` exits 0 when every test passed or was skipped, 1 when a test failed, 2 when a
definition is invalid. `PW_PLATFORM_HOST` and `PW_USER` override the pw context, as the
platform sets them inside a workflow run. Interrupting the runner cancels its
platform runs and records them as failed.

See [DEVELOPER.md](DEVELOPER.md) for the code layout, the offline self-tests and the
design decisions.
