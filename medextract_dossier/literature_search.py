"""Bounded bibliographic discovery through Europe PMC and PubMed."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode

from . import literature_http


_EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_ENTREZ = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
_IDS = ("doi", "pmid", "pmcid")
_FLAG = re.compile(r"retract|withdraw|correct|errat|expression of concern", re.I)


def _query_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{name} must be a nonempty string of at most 512 characters.")
    if not value.isprintable():
        raise ValueError(f"{name} must contain printable text.")
    return value.strip()


def _mapping(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Missing metadata object.")
    return value


def _array(value) -> list:
    if not isinstance(value, list):
        raise ValueError("Missing metadata list.")
    return value


def _fetch(endpoint: str, **params) -> dict:
    data = literature_http.fetch_bytes(endpoint + "?" + urlencode(params))
    return _mapping(json.loads(data))


def _count(value) -> int:
    if not re.fullmatch(r"[0-9]{1,12}", str(value)):
        raise ValueError("Missing provider hit count.")
    return int(value)


def _text(value, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = unescape(re.sub(r"</?(?:i|b|em|strong|sup|sub)>", "", value, flags=re.I))
    return " ".join(cleaned.split())[:maximum] or None


def _identifier(kind: str, value) -> str | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    value = value.strip()
    if kind == "doi":
        if re.match(r"https?://(?:dx\.)?doi\.org/", value, re.I):
            value = unquote(value.split("/", 3)[-1])
        value = re.sub(r"^doi:\s*", "", value, flags=re.I).lower()
        return value if re.fullmatch(r"10\.[0-9]{4,9}/\S+", value) and value.isprintable() else None
    if kind == "pmid":
        match = re.fullmatch(r"(?:PMID:\s*)?0*([1-9][0-9]{0,9})", value, re.I)
        return match[1] if match else None
    match = re.fullmatch(r"(?:PMCID:\s*)?PMC0*([1-9][0-9]{0,9})", value, re.I)
    return "PMC" + match[1] if match else None


def _record(provider, rank, title, journal, date, types, doi, pmid, pmcid, fallback_url=None):
    if isinstance(types, str):
        types = types.split(";")
    types = types if isinstance(types, list) else []
    publication_types = []
    for item in types[:32]:
        label = _text(item, 128)
        if label and label not in publication_types:
            publication_types.append(label)
    flags = [label for label in publication_types if _FLAG.search(label)]
    doi = _identifier("doi", doi)
    pmid = _identifier("pmid", pmid)
    pmcid = _identifier("pmcid", pmcid)
    if doi:
        url = "https://doi.org/" + quote(doi, safe="/")
    elif pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    elif pmcid:
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    else:
        url = fallback_url
    return {
        "title": _text(title, 1500), "journal": _text(journal, 300),
        "publication_date": _text(date, 64), "publication_types": publication_types,
        "publication_flags": flags, "doi": doi, "pmid": pmid, "pmcid": pmcid, "url": url,
        "providers": [provider], "provider_ranks": {provider: rank},
    }


def _europe_pmc(query: str, limit: int, report: dict) -> list:
    payload = _fetch(_EUROPE_PMC, query=query, format="json", resultType="lite", pageSize=limit)
    report["total_hits"] = _count(payload.get("hitCount"))
    hits = _array(_mapping(payload.get("resultList")).get("result"))
    records = []
    for rank, hit in enumerate(hits[:limit], start=1):
        if not isinstance(hit, dict) or not _text(hit.get("title"), 1500):
            continue
        source = _text(hit.get("source"), 16)
        source_id = _text(hit.get("id"), 128)
        url = None
        if source and source_id and re.fullmatch(r"[A-Z0-9]+", source) and re.fullmatch(r"[A-Za-z0-9][\w.-]*", source_id):
            url = f"https://europepmc.org/article/{source}/{source_id}"
        pmid = hit.get("pmid") or (source_id if source == "MED" else None)
        pmcid = hit.get("pmcid") or (source_id if source == "PMC" else None)
        records.append(_record(
            "europe_pmc", rank, hit["title"], hit.get("journalTitle"),
            hit.get("firstPublicationDate") or hit.get("pubYear"), hit.get("pubType"),
            hit.get("doi"), pmid, pmcid, url,
        ))
    return records


def _pubmed(query: str, limit: int, report: dict) -> list:
    search = _fetch(_ENTREZ + "esearch.fcgi", db="pubmed", term=query, retmode="json",
                    sort="relevance", retmax=limit, tool="medextract_evidence_dossier")
    result = _mapping(search.get("esearchresult"))
    report["total_hits"] = _count(result.get("count"))
    for field in ("errorlist", "warninglist"):
        messages = result.get(field)
        if isinstance(messages, dict):
            report[field] = {
                str(kind)[:80]: [_text(item, 200) for item in items[:8] if isinstance(item, str)]
                for kind, items in list(messages.items())[:8] if isinstance(items, list) and items
            }
    if report.get("errorlist"):
        report.update(status="partial", error="PubMed reported an issue interpreting the query.")
    report["query_translation"] = _text(result.get("querytranslation"), 4096)
    ids = [_identifier("pmid", uid) for uid in _array(result.get("idlist"))[:limit]]
    uids = [uid for uid in ids if uid]
    if not uids:
        return []
    summary = _fetch(_ENTREZ + "esummary.fcgi", db="pubmed", id=",".join(uids),
                     retmode="json", tool="medextract_evidence_dossier")
    by_id = _mapping(summary.get("result"))
    records = []
    for rank, uid in enumerate(ids, start=1):
        hit = by_id.get(uid)
        if not uid or not isinstance(hit, dict) or not _text(hit.get("title"), 1500):
            continue
        if hit.get("error") or _identifier("pmid", hit.get("uid")) != uid:
            continue
        ids_field = hit.get("articleids")
        identifiers = {}
        for item in ids_field if isinstance(ids_field, list) else []:
            if isinstance(item, dict) and item.get("idtype") in ("doi", "pmc"):
                identifiers[item["idtype"]] = item.get("value")
        record = _record(
            "pubmed", rank, hit.get("title"), hit.get("fulljournalname") or hit.get("source"),
            hit.get("pubdate"), hit.get("pubtype"), identifiers.get("doi"), uid, identifiers.get("pmc"),
        )
        references = hit.get("references")
        if isinstance(references, list):
            for reference in references:
                label = _text(reference.get("reftype"), 128) if isinstance(reference, dict) else None
                if label and _FLAG.search(label) and label not in record["publication_flags"]:
                    record["publication_flags"].append(label)
                    if len(record["publication_flags"]) >= 64:
                        break
        records.append(record)
    return records


def _provider(search, query, limit):
    report = {"status": "ok", "limit": limit, "total_hits": None, "retrieved": 0, "error": None}
    try:
        records = search(query, limit, report)
    except (ValueError, OSError, RecursionError) as exc:
        # Provider error bodies and URLs can contain the user's query.
        if isinstance(exc, HTTPError):
            message = f"Provider returned HTTP {exc.code}."
        elif isinstance(exc, OSError):
            message = "Provider network request failed."
        else:
            message = "Provider returned invalid or incomplete data."
        report.update(status="unavailable", error=message)
        return report, []
    report["retrieved"] = len(records)
    if len(records) < min(limit, report["total_hits"]):
        report.update(status="partial", error="Some matching records lacked usable metadata.")
    return report, records


def _matching_ids(first, second):
    shared = [key for key in _IDS if first[key] and second[key]]
    return shared and all(first[key] == second[key] for key in shared)


def _combine(first, second):
    for field in (*_IDS, "url", "journal", "publication_date"):
        first[field] = first[field] or second[field]
    for field in ("providers", "publication_types", "publication_flags"):
        known = {value.casefold() for value in first[field]}
        for value in second[field]:
            if value.casefold() not in known:
                first[field].append(value)
                known.add(value.casefold())
    for provider, rank in second["provider_ranks"].items():
        first["provider_ranks"][provider] = min(first["provider_ranks"].get(provider, rank), rank)


def _merge(pages: list[list]) -> list:
    records = []
    ordered = sorted((entry for page in pages for entry in page),
                     key=lambda entry: min(entry["provider_ranks"].values()))
    for new in ordered:
        # A later citation can bridge a DOI-only and a PMID-only citation.
        matches = [entry for entry in records if _matching_ids(entry, new)]
        if not matches:
            records.append(new)
            continue
        first = matches[0]
        _combine(first, new)
        for other in matches[1:]:
            if _matching_ids(first, other):
                _combine(first, other)
                records.remove(other)
    return records


def _prioritize_identifier(query, records):
    prefix = query.lower()
    if prefix.startswith(("10.", "doi:", "https://", "http://")):
        kind = "doi"
    elif prefix.startswith("pmc"):
        kind = "pmcid"
    elif prefix.startswith("pmid:"):
        kind = "pmid"
    else:
        return None
    value = _identifier(kind, query)
    if value is None:
        return None
    # Keep the provider order for ties and for every related citation.
    records.sort(key=lambda record: record[kind] != value)
    return {"kind": kind, "value": value,
            "matches_in_fetched_pages": sum(record[kind] == value for record in records)}


def search_literature(question: str, search_terms: str | None = None, limit: int = 5) -> dict:
    """Search ranked first pages of public indexes for a public research question."""
    query = _query_text(question, "question")
    if search_terms is not None:
        query = _query_text(search_terms, "search_terms")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20.")

    # Run PubMed's dependent requests on the caller, using one worker for Europe PMC.
    # Joining it here also bounds the lifetime of requests when a caller exits early.
    with literature_http.search_slots, ThreadPoolExecutor(max_workers=1) as worker:
        europe_pmc = worker.submit(_provider, _europe_pmc, query, limit)
        pubmed = _provider(_pubmed, query, limit)

    providers = {}
    pages = []
    for name, (report, page) in (("europe_pmc", europe_pmc.result()), ("pubmed", pubmed)):
        total = report["total_hits"]
        report["more_than_limit"] = None if total is None else total > limit
        providers[name] = report
        pages.append(page)
    records = _merge(pages)
    identifier = _prioritize_identifier(query, records)
    statuses = {report["status"] for report in providers.values()}
    status = statuses.pop() if len(statuses) == 1 else "partial"
    response = {
        "question": question, "query": query, "status": status,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "limit": limit, "providers": providers,
        "unique_records_before_limit": len(records), "output_truncated": len(records) > limit,
        "records": records[:limit],
        "limitations": [
            "Only the first ranked page from each index was requested. Results alternate by provider rank before the final cap.",
            "Index coverage overlaps. Identifier matching groups citations. Separate publications can describe the same study.",
            "Publication flags use returned publication types and PubMed reference relationships. An empty list means status was not independently checked.",
            "Search relevance, study design and clinical applicability require review of the underlying publications.",
        ],
    }
    if identifier is not None:
        response["requested_identifier"] = identifier
        response["limitations"][0] = (
            "Only the first ranked page from each index was requested. Exact identifier matches in "
            "those pages appear first, followed by related records in provider rank order."
        )
    return response
