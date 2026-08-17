"""Measure whether generated tests establish their own preconditions.

Seventy per cent of the failures in the first campaign happened in `setup`, and the
largest single cause was a test logging in as an account it had never created. Every
execution starts from an empty database, so invented credentials can only be refused.

That defect is visible in the generated suites, before anything is executed: a test that
calls a login operation without an earlier registration step in the same setup will fail,
and it takes seconds to count them rather than forty minutes to watch them fail. This
script exists so that a change to the Test Writer's prompt can be judged immediately.

    python scripts/check_setup_discipline.py --run-dir data/runs/<id> [--project NAME]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Paths are matched rather than summaries, which the generated case does not carry. The
# three patterns are applied in order and the first one to match decides, because the
# corpus contains paths that satisfy more than one: `/sessions/signup` creates an account
# and `/users/login` opens a session, and each would be read backwards by the other rule.
_SAYS_REGISTER = re.compile(r"regist|signup|sign-up", re.IGNORECASE)
_SAYS_LOGIN = re.compile(r"login|signin|sign-in|/sessions?(/|$)|/session\b", re.IGNORECASE)
_ACCOUNT_COLLECTION = re.compile(r"/(users|citizens|accounts)(/|$)", re.IGNORECASE)


def _classify(step: dict) -> str | None:
    """`register`, `login`, or None for any other setup step."""

    request = step.get("request") or {}
    if str(request.get("method", "")).upper() != "POST":
        return None
    path = str(request.get("path", ""))
    if _SAYS_REGISTER.search(path):
        return "register"
    if _SAYS_LOGIN.search(path):
        return "login"
    if _ACCOUNT_COLLECTION.search(path):
        return "register"
    return None


def _describe(case: dict) -> tuple[bool, bool]:
    """Whether this test logs in, and whether it registered before doing so."""

    registered = False
    for step in case.get("setup") or []:
        if not isinstance(step, dict):
            continue
        kind = _classify(step)
        if kind == "login":
            return True, registered
        if kind == "register":
            registered = True
    return False, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--project", action="append", default=None)
    arguments = parser.parse_args()

    total = logging_in = disciplined = reads_without_creating = 0
    for report in sorted((arguments.run_dir / "projects").glob("*/suite/generation_report.json")):
        name = report.parent.parent.name
        if arguments.project and name not in arguments.project:
            continue
        data = json.loads(report.read_text(encoding="utf-8"))
        for case in data.get("cases", []):
            total += 1
            logs_in, registered = _describe(case)
            if logs_in:
                logging_in += 1
                disciplined += registered
            # A test that asserts a collection is populated without any creating step.
            creates = any(
                str((step.get("request") or {}).get("method", "")).upper()
                in {"POST", "PUT", "PATCH"}
                for step in (case.get("setup") or []) + (case.get("steps") or [])
                if isinstance(step, dict)
            )
            asserts_content = any(
                assertion.get("operator") == "not_empty"
                for step in (case.get("steps") or [])
                if isinstance(step, dict)
                for assertion in (step.get("assertions") or [])
            )
            reads_without_creating += asserts_content and not creates

    share = f"{100 * disciplined / logging_in:.0f}%" if logging_in else "n/a"
    print(f"tests examined:                        {total}")
    print(f"tests that log in:                     {logging_in}")
    print(f"  of which register first:             {disciplined}  ({share})")
    print(f"tests asserting content they never created: {reads_without_creating}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
