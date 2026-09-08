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


def ask(question: str, default: str = "") -> str:
    try:
        return input(question).strip()
    except EOFError:
        return default
