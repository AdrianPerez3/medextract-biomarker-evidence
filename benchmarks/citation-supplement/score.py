"""Recount the paired screening experiment from ordered, publication-level observations."""
import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path


COLUMNS = (
    "cohort", "topic", "pmid", "prefix_rank", "shared_rank", "native_tail_rank",
    "citation_rank", "judgment", "family", "primary", "in_native_capture",
)
BUDGETS = {"prefix_rank": 100, "shared_rank": 50, "native_tail_rank": 10, "citation_rank": 10}
COHORTS = ("diagnostic", "intervention")
GRADES = ("include", "exclude", "unjudged")
DEFAULT_INPUT = Path(__file__).with_name("observations.tsv")
METRICS = (
    "topics", "triggered_topics", "citation_topics", "citation_slots", "final_screens",
    "sixty_screens", "native_final", "assisted_final", "native_sixty", "assisted_sixty",
    "gained", "lost", "novel_reports", "native_final_unjudged", "assisted_final_unjudged",
)


def read_observations(path):
    groups = defaultdict(dict)
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t", strict=True)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError("Unexpected observations header")
        for row in reader:
            try:
                if None in row or None in row.values():
                    raise ValueError("Invalid column count")
                cohort, topic, pmid = row["cohort"], row["topic"], row["pmid"]
                if cohort not in COHORTS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", topic):
                    raise ValueError("Unknown cohort or invalid topic ID")
                if not re.fullmatch(r"[1-9][0-9]{0,8}", pmid):
                    raise ValueError("Invalid PMID")
                if row["judgment"] not in GRADES:
                    raise ValueError("Unknown judgment")
                if row["primary"] not in ("0", "1") or row["in_native_capture"] not in ("0", "1"):
                    raise ValueError("Expected a binary flag")
                trial_hit = cohort == "intervention" and row["judgment"] == "include"
                if trial_hit:
                    if not re.fullmatch(r"[1-9][0-9]{0,8}", row["family"]):
                        raise ValueError("Included trial report lacks a valid family ID")
                elif row["family"] or row["primary"] == "1":
                    raise ValueError("Family or primary flag on an inapplicable row")
                for column, limit in BUDGETS.items():
                    value = row[column]
                    if value and (not re.fullmatch(r"[1-9][0-9]{0,2}", value) or int(value) > limit):
                        raise ValueError(f"Rank outside {column} budget")
                native = sum(bool(row[k]) for k in ("prefix_rank", "shared_rank", "native_tail_rank"))
                if native > 1 or (not native and not row["citation_rank"]):
                    raise ValueError("Invalid native stage membership")
                if native and row["in_native_capture"] != "1":
                    raise ValueError("Native PMID missing from native capture")
                if row["citation_rank"] and (row["prefix_rank"] or row["shared_rank"]):
                    raise ValueError("Already-screened PMID in citation supplement")
                records = groups[cohort, topic]
                if pmid in records:
                    raise ValueError("Duplicate topic-PMID")
                records[pmid] = row
            except ValueError as exc:
                raise ValueError(f"Line {reader.line_num}: {exc}") from exc
    if not groups:
        raise ValueError("Empty observations")
    return groups


def ranked(records, column):
    entries = sorted((int(r[column]), pid) for pid, r in records.items() if r[column])
    if any(rank != i for i, (rank, _) in enumerate(entries, 1)):
        raise ValueError(f"Ranks in {column} must be unique and consecutive from 1")
    return [pid for _, pid in entries]


def topic_counts(cohort, records):
    prefix, shared, native, citations = (ranked(records, k) for k in BUDGETS)
    if not prefix or (native and len(shared) != BUDGETS["shared_rank"]) or len(citations) > len(native):
        raise ValueError("Invalid prefix, protected block or supplement length")
    seed = [pid for pid in prefix if records[pid]["judgment"] == "include"]
    if seed and seed != prefix[-1:]:
        raise ValueError("Prefix must stop at the first included paper")
    if not seed and (citations or (shared and len(prefix) != BUDGETS["prefix_rank"])):
        raise ValueError("Missing include trigger or incomplete no-trigger prefix")
    primaries = [r["family"] for r in records.values() if r["primary"] == "1"]
    if len(primaries) != len(set(primaries)):
        raise ValueError("Several primary reports for the same trial family")

    assisted = list(dict.fromkeys(citations + native))[:len(native)]

    def known(ids):
        return {pid if cohort == "diagnostic" else records[pid]["family"]
                for pid in ids if records[pid]["judgment"] == "include"}

    seen = known(prefix)
    already = seen | known(shared)
    native_hits = known(native) - already
    assisted_hits = known(assisted) - already
    outside = [pid for pid in citations if records[pid]["in_native_capture"] == "0"
               and records[pid]["judgment"] == "include"]
    return dict(topics=1, triggered_topics=int(bool(seed)), citation_topics=int(bool(citations)),
                citation_slots=len(citations), final_screens=len(native), sixty_screens=len(shared + native),
                native_final=len(native_hits), assisted_final=len(assisted_hits),
                native_sixty=len(known(shared + native) - seen), assisted_sixty=len(known(shared + assisted) - seen),
                gained=len(assisted_hits - native_hits), lost=len(native_hits - assisted_hits),
                novel_reports=len(outside),
                native_final_unjudged=sum(records[p]["judgment"] == "unjudged" for p in native),
                assisted_final_unjudged=sum(records[p]["judgment"] == "unjudged" for p in assisted))


def score(path=DEFAULT_INPUT):
    totals = {cohort: dict.fromkeys(METRICS, 0) for cohort in COHORTS}
    for (cohort, topic), records in read_observations(path).items():
        try:
            counts = topic_counts(cohort, records)
        except ValueError as exc:
            raise ValueError(f"{topic}: {exc}") from exc
        for metric, value in counts.items():
            totals[cohort][metric] += value
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--json", action="store_true", help="print grouped counts as JSON")
    args = parser.parse_args()
    try:
        totals = score(args.input)
    except (ValueError, OSError, csv.Error) as exc:
        parser.exit(2, f"Invalid observations: {exc}\n")
    if args.json:
        print(json.dumps(totals, indent=2))
        return
    for cohort, unit in (("diagnostic", "included reports"), ("intervention", "new trial families")):
        row = totals[cohort]
        print(f"{cohort.capitalize()} ({row['topics']} topics, {unit})")
        print(f"  Final slots, native / supplemented: {row['native_final']} / {row['assisted_final']}"
              f" across {row['final_screens']} screens per strategy")
        print(f"  Full continuation, native / supplemented: {row['native_sixty']} / {row['assisted_sixty']}"
              f" across {row['sixty_screens']} screens per strategy")
        print(f"  Gained {row['gained']}, displaced {row['lost']}. "
              f"Included citations outside original pages: {row['novel_reports']}")


if __name__ == "__main__":
    main()
