"""Pin applicability, including exclusions that could reverse an interpretation."""

import json
from pathlib import Path

import pytest

from medextract_dossier.questions import applicability


@pytest.fixture
def cohort():
    fixture = Path(__file__).resolve().parents[1] / "examples/marker-mx1/casebook.json"
    book = json.loads(fixture.read_bytes())
    question = next(q for q in book["questions"] if q["id"] == "prognosis")
    passage = next(p for p in book["passages"] if p["id"] == "cohort-result")
    return question, passage


@pytest.mark.parametrize(("field", "replacement", "reason"), [
    ("intent", "intervention", "related_question"),
    ("population", "other_population", "population_mismatch"),
    ("factor", "another_marker", "contrast_mismatch"),
    ("comparator", "another_comparator", "contrast_mismatch"),
    ("outcome", "another_outcome", "outcome_mismatch"),
    ("direction", "assigned_vs_control", "contrast_mismatch"),
    ("event_coding", "favorable", "event_coding_mismatch"),
    ("months", 6, "time_horizon_mismatch"),
])
def test_one_changed_scope_field_has_an_explicit_reason(cohort, field, replacement, reason):
    question, passage = cohort
    assert applicability(question, passage) == "direct"
    passage["scope"][field] = replacement
    assert applicability(question, passage) == reason


def test_review_and_result_gates_precede_comparison(cohort):
    question, passage = cohort
    passage["scope"]["intent"] = "intervention"
    passage.update(review="unreviewed", kind="objective", estimate=None)
    assert applicability(question, passage) == "unreviewed"
    passage.update(review="reviewed")
    assert applicability(question, passage) == "no_result"


def test_opaque_question_wording_cannot_override_the_structured_contrast(cohort):
    question, passage = cohort
    question["text"] = passage["quote"]
    question["scope"]["comparator"] = "different_control"
    assert applicability(question, passage) == "contrast_mismatch"


def test_matching_but_incoherent_direction_cannot_produce_a_result(cohort):
    question, passage = cohort
    for scope in (question["scope"], passage["scope"]):
        scope["direction"] = "assigned_vs_control"
    assert applicability(question, passage) == "uncertain"


@pytest.mark.parametrize("estimate_change", [
    {"measure": "mean_difference", "unit": "mock units/L"},
    {"low": 1.0, "high": 1.0, "point": 1.0},
])
def test_matching_scope_requires_an_interpretable_estimate(cohort, estimate_change):
    question, passage = cohort
    passage["estimate"].update(estimate_change)
    assert applicability(question, passage) == "uncertain"
