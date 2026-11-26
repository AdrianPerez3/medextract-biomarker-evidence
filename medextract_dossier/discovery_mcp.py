"""Structured MCP results for browsing bounded literature searches."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Profile = Literal["pmid_indexed", "all_indexed"]
IndexLane = Literal["pubmed_broad", "epmc_broad", "pubmed_anchored", "epmc_anchored"]
View = Literal["screening", IndexLane]
LaneStatus = Literal["ok", "partial", "unavailable", "not_requested"]
Rank = Annotated[int, Field(ge=1)]
Count = Annotated[int, Field(ge=0)]
SearchId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Phrase = Annotated[str, Field(min_length=1, max_length=96)]
ConceptGroup = Annotated[list[Phrase], Field(min_length=1, max_length=8)]
ConceptGroups = Annotated[list[ConceptGroup], Field(min_length=2, max_length=6)]


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _Ranks(_Result):
    pubmed_broad_rank: Rank | None
    epmc_broad_rank: Rank | None
    pubmed_anchored_rank: Rank | None
    epmc_anchored_rank: Rank | None


class PMIDCandidate(_Ranks):
    pmid: str
    pubmed_url: str


class IndexedCandidate(_Ranks):
    row_id: str
    pmid: str | None
    doi: str | None
    pmcid: str | None
    url: str | None
    native_ids: list[str]
    identity_status: Literal["resolved", "unresolved", "conflict"]
    native_positions: dict[str, list[Rank]]


class LaneSummary(_Result):
    provider: Literal["pubmed", "epmc"]
    branch: Literal["broad", "anchored"]
    status: LaneStatus
    total_hits: Count | None
    usable_rows: Count
    truncated: bool | None


class DiscoverySummary(_Result):
    schema_version: Rank
    search_id: SearchId
    profile: Profile
    status: Literal["ok", "partial", "unavailable"]
    candidate_count: Count
    lanes: list[LaneSummary]
    preview: list[PMIDCandidate | IndexedCandidate]
    next_offset: Count | None
    candidates_uri: str
    manifest_uri: str


class RecoveryReceipt(_Result):
    parent_search_id: SearchId
    retried_lanes: Annotated[list[IndexLane], Field(min_length=1, max_length=4)]
    candidate_count_before: Count
    candidate_count_after: Count
    candidate_count_added: Count
    request_count: Rank
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RecoverySummary(DiscoverySummary):
    recovery: RecoveryReceipt


class ReuseReceipt(_Result):
    parent_search_id: SearchId
    donor_search_id: SearchId
    max_gap_seconds: Count
    reused_lanes: Annotated[list[IndexLane], Field(min_length=1, max_length=4)]
    candidate_count_before: Count
    candidate_count_after: Count
    candidate_count_added: Count
    request_count: Literal[0]


class ReuseSummary(DiscoverySummary):
    reuse: ReuseReceipt


class AutomaticRecoveryLane(_Result):
    lane: IndexLane
    match: Literal["selected", "missing", "ambiguous"]
    action: Literal["reused", "retained", "retried"]
    eligible_donor_count: Count
    donor_search_id: SearchId | None


class AutomaticRecoveryReceipt(_Result):
    parent_search_id: SearchId
    max_gap_seconds: Count
    lookup_at: str | None
    fetch_missing: bool
    donors_considered: Count
    lanes: Annotated[list[AutomaticRecoveryLane], Field(min_length=1, max_length=4)]
    candidate_count_before: Count
    candidate_count_after: Count
    candidate_count_added: Count
    request_count: Count


class AutomaticRecoverySummary(DiscoverySummary):
    automatic_recovery: AutomaticRecoveryReceipt


class DiscoveryPage(_Result):
    search_id: SearchId
    view: View
    view_count: Count
    offset: Count
    next_offset: Count | None
    rows: list[PMIDCandidate | IndexedCandidate]
    status: LaneStatus
