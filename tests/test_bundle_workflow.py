"""Follow the public CLI from a producer's original to a recipient's offline copy."""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "marker-mx1"
ENVELOPE = "manifest.dsse.json"
SOURCES = {"sources/cohort.txt", "sources/protocol.txt", "sources/trial.txt"}
MEMBERS = SOURCES | {"question.json", "evidence.json", "decision.json", "report.html"}


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    # A source-only installation has no bundled fixtures, oracle or producer state.
    runtime = tmp_path_factory.mktemp("recipient-runtime")
    shutil.copytree(ROOT / "medextract_dossier", runtime / "medextract_dossier",
                    ignore=shutil.ignore_patterns("__pycache__"))

    def run(*args, c_locale=False):
        env = os.environ.copy()
        if c_locale:
            env.update(LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        return subprocess.run([sys.executable, "-B", "-m", "medextract_dossier", *args],
                              cwd=runtime, env=env, capture_output=True, timeout=30)

    return run


def successful(result):
    assert result.returncode == 0, (result.stdout, result.stderr)


def verify(cli, bundle, trusted_key, *, valid, c_locale=False):
    result = cli("verify", bundle, "--public-key", trusted_key, "--json", c_locale=c_locale)
    assert (result.returncode == 0) == valid, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["verified"] is valid
    if not valid:
        assert report["error"]
    return report


def files(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


def fingerprint(pem_path):
    public = serialization.load_pem_public_key(pem_path.read_bytes())
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.fixture(scope="module")
def handoff(tmp_path_factory, cli):
    operator = tmp_path_factory.mktemp("operator")
    example = shutil.copytree(EXAMPLE, operator / "original")
    private, public = operator / "private.pem", operator / "public.pem"
    successful(cli("keygen", "--private-key", private, "--public-key", public))
    trusted = tmp_path_factory.mktemp("trusted") / "producer.pub"
    shutil.copyfile(public, trusted)

    recipient = tmp_path_factory.mktemp("received")
    scenarios = {"baseline": [], "without_cohort_result": ["--without-passage", "cohort-result"]}
    for name, options in scenarios.items():
        bundle = operator / name
        successful(cli("export", example, "--out", bundle, "--private-key", private, *options))
        shutil.move(bundle, recipient / name)

    # Input JSON has equivalent values under CRLF. Signed outputs, including
    # the Ed25519 signature with the same key, must reproduce byte for byte.
    casebook = example / "casebook.json"
    casebook.write_bytes(casebook.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    second = operator / "second"
    successful(cli("export", example, "--out", second, "--private-key", private, c_locale=True))
    assert files(second) == files(recipient / "baseline")
    shutil.rmtree(operator)
    return recipient, trusted


@pytest.mark.parametrize("scenario", ["baseline", "without_cohort_result"])
def test_moved_bundle_replays_the_full_frozen_comparison(cli, handoff, scenario):
    recipient, trusted = handoff
    bundle = recipient / scenario
    actual = files(bundle)
    assert set(actual) == MEMBERS | {ENVELOPE}
    for source_path in SOURCES:
        assert actual[source_path] == (EXAMPLE / source_path).read_bytes()
    for data in actual.values():
        assert b"-----BEGIN PRIVATE KEY-----" not in data
        assert b"-----BEGIN PUBLIC KEY-----" not in data

    comparison = json.loads(actual["decision.json"])
    encoded = json.dumps(comparison, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    expected = json.loads((EXAMPLE / "expected.json").read_bytes())
    assert hashlib.sha256(encoded).hexdigest() == expected["canonical_output_sha256"][scenario]
    assert comparison["omitted_passages"] == ([] if scenario == "baseline" else ["cohort-result"])
    assert {d["question_id"] for d in comparison["decisions"]} == {"intervention", "prognosis"}

    report = verify(cli, bundle, trusted, valid=True)
    assert report["signature"] == "valid"
    assert report["integrity"] == report["replay"] == "match"
    assert report["members"] == dict.fromkeys(MEMBERS, "match")
    assert report["signer_fingerprint"] == fingerprint(trusted)


@pytest.mark.parametrize("member", sorted(MEMBERS))
def test_one_changed_byte_identifies_the_exact_member(tmp_path, cli, handoff, member):
    recipient, trusted = handoff
    bundle = shutil.copytree(recipient / "baseline", tmp_path / "changed")
    path = bundle / member
    data = bytearray(path.read_bytes())
    data[data.index(b"a")] = ord("A")
    path.write_bytes(data)
    report = verify(cli, bundle, trusted, valid=False)
    assert report["signature"] == "valid"
    assert report["integrity"] == "mismatch"
    assert report["replay"] == "not_checked"
    expected = dict.fromkeys(MEMBERS, "match")
    expected[member] = "changed"
    assert report["members"] == expected


def test_missing_and_extra_members_are_reported_together(tmp_path, cli, handoff):
    recipient, trusted = handoff
    bundle = shutil.copytree(recipient / "baseline", tmp_path / "partial")
    (bundle / "decision.json").unlink()
    (bundle / "sources" / "other.txt").write_bytes(b"Unlisted synthetic note.\n")
    report = verify(cli, bundle, trusted, valid=False)
    assert report["signature"] == "valid" and report["integrity"] == "mismatch"
    assert report["replay"] == "not_checked"
    expected = dict.fromkeys(MEMBERS, "match")
    expected.update({"decision.json": "missing", "sources/other.txt": "unexpected"})
    assert report["members"] == expected


def test_recipient_can_distinguish_the_pinned_key_from_the_claim(tmp_path, cli, handoff):
    recipient, trusted = handoff
    other_key = tmp_path / "different.pub"
    successful(cli("keygen", "--private-key", tmp_path / "different.pem", "--public-key", other_key))
    report = verify(cli, recipient / "baseline", other_key, valid=False)
    assert report["signature"] == "invalid"
    assert report["integrity"] == report["replay"] == "not_checked"
    assert report["supplied_key_fingerprint"] == fingerprint(other_key)
    assert report["claimed_keyid"] == fingerprint(trusted)
    assert report["signer_fingerprint"] is None


def test_edit_to_authenticated_manifest_bytes_invalidates_signature(tmp_path, cli, handoff):
    recipient, trusted = handoff
    bundle = shutil.copytree(recipient / "baseline", tmp_path / "changed-manifest")
    path = bundle / ENVELOPE
    envelope = json.loads(path.read_bytes())
    payload = base64.b64decode(envelope["payload"], validate=True)
    envelope["payload"] = base64.b64encode(payload + b" ").decode("ascii")
    path.write_text(json.dumps(envelope), encoding="utf-8")
    report = verify(cli, bundle, trusted, valid=False)
    assert report["signature"] == "invalid"
    assert report["integrity"] == report["replay"] == "not_checked"


def test_converting_a_received_source_to_crlf_is_detected(tmp_path, cli, handoff):
    recipient, trusted = handoff
    bundle = shutil.copytree(recipient / "baseline", tmp_path / "line-endings")
    path = bundle / "sources" / "trial.txt"
    raw = path.read_bytes()
    assert b"\n" in raw and b"\r" not in raw
    path.write_bytes(raw.replace(b"\n", b"\r\n"))
    report = verify(cli, bundle, trusted, valid=False)
    assert report["members"]["sources/trial.txt"] == "changed"
    assert report["signature"] == "valid" and report["replay"] == "not_checked"


def test_explicit_crlf_synthetic_source_preserves_its_byte_locators(tmp_path, cli):
    example = shutil.copytree(EXAMPLE, tmp_path / "crlf-example")
    path = example / "sources" / "trial.txt"
    raw = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(raw)
    casebook = example / "casebook.json"
    book = json.loads(casebook.read_bytes())
    trial = next(s for s in book["sources"] if s["id"] == "trial")
    trial["sha256"] = hashlib.sha256(raw).hexdigest()
    for passage in book["passages"]:
        if passage["source_id"] == "trial":
            quote = passage["quote"].encode("utf-8")
            passage["start"] = raw.index(quote)
            passage["end"] = passage["start"] + len(quote)
    casebook.write_text(json.dumps(book), encoding="utf-8")
    public, private = tmp_path / "trusted.pub", tmp_path / "private.pem"
    successful(cli("keygen", "--private-key", private, "--public-key", public))
    bundle = tmp_path / "received"
    successful(cli("export", example, "--out", bundle, "--private-key", private, c_locale=True))
    assert (bundle / trial["file"]).read_bytes() == raw
    report = verify(cli, bundle, public, valid=True, c_locale=True)
    assert report["members"][trial["file"]] == report["replay"] == "match"
