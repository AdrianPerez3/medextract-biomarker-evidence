"""Exercise the local service through the same IDs used by the public adapter."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from medextract_dossier import service
from medextract_dossier.bundle_export import export_bundle
from medextract_dossier.bundle_verify import verify_bundle
from medextract_dossier.comparison import make_comparison
from medextract_dossier.evidence import load_casebook
from medextract_dossier.service import DossierService, ServiceError
from medextract_dossier.signing import generate_keypair

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "marker-mx1"


@pytest.fixture
def operator(tmp_path):
    example = shutil.copytree(EXAMPLE, tmp_path / "input")
    private, public = tmp_path / "producer.pem", tmp_path / "trusted.pem"
    generate_keypair(private, public)
    return dict(example=example, output_dir=tmp_path / "out", private_key=private, public_key=public)


def files(directory):
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_identical_source_cannot_be_attributed_to_two_independent_families(operator):
    example = operator["example"]
    path = example / "casebook.json"
    book = json.loads(path.read_bytes())
    source = book["sources"][0]
    other = dict(source, id="another-report", file="sources/alternate.txt")
    shutil.copyfile(example / source["file"], example / other["file"])
    book["sources"].append(other)
    path.write_text(json.dumps(book))
    load_casebook(example)  # Another citation to the same family is allowed.
    other["family_id"] = book["sources"][1]["family_id"]
    path.write_text(json.dumps(book))
    with pytest.raises(ValueError, match="study families"):
        load_casebook(example)
    with pytest.raises(ServiceError):
        DossierService(**operator).build()


def test_build_accepts_the_same_source_names_with_or_without_a_signer(operator):
    example = operator["example"]
    path = example / "casebook.json"
    book = json.loads(path.read_bytes())
    source = book["sources"][0]
    name = "sources/Étude MX-1.txt"
    (example / source["file"]).rename(example / name)
    source["file"] = name
    path.write_text(json.dumps(book))
    signing = DossierService(**operator)
    review = signing.build()
    assert review == DossierService(example=example).build()
    with pytest.raises(ServiceError, match="portable"):
        signing.export(review["review_id"])
    assert not operator["output_dir"].exists()


@pytest.mark.parametrize("limit", ["_BUNDLE_BYTES", "_BUNDLE_ENTRIES"])
def test_export_limits_match_recipient_limits_before_any_files_are_written(operator, tmp_path, monkeypatch, limit):
    instance = DossierService(**operator)
    review = instance.build()["review_id"]
    sample = export_bundle(operator["example"], tmp_path / "sample", operator["private_key"])
    entries = list(sample.rglob("*"))
    exact = len(entries) if limit == "_BUNDLE_ENTRIES" else sum(p.stat().st_size for p in entries if p.is_file())
    monkeypatch.setattr(service, limit, exact - 1)
    with pytest.raises(ServiceError, match="limit"):
        instance.export(review)
    assert not operator["output_dir"].exists()
    monkeypatch.setattr(service, limit, exact)
    assert instance.verify(instance.export(review)["bundle_id"])["verified"]


def test_review_pins_all_validated_inputs_even_when_decisions_are_unchanged(operator, tmp_path):
    instance = DossierService(**operator)
    example = operator["example"]
    out = operator["output_dir"]
    omitted = ["cohort-result"]
    review = instance.build(without_passages=omitted)
    review_id = review["review_id"]
    assert review["review_uri"] == f"dossier://reviews/{review_id}"
    assert instance.build(without_passages=omitted * 2) == review
    full = json.loads(instance.read_review(review_id))
    assert full == make_comparison(load_casebook(example), omitted)
    assert review["decisions"] == [
        {**{k: d[k] for k in ("question_id", "status", "reason")},
         "eligible_study_count": len(d["eligible_families"])}
        for d in full["decisions"]]
    assert review["decisions"][0]["status"] == "insufficient"
    assert not out.exists()
    original = export_bundle(example, tmp_path / "original", operator["private_key"], omitted)

    casebook = example / "casebook.json"
    book = json.loads(casebook.read_bytes())
    book["families"][0]["label"] += " (revised)"
    casebook.write_text(json.dumps(book), encoding="utf-8")
    revised = instance.build(without_passages=omitted)
    assert revised["review_id"] != review_id
    assert revised["decisions"] == review["decisions"]
    cohort = next(s for s in book["sources"] if s["id"] == "cohort")
    source = example / cohort["file"]
    changed_bytes = source.read_bytes() + b"Revised fictional source.\n"
    source.write_bytes(changed_bytes)
    cohort["sha256"] = hashlib.sha256(changed_bytes).hexdigest()
    casebook.write_text(json.dumps(book), encoding="utf-8")
    changed = instance.build(without_passages=omitted)
    assert changed["review_id"] != revised["review_id"]
    assert json.loads(instance.read_review(changed["review_id"])) == full
    shutil.rmtree(example)
    changed_bundle = instance.export(changed["review_id"])["bundle_id"]
    assert (out / changed_bundle / cohort["file"]).read_bytes() == changed_bytes
    for _ in range(2):
        bundle = instance.export(review_id)
        assert bundle == {"schema_version": 1, "review_id": review_id,
                          "bundle_id": f"review-{review_id}", "review_uri": review["review_uri"]}
        assert files(out / bundle["bundle_id"]) == files(original)
    assert instance.verify(bundle["bundle_id"])["verified"] is True
    assert DossierService(**operator).verify(bundle["bundle_id"])["verified"] is True
    with pytest.raises(ServiceError, match="(?i)build"):
        DossierService(**operator).export(review_id)


def test_receipts_remain_available_after_tamper_and_wrong_trusted_key(operator, tmp_path):
    bundle = export_bundle(operator["example"], tmp_path / "incoming", operator["private_key"])
    public = operator["public_key"]
    private = operator["private_key"]
    shutil.rmtree(operator["example"])
    private.unlink()
    config = dict(example=None, output_dir=None, private_key=None, public_key=public,
                  bundles={"received": bundle})
    recipient = DossierService(**config)
    before = files(tmp_path)
    check = recipient.verify("received")
    assert check == recipient.verify("received")
    assert files(tmp_path) == before
    assert check["receipt_uri"] == f"dossier://receipts/{check['receipt_id']}"
    assert check["signature"] == "valid"
    original = json.loads(recipient.read_receipt(check["receipt_id"]))
    assert original == verify_bundle(bundle, public)
    assert original["verified"]
    member = bundle / "report.html"
    member.write_bytes(member.read_bytes() + b"x")
    damaged = recipient.verify("received")
    assert damaged["verified"] is False and damaged["integrity"] == "mismatch"
    assert damaged["receipt_id"] != check["receipt_id"]
    assert json.loads(recipient.read_receipt(check["receipt_id"])) == original
    assert json.loads(recipient.read_receipt(damaged["receipt_id"])) == verify_bundle(bundle, public)
    other = tmp_path / "other.pub"
    generate_keypair(tmp_path / "other.pem", other)
    wrong = DossierService(**{**config, "public_key": other})
    check = wrong.verify("received")
    assert check["signature"] == "invalid" and not check["verified"]
    assert json.loads(wrong.read_receipt(check["receipt_id"])) == verify_bundle(bundle, other)


def test_recent_reviews_and_receipts_are_bounded(operator, monkeypatch):
    monkeypatch.setattr(service, "_CACHE_ENTRIES", 2)
    instance = DossierService(**operator)
    first, second = [instance.build(without_passages=x)["review_id"] for x in ([], ["cohort-result"])]
    instance.read_review(first)
    third = instance.build(without_passages=["trial-result"])["review_id"]
    for method in (instance.read_review, instance.export):
        with pytest.raises(ServiceError, match="(?i)build"):
            method(second)
    first_id = instance.export(first)["bundle_id"]
    third_id = instance.export(third)["bundle_id"]
    a = instance.verify(first_id)["receipt_id"]
    b = instance.verify(third_id)["receipt_id"]
    instance.read_receipt(a)
    (operator["output_dir"] / third_id / "report.html").unlink()
    c = instance.verify(third_id)["receipt_id"]
    assert c not in (a, b)
    with pytest.raises(ServiceError, match="(?i)verify"):
        instance.read_receipt(b)
    assert json.loads(instance.read_receipt(a))["verified"]


def test_hardlinked_private_key_cannot_be_captured_as_evidence(operator):
    example = operator["example"]
    key = operator["private_key"]
    name = "sources/extra.txt"
    (example / name).hardlink_to(key)
    casebook = example / "casebook.json"
    book = json.loads(casebook.read_bytes())
    book["sources"].append({"id": "extra", "family_id": book["families"][0]["id"],
                            "file": name, "sha256": hashlib.sha256(key.read_bytes()).hexdigest()})
    casebook.write_text(json.dumps(book), encoding="utf-8")
    with pytest.raises(ServiceError):
        DossierService(**operator).build()
    assert not operator["output_dir"].exists()


@pytest.mark.parametrize("replacement", ["missing", "invalid_pem", "oversized", "input_key"])
def test_bad_signing_key_leaves_no_partial_export(operator, replacement):
    instance = DossierService(**operator)
    review = instance.build()
    key = operator["private_key"]
    if replacement == "missing":
        key.unlink()
    elif replacement == "invalid_pem":
        key.write_bytes(b"unusable local key material")
    elif replacement == "oversized":
        key.write_bytes(b"X" * (service._KEY_BYTES + 1))
    else:
        relocated = key.rename(operator["example"] / "moved.pem")
        key.symlink_to(relocated)
    with pytest.raises(ServiceError, match="(?i)key") as error:
        instance.export(review["review_id"])
    assert str(key) not in str(error.value)
    assert "unusable local key material" not in str(error.value)
    assert not operator["output_dir"].exists()


def test_separate_output_directory_keeps_the_signing_key_out_of_shared_tree(operator):
    out = operator["output_dir"]
    out.mkdir()
    relocated = operator["private_key"].rename(out / "signing.pem")
    operator["private_key"].symlink_to(relocated)
    instance = DossierService(**operator)
    review = instance.build()
    with pytest.raises(ServiceError, match="(?i)key"):
        instance.export(review["review_id"])
    assert list(out.iterdir()) == [relocated]


def test_generated_bundle_name_requires_the_corresponding_authenticated_review(operator):
    instance = DossierService(**operator)
    first = instance.export(instance.build()["review_id"])["bundle_id"]
    other = instance.export(instance.build(without_passages=["trial-result"])["review_id"])["bundle_id"]
    out = operator["output_dir"]
    shutil.rmtree(out / first)
    shutil.copytree(out / other, out / first)
    with pytest.raises(ServiceError, match="(?i)review"):
        instance.verify(first)
    explicitly_selected = DossierService(public_key=operator["public_key"], bundles={"selected": out / first})
    assert explicitly_selected.verify("selected")["verified"]


def test_ids_cannot_select_paths_or_escape_via_bundle_symlinks(operator, tmp_path):
    outside = tmp_path / "unselected"
    export_bundle(operator["example"], outside, operator["private_key"])
    instance = DossierService(**operator, bundles={"missing": tmp_path / "unavailable"})
    out = operator["output_dir"]
    out.mkdir()
    name = "review-" + "a" * 64
    (out / name).symlink_to(outside, target_is_directory=True)
    for invalid in (name, "unselected", "missing", str(outside), "../unselected", "x/y", "A" * 71):
        with pytest.raises(ServiceError) as error:
            instance.verify(invalid)
        assert str(tmp_path) not in str(error.value)
    for call in (lambda: instance.build(str(outside)), lambda: instance.export(str(outside)),
                 lambda: instance.read_review(str(outside)), lambda: instance.read_receipt(str(outside)),
                 lambda: instance.build(without_passages=[str(outside)])):
        with pytest.raises(ServiceError) as error:
            call()
        assert str(tmp_path) not in str(error.value)
    (out / name).unlink()
    out.rmdir()
    out.symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / name).symlink_to(outside, target_is_directory=True)
    review = instance.build()
    with pytest.raises(ServiceError):
        instance.export(review["review_id"])
    assert not (tmp_path / f"review-{review['review_id']}").exists()


@pytest.mark.parametrize("key_problem", ["missing", "non_pem", "directory", "inside_bundle"])
def test_invalid_trusted_key_is_an_operational_error(operator, tmp_path, key_problem):
    bundle = export_bundle(operator["example"], tmp_path / "recipient", operator["private_key"])
    key = tmp_path / "invalid-trust.pem"
    if key_problem == "non_pem":
        key.write_bytes(b"invalid key")
    elif key_problem == "directory":
        key.mkdir()
    elif key_problem == "inside_bundle":
        key = bundle / "supplied.pem"
        shutil.copyfile(operator["public_key"], key)
    recipient = DossierService(example=None, output_dir=None, private_key=None, public_key=key,
                               bundles={"incoming": bundle})
    with pytest.raises(ServiceError, match="(?i)key") as error:
        recipient.verify("incoming")
    assert str(tmp_path) not in str(error.value) and "invalid key" not in str(error.value)


def test_bounds_on_input_capture_and_verification(operator, monkeypatch):
    instance = DossierService(**operator)
    bundle = instance.export(instance.build()["review_id"])["bundle_id"]
    monkeypatch.setattr(service, "_REVIEW_INPUT_BYTES", 16)
    with pytest.raises(ServiceError):
        instance.build()
    monkeypatch.setattr(service, "_BUNDLE_BYTES", 16)
    with pytest.raises(ServiceError, match="(?i)limit"):
        instance.verify(bundle)


@pytest.mark.parametrize("omissions", [["x" * 13], ["éé"] * 3, [""] * 13])
def test_oversized_omissions_are_rejected_before_reading_the_example(operator, monkeypatch, omissions):
    def reject_read(*args, **kwargs):
        pytest.fail("read the casebook before checking the client input")

    monkeypatch.setattr(service, "_REVIEW_INPUT_BYTES", 12)
    monkeypatch.setattr(service, "load_casebook_inputs", reject_read)
    with pytest.raises(ServiceError, match="(?i)limit"):
        DossierService(**operator).build(without_passages=omissions)


def test_verification_uses_the_single_captured_trusted_key(operator, tmp_path, monkeypatch):
    from medextract_dossier import signing

    path = export_bundle(operator["example"], tmp_path / "incoming", operator["private_key"])
    load = signing.load_public_key

    def capture(key):
        trusted = load(key)
        key.unlink()
        return trusted

    monkeypatch.setattr(signing, "load_public_key", capture)
    instance = DossierService(public_key=operator["public_key"], bundles={"received": path})
    assert instance.verify("received")["verified"]


def test_record_and_directory_entry_limits(operator, monkeypatch):
    instance = DossierService(**operator)
    bundle = instance.export(instance.build()["review_id"])["bundle_id"]
    monkeypatch.setattr(service, "_REVIEW_RECORD_BYTES", 100)
    with pytest.raises(ServiceError, match="(?i)limit"):
        instance.build()
    monkeypatch.setattr(service, "_BUNDLE_ENTRIES", 9)
    assert instance.verify(bundle)["verified"]
    (operator["output_dir"] / bundle / "unexpected").touch()
    with pytest.raises(ServiceError, match="(?i)limit"):
        instance.verify(bundle)


def test_distinct_authenticated_manifests_under_one_name_get_distinct_receipts(operator, tmp_path):
    path = tmp_path / "incoming"
    private = operator["private_key"]
    export_bundle(operator["example"], path, private)
    instance = DossierService(public_key=operator["public_key"], bundles={"received": path})
    first = instance.verify("received")["receipt_id"]
    shutil.rmtree(path)
    export_bundle(operator["example"], path, private, ["trial-result"])
    second = instance.verify("received")["receipt_id"]
    assert first != second
    a, b = [json.loads(instance.read_receipt(identifier)) for identifier in (first, second)]
    assert a["manifest_sha256"] != b["manifest_sha256"]
    assert a["members"] == b["members"]


def test_invalid_unicode_in_a_claim_still_yields_the_original_failed_receipt(operator, tmp_path):
    path = export_bundle(operator["example"], tmp_path / "incoming", operator["private_key"])
    manifest = path / "manifest.dsse.json"
    envelope = json.loads(manifest.read_bytes())
    envelope["signatures"][0]["keyid"] = chr(0xD800)
    manifest.write_text(json.dumps(envelope), encoding="utf-8")
    instance = DossierService(public_key=operator["public_key"], bundles={"received": path})
    check = instance.verify("received")
    assert check["signature"] == "invalid" and not check["verified"]
    assert json.loads(instance.read_receipt(check["receipt_id"])) == verify_bundle(path, operator["public_key"])


def test_concurrent_calls_return_consistent_snapshots_and_receipts(operator):
    instance = DossierService(**operator)

    def run(index):
        omissions = ["cohort-result"] if index % 2 else []
        review = instance.build(without_passages=omissions)
        bundle = instance.export(review["review_id"])
        verification = instance.verify(bundle["bundle_id"])
        receipt = json.loads(instance.read_receipt(verification["receipt_id"]))
        assert receipt["verified"]
        assert json.loads(instance.read_review(review["review_id"]))["omitted_passages"] == omissions
        return review["review_id"], verification["receipt_id"]

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run, range(12)))
    assert len(set(results[::2])) == len(set(results[1::2])) == 1
    assert results[0] != results[1]
