"""Versioned member names and offline replay rules for a synthetic review bundle."""
from __future__ import annotations

import re

from .comparison import make_comparison, review_identifier
from .evidence import json_bytes, parse_json, validate_casebook

ENVELOPE_NAME = "manifest.dsse.json"
PAYLOAD_TYPE = "application/vnd.medextract.evidence-dossier.manifest.v1+json"
REQUIRED_MEMBERS = frozenset({"question.json", "evidence.json", "decision.json", "report.html"})
_SOURCE_PATH = re.compile(r"sources/([A-Za-z0-9_-][A-Za-z0-9._-]*)\.txt")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class BundleLimitError(ValueError):
    """A bundle exceeds a caller-specified byte or directory entry limit."""


class BundlePathError(ValueError):
    """A member name cannot travel in a portable v1 handoff."""


def check_member_path(name: str) -> None:
    """Accept flat portable report names or a single portable text filename under sources/."""
    if isinstance(name, str):
        if name in REQUIRED_MEMBERS:
            return
        match = _SOURCE_PATH.fullmatch(name)
        if match and match.group(1).split(".")[0].upper() not in _RESERVED:
            return
    raise BundlePathError(f"Unsupported bundle member path: {name!r}")


def _section(members: dict[str, bytes], name: str, fields: set[str]) -> dict:
    try:
        section = parse_json(members[name])
        if not isinstance(section, dict) or section.keys() != fields | {"schema_version", "evidence_origin"}:
            raise ValueError("unexpected or missing fields")
        if type(section["schema_version"]) is not int or section["schema_version"] != 1:
            raise ValueError("unsupported schema version")
        if section["evidence_origin"] != "synthetic":
            raise ValueError("expected synthetic evidence")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{name}: {exc}") from exc
    return section


def validate_content(members: dict[str, bytes]) -> str:
    """Replay the captured members and return the identifier of the reproduced review."""
    for name in members:
        check_member_path(name)
    if len({name.casefold() for name in members}) != len(members):
        raise ValueError("Bundle member paths must be distinct on case-insensitive filesystems")
    question = _section(members, "question.json", {"questions"})
    evidence = _section(members, "evidence.json", {"sources", "families", "passages"})
    comparison = _section(members, "decision.json", {"omitted_passages", "decisions"})
    omitted = comparison["omitted_passages"]
    if not isinstance(omitted, list) or not all(isinstance(item, str) for item in omitted):
        raise ValueError("decision.json: omitted_passages must be a list of passage IDs")
    try:
        book = validate_casebook({**question, **evidence}, members.__getitem__)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid question or evidence: {exc}") from exc
    expected = REQUIRED_MEMBERS | {s["file"] for s in book["sources"]}
    if members.keys() != expected:
        raise ValueError("Declared source files differ from bundle members")
    try:
        replay = make_comparison(book, omitted)
    except ValueError as exc:
        raise ValueError(f"decision.json: {exc}") from exc
    # Structural JSON comparison must distinguish true from 1 (equal in Python).
    recorded = json_bytes(comparison)
    if json_bytes(replay) != recorded:
        # Early v1 handoffs used no_result for both an objective and an absent
        # extracted estimate. Reproduce that exact older ledger when requested.
        for decision in replay["decisions"]:
            for row in decision["ledger"]:
                if row["reason"] == "estimate_missing":
                    row["reason"] = "no_result"
        if json_bytes(replay) != recorded:
            raise ValueError("decision.json differs from the comparison reproduced from the bundled evidence")
    return review_identifier(book, replay)
