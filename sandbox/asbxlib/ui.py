from __future__ import annotations

import sys


def ok(msg: str) -> None:
    print(f"==> {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def confirm(question: str) -> bool:
    """`question` is printed verbatim (call sites carry their own '[y/N] ')."""
    return input(question).strip().lower() == "y"


def confirm_exact(question: str, expected: str) -> bool:
    return input(question).strip() == expected


def choose(question: str, options: list[str]) -> str:
    """Numbered pick from `options`; raises ValueError on a bad answer."""
    for i, opt in enumerate(options, start=1):
        print(f"  {i}) {opt}", file=sys.stderr)
    answer = input(question).strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        raise ValueError(answer)
    return options[int(answer) - 1]
