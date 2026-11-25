"""Offline, invented JATS fixtures for the public full-text reader."""

from hashlib import sha256
from urllib.error import HTTPError
from xml.etree import ElementTree

import pytest

from medextract_dossier import paper_text


URL = "https://creativecommons.org/licenses/by/4.0/"
LICENSE = f'<license><license-p>Available under <ext-link xlink:href="{URL}">{URL}</ext-link>.</license-p></license>'
BODY = """<body><sec><title>Results</title>
<fig><caption><p>Only captionmarker occurs here.</p></caption></fig>
<p>Marker levels   were <italic>higher</italic> in the invented group.</p>
<sec><title>Validation</title><p>Marker <bold>levels</bold> tracked response.</p></sec>
<p>Only embeddedmarker <disp-quote><p>Someone else's words.</p></disp-quote> occurs here.</p>
</sec></body>"""


def article(body=BODY, license_xml=LICENSE, identifier="PMC1234", extra="", kind="research-article"):
    return f"""<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="{kind}"><front>
    <journal-meta><journal-title-group><journal-title>Invented Journal</journal-title></journal-title-group></journal-meta>
    <article-meta><article-id pub-id-type="pmcid">{identifier}</article-id>
    <article-id pub-id-type="pmid">45678</article-id><article-id pub-id-type="doi">10.0000/example</article-id>
    <title-group><article-title>An invented marker</article-title></title-group>
    <contrib-group><contrib><name><surname>Example</surname><given-names>Alex</given-names></name></contrib>
    <contrib contrib-type="author"><collab>Made-up Lab</collab></contrib></contrib-group>
    <pub-date><year>2026</year></pub-date><abstract><p>Only abstractmarker occurs here.</p></abstract>
    <permissions><copyright-statement>Copyright 2026 Example</copyright-statement>{license_xml}</permissions>
    {extra}</article-meta></front>{body}
    <back><ref-list><ref><p>Only refmarker occurs here.</p></ref></ref-list></back></article>""".encode()


def provide(monkeypatch, data):
    calls = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        return data

    monkeypatch.setattr(paper_text, "fetch_bytes", fetch)
    return calls


def test_returns_replayable_body_excerpt_and_attribution(monkeypatch):
    data = article()
    calls = provide(monkeypatch, data)
    result = paper_text.read_paper("PMC1234", "marker response", 1)
    assert calls == ["https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1234/fullTextXML"]
    assert result["status"] == "available" and len(result["passages"]) == 1
    assert result["xml_sha256"] == sha256(data).hexdigest()
    assert result["doi"] == "10.0000/example" and result["pmid"] == "45678"
    assert result["title"] == "An invented marker" and result["year"] == "2026"
    assert result["authors"] == ["Alex Example", "Made-up Lab"]
    assert result["license"]["id"] == "CC-BY-4.0" and result["license"]["url"] == URL
    assert result["license"]["copyright"] == "Copyright 2026 Example"
    assert result["source_url"] == "https://europepmc.org/articles/PMC1234"
    passage = result["passages"][0]
    assert passage["section"] == ["Results", "Validation"]
    assert passage["text"] == "Marker levels tracked response."
    locator = passage["locator"]
    node = ElementTree.fromstring(data).find("." + locator["xpath"].removeprefix("/article"))
    paragraph = " ".join("".join(node.itertext()).split())
    assert paragraph[locator["start"]:locator["end"]] == passage["text"]
    assert sha256(paragraph.encode()).hexdigest() == locator["paragraph_sha256"]
    assert passage["matched_terms"] == ["marker", "response"]


@pytest.mark.parametrize("terms", ["refmarker", "abstractmarker", "captionmarker", "embeddedmarker", "ark"])
def test_scope_and_token_boundaries(monkeypatch, terms):
    provide(monkeypatch, article())
    result = paper_text.read_paper("PMC1234", terms)
    assert (result["status"], result["passages"]) == ("no-term-match", [])


