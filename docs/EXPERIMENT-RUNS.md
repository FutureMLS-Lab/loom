# Durable experiment runs

Long-running experiments should not depend on an agent terminal remaining
connected. Loom's Experiment Run v1 freezes an execution contract, runs each
stage in a detached worker, and records every transition on disk. An agent can
submit and interpret a run; the controller owns execution and recovery.

```text
immutable spec.json
        │
        ▼
durable controller ──▶ PREFLIGHT ─▶ PREPARE ─▶ VALIDATE ─▶ GPU_SERVE
        │                                             │
        │                                             ▼
        │                         CANARY ─▶ RUN ─▶ COLLECT
        │                                             │
        │                                  atomic verdict marker
        │                                             │
        └── restart/resume from run.json              ▼
                                             separate CLEANUP attempt
```

`PLAN.md` can describe why a run exists, but it is not the state database.
The durable boundary is `<project>/.RUD/experiments/<run-id>/`.

## Create a run

Experiment specs are JSON. Identity fields and input digests are required so a
retry cannot silently change the image, source, environment, or data.

```json
{
  "run_id": "glm53-g2-001",
  "source_revision": "0123456789abcdef0123456789abcdef01234567",
  "image": "registry.example/runner@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "environment_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "inputs": {
    "task-manifest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "stages": [
    {
      "name": "CPU_PREP",
      "kind": "PREPARE",
      "command": ["./scripts/prepare.sh"],
      "hermetic": false,
      "artifacts": ["artifacts/prepared-environment.json"]
    },
    {
      "name": "MOCK_VALIDATION",
      "kind": "VALIDATE",
      "command": ["./scripts/mock-validation.sh"],
      "artifacts": ["artifacts/mock-results.json"]
    },
    {
      "name": "G2",
      "kind": "RUN",
      "command": ["./scripts/run-g2.sh"],
      "artifacts": ["artifacts/g2-results.json"]
    },
    {
      "name": "REPORT",
      "kind": "COLLECT",
      "command": ["./scripts/report.sh"],
      "artifacts": ["artifacts/report.md"]
    }
  ],
  "cleanup": {
    "command": ["./scripts/cleanup-task-resources.sh"]
  }
}
```

```bash
loom experiment create experiment.json
loom experiment drive .RUD/experiments/glm53-g2-001 --detach
loom experiment status .RUD/experiments/glm53-g2-001
```

Starting `loom web` resumes unfinished runs in the project's
`.RUD/experiments` registry. The controller does not need a tmux pane or agent
session.

## Durability contract

- `spec.json` is immutable and bound to `spec_hash`. Editing it makes the run
  invalid rather than creating an undocumented arm.
- `run.json` records stage status, attempt identity, worker PID, timestamps,
  heartbeat, and errors.
- `attempts/<stage>/attempt-NNN/` contains stdout, stderr, provenance, worker
  identity, and an atomic terminal result.
- `artifacts.json` records SHA-256 and size for logs, provenance, results, and
  declared artifacts.
- `SUCCESS.json` or `FAILURE.json` is written before cleanup begins.
- Cleanup has its own attempt and status. Cleanup failure cannot rewrite a
  successful execution verdict.
- A vanished RUNNING worker becomes `BLOCKED`. Loom never silently replays an
  outcome-bearing attempt. A person or supervising agent must issue an explicit
  `loom experiment retry ... --stage NAME`; prior attempts remain intact.

## Hermetic stages

Stages other than PREFLIGHT and PREPARE default to `hermetic: true`.
Direct package-install commands are rejected when the spec is frozen. Runtime
PATH guards also fail common `pip`, `uv`, `npm`, `apt`, `conda`, `go`, and
similar mutations with exit code 86. This is a guardrail, not a substitute for
an immutable image and offline environment validation.

The worker inherits only a small execution allowlist from the control-host
environment. Secret-looking variables cannot be persisted in a spec. Use
least-privilege, task-scoped file references such as a restricted kubeconfig;
never put tokens or credentials in the JSON contract.

## What v1 intentionally does not do

Experiment Run v1 is an execution state machine, not a general workflow
language or a Kubernetes operator. Stage commands may invoke the existing
remote-GPU worker tooling, submit Kubernetes Jobs, or collect their evidence.
Cluster audit identity, quota policy, and resource provisioning remain the
responsibility of the authorized cluster control plane.
