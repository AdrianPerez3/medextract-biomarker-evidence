import json
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

from medextract_dossier.discovery import DiscoveryRequest
from medextract_dossier.discovery_providers import collect_discovery


def req(**kwargs):
    return DiscoveryRequest("local private question", [["cystatin C"], ["primary care"]], **kwargs)


def search(ids, total=None, **other):
    return {"esearchresult": {"idlist": ids, "count": str(len(ids) if total is None else total), **other}}


def europe(hits, total=None):
    return {"hitCount": len(hits) if total is None else total, "resultList": {"result": hits}}


def feed(pubmed, epmc, summary=None):
    calls = []

    def fetch(url):
        calls.append(url)
        value = summary if "esummary" in url else pubmed if "esearch" in url else epmc
        if isinstance(value, BaseException):
            raise value
        return json.dumps(value).encode()

    return fetch, calls


def test_query_validation_budget_and_native_rank_gaps():
    fetch, calls = feed(
        search(["1", "1", "9", "bad"]),
        europe(
            [
                {"id": "8", "source": "MED"},
                {"id": "3", "source": "MED", "pmid": "4"},
                {"source": "PMC", "id": "PMC1"},
            ]
        ),
    )
    snap = collect_discovery(req(depth=4, include_title_search=True), fetch=fetch)
    assert len(calls) == 4 and all("private" not in url for url in calls)
    assert snap.summary()["status"] == "partial"
    assert snap.page(view="pubmed_broad")["rows"][1]["pubmed_broad_rank"] == 3
    assert [r["pmid"] for r in snap.page()["rows"]] == ["1", "8", "9"]
    p = json.loads(snap.manifest)["lanes"][0]
    assert (p["native_slots"], p["usable_rows"], p["duplicate_slots"], p["unusable_slots"]) == (4, 2, 1, 1)
    assert p["truncated"] is False and p["cap_reached"]


def test_outages_empty_query_and_short_pages_are_distinct():
    failure = HTTPError("https://example.org/private", 429, "server secret", {}, None)
    cases = [
        (search([]), europe([]), "ok"),
        (failure, TimeoutError("private detail"), "unavailable"),
        (search(["1", "2"], 8), europe([]), "partial"),
        (search([], errorlist={"bad": ["secret"]}), failure, "partial"),
        ({"esearchresult": {"count": "4", "idlist": "bad"}}, failure, "partial"),
        ({"esearchresult": {"count": "-1", "idlist": ["20"]}}, failure, "partial"),
    ]
    for pubmed, epmc, expected in cases:
        fetch, calls = feed(pubmed, epmc)
        snap = collect_discovery(req(), fetch=fetch)
        assert snap.summary()["status"] == expected
        assert "secret" not in snap.manifest and "private detail" not in snap.manifest
        assert len(calls) == 2


def test_wide_sources_conflicts_and_summary_outage():
    fetch, calls = feed(
        search(["12"]),
        europe(
            [
                {"source": "PMC", "id": "PMC3", "doi": "10.1234/abc", "pmid": "12"},
                {"source": "PPR", "id": "PPR9", "doi": "10.1234/abc", "pmid": "25"},
                {"title": "Same title"},
                {"title": "Same title"},
                {"source": "PPR", "id": "PPR7"},
            ]
        ),
        OSError("private"),
    )
    snap = collect_discovery(req(profile="all_indexed"), fetch=fetch)
    assert len(calls) == 3 and snap.summary()["status"] == "partial"
    rows = snap.page()["rows"]
    assert {r["pmid"] for r in rows} >= {"12", "25"}
    assert sum(r["identity_status"] == "unresolved" for r in rows) == 2
    assert {r["pmid"] for r in rows if r["identity_status"] == "conflict"} >= {"12", "25"}
    assert any("PPR:PPR7" in r["native_ids"] for r in rows)
    assert len(set(r["row_id"] for r in rows)) == len(rows)


