"""Human-readable descriptions of synthetic review decisions."""

REASON_TEXT = {
    "direct": "Direct evidence for this question.",
    "related_question": "Answers a different research question.",
    "population_mismatch": "The study population differs.",
    "outcome_mismatch": "Measures a different outcome.",
    "time_horizon_mismatch": "Uses a different follow-up period.",
    "no_result": "The passage contains no observed result.",
    "estimate_missing": "No result estimate was captured in the annotation.",
    "unreviewed": "The passage awaits review.",
    "uncertain": "The estimate or its applicability requires review.",
    "contrast_mismatch": "The exposure, assignment or comparator differs.",
    "event_coding_mismatch": "The event is coded differently.",
    "single_family_association": "One independent synthetic cohort reports a baseline association.",
    "interval_includes_null": "The interval includes effects in both directions.",
    "interval_touches_null": "The interval includes the no-effect value at its boundary.",
    "single_family_intervention_result": "One synthetic trial reports an outcome estimate.",
    "no_direct_result": "No eligible result answers this question.",
    "multiple_families_require_review": "Multiple independent study families need a joint review.",
    "conflicting_family_estimates": "Reports from the same study family disagree.",
}


def describe_estimate(estimate: dict) -> str:
    measure = estimate["measure"].replace("_", " ")
    unit = "" if estimate["unit"] == "ratio" else " " + estimate["unit"]
    return (f"{measure} {estimate['point']}{unit} "
            f"(95% CI {estimate['low']} to {estimate['high']}{unit})")


def render_text(comparison: dict) -> str:
    lines = ["Synthetic evidence review"]
    if comparison["omitted_passages"]:
        lines.append("Omitted for this comparison: " + ", ".join(comparison["omitted_passages"]))
    for result in comparison["decisions"]:
        lines.extend(["", result["question_text"], "Decision: " + result["status"]])
        reason = result["reason"]
        if result["status"] == "effect_uncertain" and reason == "interval_includes_null":
            lines.append("The 95% interval includes both benefit and harm for the specified outcome.")
        else:
            lines.append(REASON_TEXT.get(reason, reason.replace("_", " ")))
        lines.append("Eligible study families: " + (", ".join(result["eligible_families"]) or "none"))
        for row in result["ledger"]:
            why = REASON_TEXT[row["reason"]]
            lines.append(f"  {row['passage_id']} [{row['family_id']}] — {why}")
            if row["reason"] == "direct" and row["estimate"] is not None:
                lines.append("    " + describe_estimate(row["estimate"]))
            lines.append(f"    {row['source_file']} · bytes [{row['start']}, {row['end']})")
            lines.append("    “" + row["quote"] + "”")
    return "\n".join(lines)
