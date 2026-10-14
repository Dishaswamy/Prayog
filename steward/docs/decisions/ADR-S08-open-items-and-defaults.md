# ADR-S08 — Open items and the placeholder defaults we build with

**Status:** Partly open (2026-10-07). Each *open* row should become a GitHub issue.

| Item | Default we build with | State |
|---|---|---|
| S1 failure classes (Project Doc §10; no FR fixes them) | `OOM`, `MISSING_DATA`, `LIBRARY_VERSION`, `CODE_BUG`, `INTERRUPTION`, `HARDWARE`, `UNKNOWN`. Frozen for the evaluation | open: confirm with the team |
| S4 "permitted metrics" (FR-STW-006) | Final and best values of the run's logged scalar metrics, the training config's hyperparameters, and the same user's earlier runs of the same project. No dataset contents, no weights | open |
| Does S3 show the S1 diagnosis of the same failure? | Yes, as an *internal* record with its own `record_id`, since both belong to the same owner | open |
| Proposal job fields the model may change | `env_pin`, `command_args`, `gpu_count`, `walltime_minutes`, `batch_size`, `precision` | open |
| `correlation_window` calibration (§16.2) | 24 h, as specified | open (Deliverable 3 measurement) |
