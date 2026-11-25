"""Check the reviewer's one-command workflow against the independently written fixture."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "marker-mx1"


def run_cli(*options, example=EXAMPLE):
    return subprocess.run(
        [sys.executable, "-B", "-m", "medextract_dossier", str(example), *options],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def canonical_sha256(report):
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def projection(decision):
    return {
        "status": decision["status"],
        "reason": decision["reason"],
        "eligible_families": decision["eligible_families"],
        "ledger": {r["passage_id"]: r["reason"] for r in decision["ledger"]},
    }


def test_two_questions_and_ablation_match_frozen_scientific_oracle():
    expected = json.loads((EXAMPLE / "expected.json").read_bytes())
    baseline = run_cli("--json")
    assert baseline.returncode == 0, baseline.stderr
    report = json.loads(baseline.stdout)
    assert report["evidence_origin"] == "synthetic"
    results = report["decisions"]
    assert {d["question_id"]: projection(d) for d in results} == expected["baseline"]
    for decision in results:
        for entry in decision["ledger"]:
            data = (EXAMPLE / entry["source_file"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["source_sha256"]
            assert data[entry["start"]:entry["end"]].decode("utf-8") == entry["quote"]
    removed = run_cli("--without-passage", "cohort-result", "--json")
    assert removed.returncode == 0, removed.stderr
    replay = json.loads(removed.stdout)
    assert replay["omitted_passages"] == ["cohort-result"]
    assert canonical_sha256(report) == expected["canonical_output_sha256"]["baseline"]
    assert canonical_sha256(replay) == expected["canonical_output_sha256"]["without_cohort_result"]
    by_question = {d["question_id"]: projection(d) for d in replay["decisions"]}
    assert by_question["prognosis"] == expected["without_cohort_result"]
    assert by_question["intervention"]["status"] == expected["baseline"]["intervention"]["status"]


def test_duplicate_report_through_the_normal_file_loader(tmp_path):
    example = tmp_path / "mx1"
    shutil.copytree(EXAMPLE, example)
    path = example / "casebook.json"
    book = json.loads(path.read_bytes())
    original = next(p for p in book["passages"] if p["id"] == "cohort-result")
    source = next(s for s in book["sources"] if s["id"] == original["source_id"])
    intro = "MX-1 follow-up report — same fictional participants.\n\n".encode()
    quote = original["quote"].encode()
    content = intro + quote + b"\n"
    filename = "sources/cohort-repeat.txt"
    (example / filename).write_bytes(content)
    book["sources"].append({**source, "id": "cohort-repeat", "file": filename,
                            "sha256": hashlib.sha256(content).hexdigest()})
    book["passages"].append({**original, "id": "cohort-repeat-result", "source_id": "cohort-repeat",
                             "start": len(intro), "end": len(intro) + len(quote)})
    path.write_text(json.dumps(book), encoding="utf-8")
    result = run_cli("--json", example=example)
    assert result.returncode == 0, result.stderr
    prognosis = next(d for d in json.loads(result.stdout)["decisions"] if d["question_id"] == "prognosis")
    expected = json.loads((EXAMPLE / "expected.json").read_bytes())
    assert projection(prognosis) == expected["duplicate_cohort_report"]


def test_reader_can_locate_the_passages_and_understand_the_estimates():
    result = run_cli()
    assert result.returncode == 0, result.stderr
    assert "synthetic" in result.stdout.lower()
    assert "risk ratio" in result.stdout.lower()
    assert "95% CI" in result.stdout
    for pid in ("cohort-result", "trial-result", "trial-marker", "protocol-objective"):
        assert pid in result.stdout
    assert "both benefit and harm" in result.stdout
    assert "sources/" in result.stdout and "bytes [" in result.stdout
    assert "different research question" in result.stdout
    assert "no observed result" in result.stdout


def test_default_fixture_renders_utf8_even_when_stdout_defaults_to_ascii():
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
           "PYTHONIOENCODING": "ascii"}
    for flags in ([], ["--json"]):
        proc = subprocess.run([sys.executable, "-B", "-m", "medextract_dossier", "build", str(EXAMPLE), *flags],
                              env=env, cwd=ROOT, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        text = proc.stdout.decode("utf-8")
        assert "−" in text
        if flags:
            assert json.loads(text)["evidence_origin"] == "synthetic"


def test_missing_annotation_does_not_imply_a_study_had_no_observed_result(tmp_path):
    from medextract_dossier.bundle_export import export_bundle
    from medextract_dossier.signing import generate_keypair

    example = shutil.copytree(EXAMPLE, tmp_path / "example")
    casebook = example / "casebook.json"
    book = json.loads(casebook.read_bytes())
    next(p for p in book["passages"] if p["id"] == "cohort-result")["estimate"] = None
    casebook.write_text(json.dumps(book))
    result = run_cli(example=example)
    assert result.returncode == 0
    assert "cohort-result [mx1-cohort] — No result estimate was captured in the annotation." in result.stdout
    assert "protocol-objective [mx1-protocol] — The passage contains no observed result." in result.stdout
    key = tmp_path / "key.pem"
    generate_keypair(key, tmp_path / "pub.pem")
    bundle = export_bundle(example, tmp_path / "bundle", key)
    html = (bundle / "report.html").read_text()
    assert "No result estimate was captured in the annotation." in html
    assert "No estimate captured in this annotation." in html


def test_unknown_passage_makes_the_command_fail():
    result = run_cli("--without-passage", "cohort-reslut", "--json")
    assert result.returncode != 0 and "cohort-reslut" in result.stderr
    assert result.stdout == ""


def test_printed_interval_preserves_which_side_of_one_the_limits_occupy():
    from medextract_dossier.presentation import describe_estimate
    estimate = {"measure": "risk_ratio", "unit": "ratio",
                "point": 1.000002, "low": 1.000001, "high": 1.000003}
    assert describe_estimate(estimate) == "risk ratio 1.000002 (95% CI 1.000001 to 1.000003)"
