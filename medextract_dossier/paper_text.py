"""Locate reviewable prose in explicitly licensed articles from Europe PMC or NCBI PMC."""

from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import re
from urllib.error import HTTPError
from xml.etree.ElementTree import Element, TreeBuilder
from xml.parsers import expat

from .literature_http import fetch_bytes


_XML_BYTES = 2_000_000
_QUOTE_CHARS = 420
_XLINK = "{http://www.w3.org/1999/xlink}href"
_ALI_REF = "{http://www.niso.org/schemas/ali/1.0/}license_ref"
_LICENSES = {
    "creativecommons.org/licenses/by/4.0": "CC-BY-4.0",
    "creativecommons.org/publicdomain/zero/1.0": "CC0-1.0",
}
_WORD = re.compile(r"\d+\.\d+(?:[eE][+-]?\d+)?|\w+(?:[-‐‑–]\w+)*")
_DASHES = str.maketrans("‐‑–", "---")
_STOPWORDS = frozenset(
    "a an and are as at be by can did do does for from how in is it of on or that the "
    "this to was were what when whether which with".split()
)
# Unknown embedded structures may contain separately credited figures, quotations or tables.
_INLINE = frozenset(
    "bold italic underline monospace overline roman sans-serif sc scp strike sub sup "
    "ext-link xref named-content styled-content inline-formula".split()
)
_SKIP_SECTIONS = re.compile(
    r"^(?:\d+[.\s]+)?(?:abstract|references|bibliography|supplement(?:ary|al)?(?: material| information| data)?|"
    r"acknowledg(?:e)?ments?)\b", re.I
)
_RESULTS = re.compile(r"^(?:\d+[.\s]+)?(?:results|findings)\b", re.I)
_NOTICE = re.compile(r"retract|correct|errat|concern|withdraw|remov|addendum", re.I)
_RESTRICTED_RIGHTS = re.compile(
    r"\b(?:non[- ]?commercial|no[- ]?derivatives|share[- ]?alike|CC[- ]BY[- ](?:NC|ND|SA)|all rights reserved)\b",
    re.I,
)
_INTERPRETATION = (
    "Passages are ranked by lexical overlap in the eligible body prose. Assess the study design, "
    "population, outcomes and publication notices in the linked article before interpreting a match."
)


