"""Acquire bounded discovery pages from PubMed and Europe PMC."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from . import literature_http
from .discovery import LANES, DiscoveryRequest, DiscoverySnapshot, Hit, Lane, build_snapshot
from .literature_search import _identifier


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _load(fetch, url):
    """Retain bounded response provenance even when its JSON is unusable."""
    receipt = dict(captured_at=_utc(), response_bytes=None, response_sha256=None)
    try:
        raw = fetch(url)
        if len(raw) > 2_000_000:
            return None, receipt, "response_limit"
        receipt.update(response_bytes=len(raw), response_sha256=sha256(raw).hexdigest())
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None, receipt, "invalid_response"
        return payload, receipt, None
    except HTTPError as exc:
        if isinstance(exc.code, int) and 400 <= exc.code <= 599:
            receipt["http_status"] = int(exc.code)
        return None, receipt, "rate_limited" if exc.code == 429 else "http_error"
    except TimeoutError:
        return None, receipt, "timeout"
    except URLError as exc:
        return None, receipt, "timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
    except OSError:
        return None, receipt, "network_error"
    except ValueError as exc:
        if str(exc) == "Provider response exceeds the size limit.":
            return None, receipt, "response_limit"
        return None, receipt, "invalid_response"
    except RecursionError:
        return None, receipt, "invalid_response"


def _valid_pmid(value):
    return value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value) else None


def _epmc_hit(item, wide):
    if not isinstance(item, dict):
        return None
    source, source_id = item.get("source"), item.get("id")
    source = source if isinstance(source, str) and re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", source) else None
    source_id = (
        source_id
        if isinstance(source_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", source_id)
        else None
    )
    if not wide:
        if source != "MED":
            return None
        pmid = _valid_pmid(item.get("pmid"))
        if item.get("pmid") is not None and not pmid:
            return None
        if source_id is not None and (not _valid_pmid(source_id) or (pmid and pmid != source_id)):
            return None
        return Hit(pmid=pmid or source_id) if pmid or source_id else None
    raw_pmid, raw_pmcid = item.get("pmid"), item.get("pmcid")
    pmid = _identifier("pmid", raw_pmid)
    pmcid = _identifier("pmcid", raw_pmcid)
    native_pmid = _identifier("pmid", source_id) if source == "MED" else None
    native_pmcid = _identifier("pmcid", source_id) if source == "PMC" else None
    conflict = bool(
        (native_pmid and raw_pmid is not None and pmid != native_pmid)
        or (native_pmcid and raw_pmcid is not None and pmcid != native_pmcid)
    )
    return Hit(
        pmid=pmid or native_pmid,
        doi=_identifier("doi", item.get("doi")),
        pmcid=pmcid or native_pmcid,
        source=source,
        source_id=source_id,
        conflict=conflict,
    )


def _nonempty_messages(value):
    return any(value.values()) if isinstance(value, dict) else bool(value)


def _capture(request, provider, branch, fetch):
    payload, receipt, problem = _load(fetch, request.url(provider, branch))
    if problem:
        return Lane(provider, branch, "unavailable", None, None, warnings=(problem,), **receipt)
    warnings = []
    translated = None
    if provider == "pubmed":
        result = payload.get("esearchresult", {})
        result = result if isinstance(result, dict) else {}
        count, slots = result.get("count"), result.get("idlist")
        if any(
            _nonempty_messages(v)
            for v in (
                payload.get("error"),
                payload.get("ERROR"),
                result.get("error"),
                result.get("errorlist"),
            )
        ):
            warnings.append("query_error")
        notices = result.get("warninglist")
        # ESearch places its normal empty-result notice in the warning envelope.
        # A field/phrase warning, an unknown message, or a populated list still matters.
        empty_notice = (
            isinstance(notices, dict)
            and count in ("0", 0)
            and type(count) in (str, int)
            and slots == []
            and {k: v for k, v in notices.items() if v} == {"outputmessages": ["No items found."]}
        )
        if _nonempty_messages(notices) and not empty_notice:
            warnings.append("query_warning")
        translated = result.get("querytranslation")
    else:
        count = payload.get("hitCount")
        result = payload.get("resultList")
        slots = result.get("result") if isinstance(result, dict) else None
        if payload.get("error") or payload.get("errCode"):
            warnings.append("query_error")

    total = int(count) if type(count) in (str, int) and re.fullmatch("[0-9]{1,12}", str(count)) else None
    diagnosis = bool(warnings)
    seen = set()
    hits = []
    invalid = duplicate = 0
    length = None
    if isinstance(slots, list):
        length = len(slots)
        if len(slots) > request.depth:
            warnings.append("excess_slots")
        for rank, value in enumerate(slots[: request.depth], 1):
            if provider == "pubmed":
                uid = _valid_pmid(value)
                hit = Hit(pmid=uid, source="MED", source_id=uid) if uid else None
            else:
                hit = _epmc_hit(value, request.profile == "all_indexed")
            if hit is None:
                invalid += 1
                continue
            # Wide snapshots preserve repeated occurrences and their native positions.
            identity = hit if request.profile == "all_indexed" else hit.pmid
            identified = hit.pmid or hit.pmcid or hit.doi or (hit.source and hit.source_id)
            if identified and identity in seen:
                duplicate += 1
                if request.profile == "pmid_indexed":
                    continue
            seen.add(identity)
            hits.append((rank, hit))
    else:
        warnings.append("missing_slots")
    if total is None:
        warnings.append("invalid_total")
    elif length is not None and length != min(total, request.depth):
        warnings.append("inconsistent_page")
    if invalid:
        warnings.append("unusable_slot")
    if duplicate:
        warnings.append("duplicate_slot")
    if not warnings:
        status = "ok"
    elif total is not None or hits or diagnosis:
        status = "partial"
    else:
        status = "unavailable"
    return Lane(
        provider,
        branch,
        status,
        total,
        length,
        tuple(hits),
        warnings=tuple(warnings),
        duplicate_slots=duplicate,
        unusable_slots=invalid,
        excess_slots=max(0, (length or 0) - request.depth),
        query_translation=translated,
        **receipt,
    )


def _hydrate(lanes, fetch):
    """ESearch remains authoritative for membership even if summary acquisition fails."""
    uids = list(dict.fromkeys(hit.pmid for lane in lanes for _, hit in lane.hits))
    enriched = {}
    receipts = []
    for start in range(0, len(uids), 50):
        batch = uids[start : start + 50]
        params = dict(db="pubmed", id=",".join(batch), retmode="json", tool="medextract_evidence_dossier")
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urlencode(params)
        payload, receipt, problem = _load(fetch, url)
        by_id = (payload or {}).get("result")
        resolved = 0
        conflicts = {}
        for uid in batch:
            item = by_id.get(uid) if isinstance(by_id, dict) else None
            if not isinstance(item, dict) or item.get("uid") != uid or item.get("error"):
                continue
            raw_ids = item.get("articleids")
            if not isinstance(raw_ids, list):
                continue
            identifiers = {"doi": set(), "pmc": set(), "pubmed": {uid}}
            kinds = {"pmc": "pmcid", "pubmed": "pmid", "doi": "doi"}
            for entry in raw_ids:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("idtype"), str)
                    and entry["idtype"] in identifiers
                ):
                    kind = entry["idtype"]
                    value = _identifier(kinds[kind], entry.get("value"))
                    if value:
                        identifiers[kind].add(value)
            incompatible = {kinds[k]: sorted(v) for k, v in identifiers.items() if len(v) > 1}
            if incompatible:
                conflicts[uid] = incompatible
            enriched[uid] = Hit(
                pmid=uid,
                source="MED",
                source_id=uid,
                conflict=bool(incompatible),
                doi=next(iter(identifiers["doi"]))
                if len(identifiers["doi"]) == 1 and "pmid" not in incompatible
                else None,
                pmcid=next(iter(identifiers["pmc"]))
                if len(identifiers["pmc"]) == 1 and "pmid" not in incompatible
                else None,
            )
            resolved += 1
        receipts.append(
            receipt
            | {
                "pmids": batch,
                "resolved": resolved,
                "status": "ok" if resolved == len(batch) else "partial" if resolved else "unavailable",
                "warning": problem,
                "identifier_conflicts": conflicts,
            }
        )
    updated = []
    for lane in lanes:
        if not lane.hits:
            updated.append(lane)
            continue
        hits = tuple((rank, enriched.get(hit.pmid, hit)) for rank, hit in lane.hits)
        done = sum(hit.pmid in enriched for _, hit in lane.hits)
        complete = done == len(hits)
        updated.append(
            replace(
                lane,
                hits=hits,
                status=lane.status if complete else "partial",
                metadata_status="ok" if complete else "partial" if done else "unavailable",
            )
        )
    return updated, receipts


def collect_discovery(request: DiscoveryRequest, *, fetch=None) -> DiscoverySnapshot:
    """Capture one frozen set. Paging a returned snapshot never performs I/O."""
    if not isinstance(request, DiscoveryRequest):
        raise ValueError("Supply a validated discovery request.")
    requested = [key for key in LANES if request.include_title_search or key[1] == "broad"]
    lanes, receipts = capture_lanes(request, requested, fetch=fetch)
    return build_snapshot(request, lanes, metadata_requests=receipts)


def capture_lanes(request, requested, *, fetch=None):
    """Acquire exactly the selected search pages, hydrating only fresh PubMed hits."""
    fetch = literature_http.fetch_bytes if fetch is None else fetch

    def capture(provider):
        return [_capture(request, p, branch, fetch) for p, branch in requested if p == provider]

    receipts = []
    with literature_http.search_slots, ThreadPoolExecutor(max_workers=1) as worker:
        epmc = worker.submit(capture, "epmc")
        pubmed = capture("pubmed")
        if request.profile == "all_indexed":
            pubmed, receipts = _hydrate(pubmed, fetch)
    return [*pubmed, *epmc.result()], receipts
