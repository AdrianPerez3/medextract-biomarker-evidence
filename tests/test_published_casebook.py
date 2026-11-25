"""Check the licensed worked example and exercise input variants using fictional records."""
from copy import deepcopy
from html import escape
import json
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from medextract_dossier.bundle_export import export_bundle
from medextract_dossier.bundle_format import ENVELOPE_NAME, validate_content
from medextract_dossier.bundle_verify import verify_bundle
from medextract_dossier.comparison import make_comparison, review_identifier
from medextract_dossier.evidence import json_bytes, load_casebook
from medextract_dossier.presentation import render_text
from medextract_dossier.signing import generate_keypair


ROOT = Path(__file__).resolve().parents[1]


def test_worked_trial_retains_the_primary_itt_estimate_and_its_provenance():
    directory = ROOT / "examples" / "crp-saa-trial"
    book = load_casebook(directory)
    comparison = make_comparison(book, [])
    assert comparison["evidence_origin"] == "published"
    decision, = comparison["decisions"]
    assert (decision["question_id"], decision["status"], decision["reason"]) == (
        "intervention", "effect_estimate_reported", "single_family_intervention_result")
    assert decision["eligible_families"] == ["nct06568432"]
    row, = decision["ledger"]
    assert row["reason"] == "direct" and row["passage_id"] == "index-antibiotic-prescribing"
    assert row["estimate"] == {"measure": "risk_ratio", "point": 0.80, "low": 0.78, "high": 0.82, "unit": "ratio"}
    assert row["scope"]["months"] == 0 and "index consultation" in row["scope"]["outcome"]
    assert row["url"] == "https://doi.org/10.1016/j.lanwpc.2026.101888"
    assert row["license"] == "CC-BY-4.0" and "101888" in row["citation"]
    source = (directory / row["source_file"]).read_bytes()
    assert source[row["start"]:row["end"]].decode() == row["quote"]
    without, = make_comparison(book, [row["passage_id"]])["decisions"]
    assert (without["status"], without["reason"]) == ("insufficient", "no_direct_result")


@pytest.fixture
def example(tmp_path):
    directory = shutil.copytree(ROOT / "examples" / "marker-mx1", tmp_path / "example")
    book = load_casebook(directory)
    book["evidence_origin"] = "published"
    for source in book["sources"]:
        source.update(citation=f"Fictional comparison & <appendix> ({source['id']}).",
                      url=f"https://journal.example.org/article/{source['id']}?lang=en&part=1",
                      license="CC-BY-4.0")
    (directory / "casebook.json").write_bytes(json_bytes(book))
    return directory, book


@pytest.fixture
def shipment(example, tmp_path):
    directory, _ = example
    private, public = tmp_path / "signer", tmp_path / "trusted"
    generate_keypair(private, public)
    bundle = export_bundle(directory, tmp_path / "handoff", private)
    return bundle, public


def bundle_members(bundle):
    return {p.relative_to(bundle).as_posix(): p.read_bytes() for p in bundle.rglob("*")
            if p.is_file() and p.name != ENVELOPE_NAME}


def test_declared_publications_survive_comparison_signature_and_replay(example, shipment):
    directory, book = example
    assert load_casebook(directory) == book
    comparison = make_comparison(book, [])
    sources = {source["id"]: source for source in book["sources"]}
    assert comparison["decisions"][0]["status"] == "association_observed"
    text = render_text(comparison)
    assert text.startswith("Published evidence review")
    assert "One study family reports an association" in text
    for decision in comparison["decisions"]:
        for row in decision["ledger"]:
            source = sources[row["source_id"]]
            for key in ("citation", "url", "license"):
                assert row[key] == source[key] and row[key] in text
    bundle, public = shipment
    receipt = verify_bundle(bundle, public)
    assert receipt["verified"] and receipt["review_id"] == review_identifier(book, comparison)
    members = bundle_members(bundle)
    assert json.loads(members["evidence.json"])["sources"] == book["sources"]
    assert json.loads(members["decision.json"]) == comparison
    report = members["report.html"].decode()
    assert "<title>Published evidence review</title>" in report
    assert "One independent synthetic cohort" not in report
    for source in book["sources"]:
        for key in ("citation", "url", "license"):
            assert escape(source[key]) in report
        assert members[source["file"]] == (directory / source["file"]).read_bytes()
    assert "<appendix>" not in report


