# PROBE developer guide

## Layout

```
probe/__main__.py     command line: run, serve, list
probe/definitions.py  test definition files: validation, ids (from the fields, not the path), target derivation
probe/pw.py           pw CLI wrapper, git checkout of workflow YAMLs
probe/runner.py       one run of the suite: gate, launch, poll, verdict, cleanup, record, bucket sync
probe/results.py      results tree: one record.json per execution directory, state and history
probe/server.py       dashboard server: static web/ plus the JSON API
web/                  index.html, app.js, styles.css (no build step, no external assets)
tests/                test definitions shipped with this repository
selftest/             offline unit tests with a mock pw CLI
workflow/workflow.yaml  platform workflow: the two dashboards
workflow/run-tests.yaml platform workflow: run all or selected tests once
.github/workflows/    dashboard.yml deploys workflow.yaml; run-tests.yml deploys run-tests.yaml (manual or nightly)
```

No third-party Python packages. The code targets Python 3.8; cluster login nodes run
3.6 to 3.12 (gcpsmall has 3.9).

## Self-tests

```bash
python3 -m unittest discover -s selftest -t .
```

No platform needed. `selftest/mockbin/pw` stands in for the CLI and follows a JSON
configuration (see its docstring); every test also gets a small local git repository
shaped like `parallelworks/workflows`. Covered: definition validation, target
derivation, every verdict path (pass, skip, launch failure, run error, timeout,
`http_expect`, leftovers, `--keep`), record and artifact layout, bucket sync, the
results reader (regressions, running detection, malformed lines) and the server API
including path safety and the read-only refusal of admin actions. About a minute.

### Browser check

`bash selftest/ui_check.sh` starts a local admin dashboard on the mock CLI with a
seeded results tree and drives every control in headless Chrome over the DevTools
protocol (`selftest/ui_check.js`): filters, search, theme, matrix chips, the drawer and
its tabs, artifact files, Refresh (bucket pull), Rerun test, Cancel run, Run all with
its second-click confirmation, and the refusal of an overlapping run. Needs Chrome and
Node 22 or newer. Destructive buttons ask for a second click instead of
`window.confirm`, which the platform blocks inside its session frame.

## One test execution

`runner.TestRun.execute`, in order:

1. Resource gate: `pw cluster ls -o json` (or `pw kube ls`), fetched once per suite run.
   Off or unlisted: a `<start>_skip` directory with the record and the log.
2. Execution directory `<start>_pending`, renamed to `<start>_<slug>` after the launch
   or `<start>_launch-failed`.
3. Workflow YAML: one shallow `git fetch --depth 1` per `(repo, ref)` per suite run, the
   YAML materialised with sparse checkout. Its HEAD is `workflow.commit`.
4. Under a lock per `(resource, workflow_name)`, so two tests of one workflow never
   install into the same directory at once: `warm_marker` check and process snapshot
   over `pw ssh`, then `pw workflows run --trust <local yaml> -i <inputs> --name
   "probe: <id>" -o json`, retried on transient errors.
5. Poll `pw workflows runs view -o json` every 15 s until a final status, the timeout
   (then `pw workflows runs cancel`) or an interrupt.
6. Verdict: `completed` passes; anything else fails at `run` with the first error
   annotation of `pw workflows runs errors`.
7. Endpoints named `*-<slug>` are listed; `http_expect` probes the first one with the
   run's `PW_API_KEY` (anonymous requests only see the login redirect).
8. Teardown: `pw endpoints delete` for each, wait until they disappear, then the
   `leftover_patterns` check over `pw ssh`, retried for two minutes. A run that
   registered no endpoint has nothing to delete; every test is treated the same.
9. Write `record.json` into the execution directory (atomically), then upload the
   directory to the bucket (`--bucket`). Each execution has its own directory, so
   uploads never overwrite another runner's results. A failed upload is reported and
   makes the runner exit 2.

The record is written on every path, including internal errors.

### Why a local copy of the YAML

The specification launches `pw workflows run <repo>/<path>@<ref>`. The platform gives
every inline run of one repository the same slug (`gh-parallelworks-workflows`, then
`-2`, `-3`), and two launches at the same moment fail with `Slug ... is already in use`.
A suite launches many tests of the same repository at once, so PROBE fetches the
repository at the ref and launches the YAML as a local file, which gets a slug of its
own. The YAML's own `parallelworks/checkout` and `uses:` references are untouched, so
the run is the same one, and the checkout's HEAD is the exact commit for the record.

### Concurrency

Tests run in a thread pool (`--workers`, default 8). Launches of the same workflow on
the same resource are serialised; everything else runs in parallel. `SIGINT` and
`SIGTERM` stop the suite: polling loops cancel their runs and record them as failed;
queued tests are not started.

## Results and the dashboard

`results.py` owns the conventions: execution directory `<start time without
colons>_<run slug>` (or `_skip`, `_launch-failed`) holding `record.json`,
`HISTORY_LENGTH` records in the state. A test shows as running when an execution
directory has no `record.json` yet and its `run.log` changed in the last two hours.

