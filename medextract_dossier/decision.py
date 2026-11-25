"""Produce a question-specific decision and the complete passage ledger."""

from copy import deepcopy

from .questions import applicability


def _single_estimate_result(intent: str, estimate: dict) -> tuple[str, str]:
    low, high = estimate["low"], estimate["high"]
    if low <= 1 <= high:
        status = "association_uncertain" if intent == "prognosis" else "effect_uncertain"
        reason = "interval_touches_null" if 1 in (low, high) else "interval_includes_null"
        return status, reason
    if intent == "prognosis":
        return "association_observed", "single_family_association"
    return "effect_estimate_reported", "single_family_intervention_result"


def decide(question: dict, passages: list[dict], sources: list[dict]) -> dict:
    """Review validated records without pooling studies or voting across reports."""
    sources_by_id = {source["id"]: source for source in sources}
    ledger = []
    direct = []
    for passage in sorted(passages, key=lambda record: record["id"]):
        source = sources_by_id[passage["source_id"]]
        reason = applicability(question, passage)
        row = {
            "passage_id": passage["id"],
            "source_id": source["id"],
            "source_file": source["file"],
            "family_id": source["family_id"],
            "reason": reason,
            "quote": passage["quote"],
            "start": passage["start"],
            "end": passage["end"],
            "source_sha256": source["sha256"],
            "scope": deepcopy(passage["scope"]),
            "estimate": deepcopy(passage["estimate"]),
        }
        ledger.append(row)
        if reason == "direct":
            direct.append(row)

    families = sorted({row["family_id"] for row in direct})
    if not families:
        status, reason = "insufficient", "no_direct_result"
    elif len(families) > 1:
        status, reason = "insufficient", "multiple_families_require_review"
    elif any(row["estimate"] != direct[0]["estimate"] for row in direct[1:]):
        status, reason = "insufficient", "conflicting_family_estimates"
    else:
        status, reason = _single_estimate_result(question["scope"]["intent"], direct[0]["estimate"])

    return {
        "question_id": question["id"],
        "question_text": question["text"],
        "question_scope": deepcopy(question["scope"]),
        "status": status,
        "reason": reason,
        "eligible_families": families,
        "ledger": ledger,
    }
