"""Apply the review policy to the declared questions and retained passages."""
from hashlib import sha256

from .decision import decide
from .evidence import json_bytes


def review_identifier(book: dict, comparison: dict) -> str:
    """Bind a validated casebook and its decisions to the version of the review policy."""
    identity = {
        "policy_version": 1,
        "casebook_sha256": sha256(json_bytes(book)).hexdigest(),
        "comparison_sha256": sha256(json_bytes(comparison)).hexdigest(),
        "omitted_passages": comparison["omitted_passages"],
    }
    return sha256(json_bytes(identity)).hexdigest()


def make_comparison(book: dict, omitted: list[str]) -> dict:
    known = {p["id"] for p in book["passages"]}
    unknown = set(omitted) - known
    if unknown:
        raise ValueError("Unknown passage ID: " + ", ".join(sorted(unknown)))
    excluded = set(omitted)
    passages = [p for p in book["passages"] if p["id"] not in excluded]
    return {"schema_version": 1, "evidence_origin": book["evidence_origin"],
            "omitted_passages": sorted(excluded),
            "decisions": [decide(q, passages, book["sources"]) for q in book["questions"]]}
