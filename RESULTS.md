# Verified result record — 2026-09-24

This file supplements the one-page REPORT.md with evidence and submission details.
The final artifact was inspected without modifying Rust or making model requests.

## Evaluation

| Check | Result |
|---|---|
| Build | PASS |
| Rust unit tests | 5 passed / 1 failed (83.3%; process exit 101) |
| Valid parsing | 318 / 318 |
| Invalid parsing | 171 / 171 |
| Comparison | 664 / 664 |
| Bumps | 954 / 954 |
| Round trips | 318 / 318 |
| Total differential cases, seed 0 | 2425 / 2425 (100%) |
| SemVer precedence chain | PASS |
| Evaluator rule violations | None |
| Explicit clone / to_owned | 0 / 0 |
| unwrap | 13, all in the test module |

The final state file's `code_hash` matches the current `rust/src/lib.rs` bytes.
The stored final evaluation is therefore associated with the inspected source.
I reran `cargo test --release --manifest-path rust/Cargo.toml` locally and reproduced
the same 5/6 result. The available bundled Python lacks the installed `semver`
package, so I did not rerun the complete differential evaluator during this audit.
The 100% result above comes from the saved run, not a new differential run.

## Failure diagnosis and additional probe

`tests::test_parse` fails at `rust/src/lib.rs:245`:

```rust
assert!(parse("1.0.0-0A.1").is_err());
```

The checked-in `reference/version.py` accepts this input. `0A` is not purely
numeric; starting with zero is allowed. The assertion, not this parsing behavior,
is wrong. Assertion panics in a failing test are distinct from explicit `panic!`
macros counted by the evaluator's quality scan.

An additional local comparison, using the checked-in Python reference and the
compiled Rust harness, produced:

```text
a = 1.0.0-18446744073709551616
b = 1.0.0-1
Python reference:  1 (a > b)
Rust harness:    -1 (a < b)
```

Cause: `nat_cmp_identifiers` uses `parse::<u64>().unwrap_or(0)`, converting overflow
to zero. This is a real semantic discrepancy outside the reported practice cases.
Neither the assertion nor the implementation was changed during this audit.

## Request accounting and trajectories

| Log file | Requests | Successful replies | Stop reason |
|---|---:|---:|---|
| run-20260923-230927-9b334534.jsonl | 15 | 14 | HTTP 503 |
| run-20260923-231902-d8ff809d.jsonl | 4 | 4 | Repeated identical actions |
| run-20260923-232246-f250eb7e.jsonl | 4 | 3 | HTTP 429 |
| run-20260924-155155-81765a18.jsonl | 17 | 10 | Interrupted at 33, resumed; budget exhausted at 40 |
| Total | 40 | 31 | Automated translation requests only |

Counts use `request` and `model` events; failed requests remain charged to the
agent's budget. The last trajectory imports 23 earlier calls, so preserve all
four logs to document the complete sequence. No more model requests were made
for this analysis. Interactive assistance used to build the scaffold is separate
and should be disclosed, as in REPORT.md.

## Submission preparation

- Required artifacts: `rust/src/lib.rs`, `agent.py`, all four `logs/run-*.jsonl`,
  and `REPORT.md`.
- Optional supporting artifact: this RESULTS.md.
- If entering the final artifact's rates in the leaderboard: compilation PASS,
  Rust tests 5/6 (83.3%), differential tests 2425/2425 (100%). Keep the two test
  rates distinct; these are not pass rates across all intermediate builds.
- `.gitignore` currently excludes `logs/*.jsonl`. If submitting through Git,
  explicitly include the four trajectory files, for example with
  `git add -f logs/run-*.jsonl`. No staging, commit, or leaderboard submission was
  performed during this audit.
- The 60/40 attribution in REPORT.md is a proposed estimate for the student to
  review. No overall course score or hidden-test performance is inferred.
