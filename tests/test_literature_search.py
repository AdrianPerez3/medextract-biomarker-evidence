"""Exercise the public literature workflow with fixed provider responses."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from threading import Barrier, Event, Lock
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from medextract_dossier import literature_http, literature_search


def europe(*hits, total=None):
    return {"hitCount": len(hits) if total is None else total, "resultList": {"result": list(hits)}}


def pubmed(*ids, total=None):
    return {"esearchresult": {"count": str(len(ids) if total is None else total), "idlist": list(ids)}}


def summary(uid, title, **metadata):
    return {"uid": uid, "title": title, **metadata}


def transport(monkeypatch, epmc, search, summaries=None):
    calls = []
    replies = {"search": epmc, "esearch.fcgi": search, "esummary.fcgi": summaries}

    def fetch(url, **kwargs):
        parsed = urlsplit(url)
        endpoint = parsed.path.split("/")[-1]
        assert parsed.scheme == "https"
        expected_host = "www.ebi.ac.uk" if endpoint == "search" else "eutils.ncbi.nlm.nih.gov"
        assert parsed.netloc == expected_host
        calls.append((endpoint, parse_qs(parsed.query)))
        reply = replies[endpoint]
        assert reply is not None, f"Unexpected {endpoint} call"
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()

    monkeypatch.setattr(literature_http, "fetch_bytes", fetch)
    return calls


@pytest.mark.parametrize(("terms", "kind", "identifier"), [
    ("https://doi.org/10.1234/Trial", "doi", "10.1234/trial"),
    ("PMID: 009", "pmid", "9"), ("PMC007", "pmcid", "PMC7"),
])
def test_requested_identifier_beats_commentary_before_final_cap(monkeypatch, terms, kind, identifier):
    epmc = europe({"title": "Commentary", "id": "28", "source": "MED", "doi": "10.1234/other"})
    hit = summary("9", "Trial", articleids=[{"idtype": "doi", "value": "10.1234/trial"},
                                           {"idtype": "pmc", "value": "PMC7"}])
    calls = transport(monkeypatch, epmc, pubmed("9"), {"result": {"9": hit}})
    result = literature_search.search_literature("Which paper?", terms, 1)
    assert result["records"][0]["title"] == "Trial"
    assert result["records"][0]["provider_ranks"] == {"pubmed": 1}
    assert result["requested_identifier"] == {"kind": kind, "value": identifier, "matches_in_fetched_pages": 1}
    assert result["output_truncated"] and len(calls) == 3


def test_missing_exact_id_is_reported_and_topic_queries_stay_ranked(monkeypatch):
    transport(monkeypatch, europe({"title": "Related paper", "doi": "10.1234/other"}), pubmed())
    result = literature_search.search_literature("10.1234/missing")
    assert result["requested_identifier"]["matches_in_fetched_pages"] == 0
    assert result["records"][0]["title"] == "Related paper"
    for query in ("2026", "papers citing 10.1234/other"):
        assert "requested_identifier" not in literature_search.search_literature(query)


def test_ranked_first_pages_merge_normalized_citations_before_the_output_cap(monkeypatch):
    ebi = europe(
        {"title": "Shared <i>report</i>", "doi": "https://doi.org/10.1234/A", "pmcid": "pmc009",
         "pubType": "journal article; retracted publication", "abstractText": "excluded abstract"},
        {"title": "Separate European record", "id": "PPR45", "source": "PPR"},
        {"title": "Another shared article", "source": "MED", "id": "00300"}, total=300,
    )
    ncbi = pubmed("100", "200", "300", total=400)
    details = {"result": {
        "300": summary("300", "Third PubMed result"),
        "200": summary("200", "Second PubMed result"),
        "100": summary("100", "First PubMed result", articleids=[
            {"idtype": "doi", "value": "doi:10.1234/a"}, {"idtype": "pmc", "value": "PMC9"}
        ], pubtype=["Journal Article", "Published Erratum"],
           references=[{"reftype": "Erratum in", "refsource": "unused source citation"}]),
    }}
    calls = transport(monkeypatch, ebi, ncbi, details)
    question = "  What is the evidence for this biomarker? "
    query = '"NT-proBNP" AND "heart failure"'
    result = literature_search.search_literature(question, query, 3)
    assert result["question"] == question
    assert result["query"] == query
    assert result["status"] == "ok"
    assert result["unique_records_before_limit"] == 4
    assert result["output_truncated"] is True
    assert all(p["more_than_limit"] for p in result["providers"].values())
    assert [r["title"] for r in result["records"]] == ["Shared report", "Separate European record", "Second PubMed result"]
    shared, epmc_item, _ = result["records"]
    assert (shared["doi"], shared["pmid"], shared["pmcid"]) == ("10.1234/a", "100", "PMC9")
    assert shared["url"] == "https://doi.org/10.1234/a"
    assert shared["providers"] == ["europe_pmc", "pubmed"]
    assert shared["provider_ranks"] == {"europe_pmc": 1, "pubmed": 1}
    assert shared["publication_flags"] == ["retracted publication", "Published Erratum", "Erratum in"]
    assert epmc_item["url"] == "https://europepmc.org/article/PPR/PPR45"
    assert result["providers"]["europe_pmc"]["total_hits"] == 300
    assert result["providers"]["pubmed"]["retrieved"] == 3
    assert datetime.fromisoformat(result["retrieved_at_utc"]).utcoffset() == timedelta(0)
    assert [endpoint for endpoint, _ in calls if endpoint != "search"] == ["esearch.fcgi", "esummary.fcgi"]
    assert len(calls) == 3
    requests = dict(calls)
    assert requests["search"] == {"query": [query], "resultType": ["lite"], "format": ["json"], "pageSize": ["3"]}
    assert requests["esearch.fcgi"]["term"] == [query] and requests["esearch.fcgi"]["sort"] == ["relevance"]
    assert requests["esummary.fcgi"]["id"] == ["100,200,300"]
    assert question not in str(calls)
    assert "excluded abstract" not in str(result)


def test_late_identifiers_bridge_rows_and_missing_ids_never_merge_by_title(monkeypatch):
    epmc = europe(
        {"title": "Initial DOI", "doi": "10.1234/01"},
        {"title": "Same title"},
        {"title": "Same title"},
    )
    # The bridge arrives after a DOI-only citation and an initial PMID-only citation.
    ncbi = pubmed("111", "111")
    details = {"result": {"111": summary("111", "PMID only")}}
    epmc["resultList"]["result"].append({"title": "Bridge", "doi": "10.1234/01", "pmid": "111"})
    epmc["hitCount"] = 4
    transport(monkeypatch, epmc, ncbi, details)
    result = literature_search.search_literature("troponin", limit=5)
    assert result["query"] == "troponin"
    assert len(result["records"]) == 3
    first, *unidentified = result["records"]
    assert first["provider_ranks"] == {"europe_pmc": 1, "pubmed": 1}
    assert first["pmid"] == "111"
    assert [r["url"] for r in unidentified] == [None, None]


def test_empty_hits_do_not_trigger_a_summary_call(monkeypatch):
    calls = transport(monkeypatch, europe(), pubmed())
    result = literature_search.search_literature("marker")
    assert result["status"] == "ok" and result["records"] == []
    assert result["output_truncated"] is False
    assert all(p["total_hits"] == 0 for p in result["providers"].values())
    assert len(calls) == 2


def test_pubmed_can_finish_while_europe_pmc_is_waiting_and_europe_keeps_tie_precedence(monkeypatch):
    pubmed_finished = Event()
    calls = transport(monkeypatch, europe({"title": "European title", "pmid": "17"}), pubmed("17"),
                      {"result": {"17": summary("17", "PubMed title")}})
    fetch = literature_http.fetch_bytes

    def overlap(url):
        if urlsplit(url).hostname == "www.ebi.ac.uk":
            assert pubmed_finished.wait(3), "PubMed should run during the Europe PMC request"
        data = fetch(url)
        if urlsplit(url).path.endswith("/esummary.fcgi"):
            pubmed_finished.set()
        return data

    monkeypatch.setattr(literature_http, "fetch_bytes", overlap)
    result = literature_search.search_literature("marker")
    assert [endpoint for endpoint, _ in calls] == ["esearch.fcgi", "esummary.fcgi", "search"]
    assert list(result["providers"]) == ["europe_pmc", "pubmed"]
    assert result["unique_records_before_limit"] == 1
    assert result["records"][0]["title"] == "European title"
    assert result["records"][0]["providers"] == ["europe_pmc", "pubmed"]


def test_concurrent_searches_share_ncbi_pacing_and_finish_with_caller_specific_results(monkeypatch):
    class Reply(BytesIO):
        headers = {}

    clock = [10.0]
    ncbi_delays = []
    entrants = Barrier(2)

    def sleep(delay):
        ncbi_delays.append(delay)
        clock[0] += delay

    def fetch(request, timeout):
        endpoint = urlsplit(request.full_url)
        params = parse_qs(endpoint.query)
        if endpoint.hostname == "www.ebi.ac.uk":
            entrants.wait(3)
            data = europe()
        else:
            if endpoint.path.endswith("/esearch.fcgi"):
                data = pubmed(params["term"][0])
            else:
                uid = params["id"][0]
                data = {"result": {uid: summary(uid, "Study " + uid)}}
        return Reply(json.dumps(data).encode())

    monkeypatch.setattr(literature_http, "monotonic", lambda: clock[0])
    monkeypatch.setattr(literature_http, "sleep", sleep)
    monkeypatch.setattr(literature_http, "_last_pubmed_request", 0.0)
    monkeypatch.setattr(literature_http._OPENER, "open", fetch)
    with ThreadPoolExecutor(max_workers=2) as callers:
        results = list(callers.map(literature_search.search_literature, ["123", "456"]))
    assert [[r["pmid"] for r in result["records"]] for result in results] == [["123"], ["456"]]
    assert ncbi_delays == pytest.approx([0.35, 0.35, 0.35])


def test_burst_of_searches_runs_at_most_two_provider_pairs_at_once(monkeypatch):
    requests = Barrier(4)
    lock = Lock()
    active = peak = 0

    def fetch(url):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        requests.wait(3)
        with lock:
            active -= 1
        reply = europe() if urlsplit(url).hostname == "www.ebi.ac.uk" else pubmed()
        return json.dumps(reply).encode()

    monkeypatch.setattr(literature_http, "fetch_bytes", fetch)
    with ThreadPoolExecutor(max_workers=6) as callers:
        results = list(callers.map(literature_search.search_literature, ["marker"] * 6))
    assert (active, peak) == (0, 4)
    assert all(result["status"] == "ok" and not result["records"] for result in results)


def test_redirect_to_ncbi_is_paced_and_redirect_off_provider_is_rejected(monkeypatch):
    delays = []
    monkeypatch.setattr(literature_http, "_last_pubmed_request", 10.0)
    monkeypatch.setattr(literature_http, "monotonic", lambda: 10.0)
    monkeypatch.setattr(literature_http, "sleep", delays.append)
    redirect = literature_http._ProviderRedirect()
    original = Request("https://www.ebi.ac.uk/europepmc/webservices/rest/search")
    with pytest.raises(ValueError, match="supported literature provider"):
        redirect.redirect_request(original, None, 302, "", {}, "https://example.org/")
    target = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    assert redirect.redirect_request(original, None, 302, "", {}, target).full_url == target
    assert delays == pytest.approx([0.35])


def test_report_partial_pubmed_interpretation_and_keep_ranked_hits(monkeypatch):
    ncbi = pubmed("13")
    ncbi["esearchresult"].update(
        errorlist={"fieldsnotfound": ["XBAD"]}, querytranslation="troponin",
        warninglist={"phrasesignored": ["unknown"]},
    )
    transport(monkeypatch, europe(), ncbi, {"result": {"13": summary("13", "Study")}})
    result = literature_search.search_literature("troponin[XBAD]")
    report = result["providers"]["pubmed"]
    assert report["query_translation"] == "troponin"
    assert report["errorlist"] == {"fieldsnotfound": ["XBAD"]}
    assert report["warninglist"] == {"phrasesignored": ["unknown"]}
    assert report["status"] == "partial" and result["records"][0]["pmid"] == "13"


def test_paging_failure_preserves_other_source_and_prior_search_count(monkeypatch):
    transport(monkeypatch, europe({"title": "Study", "id": "55", "source": "MED"}),
              pubmed("55", total=204), TimeoutError("private upstream message"))
    result = literature_search.search_literature("marker")
    assert result["status"] == "partial"
    assert result["records"][0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/55/"
    provider = result["providers"]["pubmed"]
    assert (provider["status"], provider["limit"], provider["total_hits"], provider["retrieved"]) == ("unavailable", 5, 204, 0)
    assert provider["error"] and "private upstream message" not in str(result)


def test_partial_response_skips_missing_wrong_and_untitled_summary_records(monkeypatch):
    transport(monkeypatch, europe({}, {"title": "Valid"}, total=2), pubmed("12", "13", "14", "15"),
              {"result": {"12": summary("12", "Actual"), "14": summary("10", "Wrong article"), "15": {"uid": "15"}}})
    result = literature_search.search_literature("marker")
    assert result["status"] == "partial"
    assert [r["title"] for r in result["records"]] == ["Actual", "Valid"]
    assert all(p["status"] == "partial" and p["error"] for p in result["providers"].values())


def test_unavailable_is_distinct_from_zero_hits(monkeypatch):
    transport(monkeypatch, b"<html>bad gateway</html>", {"error": "API rate limit exceeded"})
    result = literature_search.search_literature("marker")
    assert result["status"] == "unavailable" and result["records"] == []
    assert all(p["more_than_limit"] is None for p in result["providers"].values())
    assert all(p["total_hits"] is None and p["error"] for p in result["providers"].values())


@pytest.mark.parametrize(("question", "terms", "limit"), [
    (" ", None, 5), ("x" * 513, "valid", 5), ("ok", "", 5), ("ok", "x" * 513, 5),
    ("ok", "bad\x00query", 5), ("ok", None, False), ("ok", None, 21), ("ok", None, 0),
])
def test_invalid_inputs_fail_before_network_access(monkeypatch, question, terms, limit):
    calls = transport(monkeypatch, None, None)
    with pytest.raises(ValueError):
        literature_search.search_literature(question, terms, limit)
    assert calls == []
