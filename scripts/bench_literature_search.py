"""Measure federated search latency with synthetic responses and real request pacing.

To measure the current checkout, run
    python -m scripts.bench_literature_search --runs 20

To measure another checkout with the same benchmark, run
    PYTHONPATH=/path/to/checkout python scripts/bench_literature_search.py --runs 20
"""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter, sleep
from unittest.mock import patch
from urllib.parse import urlsplit

from medextract_dossier import literature_http, literature_search


DELAYS_MS = {"search": 280, "esearch.fcgi": 80, "esummary.fcgi": 140}


def mock_pages():
    ids = [str(i) for i in range(1, 21)]
    european_hits = [{"source": "MED", "id": str(i), "title": f"EPMC {i}"} for i in range(11, 31)]
    pages = {
        "search": {"hitCount": 202, "resultList": {"result": european_hits}},
        "esearch.fcgi": {"esearchresult": {"count": "203", "idlist": ids}},
        "esummary.fcgi": {"result": {uid: {"title": f"PubMed {uid}", "uid": uid} for uid in ids}},
    }
    return {endpoint: json.dumps(page).encode() for endpoint, page in pages.items()}


class Response(BytesIO):
    headers = {}


def trial(pages):
    calls = []

    def open_request(request, timeout):
        endpoint = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        calls.append((endpoint, perf_counter()))
        sleep(DELAYS_MS[endpoint] / 1000)
        return Response(pages[endpoint])

    # Each isolated search starts with a fresh budget. ESearch and ESummary
    # still go through the shared transport's real 350 ms request spacing.
    with (patch.object(literature_http._OPENER, "open", open_request),
          patch.object(literature_http, "_last_pubmed_request", 0.0)):
        start = perf_counter()
        result = literature_search.search_literature("Which public studies?", "synthetic marker", limit=20)
        elapsed_ms = 1000 * (perf_counter() - start)

    expected_pmids = [str(uid) for i in range(1, 11) for uid in (i + 10, i)]
    if (sorted(endpoint for endpoint, _ in calls) != sorted(DELAYS_MS)
            or result["status"] != "ok" or result["unique_records_before_limit"] != 30
            or not result["output_truncated"] or [r["pmid"] for r in result["records"]] != expected_pmids):
        raise RuntimeError("The search did not return the expected synthetic results")
    starts = dict(calls)
    gap_ms = 1000 * (starts["esummary.fcgi"] - starts["esearch.fcgi"])
    del result["retrieved_at_utc"]
    digest = sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return elapsed_ms, gap_ms, digest


def measure(runs):
    pages = mock_pages()
    _, _, expected_digest = trial(pages)  # Discard one warm-up.
    samples = [trial(pages) for _ in range(runs)]
    if any(digest != expected_digest for _, _, digest in samples):
        raise RuntimeError("The synthetic search response varied between samples")
    times = sorted(elapsed for elapsed, _, _ in samples)
    return {
        "runs": runs, "provider_delays_ms": DELAYS_MS,
        "median_ms": round(median(times), 3), "p95_ms": round(times[ceil(0.95 * runs) - 1], 3),
        "minimum_pubmed_interval_ms": round(min(gap for _, gap, _ in samples), 3),
        "result_sha256": expected_digest,
        "source_sha256": {module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for module in (literature_search, literature_http)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=20, help="Measured searches after one warm-up (default 20)")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    print(json.dumps(measure(args.runs), indent=2))
