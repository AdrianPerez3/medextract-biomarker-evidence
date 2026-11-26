"""Score whether the paper reader surfaces prespecified results in saved NCBI JATS.

From a source checkout, run python -m scripts.check_passage_retrieval DIRECTORY.
See docs/retrieval-quality.md for the source articles and snapshot instructions.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from medextract_dossier.paper_text import read_paper


# Each row is a distinct endpoint or analysis. All anchors must occur together
# in a single Results passage. The terms were set from the published papers.
CASES = (
    ("prescribing-package", "PMC13227237", "adjusted RR",
     ("prescri",), ("0.80", "0.78", "0.82")),
    ("kidney-reclassification", "PMC5634538", "reclassified G3aA1",
     ("reclass", "G3aA1", "no CKD"), ("653", "50", "7.7")),
    ("icu-antibiotic-duration", "PMC7266125", "median duration antibiotic therapy index episode",
     ("median", "control"), ("8.8", "11.3", "0.011")),
    ("abstinence-six-months", "PMC13568513", "abstinence risk ratio 6 months",
     ("abst", "control"), ("1.51", "1.13", "2.02")),
    ("abstinence-adjusted", "PMC13568513", "abstinence adjusted age gender education",
     ("abst",), ("1.50", "1.12", "2.01")),
    ("abstinence-complete-case", "PMC13568513", "abstinence complete case analysis",
     ("abst", "complete"), ("1.30", "1.01", "1.67")),
)


def replay_snapshot(pmcid, xml):
    def fetch(url, *, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == "www.ebi.ac.uk" and parts.path.endswith(f"/{pmcid}/fullTextXML"):
            raise HTTPError(url, 404, "Replaying NCBI snapshot", {}, None)
        if (parts.hostname != "eutils.ncbi.nlm.nih.gov" or parts.path != "/entrez/eutils/efetch.fcgi"
                or parse_qs(parts.query).get("id") != [pmcid[3:]]):
            raise ValueError("Unexpected request during offline replay")
        if len(xml) > max_bytes:
            raise ValueError("Snapshot exceeds the reader's supported size")
        return xml
    return fetch


def matches(passage, words, numbers):
    section = " ".join(passage["section"]).casefold()
    if not re.search(r"\b(results?|findings)\b", section):
        return False
    text = passage["text"].casefold()
    return (all(word.casefold() in text for word in words)
            and all(re.search(r"(?<![\d.])" + re.escape(n) + r"(?!\d|\.\d)", text) for n in numbers))


def verify_passages(xml, result):
    if result["xml_sha256"] != sha256(xml).hexdigest():
        raise ValueError("Reader used different XML bytes")
    root = ElementTree.fromstring(xml)
    for passage in result["passages"]:
        loc = passage["locator"]
        prefix = f"/{root.tag}/"
        if not loc["xpath"].startswith(prefix):
            raise ValueError("Locator does not start at the snapshot root")
        node = root.find(loc["xpath"][len(prefix):])
        if node is None:
            raise ValueError("Locator missing from snapshot")
        normalized = " ".join("".join(node.itertext()).split())
        if (sha256(normalized.encode()).hexdigest() != loc["paragraph_sha256"]
                or not 0 <= loc["start"] < loc["end"] <= len(normalized)
                or normalized[loc["start"]:loc["end"]] != passage["text"]):
            raise ValueError("Passage did not replay at its reported locator")


def evaluate(directory):
    rows = []
    for name, pmcid, terms, words, numbers in CASES:
        xml = (directory / f"{pmcid}.xml").read_bytes()
        with patch("medextract_dossier.paper_text.fetch_bytes", replay_snapshot(pmcid, xml)):
            result = read_paper(pmcid, terms, limit=3)
        verify_passages(xml, result)
        rank = next((i for i, p in enumerate(result["passages"], 1) if matches(p, words, numbers)), None)
        rows.append({"case": name, "pmcid": pmcid, "terms": terms, "status": result["status"],
                     "xml_sha256": result["xml_sha256"], "passages_verified": len(result["passages"]),
                     "rank_in_top_3": rank})
    return {"cases": rows, "top_1": sum(row["rank_in_top_3"] == 1 for row in rows),
            "top_3": sum(row["rank_in_top_3"] is not None for row in rows), "total": len(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory containing PMCID.xml NCBI EFetch files")
    print(json.dumps(evaluate(parser.parse_args().directory), indent=2))
