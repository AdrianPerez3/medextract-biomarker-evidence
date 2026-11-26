"""Offline reuse of complete recorded search pages for timed-out discovery lanes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from medextract_dossier import discovery_providers, discovery_recovery, literature_http
from medextract_dossier.discovery import DiscoveryRequest
from medextract_dossier.discovery_recovery import RecoveryError


START = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
REQUEST = DiscoveryRequest("Local question", [["marker"], ["care"]], depth=5)


def pubmed(*ids):
    return {"esearchresult": {"count": len(ids), "idlist": list(ids)}}


def epmc(*ids):
    return {"hitCount": len(ids), "resultList": {"result": [{"source": "MED", "id": uid} for uid in ids]}}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Local reuse attempted a network request")

    monkeypatch.setattr(literature_http, "fetch_bytes", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


@pytest.fixture
def capture(monkeypatch):
    def record(pages, *, when=START, request=REQUEST, metadata=None):
        responses = {request.url(*key): value for key, value in pages.items()}

        def fetch(url):
            payload = metadata if "esummary.fcgi?" in url else responses[url]
            if isinstance(payload, Exception):
                raise payload
            assert payload is not None, f"No fixture for {url}"
            return json.dumps(payload).encode()

        with monkeypatch.context() as patch:
            patch.setattr(discovery_providers, "_utc", lambda: when.isoformat(timespec="microseconds"))
            return discovery_providers.collect_discovery(request, fetch=fetch)

    return record


def broad(pubmed_page, epmc_page):
    return {("pubmed", "broad"): pubmed_page, ("epmc", "broad"): epmc_page}


def reuse(parent, donor, gap=60):
    return discovery_recovery.reuse_discovery(
        parent.manifest, parent.tsv, donor.manifest, donor.tsv, max_gap_seconds=gap
    )


def resave(snapshot, manifest):
    manifest.pop("search_id")
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    manifest["search_id"] = sha256(canonical).hexdigest()
    return replace(snapshot, search_id=manifest["search_id"], manifest=json.dumps(manifest))


@pytest.mark.parametrize("offset", [-60, 60])
def test_absolute_gap_boundary_is_inclusive_and_the_merged_export_is_deterministic(capture, offset):
    parent = capture(broad(pubmed("7", "5"), TimeoutError()))
    donor = capture(
        broad(pubmed("99"), epmc("5", "9")),
        when=START + timedelta(seconds=offset),
        request=replace(REQUEST, question="Different local wording"),
    )
    child = reuse(parent, donor)
    again = reuse(parent, donor)

    assert (again.search_id, again.manifest, again.tsv) == (child.search_id, child.manifest, child.tsv)
    assert child.search_id not in (parent.search_id, donor.search_id)
    assert [(r["pmid"], r["pubmed_broad_rank"], r["epmc_broad_rank"]) for r in child.page()["rows"]] == [
        ("7", 1, None), ("5", 2, 1), ("9", None, 2),
    ]
    meta = json.loads(child.manifest)
    parent_lanes = json.loads(parent.manifest)["lanes"]
    donor_lane = json.loads(donor.manifest)["lanes"][1]
    assert meta["lanes"] == [parent_lanes[0], donor_lane, *parent_lanes[2:]]
    assert donor_lane["response_sha256"] == sha256(json.dumps(epmc("5", "9")).encode()).hexdigest()
    assert donor_lane["captured_at"] == (START + timedelta(seconds=offset)).isoformat(timespec="microseconds")
    assert meta["candidates_sha256"] == sha256(child.tsv.encode()).hexdigest()
    assert meta["question"] == REQUEST.question and meta["status"] == "ok" and "recovery" not in meta
    info = meta["reuse"]
    assert info["parent_search_id"] == parent.search_id
    assert info["donor_search_id"] == donor.search_id
    assert info["max_gap_seconds"] == 60 and info["request_count"] == 0
    assert info["reused_lanes"] == ["epmc_broad"]
    assert (info["candidate_count_before"], info["candidate_count_after"], info["candidate_count_added"]) == (2, 3, 1)


def test_zero_hit_success_can_fill_a_timeout_at_a_zero_gap(capture):
    parent = capture(broad(pubmed("7"), TimeoutError()))
    donor = capture(broad(pubmed("99"), epmc()))
    child = reuse(parent, donor, gap=0)
    meta = json.loads(child.manifest)
    assert child.tsv == parent.tsv
    assert child.page(view="epmc_broad")["rows"] == []
    assert child.page(view="epmc_broad")["status"] == "ok"
    assert meta["lanes"][1] == json.loads(donor.manifest)["lanes"][1]
    assert meta["reuse"]["candidate_count_added"] == 0
    assert meta["reuse"]["reused_lanes"] == ["epmc_broad"]


@pytest.mark.parametrize("offset", [-60_000_001, 60_000_001])
def test_donor_just_outside_the_gap_is_rejected(capture, offset):
    parent = capture(broad(pubmed("7"), TimeoutError()))
    donor = capture(broad(pubmed(), epmc("9")), when=START + timedelta(microseconds=offset))
    with pytest.raises(RecoveryError):
        reuse(parent, donor)


@pytest.mark.parametrize("gap", [-1, True, 2.5, "60", None])
def test_gap_must_be_an_explicit_nonnegative_integer(capture, gap):
    parent = capture(broad(pubmed("7"), TimeoutError()))
    donor = capture(broad(pubmed(), epmc("9")))
    with pytest.raises(RecoveryError):
        reuse(parent, donor, gap=gap)


@pytest.mark.parametrize(
    "different",
    [
        {"concept_groups": [["marker"], ["other"]]},
        {"until_year": 2020},
        {"depth": 4},
        {"profile": "all_indexed"},
    ],
    ids=["query", "year", "depth", "profile"],
)
def test_pubmed_page_requires_the_same_profile_and_all_request_parameters(capture, different):
    parent = capture(broad(TimeoutError(), epmc("7")))
    donor = capture(
        broad(pubmed("9"), epmc()),
        request=replace(REQUEST, **different),
        metadata={"result": {"9": {"uid": "9", "articleids": []}}},
    )
    with pytest.raises(RecoveryError):
        reuse(parent, donor)


@pytest.mark.parametrize("state", ["empty", "network", "partial"])
def test_only_an_unavailable_timeout_can_be_replaced(capture, state):
    pages = {"empty": epmc(), "network": OSError(), "partial": epmc("2", "2")}
    parent = capture(broad(pubmed("7"), pages[state]))
    donor = capture(broad(pubmed("7"), epmc("9")))
    with pytest.raises(RecoveryError):
        reuse(parent, donor)


@pytest.mark.parametrize("donor_page", [epmc("9", "9"), TimeoutError(), OSError()])
def test_partial_and_unavailable_donor_pages_cannot_fill_a_timeout(capture, donor_page):
    parent = capture(broad(pubmed("7"), TimeoutError()))
    donor = capture(broad(pubmed(), donor_page))
    with pytest.raises(RecoveryError):
        reuse(parent, donor)


def test_eligibility_is_per_requested_lane_and_the_remaining_timeout_can_be_filled_later(capture):
    both = replace(REQUEST, include_title_search=True)
    parent = capture(
        broad(pubmed("1"), TimeoutError())
        | {("pubmed", "anchored"): TimeoutError(), ("epmc", "anchored"): epmc("4")},
        request=both,
    )
    broad_only = capture(broad(pubmed("99"), epmc("2")))
    middle = reuse(parent, broad_only)
    intermediate = json.loads(middle.manifest)
    assert intermediate["status"] == "partial" and intermediate["reuse"]["reused_lanes"] == ["epmc_broad"]
    assert intermediate["lanes"][2] == json.loads(parent.manifest)["lanes"][2]
    assert middle.page(view="pubmed_anchored")["rows"] == []
    with pytest.raises(RecoveryError):
        reuse(middle, broad_only)

    anchored = capture(
        broad(pubmed("88"), epmc("77")) | {("pubmed", "anchored"): pubmed("3"), ("epmc", "anchored"): epmc("66")},
        request=both,
    )
    child = reuse(middle, anchored)
    meta = json.loads(child.manifest)
    assert meta["reuse"]["parent_search_id"] == middle.search_id
    assert meta["reuse"]["donor_search_id"] == anchored.search_id
    assert meta["reuse"]["reused_lanes"] == ["pubmed_anchored"]
    assert meta["status"] == "ok"
    assert [row["pmid"] for row in child.page()["rows"]] == ["1", "2", "3", "4"]
    assert meta["lanes"] == [
        intermediate["lanes"][0], intermediate["lanes"][1],
        json.loads(anchored.manifest)["lanes"][2], intermediate["lanes"][3],
    ]


@pytest.mark.parametrize("who", ["parent", "donor"])
@pytest.mark.parametrize("changed", ["manifest", "tsv", "rehashed_ranks"])
def test_both_exports_have_their_digests_and_candidate_ranks_checked(capture, who, changed):
    exports = {
        "parent": capture(broad(pubmed("7"), TimeoutError())),
        "donor": capture(broad(pubmed("7"), epmc("7"))),
    }
    export = exports[who]
    if changed == "manifest":
        export = replace(export, manifest=export.manifest.replace("Local question", "Changed"))
    elif changed == "tsv":
        export = replace(export, tsv=export.tsv + "\n")
    else:
        tsv = export.tsv.replace("/7/\t1", "/7/\t2")
        assert tsv != export.tsv
        meta = json.loads(export.manifest)
        meta["candidates_sha256"] = sha256(tsv.encode()).hexdigest()
        export = resave(replace(export, tsv=tsv), meta)
    exports[who] = export
    with pytest.raises(RecoveryError):
        reuse(exports["parent"], exports["donor"])


@pytest.mark.parametrize("missing", ["parent_capture", "donor_capture", "donor_response"])
def test_reuse_requires_original_timestamps_and_a_hashed_provider_response(capture, missing):
    parent = capture(broad(pubmed("7"), TimeoutError()))
    donor = capture(broad(pubmed(), epmc("9")))
    if missing == "parent_capture":
        meta = json.loads(parent.manifest)
        meta["lanes"][1]["captured_at"] = None
        parent = resave(parent, meta)
    else:
        meta = json.loads(donor.manifest)
        if missing == "donor_capture":
            meta["lanes"][1]["captured_at"] = None
        else:
            meta["lanes"][1].update(response_bytes=None, response_sha256=None)
        donor = resave(donor, meta)
    with pytest.raises(RecoveryError):
        reuse(parent, donor)


def test_uses_each_lanes_own_capture_time_and_records_only_eligible_lanes(capture):
    parent = capture(broad(TimeoutError(), TimeoutError()))
    donor = capture(broad(pubmed("2"), epmc("3")))
    data = json.loads(parent.manifest)
    data["lanes"][0]["captured_at"] = (START + timedelta(minutes=20)).isoformat()
    parent = resave(parent, data)
    child = reuse(parent, donor, gap=120)
    merged = json.loads(child.manifest)
    assert merged["reuse"]["reused_lanes"] == ["epmc_broad"]
    assert merged["lanes"][0] == data["lanes"][0]
    assert [r["pmid"] for r in child.page()["rows"]] == ["3"]
    both = reuse(parent, donor, gap=1200)
    assert json.loads(both.manifest)["reuse"]["reused_lanes"] == ["pubmed_broad", "epmc_broad"]
    assert [r["pmid"] for r in both.page()["rows"]] == ["2", "3"]


def summaries(doi_by_pmid):
    return {"result": {
        uid: {"uid": uid, "articleids": [{"idtype": "doi", "value": doi}]}
        for uid, doi in doi_by_pmid.items()
    }}


def test_wide_epmc_donor_supplies_native_occurrences_from_a_complete_export(capture):
    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(
        broad(pubmed("10"), TimeoutError()), request=wide, metadata=summaries({"10": "10.1000/shared"})
    )
    donor = capture(
        broad(pubmed("99"), {"hitCount": 3, "resultList": {"result": [
            {"doi": "10.1000/shared"},
            {"source": "PPR", "id": "PPR12"},
            {"title": "unidentified work"},
        ]}}),
        request=wide,
        metadata=summaries({"99": "10.1000/unrelated"}),
    )
    original, supplied = json.loads(parent.manifest), json.loads(donor.manifest)
    assert supplied["status"] == "ok" and len(supplied["native_hits"]["epmc_broad"]) == 3

    child = reuse(parent, donor)
    merged = json.loads(child.manifest)
    assert merged["metadata_requests"] == original["metadata_requests"]
    assert merged["reuse"]["donor_search_id"] == donor.search_id
    assert merged["lanes"][0] == original["lanes"][0] and merged["lanes"][1] == supplied["lanes"][1]
    assert merged["native_hits"]["epmc_broad"] == supplied["native_hits"]["epmc_broad"]
    rows = child.page()["rows"]
    assert [(r["pmid"], r["doi"], r["native_ids"], r["identity_status"]) for r in rows] == [
        ("10", "10.1000/shared", ["MED:10"], "resolved"),
        (None, None, ["PPR:PPR12"], "resolved"),
        (None, None, [], "unresolved"),
    ]
    assert rows[0]["native_positions"] == {"pubmed_broad": [1], "epmc_broad": [1]}
    assert merged["reuse"]["candidate_count_added"] == 2


def test_wide_pubmed_donation_keeps_hydrated_claims_and_links_to_the_donors_mixed_metadata_batch(capture):
    wide = replace(REQUEST, profile="all_indexed", include_title_search=True)
    doi_page = {"hitCount": 1, "resultList": {"result": [{"doi": "10.1000/shared"}]}}
    parent = capture(
        broad(TimeoutError(), doi_page)
        | {("pubmed", "anchored"): pubmed("51"), ("epmc", "anchored"): epmc()},
        request=wide,
        metadata=summaries({"51": "10.1000/retained"}),
    )
    donor = capture(
        broad(pubmed("20"), epmc())
        | {("pubmed", "anchored"): pubmed("90"), ("epmc", "anchored"): epmc()},
        request=wide,
        metadata=summaries({"20": "10.1000/shared", "90": "10.1000/unrelated"}),
    )
    original, supplied = json.loads(parent.manifest), json.loads(donor.manifest)
    assert supplied["metadata_requests"][0]["pmids"] == ["20", "90"]
    assert original["metadata_requests"][0]["pmids"] == ["51"]

    child = reuse(parent, donor)
    merged = json.loads(child.manifest)
    assert merged["metadata_requests"] == original["metadata_requests"]
    assert merged["reuse"]["donor_search_id"] == donor.search_id
    assert merged["reuse"]["reused_lanes"] == ["pubmed_broad"]
    assert merged["reuse"]["candidate_count_added"] == 0
    assert merged["lanes"][0] == supplied["lanes"][0] and merged["lanes"][0]["metadata_status"] == "ok"
    assert merged["lanes"][1:] == original["lanes"][1:]
    assert merged["native_hits"]["pubmed_broad"] == supplied["native_hits"]["pubmed_broad"]
    assert [(r["pmid"], r["doi"], r["native_positions"]) for r in child.page()["rows"]] == [
        ("20", "10.1000/shared", {"pubmed_broad": [1], "epmc_broad": [1]}),
        ("51", "10.1000/retained", {"pubmed_anchored": [1]}),
    ]


@pytest.mark.parametrize("legacy", ["parent", "donor"])
def test_wide_exports_that_lack_original_native_claims_are_rejected(capture, legacy):
    wide = replace(REQUEST, profile="all_indexed")
    exports = {
        "parent": capture(broad(pubmed(), TimeoutError()), request=wide),
        "donor": capture(broad(pubmed(), epmc("9")), request=wide),
    }
    data = json.loads(exports[legacy].manifest)
    data.pop("native_hits", None)
    exports[legacy] = resave(exports[legacy], data)
    with pytest.raises(RecoveryError, match="native"):
        reuse(exports["parent"], exports["donor"])
