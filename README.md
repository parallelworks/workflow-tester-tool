# Workflow Status Monitor

Automated testing and live dashboard for [Parallel Works ACTIVATE](https://parallelworks.com) workflows and sessions.

The tool runs a suite of workflow tests in parallel, stores results in cloud storage, and serves a web dashboard you can share with your team. An admin dashboard lets you rerun or cancel individual tests directly from the browser.

---

## Launching the workflow

Open the **Workflow Status Monitor** from the ACTIVATE marketplace and fill in the form:

| Input | What to enter |
|---|---|
| **Resource** | Where to run — your user workspace or any connected cluster. |
| **Output Storage Bucket** | Cloud bucket where results are saved between runs. |
| **Output Path in Bucket** | Path inside the bucket (default: `workflow-tester-tool/output`). |
| **Run Tests** | **Yes** to run the test suite. **No** to just view previously saved results. |
| **Serve Dashboard** | **Yes** to open an interactive dashboard session in your browser. |

Click **Execute**. The workflow will run the tests and open two browser sessions when ready.

---

## Dashboards

Two dashboard sessions are created:

### Public dashboard
Sharable read-only view of all test results. Pass the link to teammates.

- Summary cards: total tests, pass rate, failures, last run time
- Breakdowns by status, workflow, and kind
- Searchable and filterable results table
- Click any row to inspect the `inputs.json` that was used for that test

### Admin dashboard
Personal view with additional controls. Use it to rerun or cancel tests.

- All the same views as the public dashboard
- **Rerun Test** — re-executes the selected test immediately
- **Cancel Run** — cancels an in-progress run on the platform

The workflow URL redirects automatically to the public dashboard. To reach the admin dashboard, open the session named `<workflow-run>_admin` from the ACTIVATE sessions panel.

---

## Test results

| Status | Meaning |
|---|---|
| `completed` | **Pass** — run finished successfully, or session became healthy |
| `skipped` | Target resource was off — test was not launched (not counted as a failure) |
| `error` | Run ended in an error state |
| `timeout` | Exceeded the per-test time limit (1 h for workflows, 30 min for sessions) |
| `canceled` | Run was canceled outside of the test framework |
| `launch_failed` | The `pw workflows run` command itself failed |
| `poll_error` | Could not reach the platform API after repeated retries |

> Before launching, each test whose inputs target a `pw://` compute resource is
> checked with `pw cluster ls`. If the resource is **off**, the test is reported
> as `skipped` instead of being launched and failing — so an idle GPU server
> never shows up as a red failure on the dashboard.

---

## Running via GitHub Actions

A pre-built workflow is available under **Actions → Run PW Workflow**. Trigger it manually with these inputs:

| Input | Description |
|---|---|
| **Platform** | `activate.parallel.works` or `activate.hpc.mil` |
| **Workflow name** | Registered workflow slug (e.g. `workflowstatusmonitor`) |
| **Resource** | Resource URI, e.g. `pw://alvaro/user-workspace` |
| **Bucket** | Bucket URI, e.g. `pw://alvaro/gcpbucket` |
| **Bucket path** | Leave blank to use the default path |
| **Run tests** | Whether to execute the test suite |
| **Serve dashboard** | Whether to open the interactive dashboard session |

The action downloads the `pw` CLI, authenticates with the platform secret for the selected platform, and streams workflow output to the job log.

---

## For developers

See [DEVELOPER.md](DEVELOPER.md) for how to add tests, run locally, and understand the codebase.
