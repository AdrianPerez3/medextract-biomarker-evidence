import csv
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest

from medextract_dossier.discovery import DiscoveryRequest, Hit, Lane, build_snapshot


def request(**overrides):
    return DiscoveryRequest(
        **({"question": "Which papers?", "concept_groups": [["cystatin C"], ["primary care"]]} | overrides)
    )


def ids(provider, values, branch="broad"):
    return Lane(
        provider,
        branch,
        "ok",
        len(values),
        len(values),
        tuple((i, Hit(pmid=value)) for i, value in enumerate(values, 1)),
    )


def test_ten_row_preview_can_reach_native_six():
    lanes = [ids("pubmed", [str(i) for i in range(1, 11)]), ids("epmc", [str(i) for i in range(11, 21)])]
    snap = build_snapshot(request(), lanes)
    assert "6" not in [row["pmid"] for row in snap.summary()["preview"]]
    assert snap.page(offset=10)["rows"][0]["pmid"] == "6"
    assert snap.page(view="pubmed_broad", offset=5)["rows"][0]["pubmed_broad_rank"] == 6
    exported = list(csv.DictReader(io.StringIO(snap.tsv), delimiter="\t"))
    assert [r["pmid"] for r in exported].count("6") == 1
    assert [r["pmid"] for r in exported] == [r["pmid"] for o in (0, 10) for r in snap.page(o)["rows"]]
    manifest = json.loads(snap.manifest)
    assert manifest["candidates_sha256"] == hashlib.sha256(snap.tsv.encode()).hexdigest()
    assert snap.page(20)["next_offset"] is None and snap.page(20)["rows"] == []
    assert snap.page(view="epmc_anchored")["status"] == "not_requested"
    assert snap.page(view="epmc_broad")["view_count"] == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"depth": True},
        {"depth": 101},
        {"until_year": 1799},
        {"until_year": datetime.now(timezone.utc).year + 1},
        {"include_title_search": 1},
        {"question": "hello\n"},
        {"profile": "other"},
        {"concept_groups": [["one"]]},
        {"concept_groups": [["A"], ["B*"]]},
        {"concept_groups": [["A"], ["B[title]"]]},
        {"concept_groups": [["A"], ['B"']]},
        {"concept_groups": [["A"], [" TITLE:term"]]},
        {"concept_groups": [["A"], ["\\"]]},
        {"concept_groups": [["α" * 96] * 8] * 6},
    ],
)
def test_invalid_input(kwargs):
    with pytest.raises(ValueError):
        request(**kwargs)


def test_provider_expressions_dates_and_unicode():
    req = request(
        concept_groups=[["α-test (A)", "D-dimer"], ["Crohn's"]], include_title_search=True, until_year=2016
    )
    assert req.params("pubmed", "broad")["term"] == """("α-test (A)" OR "D-dimer") AND ("Crohn's")"""
    assert '"α-test (A)"[Title]' in req.params("pubmed", "anchored")["term"]
    assert req.params("pubmed", "broad")["maxdate"] == "2016"
    assert 'TITLE_ABS:"Crohn' in req.params("epmc", "anchored")["query"]
    assert req.params("epmc", "broad")["query"].endswith("SRC:MED AND FIRST_PDATE:[1800-01-01 TO 2016-12-31]")
    assert "SRC:MED" not in request(profile="all_indexed").params("epmc", "broad")["query"]


def test_page_is_immutable_from_the_caller_and_rejects_invalid_inputs():
    snap = build_snapshot(request(), [ids("pubmed", ["9"]), ids("epmc", [])])
    snap.page()["rows"][0]["pmid"] = "99"
    assert snap.page()["rows"][0]["pmid"] == "9"
    for bad in ({"offset": -1}, {"offset": 2}, {"limit": 21}, {"limit": True}, {"view": "absent"}):
        with pytest.raises(ValueError):
            snap.page(**bad)


def test_anchored_duplicate_rows_are_retained_once_with_all_ranks():
    snap = build_snapshot(
        request(include_title_search=True),
        [
            ids("pubmed", ["7"]),
            ids("epmc", ["8"]),
            ids("pubmed", ["8"], "anchored"),
            ids("epmc", ["6", "7"], "anchored"),
        ],
    )
    assert [r["pmid"] for r in snap.page()["rows"]] == ["7", "8", "6"]
    assert snap.page()["rows"][0]["epmc_anchored_rank"] == 2
    assert snap.page()["rows"][1]["pubmed_anchored_rank"] == 1


def test_export_size_limit_is_enforced_on_both_artifacts(monkeypatch):
    from medextract_dossier import discovery

    monkeypatch.setattr(discovery, "MAX_SNAPSHOT_BYTES", 100)
    with pytest.raises(ValueError, match="2 MiB"):
        build_snapshot(request(), [ids("pubmed", []), ids("epmc", [])])


def test_wide_donor_claims_do_not_displace_a_valid_screening_export(monkeypatch):
    from medextract_dossier import discovery

    req = request(profile="all_indexed")
    lanes = [ids("pubmed", ["7"]), ids("epmc", ["8"])]
    full = build_snapshot(req, lanes)
    compact = json.loads(full.manifest)
    assert compact.pop("native_hits")["epmc_broad"] == [{"pmid": "8", "rank": 1}]
    # Replacing the digest does not change its length.
    manifest_bytes = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode()) + 1
    monkeypatch.setattr(discovery, "MAX_SNAPSHOT_BYTES", len(full.tsv.encode()) + manifest_bytes)
    screened = build_snapshot(req, lanes)
    assert "native_hits" not in json.loads(screened.manifest)
    assert screened.tsv == full.tsv and screened.search_id != full.search_id
    assert json.loads(screened.manifest)["lanes"] == compact["lanes"]
