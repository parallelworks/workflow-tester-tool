# PROBE

PROBE checks that [Parallel Works ACTIVATE](https://parallelworks.com) workflows still
work on the systems connected to a platform. A test launches one workflow with fixed
inputs on one system. PROBE waits for the run to end, deletes the endpoint the run
registered, and records the result. Repeated over time, the records show when a
workflow stops working on a system.

## How it fits together

```
  test definitions (git)                      tests/<platform>/<user>/<workflow_name>/<name>.json
          |
          v
  runner   python3 -m probe run  ──pw workflows run──>  the workflow runs on the target system
          |                                             pass = the run completes
          v
  results bucket (ground truth)      <bucket>/<path>/<platform>/<user>/<workflow_name>/<name>/
          |                                <start>_<run slug>/record.json + logs, one dir per execution
          v
  dashboards   python3 -m probe serve         matrix of workflows by system, history, logs
                                              Refresh reloads the definitions and the results
```

| Who runs the tests | How |
|---|---|
| Admin dashboard | `Run all`, or open a test and `Rerun test` |
| GitHub action **Run PROBE tests** | manual, with `all` or a list of test files |
| Platform, by hand | `pw workflows run --trust -i inputs.json /abs/path/workflow/run-tests.yaml` |
| Shell with the `pw` CLI | `python3 -m probe run ...` |

Every runner uploads each test's results to the bucket as soon as the test finishes.
The dashboards are served by `workflow/workflow.yaml` (GitHub action **Start PROBE
dashboard**).

## Start the dashboards

Run `workflow/workflow.yaml` on the platform, from the GitHub action or by hand.

| Input | Meaning |
|---|---|
| Resource | Where the dashboards run and where tests started from the admin dashboard are launched: the user workspace or a cluster login node with the `pw` CLI, `python3` and `git`. |
| API key | A platform API key of yours (account settings, API keys). The run's own credential stops working when the run completes, so the dashboards use this key afterwards. In the GitHub action it is the platform's repository secret. |
| Test definitions | Repository, branch and directory of the test files. |
| Results | Bucket and path of the results. Required. Restored when the run starts; `Refresh` replaces the dashboard's copy with the bucket's content, so results deleted from the bucket disappear too. `Refresh` also re-fetches the test definitions from their repository. |
| PROBE code | Repository and branch of this code. |

The run completes once both endpoints answer; the dashboards keep running:

- `probe-<run slug>`: read-only, safe to share.
- `probe-admin-<run slug>`: also `Run all`, `Rerun test`, `Cancel run`, and `Delete results` for a test whose definition is gone.

`pw endpoints list` shows their URLs. Take them down with
`pw endpoints delete probe-<run slug>` and `pw endpoints delete probe-admin-<run slug>`.

## Run tests

From the admin dashboard, a test shows as running until its record is written; its
results reach the bucket right after. A request that overlaps a run in progress is
refused.

The GitHub action **Run PROBE tests** deploys `workflow/run-tests.yaml`, waits, and
turns red when a test fails. Both actions authenticate with the repository secrets
`ACTIVATE_PARALLEL_WORKS` and `ACTIVATE_HPC_MIL` (platform API keys). A `schedule`
trigger can be added to `run-tests.yml` to run the suite periodically.

From a shell (the `pw` context selects platform and user; `PW_PLATFORM_HOST` and
`PW_USER` override it):

```bash
python3 -m probe list --tests tests                                  # validate the definitions
python3 -m probe run  --tests tests --results results --bucket pw://alvaro/gcpbucket/probe/results --all
python3 -m probe run  --tests tests --results results --bucket pw://alvaro/gcpbucket/probe/results \
    --test activate.parallel.works/alvaro/webshell/gcpsmall-controller.json
python3 -m probe run  --tests tests --results results --dry-run      # list what would run
python3 -m probe serve --results results --tests tests --port 8080 [--admin] [--bucket URI]
```

Other selectors: `--filter` (id substring or glob), `--id` (exact id). `--keep` leaves
the endpoints running. Without `--bucket` the results stay local. Exit codes: 0 every
test passed or was skipped, 1 a test failed, 2 an invalid definition, a bad selection
or a failed bucket upload. Interrupting the runner cancels its platform runs and records
them as failed. Only tests whose `platform` and `user` match the runner's own are run.

## Test definition

One JSON file per test. The id `<platform>/<user>/<workflow_name>/<name>` comes from the
fields, not from the file's location; `<platform>/<user>/<workflow_name>/<name>.json` is
the recommended place and `python3 -m probe list` notes files found elsewhere. Two files
with the same id, or an unknown key, are errors.

### Edit or remove a test

Tests are files in the tests repository, so both are git changes:

1. **Edit**: change the JSON file (inputs, `timeout_s`, ...), run
   `python3 -m probe list --tests tests` to check it, commit and push.
2. **Remove**: delete the JSON file, commit and push.
3. Press `Refresh` on the dashboard. It re-fetches the definitions, so the change
   shows at once. A run of `run-tests.yaml` fetches them anyway.
4. After a removal the test's history is still in the bucket, so the dashboard keeps
   showing it as a test without a definition. Open it on the admin dashboard and press
   `Delete results` to drop that history too (or `pw buckets rm -r <bucket>/<path>/<id>/`).

Renaming a test (a new `name`) is a removal plus an addition: the history stays under
the old id until you delete it. Tests that came from the workflows repository are
re-created by the next import, so remove or change them there as well.

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
| `workflow.repo`, `.path`, `.ref` | Repository (host and path, or a git URL), path of the workflow YAML, and branch, tag or commit. A branch follows development; a tag or commit pins a release. |
| `timeout_s` | Seconds to wait for the run, default 1800. On timeout the run is canceled and the test fails. |
| `inputs` | Passed verbatim to `pw workflows run -i`. |
| `warm_marker` | Optional. Path, or list of paths, on the target system. All present before launch: phase `warm`; none: `cold`; some: `partial`. |
| `leftover_patterns` | Optional. Process command-line patterns that must be gone from the target system after cleanup; compute tests also require an empty scheduler queue. Processes that existed before the launch are ignored. |
| `leftover_commands` | Optional. `{name: shell snippet}`; each snippet runs on the target after cleanup and must print `0`, for example `docker ps -q \| wc -l` for containers `ps` cannot see. |
| `setup` | Optional. Shell snippet run on the target before the launch, for example to seed input files. Must be safe to repeat. |

What happens to a test:

```
  gate      pw cluster ls (or pw kube ls): resource off or not listed  ──>  skip
  launch    pw workflows run <YAML at ref> -i inputs                    ──>  fail at launch
  wait      pw workflows runs view every 15 s until the run ends        ──>  fail at run (error, timeout)
  verdict   run completed                                               ──>  pass
  cleanup   pw endpoints delete *-<run slug>; leftover check            ──>  cleanup ok | leftover | unknown
  record    record.json + logs written, uploaded to the bucket
```

The workflow itself is responsible for failing its run when its service is not healthy
and completing once it is; PROBE trusts the run status. The target system comes from
`inputs.resource` or `inputs.cluster.resource` (a name, a `pw://user/name` URI or a
resource object); `scheduler: true` marks a compute-node test.

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

The current state of a test is the record of its newest execution; the history is all of
them. Every execution writes only its own directory and uploads it when it finishes, so
runners started anywhere share one bucket without overwriting each other. A directory
without `record.json` is an execution still in progress. Readers ignore malformed
records. To reclaim space, delete the large files of old executions but keep
`record.json`, or the history loses that execution.

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
  "outcome": {"status": "pass", "failed_at": null, "error": null, "phase": "warm",
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
| `outcome.failed_at` | `launch`, `run`, or null. |
| `outcome.error` | One line; the skip reason for skipped tests. Details are in `run.log` and `errors.txt`. |
| `outcome.phase` | `cold`, `warm`, `partial`, or null without `warm_marker`. |
| `outcome.cleanup` | `ok`, `leftover`, `unknown` (the check could not run), `kept` (`--keep`). Independent of `status`. |
| `outcome.run_slug` | Platform handle: `pw workflows runs view <slug>`. |
| `outcome.endpoint` | Endpoint names deleted after the verdict, comma separated; null when the run registered none. |
| `outcome.duration_s` | Test start to verdict, excluding cleanup. |

Every key is always present; not applicable or unknown is null. A regression is a
`pass` followed by a `fail` for the same test, ignoring skips in between; the dashboard
marks it.

The tests in `tests/` are the end-to-end tests recorded in the workflows repository,
converted with `tools/import_workflow_tests.py`:

```bash
python3 tools/import_workflow_tests.py /path/to/workflows --variant general \
    --platform activate.parallel.works --user alvaro --out tests
```

## Development

[DEVELOPER.md](DEVELOPER.md): code layout, offline self-tests, browser check, design
decisions.