@pytest.mark.parametrize("license_xml", [
    "", "<license><license-p>CC-BY License</license-p></license>",
    LICENSE.replace("/by/4.0/", "/by-nc/4.0/"), LICENSE.replace("creativecommons.org", "creativecommons.org.example"),
    LICENSE + LICENSE.replace("/by/4.0/", "/by-nc/4.0/"),
    LICENSE.replace("<license>", '<license specific-use="text-mining">'),
])
def test_uncertain_or_restricted_rights_preserve_only_metadata(monkeypatch, license_xml):
    provide(monkeypatch, article(license_xml=license_xml))
    result = paper_text.read_paper("PMC1234", "marker")
    assert (result["status"], result["passages"], result["license"]) == ("licence-not-supported", [], None)
    assert result["doi"] == "10.0000/example"


def test_cc0_and_notice_are_reported(monkeypatch):
    data = article(license_xml=LICENSE.replace("licenses/by/4.0", "publicdomain/zero/1.0"),
                   extra='<related-article related-article-type="retraction-forward"/>')
    provide(monkeypatch, data)
    result = paper_text.read_paper("PMC1234", "marker")
    assert result["license"]["id"] == "CC0-1.0"
    assert "related-article:retraction-forward" in result["publication_flags"]


def test_long_paragraph_anchors_excerpt_on_late_terms(monkeypatch):
    paragraph = ("Padding material. " * 80) + "The marker matched in validation."
    provide(monkeypatch, article(body=f"<body><p>{paragraph}</p></body>"))
    passage, = paper_text.read_paper("PMC1234", "marker validation")["passages"]
    loc = passage["locator"]
    assert 0 < loc["start"] < loc["end"]
    assert len(passage["text"]) <= 420
    assert "marker matched in validation" in passage["text"]
    assert paragraph[loc["start"]:loc["end"]] == passage["text"]


@pytest.mark.parametrize(("pmcid", "terms", "limit"), [
    ("https://example.org", "marker", 1), ("PMC1/../../x", "a", 1), ("PMC0", "marker", 1),
    ("PMC1", "", 1), ("PMC1", "?" * 257, 1), ("PMC1", "marker", 0),
    ("PMC1", "marker", 6), ("PMC1", "marker", True),
])
def test_input_rejected_before_any_request(monkeypatch, pmcid, terms, limit):
    calls = provide(monkeypatch, article())
    with pytest.raises(ValueError):
        paper_text.read_paper(pmcid, terms, limit)
    assert not calls


@pytest.mark.parametrize("data", [
    article(identifier="PMC2345"), b"<article/><", b"<html/>",
    b'<!DOCTYPE article [<!ENTITY internal "invented">]>' + article(),
    b"<article>" + b"<sec>" * 150 + b"</sec>" * 150 + b"</article>",
])
def test_protocol_or_invalid_xml_errors_are_not_negative_searches(monkeypatch, data):
    provide(monkeypatch, data)
    with pytest.raises(ValueError):
        paper_text.read_paper("PMC1234", "marker")


def test_404_is_scoped_but_transient_outage_propagates(monkeypatch):
    def retrieve(url, *, max_bytes):
        raise HTTPError(url, status, "server response", None, None)

    monkeypatch.setattr(paper_text, "fetch_bytes", retrieve)
    status = 404
    result = paper_text.read_paper("PMC1234", "marker")
    assert result["status"] == "no-fulltext" and result["xml_sha256"] is None
    status = 503
    with pytest.raises(HTTPError):
        paper_text.read_paper("PMC1234", "marker")


def test_missing_body_is_distinct_from_no_term_match(monkeypatch):
    for body in ("", "<body/>"):
        provide(monkeypatch, article(body=body))
        result = paper_text.read_paper("PMC1234", "marker")
        assert result["status"] == "no-fulltext" and result["passages"] == []


