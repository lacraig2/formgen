"""Typed errors, each mapping to a stable exit code.

Exit codes are an interface: a build gate will branch on them, so they are
defined once here rather than scattered through the CLI. The distinction that
matters most is between **1 (the document is wrong)** and **3 (we refuse to
touch it)** -- a CI job wants to fail on the first and escalate on the second.

Every refusal carries a remedy. A tool that says "cannot process this file"
and stops teaches people to work around it; one that says which feature is in
the way and how to turn it off gets used.
"""

from __future__ import annotations


class FormgenError(Exception):
    """Base class. Exit code 1: the run completed and the answer was bad."""

    exit_code = 1

    def __init__(self, message: str, remedy: str | None = None):
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def render(self) -> str:
        return self.message if not self.remedy else f"{self.message}\n  {self.remedy}"


class UsageError(FormgenError):
    """Bad invocation: missing profile, contradictory flags, no inputs."""

    exit_code = 2


class RefusalError(FormgenError):
    """We could act, and are choosing not to, because acting would do harm."""

    exit_code = 3


class InputError(FormgenError):
    """The input is not what it claims to be, or cannot be read."""

    exit_code = 4


class InvariantError(FormgenError):
    """Our own output failed its structural checks. Nothing was written."""

    exit_code = 5
