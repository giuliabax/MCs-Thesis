"""Recompute the pass rate over the tests a generator could have made pass.

A pass rate answers "how many tests agree with the service". On this corpus that number is
dominated by something else: applications that crash, throttle the caller, demand fields
their specification never documents, or gate every operation behind a verification code
delivered by email. A test blocked by one of those never reaches the behaviour it was
written for, and no rewriting of it ever will.

This script computes both figures side by side. The raw pass rate is unchanged and reported
first; the attainable pass rate divides the same number of passing tests by the passing
tests plus only those failures that are not attributable to the application or its
environment. The numerator never moves --- excluding a failure cannot manufacture a pass ---
so the difference between the two is exactly the share of the corpus that was never
winnable.

Two rules keep this from becoming a way to flatter the pipeline:

* A failure is excluded only by a deterministic test, printed with its own count, so the
  adjustment can be audited line by line rather than taken on trust.
* A failure the evaluator could not attribute stays in the denominator unless it matches a
  signature. Excluding what we do not understand would be comfortable and wrong, so the
  attainable rate is a lower bound on what the pipeline achieved.

Both campaigns are read from their per-iteration evaluation reports, whose `evidence`
fields preserve the service's own response, so the identical rules apply to runs whose raw
execution records no longer exist.

    python scripts/attainable_pass_rate.py data/runs/<run>/evaluation_report.iteration1.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

# Applied in order, first match wins, exactly as the evaluator's own rules are. The order
# matters where a response satisfies two: a 500 answered by a throttling proxy is recorded
# as throttling, because that is the reason the test could not proceed.
_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "the service stopped answering",
        re.compile(r"ConnectionError|RemoteDisconnected|MaxRetryError|Connection refused", re.I),
    ),
    (
        "the caller was throttled",
        re.compile(r"returned 429|too many (?:requests|accounts)|rate.?limit", re.I),
    ),
    (
        "authentication needs a code delivered out of band",
        re.compile(r"\bOTP\b|verification code|not verified|account is not active|"
                   r"verify your (?:email|account)", re.I),
    ),
    (
        "the service demands a field its contract omits",
        re.compile(r"\bundefined\b|must have required property", re.I),
    ),
    (
        "the operation needs a role the API cannot grant",
        re.compile(r"not an admin|admin only|forbidden: admin|requires? (?:an )?admin", re.I),
    ),
    (
        "the service answered 5xx",
        re.compile(r"returned 5\d\d|observed \[5\d\d\]", re.I),
    ),
)

# Causes the evaluator already declines to act on, because they are findings about the
# application rather than defects of the pipeline. Section 4.5.2 defines them.
_PROJECT_CAUSES = {"environment", "sut_defect", "contract_mismatch"}


def _passed(pass_rate: float | None, failures: int) -> int:
    """Recover the passing count from a rate and a failure count.

    The evaluation report stores a rate, not a tally. Since the rate is passed over passed
    plus failed and the failures are enumerated one diagnosis each, the count follows
    exactly; it is rounded because the stored rate is a float.
    """

    if pass_rate is None or pass_rate >= 1.0:
        return 0
    return round(pass_rate * failures / (1 - pass_rate))


def _excluded_by(diagnosis: dict) -> str | None:
    """Why this failure was not the pipeline's to prevent, or None if it was."""

    if diagnosis.get("cause") in _PROJECT_CAUSES:
        return f"attributed to {diagnosis['cause']}"
    evidence = " ".join(str(e) for e in (diagnosis.get("evidence") or []))
    for label, pattern in _SIGNATURES:
        if pattern.search(evidence):
            return label
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="An evaluation_report.iteration*.json")
    parser.add_argument("--per-project", action="store_true")
    parser.add_argument(
        "--only",
        type=Path,
        help="Restrict to the projects evaluated in this other report, so that two "
        "campaigns covering different corpora can still be compared like for like",
    )
    arguments = parser.parse_args()

    keep: set[str] | None = None
    if arguments.only:
        other = json.loads(arguments.only.read_text(encoding="utf-8"))
        keep = {
            name
            for name, project in other["projects"].items()
            if (project.get("metrics") or {}).get("pass_rate") is not None
        }

    report = json.loads(arguments.report.read_text(encoding="utf-8"))
    reasons: Counter[str] = Counter()
    total_passed = total_failed = total_excluded = 0
    rows = []

    for name, project in sorted(report["projects"].items()):
        if keep is not None and name not in keep:
            continue
        diagnoses = project.get("diagnoses") or []
        rate = (project.get("metrics") or {}).get("pass_rate")
        if rate is None and not diagnoses:
            continue
        passed = _passed(rate, len(diagnoses))
        excluded = 0
        for diagnosis in diagnoses:
            why = _excluded_by(diagnosis)
            if why:
                reasons[why] += 1
                excluded += 1
        addressable = len(diagnoses) - excluded
        total_passed += passed
        total_failed += len(diagnoses)
        total_excluded += excluded
        rows.append((name[-6:], passed, len(diagnoses), excluded,
                     passed / (passed + len(diagnoses)) if passed + len(diagnoses) else 0.0,
                     passed / (passed + addressable) if passed + addressable else 0.0))

    if arguments.per_project:
        print(f"{'project':9s}{'pass':>5s}{'fail':>6s}{'excl':>6s}{'raw':>8s}{'attainable':>12s}")
        for name, passed, failed, excluded, raw, attainable in rows:
            print(f"{name:9s}{passed:5d}{failed:6d}{excluded:6d}{raw:8.3f}{attainable:12.3f}")
        print()

    addressable = total_failed - total_excluded
    raw = total_passed / (total_passed + total_failed)
    attainable = total_passed / (total_passed + addressable) if total_passed + addressable else 0.0
    # Both aggregations are printed because they answer different questions and the thesis
    # reports the mean: pooling weights a project by how many tests it produced, averaging
    # weights every project equally however small its suite.
    macro_raw = sum(row[4] for row in rows) / len(rows) if rows else 0.0
    macro_attainable = sum(row[5] for row in rows) / len(rows) if rows else 0.0

    print(f"projects                           {len(rows)}")
    print(f"passing tests                      {total_passed}")
    print(f"failures                           {total_failed}")
    print(f"  not the pipeline's to prevent    {total_excluded}"
          f"  ({100 * total_excluded / total_failed:.0f}%)")
    print(f"  addressable                      {addressable}")
    print(f"\npooled, as measured                {raw:.3f}")
    print(f"pooled, over addressable tests     {attainable:.3f}")
    print(f"mean per project, as measured      {macro_raw:.3f}")
    print(f"mean per project, over addressable {macro_attainable:.3f}")
    print("\nwhy a failure was excluded:")
    for label, count in reasons.most_common():
        print(f"  {count:4d}  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
