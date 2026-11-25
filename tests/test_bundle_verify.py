"""Recipient checks against the original synthetic MX-1 fixture and local test keys."""

import base64
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from medextract_dossier.bundle_export import export_bundle
from medextract_dossier.bundle_format import ENVELOPE_NAME
from medextract_dossier.bundle_verify import verify_bundle
from medextract_dossier.signing import generate_keypair, sign_manifest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "marker-mx1"


@pytest.fixture
def shipment(tmp_path):
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    generate_keypair(private, public)
    bundle = export_bundle(EXAMPLE, tmp_path / "handoff", private)
    return bundle, private, public


def read_manifest(bundle):
    envelope = json.loads((bundle / ENVELOPE_NAME).read_bytes())
    return json.loads(base64.b64decode(envelope["payload"]))


def write_signed_manifest(bundle, private, manifest):
    # A standalone producer is free to choose whitespace for the signed JSON bytes.
    payload = json.dumps(manifest, indent=1, sort_keys=False).encode("utf-8") + b"\r\n"
    envelope = sign_manifest(payload, private)
    (bundle / ENVELOPE_NAME).write_text(json.dumps(envelope), encoding="utf-8")


def test_signature_covers_the_original_payload_bytes(shipment):
    bundle, private, public = shipment
    write_signed_manifest(bundle, private, read_manifest(bundle))
    private.unlink()
    result = verify_bundle(bundle, public)
    assert result["verified"] and result["replay"] == "match"
    assert result["supplied_key_fingerprint"] == result["claimed_keyid"] == result["signer_fingerprint"]


def test_early_v1_missing_estimate_remains_replayable_only_under_its_exact_old_reason(tmp_path):
    from medextract_dossier.comparison import review_identifier

    example = shutil.copytree(EXAMPLE, tmp_path / "example")
    path = example / "casebook.json"
    book = json.loads(path.read_bytes())
    next(p for p in book["passages"] if p["id"] == "cohort-result")["estimate"] = None
    path.write_text(json.dumps(book))
    private, public = tmp_path / "producer", tmp_path / "public"
    generate_keypair(private, public)
    bundle = export_bundle(example, tmp_path / "handoff", private)
    path = bundle / "decision.json"
    comparison = json.loads(path.read_bytes())
    rows = [row for decision in comparison["decisions"] for row in decision["ledger"]
            if row["passage_id"] == "cohort-result"]
    assert rows and all(row["reason"] == "estimate_missing" for row in rows)
    for label, valid in [("no_result", True), ("unreviewed", False)]:
        for row in rows:
            row["reason"] = label
        data = json.dumps(comparison).encode()
        path.write_bytes(data)
        manifest = read_manifest(bundle)
        item = next(item for item in manifest["members"] if item["path"] == path.name)
        item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        write_signed_manifest(bundle, private, manifest)
        receipt = verify_bundle(bundle, public)
        assert receipt["verified"] is valid
        assert receipt["review_id"] == (review_identifier(book, comparison) if valid else None)


@pytest.mark.parametrize(("change", "error"), [
    (lambda m: m.update(schema_version=2), "schema_version"),
    (lambda m: m.update(schema_version=True), "schema_version"),
    (lambda m: m.update(members={}), "array"),
    (lambda m: m["members"].reverse(), "sorted"),
    (lambda m: m["members"].append(m["members"][0]), "duplicate"),
    (lambda m: m["members"].pop(0), "required"),
    (lambda m: m["members"][0].update(path="notes.csv"), "path"),
    (lambda m: m["members"][0].update(bytes=False), "bytes"),
    (lambda m: m["members"][0].update(bytes=-1), "bytes"),
    (lambda m: m["members"][0].update(sha256="abcd"), "sha256"),
    (lambda m: m["members"][0].update(other="x"), "member"),
])
def test_signed_malformed_manifests_stop_before_member_verification(shipment, change, error):
    bundle, private, public = shipment
    manifest = read_manifest(bundle)
    change(manifest)
    write_signed_manifest(bundle, private, manifest)
    result = verify_bundle(bundle, public)
    assert not result["verified"]
    assert result["signature"] == "valid" and result["signer_fingerprint"]
    assert result["integrity"] == result["replay"] == "not_checked"
    assert result["members"] == {}
    assert error in result["error"].lower()