def _text(element: Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _parse(data: bytes) -> Element:
    """Build a bounded tree. Accept an external JATS DOCTYPE without loading its DTD."""
    if len(data) > _XML_BYTES:
        raise ValueError("Article exceeds the XML size limit.")
    builder = TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    depth = nodes = 0

    def qname(name):
        return "{" + name if "}" in name else name

    def start(name, attrs):
        nonlocal depth, nodes
        depth += 1
        nodes += 1
        if depth > 96 or nodes > 100_000:
            raise ValueError("Article exceeds XML structure limits.")
        builder.start(qname(name), {qname(key): value for key, value in attrs.items()})

    def end(name):
        nonlocal depth
        builder.end(qname(name))
        depth -= 1

    def doctype(name, system_id, public_id, has_internal_subset):
        if has_internal_subset:
            raise ValueError("Article contains an unsupported XML internal subset.")

    def skipped_entity(name, is_parameter_entity):
        raise ValueError("Article uses an unresolved XML entity.")

    parser.StartDoctypeDeclHandler = doctype
    parser.SkippedEntityHandler = skipped_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValueError("Provider returned malformed article XML.") from exc
    return builder.close()


def _license(meta: Element) -> dict | None:
    nodes = meta.findall("permissions/license")
    if len(nodes) != 1:
        return None
    node = nodes[0]
    if node.get("specific-use") or node.get("license-type", "").lower() not in {
        "", "open-access", "openaccess", "cc-by", "cc0", "cc-by-4.0", "cc0-1.0"
    }:
        return None
    links = set()
    for part in node.iter():
        if any(part.get(attr) for attr in ("applies_to", "start_date", "specific-use")):
            # NCBI also supplies machine-readable text-mining terms. A separate,
            # unrestricted article license must establish the right to quote.
            if part.tag == _ALI_REF and part in node and len(part) == 0:
                continue
            return None
        url = part.get(_XLINK)
        if url:
            links.add(url.strip())
        elif part.tag.split("}")[-1] in {"license_ref", "ext-link", "uri"}:
            links.add(_text(part))
    statement = " ".join(_text(p) for p in node.findall("license-p")) or (node.text or "").strip()
    if _RESTRICTED_RIGHTS.search(statement):
        return None
    # Some publishers supply the URL as plain text inside the license paragraph.
    links.update(url.rstrip(".,;)") for url in re.findall(r"https?://[^\s<(]+", statement))
    canonical = {re.sub(r"^https?://", "", url).rstrip("/") for url in links}
    if len(canonical) != 1:
        return None
    path = canonical.pop()
    notice = _text(meta.find("permissions/copyright-statement"))
    if path not in _LICENSES or len(statement) > 4000 or len(notice) > 2000:
        return None
    return {"id": _LICENSES[path], "url": f"https://{path}/",
            "statement": statement, "copyright": notice or None}


def _authors(meta: Element) -> list[str]:
    names = []
    for author in meta.findall("contrib-group/contrib"):
        if author.get("contrib-type", "author") != "author":
            continue
        name = author.find("name")
        if name is None:
            name = author.find("name-alternatives/name")
        if name is not None:
            parts = [_text(name.find(field)) for field in ("given-names", "surname", "suffix")]
            credit = " ".join(part for part in parts if part)
        else:
            credit = _text(author.find("collab")) or _text(author.find("string-name"))
        if credit:
            names.append(credit)
    return names


def _flags(article: Element, meta: Element, title: str) -> list[str]:
    flags = set()
    article_type = article.get("article-type", "")
    if _NOTICE.search(article_type):
        flags.add("article-type:" + article_type)
    for rel in meta.iter("related-article"):
        kind = rel.get("related-article-type", "")
        if _NOTICE.search(kind):
            flags.add("related-article:" + kind)
    for subject in meta.findall("article-categories//subject"):
        label = _text(subject)
        if re.fullmatch(r"retracted publication|retraction|erratum|correction|expression of concern", label, re.I):
            flags.add("subject:" + label)
    if re.match(r"^(retract(?:ed|ion)|withdrawn|correction|erratum|expression of concern)\b", title, re.I):
        flags.add("notice-title")
    return sorted(flags)


def _paragraphs(container, path, sections):
    """Only direct body/section prose with known inline formatting enters the search."""
    counts = Counter()
    for child in container:
        tag = child.tag
        counts[tag] += 1
        child_path = f"{path}/{tag}[{counts[tag]}]"
        if tag == "p":
            yield child, child_path, sections
        elif tag == "sec":
            heading = _text(child.find("title"))
            if _SKIP_SECTIONS.match(heading) or _SKIP_SECTIONS.match(child.get("sec-type", "")):
                continue
            yield from _paragraphs(child, child_path, sections + ([heading] if heading else []))


def _words(text):
    return [m.group().casefold().translate(_DASHES) for m in _WORD.finditer(text)]


def _windows(paragraph):
    """Search overlapping whole-word windows and retain replayable character offsets."""
    if len(paragraph) <= _QUOTE_CHARS:
        yield 0, len(paragraph)
        return
    spans = list(re.finditer(r"\S+", paragraph))
    ends = [span.end() for span in spans]
    index = 0
    while index < len(spans):
        start = spans[index].start()
        following = bisect_right(ends, start + _QUOTE_CHARS, lo=index)
        if following > index:
            yield start, ends[following - 1]
        if following == len(spans):
            break
        index = max(index + 1, bisect_right(ends, start + _QUOTE_CHARS // 2, lo=index))


def _prose_windows(node, paragraph):
    # A publisher may put an entire table inside the same p as a reported result.
    # Never bridge across it. Count its text toward offsets in the original p.
    runs = []
    prose = [node.text or ""]
    for child in node:
        if child.tag in {"fig", "table-wrap"}:
            runs.append((True, "".join(prose)))
            runs.append((False, "".join(child.itertext())))
            prose = []
        elif all(part.tag in _INLINE for part in child.iter()):
            prose.extend(child.itertext())
        else:
            return
        prose.append(child.tail or "")
    runs.append((True, "".join(prose)))
    offset = 0
    for eligible, raw in runs:
        text = " ".join(raw.split())
        if not text:
            continue
        offset = paragraph.index(text, offset)
        if eligible:
            for start, end in _windows(text):
                yield offset + start, offset + end
        offset += len(text)


def _passages(body, body_path, meta, terms, limit):
    candidates = []
    seen = {_text(p) for p in meta.findall("abstract//p")}
    for node, path, sections in _paragraphs(body, body_path, []):
        paragraph = _text(node)
        if not paragraph or paragraph in seen:
            continue
        results_section = any(_RESULTS.match(section) for section in sections)
        best = None
        for start, end in _prose_windows(node, paragraph):
            quote = paragraph[start:end]
            frequencies = Counter(_words(quote))
            matched = [term for term in terms if term in frequencies]
            if not matched:
                continue
            score = (len(matched), results_section, sum(min(3, frequencies[w]) for w in matched))
            if best is None or score > best[0]:
                best = (score, start, end, matched)
        if best:
            seen.add(paragraph)
            score, start, end, matched = best
            locator = {"xpath": path, "start": start, "end": end,
                       "paragraph_sha256": sha256(paragraph.encode()).hexdigest()}
            candidates.append((score, {"text": paragraph[start:end], "section": sections,
                                       "locator": locator, "matched_terms": matched}))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [passage for _, passage in candidates[:limit]]


def _retrieve(pmcid, result):
    """Try each official endpoint at most once and preserve the source of the actual bytes."""
    number = pmcid[3:]
    sources = [
        ("Europe PMC", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
         f"https://europepmc.org/articles/{pmcid}"),
        ("NCBI PMC", f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={number}"
         "&retmode=xml&tool=medextract_evidence_dossier", f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"),
    ]
    attempts = result["retrieval_attempts"]
    failure = None
    for provider, url, source_url in sources:
        attempt = {"provider": provider, "url": url}
        attempts.append(attempt)
        try:
            data = fetch_bytes(url, max_bytes=_XML_BYTES)
        except HTTPError as exc:
            attempt["detail"] = f"HTTP {exc.code}"
            if exc.code in {404, 410}:
                attempt["status"] = "no-fulltext"
                continue
            if exc.code not in {408, 429} and not 500 <= exc.code <= 599:
                raise
            failure = exc
            attempt["status"] = "unavailable"
            continue
        except OSError as exc:
            failure = exc
            attempt.update(status="unavailable", detail=type(exc).__name__)
            continue

        root = _parse(data)
        if provider == "NCBI PMC":
            if root.tag != "pmc-articleset" or len(root) != 1:
                raise ValueError("Expected a single record from NCBI PMC.")
            article = root[0]
            if (article.tag == "error" and article.get("id") == number
                    and _text(article) == f"The following PMCID is not available: {number}"):
                attempt.update(status="no-fulltext", detail="PMCID unavailable")
                continue
            article_path = "/pmc-articleset/article[1]"
        else:
            article, article_path = root, "/article"
        if article.tag != "article":
            raise ValueError(f"{provider} did not return a JATS article.")
        attempt["status"] = "received"
        result.update(provider=provider, full_text_url=url, source_url=source_url,
                      xml_sha256=sha256(data).hexdigest())
        return article, article_path

    if failure is not None:
        summary = ", ".join(f"{item['provider']} {item['status']} ({item['detail']})" for item in attempts)
        if isinstance(failure, HTTPError):
            raise HTTPError(failure.url, failure.code, summary, failure.headers, None) from failure
        raise OSError("Full-text retrieval incomplete: " + summary) from failure
    return None, None


def read_paper(pmcid: str, terms: str, limit: int = 3) -> dict:
    """Retrieve one article and locate up to five short lexical matches in licensed body text.

    Each XPath is absolute within the retrieved XML, including the pmc-articleset
    wrapper returned by NCBI. It selects a JATS p. Join its descendant text in document order,
    then collapse Unicode whitespace and trim. The zero-based, half-open start/end
    offsets count Unicode code points in that normalized string. Digests use SHA-256
    of the source XML bytes and of the normalized paragraph encoded as UTF-8.
    """
    if not isinstance(pmcid, str) or not re.fullmatch(r"PMC[1-9][0-9]{0,11}", pmcid):
        raise ValueError("Expected a PMCID such as PMC123456.")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Passage limit must be an integer from 1 through 5.")
    if not isinstance(terms, str) or len(terms) > 256:
        raise ValueError("Search terms must contain 1 through 256 characters.")
    query = list(dict.fromkeys(w for w in _words(terms) if w not in _STOPWORDS))
    if not query:
        raise ValueError("Supply at least one searchable term.")
    result = {"status": "no-fulltext", "pmcid": pmcid, "pmid": None, "doi": None, "title": None,
              "authors": [], "author_count": 0, "journal": None, "year": None,
              "source_url": f"https://europepmc.org/articles/{pmcid}", "full_text_url": None,
              "provider": None, "retrieval_attempts": [],
              "retrieved_at": None, "xml_sha256": None, "license": None, "publication_flags": [],
              "passages": [], "interpretation": _INTERPRETATION}
    article, article_path = _retrieve(pmcid, result)
    result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
    if article is None:
        return result
    meta = article.find("front/article-meta")
    if meta is None:
        raise ValueError("Provider returned a JATS article without article metadata.")
    ids = meta.findall("article-id")
    identifiers = {re.sub("^PMC", "", _text(node)) for node in ids
                   if node.get("pub-id-type") in {"pmc", "pmcid"}}
    if identifiers != {pmcid[3:]}:
        raise ValueError("Article identity does not match the requested PMCID.")
    title = _text(meta.find("title-group/article-title"))
    authors = _authors(meta)
    result.update(
        title=title or None, authors=authors[:25], author_count=len(authors),
        pmid=_text(meta.find("article-id[@pub-id-type='pmid']")) or None,
        doi=_text(meta.find("article-id[@pub-id-type='doi']")) or None,
        journal=_text(article.find("front/journal-meta/journal-title-group/journal-title")) or None,
        year=_text(meta.find("pub-date/year")) or None, publication_flags=_flags(article, meta, title),
        license=_license(meta),
    )
    body = article.find("body")
    if body is None or not any(text.strip() for text in body.itertext()):
        return result
    if result["license"] is None:
        result["status"] = "licence-not-supported"
        return result
    result["passages"] = _passages(body, article_path + "/body[1]", meta, query, limit)
    result["status"] = "available" if result["passages"] else "no-term-match"
    return result