def test_copied_abstract_or_reference_section_is_not_eligible(monkeypatch):
    body = "<body><p>Only abstractmarker occurs here.</p><sec sec-type='ref-list'><title>References</title><p>Only refmarker.</p></sec></body>"
    provide(monkeypatch, article(body=body))
    assert paper_text.read_paper("PMC1234", "abstractmarker refmarker")["status"] == "no-term-match"


def test_notice_in_publication_category(monkeypatch):
    extra = "<article-categories><subj-group><subject>Retracted Publication</subject></subj-group></article-categories>"
    provide(monkeypatch, article(extra=extra))
    assert "subject:Retracted Publication" in paper_text.read_paper("PMC1234", "marker")["publication_flags"]


def test_declared_restriction_overrides_a_conflicting_cc_link(monkeypatch):
    license_xml = LICENSE.replace("Available under", "Available for non-commercial use only under")
    provide(monkeypatch, article(license_xml=license_xml))
    assert paper_text.read_paper("PMC1234", "marker")["status"] == "licence-not-supported"


def test_external_dtd_requires_no_fetch_and_unknown_entities_cannot_change_a_quote(monkeypatch):
    prefix = b'<!DOCTYPE article SYSTEM "https://example.invalid/article.dtd">'
    provide(monkeypatch, prefix + article())
    assert paper_text.read_paper("PMC1234", "marker")["status"] == "available"
    provide(monkeypatch, prefix + article(body="<body><p>Marker concentration &unknownentity; 0.2</p></body>"))
    with pytest.raises(ValueError):
        paper_text.read_paper("PMC1234", "marker")


def test_same_term_coverage_prioritizes_reported_results(monkeypatch):
    body = """<body><sec><title>Introduction</title><p>Marker marker marker response.</p></sec>
    <sec><title>3. Results</title><p>Marker response varied little.</p></sec></body>"""
    provide(monkeypatch, article(body=body))
    passage, = paper_text.read_paper("PMC1234", "marker response", 1)["passages"]
    assert passage["section"] == ["3. Results"]


NCBI_URL = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            "?db=pmc&id=1234&retmode=xml&tool=medextract_evidence_dossier")
NCBI_MISSING = b'<pmc-articleset><error id="1234">The following PMCID is not available: 1234</error></pmc-articleset>'


def fallback(monkeypatch, first, second):
    responses = iter((first, second))
    urls = []

    def fetch(url, *, max_bytes):
        urls.append(url)
        assert max_bytes == 2_000_000
        response = next(responses)
        if isinstance(response, int):
            raise HTTPError(url, response, "invented status", None, None)
        if isinstance(response, OSError):
            raise response
        return response

    monkeypatch.setattr(paper_text, "fetch_bytes", fetch)
    return urls


