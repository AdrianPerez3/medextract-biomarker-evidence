"""Test review decisions on the example and controlled variations of reviewed records."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from medextract_dossier.decision import decide


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/marker-mx1"


@pytest.fixture
def book():
    return json.loads((EXAMPLE / "casebook.json").read_bytes())


def by_id(records, record_id):
    return next(record for record in records if record["id"] == record_id)


def test_both_decisions_are_order_independent_and_preserve_verified_spans(book):
    expected = json.loads((EXAMPLE / "expected.json").read_bytes())["baseline"]
    passages, sources = book["passages"], book["sources"]
    before = deepcopy(book)
    decisions = {q["id"]: decide(q, passages, sources) for q in book["questions"]}
    for question in reversed(book["questions"]):
        decision = decide(question, passages[::-1], sources[::-1])
        assert decision == decisions[question["id"]]
        oracle = expected[question["id"]]
        for key in ("status", "reason", "eligible_families"):
            assert decision[key] == oracle[key]
        assert {row["passage_id"]: row["reason"] for row in decision["ledger"]} == oracle["ledger"]
        assert [row["passage_id"] for row in decision["ledger"]] == sorted(oracle["ledger"])
        for row in decision["ledger"]:
            passage = by_id(passages, row["passage_id"])
            source = by_id(sources, passage["source_id"])
            assert row["source_id"] == source["id"] and row["family_id"] == source["family_id"]
            assert row["source_sha256"] == source["sha256"]
            for key in ("estimate", "scope", "quote", "start", "end"):
                assert row[key] == passage[key]
    assert book == before


def test_no_endpoint_substitution_when_clinical_passage_is_omitted(book):
    passages = [p for p in book["passages"] if p["id"] != "trial-result"]
    result = decide(by_id(book["questions"], "intervention"), passages, book["sources"])
    assert (result["status"], result["reason"], result["eligible_families"]) == (
        "insufficient", "no_direct_result", [],
    )
    assert {row["passage_id"]: row["reason"] for row in result["ledger"]} == {
        "cohort-result": "related_question",
        "protocol-objective": "no_result",
        "trial-marker": "outcome_mismatch",
    }


def test_empty_ledger_still_identifies_the_question_and_retains_an_independent_scope(book):
    question = by_id(book["questions"], "prognosis")
    result = decide(question, [], book["sources"])
    assert result == {
        "question_id": question["id"], "question_text": question["text"],
        "question_scope": question["scope"], "status": "insufficient",
        "reason": "no_direct_result", "eligible_families": [], "ledger": [],
    }
    question["scope"]["months"] = 3
    assert result["question_scope"]["months"] == 12


def test_reversing_the_event_definition_excludes_the_only_eligible_result(book):
    question = by_id(book["questions"], "prognosis")
    by_id(book["passages"], "cohort-result")["scope"]["event_coding"] = "favorable"
    result = decide(question, book["passages"], book["sources"])
    assert (result["status"], result["reason"], result["eligible_families"]) == (
        "insufficient", "no_direct_result", [],
    )
    reasons = {row["passage_id"]: row["reason"] for row in result["ledger"]}
    assert reasons["cohort-result"] == "event_coding_mismatch"


def test_same_family_second_report_never_creates_an_independent_study_or_votes(book):
    question = by_id(book["questions"], "prognosis")
    original = by_id(book["passages"], "cohort-result")
    report = deepcopy(original)
    report.update(id="second-report-result", source_id="second-report")
    source = {**by_id(book["sources"], "cohort"), "id": "second-report"}
    sources = [*book["sources"], source]
    passages = [*book["passages"], report]
    same = decide(question, passages, sources)
    assert same["status"] == "association_observed"
    assert same["eligible_families"] == ["mx1-cohort"]
    assert sum(r["reason"] == "direct" for r in same["ledger"]) == 2

    report["estimate"].update(point=0.5, low=0.2, high=0.8)
    disagreement = decide(question, passages, sources)
    assert (disagreement["status"], disagreement["reason"], disagreement["eligible_families"]) == (
        "insufficient", "conflicting_family_estimates", ["mx1-cohort"],
    )

    source["family_id"] = "other-cohort"
    independent = decide(question, passages, sources)
    assert (independent["status"], independent["reason"], independent["eligible_families"]) == (
        "insufficient", "multiple_families_require_review", ["mx1-cohort", "other-cohort"],
    )


@pytest.mark.parametrize(
    ("intent", "low", "point", "high", "status", "reason"),
    [
        ("intervention", 0.5, 0.8, 1.4, "effect_uncertain", "interval_includes_null"),
        ("intervention", 0.5, 0.8, 1.0, "effect_uncertain", "interval_touches_null"),
        ("intervention", 1.0, 1.3, 1.4, "effect_uncertain", "interval_touches_null"),
        ("intervention", 0.0, 0.0, 0.5, "effect_estimate_reported", "single_family_intervention_result"),
        ("prognosis", 0.5, 0.7, 0.8, "association_observed", "single_family_association"),
        ("prognosis", 0.5, 1.1, 1.2, "association_uncertain", "interval_includes_null"),
        ("prognosis", 1.0, 1.1, 1.2, "association_uncertain", "interval_touches_null"),
    ],
)
def test_preserve_direction_coding_and_uncertainty(book, intent, low, point, high, status, reason):
    question = by_id(book["questions"], intent)
    passage = by_id(book["passages"], "trial-result" if intent == "intervention" else "cohort-result")
    for scope in (question["scope"], passage["scope"]):
        scope.update(outcome="synthetic_favorable_event", event_coding="favorable")
    passage["estimate"].update(low=low, point=point, high=high)
    result = decide(question, [passage], book["sources"])
    assert (result["status"], result["reason"]) == (status, reason)
    row, = result["ledger"]
    assert row["scope"] == question["scope"]
    assert row["estimate"] == passage["estimate"]


def test_an_unreviewed_report_cannot_outvote_direct_evidence(book):
    question = by_id(book["questions"], "prognosis")
    eligible = by_id(book["passages"], "cohort-result")
    excluded = deepcopy(eligible)
    excluded.update(id="unreviewed-result", review="unreviewed")
    excluded["estimate"].update(point=0.7, low=0.6, high=0.9)
    mixed = decide(question, [excluded, eligible], book["sources"])
    assert (mixed["status"], mixed["reason"], mixed["eligible_families"]) == (
        "association_observed", "single_family_association", ["mx1-cohort"],
    )
    assert [row["reason"] for row in mixed["ledger"]] == ["direct", "unreviewed"]
    excluded_only = decide(question, [excluded], book["sources"])
    assert (excluded_only["status"], excluded_only["reason"], excluded_only["eligible_families"]) == (
        "insufficient", "no_direct_result", [],
    )
