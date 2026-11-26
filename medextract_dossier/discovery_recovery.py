"""Recover timed-out searches from a fresh request or a matching saved response."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import re
from time import monotonic

from .discovery import (
    LANES,
    MAX_SNAPSHOT_BYTES,
    PMID_COLUMNS,
    RANKS,
    WIDE_COLUMNS,
    DiscoveryRequest,
    DiscoverySnapshot,
    Hit,
    Lane,
    _json,
    _public_translation,
    _screening_table,
    _snapshot_status,
    build_snapshot,
)
from .discovery_providers import capture_lanes
from .discovery_indexed import _row_url
from .literature_search import _identifier


_FETCH_WARNINGS = {
    "timeout",
    "network_error",
    "rate_limited",
    "http_error",
    "response_limit",
    "invalid_response",
}
_PAGE_WARNINGS = _FETCH_WARNINGS | {
    "query_error",
    "query_warning",
    "excess_slots",
    "invalid_total",
    "missing_slots",
    "inconsistent_page",
    "unusable_slot",
    "duplicate_slot",
}


class RecoveryError(ValueError):
    """An expected, public-safe refusal before transmitting a saved query."""


def _require(condition):
    if not condition:
        raise RecoveryError("The saved discovery export is invalid or inconsistent.")


def _valid_capture(receipt, warnings):
    if "http_status" in receipt:
        code = receipt["http_status"]
        _require(type(code) is int and 400 <= code <= 599)
        _require(warnings == ["rate_limited" if code == 429 else "http_error"])
        _require(receipt["response_bytes"] is None and receipt["status"] == "unavailable")
    timestamp = receipt["captured_at"]
    if timestamp is not None:
        _require(isinstance(timestamp, str) and re.fullmatch(r"[0-9T:Z+.\-]{10,40}", timestamp))
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        _require(parsed.utcoffset() is not None)
    size, digest = receipt["response_bytes"], receipt["response_sha256"]
    _require((size is None) == (digest is None))
    if size is not None:
        _require(type(size) is int and 0 <= size <= 2_000_000 and re.fullmatch(r"[0-9a-f]{64}", digest))


def _validate_metadata(receipts):
    """Only carry the fields and identifier claims emitted by the PubMed adapter."""
    _require(isinstance(receipts, list) and len(receipts) <= 4)
    for receipt in receipts:
        _require(
            isinstance(receipt, dict)
            and set(receipt) - {"http_status"}
            == {
                "captured_at",
                "response_bytes",
                "response_sha256",
                "pmids",
                "resolved",
                "status",
                "warning",
                "identifier_conflicts",
            }
        )
        _valid_capture(receipt, [receipt["warning"]])
        pmids = receipt["pmids"]
        _require(isinstance(pmids, list) and 1 <= len(pmids) <= 50)
        _require(all(isinstance(i, str) and re.fullmatch(r"[1-9][0-9]{0,9}", i) for i in pmids))
        _require(len(set(pmids)) == len(pmids))
        count = receipt["resolved"]
        _require(type(count) is int and 0 <= count <= len(pmids))
        status = "ok" if count == len(pmids) else "partial" if count else "unavailable"
        _require(receipt["status"] == status)
        _require(receipt["warning"] is None or (receipt["warning"] in _FETCH_WARNINGS and not count))
        conflicts = receipt["identifier_conflicts"]
        _require(isinstance(conflicts, dict) and conflicts.keys() <= set(pmids) and len(conflicts) <= count)
        for claims in conflicts.values():
            _require(
                isinstance(claims, dict) and bool(claims) and claims.keys() <= {"pmid", "pmcid", "doi"}
            )
            for kind, identifiers in claims.items():
                _require(isinstance(identifiers, list) and len(identifiers) >= 2)
                _require(all(isinstance(i, str) and _identifier(kind, i) == i for i in identifiers))
                _require(identifiers == sorted(set(identifiers)))


def _pmid_hits(tsv, depth):
    reader = csv.DictReader(io.StringIO(tsv, newline=""), delimiter="\t", strict=True)
    _require(reader.fieldnames == list(PMID_COLUMNS))
    hits = {key: [] for key in LANES}
    for index, row in enumerate(reader):
        _require(index < depth * len(LANES))
        uid = row["pmid"]
        _require(isinstance(uid, str) and re.fullmatch(r"[1-9][0-9]{0,9}", uid))
        for key, column in zip(LANES, RANKS):
            value = row[column]
            if value != "":
                _require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,2}", value))
                rank = int(value)
                _require(rank <= depth and len(hits[key]) < depth)
                hits[key].append((rank, Hit(pmid=uid)))
    return hits


def _wide_hits(native_hits, requested, depth):
    if native_hits is None:
        raise RecoveryError(
            "This all_indexed export lacks native identity claims. Use an export that retains them."
        )
    _require(isinstance(native_hits, dict) and set(native_hits) == {"_".join(k) for k in requested})
    hits = {}
    for key in requested:
        records = native_hits["_".join(key)]
        _require(isinstance(records, list) and len(records) <= depth)
        items = []
        for record in records:
            _require(isinstance(record, dict))
            rank = record["rank"]
            hit = Hit(**{k: v for k, v in record.items() if k != "rank"})
            _require(type(rank) is int and 1 <= rank <= depth and type(hit.conflict) is bool)
            for kind in ("pmid", "doi", "pmcid"):
                value = getattr(hit, kind)
                _require(value is None or (isinstance(value, str) and _identifier(kind, value) == value))
            _require(hit.source is None or re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", hit.source))
            _require(
                hit.source_id is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", hit.source_id)
            )
            items.append((rank, hit))
        hits[key] = items
    return hits


def _legacy_wide_positions(tsv, requested, depth, count):
    """Check a screening-only export without attributing merged identities to a donor."""
    reader = csv.DictReader(io.StringIO(tsv, newline=""), delimiter="\t", strict=True)
    _require(reader.fieldnames == list(WIDE_COLUMNS))
    hits = {key: [] for key in requested}
    lane_names = {"_".join(key) for key in requested}
    rows = 0
    previous = None
    for rows, row in enumerate(reader, 1):
        _require(rows <= depth * len(requested) and row["row_id"] == f"r{rows:04}")
        _require(set(row) == set(WIDE_COLUMNS) and all(isinstance(v, str) for v in row.values()))
        _require(row["identity_status"] in ("resolved", "unresolved", "conflict"))
        for kind in ("pmid", "doi", "pmcid"):
            _require(not row[kind] or _identifier(kind, row[kind]) == row[kind])
        sources = json.loads(row["native_ids"])
        _require(
            isinstance(sources, list)
            and all(re.fullmatch(r"[A-Z][A-Z0-9]{0,15}:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", s) for s in sources)
            and len(sources) == len(set(sources))
        )
        _require(row["url"] == (_row_url(row, row["identity_status"], sources) or ""))
        identified = bool(row["pmid"] or row["doi"] or row["pmcid"] or sources)
        _require(identified == (row["identity_status"] != "unresolved"))
        positions = json.loads(row["native_positions"])
        _require(isinstance(positions, dict) and positions and positions.keys() <= lane_names)
        _require(all(positions.values()))
        first_positions = []
        for index, (key, column) in enumerate(zip(LANES, RANKS)):
            ranks = positions.get("_".join(key), [])
            _require(isinstance(ranks, list) and all(type(r) is int and 1 <= r <= depth for r in ranks))
            _require(ranks == sorted(set(ranks)) and row[column] == (str(ranks[0]) if ranks else ""))
            if ranks:
                first_positions.append((index // 2, ranks[0], index % 2))
                hits[key].extend((r, Hit(pmid=row["pmid"] or None)) for r in ranks)
        first = min(first_positions)
        _require(previous is None or previous < first)
        previous = first
    _require(rows == count)
    return hits


def _check_original_hydration(lanes, metadata, *, legacy):
    """Validate claims shared across the PubMed batches of a single original capture."""
    confirmed, unavailable, flagged = set(), set(), set()
    for lane in lanes:
        if lane.provider != "pubmed":
            continue
        for _, hit in lane.hits:
            _require(hit.pmid is not None)
            if not legacy:
                _require(hit.source == "MED" and hit.source_id == hit.pmid)
            if lane.metadata_status == "ok" or hit.conflict or hit.doi or hit.pmcid:
                confirmed.add(hit.pmid)
            if lane.metadata_status == "unavailable":
                unavailable.add(hit.pmid)
            if hit.conflict:
                flagged.add(hit.pmid)
    _require(confirmed.isdisjoint(unavailable))
    observed, conflicts = set(), set()
    for receipt in metadata:
        batch = set(receipt["pmids"])
        _require(observed.isdisjoint(batch))
        observed.update(batch)
        conflicts.update(receipt["identifier_conflicts"])
        _require(len(confirmed & batch) <= receipt["resolved"] <= len(batch - unavailable))
    _require(confirmed <= observed and (legacy or conflicts == flagged))


def _validate_automatic_receipt(data, lanes):
    receipt = data["automatic_recovery"]
    _require(not {"reuse", "recovery"}.intersection(data) and isinstance(receipt, dict))
    parent = receipt["parent_search_id"]
    _require(isinstance(parent, str) and re.fullmatch(r"[0-9a-f]{64}", parent))
    for field in ("max_gap_seconds", "donors_considered", "candidate_count_before", "candidate_count_after",
                  "candidate_count_added", "request_count"):
        _require(type(receipt[field]) is int and receipt[field] >= 0)
    _require(receipt["candidate_count_added"] <= receipt["candidate_count_after"] == data["candidate_count"])
    fetch_missing = receipt["fetch_missing"]
    _require(type(fetch_missing) is bool)
    anchor = receipt["lookup_at"]
    if anchor is not None:
        _require(isinstance(anchor, str) and anchor != "now")
        _require(_lookup_time(anchor, False).isoformat(timespec="microseconds") == anchor)
    entries = receipt["lanes"]
    by_name = {f"{lane.provider}_{lane.branch}": lane for lane in lanes if lane.status != "not_requested"}
    _require(isinstance(entries, list) and 1 <= len(entries) <= len(by_name))
    seen = set()
    retried = []
    for entry in entries:
        _require(isinstance(entry, dict))
        name, match, action = entry["lane"], entry["match"], entry["action"]
        _require(name in by_name and name not in seen)
        seen.add(name)
        count, donor = entry["eligible_donor_count"], entry["donor_search_id"]
        _require(type(count) is int and 0 <= count <= receipt["donors_considered"])
        if match == "selected":
            _require(action == "reused" and count > 0 and by_name[name].status == "ok")
            _require(isinstance(donor, str) and donor != parent and re.fullmatch(r"[0-9a-f]{64}", donor))
        else:
            _require((match == "missing" and count == 0) or (match == "ambiguous" and count >= 2))
            _require(donor is None and action == ("retried" if fetch_missing else "retained"))
            if fetch_missing:
                retried.append(by_name[name])
            else:
                _require(by_name[name].timed_out)
    # Wide PubMed retries issue one bounded summary request per 50 fresh, unique PMIDs.
    pmids = {hit.pmid for lane in retried if lane.provider == "pubmed" for _, hit in lane.hits}
    summaries = (len(pmids) + 49) // 50 if data["profile"] == "all_indexed" else 0
    _require(receipt["request_count"] == len(retried) + summaries)


def _restore_lane(request, receipt, hits, key, requested):
    _require(isinstance(receipt, dict) and (receipt["provider"], receipt["branch"]) == key)
    _require(receipt["parameters"] == (request.params(*key) if requested else None))
    status = receipt["status"]
    _require(status in ("ok", "partial", "unavailable") if requested else status == "not_requested")
    slots, total = receipt["native_slots"], receipt["total_hits"]
    for value in (slots, total):
        _require(value is None or (type(value) is int and value >= 0))
    ranks = [rank for rank, _ in hits]
    _require(len(set(ranks)) == len(ranks) and max(ranks, default=0) <= (slots or 0))
    _require(status not in ("not_requested", "unavailable") or not hits)
    warnings = receipt["warnings"]
    _require(
        isinstance(warnings, list) and all(isinstance(w, str) and w in _PAGE_WARNINGS for w in warnings)
    )
    _valid_capture(receipt, warnings)
    digest = receipt["capture_file_sha256"]
    _require(digest is None or re.fullmatch(r"[0-9a-f]{64}", digest))
    _require(receipt["metadata_status"] in (None, "ok", "partial", "unavailable"))
    if key[0] != "pubmed" or request.profile != "all_indexed" or not hits:
        _require(receipt["metadata_status"] is None)
    translation = receipt["query_translation"]
    _require(_public_translation(translation)[0] == translation)
    lane = Lane(
        *key,
        status,
        total,
        slots,
        tuple(sorted(hits, key=lambda pair: pair[0])),
        captured_at=receipt["captured_at"] or "",
        response_bytes=receipt["response_bytes"],
        response_sha256=receipt["response_sha256"],
        capture_file_sha256=receipt["capture_file_sha256"],
        query_translation=translation,
        warnings=tuple(warnings),
        duplicate_slots=receipt["duplicate_slots"],
        unusable_slots=receipt["unusable_slots"],
        metadata_status=receipt["metadata_status"],
        excess_slots=receipt.get("excess_slots", 0),
        http_status=receipt.get("http_status"),
    )
    for n in (lane.duplicate_slots, lane.unusable_slots, lane.excess_slots):
        _require(type(n) is int and 0 <= n <= (slots or 0))
    if status == "ok":
        _require(not warnings and total is not None and slots == min(total, request.depth))
    if status == "not_requested":
        _require(_json(lane.receipt(request)) == _json(Lane(*key, status, None, None).receipt(request)))
    if "timeout" in warnings:
        _require(lane.timed_out and slots is None and total is None and lane.response_bytes is None)
    # Early releases omitted these two fields. Preserve the original receipt,
    # including whether translation sanitization/truncation had already happened.
    expected = lane.receipt(request)
    if "query_translation_status" in receipt:
        allowed = ("not_provided", "omitted") if translation is None else ("complete", "truncated")
        if receipt["query_translation_status"] == "truncated":
            _require(isinstance(translation, str) and len(translation) == 4096)
        _require(receipt["query_translation_status"] in allowed)
        expected["query_translation_status"] = receipt["query_translation_status"]
    for name in ("query_translation_status", "excess_slots"):
        if name not in receipt:
            del expected[name]
    _require(_json(expected) == _json(receipt))
    count = receipt["usable_rows"] + (lane.duplicate_slots if request.profile == "all_indexed" else 0)
    _require(len(hits) == count)
    return lane


def _restore(manifest, tsv, *, require_timeout, allow_legacy_donor):
    _require(isinstance(manifest, str) and isinstance(tsv, str))
    _require(len(manifest.encode()) + len(tsv.encode()) <= MAX_SNAPSHOT_BYTES)
    data = json.loads(manifest)
    _require(isinstance(data, dict))
    parent_id = data.pop("search_id")
    if (
        sha256(_json(data).encode()).hexdigest() != parent_id
        or sha256(tsv.encode()).hexdigest() != data["candidates_sha256"]
    ):
        raise RecoveryError("The saved discovery manifest or candidates failed its integrity check.")
    request = DiscoveryRequest(
        question=data["question"],
        concept_groups=data["concept_groups"],
        depth=data["depth"],
        include_title_search=data["include_title_search"],
        until_year=data["until_year"],
        profile=data["profile"],
    )
    _require(
        type(data["schema_version"]) is int
        and data["schema_version"] == (1 if request.profile == "pmid_indexed" else 2)
    )
    _require(
        data["order"] == "broad_first_native_rank_pubmed_first" and type(data["candidate_count"]) is int
    )
    requested = [key for key in LANES if key[1] == "broad" or request.include_title_search]
    receipts = data["lanes"]
    _require(isinstance(receipts, list) and len(receipts) == len(LANES))
    if require_timeout and not any(
        isinstance(r, dict) and r.get("status") == "unavailable" and r.get("warnings") == ["timeout"]
        for r in receipts
    ):
        raise RecoveryError("No requested provider search timed out in this snapshot.")
    legacy = allow_legacy_donor and request.profile == "all_indexed" and "native_hits" not in data
    if request.profile == "pmid_indexed":
        _require(data["metadata_requests"] == [])
        hits = _pmid_hits(tsv, request.depth)
    else:
        hits = (
            _legacy_wide_positions(tsv, requested, request.depth, data["candidate_count"])
            if legacy else _wide_hits(data.get("native_hits"), requested, request.depth)
        )
        _validate_metadata(data["metadata_requests"])
        known_pmids = {hit.pmid for key in requested if key[0] == "pubmed" for _, hit in hits[key]}
        _require(all(set(receipt["pmids"]) <= known_pmids for receipt in data["metadata_requests"]))
    lanes = [
        _restore_lane(request, receipt, hits.get(key, []), key, key in requested)
        for key, receipt in zip(LANES, receipts)
    ]
    enabled = {(lane.provider, lane.branch): lane for lane in lanes if lane.status != "not_requested"}
    _require(_snapshot_status(enabled.values()) == data["status"])
    if "automatic_recovery" in data:
        _validate_automatic_receipt(data, lanes)
    if request.profile == "all_indexed" and not {"reuse", "recovery", "automatic_recovery"}.intersection(data):
        _check_original_hydration(lanes, data["metadata_requests"], legacy=legacy)
    if legacy:
        # There is no way to recover the original per-provider claims from these rows.
        return data, parent_id, request, None
    # Round-tripping the original table checks row order, every rank and URL,
    # and wide merges including duplicate and conflict positions.
    rows, restored_tsv = _screening_table(request.profile, enabled)
    _require(
        restored_tsv == tsv
        and len(rows) == data["candidate_count"]
    )
    return data, parent_id, request, lanes


def _read_snapshot(manifest, tsv, *, require_timeout, allow_legacy_donor=False):
    try:
        return _restore(manifest, tsv, require_timeout=require_timeout, allow_legacy_donor=allow_legacy_donor)
    except RecoveryError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError, csv.Error, OverflowError) as exc:
        raise RecoveryError("The saved discovery export is invalid or inconsistent.") from exc


def recover_discovery(manifest: str, tsv: str, *, fetch=None) -> DiscoverySnapshot:
    """Retry each unavailable, timed-out lane once and return a linked snapshot."""
    data, parent_id, request, lanes = _read_snapshot(manifest, tsv, require_timeout=True)
    retry = [(lane.provider, lane.branch) for lane in lanes if lane.timed_out]
    retained = {key: receipt for key, receipt in zip(LANES, data["lanes"]) if key not in retry}
    started = monotonic()
    fresh, metadata = capture_lanes(request, retry, fetch=fetch)
    elapsed = monotonic() - started
    prior = [lane for lane in lanes if not lane.timed_out and lane.status != "not_requested"]
    return build_snapshot(
        request,
        [*prior, *fresh],
        retained_receipts=retained,
        metadata_requests=[*data["metadata_requests"], *metadata],
        recovery={
            "parent_search_id": parent_id,
            "retried_lanes": ["_".join(key) for key in retry],
            "candidate_count_before": data["candidate_count"],
            "request_count": len(retry) + len(metadata),
            "elapsed_seconds": round(elapsed, 6),
        },
    )


def _eligible_gap(target, donor, target_receipt, donor_receipt, profile, max_gap_seconds, lookup_at=None):
    if not target.timed_out or donor.status != "ok":
        return None
    if target_receipt["parameters"] != donor_receipt["parameters"]:
        return None
    if not donor.captured_at or not donor.response_bytes or (lookup_at is None and not target.captured_at):
        return None
    if donor.duplicate_slots or donor.unusable_slots or donor.excess_slots:
        return None
    if profile == "all_indexed" and donor.provider == "pubmed" and donor.hits and donor.metadata_status != "ok":
        return None
    # Compare provider capture times, including fractional seconds. An integer
    # comparison remains exact at the inclusive boundary for every allowed date.
    captured = datetime.fromisoformat(donor.captured_at)
    if lookup_at is not None and captured > lookup_at:
        return None
    anchor = lookup_at if lookup_at is not None else datetime.fromisoformat(target.captured_at)
    gap = abs(captured - anchor) // timedelta(microseconds=1)
    return gap if gap <= max_gap_seconds * 1_000_000 else None


def reuse_discovery(
    parent_manifest: str, parent_tsv: str, donor_manifest: str, donor_tsv: str, *, max_gap_seconds: int
) -> DiscoverySnapshot:
    """Fill timed-out lanes from one explicitly selected export, entirely offline."""
    if type(max_gap_seconds) is not int or max_gap_seconds < 0:
        raise RecoveryError("Supply a nonnegative integer capture window in seconds.")
    parent, parent_id, request, targets = _read_snapshot(parent_manifest, parent_tsv, require_timeout=True)
    donor, donor_id, donor_request, sources = _read_snapshot(donor_manifest, donor_tsv, require_timeout=False)
    if request.profile != donor_request.profile:
        raise RecoveryError("The two discovery exports must use the same search profile.")

    lanes = []
    receipts = {}
    reused = []
    pairs = zip(LANES, targets, sources, parent["lanes"], donor["lanes"], strict=True)
    for key, target, source, original, saved in pairs:
        if _eligible_gap(target, source, original, saved, request.profile, max_gap_seconds) is not None:
            target, original = source, saved
            reused.append("_".join(key))
        receipts[key] = original
        if target.status != "not_requested":
            lanes.append(target)
    if not reused:
        raise RecoveryError("No successful matching provider response falls within the requested capture window.")
    # Donor PubMed summary batches can span both branches. Link to that export
    # for its original metadata receipts without importing unrelated records.
    return build_snapshot(
        request,
        lanes,
        retained_receipts=receipts,
        metadata_requests=parent["metadata_requests"],
        reuse={
            "parent_search_id": parent_id,
            "donor_search_id": donor_id,
            "max_gap_seconds": max_gap_seconds,
            "reused_lanes": reused,
            "candidate_count_before": parent["candidate_count"],
            "request_count": 0,
        },
    )


def _lookup_time(value, fetch_missing):
    if value is None:
        return None
    if value == "now":
        return datetime.now(timezone.utc)
    if fetch_missing:
        raise RecoveryError("A fixed lookup time requires offline recovery.")
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9T:Z+.\- ]{10,40}", value):
            raise ValueError
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is None:
            raise ValueError
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecoveryError("Supply a timezone-aware ISO lookup time or 'now'.") from None


def _local_metadata(data, lane_name):
    """Identify the local summary batches attributable to this particular PubMed branch."""
    metadata = data["metadata_requests"]
    if "reuse" in data:
        return None
    if "automatic_recovery" in data:
        followup = data["automatic_recovery"]
        _require(isinstance(followup, dict) and isinstance(followup.get("lanes"), list))
        entries = followup["lanes"]
        _require(all(isinstance(entry, dict) for entry in entries))
        retried = [entry.get("lane") for entry in entries if entry.get("action") == "retried"]
    elif "recovery" in data:
        followup = data["recovery"]
        _require(isinstance(followup, dict) and isinstance(followup.get("retried_lanes"), list))
        retried = followup["retried_lanes"]
    else:
        return metadata
    if lane_name not in retried:
        # An inherited lane can refer to a different export, even when the local
        # batch happens to contain precisely the same PMID for another branch.
        return None
    requests = followup.get("request_count")
    _require(type(requests) is int and 0 <= requests - len(retried) <= len(metadata))
    fresh_batches = requests - len(retried)
    return metadata[-fresh_batches:] if fresh_batches else []


def _lane_content(source, donor, lookup_at):
    """Compare native observations, ignoring unrelated batches, capture hashes and timestamps."""
    conflicts = {}
    if source.metadata_status == "ok":
        metadata = _local_metadata(donor, f"{source.provider}_{source.branch}")
        if metadata is None:
            if lookup_at is not None or any(hit.conflict for _, hit in source.hits):
                return None
            return source.total_hits, source.hits, conflicts
        pmids = {hit.pmid for _, hit in source.hits}
        observed = set()
        for receipt in metadata:
            overlap = pmids.intersection(receipt["pmids"])
            if not overlap:
                continue
            if not receipt["response_bytes"] or receipt["resolved"] < len(overlap):
                return None
            if lookup_at is not None and (
                not receipt["captured_at"] or datetime.fromisoformat(receipt["captured_at"]) > lookup_at
            ):
                return None
            for uid in overlap:
                claims = receipt["identifier_conflicts"].get(uid)
                if uid in observed and conflicts.get(uid) != claims:
                    return None
                observed.add(uid)
                if claims:
                    conflicts[uid] = claims
        if observed != pmids:
            return None
        if any(hit.conflict != (hit.pmid in conflicts) for _, hit in source.hits):
            return None
    return source.total_hits, source.hits, conflicts


@dataclass
class _LaneMatches:
    count: int = 0
    ambiguous: bool = False
    content: tuple | None = None
    order: tuple | None = None
    source: Lane | None = None
    receipt: dict | None = None

    def consider(self, order, source, receipt, content):
        self.count += 1
        if self.order is not None and self.content != content:
            self.ambiguous = True
        if self.order is None or order < self.order:
            self.order, self.source, self.receipt, self.content = order, source, receipt, content


def auto_recover_discovery(
    parent_manifest: str, parent_tsv: str, donors, *, max_gap_seconds: int,
    fetch_missing: bool = False, fetch=None, lookup_at: str | None = None,
) -> DiscoverySnapshot:
    """Reuse agreeing local responses per timeout, optionally fetching each unresolved page once."""
    if type(max_gap_seconds) is not int or max_gap_seconds < 0:
        raise RecoveryError("Supply a nonnegative integer capture window in seconds.")
    if type(fetch_missing) is not bool:
        raise RecoveryError("Choose a boolean fetch_missing setting.")
    anchor = _lookup_time(lookup_at, fetch_missing)
    parent, parent_id, request, targets = _read_snapshot(parent_manifest, parent_tsv, require_timeout=True)
    choices = {i: _LaneMatches() for i, lane in enumerate(targets) if lane.timed_out}
    seen = {parent_id}
    # Consume and check every pair before dispatch. Retain only one page per
    # timed-out branch regardless of the number of eligible exports.
    for pair in donors:
        _require(isinstance(pair, (tuple, list)) and len(pair) == 2)
        donor, donor_id, donor_request, sources = _read_snapshot(
            *pair, require_timeout=False, allow_legacy_donor=True
        )
        if donor_id in seen:
            continue
        seen.add(donor_id)
        if sources is None or donor_request.profile != request.profile:
            continue
        for i, choice in choices.items():
            receipt = donor["lanes"][i]
            gap = _eligible_gap(
                targets[i], sources[i], parent["lanes"][i], receipt, request.profile, max_gap_seconds, anchor
            )
            if gap is not None:
                content = _lane_content(sources[i], donor, anchor)
                if content is not None:
                    choice.consider((gap, donor_id), sources[i], receipt, content)

    retained = dict(zip(LANES, parent["lanes"], strict=True))
    lanes = {key: lane for key, lane in zip(LANES, targets, strict=True) if lane.status != "not_requested"}
    decisions = []
    retry = []
    for i, choice in choices.items():
        key = LANES[i]
        if choice.count and not choice.ambiguous:
            lanes[key], retained[key] = choice.source, choice.receipt
            match, action, donor_id = "selected", "reused", choice.order[1]
        else:
            match = "ambiguous" if choice.ambiguous else "missing"
            action, donor_id = "retained", None
            if fetch_missing:
                action = "retried"
                retry.append(key)
                del retained[key]
        decisions.append(dict(lane="_".join(key), match=match, action=action,
                              eligible_donor_count=choice.count, donor_search_id=donor_id))

    metadata = []
    if retry:
        fresh, metadata = capture_lanes(request, retry, fetch=fetch)
        lanes.update(((lane.provider, lane.branch), lane) for lane in fresh)
    return build_snapshot(
        request, lanes.values(), retained_receipts=retained,
        metadata_requests=[*parent["metadata_requests"], *metadata],
        automatic_recovery={
            "parent_search_id": parent_id, "max_gap_seconds": max_gap_seconds,
            "lookup_at": anchor.isoformat(timespec="microseconds") if anchor is not None else None,
            "fetch_missing": fetch_missing, "donors_considered": len(seen) - 1, "lanes": decisions,
            "candidate_count_before": parent["candidate_count"], "request_count": len(retry) + len(metadata),
        },
    )
