"""Resolve bibliographic identifiers without conflating reports or study families."""

from collections import defaultdict
from urllib.parse import quote

from .discovery import RANKS


def _claims(hit):
    claims = [(kind, getattr(hit, kind)) for kind in ("doi", "pmid", "pmcid") if getattr(hit, kind)]
    if hit.source and hit.source_id:
        claims.append(("source:" + hit.source, hit.source_id))
    return tuple(claims)


def _row_url(row, status, native_ids):
    native_url = None
    if len(native_ids) == 1:
        source, identifier = native_ids[0].split(":", 1)
        native_url = f"https://europepmc.org/article/{source}/{identifier}"
    if status == "conflict":
        # Only a unique observed native reference is safe to follow here.
        return native_url
    if row["pmid"]:
        return f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/"
    if row["pmcid"]:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{row['pmcid']}/"
    if row["doi"]:
        return "https://doi.org/" + quote(row["doi"], safe="/")
    return native_url


def indexed_rows(ordered):
    # Link all occurrences first: a late conflicting bridge must never make the result arrival-dependent.
    parents = list(range(len(ordered)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    owners = {}
    signatures = []
    for i, (_, _, hit) in enumerate(ordered):
        signature = _claims(hit)
        signatures.append(signature)
        for claim in signature:
            if claim in owners:
                parents[root(i)] = root(owners[claim])
            else:
                owners[claim] = i
    components = defaultdict(list)
    for i in range(len(ordered)):
        components[root(i)].append(i)

    partitions = []
    for indices in components.values():
        values = defaultdict(set)
        for i in indices:
            for key, value in signatures[i]:
                values[key].add(value)
        incompatible = (
            any(len(v) > 1 for v in values.values())
            or any(ordered[i][2].conflict for i in indices)
            or ("source:PPR" in values and ("source:MED" in values or "source:PMC" in values))
        )
        if incompatible:
            # The available evidence cannot disambiguate this cluster. Only identical
            # claims are coalesced, and each resulting record retains the conflict.
            by_signature = defaultdict(list)
            for i in indices:
                by_signature[signatures[i]].append(i)
            partitions.extend((part, "conflict") for part in by_signature.values())
        else:
            partitions.append((indices, "resolved" if values else "unresolved"))
    rows = []
    for indices, status in sorted(partitions, key=lambda part: part[0][0]):
        row = dict.fromkeys(("pmid", "doi", "pmcid", *RANKS))
        native_ids = []
        positions = defaultdict(list)
        for i in indices:
            column, rank, hit = ordered[i]
            for key, value in signatures[i]:
                if key.startswith("source:"):
                    native = f"{hit.source}:{value}"
                    if native not in native_ids:
                        native_ids.append(native)
                else:
                    row[key] = value
            positions[column.removesuffix("_rank")].append(rank)
            if row[column] is None:
                row[column] = rank
        row.update(
            row_id=f"r{len(rows) + 1:04}",
            url=_row_url(row, status, native_ids),
            native_ids=native_ids,
            identity_status=status,
            native_positions=dict(positions),
        )
        rows.append(row)
    return rows
