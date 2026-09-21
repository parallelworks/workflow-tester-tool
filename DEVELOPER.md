# PROBE developer guide

## Layout

```
probe/__main__.py     command line: run, serve, list
probe/definitions.py  test definition files: validation, ids (from the fields, not the path), target derivation
probe/pw.py           pw CLI wrapper, git checkout of workflow YAMLs
probe/runner.py       one run of the suite: gate, launch, poll, verdict, cleanup, record, bucket sync
probe/results.py      results tree: records.jsonl, artifact directories, state and history
probe/server.py       dashboard server: static web/ plus the JSON API
web/                  index.html, app.js, styles.css (no build step, no external assets)
tests/                test definitions shipped with this repository
selftest/             offline unit tests with a mock pw CLI
workflow/workflow.yaml  the ACTIVATE workflow that deploys PROBE
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

## One test execution

`runner.TestRun.execute`, in order:

1. Resource gate: `pw cluster ls -o json` (or `pw kube ls`), fetched once per suite run.
   Off or unlisted: record `skip`, no artifact directory.
2. Artifact directory `<start>_pending`, renamed to `<start>_<slug>` after the launch
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
9. Append the record, then upload the artifact directory and `records.jsonl` to the
   bucket (`--bucket`), records last so the bucket never has a record without its
   artifacts. A failed upload is reported and makes the runner exit 2.

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

`results.py` owns the conventions: `records.jsonl` per test, artifact directory
`<start time without colons>_<run slug>`, `HISTORY_LENGTH` records in the state. A test
shows as running when an artifact directory exists that no record accounts for and
whose `run.log` changed in the last two hours.

`server.py` serves `web/` and this API, recognised by the `/api/` segment wherever the
endpoint prefix puts it:

| Route | Returns |
|---|---|
| `GET api/state` | every test (from records and definitions) with status, change, history, target; suite runs; definition errors |
| `GET api/tests/<id>/records` | all records of a test |
| `GET api/tests/<id>/definition` | the definition file |
| `GET api/tests/<id>/artifacts` | artifact directories and files |
| `GET api/tests/<id>/artifacts/<dir>/<file>` | an artifact (last 4 MB) |
| `POST api/run` `{"ids": [...]}` or `{"all": true}` | admin: starts `python3 -m probe run --bucket ...` in the background; 409 while an overlapping run is in progress |
| `POST api/cancel` `{"slug", "platform"}` | admin: `pw workflows runs cancel` |

Ids and file names are validated against `[A-Za-z0-9._-]` segments and resolved inside
the results directory. Without `--admin` every POST answers 403.

The dashboard is served at `/` behind a subdomain endpoint or under
`/me/session/<user>/<name>/` behind a path-based one. `index.html` builds a `<base>`
from its own location; the server takes the prefix from `--prefix` (`pw endpoints run`
passes `{path}`), `PW_ENDPOINT_PATH` or `X-Forwarded-Prefix`.

## The workflow

`workflow/workflow.yaml` has three jobs on the chosen resource. `setup` checks out this
repository and the test definitions and restores the results from the bucket (a bucket
path with no objects yet is fine; any other failure stops the run). `dashboard` and
`admin_dashboard` each run `pw endpoints run --name probe[-admin]-${PW_RUN_SLUG} --
python3 -m probe serve ... --bucket <bucket>/<path>`, which lives for the rest of the
run. Runs started from the admin dashboard inherit the bucket, so their results are
uploaded as they finish.

Validate a change with `pw workflows run --trust --dry-run -i inputs.json
/abs/path/workflow/workflow.yaml`. The `code` inputs pick the repository and branch of
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
