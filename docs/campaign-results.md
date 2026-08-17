# The two campaigns, side by side

**Runs:** `data/runs/20260816T235801Z-configA` (A), `data/runs/20260817T-configB` (B)
**Date:** 17 August 2026

> **Campaign B's baseline evaluation was overwritten and cannot be recomputed.** Running
> `evaluate --iteration 1` reads whatever execution reports are on disk at the time, and by
> then the loop had rewritten every suite, so `evaluation_report.iteration1.json` now holds
> the post-loop state. The baseline suites are gone with it, so the figures below are the
> record: they were computed from the intact artifact and are not re-derivable from the run
> directory. The same hazard, in the same shape, is recorded for campaign A in
> `feedback-loop-results.md` — the lesson is that `evaluate` without an explicit iteration
> is a write, not a read.

## The four cells

Restricted to the thirteen projects both campaigns executed, so the comparison is like for
like. Configuration B also ran team06, team11 and team12, which A did not; those appear in
the full-corpus figures further down and never inside a row that A also occupies.

| | passed | failed | pass rate | operation cov. | status-code cov. | findings | unknown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A, baseline | 22 | 282 | 0.082 | 0.341 | 0.404 | 23 | 77 |
| A, after the loop | 22 | 268 | 0.091 | 0.337 | 0.404 | 37 | 99 |
| B, baseline | 22 | 255 | 0.103 | 0.348 | 0.433 | 36 | 76 |
| **B, after the loop** | **26** | 257 | **0.114** | **0.365** | 0.433 | **44** | 97 |

Pass rate is the mean of the per-project rates, which is what the thesis reports.

### The passing count is the row that matters

22, 22, 22, **26**. Only the last cell moves, and it moves for the right reason: failures
rose slightly at the same time, 255 to 257, so the suite did not shrink to produce it.

Neither intervention does anything alone. The loop on impaired input gains nothing; the
input fix without the loop gains nothing. Together they gain four. That is an interaction,
not a sum, and it is the study's main result: **the feedback loop is conditional on being
given something it can repair.**

Operation coverage says the same thing from the other side. The identical loop *lowers* it
in campaign A (0.341 → 0.337) and *raises* it in campaign B (0.348 → 0.365).

## Pass rate over the tests that could have passed

Computed by `scripts/attainable_pass_rate.py`, which excludes failures attributable to the
application or its environment by deterministic rule, leaves anything unattributed in the
denominator, and never moves the numerator.

| | passing | addressable failures | mean per project |
| --- | ---: | ---: | ---: |
| A, baseline | 22 | 187 | 0.129 |
| A, after the loop | 22 | 155 | 0.154 |
| B, baseline | 22 | 129 | 0.185 |
| B, after the loop | 26 | 113 | 0.223 |

Monotone across all four cells, and 73% higher at the end than at the start. Read this
carefully: for the first three cells the numerator is fixed, so the gain is the pipeline
producing fewer failures of its own rather than more passes. Only the last cell adds passes.

## The full corpus

Campaign B executed sixteen of the eighteen projects; team03 and team08 remain unrunnable.

| | projects | passed | failed | never run | pass rate | findings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A, baseline | 13 | 22 | 282 | 106 | 0.082 | 23 |
| A, after the loop | 13 | 22 | 268 | 106 | 0.091 | 37 |
| B, after the loop | 16 | 31 | 327 | 40 | 0.106 | 56 |

Campaign B's full-corpus baseline was 16 projects, 23 passed, 303 failed, mean pass rate
0.087, operation coverage 0.311, status-code coverage 0.399, 36 findings — but that figure
carried a spurious 0.0 for team12, whose suite failed to load, so it understates the
baseline and should not be quoted without the caveat.

## Cost

| | calls | tokens | wall-clock |
| --- | ---: | ---: | ---: |
| A, baseline generation | 520 | 2,768,530 | 69 min |
| A, feedback loop (two iterations) | 426 | 2,146,421 | 70 min |
| B, baseline generation | 512 | 3,132,297 | 85 min |
| B, feedback loop (two iterations) | 326 | 1,760,942 | 55 min |

B's loop cost less and returned more: 56% of its baseline's tokens against A's 78%, because
fewer failures were addressable so it rewrote fewer tests — 256 writer calls against 349.
Per additional passing test, campaign B spent 440,000 tokens; campaign A bought none at any
price.

B's baseline generation costs 13% more than A's. That is the price of resolving the `$ref`
pointers: the prompts carry the fields they now describe.

Within both loops the distribution is the same and is the part that generalises. Almost all
of it is the Test Writer rewriting tests — 1.9M of 2.1M in A, 1.6M of 1.8M in B — against
177k and 114k for replanning and 63k and 58k for the Feedback Manager. **Deciding what to
repair is nearly free; repairing it is not.**

## The Feedback Manager's notes do not measurably help

| | rewritten under a note | no longer failing |
| --- | ---: | ---: |
| campaign A | 48 | 0 |
| campaign B | 35 | 1 |

Eighty-three tests rewritten with a model-written note guiding the rewrite; one stopped
failing. Even that one is generous, since the count reads "absent from the failure list",
which a test that ceased to exist also satisfies. The with/without comparison does not
support a conclusion — only two tests were rewritten without a note — so the claim is the
absolute one, not a relative one.

## Where the remaining failures come from

Read individually from configuration B's baseline execution, 303 failures decompose into
roughly a dozen causes, because almost every test begins by creating an account and a defect
at that step removes a project's whole suite rather than one test.

| project | what stops the suite | tests |
| --- | --- | ---: |
| team01 | the application crashes: after five requests `POST /sessions` closes the connection and it never answers again | 26 |
| team11 | `createUser` rejects duplicates on a `username` its specification never documents, so every account is created `undefined` and the second collides | 20 |
| team14 | registration calls a hosted mail service whose key this environment does not have | 17 |
| team10, team18, team01 | the registration body is a `$ref` that dangles in the team's own document | 25 |
| team05 | the application demands a field it does not list among its required ones | 15 |
| team13 | authentication requires a six-digit code delivered only by email | 8 |
| various | the application throttles: "Too many accounts created from this IP" | 18 |
| various | server errors, which are findings rather than faults | 24 |

138 of 303 — 46% — could not have been avoided by any generator. Where the contract *is*
complete and registration still fails (team14, team05), the generated request carries all
five required fields, correctly named and typed; what refuses it is a constraint the
contract does not state.

## One project was lost to a rendering defect, and no metric showed it

team12 produced nothing in campaign B: pytest refused to collect its module because a
generated string contained a NUL, written verbatim into the source by the renderer. The test
was reasonable — an edge case sending control characters in a description field — and the
cost was the entire suite, 25 tests.

It appeared as 25 `not_run`, indistinguishable from a project that never started. Fixed in
`23f9083`; regenerated and re-executed, team12 passes 4 of 24, which is among the better
results in the corpus. The loop never touched it in any iteration, so those figures are a
baseline and a post-loop result at once.