@pytest.mark.parametrize(("name", "change", "diagnostic"), [
    ("evidence.json", lambda doc: doc["passages"][0].update(start=0), "quote"),
    ("decision.json", lambda doc: doc["decisions"][0].update(status="insufficient"), "decision.json"),
    ("question.json", lambda doc: doc.update(schema_version=2), "question.json"),
])
def test_signed_inconsistent_content_fails_replay_only(shipment, name, change, diagnostic):
    bundle, private, public = shipment
    manifest = read_manifest(bundle)
    path = bundle / name
    document = json.loads(path.read_bytes())
    change(document)
    data = json.dumps(document).encode("utf-8")
    path.write_bytes(data)
    item = next(item for item in manifest["members"] if item["path"] == name)
    item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    write_signed_manifest(bundle, private, manifest)

    result = verify_bundle(bundle, public)
    assert not result["verified"]
    assert result["signature"] == "valid" and result["integrity"] == "match"
    assert set(result["members"].values()) == {"match"}
    assert result["replay"] == "mismatch"
    assert diagnostic in result["error"].lower()


def test_all_digest_length_and_inventory_discrepancies_are_reported(shipment):
    bundle, private, public = shipment
    manifest = read_manifest(bundle)
    rows = {row["path"]: row for row in manifest["members"]}
    rows["report.html"]["bytes"] += 1
    rows["decision.json"]["sha256"] = "a" * 64
    write_signed_manifest(bundle, private, manifest)
    (bundle / "sources" / "trial.txt").unlink()
    (bundle / "readme.txt").write_bytes(b"annotation")
    (bundle / "other").mkdir()

    result = verify_bundle(bundle, public)
    assert not result["verified"]
    assert result["signature"] == "valid" and result["integrity"] == "mismatch"
    assert result["replay"] == "not_checked"
    assert {name: value for name, value in result["members"].items() if value != "match"} == {
        "report.html": "changed", "decision.json": "changed", "sources/trial.txt": "missing",
        "readme.txt": "unexpected", "other": "unexpected",
    }
    assert result["members"]["sources/cohort.txt"] == "match"


def test_link_to_identical_source_bytes_is_not_a_regular_member(shipment, tmp_path):
    bundle, _, public = shipment
    path = bundle / "sources" / "trial.txt"
    original = path.rename(tmp_path / "original.txt")
    path.symlink_to(original)
    result = verify_bundle(bundle, public)
    assert not result["verified"]
    assert result["members"]["sources/trial.txt"] == "not_regular"
    assert result["integrity"] == "mismatch" and result["replay"] == "not_checked"


def test_length_mismatch_skips_reading_the_replacement(shipment, monkeypatch):
    from medextract_dossier import bundle_verify

    bundle, _, public = shipment
    path = bundle / "report.html"
    with path.open("ab") as stream:
        stream.write(b"additional synthetic report content")
    read_regular = bundle_verify._read_regular

    def read_matching_size(member, before=None):
        assert member != path, "an already mismatched member should not be read into memory"
        return read_regular(member, before)

    monkeypatch.setattr(bundle_verify, "_read_regular", read_matching_size)
    result = verify_bundle(bundle, public)
    assert not result["verified"] and result["members"]["report.html"] == "changed"


def test_public_key_must_be_supplied_independently(shipment):
    bundle, _, public = shipment
    internal = bundle / "copy.pem"
    internal.write_bytes(public.read_bytes())
    result = verify_bundle(bundle, internal)
    assert not result["verified"] and result["signature"] == "not_checked"
    assert "outside" in result["error"]


def test_missing_and_unrecognized_keys_fail_before_inventory(shipment, tmp_path):
    bundle, _, _ = shipment
    key = tmp_path / "recipient.pem"
    for raw in (None, b"unrecognized synthetic key format"):
        if raw is not None:
            key.write_bytes(raw)
        result = verify_bundle(bundle, key)
        assert not result["verified"] and result["signer_fingerprint"] is None
        assert result["error"] and result["members"] == {}
        assert result["integrity"] == result["replay"] == "not_checked"
