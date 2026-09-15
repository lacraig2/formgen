"""The machine-readable report.

A build gate needs the same information the console shows, keyed so it can be
diffed between runs: every finding carries the profile pointer that produced
it, so "which rule fired" is answerable without parsing prose.

Deliberately not a stream of everything we know -- the JSON mirrors the
console report exactly, so a discrepancy between what a human sees and what a
gate acts on is impossible by construction.
"""

from __future__ import annotations

import json
from typing import Any

from ..profile.schema import encode


def locator_dict(locator) -> dict[str, Any]:
    return {
        "part": locator.part,
        "path": locator.path,
        "heading_path": list(locator.heading_path),
        "ordinal": locator.ordinal,
        "of": locator.of,
        "container": locator.container,
        "find": locator.find_string,
        "para_id": locator.para_id,
        "page": locator.page,
    }


def finding_dict(finding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "severity": finding.severity,
        "message": finding.message,
        "rule": finding.rule_id,
        "expected": encode(finding.expected, finding.rule_id),
        "actual": encode(finding.actual, finding.rule_id),
        "fixable": finding.fixable,
        "confidence": round(finding.confidence, 3),
        "locator": locator_dict(finding.locator),
    }


def plan_dict(plan) -> dict[str, Any]:
    return {
        "document": plan.document,
        "profile": plan.profile,
        "refusals": list(plan.refusals),
        "counts": plan.counts(),
        "stats": plan.stats,
        "findings": [finding_dict(f) for f in plan.sorted_findings()],
        "needs_review": [
            {
                "path": edit.path,
                "role": edit.role,
                "confidence": round(edit.confidence, 3),
                "evidence": list(edit.evidence),
                "locator": locator_dict(edit.locator),
            }
            for edit in plan.needs_review
        ],
        "exit_code": plan.exit_code,
    }


def dumps(plan) -> str:
    return json.dumps(plan_dict(plan), indent=2, sort_keys=True, ensure_ascii=False)
