# Python-to-Rust Agent: Results and Reflection

**Final result.** The final Rust artifact builds successfully. On practice seed 0
(`n=300`), differential testing passed **2,425/2,425 cases (100%)**: valid parsing
318/318, invalid parsing 171/171, comparison 664/664, bumps 954/954, and round trips
318/318. The SemVer precedence chain also passed. Agent-written unit tests passed
**5/6 (83.3%)**, so `cargo test` still exits with code 101. These are measured
results, not an overall course grade or a claim about the hidden grading seed.
The quality scan found no prohibited constructs or extra dependencies, and zero
explicit `.clone()`/`.to_owned()` calls. It reported **13 `.unwrap()` calls**, all
in tests, as a quality warning.

**Remaining problems and incomplete specifications.** The failing test expects
`parse("1.0.0-0A.1")` to fail. The checked-in Python reference accepts it: `0A` is
an alphanumeric identifier, so the restriction on leading zeros in *numeric*
prerelease identifiers does not apply. Thus this failure is an incorrect test
expectation, rather than evidence that rejecting this input would improve the
translation. Separately, a post-run local probe exposed a real implementation
defect: comparing `1.0.0-18446744073709551616` with `1.0.0-1` returns “less” in Rust
but “greater” in the Python reference. The Rust helper parses numeric identifiers
as `u64` and silently substitutes zero on overflow with `unwrap_or(0)`. The
practice generator missed this case. I retained both findings rather than
editing the final agent-generated artifact to improve the reported score.

**Agent versus scaffold contribution.** My approximate attribution is **60%
translation model / 40% scaffold and orchestration**, a qualitative estimate,
not a measured causal split. The runtime models generated the Rust implementation
and tests, including the final successful interface repair. The surrounding
Python scaffold supplied tools, fixed signatures, selected source context,
automatic evaluation, checkpoint rollback, request pacing, and budget tracking.
I used an interactive coding assistant to develop and debug that scaffold; it
was not produced independently by the translation agent. The reported 40-call
count covers the automated translation runs, not this separate assistance.

**Unexpected behavior.** The agent initially implemented methods without the
required crate-level API, later used the wrong comparison signature, and changed
a return type without updating related expressions. It repeatedly read the same
source page, invented the invalid-input expectation above, and requested a path
outside the tool allowlist. It also continued requesting reads/evaluations after
the practice score reached 100%, without resolving the unit-test failure.

**Challenges, interventions, and termination.** Early context truncation hid
relevant source; I changed the scaffold to provide current Rust directly,
retrieve related Python/harness excerpts locally, and expose consistent paging
and search, while retaining the 24,000-character message-content cap. Automatic
evaluation after edits removed an extra decision step. Atomic state checkpoints
preserved budget and progress across interruptions; pacing and bounded retries
addressed API instability. Across four trajectory files, **40 requests produced
31 model replies**; failures comprised seven HTTP 503s, one HTTP 429, and one
connection/timeout error. Models used were Gemini 2.5 Flash, 3.5 Flash, and 3.5
Flash-Lite. Earlier runs stopped on errors or repetition; the final run stopped
because the cumulative budget reached 40, **not because the agent verified
completion**. No controlled experiment isolates the effects of model switching
from scaffold changes. The outcome illustrates the assignment's central lesson:
high test reward can coexist with faulty tests, uncovered semantic errors, and
inefficient agent behavior.