@pytest.mark.parametrize("first", [TimeoutError(), 503, 404])
def test_fallback_cites_exact_ncbi_payload_and_xpath(monkeypatch, first):
    extra_scoped = ('<ali:license_ref xmlns:ali="http://www.niso.org/schemas/ali/1.0/" '
                    'specific-use="textmining">https://creativecommons.org/licenses/by/4.0/</ali:license_ref>')
    data = b"<pmc-articleset>" + article(license_xml=LICENSE.replace("<license>", "<license>" + extra_scoped)) + b"</pmc-articleset>"
    urls = fallback(monkeypatch, first, data)
    r = paper_text.read_paper("PMC1234", "marker", 1)
    assert urls == ["https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1234/fullTextXML", NCBI_URL]
    assert r["provider"] == "NCBI PMC" and r["full_text_url"] == NCBI_URL
    assert r["xml_sha256"] == sha256(data).hexdigest()
    assert r["source_url"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234/"
    assert [a["provider"] for a in r["retrieval_attempts"]] == ["Europe PMC", "NCBI PMC"]
    assert r["retrieval_attempts"][0]["status"] == ("no-fulltext" if first == 404 else "unavailable")
    assert r["retrieval_attempts"][1]["status"] == "received"
    passage, = r["passages"]
    path = passage["locator"]["xpath"]
    assert path.startswith("/pmc-articleset/article[1]/body[1]")
    node = ElementTree.fromstring(data).find("." + path.removeprefix("/pmc-articleset"))
    assert passage["text"] == " ".join("".join(node.itertext()).split())


def test_primary_license_failure_may_not_be_bypassed(monkeypatch):
    urls = fallback(monkeypatch, article(license_xml=""), b"unused")
    assert paper_text.read_paper("PMC1234", "marker")["status"] == "licence-not-supported"
    assert len(urls) == 1


def test_text_mining_uri_alone_is_insufficient(monkeypatch):
    license_xml = f'<license><ali:license_ref xmlns:ali="http://www.niso.org/schemas/ali/1.0/" specific-use="textmining">{URL}</ali:license_ref></license>'
    fallback(monkeypatch, 503, b"<pmc-articleset>" + article(license_xml=license_xml) + b"</pmc-articleset>")
    assert paper_text.read_paper("PMC1234", "marker")["status"] == "licence-not-supported"


@pytest.mark.parametrize("payload", [
    b"<pmc-articleset>" + article(identifier="PMC77") + b"</pmc-articleset>",
    b"<pmc-articleset>" + article() + article() + b"</pmc-articleset>",
    NCBI_MISSING.replace(b"1234", b"9876"), b"<pmc-articleset><error>Unknown</error></pmc-articleset>",
])
def test_unexpected_fallback_metadata_cannot_justify_absence_or_quotes(monkeypatch, payload):
    fallback(monkeypatch, TimeoutError(), payload)
    with pytest.raises(ValueError):
        paper_text.read_paper("PMC1234", "marker")


def test_missing_and_outage_stay_distinct(monkeypatch):
    fallback(monkeypatch, 404, NCBI_MISSING)
    r = paper_text.read_paper("PMC1234", "marker")
    assert r["status"] == "no-fulltext" and r["xml_sha256"] is None and r["provider"] is None
    assert [a["status"] for a in r["retrieval_attempts"]] == ["no-fulltext", "no-fulltext"]
    fallback(monkeypatch, TimeoutError(), NCBI_MISSING)
    with pytest.raises(OSError, match="Europe PMC.*NCBI PMC"):
        paper_text.read_paper("PMC1234", "marker")


def test_embedded_tables_keep_adjacent_prose_in_separate_replayable_spans(monkeypatch):
    body = """<body><sec><title>Results</title>
    <p>Marker <bold>benefit</bold> was measured.
    <table-wrap><caption><p>tableonly</p></caption><table><tr><td>Marker benefit</td></tr></table></table-wrap>
    Replication found <italic>no</italic> effect.</p></sec></body>"""
    data = article(body=body)
    provide(monkeypatch, data)
    node = ElementTree.fromstring(data).find("body/sec/p")
    normalized = " ".join("".join(node.itertext()).split())
    for terms, expected in [("marker benefit", "Marker benefit was measured."),
                            ("replication effect", "Replication found no effect.")]:
        r = paper_text.read_paper("PMC1234", terms)
        passage, = r["passages"]
        loc = passage["locator"]
        assert passage["text"] == expected == normalized[loc["start"]:loc["end"]]
        assert loc["paragraph_sha256"] == sha256(normalized.encode()).hexdigest()
    assert paper_text.read_paper("PMC1234", "tableonly")["status"] == "no-term-match"


def test_decimal_query_preserves_numeric_value(monkeypatch):
    body = "<body><p>A total of 80 observations were recorded.</p><p>The example ratio was 0.80.</p></body>"
    provide(monkeypatch, article(body=body))
    passages = paper_text.read_paper("PMC1234", "0.80")["passages"]
    assert [p["text"] for p in passages] == ["The example ratio was 0.80."]
    assert passages[0]["matched_terms"] == ["0.80"]
