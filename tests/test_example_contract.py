"""Exercise the file contract and the byte-level evidence checks used by the CLI."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from medextract_dossier.evidence import load_casebook


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "marker-mx1"


@pytest.fixture
def example(tmp_path):
    directory = tmp_path / "marker-mx1"
    shutil.copytree(EXAMPLE, directory)
    return directory, json.loads((directory / "casebook.json").read_bytes())


def reload(directory, book):
    (directory / "casebook.json").write_text(json.dumps(book), encoding="utf-8")
    return load_casebook(directory)


def record(book, collection, identifier):
    return next(item for item in book[collection] if item["id"] == identifier)


def test_example_load_returns_the_unchanged_records_and_exact_quotes():
    book = load_casebook(EXAMPLE)
    assert book == json.loads((EXAMPLE / "casebook.json").read_bytes())
    assert {q["id"] for q in book["questions"]} == {"prognosis", "intervention"}
    assert {s["id"] for s in book["sources"]} == {"cohort", "trial", "protocol"}
    assert {p["id"] for p in book["passages"]} == {
        "cohort-result", "trial-result", "trial-marker", "protocol-objective"
    }
    for passage in book["passages"]:
        source = record(book, "sources", passage["source_id"])
        raw = (EXAMPLE / source["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        assert raw[passage["start"]:passage["end"]] == passage["quote"].encode("utf-8")


@pytest.mark.parametrize("version", [0, 2, True, 1.1, "1"])
def test_only_integer_version_one_is_supported(example, version):
    directory, book = example
    book["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        reload(directory, book)


def test_json_integer_values_are_normalized_for_python_byte_slicing(example):
    directory, book = example
    book["schema_version"] = 1.0
    passage = record(book, "passages", "cohort-result")
    passage["start"] = float(passage["start"])
    validated = reload(directory, book)
    assert type(validated["schema_version"]) is int
    assert type(record(validated, "passages", "cohort-result")["start"]) is int


@pytest.mark.parametrize("origin", [None, "clinical", ""])
def test_v1_requires_an_explicit_synthetic_origin(example, origin):
    directory, book = example
    if origin is None:
        del book["evidence_origin"]
    else:
        book["evidence_origin"] = origin
    with pytest.raises(ValueError, match="evidence_origin"):
        reload(directory, book)


def test_one_question_can_review_empty_evidence_but_zero_questions_is_invalid(example):
    directory, book = example
    book.update(questions=book["questions"][:1], families=[], sources=[], passages=[])
    assert reload(directory, book) == book
    book["questions"] = []
    with pytest.raises(ValueError, match="at least one question"):
        reload(directory, book)


@pytest.mark.parametrize("collection", ["questions", "families", "sources", "passages"])
def test_ids_are_unique_within_each_collection(example, collection):
    directory, book = example
    book[collection].append(dict(book[collection][0]))
    with pytest.raises(ValueError, match="duplicate id"):
        reload(directory, book)


def test_duplicate_object_keys_are_rejected_before_they_can_overwrite_values(example):
    directory, _ = example
    file = directory / "casebook.json"
    raw = file.read_bytes().replace(b"{", b'{"schema_version": 2,', 1)
    file.write_bytes(raw)
    with pytest.raises(ValueError, match="duplicate JSON key 'schema_version'"):
        load_casebook(directory)


@pytest.mark.parametrize(("collection", "field"), [("sources", "family_id"), ("passages", "source_id")])
def test_references_must_resolve(example, collection, field):
    directory, book = example
    book[collection][0][field] = "absent"
    with pytest.raises(ValueError, match=rf"{field} references unknown id"):
        reload(directory, book)


def test_wrong_contrast_and_question_id_fail_even_when_both_appear_as_valid_strings(example):
    directory, book = example
    prognosis = record(book, "questions", "prognosis")
    scope = prognosis["scope"]
    scope["direction"] = "assigned_vs_control"
    with pytest.raises(ValueError, match="direction"):
        reload(directory, book)
    scope["intent"] = "intervention"
    with pytest.raises(ValueError, match="id must agree"):
        reload(directory, book)


def test_factor_and_comparator_must_identify_different_groups(example):
    directory, book = example
    scope = book["questions"][0]["scope"]
    scope["comparator"] = scope["factor"]
    with pytest.raises(ValueError, match="distinct"):
        reload(directory, book)


def test_declared_family_and_passage_fields_are_required(example):
    directory, book = example
    book["families"][0]["lable"] = book["families"][0].pop("label")
    with pytest.raises(ValueError, match="label.*lable"):
        reload(directory, book)
    book["families"][0]["label"] = book["families"][0].pop("lable")
    del book["passages"][0]["review"]
    with pytest.raises(ValueError, match="review"):
        reload(directory, book)


@pytest.mark.parametrize("name", ["../cohort.txt", "/tmp/cohort.txt", "sources/../cohort.txt", "sources/nested/a.txt"])
def test_source_paths_are_local_flat_text_files(example, name):
    directory, book = example
    book["sources"][0]["file"] = name
    with pytest.raises(ValueError, match="sources/<basename>.txt"):
        reload(directory, book)


def test_source_must_be_present_and_match_digest_even_when_unquoted(example):
    directory, book = example
    trial = record(book, "sources", "trial")
    book["passages"] = [p for p in book["passages"] if p["source_id"] != trial["id"]]
    file = directory / trial["file"]
    file.write_bytes(file.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        reload(directory, book)
    file.unlink()
    with pytest.raises(ValueError, match="cannot read"):
        reload(directory, book)


def test_invalid_utf8_bytes_outside_a_quote_fail_even_with_a_matching_hash(example):
    directory, book = example
    source = record(book, "sources", "cohort")
    file = directory / source["file"]
    raw = file.read_bytes() + b"\xff"
    file.write_bytes(raw)
    source["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="valid UTF-8"):
        reload(directory, book)


def test_byte_offsets_preserve_line_endings_and_reject_split_characters(example):
    directory, book = example
    passage = record(book, "passages", "cohort-result")
    source = record(book, "sources", "cohort")
    prefix = "α\r\n".encode("utf-8")
    quote = "β\r\nγ"
    encoded = quote.encode("utf-8")
    raw = prefix + encoded + b"\r\n"
    (directory / source["file"]).write_bytes(raw)
    source["sha256"] = hashlib.sha256(raw).hexdigest()
    start, end = len(prefix), len(prefix) + len(encoded)
    passage.update(start=start, end=end, quote=quote)
    assert reload(directory, book) == book
    for invalid_start, invalid_end in [(start + 1, end), (start, end - 1)]:
        passage.update(start=invalid_start, end=invalid_end)
        with pytest.raises(ValueError, match="splits a UTF-8"):
            reload(directory, book)


@pytest.mark.parametrize(("field", "value"), [("start", -1), ("end", 1_000_000), ("end", 0), ("start", True)])
def test_out_of_bounds_or_nonintegral_locator_fails(example, field, value):
    directory, book = example
    record(book, "passages", "cohort-result")[field] = value
    with pytest.raises(ValueError, match="cohort-result"):
        reload(directory, book)


def test_changed_quote_or_shifted_valid_byte_boundary_fails(example):
    directory, book = example
    passage = record(book, "passages", "cohort-result")
    quote = passage["quote"]
    passage["quote"] = quote.swapcase()
    with pytest.raises(ValueError, match="quote differs"):
        reload(directory, book)
    passage.update(quote=quote, end=passage["end"] - 1)
    with pytest.raises(ValueError, match="quote differs"):
        reload(directory, book)


@pytest.mark.parametrize(("changes", "error"), [
    ({"unit": "mock units/L"}, "unit"),
    ({"unit": ""}, "unit"),
    ({"low": -0.01}, "nonnegative"),
    ({"high": 0.5}, "low <= point <= high"),
    ({"point": False}, "finite number"),
    ({"low": float("-inf")}, "finite JSON number"),
    ({"high": float("inf")}, "finite JSON number"),
    ({"point": float("nan")}, "finite JSON number"),
    ({"measure": "odds_ratio"}, "measure"),
])
def test_estimate_domain_units_order_and_finiteness(example, changes, error):
    directory, book = example
    estimate = record(book, "passages", "cohort-result")["estimate"]
    estimate.update(changes)
    with pytest.raises(ValueError, match=error):
        reload(directory, book)


def test_exponent_overflow_cannot_pass_as_a_finite_estimate(example):
    directory, book = example
    record(book, "passages", "cohort-result")["estimate"]["high"] = 314159
    raw = json.dumps(book).replace("314159", "1e999")
    (directory / "casebook.json").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="finite number"):
        load_casebook(directory)


def test_zero_risk_ratio_and_negative_mean_difference_are_valid_numbers(example):
    directory, book = example
    record(book, "passages", "cohort-result")["estimate"].update(low=0, point=0, high=0.12)
    assert reload(directory, book) == book
    assert record(book, "passages", "trial-marker")["estimate"]["high"] < 0


def test_objective_cannot_silently_carry_a_result_estimate(example):
    directory, book = example
    objective = record(book, "passages", "protocol-objective")
    objective["estimate"] = record(book, "passages", "cohort-result")["estimate"]
    with pytest.raises(ValueError, match="objective requires a null estimate"):
        reload(directory, book)
