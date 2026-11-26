"""Selective retries preserve the original ranks and provenance of every other lane."""

import hashlib
import json

import pytest

from medextract_dossier.discovery import DiscoveryRequest
from medextract_dossier.discovery_providers import collect_discovery
from medextract_dossier.discovery_recovery import RecoveryError, recover_discovery


def request(**options):
    return DiscoveryRequest("Question stays local", [["marker"], ["care"]], **options)


def capture(req, pubmed, europe, metadata=None):
    calls = []

    def fetch(url):
        calls.append(url)
        data = metadata if "esummary" in url else pubmed if "esearch" in url else europe
        if isinstance(data, Exception):
            raise data
        return json.dumps(data).encode()

    return collect_discovery(req, fetch=fetch)


def epmc(ids):
    return {"hitCount": len(ids), "resultList": {"result": [{"source": "MED", "id": key} for key in ids]}}


def pubmed(ids):
    return {"esearchresult": {"count": len(ids), "idlist": ids}}


def resign(manifest):
    manifest = {k: v for k, v in manifest.items() if k != "search_id"}
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return json.dumps(manifest | {"search_id": hashlib.sha256(canonical).hexdigest()})


def test_legacy_export_retries_only_timeout_and_preserves_exact_receipts_and_rank_gaps():
    primary = capture(request(), pubmed(["1", "1", "3"]), TimeoutError())
    original = json.loads(primary.manifest)
    for receipt in original["lanes"]:
        del receipt["query_translation_status"], receipt["excess_slots"]
    parent = resign(original)
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(epmc(["3", "4"])).encode()

    child = recover_discovery(parent, primary.tsv, fetch=fetch)
    after = json.loads(child.manifest)
    assert len(calls) == 1 and "ebi.ac.uk" in calls[0] and "Question" not in calls[0]
    assert [after["lanes"][i] for i in (0, 2, 3)] == [original["lanes"][i] for i in (0, 2, 3)]
    assert [(r["pmid"], r["pubmed_broad_rank"]) for r in child.page()["rows"]] == [
        ("1", 1),
        ("3", 3),
        ("4", None),
    ]
    info = after["recovery"]
    assert info["parent_search_id"] == json.loads(parent)["search_id"] != child.search_id
    assert info["retried_lanes"] == ["epmc_broad"] and info["request_count"] == 1
    assert (
        info["candidate_count_before"],
        info["candidate_count_after"],
        info["candidate_count_added"],
    ) == (2, 3, 1)
    assert info["elapsed_seconds"] >= 0


def test_repeat_timeout_keeps_original_candidates_and_can_recover_from_child():
    first = capture(request(), pubmed(["2"]), TimeoutError())

    def outage(url):
        raise TimeoutError()

    retry = recover_discovery(first.manifest, first.tsv, fetch=outage)
    assert retry.tsv == first.tsv and retry.search_id != first.search_id
    assert json.loads(retry.manifest)["recovery"]["candidate_count_added"] == 0
    last = recover_discovery(retry.manifest, retry.tsv, fetch=lambda url: json.dumps(epmc([])).encode())
    assert last.summary()["status"] == "ok" and last.tsv == first.tsv
    assert json.loads(last.manifest)["recovery"]["parent_search_id"] == retry.search_id


