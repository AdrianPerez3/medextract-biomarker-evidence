"""The CLI keeps its checkout workflow and shares its exact data with the MCP adapter."""
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys

import pytest

from medextract_dossier.evidence import load_casebook_inputs

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/marker-mx1"


def test_cli_build_can_write_to_a_text_only_stream():
    from medextract_dossier.__main__ import main

    output = StringIO()
    with redirect_stdout(output):
        assert main(["build", "--json"]) == 0
    assert json.loads(output.getvalue())["evidence_origin"] == "synthetic"


def test_explicit_build_is_identical_to_legacy_review_and_needs_no_optional_deps():
    command = [sys.executable, "-B", "-S", "-m", "medextract_dossier"]
    args = [str(EXAMPLE), "--without-passage", "cohort-result", "--json"]
    old = subprocess.run([*command, *args], cwd=ROOT, capture_output=True, check=True)
    new = subprocess.run([*command, "build", *args], cwd=ROOT, capture_output=True)
    assert new.returncode == 0, new.stderr
    assert new.stdout == old.stdout
    assert json.loads(new.stdout)["decisions"][0]["status"] == "insufficient"


def test_mcp_help_runs_without_optional_sdk():
    result = subprocess.run([sys.executable, "-B", "-S", "-m", "medextract_dossier", "mcp", "--help"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and "--bundle" in result.stdout and "--output-dir" in result.stdout


def test_duplicate_alias_is_rejected_before_startup():
    command = [sys.executable, "-B", "-S", "-m", "medextract_dossier", "mcp",
               "--bundle", "sample=a", "--bundle", "sample=b"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and "duplicate" in result.stderr.lower()


def test_review_budget_covers_casebook_and_all_unique_source_bytes():
    actual = (EXAMPLE / "casebook.json").stat().st_size + sum(p.stat().st_size for p in (EXAMPLE / "sources").glob("*.txt"))
    assert load_casebook_inputs(EXAMPLE, max_bytes=actual) == load_casebook_inputs(EXAMPLE)
    with pytest.raises(ValueError, match="byte limit"):
        load_casebook_inputs(EXAMPLE, max_bytes=actual-1)


@pytest.mark.parametrize("problem", ["missing_key", "bad_key", "missing_bundle"])
def test_cli_verification_reports_setup_errors_before_checking_signature(tmp_path, problem):
    from medextract_dossier.bundle_export import export_bundle
    from medextract_dossier.signing import generate_keypair

    key = tmp_path / "public.pem"
    private = tmp_path / "private.pem"
    generate_keypair(private, key)
    directory = export_bundle(EXAMPLE, tmp_path / "bundle", private)
    if problem == "missing_key":
        key.unlink()
    elif problem == "bad_key":
        key.write_bytes(b"not a PEM")
    else:
        directory = tmp_path / "missing"
    proc = subprocess.run([sys.executable, "-B", "-m", "medextract_dossier", "verify", str(directory),
                           "--public-key", str(key), "--json"], cwd=ROOT, capture_output=True)
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["signature"] == receipt["integrity"] == receipt["replay"] == "not_checked"
    assert receipt["error_kind"] == "configuration" and receipt["error"]


def test_bundle_budget_preserves_ordinary_receipts_at_the_boundary(tmp_path):
    from medextract_dossier.bundle_export import export_bundle
    from medextract_dossier.bundle_verify import BundleLimitError, verify_bundle
    from medextract_dossier.signing import generate_keypair

    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    generate_keypair(private, public)
    directory = export_bundle(EXAMPLE, tmp_path / "bundle", private)
    entries = list(directory.rglob("*"))
    size = sum(p.stat().st_size for p in entries if p.is_file())
    ordinary = verify_bundle(directory, public)
    assert ordinary["verified"] and len(ordinary["manifest_sha256"]) == 64
    assert verify_bundle(directory, public, max_bytes=size, max_entries=len(entries)) == ordinary
    with pytest.raises(BundleLimitError):
        verify_bundle(directory, public, max_bytes=size-1)
    with pytest.raises(BundleLimitError):
        verify_bundle(directory, public, max_entries=len(entries)-1)
