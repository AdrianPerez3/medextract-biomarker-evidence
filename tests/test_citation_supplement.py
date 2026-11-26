"""The repository benchmark reproduces published outcomes without installing MedExtract."""
import csv
import json
from pathlib import Path
import subprocess
import sys


BENCHMARK = Path(__file__).resolve().parents[1] / "benchmarks" / "citation-supplement"


def run_score(cwd, *args):
    return subprocess.run(
        [sys.executable, "-I", str(BENCHMARK / "score.py"), "--json", *args],
        cwd=cwd, capture_output=True, text=True,
    )


def test_frozen_citation_supplement(tmp_path):
    result = run_score(tmp_path)
    assert result.returncode == 0, result.stderr
    totals = json.loads(result.stdout)
    diagnostic, intervention = totals["diagnostic"], totals["intervention"]
    assert (diagnostic["topics"], diagnostic["final_screens"], diagnostic["sixty_screens"]) == (8, 80, 480)
    assert (diagnostic["native_final"], diagnostic["assisted_final"]) == (4, 8)
    assert (diagnostic["native_sixty"], diagnostic["assisted_sixty"]) == (15, 19)
    assert (diagnostic["gained"], diagnostic["lost"], diagnostic["novel_reports"]) == (7, 3, 4)
    assert (diagnostic["native_final_unjudged"], diagnostic["assisted_final_unjudged"]) == (46, 40)
    assert (intervention["topics"], intervention["final_screens"], intervention["sixty_screens"]) == (2, 20, 120)
    assert (intervention["native_final"], intervention["assisted_final"]) == (0, 0)
    assert (intervention["native_sixty"], intervention["assisted_sixty"]) == (1, 1)
    assert (intervention["native_final_unjudged"], intervention["assisted_final_unjudged"]) == (20, 20)


def test_duplicate_observation_is_rejected(tmp_path):
    records = (BENCHMARK / "observations.tsv").read_text(encoding="utf-8").splitlines()
    duplicate = tmp_path / "duplicate.tsv"
    duplicate.write_text("\n".join([*records, records[1]]) + "\n", encoding="utf-8")
    result = run_score(tmp_path, "--input", str(duplicate))
    assert result.returncode == 2
    assert "Duplicate topic-PMID" in result.stderr
    assert result.stdout == ""


def test_final_ten_credit_only_new_families(tmp_path):
    path = tmp_path / "family-overlap.tsv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(("cohort", "topic", "pmid", "prefix_rank", "shared_rank", "native_tail_rank",
                         "citation_rank", "judgment", "family", "primary", "in_native_capture"))

        def add(pmid, prefix="", shared="", native="", citation="", family="", captured=1):
            writer.writerow(("intervention", "fixture", pmid, prefix, shared, native, citation,
                             "include" if family else "unjudged", family, 0, captured))

        add(1, prefix=1, family=1)
        for rank in range(1, 51):
            add(1 + rank, shared=rank, family=2 if rank == 1 else "")
        add(52, native=1, family=1)
        add(53, native=2, family=2)
        add(54, native=3, family=3)
        add(55, citation=1, family=4, captured=0)
        add(56, citation=2, family=4, captured=0)
        add(57, citation=3, family=5, captured=0)

    result = run_score(tmp_path, "--input", str(path))
    assert result.returncode == 0, result.stderr
    trial = json.loads(result.stdout)["intervention"]
    assert (trial["native_final"], trial["assisted_final"]) == (1, 2)
    assert (trial["native_sixty"], trial["assisted_sixty"]) == (2, 3)
    assert (trial["gained"], trial["lost"], trial["novel_reports"]) == (2, 1, 3)
