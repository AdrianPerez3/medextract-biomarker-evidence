"""Automatic recovery compares independent saved native pages before any fresh search."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest
import test_discovery_reuse

from medextract_dossier import discovery_recovery
from medextract_dossier.discovery_recovery import RecoveryError
from test_discovery_reuse import REQUEST, START, broad, epmc, pubmed, resave, summaries

capture = test_discovery_reuse.capture


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network request")

    monkeypatch.setattr("socket.socket.connect", forbidden)


def auto(parent, donors=(), gap=60, **options):
    return discovery_recovery.auto_recover_discovery(
        parent.manifest, parent.tsv, ((d.manifest, d.tsv) for d in donors),
        max_gap_seconds=gap, **options,
    )


def details(child):
    receipt = child.summary()["automatic_recovery"]
    assert receipt == json.loads(child.manifest)["automatic_recovery"]
    return receipt


def test_selects_nearest_agreeing_export_per_lane_once_independent_of_order(capture):
    parent = capture(broad(TimeoutError(), TimeoutError()))
    far = capture(broad(pubmed("1"), epmc("2")), when=START - timedelta(seconds=60))
    near = capture(broad(pubmed("1"), TimeoutError()), when=START + timedelta(seconds=2))
    tie = capture(broad(pubmed("1"), TimeoutError()), when=START - timedelta(seconds=2))
    other = capture(broad(pubmed("99"), epmc()), request=replace(REQUEST, profile="all_indexed"),
                    metadata=summaries({"99": "10.1000/else"}))
    donors = [parent, far, near, tie, near, other]
    child = auto(parent, donors)
    assert auto(parent, reversed(donors)).manifest == child.manifest
    meta = details(child)
    assert meta == {
        "parent_search_id": parent.search_id, "max_gap_seconds": 60, "lookup_at": None,
        "fetch_missing": False, "donors_considered": 4,
        "lanes": [
            {"lane": "pubmed_broad", "match": "selected", "action": "reused",
             "eligible_donor_count": 3, "donor_search_id": min(near.search_id, tie.search_id)},
            {"lane": "epmc_broad", "match": "selected", "action": "reused",
             "eligible_donor_count": 1, "donor_search_id": far.search_id},
        ],
        "candidate_count_before": 0, "candidate_count_after": 2, "candidate_count_added": 2,
        "request_count": 0,
    }
    assert [r["pmid"] for r in child.page()["rows"]] == ["1", "2"]
    assert details(auto(parent, donors, fetch_missing=True))["request_count"] == 0


@pytest.mark.parametrize("different", ["membership", "rank", "total"])
def test_disagreeing_complete_pages_abstain_even_if_one_is_closer(capture, different):
    req = replace(REQUEST, depth=2)
    parent = capture(broad(pubmed("5"), TimeoutError()), request=req)
    first = capture(broad(pubmed(), epmc("7", "9")), request=req)
    changed = {"membership": epmc("7", "2"), "rank": epmc("9", "7"),
               "total": epmc("7", "9") | {"hitCount": 3}}[different]
    second = capture(broad(pubmed(), changed), request=req, when=START + timedelta(seconds=4))
    child = auto(parent, [first, second])
    assert child.tsv == parent.tsv and child.search_id != parent.search_id
    assert json.loads(child.manifest)["lanes"] == json.loads(parent.manifest)["lanes"]
    assert details(child)["lanes"] == [
        {"lane": "epmc_broad", "match": "ambiguous", "action": "retained",
         "eligible_donor_count": 2, "donor_search_id": None}
    ]
    assert auto(parent, [second, first]).search_id == child.search_id


def test_fetches_only_missing_and_ambiguous_lanes_and_can_continue_after_timeout(capture):
    req = replace(REQUEST, include_title_search=True)
    parent = capture(broad(TimeoutError(), TimeoutError()) |
                     {("pubmed", "anchored"): TimeoutError(), ("epmc", "anchored"): epmc("8")}, request=req)
    first = capture(broad(pubmed("5"), epmc("4")))
    second = capture(broad(pubmed("5"), epmc("6")))
    calls = []

    def fetch(url):
        calls.append(url)
        if url == req.url("pubmed", "anchored"):
            raise TimeoutError()
        assert url == req.url("epmc", "broad")
        return json.dumps(epmc("3")).encode()

    child = auto(parent, [first, second], fetch_missing=True, fetch=fetch)
    assert sorted(calls) == sorted([req.url("pubmed", "anchored"), req.url("epmc", "broad")])
    meta = details(child)
    assert meta["request_count"] == 2
    assert [(r["match"], r["action"]) for r in meta["lanes"]] == [
        ("selected", "reused"), ("ambiguous", "retried"), ("missing", "retried")
    ]
    assert json.loads(child.manifest)["lanes"][3] == json.loads(parent.manifest)["lanes"][3]
    final = auto(child)
    assert details(final)["parent_search_id"] == child.search_id
    assert [r["lane"] for r in details(final)["lanes"]] == ["pubmed_anchored"]
    assert final.tsv == child.tsv


def test_legacy_wide_donor_is_skipped_only_after_pair_validation(capture):
    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(pubmed(), TimeoutError()), request=wide)
    source = capture(broad(pubmed(), epmc("7")), request=wide)
    meta = json.loads(source.manifest)
    del meta["native_hits"]
    legacy = resave(source, meta)
    assert details(auto(parent, [legacy]))["lanes"][0]["match"] == "missing"
    for invalid in [replace(legacy, tsv=legacy.tsv + "\n"),
                    replace(legacy, manifest=legacy.manifest.replace("Local question", "changed"))]:
        with pytest.raises(RecoveryError):
            auto(parent, [source, invalid], fetch_missing=True)
    malformed = json.loads(legacy.manifest)
    malformed["lanes"][1]["status"] = "impossible"
    with pytest.raises(RecoveryError):
        auto(parent, [resave(legacy, malformed)], fetch_missing=True)


@pytest.mark.parametrize("field", ["rank", "doi", "source", "metadata_conflict"])
def test_distinguishes_ordered_wide_native_claims_including_summary_conflicts(capture, field):
    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(TimeoutError(), TimeoutError()), request=wide)
    hits = [{"source": "PPR", "id": "PPR42", "doi": "10.1000/a"}, {"doi": "10.1000/b"}]
    values = [{"idtype": "doi", "value": "10.1000/a"}, {"idtype": "doi", "value": "10.1000/b"}]

    def donor(items, ids):
        return capture(broad(pubmed("7"), {"hitCount": 2, "resultList": {"result": items}}),
                       request=wide, metadata={"result": {"7": {"uid": "7", "articleids": ids}}})

    first = donor(hits, values)
    if field == "rank":
        hits = list(reversed(hits))
    if field == "doi":
        hits[0] = hits[0] | {"doi": "10.1000/c"}
    if field == "source":
        hits[0] = hits[0] | {"id": "PPR43"}
    if field == "metadata_conflict":
        values[0] = values[0] | {"value": "10.1000/c"}
    second = donor(hits, values)
    lanes = details(auto(parent, [first, second]))["lanes"]
    assert [r["match"] for r in lanes] == (
        ["ambiguous", "selected"] if field == "metadata_conflict" else ["selected", "ambiguous"]
    )


def test_fresh_wide_metadata_is_counted_and_retained_for_offline_followup(capture):
    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(TimeoutError(), TimeoutError()), request=wide)
    calls = []

    def fetch(url):
        calls.append(url)
        if "ebi.ac.uk" in url:
            raise TimeoutError()
        data = summaries({"7": "10.1000/a"}) if "esummary" in url else pubmed("7")
        return json.dumps(data).encode()

    child = auto(parent, fetch_missing=True, fetch=fetch)
    assert len(calls) == details(child)["request_count"] == 3
    assert child.page()["rows"][0]["doi"] == "10.1000/a"
    donor = capture(broad(pubmed(), epmc("9")), request=wide)
    # The fresh timeout carries today's capture time; use a fixed lookup-time anchor.
    final = auto(child, [donor], lookup_at=START.isoformat())
    assert json.loads(final.manifest)["metadata_requests"] == json.loads(child.manifest)["metadata_requests"]
    assert details(final)["lanes"][0]["donor_search_id"] == donor.search_id
    assert final.page()["status"] == "ok"


def test_fixed_lookup_time_matches_only_earlier_donor_inside_inclusive_window(capture):
    parent = capture(broad(pubmed("3"), TimeoutError()), when=START - timedelta(hours=2))
    edge = capture(broad(pubmed(), epmc("7")), when=START - timedelta(seconds=60))
    future = capture(broad(pubmed(), epmc("9")), when=START + timedelta(microseconds=1))
    old = capture(broad(pubmed(), epmc("8")), when=START - timedelta(seconds=60, microseconds=1))
    donors = [edge, future, old]
    assert details(auto(parent, donors))["lanes"][0]["match"] == "missing"
    child = auto(parent, donors, lookup_at="2026-06-01T14:00:00+02:00")
    assert details(child)["lanes"][0]["donor_search_id"] == edge.search_id
    assert details(child)["lookup_at"] == "2026-06-01T12:00:00.000000+00:00"
    assert auto(parent, donors, lookup_at=START.isoformat()).manifest == child.manifest


@pytest.mark.parametrize("options", [
    {"gap": True}, {"gap": -1}, {"fetch_missing": "yes"}, {"lookup_at": "PRIVATE"},
    {"lookup_at": "2026-06-01T12:00:00"}, {"lookup_at": 42},
    {"lookup_at": START.isoformat(), "fetch_missing": True},
])
def test_invalid_options_fail_before_fetch_without_echoing_input(capture, options):
    parent = capture(broad(pubmed(), TimeoutError()))
    with pytest.raises(RecoveryError) as failure:
        auto(parent, **options)
    assert "PRIVATE" not in str(failure.value)


def test_now_freezes_at_invocation_and_matches_zero_hit_response_from_prior_minute(capture, monkeypatch):
    from datetime import datetime, timezone

    parent = capture(broad(pubmed("4"), TimeoutError()), when=START - timedelta(hours=3))
    earlier = capture(broad(pubmed(), epmc()), when=START - timedelta(seconds=60))
    later = capture(broad(pubmed(), epmc("3")), when=START + timedelta(microseconds=1))
    clock = [START]

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz == timezone.utc
            return clock[0]

    monkeypatch.setattr(discovery_recovery, "datetime", Clock)

    def crawl():
        clock[0] += timedelta(minutes=1)
        yield earlier
        yield later

    child = auto(parent, crawl(), lookup_at="now", fetch_missing=True)
    assert details(child)["lanes"][0]["donor_search_id"] == earlier.search_id
    assert details(child)["lookup_at"] == START.isoformat(timespec="microseconds")
    assert details(child)["request_count"] == 0
    assert child.page(view="epmc_broad")["status"] == "ok" and child.tsv == parent.tsv


def test_asof_does_not_borrow_pubmed_summary_that_arrived_after_the_lookup(capture):
    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(TimeoutError(), epmc()), request=wide)
    donor = capture(broad(pubmed("7"), epmc()), request=wide, metadata=summaries({"7": "10.1000/abc"}))
    data = json.loads(donor.manifest)
    data["metadata_requests"][0]["captured_at"] = (START + timedelta(microseconds=1)).isoformat()
    late = resave(donor, data)
    assert details(auto(parent, [late], lookup_at=START.isoformat()))["lanes"][0]["match"] == "missing"
    assert details(auto(parent, [donor], lookup_at=START.isoformat()))["lanes"][0]["match"] == "selected"


def test_asof_checks_the_metadata_for_the_actual_pubmed_branch(capture):
    wide = replace(REQUEST, profile="all_indexed", include_title_search=True)
    anchored = {("pubmed", "anchored"): pubmed("7"), ("epmc", "anchored"): epmc()}
    parent = capture(broad(TimeoutError(), epmc()) | anchored, request=wide,
                     metadata=summaries({"7": "10.1000/older"}))
    donor = capture(broad(pubmed("7"), epmc()) | anchored, request=wide,
                    metadata=summaries({"7": "10.1000/later"}))
    info = json.loads(donor.manifest)
    info["metadata_requests"][0]["captured_at"] = (START + timedelta(seconds=2)).isoformat()
    late_donor = resave(donor, info)
    # The inherited broad lane's ESummary came from late_donor. The intermediate's
    # local ESummary also lists PMID 7, but it was captured for its anchored lane.
    intermediate = auto(parent, [late_donor])
    assert json.loads(intermediate.manifest)["metadata_requests"] == json.loads(parent.manifest)["metadata_requests"]
    assert details(auto(parent, [intermediate], lookup_at=START.isoformat()))["lanes"][0]["match"] == "missing"


def test_legacy_donor_rejects_a_rehashed_wrong_url_before_network(capture):
    from hashlib import sha256

    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(pubmed(), TimeoutError()), request=wide)
    donor = capture(broad(pubmed(), epmc("7")), request=wide)
    info = json.loads(donor.manifest)
    del info["native_hits"]
    tsv = donor.tsv.replace("https://pubmed.ncbi.nlm.nih.gov/7/", "https://example.invalid/garbage")
    assert tsv != donor.tsv
    info["candidates_sha256"] = sha256(tsv.encode()).hexdigest()
    malformed = resave(replace(donor, tsv=tsv), info)
    with pytest.raises(RecoveryError):
        auto(parent, [malformed], fetch_missing=True, fetch=lambda url: pytest.fail("sent a request"))


@pytest.mark.parametrize("bad_lane", ["single", "both_branches"])
def test_original_wide_hydration_must_cover_every_successful_branch_before_dispatch(capture, bad_lane):
    both = bad_lane == "both_branches"
    wide = replace(REQUEST, profile="all_indexed", include_title_search=both)
    anchored = {("pubmed", "anchored"): pubmed("8"), ("epmc", "anchored"): epmc()} if both else {}
    metadata = summaries({"7": "10.1000/a", "8": "10.1000/b"})
    source = capture(broad(pubmed("7"), epmc()) | anchored, request=wide, metadata=metadata)
    data = json.loads(source.manifest)
    batch = data["metadata_requests"][0]
    if both:
        batch.update(status="partial", resolved=1)
    else:
        batch.update(status="unavailable", resolved=0, warning="timeout", response_bytes=None, response_sha256=None)
    broken = resave(source, data)
    # The contradictory donor is validated even though the parent uses the other profile.
    parent = capture(broad(pubmed(), TimeoutError()))
    with pytest.raises(RecoveryError):
        auto(parent, [broken], fetch_missing=True, fetch=lambda url: pytest.fail("sent a request"))


@pytest.mark.parametrize("issue", ["no_positions", "unresolved_with_pmid", "order"])
def test_screening_only_rows_require_real_positions_identity_and_order(capture, issue):
    import csv
    from hashlib import sha256
    import io

    wide = replace(REQUEST, profile="all_indexed")
    parent = capture(broad(pubmed(), TimeoutError()), request=wide)
    donor = capture(broad(pubmed(), epmc("7", "8")), request=wide)
    data = json.loads(donor.manifest)
    del data["native_hits"]
    rows = list(csv.DictReader(io.StringIO(donor.tsv), delimiter="\t"))
    if issue == "no_positions":
        rows.append(rows[-1] | {"native_positions": '{"epmc_broad":[]}', "epmc_broad_rank": "", "row_id": "r0003"})
        data["candidate_count"] += 1
    elif issue == "unresolved_with_pmid":
        rows[0]["identity_status"] = "unresolved"
    else:
        rows[0], rows[1] = rows[1], rows[0]
        rows[0]["row_id"], rows[1]["row_id"] = "r0001", "r0002"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=rows[0], delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    tsv = output.getvalue()
    data["candidates_sha256"] = sha256(tsv.encode()).hexdigest()
    bad = resave(replace(donor, tsv=tsv), data)
    with pytest.raises(RecoveryError):
        auto(parent, [bad], fetch_missing=True, fetch=lambda url: pytest.fail("sent a request"))


@pytest.mark.parametrize("change", ["parent", "lanes", "repeated", "action", "count", "donor"])
def test_broken_automatic_provenance_refuses_even_pmid_donor_before_fetch(capture, change):
    base = capture(broad(TimeoutError(), epmc()))
    parent = capture(broad(pubmed(), TimeoutError()))
    original = capture(broad(pubmed("1"), epmc()))
    derived = auto(base, [original])
    data = json.loads(derived.manifest)
    info = data["automatic_recovery"]
    if change == "parent":
        info["parent_search_id"] = 7
    elif change == "lanes":
        info["lanes"] = "broken"
    elif change == "repeated":
        info["lanes"].append(info["lanes"][0])
    elif change == "action":
        info["lanes"][0]["action"] = "retried"
    elif change == "count":
        info["lanes"][0]["eligible_donor_count"] = 0
    else:
        info["lanes"][0]["donor_search_id"] = "PRIVATE"
    with pytest.raises(RecoveryError):
        auto(parent, [resave(derived, data)], fetch_missing=True, fetch=lambda url: pytest.fail("sent a request"))