def test_omitted_study_remains_attributed_in_signed_report(example, tmp_path):
    directory, book = example
    excluded = book["sources"][0]
    omitted = [p["id"] for p in book["passages"] if p["source_id"] == excluded["id"]]
    assert omitted
    private = tmp_path / "signer"
    generate_keypair(private, tmp_path / "trusted")
    bundle = export_bundle(directory, tmp_path / "handoff", private, omitted)
    comparison = json.loads((bundle / "decision.json").read_bytes())
    assert all(row["source_id"] != excluded["id"] for d in comparison["decisions"] for row in d["ledger"])
    assert escape(excluded["citation"]) in (bundle / "report.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["citation", "url", "license"])
@pytest.mark.parametrize("value", [None, "", "  ", 123])
def test_publication_provenance_is_required_on_each_source(example, field, value):
    directory, book = example
    if value is None:
        del book["sources"][1][field]
    else:
        book["sources"][1][field] = value
    (directory / "casebook.json").write_bytes(json_bytes(book))
    with pytest.raises(ValueError, match=field):
        load_casebook(directory)


@pytest.mark.parametrize("url", [
    "article", "http://journal.example.org/paper", "javascript:void(0)", "https:///paper",
    "https://-bad.example.org", "https://journal.example.org:99999", "https://single",
    "https://@journal.example.org", "https://journal.example.org/\npaper",
    "https://journal.example.org\\paper",
])
def test_publication_url_requires_a_meaningful_https_location(example, url):
    directory, book = example
    book["sources"][0]["url"] = url
    (directory / "casebook.json").write_bytes(json_bytes(book))
    with pytest.raises(ValueError, match="HTTPS"):
        load_casebook(directory)


@pytest.mark.parametrize("rights", ["reference-only", "permission unclear", "CC-BY-4.0 except this report"])
def test_local_rights_declaration_does_not_grant_signed_redistribution(example, tmp_path, rights):
    directory, book = example
    source = book["sources"][0]
    source["license"] = rights
    (directory / "casebook.json").write_bytes(json_bytes(book))
    comparison = make_comparison(load_casebook(directory), [])
    assert comparison["decisions"][0]["ledger"]
    # All source bytes travel in a signed handoff, even if their passages were omitted.
    omitted = [p["id"] for p in book["passages"] if p["source_id"] == source["id"]]
    private = tmp_path / "signer"
    generate_keypair(private, tmp_path / "trusted")
    destination = tmp_path / "handoff"
    with pytest.raises(ValueError, match="redistribution license"):
        export_bundle(directory, destination, private, omitted)
    assert not destination.exists()


@pytest.mark.parametrize("license_id", ["CC0-1.0", "CC-BY-3.0", "CC-BY-4.0"])
def test_supported_rights_cover_every_transported_source(example, tmp_path, license_id):
    directory, book = example
    for source in book["sources"]:
        source["license"] = license_id
    (directory / "casebook.json").write_bytes(json_bytes(book))
    private, public = tmp_path / "signer", tmp_path / "trusted"
    generate_keypair(private, public)
    bundle = export_bundle(directory, tmp_path / "handoff", private)
    assert verify_bundle(bundle, public)["verified"]
    assert license_id in (bundle / "report.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("variant", ["no_passages", "unreviewed", "different_scope", "no_estimate"])
def test_citation_alone_cannot_establish_a_result(example, variant):
    directory, book = example
    book["passages"] = [p for p in book["passages"] if p["id"] == "cohort-result"]
    passage, = book["passages"]
    if variant == "no_passages":
        book["passages"].clear()
    elif variant == "unreviewed":
        passage["review"] = "unreviewed"
    elif variant == "different_scope":
        passage["scope"]["population"] = "Other fictional population"
    else:
        passage["estimate"] = None
    (directory / "casebook.json").write_bytes(json_bytes(book))
    for decision in make_comparison(load_casebook(directory), [])["decisions"]:
        assert decision["status"] == "insufficient" and decision["eligible_families"] == []


def test_published_passage_must_match_captured_bytes(example):
    directory, book = example
    book["passages"][0]["quote"] += " Fabricated suffix."
    (directory / "casebook.json").write_bytes(json_bytes(book))
    with pytest.raises(ValueError, match="quote differs"):
        load_casebook(directory)


@pytest.mark.parametrize("name", ["question.json", "evidence.json", "decision.json"])
def test_disagreement_between_origins_fails_before_decision_replay(shipment, name):
    bundle, _ = shipment
    members = bundle_members(bundle)
    section = json.loads(members[name])
    section["evidence_origin"] = "synthetic"
    members[name] = json_bytes(section)
    with pytest.raises(ValueError, match="evidence_origin must agree"):
        validate_content(members)


def test_replay_rejects_inconsistent_publication_attribution_or_rights(shipment):
    bundle, _ = shipment
    original = bundle_members(bundle)
    for field in ("citation", "url", "license"):
        comparison = json.loads(original["decision.json"])
        del comparison["decisions"][0]["ledger"][0][field]
        with pytest.raises(ValueError, match="decision.json differs"):
            validate_content({**original, "decision.json": json_bytes(comparison)})
    evidence = json.loads(original["evidence.json"])
    evidence["sources"][0]["license"] = "reference-only"
    with pytest.raises(ValueError, match="redistribution license"):
        validate_content({**original, "evidence.json": json_bytes(evidence)})


def test_schemas_enforce_fields_for_the_declared_origin(example):
    _, book = example
    comparison = make_comparison(book, [])
    directory = ROOT / "schemas" / "v1"
    registry = Registry().with_resources(
        (p.as_uri(), Resource.from_contents(json.loads(p.read_bytes()))) for p in directory.glob("*.json"))
    for name, document in (("casebook", book), ("comparison", comparison)):
        schema = {"$ref": (directory / f"{name}.schema.json").as_uri()}
        validator = Draft202012Validator(schema, registry=registry)
        validator.validate(document)
        assert not validator.is_valid({**document, "evidence_origin": "synthetic"})
        for key in ("citation", "url", "license"):
            partial = deepcopy(document)
            record = partial["sources"][0] if name == "casebook" else partial["decisions"][0]["ledger"][0]
            del record[key]
            assert not validator.is_valid(partial)
