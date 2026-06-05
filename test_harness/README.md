# Offline test harness

Deterministic, credential-free tests for `run_tests.py` — including the
resource on/off gate added for clusters like `pw://alvaro/a30gpuserver`.

```bash
bash test_harness/run_local_tests.sh
```

## How it works

The harness reproduces what the ACTIVATE `run_tests` job does: it copies the
repo into a fake platform run directory (`~/pw/jobs/workflowstatusmonitor/00000`,
mimicking `parallelworks/checkout`), sets the platform env vars, and runs
`run_tests.py` from there.

A mock `pw` CLI (`mockbin/pw`) is placed first on `PATH` so no real platform or
credentials are needed. Its behaviour is driven by a JSON config
(`$MOCK_PW_CONFIG`); see the docstring in `mockbin/pw` for the schema.

`run_tests.py` poll intervals are shortened via `PW_TEST_*` env vars so the
suite finishes in seconds.

## Scenarios asserted

| Scenario | Cluster status | Expected outcome |
|---|---|---|
| Resource **off** | `off` | workflow tests `skipped` (launch never attempted); session test still runs |
| Resource **on** | `active` | workflow + session tests `completed` |
| Resource **indeterminate** | not listed | launch attempted anyway (`launch_failed` here) — never silently skipped |
| **409 slug conflict** | `active` | launch 409s repeatedly, then recovers via in-launch retry + suite rerun → `completed` |

The mock supports a `"conflict": {"<workflow>": N}` config key to make the
first N launch attempts return a `409 ... is already in use` error.

The "off" scenario reproduces run `00008`, where `test1`/`test2` failed with
*"Unable to find workflow"* because the GPU server was unavailable: with the
gate, those tests are now cleanly skipped instead of reported as failures.
