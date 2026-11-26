"""Immutable, bounded snapshots of the ranked literature identifiers returned by a search."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlencode


PROVIDERS = ("pubmed", "epmc")
BRANCHES = ("broad", "anchored")
LANES = tuple((provider, branch) for branch in BRANCHES for provider in PROVIDERS)
RANKS = tuple(f"{p}_{b}_rank" for p, b in LANES)
ENDPOINTS = {
    "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
    "epmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
}
PMID_COLUMNS = ("pmid", "pubmed_url", *RANKS)
WIDE_COLUMNS = (
    "row_id",
    "pmid",
    "doi",
    "pmcid",
    "url",
    "native_ids",
    "identity_status",
    "native_positions",
    *RANKS,
)
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    question: str
    concept_groups: tuple[tuple[str, ...], ...]
    depth: int = 100
    include_title_search: bool = False
    until_year: int | None = None
    profile: str = "pmid_indexed"

    def __post_init__(self):
        if (
            not isinstance(self.question, str)
            or not self.question.strip()
            or not self.question.isprintable()
            or len(self.question) > 512
        ):
            raise ValueError("Supply a printable question of 1–512 characters.")
        object.__setattr__(self, "question", self.question.strip())
        if type(self.depth) is not int or not 1 <= self.depth <= 100:
            raise ValueError("Depth must be an integer from 1 to 100.")
        if type(self.include_title_search) is not bool:
            raise ValueError("Include title search must be boolean.")
        if self.profile not in ("pmid_indexed", "all_indexed"):
            raise ValueError("Choose the pmid_indexed or all_indexed profile.")
        if self.until_year is not None and (
            type(self.until_year) is not int or not 1800 <= self.until_year <= datetime.now(timezone.utc).year
        ):
            raise ValueError("Use a publication year from 1800 through the current UTC year.")
        if not isinstance(self.concept_groups, (list, tuple)) or not 2 <= len(self.concept_groups) <= 6:
            raise ValueError("Supply two to six ordered concept groups.")
        groups = []
        for group in self.concept_groups:
            if not isinstance(group, (list, tuple)) or not 1 <= len(group) <= 8:
                raise ValueError("Each concept group needs one to eight phrases.")
            for phrase in group:
                if (
                    not isinstance(phrase, str)
                    or not phrase.strip()
                    or len(phrase) > 96
                    or not phrase.isprintable()
                    or any(c in phrase for c in '"\\*?[]:')
                ):
                    raise ValueError(
                        "Use literal printable phrases of 1–96 characters without search syntax."
                    )
            groups.append(tuple(value.strip() for value in group))
        object.__setattr__(self, "concept_groups", tuple(groups))
        for p, b in LANES:
            if (b == "broad" or self.include_title_search) and len(self.url(p, b)) > 6000:
                raise ValueError("The encoded search exceeds 6,000 bytes. Use fewer or shorter phrases.")

    def params(self, provider: str, branch: str) -> dict:
        if (provider, branch) not in LANES:
            raise ValueError("Unknown discovery provider or search branch.")
        groups = []
        for index, synonyms in enumerate(self.concept_groups):
            terms = [f'"{phrase}"' for phrase in synonyms]
            if branch == "anchored":
                if provider == "pubmed":
                    field = "Title" if index == 0 else "Title/Abstract"
                    terms = [f"{term}[{field}]" for term in terms]
                else:
                    field = "TITLE" if index == 0 else "TITLE_ABS"
                    terms = [f"{field}:{term}" for term in terms]
            groups.append("(" + " OR ".join(terms) + ")")
        expression = " AND ".join(groups)
        if provider == "pubmed":
            params = dict(
                db="pubmed",
                term=expression,
                retmode="json",
                retmax=self.depth,
                sort="relevance",
                tool="medextract_evidence_dossier",
            )
            if self.until_year is not None:
                params.update(mindate="1800", maxdate=str(self.until_year), datetype="pdat")
        else:
            query = f"({expression})"
            if self.profile == "pmid_indexed":
                query += " AND SRC:MED"
            if self.until_year is not None:
                query += f" AND FIRST_PDATE:[1800-01-01 TO {self.until_year}-12-31]"
            params = dict(query=query, resultType="lite", format="json", pageSize=self.depth)
        return params

    def url(self, provider: str, branch: str) -> str:
        return ENDPOINTS[provider] + "?" + urlencode(self.params(provider, branch))


@dataclass(frozen=True, slots=True)
class Hit:
    pmid: str | None = None
    doi: str | None = None
    pmcid: str | None = None
    source: str | None = None
    source_id: str | None = None
    conflict: bool = False


def _public_translation(value) -> tuple[str | None, str]:
    if value is None:
        return None, "not_provided"
    # Keep public Boolean syntax including Title/Abstract, excluding remote URLs,
    # filesystem paths, addresses and credential assignments from both live and replay receipts.
    if (
        not isinstance(value, str)
        or not value.isprintable()
        or re.search(r'://|[=@\\]|(?:^|[\s"])/|\b(?:bearer|authorization)\b', value, re.I)
    ):
        return None, "omitted"
    return value[:4096], "truncated" if len(value) > 4096 else "complete"


@dataclass(frozen=True, slots=True)
class Lane:
    provider: str
    branch: str
    status: str
    total_hits: int | None
    native_slots: int | None
    hits: tuple[tuple[int, Hit], ...] = ()
    captured_at: str = ""
    response_bytes: int | None = None
    response_sha256: str | None = None
    capture_file_sha256: str | None = None
    query_translation: str | None = None
    warnings: tuple[str, ...] = ()
    duplicate_slots: int = 0
    unusable_slots: int = 0
    metadata_status: str | None = None
    excess_slots: int = 0
    http_status: int | None = None

    @property
    def timed_out(self) -> bool:
        return self.status == "unavailable" and self.warnings == ("timeout",)

    def receipt(self, request: DiscoveryRequest) -> dict:
        total, slots = self.total_hits, self.native_slots
        translation, translation_status = _public_translation(self.query_translation)
        return dict(
            provider=self.provider,
            branch=self.branch,
            status=self.status,
            parameters=request.params(self.provider, self.branch) if self.status != "not_requested" else None,
            captured_at=self.captured_at or None,
            total_hits=total,
            native_slots=slots,
            truncated=total > slots if total is not None and slots is not None else None,
            cap_reached=slots >= request.depth if slots is not None else None,
            short_page=slots < min(total, request.depth) if slots is not None and total is not None else None,
            usable_rows=slots - self.excess_slots - self.duplicate_slots - self.unusable_slots
            if slots is not None
            else 0,
            excess_slots=self.excess_slots,
            duplicate_slots=self.duplicate_slots,
            unusable_slots=self.unusable_slots,
            warnings=self.warnings,
            query_translation=translation,
            query_translation_status=translation_status,
            metadata_status=self.metadata_status,
            response_bytes=self.response_bytes,
            response_sha256=self.response_sha256,
            capture_file_sha256=self.capture_file_sha256,
        ) | ({"http_status": self.http_status} if self.http_status is not None else {})


def _pmid_rows(ordered) -> list[dict]:
    rows = {}
    for column, rank, hit in ordered:
        if hit.pmid not in rows:
            rows[hit.pmid] = dict.fromkeys(RANKS) | {
                "pmid": hit.pmid,
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{hit.pmid}/",
            }
        if rows[hit.pmid][column] is None:
            rows[hit.pmid][column] = rank
    return list(rows.values())


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    search_id: str
    tsv: str
    manifest: str
    _status: str
    _profile: str
    _rows: tuple[str, ...]
    _lane_summaries: str
    _views: tuple[tuple[int, ...], ...]
    _statuses: tuple[str, ...]
    _followup: str | None = None

    def page(self, offset=0, limit=10, view="screening") -> dict:
        if type(offset) is not int or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Use an integer offset and a page size from 1 to 20.")
        if view == "screening":
            indices, status = range(len(self._rows)), self._status
        elif isinstance(view, str) and f"{view}_rank" in RANKS:
            lane = RANKS.index(f"{view}_rank")
            indices, status = self._views[lane], self._statuses[lane]
        else:
            raise ValueError("Unknown discovery view. Use screening or a provider and branch.")
        count = len(indices)
        if not 0 <= offset <= count:
            raise ValueError("Offset exceeds this view or is negative.")
        next_offset = offset + limit if offset + limit < count else None
        return dict(
            search_id=self.search_id,
            view=view,
            status=status,
            offset=offset,
            view_count=count,
            next_offset=next_offset,
            rows=[json.loads(self._rows[index]) for index in indices[offset : offset + limit]],
        )

    def summary(self) -> dict:
        page = self.page()
        base = f"dossier://discovery/{self.search_id}/"
        return dict(
            schema_version=1 if self._profile == "pmid_indexed" else 2,
            search_id=self.search_id,
            profile=self._profile,
            status=self._status,
            candidate_count=len(self._rows),
            lanes=json.loads(self._lane_summaries),
            preview=page["rows"],
            next_offset=page["next_offset"],
            candidates_uri=base + "candidates.tsv",
            manifest_uri=base + "search.json",
        ) | (json.loads(self._followup) if self._followup else {})


def _encode_manifest(manifest):
    digest = sha256(_json(manifest).encode()).hexdigest()
    return digest, _json(manifest | {"search_id": digest}) + "\n"


def _screening_table(profile, by_key):
    """Render the same ranked table during capture and offline input validation."""
    ordered = []
    for priority, key in enumerate(LANES):
        if key in by_key:
            for rank, hit in by_key[key].hits:
                ordered.append((priority // 2, rank, priority % 2, RANKS[priority], hit))
    ordered.sort(key=lambda item: item[:3])
    hits = [(col, rank, hit) for _, rank, _, col, hit in ordered]
    if profile == "pmid_indexed":
        rows, columns = _pmid_rows(hits), PMID_COLUMNS
    else:
        from .discovery_indexed import indexed_rows

        rows, columns = indexed_rows(hits), WIDE_COLUMNS
    tsv = io.StringIO(newline="")
    writer = csv.DictWriter(tsv, fieldnames=columns, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _json(v) if isinstance(v, (list, dict)) else v for k, v in row.items()})
    return rows, tsv.getvalue()


def _snapshot_status(lanes):
    statuses = {lane.status for lane in lanes}
    return statuses.pop() if len(statuses) == 1 else "partial"


def build_snapshot(request: DiscoveryRequest, lanes, *, metadata_requests=(), retained_receipts=None,
                   recovery=None, reuse=None, automatic_recovery=None) -> DiscoverySnapshot:
    by_key = {(lane.provider, lane.branch): lane for lane in lanes}
    expected = {k for k in LANES if request.include_title_search or k[1] == "broad"}
    if set(by_key) != expected:
        raise ValueError("Missing or unexpected provider branches.")
    rows, contents = _screening_table(request.profile, by_key)
    status = _snapshot_status(by_key.values())
    receipts = []
    for key in LANES:
        if retained_receipts and key in retained_receipts:
            receipts.append(retained_receipts[key])
        else:
            lane = by_key.get(key) or Lane(*key, "not_requested", None, None)
            receipts.append(lane.receipt(request))
    manifest = dict(
        schema_version=1 if request.profile == "pmid_indexed" else 2,
        profile=request.profile,
        question=request.question,
        concept_groups=request.concept_groups,
        depth=request.depth,
        until_year=request.until_year,
        include_title_search=request.include_title_search,
        order="broad_first_native_rank_pubmed_first",
        candidate_count=len(rows),
        status=status,
        lanes=receipts,
        metadata_requests=metadata_requests,
        candidates_sha256=sha256(contents.encode()).hexdigest(),
    )
    # A merged wide row cannot tell us which provider supplied each identifier.
    # Keep the original claims so a later offline reuse can copy one provider.
    if request.profile == "all_indexed":
        manifest["native_hits"] = {
            f"{provider}_{branch}": [
                {"rank": rank, **{k: v for k, v in asdict(hit).items() if v is not None and v is not False}}
                for rank, hit in lane.hits
            ]
            for (provider, branch), lane in by_key.items()
        }
    followup = {}
    if sum(item is not None for item in (recovery, reuse, automatic_recovery)) > 1:
        raise ValueError("Choose one discovery follow-up operation.")
    if recovery is not None:
        kind, receipt, changed = "recovery", recovery, recovery["retried_lanes"]
    elif reuse is not None:
        kind, receipt, changed = "reuse", reuse, reuse["reused_lanes"]
    elif automatic_recovery is not None:
        kind, receipt = "automatic_recovery", automatic_recovery
        changed = [entry["lane"] for entry in receipt["lanes"] if entry["action"] != "retained"]
    else:
        receipt = None
    if receipt is not None:
        old_ranks = [col for col in RANKS if col.removesuffix("_rank") not in changed]
        followup[kind] = receipt | {
            "candidate_count_after": len(rows),
            "candidate_count_added": sum(all(row[col] is None for col in old_ranks) for row in rows),
        }
        manifest.update(followup)
    digest, encoded = _encode_manifest(manifest)
    room = MAX_SNAPSHOT_BYTES - len(contents.encode())
    if len(encoded.encode()) > room and "native_hits" in manifest and not any(lane.timed_out for lane in by_key.values()):
        # Preserve the previous export capacity for completed wide captures.
        # A table without raw claims can still be screened, but cannot donate a lane.
        del manifest["native_hits"]
        digest, encoded = _encode_manifest(manifest)
    if len(encoded.encode()) > room:
        raise ValueError("The discovery snapshot exceeds its 2 MiB export limit.")
    summaries = [
        {k: receipt[k] for k in ("provider", "branch", "status", "total_hits", "usable_rows", "truncated")}
        for receipt in receipts
    ]
    views = tuple(
        tuple(sorted((i for i, row in enumerate(rows) if row[col] is not None), key=lambda i: rows[i][col]))
        for col in RANKS
    )
    return DiscoverySnapshot(
        digest,
        contents,
        encoded,
        status,
        request.profile,
        tuple(map(_json, rows)),
        _json(summaries),
        views,
        tuple(r["status"] for r in receipts),
        _json(followup) if followup else None,
    )
