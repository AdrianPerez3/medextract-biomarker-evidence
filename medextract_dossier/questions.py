"""Match reviewed passages to structured questions in the synthetic example."""

EXPECTED_DIRECTION = {
    "prognosis": "higher_vs_lower",
    "intervention": "assigned_vs_control",
}


def applicability(question: dict, passage: dict) -> str:
    """Return the first applicable ledger reason for a validated question and passage."""
    if passage["review"] != "reviewed":
        return "unreviewed"
    if passage["kind"] != "result":
        return "no_result"
    if passage["estimate"] is None:
        return "estimate_missing"

    expected = question["scope"]
    actual = passage["scope"]
    if actual["intent"] != expected["intent"]:
        return "related_question"
    if actual["population"] != expected["population"]:
        return "population_mismatch"
    if actual["outcome"] != expected["outcome"]:
        return "outcome_mismatch"
    if actual["months"] != expected["months"]:
        return "time_horizon_mismatch"
    if actual["event_coding"] != expected["event_coding"]:
        return "event_coding_mismatch"
    if any(actual[key] != expected[key] for key in ("factor", "comparator", "direction")):
        return "contrast_mismatch"

    # Even perfectly matching fields need a known estimand and comparison.
    if EXPECTED_DIRECTION.get(expected["intent"]) != expected["direction"]:
        return "uncertain"
    estimate = passage["estimate"]
    if estimate["measure"] != "risk_ratio" or estimate["unit"] != "ratio":
        return "uncertain"
    if estimate["low"] == estimate["high"]:
        return "uncertain"
    return "direct"