@pytest.mark.parametrize("europe", [epmc([]), OSError(), {"error": "bad syntax"}, {"resultList": {}}])
def test_other_states_never_trigger_network(europe):
    parent = capture(request(), pubmed([]), europe)
    with pytest.raises(RecoveryError, match="timed.out|timeout"):
        recover_discovery(parent.manifest, parent.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


@pytest.mark.parametrize("change", ["digest", "tsv", "query", "ranks", "status", "shape"])
def test_bad_export_fails_before_dispatch(change):
    parent = capture(request(), pubmed(["1"]), TimeoutError())
    meta, tsv = json.loads(parent.manifest), parent.tsv
    if change == "query":
        meta["lanes"][1]["parameters"]["query"] = "different"
    if change == "ranks":
        tsv = tsv.replace("/1/\t1", "/1/\t101")
        meta["candidates_sha256"] = hashlib.sha256(tsv.encode()).hexdigest()
    if change == "status":
        meta["lanes"][0]["status"] = "unavailable"
    if change == "shape":
        meta["lanes"] = []
    encoded = resign(meta)
    if change == "digest":
        encoded = encoded.replace("Question", "Changed")
    if change == "tsv":
        tsv += "\n"
    with pytest.raises(RecoveryError):
        recover_discovery(encoded, tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


def test_wide_retries_exact_claims_and_counts_metadata_requests():
    base = capture(
        request(profile="all_indexed"),
        TimeoutError(),
        {
            "hitCount": 3,
            "resultList": {
                "result": [
                    {"source": "MED", "id": "10", "doi": "10.1000/a"},
                    {"doi": "10.1000/a"},
                    {"title": "unknown"},
                ]
            },
        },
    )
    calls = []

    def fetch(url):
        calls.append(url)
        if "esummary" in url:
            return json.dumps(
                {"result": {"10": {"uid": "10", "articleids": []}, "12": {"uid": "12", "articleids": []}}}
            ).encode()
        return json.dumps(pubmed(["10", "12"])).encode()

    retry = recover_discovery(base.manifest, base.tsv, fetch=fetch)
    assert len(calls) == 2 and all("ncbi.nlm.nih.gov" in url for url in calls)
    assert len(retry.page()["rows"]) == 3
    assert retry.page()["rows"][0]["native_positions"] == {"pubmed_broad": [1], "epmc_broad": [1, 2]}
    info = json.loads(retry.manifest)["recovery"]
    assert info["candidate_count_added"] == 1 and info["request_count"] == 2
    # The completed follow-up can now donate its Europe PMC lane independently.
    assert json.loads(retry.manifest)["native_hits"]["epmc_broad"] == json.loads(base.manifest)["native_hits"]["epmc_broad"]
    original = json.loads(base.manifest)
    del original["native_hits"]
    with pytest.raises(RecoveryError, match="native"):
        recover_discovery(resign(original), base.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


def test_mixed_anchored_and_broad_timeouts_are_the_only_requests():
    req = request(include_title_search=True)
    inputs = {
        req.url("epmc", "broad"): TimeoutError(),
        req.url("pubmed", "anchored"): TimeoutError(),
        req.url("epmc", "anchored"): epmc(["5"]),
        req.url("pubmed", "broad"): pubmed(["2"]),
    }

    def fetch(url):
        result = inputs[url]
        if isinstance(result, Exception):
            raise result
        return json.dumps(result).encode()

    base = collect_discovery(req, fetch=fetch)
    expected_calls = {req.url("epmc", "broad"), req.url("pubmed", "anchored")}
    calls = []

    def retry(url):
        calls.append(url)
        return json.dumps(epmc([]) if "ebi.ac.uk" in url else pubmed(["3"])).encode()

    child = recover_discovery(base.manifest, base.tsv, fetch=retry)
    assert len(calls) == 2 and set(calls) == expected_calls
    assert child.summary()["recovery"]["retried_lanes"] == ["epmc_broad", "pubmed_anchored"]
    for i in (0, 3):
        assert json.loads(child.manifest)["lanes"][i] == json.loads(base.manifest)["lanes"][i]


@pytest.mark.parametrize("field", ["extra", "pmids", "warning", "captured_at", "identifier_conflicts"])
def test_wide_metadata_is_validated_before_reuse(field):
    base = capture(
        request(profile="all_indexed"),
        pubmed(["1"]),
        TimeoutError(),
        {"result": {"1": {"uid": "1", "articleids": []}}},
    )
    data = json.loads(base.manifest)
    data["metadata_requests"][0][field] = "private note"
    with pytest.raises(RecoveryError):
        recover_discovery(resign(data), base.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


def test_timeout_with_response_bytes_is_invalid_before_fetch():
    base = capture(request(), pubmed(["1"]), TimeoutError())
    data = json.loads(base.manifest)
    data["lanes"][1].update(response_bytes=42, response_sha256="f" * 64)
    with pytest.raises(RecoveryError):
        recover_discovery(resign(data), base.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


@pytest.mark.parametrize("status,warning", [(503, "http_error"), (429, "rate_limited")])
def test_recovered_http_error_records_only_the_numeric_status_and_never_retries_it(status, warning):
    from urllib.error import HTTPError

    first = capture(request(), pubmed(["2"]), TimeoutError())

    def fetch(url):
        raise HTTPError("https://private.invalid/", status, "private note", {"Private": "private"}, None)

    child = recover_discovery(first.manifest, first.tsv, fetch=fetch)
    meta = json.loads(child.manifest)
    lane = meta["lanes"][1]
    assert (lane["status"], lane["warnings"], lane["http_status"]) == ("unavailable", [warning], status)
    assert lane["response_bytes"] is None and "private" not in child.manifest
    assert child.tsv == first.tsv and meta["lanes"][0] == json.loads(first.manifest)["lanes"][0]
    assert "http_status" not in first.manifest
    with pytest.raises(RecoveryError, match="timed out"):
        recover_discovery(child.manifest, child.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))


@pytest.mark.parametrize("target", ["metadata", "search"])
def test_retained_http_failure_validates_status_when_present_and_preserves_older_receipts(target):
    from urllib.error import HTTPError

    error = HTTPError("https://private.invalid/", 502, "PRIVATE", {}, None)
    base = capture(
        request(profile="all_indexed"),
        error if target == "search" else pubmed(["4"]),
        TimeoutError(),
        error,
    )
    data = json.loads(base.manifest)
    old = data["lanes"][0] if target == "search" else data["metadata_requests"][0]
    assert old["http_status"] == 502 and "PRIVATE" not in base.manifest
    for invalid in (429, "502", 302, True):
        old["http_status"] = invalid
        with pytest.raises(RecoveryError):
            recover_discovery(resign(data), base.tsv, fetch=lambda url: pytest.fail("unexpected fetch"))
    for old_status in (502, None):
        if old_status is None:
            old.pop("http_status")
        else:
            old["http_status"] = old_status
        child = recover_discovery(resign(data), base.tsv, fetch=lambda url: json.dumps(epmc([])).encode())
        result = json.loads(child.manifest)
        assert (result["lanes"][0] if target == "search" else result["metadata_requests"][0]) == old
