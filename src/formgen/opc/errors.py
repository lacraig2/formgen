"""Errors shared across the OPC layer."""

from __future__ import annotations


class PackageError(Exception):
    """The package is structurally unusable, or an operation would corrupt it."""


class TargetError(PackageError):
    """A relationship target cannot be resolved to a part name."""