def test_discovery_shares_quick_search_ncbi_clock_and_provider_slots(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from threading import Barrier
    import pytest
    from medextract_dossier import literature_http, literature_search

    class Response(BytesIO):
        headers = {}

    clock = [10.0]
    delays = []
    both_providers = Barrier(2)

    def sleep(delay):
        clock[0] += delay
        delays.append(delay)

    def open_reply(request, timeout):
        url = urlsplit(request.full_url)
        if url.hostname == "www.ebi.ac.uk":
            both_providers.wait(timeout=4)
            payload = europe([])
        elif url.path.endswith("/esearch.fcgi"):
            payload = search(["1"])
        else:
            payload = {"result": {"1": {"uid": "1", "title": "Paper", "articleids": []}}}
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(literature_http, "monotonic", lambda: clock[0])
    monkeypatch.setattr(literature_http, "sleep", sleep)
    monkeypatch.setattr(literature_http, "_last_pubmed_request", 0.0)
    monkeypatch.setattr(literature_http._OPENER, "open", open_reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        quick = pool.submit(literature_search.search_literature, "primary care")
        discovery = pool.submit(collect_discovery, req())
    assert quick.result()["status"] == discovery.result().summary()["status"] == "ok"
    assert delays == pytest.approx([0.35, 0.35])


def test_oversized_response_is_unavailable_and_releases_slots():
    from medextract_dossier import literature_http

    snap = collect_discovery(req(), fetch=lambda url: b" " * 2_000_001)
    assert snap.summary()["status"] == "unavailable"
    assert json.loads(snap.manifest)["lanes"][0]["warnings"] == ["response_limit"]
    assert literature_http.search_slots.acquire(blocking=False)
    try:
        assert literature_http.search_slots.acquire(blocking=False)
        literature_http.search_slots.release()
    finally:
        literature_http.search_slots.release()


def test_receipt_tracks_excess_provider_slots_without_increasing_query_budget():
    fetch, _ = feed(search(["1", "2", "3"], 8), europe([]))
    snap = collect_discovery(req(depth=2), fetch=fetch)
    assert [row["pmid"] for row in snap.page()["rows"]] == ["1", "2"]
    p = json.loads(snap.manifest)["lanes"][0]
    assert (p["native_slots"], p["usable_rows"], p["excess_slots"], p["truncated"]) == (3, 2, 1, True)


def test_provider_diagnostic_and_wrapped_timeout_are_distinct():
    from urllib.error import URLError

    fetch, _ = feed({"ERROR": "an arbitrary private message"}, URLError(TimeoutError("private")))
    lanes = json.loads(collect_discovery(req(), fetch=fetch).manifest)["lanes"]
    assert (lanes[0]["status"], lanes[1]["status"]) == ("partial", "unavailable")
    assert "query_error" in lanes[0]["warnings"] and lanes[1]["warnings"] == ["timeout"]


def test_bad_summary_uid_conflicting_alternatives_and_missing_summary_preserve_esearch_ids():
    fetch, _ = feed(
        search(["1", "2", "3"]),
        europe([]),
        {
            "result": {
                "1": {"uid": "2", "articleids": [{"idtype": "doi", "value": "10.1000/bogus"}]},
                "2": {
                    "uid": "2",
                    "articleids": [
                        {"idtype": "pubmed", "value": "2"},
                        {"idtype": "doi", "value": "10.1000/a"},
                        {"idtype": "doi", "value": "10.1000/b"},
                    ],
                },
            }
        },
    )
    snap = collect_discovery(req(profile="all_indexed"), fetch=fetch)
    rows = snap.page()["rows"]
    assert [r["pmid"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["doi"] is None and rows[1]["identity_status"] == "conflict"
    receipt = json.loads(snap.manifest)["metadata_requests"][0]
    assert receipt["identifier_conflicts"] == {"2": {"doi": ["10.1000/a", "10.1000/b"]}}


def test_wide_batching_uses_union_of_searches_and_records_metadata_cost():
    uids = [str(i) for i in range(1, 101)]
    fetch, calls = feed(search(uids), europe([]), {"result": {i: {"uid": i, "articleids": []} for i in uids}})
    snap = collect_discovery(req(profile="all_indexed", include_title_search=True), fetch=fetch)
    metadata_calls = [url for url in calls if "esummary" in url]
    assert len(calls) == 6 and len(metadata_calls) == 2
    assert all(len(parse_qs(urlsplit(url).query)["id"][0].split(",")) == 50 for url in metadata_calls)
    assert snap.summary()["status"] == "ok" and snap.summary()["candidate_count"] == 100
    assert all(r["response_bytes"] > 0 for r in json.loads(snap.manifest)["metadata_requests"])


def test_wide_compatible_bridge_merges_and_retains_all_positions():
    fetch, _ = feed(
        search(["10"]),
        europe(
            [
                {"source": "PMC", "id": "PMC2", "doi": "https://doi.org/10.1234/ABC"},
                {"source": "MED", "id": "10", "pmid": "10", "doi": "10.1234/abc", "pmcid": "PMC2"},
                {"source": "MED", "id": "10", "pmid": "10", "doi": "10.1234/abc", "pmcid": "PMC2"},
            ]
        ),
        {"result": {"10": {"uid": "10", "articleids": []}}},
    )
    snap = collect_discovery(req(profile="all_indexed"), fetch=fetch)
    rows = snap.page()["rows"]
    assert len(rows) == 1 and rows[0]["doi"] == "10.1234/abc"
    assert rows[0]["native_positions"] == {"pubmed_broad": [1], "epmc_broad": [1, 2, 3]}
    assert sorted(rows[0]["native_ids"]) == ["MED:10", "PMC:PMC2"]
    assert rows[0]["identity_status"] == "resolved"


def test_med_record_can_supply_pmid_without_native_id_and_missing_source_is_flagged():
    fetch, _ = feed(search([]), europe([{"source": "MED", "pmid": "27"}, {"pmid": "28"}]))
    snap = collect_discovery(req(), fetch=fetch)
    assert [(r["pmid"], r["epmc_broad_rank"]) for r in snap.page()["rows"]] == [("27", 1)]
    assert snap.summary()["status"] == "partial"


def test_only_bounded_public_query_translation_is_exposed():
    fetch, _ = feed(search([], querytranslation="https://host.invalid/?api_key=private"), europe([]))
    manifest = collect_discovery(req(), fetch=fetch).manifest
    assert "host.invalid" not in manifest and "api_key" not in manifest
    fetch, _ = feed(search([], querytranslation='"cystatin c"[Title/Abstract]'), europe([]))
    assert (
        json.loads(collect_discovery(req(), fetch=fetch).manifest)["lanes"][0]["query_translation"]
        == '"cystatin c"[Title/Abstract]'
    )


def test_real_pubmed_no_items_notice_is_a_successful_empty_page():
    info = {"phrasesignored": [], "quotedphrasesnotfound": [], "outputmessages": ["No items found."]}
    for ids, total, diagnostic, extra, expected in (
        ([], 0, info, {}, "ok"),
        ([], 0, info | {"phrasesignored": ["red cells"]}, {}, "partial"),
        ([], 0, info | {"outputmessages": ["No items found.", "Unexpected notice"]}, {}, "partial"),
        ([], 0, info, {"errorlist": {"fieldsnotfound": ["marker"]}}, "partial"),
        (["101"], 1, info, {}, "partial"),
    ):
        fetch, _ = feed(search(ids, total, warninglist=diagnostic, **extra), europe([]))
        lane = collect_discovery(req(), fetch=fetch).summary()["lanes"][0]
        assert lane["status"] == expected


def test_wide_conflicting_native_id_links_to_the_observed_europe_pmc_record():
    fetch, _ = feed(search([]), europe([{"source": "MED", "id": "20", "pmid": "30"}]))
    row = collect_discovery(req(profile="all_indexed"), fetch=fetch).page()["rows"][0]
    assert row["identity_status"] == "conflict" and row["pmid"] == "30" and row["native_ids"] == ["MED:20"]
    assert row["url"] == "https://europepmc.org/article/MED/20"


def test_translation_provenance_does_not_change_provider_availability():
    for text, status, output in (
        ("a" * 4096, "complete", "a" * 4096),
        ("a" * 4097, "truncated", "a" * 4096),
        (None, "not_provided", None),
        ("secret\nunprintable", "omitted", None),
        ("https://host.invalid/?token=private", "omitted", None),
    ):
        fetch, _ = feed(search([], querytranslation=text), europe([]))
        lane = json.loads(collect_discovery(req(), fetch=fetch).manifest)["lanes"][0]
        assert lane["status"] == "ok"
        assert lane["query_translation_status"] == status and lane["query_translation"] == output
