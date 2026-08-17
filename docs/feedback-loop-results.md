# The feedback loop — configuration A

**Run:** `data/runs/20260816T235801Z-replanned`
**Date:** 17 August 2026
**Iterations:** 3 (one baseline, two feedback iterations), stopped at the limit

This supersedes `execution-results-loop1.md` as the current campaign: that document
describes an earlier run of the same procedure, kept because its figures are cited and
because comparing the two is the only measurement we have of run-to-run variation.

> **Recomputing these figures.** Every number below is iterations 1–3, over the thirteen
> projects that reached execution at the time. The run directory has since been executed
> again, with team06, team11 and team12 brought up, so it also holds a fourth iteration
> over sixteen projects at a mean pass rate of 0.085 — and executing a project rewrites its
> `execution/report.json`, so the per-project records now describe that later state rather
> than this one. Recompute configuration A from `evaluation_report.iteration{1,2,3}.json`,
> which were written once per iteration and are intact; do not recompute it by summing the
> per-project execution reports. The same applies to the `-configA` copy, which was taken
> after the fourth iteration had run.

---

## 1. Headline

**The loop caused no additional test to pass.** Twenty-two tests passed before it ran and
twenty-two after. It nevertheless changed the suite substantially, and the tests it
produced found half again as many defects in the applications under test.

## 2. The three axes, and the counts beneath them

| | Iteration 1 | Iteration 2 | Iteration 3 |
| --- | ---: | ---: | ---: |
| Mean pass rate | 0.082 | 0.081 | **0.091** |
| Mean operation coverage | 0.341 | 0.344 | 0.337 |
| Mean status-code coverage | 0.404 | 0.411 | 0.404 |

Read alone, this says the loop improved the suite by 11% relative without trading away
coverage — the shape a genuine improvement should have.

| | Iteration 1 | Iteration 2 | Iteration 3 |
| --- | ---: | ---: | ---: |
| **Tests passed** | **22** | **21** | **22** |
| Tests failed | 282 | 269 | 268 |
| Tests executed | 304 | 290 | 290 |

The rate rose because the denominator fell. Fourteen tests left the suite between the
first and second iterations — not deleted, but produced by items that yielded nothing when
rewritten.

**A rate needs its denominator reported beside it.** The three-axis design was built to
catch a loop that raises its pass rate by discarding demanding tests; coverage staying flat
showed that had not happened, and said nothing about a suite shrinking as a side-effect of
regeneration.

## 3. What the loop did change

| | Iter 1 | Iter 2 | Iter 3 |
| --- | ---: | ---: | ---: |
| Server errors (`sut_defect`) | 12 | 15 | 20 |
| Contract mismatches | 11 | 15 | 17 |
| **Total findings** | **23** | **30** | **37** |
| Unattributed (`unknown`) | 77 | 88 | 99 |

Findings rose monotonically across both iterations, by 61%. By the measure a test
generator exists to serve — finding things wrong with the service — the loop worked.

The unattributed failures rose alongside, which is consistent rather than contradictory:
as the loop repaired the defects its rules recognise, what remained was increasingly the
residue they do not describe.

## 4. Work performed and cost

Iteration 2 replanned 12 of 13 projects and rewrote 96 tests; iteration 3 replanned 11 and
rewrote 76. The Feedback Manager wrote 82 notes, of which **80 named a test that had
actually been asked about**; two were discarded for naming items nobody had.

| | Calls | Tokens | Wall-clock |
| --- | ---: | ---: | ---: |
| Baseline generation | 520 | 2,768,530 | 69 min |
| Feedback loop (2 iterations) | 426 | 2,146,421 | 70 min |

Within the loop: Test Writer 1.9 M tokens, replanning 177 k, Feedback Manager 63 k. The
cost is overwhelmingly in rewriting tests, not in deciding what to rewrite.

**The loop cost 78% of what producing the entire original suite cost, and returned no
additional passing tests.**

## 5. Per project

| Project | Pass 1 | Pass 2 | Pass 3 | Δ |
| --- | ---: | ---: | ---: | ---: |
| team01 | 0.00 | 0.00 | 0.00 | — |
| team02 | 0.09 | 0.04 | 0.04 | −0.04 |
| team04 | 0.17 | 0.12 | 0.12 | −0.04 |
| team05 | 0.11 | 0.11 | 0.12 | +0.00 |
| team07 | 0.06 | 0.05 | 0.10 | +0.04 |
| team09 | 0.07 | 0.13 | 0.13 | +0.06 |
| team10 | 0.04 | 0.05 | 0.04 | +0.00 |
| team13 | 0.07 | 0.07 | 0.07 | — |
| team14 | 0.12 | 0.13 | 0.14 | +0.02 |
| team15 | 0.05 | 0.05 | 0.05 | — |
| team16 | 0.00 | 0.00 | 0.00 | — |
| team17 | 0.24 | 0.27 | **0.33** | +0.10 |
| team18 | 0.07 | 0.03 | 0.03 | −0.03 |

Seven improved, three worsened, three unchanged.

## 6. What this does not establish

- **One loop.** The project-level variation between two independent runs of the identical
  procedure (team13 moved 0.23 → 0.07, team16 0.04 → 0.00) is larger than anything the loop
  produced. Nothing here separates a small real effect from none.
- **A third of the failures are invisible to it.** 99 of 268 remaining failures carry no
  attributed cause, so they generate neither a replan nor a regeneration request. The
  ceiling on what the loop could achieve was set before it started.
- **The largest addressable defect is upstream.** Throughout, the most frequent repairable
  failure was a strategy expecting status codes the contract does not document — a planning
  fault, not one the loop's regeneration can fix.

## 7. Next: configuration B

Two changes, to be reported alongside these figures rather than replacing them.

**Three further projects.** team11 ships `docker-compose.dev.yml`, which builds from source
instead of pulling the image that was never published; team06 and team12 are blocked only
by a Telegram bot token, which is issued free of charge — a credential that can be
*obtained* rather than *invented*. Executed corpus 13 → 16, tests never run 106 → 36.

**One correction upstream.** The planner's reconciliation should constrain each item's
expected status codes to those its operation documents, as it already discards items naming
operations the contract does not contain.

The comparison between A and B is the only thing that can distinguish a limitation of the
feedback loop from a limitation of what reaches it.