`server.py` serves `web/` and this API, recognised by the `/api/` segment wherever the
endpoint prefix puts it:

| Route | Returns |
|---|---|
| `GET api/state` | every test (from records and definitions) with status, change, history, target; suite runs; definition errors |
| `GET api/tests/<id>/records` | all records of a test, oldest first |
| `GET api/tests/<id>/definition` | the definition file |
| `GET api/tests/<id>/artifacts` | artifact directories and files |
| `GET api/tests/<id>/artifacts/<dir>/<file>` | an artifact (last 4 MB) |
| `POST api/refresh` | pulls the bucket into the local results copy (both dashboards) |
| `POST api/run` `{"ids": [...]}` or `{"all": true}` | admin: starts `python3 -m probe run --bucket ...` in the background; 409 while an overlapping run is in progress |
| `POST api/cancel` `{"slug", "platform"}` | admin: `pw workflows runs cancel` |

Ids and file names are validated against `[A-Za-z0-9._-]` segments and resolved inside
the results directory. Without `--admin` every POST answers 403.

The dashboard is served at `/` behind a subdomain endpoint or under
`/me/session/<user>/<name>/` behind a path-based one. `index.html` builds a `<base>`
from its own location; the server takes the prefix from `--prefix` (`pw endpoints run`
passes `{path}`), `PW_ENDPOINT_PATH` or `X-Forwarded-Prefix`.

## The workflows

`workflow/workflow.yaml` follows the endpoint pattern of the session workflows in
`parallelworks/workflows`. `setup` checks out this repository and the test definitions,
restores the results from the bucket (a bucket path with no objects yet is fine; any
other failure stops the run) and writes one start script per dashboard into
`dashboard/` and `admin/`; each script runs `pw endpoints run --name
probe[-admin]-${PW_RUN_SLUG} -- python3 -m probe serve ... --bucket <bucket>/<path>`.
For each dashboard a runner job submits the script through the `script_submitter`
subworkflow and a wait job calls the `wait_for_endpoint` subworkflow, which waits until
the endpoint is listed and its URL answers, touches the skip-cleanups file and cancels
the runner job. The run then completes while the servers keep running; a runner that
exits before its endpoint came online fails the run. Runs started from the admin
dashboard inherit the bucket; the `Refresh` button calls `api/refresh`, which pulls the
bucket into the local copy. `pw endpoints delete` tears a dashboard down.

**Credentials.** Every step of a run has `PW_API_KEY`, the run's token, and that token
is rejected as soon as the run completes (verified by testing a stored run token before
and after completion). The node's own `pw` has no user context, and secret user
variables are not readable from a run. The dashboards outlive their run, so
`workflow.yaml` takes an API key as a `password` input: `setup` writes it to
`dashboard/.api_key` and `admin/.api_key` with owner-only permissions, each start
script reads it into `PW_API_KEY` and deletes the file before anything else, and the
setup step's cleanup removes any file a start script never consumed. The key then lives
only in the environment of `pw endpoints run`, `probe serve` and the runners it spawns.
Like every other input, the platform keeps it in the run's record (`pw workflows runs
view -o json` shows it in `inputs`, in plain text as of September 2026) and renders it
into the step's script under the job's `logs/` directory on the resource; deleting the
job directory removes that copy. The equivalent without an input is a credentials
directory on the resource authenticated once with `pw auth apikey` and selected with
`PW_CREDENTIALS_DIR`, which the CLI honours the same way. `run-tests.yaml` needs no key:
its runner works while its run is alive.

`workflow/run-tests.yaml` has one job: checkout, fetch the test definitions, then
`python3 -m probe run --bucket <bucket>/<path> --all` or `--test <file>` per line of the
`selection` input. The step's exit code is the runner's, so a failing test ends the run
in error, which the GitHub action reports.

The GitHub actions install the `pw` CLI on the runner, authenticate with the platform's
repository secret, write `inputs.json`, and `pw workflows run --trust` the YAML from the
checked-out repository. `run-tests.yml` also has a nightly `schedule`; without dispatch
inputs it reads the repository variables `PROBE_PLATFORM`, `PROBE_RESOURCE`,
`PROBE_BUCKET` and `PROBE_BUCKET_PATH`.

Validate a change with `pw workflows run --trust --dry-run -i inputs.json
/abs/path/workflow/<file>.yaml`. The `code` inputs pick the repository and branch of
this code, so a development branch is tested by pointing `code.branch` at it.

## Adding tests

Copy a file from `tests/`, give it a new `name`, change the target and inputs, and check
it with `python3 -m probe list --tests tests`, which also notes files that are not at
`<platform>/<user>/<workflow_name>/<name>.json`. The inputs are the form payload of the workflow;
the recorded tests under `workflows/<name>/tests/<variant>/` in the workflows
repository are a good source. Keep `timeout_s` above the cold-install time of the
workflow on that system. A `script_submitter` test needs `define_cleanup_script: false`
in its inputs or the platform rejects the launch.

## Branches

`dev` is the development branch; `main` is what `workflow.yaml` defaults to for both
the code and the tests.
